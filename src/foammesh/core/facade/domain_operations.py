"""AF3 domain operation handlers — headless, Qt-free, delegating to services.

Every interactive domain action becomes a registered facade operation here.
Pure/filesystem operations (classify, mesh info, quality report, clean,
archive, recovery, native import/export, workflow transitions, history) execute
fully headlessly. Operations that drive an OpenFOAM/VTK utility resolve it
through an injectable capability registry and raise ``capability_unavailable``
when it is absent — the documented capability/failure-fixture path — so the
handler surface is complete and testable without a live OpenFOAM install.

No module here imports Qt or a view; the shared services under
``foammesh.core`` are already headless.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path

from foammesh.core.jobs import ExpectedArtifact, OperationContext, OperationSpec
from foammesh.openfoam.decomposition import DecompositionSettings
from foammesh.core.project import Event
from foammesh.core.mesh import detection_record  # RP13 #3

from .commands import Command
from .errors import (CapabilityUnavailableError, CheckOverBudgetError,
                     CheckUnavailableError, FacadeError,
                     PreconditionFailedError, ValidationFailedError,
                     unavailable_utility_text)
from .probe_cache import ProbeCache
from .results import OperationResult
from .session import CaseSession

logger = logging.getLogger(__name__)


def out_of_memory_message(name: str, *, noun: str = 'file') -> str:
    """What a geometry check that ran out of memory says (DP-1160)."""
    return (f'This {noun} ({name}) needs more memory to check than is '
            'available. Close other programs and try again, or simplify '
            'the surface first.')


def _to_payload(value) -> dict:
    if value is None:
        return {}
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    return {'value': str(value)}



def _stage_timeout(parameters, default):
    """The seconds a run may take; ``None`` is no limit. Plan 37 #7.

    ``timeout_seconds`` is the Preferences stage time limit every guided
    press sends; 0 there is "No limit", which the job manager reads as
    ``None`` -- never ``wait_for(..., 0)``, which would stop the run at once.
    """
    value = parameters.get('timeout_seconds', default)
    if value is None:
        return None
    value = float(value)
    return None if value <= 0 else value


def _gmsh_runner_path():
    from foammesh.core.gmsh.execution import runner_path

    return runner_path()


def _runner_log_tail(layout, limit: int = 400) -> str:
    """The last thing the runtime said before it stopped, in its own words."""
    try:
        text = layout.log.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return ' '.join(text.replace('\x00', '').split())[-limit:]


class TaskLockedError(FacadeError):
    """Plan 37 UF5: the edit would change a task that holds a published result."""
    code = 'task_locked'


class UnlockRefusedError(FacadeError):
    """Plan 37 UF5: an unlock or undo that changed nothing, and why
    (``details['reason']``: case_busy, not_locked, revision_conflict,
    insufficient_disk, undo_not_durable, undo_unavailable, undo_damaged)."""
    code = 'unlock_refused'


class RedistributeRefusedError(FacadeError):
    """Plan 37 UF17: a core-count change that left the live processor cases
    as they were, and why (``details['reason']``, one of
    :data:`foammesh.core.jobs.redistribute_transaction.REASONS`)."""
    code = 'redistribute_refused'


#: Signals worth naming when a runner dies of one. Anything else is reported
#: by number: the point is to say "the process was killed", not to teach the
#: user POSIX.
#: DP-33. Named because it is the one entry whose remedy differs from all the
#: others: a process killed from outside has not malfunctioned.
_SIGKILL = 9

_FATAL_SIGNALS = {
    4: ('SIGILL', 'an illegal instruction'),
    6: ('SIGABRT', 'an unhandled error inside Gmsh'),
    8: ('SIGFPE', 'an arithmetic fault'),
    9: ('SIGKILL', 'being killed from outside, usually by the machine '
                   'running out of memory'),
    11: ('SIGSEGV', 'a segmentation fault'),
}


def runner_crash_reason(returncode, *, threads: int = 0) -> str:
    """How the Gmsh process died if a signal killed it, or ``''``.

    DP-30. A crash and a refusal reach this application the same way -- no
    result file -- and were reported the same way, as "the Gmsh runner
    produced no result file". They are not the same thing and the user cannot
    do the same thing about them. A refusal is Gmsh declining a geometry,
    which is answered by changing the geometry or the sizing. A crash is the
    library dying: MEASURED twice in this plan, on surface algorithm 8 over a
    plain sphere (139, and the same sphere meshes under algorithms 6 and 11)
    and on `Mesh.HighOrderPassMax` (134, which is why that knob is not
    offered). Neither is answered by touching the geometry, and a user told to
    look at their geometry will keep looking.

    Both conventions are read. A shell -- which is what runs the meshing
    command inside WSL -- reports a signal death as 128 + N; Python's own
    `subprocess` reports it as -N. 128 exactly is not a signal and is left
    alone.

    Plan 35 CR5. The signal is read by the decoder both engines share
    (:func:`foammesh.core.run_result.exit_signal`); this keeps only the
    Gmsh-specific advice.
    """
    from foammesh.core.run_result import exit_signal
    number = exit_signal(returncode)
    if not number:
        return ''
    name, what = _FATAL_SIGNALS.get(number, ('', ''))
    named = f'{name}, {what}' if name else f'signal {number}'
    if number == _SIGKILL:
        # DP-33. SIGKILL is the one signal here that is not Gmsh's fault, and
        # the remedy is the opposite of the one every other signal wants. The
        # first version of this message named the cause correctly and then
        # appended the crash advice anyway, so a run killed for memory was
        # told to change its meshing algorithm -- which will not help, and
        # sends the user away from the one setting that will. MEASURED in the
        # `t3` leg: annulus_shell as STL, killed inside WSL, reported exactly
        # that way.
        #
        # DP-43. The remedy is right and the reassurance was not. This message
        # went on to promise that "nothing is broken and there is no defect to
        # report", and the very run it was written from disproves it: the
        # annulus_shell kill was MEASURED, later, as 168 non-manifold edges
        # sending `classifySurfaces` into an unbounded recursion that grew
        # 1.4 GB a minute for forty-seven minutes without meshing a single
        # element. Something was badly wrong with that geometry, and the
        # element count had nothing to do with it. A signal number says the
        # kernel reclaimed the memory; it cannot say what was filling it. So
        # the size advice stays -- it is the common cause and the first thing
        # to try -- and the claim that there is nothing to find does not.
        return ('The mesher was killed part-way through (SIGKILL), which on '
                'this route means it ran out of memory. Re-running unchanged '
                'will be killed again. Most often the mesh being asked for is '
                'simply larger than the memory available to it, so ask for '
                'fewer elements — a larger target element size is the first '
                'thing to raise, then any refinement or boundary-layer '
                'settings that multiply it — or give the Linux runtime more '
                'memory. If it is killed again at a size that should fit, the '
                'geometry is worth checking too: a surface that does not '
                'close can consume memory without meshing anything.')
    opening = (f'Gmsh itself crashed ({named}) part-way through, so it wrote '
               'no mesh and no reason. This is a defect in Gmsh rather than a '
               'refusal of your geometry, so re-running unchanged will crash '
               'again. ')
    # DP-56. The first version of this sentence sent the user to the surface
    # and volume algorithms, and an eleven-arm rerun of the crash it was
    # written from falsifies that: delaunay, hxt and frontal volumes and a
    # delaunay surface pass all still crash at eight threads, and the same job
    # at one thread refuses cleanly, naming the surface it could not mesh.
    if threads > 1:
        return opening + (
            f'The setting that decides it is the thread count, and this run '
            f'asked for {threads}. A single-threaded run of the job that '
            'produced this message stopped crashing and named the surface it '
            'could not mesh instead, which is something you can act on. Lower '
            'the thread count before changing anything else.')
    if threads == 1:
        return opening + (
            'This run was already single-threaded, which is the setting that '
            'stopped the crash everywhere it has been measured, so there is '
            'no thread count left to lower. Ask for a coarser mesh, and if it '
            'still crashes the job is worth reporting: a crash at one thread '
            'is not a shape this build has seen.')
    return opening + (
        'The setting measured to decide it is the thread count: a job that '
        'crashed at eight threads refused cleanly at one, naming the surface '
        'it could not mesh. Lower it before changing anything else.')


def retry_at_one_thread(layout, job, payload, *, attempt: int = 0) -> str:
    """Rewrite this run's job to a single thread, or ``''`` to leave it alone.

    DP-56. A crash is the one failure that carries no information: no mesh, no
    result file, no reason, just a signal number. MEASURED on leg t4's
    `drone_quadcopter` job over eleven arms -- nine at eight threads, all of
    them 139, against two at one thread, both of them a clean refusal naming
    `Invalid boundary mesh (overlapping facets) on surface 35`. The volume
    algorithm, the surface algorithm and the boundary layers were each varied
    across those arms and none of them changed the outcome; the thread count
    changed it every time.

    So the crash is retried rather than reported. The first attempt wrote no
    result and no mesh, so there is nothing in the run directory to clobber
    and the same one is reused. Returns the sentence to record when it has
    rewritten the job, so the caller can re-stamp the manifest and say on it
    that this mesh was not built the way the job originally asked.

    Deliberately not retried: a cancellation, which the user asked for; a
    SIGKILL, which is the machine reclaiming memory and whose remedy is a
    smaller mesh rather than fewer threads; and a job that already asked for
    one thread, which has nothing left to lower.

    Plan 35 CR6. The decision is the shared retry policy's
    (:func:`foammesh.core.jobs.retry.automatic_thread_retry`): once, and never
    on a run that is itself a retry (*attempt* above zero).
    """
    import json

    from foammesh.core.jobs.retry import automatic_thread_retry

    intent = job.get('intent')
    if not isinstance(intent, dict):
        return ''
    parallel = dict(intent.get('parallel') or {})
    try:
        threads = int(parallel.get('threads', 1) or 1)
    except (TypeError, ValueError):
        return ''
    ended = payload.get('job') or {}
    if not automatic_thread_retry(status=str(ended.get('status') or ''),
                                  returncode=ended.get('returncode'),
                                  threads=threads, attempt=attempt):
        return ''
    parallel.update({'threads': 1, 'effectiveVolumeThreads': 1,
                     'retriedFromThreads': threads})
    intent['parallel'] = parallel
    layout.job.write_text(
        json.dumps(job, indent=2, sort_keys=True) + chr(10), encoding='utf-8')
    return (f'Gmsh crashed at {threads} threads and the run was retried once '
            'at a single thread, which is the setting measured to decide it. '
            'This mesh, if there is one, was built single-threaded.')


def _read_runner_result(layout) -> dict:
    """The runner's own result, or a typed stand-in when it wrote none.

    DP-11. "the Gmsh runner produced no result file" was the whole message,
    and it reads as a Gmsh problem. It is not one when the runner never
    started: a WSL distribution that fails to come up writes
    ``HCS_E_CONNECTION_TIMEOUT`` into this run's log and exits, and eight runs
    of one sweep went into the fault register as per-model mesh refusals on
    the strength of that sentence. What the runtime actually said is one file
    away, so it is quoted here rather than left for the user to find.
    """
    if not layout.result.is_file():
        said = _runner_log_tail(layout)
        return {'status': 'failed',
                'error': (f'the Gmsh runner produced no result file. The '
                          f'runtime said: {said}' if said else
                          'the Gmsh runner produced no result file and wrote '
                          'nothing to its log, so the runtime it was asked '
                          'to start never ran')}
    try:
        return json.loads(layout.result.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return {'status': 'failed',
                'error': f'the Gmsh result file is unreadable: {error}'}


def _job_periodic_pairs(record) -> list:
    """Periodic pairs as derived, read back from the immutable job."""
    try:
        job = json.loads(record.layout.job.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    pairs = ((job.get('intent') or {}).get('periodic') or {}).get('pairs') or []
    names = job.get('surfaceNames') or {}
    scopes = job.get('scopeSurfaces') or {}

    def solver_name(token):
        indices = scopes.get(token) or []
        for index in indices:
            label = names.get(str(int(index) + 1))
            if label:
                return label
        return token

    return [dict(pair,
                 masterSolverName=solver_name(pair.get('masterScope')),
                 slaveSolverName=solver_name(pair.get('slaveScope')))
            for pair in pairs]


def _job_extrusion(record):
    """The section extrusion this run was launched with, or ``None``.

    FC-E. Read back from the immutable job rather than from the case's current
    settings, for the same reason the export settings are: a candidate may be
    published some time after it was made, and a run that meshed a section has
    to be published as a section however the case has been edited since. A job
    that never carried a dimensionality block is three-dimensional, which is
    what ``from_dict`` answers ``None`` to.
    """
    from foammesh.core.gmsh.publish import SectionExtrusion

    try:
        job = json.loads(record.layout.job.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return SectionExtrusion.from_dict(
        (job.get('intent') or {}).get('dimensionality'))


def _job_export_settings(record) -> dict:
    """The export intent this run was launched with, from its own job.

    Read back rather than recomputed. A run is judged against what it was
    asked to do, and the case's current export settings may have moved on
    since -- which is exactly the case Plan 31 CP-05 item 4 is about, where
    a candidate is accepted some time after it was made.
    """
    try:
        job = json.loads(record.layout.job.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return ((job.get('intent') or {}).get('export')) or {}


def _job_edge_categories(record) -> dict:
    """Patch name -> boundary category for a section's named curves.

    DP-675. The prepared geometry types faces, and a section's patches are
    its curves, so a curve the user named is typed from its name exactly as a
    prepared patch is (``inlet_left`` is an inlet); any other name is a wall,
    which is what an unnamed ``edge_<tag>`` publishes as.
    """
    from foammesh.db.configurations_schema import BoundaryCategory

    try:
        job = json.loads(record.layout.job.read_text(encoding='utf-8'))
    except (OSError, ValueError, AttributeError):
        return {}
    intent = job.get('intent') if isinstance(job, dict) else None
    names = ((intent or {}).get('dimensionality') or {}).get('edgeNames') or {}
    known = {item.value for item in BoundaryCategory}
    return {str(name): _category_for_name(str(name).lower(), known, 'wall')
            for name in names}


def _job_is_planar(record) -> bool:
    """Whether this run meshed a 2D or axisymmetric section, from its job.

    DP-674. A section's cells are faces, so the run is judged by those.
    """
    try:
        job = json.loads(record.layout.job.read_text(encoding='utf-8'))
    except (OSError, ValueError, AttributeError):
        return False
    intent = job.get('intent') if isinstance(job, dict) else None
    return bool(((intent or {}).get('dimensionality') or {}).get('planar'))


def _job_configured_tasks(record) -> frozenset:
    """The optional tasks whose configuration this run actually consumed.

    DP-228. Read back from the immutable job for the same reason the export
    settings above are: the question is what the run was asked to build, and
    the case may have been edited since. It is read from the job rather than
    from the workflow graph because the graph cannot answer it -- a page save
    refused as locked by prerequisites leaves its task READY, and DP-35 grades
    a READY optional task as unused.
    """
    from foammesh.core.gmsh.plan_derivation import configured_tasks

    try:
        job = json.loads(record.layout.job.read_text(encoding='utf-8'))
    except (OSError, ValueError, AttributeError):
        return frozenset()
    return configured_tasks(job.get('intent') if isinstance(job, dict) else None)


def _require_every_prepared_source(prepared, job) -> None:
    """Refuse a Gmsh run whose job does not carry every prepared source.

    WP-01 F-35. A job used to carry ``sources[0]`` and nothing else, so a case
    assembled from several imported files meshed the first file and published
    the result as if it were the whole model. The check is here rather than in
    the runner because a mesh of the wrong geometry must not start.
    """
    # The job's paths are in the runtime's namespace (a WSL job carries POSIX
    # paths on a Windows host), so the basename is compared, not the path.
    carried = {str(item).replace('\\', '/').rsplit('/', 1)[-1]
               for item in (job.get('geometry') or ())}
    missing = [name for name
               in (prepared.manifest.get('sources') or ())
               if isinstance(name, dict) and name.get('prepared_name')
               and str(name['prepared_name']) not in carried]
    if missing:
        raise ValidationFailedError(
            'the Gmsh job does not carry every prepared geometry source: '
            + ', '.join(sorted(str(item['prepared_name']) for item in missing))
            + '. Re-prepare the geometry before running.')


def _prepared_bbox(prepared):
    """Bounds of the prepared geometry, or None when it records none.

    DP-614 (field audit 0924 gmsh-sizing D1). Neither the prepared result nor
    its reference has a bounds attribute, so this always returned None and
    the job never had a box to derive a size from. The manifest records each
    source's bounds (VTK order, metres); their union is the geometry's.
    """
    for source in (prepared, getattr(prepared, 'reference', None)):
        if source is None:
            continue
        for attribute in ('bounds', 'bbox', 'bounding_box'):
            value = getattr(source, attribute, None)
            if value is not None:
                return value
    from foammesh.core.geometry.bbox import BBox

    boxes = []
    manifest = getattr(prepared, 'manifest', None) or {}
    for record in manifest.get('sources') or ():
        bounds = record.get('bbox') if isinstance(record, dict) else None
        try:
            if bounds is not None and len(bounds) == 6:
                boxes.append(BBox.from_bounds([float(item) for item in bounds]))
        except (TypeError, ValueError):
            continue
    return BBox.union(boxes)


def _gmsh_patch_categories(prepared) -> dict:
    """Solver patch name -> prepared boundary category.

    The same categories snappy publication uses, so a Gmsh mesh and a snappy
    mesh of the same geometry type their patches identically.
    """
    manifest = {}
    for source in (prepared, getattr(prepared, 'reference', None)):
        candidate = getattr(source, 'group_manifest', None)
        if candidate:
            manifest = candidate
            break
    categories = {}
    for group in manifest.get('groups') or ():
        if not isinstance(group, dict):
            continue
        name = str(group.get('solver_name') or group.get('name') or '').strip()
        if name:
            categories[name] = str(group.get('category') or 'wall')
    return categories


def _gmsh_patch_identities(prepared) -> dict:
    """Solver patch name -> prepared ``patch_uuid``.

    Carried into publication so the identity sidecar can be built from what was
    actually published rather than reconstructed afterwards.
    """
    manifest = {}
    for source in (prepared, getattr(prepared, 'reference', None)):
        candidate = getattr(source, 'group_manifest', None)
        if candidate:
            manifest = candidate
            break
    identities = {}
    for group in manifest.get('groups') or ():
        if not isinstance(group, dict):
            continue
        name = str(group.get('solver_name') or '').strip()
        patch_uuid = str(group.get('patch_uuid') or '').strip()
        if name and patch_uuid:
            identities[name] = patch_uuid
    return identities


def published_boundary_gap(publication) -> str:
    """Why this published mesh's boundary is incomplete, or ``''``.

    CP-05 item 3 requires boundary completeness alongside generation and
    structural validity before a native mesh is accepted. Nothing is
    re-derived to answer it: ``publication`` is the block the publisher just
    built, naming the patches that would reach ``constant/polyMesh/boundary``
    and, in its ``export`` sub-block, how many faces each of them carries.

    MEASURED across the 42 real Gmsh run manifests under ``test_cases/``:
    every run that published a mesh has at least one patch and no empty one,
    and the only run with no patches at all is ``finned_tube``, whose status
    is ``publication_failed`` and which the "built no mesh" precondition
    already refuses. So this rule costs no case in the corpus. What it buys
    is what it prevents: a mesh whose boundary is missing or hollow is one
    the solver opens and cannot use, and an acceptance recorded against it
    says a person looked at a boundary that was not there.

    A zero-face patch is refused rather than warned about because it is a name
    with nothing behind it -- the user asked for a boundary, the mesher
    produced none, and OpenFOAM will read the empty entry as a patch it must
    apply conditions to.
    """
    record = dict(publication or {})
    names = [str(name) for name in (record.get('patches') or ())]
    if not names:
        return ('this mesh publishes no boundary patches, so there is no '
                'boundary to accept: re-mesh with the surfaces named, or '
                'check that the geometry the run was meshed from still '
                'carries them')
    unnamed = [index for index, name in enumerate(names) if not name.strip()]
    if unnamed:
        return (f'{len(unnamed)} of this mesh\'s {len(names)} boundary '
                'patches reached the mesh without a name, so nothing can be '
                'applied to them; re-mesh with every surface named')
    faces = {str(entry.get('name') or ''): entry.get('nFaces')
             for entry in ((record.get('export') or {}).get('patches') or ())
             if isinstance(entry, dict)}
    empty = sorted(name for name in names
                   if name in faces and not faces[name])
    if empty:
        return ('these boundary patches carry no faces, so they name a '
                'boundary the mesh does not have: ' + ', '.join(empty)
                + '. Re-mesh, or remove the surfaces that produced nothing')
    return ''


def checked_boundary_gap(verdict) -> str:
    """Why this checked mesh's boundary is incomplete, or ``''``.

    The snappy counterpart of :func:`published_boundary_gap`, and a narrower
    check than it, because the two routes do not record the same thing. The
    Gmsh route keeps the published patch list and each patch's face count, so
    it can see a patch that is a name with nothing behind it. checkMesh's
    report keeps only how many patches there were, so what is visible here is
    the one case that count can show: a mesh with no boundary at all.

    That case is worth refusing on its own. This path is reached only when
    checkMesh has already refused the mesh and the user has asked to keep it
    anyway, and a waiver is a statement that a person looked at the numbers
    and judged them usable. There are no numbers to look at for a boundary
    that does not exist, and no solver will open the case.
    """
    result = (verdict or {})
    if not isinstance(result, dict):
        return ''
    patches = result.get('patches')
    if not isinstance(patches, int) or patches > 0:
        # `None` is "checkMesh did not report it", which is not evidence of
        # absence; only a reported zero is.
        return ''
    return ('checkMesh found no boundary patches in this mesh, so there is no '
            'boundary to accept and no waiver can make it usable: re-mesh '
            'with the surfaces named')


def non_conformal_couples(manifest: Mapping) -> tuple[dict, ...]:
    """The patch pairs a group manifest asks to be coupled non-conformally.

    F-13. One resolver for both routes: the pipeline schedules a
    ``createNonConformalCouples`` step per pair, and the standalone
    ``mesh.interface.non_conformal`` operation runs the same list. Scope ids
    are prepared ``patch_uuid`` values, which is what the manifest's groups
    are keyed on, so a pair naming a surface the revision does not contain is
    refused rather than skipped.
    """
    groups = {
        str(item.get('patch_uuid') or ''): item
        for item in (manifest.get('groups') or ())
        if isinstance(item, dict)}
    resolved: list[dict] = []
    for pair in (manifest.get('interface_pairs') or ()):
        if not isinstance(pair, dict):
            continue
        if str(pair.get('coupling')) != 'non_conformal':
            continue
        pair_id = str(pair.get('pair_id') or '').strip()
        master = groups.get(str(pair.get('master_scope_id') or ''))
        slave = groups.get(str(pair.get('slave_scope_id') or ''))
        if master is None or slave is None:
            raise ValidationFailedError(
                f'non-conformal pair {pair_id or "<unnamed>"} references '
                'a missing prepared surface')
        master_name = str(master.get('solver_name') or '').strip()
        slave_name = str(slave.get('solver_name') or '').strip()
        if (not master_name or not slave_name
                or master_name == slave_name):
            raise ValidationFailedError(
                f'non-conformal pair {pair_id or "<unnamed>"} requires '
                'two distinct solver patch names')
        resolved.append({
            'pair_id': pair_id or str(len(resolved)),
            'master_patch': master_name,
            'slave_patch': slave_name,
        })
    return tuple(resolved)


def _meshing_intent(configuration: dict, native_section: str = ''):
    """Normalize persisted camelCase state into the engine contract.

    ``native_section`` names the configuration section holding the selected
    engine's own controls, so this stays engine-agnostic.
    """
    from foammesh.core.engine import MeshingIntent

    mesh = configuration.get('mesh') or {}
    native = (configuration.get(native_section) or {}) if native_section else {}

    def rows(value):
        if isinstance(value, dict):
            return tuple(dict(item, control_id=str(key)) if isinstance(item, dict) else {}
                         for key, item in sorted(value.items(), key=lambda pair: str(pair[0])))
        if isinstance(value, list):
            return tuple(dict(item) for item in value if isinstance(item, dict))
        return ()

    # Plan 30 WP-09 (F-23). The six scalar sizing fields used to be read from
    # `mesh/intent`; that block is gone from the schema because no engine ever
    # consumed them. The contract dataclass keeps its defaults, so the shape
    # of the request is unchanged -- what changed is that the numbers no
    # longer come from a page that implied they governed the mesh.
    return MeshingIntent(
        local_sizes=rows(native.get('sizeFields')),
        edge_controls=rows(native.get('curveControls')),
        zone_controls=rows(native.get('volumeControls')),
        interface_pairs=rows(configuration.get('interfacePairs')),
        layer_controls=rows(native.get('boundaryLayers')),
        # Plan 29 WP8: the target solver travels with the native section so a
        # plan request carries everything the engine derivation needs. Gmsh
        # gates element order on it, and the request is all that reaches
        # `derive_job_intent`.
        native=({native_section: dict(native),
                 'targetSolver': mesh.get('targetSolver') or 'unselected'}
                if native_section else {}),
    )


def _mpi_launch_options(registry, ranks: int, *, host=None):
    """``(options, warning)`` for an ``mpirun`` of *ranks* processes.

    DP-1263. Open MPI 4.1.2 (OpenFOAM 13's) gives a host one slot per
    physical core and refuses a run that asks for more (MEASURED in
    OpenFOAM13Runtime, recorded in ``resources.py``: 16 physical cores, 32
    logical, a 32-rank ``mpirun -np`` refused outright). The launcher clamps
    ranks to the *logical* cores, so 17..32 ranks passed the clamp and were
    refused at launch. A count the user typed above the physical cores
    is theirs to ask for -- there is no fixed limit, only RAM -- so the launch
    passes ``--oversubscribe`` and the run says the ranks share cores, rather
    than failing at launch. The host is the cached reading; nothing is probed
    here. ``warning`` is ``''`` when no flag was needed.
    """
    options = tuple(registry.mpi_options()) if (
        ranks > 1 and hasattr(registry, 'mpi_options')) else ()
    if ranks <= 1:
        return options, ''
    if host is None:
        from foammesh.core.execution.resources import meshing_host
        try:
            host = meshing_host()
        except Exception:  # noqa: BLE001 - no reading, no flag
            return options, ''
    slots = int(getattr(host, 'physical_cores', 0)
                or getattr(host, 'logical_cores', 0) or 0)
    if not slots or ranks <= slots:
        return options, ''
    if '--oversubscribe' not in options:
        options = (*options, '--oversubscribe')
    warning = (f'{ranks} ranks asked for on a host with {slots} physical '
               'cores: Open MPI is started with --oversubscribe, so ranks '
               'share cores and each runs slower.')
    logger.warning('DP-1263: %s', warning)
    return options, warning


def _resource_policy(configuration: dict) -> dict:
    execution = ((configuration.get('mesh') or {}).get('execution') or {})
    maximum = int(execution.get('maxCpuCores') or 0)
    memory = int(execution.get('maxMemoryBytes') or 0)
    mode = str(execution.get('mode', 'auto')).split('.')[-1].lower()
    return {
        'mode': mode,
        'max_cpu_cores': maximum or None,
        'max_memory_bytes': memory or None,
        'allow_distributed': bool(execution.get('allowDistributed', False)),
        'preferred_backend': str(
            execution.get('preferredBackend') or 'local'),
    }


def _ensure_openfoam_control_dict(case_path: Path) -> tuple[Path, bool]:
    """Create the minimal Foundation-v13 Time database for a mesh-only case.

    An engine-first project legitimately has no solver dictionaries before its
    first mesh is published.  ``checkMesh`` still requires ``controlDict`` to
    construct the OpenFOAM Time database, so publication owns this small,
    deterministic bootstrap dictionary.
    """
    from foammesh.openfoam.dict_format import format_dictionary_file
    case_path = Path(case_path)
    destination = case_path / 'system' / 'controlDict'
    if destination.is_file():
        return destination, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = format_dictionary_file('controlDict', {
        'application': 'checkMesh',
        'startFrom': 'startTime',
        'startTime': 0,
        'stopAt': 'endTime',
        'endTime': 1,
        'deltaT': 1,
        'writeControl': 'timeStep',
        'writeInterval': 1,
        'purgeWrite': 0,
        'writeFormat': 'ascii',
        'writePrecision': 8,
        'writeCompression': 'off',
        'timeFormat': 'general',
        'timePrecision': 6,
        'runTimeModifiable': True,
    })
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(destination)
    return destination, True


def _unit_bbox_for_pipeline():
    from foammesh.core.geometry import BBox
    return BBox(0, 1, 0, 1, 0, 1)


def _prepared_fluid_seed(db, geometry_entries: list[dict]):
    regions = list(db.getElements('region').values())
    if regions:
        return regions[0].vector('point')
    for entry in geometry_entries:
        findings = (entry.get('diagnostics') or {}).get('findings', ())
        finding = next(
            (item for item in findings
             if item.get('kind') == 'fluid_seed' and item.get('locations')),
            None)
        if finding is not None:
            return finding['locations'][0]
    return None


def _category_for_name(name: str, known: set, default: str) -> str:
    """Read a category out of a patch name, not only out of the whole name.

    R74. The test used to be ``name in known`` -- an exact match against the
    seven category values -- so only a patch called exactly ``inlet`` or
    ``wall`` was ever classified. MEASURED on a five-patch tee named
    ``inlet``, ``outlet_top``, ``outlet_branch``, ``wall_main``,
    ``wall_branch``: the published constant/polyMesh/boundary read
    ``outlet_top_85bb20af type wall`` and ``outlet_branch_5be4e763 type
    wall``, because both fell through to the wall default; the two walls were
    right only by coincidence. Qualifying a name is the ordinary way to have
    several patches of one kind, so the leading word decides, and a two-word
    category such as ``far_field`` still matches as a whole.
    """
    if name in known:
        return name
    tokens = [token for token in re.split(r'[^a-z0-9]+', name) if token]
    for size in (2, 1):
        # Two words first: ``far_field_west`` is a far field, not a ``far``.
        head = '_'.join(tokens[:size])
        if head and head in known:
            return head
    return default


def _merge_surfaces(surfaces: list) -> tuple:
    """One ``(vertices, triangles)`` covering several face subsets (R179).

    A merged boundary is several faces of one body, and it has to be measured
    against all of them at once -- measured face by face, the sampled points
    of one face would be scored against a reference that stops at its edge.
    Subsets cut from the same reference share their vertex array, so the usual
    case is a triangle concatenation; the index offsetting is there for the
    case where they do not.
    """
    import numpy as np

    if len(surfaces) == 1:
        return surfaces[0]
    if all(surface[0] is surfaces[0][0] for surface in surfaces):
        return (surfaces[0][0],
                np.concatenate([surface[1] for surface in surfaces]))
    vertices = []
    triangles = []
    offset = 0
    for block, faces in surfaces:
        vertices.append(block)
        triangles.append(np.asarray(faces) + offset)
        offset += len(block)
    return np.concatenate(vertices), np.concatenate(triangles)


def _prepared_boundary_categories(db, geometry_entries: list[dict]) -> dict:
    """Derive a patch category per prepared patch from current project state.

    Without this every prepared patch stays ``unclassified`` and publishes as a
    plain OpenFOAM ``patch``, so a published mesh would reach the solver with
    no walls. A patch named after a category adopts it; everything else takes
    the configured geometry-preparation default.
    """
    from foammesh.db.configurations_schema import BoundaryCategory
    known = {item.value for item in BoundaryCategory}
    try:
        default = str(db.getValue(
            'geometryPreparation/defaultBoundaryCategory')).split('.')[-1]
    except Exception:
        default = BoundaryCategory.UNCLASSIFIED.value
    default = default.lower()
    if default not in known:
        default = BoundaryCategory.UNCLASSIFIED.value
    categories = {}
    for entry in geometry_entries:
        # Mirror how the prepared store enumerates patches: a CAD import
        # carries one patch per face, an STL one patch per geometry. Reading
        # only the geometry level would leave every CAD face uncategorised.
        patches = entry.get('patches') or ({
            'patch_uuid': entry.get('patch_uuid'),
            'name': entry.get('name') or entry.get('geometry_id'),
        },)
        for patch in patches:
            patch_uuid = str(patch.get('patch_uuid') or '').strip()
            if not patch_uuid:
                continue
            name = str(patch.get('name') or entry.get('name') or '')
            name = name.strip().lower().replace(' ', '_')
            categories[patch_uuid] = _category_for_name(name, known, default)
    return categories


#: How long a runtime verdict stays believable. Long enough that opening a
#: dialog never pays for a cold WSL boot twice; short enough that installing
#: the missing runtime is noticed without restarting the window.
_PROBE_TTL_SECONDS = 300.0
#: Plan 37 UF5: how long a worker check waits for a mesher to release the
#: case before it is refused as busy (retryable).
WORKER_LEASE_WAIT_SECONDS = 2.0

#: The engine tasks whose questions `3. Preparation` asks, and which therefore
#: have no row of their own to reopen by hand (Plan 32 section 4.4).
#:
#: The desktop names the same set in
#: `foammesh/view/main_window/meshing_method_branch.py` as `HOSTED_TASKS`,
#: because that is where a row is decided; core cannot import the view, and
#: the view importing this would make the facade a dependency of the outline.
#: Two names for one set is a thing to keep in step, so it is written down
#: here rather than derived: a task missing from this list is a task an unlock
#: leaves accepted on geometry that no longer exists.
PREPARATION_HOSTED_TASKS = frozenset({'gmsh.describe_geometry'})


def _publish_prepared_catalogue(session: CaseSession) -> None:
    """Republish the prepared scope catalogue for a revision that just landed.

    Plan 33 CURVE-01/FIELD-02. Preparing the geometry is the event that makes
    every scope picker in the application wrong, and nothing told the picker:
    it read the catalogue once, when its panel was constructed. The catalogue
    is written here, by the operation that changed the geometry, so a panel
    that is older than the revision still offers the revision.

    Advisory: a revision that materialized is a revision, whether or not the
    catalogue could be rebuilt from it.
    """
    from foammesh.core.selection.service import notify_prepared_case
    try:
        notify_prepared_case(session.case_path, session.state.db)
    except (AttributeError, FileNotFoundError, OSError, ValueError):
        pass


def _refuse_unresolved_scopes(session, prepared) -> None:
    """Stop a run whose controls name geometry this case no longer has.

    Plan 33 W-G1 (FIELD-06, CURVE-04). The refusal names each row and says
    ``Needs selection``, which is the same words the row reads on its page,
    so what the user is told to fix is where they go to fix it.

    Silence here is the whole point: a control the user enabled and a mesh
    that does not carry it are indistinguishable once the run has finished,
    and the run is what they will trust.
    """
    from foammesh.core.gmsh.execution import _native_section
    from foammesh.core.gmsh.size_fields import unresolved_scopes

    reference = getattr(prepared, 'reference', prepared)
    complaints = unresolved_scopes(_native_section(session.state.db),
                                   reference)
    if not complaints:
        return
    raise ValidationFailedError(
        'these enabled controls name geometry this case no longer has, so '
        'the mesh would silently be missing them:\n  '
        + '\n  '.join(complaints)
        + '\nChoose the geometry each one means, or turn it off, and run '
          'again.',
        details={'unresolved_scopes': list(complaints)})


def _meshes_a_section(db) -> bool:
    """Whether this case asks Gmsh for a 2D or axisymmetric section.

    DP-673 (field audit 0924 gmsh-generate-export D2). A section is one planar
    face and bounds no volume by design, so the run seams' "the surfaces
    cannot bound a volume" refusal is the wrong question for it: the runner
    meshes it with ``generate(2)`` and the publisher makes the one cell of
    thickness. The engine is asked: one that meshes no sections has no
    ``meshes_a_section`` and is held to the volume rule.
    """
    from foammesh.core.engine import resolve_engine
    try:
        engine = resolve_engine(db)
    except Exception:                                        # noqa: BLE001
        return False
    asks = getattr(engine, 'meshes_a_section', None)
    return bool(asks(db)) if callable(asks) else False


def _ensure_prepared_geometry(session, *, producer: str,
                              require_domain: bool = False):
    """The prepared geometry, prepared with defaults if there is none.

    F-12. The one route to a prepared revision, so no engine can be readier
    than another on the same imported case. ``None`` only when nothing has
    been imported yet.

    Plan 31 CP-04 (C31-06). *require_domain* is what the run seams pass. A
    surface that cannot bound a volume is not a domain, and preparing it
    "as_is" used to hand it to both meshers with the user told nothing:
    Gmsh refused by name, but only after WSL had booted, and snappyHexMesh
    did not refuse at all. Planning and previewing still pass it as false --
    a user is allowed to look at a broken surface and decide what to do
    about it. Only starting a mesh refuses.
    """
    from foammesh.core.geometry import PreparedGeometryStore
    from foammesh.core.geometry.prepared import (
        domain_refusal, ensure_prepared, prepare_readiness,
    )

    store = PreparedGeometryStore(session.case_path)
    # DP-860. The engine is named so a surface only snappy can mesh (regions
    # sharing a face in one STL) is refused on the Gmsh route before the job.
    from foammesh.core.engine import configured_engine_id
    engine_id = configured_engine_id(session.state.db)
    readiness = prepare_readiness(store, engine_id=engine_id)
    if not readiness['can_prepare']:
        return None
    if (require_domain and not readiness['prepared']
            and readiness.get('bounds_domain') is False):
        raise ValidationFailedError(
            domain_refusal(store, engine_id=engine_id) or (
                'the imported surfaces cannot bound a volume, so there is no '
                'domain to mesh'),
            details={'geometry_topology': readiness.get('topology'),
                     'producer': producer})
    if readiness['prepared']:
        return store.current()
    # Only a case that is about to be prepared pays for reading the project
    # state the preparation needs.
    entries = store.source.entries()
    return ensure_prepared(
        store, producer=producer,
        boundary_categories=_prepared_boundary_categories(
            session.state.db, entries),
        fluid_seed=_prepared_fluid_seed(session.state.db, entries))


#: DP-1230. Which case-root mesh the processor cases were split from;
#: see `DomainOperations._stale_decomposition`.
DECOMPOSITION_RECORD = 'foammesh/decomposition.json'


class DomainOperations:
    """Registrar + handlers for the full domain operation surface."""

    def __init__(self, *, capabilities=None):
        self._capabilities = capabilities
        self._geometry_prepare_tokens = {}
        #: One probe per engine at a time, and its answer once it lands.
        #: A cold Gmsh probe boots WSL and takes about half a minute; the
        #: window used to ask for it after every commit and wait for it in
        #: the write queue. Now the first caller starts it, every caller
        #: waits only as long as it said it would, and the answer is reused.
        #: Keyed by ``(engine_id, target_solver)``: Gmsh needs ``checkMesh``
        #: only when OpenFOAM is the target, so one engine has two verdicts
        #: and the old engine-only key handed the wrong one back after a
        #: target switch (F-40). Entries expire so a runtime installed while
        #: the window is open is noticed without a restart.
        self._probe_results = ProbeCache(_PROBE_TTL_SECONDS)
        #: The WSL profile probe behind ``openfoam.runtime.diagnostics``,
        #: which ran on the event loop and blocked every dialog behind it.
        self._runtime_diagnostics = ProbeCache(_PROBE_TTL_SECONDS)
        #: Plan 31 CP-07 item 4. Which decomposition libraries the selected
        #: runtime actually has, behind the same cache: the answer decides
        #: what the Execution page offers, and asking costs a WSL boot.
        self._decomposition_probe = ProbeCache(_PROBE_TTL_SECONDS)

    def forget_runtime_probes(self) -> None:
        """Plan 35 CR6: the runtime came back; its cached verdicts are stale."""
        for cache in (self._probe_results, self._runtime_diagnostics,
                      self._decomposition_probe):
            cache.clear()

    # -- registration ------------------------------------------------------ #

    #: Plan 37 UF5 DP-1063. Operations that rewrite the geometry artifacts
    #: on disk (not a working copy the lock could diff), refused up front on a
    #: case whose mesh was made from that geometry. A ``preview`` asks only.
    _GEOMETRY_REWRITES = frozenset({
        'geometry.import', 'geometry.split', 'geometry.combine',
        'geometry.transform', 'geometry.repair', 'geometry.repair.apply',
        'geometry.repair.rollback', 'geometry.wrap.apply',
        'geometry.patches.merge', 'geometry.patches.split',
        'geometry.patches.split_by_angle', 'geometry.split_interfaces',
        'geometry.prepared.create', 'geometry.prepared.select',
    })

    #: Plan 37 UF5. The exports whose provenance is recorded, so an unlock
    #: can say which of them describe the mesh it discards.
    _PROVENANCE_EXPORTS = frozenset({
        'case.export.native', 'case.export.vtk', 'case.export.cgns',
        'case.export.gmsh', 'case.export.su2', 'case.export.med',
        'case.export.unv', 'case.export.fluent',
        'mesh.canonical.export.openfoam',
    })

    def register_all(self, register) -> None:
        for operation, handler in self.handlers().items():
            if operation in self._GEOMETRY_REWRITES:
                handler = self._refusing_on_locked_geometry(operation, handler)
            if operation in self._PROVENANCE_EXPORTS:
                handler = self._recording_export_provenance(operation, handler)
            register(operation, handler)

    @staticmethod
    def _recording_export_provenance(operation: str, handler):
        """Note which mesh an export was written from, after it succeeded."""
        from functools import wraps

        def note(session, command, result):
            destination = (command.parameters or {}).get('destination')
            if (getattr(result, 'status', '') == 'accepted'
                    and isinstance(destination, str) and destination):
                from foammesh.core.jobs import export_provenance
                try:
                    export_provenance.record(session.case_path, destination,
                                             operation=operation)
                except (OSError, ValueError):
                    logger.warning('export provenance not recorded for %s',
                                   destination, exc_info=True)
            return result

        if inspect.iscoroutinefunction(handler):
            @wraps(handler)
            async def recorded(session, command, *args, **kwargs):
                return note(session, command,
                            await handler(session, command, *args, **kwargs))
        else:
            @wraps(handler)
            def recorded(session, command, *args, **kwargs):
                result = handler(session, command, *args, **kwargs)
                if inspect.isawaitable(result):
                    async def later():
                        return note(session, command, await result)
                    return later()
                return note(session, command, result)
        return recorded

    @staticmethod
    def _refusing_on_locked_geometry(operation: str, handler):
        from functools import wraps

        def refuse(session, command) -> None:
            if not (command.parameters or {}).get('preview'):
                from .facade import refuse_locked_items
                refuse_locked_items(session, 'geometry', operation=operation)

        if inspect.iscoroutinefunction(handler):
            @wraps(handler)
            async def guarded(session, command, *args, **kwargs):
                refuse(session, command)
                return await handler(session, command, *args, **kwargs)
        else:
            @wraps(handler)
            def guarded(session, command, *args, **kwargs):
                refuse(session, command)
                return handler(session, command, *args, **kwargs)
        return guarded

    def handlers(self) -> dict:
        table = {
            # Slice 1: lifecycle / persistence / history
            'case.classify': self._classify,
            'case.save': self._save,
            'case.copy': self._copy,
            'case.archive': self._archive,
            'case.clean.preview': self._clean_preview,
            'case.clean': self._clean,
            'case.parallel.redistribute': self._parallel_redistribute,
            'client_shell.terminal': self._launch_terminal,
            'artifact.stage.clear': self._stage_clear,
            'history.query': self._history_query,
            # Slice 2: geometry
            'geometry.import': self._geometry_import,
            'geometry.diagnostics': self._geometry_diagnostics,
            'geometry.readiness': self._geometry_readiness,
            'geometry.fluid_seed.suggest': self._geometry_fluid_seed_suggest,
            'geometry.fluid_seed.check': self._geometry_fluid_seed_check,
            'geometry.fluid_regions.detect': self._geometry_fluid_regions_detect,
            'geometry.fluid_regions.apply': self._geometry_fluid_regions_apply,
            'geometry.fluid_regions.offer': self._geometry_fluid_regions_offer,
            'geometry.fluid_regions.seeds': self._geometry_fluid_regions_seeds,
            'quality.tolerance.get': self._quality_tolerance_get,
            'quality.tolerance.set': self._quality_tolerance_set,
            'geometry.preparation.decide': self._geometry_preparation_decide,
            'geometry.classify': self._geometry_classify,
            'geometry.split': self._geometry_split,
            'geometry.combine': self._geometry_combine,
            'geometry.transform': self._geometry_transform,
            'geometry.repair': self._geometry_repair,
            'geometry.repair.suggest': self._geometry_repair_suggest,
            'geometry.repair.preview': self._geometry_repair_preview,
            'geometry.repair.apply': self._geometry_repair_apply,
            'geometry.repair.rollback': self._geometry_repair_rollback,
            'geometry.wrap.preview': self._geometry_wrap_preview,
            'geometry.wrap.estimate': self._geometry_wrap_estimate,
            'geometry.wrap.apply': self._geometry_wrap_apply,
            'geometry.rename': self._geometry_rename,
            'geometry.patches.list': self._geometry_patches_list,
            'geometry.patches.rename': self._geometry_patches_rename,
            'geometry.patches.merge': self._geometry_patches_merge,
            'geometry.patches.split': self._geometry_patches_split,
            'geometry.patches.split_by_angle': self._geometry_patches_split_by_angle,
            'geometry.split_interfaces': self._geometry_split_interfaces,
            'geometry.prepare.cancel': self._geometry_prepare_cancel,
            'geometry.prepared.create': self._geometry_prepared_create,
            'geometry.prepared.load': self._geometry_prepared_load,
            'geometry.prepared.current': self._geometry_prepared_current,
            'geometry.prepared.select': self._geometry_prepared_select,
            'geometry.prepared.discard': self._geometry_prepared_discard,
            # Slice 4: workflow
            'mesh.engine.list': self._mesh_engine_list,
            'mesh.target_solver.get': self._mesh_target_solver_get,
            'mesh.target_solver.set': self._mesh_target_solver_set,
            'mesh.engine.probe': self._mesh_engine_probe,
            'openfoam.runtime.diagnostics': self._openfoam_runtime_diagnostics,
            'mesh.execution.decomposition_methods':
                self._decomposition_methods,
            'mesh.execution.plan': self._execution_plan,
            'mesh.engine.workflow': self._mesh_engine_workflow,
            'mesh.engine.select': self._mesh_engine_select,
            'mesh.plan.derive': self._mesh_plan_derive,
            'mesh.gmsh.run': self._mesh_gmsh_run,
            'mesh.gmsh.runs': self._mesh_gmsh_runs,
            'mesh.workflow.task_state': self._mesh_workflow_task_state,
            'mesh.workflow.task_page': self._mesh_workflow_task_page,
            'mesh.workflow.task_transition': self._mesh_workflow_task_transition,
            'mesh.workflow.unlock_preview': self._mesh_workflow_unlock_preview,
            'mesh.workflow.unlock': self._mesh_workflow_unlock,
            'mesh.workflow.undo_unlock_preview':
                self._mesh_workflow_undo_unlock_preview,
            'mesh.workflow.undo_unlock': self._mesh_workflow_undo_unlock,
            'mesh.redistribute.preview': self._mesh_redistribute_preview,
            'mesh.redistribute': self._mesh_redistribute,
            'workflow.status': self._workflow_status,
            'workflow.start_authored': self._workflow_start_authored,
            'workflow.return_to_external': self._workflow_return_to_external,
            'workflow.generate_dictionaries': self._workflow_generate_dictionaries,
            'workflow.effective_dictionaries':
                self._workflow_effective_dictionaries,
            'workflow.run_stage': self._workflow_run_stage,
            'workflow.reset_stage': self._workflow_reset_stage,
            'workflow.run_pipeline': self._workflow_run_pipeline,
            # Slice 5: mesh info / QA / transforms / repair / recovery
            'mesh.info': self._mesh_info,
            'mesh.canonical.info': self._mesh_canonical_info,
            'mesh.canonical.validate': self._mesh_canonical_validate,
            'mesh.canonical.export.openfoam': self._mesh_canonical_export_openfoam,
            'quality.canonical': self._quality_canonical,
            'quality.canonical.failed_set.export': self._quality_canonical_failed_set_export,
            'mesh.canonical.selection_capabilities': self._canonical_selection_capabilities,
            'mesh.check': self._mesh_check,
            'quality.su2_readiness': self._quality_su2_readiness,
            'mesh.reconstruct': self._mesh_reconstruct,
            'mesh.feature_edges': self._mesh_feature_edges,
            'mesh.layer_coverage': self._mesh_layer_coverage,
            'quality.cell_fields': self._worker_handler('quality.cell_fields'),
            'quality.mesh_report': self._quality_mesh_report,
            'mesh.run.accept': self._mesh_run_accept,
            'quality.report': self._quality_report,
            'quality.failed_sets': self._quality_failed_sets,
            # Plan 37 UF18: what checkMesh wrote, by revision; the geometry
            # is parsed in the mesh worker, under the check_artifacts budgets.
            'quality.check_artifacts': self._quality_check_artifacts,
            'quality.check_highlights':
                self._worker_handler('quality.check_highlights'),
            'quality.failed_set.select': self._quality_failed_set_select,
            'quality.compare': self._quality_compare,
            'quality.report.export': self._quality_report_export,
            'quality.waiver.record': self._quality_waiver_record,
            'quality.fidelity': self._worker_handler('quality.fidelity'),
            'quality.resolution': self._worker_handler('quality.resolution'),
            'quality.summary': self._worker_handler('quality.summary'),
            'quality.summary.read': self._quality_summary_read,
            'quality.evidence.read': self._quality_evidence_read,
            'mesh.repair.preview': self._mesh_repair_preview,
            'mesh.repair.recommendations': self._mesh_repair_recommendations,
            'mesh.repair': self._mesh_repair,
            'mesh.recovery.list': self._mesh_recovery_list,
            'mesh.restore': self._mesh_restore,
            'mesh.transform.rotate': self._mesh_rotate,
            'mesh.transform.translate': self._mesh_translate,
            'mesh.transform.scale': self._mesh_scale,
            'mesh.extrude': self._mesh_extrude,
            # Slice 6: import / export / conversion
            'mesh.import.native': self._import_native,
            'mesh.import.converter': self._import_converter,
            'case.export.entries': self._export_entries,
            'case.export.native': self._export_native,
            'case.export.vtk': self._export_vtk,
            'case.export.cgns': self._export_cgns,
            'case.export.gmsh': self._export_gmsh,
            'case.export.su2': self._export_su2,
            'case.export.med': self._export_med,
            'case.export.unv': self._export_unv,
            'case.export.fluent': self._export_fluent,
            'case.export.format_convert': self._export_format_convert,
            'case.export.authored': self._export_authored,
        }
        _assert_worker_isolation(table)
        return table

    # -- shared helpers ---------------------------------------------------- #

    def _capabilities_registry(self):
        if self._capabilities is None:
            from foammesh.core.shell import CapabilityRegistry
            self._capabilities = CapabilityRegistry()
        return self._capabilities

    def _start_geometry_prepare(self, case_id: str):
        from threading import Event as ThreadEvent
        token = ThreadEvent()
        self._geometry_prepare_tokens[case_id] = token
        return token

    def _finish_geometry_prepare(self, case_id: str, token) -> None:
        if self._geometry_prepare_tokens.get(case_id) is token:
            self._geometry_prepare_tokens.pop(case_id, None)

    def _geometry_prepare_cancel(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        token = self._geometry_prepare_tokens.get(session.case_id)
        if token is not None:
            token.set()
        return self._read_result(session, command, {
            'cancel_requested': token is not None,
            'state': ('finishing_current_sub_step' if token is not None else 'idle')})

    def _geometry_prepared_create(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        session.require_writable()
        from foammesh.core.geometry import PreparedGeometryError, PreparedGeometryStore
        from foammesh.core.geometry.prepared import (
            prepare_readiness, preparation_with_topology,
        )
        store = PreparedGeometryStore(session.case_path)
        categories = command.parameters.get('boundary_categories')
        if categories is None:
            categories = _prepared_boundary_categories(
                session.state.db, store.source.entries())
        try:
            # DP-122. The automatic route recorded the classifier's volume
            # count on the revision and this one, the route the GUI's Prepare
            # step takes, passed the caller's `{'decision': 'as_is'}` through
            # untouched -- so every revision a user made read
            # `volumes_source: unknown`, and DP-52, DP-53 and DP-115 all
            # refuse nothing on an unknown count. Inside the `try`, because
            # reading the store to count is as able to fail as writing to it.
            preparation = preparation_with_topology(
                prepare_readiness(store),
                command.parameters.get('preparation'))
            result = store.materialize(
                preparation=preparation,
                boundary_categories=categories,
                fluid_seed=command.parameters.get('fluid_seed'),
                transform=command.parameters.get('transform'))
        except (PreparedGeometryError, TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        payload = result.to_dict()
        payload['geometry_reference'] = self._materialize_geometry_reference(
            session, result,
            tolerance=command.parameters.get('fidelity_tolerance_m'))
        _publish_prepared_catalogue(session)
        return self._artifact_result(
            session, command, payload,
            invalidates=('engine_plan', 'mesh', 'quality', 'exports'),
            event=Event.ARTIFACT_GEOMETRY_CHANGED)

    @staticmethod
    def _materialize_geometry_reference(session: CaseSession, prepared,
                                        *, tolerance=None) -> dict:
        """Build the validation reference and feature manifest for a revision.

        Plan 23 WP3, §16.6: detection runs automatically as part of reference
        preparation, so the checker can run headlessly on a first attempt while
        still accepting engineering intent when a user supplies it.

        Neither artifact may fail geometry preparation. A revision that
        materialized is a real, usable revision; the consequence of a missing
        reference is that fidelity reports `unrated`, which is exactly what the
        absent artifact means.
        """
        from foammesh.core.geometry.features import (
            FeatureManifestStore, carry_forward, default_detection,
        )
        from foammesh.core.geometry.validation import materialize

        revision = prepared.reference.revision_id
        summary: dict = {'prepared_revision_id': revision}
        try:
            reference = materialize(
                prepared, tau_min=float(tolerance or 0.0),
                case_path=session.case_path,
                detection=default_detection())
            summary['validation_reference'] = {
                'rated': reference.rated,
                'reference_class': reference.reference_class,
                'epsilon_reference': reference.epsilon_reference,
                'reason': reference.reason,
            }
        except Exception as error:                      # noqa: BLE001
            summary['validation_reference'] = {
                'rated': False, 'reason': f'not built: {error}'}

        try:
            store = FeatureManifestStore(session.case_path)
            # Carry identity forward from whichever revision was prepared
            # before this one, so a tolerance a user attached to a feature
            # survives re-preparation.
            previous = None
            for candidate in sorted(
                    (item.name for item in store.root.iterdir()
                     if item.is_dir()) if store.root.is_dir() else ()):
                if candidate != revision:
                    previous = store.read(candidate) or previous
            manifest = carry_forward(
                previous, [], prepared_revision_id=revision,
                detection=default_detection())
            store.write(manifest)
            summary['feature_manifest'] = {
                'features': len(manifest.features),
                'lost': len(manifest.lost),
                'fingerprint': manifest.fingerprint(),
            }
        except Exception as error:                      # noqa: BLE001
            summary['feature_manifest'] = {'reason': f'not built: {error}'}
        return summary

    def _geometry_prepared_load(self, session: CaseSession,
                                command: Command) -> OperationResult:
        revision_id = command.parameters.get('revision_id')
        if not isinstance(revision_id, str) or not revision_id:
            raise ValidationFailedError('revision_id is required')
        from foammesh.core.geometry import PreparedGeometryError, PreparedGeometryStore
        try:
            result = PreparedGeometryStore(session.case_path).load(revision_id)
        except FileNotFoundError as error:
            raise PreconditionFailedError(
                'prepared geometry revision was not found',
                details={'revision_id': revision_id}) from error
        except PreparedGeometryError as error:
            raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, result.to_dict())

    def _geometry_prepared_current(self, session: CaseSession,
                                   command: Command) -> OperationResult:
        from foammesh.core.geometry import PreparedGeometryError, PreparedGeometryStore
        try:
            result = PreparedGeometryStore(session.case_path).current()
        except PreparedGeometryError as error:
            raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, {
            'selected': result is not None,
            'prepared': result.to_dict() if result is not None else None,
        })

    def _geometry_prepared_discard(self, session: CaseSession,
                                   command: Command) -> OperationResult:
        """Throw the published prepared revision away and reopen the step.

        Plan 33 GEO-08. Unlocking `1. Geometry` is the reader saying the
        geometry is about to change, and every page above the unlocked step is
        asked to drop what it produced. Preparation had nothing to ask for:
        `current.json` is written by `geometry.prepared.create` and there was
        no operation that unwrote it, so the meshers -- which read
        `PreparedGeometryStore.current()` and nothing else -- went on reading a
        revision of the file the reader had just replaced.

        Three things say the geometry is prepared and all three are undone
        here, because leaving any one of them standing leaves a surface that
        contradicts the other two: the published revision on disk, the
        recorded decision in the case, and the state of the tasks whose
        questions `3. Preparation` asks.

        The revision folders themselves are kept. They are dated work with a
        manifest, `geometry.prepared.select` can still bring one back, and
        deleting a reader's earlier preparation is not what unlocking a step
        asked for.
        """
        session.require_writable()
        from foammesh.core.geometry import PreparedGeometryStore

        store = PreparedGeometryStore(session.case_path)
        discarded = store.current_path.is_file()
        if discarded:
            store.current_path.unlink()
        reopened = self._invalidate_geometry_preparation(session)
        reverted = self._revert_preparation_tasks(session, command)
        payload = {'discarded': discarded, 'reverted_tasks': reverted,
                   'changed': bool(discarded or reopened or reverted)}
        if not payload['changed']:
            # DP-350. A discard that found nothing prepared has not changed
            # the geometry, and saying it has costs more than a wrong word.
            # `MainWindow` refreshes the whole scene on
            # ARTIFACT_GEOMETRY_CHANGED; the refresh runs `StepManager.load`,
            # which answers by calling `clearResult()` on every page above the
            # reachable step; `GeometryRepairPage.clearResult` submits this
            # operation. MEASURED on a live gmsh leg: the announcement came
            # back round as the refresh that produced it, 1,940 times in
            # 240 s -- 8.1/s, on an idle application, starting 0.01 s after
            # the case attached and never stopping. Each turn of it wrote
            # configurations.h5 and reloaded every page in the shell, which is
            # why presses went unanswered underneath it.
            return self._read_result(session, command, payload)
        _publish_prepared_catalogue(session)
        return self._artifact_result(
            session, command, payload,
            invalidates=('engine_plan', 'mesh', 'quality', 'exports'),
            event=Event.ARTIFACT_GEOMETRY_CHANGED)

    def _revert_preparation_tasks(self, session: CaseSession,
                                  command: Command) -> list:
        """Reopen the tasks `3. Preparation` hosts, and say which.

        A hosted task has no row of its own, so a reader cannot see that it is
        still accepted and cannot reopen it by hand; the press that settled it
        is the press this undoes. A store that cannot name an engine, or a
        graph that refuses the transition, is not a reason to leave the
        revision on disk -- the revision is already gone by the time this
        runs, and the task state is a claim about it.
        """
        from foammesh.core.workflow.task_state_store import TaskStateError

        try:
            _engine_id, store = self._task_state_store(session, command)
        except (FacadeError, KeyError, OSError, ValueError):
            return []
        reverted = []
        for task_id in sorted(PREPARATION_HOSTED_TASKS):
            try:
                store.apply(task_id, 'revert')
            except (TaskStateError, KeyError, OSError, ValueError):
                continue
            reverted.append(task_id)
        return reverted

    def _geometry_prepared_select(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        session.require_writable()
        revision_id = command.parameters.get('revision_id')
        if not isinstance(revision_id, str) or not revision_id:
            raise ValidationFailedError('revision_id is required')
        from foammesh.core.geometry import PreparedGeometryError, PreparedGeometryStore
        try:
            result = PreparedGeometryStore(session.case_path).select(revision_id)
        except FileNotFoundError as error:
            raise PreconditionFailedError(
                'prepared geometry revision was not found') from error
        except PreparedGeometryError as error:
            raise ValidationFailedError(str(error)) from error
        _publish_prepared_catalogue(session)
        return self._artifact_result(
            session, command, result.to_dict(),
            invalidates=('engine_plan', 'mesh', 'quality', 'exports'),
            event=Event.ARTIFACT_GEOMETRY_CHANGED)

    def _mesh_engine_list(self, session: CaseSession,
                          command: Command) -> OperationResult:
        """The engines, and whether each can serve this project's solver.

        Plan 28: the compatibility verdict travels with the list so no caller
        has to re-derive it. The page reads it to decide what to offer; the
        summary and the CLI read the same field.
        """
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_engine_id, configured_target_solver,
            engine_incompatibility,
        )
        target = configured_target_solver(session.state.db)
        engines = []
        for engine_id in ENGINE_REGISTRY.ids():
            entry = ENGINE_REGISTRY.get(engine_id).descriptor.to_dict()
            reason = engine_incompatibility(engine_id, target)
            entry['incompatible_reason'] = reason
            entry['compatible'] = not reason
            engines.append(entry)
        return self._read_result(session, command, {
            'current_engine': configured_engine_id(session.state.db),
            'target_solver': target,
            'engines': engines,
            'selection_operation': 'mesh.engine.select',
            'qa_operation': self._qa_operation_for(target),
            'same_application': True,
        })

    def _mesh_target_solver_get(self, session: CaseSession,
                                command: Command) -> OperationResult:
        from foammesh.core.engine.registry import (
            compatible_engines, configured_target_solver, solver_display_name,
        )
        target = configured_target_solver(session.state.db)
        return self._read_result(session, command, {
            'target_solver': target,
            'display_name': solver_display_name(target),
            'compatible_engines': list(compatible_engines(target)),
            'qa_operation': self._qa_operation_for(target),
        })

    def _mesh_target_solver_set(self, session: CaseSession,
                                command: Command) -> OperationResult:
        """Record which solver this mesh is for.

        Changing it can strand the selected engine -- picking SU2 while snappy
        is selected leaves a project that cannot produce a readable mesh. That
        is reported, not silently repaired: which engine to move to is the
        user's call, and this operation does not make it for them.

        Plan 30 WP-07 (F-07). It used to record the choice and stop, so a mesh
        made for the other solver kept its green verdict on screen and its
        export record: the field is written through this operation rather than
        through ``patch``, so nothing on the ordinary edit path ever saw the
        change and the registry's ``invalidates`` entry was never read. The
        invalidation is stated here, and the engine probe cache -- which is
        keyed by engine alone but whose answer now depends on the target -- is
        dropped with it.
        """
        from foammesh.core.engine.registry import (
            compatible_engines, configured_engine_id, engine_incompatibility,
        )
        from foammesh.core.facade.facade import (
            _invalidation_for, _publish_verdict_staleness,
        )
        from foammesh.core.project import Source
        from foammesh.db.configurations_schema import TargetSolver

        token = str(command.parameters.get('target_solver') or '').strip().lower()
        allowed = tuple(item.value for item in TargetSolver)
        if token not in allowed:
            raise ValidationFailedError(
                'target_solver must be one of: ' + ', '.join(allowed))
        session.require_writable()
        from foammesh.core.engine.registry import configured_target_solver
        previous = configured_target_solver(session.state.db)
        data = session.state.checkout()
        data.setValue('mesh/targetSolver', TargetSolver(token))
        source_map = {
            'gui': Source.GUI, 'rest': Source.API, 'cli': Source.CLI,
            'automation': Source.AGENT, 'system': Source.SYSTEM,
        }
        transaction = session.state.commit(
            data, action='select target solver',
            source=source_map[command.source.value],
            target='mesh/targetSolver', reason=f'meshing for {token}')
        engine_id = configured_engine_id(session.state.db)
        stranded = engine_incompatibility(engine_id, token)
        changed = ('mesh.target_solver',) if token != previous else ()
        invalidated = _invalidation_for(changed)
        if changed:
            # The probe answer depends on the target now (F-40), and the cache
            # cannot tell the two answers apart.
            self._probe_results.clear()
            _publish_verdict_staleness(session, invalidated, changed)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            changed, invalidated, payload={
                'target_solver': token,
                'previous_target_solver': previous,
                'current_engine': engine_id,
                'engine_incompatible_reason': stranded,
                'compatible_engines': list(compatible_engines(token)),
                'qa_operation': self._qa_operation_for(token),
                'transaction': transaction.to_dict(),
            })

    def _prepared_diagonal(self, session: CaseSession) -> float:
        """Diagonal of the prepared bounding box, in metres, or ``0.0``.

        The prepared revision first, because that is what the fidelity report
        measures against; the staged surface as a fallback, so a project that
        has imported geometry but not prepared it yet still gets a number.
        """
        from foammesh.core.geometry import BBox

        # R57. ``BBox.diagonal`` is a property, and both branches called it
        # as ``diagonal()``. That raises ``TypeError: 'float' object is not
        # callable``, which the two ``except (TypeError, ValueError)`` arms
        # below swallowed, so this returned 0.0 on every case ever meshed.
        # A zero diagonal means ``suggested_m`` is 0, which is why Reference
        # Readiness has always said "No geometry is prepared yet, so there is
        # nothing to base a suggestion on" and kept **Use suggestion**
        # disabled -- MEASURED on a prepared venturi whose assembled surface
        # is right there with a 0.616441 m diagonal.
        bounds = _prepared_bbox(self._current_prepared(session))
        if bounds is not None:
            try:
                box = (bounds if isinstance(bounds, BBox)
                       else BBox.from_bounds(bounds))
                return float(box.diagonal)
            except (TypeError, ValueError):
                pass
        try:
            surface = self._assembled_surface(session)
        except Exception:                                   # noqa: BLE001
            surface = None
        if surface is None:
            return 0.0
        try:
            return float(BBox.from_polydata(surface).diagonal)
        except (TypeError, ValueError):
            return 0.0

    def _quality_tolerance_get(self, session: CaseSession,
                               command: Command) -> OperationResult:
        """The project-wide fidelity tolerance, and what to offer if unset.

        Plan 28 WP6. Level 5 of the five the fidelity report resolves had no
        storage at all, so every case ran with it empty -- which is why an
        otherwise complete walk reported `unrated` sections and a summary that
        could not leave report-only. The suggestion is returned beside the
        stored value rather than in place of it: an unset project is visibly
        unset until someone accepts a number.
        """
        from foammesh.core.quality.geometry_fidelity import tolerance_source

        stored = tolerance_source.project_tolerance(session.state.db)
        diagonal = self._prepared_diagonal(session)
        return self._read_result(session, command, {
            'tolerance_m': stored,
            'suggested_m': tolerance_source.suggested_tolerance(diagonal),
            'diagonal_m': diagonal,
            'source': 'project' if stored else 'unset',
            'storage_path': tolerance_source.PROJECT_TOLERANCE_KEY,
        })

    def _quality_tolerance_set(self, session: CaseSession,
                               command: Command) -> OperationResult:
        """Record the project-wide fidelity tolerance. Zero clears it.

        Clearing is a real choice, not an error: a project that would rather
        report `unrated` than rate against a number nobody defended is
        entitled to say so, and this is how it says it.
        """
        from foammesh.core.project import Source
        from foammesh.core.quality.geometry_fidelity import tolerance_source

        raw = command.parameters.get('tolerance_m')
        try:
            value = 0.0 if raw is None else float(raw)
        except (TypeError, ValueError):
            raise ValidationFailedError(
                'tolerance_m must be a number of metres, or 0 to clear it')
        if value < 0:
            raise ValidationFailedError('tolerance_m cannot be negative')
        session.require_writable()
        data = session.state.checkout()
        data.setValue(tolerance_source.PROJECT_TOLERANCE_KEY, str(value))
        source_map = {
            'gui': Source.GUI, 'rest': Source.API, 'cli': Source.CLI,
            'automation': Source.AGENT, 'system': Source.SYSTEM,
        }
        transaction = session.state.commit(
            data, action='set qualification tolerance',
            source=source_map[command.source.value],
            target=tolerance_source.PROJECT_TOLERANCE_KEY,
            reason=('clear the project tolerance' if not value
                    else f'{value} m project tolerance'))
        return OperationResult(
            'accepted', command.operation, session.revisions, payload={
                'tolerance_m': value or None,
                'source': 'project' if value else 'unset',
                'transaction': transaction.to_dict(),
            })

    def _qa_operation(self, session: CaseSession) -> str:
        """Which QA operation this project's mesh should be judged by.

        One place, so the GUI, the CLI and the Gmsh run cannot disagree.
        """
        from foammesh.core.engine.registry import configured_target_solver

        return self._qa_operation_for(configured_target_solver(session.state.db))

    @staticmethod
    def _qa_operation_for(target_solver) -> str:
        """Which quality check answers "is this mesh good?" for this solver.

        Plan 30 F-24. The rule itself lives on the engine seam
        (:func:`foammesh.core.engine.base.qa_operation`) because the GUI and
        the CLI decided it separately and could disagree with the facade about
        the same mesh. This stays as the facade's name for it.
        """
        from foammesh.core.engine.base import qa_operation

        return qa_operation(target_solver)

    async def _mesh_engine_probe(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        """Probe one engine's runtime through its own protocol member.

        Every engine reports availability the same way, so adding an engine
        needs no change here. The probe itself runs on a worker thread behind
        a TTL cache: it boots WSL, which takes about half a minute cold, and
        the window asks for it every time a dialog opens.

        The verdict depends on the target solver as well as the engine --
        Gmsh needs ``checkMesh`` only when OpenFOAM is the target -- so the
        cache is keyed by both (F-40). Keying by engine alone handed a
        SU2 user the OpenFOAM verdict, and said Gmsh was unavailable for a
        tool it does not need.
        """
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_target_solver,
        )

        engine_id = str(command.parameters.get('engine_id') or '').strip().lower()
        if engine_id not in ENGINE_REGISTRY.ids():
            raise ValidationFailedError(
                'engine_id must be one of: ' + ', '.join(ENGINE_REGISTRY.ids()))
        engine = ENGINE_REGISTRY.get(engine_id)
        target_solver = str(configured_target_solver(session.state.db) or '')
        refresh = bool(command.parameters.get('refresh'))
        if refresh:
            self._capabilities_registry().refresh()

        capabilities = self._capabilities_registry()

        def probe():
            report = dict(engine.probe(capabilities, refresh=refresh,
                                       target_solver=target_solver).to_dict())
            report['status'] = 'ready'
            return report

        timeout = command.parameters.get('timeout_seconds')
        try:
            timeout = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout = None

        # A caller that named a deadline gets an answer by it. The probe is
        # shielded so the deadline ends the *wait*, not the work: the next
        # caller finds the same probe still running instead of starting a
        # second one, and its answer is cached the moment it lands.
        answer = await self._probe_results.answer(
            (engine_id, target_solver), probe, timeout=timeout, refresh=refresh)
        if not answer.ready:
            return self._read_result(session, command, {'probes': [{
                'engine_id': engine_id,
                'status': 'pending',
                'available': None,
                'reason': 'still probing the runtime',
            }]})
        return self._read_result(session, command, {'probes': [answer.value]})

    async def _openfoam_runtime_diagnostics(
            self, session: CaseSession, command: Command) -> OperationResult:
        """What the OpenFOAM runtime looks like from here, without freezing.

        Plan 30 WP-08 (F-09). This sources a WSL profile and runs version
        commands inside it; it was doing that on the event loop, so every
        dialog that asks "is OpenFOAM there?" on the way up waited for a cold
        distribution to boot -- about half a minute, measured. Now it runs on
        a worker thread behind the same TTL cache the engine probe uses.
        """
        registry = self._capabilities_registry()
        refresh = bool(command.parameters.get('refresh'))
        if refresh:
            registry.refresh()

        timeout = command.parameters.get('timeout_seconds')
        try:
            timeout = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout = None

        answer = await self._runtime_diagnostics.answer(
            id(registry), registry.runtime_diagnostics,
            timeout=timeout, refresh=refresh)
        if not answer.ready:
            return self._read_result(session, command, {
                'status': 'pending',
                'reason': 'still probing the OpenFOAM runtime',
            })
        return self._read_result(session, command, answer.value)

    async def _decomposition_methods(self, session: CaseSession,
                                     command: Command) -> OperationResult:
        """Every method the writer knows, and whether this runtime has it.

        Plan 31 CP-07 item 4. The methods were declared in three places -- the
        schema enum, a writer constant and a page -- and the runtime was asked
        about none of them, so a user could pick a method whose library the
        selected OpenFOAM does not ship and find out when the run aborted.

        Every method is returned, including the ones that are unavailable and
        why, because a control that silently loses a row is indistinguishable
        from a product that never had the feature. ``probed`` says whether the
        availability verdicts rest on a runtime answer or on nothing yet: the
        probe costs a cold WSL boot, so a page that opens before it lands is
        told that, rather than being handed guesses dressed as findings.
        """
        from foammesh.openfoam import decomposition

        registry = self._capabilities_registry()
        refresh = bool(command.parameters.get('refresh'))
        timeout = command.parameters.get('timeout_seconds')
        try:
            timeout = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout = None

        libraries, probed = None, False
        prober = getattr(registry, 'decomposition_libraries', None)
        if prober is not None:
            answer = await self._decomposition_probe.answer(
                id(registry), prober, timeout=timeout, refresh=refresh)
            if answer.ready:
                libraries, probed = answer.value, answer.value is not None

        methods = [{
            'name': item.name,
            'available': bool(item.available),
            'reason': item.reason,
            'library': (decomposition.capability(item.name).library or ''),
            'authorable': bool(
                decomposition.capability(item.name).authorable),
        } for item in decomposition.selectable(libraries)]
        return self._read_result(session, command, {
            'methods': methods,
            'probed': probed,
            'reason': ('' if probed else
                       'the selected OpenFOAM runtime has not answered yet, '
                       'so availability is not known'),
        })

    def _execution_plan(self, session: CaseSession,
                        command: Command) -> OperationResult:
        """What the next run will actually run on, before it runs.

        Plan 31 CP-07 item 6. Whether a mesh would be serial or parallel, and
        on how many workers, was decided inside ``run_snappy_pipeline`` and
        visible nowhere until the run was over -- and then only as
        ``processor0``, ``processor1`` ... on disk. A user who set four cores
        in the Parallel Environment dialog and a ceiling of two had no way to
        learn, before waiting out a mesh, that two is what they would get.

        This answers with the *same* resolution the run performs -- the same
        policy, the same request, the same allocator -- rather than a second
        copy of the precedence rule in the view, because two surfaces deciding
        separately is how the dialog and the dictionary came to disagree in
        the first place.
        """
        from foammesh.core.execution import (
            ResourceMode, ResourcePolicy, ResourceRequest,
            allocate_resources,
        )
        from foammesh.core.execution.resources import (
            ResourceError, requested_cpu_count,
        )

        configuration = _resource_policy(session.configuration())
        mode = ResourceMode(str(command.parameters.get('mode')
                                or configuration['mode']))
        # Plan 33 DP-X2. The ceiling used to be reachable only when the case
        # had no parallel environment at all, and every case has one answering
        # 1, so the count on the Mesh setup page was never the count previewed
        # here. One function decides the precedence now, and the launcher asks
        # it the same question.
        # DP-691. ``configured`` was the Parallel Environment dialog's count
        # in local.cfg, a second input the page never showed. Meshing
        # resources is the only input now; an old case's count was carried
        # onto it when the case was opened.
        # DP-1231. Nothing asked is Auto, answered the way the run answers
        # it. ``refresh_host`` starts a WSL reading on a worker thread when
        # the one held is cold or old; this call never waits for it.
        from foammesh.core.execution.resources import meshing_host
        host = meshing_host(refresh=bool(
            command.parameters.get('refresh_host')))
        auto_cpu, host = self._snappy_cpu(
            session.case_path, configuration,
            cores=int(command.parameters.get('cores') or 0), host=host)
        requested = (1 if mode is ResourceMode.SERIAL
                     else max(1, auto_cpu.count))
        if not auto_cpu.auto:
            requested = requested_cpu_count(
                configuration,
                requested=int(command.parameters.get('cores') or 0)) or 1
        policy = ResourcePolicy(
            mode, configuration['max_cpu_cores'],
            configuration['max_memory_bytes'],
            configuration['allow_distributed'],
            'openfoam-mpi' if requested > 1 else 'local')
        document = {
            'mode': mode.value if hasattr(mode, 'value') else str(mode),
            'requested_cores': requested,
            'max_cpu_cores': configuration['max_cpu_cores'] or 0,
            'source': ('the run' if 'cores' in command.parameters
                       else 'automatic' if auto_cpu.auto
                       else 'the execution ceiling'),
            # DP-1231. How the count was reached: ``source`` serial /
            # requested / recorded / auto, the host's cores and their kind,
            # the headroom, the memory limit and which of them bound it.
            'auto': auto_cpu.to_dict(),
            'host': host.to_dict(),
            # DP-1236. Whether ``workflow.run_pipeline`` with ``resume``
            # would carry on a stopped run, from which stage, and why not.
            'resume': self._pipeline_resume_point(session, command),
        }
        try:
            allocation = allocate_resources(
                policy,
                ResourceRequest(requested, backend_id='openfoam-mpi',
                                explicit='cores' in command.parameters),
                host.resource_facts())
        except (ResourceError, ValueError, TypeError) as error:
            document.update({
                'effective_ranks': 0, 'parallel': False, 'backend_id': '',
                'refused': True, 'reason': str(error)})
            return self._read_result(session, command, document)
        ranks = int(allocation.effective_ranks)
        document.update({
            'allocation': allocation.to_dict(),
            'effective_ranks': ranks,
            'parallel': ranks > 1,
            'backend_id': allocation.backend_id,
            'refused': False,
            'reason': ('' if ranks == requested else
                       f'the execution ceiling took precedence: {requested} '
                       f'requested, {ranks} allowed'),
        })
        return self._read_result(session, command, document)

    def _mesh_engine_workflow(self, session: CaseSession,
                              command: Command) -> OperationResult:
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_engine_id,
        )
        engine_id = str(command.parameters.get('engine_id') or
                        configured_engine_id(session.state.db)).strip().lower()
        if engine_id == 'unselected':
            return self._read_result(session, command, {
                'engine_id': engine_id,
                'workflow': {
                    'engine_id': engine_id,
                    'version': 1,
                    'display_name': 'No meshing method selected',
                    'description': (
                        'Choose a meshing method to populate the '
                        'engine-specific workflow.'),
                    'tasks': [],
                },
                'workflow_digest': None,
            })
        try:
            workflow = ENGINE_REGISTRY.get(engine_id).workflow_descriptor()
        except Exception as error:
            raise ValidationFailedError('unknown meshing engine') from error
        return self._read_result(session, command, {
            'engine_id': engine_id, 'workflow': workflow.to_dict(),
            'workflow_digest': workflow.digest,
        })

    def _task_state_store(self, session: CaseSession, command: Command):
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_engine_id,
        )
        from foammesh.core.workflow.task_state_store import EngineTaskStateStore
        engine_id = str(command.parameters.get('engine_id') or
                        configured_engine_id(session.state.db)).strip().lower()
        try:
            descriptor = ENGINE_REGISTRY.get(engine_id).workflow_descriptor()
        except Exception as error:
            raise ValidationFailedError('unknown meshing engine') from error
        return engine_id, EngineTaskStateStore(session.case_path, descriptor)

    def _mesh_workflow_task_state(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        engine_id, store = self._task_state_store(session, command)
        return self._read_result(session, command, {
            'engine_id': engine_id, 'state': store.snapshot(),
            # WP2.1. Warnings ride the *dynamic* channel beside status, never
            # the workflow descriptor: `WorkflowDescriptor.digest` is a sha256
            # over `WorkflowTask.to_dict()` and is the invalidation key for
            # persisted task state, so a field that changes whenever a warning
            # appears would wipe the user's progress on every status change.
            'warnings': self._task_warnings(session, engine_id),
            # Plan 35 CR2. Checks run in workers now, so "running" is a
            # state a task can be in between two reads, and "could not run"
            # is an answer that is not a verdict.
            **self._check_status(session)})

    def _check_status(self, session: CaseSession) -> dict:
        checks = self._checks_for(session)
        return {'checking': dict(checks['checking']),
                'check_failures': {task: dict(failure) for task, failure
                                   in checks['failures'].items()}}

    def _mesh_workflow_task_page(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        """Everything one task page draws, in one read (F-22).

        The page used to cost three synchronous facade calls every time the
        graph moved: the workflow descriptor for the task, the descriptor
        again for the titles its prerequisites are named by, and the task
        state for its status and warnings. Each of those went through the
        command queue, so a page refresh during a running stage waited three
        times behind whatever the queue was busy with.

        One descriptor is built here and read three ways.
        """
        task_id = str(command.parameters.get('task_id') or '').strip()
        if not task_id:
            raise ValidationFailedError('task_id is required')
        engine_id, store = self._task_state_store(session, command)

        from foammesh.core.engine.registry import ENGINE_REGISTRY

        descriptor = ENGINE_REGISTRY.get(engine_id).workflow_descriptor()
        document = descriptor.to_dict()
        tasks = list(document.get('tasks') or ())
        task = next((item for item in tasks
                     if item.get('task_id') == task_id), None)
        if task is None:
            raise ValidationFailedError(
                f'unknown {engine_id} task: {task_id}')
        snapshot = store.snapshot()
        statuses = (snapshot or {}).get('tasks') or {}
        warnings = self._task_warnings(session, engine_id)
        return self._read_result(session, command, {
            'engine_id': engine_id,
            'task_id': task_id,
            'task': task,
            'titles': {item['task_id']: item.get('title') or item['task_id']
                       for item in tasks if item.get('task_id')},
            # DP-144. `task['depends_on']` is the chain the state machine
            # walks; this is the answer to "what must I do before this", which
            # is a different question wherever an optional task sits in that
            # chain. Computed here so the CLI and the API read the same
            # sentence the page shows (§9 parity).
            'requires': list(descriptor.required_prerequisites(task_id)),
            'status': str(statuses.get(task_id) or 'ready'),
            'warnings': list(warnings.get(task_id) or ()),
            'state': snapshot,
            # Plan 35 CR2: running in a worker, or could not run.
            'checking': task_id in self._checks_for(session)['checking'],
            'check_failure': dict(
                self._checks_for(session)['failures'].get(task_id) or {}),
            # The tasks this one unlocks, under the names the outline shows.
            # The readiness page names them ("Until you do, Snap Fidelity
            # stays locked") and read the whole descriptor again to do it.
            'dependents': [str(item.get('title') or item.get('task_id'))
                           for item in tasks
                           if task_id in (item.get('depends_on') or ())],
        })

    def _task_warnings(self, session: CaseSession, engine_id: str) -> dict:
        """Everything the pages should be warning about, by task.

        Two sources, one channel. The engine contributes what its own
        derivation changed; the case contributes what its geometry makes
        impossible. Merging here rather than in the page keeps the CLI and the
        API seeing the same warnings the GUI does (§9 parity).
        """
        warnings = self._derivation_warnings(session, engine_id)
        for source in (self._boundary_coverage_warnings(session),
                       self._run_warnings(session, engine_id)):
            for task_id, texts in source.items():
                warnings[task_id] = (list(warnings.get(task_id) or ())
                                     + list(texts))
        return warnings

    @staticmethod
    def _run_warnings(session: CaseSession, engine_id: str) -> dict:
        """DP-817: why a task that ran reads "finished with warnings".

        The recorder keeps the texts a run warned with in the evidence of the
        task it left at WARNING. Only a task still at WARNING is reported:
        a later clean run replaces the evidence along with the state.
        """
        try:
            from foammesh.core.engine.contracts import TaskState
            from foammesh.core.engine.registry import ENGINE_REGISTRY
            from foammesh.core.workflow.task_state_store import (
                EngineTaskStateStore,
            )

            store = EngineTaskStateStore(
                session.case_path,
                ENGINE_REGISTRY.get(engine_id).workflow_descriptor())
            graph = store.load_result().graph
            evidence = store.evidence()
            found = {}
            for task_id, record in evidence.items():
                texts = [str(text) for text in
                         (dict(record or {}).get('warnings') or ())]
                if texts and graph.state(task_id) is TaskState.WARNING:
                    found[task_id] = ['The last run warned: ' + text
                                      for text in texts]
            return found
        except Exception:                                    # noqa: BLE001
            # The warning channel must never break the page it warns on.
            return {}

    def _boundary_coverage_warnings(self, session: CaseSession) -> dict:
        """Plan 28 WP5: one boundary is not a CFD case.

        A tessellated import is a single surface, so the whole model arrives as
        one patch. Nothing rejects that -- it meshes, it publishes, it passes
        every check -- and then the solver has one boundary to apply conditions
        to, which means no inlet and no outlet and no run. The failure surfaced
        in the solver, a whole pipeline away from the import that caused it.

        It rides `common.reference_readiness` because that task already asks
        whether there is something to measure against, and a boundary you
        cannot name a condition on is the same kind of gap. It is a warning,
        not a block: a single closed wall is a legitimate conduction or
        enclosure case, and the product does not get to decide it is not.
        """
        try:
            rows = self._patch_editor(session).rows()
        except Exception:                                    # noqa: BLE001
            # The warning channel must never be able to break the page it
            # warns on -- an unreadable manifest is the readiness task's
            # problem to report, not this one's.
            return {}
        if len(rows) != 1:
            return {}
        name = str(rows[0].get('name') or 'the surface')
        return {'common.reference_readiness': (
            f'The whole prepared surface is one boundary ({name}), so no '
            'inlet or outlet can be applied to it. Split the geometry or '
            'rename its sub-surfaces on the Prepare page before meshing.',)}

    def _derivation_warnings(self, session: CaseSession, engine_id: str) -> dict:
        """``{task_id: [text, ...]}`` for the engine's current configuration.

        An engine that declares no derivation warnings simply has none; this
        is not an error and must not stop the task state being read.
        """
        from foammesh.core.engine.registry import ENGINE_REGISTRY

        try:
            engine = ENGINE_REGISTRY.get(engine_id)
        except (KeyError, LookupError, ValueError):
            return {}
        reader = getattr(engine, 'derivation_warnings', None)
        if reader is None:
            return {}
        try:
            # DP-614: the prepared bounds, so an unset ("Auto") target size
            # is derived here as it is for the run, and the page shows it.
            bbox = _prepared_bbox(self._current_prepared(session))
            return dict(reader(session.state.db, bbox=bbox) or {})
        except Exception:                                    # noqa: BLE001
            # A warning channel that can break the page it warns on would be
            # worse than the silence it replaces.
            return {}

    #: Check tasks the workflow can execute, and the operation that does it.
    #: Plan 23 §8.4 registers these tasks and §5/§9 build their producers; this
    #: is the table that connects the two. Without it the tasks appear in the
    #: workflow, their pages render, and nothing computes -- which is exactly
    #: the state this capability shipped in until it was noticed that nothing
    #: invoked `quality.fidelity` at all.
    CHECK_TASK_OPERATIONS = {
        'common.fidelity': 'quality.fidelity',
        'common.resolution': 'quality.resolution',
        'snappy.fidelity_snap': 'quality.fidelity',
        'gmsh.fidelity_native': 'quality.fidelity',
        'common.summary': 'quality.summary',
    }

    def _check_command(self, command: Command, task_id: str):
        from dataclasses import replace as _replace

        operation = self.CHECK_TASK_OPERATIONS[task_id]
        return operation, _replace(
            command, operation=operation,
            parameters={**dict(command.parameters), 'task_id': task_id})

    def _run_check_task(self, session: CaseSession, command: Command,
                        task_id: str, store):
        """Execute a check task and record its verdict as evidence.

        These tasks are ``run_gated``, so they cannot be hand-accepted -- and
        that flag is only meaningful if something can actually run them. This
        is that something.

        The verdict never becomes the task's state directly: §8.5 separates
        "the evidence exists" (``COMPLETED``) from "the mesh passed". A
        ``fail`` report is complete evidence, and recording it as a failed
        *task* would make a successful check look like a broken one.

        Plan 35 CR2. The check handlers now run in a worker process, so the
        handler returns an awaitable; this then returns a coroutine that
        awaits the worker and records the verdict when it lands. A handler
        that answers synchronously (a test double) is recorded at once, as
        before.
        """
        operation, inner = self._check_command(command, task_id)
        title = self._check_title(store, task_id)
        # Plan 37 UF4 DP-1025. A task still locked behind a prerequisite is
        # refused here, naming the prerequisite, before anything measures the
        # mesh. It used to run the whole check in a worker and only then
        # answer "produced a report but the task could not advance" --
        # measured live on the Gmsh elbow with Reference readiness unaccepted.
        self._refuse_locked_check(store, task_id, title)
        handler = self.handlers()[operation]
        result = handler(session, inner)
        if not inspect.isawaitable(result):
            return self._record_check(task_id, operation, result, store)

        async def finish():
            # DP-1027. One job per check and mesh: a second request joins
            # the one in flight rather than measuring the mesh again.
            job = self._check_job(session, task_id, operation, result, title)
            outcome = await asyncio.shield(job['future'])
            self._assert_job_current(session, job)
            return self._record_check(task_id, operation, outcome, store)

        return finish()

    @staticmethod
    def _check_title(store, task_id: str) -> str:
        """The name the user reads for ``task_id`` (its row, not its id)."""
        try:
            return str(store.descriptor.task(task_id).title or task_id)
        except Exception:                                   # noqa: BLE001
            return task_id

    def _refuse_locked_check(self, store, task_id: str, title: str) -> None:
        try:
            graph = store.load_result().graph
            if graph.is_runnable(task_id):
                return
            blocking = graph.blocking_prerequisites(task_id)
        except Exception:                                   # noqa: BLE001
            return                  # the recorder still refuses, by name
        names = ', '.join(self._check_title(store, parent)
                          for parent in blocking)
        if names:
            text = ('{0} was not checked and could not advance: it is locked '
                    'by prerequisites until {1} is done. Finish {1} on its '
                    'step (press Proceed there), {2}').format(
                        title, names, _CHECK_AGAIN)
        else:
            text = ('{0} was not checked and could not advance: it is locked '
                    'by prerequisites. Finish the steps above it, {1}').format(
                        title, _CHECK_AGAIN)
        raise ValidationFailedError(text, details={
            'task_id': task_id, 'reason': 'locked', 'actionable': True,
            'waiting_on': list(blocking), 'retryable': True})

    # -- Plan 37 UF4 DP-1027: one revision-keyed job per check -------------- #

    def _check_job(self, session: CaseSession, task_id: str, operation: str,
                   pending, title: str) -> dict:
        """Start the job that runs ``pending``, or join the one in flight.

        A job is keyed by the check, the mesh on disk and the settings. The
        same key joins (``pending`` is dropped unrun); a different key -- a
        new mesh arrived -- supersedes the old job, whose waiters are told so
        and whose result is never recorded. Only the current job's ending
        touches the ``checking`` flag and the failure the page shows, so a
        superseded or background job cannot clear a flag it does not own.
        """
        checks = self._checks_for(session)
        jobs = checks.setdefault('jobs', {})
        revision = _mesh_revision(getattr(session, 'case_path', ''))
        key = (operation, revision, _settings_digest(session))
        job = jobs.get(task_id)
        if job is not None and not job['future'].done():
            if job['key'] == key:
                if inspect.iscoroutine(pending):
                    pending.close()
                return job
            self._stop_job(job, 'superseded')
        job = {'task_id': task_id, 'operation': operation, 'key': key,
               'revision': revision, 'title': title, 'stop': None,
               'case_path': getattr(session, 'case_path', '')}

        async def runner():
            try:
                outcome = await pending
            except asyncio.CancelledError:
                raise self._stopped_error(job) from None
            except FacadeError as error:
                raise _actionable_check_error(title, operation, error) from None
            if _mesh_revision(job['case_path']) != job['revision']:
                raise self._stale_error(job)
            return outcome

        future = asyncio.ensure_future(runner())
        job['future'] = future
        jobs[task_id] = job
        checks['checking'][task_id] = operation
        checks['failures'].pop(task_id, None)

        def finished(done, job=job):
            error = None if done.cancelled() else done.exception()
            if jobs.get(task_id) is not job:
                return                               # superseded: not ours
            jobs.pop(task_id, None)
            checks['checking'].pop(task_id, None)
            if done.cancelled():
                error = self._stopped_error(job)
            if isinstance(error, FacadeError):
                checks['failures'][task_id] = _check_failure(operation, error)
            elif error is not None:
                checks['failures'][task_id] = {
                    'operation': operation, 'code': 'check_unavailable',
                    'reason': 'worker_error', 'retryable': True,
                    'message': '{0} could not run ({1}). {2}'.format(
                        title, error, _CHECK_AGAIN_SENTENCE)}
            else:
                checks['failures'].pop(task_id, None)

        future.add_done_callback(finished)
        return job

    @staticmethod
    def _stop_job(job: dict, reason: str) -> None:
        job['stop'] = reason
        job['future'].cancel()

    @staticmethod
    def _stopped_error(job: dict) -> FacadeError:
        reason = job.get('stop') or 'cancelled'
        if reason == 'superseded':
            text = ('{0} was stopped because a new mesh arrived while it was '
                    'checking the old one; the old result was discarded. '
                    'Press Check & Proceed to check the current mesh.')
        else:
            text = ('{0} was cancelled before it finished, so Quality was not '
                    'marked done. The mesh is unchanged; press Check & Proceed '
                    'to run it again.')
        return CheckUnavailableError(text.format(job['title']), details={
            'reason': reason, 'outcome': 'cancelled', 'retryable': True,
            'operation': job['operation'], 'actionable': True})

    @staticmethod
    def _stale_error(job: dict) -> FacadeError:
        return CheckUnavailableError(
            'The mesh changed while {0} was checking it, so that result was '
            'discarded. Press Check & Proceed to check the current mesh.'
            .format(job['title']), details={
                'reason': 'stale_result', 'outcome': 'stale',
                'retryable': True, 'operation': job['operation'],
                'actionable': True})

    def _assert_job_current(self, session: CaseSession, job: dict) -> None:
        """Refuse to record a job's result once the mesh has moved on."""
        if _mesh_revision(job['case_path']) == job['revision']:
            return
        error = self._stale_error(job)
        checks = self._checks_for(session)
        if checks.setdefault('jobs', {}).get(job['task_id']) in (None, job):
            checks['failures'][job['task_id']] = _check_failure(
                job['operation'], error)
        raise error

    def cancel_check_jobs(self, session: CaseSession,
                          task_id: str | None = None) -> list:
        """Cancel the check jobs of ``session`` (or just ``task_id``).

        Each cancelled check is left unrun and retryable: its page says it
        was cancelled, and the next Check & Proceed starts a fresh job.
        Returns the task ids that were cancelled.
        """
        jobs = self._checks_for(session).setdefault('jobs', {})
        stopped = []
        for key, job in list(jobs.items()):
            if task_id is not None and key != task_id:
                continue
            if not job['future'].done():
                self._stop_job(job, 'cancelled')
                stopped.append(key)
        return stopped

    def _record_check(self, task_id: str, operation: str, result, store):
        """Store one check's verdict as the task's evidence."""
        payload = result.payload or {}
        document = payload.get('report') or payload.get('summary') or {}
        # R38/R161. `Summary.to_dict()` publishes `worst_verdict`, never
        # `verdict`, so this fell through to the `qualified` branch on every
        # summary -- and report-only mode always sets `qualified: false`. A
        # composed FAIL was therefore recorded as evidence reading `unrated`,
        # and the one document that says the mesh did not qualify contributed
        # nothing to what the workflow stored about it.
        verdict = str(document.get('verdict') or document.get('worst_verdict')
                      or ('pass' if document.get('qualified') else 'unrated'))
        # `record_check_result(task_id, *, evidence, warning)` -- the real
        # signature. Evidence is required and non-empty by contract, because a
        # task advanced with nothing recorded is a task nobody can audit.
        fingerprint = str(document.get('report_fingerprint')
                          or document.get('summary_fingerprint') or '')
        if not fingerprint:
            raise ValidationFailedError(
                '{0} produced no report fingerprint, so there is no evidence '
                'to record; the check did not run. The mesh is unchanged; '
                '{1}'.format(self._check_title(store, task_id), _CHECK_AGAIN))
        recorded = store.record_check_result(
            task_id,
            evidence={'operation': operation, 'verdict': verdict,
                      'report_fingerprint': fingerprint},
            # §8.5: a `warning` verdict advances the task and is flagged, not
            # failed. A `fail` report is still complete evidence -- what it
            # gates is qualification, which is the summary's business.
            warning=verdict in ('warning', 'fail', 'unrated', 'incomplete'))
        blocked = recorded.get('blocked')
        if blocked:
            # R180. `_record` reports a refusal in its return value rather
            # than raising, so that whatever did advance is still persisted.
            # Nothing read it: a check whose report was written and whose task
            # then refused to advance returned `accepted` carrying the reason,
            # and the GUI showed a run that had apparently succeeded in front
            # of a row that had not moved and a pipeline still locked behind
            # it. The evidence is written either way; what changes is that the
            # user is told why the row did not move.
            # Plan 37 UF4 DP-1025: named as the row the user reads, and
            # ending on what to do.
            raise ValidationFailedError(
                '{0} produced a report but the task could not advance: '
                '{1} (state {2}). Finish the step it is waiting on, '
                '{3}'.format(
                    self._check_title(store, task_id),
                    blocked.get('reason') or 'refused',
                    blocked.get('state') or 'unknown', _CHECK_AGAIN),
                details={'task_id': task_id, 'actionable': True,
                         'reason': 'not_advanced', 'retryable': True})
        return dict(recorded, check={'operation': operation,
                                     'verdict': verdict,
                                     'report_fingerprint': fingerprint,
                                     'payload': payload})

    # -- Plan 35 CR2: checks run in a worker process ------------------------ #

    def _worker_handler(self, operation: str):
        """The registered handler of a check: dispatch to a worker process.

        The measuring bodies (``_quality_fidelity`` and the rest) are
        unchanged and still methods here; the worker runs them. Nothing in
        this process calls them, which ``_assert_worker_isolation`` holds.
        """
        async def dispatch(session, command):
            return await self._run_in_worker(session, command, operation)

        dispatch._foammesh_worker = operation
        dispatch.__name__ = dispatch.__qualname__ = (
            'worker:' + operation.replace('.', '_'))
        return dispatch

    def _worker_args(self, session: CaseSession, command: Command) -> dict:
        db = getattr(getattr(session, 'state', None), 'db', None)
        db_yaml = None
        if db is not None and hasattr(db, 'toYaml'):
            db_yaml = db.toYaml()
        return {'case_path': str(session.case_path),
                'case_id': str(getattr(session, 'case_id', '')
                               or command.case_id or ''),
                'db_yaml': db_yaml,
                'parameters': dict(command.parameters or {}),
                'fidelity_budget_seconds':
                    self._fidelity_budget_seconds(session)}

    async def _run_in_worker(self, session: CaseSession, command: Command,
                             operation: str) -> OperationResult:
        """Admit, run ``operation`` in a worker, and map how it ended.

        Admission reads only the mesh headers (``read_counts``), estimates
        the peak and asks the budget before any list is parsed: a check that
        would not fit is refused with both numbers rather than started. The
        worker then runs under a memory cap; hitting it is ``over_budget``,
        not a crash. Every other way it can end -- it would not start, it
        died, it was cancelled -- is ``check_unavailable`` and retryable. No
        path falls back to measuring in this process.
        """
        # Plan 37 UF5 DP-1039: a worker reads the mesh, so it shares the case
        # lease; a mesher or an unlock holding it exclusively refuses the
        # check (retryable) rather than let it read a mesh being rewritten.
        # Taken before admission, so a refused check holds no budget.
        from foammesh.core.jobs import case_lease
        try:
            lease = case_lease.acquire(session.case_path, case_lease.SHARED, operation,
                                       timeout=WORKER_LEASE_WAIT_SECONDS)
            await lease.__aenter__()
        except case_lease.CaseBusyError as busy:
            raise CheckUnavailableError(
                f'{operation} was not started: {busy}',
                details={'outcome': 'case_busy', 'operation': operation,
                         'holders': list(busy.holders), 'retryable': True}) from None
        try:
            return await self._run_in_worker_held(session, command, operation)
        finally:
            await lease.__aexit__(None, None, None)

    async def _run_in_worker_held(self, session: CaseSession, command: Command,
                                  operation: str) -> OperationResult:
        from foammesh.core.jobs import local_worker
        from foammesh.core.mesh.poly_mesh_boundary import (
            PolyMeshReadError, read_counts,
        )
        from foammesh.support import resource_budget as budget

        from . import errors as errors_module

        counts = None
        if operation in budget.HEAVY_OPERATIONS:
            try:
                counts = (await asyncio.to_thread(
                    read_counts, session.case_path)).to_dict()
            except (PolyMeshReadError, OSError, ValueError):
                counts = None           # the worker's reader refuses by name
        estimate = budget.estimate_peak_bytes(operation, counts)
        priority = (budget.PRIORITY_INTERACTIVE
                    if operation == 'quality.cell_fields'
                    else budget.PRIORITY_BACKGROUND)
        try:
            grant = await budget.controller().admit(
                operation, estimate, priority=priority)
        except budget.OverBudget as refusal:
            raise CheckOverBudgetError(
                f'{operation} was not started: {refusal}',
                details=dict(refusal.to_dict(), outcome='refused',
                             operation=operation, retryable=True)) from None
        args = await asyncio.to_thread(self._worker_args, session, command)
        async with grant:
            outcome = await local_worker.run_worker(
                operation, args, cap_bytes=grant.cap_bytes,
                group=str(session.case_path))
        if outcome.ok:
            return OperationResult('accepted', command.operation,
                                   session.revisions, payload=outcome.payload)
        details = dict(outcome.to_dict(), outcome=outcome.status,
                       estimate=estimate.to_dict()
                       if hasattr(estimate, 'to_dict') else {},
                       retryable=outcome.status != local_worker.FAILED)
        if outcome.status == local_worker.OVER_BUDGET:
            raise CheckOverBudgetError(
                f'{operation} stopped: {outcome.message}', details=details)
        if outcome.status == local_worker.FAILED:
            # The body refused, in the worker, exactly as it would have here:
            # re-raise the same facade error with the same code.
            details = dict(outcome.details or {}, **{
                'outcome': outcome.status, 'reason': outcome.reason,
                'retryable': False})
            error_class = getattr(errors_module, outcome.error_class or '', None)
            if (isinstance(error_class, type)
                    and issubclass(error_class, FacadeError)):
                raise error_class(outcome.message, details=details)
            raise ValidationFailedError(
                f'{operation} failed: {outcome.message}', details=details)
        raise CheckUnavailableError(
            f'{operation} did not produce a result: {outcome.message}',
            details=details)

    def _mesh_workflow_task_transition(self, session: CaseSession,
                                       command: Command) -> OperationResult:
        from foammesh.core.workflow.task_state_store import TaskStateError
        engine_id, store = self._task_state_store(session, command)
        task_id = str(command.parameters.get('task_id') or '').strip()
        transition = str(command.parameters.get('transition') or '').strip()
        if not task_id or not transition:
            raise ValidationFailedError(
                'task_id and transition are both required')

        # `run` on a check task executes it. Anything else falls through to the
        # ordinary state machine, so a check task's `accept` still refuses --
        # `run_gated` means the evidence has to exist.
        if transition == 'run' and task_id in self.CHECK_TASK_OPERATIONS:
            try:
                recorded = self._run_check_task(
                    session, command, task_id, store)
            except (TaskStateError, ValueError, KeyError) as error:
                raise ValidationFailedError(str(error)) from error
            if inspect.isawaitable(recorded):
                # Plan 35 CR2: the check is in a worker. This awaits it on
                # the loop -- the window keeps drawing -- and records the
                # verdict when it lands.
                pending = recorded

                async def finish():
                    try:
                        landed = await pending
                    except (TaskStateError, ValueError, KeyError) as error:
                        raise ValidationFailedError(str(error)) from error
                    session.state.bus.publish(
                        Event.ARTIFACT_QUALITY_CHANGED,
                        operation=command.operation, job_id=None,
                        artifacts=[], quality=task_id)
                    return self._read_result(session, command, dict(
                        landed, engine_id=engine_id))

                return finish()
            return self._read_result(session, command, dict(
                recorded, engine_id=engine_id))

        # Plan 37 UF5 DP-1040. A task holding a published result is locked:
        # reopening it here would leave the mesh on disk claiming settings
        # it was not made from. `mesh.workflow.unlock` is the way back.
        #
        # DP-1200. The lock check and the write both read and fsync the task
        # state on disk; on a busy disk that held the owner loop for 2.1 s
        # after Yes on an unlock. They run in a worker; the scheduler still
        # awaits this command before the next one, so the order is unchanged.
        def locked_or_applied():
            if store.is_locked(task_id):
                if transition == 'accept':
                    return 'accepted', store.snapshot()
                return 'locked', store.descriptor.task(task_id).title
            try:
                return 'applied', store.apply(task_id, transition)
            except (TaskStateError, ValueError, KeyError) as error:
                return 'invalid', error

        def answer(outcome, value):
            if outcome == 'accepted':
                # Accepting what is already accepted changes nothing; going
                # through configure->finish would stale every result below.
                return self._read_result(session, command, {
                    'transition': None, 'tasks': value['tasks'],
                    'locked': True, 'engine_id': engine_id,
                    'workflow_reset_notice': value.get('workflow_reset_notice')})
            if outcome == 'locked':
                raise TaskLockedError(
                    f'{value} is locked: the mesh on disk was made from its '
                    'settings. Unlock it (discarding the results after it) '
                    'before changing it.',
                    details={'tasks': [task_id], 'titles': [value],
                             'engine_id': engine_id, 'transition': transition,
                             'unlock_operation': 'mesh.workflow.unlock'})
            if outcome == 'invalid':
                raise ValidationFailedError(str(value)) from value
            return self._read_result(session, command, dict(
                value, engine_id=engine_id))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop runs on this thread (the CLI's and tests' synchronous
            # `execute_sync`, or a page before the loop starts): there is
            # nothing to keep drawing, so the write is made here.
            return answer(*locked_or_applied())

        async def run():
            return answer(*await asyncio.to_thread(locked_or_applied))

        return run()

    # -- Plan 37 UF5: unlock and undo ------------------------------------- #

    @staticmethod
    def _unlock_refused(error) -> 'UnlockRefusedError':
        return UnlockRefusedError(str(error), details=dict(
            error.details, reason=error.code,
            retryable=error.code in ('case_busy', 'insufficient_disk')))

    def _mesh_workflow_unlock_preview(self, session: CaseSession,
                                      command: Command) -> OperationResult:
        """What unlocking a task would reset, and what it costs on disk."""
        from foammesh.core.jobs import unlock_transaction
        engine_id, store = self._task_state_store(session, command)
        task_id = str(command.parameters.get('task_id') or '').strip()
        if not task_id:
            raise ValidationFailedError('task_id is required')
        try:
            preview = unlock_transaction.preview_unlock(
                session.case_path, store, task_id)
        except KeyError as error:
            raise ValidationFailedError(f'unknown task: {task_id}') from error
        return self._read_result(session, command, dict(
            preview, engine_id=engine_id))

    def _mesh_workflow_unlock(self, session: CaseSession, command: Command):
        """Unlock a task: it and its dependants reopen; one undo is kept.

        The copy the undo needs is written off the owner loop. The mesh on
        disk is not touched: it stays as the previous result, and the
        outline says its step is no longer published.
        """
        from foammesh.core.jobs import unlock_transaction
        session.require_writable()
        engine_id, store = self._task_state_store(session, command)
        task_id = str(command.parameters.get('task_id') or '').strip()
        if not task_id:
            raise ValidationFailedError('task_id is required')
        try:
            store.descriptor.task(task_id)
        except KeyError as error:
            raise ValidationFailedError(f'unknown task: {task_id}') from error
        expected = command.parameters.get('expected_revision')
        settings_text = session.state.db.toYaml()
        surfaces = self._surface_snapshot(session)

        async def run():
            gather = None
            if self._gather_pending(session.case_path):
                # DP-1203. A parallel stage left its result in the processor
                # cases; the case root still held the mesh before it, and
                # that is what the undo copy kept -- an undo then put back a
                # mesh no stage had produced. The result is gathered first,
                # so the copy is of the mesh the run made.
                try:
                    await self._ensure_reconstructed(session, command)
                    gather = {'gathered': True}
                except PreconditionFailedError as error:
                    # The unlock still goes ahead; the copy is then of the
                    # case root as it stands and the processor cases (with
                    # their pending marker) are left alone, so an undo puts
                    # the case back exactly as it was.
                    gather = {'gathered': False, 'message': str(error)}
            try:
                result = await asyncio.to_thread(
                    unlock_transaction.unlock, session.case_path, store, task_id,
                    settings_text=settings_text,
                    expected_revision=(None if expected is None else int(expected)),
                    capture_surfaces=lambda directory: self._write_surfaces(
                        surfaces, directory))
            except unlock_transaction.UnlockError as error:
                raise self._unlock_refused(error) from None
            session.state.bus.publish(
                Event.ARTIFACT_QUALITY_CHANGED, operation=command.operation,
                job_id=None, artifacts=[], quality=task_id)
            if gather is not None:
                result = dict(result, gather=gather)
            return self._read_result(session, command, dict(
                result, engine_id=engine_id, state=store.snapshot()))

        return run()

    # -- Plan 37 UF17: change the core count of a decomposed mesh ---------- #

    @staticmethod
    def _redistribute_refused(error) -> 'RedistributeRefusedError':
        return RedistributeRefusedError(str(error), details=dict(
            error.details, reason=error.code,
            retryable=error.code in ('case_busy', 'insufficient_disk',
                                     'pending_transaction', 'stale_revision')))

    @staticmethod
    def _redistribute_target(command: Command) -> int:
        try:
            return int(command.parameters.get('ranks'))
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(
                'ranks must be a whole number of cores', details={
                    'reason': 'target_invalid',
                    'ranks': command.parameters.get('ranks')}) from error

    @staticmethod
    def _redistribute_settings(session: CaseSession):
        from dataclasses import replace
        from foammesh.openfoam import decomposition
        # A weight field is read from the case root at the start time; the
        # stage has none, and redistributePar would abort on it.
        return replace(decomposition.DecompositionSettings.read(session.state.db),
                       weight_field='')

    def _redistribute_assess(self, session: CaseSession, target: int,
                             expected_revision=None, settings=None) -> dict:
        """The transaction's own assessment, plus the method's cell split."""
        from foammesh.core.execution.resources import machine_cpu_limit
        from foammesh.core.jobs import redistribute_transaction
        from foammesh.openfoam import decomposition
        try:
            cpu_limit = machine_cpu_limit()
        except Exception:  # noqa: BLE001 - an unknown machine refuses nothing
            cpu_limit = None
        report = redistribute_transaction.assess(
            session.case_path, target, cpu_limit=cpu_limit,
            expected_revision=expected_revision)
        report['cpu_limit'] = cpu_limit
        settings = settings or self._redistribute_settings(session)
        report['method'] = settings.method
        if report['refusal'] is None:
            try:
                decomposition.build(
                    target, method=settings.method, order=settings.order,
                    cells=settings.cells, constraints=settings.constraints(
                        (report['census'] or {}).get('face_zones') or ()))
            except decomposition.DecompositionError as error:
                report['refusal'] = {
                    'code': 'method_needs_cells',
                    'reason': redistribute_transaction.REASONS['method_needs_cells'],
                    'details': {'method': settings.method, 'error': str(error)}}
        return report

    async def _settle_redistribute(self, session: CaseSession, *,
                                   lease_held: bool) -> dict | None:
        """Settle a core-count change an interruption left behind.

        UF20 D-1/D-2: a transaction whose run a killed app left open, or a
        stage folder a failed launch could not remove, refused every later
        change until the case was reopened (and, for the open run, swept
        first). The run-record gate closes a run only once its writer is
        confirmed dead, then the stage is discarded as at case open; a run
        that may still be alive keeps its transaction waiting. Without the
        lease (the preview) this is skipped when another flow holds the case.
        """
        from foammesh.core.jobs import case_lease
        from foammesh.core.jobs import redistribute_transaction as transaction
        case_path = session.case_path
        if session.read_only or not transaction.blocking(case_path):
            return None
        active = tuple(getattr(session.jobs, 'active_job_ids', ()) or ())

        def settle():
            if lease_held:
                return transaction.settle(case_path, active_run_ids=active)
            try:
                with case_lease.hold(case_path, case_lease.EXCLUSIVE,
                                     transaction.RECOVER_OPERATION):
                    return transaction.settle(case_path, active_run_ids=active)
            except case_lease.CaseBusyError:
                return None

        report = await asyncio.to_thread(settle)
        for item in (report or {}).get('settings', ()):
            try:
                transaction.apply_setting(session, item['target_ranks'],
                                          reason='redistribute recovery')
            except Exception:  # noqa: BLE001 - stays pending for the next pass
                logger.warning('core-count setting of %s could not be published',
                               item['id'], exc_info=True)
                continue
            await asyncio.to_thread(transaction.finish, case_path, item['id'])
        return report

    def _mesh_redistribute_preview(self, session: CaseSession, command: Command):
        """What changing the core count would do, or why it cannot."""
        target = self._redistribute_target(command)
        expected = command.parameters.get('expected_revision')
        settings = self._redistribute_settings(session)

        async def run():
            await self._settle_redistribute(session, lease_held=False)
            report = await asyncio.to_thread(
                self._redistribute_assess, session, target, expected, settings)
            report['allowed'] = report['refusal'] is None
            return self._read_result(session, command, report)

        return run()

    def _mesh_redistribute(self, session: CaseSession, command: Command):
        """Redistribute the processor cases over ``ranks`` cores.

        ``redistributePar -parallel`` on ``max(source, target)`` ranks, on a
        staging copy (v13 writes in place even when it refuses), validated
        against the source and by ``checkMesh -parallel`` before the live
        processor cases are swapped by rename. The core-count setting is
        published last. The case-root mesh is not touched.
        """
        from foammesh.core.jobs import case_lease
        from foammesh.core.jobs import redistribute_transaction as transaction
        from foammesh.openfoam import decomposition
        session.require_writable()
        target = self._redistribute_target(command)
        expected = command.parameters.get('expected_revision')
        settings = self._redistribute_settings(session)

        def refused(code, message=None, **details):
            return self._redistribute_refused(
                transaction.RedistributeError(code, message, details=details))

        async def run():
            try:
                async with case_lease.acquire(
                        session.case_path, case_lease.EXCLUSIVE, command.operation,
                        timeout=0):
                    return await held()
            except case_lease.CaseBusyError as error:
                raise refused('case_busy', holders=[
                    item.get('operation') for item in error.holders]) from None

        async def held():
            if tuple(session.jobs.active_job_ids):
                raise refused('case_busy', jobs=list(session.jobs.active_job_ids))
            await self._settle_redistribute(session, lease_held=True)
            report = await asyncio.to_thread(
                self._redistribute_assess, session, target, expected, settings)
            if report['refusal'] is not None:
                refusal = report['refusal']
                raise refused(refusal['code'], refusal['reason'], **refusal['details'])
            await self._utilities_ready(('redistributePar', 'mpirun', 'checkMesh'))
            registry = self._capabilities_registry()
            if hasattr(registry, 'utility'):
                for name in ('redistributePar', 'mpirun', 'checkMesh'):
                    capability = registry.utility(name)
                    if capability is not None and not capability.available:
                        raise CapabilityUnavailableError(
                            'changing the core count needs the full OpenFOAM '
                            'runtime', details={'utility': name,
                                                'reason': getattr(capability, 'reason', '')})
            face_zones = (report['census'] or {}).get('face_zones') or ()

            def write_dictionary(stage, ranks):
                decomposition.write(stage, ranks, settings, face_zones=face_zones)

            try:
                manifest = await asyncio.to_thread(
                    transaction.prepare, session.case_path, target,
                    write_decompose_dict=write_dictionary,
                    cpu_limit=report.get('cpu_limit'),
                    expected_revision=report['revision'],
                    operation=command.operation)
            except transaction.RedistributeError as error:
                raise self._redistribute_refused(error) from None
            transaction_id = manifest['id']
            try:
                return await launched(manifest, registry)
            except BaseException:
                # Nothing live was renamed before PUBLISHING; a transaction
                # that reached it is settled by recovery, never abandoned.
                current = transaction.load(session.case_path, transaction_id)
                if current is not None and current.get('state') in (
                        transaction.PREPARED, transaction.LAUNCHED,
                        transaction.VALIDATED):
                    try:
                        await asyncio.to_thread(
                            transaction.abandon, session.case_path, transaction_id)
                    except Exception:  # noqa: BLE001 - recovery at open settles it
                        pass
                transaction.release(transaction_id)
                raise

        async def launched(manifest, registry):
            transaction_id = manifest['id']
            stage = transaction.stage_path(session.case_path, transaction_id)
            relative = stage.relative_to(session.case_path).as_posix()
            np = int(manifest['np'])
            # DP-1263. --oversubscribe when np exceeds the physical cores.
            mpi, crowded = _mpi_launch_options(registry, np)
            await asyncio.to_thread(
                transaction.mark, session.case_path, transaction_id,
                transaction.LAUNCHED)
            launch = registry.command(
                'mpirun', (*mpi, '-np', str(np), 'redistributePar', '-parallel'),
                cwd=stage)
            execution = await self._context(session).executor.execute(
                session, OperationSpec(
                    operation=command.operation, argv=launch.argv, cwd=stage,
                    mutation=True, timeout=command.parameters.get('timeout_seconds'),
                    max_output_bytes=4 * 1024 * 1024, recover_mesh=False,
                    artifact_event=Event.ARTIFACT_QUALITY_CHANGED,
                    cleanup_argv=launch.cleanup_argv, expects_mesh_change=False,
                    ranks=np, record_extra=(('staging', relative),
                                            ('redistribute_id', transaction_id))),
                on_line=command.parameters.get('on_line'))
            if not execution.succeeded:
                job = execution.job
                raise refused('launch_failed', (
                    'redistributePar did not finish; the processor cases are '
                    'unchanged'), job=job.to_dict(), error=job.error or '')
            try:
                validated = await asyncio.to_thread(
                    transaction.validate, session.case_path, transaction_id)
            except transaction.RedistributeError as error:
                raise self._redistribute_refused(error) from None
            check_mpi, check_crowded = _mpi_launch_options(registry, target)
            check_launch = registry.command(
                'mpirun', (*check_mpi, '-np', str(target), 'checkMesh',
                           '-parallel'),
                cwd=stage)
            check = await self._context(session).executor.execute(
                session, OperationSpec(
                    operation='mesh.redistribute.check', argv=check_launch.argv,
                    cwd=stage, mutation=False,
                    timeout=command.parameters.get('timeout_seconds'),
                    max_output_bytes=4 * 1024 * 1024,
                    cleanup_argv=check_launch.cleanup_argv,
                    parser=_parse_parallel_check, ranks=target))
            parsed = check.parsed or {}
            cells = validated['census']['cells']
            if not check.succeeded or parsed.get('cells') != cells:
                raise refused('check_failed', job=check.job.to_dict(),
                              expected_cells=cells, found=parsed)
            # checkMesh may have left sets behind: nothing it writes is kept.
            await asyncio.to_thread(
                transaction.strip_unmapped, transaction.processor_dirs(stage))
            await asyncio.to_thread(transaction.publish, session.case_path, transaction_id)
            changed = transaction.apply_setting(
                session, target, reason=f'redistributed {manifest["source_ranks"]} '
                                        f'-> {target} cores')
            await asyncio.to_thread(transaction.finish, session.case_path, transaction_id)
            session.state.bus.publish(
                Event.ARTIFACT_QUALITY_CHANGED, operation=command.operation,
                job_id=execution.job.job_id, artifacts=[], quality='redistributed')
            dropped = validated['dropped']
            crowding = tuple(dict.fromkeys(
                text for text in (crowded, check_crowded) if text))
            return OperationResult(
                'accepted', command.operation, session.revisions,
                invalidated_outputs=('quality',),
                warnings=crowding,
                payload={
                    **({'oversubscribed': True} if crowding else {}),
                    'transaction_id': transaction_id,
                    'source_ranks': manifest['source_ranks'],
                    'target_ranks': target, 'np': np,
                    'census': validated['census'],
                    'removed_surplus': validated['removed_surplus'],
                    'dropped': [{'file': name,
                                 'what': transaction.UNMAPPED.get(name, name)}
                                for name in dropped],
                    # Anything indexed by the old ranks' local cell labels.
                    'stale': ['quality'] + sorted(dropped),
                    'check': {'mesh_ok': bool(parsed.get('mesh_ok')),
                              'failed_checks': parsed.get('failed_checks', 0),
                              'cells': parsed.get('cells')},
                    'settings': changed,
                    'execution': execution.to_payload(),
                    'check_execution': check.to_payload(),
                })

        return run()

    def _mesh_workflow_undo_unlock_preview(self, session: CaseSession,
                                           command: Command) -> OperationResult:
        from foammesh.core.jobs import unlock_transaction
        try:
            preview = unlock_transaction.preview_undo(
                session.case_path, session.state.db)
        except unlock_transaction.UnlockError as error:
            return self._read_result(session, command, {
                'available': False, 'reason': error.code})
        return self._read_result(session, command, dict(preview, available=True))

    def _mesh_workflow_undo_unlock(self, session: CaseSession, command: Command):
        """Restore previous mesh and settings: undo the last unlock whole."""
        from foammesh.core.jobs import unlock_transaction
        session.require_writable()
        engine_id, store = self._task_state_store(session, command)
        record = unlock_transaction.undo_available(session.case_path)
        if record and record.get('engine_id') and record['engine_id'] != engine_id:
            engine_id, store = self._task_state_store(session, Command(
                command.operation, command.case_id,
                {'engine_id': record['engine_id']}))
        loop = asyncio.get_running_loop()

        async def replace_settings(text):
            import yaml
            db = session.state.db
            document = db.validateData(yaml.full_load(text), fillWithDefault=True)
            session.state.replace_document(
                document, action='restore previous mesh and settings')

        def restore_settings(text):
            # Called from the worker thread; the project state belongs to
            # the owner loop, which is free while it awaits this thread.
            asyncio.run_coroutine_threadsafe(
                replace_settings(text), loop).result()

        async def put_surfaces(surfaces):
            self._assign_surfaces(session, surfaces)

        def restore_surfaces(directory):
            surfaces = self._read_surfaces(directory)       # parsed off the loop
            asyncio.run_coroutine_threadsafe(
                put_surfaces(surfaces), loop).result()

        async def run():
            try:
                result = await asyncio.to_thread(
                    unlock_transaction.undo, session.case_path, store,
                    restore_settings=restore_settings,
                    restore_surfaces=restore_surfaces)
            except unlock_transaction.UnlockError as error:
                raise self._unlock_refused(error) from None
            if result.get('restored_mesh'):
                session.state.bus.publish(
                    Event.ARTIFACT_MESH_CHANGED, operation=command.operation,
                    run_id=result['operation_id'])
            session.state.bus.publish(
                Event.ARTIFACT_QUALITY_CHANGED, operation=command.operation,
                job_id=None, artifacts=[], quality=result.get('task_id'))
            return self._read_result(session, command, dict(
                result, engine_id=engine_id, state=store.snapshot()))

        return run()

    # Plan 37 UF5 DP-1063. The geometry surfaces a case holds in memory (the
    # configuration's ``_files``) are part of what undo restores: a geometry
    # edited, re-imported or deleted after an unlock is otherwise left behind
    # by settings that name the surfaces it had before.

    @staticmethod
    def _surface_snapshot(session: CaseSession) -> dict:
        """The surfaces now, by key (a shallow copy; VTK objects are
        replaced, never edited in place, so this is a stable view)."""
        files = getattr(session.state.db, '_files', None) or {}
        return dict(files.get('geometry') or {})

    @staticmethod
    def _write_surfaces(surfaces: dict, directory) -> None:
        from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        index = {}
        for key, poly in sorted(surfaces.items()):
            if poly is None:
                index[key] = None
                continue
            name = f'{key}.vtp'
            writer = vtkXMLPolyDataWriter()
            writer.SetInputData(poly)
            writer.SetFileName(str(directory / name))
            writer.SetDataModeToBinary()
            if not writer.Write():
                raise OSError(f'could not write the surface {key}')
            index[key] = name
        (directory / 'index.json').write_text(
            json.dumps(index, indent=2, sort_keys=True), encoding='utf-8')

    @staticmethod
    def _read_surfaces(directory) -> dict:
        from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
        directory = Path(directory)
        index = json.loads((directory / 'index.json').read_text(encoding='utf-8'))
        surfaces = {}
        for key, name in index.items():
            if name is None:
                surfaces[key] = None
                continue
            reader = vtkXMLPolyDataReader()
            reader.SetFileName(str(directory / name))
            reader.Update()
            surfaces[key] = reader.GetOutput()
        return surfaces

    @staticmethod
    def _assign_surfaces(session: CaseSession, surfaces: dict) -> None:
        """Put the captured surfaces back; one added since is dropped."""
        files = getattr(session.state.db, '_files', None)
        if files is None:
            return
        current = files.setdefault('geometry', {})
        for key in list(current):
            if key not in surfaces:
                current[key] = None
        current.update(surfaces)

    async def _mesh_gmsh_run(self, session: CaseSession,
                             command: Command) -> OperationResult:
        """Deprecated alias for ``workflow.run_pipeline`` (Plan 30 F-03).

        Kept registered because things are bound to the name -- the engine
        branch's run route, an **Accept anyway** recorded against Gmsh's
        :attr:`accept_quality_operation`, a script someone wrote. It is no
        longer a second orchestrator: it dispatches through the seam like
        every other whole-mesh run, so it now runs whichever engine the case
        actually holds rather than Gmsh regardless -- which is what F-02 was.
        """
        logger.warning(
            'mesh.gmsh.run is deprecated; call workflow.run_pipeline, which '
            'runs whichever engine the case configured')
        return await self._workflow_run_pipeline(session, command)

    async def run_gmsh_pipeline(self, session: CaseSession,
                                command: Command) -> OperationResult:
        """Write the job, run Gmsh, publish the mesh, and record what happened.

        The chain is one operation because a mesh that computed but did not
        publish is not a result the user can use, and a publication nothing
        checked is not one they should trust.

        The body of :meth:`GmshMeshingEngine.run`, which is what reaches it.
        """
        session.require_writable()
        import asyncio
        import uuid

        from foammesh.core.engine.registry import ENGINE_REGISTRY
        from foammesh.core.gmsh.execution import ExecutionError, write_job
        from foammesh.core.gmsh.manifest import (
            RunBuilder, RunLayout, RunManifest, sha256_of,
        )
        from foammesh.core.gmsh.quality import (
            REPORT_TASK_ID as _GMSH_GATE_TASK_ID, assess, derive_thresholds,
            refusal_words,
        )
        from foammesh.core.run_result import RunResultHandle, failure_payload

        profile = ENGINE_REGISTRY.get('gmsh').resolve_profile()
        if profile is None:
            raise CapabilityUnavailableError(
                'no qualified Gmsh runtime is configured; Gmsh runs inside a '
                'WSL distribution and none was found')

        # F-12. One readiness predicate for both engines. snappy prepared the
        # geometry for the user with defaults and Gmsh refused to start until
        # they had done it by hand, so the same imported case was ready in
        # one engine and blocked in the other.
        prepared = await asyncio.to_thread(
            _ensure_prepared_geometry, session, producer='mesh.gmsh.run',
            # DP-673. A 2D or axisymmetric section bounds no volume by
            # design; asking it to refused every section not prepared by hand.
            require_domain=not _meshes_a_section(session.state.db))
        if prepared is None:
            raise PreconditionFailedError(
                'import geometry before running Gmsh; there is nothing to '
                'prepare or mesh')

        # Plan 33 W-G1 (FIELD-06, CURVE-04). Every enabled control that names
        # geometry is resolved against the revision about to be meshed, here,
        # before a run directory exists. The runner used to be where this was
        # found -- inside WSL, minutes in, after the geometry had imported --
        # and for a per-surface row naming its own Gmsh tag it was not found
        # at all: the run published a mesh with the refinement silently
        # missing.
        _refuse_unresolved_scopes(session, prepared)

        run_id = str(command.parameters.get('run_id')
                     or f'gmsh-{uuid.uuid4().hex[:16]}')
        # DP-676. What the run writes follows from the saved case alone: the
        # MSH, and the SU2 on an SU2 case. It used to read a `formats` list
        # off the command that nothing could send; MED and UNV are written by
        # the Export page's Gmsh conversion.
        try:
            written = write_job(
                session.state.db, _prepared_bbox(prepared), session.case_path,
                prepared_geometry=prepared.reference, profile=profile,
                run_id=run_id)
        except ExecutionError as error:
            raise ValidationFailedError(str(error)) from error
        _require_every_prepared_source(prepared, written['job'])

        layout = RunLayout(RunBuilder(session.case_path).root(run_id))
        record = RunManifest.read(layout.root)
        launch = profile.runner_argv(
            _gmsh_runner_path(), layout.job, cwd=session.case_path)
        RunBuilder.record_launch(record, launch)

        spec = OperationSpec(
            operation=command.operation,
            argv=launch.argv, cwd=session.case_path,
            timeout=_stage_timeout(command.parameters, 3600),
            max_output_bytes=8 * 1024 * 1024,
            log_path=layout.log,
            cleanup_argv=launch.cleanup_argv,
            invalidated_outputs=('quality', 'exports'))
        execution = await self._context(session).executor.execute(
            session, spec, on_line=command.parameters.get('on_line'))

        # DP-56. A segmentation fault is the one failure with nothing in it
        # for the user, and the thread count is what decides it. Retried here
        # rather than reported, once, and only once: the second attempt has
        # one thread, so `retry_at_one_thread` refuses to arm again.
        from foammesh.core.jobs.retry import attempt_of
        attempt = attempt_of(command.parameters)
        retry = retry_at_one_thread(layout, written['job'],
                                    execution.to_payload(), attempt=attempt)
        if retry:
            record.document['job_sha256'] = sha256_of(layout.job)
            record.document.setdefault('warnings', []).append(retry)
            record.write()
            execution = await self._context(session).executor.execute(
                session, spec, on_line=command.parameters.get('on_line'))

        result = _read_runner_result(layout)
        payload = execution.to_payload()
        # DP-12. `to_payload()` has never carried an `exit_code` key -- the
        # process status lives under `job.returncode` -- so this recorded None
        # on every Gmsh run this repository has ever made, succeeded and
        # failed alike, and the manifest's exit-code column was a facade.
        RunBuilder.record_result(
            record, result,
            exit_code=(payload.get('job') or {}).get('returncode'))
        # Plan 31 CP-05 item 7. The runner writes no result file when it is
        # stopped, so `_read_runner_result` hands back its "produced no result
        # file" stand-in and the run was persisted as *failed*: MEASURED, a
        # cancelled run and a broken one were the same two fields on disk, and
        # the Runs list offered the user no way to tell a mesh they stopped
        # from a mesh that could not be built. The job knows which happened.
        cancelled = str((payload.get('job') or {}).get('status') or '') == 'cancelled'
        if cancelled:
            RunBuilder.record_cancelled(
                record, 'the run was cancelled before it finished')
        payload.update({'run_id': run_id, 'run_manifest': record.document,
                        'job_warnings': list(written.get('warnings') or ()),
                        # F-37. Named here, before the quality gate below can
                        # return: a refused run never publishes, so its mesh
                        # is the one in its own directory and `constant/
                        # polyMesh` still belongs to whichever run was last
                        # accepted. The viewport draws what this says.
                        **RunResultHandle.identity_payload(
                            run_id=run_id, engine='gmsh',
                            artifact=layout.mesh, run_path=layout.root)})

        if not execution.succeeded or result.get('status') != 'succeeded':
            # One failure shape, whichever engine wrote it (Plan 30 F-03);
            # `RunResultHandle.from_payload` is the reader of it.
            # DP-30. The exit code is the only thing that separates a crash
            # from a refusal here -- both arrive as a missing result file --
            # and it is already in the payload, recorded on the manifest two
            # statements above.
            crashed = runner_crash_reason(
                (payload.get('job') or {}).get('returncode'),
                # DP-56. The remedy depends on how many threads this attempt
                # had, and after a retry that is one -- so the message must
                # read the job as it now stands, not as it was written.
                threads=int((((written['job'].get('intent') or {})
                              .get('parallel') or {})
                             .get('threads', 0)) or 0))
            payload.update(failure_payload(
                task='gmsh.compute',
                reason=('the run was cancelled before it finished' if cancelled
                        else crashed
                        or result.get('error') or 'the Gmsh run failed'),
                log=str(layout.log)))
            payload['cancelled'] = cancelled
            if not cancelled:
                # DP-506. The long text behind the failure's Details, read
                # from this run's log the way a snappy stage's is.
                from foammesh.core.run_result import read_failure_cause
                payload['details'] = read_failure_cause(layout.log)[1]
                # Plan 35 CR6. A lost WSL relay or a memory kill is offered a
                # retry; the thread retry above already spent this run's one.
                from foammesh.core.jobs.retry import retry_offer
                offer = retry_offer(
                    (payload.get('job') or {}).get('exit'),
                    attempt=attempt + (1 if retry else 0),
                    fatal='FOAM FATAL' in str(payload.get('details') or ''))
                if offer is not None:
                    payload['retry'] = offer
            return OperationResult('failed', command.operation,
                                   session.revisions, payload=payload)

        thresholds = derive_thresholds(
            ((written['job'].get('intent') or {}).get('quality')) or {})
        verdict = assess(
            thresholds, (result.get('statistics') or {}).get('achievedQuality'))
        RunBuilder.record_quality_verdict(record, verdict)
        payload['quality_verdict'] = verdict.to_dict()
        # Written whatever the verdict, including a pass: the report is what a
        # waiver binds to, and one that only appeared on failure would give a
        # user no way to see the numbers behind a mesh that passed.
        payload['quality_report'] = self._write_quality_report(
            session, record, verdict, thresholds,
            # R158. The revision lives on the *reference*, not on the result
            # -- `PreparedGeometryResult` has no `revision_id` at all, so the
            # getattr default fired every time and the report (and every
            # waiver bound to it) recorded `prepared_revision: ""` while
            # `evidence.json` for the same run held `pg-1445135f6c5e21a8`.
            # That empty field is the link an auditor follows back to the
            # geometry the decision was granted against. Line 1174 above
            # already reads the reference for exactly this reason.
            prepared_revision=str(
                getattr(prepared.reference, 'revision_id', '') or ''))
        # The gate reads what the mesh achieved. Accepting anyway is the
        # user's call and is recorded as such -- but only where the verdict
        # permits it. An inverted or zero-volume element is `invalid`, not
        # `fail`: there is nothing for a human to consent to, because no
        # solver can integrate over it. `accept_quality` must not reach it.
        accept = bool(command.parameters.get('accept_quality'))
        if not verdict.accepted and accept and not verdict.overridable:
            # DP-94. The remedy the gate found travels with the refusal.
            reason = (
                f'{refusal_words(verdict)} This verdict is '
                f'{verdict.verdict}, which cannot be accepted; re-mesh '
                'instead.')
            RunBuilder.record_publication_failure(record, reason)
            payload.update(failure_payload(
                task=_GMSH_GATE_TASK_ID, reason=reason, log=str(layout.log),
                built=True))
            payload['override_refused'] = True
            return OperationResult('failed', command.operation,
                                   session.revisions, payload=payload)
        if not verdict.accepted and not accept:
            # DP-94. The gate wrote the remedy into the verdict's warnings
            # and the failure carried only the count, so the one sentence
            # that says what to do next never reached the window.
            refusal = refusal_words(verdict)
            RunBuilder.record_publication_failure(record, refusal)
            payload.update(failure_payload(
                task=_GMSH_GATE_TASK_ID, reason=refusal,
                log=str(layout.log), built=True))
            return OperationResult('failed', command.operation,
                                   session.revisions, payload=payload)
        if not verdict.accepted:
            # WP1.4. An override leaving no trace is the same defect class as a
            # gate certifying layers a mesh does not have, so the decision goes
            # on the manifest beside the verdict it overrode.
            # Bound to `gmsh.compute`, this engine's own element gate, and not
            # to `gmsh.qa`: the decision is taken here, before the mesh is
            # published, so there is no polyMesh for checkMesh to have opened
            # and nothing else for the waiver to cover.
            payload['quality_override'] = self._record_quality_override(
                session, record, verdict, engine_id='gmsh',
                task_id=_GMSH_GATE_TASK_ID,
                reason=str(command.parameters.get('accept_reason') or ''))

        # Plan 31 CP-05 item 4. The rest of this run is the same work
        # accepting a stored candidate does, so it lives in one place.
        return await self._finish_gmsh_run(
            session, command, record, prepared, payload,
            accepted=verdict.accepted,
            warning=bool(verdict.warnings if hasattr(verdict, 'warnings')
                         else False))


    async def _finish_gmsh_run(self, session: CaseSession, command: Command,
                               record, prepared, payload: dict, *,
                               accepted: bool, warning: bool = False
                               ) -> OperationResult:
        """Turn one judged Gmsh run into the case's mesh, exports and tasks.

        Lifted out of :meth:`run_gmsh_pipeline` unchanged so that accepting a
        *stored* run can reach it (Plan 31 CP-05 item 4). Everything above this
        point in the pipeline decides whether there is a mesh and what the gate
        made of it; everything here is what the case then does with it, and it
        is identical whether the verdict was reached a second ago or a week
        ago. Copying it into a second orchestrator is what Plan 30 F-03
        removed, so there is one.

        ``accepted`` is the gate's own answer, not the user's: a run the user
        waived arrives here with ``accepted=False`` and is recorded as waived
        rather than passed, which is what draws the outline's ``waived`` glyph
        instead of a plain tick.
        """
        from foammesh.core.gmsh.manifest import RunBuilder
        from foammesh.core.run_result import failure_payload

        # Plan 30 WP-07 (F-36), reopened and rescoped by Plan 31 CP-01
        # (C31-01). The mesh Gmsh wrote is the run's result. Everything below
        # is about what is *additionally* made from it, and none of it may
        # decide whether the run produced a mesh:
        #
        #   * the optional ``.su2`` export is judged on its own file, so a
        #     failed exporter is reported as a failed export rather than
        #     leaving a valid MSH to stand in for it;
        #   * the polyMesh publication runs only for the targets whose export
        #     *is* a polyMesh, and only from an MSH version its reader accepts
        #     -- the derivation guarantees the pairing, which is the defect
        #     this closes: a first-order SU2 case wrote MSH 4.1 and was then
        #     handed to a publisher that reads 2.2;
        #   * with no target chosen there is no export anyone asked for, so a
        #     publisher failure is recorded and the native mesh survives it.
        # Check 5. What the run grew, written before anything is made from the
        # mesh, so a publication that fails still leaves the layer measurement
        # the run took.
        coverage = self._record_gmsh_layer_coverage(session, record)
        if coverage:
            payload['layer_coverage'] = coverage

        export_settings = _job_export_settings(record)
        target = str(export_settings.get('targetSolver') or 'unselected')
        if export_settings.get('writesSu2'):
            payload['su2_export'] = self._su2_export_receipt(record)
        published = False
        if export_settings.get('publishesPolyMesh', True):
            try:
                payload['publication'] = self._publish_gmsh_result(
                    session, record, prepared)
            except ValidationFailedError as error:
                if target != 'unselected':
                    raise
                payload['publication'] = {
                    'status': 'failed', 'artifact': str(record.layout.mesh),
                    'reason': (
                        f'{error} No target solver is selected, so nothing '
                        'asked for a polyMesh; the Gmsh mesh itself is in the '
                        'run directory and can be inspected and saved.')}
            else:
                published = True
        if published:
            check = await self._check_published_gmsh_mesh(session, command)
            payload['check_mesh'] = check
        else:
            census = self._census_native_gmsh_mesh(record)
            if _job_is_planar(record):
                # DP-674. A planar section's cells are its faces.
                from dataclasses import replace as _replace
                census = _replace(census, dimension=2)
            payload['element_census'] = census.to_dict()
            if not census.has_cells:
                reason = census.read_error or (
                    'the mesh has no volume elements of any order; Gmsh '
                    'produced a surface mesh, which usually means the CAD '
                    'imported without solids')
                RunBuilder.record_publication_failure(record, reason)
                payload.update(failure_payload(
                    task='gmsh.publish', reason=reason, log=str(record.layout.log),
                    built=True))
                return OperationResult('failed', command.operation,
                                       session.revisions, payload=payload)
            if payload.get('publication', {}).get('status') == 'failed':
                RunBuilder.record_publication_failure(
                    record, payload['publication']['reason'])
            else:
                reason = self._no_publication_reason(export_settings)
                RunBuilder.record_publication(record, {
                    'status': 'skipped', 'reason': reason,
                    'element_census': census.to_dict()})
                payload['publication'] = {
                    'status': 'skipped', 'reason': reason,
                    'artifact': str(census.path),
                    'summary': census.summary}
            check = {'ran': False, 'status': 'skipped',
                     'reason': 'checkMesh reads a polyMesh, and this run '
                               'published none'}
            payload['check_mesh'] = check
        # One native Gmsh run satisfies the whole engine chain, so the workflow
        # has to be told. Without this the state machine never advances and QA
        # stays "locked by prerequisites" behind a mesh that already exists.
        # QA is only claimed when checkMesh really ran; a runtime without it
        # leaves the task to be run rather than marking it passed.
        # DP-817. Each warning belongs to the task that raised it: the element
        # gate's to compute, a checkMesh that did not accept the mesh to QA.
        # Both used to be one flag over every task the run covered.
        warning_for = {'gmsh.compute'} if warning else set()
        reasons = []
        if check.get('ran') and check.get('status') != 'accepted':
            warning_for.add('gmsh.qa')
            reasons.append('checkMesh did not accept the mesh: {0}'.format(
                check.get('status') or 'no verdict'))
        warning = bool(warning_for)
        # R119/R158. The compute task is recorded as *waived*, not passed, when
        # its own gate refused and a human overrode it. The run used to record
        # PASSED regardless, so the outline painted the plain COMPLETED tick
        # over a recorded override and the `⚑` WAIVED glyph the app already
        # defines was never drawn for the one case it exists for.
        waived = () if accepted else ('gmsh.compute',)
        payload['task_state'] = self._record_engine_run_success(
            session, command, warning=warning, waived=waived,
            warning_for=tuple(sorted(warning_for)), reasons=tuple(reasons),
            configured=_job_configured_tasks(record),
            exclude=() if check.get('ran') else ('gmsh.qa',))
        return self._artifact_result(session, command, payload,
                                     invalidates=('quality', 'exports'))

    @staticmethod
    def _census_native_gmsh_mesh(record):
        """Count the elements in the mesh this run produced.

        Plan 30 WP-07 (F-36), corrected by Plan 31 CP-01 (C31-01). It used to
        census ``mesh.su2`` whenever that file existed, which made the run's
        result a statement about an *export*: a run whose native mesh was fine
        and whose SU2 write was truncated could be counted against the
        truncated file, and a run whose SU2 write never happened at all was
        counted against the MSH and reported as though the export had
        succeeded. The native MSH is the run's artifact, so it is the one
        counted here; :meth:`_su2_export_receipt` judges the export separately.
        """
        from foammesh.core.mesh.census import element_census

        return element_census(record.layout.mesh)

    @staticmethod
    def _su2_export_receipt(record) -> dict:
        """Whether the requested ``mesh.su2`` exists and holds a mesh.

        Plan 31 CP-01 (C31-01), step 5: a valid MSH beside a missing ``.su2``
        does not make the SU2 export successful. Nothing here changes whether
        the *run* succeeded -- a failed optional exporter leaves a native mesh
        that is still generated, inspectable and saveable.
        """
        from foammesh.core.mesh.census import element_census

        path = record.layout.su2
        if not path.is_file():
            return {'status': 'failed', 'artifact': str(path), 'reason': (
                'the SU2 export was requested but no file was written; the '
                'Gmsh mesh itself is in the run directory')}
        census = element_census(path)
        # DP-674: a planar SU2 file (NDIME= 2) holds faces as its cells.
        if census.read_error or not census.has_cells:
            return {'status': 'failed', 'artifact': str(path),
                    'reason': census.read_error or (
                        f'{path.name} holds no volume elements; the SU2 write '
                        'did not produce a usable mesh'),
                    'element_census': census.to_dict()}
        return {'status': 'succeeded', 'artifact': str(path),
                'summary': census.summary,
                'warnings': list(census.warnings),
                'element_census': census.to_dict()}

    @staticmethod
    def _no_publication_reason(export_settings: dict) -> str:
        """Why this run published no polyMesh, in the user's own terms.

        One sentence per actual cause. "The target solver reads the mesh file
        directly" was the only one on offer and it was wrong for the case that
        now reaches here most often: no target selected and a second-order
        mesh, where nothing was asked for and nothing is missing.
        """
        target = str(export_settings.get('targetSolver') or 'unselected')
        order = int(export_settings.get('elementOrder') or 1)
        if order > 1:
            return ('the mesh is second order and constant/polyMesh is a '
                    'first-order format, so the run kept its native MSH '
                    'instead; it is counted, inspectable and saveable')
        if target == 'su2':
            return ('the SU2 route reads the mesh file Gmsh wrote, so no '
                    'polyMesh was published for it')
        return ('no polyMesh was published for this run; the native Gmsh mesh '
                'is the result')

    async def _mesh_run_accept(self, session: CaseSession,
                               command: Command) -> OperationResult:
        """Accept the candidate being inspected, without re-meshing it.

        Plan 31 CP-05 item 4: *Accept must operate on the exact candidate
        being inspected. It must not launch a different engine or silently
        regenerate a different candidate.*

        MEASURED before this existed. **Accept anyway** resolved
        ``accept_quality_route`` and, for Gmsh, called ``mesh.gmsh.run`` with
        ``accept_quality: True`` -- which writes a fresh job, launches Gmsh
        again under a new ``run_id``, and records the decision against *that*
        run: about 90 s, by the app's own comment. The candidate the user had
        inspected and decided about stayed refused on disk for ever, the waiver
        named a run nobody had looked at, and anything changed between
        inspecting and accepting silently went into the mesh that got accepted.
        Plan 30 F-02 fixed the other half of the same sentence -- Accept used
        to run Gmsh over a snappy mesh -- and this is the rest of it.

        Nothing is re-derived. The run already recorded the verdict it was
        judged by, the report that verdict was written into, the geometry
        revision it was meshed from and the export intent it was launched
        with; all four are read back, and what follows is
        :meth:`_finish_gmsh_run`, the same tail the run itself would have run.

        The rules item 3 keeps non-overridable are kept as they are in the
        pipeline: a run that built no mesh has nothing to accept, and an
        ``invalid`` verdict -- an inverted or zero-volume element -- is refused
        here too, because no solver can integrate over it.
        """
        from foammesh.core.engine.base import accept_quality_route
        from foammesh.core.gmsh.manifest import (
            ManifestError, RunBuilder, RunManifest,
        )
        from foammesh.core.gmsh.quality import REPORT_TASK_ID as _GATE_TASK_ID
        from foammesh.core.quality import binding

        run_id = str(command.parameters.get('run_id') or '').strip()
        if not run_id:
            raise ValidationFailedError(
                'run_id is required: an acceptance names the candidate it is '
                'about')
        session.require_writable()
        try:
            record = RunManifest.read(RunBuilder(session.case_path).root(run_id))
        except (ManifestError, OSError, ValueError) as error:
            raise PreconditionFailedError(
                'this run was not found, so there is no candidate to accept',
                details={'run_id': run_id}) from error

        engine_id = str(record.document.get('engine_id') or '')
        # Asked of the engine, not decided by its name (F-03's rule). An
        # engine serves this route only if it names it: snappy meshes the case
        # in place, so its candidate *is* the published mesh and its
        # acceptance is recorded against that mesh by its own QA route.
        # Publishing a run directory at it would publish a mesh already there.
        if accept_quality_route(engine_id) != command.operation:
            raise ValidationFailedError(
                f'{engine_id or "this engine"} does not keep candidates in '
                'run directories; its acceptance is recorded against the mesh '
                'it published')
        if not record.layout.mesh.is_file():
            raise PreconditionFailedError(
                'this run built no mesh, so there is nothing to accept',
                details={'run_id': run_id})

        stored = dict(record.document.get('quality_verdict') or {})
        if not stored:
            raise PreconditionFailedError(
                'this run recorded no quality verdict, so there is no '
                'decision to take about it', details={'run_id': run_id})
        accepted = bool(stored.get('accepted'))
        payload = {'run_id': run_id, 'engine_id': engine_id,
                   'quality_verdict': stored}
        if not accepted and not stored.get('overridable'):
            reason = (
                f'{stored.get("reason") or ""} This verdict is '
                f'{stored.get("verdict") or "invalid"}, which cannot be '
                'accepted; re-mesh instead.').strip()
            payload['override_refused'] = True
            payload['reason'] = reason
            return OperationResult('failed', command.operation,
                                   session.revisions, payload=payload)

        report = binding.run_report(session.case_path, run_id)
        if report is None:
            raise PreconditionFailedError(
                'no quality report is bound to this run, so an acceptance '
                'could not be bound to the mesh it covers',
                details={'run_id': run_id})
        if not accepted:
            # Onto the run's own manifest, which is what makes the accepted
            # candidate readable afterwards: `run_disposition` reads a refused
            # verdict as `rejected` unless the override sits beside it.
            payload['quality_override'] = self._record_quality_override(
                session, record, None, engine_id='gmsh',
                task_id=_GATE_TASK_ID, report=report,
                reason=str(command.parameters.get('accept_reason') or ''))
        return await self._finish_gmsh_run(
            session, command, record, self._prepared_for_run(session, report),
            payload, accepted=accepted)

    def _prepared_for_run(self, session: CaseSession, report: dict):
        """The prepared revision *this run* was meshed from.

        The report names it -- R158 made sure of that, because it is the link
        an auditor follows back to the geometry a decision was granted
        against. Publishing a stored run against whatever revision is current
        instead would name its patches after geometry it was never meshed
        from. The current revision is the fallback only for a run that named
        none, which is a run written before that field was populated.
        """
        from foammesh.core.geometry.prepared import (
            PreparedGeometryError, PreparedGeometryStore,
        )

        revision = str(report.get('prepared_revision') or '').strip()
        if revision:
            try:
                return PreparedGeometryStore(session.case_path).load(revision)
            except (PreparedGeometryError, FileNotFoundError, OSError,
                    ValueError):
                logger.warning(
                    'run was meshed from prepared revision %s, which could '
                    'not be read; publishing against the current one',
                    revision)
        return self._current_prepared(session)

    async def _check_published_gmsh_mesh(self, session: CaseSession,
                                         command: Command) -> dict:
        """checkMesh on the mesh the Gmsh run just published.

        ``ATOMIC_RUN_TASKS`` names ``gmsh.qa`` as something the run performs
        and the engine declares a checkMesh stage for it, but the run never
        launched checkMesh: QA showed "passed" on a mesh OpenFOAM had never
        opened, no ``quality/latest.json`` existed, and the summary's
        mesh-quality block stayed incomplete. MEASURED on the live Gmsh walk.
        The Gmsh-native SICN gate is kept as what it is -- the generator's
        own view -- and checkMesh is what the solver will say.
        """
        from dataclasses import replace as _replace

        # Plan 28 WP4. Which check, per the target solver: a Gmsh user meshing
        # for SU2 usually has no OpenFOAM at all, and running checkMesh at
        # them left `gmsh.qa` unrun on a mesh that was in fact fine.
        operation = self._qa_operation(session)
        inner = _replace(command, operation=operation, parameters={
            key: value for key, value in dict(command.parameters).items()
            if key in ('on_line', 'timeout_seconds')})
        handler = (self._quality_su2_readiness
                   if operation == 'quality.su2_readiness' else self._mesh_check)
        try:
            result = await handler(session, inner)
        except FacadeError as error:
            # DP-50. Named, because the record was a dict with no status in it
            # and every reader had to guess. A check that could not run is not
            # a check that failed: the mesh was published and nobody has an
            # opinion on it yet. Callers key off this to leave `gmsh.qa` to be
            # run rather than marking it passed or refused.
            return {'ran': False, 'status': 'unchecked', 'reason': str(error),
                    'check': operation,
                    'details': dict(getattr(error, 'details', None) or {})}
        payload = result.payload or {}
        report = payload.get('parsed') if isinstance(
            payload.get('parsed'), dict) else {}
        verdict = report.get('result') if isinstance(
            report.get('result'), dict) else {}
        if not verdict and isinstance(payload.get('readiness'), dict):
            verdict = payload['readiness']
        return {'ran': True, 'status': result.status, 'check': operation,
                'job_id': payload.get('job_id'),
                'mesh_ok': verdict.get('mesh_ok'),
                'severity': verdict.get('severity'),
                'warnings': list(result.warnings or ())}

    @staticmethod
    def _staged_surface_labels(case_path) -> dict[str, str]:
        """Staged surface stem -> the name the user gave that geometry.

        R136. The staged tessellation is written as
        ``surface_<geometry uuid>.stl``, so everything derived from the file
        name carries the uuid: the extracted-edges table listed its one row as
        ``surface_0ff6ff6343d247a39df11ad5dffa94ce`` for a geometry the
        outline, the Repair tree and the Region page all call ``annulus``.
        The store has held the name all along.
        """
        from foammesh.core.geometry import GeometryArtifactStore

        labels: dict[str, str] = {}
        try:
            entries = GeometryArtifactStore(case_path).entries()
        except Exception:                                    # noqa: BLE001
            return labels
        for entry in entries:
            geometry_id = str(entry.get('geometry_id') or '')
            name = str(entry.get('name') or '').strip()
            if geometry_id and name:
                labels[f'surface_{geometry_id}'] = name
        return labels

    def _mesh_feature_edges(self, session: CaseSession,
                            command: Command) -> OperationResult:
        """List what ``surfaceFeatures`` extracted, and optionally its geometry.

        ``include_segments`` returns the edges as coordinate pairs for the
        renderer. Off by default: a feature mesh on a large model is tens of
        thousands of segments, and a listing that always paid for them would
        make the page slow for the common case of counting them.
        """
        from foammesh.core.mesh.emesh_reader import discover_feature_edges

        found = discover_feature_edges(session.case_path)
        labels = self._staged_surface_labels(session.case_path)
        include = bool(command.parameters.get('include_segments'))
        # Plan 37 #2(a). A surface whose solids take different feature levels
        # is also extracted piece by piece (``<stem>_features_<n>.eMesh``).
        # The pieces are the same edges again, cut along the solids, so they
        # are listed apart: as rows of their own they doubled the edge count
        # and showed the uuid stem the table exists to hide.
        stems = {item.name for item in found}
        pieces, surfaces = [], []
        for item in found:
            base, infix, index = item.name.rpartition('_features_')
            if infix and base in stems and index.isdigit():
                pieces.append({
                    **item.to_dict(), 'piece_of': base,
                    'display_name': '%s, feature piece %s' % (
                        labels.get(base, base), index)})
            else:
                surfaces.append(item)
        payload = {
            # R136. The row is keyed by the staged file's stem, which is the
            # prepared-geometry uuid; the name beside it is what the user
            # called the surface everywhere else in the app.
            'surfaces': [{**item.to_dict(),
                          'display_name': labels.get(item.name, item.name)}
                         for item in surfaces],
            'total_edges': sum(item.edge_count or 0 for item in surfaces),
            # A binary .eMesh is listed but not counted; saying so is the
            # difference between "no edges" and "edges we did not read".
            'unreadable': [item.name for item in surfaces if item.binary],
            'pieces': pieces,
        }
        if include:
            payload['segments'] = {
                item.name: [[list(start), list(end)]
                            for start, end in item.segments()]
                for item in surfaces}
        return self._read_result(session, command, payload)

    def _quality_cell_fields(self, session: CaseSession,
                             command: Command) -> OperationResult:
        """Per-cell quality arrays, their summary, and one distribution.

        ``metric`` selects which array the histogram describes; the summary
        covers all four either way, because the acceptance criterion is that
        each on-screen maximum equals the log maximum and a caller must be able
        to check all four without four round trips.

        ``include_values`` returns the arrays themselves for the renderer. Off
        by default: a million cells is eight megabytes per metric, and a
        summary caller should not pay for that.
        """
        from foammesh.core.mesh.poly_mesh_boundary import (
            PolyMeshReadError, read_poly_mesh,
        )
        from foammesh.core.quality.cell_fields import (
            CALCULATION_VERSION, FIELD_NAMES, compute_cell_fields, histogram,
            summarise,
        )

        try:
            mesh = read_poly_mesh(session.case_path, layout_expectation='any')
        except PolyMeshReadError as error:
            raise PreconditionFailedError(
                f'the case has no readable mesh to measure: {error}') from error

        fields = compute_cell_fields(mesh)
        metric = str(command.parameters.get('metric') or FIELD_NAMES[1])
        if metric not in fields:
            raise ValidationFailedError(
                f'unknown quality metric {metric!r}; expected one of '
                f'{", ".join(FIELD_NAMES)}')
        bins = int(command.parameters.get('bins', 40) or 40)
        payload = {
            'metric': metric,
            'summary': summarise(fields),
            'histogram': histogram(fields[metric], bins=bins),
            'cells': mesh.cell_count,
            'calculation_version': CALCULATION_VERSION,
            # Named so a caller can tell "checkMesh could not write these" from
            # "this build computes them itself". Foundation v13 has no
            # -writeAllFields; the arrays are ours.
            'source': 'foammesh',
        }
        if command.parameters.get('include_values'):
            # `tolist()` rather than a comprehension over `float()`: same JSON
            # -safe Python floats, measured four times faster, and this array
            # is one entry per cell on a mesh whose whole point is being large.
            payload['values'] = {name: values.tolist()
                                 for name, values in fields.items()}
        return self._read_result(session, command, payload)

    def _quality_mesh_report(self, session: CaseSession,
                             command: Command) -> OperationResult:
        """Compose one self-contained HTML report from what already exists.

        Every figure is read from the artefact that measured it. Nothing here
        recomputes: a report that recalculated would eventually disagree with
        the gate that made the decision, and the disagreement would surface as
        an argument about which number was right.
        """
        import datetime

        from foammesh.core.quality.mesh_report import (
            DEFAULT_REPORT_PATH, compose, worst_verdict,
        )

        case = Path(session.case_path)
        destination = Path(command.parameters.get('destination')
                           or case / DEFAULT_REPORT_PATH)
        if destination.suffix.lower() not in ('.html', '.htm'):
            raise ValidationFailedError(
                'the mesh report is an HTML document; use quality.report.export '
                'for .json or .csv')

        engine_id = self._engine_id(session)
        checkmesh = self._current_report(session, 'checkmesh') or {}
        summary = self._read_json(case / 'foammesh' / 'quality' / 'summary.json')
        mesh_quality = self._current_report(session, 'gmsh.compute') or {}
        layers = self._read_json(case / self.LAYER_COVERAGE_PATH) or {}
        manifest = self._read_json(
            case / 'system' / 'foammesh-dictionaries-manifest.json') or {}

        verdict = worst_verdict(
            summary.get('disposition') or summary.get('verdict'),
            checkmesh.get('verdict'), mesh_quality.get('gate_verdict'))
        report = compose(
            header={
                'case': case.name,
                'generated': datetime.datetime.now(
                    datetime.timezone.utc).isoformat(timespec='seconds'),
                'engine': engine_id,
                'runtime_fingerprint': (mesh_quality.get('checkpoint_fingerprint')
                                        or manifest.get('configuration_sha256')),
                'configuration_revision': manifest.get('configuration_sha256'),
            },
            geometry=self._report_geometry(session),
            repair_history=self._report_repair_history(session),
            settings=self._report_settings(mesh_quality, manifest),
            result=self._report_result(session, checkmesh),
            quality=self._report_quality(checkmesh, mesh_quality),
            layers=layers,
            warnings=list(manifest.get('warnings') or ())
                     + list(mesh_quality.get('warnings') or ()),
            verdict=verdict,
            reason=(mesh_quality.get('reason')
                    or summary.get('reason') or ''))
        path = report.write(destination)
        return self._artifact_result(session, command, {
            'path': str(path), 'verdict': verdict,
            'bytes': path.stat().st_size})

    @staticmethod
    def _read_json(path):
        import json as _json

        try:
            return _json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _report_geometry(self, session: CaseSession) -> dict:
        from foammesh.core.geometry import GeometryArtifactStore

        try:
            entries = GeometryArtifactStore(session.case_path).entries()
        except Exception:                                    # noqa: BLE001
            return {}
        if not entries:
            return {}
        entry = entries[0]
        return {
            'sources': [str(item.get('name') or item.get('artifact'))
                        for item in entries],
            'declared_unit': entry.get('declared_unit') or entry.get('unit'),
            'interpreted_unit': 'm',
            'bbox': entry.get('bbox') or (),
            'prepared_revision': entry.get('revision'),
            'patches': [{'name': item.get('solver_name') or item.get('name'),
                         'category': item.get('category') or item.get('type')}
                        for item in (entry.get('patches') or ())],
        }

    def _report_repair_history(self, session: CaseSession) -> list:
        from foammesh.core.geometry import GeometryArtifactStore

        try:
            entries = GeometryArtifactStore(session.case_path).entries()
        except Exception:                                    # noqa: BLE001
            return []
        history = []
        for entry in entries:
            for revision in entry.get('revisions') or ():
                history.append({
                    'revision': revision.get('revision'),
                    'kind': revision.get('kind'),
                    'action': revision.get('action') or revision.get('operation'),
                    'fingerprint': str(revision.get('fingerprint') or '')[:16],
                })
        return history

    @staticmethod
    def _report_settings(mesh_quality: dict, manifest: dict) -> dict:
        thresholds = (mesh_quality.get('thresholds') or {})
        settings = {name: value for name, value in thresholds.items()
                    if not isinstance(value, (dict, list))}
        if manifest.get('target'):
            settings['OpenFOAM target'] = manifest['target']
        return settings

    def _report_result(self, session: CaseSession, checkmesh: dict) -> dict:
        result = {name: checkmesh.get(name)
                  for name in ('cells', 'faces', 'points')
                  if checkmesh.get(name) is not None}
        if result:
            return result
        # No checkMesh report: read the mesh itself rather than reporting
        # nothing, since the counts are what a reader looks for first.
        try:
            from foammesh.core.mesh.poly_mesh_boundary import read_poly_mesh

            mesh = read_poly_mesh(session.case_path, layout_expectation='any')
        except Exception:                                    # noqa: BLE001
            return {}
        return {'cells': mesh.cell_count, 'faces': mesh.face_count,
                'points': int(mesh.points.shape[0])}

    @staticmethod
    def _report_quality(checkmesh: dict, mesh_quality: dict) -> dict:
        checks = [dict(item) for item in (checkmesh.get('checks') or ())]
        for metric in mesh_quality.get('metrics') or ():
            checks.append({
                'name': f'element {metric.get("measure")}',
                'value': metric.get('achievedMinimum'),
                'verdict': metric.get('verdict'),
                'detail': metric.get('reason') or '',
            })
        return {'checks': checks, 'sets': checkmesh.get('sets') or ()}

    def _write_quality_report(self, session: CaseSession, record, verdict,
                              thresholds, *, prepared_revision: str) -> dict:
        """Persist the mesh-quality report the GUI reads and a waiver binds to.

        The five evidence keys are the ones :mod:`core.quality.waiver` requires
        and every one of them is real: the job digest, the mesh hash, the
        prepared revision, the limits it was judged against and this gate's
        calculation version. Nothing is synthesised to satisfy the check --
        that would defeat the property the waiver exists for, which is that a
        decision stops covering a mesh once the mesh changes.
        """
        import json as _json
        from foammesh.core.gmsh.manifest import RunBuilder
        from foammesh.core.gmsh.quality import REPORT_PATH, build_report

        document = build_report(
            verdict, thresholds,
            subject_mesh_fingerprint=str(
                record.document.get('mesh_sha256') or ''),
            checkpoint_fingerprint=str(record.document.get('job_digest') or ''),
            prepared_revision=prepared_revision)
        # CP-05 item 2. The report names the artifact it is about. Without it
        # the case-level file said what was measured and not what was
        # measured, so nothing reading it back -- an acceptance above all --
        # could tell which candidate it belonged to.
        document['run_id'] = str(record.document.get('run_id') or '')
        path = Path(session.case_path) / REPORT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(
            _json.dumps(document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, path)
        # CP-05 item 2. The same document on the run that was judged, so a
        # later run rewriting the case-level file cannot take this candidate's
        # evidence -- or the decision somebody recorded against it -- with it.
        RunBuilder.record_quality_report(record, document)
        return document

    def _record_quality_override(self, session: CaseSession, record,
                                 verdict, *, reason: str = '',
                                 engine_id: str = '',
                                 task_id: str = '',
                                 report: dict | None = None) -> dict:
        """Record a human's decision to keep a mesh the gate refused.

        Reuses ``quality.waiver`` rather than inventing a second override
        record. A refusal here is not swallowed: if the waiver subsystem will
        not bind the decision, the caller must hear that the trace it thinks it
        left does not exist.

        Plan 30 F-05. This was written for one engine: it looked the report up
        under Gmsh's own gate task and asked the Gmsh workflow for it, so a
        snappy user had no way to record an acceptance at all -- the one
        engine whose gate is checkMesh, the check most likely to refuse a
        mesh a human still wants. ``engine_id`` and ``task_id`` name whose
        decision this is; both default to the case's engine and
        :func:`qa_task_id`. Gmsh's atomic run passes its own gate task
        explicitly, because there the decision is taken against the native
        element gate, before any mesh exists for checkMesh to open.

        ``record`` is the run manifest to note the decision on, or ``None``
        for an engine whose acceptance is not taken inside a run.

        R124. ``reason`` is the operator's own words. It used to be absent, and
        the waiver stored the app's own failure sentence played back at it --
        every record on the measured run read "1107 of 90418 elements are below
        the requested sicn 0.1", which says what the gate found and nothing at
        all about why a human decided to ship it anyway. The Repair page's
        equivalent decision (``Use as-is``) has always asked for written
        justification first; this is the more consequential of the two.
        """
        from foammesh.core.engine.base import qa_task_id
        from foammesh.core.quality import waiver as waiver_module

        stated = (reason or '').strip()
        if not stated:
            # R124. Refused rather than back-filled, and refused before
            # anything else is read: what is missing is the caller's, not the
            # case's. The old fallback quoted
            # `verdict.reason` -- the gate's own failure sentence -- so every
            # record on disk explained what the app found and none of them
            # explained what the human decided. A waiver with no human words
            # in it is not an audit record.
            raise ValidationFailedError(
                'accepting a mesh the quality gate refused requires a stated '
                'reason; pass accept_reason with the operator\'s own words')
        engine = str(engine_id or self._engine_id(session))
        task = str(task_id or qa_task_id(engine))
        # Plan 31 CP-05 items 2 and 4. `report` names the report this decision
        # is about. Without it the only answer available was "whatever report
        # the case holds now", which is the newest run's -- so a decision taken
        # about the candidate on screen bound itself to a different
        # candidate's evidence the moment another run had been made.
        if report is None:
            report = self._current_report(session, task)
        if report is None:
            raise PreconditionFailedError(
                f'{task} wrote no quality report, so an override could not be '
                'bound to the mesh it covers')
        try:
            waiver = waiver_module.record(
                self._workflow_task(engine, task), self._bound_report(
                    session, report),
                actor=self._session_actor(session), reason=stated)
        except waiver_module.WaiverRefused as error:
            raise ValidationFailedError(
                f'the mesh could not be accepted: {error}') from error
        path = waiver_module.write(session.case_path, waiver)
        document = dict(waiver.to_dict(),
                        waiver_fingerprint=waiver.fingerprint, path=str(path))
        if record is not None:
            record.document['quality_override'] = document
            record.write()
        return document

    def _bound_report(self, session: CaseSession, report: dict) -> dict:
        """*report*, with any evidence key it does not carry filled from the case.

        A waiver names the checkpoint, mesh, geometry, policy and calculation
        it was granted against, and refuses to record itself if the report
        cannot state them (§8.6). Gmsh's gate writes all five; the checkMesh
        projection -- which is snappy's gate report -- states the mesh and
        nothing else, so a snappy acceptance was refused as `unbound_evidence`
        before it could be written. The case already identifies the other four
        in `quality/evidence.json`, written by every mutating stage, so they
        are read from there rather than invented. A value the report states
        itself always wins.
        """
        bound = dict(report)
        for key, value in self._current_evidence(session).items():
            if str(value or '').strip() and not str(bound.get(key) or '').strip():
                bound[key] = value
        return bound

    def _record_engine_run_success(self, session: CaseSession, command: Command,
                                   *, warning: bool = False,
                                   exclude: tuple = (),
                                   configured=(),
                                   waived: tuple = (),
                                   warning_for=None,
                                   reasons=()) -> dict | None:
        """Advance the tasks this engine's atomic run actually performed.

        Bounded by what the engine declares it does, not by which tasks happen
        to have a stage. Without that, a single successful run would advance the
        fidelity and summary gates it is supposed to be judged by.
        """
        from foammesh.core.workflow.task_state_store import TaskStateError

        try:
            from foammesh.core.engine.registry import ENGINE_REGISTRY

            engine_id, store = self._task_state_store(session, command)
            covered = getattr(
                ENGINE_REGISTRY.get(engine_id), 'ATOMIC_RUN_TASKS', None)
            if covered is None:
                # An engine that has not declared its coverage advances nothing
                # rather than everything: silence is not evidence.
                return None
            covered = tuple(task for task in covered if task not in exclude)
            # DP-817. What the warning is about, when it is not about every
            # task the run covered, and what it said. Passed only when there
            # is something to say, so a store that predates them still works.
            attribution = {}
            if warning and warning_for is not None:
                attribution['warning_for'] = tuple(warning_for)
            if reasons:
                attribution['reasons'] = tuple(reasons)
            staled = self._stale_checks_for_rerun(session, store, covered)
            return self._with_staled(staled, store.record_atomic_run_success(
                covered, warning=warning, **attribution,
                # DP-228. The optional tasks the job this run consumed carried
                # settings for. The graph answers the same question from a page
                # save that may have been refused, so it is not asked.
                configured=tuple(configured),
                # R119/R158. Only tasks this run actually covered: a waiver
                # names a gate the run reached, never one it skipped.
                waived=tuple(task for task in waived if task in covered)))
        except (FacadeError, TaskStateError, LookupError, OSError, ValueError):
            # A workflow that cannot be advanced must not invalidate a mesh
            # that was produced and published. `FacadeError` belongs here for
            # the same reason as the rest: a case whose engine selection is
            # missing or unrecognised raises `unknown meshing engine` out of
            # the store lookup, and that took the whole published result down
            # with it -- a bookkeeping failure discarding an artifact that had
            # already reached the case.
            return None

    def _record_stage_run_success(self, session: CaseSession, command: Command,
                                  task_id: str | None, *, warning: bool = False,
                                  reasons=()) -> dict | None:
        """Advance the task a ``workflow.run_stage`` call just performed.

        The legacy step pages run stages one at a time and never touch the
        tree, so this is the only way the tree learns that a stage happened.
        The store advances the stage's own chain and stops at any gate; the
        result carries ``blocked`` so the caller can say why the tree did not
        move further, instead of leaving the user to guess.
        """
        if not task_id:
            return None
        from foammesh.core.workflow.task_state_store import TaskStateError

        try:
            _engine_id, store = self._task_state_store(session, command)
            # DP-817. The warning's text, passed only when there is one.
            extra = {'reasons': tuple(reasons)} if reasons else {}
            staled = self._stale_checks_for_rerun(session, store, (task_id,))
            return self._with_staled(staled, store.record_stage_chain_success(
                task_id, warning=warning, **extra))
        except (TaskStateError, LookupError, OSError, ValueError):
            return None

    def _stale_checks_for_rerun(self, session: CaseSession, store,
                                performed) -> list:
        """A new mesh makes every verdict on the old one stale (DP-1038).

        Plan 37 UF3. The checks below what this run performed are marked
        stale before the run is recorded, so their old evidence is dropped
        and the recorder stops at them as it does after a first run; and a
        check job still measuring the replaced mesh is superseded now
        rather than left to finish and be discarded (UF4's job record).
        """
        stale_below = getattr(store, 'stale_checks_below', None)
        staled = list(stale_below(tuple(performed))) if stale_below else []
        jobs = self._checks_for(session).setdefault('jobs', {})
        revision = _mesh_revision(getattr(session, 'case_path', ''))
        for job in list(jobs.values()):
            if not job['future'].done() and job['revision'] != revision:
                self._stop_job(job, 'superseded')
        return staled

    @staticmethod
    def _with_staled(staled: list, recorded):
        if isinstance(recorded, dict) and staled:
            recorded = dict(recorded, staled_checks=list(staled))
        return recorded

    def _record_stage_run_failure(self, session: CaseSession, command: Command,
                                  task_id: str | None) -> dict | None:
        """Record that the stage this task stands for has just failed (R92).

        The `fail` transition also invalidates the task's descendants, which
        is the point: a failed castellation cannot leave snap and layers
        reading as though they still describe a mesh that exists. Failures to
        record are swallowed for the same reason the success path swallows
        them -- a tree that cannot be advanced must not turn a reported
        failure into an exception on top of it.
        """
        if not task_id:
            return None
        from foammesh.core.workflow.task_state_store import TaskStateError

        try:
            _engine_id, store = self._task_state_store(session, command)
            return store.apply(task_id, 'fail')
        except (TaskStateError, LookupError, OSError, ValueError):
            return None

    @staticmethod
    def _mesh_written_at(case_path) -> float | None:
        try:
            return (Path(case_path) / 'constant' / 'polyMesh' / 'owner'
                    ).stat().st_mtime
        except OSError:
            return None

    def _collect_check_artifacts(self, session: CaseSession, argv, report, *,
                                 started_at=None, wanted=None) -> dict | None:
        """Keep the sets and surfaces this checkMesh run wrote (Plan 37 UF18).

        Best effort: the verdict is already filed, and a highlight that
        cannot be kept must not turn a finished check into a failed one.
        """
        from foammesh.core.quality import check_artifacts

        try:
            revisions = getattr(session, 'revisions', None)
            return check_artifacts.collect(
                session.case_path, argv=tuple(str(item) for item in argv or ()),
                started_at=started_at, wanted=wanted, check='openfoam',
                mesh_fingerprint=str(getattr(report, 'mesh_fingerprint', '')),
                mesh_revision=getattr(revisions, 'artifact_sequence', None),
                checked_at=str(getattr(report, 'checked_at', '') or ''))
        except Exception as error:                            # noqa: BLE001
            logger.warning('checkMesh outputs were not kept: %s', error)
            return None

    def _persist_pipeline_check(self, session: CaseSession, execution) -> dict | None:
        """Store the checkMesh a pipeline run produced, as ``mesh.check`` would.

        The pipeline always ends in a checkMesh node, and until now its output
        was thrown away: the only report on disk came from a *separate*
        Mesh > Check Mesh, so the verdict strip stayed on "not checked"
        after a complete mesh and the quality tab was empty. The parsed
        report is written to the same ``quality/latest.json`` with the same
        schema, and the same artifact event tells the GUI to re-read it.
        """
        if execution is None or not execution.succeeded:
            return None
        from foammesh.core.quality import MeshCheckService, parse_checkmesh

        job = execution.job
        retained_context = self._retained_region_context(session)
        try:
            parsed = parse_checkmesh(str(getattr(job, 'output', '') or ''))
            self._apply_retained_regions(retained_context, parsed)
            _, report = MeshCheckService.persist_result(
                session.case_path, parsed,
                command=tuple(getattr(job, 'argv', ()) or ()),
                log_path=getattr(job, 'log_path', None)
                or session.case_path / 'foammesh' / 'logs' / 'checkMesh.log')
        except (OSError, ValueError) as error:
            logger.warning('pipeline checkMesh output was not stored: %s', error)
            return None
        # Plan 37 UF18. The pipeline's checkMesh keeps its outputs too. The
        # job carries no start time; a file older than the mesh it checked
        # cannot be this check's, so the mesh's own write time bounds it.
        self._collect_check_artifacts(
            session, tuple(getattr(job, 'argv', ()) or ()), report,
            started_at=self._mesh_written_at(session.case_path))
        session.state.bus.publish(
            Event.ARTIFACT_QUALITY_CHANGED, operation='workflow.run_pipeline',
            job_id=getattr(job, 'job_id', None), artifacts=[], quality='checkMesh')
        return report.to_dict()

    def _advance_pipeline_gates(self, session: CaseSession, command: Command,
                                recorded: dict | None) -> dict | None:
        """Start the check gates a finished pipeline is blocked behind.

        The atomic recorder stops at the first gate. When that gate is a
        check task whose own prerequisites are met, the mesh it judges is on
        disk right now, so the check is run and recorded and the remaining
        run tasks are advanced. A gate whose prerequisite is a manual
        confirmation stays blocked, and the payload says so.

        Plan 35 CR2. The checks parse the mesh, which on a large case is
        minutes and gigabytes, and they used to do it here, on the thread
        that runs the window. Now they are submitted to worker processes and
        this returns at once, naming them under ``checking``; the verdicts
        arrive later as task-state transitions and an
        ``ARTIFACT_QUALITY_CHANGED`` event. A check that cannot run is left
        unrun and retryable, and the mesh stays published and usable.
        """
        if not recorded:
            return recorded
        gates = self._runnable_gates(session, command, recorded)
        if not gates:
            return recorded
        checks = self._checks_for(session)
        for gate in gates:
            checks['checking'][gate] = CHECK_TASK_OPERATIONS[gate]
            checks['failures'].pop(gate, None)
        task = asyncio.ensure_future(
            self._gates_in_background(session, command, recorded))
        checks['tasks'].add(task)
        task.add_done_callback(checks['tasks'].discard)
        return dict(recorded, checking=list(gates))

    def _runnable_gates(self, session: CaseSession, command: Command,
                        recorded: dict | None) -> list:
        """The check tasks ``recorded`` is blocked behind that may run now."""
        from foammesh.core.workflow.task_state_store import TaskStateError

        blocked = recorded.get('blocked') if isinstance(recorded, dict) else None
        if not blocked:
            return []
        accepted = ('passed', 'warning', 'skipped', 'completed', 'waived')
        try:
            _engine_id, store = self._task_state_store(session, command)
            graph = store.load_result().graph
            task = store.descriptor.task(str(blocked.get('task_id') or ''))
            return [
                parent for parent in task.depends_on
                if parent in CHECK_TASK_OPERATIONS
                and graph.state(parent).value not in accepted
                and graph.is_runnable(parent)]
        except (TaskStateError, LookupError, OSError, ValueError,
                AttributeError):
            return []

    async def _gates_in_background(self, session: CaseSession,
                                   command: Command, recorded: dict) -> dict:
        """Run the gates in workers, then record them through the queue.

        The workers are awaited outside the command queue, so edits made
        while a check runs are not held behind it; the store is written only
        from inside the queue, where every other write to it happens.
        """
        from foammesh.core.engine.registry import ENGINE_REGISTRY
        from foammesh.core.workflow.task_state_store import TaskStateError

        checks = self._checks_for(session)
        try:
            for _attempt in range(8):
                blocked = recorded.get('blocked')
                gates = self._runnable_gates(session, command, recorded)
                if not blocked or not gates:
                    return recorded
                results = {}
                jobs_run = {}
                for gate in gates:
                    operation, inner = self._check_command(command, gate)
                    title = self._gate_title(session, command, gate)
                    try:
                        outcome = self.handlers()[operation](session, inner)
                        if inspect.isawaitable(outcome):
                            # Plan 37 UF4 DP-1027: the same job a
                            # foreground Check & Proceed joins.
                            job = self._check_job(
                                session, gate, operation, outcome, title)
                            jobs_run[gate] = job
                            outcome = await asyncio.shield(job['future'])
                        results[gate] = (operation, outcome)
                    except FacadeError as error:
                        if gate not in jobs_run:
                            error = _actionable_check_error(
                                title, operation, error)
                            checks['failures'][gate] = _check_failure(
                                operation, error)
                        logger.warning('check %s did not run: %s', gate, error)
                    finally:
                        if gate not in jobs_run:
                            checks['checking'].pop(gate, None)
                if len(results) != len(gates):
                    return recorded
                previous = recorded

                async def record(results=results, previous=previous,
                                 jobs_run=jobs_run):
                    engine_id, store = self._task_state_store(session, command)
                    for gate, (operation, outcome) in results.items():
                        if gate in jobs_run:
                            # A result for a mesh replaced since is dropped.
                            self._assert_job_current(session, jobs_run[gate])
                        self._record_check(gate, operation, outcome, store)
                    covered = getattr(
                        ENGINE_REGISTRY.get(engine_id), 'ATOMIC_RUN_TASKS', ())
                    attribution = {}
                    if (previous.get('warning')
                            and previous.get('warning_for') is not None):
                        attribution['warning_for'] = tuple(
                            previous['warning_for'])
                    if previous.get('warning_reasons'):
                        attribution['reasons'] = tuple(
                            previous['warning_reasons'])
                    return store.record_atomic_run_success(
                        covered, warning=bool(previous.get('warning')),
                        **attribution)

                try:
                    scheduler = getattr(session, 'scheduler', None)
                    if scheduler is not None:
                        progressed = await scheduler.submit(record)
                    else:
                        progressed = await record()
                except (TaskStateError, LookupError, OSError, ValueError,
                        FacadeError) as error:
                    for gate, (operation, _outcome) in results.items():
                        if (isinstance(error, FacadeError)
                                and (error.details or {}).get('actionable')):
                            checks['failures'][gate] = _check_failure(
                                operation, error)
                            continue
                        checks['failures'][gate] = {
                            'operation': operation, 'reason': 'not_recorded',
                            'message': str(error), 'retryable': True}
                    return recorded
                progressed['advanced'] = list(
                    recorded.get('advanced', ())) + list(
                        progressed.get('advanced', ()))
                progressed['gates_run'] = list(
                    recorded.get('gates_run', ())) + list(gates)
                progressed['warning'] = recorded.get('warning')
                for key in ('warning_for', 'warning_reasons'):
                    if key in recorded:
                        progressed[key] = recorded[key]
                if progressed.get('blocked') == blocked:
                    return progressed
                recorded = progressed
            return recorded
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            # A background task has nobody to raise to: say it, keep going.
            logger.exception('the pipeline check gates could not run')
            return recorded
        finally:
            # Plan 37 UF4 DP-1027. Only the flags this run set and no live
            # job owns: popping every entry cleared the "checking" flag of a
            # check the user had started from the page, while it still ran.
            live = checks.setdefault('jobs', {})
            for gate in list(checks['checking']):
                if gate not in live:
                    checks['checking'].pop(gate, None)
            try:
                session.state.bus.publish(
                    Event.ARTIFACT_QUALITY_CHANGED,
                    operation='workflow.run_pipeline', job_id=None,
                    artifacts=[], quality='checks')
            except Exception:                               # noqa: BLE001
                logger.debug('check completion was not announced',
                             exc_info=True)

    def _checks_for(self, session: CaseSession) -> dict:
        """What this case is checking right now, and what could not be.

        ``jobs`` (Plan 37 UF4 DP-1027) holds the one in-flight job per check
        task that foreground and background requests share.
        """
        registry = self.__dict__.setdefault('_gate_checks', {})
        key = str(getattr(session, 'case_path', '') or '')
        return registry.setdefault(
            key, {'checking': {}, 'failures': {}, 'tasks': set(),
                  'jobs': {}})

    def _gate_title(self, session: CaseSession, command: Command,
                    task_id: str) -> str:
        try:
            _engine_id, store = self._task_state_store(session, command)
        except Exception:                                   # noqa: BLE001
            return task_id
        return self._check_title(store, task_id)

    def check_tasks_in_flight(self, session: CaseSession) -> list:
        """The background gate tasks of ``session`` (tests and shutdown)."""
        return list(self._checks_for(session)['tasks'])

    @staticmethod
    def _gmsh_volume_types(session: CaseSession, prepared) -> dict:
        """Plan 36 RP11: each typed solid's type, by its published name."""
        from foammesh.core.gmsh.execution import (
            region_display_names, volume_types,
        )
        from foammesh.core.mesh.cad_solids import typing_of

        db = getattr(session.state, 'db', None)
        try:
            rows = dict(db.getElements('gmsh/volumeControls') or {})
        except Exception:  # noqa: BLE001 - a case without the list
            return {}
        types = {token: item['type'] for token, item in typing_of(rows).items()
                 if item.get('type') and item.get('included', True)}
        if not types:
            return {}
        chosen = region_display_names(db)
        for source in (prepared, getattr(prepared, 'reference', None)):
            found = volume_types(source, types, chosen) if source else {}
            if found:
                return found
        return {}

    def _publish_gmsh_result(self, session: CaseSession, record, prepared) -> dict:
        """Publish one Gmsh mesh as constant/polyMesh, atomically."""
        from foammesh.core.gmsh.manifest import RunBuilder
        from foammesh.core.gmsh.publish import PublishError, publish

        staged = record.layout.root / 'publication' / 'polyMesh'
        try:
            report = publish(
                record.layout.mesh, staged,
                categories={**_gmsh_patch_categories(prepared),
                            **_job_edge_categories(record)},
                identities=_gmsh_patch_identities(prepared),
                region_metadata=self._gmsh_volume_types(session, prepared),
                periodic_pairs=_job_periodic_pairs(record),
                extrusion=_job_extrusion(record),
                source_fingerprint=record.document.get('job_digest', ''))
        except PublishError as error:
            RunBuilder.record_publication_failure(record, str(error))
            raise ValidationFailedError(
                f'the Gmsh mesh could not be published: {error}') from error

        # CP-05 item 3, the third of its three preconditions. Generation is
        # judged before this method is reached and structural validity is the
        # gate's verdict; boundary completeness is judged here, because here
        # is the first moment it is known. It cannot be a precondition on the
        # acceptance itself: a stored run carries no `publication` block until
        # publication happens, and publication is what accepting it does.
        # Before the swap, so a mesh whose boundary is missing never becomes
        # the case's mesh -- the same order, and the same recorded failure, as
        # a publisher that raised.
        gap = published_boundary_gap(report.to_dict())
        if gap:
            RunBuilder.record_publication_failure(record, gap)
            raise ValidationFailedError(
                f'the Gmsh mesh could not be published: {gap}')

        # Plan 31 CP-05 item 7. Every publication record is written after the
        # mesh has already moved, so a process that died between the swap and
        # the record left a manifest reading `succeeded` with no publication
        # at all -- MEASURED, indistinguishable from a run that deliberately
        # published nothing, and the reopened case called it accepted. Saying
        # it first is what makes the interruption legible afterwards.
        RunBuilder.record_publication_started(
            record, session.case_path / 'constant' / 'polyMesh')
        activation = self._swap_in_published_mesh(session, staged, record)
        document = {**report.to_dict(), **activation}
        document['case_scaffolding'] = self._ensure_openfoam_case(session)
        document['patch_identity'] = self._write_patch_identity(
            session, 'gmsh', report.patch_records, prepared)
        document['evidence'] = self._bind_evidence(
            session, checkpoint_fingerprint=str(
                record.document.get('job_digest') or ''),
            prepared=prepared)
        RunBuilder.record_publication(record, document)
        return document

    def _bind_evidence(self, session: CaseSession, *,
                       checkpoint_fingerprint: str = '',
                       prepared=None) -> dict:
        """Record what the case identifies right now, for §9's staleness check.

        `_current_evidence` read `quality/evidence.json` and nothing ever
        wrote it -- so every summary composed on every case was `stale` with
        "the case does not currently identify checkpoint_fingerprint, ...",
        and the disposition was `unqualified` whatever the blocks said.
        MEASURED on a live Gmsh walk of the tree route: three passing blocks,
        summary stale. Written atomically, and only from the places that
        change what the evidence names: a mesh publication, a mutating
        snappy stage, a reconstruction.

        A case with no mesh in its root has nothing to bind and says so; the
        summary then stays honestly stale rather than naming a mesh that is
        not there.
        """
        from foammesh.core.case.model import fingerprint_poly_mesh
        from foammesh.core.quality.geometry_fidelity import (
            report as report_module, tolerance_source,
        )
        from foammesh.core.quality.summary import BOUND_EVIDENCE

        case_path = Path(session.case_path)
        poly_mesh = case_path / 'constant' / 'polyMesh'
        try:
            mesh_digest = str(fingerprint_poly_mesh(poly_mesh).digest)
        except Exception as error:                          # noqa: BLE001
            return {'bound': False,
                    'reason': f'no fingerprintable mesh in the case root: '
                              f'{error}'}
        if prepared is None:
            prepared = self._current_prepared(session)
        reference = getattr(prepared, 'reference', None)
        try:
            policy = tolerance_source.for_case(case_path, db=session.state.db)
            policy_digest = report_module.policy_fingerprint(policy)
        except Exception:                                   # noqa: BLE001
            policy_digest = ''
        document = {
            'checkpoint_fingerprint': str(checkpoint_fingerprint
                                          or mesh_digest),
            'subject_mesh_fingerprint': mesh_digest,
            'prepared_revision': str(getattr(reference, 'revision_id', '')
                                     or ''),
            'policy_fingerprint': str(policy_digest or ''),
            'calculation_version': str(report_module.CALCULATION_VERSION),
        }
        assert set(document) == set(BOUND_EVIDENCE)
        marker = case_path / 'foammesh' / 'quality' / 'evidence.json'
        staging = marker.with_name(f'.evidence.{os.getpid()}.tmp')
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(json.dumps(document, indent=2, sort_keys=True),
                               encoding='utf-8')
            os.replace(staging, marker)
        except OSError as error:
            staging.unlink(missing_ok=True)
            return {'bound': False, 'reason': str(error)}
        return {'bound': True, 'path': str(marker), **document}

    @staticmethod
    def _current_prepared(session: CaseSession):
        """The current prepared-geometry revision, or ``None``."""
        from foammesh.core.geometry.prepared import (
            PreparedGeometryError, PreparedGeometryStore,
        )
        try:
            return PreparedGeometryStore(session.case_path).current()
        except (PreparedGeometryError, OSError, ValueError):
            return None

    def _write_snappy_patch_identity(self, session: CaseSession,
                                     prepared=None) -> dict:
        """Join the snappy boundary to the prepared groups, beside the mesh.

        Gmsh wrote the patch-identity sidecar on publication; snappy never
        did, so on a snappy case `_boundary_model` found no sidecar and both
        fidelity and resolution reported zero sections -- MEASURED on the
        live elbow walk. snappy names its patches after the prepared group's
        solver name (with the patch uuid suffix), so the same join applies:
        the polyMesh boundary against the group manifest, with an unmatched
        patch recorded as fabricated rather than dropped.
        """
        from foammesh.core.mesh.poly_mesh_boundary import _read_boundary

        case_path = Path(session.case_path)
        try:
            patches = _read_boundary(case_path / 'constant' / 'polyMesh')
        except Exception as error:                          # noqa: BLE001
            return {'written': False, 'rated': False,
                    'reason': f'no readable boundary in the case root: {error}'}
        if prepared is None:
            prepared = self._current_prepared(session)
        categories = _gmsh_patch_categories(prepared) if prepared else {}
        records = [{'name': patch.name, 'solver_name': patch.name,
                    'category': categories.get(patch.name) or patch.patch_type}
                   for patch in patches]
        return self._write_patch_identity(
            session, 'snappy', records, prepared,
            background_boundaries=self._case_background_boundaries(case_path))

    @staticmethod
    def _case_background_boundaries(case_path) -> dict:
        """What the generated case says about its own background faces.

        Plan 31 CP-07 item 3. The prepared store's manifest lists imported
        surfaces only, so the background block's six faces reached the join as
        patches nothing declared and came out fabricated. The case builder
        writes its own manifest beside the case, and that one knows whose
        faces they are and which of them a person named.
        """
        from foammesh.openfoam.case_builder import group_manifest_path

        try:
            document = json.loads(
                group_manifest_path(case_path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}

    @staticmethod
    def _save_snap_checkpoint(session: CaseSession, *, run_id: str = '') -> dict:
        """Capture the snapped boundary before the next phase overwrites it.

        Immutable and fingerprinted, so a later snap adds a sibling rather than
        replacing evidence a GF1 report or a blocked run still refers to.

        A checkpoint that cannot be taken does not fail the pipeline: the mesh
        is real and usable, and the consequence of a missing checkpoint is that
        GF1 has nothing to measure, which is reported as unrated rather than as
        a meshing failure.
        """
        from foammesh.core.workflow.snap_checkpoint import (
            SnapCheckpointError, SnapCheckpointStore,
        )

        try:
            checkpoint = SnapCheckpointStore(session.case_path).save(
                run_id=run_id)
        except (SnapCheckpointError, OSError) as error:
            return {'stage': 'snap', 'saved': False, 'reason': str(error)}
        return {'stage': 'snap', 'saved': True,
                'fingerprint': checkpoint.fingerprint, 'run_id': run_id}

    @staticmethod
    def _write_patch_identity(session: CaseSession, engine_id: str,
                              published_patches, prepared,
                              background_boundaries=None) -> dict:
        """Persist the published-patch to prepared-patch join beside the mesh.

        Plan 23 §4 identifies a boundary section by ``patch_uuid``, but the
        published mesh carries only solver names. This sidecar is the join, and
        it records a fabricated identity rather than dropping it, so a section
        that cannot be traced to prepared geometry becomes ``unrated`` instead
        of silently joining to nothing.
        """
        from foammesh.core.quality import patch_identity

        reference = getattr(prepared, 'reference', None)
        identity = patch_identity.build(
            engine_id,
            published_patches=list(published_patches or ()),
            group_manifest=getattr(prepared, 'group_manifest', None),
            prepared_revision_id=getattr(reference, 'revision_id', None),
            prepared_fingerprint=getattr(reference, 'fingerprint', None),
            background_boundaries=background_boundaries)
        destination = (session.case_path / 'foammesh' / 'quality' / 'geometry'
                       / patch_identity.IDENTITY_FILENAME)
        try:
            patch_identity.write(destination, identity)
        except OSError:
            # A mesh that published must not be invalidated because its
            # provenance sidecar could not be written; the checker will report
            # the mesh unrated instead.
            return {'written': False, 'rated': False}
        return {'written': True, 'path': str(destination),
                'rated': identity.rated,
                'unrated_reason': identity.unrated_reason(),
                'fabricated': [item.solver_name
                               for item in identity.fabricated],
                'background': [
                    {'name': item.solver_name, 'naming': item.naming,
                     'role': item.category}
                    for item in identity.background]}

    #: The minimum an OpenFOAM utility needs to open a case. A Gmsh case is
    #: configured entirely through the Gmsh job, so nothing else writes
    #: ``system/``, and ``checkMesh`` on a published mesh has nowhere to run.
    _MINIMAL_CONTROL_DICT = """\
FoamFile
{
    format      ascii;
    class       dictionary;
    location    "system";
    object      controlDict;
}

application     foamRun;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
writeFormat     ascii;
writePrecision  6;
timeFormat      general;
runTimeModifiable true;
"""

    def _ensure_openfoam_case(self, session: CaseSession) -> dict:
        """Give a published mesh the case files OpenFOAM needs to read it.

        The snappy path writes ``system/`` as part of generating its
        dictionaries. The Gmsh path has no dictionaries to generate, so the
        enumerated QA step -- checkMesh on the published mesh -- had nothing to
        run against. Written only when absent, so an authored case is never
        overwritten.
        """
        system = session.case_path / 'system'
        control = system / 'controlDict'
        created = []
        if not control.is_file():
            system.mkdir(parents=True, exist_ok=True)
            control.write_text(self._MINIMAL_CONTROL_DICT, encoding='utf-8')
            created.append('system/controlDict')
        return {'created': created, 'system': str(system)}

    def _swap_in_published_mesh(self, session: CaseSession, staged, record) -> dict:
        """Replace the active mesh, keeping the previous one recoverable."""
        import os

        from foammesh.core.mesh.recovery import MeshRecoveryService

        active = session.case_path / 'constant' / 'polyMesh'
        previous = record.layout.root / 'publication' / 'previous-active-polyMesh'
        service = MeshRecoveryService()
        recovery = (service.snapshot(session.case_path, operation='mesh.publish')
                    if active.is_dir() else None)
        active.parent.mkdir(parents=True, exist_ok=True)
        try:
            if active.exists():
                os.replace(active, previous)
            os.replace(staged, active)
        except Exception:
            if previous.exists() and not active.exists():
                os.replace(previous, active)
            raise
        if recovery is not None:
            service.mark_available(recovery)
            # H9. The run that has just written constant/polyMesh is where
            # the fact that this mesh is ours becomes knowable, and nothing
            # recorded it. `resolve_workflow` therefore read the sidecar's
            # `workflow=none` as `mesh exists but metadata has no workflow
            # mode` -- External mesh -- so the first re-resolution after a
            # save re-opened our own freshly meshed case as an import and
            # collapsed the workflow outline to Scene / Display.
        from foammesh.core.case import record_generated_mesh
        record_generated_mesh(session.case_path, provenance={
            'generated_by': f'gmsh:{record.run_id}'})
        session.state.bus.publish(
            Event.ARTIFACT_MESH_CHANGED, operation='mesh.publish',
            run_id=record.run_id)
        return {
            'destination': str(active),
            'replaced_active_mesh': recovery is not None,
            'recovery_id': recovery.recovery_id if recovery else None,
        }

    def _mesh_gmsh_runs(self, session: CaseSession,
                        command: Command) -> OperationResult:
        from foammesh.core.gmsh.manifest import list_runs

        return self._read_result(session, command, {
            'runs': [item for item in list_runs(session.case_path)
                     if item.get('engine_id')]})

    def _mesh_plan_derive(self, session: CaseSession,
                          command: Command) -> OperationResult:
        """Derive the selected engine's complete immutable execution plan."""
        from foammesh.core.engine import EnginePlanRequest
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_engine_id,
        )
        from foammesh.core.geometry import PreparedGeometryStore
        engine_id = str(command.parameters.get('engine_id') or
                        configured_engine_id(session.state.db)).lower()
        try:
            engine = ENGINE_REGISTRY.get(engine_id)
        except Exception as error:
            raise ValidationFailedError('unknown meshing engine') from error
        prepared = None
        prepared_store = PreparedGeometryStore(session.case_path)
        revision_id = command.parameters.get('prepared_revision_id')
        if revision_id:
            try:
                prepared = PreparedGeometryStore(session.case_path).load(
                    str(revision_id)).reference
            except (FileNotFoundError, ValueError) as error:
                raise PreconditionFailedError(
                    'prepared geometry revision is unavailable',
                    details={'prepared_revision_id': revision_id}) from error
        else:
            try:
                current = prepared_store.current()
                prepared = current.reference if current is not None else None
            except ValueError as error:
                raise PreconditionFailedError(
                    'selected prepared geometry is invalid') from error
        if prepared is None:
            # F-12. The same readiness answer the run and the dictionary
            # writer give: prepare with defaults rather than refuse a case
            # whose geometry is imported and never asked for anything else.
            created = _ensure_prepared_geometry(
                session, producer='mesh.plan.derive')
            prepared = created.reference if created is not None else None
        if engine.requires_prepared_geometry and prepared is None:
            raise PreconditionFailedError(
                f'{engine_id} planning requires imported geometry to prepare')
        run_id = str(command.parameters.get('run_id') or
                     f'plan-r{session.authored_revision}')
        run_path = session.case_path / 'foammesh' / 'runs' / run_id
        try:
            plan = engine.create_plan(EnginePlanRequest(
                case_path=session.case_path, run_path=run_path,
                intent=_meshing_intent(session.configuration(),
                                       engine.native_section),
                prepared_geometry=prepared,
                configuration_revision=session.authored_revision,
                resource_policy=_resource_policy(session.configuration())))
        except (ValueError, TypeError) as error:
            raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, {
            'plan': plan.to_dict(), 'plan_digest': plan.digest,
            'prepared_geometry': prepared.to_dict() if prepared else None,
        })

    def _activate_published_openfoam_mesh(self, session: CaseSession, run,
                                       artifact_id: str) -> tuple[dict, dict]:
        """Stage and atomically activate a repeatable mesh publication.

        Explicit one-off canonical exports remain fail-closed when their
        destination exists.  A meshing run, however, must be able to replace
        the active mesh while retaining the last one until ``checkMesh`` has
        accepted the candidate.
        """
        import os
        from foammesh.core.export.poly_mesh_writer import (
            FoamPolyMeshWriter, PolyMeshWriteError,
        )
        from foammesh.core.mesh.recovery import MeshRecoveryService

        canonical_path = (
            session.case_path / 'foammesh' / 'canonical' / artifact_id)
        from foammesh.core.mesh import CanonicalMeshStore
        try:
            mesh = CanonicalMeshStore().read(canonical_path)
        except Exception as error:
            raise ValidationFailedError(
                'canonical mesh artifact is invalid',
                details={'artifact_id': artifact_id, 'error': str(error)}) from error

        publication = run.layout.root / 'publication'
        staged = publication / 'polyMesh'
        previous = publication / 'previous-active-polyMesh'
        active = session.case_path / 'constant' / 'polyMesh'
        publication.mkdir(parents=True, exist_ok=True)
        try:
            report = FoamPolyMeshWriter().write(staged, mesh)
        except PolyMeshWriteError as error:
            raise ValidationFailedError(str(error)) from error

        recovery_service = MeshRecoveryService()
        recovery = (
            recovery_service.snapshot(
                session.case_path, operation='mesh.publish')
            if active.is_dir() else None)
        active.parent.mkdir(parents=True, exist_ok=True)
        try:
            if active.exists():
                os.replace(active, previous)
            os.replace(staged, active)
        except Exception:
            if previous.exists() and not active.exists():
                os.replace(previous, active)
            raise
        # H9. Publishing a canonical mesh into constant/polyMesh is a mesh
        # this workflow produced; say so, or a later re-resolution calls it
        # an external artifact.
        from foammesh.core.case import record_generated_mesh
        record_generated_mesh(session.case_path, provenance={
            'generated_by': f'canonical:{artifact_id}'})
        session.state.bus.publish(
            Event.ARTIFACT_MESH_CHANGED,
            operation='mesh.publish',
            run_id=run.layout.root.name,
            artifact_id=artifact_id,
        )
        payload = {
            'artifact_id': artifact_id,
            'export': {
                **report.to_dict(),
                'destination': str(active),
                'staged_destination': report.destination,
            },
            'replaced_active_mesh': recovery is not None,
            'recovery_id': recovery.recovery_id if recovery else None,
        }
        return payload, {
            'active': active,
            'previous': previous,
            'publication': publication,
            'recovery': recovery,
            'recovery_service': recovery_service,
        }

    @staticmethod
    def _rollback_published_openfoam_mesh(session: CaseSession,
                                       activation: dict) -> None:
        """Preserve the rejected candidate and restore the previous mesh."""
        import os
        active = Path(activation['active'])
        publication = Path(activation['publication'])
        rejected = publication / 'rejected-polyMesh'
        if active.exists():
            os.replace(active, rejected)
        recovery = activation.get('recovery')
        if recovery is not None:
            activation['recovery_service'].restore(
                session.case_path, recovery)
        elif Path(activation['previous']).exists():
            os.replace(Path(activation['previous']), active)

    @staticmethod
    def _commit_published_openfoam_mesh(activation: dict) -> None:
        """Mark the displaced active mesh recoverable only after QA passes."""
        import shutil
        recovery = activation.get('recovery')
        if recovery is not None:
            activation['recovery_service'].mark_available(recovery)
        previous = Path(activation['previous'])
        if previous.exists():
            shutil.rmtree(previous)

    @staticmethod
    def _pipeline_interface_couples(session: CaseSession) -> tuple[dict, ...]:
        """The couples this case's group manifest schedules, if any.

        No manifest and no pairs both mean the same thing -- nothing to
        couple -- so the pipeline is unchanged for every case that never
        authored an interface pair.
        """
        from foammesh.openfoam.case_builder import group_manifest_path
        path = group_manifest_path(session.case_path)
        if not path.is_file():
            return ()
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationFailedError(
                'case group manifest could not be read for interface pairing'
            ) from error
        return non_conformal_couples(manifest)

    async def _apply_non_conformal_pairs(
            self, session: CaseSession, parent: Command,
            group_manifest_path: Path) -> list[dict]:
        """Materialize geometry-level NCC intent with the Foundation-v13 tool.

        The meshing engine owns geometry, grouping, sizing, and generation.
        The solver-specific non-conformal addressing is intentionally created
        only after canonical OpenFOAM publication, using the exact qualified
        v13 runtime.
        """
        try:
            manifest = json.loads(
                Path(group_manifest_path).read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationFailedError(
                'group manifest could not be read for interface pairing') \
                from error
        pairs = non_conformal_couples(manifest)
        if not pairs:
            return []

        await self._utility_ready('createNonConformalCouples')
        registry = self._capabilities_registry()
        results = []
        for pair in pairs:
            pair_id = pair['pair_id']
            master_name = pair['master_patch']
            slave_name = pair['slave_patch']
            semantic = (master_name, slave_name)
            launch = registry.command(
                'createNonConformalCouples', semantic, cwd=session.case_path)
            execution = await self._context(session).executor.execute(
                session, OperationSpec(
                    operation='mesh.interface.non_conformal',
                    argv=launch.argv,
                    cwd=session.case_path,
                    mutation=True,
                    timeout=parent.parameters.get(
                        'interface_timeout_seconds', 900),
                    max_output_bytes=4 * 1024 * 1024,
                    log_path=(
                        session.case_path / 'foammesh' / 'logs' /
                        f'createNonConformalCouples-{pair_id or len(results)}.log'),
                    recover_mesh=True,
                    invalidated_outputs=('quality', 'exports'),
                    cleanup_argv=launch.cleanup_argv,
                ),
                on_line=parent.parameters.get('on_line'))
            payload = execution.to_payload()
            payload.update({
                'pair_id': pair_id,
                'master_patch': master_name,
                'slave_patch': slave_name,
                'runtime_profile': launch.profile_id,
            })
            if not execution.succeeded:
                raise ValidationFailedError(
                    f'OpenFOAM 13 could not create non-conformal pair '
                    f'{pair_id or master_name}', details=payload)
            results.append(payload)
        return results

    async def _mesh_engine_select(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        from foammesh.core.engine import EngineSelectionService
        from foammesh.core.engine.registry import ENGINE_REGISTRY
        engine_id = str(command.parameters.get('engine_id') or '').strip().lower()
        if engine_id not in ENGINE_REGISTRY.ids():
            raise ValidationFailedError(
                'engine_id must be one of: ' + ', '.join(ENGINE_REGISTRY.ids()))
        service = EngineSelectionService()
        # Selection deliberately does not probe: an engine's controls are
        # authored from the schema and stay editable with its runtime absent.
        # Availability is reported by mesh.engine.probe, and running fails
        # closed with a typed reason.
        probe = None
        retained = command.parameters.get('retained_artifacts')
        if retained is None:
            candidates = (
                session.case_path / 'constant' / 'polyMesh',
                session.case_path / 'foammesh' / 'quality' / 'latest.json',
                session.case_path / 'system' /
                'foammesh-dictionaries-manifest.json',
            )
            retained = [
                str(path.relative_to(session.case_path)).replace('\\', '/')
                for path in candidates if path.exists()
            ]
            runs_root = session.case_path / 'foammesh' / 'runs'
            if runs_root.is_dir() and any(runs_root.iterdir()):
                retained.append('foammesh/runs')
        preview = service.preview(session.state.db, engine_id, probe=probe,
                                  retained_artifacts=retained)
        if not getattr(ENGINE_REGISTRY.get(engine_id),
                       'mixes_cad_and_surfaces', True):
            # DP-638. Refused here, dry run included, because the method page
            # shows the dry run's refusal beside the method; the Gmsh job
            # would otherwise refuse the same case only at the run.
            self._refuse_mixed_sources_for_gmsh(session)
        if command.parameters.get('dry_run', False):
            return self._read_result(session, command, {'preview': preview.to_dict()})
        session.require_writable()
        if preview.incompatible_reason:
            raise PreconditionFailedError(
                'requested meshing engine cannot serve this target solver',
                details=preview.to_dict())
        if not preview.selectable:
            raise CapabilityUnavailableError(
                'requested meshing engine is unavailable', details=preview.to_dict())
        if preview.confirmation_required and not command.parameters.get('confirmed', False):
            raise PreconditionFailedError(
                'engine switch requires retained-artifact confirmation',
                details=preview.to_dict())
        source_map = {'gui': 'GUI', 'rest': 'API', 'cli': 'CLI',
                      'automation': 'AGENT', 'system': 'SYSTEM'}
        from foammesh.core.project import Source
        _preview, transaction = service.apply(
            session.state, engine_id, probe=probe, retained_artifacts=retained,
            source=getattr(Source, source_map[command.source.value]),
            reason=str(command.parameters.get('reason') or 'meshing method selected'))
        return OperationResult(
            'accepted', command.operation, session.revisions,
            changed_fields=(('mesh.engine',) if preview.changed else ()),
            invalidated_outputs=preview.invalidated_tasks,
            payload={'preview': preview.to_dict(),
                     'transaction': transaction.to_dict() if transaction else None})

    @staticmethod
    def _refuse_mixed_sources_for_gmsh(session: CaseSession,
                                       new_sources=()) -> None:
        """DP-638: raise when Gmsh would be handed CAD and surfaces together."""
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.store import gmsh_mixed_sources_refusal
        try:
            entries = GeometryArtifactStore(session.case_path).entries()
        except (OSError, ValueError):
            entries = []
        reason = gmsh_mixed_sources_refusal(entries, new_sources)
        if reason:
            raise PreconditionFailedError(reason, details={
                'error': 'mixed_geometry_sources', 'engine': 'gmsh',
                'new_sources': [str(source) for source in new_sources]})

    #: Every OpenFOAM utility a mesh run reaches for before it launches
    #: anything. One probe boots the runtime and answers for all of them, so
    #: a run path asks for the whole list once rather than paying the boot at
    #: whichever question happens to be first.
    RUN_PATH_UTILITIES = (
        'blockMesh', 'surfaceFeatures', 'snappyHexMesh', 'checkMesh',
        'createNonConformalCouples', 'decomposePar', 'reconstructPar',
        'mpirun')

    async def _warm_utility_probes(self, names=None) -> None:
        """Ask "is this utility there?" on a worker thread, before asking here.

        DP-354. :meth:`CapabilityRegistry.utility` is a read that boots
        something: it enters WSL and sources an OpenFOAM profile. Plan 30
        WP-08 moved the *reading* paths off the event loop -- engine probes,
        runtime diagnostics -- and left the *running* ones where they were,
        and nothing noticed, because the CP-10 latency harness ran against a
        stand-in load that awaited a timer and yielded instantly.

        MEASURED once a live mesher was the load: the first yield after
        ``workflow.run_pipeline`` was started came back in 22,630 ms cold and
        4,554 ms warm, against CP-10's 200 ms. Timed one probe at a time, the
        first costs 19,631 ms and the six after it cost 0.1 ms together --
        the boot is the whole figure, and it was being paid by the window
        that started the run, which is frozen for the duration.

        The work still happens and still refuses exactly the runs it refused
        before; it happens on a worker thread. The registry's own cache means
        every later question in the same session is free.
        """
        import asyncio

        registry = self._capabilities_registry()
        if not hasattr(registry, 'utility'):
            return {}
        wanted = tuple(names or self.RUN_PATH_UTILITIES)

        def probe():
            found = {}
            for name in wanted:
                try:
                    found[name] = registry.utility(name)
                except Exception:                             # noqa: BLE001
                    # Warming is never where a run fails. Whatever this was,
                    # the caller is about to ask the same question on this
                    # thread and report it in the words it already uses.
                    pass
            return found

        return await asyncio.to_thread(probe)

    async def _utilities_ready(self, names) -> None:
        """Probe ``names`` off the loop; refuse one the runtime did not answer.

        Plan 35 CR7. After the warm, every answer the registry *cached* is
        free to ask again here. One it did not cache -- a probe that timed
        out or could not reach the runtime (R194) -- would be asked again on
        the owner loop by whoever asks next, and wait as long; so it is
        refused now, with the probe's own reason, and the next attempt asks
        again off the loop.
        """
        found = await self._warm_utility_probes(tuple(names))
        for name, capability in (found or {}).items():
            if getattr(capability, 'transient', False) is True and \
                    not getattr(capability, 'available', False):
                reason = str(getattr(capability, 'reason', '') or '')
                raise CapabilityUnavailableError(
                    unavailable_utility_text(name, reason), details={
                        'utility': name,
                        'reason': reason,
                        'retryable': True})

    async def _utility_ready(self, name: str) -> str:
        """:meth:`_require_utility`, with the probe paid off the owner loop."""
        await self._utilities_ready((name,))
        return self._require_utility(name)

    def _utility_without_waiting(self, registry, name: str):
        """A utility's cached answer, or ``None`` while one is fetched.

        Plan 35 CR7. For a synchronous read the window makes from a Qt slot
        (the Export step lists its formats that way): an unknown utility is
        probed on a background thread, and this listing treats it as not
        there yet rather than booting a cold runtime on the GUI thread. The
        next listing has the answer.
        """
        known = getattr(registry, 'utility_if_known', None)
        if known is None:
            return registry.utility(name)       # a test double: no runtime
        capability = known(name)
        inflight = self.__dict__.setdefault('_warming_in_background', set())
        if capability is None and name not in inflight:
            import threading

            inflight.add(name)

            def warm():
                try:
                    registry.utility(name)
                except Exception:                             # noqa: BLE001
                    pass
                finally:
                    inflight.discard(name)

            threading.Thread(target=warm, name=f'probe-{name}',
                             daemon=True).start()
        return capability

    def _require_utility(self, name: str) -> str:
        capability = self._capabilities_registry().utility(name)
        if not getattr(capability, 'available', False) or not getattr(capability, 'executable', None):
            # Plan 37 F2 (§8 check 4). "required utility is unavailable"
            # named neither the utility nor why, so the Quality refusal could
            # not be acted on; the probe's own reason says both.
            reason = str(getattr(capability, 'reason', '') or '')
            raise CapabilityUnavailableError(
                unavailable_utility_text(name, reason), details={
                    'utility': name, 'reason': reason})
        return capability.executable

    def _context(self, session: CaseSession) -> OperationContext:
        return OperationContext.from_session(session, capabilities=self._capabilities_registry())

    @staticmethod
    def _require_mesh(session: CaseSession) -> Path:
        poly_mesh = session.case_path / 'constant' / 'polyMesh'
        if not (poly_mesh / 'points').exists() and not (poly_mesh / 'faces').exists():
            raise PreconditionFailedError('the case has no polyMesh', details={
                'case_path': str(session.case_path)})
        return poly_mesh

    #: Which mesh each single-file export is written from, and the reader
    #: on ``ImportExportService`` that finds it. ``vtk`` and ``gmsh`` are
    #: absent because they read the published polyMesh directly. Plan 31
    #: DP-15.
    _EXPORT_SOURCES = {
        'su2': ('native_su2_artifact', 'mesh.su2'),
        'med': ('native_msh_artifact', 'mesh.msh'),
        'unv': ('native_msh_artifact', 'mesh.msh'),
        'cgns': ('native_msh_artifact', 'mesh.msh'),
    }

    @classmethod
    def _require_exportable_mesh(cls, session: CaseSession,
                                 entry_id: str) -> Path:
        """The mesh this format is written from, or a refusal naming both.

        Plan 31 DP-15, the export half of the wall DP-19 met on the readiness
        check. MEASURED in leg t7-su2-r2 of the campaign: ten Gmsh runs
        against an SU2 target meshed, passed the readiness check, and were
        then refused at the export -- because a Gmsh run aimed at SU2
        publishes no ``constant/polyMesh`` by design (the ``mesh.su2`` it
        wrote *is* the mesh, and re-deriving one would hand the user a
        different mesh under the same name), and this precondition asked for
        one anyway. ``ImportExportService.export_su2`` hands over that file
        without ever looking at a polyMesh; it was simply never reached, and
        the GUI showed the refusal inside a progress dialog, so the campaign
        recorded only the window's title.

        Which mesh is needed depends on the format. ``vtk`` and ``gmsh`` read
        the published polyMesh directly -- ``load_case_dataset`` and
        ``load_case_blocks`` open it -- and still demand one. ``su2`` is
        copied from the accepted run's ``mesh.su2``, and ``med``, ``unv`` and
        ``cgns`` are converted from its ``mesh.msh``; each of those is
        satisfied by the artifact its exporter actually reads, asked for
        through the same reader the exporter asks with.
        """
        poly_mesh = session.case_path / 'constant' / 'polyMesh'
        if (poly_mesh / 'points').exists() or (poly_mesh / 'faces').exists():
            return poly_mesh
        reader, filename = cls._EXPORT_SOURCES.get(entry_id, ('', ''))
        if not reader:
            raise PreconditionFailedError('the case has no polyMesh', details={
                'case_path': str(session.case_path), 'entry_id': entry_id})
        from foammesh.core.import_export.service import ImportExportService

        artifact = getattr(ImportExportService, reader)(session.case_path)
        if artifact is not None:
            return Path(artifact.get('path') or '')
        raise PreconditionFailedError(
            f'the case has no mesh the {entry_id} export can read: no '
            f'polyMesh in constant/, and no accepted Gmsh run has recorded a '
            f'native {filename}',
            details={'case_path': str(session.case_path),
                     'poly_mesh': str(poly_mesh), 'entry_id': entry_id})

    @staticmethod
    def _require_readiness_mesh(session: CaseSession) -> Path:
        """The mesh the SU2 readiness check will read, or a refusal naming both.

        Plan 31 DP-19. An SU2 project has no ``constant/polyMesh`` by design,
        so demanding one refused the only QA check that target has. The
        refusal is kept for a case that genuinely has neither mesh, and says
        where it looked for each.
        """
        poly_mesh = session.case_path / 'constant' / 'polyMesh'
        if (poly_mesh / 'points').exists() or (poly_mesh / 'faces').exists():
            return poly_mesh
        from foammesh.core.quality.su2_readiness import native_mesh_path

        native = native_mesh_path(session.case_path)
        if native is not None:
            return native
        raise PreconditionFailedError(
            'the case has no mesh the SU2 readiness check can read: no '
            'polyMesh in constant/, and no accepted Gmsh run has recorded a '
            'native mesh.su2', details={'case_path': str(session.case_path),
                                        'poly_mesh': str(poly_mesh)})

    @staticmethod
    def _canonical_artifact(session: CaseSession, command: Command):
        from foammesh.core.mesh import CanonicalMeshStore
        artifact_id = command.parameters.get('artifact_id')
        if not isinstance(artifact_id, str) or not artifact_id.startswith('cm-'):
            raise ValidationFailedError('artifact_id must be a canonical cm-* ID')
        root = (session.case_path / 'foammesh' / 'canonical').resolve()
        path = (root / artifact_id).resolve()
        if root not in path.parents:
            raise ValidationFailedError('canonical artifact path escapes its store')
        if not path.is_dir():
            raise PreconditionFailedError(
                'canonical mesh artifact was not found', details={'artifact_id': artifact_id})
        try:
            return path, CanonicalMeshStore().read(path)
        except Exception as error:
            raise ValidationFailedError(
                'canonical mesh artifact is invalid', details={
                    'artifact_id': artifact_id, 'error': str(error)}) from error

    def _mesh_canonical_info(self, session: CaseSession,
                             command: Command) -> OperationResult:
        path, mesh = self._canonical_artifact(session, command)
        return self._read_result(session, command, {
            'artifact_id': path.name, 'path': str(path),
            'manifest': mesh.to_manifest()})

    def _mesh_canonical_validate(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        path, mesh = self._canonical_artifact(session, command)
        from foammesh.core.mesh import validate_mesh
        try:
            tolerance = float(command.parameters.get('volume_tolerance', 1e-18))
        except (TypeError, ValueError) as error:
            raise ValidationFailedError('volume_tolerance must be numeric') from error
        report = validate_mesh(
            mesh, volume_tolerance=tolerance,
            require_complete_boundary=bool(
                command.parameters.get('require_complete_boundary', True)))
        return self._read_result(session, command, {
            'artifact_id': path.name, 'validation': report.to_dict()})

    def _quality_canonical(self, session: CaseSession,
                           command: Command) -> OperationResult:
        session.require_writable()
        path, mesh = self._canonical_artifact(session, command)
        from foammesh.core.quality.canonical_quality import QualityError, evaluate_volume
        import os
        try:
            report = evaluate_volume(
                mesh, policy=str(command.parameters.get('policy', 'balanced')))
        except QualityError as error:
            raise ValidationFailedError(str(error)) from error
        destination = path / 'volume-quality.json'
        temporary = destination.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + '\n',
                             encoding='utf-8')
        os.replace(temporary, destination)
        return self._artifact_result(
            session, command, {'artifact_id': path.name, 'report': report.to_dict(),
                               'path': str(destination)}, invalidates=())

    def _canonical_selection_capabilities(self, session: CaseSession,
                                          command: Command) -> OperationResult:
        from foammesh.rendering.selection_capabilities import capability_document
        return self._read_result(session, command, capability_document(
            pick_round_trip_qualified=bool(
                command.parameters.get('pick_round_trip_qualified', False)),
            drawing_qualified=False, topology_edit_qualified=False))

    def _quality_canonical_failed_set_export(self, session: CaseSession,
                                             command: Command) -> OperationResult:
        path, mesh = self._canonical_artifact(session, command)
        quality_path = path / 'volume-quality.json'
        if not quality_path.is_file():
            raise PreconditionFailedError('evaluate canonical quality before exporting a failed set')
        try:
            quality = json.loads(quality_path.read_text(encoding='utf-8'))
            metric = str(command.parameters.get('metric') or '')
            item = next(value for value in quality['failed_sets']
                        if value['metric'] == metric)
            from foammesh.core.quality.canonical_quality import FailedEntitySet
            failed_set = FailedEntitySet(
                item['metric'], item['entity_kind'], item['entity_ids'], item['values'],
                item['threshold'], item['comparison'])
            destination = self._destination(session, command)
            from foammesh.rendering.canonical_mesh_actor import export_failed_entities
            export_failed_entities(mesh, failed_set, destination)
        except StopIteration as error:
            raise ValidationFailedError('requested failed-set metric does not exist') from error
        except (KeyError, ValueError, OSError) as error:
            raise ValidationFailedError(str(error)) from error
        return self._artifact_result(session, command, {
            'artifact_id': path.name, 'metric': metric,
            'destination': str(destination)}, invalidates=())

    def _mesh_canonical_export_openfoam(self, session: CaseSession,
                                        command: Command) -> OperationResult:
        session.require_writable()
        path, mesh = self._canonical_artifact(session, command)
        destination_raw = command.parameters.get('destination')
        destination = (Path(destination_raw) if destination_raw else
                       session.case_path / 'constant' / 'polyMesh').resolve()
        case_root = session.case_path.resolve()
        if case_root != destination and case_root not in destination.parents:
            raise ValidationFailedError('OpenFOAM destination must stay inside the case')
        from foammesh.core.export.poly_mesh_writer import (
            FoamPolyMeshWriter, PolyMeshWriteError,
        )
        try:
            report = FoamPolyMeshWriter().write(destination, mesh)
        except PolyMeshWriteError as error:
            raise ValidationFailedError(str(error)) from error
        return self._artifact_result(
            session, command, {'artifact_id': path.name, 'export': report.to_dict()},
            invalidates=('quality',), event=Event.ARTIFACT_MESH_CHANGED)

    @staticmethod
    def _destination(session: CaseSession, command: Command, *, key: str = 'destination',
                     required_suffix: str | None = None) -> Path:
        raw = command.parameters.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValidationFailedError(f'{key} path is required')
        destination = Path(raw)
        if required_suffix and destination.suffix.lower() != required_suffix:
            raise ValidationFailedError(f'{key} must end with {required_suffix}', details={
                'destination': raw})
        return destination

    @staticmethod
    def _source(command: Command) -> Path:
        raw = command.parameters.get('source')
        if not isinstance(raw, str) or not raw:
            raise ValidationFailedError('source path is required')
        source = Path(raw)
        if not source.exists():
            raise PreconditionFailedError('source path does not exist', details={'source': raw})
        return source

    def _artifact_result(self, session: CaseSession, command: Command, payload: dict,
                         *, invalidates=('mesh', 'quality'), event=Event.ARTIFACT_MESH_CHANGED
                         ) -> OperationResult:
        # R202. A case keeps its state in two stores: the artifact stores the
        # facade writes as it goes (the geometry manifest, the event journal,
        # quality reports, run directories) and configurations.h5, which the
        # GUI reads to draw itself and which used to be written only by an
        # explicit case.save at close. MEASURED on the tee case: an unclean
        # exit left a case whose manifest, mesh, quality report and waiver all
        # described a geometry the GUI db had no record of, so it reopened
        # with an empty geometry list and no task tree beside a verdict strip
        # reading "waived - quality: good". Unusable, and nothing said why.
        # Every operation that reaches here has just made a durable artifact,
        # so the record of what produced it is made durable in the same
        # breath. save() is a no-op unless the db is actually modified, and a
        # read-only session is never asked to persist -- as at the R182 seam,
        # that save would raise and take the whole result down with it.
        if not session.read_only:
            session.state.db.save()
        session.state.bus.publish(event, path=str(session.case_path))
        return OperationResult('accepted', command.operation, session.revisions,
                               invalidated_outputs=tuple(invalidates), payload=payload)

    @staticmethod
    def _record_artifact_transaction(session: CaseSession, command: Command,
                                     *, target: str, reason: str):
        from foammesh.core.project import Source, Transaction
        source_map = {'gui': Source.GUI, 'rest': Source.API, 'cli': Source.CLI,
                      'automation': Source.AGENT, 'system': Source.SYSTEM}
        tx = session.state.log.append(Transaction(
            action=command.operation, source=source_map[command.source.value],
            target=target, reason=reason))
        session.state.bus.publish(Event.TRANSACTION_APPLIED, transaction=tx)
        return tx

    @staticmethod
    def _read_result(session: CaseSession, command: Command, payload: dict) -> OperationResult:
        return OperationResult('accepted', command.operation, session.revisions, payload=payload)

    # -- Slice 1: lifecycle / persistence / history ------------------------ #

    @staticmethod
    def _native_mesh_artifact(case_path):
        """The mesh file an accepted run wrote, where no polyMesh exists.

        DP-62. ``has_mesh`` answers one question -- is there a
        ``constant/polyMesh`` -- and for most of this application that is the
        same question as "is there a mesh". It is not the same question on the
        SU2 route: a Gmsh run targeting SU2 records a publication whose status
        is ``skipped``, because the solver reads the file Gmsh wrote and a
        polyMesh would be a copy nobody asked for. Ten such runs in leg t7-su2
        meshed and then found every Export button disabled.

        SU2 first, then the ``.msh``, because the SU2 artifact is the one the
        run was asked for; the ``.msh`` is there for every accepted run and is
        what the conversion formats are written from. Either is a mesh the
        user can be handed a file of.

        Wrapped, because a classification that cannot answer must not stop a
        case from opening -- the caller reads a payload, not an exception.

        The reading itself now lives in ``core.facade.mesh_presence``, so
        that the shell asking "has this case a mesh" and this payload key
        are one implementation rather than two that agree today.
        """
        from foammesh.core.facade.mesh_presence import native_mesh_artifact
        return native_mesh_artifact(case_path)

    def _classify(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.case import classify_case
        classification = classify_case(session.case_path)
        native_mesh_path = self._native_mesh_artifact(session.case_path)
        payload = {'kind': getattr(classification.kind, 'value', str(classification.kind)),
                   'reasons': list(getattr(classification, 'reasons', [])),
                   'has_mesh': classification.has_mesh,
                   # DP-62. Both, separately: `has_mesh` is still exactly
                   # "there is a polyMesh", which is what every other reader
                   # of this payload means by it, and the export gate asks
                   # the wider question it actually has.
                   'native_mesh_path': native_mesh_path,
                   'has_exportable_mesh': bool(classification.has_mesh
                                               or native_mesh_path),
                   'case_path': str(classification.path),
                   'poly_mesh_path': (str(classification.poly_mesh_path)
                                      if classification.poly_mesh_path else None)}
        return self._read_result(session, command, payload)

    def _save(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        session.state.db.save()
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            retention = int(command.parameters.get('geometry_revision_keep', 3))
            revisions = GeometryArtifactStore(
                session.case_path).garbage_collect_revisions(retention)
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, {'saved': True,
                                                     'path': str(session.storage_path),
                                                     'geometry_revisions': revisions})

    def _copy(self, session: CaseSession, command: Command) -> OperationResult:
        """Copy the case, optionally as a *move* that keeps its history.

        ``carry_history`` is for relocating a scratch case: the same case
        reaching its permanent address should arrive with the record of how it
        got there, where an ordinary Save As makes a second case whose history
        is its own. The live journal is checkpointed first -- a WAL database is
        three files that only agree between transactions, and copying them
        mid-write yields a history that reads short.
        """
        destination = self._destination(session, command)
        from foammesh.core.case import copy_case_directory
        carry_history = bool(command.parameters.get('carry_history'))
        if carry_history and session.journal is not None:
            session.journal.checkpoint()
        result = copy_case_directory(session.case_path, destination,
                                     carry_history=carry_history)
        return self._read_result(session, command, {
            'source': str(result.source), 'destination': str(result.destination),
            'excluded_cache': result.excluded_cache,
            'carried_history': carry_history})

    def _archive(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.import_export.archive import CaseArchiveService
        destination = self._destination(session, command, required_suffix='.zip')
        result = CaseArchiveService().create(session.case_path, destination)
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'archive': {
                                   'archive': str(result.archive), 'files': result.files,
                                   'manifest': dict(result.manifest)},
                                        'destination': str(destination)})

    def _clean_preview(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.import_export.clean import CaseCleanService
        preview = CaseCleanService().preview(session.case_path)
        return self._read_result(session, command, {
            'case_path': str(preview.case_path),
            'removable': [str(path) for path in preview.removable],
            'decomposed_only': preview.decomposed_only,
            'required_confirm_token':
                preview.case_path.name if preview.decomposed_only else None})

    def _clean(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.import_export.clean import CaseCleanService
        service = CaseCleanService()
        preview = service.preview(session.case_path)
        try:
            removed = service.clean(
                preview, confirm_token=command.parameters.get('confirm_token'))
        except ValueError as error:
            raise ValidationFailedError(str(error)) from error
        return self._artifact_result(session, command,
                                     {'removed': removed, 'preview': {
                                         'case_path': str(preview.case_path),
                                         'removable': [str(path) for path in preview.removable]}})

    def _history_query(self, session: CaseSession, command: Command) -> OperationResult:
        limit = command.parameters.get('limit', 200)
        transactions = [tx.to_dict() for tx in session.state.log.all()][-int(limit):]
        from foammesh.core.case import ArtifactHistoryStore
        artifacts = [entry.to_dict() for entry in
                     ArtifactHistoryStore(session.case_path).entries()][-int(limit):]
        return self._read_result(session, command, {
            'transactions': transactions, 'artifacts': artifacts,
            'change_sets': list(session.change_sets)})

    async def _parallel_redistribute(self, session: CaseSession, command: Command) -> OperationResult:
        case_root = (session.case_path if (session.case_path / 'constant').is_dir()
                     else session.case_path / 'case')
        # DP-691. The default was the Parallel Environment dialog's count in
        # local.cfg, so the base grid decomposed for a number the meshing run
        # did not use and castellation threw the processor cases away. It is
        # the run's own rank count now.
        cores = int(command.parameters.get(
            'cores', self._stage_ranks(session, command)))
        if cores < 1:
            raise ValidationFailedError('cores must be positive')
        execution = None
        if cores > 1:
            self._write_decompose_par_dict(
                case_root, cores,
                DecompositionSettings.read(session.state.db))
            execution = await self._run_openfoam_utility(
                session, command, 'decomposePar',
                ('-case', str(case_root), '-force'),
                cwd=case_root, mutation=True,
                expected=(ExpectedArtifact(
                    case_root / 'processor0', kind='processor-case',
                    validator=lambda path: path.is_dir()),))
        payload = {'cores': cores, 'decomposed': cores > 1}
        if execution is not None:
            payload['execution'] = execution.to_payload()
        return OperationResult(
            ('accepted' if execution is None or execution.succeeded else 'failed'),
            command.operation, session.revisions, payload=payload)

    @staticmethod
    def _write_decompose_par_dict(case_root: Path, cores: int,
                                  settings=None) -> Path:
        """Plan 26 WP9.2. The method was hardcoded here and in `case_builder`.

        Both now go through one builder, which is what keeps a method and the
        coefficients it requires from being written apart -- `hierarchical`
        and `simple` refuse to run without an `n` vector, so a dictionary
        naming one of them and omitting it is one `decomposePar` rejects.

        Plan 31 CP-07 item 4 closed the half of that which was still open.
        The method was a *default argument* here and neither caller passed
        one, so applying the Parallel Environment dialog overwrote a
        `hierarchical` dictionary with a `scotch` one and the user's choice
        vanished between two controls on the same case. It is now read from
        the project, and the three fields travel as one object so the order
        and the cell counts cannot be left behind either. (DP-692 removed that
        dialog; `_parallel_redistribute` is the one caller left.)
        """
        from foammesh.openfoam import decomposition

        return decomposition.write(case_root, int(cores), settings)

    async def _run_openfoam_utility(
            self, session: CaseSession, command: Command, utility: str,
            arguments, *, cwd: Path, mutation: bool = False,
            expected=()):
        await self._utility_ready(utility)
        launch = self._capabilities_registry().command(
            utility, arguments, cwd=cwd)
        return await self._context(session).executor.execute(
            session, OperationSpec(
                operation=command.operation, argv=launch.argv, cwd=cwd,
                mutation=mutation,
                timeout=command.parameters.get('timeout_seconds', 900),
                max_output_bytes=4 * 1024 * 1024,
                expected_artifacts=tuple(expected),
                recover_mesh=mutation,
                cleanup_argv=launch.cleanup_argv),
            on_line=command.parameters.get('on_line'))

    async def _launch_terminal(self, session: CaseSession, command: Command) -> OperationResult:
        import shutil
        import shlex
        import subprocess
        registry = self._capabilities_registry()
        await self._utility_ready('blockMesh')
        profile = registry.launch_profile('blockMesh')
        if profile is None:
            raise CapabilityUnavailableError(
                'the qualified OpenFOAM terminal profile is unavailable')
        if profile.kind == 'wsl':
            runtime_cwd = profile.translate_host_path(session.case_path)
            argv = [
                profile.wsl_executable, '--distribution',
                str(profile.distribution),
            ]
            if profile.user:
                argv.extend(('--user', profile.user))
            argv.extend((
                '--cd', runtime_cwd, 'bash', '-lc',
                f'source {shlex.quote(profile.bashrc)}; exec bash',
            ))
            terminal = shutil.which('wt.exe')
            if terminal:
                argv.insert(0, terminal)
                creationflags = 0
            else:
                creationflags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
            process = subprocess.Popen(tuple(argv), cwd=session.case_path,
                                       creationflags=creationflags)
        else:
            terminal = shutil.which('powershell.exe' if __import__('os').name == 'nt'
                                    else 'xterm')
            if terminal is None:
                raise CapabilityUnavailableError(
                    'no supported terminal emulator is available')
            process = subprocess.Popen((terminal,), cwd=session.case_path)
        return self._read_result(session, command, {
            'started': True, 'pid': process.pid,
            'profile_id': profile.profile_id,
            'runtime_fingerprint': registry.runtime_fingerprint('blockMesh'),
        })

    def _stage_clear(self, session: CaseSession, command: Command) -> OperationResult:
        try:
            output_time = int(command.parameters['output_time'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationFailedError('output_time must be an integer') from error
        if tuple(session.jobs.active_job_ids):
            # Clearing stage output while a mesh job is running destroys what
            # that job is working on: for output time 0 the targets include
            # every ``processor*`` case, so a refresh landing mid-run deleted
            # the decomposition a parallel snappy stage was about to read.
            return OperationResult('accepted', command.operation,
                                   session.revisions,
                                   payload={'removed': [],
                                            'skipped': 'a case job is running'})
        from foammesh.support.utils import rmtree
        case_root = (session.case_path if (session.case_path / 'constant').is_dir()
                     else session.case_path / 'case')
        targets = [case_root / str(output_time)]
        # Clear the stage's own output inside each processor case, not the
        # processor cases themselves. Removing them wholesale destroyed the
        # decomposition that a parallel meshing stage runs on, so the mesher
        # was handed a case with no ``processor0`` moments after decomposePar
        # had built one. Redistribution is the parallel-environment operation's
        # job, not stage clearing's.
        targets.extend(case_root.glob(f'processor*/{output_time}'))
        removed = []
        for target in targets:
            if target.exists():
                rmtree(target)
                removed.append(str(target))
        if not removed:
            # Clearing a stage that had nothing staged is not a mesh change.
            # Announcing one anyway is a closed loop: the desktop window
            # refreshes on ARTIFACT_MESH_CHANGED, and its refresh clears every
            # downstream stage -- so a no-op clear brought the window straight
            # back to the same refresh, tens of times a second, until the
            # command scheduler was too deep for any mesh run to be reached.
            return OperationResult('accepted', command.operation,
                                   session.revisions, payload={'removed': []})
        return self._artifact_result(
            session, command, {'removed': removed}, invalidates=('mesh', 'quality'))

    # -- Slice 2: geometry ------------------------------------------------- #

    _GEOMETRY_EXTENSIONS = {'.stl', '.obj', '.step', '.stp', '.iges', '.igs', '.brep'}

    async def _geometry_import(self, session: CaseSession, command: Command) -> OperationResult:
        import asyncio
        session.require_writable()
        source = self._source(command)
        if source.suffix.lower() not in self._GEOMETRY_EXTENSIONS:
            raise ValidationFailedError('unsupported geometry format', details={
                'suffix': source.suffix,
                'supported': sorted(self._GEOMETRY_EXTENSIONS)})
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_engine_id)
        engine_id = configured_engine_id(session.state.db)
        if engine_id in ENGINE_REGISTRY.ids() and not getattr(
                ENGINE_REGISTRY.get(engine_id), 'mixes_cad_and_surfaces', True):
            # DP-638: a STEP beside an STL is a case Gmsh cannot mesh.
            self._refuse_mixed_sources_for_gmsh(session, (source,))
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.diagnostics import budget as budget_module

        # Import runs surface diagnostics in-process, and one of them used to
        # be unbounded: a 37,240-triangle propeller wedged the application for
        # over seven hours with nothing on screen. Publishing the same job
        # events a subprocess would lets the status-bar widget show progress
        # and drive its Cancel button, and registering the budget gives that
        # button something to act on.
        job_id = f'geometry-import-{uuid.uuid4().hex[:12]}'
        loop = asyncio.get_running_loop()

        def progress(check, fraction, message):
            # Reported from the worker thread; the bus belongs to the loop.
            loop.call_soon_threadsafe(lambda: session.state.bus.publish(
                Event.JOB_PROGRESS, job_id=job_id, name=command.operation,
                stage=check, fraction=fraction, message=message))

        budget = budget_module.budget_from_settings(
            'geometry.import', on_progress=progress)
        budget_module.register(job_id, budget)
        session.state.bus.publish(
            Event.JOB_STARTED, job_id=job_id, name=command.operation,
            argv=[], cwd=str(session.case_path), mutation=True,
            environment_fingerprint='in-process',
            message=f'importing {source.name}')
        try:
            # STL and OBJ declare no length unit and FoamMesh works in metres,
            # so the caller says which the file is in. Absent means metres,
            # which is what every caller got implicitly before this existed.
            # `tessellation` is how fine a CAD source is faceted. It used to
            # be a constant inside the store, so the panel that asks for it
            # had nowhere to send the answer and no import ever recorded what
            # it had been faceted at.
            #
            # DP-508 (MA-12). Off the loop. In the desktop this handler ran on
            # the GUI thread, so the status-bar Cancel the budget is registered
            # for could never be clicked: MEASURED, `helical_pipe.step` held
            # the owner loop for 9.3 s in one slice at the default deflection,
            # and the audit's import of it sat at "Not Responding" for four
            # minutes.
            imported = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).import_file,
                source, budget=budget,
                unit=command.parameters.get('unit'),
                tessellation=command.parameters.get('tessellation'))
        except budget_module.Cancelled as error:
            session.state.bus.publish(
                Event.JOB_CANCELLED, job_id=job_id, name=command.operation)
            raise PreconditionFailedError(
                str(error), details={'error': 'import_cancelled'}) from error
        except RuntimeError as error:
            session.state.bus.publish(
                Event.JOB_FAILED, job_id=job_id, name=command.operation,
                error=str(error))
            if source.suffix.lower() in {'.step', '.stp', '.iges', '.igs', '.brep'}:
                raise CapabilityUnavailableError(
                    str(error), details={'capability': 'pythonocc-core',
                                         'error': 'occt_unavailable'}) from error
            raise ValidationFailedError(str(error)) from error
        except (OSError, ValueError) as error:
            session.state.bus.publish(
                Event.JOB_FAILED, job_id=job_id, name=command.operation,
                error=str(error))
            raise ValidationFailedError(str(error)) from error
        except MemoryError as error:
            # DP-1160. MEASURED: a 0.56 MB OBJ ran the import thread out of
            # memory in the shell-pair check, and MemoryError is neither a
            # RuntimeError nor an OSError, so it left this handler with the
            # job STARTED and never ended -- the status bar showed the import
            # running forever and the page's handler, which catches the
            # facade's errors, never saw it.
            message = out_of_memory_message(source.name)
            session.state.bus.publish(
                Event.JOB_FAILED, job_id=job_id, name=command.operation,
                error=message)
            raise ValidationFailedError(message, details={
                'error': 'out_of_memory'}) from error
        except Exception as error:  # noqa: BLE001 - the job must end
            # DP-1160. Whatever else escapes the reader or the checks, the job
            # it started has to be ended, and the page has to be told in a
            # form its handler catches.
            message = f'{source.name} could not be imported: {error}'
            logger.exception('geometry import of %s failed', source)
            session.state.bus.publish(
                Event.JOB_FAILED, job_id=job_id, name=command.operation,
                error=message)
            raise ValidationFailedError(message, details={
                'error': 'import_failed'}) from error
        finally:
            budget_module.unregister(job_id)
        session.state.bus.publish(
            Event.JOB_FINISHED, job_id=job_id, name=command.operation,
            returncode=0)
        if budget.warnings:
            imported['diagnostic_warnings'] = list(budget.warnings)
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=imported['geometry_id'], artifact=imported['artifact'])
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=imported)

    async def _geometry_diagnostics(self, session: CaseSession, command: Command) -> OperationResult:
        import asyncio

        from foammesh.core.geometry import GeometryArtifactStore
        # DP-1160. Off the owner loop: `diagnose` re-reads and re-assesses
        # every geometry its cache does not hold, which is the same work the
        # import runs on a thread, and run here it held the loop -- and in
        # the desktop the GUI -- for as long as that took.
        # The arguments are still read here, on the loop, before the thread.
        try:
            reports = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).diagnose,
                command.parameters.get('geometry_id'),
                target_cell_size=self._readiness_target_cell_size(session),
                engine=self._readiness_engine(session))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except MemoryError as error:
            raise ValidationFailedError(
                out_of_memory_message(
                    command.parameters.get('geometry_id') or 'all geometry',
                    noun='geometry'),
                details={'error': 'out_of_memory'}) from error
        return self._read_result(session, command, {
            'geometry_count': len(reports), 'geometries': reports})

    @staticmethod
    def _readiness_target_cell_size(session: CaseSession) -> float | None:
        """The size a geometry verdict is taken against, or None (DP-409).

        Four call sites ask the store for the same verdict and three of them
        used to ask it differently: the readiness operation passed the target
        cell size and the engine, the preparation decision passed only the
        engine, the repair plan passed only the size, and the diagnostics
        operation passed only the engine. `assess` is handed both and grades
        by both, so those are four different questions about one geometry,
        and the gate that decides preparation was reading a verdict the
        readiness page never showed. They ask the same question now.
        """
        try:
            return float(session.state.db.getValue('baseGrid/targetCellSize'))
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _readiness_engine(session: CaseSession) -> str | None:
        """Which mesher a geometry verdict is being asked for (DP-114).

        ``None`` while no engine is chosen: the verdict is then advisory for
        both, which is the honest answer and not a silent vote for either.
        """
        from foammesh.core.engine import configured_engine_id
        engine = configured_engine_id(session.state.db)
        return engine if engine in ('snappy', 'gmsh') else None

    def _geometry_readiness(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.geometry import GeometryArtifactStore
        store = GeometryArtifactStore(session.case_path)
        try:
            report = store.readiness_report(
                command.parameters.get('geometry_id'),
                target_cell_size=self._readiness_target_cell_size(session),
                engine=self._readiness_engine(session))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        destination = command.parameters.get('destination')
        if destination:
            destination = Path(str(destination))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            report = {**report, 'exported_to': str(destination)}
        return self._read_result(session, command, report)

    def _geometry_preparation_decide(
            self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.project import Source
        from foammesh.db.configurations_schema import GeometryPreparationDecision

        raw = str(command.parameters.get('decision', '')).lower()
        try:
            decision = GeometryPreparationDecision(raw)
        except ValueError as error:
            raise ValidationFailedError('invalid geometry preparation decision', details={
                'decision': raw,
                'supported': [item.value for item in GeometryPreparationDecision
                              if item is not GeometryPreparationDecision.UNDECIDED],
            }) from error
        if decision is GeometryPreparationDecision.UNDECIDED:
            raise ValidationFailedError('undecided is not a completion decision')

        report = GeometryArtifactStore(session.case_path).readiness_report(
            target_cell_size=self._readiness_target_cell_size(session),
            engine=self._readiness_engine(session))
        states = {
            item['diagnostics']['readiness']['state']
            for item in report['geometries']
            if item['diagnostics'].get('readiness')
        }
        acknowledgment = command.parameters.get('ack_reason')
        # DP-125. `blocked` is overridable on a written reason. That is right
        # where the block is a judgement -- a wrap recommendation, a risk the
        # user may know better than the checks do -- and wrong where it is a
        # fact about the chosen engine. DP-114 taught the grader that a free
        # edge is a leak risk to snappy and fatal to Gmsh, which has to fill a
        # volume the surface bounds; it did not stop the override. MEASURED on
        # `cyclone` in the `9f6abca1` sweep: readiness said `blocked` and named
        # the 54 edges, the Prepare step took a written reason for them, and
        # the Gmsh run died 62 s later on the same 54 edges. No reason a user
        # can write closes a hole, so the override is refused with the routes
        # that do: repair, wrap, or the engine that meshed this geometry.
        fatal = sorted({
            kind
            for item in report['geometries']
            for kind in ((item['diagnostics'].get('readiness') or {})
                         .get('engine_fatal') or ())})
        if (decision in (GeometryPreparationDecision.AS_IS,
                         GeometryPreparationDecision.OVERRIDDEN) and fatal):
            engine = self._readiness_engine(session)
            raise ValidationFailedError(
                f'this geometry cannot be meshed by {engine} as it is, and no '
                'acknowledgement changes that: '
                + '; '.join(sorted({
                    reason
                    for item in report['geometries']
                    for reason in ((item['diagnostics'].get('readiness') or {})
                                   .get('reasons') or ())}))
                + '. Repair or wrap the geometry, or choose the other engine.',
                details={'readiness_states': sorted(states),
                         'engine': engine,
                         'engine_fatal': fatal})
        risky = bool(states & {'blocked', 'wrap_recommended'})
        if decision is GeometryPreparationDecision.AS_IS and risky:
            if not isinstance(acknowledgment, str) or not acknowledgment.strip():
                raise ValidationFailedError(
                    'ack_reason is required to use blocked geometry as-is',
                    details={'readiness_states': sorted(states)})
            decision = GeometryPreparationDecision.OVERRIDDEN

        source_map = {
            'gui': Source.GUI, 'rest': Source.API, 'cli': Source.CLI,
            'automation': Source.AGENT, 'system': Source.SYSTEM,
        }
        data = session.state.checkout()
        data.setValue('geometryPreparation/decision', decision)
        data.setValue('geometryPreparation/geometryFingerprint',
                      report['geometry_fingerprint'])
        data.setValue('geometryPreparation/acknowledgment',
                      acknowledgment.strip() if isinstance(acknowledgment, str) else None)
        data.setValue('geometryPreparation/rulesVersion', 1)
        tx = session.state.commit(
            data, action='record geometry preparation decision',
            source=source_map[command.source.value], target='geometryPreparation',
            reason=acknowledgment or decision.value)
        session.state.bus.publish(
            Event.GEOMETRY_PREPARATION_DECIDED, decision=decision.value,
            geometry_fingerprint=report['geometry_fingerprint'], transaction=tx)
        return OperationResult('accepted', command.operation, session.revisions, payload={
            'decision': decision.value,
            'geometry_fingerprint': report['geometry_fingerprint'],
            'rules_version': 1,
            'transaction': tx.to_dict(),
        })

    @staticmethod
    def _invalidate_geometry_preparation(session: CaseSession) -> bool:
        """Reopen the preparation decision, and say whether it had to.

        DP-350. The answer is the caller's evidence that anything happened.
        A decision already reading `undecided` is the ordinary state of a
        case nobody has prepared yet, and an operation that reaches this and
        finds it has changed nothing.
        """
        from foammesh.core.project import Source
        from foammesh.db.configurations_schema import GeometryPreparationDecision
        current = session.state.db.getValue('geometryPreparation/decision')
        current_value = current.value if hasattr(current, 'value') else current
        if current_value == GeometryPreparationDecision.UNDECIDED.value:
            return False
        data = session.state.checkout()
        data.setValue('geometryPreparation/decision', GeometryPreparationDecision.UNDECIDED)
        data.setValue('geometryPreparation/geometryFingerprint', None)
        data.setValue('geometryPreparation/acknowledgment', None)
        session.state.commit(
            data, action='reopen geometry preparation', source=Source.SYSTEM,
            target='geometryPreparation', reason='geometry changed')
        return True

    def _geometry_classify(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            classifications = GeometryArtifactStore(session.case_path).classify(
                command.parameters.get('geometry_id'))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        return self._read_result(session, command, {
            'geometry_count': len(classifications), 'geometries': classifications})

    def _geometry_split(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        geometry_id = command.parameters.get('geometry_id')
        if not geometry_id:
            raise ValidationFailedError('geometry_id is required')
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            created = GeometryArtifactStore(session.case_path).split(
                geometry_id, replace_source=bool(command.parameters.get('replace_source', False)))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, created=[item['geometry_id'] for item in created])
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'),
            payload={'source_geometry_id': geometry_id, 'geometries': created})

    # -- boundary patches (Plan 28 WP5) ------------------------------------ #
    #
    # A CAD import names its boundaries `Face_1` .. `Face_212` and a
    # tessellated one names the whole surface after the file. Those names went
    # to the solver unchanged, because nothing in the product could edit them:
    # `PatchSet` had held rename/merge/split since Plan 12 and no caller ever
    # reached it. These four operations are that caller.

    @staticmethod
    def _patch_editor(session: CaseSession):
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.patches.manifest import PatchGroupEditor

        return PatchGroupEditor(GeometryArtifactStore(session.case_path))

    def _geometry_patches_list(self, session: CaseSession,
                               command: Command) -> OperationResult:
        """Every boundary the case would produce, and what each covers."""
        rows = self._patch_editor(session).rows()
        return self._read_result(session, command, {
            'patches': rows,
            'count': len(rows),
            # The one-boundary case is worth saying out loud here as well as on
            # the readiness task: a caller that never opens the GUI still has
            # to be able to see it.
            'single_boundary': len(rows) == 1,
        })

    def _geometry_patch_edit(self, session: CaseSession, command: Command,
                             apply) -> OperationResult:
        """Run one patch edit and record it as a reversible transaction.

        Every edit invalidates the prepared revision, because the prepared
        groups *are* the patch records: leaving a published revision in place
        would show the user the old names beside the new ones with no way to
        tell which the solver would see.
        """
        from foammesh.core.geometry.patches.ops import PatchError

        session.require_writable()
        editor = self._patch_editor(session)
        before = self._patch_rows(editor)
        try:
            action, detail = apply(editor)
        except PatchError as error:
            raise ValidationFailedError(str(error)) from error
        except (KeyError, OSError) as error:
            raise PreconditionFailedError(str(error)) from error
        rows = editor.rows()
        reconciled = self._reconcile_geometry_rows(session, before, rows)
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            action=action, detail=detail)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'),
            payload={'action': action, 'detail': detail,
                     'patches': rows, 'geometry_rows': reconciled})

    def _geometry_patches_rename(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        """Name a boundary what it is. The uuid does not move with the name."""
        patch_uuid = str(command.parameters.get('patch_uuid') or '').strip()
        name = command.parameters.get('name')
        if not patch_uuid:
            raise ValidationFailedError('patch_uuid is required')
        return self._geometry_patch_edit(
            session, command, lambda editor: editor.rename(patch_uuid, name))

    def _geometry_patches_merge(self, session: CaseSession,
                                command: Command) -> OperationResult:
        """Fold several sub-surfaces into one named boundary.

        The operation a 212-face CAD import actually needs: an inlet is one
        boundary condition, not forty faces the solver has to be told about
        one at a time.
        """
        patch_uuids = command.parameters.get('patch_uuids')
        if not isinstance(patch_uuids, (list, tuple)):
            raise ValidationFailedError('patch_uuids must be a list')
        name = command.parameters.get('name')
        return self._geometry_patch_edit(
            session, command,
            lambda editor: editor.merge(list(patch_uuids), name))

    def _geometry_patches_split(self, session: CaseSession,
                                command: Command) -> OperationResult:
        """Undo a merge, restoring each sub-surface's original identity."""
        patch_uuid = str(command.parameters.get('patch_uuid') or '').strip()
        if not patch_uuid:
            raise ValidationFailedError('patch_uuid is required')
        return self._geometry_patch_edit(
            session, command, lambda editor: editor.split(patch_uuid))

    def _geometry_patches_split_by_angle(self, session: CaseSession,
                                         command: Command) -> OperationResult:
        """Cut a tessellated surface into boundaries along its feature edges.

        Plan 28 WP7. The other patch edits rearrange sub-surfaces that
        already exist; this one makes them, which is why a single-solid STL
        needs it before any of the others can do anything. It writes a
        geometry revision, so it is a mesh mutation with artifact backup
        like ``geometry.split``, not a reversible manifest edit. ``preview``
        reports what the cut would produce and writes nothing.
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.patches.feature_split import FeatureSplitError

        preview = bool(command.parameters.get('preview', False))
        if not preview:
            session.require_writable()
        store = GeometryArtifactStore(session.case_path)
        before = self._patch_rows(self._patch_editor(session))
        geometry_id = str(command.parameters.get('geometry_id') or '').strip()
        if not geometry_id:
            # One geometry needs no id; more than one would make the choice
            # for the user, which is the one thing it must not do. F-27: a
            # CAD geometry counts, its tessellation being what gets cut.
            candidates = list(store.entries())
            if not candidates:
                raise ValidationFailedError('no geometry to split')
            if len(candidates) > 1:
                raise ValidationFailedError(
                    'geometry_id is required when the case holds more than '
                    'one geometry', details={
                        'geometry_ids': [item['geometry_id'] for item in candidates]})
            geometry_id = candidates[0]['geometry_id']
        try:
            result = store.split_by_angle(
                geometry_id,
                angle_deg=command.parameters.get('angle_deg'),
                min_area_fraction=command.parameters.get('min_area_fraction') or 0.0,
                preview=preview)
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (FeatureSplitError, OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        if preview:
            return self._read_result(session, command, result)
        rows = self._patch_editor(session).rows()
        reconciled = self._reconcile_geometry_rows(session, before, rows)
        self._invalidate_geometry_preparation(session)
        report = (result.get('revisions') or [{}])[-1].get('report') or {}
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, revision=result.get('revision'),
            count=report.get('count'))
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'),
            payload={'geometry_id': geometry_id, 'revision': result.get('revision'),
                     'split': report, 'patches': rows,
                     'geometry_rows': reconciled})

    def _geometry_split_interfaces(self, session: CaseSession,
                                   command: Command) -> OperationResult:
        """Cut an assembly into the surfaces OpenFOAM 13 names.

        DP-421/DP-422. ``multiRegion/CHT/heatedDuct`` is written as one
        surface per interface plus one for the outer skin, each interface
        carrying its own ``faceZone``/``cellZone`` pair and the point inside
        the region it encloses. FoamMesh could not author that shape: an
        imported body is one closed surface, and one closed surface can be
        typed one thing, so a conjugate assembly came out as two cell zones
        with the shared wall drawn twice and no interface at all.

        This is the operation that makes the shape: the store cuts the shared
        walls away, and the rows here are what the writer reads -- the body
        keeps its own skin as a plain boundary, the shared wall becomes a
        surface typed ``interface`` hanging off the enclosed body's volume,
        and that volume is typed ``cellZone`` and carries the seed. Bodies
        that share nothing are the single-region catalogue and are left
        exactly as they were, rows and fingerprints both.

        ``preview`` measures and writes nothing, in either store.
        """
        from foammesh.core.geometry import GeometryArtifactStore

        preview = bool(command.parameters.get('preview', False))
        if not preview:
            session.require_writable()
        store = GeometryArtifactStore(session.case_path)
        geometry_ids = command.parameters.get('geometry_ids') or None
        try:
            result = store.split_interfaces(geometry_ids, preview=preview)
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        if preview:
            return self._read_result(session, command, result)
        rows = self._rows_from_the_cut(session, result)
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            count=len(result['interfaces']))
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'),
            payload={'is_assembly': result['is_assembly'],
                     'interfaces': result['interfaces'],
                     'bodies': result['bodies'],
                     'warnings': result['warnings'],
                     'geometry_rows': rows})

    def _rows_from_the_cut(self, session: CaseSession, result: dict) -> dict:
        """Give the project tree the pieces the cut left behind.

        Three edits, and each of them is a fact the snappy writer reads. The
        bodies that lost faces get their polydata replaced, or the tree and
        the viewport would go on showing a wall that is no longer theirs.
        The enclosed body's volume is typed ``cellZone`` and given the seed,
        because ``surfaceZonesInfo.C:79-82`` makes the point mandatory under
        ``mode insidePoint`` and an interface -- enclosing nothing -- cannot
        be written any other way. And the interface itself becomes a surface
        row typed ``interface`` under that volume, which is the shape
        ``_effective_cfd_type`` reads to decide the wall carves the zone.
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.patches.pieces import patch_polydata
        from foammesh.core.project import Source
        from foammesh.db.configurations_schema import CFDType, GeometryType, Shape

        if not result['interfaces']:
            # Nothing was cut, so nothing in the tree is out of date. Most of
            # the catalogue lands here and must not pay a commit for it.
            return {}
        artifacts = {str(entry.get('geometry_id')): entry.get('artifact')
                     for entry in GeometryArtifactStore(session.case_path).entries()}
        editor = self._patch_editor(session)
        manifest = self._patch_rows(editor)
        data = session.state.checkout()
        summary: dict = {'bodies': {}, 'interfaces': []}

        for body in result['bodies']:
            if not body['removed']:
                continue
            geometry_id = str(body['geometry_id'])
            keys = self._owned_surface_keys(data, geometry_id, [])
            artifact = artifacts.get(geometry_id)
            if not keys or not artifact:
                summary['bodies'][geometry_id] = {'refreshed': False}
                continue
            kept = self._rows_for(manifest, geometry_id)
            pieces = patch_polydata(artifact, kept)
            refreshed = self._refresh_polydata(data, keys, kept, pieces)
            summary['bodies'][geometry_id] = {'refreshed': True,
                                              'rows': refreshed}

        for item in result['interfaces']:
            enclosed = str(item.get('inside_geometry_id') or '')
            volume = self._volume_of(data, enclosed)
            if volume is None:
                raise PreconditionFailedError(
                    'the body this interface encloses has no volume in the '
                    'project tree', details={'geometry_id': enclosed,
                                             'interface': item['name']})
            data.setValue(f'geometry/{volume}/cfdType', CFDType.CELL_ZONE.value)
            for axis, value in zip(('x', 'y', 'z'), item['inside_point']):
                data.setValue(
                    f'geometry/{volume}/zoneInsidePoint/{axis}', float(value))
            data.setValue(f'geometry/{volume}/zoneInsidePointSet', True)

            geometry_id = str(item['geometry_id'])
            rows = self._rows_for(manifest, geometry_id)
            if not rows:
                raise PreconditionFailedError(
                    'the interface the cut wrote has no boundary of its own',
                    details={'geometry_id': geometry_id,
                             'interface': item['name']})
            pieces = patch_polydata(artifacts.get(geometry_id), rows)
            added = []
            for row in rows:
                name = str(row.get('name'))
                surface = data.newElement('geometry')
                surface.setValue('gType', GeometryType.SURFACE.value)
                surface.setValue('geometryId', geometry_id)
                if row.get('patch_uuid'):
                    surface.setValue('patchUuid', str(row['patch_uuid']))
                surface.setValue('name', name)
                surface.setValue('shape', Shape.TRI_SURFACE_MESH.value)
                surface.setValue('cfdType', CFDType.INTERFACE.value)
                surface.setValue('volume', volume)
                surface.setValue('path', data.addGeometryPolyData(pieces[name]))
                added.append(str(data.addElement('geometry', surface)))
            summary['interfaces'].append({
                'geometry_id': geometry_id, 'name': item['name'],
                'volume': str(volume), 'added': added,
                'inside_point': list(item['inside_point'])})

        session.state.commit(
            data, action='split interfaces', source=Source.GUI,
            target='geometry',
            reason='; '.join(str(item['name'])
                             for item in summary['interfaces']))
        for item in summary['interfaces']:
            item['added'] = [str(data.remappedKey('geometry', key))
                             for key in item['added']]
        return summary

    @staticmethod
    def _refresh_polydata(data, keys: list, rows: list, pieces: dict) -> list:
        """Point each surviving row at the shell the cut left it.

        By patch identity first, then by name, and by position only when the
        two lists are the same length -- which is the single-solid body, the
        one case where a name can have drifted (an import suffixes a name the
        tree already holds) and position still says the right thing.
        """
        elements = {str(key): data.getElement('geometry', key) for key in keys}
        by_uuid = {str(element.value('patchUuid')): key
                   for key, element in elements.items()
                   if element.value('patchUuid')}
        by_name = {str(element.value('name')): key
                   for key, element in elements.items()}
        moved = []
        for position, row in enumerate(rows):
            name = str(row.get('name'))
            key = by_uuid.get(str(row.get('patch_uuid') or ''))
            if key is None:
                key = by_name.get(name)
            if key is None and len(rows) == len(keys):
                key = str(keys[position])
            if key is None or name not in pieces:
                continue
            previous = elements[key].value('path')
            if previous:
                data.removeGeometryPolyData(previous)
            data.setValue(f'geometry/{key}/path',
                          data.addGeometryPolyData(pieces[name]))
            moved.append(key)
        return moved

    @staticmethod
    def _volume_of(data, geometry_id: str):
        """The volume row the surfaces of one artifact hang off."""
        from foammesh.db.configurations_schema import GeometryType

        if not geometry_id:
            return None
        for key in data.getKeys(
                'geometry',
                lambda key, element: (
                    str(element.get('gType')) == GeometryType.SURFACE.value
                    and str(element.get('geometryId') or '') == str(geometry_id))):
            volume = data.getElement('geometry', key).value('volume')
            if volume is not None and str(volume) != '':
                return volume
        return None

    # -- one boundary, two stores (R169) ----------------------------------- #

    def _reconcile_geometry_rows(self, session: CaseSession,
                                 before: list, after: list) -> dict:
        """Give the project tree the boundaries the manifest now holds.

        A boundary lives in two stores: the artifact's patch manifest, which
        is what gets meshed, and a database row, which is what the geometry
        tree lists and what the case builder binds a prepared group to -- by
        name, because the row has no other handle on the artifact. Cutting a
        surface into five boundaries on the Repair page wrote only the
        manifest. The tree went on showing the single surface the import
        made, and the case builder, asked to map five prepared groups onto
        one row, refused to write any dictionary at all: every snappy run
        died before blockMesh, with the boundaries it was refusing to bind
        listed on the Repair page and nowhere else. A patch edit now moves
        both stores, so the boundaries appear under Geometry, where they are
        used, and not only under Repair, where they were cut.

        Returns what changed per geometry, for the operation payload.
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.patches.pieces import patch_polydata
        from foammesh.core.project import Source
        from foammesh.db.configurations_schema import CFDType, GeometryType, Shape

        touched = {}
        for geometry_id in {str(row.get('geometry_id') or '')
                            for row in list(before or ()) + list(after or ())}:
            if not geometry_id:
                continue
            was = self._rows_for(before, geometry_id)
            now = self._rows_for(after, geometry_id)
            if [row.get('name') for row in was] == [row.get('name') for row in now]:
                continue
            touched[geometry_id] = (was, now)
        if not touched:
            return {}

        artifacts = {str(entry.get('geometry_id')): entry.get('artifact')
                     for entry in GeometryArtifactStore(session.case_path).entries()}
        data = session.state.checkout()
        summary: dict = {}
        for geometry_id, (was, now) in sorted(touched.items()):
            artifact = artifacts.get(geometry_id)
            owned = self._owned_surface_keys(data, geometry_id, was)
            if not artifact or not owned or not now:
                # Nothing in the tree stands for this artifact: an import that
                # never reached the database, or a geometry the user removed.
                # Saying so beats guessing which rows to replace.
                summary[geometry_id] = {
                    'reconciled': False,
                    'reason': ('no artifact' if not artifact
                               else 'no boundaries' if not now
                               else 'no matching geometry row')}
                continue
            pieces = patch_polydata(artifact, now)
            missing = [str(row.get('name')) for row in now
                       if str(row.get('name')) not in pieces]
            if missing:
                raise PreconditionFailedError(
                    'the geometry artifact does not hold every boundary its '
                    'manifest names', details={'geometry_id': geometry_id,
                                               'missing': missing})
            keep = set(owned)
            taken = data.getKeys(
                'geometry',
                lambda key, element: (str(key) not in keep
                                      and str(element.get('name')) in pieces))
            if taken:
                raise ValidationFailedError(
                    'another geometry already carries one of these boundary '
                    'names', details={'geometry_id': geometry_id,
                                      'geometry_ids': [str(key) for key in taken]})
            template = data.getElement('geometry', owned[0])
            inherited = {field: template.value(field)
                         for field in ('volume', 'cfdType', 'castellationGroup',
                                       'layerGroup', 'slaveLayerGroup')}
            for key in owned:
                path = data.getElement('geometry', key).value('path')
                if path:
                    data.removeGeometryPolyData(path)
                data.removeElement('geometry', key)
            added = []
            for row in now:
                name = str(row.get('name'))
                element = data.newElement('geometry')
                element.setValue('gType', GeometryType.SURFACE.value)
                element.setValue('geometryId', geometry_id)
                # DP-383. The rebuilt row keeps the patch identity too, or the
                # first edit on the Repair page would cost the tree the handle
                # the edit dialog renames by.
                if row.get('patch_uuid'):
                    element.setValue('patchUuid', str(row['patch_uuid']))
                element.setValue('name', name)
                element.setValue('shape', Shape.TRI_SURFACE_MESH.value)
                element.setValue('cfdType',
                                 inherited['cfdType'] or CFDType.BOUNDARY.value)
                for field in ('volume', 'castellationGroup', 'layerGroup',
                              'slaveLayerGroup'):
                    if inherited[field] is not None:
                        element.setValue(field, inherited[field])
                element.setValue('path', data.addGeometryPolyData(pieces[name]))
                added.append(str(data.addElement('geometry', element)))
            summary[geometry_id] = {
                'reconciled': True, 'removed': list(owned), 'added': added,
                'names': [str(row.get('name')) for row in now]}

        if not any(item.get('reconciled') for item in summary.values()):
            return summary
        session.state.commit(
            data, action='reconcile geometry boundaries', source=Source.GUI,
            target='geometry',
            reason='; '.join('{0}: {1}'.format(key, ', '.join(item['names']))
                             for key, item in sorted(summary.items())
                             if item.get('reconciled')))
        # DP-16. The keys reported back are the ones the rows ended up under:
        # a merge that found an allocated key already taken moves the new row
        # rather than refusing it.
        for item in summary.values():
            if item.get('reconciled'):
                item['added'] = [str(data.remappedKey('geometry', key))
                                 for key in item['added']]
        return summary

    @staticmethod
    def _rows_for(rows: list, geometry_id: str) -> list:
        """The manifest rows belonging to one geometry artifact."""
        return [row for row in rows or ()
                if str(row.get('geometry_id') or '') == str(geometry_id)]

    @staticmethod
    def _owned_surface_keys(data, geometry_id: str, previous: list) -> list:
        """The database rows that stand for one artifact's boundaries.

        The stamped id first, because it is the only handle that survives a
        rename. Then the names the manifest carried before the edit. Then the
        volume the import grouped them under -- a surface imported before this
        field existed is called ``<volume>_surface`` while the manifest calls
        the same surface ``<volume>``, so neither name finds the other and
        only the grouping does.
        """
        from foammesh.db.configurations_schema import GeometryType

        def _surfaces(test):
            return [str(key) for key in data.getKeys(
                'geometry',
                lambda key, element: (
                    str(element.get('gType')) == GeometryType.SURFACE.value
                    and test(key, element)))]

        keys = _surfaces(lambda key, element:
                         str(element.get('geometryId') or '') == str(geometry_id))
        if keys:
            return keys
        names = {str(row.get('name')) for row in previous or ()}
        if not names:
            return []
        keys = _surfaces(lambda key, element: str(element.get('name')) in names)
        if keys:
            return keys
        volumes = {str(key) for key in data.getKeys(
            'geometry',
            lambda key, element: (
                str(element.get('gType')) == GeometryType.VOLUME.value
                and str(element.get('name')) in names))}
        if not volumes:
            return []
        return _surfaces(lambda key, element:
                         str(element.get('volume')) in volumes)

    # -- one name, two stores (Plan 29 WP4) -------------------------------- #

    def _geometry_rename(self, session: CaseSession,
                         command: Command) -> OperationResult:
        """Give a surface one name in the tree and in the solver.

        A surface carries a display name in the project database and a solver
        name in the geometry artifact's patch manifest. Both were editable and
        neither wrote to the other, so renaming in the edit dialog left the
        Repair page showing the old name and renaming on the Repair page left
        the tree showing the old one. This moves both, or neither.
        """
        from foammesh.core.geometry.patches.ops import PatchError, RESERVED_NAMES
        from foammesh.core.project import Source

        session.require_writable()
        geometry_id = str(command.parameters.get('geometry_id') or '').strip()
        if not geometry_id:
            raise ValidationFailedError('geometry_id is required')
        name = command.parameters.get('name')
        name = str('' if name is None else name).strip()
        if not name:
            raise ValidationFailedError(
                'a boundary needs a name',
                details={'geometry_id': geometry_id})
        if name in RESERVED_NAMES:
            raise ValidationFailedError(
                'OpenFOAM keeps {0!r} for itself'.format(name),
                details={'name': name, 'reserved': list(RESERVED_NAMES)})

        data = session.state.checkout()
        if not data.hasElement('geometry', geometry_id):
            raise PreconditionFailedError(
                'no such geometry', details={'geometry_id': geometry_id})
        previous = str(data.getElement('geometry', geometry_id).value('name'))
        taken = data.getKeys(
            'geometry',
            lambda key, element: (str(key) != str(geometry_id)
                                  and str(element.get('name')) == name))
        if taken:
            raise ValidationFailedError(
                'another geometry is already called {0!r}'.format(name),
                details={'name': name, 'geometry_ids': [str(k) for k in taken]})

        if previous == name:
            # Saying the same name again is not an error and must not spend a
            # revision or invalidate the prepared geometry.
            return OperationResult(
                'accepted', command.operation, session.revisions,
                payload={'geometry_id': geometry_id, 'name': name,
                         'previous_name': previous, 'patch_renamed': False})

        # The manifest goes first: it is the store that can still refuse, and
        # undoing a patch rename is a second write, while abandoning an
        # uncommitted working copy costs nothing.
        editor = self._patch_editor(session)
        patch_uuid = self._patch_uuid_for(
            editor, data.getElement('geometry', geometry_id), previous, data)
        action = None
        if patch_uuid is not None:
            try:
                action = editor.rename(patch_uuid, name)
            except PatchError as error:
                raise ValidationFailedError(str(error)) from error
            except (KeyError, OSError) as error:
                raise PreconditionFailedError(str(error)) from error

        data.setValue('geometry/{0}/name'.format(geometry_id), name, 'name')
        try:
            transaction = session.state.commit(
                data, action='rename geometry', source=Source.GUI,
                target='geometry/{0}/name'.format(geometry_id),
                reason='{0} -> {1}'.format(previous, name))
        except Exception:
            if patch_uuid is not None:
                # Put the manifest back rather than leave the two stores
                # disagreeing, which is the fault this operation exists to end.
                try:
                    editor.rename(patch_uuid, previous)
                except (PatchError, KeyError, OSError):  # pragma: no cover
                    pass
            raise

        if patch_uuid is not None:
            self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, action=action,
            before=previous, after=name)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'),
            payload={'geometry_id': geometry_id, 'name': name,
                     'previous_name': previous,
                     'patch_renamed': patch_uuid is not None,
                     'transaction': transaction.to_dict(),
                     'patches': self._patch_rows(editor)})

    @staticmethod
    def _patch_rows(editor) -> list:
        """The manifest rows, or none when nothing has been imported yet."""
        from foammesh.core.geometry.patches.ops import PatchError

        try:
            return editor.rows()
        except (PatchError, KeyError, OSError, ValueError):
            return []

    def _patch_uuid_named(self, editor, name: str):
        """The patch the database row is talking about, if there is one."""
        for row in self._patch_rows(editor):
            if str(row.get('name')) == str(name):
                return row.get('patch_uuid')
        return None

    def _patch_uuid_for(self, editor, element, previous: str, data=None):
        """The manifest row one tree row stands for.

        The stamped uuid first, because it is the only handle that means the
        same thing in both stores. DP-383: the name is not a handle. Every
        STEP body is faced ``face0..faceN`` from zero, so a second CAD file
        collides with the first, and the tree -- which has to show unique
        names -- quietly records the second one as ``face01`` while the
        manifest keeps ``face0``. Matching by name then found nothing, the
        rename wrote the tree alone, and the mesh shipped the boundary under a
        name the user had already replaced and could no longer see anywhere.

        Then the name, scoped to the artifact the row belongs to, for a row
        stamped before the uuid was carried; then the name alone, for a
        project saved before either field existed. Both fall back to what this
        did before rather than refusing a rename that used to work.
        """
        rows = self._patch_rows(editor)
        stamped = str(element.value('patchUuid') or '')
        if stamped:
            for row in rows:
                if str(row.get('patch_uuid')) == stamped:
                    return row.get('patch_uuid')
        geometry_id = str(element.value('geometryId') or '')
        if geometry_id:
            owned = [row for row in rows
                     if str(row.get('geometry_id') or '') == geometry_id]
            if owned:
                for row in owned:
                    if str(row.get('name')) == str(previous):
                        return row.get('patch_uuid')
                # DP-636. One boundary in the artifact and one surface row
                # standing for it: that pairing is not a guess. A single-solid
                # STL's row was stamped with the artifact but never with the
                # uuid, and the tree names it `<volume>_surface` where the
                # manifest keeps the solid's own name, so the name pass above
                # misses it and the rename used to write the tree alone.
                if len(owned) == 1 and data is not None and len(data.getKeys(
                        'geometry', lambda _key, row: (
                            str(row.get('geometryId') or '') == geometry_id
                            and str(row.get('gType')) == 'surface'))) == 1:
                    return owned[0].get('patch_uuid')
                # The artifact is known and none of its boundaries answers to
                # this name. Falling through to the unscoped search from here
                # would rename another file's boundary, which is the whole of
                # the fault above with the blame moved.
                return None
        return self._patch_uuid_named(editor, previous)

    async def _geometry_combine(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        geometry_ids = command.parameters.get('geometry_ids')
        if not isinstance(geometry_ids, list):
            raise ValidationFailedError('geometry_ids must be a list')
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            # Plan 35 CR7: the combined revision is re-diagnosed, and the
            # shell-pair intersection check waits on a Python child per pair
            # (0.4-0.7 s measured) -- on a worker thread, not the owner loop.
            combined = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).combine,
                geometry_ids, name=command.parameters.get('name'),
                replace_sources=bool(command.parameters.get('replace_sources', False)))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=combined['geometry_id'], combined_from=geometry_ids)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=combined)

    async def _geometry_transform(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        geometry_id = command.parameters.get('geometry_id')
        if not geometry_id:
            raise ValidationFailedError('geometry_id is required')
        operations = command.parameters.get('operations')
        if operations is None:
            kind = command.parameters.get('kind')
            values = command.parameters.get('values')
            operations = [{'kind': kind, 'values': values}]
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            # Plan 35 CR7: as combine -- the re-diagnosis runs off the loop.
            transformed = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).transform,
                geometry_id, operations)
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (OSError, TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, artifact=transformed['artifact'])
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=transformed)

    def _geometry_repair(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        operation = command.parameters.get('repair')
        if not operation:
            raise ValidationFailedError('a repair operation is required')
        geometry_id = command.parameters.get('geometry_id')
        if not geometry_id:
            raise PreconditionFailedError('geometry_id is required for surface repair')
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            repaired = GeometryArtifactStore(session.case_path).repair(
                geometry_id, operation,
                hole_size=float(command.parameters.get('hole_size', 1e6)),
                flip_normals=bool(command.parameters.get('flip_normals', False)))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': geometry_id}) from error
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        self._invalidate_geometry_preparation(session)
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, artifact=repaired['artifact'])
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=repaired)

    def _geometry_repair_suggest(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.repair_plan import suggest
        from foammesh.core.geometry.store import is_cad_entry
        store = GeometryArtifactStore(session.case_path)
        geometry_id = command.parameters.get('geometry_id')
        report = store.readiness_report(
            geometry_id,
            target_cell_size=self._readiness_target_cell_size(session),
            engine=self._readiness_engine(session))
        selected = report.get('geometries', ())
        inferred_route = ('cad' if len(selected) == 1 and is_cad_entry(selected[0])
                          else 'tessellated')
        plan = suggest(report, str(command.parameters.get('route', inferred_route)))
        return self._read_result(session, command, plan)

    async def _geometry_repair_preview(self, session: CaseSession, command: Command) -> OperationResult:
        import asyncio
        from foammesh.core.geometry import GeometryArtifactStore
        plan = command.parameters.get('plan') or command.parameters
        if not isinstance(plan, dict):
            raise ValidationFailedError('repair plan must be an object')
        loop = asyncio.get_running_loop()
        def progress(stage, fraction):
            loop.call_soon_threadsafe(
                lambda: session.state.bus.publish(
                    Event.GEOMETRY_PREPARE_PROGRESS,
                    stage=stage, fraction=float(fraction), operation=command.operation))
        token = self._start_geometry_prepare(session.case_id)
        try:
            report = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).preview_repair_plan,
                plan, progress=progress, cancelled=token.is_set)
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except RuntimeError as error:
            raise CapabilityUnavailableError(
                str(error), details={'capability': 'pythonocc-core',
                                     'error': 'occt_unavailable'}) from error
        except ValueError as error:
            raise ValidationFailedError(str(error)) from error
        finally:
            self._finish_geometry_prepare(session.case_id, token)
        # DP-89. The store's convention is that a private key on a report
        # starts with an underscore; the CAD route returns six of them, two
        # of which are raw OCCT shapes. Stripping one by name shipped those
        # to the GUI, where json.dumps raised and killed the repair. Strip
        # the prefix, the way the store's own apply path does.
        return self._read_result(session, command, {
            key: value for key, value in report.items()
            if not key.startswith('_')})

    async def _geometry_repair_apply(self, session: CaseSession, command: Command) -> OperationResult:
        import asyncio
        session.require_writable()
        from foammesh.core.geometry import GeometryArtifactStore
        plan = command.parameters.get('plan') or command.parameters
        if not isinstance(plan, dict):
            raise ValidationFailedError('repair plan must be an object')
        loop = asyncio.get_running_loop()
        def progress(stage, fraction):
            loop.call_soon_threadsafe(
                lambda: session.state.bus.publish(
                    Event.GEOMETRY_PREPARE_PROGRESS,
                    stage=stage, fraction=float(fraction), operation=command.operation))
        token = self._start_geometry_prepare(session.case_id)
        try:
            repaired = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).apply_repair_plan,
                plan, progress=progress, cancelled=token.is_set)
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except RuntimeError as error:
            raise CapabilityUnavailableError(
                str(error), details={'capability': 'pythonocc-core',
                                     'error': 'occt_unavailable'}) from error
        except ValueError as error:
            raise ValidationFailedError(str(error)) from error
        finally:
            self._finish_geometry_prepare(session.case_id, token)
        self._invalidate_geometry_preparation(session)
        self._record_artifact_transaction(
            session, command, target=repaired['geometry_id'],
            reason=f"created geometry revision {repaired['revision']}")
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=repaired['geometry_id'], revision=repaired['revision'],
            artifact=repaired['artifact'])
        session.state.bus.publish(
            Event.GEOMETRY_REVISION_CREATED,
            geometry_id=repaired['geometry_id'], revision=repaired['revision'],
            kind=repaired.get('kind', 'repaired'))
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=repaired)

    def _geometry_repair_rollback(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        geometry_id = command.parameters.get('geometry_id')
        target = command.parameters.get('target_revision')
        if not geometry_id or target is None:
            raise ValidationFailedError('geometry_id and target_revision are required')
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            restored = GeometryArtifactStore(session.case_path).rollback(
                str(geometry_id), int(target))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (TypeError, ValueError) as error:
            if hasattr(error, 'code'):
                raise ValidationFailedError(str(error), details={
                    'error': error.code, **getattr(error, 'details', {})}) from error
            raise ValidationFailedError(str(error)) from error
        self._invalidate_geometry_preparation(session)
        self._record_artifact_transaction(
            session, command, target=str(geometry_id),
            reason=f'restored geometry revision {restored["revision"]}')
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, revision=restored['revision'],
            artifact=restored['artifact'])
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=restored)

    async def _geometry_wrap_preview(self, session: CaseSession, command: Command) -> OperationResult:
        import asyncio
        geometry_id = command.parameters.get('geometry_id')
        if not geometry_id:
            raise ValidationFailedError('geometry_id is required')
        from foammesh.core.geometry import GeometryArtifactStore
        session.state.bus.publish(
            Event.GEOMETRY_PREPARE_PROGRESS,
            stage='grid_sizing', fraction=0.0, operation=command.operation)
        token = self._start_geometry_prepare(session.case_id)
        try:
            preview = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).preview_wrap,
                str(geometry_id), cancelled=token.is_set, **{
                    key: value for key, value in command.parameters.items()
                    if key != 'geometry_id'})
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (TypeError, ValueError) as error:
            if hasattr(error, 'code'):
                raise ValidationFailedError(str(error), details={
                    'error': error.code, **getattr(error, 'details', {})}) from error
            raise ValidationFailedError(str(error)) from error
        finally:
            self._finish_geometry_prepare(session.case_id, token)
        session.state.bus.publish(
            Event.GEOMETRY_PREPARE_PROGRESS,
            stage='complete', fraction=1.0, operation=command.operation)
        return self._read_result(session, command, preview)

    def _geometry_wrap_estimate(self, session: CaseSession,
                                command: Command) -> OperationResult:
        geometry_id = command.parameters.get('geometry_id')
        if not geometry_id:
            raise ValidationFailedError('geometry_id is required')
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            estimate = GeometryArtifactStore(session.case_path).estimate_wrap(
                str(geometry_id), int(command.parameters.get('resolution', 64)),
                command.parameters.get('smallest_feature'))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, estimate)

    async def _geometry_wrap_apply(self, session: CaseSession, command: Command) -> OperationResult:
        import asyncio
        session.require_writable()
        geometry_id = command.parameters.get('geometry_id')
        if not geometry_id:
            raise ValidationFailedError('geometry_id is required')
        parameters = {key: value for key, value in command.parameters.items()
                      if key != 'geometry_id'}
        from foammesh.core.geometry import GeometryArtifactStore
        session.state.bus.publish(
            Event.GEOMETRY_PREPARE_PROGRESS,
            stage='grid_sizing', fraction=0.0, operation=command.operation)
        token = self._start_geometry_prepare(session.case_id)
        try:
            wrapped = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).apply_wrap,
                str(geometry_id), cancelled=token.is_set, **parameters)
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        except (TypeError, ValueError) as error:
            if hasattr(error, 'code'):
                raise ValidationFailedError(str(error), details={
                    'error': error.code, **getattr(error, 'details', {})}) from error
            raise ValidationFailedError(str(error)) from error
        finally:
            self._finish_geometry_prepare(session.case_id, token)
        self._invalidate_geometry_preparation(session)
        self._record_artifact_transaction(
            session, command, target=str(geometry_id),
            reason=f"created wrapped revision {wrapped['revision']}")
        session.state.bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, operation=command.operation,
            geometry_id=geometry_id, revision=wrapped['revision'],
            artifact=wrapped['artifact'])
        session.state.bus.publish(
            Event.GEOMETRY_REVISION_CREATED,
            geometry_id=geometry_id, revision=wrapped['revision'], kind='wrapped')
        session.state.bus.publish(
            Event.GEOMETRY_PREPARE_PROGRESS,
            stage='complete', fraction=1.0, operation=command.operation)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=wrapped)

    # -- Slice 4: workflow ------------------------------------------------- #

    def _workflow_status(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.case.workflow import WorkflowTransitionService
        service = WorkflowTransitionService()
        try:
            can_return = service.can_return_to_external(session.case_path)
        except (FileNotFoundError, ValueError, KeyError):
            can_return = False  # no external-mesh provenance recorded yet
        from foammesh.core.geometry import GeometryArtifactStore
        preparation = session.configuration().get('geometryPreparation', {})
        decision = preparation.get('decision', 'undecided')
        if hasattr(decision, 'value'):
            decision = decision.value
        fingerprint = GeometryArtifactStore(session.case_path).geometry_fingerprint()
        preparation_current = (
            decision != 'undecided'
            and preparation.get('geometryFingerprint') == fingerprint)
        return self._read_result(session, command, {
            'can_return_to_external': can_return,
            'geometry_preparation': {
                'decision': decision,
                'geometry_fingerprint': fingerprint,
                'decision_fingerprint': preparation.get('geometryFingerprint'),
                'current': preparation_current,
            },
        })

    def _workflow_start_authored(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        from foammesh.core.case.workflow import WorkflowTransitionService
        outcome = WorkflowTransitionService().start_authored(session.case_path)
        return self._artifact_result(session, command, _to_payload(outcome),
                                     invalidates=('mesh',))

    def _workflow_return_to_external(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        from foammesh.core.case.workflow import WorkflowTransitionService
        service = WorkflowTransitionService()
        try:
            can_return = service.can_return_to_external(session.case_path)
        except (FileNotFoundError, ValueError, KeyError):
            can_return = False
        if not can_return:
            raise PreconditionFailedError('case cannot return to external workflow')
        outcome = service.return_to_external(session.case_path)
        return self._artifact_result(session, command, _to_payload(outcome), invalidates=('mesh',))

    async def _workflow_generate_dictionaries(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        values = command.parameters.get('bbox')
        if values is None:
            # DP-576 (field audit 0924 snappy-front D3). The recipes and any
            # caller that has already saved its standoff ask for the
            # dictionaries without a box, and were refused. The box is the
            # geometry's own extent (the builder adds the saved standoff, or
            # uses the named bounding hex), so derive it rather than refuse.
            values = self._generation_bounds(session)
            if values is None:
                raise ValidationFailedError(
                    'bbox was not given and there is no imported geometry or '
                    'bounding hex to take it from')
        if not isinstance(values, (list, tuple)) or len(values) != 6:
            raise ValidationFailedError(
                'bbox must contain xmin, xmax, ymin, ymax, zmin, zmax')
        try:
            coordinates = tuple(float(value) for value in values)
        except (TypeError, ValueError) as error:
            raise ValidationFailedError('bbox coordinates must be finite numbers') from error
        if (not all(value == value and abs(value) != float('inf') for value in coordinates)
                or coordinates[0] >= coordinates[1]
                or coordinates[2] >= coordinates[3]
                or coordinates[4] >= coordinates[5]):
            raise ValidationFailedError('bbox minimums must be finite and less than maximums')

        from foammesh.core.case import record_artifact_event
        from foammesh.core.engine import configured_engine_id, resolve_engine
        from foammesh.core.geometry import (
            BBox, PreparedGeometryError, PreparedGeometryStore,
        )
        import asyncio
        bbox = BBox(*coordinates)
        if configured_engine_id(session.state.db) == 'unselected':
            raise PreconditionFailedError(
                'Choose a meshing method before generating dictionaries. '
                'Open the Meshing method step and apply one.')
        engine = resolve_engine(session.state.db)
        # F-12. Both engines, one answer. This used to run for snappy only,
        # so an imported case was ready to generate dictionaries in one
        # engine and refused in the other for want of a step the user was
        # never told to take.
        try:
            prepared = await asyncio.to_thread(
                _ensure_prepared_geometry, session,
                producer='workflow.generate_dictionaries',
                require_domain=not _meshes_a_section(session.state.db))
        except (PreparedGeometryError, TypeError, ValueError) as error:
            raise ValidationFailedError(
                f'geometry preparation failed: {error}') from error
        try:
            manifest = await asyncio.to_thread(
                engine.generate_config, session.state.db, bbox, session.case_path,
                prepared)
        except (KeyError, OSError, TypeError, ValueError) as error:
            # R170. The case builder refuses to write a dictionary it cannot
            # bind, and said so with a plain ValueError. The GUI catches
            # FacadeError, so nothing caught this: the "Base Grid Generating"
            # dialog spun on with an empty console and a Cancel that greyed
            # itself out without closing, and the application had to be
            # killed. A refusal is a validation failure and has to arrive as
            # one, carrying its reason.
            raise ValidationFailedError(
                'dictionaries could not be generated: {0}'.format(error),
                details={'engine': engine.engine_id}) from error
        payload = manifest.to_dict()
        entry = await asyncio.to_thread(
            record_artifact_event, session.case_path, operation=command.operation,
            status='done', details={'manifest': payload})
        payload['history_entry_id'] = entry.entry_id
        session.state.bus.publish(
            Event.ARTIFACT_DICTIONARIES_CHANGED, operation=command.operation,
            manifest=payload, history_entry_id=entry.entry_id)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('mesh', 'quality'), payload=payload)

    def _workflow_effective_dictionaries(
            self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.openfoam.case_builder import CaseBuilder
        try:
            payload = CaseBuilder.effective_dictionaries(
                session.case_path, session.state.db)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise PreconditionFailedError(str(error)) from error
        return self._read_result(session, command, payload)

    def _workflow_reset_stage(self, session: CaseSession,
                              command: Command) -> OperationResult:
        """Undo a mesh stage so it can be run again (R85).

        Every snappy stage writes the same ``constant/polyMesh``, so "a mesh
        exists" is all the case directory remembers -- it cannot say which
        stage put it there. That is why the Base Grid page, which asked
        exactly that question, stopped offering Generate after the first
        blockMesh and never offered it again. Resetting a stage drops its
        recorded run and those of every stage after it, and puts the mesh back
        to what that stage started from: for ``blockMesh`` that is no mesh at
        all, and for a later stage it is the snapshot the engine kept of its
        own input. The stages after it are gone either way, which is the
        honest outcome -- their results were built on the mesh being discarded.
        """
        session.require_writable()
        from foammesh.core.engine import resolve_engine

        stage = str(command.parameters.get('stage') or '').strip()
        if tuple(session.jobs.active_job_ids):
            raise PreconditionFailedError(
                'a case job is running; wait for it to finish before '
                'resetting a meshing stage')

        engine = resolve_engine(session.state.db)
        # Plan 30 F-15. `reset` is the name every engine answers to now: a
        # snappy stage and a Gmsh run are both "the thing this engine can put
        # back", and the facade only knows that some engine can do it.
        reset = getattr(engine, 'reset', None)
        if reset is None:
            raise PreconditionFailedError(
                'the configured meshing method cannot reset a stage')
        case_root = (session.case_path if (session.case_path / 'constant').is_dir()
                     else session.case_path / 'case')
        try:
            payload = reset(case_root, stage)
        except ValueError as error:
            raise ValidationFailedError(
                str(error), details={'stage': stage}) from error
        # DP-1230. The reset put another mesh in the case root, so processor
        # cases split from the one it replaced -- and a result still waiting
        # in them to be gathered -- are no stage's input any more. Left, the
        # next parallel run meshed them, and the next reader gathered them
        # back over the mesh the reset had just restored.
        for root in {Path(session.case_path), Path(case_root)}:
            if (any(root.glob('processor[0-9]*'))
                    or (root / self.PENDING_GATHER).exists()):
                self._discard_decomposition(root)
                (root / self.PENDING_GATHER).unlink(missing_ok=True)

        return self._read_result(session, command, payload)

    async def _workflow_run_stage(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        stage = command.parameters.get('stage')
        from foammesh.core.engine import configured_engine_id, resolve_engine
        if configured_engine_id(session.state.db) == 'unselected':
            raise PreconditionFailedError(
                'Choose a meshing method before running a meshing stage. '
                'Open the Meshing method step and apply one.')
        engine = resolve_engine(session.state.db)
        try:
            definition = engine.validate(stage)
        except ValueError as error:
            raise ValidationFailedError(
                'unknown meshing stage', details={'stage': stage}) from error
        # DP-354. Before anything on this path asks the runtime a question.
        await self._warm_utility_probes()
        await self._warm_meshing_host()
        feature_payload = None
        seed_warnings: list[str] = []
        if definition.stage in {'castellation', 'snappyHexMesh'}:
            seed_warnings = await self._snappy_seed_gate(
                session, allow_region_clash=self._allows_region_clash(command))
            # Snappy consumes ``.eMesh`` edges that only ``surfaceFeatures``
            # produces. Extract them on demand so a single staged stage is
            # runnable on a fresh case and after a batch pipeline run alike.
            feature_payload = await self._ensure_surface_features(
                session, command, engine)
            self._require_current_surface_features(session)
        # Plan 37 UF5. An edited stage never runs on its own previous output:
        # the stage it reads from is put back first, from its kept snapshot,
        # or regenerated from the nearest one that is kept.
        replay = await self._replay_stage_input(
            session, command, engine, definition)
        execution, payload = await self._run_stage_definition(
            session, command, engine, definition)
        if replay:
            payload['replay'] = replay
        if feature_payload is not None:
            payload['surface_features'] = feature_payload
        payload.update(self._region_warnings_payload(seed_warnings))
        if execution.succeeded:
            # The implicit surfaceFeatures ran and succeeded too, so the tree
            # records it before the stage that consumed its edges.
            if feature_payload is not None:
                self._record_stage_run_success(
                    session, command, 'snappy.surface_features')
            payload['task_state'] = self._record_stage_run_success(
                session, command, getattr(definition, 'task_id', None),
                warning=bool(execution.warnings),
                reasons=tuple(execution.warnings))
            if (getattr(definition, 'mutation', True)
                    and payload.get('left_decomposed')
                    and self._keeps_stage_snapshots(engine, definition.stage)):
                # DP-1201. A parallel stage was never kept: its result stayed
                # in the processor cases, so a later replay found no snapshot
                # and regenerated it. It is gathered now -- the processor
                # cases stay, so the next parallel stage still runs off them
                # -- and the gather keeps it under its own name.
                gather = await self._gather_stage_result(
                    session, command, definition.stage)
                if gather.get('gathered'):
                    payload['left_decomposed'] = False
                    payload['reconstructed'] = gather.get('execution')
                    if gather.get('stage_snapshot') is not None:
                        payload['stage_snapshot'] = gather['stage_snapshot']
                else:
                    payload['stage_snapshot'] = gather.get('stage_snapshot')
            if (getattr(definition, 'mutation', True)
                    and not payload.get('left_decomposed')):
                # The tree route runs snappy one stage at a time and used to
                # leave no trace beside the mesh: no snap checkpoint (so the
                # fidelity-after-snap check had nothing to measure), no
                # patch identity (so fidelity and resolution had no
                # sections) and no evidence (so every summary was stale).
                # A stage left decomposed binds when it is reconstructed.
                payload.update(self._trace_mesh_state(
                    session, definition.stage, execution))
                if payload.get('stage_snapshot') is None:
                    payload['stage_snapshot'] = await self._capture_stage_snapshot(
                        session, engine, definition.stage, execution)
        else:
            # R92. The task state was recorded only on success, so a stage
            # that failed left the tree holding whatever it said before.
            # MEASURED after the R84 core dump: the Castellation row still
            # read `checkmark` over a case whose mesh had been destroyed by
            # the run that had just been reported as failed. The other
            # staleness defects are about a state that is merely out of date;
            # this one was wrong in the user's favour, immediately after the
            # app had told them something went wrong.
            payload['task_state'] = self._record_stage_run_failure(
                session, command, getattr(definition, 'task_id', None))
            job = payload.get('job') or {}
            if str(job.get('status') or '') != 'cancelled':
                from foammesh.core.jobs.retry import attempt_of
                payload.update(self._with_retry_offer(
                    self._stage_failure(definition.stage, job), job,
                    ranks=payload.get('ranks') or 1,
                    attempt=attempt_of(command.parameters)))
            if payload.get('discard_note'):
                # Plan 35 D8: the reason says what was cleaned up.
                reason = str(payload.get('reason') or f'{definition.stage} failed')
                payload['reason'] = f"{reason} ({payload['discard_note']})"
        return OperationResult(
            'accepted' if execution.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=(tuple(seed_warnings) + tuple(execution.warnings or ())
                      + tuple(payload.get('launch_warnings') or ())),
            payload=payload)

    # -- Plan 37 UF5: stage snapshots and predecessor replay --------------- #

    @staticmethod
    def _keeps_stage_snapshots(engine, stage: str) -> bool:
        """Whether *stage* is one of *engine*'s own workflow stages that
        are kept and replayed -- asked of the engine's workflow, so an engine
        whose run folder already keeps its result (Gmsh) is left alone."""
        from foammesh.core.jobs import stage_snapshots as snapshots

        task_id = snapshots.STAGE_TASKS.get(stage)
        if task_id is None:
            return False
        try:
            engine.workflow_descriptor().task(task_id)
        except (AttributeError, LookupError, TypeError, ValueError):
            return False
        return True

    async def _capture_stage_snapshot(self, session: CaseSession, engine,
                                      stage: str, execution=None) -> dict | None:
        """Keep what *stage* just published, off the owner loop.

        Nonessential: a snapshot that cannot be taken (admission, a cancel,
        a disk error) is reported in the payload and never fails the stage.
        """
        from foammesh.core.jobs import stage_snapshots as snapshots

        if not self._keeps_stage_snapshots(engine, stage):
            return None
        if self._gather_pending(session.case_path):
            # Plan 37 UF20. The stage's result is still in the processor
            # cases; the case root is the mesh it started from, and keeping
            # that under this stage's name makes a later replay start from it.
            return {'captured': False, 'stage': stage, 'reason': 'decomposed',
                    'message': f'{stage} is still decomposed; the case root '
                               'is not its result, so it was not kept',
                    'consequence': 'a re-run that starts from it regenerates it'}
        job = getattr(execution, 'job', None)
        argv = tuple(getattr(job, 'argv', ()) or ())
        runtime = {'utility': 'blockMesh' if stage == 'blockMesh'
                   else 'snappyHexMesh',
                   'executable': str(argv[0]) if argv else ''}
        token = snapshots.cancel_token(session.case_path)
        try:
            captured = await asyncio.to_thread(
                snapshots.capture, session.case_path, stage,
                engine_id=getattr(engine, 'engine_id', ''), runtime=runtime,
                cancel=token)
        except snapshots.SnapshotError as error:
            return {'captured': False, 'stage': stage, 'reason': error.code,
                    'message': str(error)}
        except OSError as error:
            logger.warning('stage snapshot of %s not kept', stage, exc_info=True)
            return {'captured': False, 'stage': stage, 'reason': 'io_error',
                    'message': str(error)}
        finally:
            snapshots.release_token(session.case_path, token)
        return {key: captured.get(key) for key in (
            'captured', 'skipped', 'stage', 'revision', 'path', 'bytes',
            'digest', 'copy_seconds', 'verify_seconds', 'reason',
            'consequence') if key in captured}

    async def _gather_stage_result(self, session: CaseSession,
                                   command: Command, stage: str) -> dict:
        """Gather a parallel *stage*'s result into the case root (DP-1201).

        ``{'gathered': True, 'execution', 'stage_snapshot'}`` once the case
        root holds it, or ``{'gathered': False, 'stage_snapshot': why}`` when
        the gather failed: the stage stays decomposed and is not kept, and
        the next reader gathers it as before.
        """
        try:
            gathered = await self._ensure_reconstructed(session, command)
        except PreconditionFailedError as error:
            return {'gathered': False, 'stage_snapshot': {
                'captured': False, 'stage': stage,
                'reason': 'reconstruct_failed', 'message': str(error),
                'consequence': 'a re-run that starts from it regenerates it'}}
        gathered = dict(gathered or {})
        return {'gathered': not self._gather_pending(session.case_path),
                'execution': {key: value for key, value in gathered.items()
                              if key != 'stage_snapshot'},
                'stage_snapshot': gathered.get('stage_snapshot')}

    async def _keep_gathered_stage(self, session: CaseSession,
                                   stage: str) -> dict | None:
        """Keep a stage the gather has just put in the case root (DP-1201).

        A lazy gather (an export, a serial stage, ``mesh.reconstruct``, an
        unlock) is the first moment a parallel stage's result is the case
        root, so it is kept then, under that stage's name. Never fails the
        gather.
        """
        from foammesh.core.engine import resolve_engine

        try:
            engine = resolve_engine(session.state.db)
        except Exception:  # noqa: BLE001 - no engine, nothing to keep
            return None
        if not self._keeps_stage_snapshots(engine, stage):
            return None
        return await self._capture_stage_snapshot(session, engine, stage)

    def _stage_validity(self, session: CaseSession, command: Command,
                        stage: str) -> tuple[list | None, bool]:
        """``(stages whose published result stands, live mesh is *stage*'s
        output or later)``. Unknown answers are ``(None, False)``: a case the
        graph cannot describe runs as it always did."""
        from foammesh.core.jobs import stage_snapshots as snapshots
        from foammesh.core.workflow.task_state_store import (
            _PUBLISHED_STATES, TaskStateError, mesh_identity,
        )

        try:
            _engine_id, store = self._task_state_store(session, command)
            graph = store.load()
            valid = [name for name, task in snapshots.STAGE_TASKS.items()
                     if graph.state(task) in _PUBLISHED_STATES]
            live = mesh_identity(session.case_path)
            rank = snapshots._stage_rank(stage)
            records = store.publications().get('publications') or {}
            # Plan 37 UF20 follow-up. Which stage wrote the live mesh is read
            # off the publication order, not off the identity each record
            # carries: a parallel stage is recorded while its result is still
            # in the processor cases, so its `mesh_revision` names the mesh it
            # started from. MEASURED on the SU2 walk: Layers was recorded over
            # the snapped root and gathered afterwards, so after Snap was
            # unlocked no record matched the live (layered) mesh, the Snap
            # re-run skipped the castellation replay the unlock box promised
            # and snapped the layered mesh; and the Layers press then matched
            # Snap's record against the root it started from, regenerated
            # Castellation and Snap, staled the snap fidelity gate and left
            # Layers unpublished. The newest publication is the run that wrote
            # the mesh; a run publishes the stage it performed under the same
            # revision as the stages it implies, so its latest stage is the
            # one that ran.
            ranked = {task: snapshots._stage_rank(name)
                      for name, task in snapshots.STAGE_TASKS.items()}
            latest, newest = 0, -1
            for task, record in records.items():
                if task not in ranked or not isinstance(record, dict):
                    continue
                try:
                    revision = int(record.get('revision') or 0)
                except (TypeError, ValueError):
                    continue
                if revision > latest:
                    latest, newest = revision, ranked[task]
                elif revision == latest:
                    newest = max(newest, ranked[task])
            later_output = bool(latest) and newest >= rank and newest > 0
            active = snapshots.active(session.case_path) or {}
            if (live and active.get('mesh_identity') == live
                    and active.get('stage') not in (None, 'blockMesh')
                    and snapshots._stage_rank(active['stage']) >= rank):
                later_output = True
            return valid, later_output
        except (FacadeError, TaskStateError, LookupError, OSError, ValueError):
            return None, False

    @staticmethod
    def _drop_stage_inputs(case_path, stage: str) -> None:
        """Forget the engine's input copies from *stage* on, so the stage
        copies the input replay just put in place instead of an older one."""
        root = Path(case_path) / 'foammesh' / 'mesh-snapshots'
        key = 'castellation' if stage == 'snappyHexMesh' else stage
        order = ('castellation', 'snap', 'layers')
        if key not in order:
            return
        for later in order[order.index(key):]:
            shutil.rmtree(root / later, ignore_errors=True)

    async def _restore_stage_snapshot(self, session: CaseSession,
                                      source: dict) -> None:
        from foammesh.core.jobs import stage_snapshots as snapshots

        token = snapshots.cancel_token(session.case_path)
        try:
            await asyncio.to_thread(
                snapshots.restore, session.case_path, source['stage'],
                source['revision'], cancel=token)
        except snapshots.DiskAdmissionError as error:
            raise PreconditionFailedError(str(error), details=dict(
                error.details, error='insufficient_disk')) from error
        finally:
            snapshots.release_token(session.case_path, token)
        # Plan 37 UF20. The case-root mesh was just replaced, so processor
        # cases split from the mesh it replaced are no longer this stage's
        # input. `_ensure_decomposed` reuses processor cases whose count
        # matches the ranks, and a parallel re-run of an unlocked
        # Castellation meshed the old *snapped* mesh from them (MEASURED:
        # "Initial mesh : cells:3230", not the base grid's 1000), so a raised
        # refinement level refined nothing. A gather still pending for them
        # would write that old mesh back over the restored root.
        self._discard_decomposition(session.case_path)
        (session.case_path / self.PENDING_GATHER).unlink(missing_ok=True)

    async def _replay_stage_input(self, session: CaseSession, command: Command,
                                  engine, definition) -> dict | None:
        """Put the mesh *definition*'s stage reads from in place (UF5).

        Snap starts from Castellation, Layers from Snap, Castellation from
        the base grid. The kept snapshot of that stage is restored when the
        live mesh is not already a copy of it; when it is missing, the
        stages in between are regenerated from the nearest kept one. With no
        snapshots and no sign that the live mesh is this stage's own output
        (a first run, or a case older than the snapshots), nothing changes.

        Launching is refused, with a typed reason, when the stage's input
        copy could not be kept.
        """
        from foammesh.core.jobs import stage_snapshots as snapshots

        stage = definition.stage
        if (stage not in snapshots.PREDECESSOR
                or not self._keeps_stage_snapshots(engine, stage)):
            return None
        case = session.case_path
        live_bytes = await asyncio.to_thread(
            snapshots._tree_bytes, Path(case) / 'constant' / 'polyMesh')
        try:
            snapshots.require_admission(
                case, live_bytes, what=f'the mesh {stage} starts from')
        except snapshots.DiskAdmissionError as error:
            raise PreconditionFailedError(str(error), details=dict(
                error.details, error='insufficient_disk', stage=stage)) from error
        valid, later_output = self._stage_validity(session, command, stage)
        plan = snapshots.plan_replay(case, stage, valid_stages=valid)
        predecessor = plan['input']
        source = plan['restore']
        summary = {'stage': stage, 'input': predecessor, 'from': source,
                   'regenerated': []}
        if source is not None and source['stage'] == predecessor:
            if not plan['live_is_input']:
                await self._restore_stage_snapshot(session, source)
                summary['restored'] = True
            self._drop_stage_inputs(case, stage)
            return summary
        if not later_output:
            return None
        if source is not None:
            await self._restore_stage_snapshot(session, source)
            summary['restored'] = True
        for step in plan['regenerate']:
            step_definition = engine.validate(step)
            self._drop_stage_inputs(case, step)
            if step != 'blockMesh':
                await self._ensure_surface_features(session, command, engine)
            execution, step_payload = await self._run_stage_definition(
                session, command, engine, step_definition)
            if not execution.succeeded:
                raise PreconditionFailedError(
                    f'{stage} was not run: regenerating {step}, which it '
                    'starts from, failed',
                    details={'error': 'replay_failed', 'stage': stage,
                             'replayed': step,
                             'job': step_payload.get('job')})
            self._record_stage_run_success(
                session, command, getattr(step_definition, 'task_id', None))
            # Plan 37 UF20. A parallel step leaves its result in the processor
            # cases and the case root still holding the mesh it started from;
            # the snapshot below copies the case root. Gathered first, or the
            # step is kept as its own input (MEASURED: the kept castellation
            # held the base grid's 1000 cells, and an unlocked Snap re-run
            # "snapped" that grid and passed Quality on it).
            kept = None
            if self._gather_pending(session.case_path):
                kept = (await self._ensure_reconstructed(session, command)
                        or {}).get('stage_snapshot')
            self._trace_mesh_state(session, step, execution)
            summary['regenerated'].append({
                'stage': step,
                'snapshot': kept if kept is not None else
                await self._capture_stage_snapshot(
                    session, engine, step, execution)})
        self._drop_stage_inputs(case, stage)
        return summary

    @staticmethod
    def _stage_failure(stage: str, job: Mapping) -> dict:
        """Why a meshing stage failed, and where the rest of it is written.

        DP-506 (MA-02). A failed stage carried only the job's own words,
        ``process exited with code 1``, and no ``reason`` at all, so the
        window fell back to "The stage could not run." MEASURED on S1_box_cavity:
        castellation died of ``FOAM FATAL ERROR: Unknown region name
        box_with_cavity ... Valid region names are 1(...)`` -- the one sentence
        that says what to change -- and it reached nothing but the log file.
        The cause is read out of that log here, once, so every surface that
        reports the failure says the same thing.
        """
        from foammesh.core.run_result import read_failure_cause

        log = str(job.get('log_path') or '')
        cause, details = read_failure_cause(log)
        # Plan 35 CR5 step 3. How the process ended, decoded: a signal, a
        # lost WSL connection or an earlier run still unresolved says more
        # than `process exited with code 139`.
        decoded = job.get('exit') if isinstance(job.get('exit'), Mapping) else {}
        kind = str(decoded.get('kind') or '')
        exit_reason = str(decoded.get('reason') or '').strip()
        if kind in {'segfault', 'fpe', 'oom', 'signal', 'transport',
                    'recovery_pending'} and exit_reason:
            cause = exit_reason if not cause else f'{exit_reason}: {cause}'
        elif kind == 'abort' and exit_reason:
            cause = cause or exit_reason
        cause = cause or str(job.get('error') or '').strip()
        reason = f'{stage} failed: {cause}' if cause else f'{stage} failed'
        failure = {'reason': reason, 'cause': cause, 'details': details,
                   'log': log}
        if kind and kind not in {'ok', 'exit'}:
            failure.update({'exit_kind': kind,
                            'actions': list(decoded.get('actions') or ()),
                            'hint': str(decoded.get('hint') or '')})
            if decoded.get('signal'):
                failure['signal'] = decoded['signal']
        return failure

    @staticmethod
    def _with_retry_offer(failure: dict, job: Mapping, *, ranks: int = 1,
                          attempt: int = 0) -> dict:
        """Plan 35 CR6. *failure* with the retry the policy offers, if any.

        [Retry] for a lost transport or a memory kill, and [Retry with fewer
        cores] for the kill of a parallel run -- never for a FOAM FATAL ERROR,
        and never for a run that is already a retry.
        """
        from foammesh.core.jobs.retry import retry_offer
        decoded = job.get('exit') if isinstance(job.get('exit'), Mapping) else {}
        text = f"{failure.get('cause') or ''} {failure.get('details') or ''}"
        offer = retry_offer(decoded, ranks=ranks, attempt=attempt,
                            fatal='FOAM FATAL' in text)
        if offer is not None:
            failure = {**failure, 'retry': offer}
        return failure

    def _trace_mesh_state(self, session: CaseSession, stage: str,
                          execution=None) -> dict:
        """Checkpoint, identity sidecar and evidence for the mesh in the root."""
        traced: dict = {}
        checkpoint = ''
        if stage == 'snap':
            run_id = ''
            if execution is not None:
                run_id = str(execution.to_payload().get('job_id') or '')
            traced['checkpoint'] = self._save_snap_checkpoint(
                session, run_id=run_id)
            checkpoint = str(traced['checkpoint'].get('fingerprint') or '')
        if stage == 'blockMesh':
            # A base grid has no surface to join to; the sidecar written on
            # it would fabricate every prepared patch. Evidence is enough.
            traced['evidence'] = self._bind_evidence(session)
            return traced
        prepared = self._current_prepared(session)
        traced['patch_identity'] = self._write_snappy_patch_identity(
            session, prepared)
        traced['evidence'] = self._bind_evidence(
            session, checkpoint_fingerprint=checkpoint, prepared=prepared)
        return traced

    async def _ensure_surface_features(self, session: CaseSession,
                                       command: Command, engine):
        """Run ``surfaceFeatures`` when its artifacts are absent or stale.

        Returns the extraction payload when a run happened, otherwise ``None``.
        """
        try:
            self._require_current_surface_features(session)
        except PreconditionFailedError as error:
            if (error.details or {}).get('error') != 'surface_features_stale':
                raise
        else:
            tri_surface = session.case_path / 'constant' / 'triSurface'
            if any(tri_surface.glob('*.eMesh')):
                return None
        definition = engine.validate('surfaceFeatures')
        execution, payload = await self._run_stage_definition(
            session, command, engine, definition)
        if not execution.succeeded:
            raise PreconditionFailedError(
                'surface feature extraction failed; snappy cannot run without '
                'current feature edges',
                details={'error': 'surface_features_failed',
                         'stage': definition.stage,
                         'exit_code': payload.get('exit_code')})
        return payload

    @classmethod
    def _stage_ranks(cls, session: CaseSession, command: Command,
                     definition=None) -> int:
        """How many ranks this meshing stage should run on.

        The count is the one on ``2. Mesh setup > Meshing resources``,
        clamped by the machine. Stages used to ignore it entirely and always
        build a serial command line, so a case configured for sixteen cores
        still meshed on one. DP-691: the Parallel Environment dialog's
        ``local.cfg`` count is no longer a second input. DP-1231: Auto (no
        count asked) is the WSL host's cores less one, within its free RAM --
        see :meth:`_stage_cpu`.
        """
        return cls._stage_cpu(session, command, definition)[0]

    @classmethod
    def _stage_cpu(cls, session: CaseSession, command: Command,
                   definition=None):
        """``(ranks, CpuCount | None)`` for one snappy stage.

        Every snappy phase decomposes. Measured directly against OpenFOAM 13
        on a 1M-cell annulus, 16 ranks meshed in 34s against 139s serial and
        agreed to 0.03% -- so there is no phase that has to be held back, and
        no reason for a mode that decomposes only some of them.

        Plan 33 DP-X2 put the ceiling in this precedence and
        ``requested_cpu_count`` keeps the preview, the page and the launcher
        on one rule. DP-1231: that rule answered 0 for "nobody asked" and
        this read 0 as one rank, so an Auto case meshed serially on a
        sixteen-core machine. Auto now asks
        :func:`~foammesh.core.execution.resources.meshing_cpu_count` -- the
        same function the plan preview returns as ``auto`` -- and Snap and
        Layers keep the count Castellation ran on (DP-1232).
        """
        from foammesh.core.execution import (
            ResourceMode, ResourcePolicy, ResourceRequest, allocate_resources,
        )
        from foammesh.core.execution.resources import ResourceError

        policy_values = _resource_policy(session.configuration())
        if str(policy_values['mode'] or '').lower() == 'serial':
            return 1, None
        try:
            cpu, host = cls._snappy_cpu(
                session.case_path, policy_values,
                cores=int(command.parameters.get('cores') or 0),
                stage=getattr(definition, 'stage', None))
        except (TypeError, ValueError):
            return 1, None
        if cpu.count <= 1:
            return 1, cpu
        try:
            allocation = allocate_resources(
                ResourcePolicy(
                    ResourceMode(str(policy_values['mode'])),
                    policy_values['max_cpu_cores'],
                    policy_values['max_memory_bytes'],
                    policy_values['allow_distributed'], 'openfoam-mpi'),
                ResourceRequest(cpu.count, backend_id='openfoam-mpi'),
                host.resource_facts())
        except (ResourceError, ValueError, TypeError):
            return 1, cpu
        return max(1, int(allocation.effective_ranks)), cpu

    @classmethod
    def _snappy_cpu(cls, case_path: Path, policy_values: dict, *,
                    cores: int = 0, stage: str | None = None, host=None):
        """``(CpuCount, MeshingHost)``: what a snappy run asks for, and of
        which machine (DP-1231).

        The host is the last WSL reading (never probed here: this runs on
        the loop) narrowed to the Meshing-resources memory ceiling, and the
        cells are the largest mesh this case already holds, so Auto leaves
        RAM for the mesh it is about to grow.
        """
        from dataclasses import replace

        from foammesh.core.execution.resources import (
            meshing_cpu_count, meshing_host, recorded_auto_count,
        )

        host = host or meshing_host()
        ceiling = policy_values.get('max_memory_bytes')
        if ceiling and (host.memory_available_bytes is None
                        or int(ceiling) < host.memory_available_bytes):
            host = replace(host, memory_available_bytes=int(ceiling))
        cpu = meshing_cpu_count(
            policy_values, engine='snappy', requested=int(cores or 0),
            cells=cls._known_cells(case_path),
            recorded=recorded_auto_count(case_path, stage) if stage else 0,
            host=host)
        return cpu, host

    @classmethod
    def _known_cells(cls, case_path) -> int | None:
        """The largest cell count this case holds: the live mesh and every
        kept stage, read from the ``owner`` headers (DP-1231). ``None`` when
        nothing has been meshed yet."""
        from foammesh.core.jobs import stage_snapshots

        case_path = Path(case_path)
        meshes = [case_path / 'constant' / 'polyMesh']
        try:
            for stage, revision in stage_snapshots.resolve(case_path).items():
                meshes.append(stage_snapshots.stages_root(case_path)
                              / revision / stage / 'constant' / 'polyMesh')
        except (OSError, ValueError, KeyError, TypeError):
            pass
        counts = [count for count in map(cls._polymesh_cell_count, meshes)
                  if count]
        return max(counts) if counts else None

    async def _warm_meshing_host(self) -> None:
        """DP-1231. Read the WSL host's cores and free memory on a worker
        thread before a run asks for them. Skipped where the capability
        registry is a stand-in (no ``utility``): there is no runtime there
        to ask, and a test's count must not depend on this machine's WSL."""
        if not hasattr(self._capabilities_registry(), 'utility'):
            return
        from foammesh.core.execution.resources import warm_meshing_host
        try:
            await warm_meshing_host()
        except Exception:                                   # noqa: BLE001
            pass

    #: Written when a parallel stage leaves its result in the processor cases,
    #: removed once the case root has been gathered from them. An explicit
    #: marker rather than comparing timestamps: the case lives on a Windows
    #: mount written from WSL, where mtimes are too coarse to decide this, and
    #: an indecisive answer means reconstructing on every repaint.
    PENDING_GATHER = 'foammesh/pending-gather.json'

    @classmethod
    def _mark_pending_gather(cls, case_path: Path, stage: str, ranks: int) -> None:
        marker = case_path / cls.PENDING_GATHER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({'stage': stage, 'ranks': ranks}, indent=2),
            encoding='utf-8')

    @classmethod
    def _gather_pending(cls, case_path: Path) -> bool:
        return (case_path / cls.PENDING_GATHER).is_file()

    #: Where the case-root mesh waits while the processor cases are gathered
    #: over it. See `_ensure_reconstructed` for why it has to wait anywhere.
    PARKED_ROOT_MESH = 'constant/polyMesh.before-gather'

    @staticmethod
    def _polymesh_cell_count(poly_mesh: Path) -> int | None:
        """Cells in a written mesh, read from the header OpenFOAM writes.

        `owner` carries `note "nPoints: .. nCells: .. nFaces: .."`. Counting
        the entries instead would mean reading a file that is hundreds of
        megabytes on a real mesh to learn one number. `None` means the header
        did not say, and a caller that cannot read the count does not get to
        claim the gather was wrong.
        """
        owner = poly_mesh / 'owner'
        if not owner.is_file():
            return None
        try:
            with owner.open(encoding='utf-8', errors='replace') as handle:
                head = handle.read(4096)
        except OSError:
            return None
        found = re.search(r'nCells\s*:\s*(\d+)', head)
        return int(found.group(1)) if found else None

    @classmethod
    def _decomposed_cell_count(cls, case_path: Path) -> int | None:
        """Cells across the processor cases, or `None` if one will not say.

        Decomposition shares no cell between two ranks, so this is what a
        gathered root must hold.
        """
        total = 0
        seen = False
        for processor in sorted(case_path.glob('processor[0-9]*')):
            counted = cls._polymesh_cell_count(
                processor / 'constant' / 'polyMesh')
            if counted is None:
                return None
            total += counted
            seen = True
        return total if seen else None

    @staticmethod
    def _discard_decomposition(case_path: Path) -> None:
        """Remove processor cases whose mesh has been superseded."""
        from foammesh.support.utils import rmtree
        for processor in case_path.glob('processor[0-9]*'):
            rmtree(processor)
        # DP-1230. What they were split from goes with them.
        try:
            (Path(case_path) / DECOMPOSITION_RECORD).unlink(missing_ok=True)
        except OSError:
            pass

    #: DP-1230. Which case-root mesh the processor cases were split from.
    DECOMPOSITION_RECORD = DECOMPOSITION_RECORD

    @classmethod
    def _record_decomposition(cls, case_path: Path, ranks: int) -> None:
        """Note that the processor cases now hold the case-root mesh.

        Written after ``decomposePar`` and again after a gather: both leave
        the processor cases and the case root holding the same mesh, and the
        identity read here is the one a later reuse has to find again.
        """
        from foammesh.core.workflow.task_state_store import mesh_identity
        record = Path(case_path) / cls.DECOMPOSITION_RECORD
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({
                'root_identity': mesh_identity(case_path),
                'ranks': int(ranks)}, indent=2), encoding='utf-8')
        except OSError:
            record.unlink(missing_ok=True)

    @classmethod
    def _stale_decomposition(cls, case_path: Path, ranks: int,
                             stage: str | None = None) -> str | None:
        """Why the processor cases on disk cannot be *stage*'s input.

        ``None`` when they can. DP-1230: the count used to be the only test,
        so processor cases holding a mesh the case root no longer holds were
        reused whenever their number matched. Read off the sequence: a
        parallel Snap is gathered and kept (DP-1201) and its processor cases
        stay; Reset Snap puts the castellated mesh back in the case root; the
        replay finds the root already is Snap's input (UF20 early return) and
        changes nothing; `_ensure_decomposed` saw N processor cases for N
        ranks and Snap ran on the snapped mesh. The identity of the root they
        were split from is now kept beside them and has to match the live
        root.
        """
        from foammesh.core.jobs import stage_snapshots as snapshots
        from foammesh.core.workflow.task_state_store import mesh_identity
        case_path = Path(case_path)
        existing = tuple(case_path.glob('processor[0-9]*'))
        if not existing:
            return 'none'
        if len(existing) != int(ranks):
            return 'rank_count'
        pending = case_path / cls.PENDING_GATHER
        if pending.is_file():
            # A result not yet gathered is a later stage's input only.
            try:
                held = json.loads(pending.read_text(encoding='utf-8')).get(
                    'stage')
            except (OSError, ValueError, AttributeError):
                held = None
            if (not held or stage is None
                    or snapshots._stage_rank(str(stage))
                    <= snapshots._stage_rank(str(held))):
                return 'pending_result_superseded'
        try:
            record = json.loads((case_path / cls.DECOMPOSITION_RECORD)
                                .read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 'unrecorded'
        if not isinstance(record, dict):
            return 'unrecorded'
        live = mesh_identity(case_path)
        if not live or record.get('root_identity') != live:
            return 'root_changed'
        return None

    @classmethod
    def _pending_gather_ranks(cls, case_path: Path) -> int | None:
        """The rank count the pending gather has to read, or `None`."""
        try:
            ranks = int(json.loads(
                (case_path / cls.PENDING_GATHER).read_text(
                    encoding='utf-8')).get('ranks') or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            ranks = 0
        if ranks < 1:
            ranks = len(tuple(case_path.glob('processor[0-9]*')))
        return ranks or None

    def _align_gather_ranks(self, session: CaseSession) -> None:
        """DP-663. `reconstructPar` reads `numberOfSubdomains`, not the disk.

        The dictionaries are regenerated between snappy phases, and generation
        writes the serial default (CP-07 item 6). A gather reading that found
        one processor case of twelve and stopped on its processor patches, so
        the dictionary is put back to the ranks that split the mesh first.
        """
        from foammesh.openfoam import decomposition
        case_path = session.case_path
        ranks = self._pending_gather_ranks(case_path)
        if ranks is None or decomposition.written_ranks(case_path) == ranks:
            return
        try:
            settings = DecompositionSettings.read(session.state.db)
        except Exception:  # noqa: BLE001 - the method does not change a gather
            settings = None
        decomposition.write(case_path, ranks, settings)

    async def _ensure_reconstructed(self, session: CaseSession,
                                    command: Command) -> dict | None:
        """Rebuild the case-root mesh from the processor cases, if it is stale.

        Parallel stages leave their result decomposed and hand the next stage
        the processor cases directly -- reconstructing between every stage cost
        more than the meshing did. The case root is rebuilt here instead, at
        the moment something actually reads it.
        """
        if not self._gather_pending(session.case_path):
            return None
        from foammesh.support.utils import rmtree
        case_path = session.case_path
        root_mesh = case_path / 'constant' / 'polyMesh'
        parked = case_path / self.PARKED_ROOT_MESH
        # DP-70. `reconstructPar` gathers the *points* only -- and still exits
        # 0 -- when a complete mesh is already at the case root and the
        # processor cases still carry `cellProcAddressing`. OF13,
        # `domainDecomposition::readReconstruct`:
        #
        #     const bool load = faceIo.headerOk() && procAddrIo.headerOk();
        #     if (load) { readComplete(false); ... reconstructPoints(); }
        #     else      { ... reconstruct(); }
        #
        # Both files are present as a matter of course, because a full
        # reconstruct writes `cellProc` into the root and `cellProcAddressing`
        # back into every processor case. So the gather that follows one moves
        # the stale grid's vertices and leaves its cells alone. MEASURED in
        # the acceptance sweep as a finished snappy run whose root held
        # blockMesh's 14,450 cells at 0.0 non-orthogonality -- the best
        # quality verdict in the sweep, on a mesh that had never been snapped.
        #
        # The root mesh is therefore taken out of `reconstructPar`'s sight
        # rather than left where it decides on it. Parked, not deleted: if the
        # gather does not produce a mesh to replace it, it goes back.
        self._align_gather_ranks(session)
        if parked.exists():
            rmtree(parked)
        if root_mesh.is_dir():
            root_mesh.rename(parked)
        try:
            execution = await self._run_openfoam_utility(
                session, command, 'reconstructPar',
                ('-constant', '-case', str(case_path)),
                cwd=case_path, mutation=True)
            if not execution.succeeded:
                raise PreconditionFailedError(
                    'the parallel mesh could not be reconstructed into the '
                    'case root', details={'error': 'reconstruct_failed'})
            # And then say so out loud. A gather that returns 0 having done
            # nothing is what DP-70 was, and reading the two counts back is
            # the cheapest thing that can tell the difference.
            if not root_mesh.is_dir():
                raise PreconditionFailedError(
                    'the gather reported success and left no mesh in the '
                    'case root', details={'error': 'reconstruct_incomplete'})
            gathered = self._polymesh_cell_count(root_mesh)
            decomposed = self._decomposed_cell_count(case_path)
            if (gathered is not None and decomposed is not None
                    and gathered != decomposed):
                raise PreconditionFailedError(
                    'the gathered mesh does not hold the cells the processor '
                    'cases do, so the case root is not this stage result',
                    details={'error': 'reconstruct_incomplete',
                             'root_cells': gathered,
                             'processor_cells': decomposed})
        except BaseException:
            if parked.is_dir():
                if root_mesh.is_dir():
                    rmtree(root_mesh)
                parked.rename(root_mesh)
            raise
        if parked.exists():
            rmtree(parked)
        marker = case_path / self.PENDING_GATHER
        try:
            gathered_stage = json.loads(
                marker.read_text(encoding='utf-8')).get('stage') or 'parallel'
        except (OSError, ValueError, AttributeError):
            gathered_stage = 'parallel'
        marker.unlink(missing_ok=True)
        # DP-1230. The root now holds what the processor cases hold.
        self._record_decomposition(
            case_path, len(tuple(case_path.glob('processor[0-9]*'))))
        # Plan 37 UF20. The parallel stage recorded the mesh as the
        # workflow's own while the root still held the mesh before it; the
        # gather has just replaced the root, so the record has to follow, or
        # the next open reads "fingerprint differs" and reopens the case as
        # an external mesh with its whole workflow outline gone.
        from foammesh.core.case import record_generated_mesh
        record_generated_mesh(case_path, provenance={
            'generated_by': f'reconstructPar:{gathered_stage}'})
        payload = execution.to_payload()
        payload['trace'] = self._trace_mesh_state(session, 'reconstruct')
        if gathered_stage != 'parallel':
            payload['stage_snapshot'] = await self._keep_gathered_stage(
                session, gathered_stage)
        return payload

    async def _mesh_reconstruct(self, session: CaseSession,
                                command: Command) -> OperationResult:
        """Bring the case-root mesh up to date with the processor cases."""
        session.require_writable()
        payload = await self._ensure_reconstructed(session, command)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('quality',),
            payload={'reconstructed': payload is not None,
                     'execution': payload})

    async def _ensure_decomposed(self, session: CaseSession, command: Command,
                                 engine, ranks: int, *,
                                 stage: str | None = None) -> list:
        """Decompose the case once, so later stages continue on processor meshes.

        Processor cases are reused only while they still hold the case-root
        mesh they were split from (DP-1230); *stage* is the stage about to
        read them.
        """
        case_root = session.case_path
        existing = tuple(case_root.glob('processor[0-9]*'))
        stale = self._stale_decomposition(case_root, ranks, stage)
        if existing and stale is None:
            # DP-663. Regenerating the dictionaries between phases writes the
            # serial default, and a reused decomposition kept it -- so the
            # gather that followed read one processor case of twelve.
            from foammesh.openfoam import decomposition
            if decomposition.written_ranks(case_root) != int(ranks):
                engine.write_parallel_config(
                    session.state.db, case_root, int(ranks))
            return []
        if existing:
            # Processor cases from an earlier run with a different rank count
            # would be picked up by mpirun as if they were this run's, and
            # ones split from another root mesh would be meshed in its place
            # (DP-1230). Remove them and decompose the live root.
            self._discard_decomposition(case_root)
            (case_root / self.PENDING_GATHER).unlink(missing_ok=True)
        engine.write_parallel_config(session.state.db, case_root, ranks)
        execution = await self._run_openfoam_utility(
            session, command, 'decomposePar',
            ('-case', str(case_root), '-force'),
            cwd=case_root, mutation=True)
        if not execution.succeeded:
            raise PreconditionFailedError(
                'the case could not be decomposed for parallel meshing',
                details={'error': 'decompose_failed', 'ranks': ranks})
        self._record_decomposition(case_root, ranks)
        return [execution.to_payload()]

    async def _run_stage_definition(self, session: CaseSession, command: Command,
                                    engine, definition):
        """Regenerate the stage dictionary and execute one meshing stage."""
        stage = definition.stage
        utility = await self._utility_ready(definition.utility)
        dictionary = session.case_path / 'system' / definition.dictionary
        if not dictionary.is_file():
            raise PreconditionFailedError(
                f'{definition.dictionary} is required; generate dictionaries first')
        if definition.utility in {'surfaceFeatures', 'snappyHexMesh'}:
            self._preflight_snappy_inputs(session, definition.utility)
        try:
            # Plan 35 CR2 (F11). Preparing the stage copies the input mesh
            # aside -- a whole polyMesh, gigabytes on a large case -- and
            # regenerates its dictionary: file work, done off the loop so
            # the window keeps drawing through it.
            stage_run = await asyncio.to_thread(
                engine.run_stage, stage, db=session.state.db,
                case_path=session.case_path, executable=utility)
        except (FileNotFoundError, ValueError) as error:
            raise PreconditionFailedError(
                str(error), details={'error': 'stage_inputs_unavailable',
                                     'stage': definition.stage}) from error
        registry = self._capabilities_registry()

        # Only the snappy stages decompose; blockMesh is trivial and
        # surfaceFeatures has no parallel form.
        ranks = (self._stage_ranks(session, command, definition)
                 if definition.utility == 'snappyHexMesh' else 1)
        if definition.utility == 'snappyHexMesh':
            # DP-678. The cap and the ranks it gave this stage, on disk.
            # DP-1232. And, for Auto, how Auto came to that count: Snap and
            # Layers read it back, so one mesh is meshed on one rank count.
            from foammesh.core.execution.resources import (
                execution_record, record_stage_execution,
            )
            try:
                cpu = self._snappy_cpu(
                    session.case_path,
                    _resource_policy(session.configuration()),
                    cores=int(command.parameters.get('cores') or 0),
                    stage=definition.stage)[0]
            except (TypeError, ValueError):
                cpu = None
            try:
                record_stage_execution(
                    session.case_path, definition.stage, execution_record(
                        _resource_policy(session.configuration()),
                        effective=ranks, unit='ranks',
                        auto=(cpu.to_dict() if cpu is not None and cpu.auto
                              else None)))
            except OSError:
                pass
        decomposition: list = []
        if (self._gather_pending(session.case_path)
                and self._stale_decomposition(
                    session.case_path,
                    len(tuple(session.case_path.glob('processor[0-9]*'))),
                    definition.stage) == 'pending_result_superseded'):
            # DP-1230. The ungathered result is this stage's own earlier
            # output (or a later stage's), and the input it starts from has
            # just been put back. Gathering it now would write that output
            # over the input.
            self._discard_decomposition(session.case_path)
            (session.case_path / self.PENDING_GATHER).unlink(missing_ok=True)
        if ranks == 1 and self._gather_pending(session.case_path):
            # A serial phase reads the case root, so an earlier decomposed
            # phase has to be gathered first. The processor cases then hold a
            # mesh this phase is about to supersede; leaving them would let a
            # later parallel phase pick up a stale decomposition.
            await self._ensure_reconstructed(session, command)
            self._discard_decomposition(session.case_path)
        elif (ranks == 1 and definition.utility != 'surfaceFeatures'
              and any(session.case_path.glob('processor[0-9]*'))):
            # DP-1201. A parallel stage is now gathered as soon as it ends,
            # and its processor cases are kept for the next parallel stage.
            # A serial stage supersedes them just the same.
            self._discard_decomposition(session.case_path)
        if ranks > 1:
            await self._utilities_ready(
                ('decomposePar', 'reconstructPar', 'mpirun'))
            for name in ('decomposePar', 'reconstructPar', 'mpirun'):
                capability = registry.utility(name) if hasattr(
                    registry, 'utility') else None
                if capability is not None and not capability.available:
                    raise CapabilityUnavailableError(
                        'parallel meshing needs the full OpenFOAM runtime',
                        details={'utility': name, 'reason': capability.reason})
            decomposition = await self._ensure_decomposed(
                session, command, engine, ranks, stage=definition.stage)

        launch_warning = ''
        if hasattr(registry, 'command'):
            if ranks > 1:
                # DP-1263. This launch passed no MPI options at all -- not
                # even the probed `--allow-run-as-root` the pipeline and the
                # redistribute pass -- and nothing for a count above the
                # host's physical cores, which Open MPI refuses.
                mpi, launch_warning = _mpi_launch_options(registry, ranks)
                launch = registry.command(
                    'mpirun',
                    (*mpi, '-np', str(ranks), definition.utility, '-parallel',
                     *stage_run.argv[1:]),
                    cwd=stage_run.cwd)
            else:
                launch = registry.command(
                    definition.utility, stage_run.argv[1:], cwd=stage_run.cwd)
        else:
            # Small injected capability doubles used by facade clients before
            # launch profiles existed remain valid native adapters.
            from foammesh.core.openfoam_runtime import LaunchCommand
            launch = LaunchCommand(stage_run.argv)

        feature_stage = definition.stage == 'surfaceFeatures'
        poly_mesh = (session.case_path / 'constant' / 'triSurface' if feature_stage
                     else session.case_path / 'constant' / 'polyMesh')
        required = ('points', 'faces', 'owner', 'neighbour', 'boundary')

        def valid_poly_mesh(path: Path) -> bool:
            if feature_stage:
                return path.is_dir() and any(path.glob('*.eMesh'))
            if ranks > 1:
                # A parallel stage writes into the processor cases; the case
                # root is only refreshed by the reconstruction below.
                return all(
                    (processor / 'constant' / 'polyMesh' / name).is_file()
                    for processor in session.case_path.glob('processor[0-9]*')
                    for name in required)
            return path.is_dir() and all((path / name).is_file() for name in required)

        execution = await self._context(session).executor.execute(session, OperationSpec(
            operation=command.operation,
            argv=launch.argv,
            cwd=stage_run.cwd,
            mutation=True,
            timeout=_stage_timeout(command.parameters,
                                   definition.timeout_seconds),
            max_output_bytes=4 * 1024 * 1024,
            expected_artifacts=(ExpectedArtifact(
                poly_mesh, kind='feature-edges' if feature_stage else 'polyMesh',
                validator=valid_poly_mesh),),
            recover_mesh=not feature_stage,
            artifact_event=(Event.ARTIFACT_DICTIONARIES_CHANGED if feature_stage
                            else Event.ARTIFACT_MESH_CHANGED),
            invalidated_outputs=('mesh', 'quality') if feature_stage else ('quality',),
            cleanup_argv=launch.cleanup_argv,
            # DP-817. DP-113's "changed nothing" check compares the mesh this
            # stage writes. surfaceFeatures writes no mesh, and a parallel
            # stage writes the processor meshes, not constant/polyMesh; both
            # were told they had changed nothing on every run.
            expects_mesh_change=not feature_stage,
            mesh_change_probes=tuple(
                processor / 'constant' / 'polyMesh'
                for processor in sorted(
                    session.case_path.glob('processor[0-9]*'))
            ) if ranks > 1 else (),
            ranks=ranks,
        ), on_line=command.parameters.get('on_line'))
        payload = execution.to_payload()
        payload.update({
            'stage': definition.stage, 'utility': definition.utility,
            'engine': engine.engine_id,
            'profile_id': launch.profile_id,
            'ranks': ranks,
        })
        if launch_warning:
            payload['oversubscribed'] = True
            payload['launch_warnings'] = [launch_warning]
        if ranks > 1 and not execution.succeeded:
            # Plan 35 D8. A failed parallel run leaves processor cases that
            # are neither the old mesh nor a new one; the next run would
            # start off them. They go, and the failure says so.
            removed = sorted(path.name for path in
                             session.case_path.glob('processor[0-9]*') if path.is_dir())
            self._discard_decomposition(session.case_path)
            try:
                (session.case_path / 'foammesh' / 'pending-gather.json').unlink(
                    missing_ok=True)
            except OSError:
                pass
            if removed:
                payload['discarded_decomposition'] = removed
                payload['discard_note'] = (
                    f'the {len(removed)} processor directories of the failed '
                    'parallel run were deleted')
        if decomposition:
            payload['decomposition'] = decomposition
        if ranks > 1 and execution.succeeded:
            # Deliberately left decomposed: the next stage runs straight off
            # these processor cases. The case root is rebuilt lazily by
            # ``_ensure_reconstructed`` when something reads it.
            payload['left_decomposed'] = True
            self._mark_pending_gather(session.case_path, definition.stage, ranks)
        if execution.succeeded and not feature_stage:
            # H9. The run that has just written constant/polyMesh is where
            # the fact that this mesh is ours becomes knowable, and nothing
            # recorded it. `resolve_workflow` therefore read the sidecar's
            # `workflow=none` as `mesh exists but metadata has no workflow
            # mode` -- External mesh -- so the first re-resolution after a
            # save re-opened our own freshly meshed case as an import and
            # collapsed the workflow outline to Scene / Display.
            from foammesh.core.case import record_generated_mesh
            record_generated_mesh(session.case_path, provenance={
                'generated_by': f'{engine.engine_id}:{definition.stage}'})
        if feature_stage and execution.succeeded:
            import hashlib

            def feature_artifacts():
                return [{
                    'path': str(path),
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'dictionary': str(dictionary),
                } for path in sorted(poly_mesh.glob('*.eMesh'))]

            # Plan 35 CR2 (F11): hashing every .eMesh is file work, off the
            # loop.
            payload['feature_artifacts'] = await asyncio.to_thread(
                feature_artifacts)
            provenance = self._surface_feature_inputs(session)
            from foammesh.core.geometry import GeometryArtifactStore
            advisories = []
            # Plan 35 CR7: an uncached diagnosis reads each B-Rep in a CAD
            # worker; the loop does not wait for it.
            diagnosed = await asyncio.to_thread(
                GeometryArtifactStore(session.case_path).diagnose)
            for geometry in diagnosed:
                if int(geometry.get('cells', 0)) < 100:
                    advisories.append({
                        'geometry_id': geometry['geometry_id'], 'kind': 'coarse_tessellation',
                        'message': 'Fewer than 100 facets; explicit feature extraction may be coarse.'})
                needles = next((finding for finding in
                                geometry['diagnostics']['findings']
                                if finding['kind'] == 'needle_triangles'), None)
                if needles and needles['count']:
                    advisories.append({
                        'geometry_id': geometry['geometry_id'], 'kind': 'noisy_tessellation',
                        'message': f"{needles['count']} needle facets can create noisy feature edges."})
            from foammesh.openfoam.target import DEFAULT_TARGET
            provenance.update({
                'utility': str(utility),
                'tool_target': f'{DEFAULT_TARGET.flavor.value}-{DEFAULT_TARGET.version}',
                'artifacts': payload['feature_artifacts'],
                'quality_advisories': advisories,
            })
            sidecar = poly_mesh / '.surfaceFeatures.provenance.json'
            sidecar.write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n',
                               encoding='utf-8')
            payload['provenance'] = provenance
            payload['feature_warnings'] = advisories
        if execution.succeeded and definition.stage in {'layers', 'snappyHexMesh'}:
            payload.update(
                self._layer_coverage_payload(session, engine, payload))
        if execution.succeeded and hasattr(engine, 'snapshot_run_config'):
            snapshot = engine.snapshot_run_config(session.case_path)
            payload['dictionary_snapshot'] = str(snapshot)
            # R182. `mark_stage_run` fingerprints the configuration in
            # memory, and the configuration only reached disk on Save.
            # A case interrupted between the two came back with every
            # stage recorded as run against a configuration that was
            # never persisted, so `require_current_stage_dependencies`
            # compared the recorded hash against the older saved one
            # and reported stages the workflow had marked passed as
            # stale -- a dead end with no way forward on screen.
            # MEASURED on tee_snappy_v2: configurations.h5 written at
            # 12:35, stage-runs.json at 13:03, and Apply on Boundary
            # Layers refused with `upstream mesh stages are stale and
            # must be rerun first: surfaceFeatures, castellation`.
            # A run is a commitment, so the configuration it ran on is
            # written before the record that claims it.
            if not session.read_only:
                session.state.db.save()
            stage_state = engine.mark_stage_run(
                session.state.db, session.case_path, definition.stage)
            payload['stage_state'] = str(stage_state)
        return execution, payload

    @staticmethod
    def _requested_layer_counts(session: CaseSession, engine) -> dict:
        """Ask the engine which patch received each requested layer count."""
        if not hasattr(engine, 'requested_layer_counts'):
            return {}
        try:
            return engine.requested_layer_counts(
                session.state.db, session.case_path)
        except (OSError, KeyError, TypeError, ValueError):
            return {}

    @staticmethod
    def _frozen_layer_patches(session: CaseSession, engine) -> set:
        """Which patches the dictionary froze, so the report can say so."""
        if not hasattr(engine, 'frozen_layer_patches'):
            return set()
        try:
            return engine.frozen_layer_patches(
                session.state.db, session.case_path)
        except (OSError, KeyError, TypeError, ValueError):
            return set()

    @classmethod
    def _layer_coverage_payload(cls, session: CaseSession, engine,
                                payload: dict) -> dict:
        """Report the prism layers snappy actually produced, not the request.

        Snappy exits successfully even when every layer was rejected, so a
        stage result without this reads as a layered mesh that does not exist.
        """
        from foammesh.core.quality import NO_LAYERS_MARKER, parse_layer_log
        log_path = (payload.get('job') or {}).get('log_path')
        if not log_path:
            return {}
        try:
            text = Path(log_path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            return {}
        report = parse_layer_log(
            text, cls._requested_layer_counts(session, engine),
            cls._frozen_layer_patches(session, engine))
        if not report.patches:
            # DP-112. This is the one place that exists to say what the layers
            # stage actually produced, and on a run that produced nothing it
            # returned an empty dict and said nothing at all -- so a stage that
            # printed `No layers to generate ...` and left the mesh byte for
            # byte as it found it was reported exactly like one that layered
            # every wall. MEASURED on all nine meshed snappy legs of the
            # 1f2787eb sweep. There is no coverage document to write, but
            # there is a fact to report.
            if NO_LAYERS_MARKER in text:
                return {'layer_warnings': [
                    'snappyHexMesh added no layers: no patch was selected to '
                    'grow them on, so this stage left the mesh unchanged']}
            return {'layer_warnings': [
                'the layers stage produced no per-patch coverage table, so '
                'how many layers reached the mesh could not be read from its '
                'log']}
        document = report.to_dict()
        # Plan 26 WP6.1. Only the warning list was ever consumed, and only for
        # patches below the 50% floor -- so a patch that got its layers was
        # never reported at all, and no figure survived the completion dialog
        # being dismissed. Persisting it is what lets the page, the renderer
        # and the report read the same numbers instead of each re-parsing a
        # log that the next run overwrites.
        cls._write_layer_coverage(session, document)
        return {'layer_coverage': document,
                'layer_warnings': report.warnings}

    #: Where achieved layer coverage lives between runs.
    LAYER_COVERAGE_PATH = 'foammesh/quality/layer-coverage.json'

    @classmethod
    def _write_layer_coverage(cls, session: CaseSession, document: dict) -> None:
        import json as _json

        path = Path(session.case_path) / cls.LAYER_COVERAGE_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(
                _json.dumps(document, indent=2, sort_keys=True) + '\n',
                encoding='utf-8')
            os.replace(temporary, path)
        except OSError:
            # Losing the record must not fail the layer stage that produced it.
            pass

    @classmethod
    def _record_gmsh_layer_coverage(cls, session: CaseSession, record) -> dict:
        """Keep what a finished Gmsh run grew, where the page reads it.

        Plan 32 check 5. The snappy layers stage has written
        ``foammesh/quality/layer-coverage.json`` since Plan 26 and the Quality
        page reads it back through ``mesh.layer_coverage``; the Gmsh run
        measured the same thing per patch and left it in its own manifest, so
        the page said no per-patch layer measurement had been recorded. This
        is the one line that connects the two -- the projection itself lives
        in ``core/gmsh/layers.py`` beside the request it is held against.
        """
        from foammesh.core.gmsh.layers import achieved_coverage

        document = achieved_coverage(
            (getattr(record, 'document', None) or {}).get('statistics') or {})
        if document.get('patches'):
            cls._write_layer_coverage(session, document)
            return document
        return {}

    def _mesh_layer_coverage(self, session: CaseSession,
                             command: Command) -> OperationResult:
        """Achieved layers per patch, for every patch -- not only the failures.

        A patch that got what it asked for was previously never mentioned, so
        a user could not distinguish "all good" from "not measured".
        """
        import json as _json

        path = Path(session.case_path) / self.LAYER_COVERAGE_PATH
        try:
            document = _json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return self._read_result(session, command, {
                'patches': [], 'warnings': [], 'measured': False})
        return self._read_result(session, command, dict(document, measured=True))

    # -- the run record both engines keep (Plan 30 F-15) ------------------- #

    @staticmethod
    def _run_manifest_id(record) -> str:
        return str(getattr(record, 'run_id', '') or '')

    def _open_run_manifest(self, session: CaseSession, *, engine_id: str,
                           job: dict):
        """Open a run directory and manifest for this run, or ``None``.

        Never fatal. A case that cannot be given a run record has still built
        a mesh, and refusing the mesh because the bookkeeping failed would be
        a worse outcome than a missing row in the Runs list.
        """
        import uuid

        from foammesh.core.gmsh.manifest import ManifestError, RunBuilder

        run_id = f'{engine_id}-{uuid.uuid4().hex[:16]}'
        try:
            return RunBuilder(session.case_path).create(
                run_id, job=dict(job), engine_id=engine_id)
        except (ManifestError, OSError, TypeError, ValueError) as error:
            logger.warning('run manifest could not be opened: %s', error)
            return None

    @staticmethod
    def _close_run_manifest(record, *, status: str, reason: str = '',
                            publication: dict | None = None) -> None:
        from foammesh.core.gmsh.manifest import RunBuilder

        if record is None:
            return
        try:
            RunBuilder.record_result(
                record, {'status': status, 'error': reason})
            if status == 'cancelled':
                # CP-05 item 7. `record_result` maps anything that is not a
                # success onto the runner's own vocabulary; a run the user
                # stopped is not a run that failed, and the Runs list has to
                # be able to say which one this was.
                RunBuilder.record_cancelled(record, reason)
                return
            if publication is not None:
                RunBuilder.record_publication(record, publication)
            elif reason:
                RunBuilder.record_publication_failure(record, reason)
        except (OSError, TypeError, ValueError) as error:
            logger.warning('run manifest could not be closed: %s', error)

    async def _workflow_run_pipeline(self, session: CaseSession,
                                     command: Command) -> OperationResult:
        """Run this case's whole mesh, through whichever engine it chose.

        Plan 30 F-03. There were two whole-mesh orchestrators: this one, which
        ran snappy's DAG and refused every other engine outright, and
        ``mesh.gmsh.run``, which was a second door with its own preconditions,
        its own failure payload and its own finisher. Nothing above them could
        say "run this case's mesh" without first asking which engine it was.

        The dispatch is the registry's now. What a run *consists of* is the
        engine's (:meth:`MeshingEngine.run`); the machinery it consists of --
        the supervised executor, mutation recovery, the checkpoint and
        evidence writers -- is still the facade's, and is handed to the engine
        as :class:`EngineRunRequest.orchestrator`.
        """
        session.require_writable()
        from foammesh.core.engine import configured_engine_id, resolve_engine
        from foammesh.core.engine.base import EngineRunRequest
        from foammesh.core.engine.registry import EngineNotRegisteredError
        from foammesh.db.configurations_schema import MeshEngine

        if configured_engine_id(session.state.db) == MeshEngine.UNSELECTED.value:
            raise PreconditionFailedError(
                'Choose a meshing method before running the mesh. Open '
                'the Meshing method step and apply one.')
        try:
            engine = resolve_engine(session.state.db)
        except EngineNotRegisteredError as error:
            raise PreconditionFailedError(
                f'this case names a meshing method this build does not have: '
                f'{configured_engine_id(session.state.db)}') from error
        # DP-354. The engine's own preflight asks the runtime which utilities
        # it has, one at a time and from inside this coroutine. Answer them
        # all off the loop first so the window stays alive while it does.
        await self._warm_utility_probes()
        return await engine.run(EngineRunRequest(session, command, self))

    @staticmethod
    def _pipeline_stage_keeps(nodes) -> dict[str, str]:
        """``{node id: stage}``: after which pipeline node each stage is kept.

        DP-1202. A stage is kept only where the case root holds that stage's
        own, complete result:

        * ``blockMesh`` after its node, on every route;
        * the serial split route (one ``snappyHexMesh`` per phase, each
          overwriting the case root): each phase after its node;
        * the MPI split route: Castellation after ``reconstructCastellation``
          (DP-1234 added that gather), Snap after ``reconstructSnap``, and
          Layers after the final ``reconstructPar`` (before any interface
          couple rewrites the mesh).

        Not kept, by design: the combined route's three phases -- one
        invocation, so only its finished mesh exists; it is kept whole as
        ``snappyHexMesh``. DP-1234: ``workflow.run_pipeline`` no longer
        takes the combined route.
        """
        ids = [node.node_id for node in nodes]
        keeps = {'blockMesh': 'blockMesh'} if 'blockMesh' in ids else {}
        # DP-1236. A resumed run starts part-way, so any phase counts.
        if not any(phase in ids for phase in ('castellation', 'snap',
                                              'layers')):
            return keeps
        if 'decomposePar' not in ids:
            keeps.update({phase: phase for phase in
                          ('castellation', 'snap', 'layers') if phase in ids})
            return keeps
        if 'reconstructCastellation' in ids:
            keeps['reconstructCastellation'] = 'castellation'
        if 'reconstructSnap' in ids:
            keeps['reconstructSnap'] = 'snap'
        if 'layers' in ids and 'reconstructPar' in ids:
            keeps['reconstructPar'] = 'layers'
        return keeps

    #: DP-1236. What a stopped or failed pipeline run left to resume from.
    PIPELINE_RESUME_RECORD = 'foammesh/pipeline-resume.json'

    @classmethod
    def _park_root_for_gather(cls, case_path: Path) -> Path | None:
        """Move the case-root mesh out of ``reconstructPar``'s sight (DP-1235).

        DP-70 measured that a gather over a complete case-root mesh, with
        ``cellProcAddressing`` in the processor cases, gathers the points
        only and exits 0. ``_ensure_reconstructed`` parks the root for that
        reason; the pipeline's own gathers did not, and the split route
        gathers three times over a root that holds the previous stage's mesh
        while every earlier gather has written ``cellProcAddressing`` back.
        """
        from foammesh.support.utils import rmtree
        root = Path(case_path) / 'constant' / 'polyMesh'
        parked = Path(case_path) / cls.PARKED_ROOT_MESH
        if parked.exists():
            rmtree(parked)
        if not root.is_dir():
            return None
        root.rename(parked)
        return parked

    @classmethod
    def _settle_gather(cls, case_path: Path, parked: Path | None,
                       succeeded: bool) -> str:
        """Keep the gather, or put the parked root back; ``''`` when kept.

        A gather that exits 0 is read back: the case root must hold every
        cell the processor cases do, or it is not the stage's result.
        """
        from foammesh.support.utils import rmtree
        root = Path(case_path) / 'constant' / 'polyMesh'
        problem = ''
        if succeeded:
            if not root.is_dir():
                problem = ('the gather reported success and left no mesh in '
                           'the case root')
            else:
                gathered = cls._polymesh_cell_count(root)
                decomposed = cls._decomposed_cell_count(Path(case_path))
                if (gathered is not None and decomposed is not None
                        and gathered != decomposed):
                    problem = (
                        f'the gathered mesh holds {gathered} cells and the '
                        f'processor cases {decomposed}, so the case root is '
                        'not this stage result')
        if parked is not None and parked.is_dir():
            if succeeded and not problem:
                rmtree(parked)
            else:
                if root.is_dir():
                    rmtree(root)
                parked.rename(root)
        return problem

    @classmethod
    def _read_pipeline_resume(cls, case_path: Path) -> dict | None:
        try:
            document = json.loads((Path(case_path) / cls.PIPELINE_RESUME_RECORD)
                                  .read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    @classmethod
    def _clear_pipeline_resume(cls, case_path: Path) -> None:
        try:
            (Path(case_path) / cls.PIPELINE_RESUME_RECORD).unlink(
                missing_ok=True)
        except OSError:
            pass

    @classmethod
    def _write_pipeline_resume(cls, case_path: Path, document: dict) -> None:
        path = Path(case_path) / cls.PIPELINE_RESUME_RECORD
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document, indent=2, sort_keys=True),
                            encoding='utf-8')
        except OSError:
            logger.warning('could not record where the run stopped',
                           exc_info=True)

    def _keep_interrupted_run(self, session: CaseSession, command: Command,
                              record, failed_node: str, stopped: bool,
                              stage_snapshots, resumed) -> dict:
        """Record what a stopped or failed pipeline run finished (DP-1236).

        Before this, a run stopped in Layers recorded nothing: the tree read
        as though no stage had run, though the base grid, Castellation and
        Snap were each kept on disk, and the only way on was to run all of
        it again. Each stage kept by this run is recorded done (its chain,
        never across a gate); stages a resumed run started from were recorded
        when they were kept. Processor cases are discarded -- they hold the
        interrupted step, which is not kept -- so the case root, the last
        kept stage, is the case's mesh. Returns ``{kept_stages, recorded,
        resume}``; ``resume`` is :meth:`_pipeline_resume_point`'s answer.
        """
        from datetime import datetime, timezone

        from foammesh.core.jobs import stage_snapshots as snapshots

        kept = []
        if resumed:
            limit = snapshots._stage_rank(resumed['from_stage'])
            kept.extend(item for item in resumed.get('kept') or ()
                        if snapshots._stage_rank(item['stage']) <= limit)
        fresh = [{'stage': item['stage'], 'revision': item['revision']}
                 for item in stage_snapshots
                 if isinstance(item, dict) and item.get('captured')
                 and item.get('revision') and item.get('stage')
                 in snapshots.STAGE_ORDER]
        kept = [item for item in kept
                if item['stage'] not in {new['stage'] for new in fresh}]
        kept.extend(fresh)
        recorded = []
        for item in fresh:
            if self._record_stage_run_success(
                    session, command,
                    snapshots.STAGE_TASKS[item['stage']]) is not None:
                recorded.append(item['stage'])
        if any(session.case_path.glob('processor[0-9]*')):
            self._discard_decomposition(session.case_path)
        if kept:
            self._write_pipeline_resume(session.case_path, {
                'run_id': self._run_manifest_id(record),
                'failed_node': failed_node, 'stopped': bool(stopped),
                'kept': kept, 'at': datetime.now(timezone.utc).isoformat()})
        else:
            self._clear_pipeline_resume(session.case_path)
        return {'kept_stages': [item['stage'] for item in kept],
                'recorded': recorded,
                'resume': self._pipeline_resume_point(session, command)}

    def _pipeline_resume_point(self, session: CaseSession, command: Command,
                               *, engine=None) -> dict:
        """Where a stopped ``workflow.run_pipeline`` can carry on from.

        DP-1236. ``{available, from_stage, revision, next_stage, kept,
        failed_node, stopped, reason, settings_checked}``. A stage counts only
        when the stopped run kept it, the kept copy still resolves, the task
        tree still has it published (an edit or reset unpublishes it), every
        stage before it counts, and its dictionary is the one it ran from:
        ``blockMeshDict`` always, and with *engine* (the resume itself) each
        phase's ``snappyHexMeshDict`` regenerated from the current settings.
        The query (no *engine*) writes nothing and leaves the phase
        dictionaries to the resume, which falls back to an earlier stage when
        one changed.
        """
        from foammesh.core.jobs import stage_snapshots as snapshots

        case = session.case_path
        answer = {'available': False, 'from_stage': None, 'revision': None,
                  'next_stage': None, 'kept': [], 'failed_node': None,
                  'stopped': False, 'settings_checked': engine is not None,
                  'reason': 'no run was stopped part-way'}
        record = self._read_pipeline_resume(case)
        if not record:
            return answer
        kept = {str(item.get('stage')): str(item.get('revision'))
                for item in record.get('kept') or ()
                if isinstance(item, dict) and item.get('revision')}
        answer.update(kept=[{'stage': stage, 'revision': kept[stage]}
                            for stage in snapshots.STAGE_ORDER
                            if stage in kept],
                      failed_node=record.get('failed_node'),
                      stopped=bool(record.get('stopped')))
        try:
            resolved = snapshots.resolve(case)
        except (OSError, ValueError, KeyError):
            resolved = {}
        valid, _later = self._stage_validity(session, command, 'layers')
        chosen = None
        why = 'the stopped run kept no stage'
        for stage in snapshots.STAGE_ORDER:
            revision = kept.get(stage)
            if not revision:
                break
            if resolved.get(stage) != revision:
                why = f'the kept {stage} mesh is no longer on disk'
                break
            if valid is not None and stage not in valid:
                why = f'{stage} has changed or been reset since the run stopped'
                break
            manifest = snapshots.read_manifest(case, stage, revision) or {}
            ran_from = str(manifest.get('settings_fingerprint') or '')
            if stage == 'blockMesh' or engine is not None:
                if engine is not None and stage != 'blockMesh':
                    try:
                        engine.write_phase_dictionary(
                            session.state.db, case, stage)
                    except (OSError, ValueError, KeyError) as error:
                        why = f'the {stage} dictionary could not be written: {error}'
                        break
                if not ran_from or ran_from != snapshots.settings_fingerprint(
                        case, stage):
                    why = (f'the {stage} settings changed since the run '
                           'stopped')
                    break
            chosen = stage
        if chosen is None:
            answer['reason'] = why
            return answer
        order = snapshots.STAGE_ORDER
        following = order[order.index(chosen) + 1:]
        answer.update(available=True, from_stage=chosen,
                      revision=kept[chosen],
                      next_stage=following[0] if following else 'checkMesh',
                      reason=(f'resumes after {chosen}' if chosen == order[-1]
                              or kept.get(following[0]) is None
                              else f'resumes after {chosen}: {why}'))
        return answer

    async def run_snappy_pipeline(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        """Execute the serial/MPI DAG and publish a validated mesh-state ref.

        The body of :meth:`SnappyMeshingEngine.run`, which is what reaches it.
        """
        session.require_writable()
        from foammesh.core.engine import resolve_engine
        from foammesh.core.execution import (
            ResourceMode, ResourcePolicy, ResourceRequest,
            allocate_resources, openfoam_meshing_dag,
        )
        from foammesh.core.execution.resources import execution_record
        from foammesh.core.jobs import OperationSpec
        from foammesh.core.mesh import MeshLayout, MeshStateError, MeshStateStore
        from foammesh.core.run_result import failure_payload
        for name in ('blockMeshDict', 'surfaceFeaturesDict',
                     'snappyHexMeshDict', 'controlDict'):
            if not (session.case_path / 'system' / name).is_file():
                raise PreconditionFailedError(
                    f'{name} is required; generate dictionaries first')
        engine = resolve_engine(session.state.db)
        try:
            manifest = engine.refresh_pipeline_config(
                session.state.db, session.case_path)
        except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
            raise ValidationFailedError(
                f'current Snappy dictionaries could not be generated: {error}'
            ) from error
        self._preflight_snappy_inputs(session, 'surfaceFeatures')
        # DP-851. The same seed gate the stage route asks, against the
        # blockMeshDict just rewritten above, before any node launches.
        seed_warnings = await self._snappy_seed_gate(
            session, allow_region_clash=self._allows_region_clash(command))
        configuration = _resource_policy(session.configuration())
        mode = ResourceMode(str(command.parameters.get(
            'mode') or configuration['mode']))
        # What to run on is the count on Meshing resources (DP-691: the
        # Parallel Environment dialog, which used to be read first, is gone).
        # ``maxCpuCores`` still clamps an explicit request below.
        # DP-590 (field audit 0924 snappy-back D1). An unopened dialog answers
        # 1, and 1 used to outrank the ceiling here, so "Run to end" meshed a
        # case whose page said six cores on one rank while the Plan preview
        # said six. The same rule the preview and ``_stage_ranks`` ask.
        # DP-1231. Nothing asked is Auto -- the WSL host's cores less one,
        # within its free RAM -- not one rank.
        await self._warm_meshing_host()
        try:
            auto_cpu, meshing_host = self._snappy_cpu(
                session.case_path, configuration,
                cores=int(command.parameters.get('cores') or 0))
            requested_cores = (1 if mode is ResourceMode.SERIAL
                               else max(1, auto_cpu.count))
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        policy = ResourcePolicy(
            mode, configuration['max_cpu_cores'],
            configuration['max_memory_bytes'],
            configuration['allow_distributed'],
            'openfoam-mpi' if requested_cores > 1 else 'local')
        try:
            allocation = allocate_resources(
                policy,
                ResourceRequest(
                    requested_cores, backend_id='openfoam-mpi',
                    explicit='cores' in command.parameters),
                meshing_host.resource_facts())
        except (ValueError, TypeError) as error:
            raise ValidationFailedError(str(error)) from error
        # F-13. The interface pairs the Geometry page authored reach the run
        # here: the case group manifest the builder just rewrote carries them
        # beside the prepared groups they name, and each non-conformal pair
        # becomes a step of this run rather than something the user is left to
        # apply by hand afterwards.
        couples = self._pipeline_interface_couples(session)
        registry = self._capabilities_registry()
        required_utilities = [
            'blockMesh', 'surfaceFeatures', 'snappyHexMesh', 'checkMesh']
        if couples:
            required_utilities.append('createNonConformalCouples')
        if allocation.effective_ranks > 1:
            required_utilities.extend(
                ('decomposePar', 'reconstructPar', 'mpirun'))
        if hasattr(registry, 'utility'):
            await self._utilities_ready(required_utilities)
            capabilities = [
                registry.utility(name) for name in required_utilities]
            missing = [
                item for item in capabilities if not item.available]
            if missing:
                # Plan 37 F2. Name each missing utility and the probe's
                # reason in the sentence, not only in the details.
                raise CapabilityUnavailableError(
                    'OpenFOAM 13 pipeline runtime is incomplete. ' + ' '.join(
                        unavailable_utility_text(item.name, item.reason)
                        for item in missing),
                    details={'missing': [
                        {'utility': item.name, 'reason': item.reason}
                        for item in missing]})
            profile_ids = {
                item.profile_id for item in capabilities if item.profile_id}
            fingerprints = {
                registry.runtime_fingerprint(item.name)
                for item in capabilities}
            fingerprints.discard(None)
            if len(profile_ids) != 1 or len(fingerprints) != 1:
                raise CapabilityUnavailableError(
                    'pipeline utilities resolved to mixed OpenFOAM runtimes',
                    details={'profile_ids': sorted(profile_ids),
                             'fingerprints': sorted(fingerprints)})
        if hasattr(registry, 'runtime_fingerprint'):
            from dataclasses import replace
            allocation = replace(
                allocation,
                runtime_fingerprint=registry.runtime_fingerprint('blockMesh'))
        engine.write_parallel_config(
            session.state.db, session.case_path, allocation.effective_ranks)
        # Plan 31 CP-07 item 5. Every node below reads these files off disk
        # in turn; seal them now so "the mesh, the decomposition and the
        # check all came from one revision" is something the run verifies
        # rather than something the reader assumes.
        from foammesh.core.engine.snappy import SnappyMeshingEngine
        prepared_at_start = self._current_prepared(session)
        prepared_reference = getattr(prepared_at_start, 'reference', None)
        revision_seal = SnappyMeshingEngine.revision_seal(
            session.case_path,
            prepared_revision=str(
                getattr(prepared_reference, 'revision_id', '') or ''))
        # Plan 23 §8.2: the route decomposes so the snapped boundary exists
        # un-overwritten for a moment. Shipping dark -- in report-only, which is
        # the default, nothing about the route changes.
        from foammesh.core.quality.qualification import qualification_mode

        mode = qualification_mode()
        # F-04. The pipeline's checkMesh node is built from the same probed
        # profile ``mesh.check`` uses, so both write the same report.
        # CP-07 item 6. What the launcher needs to actually start the ranks
        # it was asked for, taken from the probed runtime rather than assumed.
        # DP-1263. --oversubscribe when the ranks exceed the physical cores.
        mpi_options, launch_warning = _mpi_launch_options(
            registry, allocation.effective_ranks)
        launch_warnings = (launch_warning,) if launch_warning else ()
        from foammesh.core.quality.checkmesh_service import checkmesh_request

        # DP-591. A skipped Boundary layers task grows nothing: the
        # dictionary was regenerated without them above, the split route
        # drops its layers phase, and the tree keeps the skip.
        layers_skipped = bool(getattr(
            engine, 'layers_skipped', lambda _path: False)(session.case_path))
        await self._checkmesh_help_ready()
        # DP-1234. Every run is split into its stages, so each finished stage
        # is kept as it lands and a run stopped in Layers has lost Layers,
        # not the whole mesh. MEASURED in plans/evidence/plan23-wp7a-spike:
        # the split and combined routes give the same 22,178 cells (serial
        # and MPI x2), and the facade run on the curved pipe 10,234 cells
        # either way. Only enforcing qualification pauses at Snap.
        dag = openfoam_meshing_dag(
            session.case_path, allocation, split_at_snap=True,
            pause_at_snap=mode.enforces,
            layers=not layers_skipped,
            mpi_options=mpi_options,
            check_profile=self._checkmesh_profile()[0],
            # Plan 31. The same request ``mesh.check`` builds, from the same
            # settings. Both nodes write the same report to the same path, so
            # a threshold that reached only one of them meant the verdict on a
            # mesh depended on which check had run last.
            check_request=checkmesh_request(
                session.state.db, session.case_path),
            interface_couples=tuple(
                (pair['master_patch'], pair['slave_patch'])
                for pair in couples))
        # DP-1236. ``resume`` carries on from the last stage a stopped run
        # kept: that mesh is put back and only the nodes after it run.
        resumed = None
        if command.parameters.get('resume'):
            from foammesh.core.execution.dag import resume_dag
            from foammesh.core.jobs import stage_snapshots as snapshots

            resumed = self._pipeline_resume_point(
                session, command, engine=engine)
            if not resumed['available']:
                raise PreconditionFailedError(
                    f'There is nothing to resume: {resumed["reason"]}.',
                    details={'error': 'resume_unavailable',
                             'resume': resumed})
            keeps_at = {stage: node for node, stage
                        in self._pipeline_stage_keeps(dag.nodes).items()}
            # A stage this route does not keep (Layers, now skipped) is
            # resumed from the stage before it.
            from_stage = resumed['from_stage']
            while from_stage not in keeps_at:
                from_stage = snapshots.PREDECESSOR[from_stage]
            if from_stage != resumed['from_stage']:
                resumed = dict(resumed, from_stage=from_stage, revision=(
                    {item['stage']: item['revision']
                     for item in resumed['kept']}[from_stage]))
            if snapshots.live_mesh_is(session.case_path, from_stage,
                                      resumed['revision']):
                self._discard_decomposition(session.case_path)
                (session.case_path / self.PENDING_GATHER).unlink(
                    missing_ok=True)
            else:
                await self._restore_stage_snapshot(
                    session, {'stage': from_stage,
                              'revision': resumed['revision']})
            dag = resume_dag(dag, keeps_at[from_stage])
        else:
            self._clear_pipeline_resume(session.case_path)
        # Plan 30 F-15. Both engines leave the same record behind. Gmsh wrote
        # a run manifest for every run and snappy wrote none, so the Runs
        # surface could only ever show half a case's history -- which is why
        # it filtered to the Gmsh rows and called that the list.
        record = self._open_run_manifest(
            session, engine_id='snappy',
            job={'job_digest': dag.digest if hasattr(dag, 'digest') else '',
                 'dag': dag.to_dict(), 'allocation': allocation.to_dict(),
                 # DP-678. The CPU cap beside the ranks it resolved to.
                 'execution': execution_record(
                     {**configuration, 'mode': mode.value},
                     effective=allocation.effective_ranks,
                     unit='ranks'),
                 'revision_seal': revision_seal})
        checkpoints = []
        executions = []
        check_execution = None
        keeps = self._pipeline_stage_keeps(dag.nodes)
        stage_snapshots = []
        for node in dag.nodes:
            if node.node_id in getattr(engine, 'SPLIT_PHASES', ()):
                # The three phases share a command line and differ only in the
                # dictionary, so it has to be rewritten before each one.
                engine.write_phase_dictionary(
                    session.state.db, session.case_path, node.node_id)
            utility, arguments = node.argv[0], node.argv[1:]
            try:
                launch = registry.command(utility, arguments, cwd=node.cwd)
            except (FileNotFoundError, ValueError) as error:
                raise CapabilityUnavailableError(
                    unavailable_utility_text(
                        utility, str(error), kind='pipeline utility'),
                    details={'utility': utility,
                             'reason': str(error)}) from error
            # DP-1235. Every gather runs with the case root parked.
            gathers = tuple(node.argv[:1]) == ('reconstructPar',)
            parked = (self._park_root_for_gather(session.case_path)
                      if gathers else None)
            try:
                execution = await self._context(session).executor.execute(
                    session, OperationSpec(
                        operation=f'workflow.pipeline.{node.node_id}',
                        argv=launch.argv, cwd=node.cwd,
                        mutation=node.mutates_mesh,
                        timeout=_stage_timeout(command.parameters, 3600),
                        max_output_bytes=10 * 1024 * 1024,
                        recover_mesh=node.mutates_mesh,
                        cleanup_argv=launch.cleanup_argv),
                    on_line=command.parameters.get('on_line'))
            except BaseException:
                if gathers:
                    self._settle_gather(session.case_path, parked, False)
                raise
            gather_problem = (self._settle_gather(
                session.case_path, parked, execution.succeeded)
                if gathers else '')
            executions.append(execution.to_payload())
            if node.node_id == 'checkMesh':
                check_execution = execution
            if not execution.succeeded or gather_problem:
                failure = execution.to_payload()
                job = failure.get('job') or {}
                stopped = str(job.get('status') or '') == 'cancelled'
                # DP-506. This read `error` and `log_path` off the execution
                # payload, where neither lives -- both are on its `job` -- so
                # every failed node was reported as "<node> failed" with no
                # log. The same cause the stage route reports, read the same
                # way.
                from foammesh.core.jobs.retry import attempt_of
                explained = self._with_retry_offer(
                    self._stage_failure(node.node_id, job), job,
                    ranks=allocation.effective_ranks,
                    attempt=attempt_of(command.parameters))
                if gather_problem and not stopped:
                    explained = dict(explained, reason=gather_problem,
                                     details=gather_problem)
                reason = ('the run was cancelled before it finished' if stopped
                          else explained['reason'])
                self._close_run_manifest(
                    record, status='cancelled' if stopped else 'failed',
                    reason=reason)
                # DP-1236. What finished stays finished: each stage this run
                # kept is recorded done, the interrupted node alone was rolled
                # back (the executor's recovery point, or the parked root),
                # and the run can be resumed from the last of them.
                interrupted = self._keep_interrupted_run(
                    session, command, record, node.node_id, stopped,
                    stage_snapshots, resumed)
                return OperationResult(
                    'failed', command.operation, session.revisions,
                    invalidated_outputs=('quality', 'exports'),
                    warnings=tuple(seed_warnings) + launch_warnings,
                    payload={'allocation': allocation.to_dict(),
                             'dag': dag.to_dict(), 'executions': executions,
                             'checkpoints': checkpoints,
                             'failed_node': node.node_id,
                             'stage_snapshots': [
                                 kept for kept in stage_snapshots
                                 if kept is not None],
                             'kept_stages': interrupted['kept_stages'],
                             'recorded_stages': interrupted['recorded'],
                             'resume': interrupted['resume'],
                             **({'resumed': resumed} if resumed else {}),
                             'run_id': self._run_manifest_id(record),
                             **self._region_warnings_payload(seed_warnings),
                             # One failure shape, whichever engine wrote it
                             # (Plan 30 F-03). `RunResultHandle.from_payload`
                             # is the reader; the finisher no longer has to
                             # know which orchestrator it is reading.
                             **failure_payload(
                                 task=f'snappy.{node.node_id}',
                                 reason=reason, log=explained['log']),
                             'details': '' if stopped else explained['details'],
                             # Plan 35 CR6: re-run from the restored snapshot.
                             **({'retry': explained['retry']}
                                if not stopped and explained.get('retry')
                                else {})})
            if node.pauses_after:
                # The snapped boundary is in the case root and the next node is
                # about to overwrite it. Capture it immutably now; GF1 measures
                # this, and a blocked run will name its fingerprint.
                checkpoints.append(self._save_snap_checkpoint(
                    session, run_id=str(execution.to_payload().get('job_id') or '')))
            if node.node_id in keeps:
                # DP-1202. The case root holds this stage's own result now and
                # the next node is about to overwrite it.
                stage_snapshots.append(await self._capture_stage_snapshot(
                    session, engine, keeps[node.node_id], execution))
        divergence = SnappyMeshingEngine.seal_divergence(
            revision_seal, SnappyMeshingEngine.revision_seal(
                session.case_path,
                prepared_revision=revision_seal.get('prepared_revision', '')))
        if divergence:
            self._close_run_manifest(
                record, status='failed', reason=divergence)
            raise ValidationFailedError(
                'this run did not read one revision throughout: '
                + divergence)
        try:
            reference = MeshStateStore(session.case_path).inspect(
                layout=MeshLayout.RECONSTRUCTED, engine_id='snappy',
                run_id=str(command.parameters.get(
                    'run_id') or executions[-1]['job_id']),
                runtime_fingerprint=allocation.runtime_fingerprint)
            MeshStateStore(session.case_path).publish(reference)
        except MeshStateError as error:
            raise ValidationFailedError(
                f'pipeline output could not be published: {error}') from error
        session.state.bus.publish(
            Event.ARTIFACT_MESH_CHANGED, mesh_state=reference.to_dict())
        prepared = self._current_prepared(session)
        patch_identity_state = self._write_snappy_patch_identity(
            session, prepared)
        last_checkpoint = ''
        for item in checkpoints:
            if item.get('saved'):
                last_checkpoint = str(item.get('fingerprint') or '')
        evidence_state = self._bind_evidence(
            session, checkpoint_fingerprint=last_checkpoint, prepared=prepared)
        snapshot = engine.snapshot_run_config(session.case_path)
        stage_state = engine.mark_pipeline_run(
            session.state.db, session.case_path)
        quality_report = self._persist_pipeline_check(session, check_execution)
        from foammesh.core.quality import QualityReport
        from foammesh.core.quality.verdict import verdict_from_report
        quality_verdict = verdict_from_report(
            QualityReport.from_dict(quality_report) if quality_report else None)
        not_clean = quality_verdict.get('verdict') not in ('pass', None)
        # DP-817. The checkMesh verdict judges the finished mesh, so it is the
        # QA task's warning. It was handed to every task the run covered, and
        # surface features, castellation, snap and layers all read "finished
        # with warnings" for a verdict none of them produced.
        qa_warning = (
            ('checkMesh verdict: {0}. {1}'.format(
                quality_verdict.get('verdict'),
                quality_verdict.get('reason') or '').strip(),)
            if not_clean else ())
        try:
            recorded = self._record_engine_run_success(
                session, command, warning=not_clean,
                warning_for=('snappy.qa',), reasons=qa_warning,
                exclude=('snappy.layers',) if layers_skipped else ())
        except FacadeError:
            # The mesh is on disk and published; a tree that cannot be told
            # about it (no registered engine, no descriptor) is not a failure
            # of the run.
            recorded = None
        if recorded is not None:
            recorded['warning'] = not_clean
            recorded['warning_for'] = ['snappy.qa']
            recorded['warning_reasons'] = list(qa_warning)
        task_state = self._advance_pipeline_gates(session, command, recorded)
        # Plan 37 UF5. One pipeline run leaves only its finished mesh; that
        # is kept as the all-in-one stage, so it can be compared and a later
        # single-stage re-run knows the live mesh is not a stage input.
        if resumed or any(stage in keeps.values()
                          for stage in ('castellation', 'snap', 'layers')):
            # DP-1202. The split route kept each stage as it finished.
            # DP-1236: so did the run a resume carries on.
            stage_snapshot = stage_snapshots[-1] if stage_snapshots else None
        else:
            stage_snapshot = await self._capture_stage_snapshot(
                session, engine, 'snappyHexMesh')
            stage_snapshots.append(stage_snapshot)
        # DP-1236. A finished mesh leaves nothing to resume.
        self._clear_pipeline_resume(session.case_path)
        self._close_run_manifest(
            record, status='succeeded',
            publication={'status': 'published',
                         'artifact': str(session.case_path / 'constant'
                                         / 'polyMesh'),
                         'mesh_state': reference.to_dict()})
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('quality', 'exports'),
            warnings=tuple(seed_warnings) + launch_warnings,
            payload={'allocation': allocation.to_dict(), 'dag': dag.to_dict(),
                     **({'oversubscribed': True} if launch_warnings else {}),
                     **self._region_warnings_payload(seed_warnings),
                     'run_manifest': record.document if record else {},
                     'stage_snapshot': stage_snapshot,
                     'stage_snapshots': [kept for kept in stage_snapshots
                                         if kept is not None],
                     'executions': executions,
                     **({'resumed': resumed} if resumed else {}),
                     'qualification_mode': mode.value,
                     'checkpoints': checkpoints,
                     'mesh_state': reference.to_dict(),
                     'dictionary_manifest': manifest.to_dict(),
                     'dictionary_snapshot': str(snapshot),
                     'stage_state': str(stage_state),
                     'quality_report': quality_report,
                     'quality_verdict': quality_verdict,
                     'patch_identity': patch_identity_state,
                     'evidence': evidence_state,
                     'task_state': task_state})

    @staticmethod
    def _assembled_surface(session: CaseSession):
        """The immutable surface set as the solver will see it, or ``None``.

        A valid CFD enclosure is usually partitioned into separate wall, inlet
        and outlet artifacts, and no single one of them is watertight. Both the
        seed suggestion and the seed validation have to judge the assembled
        set, or the suggestion would offer a point the validation then refuses.
        """
        from foammesh.core.geometry import GeometryArtifactStore

        store = GeometryArtifactStore(session.case_path)
        entries = store.entries()
        if not entries:
            return None
        if len(entries) == 1:
            return store._polydata(entries[0])
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkCleanPolyData
        append = vtkAppendPolyData()
        for entry in entries:
            append.AddInputData(store._polydata(entry))
        clean = vtkCleanPolyData()
        clean.SetInputConnection(append.GetOutputPort())
        clean.Update()
        return clean.GetOutput()

    @staticmethod
    def _seed_candidates(bounds) -> list[tuple[float, float, float]]:
        """Bounding-box centre first, then a deterministic 20/50/80 lattice.

        The centre is right for the common case and is what the point widget
        already showed; the lattice catches an off-centre cavity while staying
        clear of the surface. Deterministic on purpose -- a suggestion that
        moved between two runs of the same case would be untrustworthy.
        """
        span = [(bounds[axis * 2], bounds[axis * 2 + 1]) for axis in range(3)]
        centre = tuple((low + high) / 2 for low, high in span)
        candidates = [centre]
        for fx in (.2, .5, .8):
            for fy in (.2, .5, .8):
                for fz in (.2, .5, .8):
                    point = tuple(
                        low + fraction * (high - low)
                        for fraction, (low, high) in zip((fx, fy, fz), span))
                    if point not in candidates:
                        candidates.append(point)
        return candidates

    def _geometry_fluid_seed_suggest(self, session: CaseSession,
                                     command: Command) -> OperationResult:
        """A point that is actually inside the geometry, for locationInMesh.

        Plan 28 WP6. The Region form defaulted to the bounding-box centre,
        which for anything but a convex blob is outside the fluid -- an elbow,
        an annulus, a duct with a bend. snappy then meshed the wrong side, or
        `_validate_fluid_seed` refused the run at launch with the user's only
        clue being that the point they were handed was wrong.

        The suggestion is judged against the same assembled surface the launch
        validation uses, so a point offered here cannot be refused there.
        Always a suggestion: the caller is free to move it.
        """
        from foammesh.core.mesh.sizing import validate_fluid_seed

        surface = self._assembled_surface(session)
        if surface is None:
            return self._read_result(session, command, {
                'point': None, 'inside': False, 'source': 'none',
                'reason': 'this case has no staged geometry to search'})
        # Plan 36 RP7: the deepest point of the largest enclosed space, when
        # the domain can be labelled; the lattice below is the fallback.
        delegated = self._suggest_from_fluid_spaces(session, surface)
        if delegated is not None:
            return self._read_result(session, command, delegated)
        bounds = surface.GetBounds()
        candidates = self._seed_candidates(bounds)
        centre = list(candidates[0])
        for candidate in candidates:
            try:
                probe = validate_fluid_seed(surface, candidate)
            except (ValueError, RuntimeError):              # noqa: PERF203
                continue
            if probe.get('valid'):
                return self._read_result(session, command, {
                    'point': [float(value) for value in candidate],
                    'inside': True,
                    'source': ('bounding_box_centre'
                               if candidate == candidates[0] else 'search'),
                    'distance_to_surface': probe.get('distance_to_surface'),
                    'reason': 'a point inside the assembled surface'})
        return self._read_result(session, command, {
            'point': centre, 'inside': False, 'source': 'bounding_box_centre',
            'reason': 'no interior point was found — the surface is probably '
                      'open, so place the seed yourself'})

    #: Seconds a synchronous suggestion waits for a detection that is not
    #: cached before it falls back to the lattice ("over budget", RP7 point
    #: 3). MEASURED: the annulus labels in 1.1 s at 0.9 M voxels.
    SUGGEST_DETECTION_SECONDS = 5.0

    def _suggest_from_fluid_spaces(self, session: CaseSession, surface):
        """The fluid-space suggestion payload, or ``None`` to fall back.

        The answer comes from the cache when a detection has run; otherwise
        the labelling runs on the VTK worker thread -- never on this one,
        which is the GUI thread in the desktop -- within a budget. The seed
        is then judged by the same probe the launch gate uses, so a point
        offered here cannot be refused there.
        """
        from foammesh.core.mesh import fluid_regions
        from foammesh.core.mesh.fluid_spaces import FluidSpacesCancelled
        from foammesh.core.mesh.sizing import validate_fluid_seed

        try:
            inputs = self._fluid_space_inputs(session)
        except (OSError, ValueError, RuntimeError):
            return None
        if inputs is None:
            return None
        surfaces, _geometry_bounds, box, base_cell = inputs
        options = self._fluid_space_options(session, base_cell)
        try:
            field = fluid_regions.cached_detection(surfaces, box, **options)
            if field is None:
                field = fluid_regions.detect_blocking(
                    surfaces, box, timeout=self.SUGGEST_DETECTION_SECONDS,
                    **options)
        except (TimeoutError, FluidSpacesCancelled, ValueError, RuntimeError,
                MemoryError):
            return None
        faces = self._mesh_faces(session, _geometry_bounds)
        finest = self._finest_cell(session, base_cell)
        for space in field.enclosed:
            # RP13 #4: too thin for the finest cell the case refines to.
            if fluid_regions.too_thin(space, finest):
                continue
            # RP13 #2 / DP-862: never offered on a face of the mesh.
            point = self._off_faces(faces, field, space.seed, space.id)
            try:
                probe = validate_fluid_seed(surface, point)
                if not probe.get('valid') and not probe.get('on_surface'):
                    # A nested multiregion assembly reads an inner space as
                    # outside by ray parity (DP-391); a component decides.
                    components = [validate_fluid_seed(part, point)
                                  for part in surfaces]
                    valid = [item for item in components if item.get('valid')]
                    if valid and not any(item.get('on_surface')
                                         for item in components):
                        probe = valid[0]
            except (ValueError, RuntimeError):
                return None
            if not probe.get('valid'):
                return None
            return {'point': point, 'inside': True, 'source': 'fluid_spaces',
                    'distance_to_surface': probe.get('distance_to_surface'),
                    'reason': 'the deepest point of the largest enclosed space',
                    'space': {'id': int(space.id),
                              'volume': float(space.volume),
                              'depth': float(space.depth)}}
        return None

    def _geometry_fluid_seed_check(self, session: CaseSession,
                                   command: Command) -> OperationResult:
        """Is this point inside the geometry the mesher will see? (R164)

        The Region page accepted any point as the material point with no test
        that it lies inside the geometry: on annulus.stl the default fell
        outside the annular gap, and the only feedback was a meshing error
        several tasks later. This asks the same question
        `_validate_fluid_seed` asks at launch, against the same assembled
        surface, so a point this call passes cannot be refused there.

        A case with no staged geometry answers `known: False` rather than
        `inside: False` -- there is nothing to be outside of, and a page must
        not warn about a point it cannot judge.
        """
        from foammesh.core.mesh.sizing import validate_fluid_seed

        raw = command.parameters.get('point')
        try:
            point = [float(value) for value in raw]
        except (TypeError, ValueError):
            raise ValidationFailedError(
                'point must be three finite coordinates') from None

        surface = self._assembled_surface(session)
        if surface is None:
            return self._read_result(session, command, {
                'known': False, 'inside': False, 'point': point,
                'reason': 'this case has no staged geometry to test against'})
        try:
            probe = validate_fluid_seed(surface, point)
        except (ValueError, RuntimeError) as error:
            raise ValidationFailedError(str(error)) from error
        # DP-574: outside the body but inside the background box is an
        # external-flow seed, which the launch gate accepts, so the page
        # must not call it invalid.
        external = False
        # RP13 #5: in the hull of an L-shaped domain but off its blocks there
        # are no background cells -- neither inside nor external flow.
        outside_domain = self._outside_domain_shape(session, point, surface)
        if (not probe.get('valid') and not probe.get('on_surface')
                and not outside_domain):
            domain = self._background_domain_bounds(session.case_path)
            external = bool(domain is not None
                            and self._point_inside_bounds(point, domain))
        return self._read_result(session, command, dict(
            probe, known=True, point=point, external=external,
            outside_domain=outside_domain))

    @staticmethod
    def _domain_shape(session: CaseSession, surface):
        """RP13 #5: the `DomainBox` of a domain that is not a cuboid, or ``None``."""
        from foammesh.core.mesh import fluid_regions

        try:
            bounds = surface.GetBounds() if surface is not None else None
            box = fluid_regions.detection_box(
                getattr(session.state, 'db', None), session.case_path, bounds)
        except Exception:  # noqa: BLE001 - an unreadable domain is a box
            return None
        return getattr(box, 'domain', None)

    def _outside_domain_shape(self, session: CaseSession, point,
                              surface) -> bool:
        """True when *point* is in the domain's hull but off every block."""
        shape = self._domain_shape(session, surface)
        if shape is None:
            return False
        bounds = shape.bounds
        inside_hull = all(bounds[2 * axis] <= float(point[axis])
                          <= bounds[2 * axis + 1] for axis in range(3))
        return inside_hull and not shape.contains(point)

    # -- Plan 36 RP7: fluid regions from the labelled domain ---------------- #

    @staticmethod
    def _fluid_space_inputs(session: CaseSession):
        """``(surfaces, geometry_bounds, box, base_cell)``, or ``None``.

        The surfaces are the staged artifacts -- the set the launch gate
        judges seeds against -- and the box is the background mesh's (RP1).
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.mesh import fluid_regions

        store = GeometryArtifactStore(session.case_path)
        surfaces = [store._polydata(entry) for entry in store.entries()]
        surfaces = [surface for surface in surfaces
                    if surface is not None and surface.GetNumberOfCells()]
        geometry_bounds = fluid_regions.union_bounds(surfaces)
        if geometry_bounds is None:
            return None
        db = getattr(session.state, 'db', None)
        box = fluid_regions.detection_box(
            db, session.case_path, geometry_bounds)
        if box is None:
            return None
        return (surfaces, geometry_bounds, box,
                fluid_regions.base_cell_size(db, box))

    @staticmethod
    def _fluid_space_options(session: CaseSession, base_cell, resolution=None):
        from foammesh.core.mesh import fluid_regions

        options = {'cache_dir': fluid_regions.cache_dir(session.case_path)}
        if resolution is not None:
            options['h'] = float(resolution)
        else:
            options['base_cell'] = base_cell
        return options

    @staticmethod
    def _detection_parameters(command: Command):
        parameters = command.parameters
        try:
            count = int(parameters.get('count', 1))
        except (TypeError, ValueError):
            raise ValidationFailedError('count must be a whole number') from None
        if count < 1:
            raise ValidationFailedError('count must be at least 1',
                                        details={'count': count})
        external = bool(parameters.get('external', False))
        resolution = parameters.get('resolution')
        if resolution is not None:
            try:
                resolution = float(resolution)
            except (TypeError, ValueError):
                raise ValidationFailedError(
                    'resolution must be a length in metres') from None
            if not (resolution > 0 and resolution < float('inf')):
                raise ValidationFailedError(
                    'resolution must be a positive length',
                    details={'resolution': resolution})
        return count, external, resolution

    async def _detect_fluid_spaces(self, session: CaseSession, command: Command,
                                   inputs, resolution=None):
        """Label the domain on the VTK worker thread, as a cancellable job."""
        import asyncio
        from foammesh.core.geometry.diagnostics import budget as budget_module
        from foammesh.core.mesh import fluid_regions
        from foammesh.core.mesh.fluid_spaces import FluidSpacesCancelled
        from foammesh.support.vtk_threads import vtk_run_in_thread

        surfaces, _geometry_bounds, box, base_cell = inputs
        job_id = str(command.parameters.get('job_id') or
                     f'fluid-regions-{uuid.uuid4().hex[:12]}')
        loop = asyncio.get_running_loop()
        bus = session.state.bus

        def progress(stage, fraction):
            # Called on the worker thread; the bus belongs to the loop.
            loop.call_soon_threadsafe(lambda: bus.publish(
                Event.JOB_PROGRESS, job_id=job_id, name=command.operation,
                stage=stage, fraction=fraction,
                message=f'detecting fluid spaces: {stage}'))

        budget = budget_module.budget_from_settings(command.operation)
        budget_module.register(job_id, budget)
        bus.publish(Event.JOB_STARTED, job_id=job_id, name=command.operation,
                    argv=[], cwd=str(session.case_path), mutation=False,
                    environment_fingerprint='in-process',
                    message='detecting fluid spaces')
        try:
            field = await vtk_run_in_thread(
                fluid_regions.run_detection, surfaces, box,
                cancelled=lambda: bool(budget.cancelled), progress=progress,
                **self._fluid_space_options(session, base_cell, resolution))
        except FluidSpacesCancelled as error:
            bus.publish(Event.JOB_CANCELLED, job_id=job_id,
                        name=command.operation)
            raise PreconditionFailedError(
                'fluid-space detection was cancelled',
                details={'error': 'detection_cancelled',
                         'job_id': job_id}) from error
        except (ValueError, RuntimeError, MemoryError) as error:
            bus.publish(Event.JOB_FAILED, job_id=job_id,
                        name=command.operation, error=str(error))
            raise PreconditionFailedError(
                f'fluid-space detection failed: {error}',
                details={'error': 'detection_failed'}) from error
        finally:
            budget_module.unregister(job_id)
        bus.publish(Event.JOB_FINISHED, job_id=job_id, name=command.operation,
                    returncode=0)
        return field

    async def _geometry_fluid_regions_detect(self, session: CaseSession,
                                             command: Command) -> OperationResult:
        """How many fluid regions? Label the domain and propose ``count``.

        Plan 36 RP7. The result is ``spaces`` (id, volume, depth, seed,
        outside, too_thin, bounds), ``proposed`` (``count`` ids, the outside
        first when ``external``), ``found_enclosed``, ``mismatch`` (``null``
        or ``{asked, found, reason}``), ``h``, ``voxels`` and ``elapsed``.
        The field is cached beside the case, so asking again -- from the GUI
        or the CLI -- answers from the file.

        RP13 #7: every answer carries ``case_id`` and ``request_id`` (the
        caller's, or the command's id), so a caller drops one that arrives
        after its editor or case closed, or after a newer request.
        """
        from foammesh.core.mesh import cad_solids, fluid_regions

        count, external, resolution = self._detection_parameters(command)
        tags = self._detection_tags(session, command)
        # Plan 36 RP11. On Gmsh every solid is a region, so the answer is the
        # solids (`source: 'solids'`, `seed: null`, each row carrying the
        # `region_uuid` apply takes). Only a Gmsh case with no closed solid
        # labels the domain like snappy, and says so.
        tail = {'source': 'fluid_spaces'}
        if self._regions_are_solids(session):
            found, typing = await self._gmsh_solids(session)
            if found.solids:
                # DP-915: with the far field on (box, sphere or cylinder), the
                # cut subtracts every solid; the fluid is the space around
                # them, not a solid.
                payload = cad_solids.propose(
                    found, count, typing, external=external,
                    farfield=cad_solids.farfield_enabled(session.state.db))
                # RP13 #3: the solids answered are kept under an id an apply
                # by ids must name, fingerprinted by the solid topology.
                payload.update(self._keep_detection(
                    session, source=detection_record.SOLIDS,
                    fingerprint=detection_record.solid_fingerprint(found),
                    external=external, spaces=payload['spaces']))
                return self._read_result(session, command,
                                         {**payload, **tags})
            tail.update({'fallback': 'no_closed_solids',
                         'open_regions': list(found.open_regions),
                         'not_solids': list(found.not_solids)})
        inputs = self._fluid_space_inputs(session)
        if inputs is None:
            return self._read_result(
                session, command,
                {**fluid_regions.empty_proposal(count), **tail,
                 'detection_id': None, **tags})
        field = await self._detect_fluid_spaces(
            session, command, inputs, resolution)
        _surfaces, geometry_bounds, box, base_cell = inputs
        payload = {**fluid_regions.propose(
            field, count, external=external, base_cell=base_cell,
            geometry_bounds=geometry_bounds, box=box,
            finest=self._finest_cell(session, base_cell)), **tail}
        # DP-1111: with a farfield, the outside seed is moved from the
        # block's corner (the ring snappy discards) to inside the farfield.
        self._seeds_inside_farfield(session, field, payload['spaces'],
                                    geometry_bounds)
        # RP13 #2 / DP-862: a voxel centre can sit exactly on a base-grid
        # face; every seed detect offers is moved off the mesh's faces.
        self._seeds_off_faces(session, field, payload['spaces'],
                              geometry_bounds)
        # RP13 #3: the labelled answer is kept under an id an apply by ids
        # must name, fingerprinted by the surfaces' content and the box.
        payload.update(self._keep_detection(
            session, source=detection_record.VOXELS,
            fingerprint=detection_record.voxel_fingerprint(
                field.surface_key, box),
            box=box, h=field.h, external=external, field_key=field.key,
            spaces=payload['spaces']))
        # Plan 37 #1 / #2: what the proposal would mesh that may surprise --
        # the outside on a box flush with the geometry, both sides of one
        # closed surface, or the outside shell of a duct.
        # The key is there only when there is something to say.
        placement = self._proposal_placement(
            session, field, payload, inputs) if external else []
        if placement:
            payload['placement_warnings'] = placement
        return self._read_result(session, command, {**payload, **tags})

    def _proposal_placement(self, session: CaseSession, field, payload,
                            inputs) -> list[dict]:
        """The placement warnings for the spaces a detection proposes."""
        rows = {int(row['id']): row for row in payload.get('spaces') or ()}
        seeds = [(f'space {space_id}', 'fluid', rows[space_id]['seed'])
                 for space_id in payload.get('proposed') or ()
                 if space_id in rows]
        return self._placement_warnings(session, field, seeds, inputs,
                                        external=True)

    @staticmethod
    def _detection_tags(session: CaseSession, command: Command) -> dict:
        """RP13 #7: the case and request a detection answers."""
        request = (command.parameters or {}).get('request_id')
        return {'case_id': str(session.case_id),
                'request_id': str(request or command.command_id)}

    @staticmethod
    def _finest_cell(session: CaseSession, base_cell):
        """base / 2^maxLevel: what a thin space is judged against (RP13 #4)."""
        from foammesh.core.mesh import fluid_regions
        from foammesh.core.mesh.face_clearance import max_refinement_level

        try:
            level = max_refinement_level(getattr(session.state, 'db', None))
        except Exception:  # noqa: BLE001 - no levels: the base cell stands
            level = 0
        return fluid_regions.finest_cell(base_cell, level)

    @staticmethod
    def _mesh_faces(session: CaseSession, geometry_bounds):
        """``(grid, max_level)`` of the mesh the case will cut, RP13 #2."""
        from foammesh.core.mesh.face_clearance import (
            background_grid, max_refinement_level,
        )

        db = getattr(session.state, 'db', None)
        try:
            return (background_grid(db, geometry_bounds),
                    max_refinement_level(db))
        except Exception:  # noqa: BLE001 - no grid: nothing to keep off
            return None, 0

    @staticmethod
    def _off_faces(faces, field, seed, space_id) -> list:
        """*seed* moved off every mesh face, if it stays in its space.

        RP13 #2 / DP-862. The move is a third of the finest cell; a space
        too thin to take it keeps the voxel centre, which is then the
        launch gate's to judge.
        """
        from foammesh.core.mesh.face_clearance import nudge_off_faces

        grid, level = faces
        point = tuple(float(value) for value in seed)
        moved = nudge_off_faces(point, grid, level)
        if moved != point and field is not None:
            found = field.space_at(moved)
            if found is None or int(found.label) != int(space_id):
                moved = point
        return [float(value) for value in moved]

    @staticmethod
    def _seeds_inside_farfield(session: CaseSession, field, rows,
                               geometry_bounds) -> None:
        """DP-1111: an outside space's seed, moved inside the farfield.

        The outside space is seeded at its deepest voxel, a corner of the
        background block; with a farfield that corner is in the ring the
        launch gate refuses (`_farfield_seed_gate`). No farfield, or no
        voxel of the space inside it: the rows are left as they are, and the
        gate names the seed.
        """
        from foammesh.core.mesh import snappy_farfield

        if field is None or not any(row.get('outside') for row in rows):
            return
        db = getattr(session.state, 'db', None)
        try:
            spec = snappy_farfield.active(db) if db is not None else None
            farfield = (snappy_farfield.resolve(spec, geometry_bounds)
                        if spec is not None else None)
        except Exception:  # noqa: BLE001 - the launch gate refuses the spec
            return
        if farfield is None:
            return
        for row in rows:
            if not row.get('outside') or row.get('seed') is None:
                continue
            if farfield.contains(row['seed']) == snappy_farfield.INSIDE:
                continue
            moved = snappy_farfield.seed_inside(
                farfield.primitive, field, row['id'], geometry_bounds)
            if moved is not None:
                row['seed'] = moved

    def _seeds_off_faces(self, session: CaseSession, field, rows,
                         geometry_bounds) -> None:
        """Every detected row's seed, moved off the mesh faces (DP-862)."""
        faces = self._mesh_faces(session, geometry_bounds)
        if faces[0] is None:
            return
        for row in rows:
            if row.get('seed') is not None:
                row['seed'] = self._off_faces(faces, field, row['seed'],
                                              row['id'])

    @staticmethod
    def _keep_detection(session: CaseSession, **record) -> dict:
        """RP13 #3: keep a detection beside the cache; its id for the payload."""
        from foammesh.core.mesh import fluid_regions

        kept = detection_record.write(
            fluid_regions.cache_dir(session.case_path),
            case_id=session.case_id, **record)
        return {'detection_id': kept['detection_id']}

    async def _kept_detection(self, session: CaseSession, parameters,
                              current_fingerprint, result_present=None):
        """The record an apply by ids names, if it still describes the case.

        RP13 #3. *current_fingerprint* is awaited only once a record is
        found; a stale or unknown id is refused as ``detection_stale``.
        """
        from foammesh.core.mesh import fluid_regions

        identifier = str(parameters.get('detection_id') or '').strip()
        if not identifier:
            raise ValidationFailedError(
                'an apply by ids must name the detection it applies: give '
                'the detection_id detect answered',
                details={'error': detection_record.DETECTION_ID_REQUIRED})
        cache = fluid_regions.cache_dir(session.case_path)
        record = detection_record.read(cache, identifier)
        fingerprint = None
        if record is not None:
            fingerprint = await current_fingerprint(record)
        try:
            return detection_record.check(
                cache, identifier, case_id=session.case_id,
                fingerprint=fingerprint, result_present=result_present)
        except detection_record.DetectionStale as stale:
            raise PreconditionFailedError(
                str(stale), details={
                    'error': detection_record.DETECTION_STALE,
                    'reason': stale.reason,
                    'detection_id': identifier}) from None

    #: Where the offer of the fluid-region question is noted (D7). The cache
    #: folder is outside the case fingerprint, so noting it is not an edit.
    FLUID_REGIONS_OFFERED = 'region_detection_offered'

    def _geometry_fluid_regions_offer(self, session: CaseSession,
                                      command: Command) -> OperationResult:
        """Should "How many fluid regions?" open by itself? (Plan 36 D7.)

        Once per case: while the case has no regions and at least one staged
        surface is closed -- an open surface has no enclosed space to find,
        and the Detect button stays for everything else. ``record`` notes the
        offer when the answer is yes, so the next visit does not ask again.
        The note is a marker in the case's cache folder, beside the detection
        field: not configuration, not history, not an external edit.
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.mesh import fluid_regions

        marker = (fluid_regions.cache_dir(session.case_path)
                  / self.FLUID_REGIONS_OFFERED)
        db = getattr(session.state, 'db', None)
        try:
            regions = db.getElements('region') if db is not None else {}
        except Exception:  # noqa: BLE001 - a case without the section
            regions = {}
        entries = GeometryArtifactStore(session.case_path).entries()
        if marker.exists():
            reason = 'offered'
        elif getattr(session, 'read_only', False):
            reason = 'read_only'
        elif regions:
            reason = 'has_regions'
        elif not entries:
            reason = 'no_surface'
        elif not any((entry.get('diagnostics') or {}).get('watertight')
                     for entry in entries):
            reason = 'open_surface'
        else:
            reason = None
        offer = reason is None
        if offer and command.parameters.get('record'):
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text('offered\n', encoding='utf-8')
            except OSError:
                # Not noting it means it may open once more; never an error.
                pass
        return self._read_result(session, command, {
            'offer': offer, 'reason': reason or 'first_visit'})

    def _geometry_fluid_regions_seeds(self, session: CaseSession,
                                      command: Command) -> OperationResult:
        """Which space each region's seed is in (Plan 36 RP8, the table).

        Read from the cached labelling only -- the one detection or a launch
        left beside the case -- so it is cheap on the GUI thread and never
        starts a labelling. ``labelled`` is ``False`` without one. Each
        region (by key) gets ``name``, ``type``, ``space`` (``None`` on a
        wall or off the box), ``volume``, ``outside``, ``same_as`` (the
        first region, in id order, already seeded in that space) and
        ``clash`` (a Fluid and a Solid seed share the space).
        """
        from foammesh.core.mesh import fluid_regions

        empty = {'labelled': False, 'regions': {}}
        db = getattr(session.state, 'db', None)
        try:
            regions = dict(db.getElements('region') or {}) if db else {}
        except Exception:  # noqa: BLE001 - a case without the section
            regions = {}
        if not regions:
            return self._read_result(session, command, empty)
        inputs = self._fluid_space_inputs(session)
        if inputs is None:
            return self._read_result(session, command, empty)
        surfaces, _geometry_bounds, box, base_cell = inputs
        field = fluid_regions.cached_detection(
            surfaces, box, **self._fluid_space_options(session, base_cell))
        if field is None:
            return self._read_result(session, command, empty)

        def order(key):
            text = str(key)
            return (0, int(text), '') if text.isdigit() else (1, 0, text)

        rows, members = {}, {}
        for key in sorted(regions, key=order):
            region = regions[key]
            row = {'name': self._region_label(key, region),
                   'type': self._region_type(region), 'space': None,
                   'volume': None, 'outside': False, 'same_as': None,
                   'clash': False}
            try:
                point = tuple(float(value) for value in region.vector('point'))
                found = field.space_at(point)
            except Exception:  # noqa: BLE001 - an unreadable seed is no space
                found = None
            if found is not None and int(found.label) != 0:
                space_id = int(found.label)
                space = field.space(space_id)
                row['space'] = space_id
                if space is not None:
                    row['volume'] = float(space.volume)
                    row['outside'] = bool(space.outside)
                group = members.setdefault(space_id, [])
                if group:
                    row['same_as'] = rows[group[0]]['name']
                group.append(str(key))
            rows[str(key)] = row
        for group in members.values():
            if len({rows[key]['type'] for key in group}) > 1:
                for key in group:
                    rows[key]['clash'] = True
        # Plan 37 #1 / #2: the page says what the launch will warn about.
        seeds = []
        for key in sorted(regions, key=order):
            try:
                point = [float(value) for value in regions[key].vector('point')]
            except Exception:  # noqa: BLE001 - an unreadable seed is no space
                continue
            seeds.append((rows[str(key)]['name'], rows[str(key)]['type'],
                          point))
        return self._read_result(session, command, {
            'labelled': True, 'regions': rows,
            'warnings': self._placement_warnings(
                session, field, seeds, inputs)})

    async def _geometry_fluid_regions_apply(self, session: CaseSession,
                                            command: Command) -> OperationResult:
        """Write the accepted regions in one transaction: one undo removes all.

        ``regions`` is ``[{name?, type?, point}]`` -- what the GUI's Accept
        all sends after the user renamed or retyped a row. ``ids`` picks
        spaces of a detection instead, which is the CLI's second call; it
        must name the ``detection_id`` detect answered, and is refused as
        ``detection_stale`` when the geometry or the box changed since, or
        the labelled result is gone (RP13 #3).

        The regions are added to the ones the case has; a new seed in a
        space an existing region already holds is refused, naming it.
        ``replace`` first drops the existing regions of the types applied.
        """
        from foammesh.core.facade.facade import _source as commit_source
        from foammesh.core.facade.field_adapters import build_entity_adapters
        from foammesh.core.facade.fields import REGISTRY
        from foammesh.db.configurations_schema import RegionType

        session.require_writable()
        if self._regions_are_solids(session):
            # Plan 36 RP11: Gmsh reads no seed; its regions are the solids.
            return await self._apply_gmsh_solids(session, command)
        parameters = command.parameters
        default_type = str(parameters.get('type') or RegionType.FLUID.value)
        allowed = {item.value for item in RegionType}
        if default_type not in allowed:
            raise ValidationFailedError(
                'type must be one of: ' + ', '.join(sorted(allowed)))
        rows = parameters.get('regions')
        if rows is None:
            ids = parameters.get('ids')
            if isinstance(ids, str):
                ids = [part for part in ids.split(',') if part.strip()]
            try:
                ids = [int(value) for value in ids or ()]
            except (TypeError, ValueError):
                raise ValidationFailedError(
                    'ids must be whole numbers') from None
            if not ids:
                raise ValidationFailedError(
                    'give the regions to write, or the ids of detected spaces')
            field, rows = await self._detected_seeds(session, parameters, ids)
        elif parameters.get('detection_id'):
            # The GUI names the detection its rows came from: refused when
            # the geometry or the box changed while the review was open.
            _record, field = await self._voxel_detection(session, parameters)
        else:
            field = self._labelled_field(session, command)
        if not isinstance(rows, list) or not rows:
            raise ValidationFailedError('regions must be a non-empty list')

        adapter = build_entity_adapters(REGISTRY.collections)['regions.items']
        storage = adapter.storage_path
        data = session.state.checkout()
        if parameters.get('replace'):
            # RP13 #3: replace the regions of the types applied, not all.
            applied = {str(row.get('type') or default_type).lower()
                       for row in rows if isinstance(row, Mapping)}
            for key, element in list(
                    (data.getElements(storage) or {}).items()):
                if self._region_type(element) in applied:
                    data.removeElement(storage, key)
        held = self._held_spaces(data, storage, field)
        taken = set()
        for element in (data.getElements(storage) or {}).values():
            try:
                taken.add(str(element.value('name')))
            except Exception:  # noqa: BLE001 - a row without a name
                pass
        written = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise ValidationFailedError('each region must be an object')
            try:
                point = [float(value) for value in row.get('point')]
            except (TypeError, ValueError):
                point = []
            if len(point) != 3 or not all(
                    value == value and abs(value) != float('inf')
                    for value in point):
                raise ValidationFailedError(
                    'point must be three finite coordinates',
                    details={'region': index})
            kind = str(row.get('type') or default_type)
            name = str(row.get('name') or '').strip()
            if not name:
                # DP-868: an unnamed region is named after its type.
                prefix = ('solid' if kind.strip().lower() == 'solid'
                          else 'fluid')
                number = len(taken) + 1
                while f'{prefix}_{number}' in taken:
                    number += 1
                name = f'{prefix}_{number}'
            if name in taken:
                raise ValidationFailedError(
                    'a region with that name already exists',
                    details={'name': name})
            taken.add(name)
            holder = self._space_holder(field, held, point)
            if holder is not None:
                # RP13 #3: one space, one seed; appending never doubles it.
                raise ValidationFailedError(
                    f'region "{holder[1]}" already holds that space: '
                    'replace it, or pick another space',
                    details={'error': 'space_taken', 'region': index,
                             'space': holder[0], 'held_by': holder[1]})
            normalized = adapter.normalize_patch({
                'name': name, 'type': kind, 'point.x': point[0],
                'point.y': point[1], 'point.z': point[2]})
            key, _element = data.addNewElement(storage)
            for relative_path, value in normalized.items():
                data.setValue(f'{storage}/{key}/{relative_path}', value,
                              relative_path)
            written.append((key, name, kind, point, row.get('space')))
        from .facade import refuse_locked_edit
        refuse_locked_edit(session, data)       # Plan 37 UF5 DP-1063
        transaction = session.state.commit(
            data, action='create fluid regions',
            source=commit_source(command.source), target='regions.items',
            reason=f'actor={command.actor.id}')
        regions = [{'id': str(data.remappedKey(storage, key)), 'name': name,
                    'type': kind, 'point': point,
                    **({} if space is None else {'space': int(space)})}
                   for key, name, kind, point, space in written]
        changed = tuple(f'regions.items/{row["id"]}' for row in regions)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            changed_fields=changed,
            invalidated_outputs=('mesh.base_grid', 'quality'),
            payload={'regions': regions,
                     'transaction_id': getattr(transaction, 'tx_id', None)})

    # -- Plan 36 RP13 #3: which detection an apply applies ------------------ #

    async def _detected_seeds(self, session: CaseSession, parameters, ids):
        """``(field, rows)`` for the ids of a kept, still-current detection."""
        record, field = await self._voxel_detection(session, parameters)
        spaces = {int(row['id']): row for row in record.get('spaces') or ()}
        rows = []
        for space_id in ids:
            row = spaces.get(space_id)
            if row is None or row.get('seed') is None:
                raise ValidationFailedError(
                    'no detected space has that id',
                    details={'id': space_id, 'spaces': sorted(spaces)})
            rows.append({'point': list(row['seed']), 'space': space_id})
        return field, rows

    async def _voxel_detection(self, session: CaseSession, parameters):
        """``(record, field)`` of the named detection, if still current."""
        from foammesh.core.mesh import fluid_regions
        from foammesh.core.mesh.fluid_spaces import _cached
        from foammesh.support.vtk_threads import vtk_run_in_thread

        inputs = self._fluid_space_inputs(session)
        if inputs is None:
            raise PreconditionFailedError(
                'this case has no staged geometry to detect spaces in')
        surfaces, _geometry_bounds, box, _base_cell = inputs
        cache = fluid_regions.cache_dir(session.case_path)

        async def current(_record):
            key = await vtk_run_in_thread(fluid_regions.surface_key, surfaces)
            return detection_record.voxel_fingerprint(key, box)

        def present(record):
            key = record.get('field_key')
            return bool(key) and _cached(key, cache) is not None

        record = await self._kept_detection(session, parameters, current,
                                            present)
        if record.get('source') != detection_record.VOXELS:
            raise PreconditionFailedError(
                'that detection answered solids, not labelled spaces; '
                'detect again', details={
                    'error': detection_record.DETECTION_STALE,
                    'reason': detection_record.UNKNOWN,
                    'detection_id': record.get('detection_id')})
        return record, _cached(record['field_key'], cache)

    def _labelled_field(self, session: CaseSession, command: Command):
        """The cached field for the case as it is, or ``None``: never labels."""
        from foammesh.core.mesh import fluid_regions

        inputs = self._fluid_space_inputs(session)
        if inputs is None:
            return None
        surfaces, _geometry_bounds, box, base_cell = inputs
        resolution = command.parameters.get('resolution')
        try:
            resolution = None if resolution is None else float(resolution)
        except (TypeError, ValueError):
            resolution = None
        return fluid_regions.cached_detection(
            surfaces, box,
            **self._fluid_space_options(session, base_cell, resolution))

    def _held_spaces(self, data, storage, field) -> dict:
        """``space id -> region name`` for the regions *data* already has."""
        held: dict = {}
        if field is None:
            return held
        for key, element in (data.getElements(storage) or {}).items():
            try:
                point = tuple(float(value)
                              for value in element.vector('point'))
                found = field.space_at(point)
            except Exception:  # noqa: BLE001 - an unreadable seed holds none
                continue
            if found is not None and int(found.label) != 0:
                held.setdefault(int(found.label),
                                self._region_label(key, element))
        return held

    @staticmethod
    def _space_holder(field, held, point):
        """``(space, region name)`` when *point* is in a held space."""
        if field is None or not held:
            return None
        found = field.space_at(tuple(point))
        if found is None or int(found.label) == 0:
            return None
        name = held.get(int(found.label))
        return None if name is None else (int(found.label), name)

    # -- Plan 36 RP11: Gmsh regions are the solids -------------------------- #

    def _regions_are_solids(self, session: CaseSession) -> bool:
        """Whether the case's engine meshes each solid as its own region."""
        from foammesh.core.engine.registry import ENGINE_REGISTRY

        engine_id = self._engine_id(session)
        if engine_id not in ENGINE_REGISTRY.ids():
            return False
        return bool(getattr(ENGINE_REGISTRY.get(engine_id),
                            'regions_are_solids', False))

    @staticmethod
    def _gmsh_farfield_shape(db) -> str:
        """``box``, ``sphere`` or ``cylinder``: the Gmsh far field's shape.

        Plan 37 UF13 made the far field a box, a sphere or a cylinder; a
        message that said "the far-field box" for the other two named a
        shape the case does not have. Unset or unreadable is the box, the
        shape the runner builds by default.
        """
        try:
            value = db.getValue('gmsh/farfield/shape')
        except Exception:  # noqa: BLE001 - a case without the leaf
            value = None
        shape = str(getattr(value, 'value', value) or '').strip().lower()
        return shape if shape in ('box', 'sphere', 'cylinder') else 'box'

    @staticmethod
    def _gmsh_typing(db) -> dict:
        """``region_uuid`` -> the typing the Gmsh volume controls hold."""
        from foammesh.core.mesh.cad_solids import typing_of

        try:
            rows = dict(db.getElements('gmsh/volumeControls') or {})
        except Exception:  # noqa: BLE001 - a case without the list
            rows = {}
        return typing_of(rows)

    async def _gmsh_solids(self, session: CaseSession):
        """``(CaseSolids, typing)``, measured on the VTK worker thread."""
        from foammesh.core.mesh import cad_solids
        from foammesh.support.vtk_threads import vtk_run_in_thread

        found = await vtk_run_in_thread(cad_solids.case_solids,
                                        session.case_path)
        return found, self._gmsh_typing(session.state.db)

    async def _apply_gmsh_solids(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        """Type the accepted solids, in one transaction: one undo reverts all.

        Plan 36 RP11. Gmsh meshes the solids the geometry has, so accepting a
        candidate writes no point: it types the solid, keyed by its
        ``region_uuid`` -- the identity the import minted and every Gmsh
        volume control scopes to -- never by its position in a list.

        ``solids`` is ``[{region_uuid, name?, type?}]`` (type ``fluid``,
        ``solid`` or ``excluded``); ``ids`` picks solids of the detection
        instead (its ``spaces[].id``, largest first), and must name that
        detection's ``detection_id`` (RP13 #3). Each solid's volume
        control -- the first row scoping it, or a new one -- gets
        ``volumeType``; ``excluded`` clears ``included``. An exclusion already
        there is kept: accepting a candidate never re-includes a solid the
        user took out (it is listed in ``kept_excluded``). A ``name`` renames
        the control and the tree's volume row for that solid, which is the
        name the cell zone publishes under (DP-419).
        """
        from foammesh.core.facade.facade import _source as commit_source
        from foammesh.core.facade.field_adapters import build_entity_adapters
        from foammesh.core.facade.fields import REGISTRY
        from foammesh.core.mesh import cad_solids

        parameters = command.parameters
        if parameters.get('regions') is not None:
            raise ValidationFailedError(
                'Gmsh reads no seed point: its regions are the solids. Give '
                '`solids` (by region_uuid) or the `ids` detection answered.',
                details={'engine': 'gmsh', 'error': 'gmsh_takes_solids'})
        if parameters.get('replace'):
            raise ValidationFailedError(
                'replace does not apply on Gmsh: a solid cannot be removed '
                'by typing; exclude it instead.',
                details={'engine': 'gmsh'})
        default_type = str(parameters.get('type') or cad_solids.FLUID)
        if default_type not in cad_solids.TYPES:
            raise ValidationFailedError(
                'type must be one of: ' + ', '.join(cad_solids.TYPES))
        found, _typing = await self._gmsh_solids(session)
        if not found.solids:
            raise PreconditionFailedError(
                'this Gmsh case has no closed solid to type: Gmsh does not '
                'read seed points, so a labelled space cannot become a '
                'region here',
                details={'error': 'no_closed_solids',
                         'open_regions': list(found.open_regions),
                         'not_solids': list(found.not_solids)})
        if cad_solids.farfield_cuts(
                found, cad_solids.farfield_enabled(session.state.db)):
            # DP-915: the far-field cut consumes every imported solid, so a
            # control scoped to one is refused by the runner. Refuse here.
            # Plan 37 UF13: the far field is a box, a sphere or a cylinder,
            # and the sentence names the one the case has.
            names = [solid.name for solid in found.solids]
            shape = self._gmsh_farfield_shape(session.state.db)
            raise PreconditionFailedError(
                f'the far-field {shape} is on, so the run cuts '
                + ', '.join(f"'{name}'" for name in names)
                + f' out of the {shape}: '
                + ('that solid is' if len(names) == 1 else 'those solids are')
                + ' the obstacle, and the fluid is the space around '
                + ('it' if len(names) == 1 else 'them')
                + ', which the run builds itself. There is no solid to type; '
                  f'turn the far-field {shape} off to mesh the solids '
                  'instead.',
                details={'error': 'farfield_cuts_solids', 'engine': 'gmsh',
                         'shape': shape,
                         'solids': [solid.region_uuid
                                    for solid in found.solids],
                         'names': names})
        by_uuid = found.by_uuid()
        rows = parameters.get('solids')
        if rows is None:
            ids = parameters.get('ids')
            if isinstance(ids, str):
                ids = [part for part in ids.split(',') if part.strip()]
            try:
                ids = [int(value) for value in ids or ()]
            except (TypeError, ValueError):
                raise ValidationFailedError(
                    'ids must be whole numbers') from None
            if not ids:
                raise ValidationFailedError(
                    'give the solids to type, or the ids detection answered')

            async def current(_record):
                return detection_record.solid_fingerprint(found)

            # RP13 #3: the ids are the numbers of one detection, bound to the
            # solid topology it answered from.
            record = await self._kept_detection(session, parameters, current)
            if record.get('source') != detection_record.SOLIDS:
                raise PreconditionFailedError(
                    'that detection answered labelled spaces, not solids; '
                    'detect again', details={
                        'error': detection_record.DETECTION_STALE,
                        'reason': detection_record.UNKNOWN,
                        'detection_id': record.get('detection_id')})
            spaces = {int(space['id']): space
                      for space in record.get('spaces') or ()
                      if space.get('region_uuid')}
            rows = []
            for space_id in ids:
                if space_id not in spaces:
                    raise ValidationFailedError(
                        'no detected solid has that id',
                        details={'id': space_id, 'spaces': sorted(spaces)})
                rows.append({'region_uuid': spaces[space_id]['region_uuid'],
                             'space': space_id})
        if not isinstance(rows, list) or not rows:
            raise ValidationFailedError('solids must be a non-empty list')

        adapter = build_entity_adapters(REGISTRY.collections)[
            'gmsh.volume_controls.controls']
        storage = adapter.storage_path
        data = session.state.checkout()
        typing = self._gmsh_typing(data)
        names = {str(item.get('name') or ''): token
                 for token, item in typing.items() if item.get('name')}
        seen: set = set()
        written, kept_excluded = [], []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise ValidationFailedError('each solid must be an object')
            token = str(row.get('region_uuid') or '').strip()
            solid = by_uuid.get(token)
            if solid is None:
                raise ValidationFailedError(
                    'no solid of this case has that region_uuid',
                    details={'solid': index, 'region_uuid': token,
                             'solids': sorted(by_uuid)})
            if token in seen:
                raise ValidationFailedError(
                    'a solid is listed twice', details={'region_uuid': token})
            seen.add(token)
            kind = str(row.get('type') or default_type)
            if kind not in cad_solids.TYPES:
                raise ValidationFailedError(
                    'type must be one of: ' + ', '.join(cad_solids.TYPES),
                    details={'solid': index})
            name = str(row.get('name') or '').strip()
            if name and names.get(name, token) != token:
                raise ValidationFailedError(
                    'another volume control already has that name',
                    details={'name': name})
            current = typing.get(token)
            patch = {}
            if kind == cad_solids.EXCLUDED:
                patch['included'] = False
            else:
                patch['volume_type'] = kind
            if kind == cad_solids.EXCLUDED:
                included = False
            else:
                included = bool(current.get('included', True)) \
                    if current else True
                if not included:
                    kept_excluded.append(token)
            if name:
                patch['name'] = name
            if current is None:
                patch = {'name': name or solid.name, 'enabled': True,
                         'scope_token': token, 'included': included,
                         **patch}
                key, _element = data.addNewElement(storage)
            else:
                key = current['control']
            for relative_path, value in adapter.normalize_patch(
                    patch).items():
                data.setValue(f'{storage}/{key}/{relative_path}', value,
                              relative_path)
            if name:
                names[name] = token
                self._rename_tree_volume(data, token, name)
            written.append((key, token, name or (current or {}).get('name')
                            or solid.name, kind, included, row.get('space')))
        from .facade import refuse_locked_edit
        refuse_locked_edit(session, data)       # Plan 37 UF5 DP-1063
        transaction = session.state.commit(
            data, action='type solids',
            source=commit_source(command.source),
            target='gmsh.volume_controls.controls',
            reason=f'actor={command.actor.id}')
        solids = [{'control': str(data.remappedKey(storage, key)),
                   'region_uuid': token, 'name': name, 'type': kind,
                   'included': included,
                   **({} if space is None else {'space': int(space)})}
                  for key, token, name, kind, included, space in written]
        changed = tuple(f'gmsh.volume_controls.controls/{row["control"]}'
                        for row in solids)
        return OperationResult(
            'accepted', command.operation, session.revisions,
            changed_fields=changed,
            invalidated_outputs=('mesh', 'quality', 'exports'),
            payload={'engine': 'gmsh', 'solids': solids,
                     'kept_excluded': kept_excluded,
                     'transaction_id': getattr(transaction, 'tx_id', None)})

    @staticmethod
    def _rename_tree_volume(data, token: str, name: str) -> None:
        """Rename the tree's volume row for *token*, where the tree has one."""
        try:
            rows = dict(data.getElements('geometry') or {})
        except Exception:  # noqa: BLE001 - a case without a tree
            return
        for key, row in rows.items():
            try:
                kind = row.value('gType')
                owner = row.value('regionUuid')
            except Exception:  # noqa: BLE001 - a row without the leaf
                continue
            if str(getattr(kind, 'value', kind)) == 'volume' \
                    and str(owner or '') == token:
                data.setValue(f'geometry/{key}/name', name, 'name')

    @staticmethod
    def _region_label(key, region) -> str:
        """What to call this region in a refusal the user has to act on."""
        try:
            name = region.value('name')
        except Exception:  # noqa: BLE001 - a double, or a row without a name
            name = None
        return str(name) if name else f'region {key}'

    def _validate_fluid_seed(self, session: CaseSession, *,
                             allow_region_clash=False) -> list[str]:
        """Reject a surface/off-domain locationInMesh before launching snappy.

        Returns the warnings the launch should carry (Plan 36 RP8): two seeds
        of one type in one space, which snappy meshes once as one region. A
        Fluid and a Solid seed in one space is refused instead -- snappy
        cannot keep both -- naming both regions.

        DP-574: a seed outside every closed body is an external-flow seed and
        is accepted when it lies inside the background mesh box; every seed
        must lie inside that box once a blockMeshDict has been written.

        Every region, not the first one. DP-391, MEASURED on the multiregion
        fixture `jacketed_pipe`: this read `regions[0]` and nothing else, so a
        second region could carry any point at all and snappy was launched
        with it. Here that happened to be harmless; with the region order
        reversed it would have meshed the wrong cells without a word.

        And against the components as well as the assembly. A valid CFD
        enclosure is often partitioned into separate wall, inlet and outlet
        artifacts, none of them watertight on its own, so the assembled
        surface set is the right thing to probe for a single region -- it is
        also what `geometry.fluid_seed.suggest` searches, so a suggested seed
        cannot be refused here. But a *multiregion* case is the other shape:
        each region is its own closed solid, the assembly nests them, and
        `vtkSelectEnclosedPoints` counts ray crossings with odd parity. A
        point in the inner region crosses the inner wall and then the outer
        wall -- two crossings, even, "outside" -- so the assembled test can
        never accept it. MEASURED on `jacketed_pipe`: the fluid seed
        [0, 0, 0.1] is inside `jacketed_pipe_fluid` by winding number 1.0 and
        was refused by the assembly, which sees the shared interface twice
        (84 triangles written into both region files by design) and reads six
        crossings where the component reads two.

        So the seed is accepted when the assembly encloses it *or* any single
        component does. Both readings are recorded in the refusal, because
        which one refused it is what tells the user whether the seed or the
        geometry is wrong.
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.mesh.sizing import validate_fluid_seed
        store = GeometryArtifactStore(session.case_path)
        entries = store.entries()
        if not entries:  # legacy DB-only geometry has no immutable artifact to probe
            return []
        regions = session.state.db.getElements('region')
        if not regions:
            raise PreconditionFailedError('a fluid region seed is required')
        assembled_surface = self._assembled_surface(session)
        # RP13 #5: a seed in the hull of a domain that is not a cuboid, off
        # every block, has no background cell for snappy to start from.
        shape = self._domain_shape(session, assembled_surface)
        for key, region in (regions.items() if shape is not None else ()):
            point = [float(value) for value in region.vector('point')]
            if shape.contains(point):
                continue
            label = self._region_label(key, region)
            raise PreconditionFailedError(
                f'{label} seed is outside the domain: the background blocks '
                'do not cover that point, so blockMesh makes no cells there '
                'and snappy cannot find one to start from. Move it into one '
                'of the blocks.',
                details={'error': 'invalid_fluid_seed',
                         'reason': 'outside_domain', 'region': label,
                         'point': point,
                         'domain_bounds': list(shape.bounds)})
        for key, region in regions.items():
            point = region.vector('point')
            label = self._region_label(key, region)
            assembled = validate_fluid_seed(assembled_surface, point)
            if assembled['valid']:
                continue
            individual = (
                [assembled] if len(entries) == 1 else [
                    validate_fluid_seed(store._polydata(entry), point)
                    for entry in entries
                ])
            # Sitting on a wall is refused before the components are consulted,
            # and the assembly is the right judge of it: it carries every
            # component's triangles, so its distance test already answers for
            # all of them. Consulting the components first would accept a seed
            # lying exactly on an inner wall merely because the outer solid
            # encloses that wall -- a point snappy cannot use either way.
            on_surface = (assembled['on_surface']
                          or any(probe['on_surface'] for probe in individual))
            if not on_surface and any(probe['valid'] for probe in individual):
                continue
            details = {'error': 'invalid_fluid_seed', 'region': label,
                       'point': list(point),
                       'assembled_probe': assembled,
                       'component_probes': individual}
            if on_surface:
                raise PreconditionFailedError(
                    f'{label} seed lies on a geometry surface', details=details)
            # DP-574 (field audit 0924 snappy-front D1). Outside every closed
            # body is not "outside the domain": it is external flow, the
            # motorBike pattern, where snappy keeps the cells around the body
            # and removes the body. The domain is the background mesh, so a
            # seed there is judged against the blockMeshDict box. What stays
            # refused is a seed off that box, or one that cannot be judged
            # because the background mesh has not been written yet.
            domain = self._background_domain_bounds(session.case_path)
            details['background_bounds'] = domain
            if domain is None:
                raise PreconditionFailedError(
                    f'{label} seed is outside the intended closed geometry '
                    'region, and there is no background mesh (blockMeshDict) '
                    'to check it against as an external-flow seed; generate '
                    'the base grid first', details=details)
            if not self._point_inside_bounds(point, domain):
                raise PreconditionFailedError(
                    f'{label} seed is outside the geometry and outside the '
                    'background mesh box '
                    f'{self._describe_bounds(domain)}; move it inside the '
                    'box, around the body', details=details)
        warnings = self._same_space_warnings(
            session, regions, allow_region_clash=allow_region_clash)
        warnings.extend(self._face_seed_warnings(
            session, regions, assembled_surface))
        domain = self._background_domain_bounds(session.case_path)
        if domain is None:
            return warnings
        for key, region in regions.items():
            point = region.vector('point')
            if not self._point_inside_bounds(point, domain):
                label = self._region_label(key, region)
                raise PreconditionFailedError(
                    f'{label} seed is outside the background mesh box '
                    f'{self._describe_bounds(domain)}; snappy cannot find a '
                    'cell to start from there',
                    details={'error': 'invalid_fluid_seed', 'region': label,
                             'point': list(point),
                             'background_bounds': domain})
        return warnings

    #: How long the launch gate waits for a fluid-space field it has to label
    #: itself. The launch primes the cache off the main thread first, so this
    #: is only reached by a synchronous caller.
    SEED_SPACE_SECONDS = 10.0

    @staticmethod
    def _region_type(region) -> str:
        try:
            kind = region.value('type')
        except Exception:  # noqa: BLE001 - a row without a type is a fluid
            kind = None
        kind = getattr(kind, 'value', kind)
        return str(kind).lower() if kind else 'fluid'

    def _seed_space_field(self, session: CaseSession, inputs):
        """The fluid-space field for the launch gate, or ``None``.

        From a cache when there is one; otherwise labelled on the VTK worker
        thread, never on the main thread. A field that cannot be had leaves
        the seeds to the other checks rather than blocking the launch.
        """
        from foammesh.core.mesh import fluid_regions
        from foammesh.core.mesh.fluid_spaces import FluidSpacesCancelled

        surfaces, _geometry_bounds, box, base_cell = inputs
        options = self._fluid_space_options(session, base_cell)
        field = fluid_regions.cached_detection(surfaces, box, **options)
        if field is not None:
            return field
        try:
            return fluid_regions.detect_blocking(
                surfaces, box, timeout=self.SEED_SPACE_SECONDS, **options)
        except (TimeoutError, FluidSpacesCancelled, ValueError, RuntimeError,
                MemoryError):
            return None
        except Exception:  # noqa: BLE001 - an unlabellable geometry is judged
            return None    # by the per-seed checks alone

    def _face_seed_warnings(self, session: CaseSession, regions,
                            surface) -> list[str]:
        """Plan 36 RP13 #2: a seed on a face of the mesh snappy will cut.

        snappy looks for the cell holding each seed; on a face it finds two
        or none. The gizmo and detect keep seeds off the faces, but a typed
        point or an older case can still sit on one: the launch says so.
        """
        from foammesh.core.mesh.face_clearance import (
            MIN_CLEARANCE, seed_face_clearance,
        )

        try:
            bounds = surface.GetBounds() if surface is not None else None
        except Exception:  # noqa: BLE001 - an advisory check, never a gate
            bounds = None
        grid, level = self._mesh_faces(session, bounds)
        if grid is None:
            return []
        warnings = []
        for key, region in regions.items():
            try:
                point = [float(value) for value in region.vector('point')]
                clearance = seed_face_clearance(point, grid, level)
            except Exception:  # noqa: BLE001 - the checks above judge it
                continue
            if clearance is None or clearance.relative >= MIN_CLEARANCE:
                continue
            label = self._region_label(key, region)
            warnings.append(
                f'{label} seed lies on a face of the mesh (along '
                f'{"xyz"[clearance.axis]}, {clearance.relative:.3g} of a '
                f'{clearance.cell:.4g} m cell from it); snappy may not find '
                'the cell it is in. Nudge the seed off the face.')
        return warnings

    @staticmethod
    def _allows_region_clash(command: Command) -> bool:
        """RP13 #1: the launch's override of a confirmed fluid/solid clash."""
        try:
            return bool((command.parameters or {}).get('allow_region_clash'))
        except AttributeError:
            return False

    def _seed_space_finer(self, session: CaseSession, inputs, field):
        """RP13 #1: the gate's field again at h/2, or ``None``.

        From a cache when the launch primed it; otherwise labelled on the VTK
        worker within `fluid_regions.RECHECK_SECONDS`. Past that, or when the
        voxel cap leaves no finer resolution, the conflicts stay unresolved.
        """
        import time

        from foammesh.core.mesh import fluid_regions
        from foammesh.core.mesh.fluid_spaces import FluidSpacesCancelled

        surfaces, _geometry_bounds, box, _base_cell = inputs
        finer_h = fluid_regions.recheck_h(field)
        if finer_h is None:
            return None
        options = {'cache_dir': fluid_regions.cache_dir(session.case_path),
                   'h': finer_h}
        cached = fluid_regions.cached_detection(surfaces, box, **options)
        if cached is not None:
            return cached
        seconds = fluid_regions.RECHECK_SECONDS
        deadline = time.monotonic() + seconds
        try:
            return fluid_regions.detect_blocking(
                surfaces, box, timeout=seconds,
                cancelled=lambda: time.monotonic() > deadline, **options)
        except (TimeoutError, FluidSpacesCancelled, ValueError, RuntimeError,
                MemoryError):
            return None
        except Exception:  # noqa: BLE001 - an unlabellable geometry stays
            return None    # unresolved: a warning, never a refusal

    #: RP13 #1: why a conflict is not confirmed, as the launch says it.
    _CONFLICT_DOUBT = {
        'approximate': 'the voxel check is approximate: at half the voxel '
                       'size the spaces differ or the passage between the '
                       'seeds is under two voxels wide',
        'unresolved': 'the voxel check is unresolved: the finer check did '
                      'not finish in time',
    }

    def _same_space_warnings(self, session: CaseSession, regions, *,
                             allow_region_clash=False) -> list[str]:
        """Plan 36 RP8 (F5): two region seeds in one space.

        snappy keeps a space once, as the region whose seed it met first. Two
        seeds of one type there is a region that will not exist -- a warning.
        A Fluid and a Solid seed there cannot both be honoured: refused,
        naming both, before snappy silently meshes one of them.

        RP13 #1: the voxels only approximate the connectivity. Every conflict
        is graded against a second field at h/2 (`graded_conflicts`); only a
        ``confirmed`` clash refuses, and ``allow_region_clash`` overrides
        even that. An approximate or unresolved one -- including two seeds
        only the finer field joins through a narrow neck (DP-861) -- warns.
        """
        from foammesh.core.mesh import fluid_regions

        if len(regions) < 2:
            return []
        inputs = self._fluid_space_inputs(session)
        if inputs is None:
            return []
        field = self._seed_space_field(session, inputs)
        if field is None:
            return []
        finer = self._seed_space_finer(session, inputs, field)
        seeds = []
        for key, region in regions.items():
            try:
                point = [float(value) for value in region.vector('point')]
            except Exception:  # noqa: BLE001 - the loop above refuses it
                continue
            seeds.append((self._region_label(key, region),
                          self._region_type(region), point))
        warnings = []
        for group in fluid_regions.graded_conflicts(field, seeds, finer):
            named = ' and '.join(
                f'{label} ({kind})'
                for label, kind in zip(group['regions'], group['types']))
            where = ('the space around the geometry' if group['outside']
                     else 'one enclosed space')
            confirmed = group['confidence'] == fluid_regions.CONFIRMED
            doubt = self._CONFLICT_DOUBT.get(group['confidence'], '')
            if group['clash'] and confirmed and not allow_region_clash:
                raise PreconditionFailedError(
                    f'{named} are seeded in {where}; snappy keeps a space '
                    'as one region, so a fluid and a solid cannot share it. '
                    'Move one seed into its own space.',
                    details={'error': 'region_space_clash',
                             'regions': group['regions'],
                             'types': group['types'],
                             'space': group['space'],
                             'outside': group['outside'],
                             'confidence': group['confidence']})
            if group['clash'] and confirmed:
                warnings.append(
                    f'{named} are seeded in {where}; a fluid and a solid '
                    'cannot share it, and the launch goes ahead only because '
                    'allow_region_clash was given. snappy will keep one.')
            elif group['clash']:
                warnings.append(
                    f'{named} may be seeded in {where} ({doubt}). If they '
                    'are, snappy keeps it as one region and a fluid and a '
                    'solid cannot share it. Check the passage between the '
                    'seeds, or move one seed.')
            elif confirmed:
                first = group['regions'][0]
                warnings.append(
                    f'{named} are seeded in {where}; snappy meshes it once, '
                    f'as {first}, so the others will not exist. Move or '
                    'remove the extra seeds.')
            else:
                first = group['regions'][0]
                warnings.append(
                    f'{named} may be seeded in {where} ({doubt}). If they '
                    f'are, snappy meshes it once, as {first}, and the others '
                    'will not exist. Check the passage between the seeds.')
        return warnings

    async def _snappy_seed_gate(self, session: CaseSession, *,
                                allow_region_clash=False) -> list[str]:
        """The seed checks every snappy launch passes, whichever route.

        DP-851. ``workflow.run_stage`` primed the fluid spaces and asked
        :meth:`_validate_fluid_seed`; ``workflow.run_pipeline`` asked neither,
        so "Run to end" launched snappy with a seed on a wall, off the box, or
        a fluid and a solid in one space, and never said two seeds shared a
        space. Both routes ask here now. Refusals raise; the returned list is
        the warnings the launch carries, as ``warnings`` and as
        ``payload['region_warnings']``.
        """
        excludes = self._exclude_point_rows(session)
        # Plan 37 UF18: an exclude point is judged against the labelled
        # domain even with one region, so the field is primed for it too.
        await self._prime_seed_spaces(session, force=bool(excludes))
        warnings = list(self._validate_fluid_seed(
            session, allow_region_clash=allow_region_clash) or ())
        if excludes:
            warnings.extend(self._exclude_point_gate(session, excludes))
        warnings.extend(self._farfield_seed_gate(session, excludes))
        # Plan 37 #1 / #2: a flush box under an outside seed, seeds on both
        # sides of one closed surface, External on a duct -- said, not refused.
        warnings.extend(self._placement_seed_gate(session))
        self._record_retained_estimate(session, excludes)
        return warnings

    def _region_seed_rows(self, session: CaseSession) -> list:
        """``(label, type, xyz)`` of every region seed with a point."""
        seeds = []
        try:
            regions = session.state.db.getElements('region') or {}
        except Exception:  # noqa: BLE001 - a project without regions
            regions = {}
        for key, region in dict(regions).items():
            try:
                point = [float(value) for value in region.vector('point')]
            except Exception:  # noqa: BLE001 - the seed checks refuse it
                continue
            seeds.append((self._region_label(key, region),
                          self._region_type(region), point))
        return seeds

    def _record_retained_estimate(self, session: CaseSession,
                                  excludes) -> None:
        """Plan 37 F-2: what this launch expects to keep, for after the run.

        Written when there are exclude points -- the voxel volume the seeds
        keep and the exclude points remove -- and removed otherwise, so an
        estimate never outlives the points it was made for. Best effort.
        """
        from foammesh.core.quality import retained_regions

        try:
            if not excludes:
                retained_regions.write_estimate(session.case_path, None)
                return
            field = None
            try:
                inputs = self._fluid_space_inputs(session)
                if inputs is not None:
                    field = self._seed_space_field(session, inputs)
            except Exception:  # noqa: BLE001 - estimated without the voxels
                field = None
            retained_regions.write_estimate(
                session.case_path, retained_regions.estimate(
                    self._region_seed_rows(session), excludes, field))
        except Exception as error:  # noqa: BLE001 - never stops a launch
            logger.warning('retained-region estimate not recorded: %s', error)

    def _retained_region_context(self, session: CaseSession) -> dict | None:
        """Plan 37 F-2: what a checked snappy mesh is judged against.

        ``None`` on any other engine. Read on the command thread, so the
        checkMesh parser -- which may run elsewhere -- touches no database.
        """
        from foammesh.core.quality import retained_regions

        try:
            # The engine whose regions are the spaces its seeds name (snappy)
            # is asked, not named: a registered engine that does not mesh
            # each solid as its own region.
            from foammesh.core.engine.registry import ENGINE_REGISTRY

            if (self._engine_id(session) not in ENGINE_REGISTRY.ids()
                    or self._regions_are_solids(session)):
                return None
            # Seeds that cannot be read are not "no seeds": without them
            # the expected count is unknown, so nothing is judged.
            session.state.db.getElements('region')
            excludes = self._exclude_point_rows(session)
            names = [str(name) for name, _point in excludes]
            estimate = retained_regions.read_estimate(session.case_path)
            if estimate is not None and [
                    str(row.get('name')) for row in
                    estimate.get('excludes') or ()] != names:
                estimate = None
            return {'case_path': session.case_path,
                    'seeds': len(self._region_seed_rows(session)),
                    'excludes': names, 'estimate': estimate}
        except Exception as error:  # noqa: BLE001 - judged without it
            logger.warning('retained-region context not read: %s', error)
            return None

    @staticmethod
    def _apply_retained_regions(context: dict | None, parsed) -> None:
        """Judge *parsed* for pieces the seeds did not ask for (Plan 37 F-2).

        The count is checkMesh's; when its log has none and exclude points
        were set, the mesh's own connectivity is counted instead. A split
        mesh is filed as not runnable -- the Quality task must not read as
        passed -- and nothing is removed from it.
        """
        if not context or parsed is None:
            return
        from foammesh.core.quality import retained_regions

        try:
            counted = None
            if getattr(parsed, 'regions', None) is None and context['excludes']:
                counted = retained_regions.count_cell_regions(
                    context['case_path'])
            retained_regions.apply(parsed, retained_regions.assess(
                parsed, seeds=context['seeds'],
                excludes=context['excludes'],
                estimate=context.get('estimate'), counted=counted))
        except Exception as error:  # noqa: BLE001 - the check still files
            logger.warning('retained regions not judged: %s', error)

    def _farfield_seed_gate(self, session: CaseSession, excludes) -> list[str]:
        """Plan 37 UF14: with a farfield, the seeds against it and the bodies.

        With a farfield the fluid is the space inside it and outside every
        body, so a fluid seed outside the farfield (snappy keeps the ring
        between it and the block), on it, or inside a closed body is refused
        here, before snappy runs, naming every one. A seed that may be in an
        open body or sealed in a cavity (from the voxels) is a warning. No
        farfield: nothing is asked and nothing is returned.
        """
        from foammesh.core.mesh import snappy_farfield

        db = getattr(session.state, 'db', None)
        try:
            spec = snappy_farfield.active(db) if db is not None else None
        except Exception:  # noqa: BLE001 - the case writer refuses the record
            spec = None
        if spec is None:
            return []
        try:
            inputs = self._fluid_space_inputs(session)
        except Exception:  # noqa: BLE001 - judged without the geometry
            inputs = None
        if inputs is None:
            return []
        surfaces, geometry_bounds, _box, _cell = inputs
        try:
            farfield = snappy_farfield.resolve(spec, geometry_bounds)
        except ValueError as error:
            raise PreconditionFailedError(
                f'farfield: {error}',
                details={'error': 'farfield_refused', 'findings': []}) from None
        seeds = []
        try:
            regions = db.getElements('region') or {}
        except Exception:  # noqa: BLE001 - the seed checks refuse this
            regions = {}
        for key, region in dict(regions).items():
            try:
                point = [float(value) for value in region.vector('point')]
            except Exception:  # noqa: BLE001 - the seed checks refuse it
                continue
            seeds.append((self._region_label(key, region),
                          self._region_type(region), point))
        field = self._seed_space_field(session, inputs)
        report = snappy_farfield.preflight(
            farfield, seeds=seeds, excludes=excludes, surfaces=surfaces,
            field=field)
        if report.errors:
            raise PreconditionFailedError(
                '; '.join(one.message for one in report.errors),
                details={'error': 'farfield_refused',
                         'findings': [one.to_dict()
                                      for one in report.findings]})
        return [one.message for one in report.warnings]

    @staticmethod
    def _closed_surface_rows(session: CaseSession) -> list[dict]:
        """``[{name, bbox, openings}]`` of every closed (watertight) geometry.

        Plan 37 #2(c): ``openings`` are its faces named as an inlet or an
        outlet, which make a closed surface a duct or a container rather
        than a body the flow goes around.
        """
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.patches.ops import (
            boundary_category_for_name,
        )

        try:
            entries = GeometryArtifactStore(session.case_path).entries()
        except Exception:  # noqa: BLE001 - no geometry, nothing to name
            return []
        rows = []
        for entry in entries:
            diagnostics = entry.get('diagnostics') or {}
            bbox = entry.get('bbox')
            if not diagnostics.get('watertight') or not bbox:
                continue
            openings = []
            for patch in entry.get('patches') or ():
                name = str((patch or {}).get('name') or '')
                category = boundary_category_for_name(
                    name.strip().lower().replace(' ', '_'), default='wall')
                if category in ('inlet', 'outlet'):
                    openings.append(name)
            try:
                bbox = [float(value) for value in bbox]
            except (TypeError, ValueError):
                continue
            rows.append({'name': str(entry.get('name')
                                     or entry.get('geometry_id')),
                         'bbox': bbox, 'openings': openings})
        return rows

    def _placement_warnings(self, session: CaseSession, field, seeds,
                            inputs, *, external=False) -> list[dict]:
        """Plan 37 #1 / #2: the flush-box, both-sides and duct warnings."""
        from foammesh.core.mesh import fluid_regions, snappy_farfield

        if field is None or inputs is None or not (seeds or external):
            return []
        _surfaces, geometry_bounds, box, base_cell = inputs
        db = getattr(session.state, 'db', None)
        try:
            farfield = (snappy_farfield.active(db) is not None
                        if db is not None else False)
        except Exception:  # noqa: BLE001 - the case writer refuses the record
            farfield = False
        try:
            return fluid_regions.placement_warnings(
                field, seeds, box=box, geometry_bounds=geometry_bounds,
                base_cell=base_cell,
                closed_surfaces=self._closed_surface_rows(session),
                farfield=farfield, external=external)
        except Exception as error:  # noqa: BLE001 - never stops a launch
            logger.warning('region placement not judged: %s', error)
            return []

    def _placement_seed_gate(self, session: CaseSession) -> list[str]:
        """Plan 37 #1 / #2: what the launch's seeds mesh, said before it runs.

        Warnings only: an outside seed on a box flush with the geometry, seeds
        on both sides of one closed surface, and External on a duct.
        """
        try:
            inputs = self._fluid_space_inputs(session)
        except Exception:  # noqa: BLE001 - judged without the geometry
            inputs = None
        if inputs is None:
            return []
        seeds = self._region_seed_rows(session)
        if not seeds:
            return []
        field = self._seed_space_field(session, inputs)
        return [one['message'] for one in self._placement_warnings(
            session, field, seeds, inputs)]

    @staticmethod
    def _exclude_point_rows(session: CaseSession) -> list:
        """``(name, xyz)`` of every exclude point the project defines."""
        try:
            items = session.state.db.getElements('castellation/excludePoints')
        except Exception:  # noqa: BLE001 - a project without the list
            return []
        rows = []
        for key, item in sorted(
                dict(items or {}).items(),
                key=lambda pair: (0, int(pair[0])) if str(pair[0]).isdigit()
                else (1, str(pair[0]))):
            try:
                name = str(getattr(item.value('name'), 'value',
                                   item.value('name')) or '').strip()
            except Exception:  # noqa: BLE001 - an unnamed row
                name = ''
            try:
                point = tuple(float(value) for value in item.vector('point'))
            except Exception:  # noqa: BLE001 - the case writer refuses it
                continue
            rows.append((name or f'exclude {key}', point))
        return rows

    def _exclude_point_gate(self, session: CaseSession, excludes) -> list[str]:
        """Plan 37 UF18: the UF16 exclude-point preflight, at launch.

        ``core/mesh/exclude_points.preflight`` against the region seeds, the
        background domain and -- when it can be had -- the Plan 36 voxel
        field and its h/2 re-check. Its errors refuse the launch, naming
        every one; its warnings are returned for the launch to carry, the
        way the seed gate's are.
        """
        from foammesh.core.mesh.exclude_points import preflight

        seeds = []
        try:
            regions = session.state.db.getElements('region') or {}
        except Exception:  # noqa: BLE001 - the seed checks refuse this
            regions = {}
        for key, region in dict(regions).items():
            try:
                point = [float(value) for value in region.vector('point')]
            except Exception:  # noqa: BLE001 - the seed checks refuse it
                continue
            seeds.append((self._region_label(key, region),
                          self._region_type(region), point))
        try:
            inputs = self._fluid_space_inputs(session)
        except Exception:  # noqa: BLE001 - judged without the geometry
            inputs = None
        surfaces, cell_size, field, finer = None, None, None, None
        if inputs is not None:
            surfaces, _bounds, box, cell_size = inputs
            field = self._seed_space_field(session, inputs)
            if field is not None:
                finer = self._seed_space_finer(session, inputs, field)
        domain = self._background_domain_bounds(session.case_path)
        if domain is None and inputs is not None and hasattr(box, 'metres'):
            domain = box
        report = preflight(excludes, seeds=seeds, domain=domain,
                           surfaces=surfaces, field=field, finer=finer,
                           cell_size=cell_size)
        if report.errors:
            raise PreconditionFailedError(
                '; '.join(one.message for one in report.errors),
                details={'error': 'exclude_point_refused',
                         'findings': [one.to_dict()
                                      for one in report.findings],
                         'removals': [dict(row) for row in report.removals]})
        return [one.message for one in report.warnings]

    @staticmethod
    def _region_warnings_payload(seed_warnings) -> dict:
        """``{'region_warnings': [...]}`` when there are any, else ``{}``."""
        return {'region_warnings': list(seed_warnings)} if seed_warnings else {}

    async def _prime_seed_spaces(self, session: CaseSession, *,
                                 force: bool = False) -> None:
        """Label the fluid spaces off the main thread before the launch gate.

        The gate is synchronous; with the field cached here it answers from
        memory instead of waiting on the worker thread. ``force`` labels it
        for a single region too (Plan 37 UF18: exclude points need it).
        """
        from foammesh.core.mesh import fluid_regions
        from foammesh.support.vtk_threads import vtk_run_in_thread

        try:
            regions = session.state.db.getElements('region')
            if not force and (not regions or len(regions) < 2):
                return
            inputs = self._fluid_space_inputs(session)
        except Exception:  # noqa: BLE001 - the gate reports what is wrong
            return
        if inputs is None:
            return
        surfaces, _geometry_bounds, box, base_cell = inputs
        try:
            field = await vtk_run_in_thread(
                fluid_regions.run_detection, surfaces, box,
                **self._fluid_space_options(session, base_cell))
        except Exception:  # noqa: BLE001 - priming is best effort; the gate
            return         # reports a geometry it cannot judge
        # RP13 #1: the h/2 re-check, on the VTK worker, within its budget;
        # a run that is stopped leaves the gate's conflicts unresolved.
        finer_h = fluid_regions.recheck_h(field)
        if finer_h is None:
            return
        import time

        deadline = time.monotonic() + fluid_regions.RECHECK_SECONDS
        try:
            await vtk_run_in_thread(
                fluid_regions.run_detection, surfaces, box, h=finer_h,
                cache_dir=fluid_regions.cache_dir(session.case_path),
                cancelled=lambda: time.monotonic() > deadline)
        except Exception:  # noqa: BLE001 - past the budget: unresolved
            return

    @staticmethod
    def _background_domain_bounds(case_path) -> list[float] | None:
        """The box the background mesh spans, from the written blockMeshDict.

        ``[xmin, xmax, ymin, ymax, zmin, zmax]`` in metres (vertices times
        ``scale``), or ``None`` when no dictionary has been written or it
        cannot be read. Plan 36 RP1: the reading lives with the other sources
        of the same box, in `foammesh.core.mesh.domain_box`, where it is the
        last one tried.
        """
        from foammesh.core.mesh.domain_box import written_domain_box
        box = written_domain_box(case_path)
        return None if box is None else list(box.metres())

    @staticmethod
    def _point_inside_bounds(point, bounds) -> bool:
        span = max(bounds[1] - bounds[0], bounds[3] - bounds[2],
                   bounds[5] - bounds[4], 0.0)
        margin = span * 1e-9
        return all(bounds[2 * axis] + margin < float(point[axis])
                   < bounds[2 * axis + 1] - margin for axis in range(3))

    @staticmethod
    def _describe_bounds(bounds) -> str:
        return ('x [{:g}, {:g}] y [{:g}, {:g}] z [{:g}, {:g}] m'
                .format(*bounds))

    @staticmethod
    def _preflight_snappy_inputs(session: CaseSession, utility: str) -> None:
        """Fail before launch when an immutable staged surface is missing/stale."""
        import hashlib
        import json
        import re
        tri_surface = session.case_path / 'constant' / 'triSurface'
        manifest_path = tri_surface / '.foammesh-surfaces.json'
        if not manifest_path.is_file():
            raise PreconditionFailedError(
                'prepared surface manifest is missing; prepare geometry before '
                f'running {utility}',
                details={'error': 'prepared_surface_manifest_missing'})
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise PreconditionFailedError(
                f'invalid staged-surface manifest: {error}') from error
        missing = []
        stale = []
        for item in manifest.get('surfaces', ()):
            path = tri_surface / str(item.get('file') or '')
            if not path.is_file():
                missing.append(path.name)
            elif hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256'):
                stale.append(path.name)
        dictionary_name = (
            'surfaceFeaturesDict' if utility == 'surfaceFeatures'
            else 'snappyHexMeshDict')
        text = (session.case_path / 'system' / dictionary_name).read_text(
            encoding='utf-8')
        referenced = set(re.findall(r'file\s+"([^"]+)"', text))
        if utility == 'surfaceFeatures':
            referenced.update(re.findall(r'"([^"]+\.(?:stl|obj|vtk|vtp))"', text))
        for name in sorted(referenced):
            if not (tri_surface / name).is_file():
                missing.append(name)
        if missing or stale:
            raise PreconditionFailedError(
                'Snappy surface preflight failed',
                details={
                    'error': 'invalid_staged_surfaces',
                    'missing': sorted(set(missing)),
                    'checksum_mismatch': sorted(set(stale)),
                    'utility': utility,
                })

    @staticmethod
    def _surface_feature_inputs(session: CaseSession) -> dict:
        import hashlib
        tri_surface = session.case_path / 'constant' / 'triSurface'
        sources = []
        for path in sorted(tri_surface.glob('*')):
            if path.is_file() and path.suffix.lower() in {'.stl', '.obj', '.vtk', '.vtp'}:
                sources.append({'path': path.name,
                                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        angles = []
        castellation = session.state.db.getElement('castellation')
        for group_id, group in castellation.elements('refinementSurfaces').items():
            # ``includedAngle`` is a sibling of the nested ``surfaceRefinement``
            # level pair, not a member of it (see the refinementSurfaces schema).
            angles.append({'group_id': group_id,
                           'included_angle': float(group.value('includedAngle'))})
        dictionary = session.case_path / 'system' / 'surfaceFeaturesDict'
        return {'sources': sources, 'included_angles': angles,
                'dictionary_sha256': (hashlib.sha256(dictionary.read_bytes()).hexdigest()
                                      if dictionary.is_file() else None)}

    @classmethod
    def _require_current_surface_features(cls, session: CaseSession) -> None:
        tri_surface = session.case_path / 'constant' / 'triSurface'
        sidecar = tri_surface / '.surfaceFeatures.provenance.json'
        # No explicit feature artifacts is a valid degraded mode. Once an
        # artifact exists, however, Snappy must never consume it stale.
        if not any(tri_surface.glob('*.eMesh')):
            return
        if not sidecar.is_file():
            raise PreconditionFailedError(
                'surface feature artifacts have no provenance; rerun surfaceFeatures',
                details={'error': 'surface_features_stale'})
        try:
            recorded = json.loads(sidecar.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise PreconditionFailedError(
                'surface feature provenance is unreadable; rerun surfaceFeatures',
                details={'error': 'surface_features_stale'}) from error
        current = cls._surface_feature_inputs(session)
        if any(recorded.get(key) != current.get(key)
               for key in ('sources', 'included_angles', 'dictionary_sha256')):
            raise PreconditionFailedError(
                'surface feature artifacts are stale; rerun surfaceFeatures',
                details={'error': 'surface_features_stale',
                         'recorded': recorded, 'current': current})

    # -- Slice 5: mesh info / QA / transforms / repair / recovery ---------- #

    def _mesh_info(self, session: CaseSession, command: Command) -> OperationResult:
        self._require_mesh(session)
        from foammesh.core.mesh.info import MeshInfoService
        unit = command.parameters.get('display_unit', 'm')
        service = MeshInfoService()
        info = service.inspect(session.case_path, display_unit=unit)
        payload = _to_payload(info)
        report = command.parameters.get('report')
        if report:
            try:
                payload['report'] = str(service.save_report(info, report))
            except (OSError, ValueError) as error:
                raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, payload)

    async def _checkmesh_help_ready(self) -> None:
        """Ask ``checkMesh -help`` on a worker thread (Plan 35 CR7).

        The first ask of a session boots the runtime (DP-693 measured a cold
        start past 5 s); the registry caches an answer, and
        :meth:`_checkmesh_profile` reads that cache on the loop.
        """
        import asyncio

        registry = self._capabilities_registry()
        if getattr(registry, 'help_if_known', None) is None:
            return
        try:
            await asyncio.to_thread(registry.help, 'checkMesh')
        except Exception:                                     # noqa: BLE001
            pass    # the profile falls back to the verified baseline

    def _checkmesh_profile(self) -> tuple[object, list[str]]:
        """What the configured ``checkMesh`` can do, and what was assumed.

        Plan 30 WP-04 (F-04). Both users of ``checkMesh`` -- ``mesh.check`` and
        the meshing pipeline's own check node -- resolve the utility's flags
        here, so the two can no longer disagree about which report they wrote.
        Flags are derived from the utility's own ``-help``; when that probe is
        unavailable or unparseable the verified Foundation-13 baseline applies
        rather than failing the check closed.
        """
        from foammesh.core.quality import CheckMeshProfile

        registry = self._capabilities_registry()
        warnings: list[str] = []
        # Plan 35 CR7: read the cached answer when the registry keeps one;
        # `_checkmesh_help_ready` asked it off the owner loop first.
        lookup = (getattr(registry, 'help_if_known', None)
                  or getattr(registry, 'help', None))
        help_result = lookup('checkMesh') if lookup is not None else None
        if help_result is not None and getattr(help_result, 'available', False):
            try:
                return CheckMeshProfile.from_help(help_result.output), warnings
            except ValueError as error:
                warnings.append(
                    f'checkMesh help did not advertise the expected flags ({error}); '
                    'using the baseline profile.')
        if not warnings:
            warnings.append(
                'checkMesh help could not be probed; using the baseline '
                '-allTopology -allGeometry -writeSets profile.')
        return CheckMeshProfile.fallback(), warnings

    async def _mesh_check(self, session: CaseSession,
                          command: Command) -> OperationResult:
        """Qualify this mesh the way the saved Mesh setup selection asks.

        Plan 33 QA-06. This operation used to *be* checkMesh, so which checks
        ran was decided by which button had been pressed. On a Gmsh project
        that meant the native element gate -- the only measurement of the
        elements Gmsh actually produced -- counted for nothing here, and a
        route that needed two checks settled its QA row on one.

        The entry point is unchanged, because the footer, the menu and the CLI
        all reach the quality run through
        :func:`core.engine.base.qa_operation` and a mesh is still qualified by
        pressing it. What changed is that the run now reads
        :func:`core.facade.quality_routes.quality_checks_for` and executes
        every check that selection names, in the order it names them.
        """
        return await self._run_route_checks(session, command)

    async def _run_checkmesh(self, session: CaseSession,
                             command: Command) -> OperationResult:
        # checkMesh judges the case-root mesh, so a decomposed result has to be
        # gathered first or the check would grade the previous stage's mesh.
        await self._ensure_reconstructed(session, command)
        self._require_mesh(session)
        utility = await self._utility_ready('checkMesh')
        from foammesh.core.quality import (
            CheckMeshRequest, MeshCheckService, parse_checkmesh)
        from foammesh.core.quality.checkmesh_service import checkmesh_flags
        registry = self._capabilities_registry()
        await self._checkmesh_help_ready()
        profile, profile_warnings = self._checkmesh_profile()
        from foammesh.core.quality.checkmesh_service import checkmesh_request
        # Plan 31. The project's own checkMesh settings first -- thresholds,
        # the problem-face surfaces, the user-defined criteria -- then the
        # operation's parameters on top, because a caller that names one is
        # asking for this run and not for the project.
        # Plan 37 UF18. A parameter overrides the project only when it is
        # given: the defaults used to be written in here as True, so the
        # project's own allTopology/allGeometry/writeSets were never read.
        overrides = {}
        if 'write_sets' in command.parameters:
            overrides['write_sets'] = bool(command.parameters['write_sets'])
        if 'set_format' in command.parameters:
            overrides['set_format'] = str(command.parameters['set_format'])
        if 'extended_checks' in command.parameters:
            extended = bool(command.parameters['extended_checks'])
            overrides['extended_topology'] = extended
            overrides['extended_geometry'] = extended
        request = checkmesh_request(
            session.state.db, session.case_path, **overrides)
        # F-04. The same flag set the meshing DAG's checkMesh nodes carry;
        # both write the same report at the same path, so they must not be
        # able to disagree about what was measured.
        semantic_argv = checkmesh_flags(profile=profile, request=request) + (
            '-case', str(session.case_path))
        if hasattr(registry, 'command'):
            launch = registry.command(
                'checkMesh', semantic_argv, cwd=session.case_path)
        else:
            from foammesh.core.openfoam_runtime import LaunchCommand
            launch = LaunchCommand((utility, *semantic_argv))
        report_path = session.case_path / 'foammesh' / 'quality' / 'latest.json'
        # Plan 37 UF18. checkMesh never clears its output directory; what
        # this run wrote is what was modified after it started.
        import time as _time
        started_at = _time.time()

        # Plan 37 F-2. Read on this thread; the parser may run on another.
        retained_context = self._retained_region_context(session)

        def parse_and_persist(result):
            parsed = parse_checkmesh(result.output)
            self._apply_retained_regions(retained_context, parsed)
            _, report = MeshCheckService.persist_result(
                session.case_path, parsed, command=result.argv,
                log_path=result.log_path or session.case_path / 'foammesh' / 'logs' /
                'checkMesh.log')
            self._collect_check_artifacts(
                session, result.argv, report, started_at=started_at,
                wanted=request)
            return report.to_dict()

        execution = await self._context(session).executor.execute(session, OperationSpec(
            operation=command.operation,
            argv=launch.argv,
            cwd=session.case_path,
            timeout=_stage_timeout(command.parameters, 900),
            max_output_bytes=4 * 1024 * 1024,
            expected_artifacts=(ExpectedArtifact(
                report_path, kind='quality-report',
                validator=lambda path: path.is_file() and path.stat().st_size > 0,
                produced_by_parser=True),),
            parser=parse_and_persist,
            artifact_event=Event.ARTIFACT_QUALITY_CHANGED,
            invalidated_outputs=('quality',),
            cleanup_argv=launch.cleanup_argv,
        ), on_line=command.parameters.get('on_line'))
        payload = execution.to_payload()
        payload['profile_source'] = profile.source
        parsed = execution.parsed if isinstance(execution.parsed, dict) else {}
        verdict = parsed.get('result') if isinstance(parsed.get('result'), dict) else {}
        # DP-869. checkMesh's own tally is not the acceptance rule. The
        # profile runs `-allGeometry`, whose concave-cell test has no angle
        # limit, while the quality policy lets snappy keep cells up to
        # `maxConcave 80`. MEASURED on the Plan 36 campaign: 10 of 11 snappy
        # meshes ended `Failed 1 mesh checks` on concave cells alone -- 16,321
        # cells, every one at a refinement-level transition and 16,317 of them
        # split-hex polyhedra; plain checkMesh called all four re-checked
        # meshes `Mesh OK`. The parser already grades that finding advisory
        # and the mesh runnable; refusing here made `foammesh check` exit 1
        # and stopped every automation recipe on a runnable mesh. A mesh with
        # only advisory findings is accepted as a blemish -- the Gmsh gate's
        # rule -- and says so; a blocking finding still refuses.
        mesh_accepted = (
            verdict.get('incomplete') is False
            and (verdict.get('mesh_ok') is True
                 or verdict.get('runnable') is True))
        blemish_warnings: tuple[str, ...] = ()
        if mesh_accepted and verdict.get('mesh_ok') is not True:
            from types import SimpleNamespace

            from foammesh.core.quality.verdict import (
                failed_check_line, failed_check_names)
            line = failed_check_line(
                verdict.get('failed_checks'),
                failed_check_names(SimpleNamespace(**verdict)))
            payload['quality_blemish'] = {
                'failed_checks': verdict.get('failed_checks'),
                'advisory': list(verdict.get('advisory_findings') or ()),
                'line': line,
            }
            blemish_warnings = (
                f'{line or "checkMesh reported advisory findings"}; every '
                'finding is advisory, so the mesh is runnable and accepted '
                'as a blemish.',)
        # Plan 30 F-05. **Accept anyway** on a snappy mesh lands here: snappy
        # publishes as it goes, so the mesh checkMesh judged is the mesh on
        # disk and there is nothing to re-mesh -- only a decision to record
        # against it. A refusal to bind the decision is raised at the caller
        # rather than swallowed, exactly as it is on the Gmsh route.
        override = (self._accept_checked_mesh(session, command, payload,
                                              verdict)
                    if execution.succeeded and not mesh_accepted else None)
        # checkMesh is one of the QA task's checks, so the row is advanced by
        # the route runner that knows which others the selection asked for --
        # not here, where only this one check's verdict is in view. A poor
        # mesh is still complete evidence (§8.5) and advances the task as a
        # warning; a route with a check still missing advances nothing.
        return OperationResult(
            'accepted' if execution.succeeded and (mesh_accepted or override)
            else 'failed',
            command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=(tuple(execution.warnings) + tuple(profile_warnings)
                      + blemish_warnings),
            payload=payload)

    def _accept_checked_mesh(self, session: CaseSession, command: Command,
                             payload: dict, verdict=None) -> dict | None:
        """Record the decision to keep a mesh checkMesh refused, or ``None``.

        Plan 30 F-02/F-05. ``accept_quality`` reached only ``mesh.gmsh.run``,
        so **Accept anyway** ran Gmsh whatever engine had made the mesh and a
        snappy user could not record an acceptance at all. The decision is
        bound to this engine's own QA task and to the report checkMesh just
        wrote, so it lapses when the mesh does.
        """
        if not command.parameters.get('accept_quality'):
            return None
        # CP-05 item 3 on this route. Generation and structural validity are
        # checkMesh's own verdict, which has already been read by the time
        # this is reached; boundary completeness is the part that verdict
        # states but does not itself refuse over.
        gap = checked_boundary_gap(verdict)
        if gap:
            raise PreconditionFailedError(gap, details={
                'operation': command.operation})
        from foammesh.core.engine.base import qa_task_id

        engine = self._engine_id(session)
        task = qa_task_id(engine)
        document = self._record_quality_override(
            session, None, None, engine_id=engine, task_id=task,
            reason=str(command.parameters.get('accept_reason') or ''))
        payload['quality_override'] = document
        # The verdict is the failing one the check just produced. Accepting
        # never rewrites it (§8.6): the override travels beside it so the
        # strip reads `waived` rather than flipping green.
        payload['quality_verdict'] = self._current_report(session, task) or {}
        return document

    async def _quality_su2_readiness(self, session: CaseSession,
                                     command: Command) -> OperationResult:
        """Qualify this mesh the way the saved selection asks, for SU2.

        The SU2 counterpart of :meth:`_mesh_check`, and route-aware for the
        same reason: a Gmsh project targeting SU2 needs the native element
        check as well as the readiness check, and a readiness pass on its own
        never stood for both.
        """
        return await self._run_route_checks(session, command)

    async def _run_su2_readiness(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        """Judge the mesh for SU2, and report what the check found.

        The counterpart of ``mesh.check`` for an SU2 project: same report
        file, same task-state effect, and no OpenFOAM runtime, because a Gmsh
        user meshing for SU2 has no reason to have one installed.

        Plan 31 DP-19: the precondition used to be ``mesh.check``'s -- "this
        case has a polyMesh" -- on the one target that deliberately publishes
        none, so the QA row for an SU2 project refused before the check was
        reached. It is now "this case has a mesh the readiness check can
        read", which is a polyMesh or the ``mesh.su2`` the accepted run wrote.
        """
        await self._ensure_reconstructed(session, command)
        self._require_readiness_mesh(session)
        from foammesh.core.quality.su2_readiness import check_su2_readiness

        verdict = check_su2_readiness(session.case_path)
        accepted = bool(verdict['mesh_ok']) and not verdict['incomplete']
        session.state.bus.publish(
            Event.ARTIFACT_QUALITY_CHANGED, operation=command.operation,
            job_id=None, artifacts=[], quality='su2-readiness')
        payload = {'readiness': verdict, 'report': verdict,
                   'succeeded': accepted}
        return OperationResult(
            'accepted' if accepted else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=tuple(verdict['warnings']), payload=payload)

    def _record_qa_run(self, session: CaseSession, command: Command,
                       *, warning: bool, waived: bool = False) -> dict | None:
        """Advance ``<engine>.qa`` on the strength of a checkMesh that ran.

        ``waived`` is R119's distinction: a check a human overrode is recorded
        as WAIVED, not as a pass with a warning beside it. The row is the only
        place the outline can show that somebody made a decision here.
        """
        from foammesh.core.engine.base import qa_task_id
        from foammesh.core.workflow.task_state_store import TaskStateError

        try:
            engine_id, store = self._task_state_store(session, command)
            task_id = qa_task_id(engine_id)
            if waived:
                return store.record_atomic_run_success(
                    [task_id], warning=warning, waived=(task_id,))
            return store.record_stage_success(task_id, warning=warning)
        except (TaskStateError, FacadeError, LookupError, OSError,
                ValueError) as error:
            # No engine selected yet, no store, or the graph refused: the
            # check still ran and its report stands; only the row is left.
            return {'advanced': [], 'blocked': {'reason': str(error)}}

    # -- QA-06: the route decides which checks run ------------------------- #

    async def _run_route_checks(self, session: CaseSession,
                                command: Command) -> OperationResult:
        """Run every check the saved Mesh setup selection asks for, in order.

        Plan 33 QA-06. The selection is the single source of truth about
        which checks a mesh owes, so it is read here and nowhere else in this
        run. Each check contributes one entry to
        ``foammesh/quality/route-checks.json``: what it answered, which mesh
        it answered about, and whether it ran now or stood already.

        Progression follows from the whole list rather than from whichever
        check happened to be pressed. ``PASSED`` needs every check; a check
        that never ran leaves the route ``INCOMPLETE`` and advances no row at
        all, because a row that settles on absent evidence is the fault this
        exists to stop.
        """
        from foammesh.core.engine.registry import configured_target_solver
        from foammesh.core.facade.quality_routes import (
            PASSED, compose_verdict, load_route_record, quality_checks_for,
            reusable_entry, route_key, save_route_record,
        )

        target = configured_target_solver(session.state.db)
        engine = self._engine_id(session)
        required = quality_checks_for(target, engine)
        route = route_key(target, engine)
        previous = load_route_record(session.case_path)
        terminal = required[-1]
        entries: list[dict] = []
        outcome: OperationResult | None = None
        for name in required:
            carried = None
            if name != terminal:
                # The terminal check is the one the user just pressed, so it
                # always runs; the evidence beside it is carried forward when
                # it still describes this mesh on this route.
                carried = reusable_entry(
                    previous, route, name,
                    self._route_check_identity(session, name))
            if carried is not None:
                entries.append(carried)
                continue
            entry, produced = await self._run_route_check(
                session, command, name)
            entries.append(entry)
            if name == terminal:
                outcome = produced
        verdict = compose_verdict(entries)
        # DP-869. A route that passed on a blemish advances its row as a
        # warning, never as a clean pass.
        blemished = [str(entry['blemish']) for entry in entries
                     if entry.get('blemish')]
        record = {
            'schema_version': 1, 'route': route, 'target_solver': target,
            'engine_id': engine, 'required': list(required),
            'checks': entries, 'verdict': verdict, 'blemish': blemished,
            'mesh_identity': entries[-1].get('mesh_identity', '')
            if entries else '',
        }
        save_route_record(session.case_path, record)

        payload = dict(outcome.payload) if outcome is not None else {}
        payload['route_checks'] = record
        missing = [entry['check'] for entry in entries
                   if entry.get('verdict') == 'missing']
        if missing:
            # Plan 37 UF4 DP-1026. This used to read "this route still owes
            # gmsh-native" in a payload nothing displayed: the row did not
            # move and nobody said why. Now it names the missing report in
            # words and what produces it, and says so at the top level too.
            reason = self._missing_route_reason(entries, missing)
            payload['task_state'] = {'advanced': [], 'blocked': {
                'reason': reason, 'missing': [str(name) for name in missing]}}
            payload['message'] = reason
        else:
            payload['task_state'] = self._record_qa_run(
                session, command, warning=verdict != PASSED or bool(blemished),
                waived='quality_override' in payload)
        warnings = tuple(outcome.warnings) if outcome is not None else ()
        return OperationResult(
            'accepted' if verdict == PASSED else 'failed',
            command.operation, session.revisions,
            invalidated_outputs=('quality',), warnings=warnings,
            payload=payload)

    @staticmethod
    def _missing_route_reason(entries: list, missing: list) -> str:
        """What a route with an absent report tells the user to do."""
        from foammesh.core.facade.quality_routes import NATIVE_GMSH_CHECK

        sentences = []
        for name in missing:
            if name == NATIVE_GMSH_CHECK:
                sentences.append(
                    'The Gmsh element check has produced no report for this '
                    'mesh, so Quality cannot be marked done. Run Generate '
                    'mesh again (the mesh run writes that report), then press '
                    'Check & Proceed.')
                continue
            label = next((str(entry.get('label') or name) for entry in entries
                          if entry.get('check') == name), str(name))
            sentences.append(
                '{0} has no result for this mesh, so Quality cannot be marked '
                'done. Press Check & Proceed again to run it.'.format(label))
        return ' '.join(sentences)

    async def _run_route_check(self, session: CaseSession, command: Command,
                               check: str):
        """Run one named check, and describe what it answered.

        Returns the record entry and, for a check that is an operation of its
        own, the result that operation produced -- the caller needs it to keep
        the payload the pressed operation has always returned.
        """
        from foammesh.core.facade.quality_routes import (
            CHECK_LABELS, NATIVE_GMSH_CHECK,
        )
        from foammesh.core.quality.checkmesh_service import (
            NATIVE_CHECK, SU2_READINESS_CHECK,
        )

        if check == NATIVE_GMSH_CHECK:
            return self._native_route_entry(session), None
        if check == NATIVE_CHECK:
            produced = await self._run_checkmesh(session, command)
        elif check == SU2_READINESS_CHECK:
            produced = await self._run_su2_readiness(session, command)
        else:                                               # pragma: no cover
            raise PreconditionFailedError(
                'no check is registered under this name',
                details={'check': check})
        entry = {
            'check': check, 'label': CHECK_LABELS.get(check, check),
            'status': 'ran',
            'verdict': 'passed' if produced.status == 'accepted' else 'failed',
            'mesh_identity': self._route_check_identity(session, check),
            'detail': self._route_check_detail(session, check),
        }
        # DP-869. Accepted with advisory findings only: the check passed the
        # policy, and the row must still say it was not clean.
        blemish = (produced.payload or {}).get('quality_blemish')
        if produced.status == 'accepted' and blemish:
            entry['blemish'] = str(blemish.get('line') or 'advisory findings')
        return entry, produced

    def _native_route_entry(self, session: CaseSession) -> dict:
        """What the generator's element gate already said about this mesh.

        Nothing re-runs it: it is produced by the meshing run itself and filed
        at ``foammesh/quality/mesh-quality.json``. Absence is therefore a
        missing check rather than a failed one -- the mesh was made by a run
        that did not measure it, and saying so is the honest answer.
        """
        from foammesh.core.facade.quality_routes import (
            CHECK_LABELS, NATIVE_GMSH_CHECK,
        )
        from foammesh.core.gmsh.quality import REPORT_TASK_ID

        report = self._current_report(session, REPORT_TASK_ID)
        entry = {'check': NATIVE_GMSH_CHECK, 'status': 'ran',
                 'label': CHECK_LABELS[NATIVE_GMSH_CHECK]}
        if not report:
            entry.update({
                'verdict': 'missing', 'mesh_identity': '',
                'detail': 'the Gmsh element check has produced no report for '
                          'this mesh'})
            return entry
        gate = str(report.get('gate_verdict')
                   or report.get('verdict') or '').lower()
        entry.update({
            # DP-910. A blemish is inside the allowance the user authored
            # and publishes on its own (`core.gmsh.quality`), so the route
            # does not call it failed; it carries the blemish instead.
            'verdict': 'passed' if gate in ('pass', 'blemish') else 'failed',
            'native_verdict': gate,
            'mesh_identity': str(report.get('subject_mesh_fingerprint')
                                 or report.get('report_fingerprint') or ''),
            'detail': self._native_route_detail(report) or gate,
        })
        if gate == 'blemish':
            entry['blemish'] = entry['detail'] or 'Gmsh element gate: blemish'
        return entry

    @staticmethod
    def _native_route_detail(report: dict) -> str:
        """The element gate's own words about the mesh it measured."""
        reason = str(report.get('reason') or '').strip()
        if reason:
            return reason
        measure = str(report.get('measure') or '').strip()
        if not measure:
            return ''
        return '{0} at least {1:g}, worst {2:g}'.format(
            measure, float(report.get('requestedMinimum') or 0.0),
            float(report.get('achievedMinimum') or 0.0))

    def _route_check_identity(self, session: CaseSession, check: str) -> str:
        """Which mesh a check's stored answer is about.

        The digest the check's own report carries, never one recomputed here:
        reuse is only safe when the identity travels with the evidence.
        """
        from foammesh.core.facade.quality_routes import NATIVE_GMSH_CHECK
        from foammesh.core.gmsh.quality import REPORT_TASK_ID
        from foammesh.core.quality.checkmesh_service import MeshCheckService

        if check == NATIVE_GMSH_CHECK:
            report = self._current_report(session, REPORT_TASK_ID) or {}
            return str(report.get('subject_mesh_fingerprint')
                       or report.get('report_fingerprint') or '')
        stored = MeshCheckService.load_report(session.case_path, check)
        return str(getattr(stored, 'mesh_fingerprint', '') or '')

    def _route_check_detail(self, session: CaseSession, check: str) -> str:
        from foammesh.core.quality.checkmesh_service import MeshCheckService

        stored = MeshCheckService.load_report(session.case_path, check)
        result = getattr(stored, 'result', None)
        return str(getattr(result, 'verdict', '') or '')

    def _current_quality_report(self, session: CaseSession):
        """The stored verdict of the check this project's target asks for.

        CP-05 item 2. Each check owns its own report slot, so the SU2
        readiness check no longer overwrites checkMesh's verdict on a mesh
        nobody changed. Which one is *current* therefore depends on the
        configured target, and is read through the one place that decides it
        (:meth:`_qa_operation`). The native report is the fallback rather
        than a blank: it names its own command, so nothing is mislabelled,
        and a project whose target check has not run yet is no worse off than
        it was before the slots were split.

        Plan 33 QA-06 narrows that fallback. It used to hand back the
        checkMesh verdict on every route, so switching a Gmsh project from
        OpenFOAM to SU2 kept showing the OpenFOAM numbers under the SU2
        question -- a result relabelled for a route that never asked for it.
        The fallback now applies only where the saved selection really does
        require checkMesh, which is every OpenFOAM route and no SU2 one.
        """
        from foammesh.core.engine.registry import configured_target_solver
        from foammesh.core.facade.quality_routes import quality_checks_for
        from foammesh.core.quality.checkmesh_service import (
            NATIVE_CHECK, MeshCheckService, check_for,
        )

        try:
            check = check_for(self._qa_operation(session))
        except Exception:                                   # noqa: BLE001
            check = NATIVE_CHECK
        report = MeshCheckService.load_report(session.case_path, check)
        if report is None and check != NATIVE_CHECK:
            try:
                required = quality_checks_for(
                    configured_target_solver(session.state.db),
                    self._engine_id(session))
            except Exception:                               # noqa: BLE001
                required = (NATIVE_CHECK,)
            if NATIVE_CHECK in required:
                report = MeshCheckService.load_report(session.case_path)
        return report

    def _quality_report(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.quality import waiver as waiver_module

        # CP-05 item 2. `run_id` asks about one candidate instead of about
        # whatever ran last. Without it this operation could only describe the
        # newest report, so a surface inspecting an accepted mesh after a
        # later candidate had been refused and waived showed that later
        # candidate's verdict and that later candidate's waiver. With it, the
        # answer comes from the named run's own record and nowhere else, and a
        # run nothing was ever filed against says so rather than borrowing.
        run_id = str(command.parameters.get('run_id') or '').strip()
        if run_id:
            from foammesh.core.quality import binding

            bound = binding.run_quality(session.case_path, run_id)
            if not bound['bound']:
                raise PreconditionFailedError(
                    'no quality report is bound to this run',
                    details={'run_id': run_id})
            return self._read_result(session, command, {
                'run_id': run_id,
                'report': bound['report'],
                'waivers': bound['waivers']})

        report = self._current_quality_report(session)
        # R119. checkMesh and the generator's element gate measure different
        # things, and on the measured run the first passed while the second
        # failed and was waived. A surface reading only this operation had no
        # way to know a human had overridden anything, so the strip repainted
        # `Quality limits: pass · quality: good` straight after Accept anyway.
        # Only waivers bound to the gate report currently on disk are carried:
        # the report is rewritten by every run, so a decision taken against an
        # earlier mesh lapses here exactly as §8.6 says it should.
        waivers = self._binding_waivers(session, waiver_module)
        return self._read_result(session, command, {
            'report': _to_payload(report) if report else None,
            'waivers': waivers,
            'route_checks': self._route_checks_payload(session)})

    def _route_checks_payload(self, session: CaseSession) -> dict:
        """What this project's route asks for, and what it has so far.

        Plan 33 QA-06. A page cannot say which checks are outstanding unless
        it is told, and it must not work that out from which page it is: the
        saved Mesh setup selection decides, so the answer is composed here and
        carried on the report the page already reads.
        """
        from foammesh.core.engine.registry import configured_target_solver
        from foammesh.core.facade.quality_routes import (
            CHECK_LABELS, INCOMPLETE, load_route_record, quality_checks_for,
            route_key, route_verdict,
        )

        try:
            target = configured_target_solver(session.state.db)
            engine = self._engine_id(session)
        except Exception:                                   # noqa: BLE001
            return {'required': [], 'checks': [], 'verdict': INCOMPLETE}
        required = quality_checks_for(target, engine)
        route = route_key(target, engine)
        record = load_route_record(session.case_path) or {}
        if str(record.get('route') or '') != route:
            record = {}
        stored = {str(entry.get('check')): entry
                  for entry in record.get('checks') or ()
                  if isinstance(entry, dict)}
        checks = []
        for name in required:
            entry = dict(stored.get(name) or {
                'check': name, 'verdict': 'missing', 'status': 'not-run',
                'mesh_identity': '', 'detail': ''})
            entry.setdefault('label', CHECK_LABELS.get(name, name))
            checks.append(entry)
        return {'route': route, 'target_solver': target, 'engine_id': engine,
                'required': list(required), 'checks': checks,
                'verdict': route_verdict(session.case_path, target, engine)}

    def _mesh_repair_recommendations(
            self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.quality.checkmesh_service import MeshCheckService
        from foammesh.core.quality.repair_recommendations import recommendations
        # Deliberately the native report, not `_current_quality_report`. Every
        # recommendation this returns is an operation on a `constant/polyMesh`
        # -- renumber, subset, a cell set to delete -- and a project targeting
        # SU2 has no polyMesh to run them against. Reading the readiness
        # verdict here would answer a question about a mesh that is not there.
        report = MeshCheckService.load_report(session.case_path)
        if report is None:
            raise PreconditionFailedError('no current quality report exists')
        items = recommendations(report.result)
        return self._read_result(session, command, {
            'mesh_fingerprint': report.mesh_fingerprint,
            'recommendations': items, 'recommendation_count': len(items),
        })

    def _quality_check_artifacts(self, session: CaseSession,
                                 command: Command) -> OperationResult:
        """Plan 37 UF18: the newest check's written outputs, with a reason
        for each one that cannot be drawn. Reads the manifest only."""
        from foammesh.core.quality import check_artifacts
        check = str(command.parameters.get('check') or 'openfoam')
        manifest = check_artifacts.load_manifest(session.case_path, check=check)
        return self._read_result(session, command, dict(manifest or {}))

    def _quality_failed_sets(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.quality.failed_cells import (
            discover_set_details, read_set_file)
        details = discover_set_details(session.case_path)
        requested = command.parameters.get('set_name')
        if requested is not None:
            details = [item for item in details if item['name'] == requested]
            if not details:
                raise PreconditionFailedError('failed set does not exist', details={
                    'set_name': requested})
        include_ids = bool(command.parameters.get('include_ids', False))
        if include_ids:
            limit = int(command.parameters.get('limit', 10_000))
            if limit < 1 or limit > 100_000:
                raise ValidationFailedError('failed-set limit must be between 1 and 100000')
            details = [{**item, 'ids': read_set_file(item['path'])[:limit]}
                       for item in details]
        return self._read_result(session, command, {
            'sets': details, 'set_count': len(details)})

    def _quality_failed_set_select(self, session: CaseSession,
                                   command: Command) -> OperationResult:
        from foammesh.core.quality.failed_cells import discover_set_details, read_set_file
        name = command.parameters.get('set_name')
        if not isinstance(name, str) or not name:
            raise ValidationFailedError('set_name is required')
        detail = next((item for item in discover_set_details(session.case_path)
                       if item['name'] == name), None)
        if detail is None:
            raise PreconditionFailedError('failed set does not exist', details={'set_name': name})
        ids = read_set_file(detail['path'])
        field_type = {'cellSet': 'CELL', 'faceSet': 'FACE', 'pointSet': 'POINT'}.get(
            detail['kind'], 'CELL')
        return self._read_result(session, command, {
            'selection': {'set_name': name, 'entity_kind': detail['kind'],
                          'field_type': field_type, 'ids': ids},
            'count': len(ids)})

    def _quality_compare(self, session: CaseSession, command: Command) -> OperationResult:
        import json
        from foammesh.core.quality.checkmesh_service import (
            QualityReport, compare_reports)
        # The comparison has to be against the same check the rest of the
        # product is showing, or a project targeting SU2 compares a readiness
        # baseline with a checkMesh current and reports the difference between
        # two different questions. On an SU2 project this used to find no
        # `latest.json` at all and refuse, on a mesh that had been checked.
        current = self._current_quality_report(session)
        if current is None:
            raise PreconditionFailedError('no current quality report exists')
        baseline_value = command.parameters.get('baseline')
        if isinstance(baseline_value, dict):
            baseline_data = baseline_value
        elif isinstance(baseline_value, str) and baseline_value:
            path = Path(baseline_value)
            if not path.is_file():
                raise PreconditionFailedError('baseline quality report does not exist', details={
                    'baseline': baseline_value})
            try:
                baseline_data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as error:
                raise ValidationFailedError(f'could not read baseline report: {error}') from error
        else:
            raise ValidationFailedError('baseline must be a report object or JSON path')
        try:
            baseline = QualityReport.from_dict(baseline_data)
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(f'invalid baseline quality report: {error}') from error
        return self._read_result(session, command, compare_reports(baseline, current))

    def _quality_report_export(self, session: CaseSession,
                               command: Command) -> OperationResult:
        import csv
        import json
        import os
        # The report on screen, not the native slot. Exporting is the user
        # asking for a copy of the verdict they were just shown; on an SU2
        # project that verdict is the readiness check, and this refused with
        # 'no current quality report exists' rather than writing it.
        report = self._current_quality_report(session)
        if report is None:
            raise PreconditionFailedError('no current quality report exists')
        destination = self._destination(session, command)
        suffix = destination.suffix.lower()
        if suffix not in {'.json', '.csv'}:
            raise ValidationFailedError('quality report destination must end with .json or .csv')
        if destination.exists():
            raise ValidationFailedError('quality report destination already exists', details={
                'destination': str(destination)})
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f'{destination.suffix}.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='') as output:
                if suffix == '.json':
                    json.dump(report.to_dict(), output, indent=2, sort_keys=True)
                    output.write('\n')
                else:
                    writer = csv.writer(output)
                    writer.writerow(('metric', 'value'))
                    for key, value in report.result.to_dict().items():
                        writer.writerow((key, json.dumps(value) if isinstance(value, (list, dict)) else value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return OperationResult(
            'accepted', command.operation, session.revisions,
            payload={'destination': str(destination), 'format': suffix.removeprefix('.'),
                     'bytes': destination.stat().st_size})

    def _mesh_repair_preview(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.mesh.repair import MeshRepairService
        operations = MeshRepairService().available_operations(session.case_path)
        return self._read_result(session, command, {
            'available': [op.value for op in operations]})

    async def _mesh_repair(self, session: CaseSession, command: Command) -> OperationResult:
        self._require_mesh(session)
        operation = command.parameters.get('repair')
        if not operation:
            raise ValidationFailedError('a repair operation is required')
        from foammesh.core.mesh.repair import RepairOperation
        try:
            repair = RepairOperation(operation)
        except ValueError as error:
            raise ValidationFailedError('unknown repair operation', details={
                'repair': operation}) from error
        utility = await self._utility_ready(repair.utility_name)
        registry = self._capabilities_registry()
        launcher = getattr(registry, 'command', None)
        from foammesh.core.mesh.repair import MeshRepairService, RepairRequest
        request = RepairRequest(repair, command.parameters.get('cell_set'))
        try:
            result = await MeshRepairService(
                {repair.utility_name: (
                    repair.utility_name if launcher is not None else utility)},
                session.jobs,
                check_utility=('checkMesh' if launcher is not None else None),
                launcher=launcher).run(
                    session.case_path, request,
                    subset_destination=command.parameters.get('subset_destination'),
                    on_line=command.parameters.get('on_line'))
        except (OSError, ValueError, RuntimeError) as error:
            raise ValidationFailedError(str(error)) from error
        if result.succeeded:
            session.state.bus.publish(
                Event.ARTIFACT_MESH_CHANGED, operation=command.operation,
                destination=str(result.case_path))
        return OperationResult(
            'accepted' if result.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            payload=result.to_dict())

    def _mesh_recovery_list(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.mesh.recovery import MeshRecoveryService
        service = MeshRecoveryService()
        points = service.list_points(session.case_path)
        return self._read_result(session, command, {
            'recovery_points': [
                {**point.to_dict(), **service.manifest_payload(point)} for point in points]})

    def _mesh_restore(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        from foammesh.core.mesh.recovery import MeshRecoveryService
        service = MeshRecoveryService()
        if not service.has_available(session.case_path):
            raise PreconditionFailedError('no recovery point is available')
        outcome = service.restore_previous(session.case_path)
        return self._artifact_result(session, command, _to_payload(outcome),
                                     event=Event.ARTIFACT_RESTORED)

    async def _transform(self, session: CaseSession, command: Command, kind: str) -> OperationResult:
        self._require_mesh(session)
        utility = await self._utility_ready('transformPoints')
        registry = self._capabilities_registry()
        from foammesh.core.mesh import MeshInfoService, MeshTransformRequest
        try:
            vector = tuple(float(value) for value in command.parameters.get('vector', ()))
            pivot_value = command.parameters.get('pivot')
            pivot = tuple(float(value) for value in pivot_value) if pivot_value is not None else None
            request = MeshTransformRequest(
                kind, vector, command.parameters.get('angle_degrees'), pivot,
                bool(command.parameters.get('transform_fields', False)))
            semantic_argv = list(
                request.argv('transformPoints', session.case_path)[1:])
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        if request.transform_fields:
            if kind != 'rotate':
                raise ValidationFailedError('field transformation is supported only for rotation')
            semantic_argv.insert(1, '-rotateFields')
        if hasattr(registry, 'command'):
            launch = registry.command(
                'transformPoints', semantic_argv, cwd=session.case_path)
        else:
            from foammesh.core.openfoam_runtime import LaunchCommand
            launch = LaunchCommand((utility, *semantic_argv))
        before = MeshInfoService().inspect(session.case_path).to_dict()
        poly_mesh = session.case_path / 'constant' / 'polyMesh'
        required = ('points', 'faces', 'owner', 'neighbour', 'boundary')

        def valid_mesh(path: Path) -> bool:
            return path.is_dir() and all((path / name).is_file() for name in required)

        def inspect_after(_result):
            from foammesh.core.mesh.transform import MeshTransformService
            try:
                MeshTransformService._record_mutation(session.case_path, request)
            except (FileNotFoundError, ValueError):
                # Legacy cases may not yet carry workflow sidecar metadata;
                # mesh validation and artifact history remain authoritative.
                pass
            return {'after': MeshInfoService().inspect(session.case_path).to_dict()}

        execution = await self._context(session).executor.execute(session, OperationSpec(
            operation=command.operation, argv=launch.argv, cwd=session.case_path,
            mutation=True, timeout=command.parameters.get('timeout_seconds', 300),
            max_output_bytes=4 * 1024 * 1024,
            expected_artifacts=(ExpectedArtifact(
                poly_mesh, kind='polyMesh', validator=valid_mesh),),
            parser=inspect_after, recover_mesh=True,
            artifact_event=Event.ARTIFACT_MESH_CHANGED,
            invalidated_outputs=('quality',),
            cleanup_argv=launch.cleanup_argv,
        ), on_line=command.parameters.get('on_line'))
        payload = execution.to_payload()
        payload.update({'transform': kind, 'before': before,
                        'after': (execution.parsed or {}).get('after')})
        return OperationResult(
            'accepted' if execution.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=execution.warnings, payload=payload)

    async def _mesh_rotate(self, session, command):
        return await self._transform(session, command, 'rotate')

    async def _mesh_translate(self, session, command):
        return await self._transform(session, command, 'translate')

    async def _mesh_scale(self, session, command):
        return await self._transform(session, command, 'scale')

    async def _mesh_extrude(self, session: CaseSession, command: Command) -> OperationResult:
        self._require_mesh(session)
        utility = await self._utility_ready('extrudeMesh')
        registry = self._capabilities_registry()
        import math
        import os
        import re
        source_patch = command.parameters.get('source_patch')
        exposed_patch = command.parameters.get('exposed_patch')
        model = command.parameters.get('model', 'plane')
        safe_name = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*$')
        if not all(isinstance(value, str) and safe_name.fullmatch(value)
                   for value in (source_patch, exposed_patch)):
            raise ValidationFailedError('source_patch and exposed_patch must be safe patch names')
        if model not in {'plane', 'wedge'}:
            raise ValidationFailedError('extrude model must be plane or wedge')
        data = {
            'constructFrom': 'patch', 'sourceCase': '"."',
            'sourcePatches': [source_patch], 'exposedPatchName': exposed_patch,
            'extrudeModel': model, 'flipNormals': False, 'mergeFaces': False,
        }
        try:
            if model == 'plane':
                thickness = float(command.parameters.get('thickness'))
                if not math.isfinite(thickness) or thickness <= 0:
                    raise ValueError('plane thickness must be positive and finite')
                data['thickness'] = thickness
            else:
                point = tuple(float(v) for v in command.parameters.get('point', ()))
                axis = tuple(float(v) for v in command.parameters.get('axis', ()))
                angle = float(command.parameters.get('angle'))
                if (len(point) != 3 or len(axis) != 3 or
                        not all(math.isfinite(v) for v in (*point, *axis, angle)) or
                        sum(v * v for v in axis) == 0):
                    raise ValueError('wedge point, axis, and angle must be finite and valid')
                data['sectorCoeffs'] = {'point': point, 'axis': axis, 'angle': angle}
        except (TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        from foammesh.openfoam.dict_format import format_dictionary_file
        dictionary = session.case_path / 'system' / 'extrudeMeshDict'
        dictionary.parent.mkdir(parents=True, exist_ok=True)
        temporary = dictionary.with_suffix('.tmp')
        temporary.write_text(
            format_dictionary_file('extrudeMeshDict', data), encoding='utf-8')
        os.replace(temporary, dictionary)
        region = command.parameters.get('region')
        semantic_argv = []
        if region:
            if not isinstance(region, str) or not safe_name.fullmatch(region):
                raise ValidationFailedError('region must be a safe OpenFOAM name')
            semantic_argv.extend(('-region', region))
        semantic_argv.extend(
            ('-dict', 'system/extrudeMeshDict',
             '-case', str(session.case_path)))
        if hasattr(registry, 'command'):
            launch = registry.command(
                'extrudeMesh', semantic_argv, cwd=session.case_path)
        else:
            from foammesh.core.openfoam_runtime import LaunchCommand
            launch = LaunchCommand((utility, *semantic_argv))
        poly_mesh = session.case_path / 'constant' / 'polyMesh'
        required = ('points', 'faces', 'owner', 'neighbour', 'boundary')
        execution = await self._context(session).executor.execute(session, OperationSpec(
            operation=command.operation, argv=launch.argv, cwd=session.case_path,
            mutation=True, timeout=command.parameters.get('timeout_seconds', 300),
            max_output_bytes=4 * 1024 * 1024,
            expected_artifacts=(ExpectedArtifact(
                poly_mesh, kind='polyMesh', validator=lambda path: path.is_dir() and
                all((path / name).is_file() for name in required)),),
            recover_mesh=True, artifact_event=Event.ARTIFACT_MESH_CHANGED,
            invalidated_outputs=('quality',),
            cleanup_argv=launch.cleanup_argv,
        ), on_line=command.parameters.get('on_line'))
        payload = execution.to_payload()
        payload.update({'model': model, 'source_patch': source_patch,
                        'exposed_patch': exposed_patch, 'region': region})
        return OperationResult(
            'accepted' if execution.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=execution.warnings, payload=payload)

    # -- Slice 6: import / export / conversion ----------------------------- #

    def _import_native(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        from foammesh.core.import_export.native_mesh import NativeMeshImportService
        source = self._source(command)
        destination = command.parameters.get('copy_destination')
        if destination:
            result = NativeMeshImportService().import_into_copy(
                source, session.case_path, Path(destination))
            return self._read_result(session, command, {
                'target_case': str(result.target_case),
                'source_case': str(result.source_case),
                'fingerprint': result.fingerprint})
        result = NativeMeshImportService().import_from_case(source, session.case_path)
        return self._artifact_result(session, command, _to_payload(result))

    async def _import_converter(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        source = self._source(command)
        fmt = command.parameters.get('format')
        from foammesh.core.import_export.converter import (
            ConverterFormat, ConverterImportService, ConverterRequest)
        try:
            converter = ConverterFormat(fmt)
        except ValueError as error:
            raise ValidationFailedError('unknown converter format', details={'format': fmt}) from error
        await self._utility_ready(converter.utility_name)
        registry = self._capabilities_registry()
        try:
            result = await ConverterImportService(
                {converter.utility_name: converter.utility_name}, session.jobs,
                launcher=registry.command).import_file(
                    session.case_path, ConverterRequest(converter, source))
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        if result.succeeded:
            session.state.bus.publish(
                Event.ARTIFACT_MESH_CHANGED, operation=command.operation,
                fingerprint=result.fingerprint)
        return OperationResult(
            'accepted' if result.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            payload=result.to_dict())

    def _quality_fidelity(self, session: CaseSession,
                          command: Command) -> OperationResult:
        """Plan 23 §5. Measure the published mesh and write the report.

        Assembled here rather than in `core` because only the facade knows
        where a case keeps its identity sidecar, its validation reference and
        its tolerance settings. The measurement itself is `run.measure_case`,
        which this must not duplicate: two implementations of a verdict is how
        a report comes to disagree with the artifact it summarises.

        Every missing input yields `unrated` with a stated reason rather than
        an error. A case with no prepared revision is not a broken case, it is
        a case nothing can be measured against, and §4 requires that to be
        reported rather than raised.
        """
        from foammesh.core.quality import patch_identity
        from foammesh.core.quality.geometry_fidelity import (
            report as report_module, run as run_module, tolerance_source,
        )

        task_id = str(command.parameters.get('task_id')
                      or 'common.fidelity')
        if task_id not in report_module.REPORT_PATHS:
            raise ValidationFailedError(
                f'{task_id!r} is not a fidelity task; expected one of '
                f'{sorted(report_module.REPORT_PATHS)}')

        case_path = Path(session.case_path)
        policy = tolerance_source.for_case(case_path, db=session.state.db)
        evidence = self._current_evidence(session)

        refusals: list = []
        model = self._boundary_model(session, patch_identity,
                                     boundary_only=True, refusals=refusals)
        if model is None:
            report = report_module.build(
                task_id, (), evidence=evidence, policy=policy)
            path = report_module.write(case_path, report)
            return self._read_result(session, command, {
                'report': report.to_dict(), 'path': str(path),
                **_unreadable_mesh(refusals)})

        reference_for = self._section_reference(
            session, str(evidence.get('prepared_revision') or ''),
            policy=policy)
        hotspots: dict = {}
        featureResults: dict = {}
        features_for = self._section_features(
            session, str(evidence.get('prepared_revision') or ''))
        report = run_module.measure_case(
            task_id=task_id, mesh=model.mesh, sections=model.sections,
            reference_for=reference_for, policy=policy, evidence=evidence,
            budget_seconds=self._fidelity_budget_seconds(session),
            hotspots=hotspots, features_for=features_for,
            feature_results=featureResults)
        path = report_module.write(case_path, report)
        # Kept out of the JSON report: one float per boundary face is millions
        # of numbers on the meshes this matters for, and a report a human is
        # meant to read should not carry them.
        from foammesh.core.quality.geometry_fidelity import hotspot as hotspot_module

        hotspot_path = hotspot_module.write(case_path, task_id, hotspots)
        features_path = hotspot_module.write_features(
            case_path, task_id,
            self._drawable_features(features_for, model.sections,
                                    featureResults))
        return self._read_result(session, command, {
            'report': report.to_dict(), 'path': str(path),
            'hotspot_path': str(hotspot_path) if hotspot_path else '',
            'features_path': str(features_path) if features_path else '',
            'features': {name: [item.to_dict() for item in results]
                         for name, results in featureResults.items()},
            'unresolved_tolerance_levels': list(
                tolerance_source.unresolved_levels(policy))})

    def _quality_resolution(self, session: CaseSession,
                            command: Command) -> OperationResult:
        """Plan 23 §6.5/§6.6. Are there enough cells where it matters?

        The producer `common.resolution` never had: the task was run-gated,
        the summary read its report, and nothing wrote one -- so the tree
        route stopped at Resolution Adequacy for both engines. Assembled here
        for the same reason as fidelity: only the facade knows the case's
        identity sidecar, its feature manifest and what size the engine was
        asked for. The measurement is `resolution.run.measure_case`.

        Every missing input is `unrated` with a stated reason. A case whose
        requested size cannot be established still gets its channels walked;
        the size comparison alone is what it cannot have.
        """
        from foammesh.core.quality import patch_identity
        from foammesh.core.quality.geometry_fidelity import (
            report as report_module, tolerance_source,
        )
        from foammesh.core.quality.resolution import run as resolution_run

        task_id = str(command.parameters.get('task_id')
                      or resolution_run.TASK_ID)
        if task_id != resolution_run.TASK_ID:
            raise ValidationFailedError(
                f'{task_id!r} is not the resolution task; expected '
                f'{resolution_run.TASK_ID!r}')

        case_path = Path(session.case_path)
        policy = tolerance_source.for_case(case_path, db=session.state.db)
        evidence = self._current_evidence(session)

        refusals: list = []
        model = self._boundary_model(session, patch_identity,
                                     refusals=refusals)
        if model is None:
            report = report_module.build(
                task_id, (), evidence=evidence, policy=policy)
            path = report_module.write(case_path, report)
            return self._read_result(session, command, {
                'report': report.to_dict(), 'path': str(path),
                'requested_size': None, **_unreadable_mesh(refusals)})

        requested = self._requested_cell_size(session, model.mesh)
        features_for = self._section_features(
            session, str(evidence.get('prepared_revision') or ''))
        report = resolution_run.measure_case(
            task_id=task_id, mesh=model.mesh, sections=model.sections,
            requested=requested, evidence=evidence, policy=policy,
            features_for=features_for,
            budget_seconds=self._fidelity_budget_seconds(session))
        path = report_module.write(case_path, report)
        return self._read_result(session, command, {
            'report': report.to_dict(), 'path': str(path),
            'requested_size': requested.to_dict()})

    def _requested_cell_size(self, session: CaseSession, mesh):
        """What element size the engine was asked for, and where that came from.

        Plan 30 F-14. The derivation itself is the engine's -- Gmsh's target
        size times its size factor, snappy's base-grid cell divided by two to
        the power of the deepest surface refinement level -- so it lives on
        the engine and this asks for it. A value that cannot be established is
        reported as such rather than guessed, because a ratio against a
        guessed size is a number that looks like evidence.
        """
        import numpy as np

        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, EngineNotRegisteredError,
        )
        from foammesh.core.quality.resolution.run import RequestedSize

        points = np.asarray(getattr(mesh, 'points', ()), dtype=np.float64)
        bounds = None
        if len(points):
            low, high = points.min(axis=0), points.max(axis=0)
            bounds = (float(low[0]), float(high[0]), float(low[1]),
                      float(high[1]), float(low[2]), float(high[2]))
        try:
            engine = ENGINE_REGISTRY.get(self._engine_id(session))
        except (EngineNotRegisteredError, LookupError):
            return RequestedSize(
                None, 'unavailable',
                'this case has not chosen a meshing method, so nothing asked '
                'for a cell size')
        size, source, reason = engine.requested_cell_size(
            session.state.db, bounds=bounds,
            hex_bounds=self._bounding_hex_bounds(session) or bounds)
        return RequestedSize(size, source, reason)

    def _generation_bounds(self, session: CaseSession):
        """DP-576. The imported geometry's extent, else the bounding hex's."""
        try:
            surface = self._assembled_surface(session)
        except Exception:                                   # noqa: BLE001
            surface = None
        if surface is not None and surface.GetNumberOfPoints() > 0:
            return list(surface.GetBounds())
        hex_bounds = self._bounding_hex_bounds(session)
        return list(hex_bounds) if hex_bounds is not None else None

    def _bounding_hex_bounds(self, session: CaseSession):
        """The base-grid hex the case names, as six bounds, or ``None``."""
        db = session.state.db
        try:
            selected = db.getValue('baseGrid/boundingHex6')
        except Exception:                                   # noqa: BLE001
            return None
        if selected is None:
            return None
        try:
            geometries = dict(db.getElements('geometry'))
        except Exception:                                   # noqa: BLE001
            return None
        element = geometries.get(selected)
        if element is None:
            element = geometries.get(str(selected))
        if element is None:
            try:
                element = geometries.get(int(selected))
            except (TypeError, ValueError):
                element = None
        if element is None:
            return None
        try:
            p1 = [float(item) for item in element.vector('point1')]
            p2 = [float(item) for item in element.vector('point2')]
        except Exception:                                   # noqa: BLE001
            return None
        return (min(p1[0], p2[0]), max(p1[0], p2[0]),
                min(p1[1], p2[1]), max(p1[1], p2[1]),
                min(p1[2], p2[2]), max(p1[2], p2[2]))

    def _drawable_features(self, features_for, sections, results) -> dict:
        """Feature polylines paired with the verdict measured on them.

        The polyline travels with its verdict rather than being looked up again
        at draw time: a feature manifest belongs to a *prepared revision*, and a
        later revision would move the geometry out from under a verdict
        measured against the old one.
        """
        from foammesh.core.quality.geometry_fidelity.features import (
            polyline_points)

        if not features_for or not results:
            return {}

        drawable: dict = {}
        byName = {str(getattr(section, 'solver_name', '')): section
                  for section in sections}
        for name, measured in results.items():
            section = byName.get(name)
            if section is None:
                continue
            declared = {str(getattr(item, 'feature_uuid', '')): item
                        for item in (features_for(section) or ())}
            records = []
            for result in measured:
                feature = declared.get(str(result.feature_uuid))
                points = polyline_points(feature) if feature is not None else None
                if points is None or len(points) < 2:
                    continue
                records.append({
                    'feature_uuid': result.feature_uuid,
                    'verdict': result.verdict,
                    'critical': bool(result.critical),
                    'max_distance': float(result.max_distance),
                    'length_coverage': float(result.length_coverage),
                    'points': [[float(value) for value in point]
                               for point in points],
                })
            if records:
                drawable[name] = records
        return drawable

    def _boundary_model(self, session: CaseSession, patch_identity, *,
                        boundary_only: bool = False, refusals=None):
        """The reconciled boundary, or ``None`` when it cannot be built.

        ``boundary_only`` (Plan 35 CR2) reads the boundary faces and their
        points, not the volume -- all fidelity measures. ``refusals``, a list,
        receives the reader's refusal code when the mesh could not be read,
        so the report can say *why* it is unrated. ``MemoryError`` is not a
        refusal: it propagates, and the worker reports ``over_budget``.
        """
        from foammesh.core.quality.geometry_fidelity import boundary

        case_path = Path(session.case_path)
        # `_write_patch_identity` puts the sidecar under `quality/geometry/`,
        # beside the validation reference it joins to. This read it from
        # `quality/` -- so no case ever had a boundary model, and every
        # fidelity report was `unrated` with the same reason a case that
        # genuinely lacked a sidecar would give. MEASURED on a live Gmsh
        # run: `fidelity.json` rated 0 sections while the sidecar sat one
        # directory down. The old location is still honoured for cases
        # written before the producer moved.
        quality = case_path / 'foammesh' / 'quality'
        identity = None
        for sidecar in (quality / 'geometry' / patch_identity.IDENTITY_FILENAME,
                        quality / patch_identity.IDENTITY_FILENAME):
            try:
                identity = patch_identity.read(sidecar)
                break
            except (OSError, ValueError):
                continue
        if identity is None:
            return None
        try:
            return boundary.load(case_path, identity,
                                 boundary_only=boundary_only)
        except MemoryError:
            raise
        except Exception as error:                          # noqa: BLE001
            if refusals is not None:
                refusals.append({
                    'reason': str(getattr(error, 'reason', '')
                                  or type(error).__name__),
                    'message': str(error)})
            return None

    def _section_features(self, session: CaseSession, revision_id: str = ''):
        """``section -> the declared features it should carry``.

        Plan 23 §6.3's rule -- a critical feature below threshold fails its
        section whatever the surface score says -- was implemented in
        ``features.section_verdict`` and never invoked, because nothing ever
        supplied the features. So a blade edge rounded away passed on its
        area-weighted surface score, which is verbatim the failure the feature
        check was written to catch.

        Features are owned by patch uuid, so a section carries the features
        whose ``owner_patch_uuids`` name it. A feature with no owner is not
        attributed to every section: that would fail whichever section happened
        to be measured first.
        """
        from foammesh.core.geometry.features import manifest as manifest_module

        if not revision_id:
            return None
        try:
            document = manifest_module.FeatureManifestStore(
                session.case_path).read(revision_id)
        except manifest_module.FeatureManifestError:
            return None
        if document is None:
            return None

        def resolve(section):
            uuid = str(getattr(section, 'patch_uuid', '') or '')
            return document.for_patch(uuid) if uuid else ()

        return resolve

    def _section_reference(self, session: CaseSession, revision_id: str = '',
                           policy=None):
        """``section -> (vertices, triangles)``, or ``None`` per section.

        A section whose reference is absent is `unrated`, which is what its
        absence means -- so this returns ``None`` rather than substituting an
        unrelated body's reference. A section is measured against the surface
        of the geometry it was cut from, never against another one.

        R177: the surfaces are keyed by ``geometry_id`` -- the imported body --
        and this looked them up by ``patch_uuid``. A patch UUID is never a
        geometry id, so the join could not match even once the reference was
        built and readable, and every section still read "no reference for
        section". The prepared revision's group manifest is what relates the
        two, so it is what does the joining.
        """
        surfaces = self._validation_reference_surfaces(
            session, revision_id, policy=policy)
        faces = self._reference_face_subsets(
            session, revision_id, policy=policy)
        keys = self._patch_reference_keys(session, revision_id)

        def resolve(section):
            uuid = str(getattr(section, 'patch_uuid', '') or '')
            key = keys.get(uuid)
            if key is None:
                return None
            geometry_id, face_indices = key
            subsets = faces.get(geometry_id) or {}
            if subsets:
                selected = [subsets[index] for index in face_indices
                            if index in subsets]
                if len(selected) != len(face_indices) or not selected:
                    # A boundary whose faces the reference does not carry is
                    # unrated. The alternative -- falling back to the body --
                    # is the defect this resolves.
                    return None
                return _merge_surfaces(selected)
            whole = surfaces.get(geometry_id)
            if whole is None:
                return None
            if len(self._patches_of(keys, geometry_id)) > 1:
                # An untagged reference (a tessellated CAD body is one
                # `.vtp`, faceless) cannot say which part of itself this
                # boundary is. Measuring against the whole body reports the
                # body's extent, so this stays unrated and says why.
                return None
            return whole

        return resolve

    @staticmethod
    def _patch_reference_keys(session: CaseSession, revision_id: str) -> dict:
        """``patch_uuid -> (geometry_id, face indices)`` for one revision.

        The group manifest is the record of which body each boundary was cut
        from *and* which faces of that body it covers. A patch it does not
        name gets no reference rather than a plausible one: guessing here
        would measure a boundary against a surface it never belonged to.

        R179: the body alone is not enough. A merged boundary spans several
        faces and a split one covers a single face, so the faces come along
        -- they are what makes the reference a reference for *this* boundary.
        """
        if not revision_id:
            return {}
        path = (Path(session.case_path) / 'foammesh' / 'geometry' / 'prepared'
                / str(revision_id) / 'group-manifest.json')
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        mapping = {}
        for group in document.get('groups') or ():
            uuid = str(group.get('patch_uuid') or '')
            geometry_id = str(group.get('geometry_id') or '')
            if not uuid or not geometry_id:
                continue
            faces = []
            for reference in group.get('source_refs') or ():
                index = reference.get('face_index')
                if index is not None:
                    faces.append(int(index))
            mapping[uuid] = (geometry_id, tuple(faces))
        return mapping

    @staticmethod
    def _patches_of(keys: dict, geometry_id: str) -> tuple:
        """Every patch cut from one body."""
        return tuple(uuid for uuid, key in keys.items()
                     if key[0] == geometry_id)

    def _reference_face_subsets(self, session: CaseSession,
                                revision_id: str = '', policy=None) -> dict:
        """``geometry_id -> {face index: (vertices, triangles)}`` (R179).

        Empty for a body whose reference records no faces -- a tessellated
        CAD body is written as one `.vtp` and carries none. That emptiness is
        load-bearing: it is what tells the caller it may not attribute part of
        this surface to one boundary.
        """
        from foammesh.core.geometry.validation import surface as surface_mod

        subsets = {}
        for geometry_id, polydata in self._reference_polydata(
                session, revision_id, policy=policy):
            try:
                found = surface_mod.as_arrays_by_solid(polydata)
            except (ValueError, RuntimeError, AttributeError):
                continue
            if found:
                subsets[geometry_id] = found
        return subsets

    def _validation_reference_surfaces(self, session: CaseSession,
                                       revision_id: str = '',
                                       policy=None) -> dict:
        """``geometry_id -> (vertices, triangles)`` from the staged reference.

        Reads what `ValidationReference` actually records -- a `sources` tuple
        of `SurfaceRecord`, each naming a written `.vtp` -- rather than asking
        the store for a per-patch mapping it does not provide. An earlier
        version called a `surfaces_by_patch()` that does not exist, caught the
        `AttributeError` and returned `{}`. Every section would then have been
        `unrated` forever, indistinguishable from a case that genuinely has no
        reference, and no test would have failed. That is the fourth instance
        in this plan of a lookup keyed on something its producer does not
        write, so the guessing is replaced by the real field and the silent
        catch by a narrow one.

        Empty is still the answer when no reference was materialized, because
        then there is genuinely nothing to measure against.

        R176: which reference is *the* reference is a question of tolerance.
        The store keys each one by the deflection §16.1 asked for, and this
        read a fixed 0.0 -- the tag a reference sized by no tolerance at all
        gets. So it could only ever find the unrated one, and a correctly
        sized reference sitting in the sibling directory was invisible. The
        deflection now comes from the policy that is about to be measured
        against, and when nothing is staged there, one is built.
        """
        from foammesh.core.geometry.validation import surface as surface_mod

        surfaces = {}
        for geometry_id, polydata in self._reference_polydata(
                session, revision_id, policy=policy):
            try:
                surfaces[geometry_id] = surface_mod.as_arrays(polydata)
            except (ValueError, RuntimeError, AttributeError):
                continue
        return surfaces

    def _reference_polydata(self, session: CaseSession, revision_id: str = '',
                            policy=None):
        """``(geometry_id, polydata)`` for every surface the reference records.

        Split out of `_validation_reference_surfaces` for R179, so the same
        staged reference can be read once as whole bodies and once as the
        faces inside them without duplicating the store lookup, the deflection
        it is keyed by, or the build-on-demand fallback.
        """
        from foammesh.core.geometry.validation import reference as reference_mod
        from foammesh.core.geometry.validation import surface as surface_mod
        from foammesh.core.quality.geometry_fidelity import tolerance_source

        if not revision_id:
            return
        tau_min = tolerance_source.tightest(policy)
        deflection = reference_mod.budget_for(tau_min).linear_deflection
        store = reference_mod.ValidationReferenceStore(session.case_path)
        document = store.read(revision_id, deflection)
        if document is None or not getattr(document, 'sources', ()):
            document = self._build_validation_reference(
                session, revision_id, tau_min)
        if document is None:
            return

        for record in getattr(document, 'sources', ()) or ():
            geometry_id = str(self._source_field(record, 'geometry_id') or '')
            path = self._source_field(record, 'path')
            if not geometry_id or not path:
                continue
            try:
                polydata = surface_mod.read_polydata(path)
            except (OSError, ValueError, RuntimeError, AttributeError):
                # A reference file named but unreadable leaves that geometry
                # without a reference, which is `unrated` -- not a failure of
                # the whole check.
                continue
            # MEASURED, the first time this ever ran: VTK's XML reader does not
            # raise on a corrupt `.vtp`. It logs, returns a polydata, and
            # leaves `GetPoints()` as None -- so `as_arrays` raises
            # `AttributeError`, which no plausible except clause for a *file
            # read* would list. One corrupt reference file would have taken
            # down the whole check, turning a diagnosable case into an
            # undiagnosable one.
            if polydata is None or polydata.GetPoints() is None:
                continue
            yield geometry_id, polydata

    @staticmethod
    def _build_validation_reference(session: CaseSession, revision_id: str,
                                    tau_min: float):
        """Build the reference this measurement is about to need (R176).

        It used to be built once, when geometry was prepared, sized by a
        `fidelity_tolerance_m` command parameter -- which the GUI does not
        send, and could not: the tolerance is chosen on Reference Readiness,
        a page the user reaches *after* preparing. So every case prepared
        through the GUI sized its reference with tau_min = 0, which §16.1
        cannot size, and got back an unrated reference carrying no surfaces.
        Every section of Snap Fidelity and Geometry Fidelity then read "no
        reference for section", on every case, forever -- a gate that could
        not measure anything and did not say why. MEASURED on the live tee
        case: sized by the 0.678 mm tolerance the user had actually set, the
        same revision builds a rated reference with its surface present.

        Building it at measurement time means it is sized by the tolerance in
        force when the question is asked. A case with no tolerance at any
        level still gets no reference: §6's rule that no hidden default may
        turn an uncalibrated case green is untouched.
        """
        if not tau_min > 0:
            return None
        from foammesh.core.geometry import (
            PreparedGeometryError, PreparedGeometryStore,
        )
        from foammesh.core.geometry.features import default_detection
        from foammesh.core.geometry.validation import materialize
        from foammesh.core.geometry.validation import reference as reference_mod

        try:
            prepared = PreparedGeometryStore(session.case_path).load(
                revision_id)
            return materialize(prepared, tau_min=tau_min,
                               case_path=session.case_path,
                               detection=default_detection())
        except (PreparedGeometryError, reference_mod.ValidationReferenceError,
                FileNotFoundError, OSError, RuntimeError, TypeError,
                ValueError):
            # A reference that cannot be built leaves its sections unrated,
            # which is what its absence means. Never an error on the check.
            return None

    @staticmethod
    def _source_field(record, key):
        if isinstance(record, dict):
            return record.get(key)
        return getattr(record, key, None)

    def _fidelity_budget_seconds(self, session: CaseSession):
        """§10.1's diagnostic budget, or ``None`` for unbounded."""
        from foammesh.core.geometry.diagnostics.budget import (
            budget_from_settings,
        )

        try:
            budget = budget_from_settings()
        except Exception:                                   # noqa: BLE001
            return None
        return getattr(budget, 'seconds', None)

    def _quality_waiver_record(self, session: CaseSession,
                               command: Command) -> OperationResult:
        """Plan 23 §8.6. The only route to WAIVED, and it is never a pass.

        The actor comes from the session context rather than a parameter: a
        caller-supplied display name records who someone *said* they were,
        which is not a record of a decision.
        """
        from foammesh.core.quality import waiver as waiver_module

        task_id = str(command.parameters.get('task_id') or '').strip()
        if not task_id:
            raise ValidationFailedError('a waiver names the task it covers')
        reason = str(command.parameters.get('reason') or '')

        engine_id = self._engine_id(session)
        try:
            task = self._workflow_task(engine_id, task_id)
        except (KeyError, LookupError, ValueError) as error:
            raise ValidationFailedError(
                f'unknown task {task_id!r} on engine {engine_id!r}') from error

        report = self._current_report(session, task_id)
        if report is None:
            raise PreconditionFailedError(
                f'{task_id} has no current report, so there is no verdict to '
                'waive; run the check first')
        try:
            waiver = waiver_module.record(
                task, report, actor=self._session_actor(session),
                reason=reason)
        except waiver_module.WaiverRefused as error:
            raise ValidationFailedError(str(error)) from error

        path = waiver_module.write(session.case_path, waiver)
        return self._read_result(session, command, {
            'waiver': waiver.to_dict(),
            'waiver_fingerprint': waiver.fingerprint,
            'path': str(path)})

    def _session_actor(self, session: CaseSession) -> str:
        """Who the system believes is acting. Never a user-typed string."""
        actor = getattr(session, 'actor', None)
        if actor:
            return str(actor)
        import getpass

        try:
            return getpass.getuser()
        except Exception:
            return 'unknown'

    def _engine_id(self, session: CaseSession) -> str:
        """The engine this *case* configured (Plan 30 F-14).

        The default used to be ``'snappy'``, which is a session's habit and
        not a fact about the case. Every caller binds something to the answer
        -- a waiver, a QA task id, a summary block, a quality report -- so a
        project that had chosen no meshing method had snappy's name written
        onto records belonging to an engine it had never run.
        """
        from foammesh.core.engine.registry import configured_engine_id
        from foammesh.db.configurations_schema import MeshEngine

        return str(configured_engine_id(session.state.db)
                   or MeshEngine.UNSELECTED.value)

    def _workflow_task(self, engine_id: str, task_id: str):
        from foammesh.core.engine.registry import ENGINE_REGISTRY

        engine = ENGINE_REGISTRY.get(engine_id)
        return engine.workflow_descriptor().task(task_id)

    def _current_evidence(self, session: CaseSession) -> dict:
        """What the case identifies right now, for §9's staleness check.

        Every value is read from the case rather than remembered, so a summary
        composed after a re-mesh compares against the new mesh and not against
        whatever the last composition happened to hold.
        """
        from foammesh.core.quality.summary import BOUND_EVIDENCE

        marker = (Path(session.case_path) / 'foammesh' / 'quality'
                  / 'evidence.json')
        try:
            document = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            document = {}
        return {key: str(document.get(key) or '') for key in BOUND_EVIDENCE}

    def _summary_blocks(self, session: CaseSession):
        """§9's three blocks, projected from reports that already exist.

        A block whose report is absent is `incomplete` and named as such --
        never omitted. A summary that silently dropped an unrun check would
        report on a subset of the mesh while looking complete, which is the
        one thing §9 must not do.
        """
        from foammesh.core.engine.base import qa_task_id
        from foammesh.core.quality.summary import Block, mesh_quality_block

        blocks, evidence = [], {}
        for name, task_id in (('geometry_fidelity', 'common.fidelity'),
                              ('resolution_adequacy', 'common.resolution')):
            report = self._current_report(session, task_id)
            if report is None:
                blocks.append(Block(name, 'incomplete', task_id=task_id,
                                    detail={'reason': f'{task_id} has not run'}))
                continue
            blocks.append(Block(
                name, str(report.get('verdict') or 'unrated'),
                str(report.get('report_fingerprint') or ''), task_id=task_id))
            evidence[name] = report

        engine_id = self._engine_id(session)
        quality_task = qa_task_id(engine_id)
        check_mesh = self._current_report(session, 'checkmesh')
        block = mesh_quality_block(check_mesh,
                                   self._native_quality_report(session))
        blocks.append(Block(block.name, block.verdict,
                            block.report_fingerprint, block.required,
                            block.detail, task_id=quality_task))
        if check_mesh and check_mesh.get('subject_mesh_fingerprint'):
            # checkMesh binds to the mesh it read, and nothing else. A report
            # from before a re-mesh makes the summary stale by the same rule
            # as any other block, rather than passing on the previous mesh.
            evidence['mesh_quality'] = {
                'subject_mesh_fingerprint':
                    str(check_mesh['subject_mesh_fingerprint'])}
        return blocks, {name: report for name, report in evidence.items()}

    #: R120. FoamMesh's own gate answers in four states; §9's summary ranks
    #: five severities. A word that is in neither vocabulary ranks as
    #: `unrated`, which is how a rejected mesh could read as merely unrated.
    #: `invalid` maps to `fail` because §9 has nothing worse than fail, not
    #: because the two mean the same thing.
    _NATIVE_VERDICT = {'pass': 'pass', 'blemish': 'warning',
                       'fail': 'fail', 'invalid': 'fail'}

    def _native_quality_report(self, session: CaseSession) -> dict | None:
        """FoamMesh's own verdict on this mesh, to set beside checkMesh's.

        R120. `mesh_quality_block` takes two inputs and exists so that neither
        can vanish, and the second was looked up as `canonical` -- a report no
        Gmsh run has ever written. MEASURED on run 4: the sicn gate had failed
        41.65% of the elements and the summary still composed
        `canonical_quality: "not evaluated"`, so the block read `verdict: pass`
        on the strength of checkMesh alone and Export unlocked. The native gate
        writes `foammesh/quality/mesh-quality.json`; this reads it there.

        An engine with no native gate keeps the old `canonical` lookup and,
        finding nothing, still says "not evaluated" -- which is then true.
        """
        from foammesh.core.gmsh.quality import REPORT_TASK_ID

        report = self._current_report(session, REPORT_TASK_ID)
        if report is None:
            report = self._current_report(session, 'canonical')
        if report is None:
            return None

        native = str(report.get('verdict') or '').lower()
        projected = dict(report)
        projected['verdict'] = self._NATIVE_VERDICT.get(native, native)
        projected['native_verdict'] = native
        metric = self._governing_metric(report)
        if metric and projected['verdict'] not in ('pass', ''):
            projected['differing_metric'] = metric
        return projected

    @staticmethod
    def _governing_metric(report: dict) -> str:
        """Which measure decided the native verdict, and by how much.

        Naming it is the difference between "canonical quality reports fail"
        and a line the user can act on: it was sicn, and this many elements.
        """
        from foammesh.core.gmsh.quality import VERDICT_SEVERITY

        metrics = [item for item in (report.get('metrics') or [])
                   if isinstance(item, dict)]
        if not metrics:
            return str(report.get('measure') or '')
        worst = max(metrics, key=lambda item: (
            VERDICT_SEVERITY.get(str(item.get('verdict') or '').lower(), 0),
            item.get('belowThreshold') or 0))
        measure = str(worst.get('measure') or '')
        below, total = worst.get('belowThreshold'), worst.get('total')
        if not measure or not total:
            return measure
        return (f'{measure} ({below} of {total} elements below the '
                f'requested minimum)')

    def _blocking_gate_state(self, session: CaseSession) -> dict:
        """The configured engine's blocking gate, or empty when it has none.

        Plan 30 F-14. This asked whether the engine was snappy, which is the
        same defect one layer up: a third engine with a gate of its own would
        have been told it had none. The engine names its gate.
        """
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, EngineNotRegisteredError,
        )

        try:
            engine = ENGINE_REGISTRY.get(self._engine_id(session))
        except (EngineNotRegisteredError, LookupError):
            return {}
        task_id = str(getattr(engine, 'blocking_gate_task', '') or '')
        if not task_id:
            return {}
        report = self._current_report(session, task_id)
        if report is None:
            return {}
        return {'state': str(report.get('state')
                             or report.get('verdict') or '').upper(),
                'task_id': task_id,
                'report_fingerprint': str(
                    report.get('report_fingerprint') or '')}

    def _binding_waivers(self, session: CaseSession, waiver_module) -> list:
        """Waivers that still describe the mesh-quality report on disk (R119).

        A waiver names the report fingerprint it overrode. Every Gmsh run
        rewrites that report, so comparing fingerprints is what makes a
        decision lapse when the mesh changes -- which is the property §8.6
        gives waivers and the reason none of this is a stored "accepted" flag.
        """
        from foammesh.core.gmsh.quality import REPORT_TASK_ID

        # Both gates, because not every engine has both. Gmsh's native
        # element gate writes the mesh-quality report; snappy's gate *is*
        # checkMesh, so a snappy acceptance is bound to the checkMesh report
        # and read here from nowhere else. Reading only the first left the
        # strip repainting a bare verdict over a decision somebody had just
        # taken and recorded (F-05).
        current = {
            str((self._current_report(session, task_id) or {})
                .get('report_fingerprint') or '').strip()
            for task_id in (REPORT_TASK_ID, 'checkmesh')}
        current.discard('')
        if not current:
            return []
        return [dict(item.to_dict(), fingerprint=item.fingerprint)
                for item in waiver_module.load_all(session.case_path)
                if str(item.report_fingerprint or '').strip() in current]

    def _current_report(self, session: CaseSession, task_id: str):
        """The stored report for *task_id*, or ``None``.

        Resolved through the producer's own `REPORT_PATHS` rather than by
        deriving a filename from the task id. The derivation happened to agree
        -- `common.fidelity` -> `fidelity.json` either way -- and agreeing by
        coincidence is how this codebase has repeatedly ended up with a lookup
        keyed on something its producer does not write. One table, read by
        both sides, cannot drift.
        """
        import json as _json
        from foammesh.core.quality.geometry_fidelity.report import (
            REPORT_PATHS,
        )

        # R208. `gmsh.qa` and `snappy.qa` are the task ids the pages and
        # the CLI carry; `checkmesh` is the internal alias this method has
        # always answered to. All three name the one report checkMesh writes.
        if task_id in ('checkmesh', 'gmsh.qa', 'snappy.qa'):
            return self._checkmesh_report(session)

        from foammesh.core.gmsh.quality import (
            REPORT_PATH as MESH_QUALITY_REPORT_PATH,
            REPORT_TASK_ID as MESH_QUALITY_TASK_ID,
        )

        candidates = []
        relative = REPORT_PATHS.get(task_id)
        if relative is not None:
            candidates.append(Path(session.case_path) / relative)
        # WP1.4's mesh-quality report. Named in its producer's own table for
        # the same reason as the fidelity ones, rather than left to the
        # derivation below, which would have resolved it to `compute.json`.
        if task_id == MESH_QUALITY_TASK_ID:
            candidates.append(
                Path(session.case_path) / MESH_QUALITY_REPORT_PATH)
        # checkMesh and canonical quality are not §8.4 report tasks; they keep
        # their existing names and are looked up by them.
        name = task_id.split('.')[-1].replace('_', '-')
        candidates.append(
            Path(session.case_path) / 'foammesh' / 'quality' / f'{name}.json')
        for path in candidates:
            try:
                return _json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
        return None

    def _checkmesh_report(self, session: CaseSession) -> dict | None:
        """checkMesh's verdict on the mesh in the case root, or ``None``.

        The lookup derived ``quality/checkmesh.json`` from the task name, and
        checkMesh writes ``quality/latest.json`` -- a schema-2 document whose
        verdict lives under ``result``, not at the top. So the summary's
        mesh-quality block was "checkMesh has not run on this mesh" on every
        case, including two live walks where the report sat beside it. The
        projection is the same one the verdict strip uses, so the summary
        and the strip cannot disagree about the same report.
        """
        from foammesh.core.quality.verdict import verdict_from_report

        try:
            report = self._current_quality_report(session)
        except (OSError, ValueError):
            return None
        if report is None:
            return None
        projected = dict(verdict_from_report(report))
        projected.update({
            'report_fingerprint': str(report.mesh_fingerprint or ''),
            'subject_mesh_fingerprint': str(report.mesh_fingerprint or ''),
            'stale': bool(report.stale),
            'checked_at': str(getattr(report, 'checked_at', '') or ''),
        })
        return projected

    def _quality_summary(self, session: CaseSession,
                         command: Command) -> OperationResult:
        """Plan 23 §9. Compose, never recompute."""
        from foammesh.core.quality import summary as summary_module

        blocks, block_evidence = self._summary_blocks(session)
        from foammesh.core.quality import waiver as waiver_module

        composed = summary_module.compose(
            blocks=blocks,
            current_evidence=self._current_evidence(session),
            block_evidence=block_evidence,
            blocking_gate=self._blocking_gate_state(session),
            waivers=waiver_module.load_all(session.case_path))
        path = summary_module.write(session.case_path, composed)
        return self._read_result(session, command, {
            'summary': composed.to_dict(), 'path': str(path)})

    def _quality_summary_read(self, session: CaseSession,
                              command: Command) -> OperationResult:
        from foammesh.core.quality import summary as summary_module

        document = summary_module.read(session.case_path)
        return self._read_result(session, command, {'summary': document})

    def _quality_evidence_read(self, session: CaseSession,
                               command: Command) -> OperationResult:
        """The stored report for one qualification task, and its readout.

        Plan 23 §9 requires the same evidence from the GUI, the CLI and the
        API. `quality.summary.read` existed; the fidelity and resolution
        reports had no reader at all, so the only way to see what a gate had
        measured was to open the JSON by hand -- and the pages that exist to
        show it rendered empty grey panels instead (R31, R41, R68, R90, R103,
        R123, R160).

        The projection travels with the document rather than being rebuilt by
        each caller: which absence has to be stated out loud is a judgement,
        and three surfaces making it separately is three chances to imply a
        pass nothing measured.
        """
        from foammesh.core.quality import readout as readout_module
        from foammesh.core.quality import summary as summary_module

        task_id = str(command.parameters.get('task_id') or '').strip()
        if task_id not in readout_module.READOUTS:
            raise ValidationFailedError(
                f'{task_id!r} is not a qualification report task; expected '
                f'one of {", ".join(sorted(readout_module.READOUTS))}')
        document = (summary_module.read(session.case_path)
                    if task_id == 'common.summary'
                    else self._current_report(session, task_id))
        # R183: the gate note is a claim about what this very reading will
        # do, so the mode rides along with it rather than costing the page a
        # second round trip to find out.
        from foammesh.core.quality.qualification import qualification_mode

        return self._read_result(session, command, {
            'task_id': task_id, 'document': document,
            'qualification_mode': qualification_mode().value,
            'readout': readout_module.readout_for(task_id, document).to_dict()})

    def _require_export_authorization(self, session: CaseSession,
                                      command: Command):
        """Plan 23 §8.6. Call before creating the destination, never after.

        A refusal that fires after the file exists has already published the
        mesh it was refusing; the ordering is the enforcement. Returns the
        stamp for the artifact manifest, so a permitted export still records
        what was claimed and on what evidence.

        In report-only -- the shipping default -- this never refuses. It
        stamps ``qualified: false`` and attaches whatever report fingerprints
        exist, so an artifact that left during rollout stays traceable.
        """
        from foammesh.core.quality.export_authorization import (
            ExportRefused, authorize,
        )

        try:
            stamp = authorize(command.operation,
                              summary=self._qualification_summary(session),
                              case_path=session.case_path)
        except ExportRefused as error:
            raise PreconditionFailedError(str(error)) from error
        return stamp.to_manifest()

    def _qualification_summary(self, session: CaseSession):
        """§9's summary document, or ``None`` when the case has none.

        NOTE: the summary *producer* (``common.summary``) is not yet built, so
        this reads a file nothing writes and returns ``None`` for every case
        today. That is deliberate and its consequences are the intended ones:
        report-only stamps ``qualified: false``, and enforcing mode refuses
        every engineering export with ``missing_summary`` -- which is exactly
        §8.6's rule, and the reason enforcing is not the default. Wiring the
        call site now means the producer has one place to land rather than six.
        """
        path = (Path(session.case_path) / 'foammesh' / 'quality' /
                'summary.json')
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _export_entries(self, session: CaseSession,
                        command: Command) -> OperationResult:
        """Every export format for *this* case, and which one to offer first.

        Plan 28 WP3. The GUI's export page could only write an OpenFOAM case,
        so an SU2 project had no way to produce a mesh at all without the CLI.
        The listing is case-aware: SU2 reads "ready" only when this mesh's own
        census says SU2 can open it, and carries the census sentence when it
        cannot.
        """
        from foammesh.core.engine.registry import (
            configured_target_solver, solver_display_name,
        )
        from foammesh.core.import_export.service import ImportExportService

        registry = self._capabilities_registry()
        utilities = {}
        for name in ('foamMeshToFluent', 'foamFormatConvert'):
            capability = self._utility_without_waiting(registry, name)
            if capability is not None and capability.available:
                utilities[name] = capability.executable
        entries = ImportExportService(utilities).export_entries(
            case_path=session.case_path)
        target = configured_target_solver(session.state.db)
        recommended = 'su2' if target == 'su2' else 'openfoam'
        return self._read_result(session, command, {
            'entries': [entry.to_dict() for entry in entries],
            'target_solver': target,
            'target_solver_name': solver_display_name(target),
            'recommended_entry_id': recommended,
        })

    def _export_native(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.import_export.service import ImportExportService
        qualification = self._require_export_authorization(session, command)
        destination = self._destination(session, command)
        result = ImportExportService().export_native_case(session.case_path, destination)
        return self._read_result(session, command, {'export': _to_payload(result),
                                                    'destination': str(destination),
                                                    'qualification': qualification})

    #: The exports that convert through Gmsh. Plan 35 CR7: Gmsh is native
    #: code the window does not load, and its WSL route used to hold the
    #: owner loop for up to 900 s, so these run in a worker process. CGNS
    #: converts through Gmsh whenever the case has an accepted Gmsh run.
    _GMSH_EXPORTS = frozenset({'gmsh', 'med', 'unv', 'cgns'})

    async def _dataset_export(self, session, command, entry_id) -> OperationResult:
        self._require_exportable_mesh(session, entry_id)
        qualification = self._require_export_authorization(session, command)
        destination = self._destination(session, command)
        from foammesh.core.import_export.service import ImportExportService
        adapter = {
            'vtk': 'export_vtu',
            'cgns': 'export_cgns',
            'gmsh': 'export_gmsh',
            'su2': 'export_su2',
            'med': 'export_med',
            'unv': 'export_unv',
        }[entry_id]
        try:
            with self._replacing(session, command, destination, entry_id):
                if entry_id in self._GMSH_EXPORTS:
                    payload = await self._export_in_worker(
                        session, adapter, destination)
                else:
                    # Plan 35 CR7: a VTK write is file work, off the loop.
                    payload = dict(_to_payload(await asyncio.to_thread(
                        getattr(ImportExportService(), adapter),
                        session.case_path, destination)))
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        payload['qualification'] = qualification
        return self._read_result(session, command, payload)

    async def _export_in_worker(self, session, adapter: str,
                                destination) -> dict:
        """One ``ImportExportService`` export, run in a worker process.

        A refusal the service raised comes back as the same ``ValueError`` or
        ``OSError``; a worker that died says the writer crashed; one that
        could not start or was stopped says so. Nothing falls back to running
        the conversion in this process.
        """
        from foammesh.core.jobs import local_worker
        from foammesh.support import resource_budget as budget

        operation = 'export.dataset'
        estimate = budget.estimate_peak_bytes(operation, None)
        try:
            grant = await budget.controller().admit(
                operation, estimate, priority=budget.PRIORITY_INTERACTIVE)
        except budget.OverBudget as refusal:
            raise CheckOverBudgetError(
                f'the export was not started: {refusal}',
                details=dict(refusal.to_dict(), outcome='refused',
                             operation=operation, retryable=True)) from None
        async with grant:
            outcome = await local_worker.run_worker(
                operation, {'parameters': {
                    'format': adapter.removeprefix('export_'),
                    'case_path': str(Path(session.case_path).resolve()),
                    'destination': str(Path(destination).resolve()),
                    'gmsh_runtime': _gmsh_runtime()}},
                cap_bytes=grant.cap_bytes, group=str(session.case_path))
        if outcome.ok:
            return dict(outcome.payload.get('result') or {})
        if outcome.status == local_worker.FAILED:
            if outcome.error_class in ('OSError', 'FileNotFoundError',
                                       'PermissionError'):
                raise OSError(outcome.message)
            if outcome.error_class == 'RuntimeError':
                raise CapabilityUnavailableError(
                    outcome.message, details={'capability': 'gmsh',
                                              'error': 'gmsh_unavailable'})
            raise ValueError(outcome.message)
        details = dict(outcome.to_dict(), outcome=outcome.status,
                       retryable=True)
        if outcome.status == local_worker.CRASHED:
            raise ValidationFailedError(
                'export failed (the Gmsh writer crashed); the case is '
                'untouched and the export can be run again', details=details)
        if outcome.status == local_worker.OVER_BUDGET:
            raise CheckOverBudgetError(
                f'the export stopped: {outcome.message}', details=details)
        raise CheckUnavailableError(
            f'the export did not run: {outcome.message}', details=details)

    @staticmethod
    def _replacing(session, command, destination, entry_id):
        """The one overwrite an export may do, when it was asked (DP-680).

        ``overwrite`` is honoured for the two exports the Export page offers
        it on, an OpenFOAM case folder and an SU2 file; asked of any other
        format it is refused rather than ignored.
        """
        from foammesh.core.import_export.overwrite import (
            FOLDER, SU2, replacing)
        overwrite = bool(command.parameters.get('overwrite', False))
        kind = {'openfoam': FOLDER, 'su2': SU2}.get(entry_id)
        if overwrite and kind is None:
            raise ValidationFailedError(
                f'overwrite is offered for OpenFOAM case folders and SU2 '
                f'files only, not {entry_id}')
        return replacing(destination, kind or FOLDER, overwrite=overwrite,
                         protected=(session.case_path,))

    async def _export_vtk(self, session, command):
        return await self._dataset_export(session, command, 'vtk')

    async def _export_cgns(self, session, command):
        return await self._dataset_export(session, command, 'cgns')

    async def _export_gmsh(self, session, command):
        return await self._dataset_export(session, command, 'gmsh')

    async def _export_su2(self, session, command):
        return await self._dataset_export(session, command, 'su2')

    async def _export_med(self, session, command):
        return await self._dataset_export(session, command, 'med')

    async def _export_unv(self, session, command):
        return await self._dataset_export(session, command, 'unv')

    async def _export_fluent(self, session, command):
        self._require_mesh(session)
        await self._utility_ready('foamMeshToFluent')
        qualification = self._require_export_authorization(session, command)
        registry = self._capabilities_registry()
        from foammesh.core.import_export.fluent_export import FluentMeshExportService
        result = await FluentMeshExportService(
            session.jobs, utility='foamMeshToFluent',
            launcher=registry.command).run(session.case_path)
        payload = dict(result.to_dict())
        payload['qualification'] = qualification
        return OperationResult(
            'accepted' if result.succeeded else 'failed', command.operation,
            session.revisions, payload=payload)

    async def _export_format_convert(self, session: CaseSession, command: Command) -> OperationResult:
        self._require_mesh(session)
        write_format = command.parameters.get('write_format')
        if write_format not in {'ascii', 'binary'}:
            raise ValidationFailedError('write_format must be ascii or binary')
        await self._utility_ready('foamFormatConvert')
        registry = self._capabilities_registry()
        from foammesh.core.import_export.format_convert import FoamFormatConvertService
        result = await FoamFormatConvertService(
            session.jobs, utility='foamFormatConvert',
            launcher=registry.command).run(
                session.case_path, write_format=write_format,
                compression=bool(command.parameters.get('compression', False)),
                commit_settings=bool(command.parameters.get('commit_settings', False)),
                on_line=command.parameters.get('on_line'))
        if result.succeeded:
            session.state.bus.publish(
                Event.ARTIFACT_MESH_CHANGED, operation=command.operation)
        return OperationResult(
            'accepted' if result.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',), payload=result.to_dict())

    async def _export_authored(self, session: CaseSession, command: Command) -> OperationResult:
        qualification = self._require_export_authorization(session, command)
        destination = self._destination(session, command)
        raw_options = command.parameters.get('options')
        options = None
        if raw_options is not None:
            from foammesh.core.mesh.extrusion_options import ExtrudeModel, ExtrudeOptions
            try:
                options = ExtrudeOptions(
                    ExtrudeModel(raw_options['model']),
                    thickness=raw_options.get('thickness'),
                    point=raw_options.get('point'), axis=raw_options.get('axis'),
                    angle=raw_options.get('angle'))
            except (KeyError, TypeError, ValueError) as error:
                raise ValidationFailedError('invalid authored extrusion options') from error
        from foammesh.core.import_export.authored import AuthoredExportService
        try:
            with self._replacing(session, command, destination, 'openfoam'):
                payload = await AuthoredExportService(
                    capabilities=self._capabilities_registry()).run(
                    session, destination,
                    boundaries=command.parameters.get('boundaries', ()),
                    options=options,
                    on_line=command.parameters.get('on_line'),
                    on_progress=command.parameters.get('on_progress'))
        except (OSError, ValueError, RuntimeError) as error:
            raise ValidationFailedError(str(error)) from error
        payload = dict(payload or {})
        payload['qualification'] = qualification
        return self._read_result(session, command, payload)


#: The check-task table at module level, for the callers that never hold a
#: ``DomainOperations`` (the wizard's step manager, the gate advancer).
CHECK_TASK_OPERATIONS = DomainOperations.CHECK_TASK_OPERATIONS


#: Plan 35 CR2. The operations that parse a mesh and so run in a worker
#: process, never in the window's.
WORKER_OPERATIONS = ('quality.fidelity', 'quality.resolution',
                     'quality.summary', 'quality.cell_fields')


def _unreadable_mesh(refusals: list) -> dict:
    """Why no section could be measured, with the reader's code if it refused."""
    reason = ('no published mesh or no patch-identity sidecar, so no section '
              'could be joined to a prepared patch')
    if not refusals:
        return {'reason': reason}
    refusal = refusals[-1]
    return {'reason': '{0}: the mesh could not be read ({1}: {2})'.format(
                reason, refusal['reason'], refusal['message']),
            'read_error': refusal['reason']}


def _check_failure(operation: str, error: FacadeError) -> dict:
    """What the task page says about a check that produced no verdict."""
    details = dict(getattr(error, 'details', None) or {})
    return {'operation': operation, 'code': error.code,
            'reason': str(details.get('reason') or error.code),
            'message': str(error),
            'retryable': bool(details.get('retryable', True))}


#: Plan 37 UF4. What every check refusal ends on: the one thing to press.
_CHECK_AGAIN = 'then press Check & Proceed again.'
_CHECK_AGAIN_SENTENCE = 'Then press Check & Proceed again.'


def _actionable_check_error(title: str, operation: str,
                            error: FacadeError) -> FacadeError:
    """``error`` reworded as what failed and what to do, class kept.

    Plan 37 UF4 DP-1025. A check that could not produce a verdict reached the
    user as the facade's own sentence -- ``quality.fidelity did not produce a
    result: ...`` -- in a box titled "Task state", naming an operation id and
    no next step. The evidence is kept verbatim inside the new text; the class
    and the details are kept, so the code the page and the tests read
    (``over_budget``, ``check_unavailable``) is unchanged.
    """
    details = dict(getattr(error, 'details', None) or {})
    if details.get('actionable'):
        return error
    raw = str(error).strip().rstrip('.')
    outcome = str(details.get('outcome') or '')
    if isinstance(error, CheckOverBudgetError):
        if outcome == 'refused':
            text = ('{0} was not started: there is not enough free memory '
                    'for it right now ({1}). The mesh is unchanged. Close '
                    'other programs, or wait a minute for WSL to hand back '
                    'the memory the mesher used, {2}').format(
                        title, raw, _CHECK_AGAIN)
        else:
            text = ('{0} ran out of the memory set aside for it and was '
                    'stopped ({1}). The mesh is unchanged. Close other '
                    'programs to free memory, {2}').format(
                        title, raw, _CHECK_AGAIN)
    elif isinstance(error, CheckUnavailableError):
        text = ('{0} could not run, so Quality was not marked done ({1}). '
                'The mesh is unchanged; {2} If it keeps happening, restart '
                'FoamMesh.').format(title, raw, _CHECK_AGAIN)
    else:
        text = ('{0} could not check this mesh: {1}. Fix what it names, '
                '{2}').format(title, raw, _CHECK_AGAIN)
    details.update(actionable=True, operation=details.get('operation')
                   or operation)
    try:
        return type(error)(text, details=details)
    except TypeError:                                   # pragma: no cover
        return error


def _mesh_revision(case_path) -> tuple:
    """Which mesh is on disk: size and mtime of each polyMesh file.

    Plan 37 UF4 DP-1027. A check job is keyed by it, so a result measured on
    a mesh that has since been replaced is recognised as stale. Only stats,
    never a read: it is taken on the thread that runs the window.
    """
    base = Path(str(case_path or '')) / 'constant' / 'polyMesh'
    signature = []
    for name in ('points', 'faces', 'owner', 'neighbour', 'boundary'):
        found = None
        for candidate in (base / name, base / (name + '.gz')):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            found = (candidate.name, stat.st_size, stat.st_mtime_ns)
            break
        signature.append(found)
    return tuple(signature)


def _settings_digest(session) -> str:
    """The configuration a check job measured against (part of its key)."""
    import hashlib

    db = getattr(getattr(session, 'state', None), 'db', None)
    try:
        text = db.toYaml() if db is not None and hasattr(db, 'toYaml') else ''
    except Exception:                                   # noqa: BLE001
        return ''
    return hashlib.sha1(str(text or '').encode('utf-8', 'replace')).hexdigest()


def _parse_parallel_check(job) -> dict:
    """``checkMesh -parallel``: the global cell count and its verdict.

    Plan 37 UF17. Points, faces and internal faces in this output count the
    processor-boundary duplicates, so only the cells are compared.
    """
    import re
    text = getattr(job, 'output', '') or ''
    cells = re.search(r'^\s*cells:\s*(\d+)', text, re.MULTILINE)
    failed = re.search(r'Failed\s+(\d+)\s+mesh\s+checks', text)
    return {'cells': int(cells.group(1)) if cells else None,
            'mesh_ok': 'Mesh OK' in text,
            'failed_checks': int(failed.group(1)) if failed else 0}


def _assert_worker_isolation(table: dict) -> None:
    """Every mesh-parsing check is dispatched to a worker (Plan 35 CR2).

    A handler table that maps one of them to anything else would parse the
    mesh on the window's thread again, which is the crash this plan removes;
    it is refused when the table is built rather than found in the field.
    """
    for operation in WORKER_OPERATIONS:
        handler = table.get(operation)
        if getattr(handler, '_foammesh_worker', None) != operation:
            raise RuntimeError(
                f'{operation} must be dispatched to the mesh worker, not '
                f'run in-process ({handler!r})')


_assert_worker_isolation(DomainOperations().handlers())


def _gmsh_runtime() -> dict:
    """The WSL distribution and user Gmsh runs as, for a worker to inherit.

    DP-816 detects them at start and keeps them in this process; a worker
    starts without that state and would fall back to the defaults.
    """
    from foammesh.core.gmsh import launch_profiles

    return dict(launch_profiles._shared_runtime)
