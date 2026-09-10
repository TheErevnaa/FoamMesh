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

from .commands import Command
from .errors import (CapabilityUnavailableError, FacadeError,
                     PreconditionFailedError, ValidationFailedError)
from .probe_cache import ProbeCache
from .results import OperationResult
from .session import CaseSession

logger = logging.getLogger(__name__)


def _to_payload(value) -> dict:
    if value is None:
        return {}
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    return {'value': str(value)}



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


def runner_crash_reason(returncode) -> str:
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
    """
    try:
        code = int(returncode)
    except (TypeError, ValueError):
        return ''
    number = -code if code < 0 else (code - 128 if code > 128 else 0)
    if number <= 0 or number > 64:
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
                'fewer elements -- a larger target element size is the first '
                'thing to raise, then any refinement or boundary-layer '
                'settings that multiply it -- or give the Linux runtime more '
                'memory. If it is killed again at a size that should fit, the '
                'geometry is worth checking too: a surface that does not '
                'close can consume memory without meshing anything.')
    return (f'Gmsh itself crashed ({named}) part-way through, so it wrote no '
            'mesh and no reason. This is a defect in Gmsh rather than a '
            'refusal of your geometry, so re-running unchanged will crash '
            'again: change one meshing choice and re-run -- the surface or '
            'volume algorithm first, since those are the settings measured '
            'to trigger it.')


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
    """Bounds of the prepared geometry, or None when it records none."""
    for source in (prepared, getattr(prepared, 'reference', None)):
        if source is None:
            continue
        for attribute in ('bounds', 'bbox', 'bounding_box'):
            value = getattr(source, attribute, None)
            if value is not None:
                return value
    return None


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


def _configured_parallel_cores(session) -> int:
    """The core count the case's parallel environment asks meshing to use.

    Returns 0 when the case has no readable settings -- including the light
    session doubles the facade tests use -- so callers fall back to the
    execution ceiling exactly as they did before.
    """
    storage = getattr(session, 'storage_path', None)
    if storage is None:
        return 0
    try:
        from foammesh.settings.local_settings import LocalSettings
        return int(LocalSettings(storage).parallelEnvironment().np())
    except Exception:  # noqa: BLE001 - settings are advisory here
        return 0


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
    readiness = prepare_readiness(store)
    if not readiness['can_prepare']:
        return None
    if (require_domain and not readiness['prepared']
            and readiness.get('bounds_domain') is False):
        raise ValidationFailedError(
            domain_refusal(store) or (
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

    # -- registration ------------------------------------------------------ #

    def register_all(self, register) -> None:
        for operation, handler in self.handlers().items():
            register(operation, handler)

    def handlers(self) -> dict:
        return {
            # Slice 1: lifecycle / persistence / history
            'case.classify': self._classify,
            'case.save': self._save,
            'case.copy': self._copy,
            'case.archive': self._archive,
            'case.clean.preview': self._clean_preview,
            'case.clean': self._clean,
            'case.parallel.redistribute': self._parallel_redistribute,
            'case.parallel.configure': self._parallel_configure,
            'client_shell.paraview': self._launch_paraview,
            'client_shell.terminal': self._launch_terminal,
            'artifact.stage.clear': self._stage_clear,
            'history.query': self._history_query,
            # Slice 2: geometry
            'geometry.import': self._geometry_import,
            'geometry.diagnostics': self._geometry_diagnostics,
            'geometry.readiness': self._geometry_readiness,
            'geometry.fluid_seed.suggest': self._geometry_fluid_seed_suggest,
            'geometry.fluid_seed.check': self._geometry_fluid_seed_check,
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
            'geometry.prepare.cancel': self._geometry_prepare_cancel,
            'geometry.prepared.create': self._geometry_prepared_create,
            'geometry.prepared.load': self._geometry_prepared_load,
            'geometry.prepared.current': self._geometry_prepared_current,
            'geometry.prepared.select': self._geometry_prepared_select,
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
            'quality.cell_fields': self._quality_cell_fields,
            'quality.mesh_report': self._quality_mesh_report,
            'mesh.run.accept': self._mesh_run_accept,
            'quality.report': self._quality_report,
            'quality.failed_sets': self._quality_failed_sets,
            'quality.failed_set.select': self._quality_failed_set_select,
            'quality.compare': self._quality_compare,
            'quality.report.export': self._quality_report_export,
            'quality.waiver.record': self._quality_waiver_record,
            'quality.fidelity': self._quality_fidelity,
            'quality.resolution': self._quality_resolution,
            'quality.summary': self._quality_summary,
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
        store = PreparedGeometryStore(session.case_path)
        categories = command.parameters.get('boundary_categories')
        if categories is None:
            categories = _prepared_boundary_categories(
                session.state.db, store.source.entries())
        try:
            result = store.materialize(
                preparation=command.parameters.get('preparation'),
                boundary_categories=categories,
                fluid_seed=command.parameters.get('fluid_seed'),
                transform=command.parameters.get('transform'))
        except (PreparedGeometryError, TypeError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        payload = result.to_dict()
        payload['geometry_reference'] = self._materialize_geometry_reference(
            session, result,
            tolerance=command.parameters.get('fidelity_tolerance_m'))
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
            ResourceFacts, ResourceMode, ResourcePolicy, ResourceRequest,
            allocate_resources,
        )
        from foammesh.core.execution.resources import ResourceError

        configuration = _resource_policy(session.configuration())
        mode = ResourceMode(str(command.parameters.get('mode')
                                or configuration['mode']))
        parallel_cores = _configured_parallel_cores(session)
        requested = int(command.parameters.get('cores')
                        or parallel_cores
                        or configuration['max_cpu_cores'] or 1)
        policy = ResourcePolicy(
            mode, configuration['max_cpu_cores'],
            configuration['max_memory_bytes'],
            configuration['allow_distributed'],
            'openfoam-mpi' if requested > 1 else 'local')
        document = {
            'mode': mode.value if hasattr(mode, 'value') else str(mode),
            'requested_cores': requested,
            'max_cpu_cores': configuration['max_cpu_cores'] or 0,
            'source': ('parallel environment' if parallel_cores
                       else 'the execution ceiling'),
        }
        try:
            allocation = allocate_resources(
                policy,
                ResourceRequest(requested, backend_id='openfoam-mpi',
                                explicit='cores' in command.parameters),
                ResourceFacts.local())
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
            'warnings': self._task_warnings(session, engine_id)})

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

        document = ENGINE_REGISTRY.get(engine_id).workflow_descriptor().to_dict()
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
            'status': str(statuses.get(task_id) or 'ready'),
            'warnings': list(warnings.get(task_id) or ()),
            'state': snapshot,
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
        for task_id, texts in self._boundary_coverage_warnings(session).items():
            warnings[task_id] = list(warnings.get(task_id) or ()) + list(texts)
        return warnings

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
            return dict(reader(session.state.db) or {})
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
        """
        from dataclasses import replace as _replace

        operation = self.CHECK_TASK_OPERATIONS[task_id]
        inner = _replace(command, operation=operation,
                         parameters={**dict(command.parameters),
                                     'task_id': task_id})
        # Both check handlers are synchronous, so this is too. Making the
        # transition operation async to accommodate an awaitable would change
        # the contract of an existing operation every caller already uses --
        # measured: it broke 17 tests with "requires the async execute() path".
        handler = self.handlers()[operation]
        result = handler(session, inner)

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
                f'{task_id} produced no report fingerprint, so there is no '
                'evidence to record; the check did not run')
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
            raise ValidationFailedError(
                '{0} produced a report but the task could not advance: '
                '{1} (state {2})'.format(
                    task_id, blocked.get('reason') or 'refused',
                    blocked.get('state') or 'unknown'))
        return dict(recorded, check={'operation': operation,
                                     'verdict': verdict,
                                     'report_fingerprint': fingerprint,
                                     'payload': payload})

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
            return self._read_result(session, command, dict(
                recorded, engine_id=engine_id))

        try:
            result = store.apply(task_id, transition)
        except (TaskStateError, ValueError, KeyError) as error:
            raise ValidationFailedError(str(error)) from error
        return self._read_result(session, command, dict(
            result, engine_id=engine_id))

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
        from foammesh.core.gmsh.manifest import RunBuilder, RunLayout, RunManifest
        from foammesh.core.gmsh.quality import (
            REPORT_TASK_ID as _GMSH_GATE_TASK_ID, assess, derive_thresholds,
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
            require_domain=True)
        if prepared is None:
            raise PreconditionFailedError(
                'import geometry before running Gmsh; there is nothing to '
                'prepare or mesh')

        run_id = str(command.parameters.get('run_id')
                     or f'gmsh-{uuid.uuid4().hex[:16]}')
        formats = tuple(command.parameters.get('formats') or ('msh',))
        try:
            written = write_job(
                session.state.db, _prepared_bbox(prepared), session.case_path,
                prepared_geometry=prepared.reference, profile=profile,
                run_id=run_id, formats=formats)
        except ExecutionError as error:
            raise ValidationFailedError(str(error)) from error
        _require_every_prepared_source(prepared, written['job'])

        layout = RunLayout(RunBuilder(session.case_path).root(run_id))
        record = RunManifest.read(layout.root)
        launch = profile.runner_argv(
            _gmsh_runner_path(), layout.job, cwd=session.case_path)
        RunBuilder.record_launch(record, launch)

        execution = await self._context(session).executor.execute(
            session, OperationSpec(
                operation=command.operation,
                argv=launch.argv, cwd=session.case_path,
                timeout=float(command.parameters.get('timeout_seconds', 3600)),
                max_output_bytes=8 * 1024 * 1024,
                log_path=layout.log,
                cleanup_argv=launch.cleanup_argv,
                invalidated_outputs=('quality', 'exports')),
            on_line=command.parameters.get('on_line'))

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
                (payload.get('job') or {}).get('returncode'))
            payload.update(failure_payload(
                task='gmsh.compute',
                reason=('the run was cancelled before it finished' if cancelled
                        else crashed
                        or result.get('error') or 'the Gmsh run failed'),
                log=str(layout.log)))
            payload['cancelled'] = cancelled
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
            reason = (
                f'{verdict.reason} This verdict is {verdict.verdict}, which '
                'cannot be accepted; re-mesh instead.')
            RunBuilder.record_publication_failure(record, reason)
            payload.update(failure_payload(
                task=_GMSH_GATE_TASK_ID, reason=reason, log=str(layout.log),
                built=True))
            payload['override_refused'] = True
            return OperationResult('failed', command.operation,
                                   session.revisions, payload=payload)
        if not verdict.accepted and not accept:
            RunBuilder.record_publication_failure(record, verdict.reason)
            payload.update(failure_payload(
                task=_GMSH_GATE_TASK_ID, reason=verdict.reason,
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
            payload['element_census'] = census.to_dict()
            if not census.has_volume_elements:
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
        warning = bool(warning)
        if check.get('ran') and check.get('status') != 'accepted':
            warning = True
        # R119/R158. The compute task is recorded as *waived*, not passed, when
        # its own gate refused and a human overrode it. The run used to record
        # PASSED regardless, so the outline painted the plain COMPLETED tick
        # over a recorded override and the `⚑` WAIVED glyph the app already
        # defines was never drawn for the one case it exists for.
        waived = () if accepted else ('gmsh.compute',)
        payload['task_state'] = self._record_engine_run_success(
            session, command, warning=warning, waived=waived,
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
        if census.read_error or not census.has_volume_elements:
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
            return {'ran': False, 'reason': str(error), 'check': operation,
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

        surfaces = discover_feature_edges(session.case_path)
        labels = self._staged_surface_labels(session.case_path)
        include = bool(command.parameters.get('include_segments'))
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
                                   waived: tuple = ()) -> dict | None:
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
            return store.record_atomic_run_success(
                covered, warning=warning,
                # R119/R158. Only tasks this run actually covered: a waiver
                # names a gate the run reached, never one it skipped.
                waived=tuple(task for task in waived if task in covered))
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
                                  task_id: str | None, *, warning: bool = False
                                  ) -> dict | None:
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
            return store.record_stage_chain_success(task_id, warning=warning)
        except (TaskStateError, LookupError, OSError, ValueError):
            return None

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
        try:
            parsed = parse_checkmesh(str(getattr(job, 'output', '') or ''))
            _, report = MeshCheckService.persist_result(
                session.case_path, parsed,
                command=tuple(getattr(job, 'argv', ()) or ()),
                log_path=getattr(job, 'log_path', None)
                or session.case_path / 'foammesh' / 'logs' / 'checkMesh.log')
        except (OSError, ValueError) as error:
            logger.warning('pipeline checkMesh output was not stored: %s', error)
            return None
        session.state.bus.publish(
            Event.ARTIFACT_QUALITY_CHANGED, operation='workflow.run_pipeline',
            job_id=getattr(job, 'job_id', None), artifacts=[], quality='checkMesh')
        return report.to_dict()

    def _advance_pipeline_gates(self, session: CaseSession, command: Command,
                                recorded: dict | None) -> dict | None:
        """Run the check gates a finished pipeline is blocked behind, when it may.

        The atomic recorder stops at the first gate. When that gate is a
        check task whose own prerequisites are met, the mesh it judges is on
        disk right now, so the check is run and recorded and the remaining
        run tasks are advanced. A gate whose prerequisite is a manual
        confirmation stays blocked, and the payload says so.
        """
        if not recorded:
            return recorded
        from foammesh.core.engine.registry import ENGINE_REGISTRY
        from foammesh.core.workflow.task_state_store import TaskStateError

        accepted = ('passed', 'warning', 'skipped', 'completed', 'waived')
        for _attempt in range(8):
            blocked = recorded.get('blocked') if isinstance(recorded, dict) else None
            if not blocked:
                return recorded
            try:
                engine_id, store = self._task_state_store(session, command)
                covered = getattr(
                    ENGINE_REGISTRY.get(engine_id), 'ATOMIC_RUN_TASKS', ())
                graph = store.load_result().graph
                task = store.descriptor.task(str(blocked.get('task_id') or ''))
                gates = [
                    parent for parent in task.depends_on
                    if parent in CHECK_TASK_OPERATIONS
                    and graph.state(parent).value not in accepted
                    and graph.is_runnable(parent)]
            except (TaskStateError, LookupError, OSError, ValueError,
                    AttributeError):
                return recorded
            if not gates:
                return recorded
            try:
                for gate in gates:
                    self._run_check_task(session, command, gate, store)
                progressed = store.record_atomic_run_success(
                    covered, warning=bool(recorded.get('warning')))
            except (TaskStateError, LookupError, OSError, ValueError,
                    FacadeError):
                return recorded
            progressed['advanced'] = list(recorded.get('advanced', ())) + list(
                progressed.get('advanced', ()))
            progressed['gates_run'] = list(recorded.get('gates_run', ())) + gates
            progressed['warning'] = recorded.get('warning')
            if progressed.get('blocked') == blocked:
                return progressed
            recorded = progressed
        return recorded

    def _publish_gmsh_result(self, session: CaseSession, record, prepared) -> dict:
        """Publish one Gmsh mesh as constant/polyMesh, atomically."""
        from foammesh.core.gmsh.manifest import RunBuilder
        from foammesh.core.gmsh.publish import PublishError, publish

        staged = record.layout.root / 'publication' / 'polyMesh'
        try:
            report = publish(
                record.layout.mesh, staged,
                categories=_gmsh_patch_categories(prepared),
                identities=_gmsh_patch_identities(prepared),
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
            # mode` -- External Mesh -- so the first re-resolution after a
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

        self._require_utility('createNonConformalCouples')
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

    def _require_utility(self, name: str) -> str:
        capability = self._capabilities_registry().utility(name)
        if not getattr(capability, 'available', False) or not getattr(capability, 'executable', None):
            raise CapabilityUnavailableError('required utility is unavailable', details={
                'utility': name})
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

    def _classify(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.case import classify_case
        classification = classify_case(session.case_path)
        payload = {'kind': getattr(classification.kind, 'value', str(classification.kind)),
                   'reasons': list(getattr(classification, 'reasons', [])),
                   'has_mesh': classification.has_mesh,
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
        from foammesh.settings.local_settings import LocalSettings, LocalSettingKey
        case_root = (session.case_path if (session.case_path / 'constant').is_dir()
                     else session.case_path / 'case')
        cores = int(command.parameters.get(
            'cores', LocalSettings(session.storage_path).get(LocalSettingKey.PARALLEL_NP, 1)))
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

    async def _parallel_configure(self, session: CaseSession, command: Command) -> OperationResult:
        parallel = command.parameters.get('parallel')
        if not isinstance(parallel, dict):
            raise ValidationFailedError('a serializable parallel configuration is required')
        from foammesh.support.mpi import ParallelEnvironment, ParallelType
        try:
            cores = int(parallel['cores'])
            if cores < 1:
                raise ValueError('cores must be positive')
            type_value = parallel.get('type', 'LOCAL_MACHINE')
            parallel_type = (ParallelType[type_value] if isinstance(type_value, str)
                             else ParallelType(int(type_value)))
            environment = ParallelEnvironment(
                cores, parallel_type, str(parallel.get('hosts', '')))
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationFailedError(f'invalid parallel configuration: {error}') from error
        from foammesh.settings.local_settings import LocalSettings
        settings = LocalSettings(session.storage_path)
        previous = settings.parallelEnvironment().np()
        case_root = (session.case_path if (session.case_path / 'constant').is_dir()
                     else session.case_path / 'case')
        executions = []
        if cores != previous:
            processor_cases = tuple(case_root.glob('processor[0-9]*'))
            if processor_cases:
                reconstructed = await self._run_openfoam_utility(
                    session, command, 'reconstructPar',
                    ('-constant', '-noFields', '-case', str(case_root)),
                    cwd=case_root, mutation=True)
                executions.append(reconstructed)
                if not reconstructed.succeeded:
                    return OperationResult(
                        'failed', command.operation, session.revisions,
                        payload={'previous_cores': previous, 'cores': cores,
                                 'executions': [
                                     item.to_payload() for item in executions]})
            # Redistribution only applies to a mesh that exists. Choosing the
            # core count before meshing is the normal order of work, and
            # running decomposePar on an empty case failed -- which left the
            # setting unwritten, so parallel meshing could never be requested
            # up front. The dictionary is still written, because that is what
            # the mesher decomposes with when it runs.
            has_mesh = (case_root / 'constant' / 'polyMesh' / 'owner').is_file()
            if cores > 1:
                self._write_decompose_par_dict(
                    case_root, cores,
                    DecompositionSettings.read(session.state.db))
            if cores > 1 and has_mesh:
                decomposed = await self._run_openfoam_utility(
                    session, command, 'decomposePar',
                    ('-case', str(case_root), '-force'),
                    cwd=case_root, mutation=True,
                    expected=(ExpectedArtifact(
                        case_root / 'processor0', kind='processor-case',
                        validator=lambda path: path.is_dir()),))
                executions.append(decomposed)
                if not decomposed.succeeded:
                    return OperationResult(
                        'failed', command.operation, session.revisions,
                        payload={'previous_cores': previous, 'cores': cores,
                                 'executions': [
                                     item.to_payload() for item in executions]})
        settings.setParallelEnvironment(environment)
        return self._read_result(
            session, command, {
                'previous_cores': previous, 'cores': cores,
                'executions': [item.to_payload() for item in executions],
                'runtime_fingerprint':
                    self._capabilities_registry().runtime_fingerprint(
                        'decomposePar' if cores > 1 else 'reconstructPar')
                    if executions else None,
            })

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
        and the cell counts cannot be left behind either.
        """
        from foammesh.openfoam import decomposition

        return decomposition.write(case_root, int(cores), settings)

    async def _run_openfoam_utility(
            self, session: CaseSession, command: Command, utility: str,
            arguments, *, cwd: Path, mutation: bool = False,
            expected=()):
        self._require_utility(utility)
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

    def _launch_paraview(self, session: CaseSession, command: Command) -> OperationResult:
        import subprocess
        from foammesh.core.shell import paraview_argv
        executable = self._require_utility('paraview')
        process = subprocess.Popen(
            paraview_argv(executable, session.case_path), cwd=session.case_path)
        return self._read_result(session, command, {'pid': process.pid, 'started': True})

    async def _launch_terminal(self, session: CaseSession, command: Command) -> OperationResult:
        import shutil
        import shlex
        import subprocess
        registry = self._capabilities_registry()
        self._require_utility('blockMesh')
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

    def _geometry_import(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        source = self._source(command)
        if source.suffix.lower() not in self._GEOMETRY_EXTENSIONS:
            raise ValidationFailedError('unsupported geometry format', details={
                'suffix': source.suffix,
                'supported': sorted(self._GEOMETRY_EXTENSIONS)})
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.diagnostics import budget as budget_module

        # Import runs surface diagnostics in-process, and one of them used to
        # be unbounded: a 37,240-triangle propeller wedged the application for
        # over seven hours with nothing on screen. Publishing the same job
        # events a subprocess would lets the status-bar widget show progress
        # and drive its Cancel button, and registering the budget gives that
        # button something to act on.
        job_id = f'geometry-import-{uuid.uuid4().hex[:12]}'
        budget = budget_module.budget_from_settings(
            'geometry.import',
            on_progress=lambda check, fraction, message: session.state.bus.publish(
                Event.JOB_PROGRESS, job_id=job_id, name=command.operation,
                stage=check, fraction=fraction, message=message))
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
            imported = GeometryArtifactStore(session.case_path).import_file(
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

    def _geometry_diagnostics(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            reports = GeometryArtifactStore(session.case_path).diagnose(
                command.parameters.get('geometry_id'))
        except KeyError as error:
            raise PreconditionFailedError('geometry artifact does not exist', details={
                'geometry_id': str(error.args[0])}) from error
        return self._read_result(session, command, {
            'geometry_count': len(reports), 'geometries': reports})

    def _geometry_readiness(self, session: CaseSession, command: Command) -> OperationResult:
        from foammesh.core.geometry import GeometryArtifactStore
        store = GeometryArtifactStore(session.case_path)
        target_cell_size = None
        try:
            target_cell_size = float(session.state.db.getValue('baseGrid/targetCellSize'))
        except (KeyError, TypeError, ValueError):
            pass
        try:
            report = store.readiness_report(
                command.parameters.get('geometry_id'),
                target_cell_size=target_cell_size)
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

        report = GeometryArtifactStore(session.case_path).readiness_report()
        states = {
            item['diagnostics']['readiness']['state']
            for item in report['geometries']
            if item['diagnostics'].get('readiness')
        }
        acknowledgment = command.parameters.get('ack_reason')
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
    def _invalidate_geometry_preparation(session: CaseSession) -> None:
        from foammesh.core.project import Source
        from foammesh.db.configurations_schema import GeometryPreparationDecision
        current = session.state.db.getValue('geometryPreparation/decision')
        current_value = current.value if hasattr(current, 'value') else current
        if current_value == GeometryPreparationDecision.UNDECIDED.value:
            return
        data = session.state.checkout()
        data.setValue('geometryPreparation/decision', GeometryPreparationDecision.UNDECIDED)
        data.setValue('geometryPreparation/geometryFingerprint', None)
        data.setValue('geometryPreparation/acknowledgment', None)
        session.state.commit(
            data, action='reopen geometry preparation', source=Source.SYSTEM,
            target='geometryPreparation', reason='geometry changed')

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
        patch_uuid = self._patch_uuid_named(editor, previous)
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

    def _geometry_combine(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        geometry_ids = command.parameters.get('geometry_ids')
        if not isinstance(geometry_ids, list):
            raise ValidationFailedError('geometry_ids must be a list')
        from foammesh.core.geometry import GeometryArtifactStore
        try:
            combined = GeometryArtifactStore(session.case_path).combine(
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

    def _geometry_transform(self, session: CaseSession, command: Command) -> OperationResult:
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
            transformed = GeometryArtifactStore(session.case_path).transform(
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
        try:
            target_cell_size = float(session.state.db.getValue('baseGrid/targetCellSize'))
        except (KeyError, TypeError, ValueError):
            target_cell_size = None
        report = store.readiness_report(
            geometry_id, target_cell_size=target_cell_size)
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
        return self._read_result(session, command, {
            key: value for key, value in report.items() if key != '_polydata'})

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
                'Open step 3, Meshing Method, and apply one.')
        engine = resolve_engine(session.state.db)
        # F-12. Both engines, one answer. This used to run for snappy only,
        # so an imported case was ready to generate dictionaries in one
        # engine and refused in the other for want of a step the user was
        # never told to take.
        try:
            prepared = await asyncio.to_thread(
                _ensure_prepared_geometry, session,
                producer='workflow.generate_dictionaries',
                require_domain=True)
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

        return self._read_result(session, command, payload)

    async def _workflow_run_stage(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        stage = command.parameters.get('stage')
        from foammesh.core.engine import configured_engine_id, resolve_engine
        if configured_engine_id(session.state.db) == 'unselected':
            raise PreconditionFailedError(
                'Choose a meshing method before running a meshing stage. '
                'Open step 3, Meshing Method, and apply one.')
        engine = resolve_engine(session.state.db)
        try:
            definition = engine.validate(stage)
        except ValueError as error:
            raise ValidationFailedError(
                'unknown meshing stage', details={'stage': stage}) from error
        feature_payload = None
        if definition.stage in {'castellation', 'snappyHexMesh'}:
            self._validate_fluid_seed(session)
            # Snappy consumes ``.eMesh`` edges that only ``surfaceFeatures``
            # produces. Extract them on demand so a single staged stage is
            # runnable on a fresh case and after a batch pipeline run alike.
            feature_payload = await self._ensure_surface_features(
                session, command, engine)
            self._require_current_surface_features(session)
        execution, payload = await self._run_stage_definition(
            session, command, engine, definition)
        if feature_payload is not None:
            payload['surface_features'] = feature_payload
        if execution.succeeded:
            # The implicit surfaceFeatures ran and succeeded too, so the tree
            # records it before the stage that consumed its edges.
            if feature_payload is not None:
                self._record_stage_run_success(
                    session, command, 'snappy.surface_features')
            payload['task_state'] = self._record_stage_run_success(
                session, command, getattr(definition, 'task_id', None),
                warning=bool(execution.warnings))
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
        return OperationResult(
            'accepted' if execution.succeeded else 'failed', command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=execution.warnings, payload=payload)

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

        The count is the case's parallel environment -- what the Parallel
        Environment dialog writes -- clamped by the execution ceiling and by
        the machine. Stages used to ignore it entirely and always build a
        serial command line, so a case configured for sixteen cores still
        meshed on one.
        """
        from foammesh.core.execution import (
            ResourceFacts, ResourceMode, ResourcePolicy, ResourceRequest,
            allocate_resources,
        )
        from foammesh.core.execution.resources import ResourceError

        policy_values = _resource_policy(session.configuration())
        # Every snappy phase decomposes. Measured directly against OpenFOAM 13
        # on a 1M-cell annulus, 16 ranks meshed in 34s against 139s serial and
        # agreed to 0.03% -- so there is no phase that has to be held back, and
        # no reason for a mode that decomposes only some of them.
        if str(policy_values['mode'] or '').lower() == 'serial':
            return 1
        try:
            requested = (int(command.parameters.get('cores') or 0)
                         or _configured_parallel_cores(session))
        except (TypeError, ValueError):
            return 1
        if requested <= 1:
            return 1
        try:
            allocation = allocate_resources(
                ResourcePolicy(
                    ResourceMode(str(policy_values['mode'])),
                    policy_values['max_cpu_cores'],
                    policy_values['max_memory_bytes'],
                    policy_values['allow_distributed'], 'openfoam-mpi'),
                ResourceRequest(requested, backend_id='openfoam-mpi'),
                ResourceFacts.local())
        except (ResourceError, ValueError, TypeError):
            return 1
        return max(1, int(allocation.effective_ranks))

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

    @staticmethod
    def _discard_decomposition(case_path: Path) -> None:
        """Remove processor cases whose mesh has been superseded."""
        from foammesh.support.utils import rmtree
        for processor in case_path.glob('processor[0-9]*'):
            rmtree(processor)

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
        execution = await self._run_openfoam_utility(
            session, command, 'reconstructPar',
            ('-constant', '-case', str(session.case_path)),
            cwd=session.case_path, mutation=True)
        if not execution.succeeded:
            raise PreconditionFailedError(
                'the parallel mesh could not be reconstructed into the case '
                'root', details={'error': 'reconstruct_failed'})
        (session.case_path / self.PENDING_GATHER).unlink(missing_ok=True)
        payload = execution.to_payload()
        payload['trace'] = self._trace_mesh_state(session, 'reconstruct')
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
                                 engine, ranks: int) -> list:
        """Decompose the case once, so later stages continue on processor meshes."""
        case_root = session.case_path
        existing = tuple(case_root.glob('processor[0-9]*'))
        if existing and len(existing) == int(ranks):
            return []
        if existing:
            # Processor cases from an earlier run with a different rank count
            # would be picked up by mpirun as if they were this run's. Remove
            # them and decompose for the ranks actually requested.
            self._discard_decomposition(case_root)
        engine.write_parallel_config(session.state.db, case_root, ranks)
        execution = await self._run_openfoam_utility(
            session, command, 'decomposePar',
            ('-case', str(case_root), '-force'),
            cwd=case_root, mutation=True)
        if not execution.succeeded:
            raise PreconditionFailedError(
                'the case could not be decomposed for parallel meshing',
                details={'error': 'decompose_failed', 'ranks': ranks})
        return [execution.to_payload()]

    async def _run_stage_definition(self, session: CaseSession, command: Command,
                                    engine, definition):
        """Regenerate the stage dictionary and execute one meshing stage."""
        stage = definition.stage
        utility = self._require_utility(definition.utility)
        dictionary = session.case_path / 'system' / definition.dictionary
        if not dictionary.is_file():
            raise PreconditionFailedError(
                f'{definition.dictionary} is required; generate dictionaries first')
        if definition.utility in {'surfaceFeatures', 'snappyHexMesh'}:
            self._preflight_snappy_inputs(session, definition.utility)
        try:
            stage_run = engine.run_stage(
                stage, db=session.state.db, case_path=session.case_path,
                executable=utility)
        except (FileNotFoundError, ValueError) as error:
            raise PreconditionFailedError(
                str(error), details={'error': 'stage_inputs_unavailable',
                                     'stage': definition.stage}) from error
        registry = self._capabilities_registry()

        # Only the snappy stages decompose; blockMesh is trivial and
        # surfaceFeatures has no parallel form.
        ranks = (self._stage_ranks(session, command, definition)
                 if definition.utility == 'snappyHexMesh' else 1)
        decomposition: list = []
        if ranks == 1 and self._gather_pending(session.case_path):
            # A serial phase reads the case root, so an earlier decomposed
            # phase has to be gathered first. The processor cases then hold a
            # mesh this phase is about to supersede; leaving them would let a
            # later parallel phase pick up a stale decomposition.
            await self._ensure_reconstructed(session, command)
            self._discard_decomposition(session.case_path)
        if ranks > 1:
            for name in ('decomposePar', 'reconstructPar', 'mpirun'):
                capability = registry.utility(name) if hasattr(
                    registry, 'utility') else None
                if capability is not None and not capability.available:
                    raise CapabilityUnavailableError(
                        'parallel meshing needs the full OpenFOAM runtime',
                        details={'utility': name, 'reason': capability.reason})
            decomposition = await self._ensure_decomposed(
                session, command, engine, ranks)

        if hasattr(registry, 'command'):
            if ranks > 1:
                launch = registry.command(
                    'mpirun',
                    ('-np', str(ranks), definition.utility, '-parallel',
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
            timeout=command.parameters.get(
                'timeout_seconds', definition.timeout_seconds),
            max_output_bytes=4 * 1024 * 1024,
            expected_artifacts=(ExpectedArtifact(
                poly_mesh, kind='feature-edges' if feature_stage else 'polyMesh',
                validator=valid_poly_mesh),),
            recover_mesh=not feature_stage,
            artifact_event=(Event.ARTIFACT_DICTIONARIES_CHANGED if feature_stage
                            else Event.ARTIFACT_MESH_CHANGED),
            invalidated_outputs=('mesh', 'quality') if feature_stage else ('quality',),
            cleanup_argv=launch.cleanup_argv,
        ), on_line=command.parameters.get('on_line'))
        payload = execution.to_payload()
        payload.update({
            'stage': definition.stage, 'utility': definition.utility,
            'engine': engine.engine_id,
            'profile_id': launch.profile_id,
            'ranks': ranks,
        })
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
            # mode` -- External Mesh -- so the first re-resolution after a
            # save re-opened our own freshly meshed case as an import and
            # collapsed the workflow outline to Scene / Display.
            from foammesh.core.case import record_generated_mesh
            record_generated_mesh(session.case_path, provenance={
                'generated_by': f'{engine.engine_id}:{definition.stage}'})
        if feature_stage and execution.succeeded:
            import hashlib
            payload['feature_artifacts'] = [{
                'path': str(path),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'dictionary': str(dictionary),
            } for path in sorted(poly_mesh.glob('*.eMesh'))]
            provenance = self._surface_feature_inputs(session)
            from foammesh.core.geometry import GeometryArtifactStore
            advisories = []
            for geometry in GeometryArtifactStore(session.case_path).diagnose():
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
        from foammesh.core.quality import parse_layer_log
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
            return {}
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
                'step 3, Meshing Method, and apply one.')
        try:
            engine = resolve_engine(session.state.db)
        except EngineNotRegisteredError as error:
            raise PreconditionFailedError(
                f'this case names a meshing method this build does not have: '
                f'{configured_engine_id(session.state.db)}') from error
        return await engine.run(EngineRunRequest(session, command, self))

    async def run_snappy_pipeline(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        """Execute the serial/MPI DAG and publish a validated mesh-state ref.

        The body of :meth:`SnappyMeshingEngine.run`, which is what reaches it.
        """
        session.require_writable()
        from foammesh.core.engine import resolve_engine
        from foammesh.core.execution import (
            ResourceFacts, ResourceMode, ResourcePolicy, ResourceRequest,
            allocate_resources, openfoam_meshing_dag,
        )
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
        configuration = _resource_policy(session.configuration())
        mode = ResourceMode(str(command.parameters.get(
            'mode') or configuration['mode']))
        # What to run on comes from the case's parallel environment -- the
        # count the Parallel Environment dialog writes and decomposeParDict is
        # built from. Without this the dialog applied, reported success, and
        # every mesh stage still ran on one rank, because the only number
        # consulted here was the execution *ceiling*, which has no GUI control.
        # ``maxCpuCores`` stays the ceiling and still clamps the request below.
        parallel_cores = _configured_parallel_cores(session)
        requested_cores = int(command.parameters.get('cores')
                              or parallel_cores
                              or configuration['max_cpu_cores'] or 1)
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
                ResourceFacts.local())
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
            capabilities = [
                registry.utility(name) for name in required_utilities]
            missing = [
                item for item in capabilities if not item.available]
            if missing:
                raise CapabilityUnavailableError(
                    'OpenFOAM 13 pipeline runtime is incomplete',
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
        mpi_options = ()
        if allocation.effective_ranks > 1 and hasattr(registry, 'mpi_options'):
            mpi_options = tuple(registry.mpi_options())
        from foammesh.core.quality.checkmesh_service import checkmesh_request

        dag = openfoam_meshing_dag(
            session.case_path, allocation, split_at_snap=mode.enforces,
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
        # Plan 30 F-15. Both engines leave the same record behind. Gmsh wrote
        # a run manifest for every run and snappy wrote none, so the Runs
        # surface could only ever show half a case's history -- which is why
        # it filtered to the Gmsh rows and called that the list.
        record = self._open_run_manifest(
            session, engine_id='snappy',
            job={'job_digest': dag.digest if hasattr(dag, 'digest') else '',
                 'dag': dag.to_dict(), 'allocation': allocation.to_dict(),
                 'revision_seal': revision_seal})
        checkpoints = []
        executions = []
        check_execution = None
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
                    f'pipeline utility unavailable: {utility}') from error
            execution = await self._context(session).executor.execute(
                session, OperationSpec(
                    operation=f'workflow.pipeline.{node.node_id}',
                    argv=launch.argv, cwd=node.cwd,
                    mutation=node.mutates_mesh,
                    timeout=float(command.parameters.get(
                        'timeout_seconds', 3600)),
                    max_output_bytes=10 * 1024 * 1024,
                    recover_mesh=node.mutates_mesh,
                    cleanup_argv=launch.cleanup_argv),
                on_line=command.parameters.get('on_line'))
            executions.append(execution.to_payload())
            if node.node_id == 'checkMesh':
                check_execution = execution
            if not execution.succeeded:
                failure = execution.to_payload()
                stopped = str((failure.get('job') or {}).get('status')
                              or '') == 'cancelled'
                reason = ('the run was cancelled before it finished' if stopped
                          else (str(failure.get('error') or '').strip()
                                or f'{node.node_id} failed'))
                self._close_run_manifest(
                    record, status='cancelled' if stopped else 'failed',
                    reason=reason)
                return OperationResult(
                    'failed', command.operation, session.revisions,
                    invalidated_outputs=('quality', 'exports'),
                    payload={'allocation': allocation.to_dict(),
                             'dag': dag.to_dict(), 'executions': executions,
                             'checkpoints': checkpoints,
                             'failed_node': node.node_id,
                             'run_id': self._run_manifest_id(record),
                             # One failure shape, whichever engine wrote it
                             # (Plan 30 F-03). `RunResultHandle.from_payload`
                             # is the reader; the finisher no longer has to
                             # know which orchestrator it is reading.
                             **failure_payload(
                                 task=f'snappy.{node.node_id}',
                                 reason=reason,
                                 log=str(failure.get('log_path') or ''))})
            if node.pauses_after:
                # The snapped boundary is in the case root and the next node is
                # about to overwrite it. Capture it immutably now; GF1 measures
                # this, and a blocked run will name its fingerprint.
                checkpoints.append(self._save_snap_checkpoint(
                    session, run_id=str(execution.to_payload().get('job_id') or '')))
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
        try:
            recorded = self._record_engine_run_success(
                session, command, warning=not_clean)
        except FacadeError:
            # The mesh is on disk and published; a tree that cannot be told
            # about it (no registered engine, no descriptor) is not a failure
            # of the run.
            recorded = None
        if recorded is not None:
            recorded['warning'] = not_clean
        task_state = self._advance_pipeline_gates(session, command, recorded)
        self._close_run_manifest(
            record, status='succeeded',
            publication={'status': 'published',
                         'artifact': str(session.case_path / 'constant'
                                         / 'polyMesh'),
                         'mesh_state': reference.to_dict()})
        return OperationResult(
            'accepted', command.operation, session.revisions,
            invalidated_outputs=('quality', 'exports'),
            payload={'allocation': allocation.to_dict(), 'dag': dag.to_dict(),
                     'run_manifest': record.document if record else {},
                     'executions': executions,
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
            'reason': 'no interior point was found -- the surface is probably '
                      'open, so place the seed yourself'})

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
        return self._read_result(session, command, dict(
            probe, known=True, point=point))

    def _validate_fluid_seed(self, session: CaseSession) -> None:
        """Reject a surface/outside locationInMesh before launching snappy."""
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.mesh.sizing import validate_fluid_seed
        store = GeometryArtifactStore(session.case_path)
        entries = store.entries()
        if not entries:  # legacy DB-only geometry has no immutable artifact to probe
            return
        regions = list(session.state.db.getElements('region').values())
        if not regions:
            raise PreconditionFailedError('a fluid region seed is required')
        point = regions[0].vector('point')
        # A valid CFD enclosure is often partitioned into separate wall,
        # inlet, and outlet artifacts.  No individual artifact is watertight,
        # so probing them one-by-one rejects a perfectly valid pipe.  Validate
        # the assembled immutable surface set as the solver sees it -- the
        # same assembly `geometry.fluid_seed.suggest` searches, so a suggested
        # seed cannot be refused here.
        assembled_surface = self._assembled_surface(session)
        assembled = validate_fluid_seed(assembled_surface, point)
        if assembled['valid']:
            return
        individual = (
            [assembled] if len(entries) == 1 else [
                validate_fluid_seed(store._polydata(entry), point)
                for entry in entries
            ])
        on_surface = assembled['on_surface']
        raise PreconditionFailedError(
            'fluid seed lies on a geometry surface' if on_surface else
            'fluid seed is outside the intended closed geometry region',
            details={'error': 'invalid_fluid_seed', 'point': list(point),
                     'assembled_probe': assembled,
                     'component_probes': individual})

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
        help_result = registry.help('checkMesh') if hasattr(registry, 'help') else None
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

    async def _mesh_check(self, session: CaseSession, command: Command) -> OperationResult:
        # checkMesh judges the case-root mesh, so a decomposed result has to be
        # gathered first or the check would grade the previous stage's mesh.
        await self._ensure_reconstructed(session, command)
        self._require_mesh(session)
        utility = self._require_utility('checkMesh')
        from foammesh.core.quality import (
            CheckMeshRequest, MeshCheckService, parse_checkmesh)
        from foammesh.core.quality.checkmesh_service import checkmesh_flags
        registry = self._capabilities_registry()
        profile, profile_warnings = self._checkmesh_profile()
        from foammesh.core.quality.checkmesh_service import checkmesh_request
        # Plan 31. The project's own checkMesh settings first -- thresholds,
        # the problem-face surfaces, the user-defined criteria -- then the
        # operation's parameters on top, because a caller that names one is
        # asking for this run and not for the project.
        request = checkmesh_request(
            session.state.db, session.case_path,
            write_sets=bool(command.parameters.get('write_sets', True)),
            set_format=str(command.parameters.get('set_format', 'vtk')),
            extended_topology=bool(
                command.parameters.get('extended_checks', True)),
            extended_geometry=bool(
                command.parameters.get('extended_checks', True)))
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

        def parse_and_persist(result):
            parsed = parse_checkmesh(result.output)
            _, report = MeshCheckService.persist_result(
                session.case_path, parsed, command=result.argv,
                log_path=result.log_path or session.case_path / 'foammesh' / 'logs' /
                'checkMesh.log')
            return report.to_dict()

        execution = await self._context(session).executor.execute(session, OperationSpec(
            operation=command.operation,
            argv=launch.argv,
            cwd=session.case_path,
            timeout=command.parameters.get('timeout_seconds', 900),
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
        mesh_accepted = (
            verdict.get('mesh_ok') is True
            and verdict.get('incomplete') is False)
        # Plan 30 F-05. **Accept anyway** on a snappy mesh lands here: snappy
        # publishes as it goes, so the mesh checkMesh judged is the mesh on
        # disk and there is nothing to re-mesh -- only a decision to record
        # against it. A refusal to bind the decision is raised at the caller
        # rather than swallowed, exactly as it is on the Gmsh route.
        override = (self._accept_checked_mesh(session, command, payload,
                                              verdict)
                    if execution.succeeded and not mesh_accepted else None)
        if execution.succeeded:
            # checkMesh is the QA task's evidence, so the run that produced
            # the report is what advances the row. Exactly this one task: a
            # report on the case-root mesh proves nothing about which stages
            # ran before it, and never about an optional layers stage that
            # was skipped. A poor mesh is still complete evidence (§8.5) and
            # advances the task as a warning; qualification is the summary's
            # business. A QA row whose parents are not yet accepted is left
            # blocked, with the reason in the payload.
            payload['task_state'] = self._record_qa_run(
                session, command, warning=not mesh_accepted,
                waived=override is not None)
        return OperationResult(
            'accepted' if execution.succeeded and (mesh_accepted or override)
            else 'failed',
            command.operation,
            session.revisions, invalidated_outputs=('quality',),
            warnings=tuple(execution.warnings) + tuple(profile_warnings),
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
        """Judge the mesh for SU2, and advance the QA row on the strength of it.

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
        payload['task_state'] = self._record_qa_run(
            session, command, warning=not accepted)
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
        """
        from foammesh.core.quality.checkmesh_service import (
            NATIVE_CHECK, MeshCheckService, check_for,
        )

        try:
            check = check_for(self._qa_operation(session))
        except Exception:                                   # noqa: BLE001
            check = NATIVE_CHECK
        report = MeshCheckService.load_report(session.case_path, check)
        if report is None and check != NATIVE_CHECK:
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
            'waivers': waivers})

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
        utility = self._require_utility(repair.utility_name)
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
        utility = self._require_utility('transformPoints')
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
        utility = self._require_utility('extrudeMesh')
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
        self._require_utility(converter.utility_name)
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

        model = self._boundary_model(session, patch_identity)
        if model is None:
            report = report_module.build(
                task_id, (), evidence=evidence, policy=policy)
            path = report_module.write(case_path, report)
            return self._read_result(session, command, {
                'report': report.to_dict(), 'path': str(path),
                'reason': 'no published mesh or no patch-identity sidecar, so '
                          'no section could be joined to a prepared patch'})

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

        model = self._boundary_model(session, patch_identity)
        if model is None:
            report = report_module.build(
                task_id, (), evidence=evidence, policy=policy)
            path = report_module.write(case_path, report)
            return self._read_result(session, command, {
                'report': report.to_dict(), 'path': str(path),
                'requested_size': None,
                'reason': 'no published mesh or no patch-identity sidecar, so '
                          'no section could be joined to a prepared patch'})

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

    def _boundary_model(self, session: CaseSession, patch_identity):
        """The reconciled boundary, or ``None`` when it cannot be built."""
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
            return boundary.load(case_path, identity)
        except Exception:                                   # noqa: BLE001
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
            capability = registry.utility(name)
            if capability.available:
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

    def _dataset_export(self, session, command, entry_id) -> OperationResult:
        self._require_mesh(session)
        qualification = self._require_export_authorization(session, command)
        destination = self._destination(session, command)
        from foammesh.core.import_export.service import ImportExportService
        service = ImportExportService()
        try:
            adapter = {
                'vtk': 'export_vtu',
                'cgns': 'export_cgns',
                'gmsh': 'export_gmsh',
                'su2': 'export_su2',
                'med': 'export_med',
                'unv': 'export_unv',
            }[entry_id]
            result = getattr(service, adapter)(session.case_path, destination)
        except (OSError, ValueError) as error:
            raise ValidationFailedError(str(error)) from error
        payload = dict(_to_payload(result))
        payload['qualification'] = qualification
        return self._read_result(session, command, payload)

    def _export_vtk(self, session, command):
        return self._dataset_export(session, command, 'vtk')

    def _export_cgns(self, session, command):
        return self._dataset_export(session, command, 'cgns')

    def _export_gmsh(self, session, command):
        return self._dataset_export(session, command, 'gmsh')

    def _export_su2(self, session, command):
        return self._dataset_export(session, command, 'su2')

    def _export_med(self, session, command):
        return self._dataset_export(session, command, 'med')

    def _export_unv(self, session, command):
        return self._dataset_export(session, command, 'unv')

    async def _export_fluent(self, session, command):
        self._require_mesh(session)
        self._require_utility('foamMeshToFluent')
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
        self._require_utility('foamFormatConvert')
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
            payload = await AuthoredExportService(
                capabilities=self._capabilities_registry()).run(
                session, destination,
                boundaries=command.parameters.get('boundaries', ()), options=options,
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
