"""What one meshing run produced, and which artifact the viewport must draw.

F-37. The Gmsh quality gate returns *before* the publication step, so a
refused run never writes ``constant/polyMesh``. R210 then made both finishers
draw whenever a quality verdict existed, and the only thing they knew how to
draw was the case root -- which, after an earlier accepted run, still holds
that earlier mesh. The user was shown one mesh under another mesh's verdict,
with nothing on screen saying so.

The fix is an identity, not a flag. A run hands back a handle: which run, which
engine, which file on disk, a fingerprint of that file, the verdict, and the
one directory the polyMesh loader may be pointed at for *this* run. When there
is no such directory the handle says so, and the viewport says "no mesh for
this run" rather than keeping a picture that belongs to a different run.

Deliberately pure: no Qt, no VTK, no facade. The rules about which artifact
belongs to which run are the part worth testing, and they are all here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: The run's mesh was accepted and published to the case root.
ACCEPTED = 'accepted'
#: The run built a mesh and the quality gate refused it.
REFUSED = 'refused'
#: The run built a mesh, and the run did not end as an acceptance. Section
#: 4.2's own state name. CP-05 item 4: the gate verdict is recorded before
#: publication, so a run whose gate passed and whose publication was then
#: cancelled or failed had `quality_verdict.accepted: true` on a manifest that
#: says `publication_failed` -- and read back as `accepted`, which is the
#: completed badge on a run that did not complete.
CANDIDATE = 'candidate'
#: The run produced no mesh at all.
NOTHING = 'none'

_DISPOSITION = {
    ACCEPTED: 'accepted',
    CANDIDATE: 'candidate, this run did not finish',
    REFUSED: 'candidate, refused on quality',
    NOTHING: 'no mesh for this run',
}

# -- artifact identity (Plan 31 section 4.2, CP-02) ------------------------- #
#
# C31-03. A result used to be identified by "whatever is at
# ``constant/polyMesh`` right now". MEASURED in the tier-1 sweep: four of ten
# Gmsh runs were refused on quality, every one of those cases holds the run's
# ``mesh.msh``, and none of them holds a ``constant/polyMesh`` at all -- so
# every reader downstream concluded there was no mesh. Since CP-01 an
# *accepted* run with no solver target is in the same position: nothing asked
# for a polyMesh, so none was published, and the artifact of that run is
# permanently the ``.msh``.
#
# A result is therefore named by the run that made it and the format its
# artifact is in, and the viewport dispatches on that format.

#: An OpenFOAM ``constant/polyMesh``, opened through a case root.
POLY_MESH = 'openfoam.polymesh'
#: A native Gmsh ``.msh``, opened directly.
GMSH_MSH = 'gmsh.msh'
#: DP-133. The surface pass of a three-dimensional Gmsh run: the same file
#: format, read the other way. It is a separate format rather than a flag on
#: :data:`GMSH_MSH` because the reader has to be *told* -- a surface mesh
#: arriving where a volume was expected is the silent failure
#: ``read_msh_scene`` refuses by default, and one identity that sometimes
#: means a volume and sometimes a surface would hand that refusal back.
GMSH_SURFACE_MSH = 'gmsh.surface.msh'
#: Formats the viewport has a reader for.
READABLE_FORMATS = (POLY_MESH, GMSH_MSH, GMSH_SURFACE_MSH)

#: The artifact the mesher itself wrote.
NATIVE = 'native'
#: Something made from the native artifact so it could be drawn.
DERIVATIVE = 'visualization-derivative'
#: Something made from the native artifact for a solver.
EXPORTED = 'exported'

#: Verdict -> the section-4.2 state name. The verdicts are what the run
#: reports; the states are what the contract calls them.
_STATE = {
    ACCEPTED: 'accepted',
    CANDIDATE: 'candidate',
    REFUSED: 'rejected candidate',
    NOTHING: 'failed without mesh',
}

#: How each format names itself in the line above the picture.
_FORMAT_LABEL = {
    POLY_MESH: 'polyMesh',
    GMSH_MSH: 'native Gmsh mesh',
    GMSH_SURFACE_MSH: 'surface pass, before the volume pass',
}

#: Where a run keeps a polyMesh a viewer could open, relative to its own
#: directory. Each entry is a *case* root, so ``<entry>/constant/polyMesh`` is
#: what has to exist -- that is the shape ``vtkPOpenFOAMReader`` reads.
_RUN_CASE_ROOTS = ('candidate', 'publication', '.')


def _verdict_for(status: str, built: bool) -> str:
    """The verdict a run reporting this *status* has.

    ``accepted`` is the only status that yields :data:`ACCEPTED`, and
    ``candidate`` is available to a caller that knows a mesh was built but
    that the run did not end in an acceptance.
    """
    token = str(status or '').strip().lower()
    if token == 'accepted':
        return ACCEPTED
    if not built:
        return NOTHING
    return CANDIDATE if token == 'candidate' else REFUSED


def fingerprint_of(path) -> str:
    """``size:mtime`` for a file, or the aggregate of a directory's files.

    Cheap on purpose. This is asked after every run, on artifacts that reach
    gigabytes; a hash of the mesh would cost more than the draw it guards.
    What it has to do is distinguish "the file this run wrote" from "the file
    the previous run wrote", and size plus modification time does that.
    """
    target = Path(path)
    try:
        if target.is_file():
            stat = target.stat()
            return f'{stat.st_size}:{int(stat.st_mtime)}'
        if target.is_dir():
            size = 0
            newest = 0
            for entry in sorted(target.iterdir()):
                if not entry.is_file():
                    continue
                stat = entry.stat()
                size += stat.st_size
                newest = max(newest, int(stat.st_mtime))
            if not size:
                return ''
            return f'{size}:{newest}'
    except OSError:
        return ''
    return ''


def failure_payload(*, task: str, reason: str, log='',
                    built: bool = False) -> dict:
    """The one shape a failed run reports, whichever engine ran it.

    Plan 30 F-03. The two orchestrators failed differently: snappy's payload
    named a ``failed_node``, Gmsh's named a ``reason``, and each finisher read
    only its own -- so a Gmsh failure shown by snappy's finisher, or the other
    way round, said nothing at all. ``built`` is the R205 discriminator: a
    mesh that was measured and refused is not a run that never happened, and
    the two need different words.

    :func:`RunResultHandle.from_payload` is the reader of this shape.
    """
    return {
        'task': str(task or ''),
        'reason': str(reason or ''),
        'log': str(log or ''),
        'built': bool(built),
    }


#: How much of a log's end is read for its cause. A failing utility says why
#: in its last lines; a 4 MiB stage log is not read whole to find them.
_CAUSE_TAIL_BYTES = 256 * 1024
#: OpenFOAM's header for the message it dies with, in either form.
_FOAM_FATAL = re.compile(r'FOAM FATAL (?:IO )?ERROR', re.IGNORECASE)
#: The lines OpenFOAM prints after the message: where in its source it was.
_FOAM_TRAILER = ('From ', 'in file', 'FOAM exiting', 'FOAM aborting')
#: A line that says something went wrong, in the words tools use for it --
#: Gmsh's ``Error   : ...``, Python's ``Traceback`` and ``...Error:``, a shell's
#: ``command not found``.
_ERROR_LINE = re.compile(
    r'(?:error|fatal|exception|traceback|abort(?:ed|ing)?|not found|'
    r'segmentation fault|killed)\b', re.IGNORECASE)
#: How many error lines make a cause, and how many make its details.
_CAUSE_LINES = 3
_DETAIL_LINES = 40


def failure_cause(text: str) -> tuple[str, str]:
    """``(cause, details)`` for a failed run, read from its output.

    DP-506 (MA-02). A snappy stage that exited 1 reached the user as "The
    stage could not run." while its log held the one sentence that says what
    to change -- OpenFOAM's ``FOAM FATAL ERROR`` block naming the unknown
    region and listing the valid ones. That block is the cause when there is
    one: its message lines are the gist, the whole block is the details.
    Otherwise the last lines that say *error* are the cause, and the log's
    tail is the details. The cause is empty when no line says what went
    wrong; nothing is made up to fill it.
    """
    lines = [line.rstrip() for line in str(text or '').splitlines()]
    start = None
    for index, line in enumerate(lines):
        if _FOAM_FATAL.search(line):
            start = index
    if start is not None:
        block = []
        for line in lines[start:]:
            block.append(line)
            if line.strip().startswith(('FOAM exiting', 'FOAM aborting')):
                break
        header = _FOAM_FATAL.split(lines[start], maxsplit=1)[-1]
        message = [header.strip(' :')] if header.strip(' :') else []
        for line in block[1:]:
            stripped = line.strip()
            if stripped.startswith(_FOAM_TRAILER):
                break
            if stripped:
                message.append(stripped)
        details = '\n'.join(block).strip('\n')
        return '\n'.join(message) or details, details
    meaningful = [line for line in lines if line.strip()]
    errors = [line.strip() for line in meaningful if _ERROR_LINE.search(line)]
    if not errors:
        return '', '\n'.join(meaningful[-_DETAIL_LINES:])
    return ('\n'.join(errors[-_CAUSE_LINES:]),
            '\n'.join(meaningful[-_DETAIL_LINES:]))


def read_failure_cause(log) -> tuple[str, str]:
    """:func:`failure_cause` of the log at *log*; ``('', '')`` if unreadable."""
    if not log:
        return '', ''
    try:
        path = Path(log)
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - _CAUSE_TAIL_BYTES))
            raw = stream.read()
    except (OSError, ValueError):
        return '', ''
    return failure_cause(raw.decode('utf-8', errors='replace'))


def drawable_root(run_root) -> str:
    """The case root under ``run_root`` the polyMesh loader can open, or ''.

    A refused Gmsh run leaves ``mesh.msh`` and nothing else: the publisher is
    what turns an ``.msh`` into a polyMesh and it never ran. Nothing in the
    render path reads Gmsh's format, so unless some step has staged a polyMesh
    inside the run directory there is nothing to draw -- which is a fact to
    report, not one to paper over with the previous mesh.
    """
    root = Path(run_root)
    for name in _RUN_CASE_ROOTS:
        candidate = (root if name == '.' else root / name)
        if (candidate / 'constant' / 'polyMesh').is_dir():
            return str(candidate)
    return ''


def _content_id(payload: dict) -> str:
    """The run's own content identity for its mesh, if it computed one.

    The Gmsh run manifest records ``mesh_sha256`` -- a real digest of the
    ``.msh`` -- which is what section 4.2 asks acceptance and export to bind
    to. A second-resolution modification time is not a publication identity;
    the cheap fingerprint is only the fallback for a run that recorded none.
    """
    manifest = payload.get('run_manifest') or {}
    return str(manifest.get('mesh_sha256') or payload.get('mesh_sha256') or '')


def _surface_path(payload: dict, run_path) -> str:
    """Where this run left its surface pass, or '' -- DP-133.

    The run's own manifest records it. The fallback to the run directory is
    for a run that finished before that key existed and whose `surface.msh`
    is nonetheless sitting there; it is a re-read of a file the run wrote,
    not a guess at one it might have.

    Either way the file is checked. A recorded path is an absolute path from
    the machine the run happened on, and a run directory that has since been
    moved or cleaned out must read as "no surface pass" rather than as one
    the viewport will fail to open.
    """
    record = (payload.get('run_manifest') or {}).get('surface_artifact') or {}
    candidate = str(record.get('path') or '')
    if not candidate and run_path:
        candidate = str(Path(run_path) / 'surface.msh')
    if candidate and Path(candidate).is_file():
        return candidate
    return ''


def _cell_count(payload: dict) -> int:
    """How many cells the *run* reported, before anything was drawn."""
    manifest = payload.get('run_manifest') or {}
    statistics = (manifest.get('statistics') or {}).get('mesh') or {}
    for value in (statistics.get('cells'),
                  ((payload.get('quality_report') or {}).get('result')
                   or {}).get('cells')):
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return count
    return 0


@dataclass(frozen=True)
class RunResultHandle:
    """One run's identity and the artifact that belongs to it."""

    run_id: str = ''
    engine: str = ''
    artifact_path: str = ''
    fingerprint: str = ''
    verdict: str = NOTHING
    cell_count: int = 0
    #: Case root for the polyMesh loader. Empty for a native artifact, which
    #: is opened by path rather than through a case.
    case_root: str = ''
    #: Which reader opens :attr:`artifact_path` -- one of
    #: :data:`READABLE_FORMATS`.
    artifact_format: str = ''
    #: Native, visualization derivative or exported (section 4.2).
    artifact_role: str = NATIVE
    #: Content identity where the run computed one (the manifest's
    #: ``mesh_sha256``). The cheap fingerprint stands in when it did not.
    content_id: str = ''
    #: Set when this handle is a deliberate re-selection of an earlier result
    #: rather than the result of the run that just finished.
    label: str = ''
    #: DP-133. The surface pass this run kept, if it kept one. Carried on the
    #: handle rather than looked for at the moment of asking, because by then
    #: the only thing that knows which run directory to look in is the handle.
    #: Empty for a section, for a run that wrote none, and for snappy, which
    #: shows its stages as it goes and has never needed this.
    surface_path: str = ''

    # -- producing side ---------------------------------------------------- #

    @staticmethod
    def identity_payload(*, run_id: str, engine: str, artifact,
                         run_path='') -> dict:
        """The identity fields an operation puts on its result payload.

        Written before the quality gate can refuse the run, because a refused
        run is exactly the one whose artifact the viewport has to name.
        """
        return {
            'engine': str(engine),
            'run_id': str(run_id),
            'run_path': str(run_path or ''),
            'artifact_path': str(artifact or ''),
            'artifact_fingerprint': fingerprint_of(artifact) if artifact
            else '',
        }

    # -- consuming side ---------------------------------------------------- #

    @classmethod
    def from_payload(cls, payload, *, status: str, case_path,
                     engine: str = '') -> 'RunResultHandle':
        """Read one run's handle off the payload its operation returned."""
        payload = dict(payload or {})
        case = Path(case_path)
        mesh_state = payload.get('mesh_state') or {}
        engine_id = str(payload.get('engine') or mesh_state.get('engine_id')
                        or engine or '')
        run_id = str(payload.get('run_id') or mesh_state.get('run_id') or '')
        built = (bool(payload.get('quality_verdict'))
                 or bool(payload.get('built')))
        verdict = _verdict_for(str(status), built)
        run_path = payload.get('run_path') or (
            case / 'foammesh' / 'runs' / run_id if run_id else '')
        # DP-133. Read before the branches below and given to every one of
        # them, because a run that left a surface pass left it whichever way
        # the run ended -- and the run whose volume pass failed outright is
        # the one for which it is worth the most.
        surface = _surface_path(payload, run_path)

        root_mesh = case / 'constant' / 'polyMesh'
        if verdict == NOTHING:
            return cls(run_id=run_id, engine=engine_id, verdict=verdict,
                       cell_count=_cell_count(payload),
                       surface_path=surface)
        if engine_id != 'gmsh':
            # snappyHexMesh meshes the case in place: its candidate *is* the
            # root mesh, refused or not, because it has already overwritten
            # whatever was there.
            #
            # The case root is named without checking for
            # `constant/polyMesh`: a multi-region mesh is in
            # `constant/<region>/polyMesh` and a time-directory mesh is
            # somewhere else again, and the loader already refuses a case
            # with no mesh anywhere (`PolyMeshLoader.hasMesh`). Guessing
            # here would take a region mesh off the screen to fix a Gmsh
            # defect that has nothing to do with it.
            return cls(
                run_id=run_id, engine=engine_id, verdict=verdict,
                artifact_path=str(root_mesh),
                fingerprint=(str(mesh_state.get('fingerprint') or '')
                             or fingerprint_of(root_mesh)),
                cell_count=_cell_count(payload),
                case_root=str(case), artifact_format=POLY_MESH,
                surface_path=surface)

        # A Gmsh run. C31-03: the polyMesh at the case root is claimed only
        # when *this* run's publication record says this run wrote it. An
        # accepted run that published nothing -- which since CP-01 is every
        # run with no solver target -- owns its `.msh` and not the root, and
        # so does a refused candidate. Substituting the root here is how a
        # previous run's cells came to be shown under this run's verdict.
        published = _published_case_root(payload, case)
        if published:
            return cls(
                run_id=run_id, engine=engine_id, verdict=verdict,
                artifact_path=str(Path(published) / 'constant' / 'polyMesh'),
                fingerprint=fingerprint_of(
                    Path(published) / 'constant' / 'polyMesh'),
                cell_count=_cell_count(payload),
                case_root=str(published), artifact_format=POLY_MESH,
                content_id=_content_id(payload), surface_path=surface)

        artifact = payload.get('artifact_path') or (
            str(Path(run_path) / 'mesh.msh') if run_path else '')
        # The mesher's own file is the result. The viewport reads it directly
        # (`core.mesh.msh_scene`), so a refused candidate is drawn from its
        # own run directory and is never published to the accepted case root
        # merely to be rendered.
        if artifact and Path(artifact).is_file():
            return cls(
                run_id=run_id, engine=engine_id, verdict=verdict,
                artifact_path=str(artifact),
                fingerprint=str(payload.get('artifact_fingerprint') or ''),
                cell_count=_cell_count(payload), case_root='',
                artifact_format=GMSH_MSH, content_id=_content_id(payload),
                surface_path=surface)
        # No native file to read -- but a step may have staged a polyMesh
        # inside the run directory. That is a labelled derivative *under the
        # run*, not a publication.
        staged = drawable_root(run_path) if run_path else ''
        if staged:
            return cls(
                run_id=run_id, engine=engine_id, verdict=verdict,
                artifact_path=str(Path(staged) / 'constant' / 'polyMesh'),
                fingerprint=str(payload.get('artifact_fingerprint') or ''),
                cell_count=_cell_count(payload), case_root=str(staged),
                artifact_format=POLY_MESH, artifact_role=DERIVATIVE,
                content_id=_content_id(payload), surface_path=surface)
        return cls(
            run_id=run_id, engine=engine_id, verdict=verdict,
            artifact_path=str(artifact or ''),
            fingerprint=str(payload.get('artifact_fingerprint') or ''),
            cell_count=_cell_count(payload), case_root='',
            artifact_format=GMSH_MSH if artifact else '',
            content_id=_content_id(payload), surface_path=surface)

    # -- what the viewport asks it ----------------------------------------- #

    @property
    def produced_a_mesh(self) -> bool:
        return self.verdict in (ACCEPTED, CANDIDATE, REFUSED)

    @property
    def state(self) -> str:
        """The section-4.2 state name for this result."""
        return _STATE.get(self.verdict, self.verdict)

    @property
    def artifact_id(self) -> str:
        """What headers, counters, overlays and selection are bound to.

        Run plus format plus content: two runs of the same case differ, and so
        do the native mesh and the polyMesh published from it, which is the
        distinction an overlay computed on one must not cross onto the other.
        """
        if not self.artifact_path:
            return ''
        return '#'.join((self.run_id or self.engine or 'run',
                         self.artifact_format or 'unknown',
                         self.content_id or self.fingerprint or ''))

    @property
    def drawable(self) -> bool:
        """Whether the viewport has a reader for this run's own artifact."""
        if self.artifact_format == POLY_MESH:
            return bool(self.case_root)
        if self.artifact_format in (GMSH_MSH, GMSH_SURFACE_MSH):
            return (bool(self.artifact_path)
                    and Path(self.artifact_path).is_file())
        return False

    def surface_result(self) -> 'RunResultHandle | None':
        """This run's surface pass as a result in its own right, or ``None``.

        DP-133. A separate handle rather than a second path on this one,
        because everything downstream of a handle -- the reader that opens
        it, the sentence above the picture, the identity the overlay and the
        selection are bound to -- has to change together when the user asks
        for the surface instead of the volume. Two artifacts of one run are
        two results; treating them as one result with a flag is how a
        selection computed on the volume ends up drawn over the surface.

        The cell count is dropped rather than carried over: it counts the
        volume's cells, and this mesh has none. No ``label`` either: the
        format already names itself in the sentence, and a label would say
        the same words twice.
        """
        if not self.surface_path:
            return None
        return RunResultHandle(
            run_id=self.run_id, engine=self.engine,
            artifact_path=self.surface_path,
            fingerprint=fingerprint_of(self.surface_path),
            verdict=self.verdict, cell_count=0, case_root='',
            artifact_format=GMSH_SURFACE_MSH, artifact_role=NATIVE)

    def describe(self) -> str:
        """The line the viewport header shows above the picture."""
        disposition = _DISPOSITION.get(self.verdict, self.verdict)
        parts = [self.run_id or self.engine or 'run', disposition]
        if self.label:
            parts.insert(0, self.label)
        if not self.produced_a_mesh:
            return ' · '.join(parts)
        if self.cell_count:
            parts.append(f'{self.cell_count:,} cells')
        if self.drawable and self.artifact_format in _FORMAT_LABEL:
            parts.append(_FORMAT_LABEL[self.artifact_format])
        if not self.drawable:
            # Said out loud, because the alternative the user would otherwise
            # assume -- that the cells on screen are this run's -- is the
            # thing F-37 was.
            parts.append('not drawn: this run left no artifact to read')
        return ' · '.join(parts)


def _published_case_root(payload: dict, case: Path) -> str:
    """The case root this run published to, or '' if it published nothing.

    The run's own publication record is the only evidence accepted. A record
    that says ``skipped`` or ``failed`` published nothing, and an absent
    record is not evidence of a publication either: since CP-01 a run with no
    solver target skips publication and still finishes accepted, so "accepted"
    on its own says nothing about ``constant/polyMesh``.
    """
    publication = payload.get('publication')
    if not isinstance(publication, dict):
        return ''
    if str(publication.get('status') or '') in ('skipped', 'failed'):
        return ''
    destination = str(publication.get('destination') or '')
    if not destination:
        return ''
    path = Path(destination)
    if path.name == 'polyMesh' and path.parent.name == 'constant':
        return str(path.parent.parent)
    return str(case)


# -- reopening a case (CP-02 item 7) ---------------------------------------- #
#
# Everything above answers "what did the run that just finished produce". A
# case opened tomorrow asks the same question with no payload to read it off,
# and the old answer -- draw ``constant/polyMesh`` if one is there -- is the
# same substitution in a different place: it cannot name the run, and on a
# native Gmsh case it finds nothing at all while the run's ``.msh`` sits in
# the run directory.
#
# The durable record is each run's own ``run-manifest.json``. MEASURED across
# the fourteen manifests under ``test_cases/gmsh``: every one carries
# ``run_id``, ``engine_id``, ``mesh_sha256``, ``finished_at``,
# ``statistics.mesh.cells``, a ``quality_verdict`` and a ``publication``
# document -- and finned_tube's run gmsh-7f84fde4b0c3454e carries
# ``accepted: false``, ``publication.status: failed``, no
# ``constant/polyMesh`` and a 141,486-cell ``mesh.msh``. That case is exactly
# the one this section has to be able to reopen.
#
# Read as plain JSON rather than through ``core.gmsh.manifest``, to keep this
# module free of the engine packages.

#: The file each run writes describing itself.
RUN_MANIFEST = 'run-manifest.json'


def _read_manifest(run_root: Path) -> dict:
    import json

    try:
        document = json.loads(
            (run_root / RUN_MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _reopened_publication(case: Path, document: dict) -> dict:
    """The run's publication record, re-checked against the case on disk.

    The recorded destination is an absolute path from the machine the run
    happened on; a case that has since been copied, moved, or had its mesh
    deleted would otherwise be reported as publishing to a directory that is
    not there. So the record is honoured only when this case really holds a
    ``constant/polyMesh`` now, and it names *this* case root when it does.
    """
    publication = document.get('publication')
    if not isinstance(publication, dict):
        return {}
    if not (case / 'constant' / 'polyMesh').is_dir():
        return {}
    return dict(publication, destination=str(case / 'constant' / 'polyMesh'))


def verdict_for_disposition(disposition: str, *, built: bool) -> str:
    """The verdict for one of ``core.gmsh.manifest``'s dispositions.

    Only ``accepted`` is an acceptance and only ``rejected`` is a refusal on
    quality. Everything else -- failed, cancelled, incomplete, a publication
    interrupted mid-flight -- is a mesh that exists and is not the accepted
    result, which is what section 4.2 calls a *candidate*. Written as a
    default rather than a table so that a disposition the manifest module
    grows later reads as a candidate, never as an acceptance: the safe
    direction for a word this module has not been taught yet.
    """
    if not built:
        return NOTHING
    token = str(disposition or '').strip().lower()
    if token == 'accepted':
        return ACCEPTED
    if token == 'rejected':
        return REFUSED
    return CANDIDATE


def _run_status(document: dict) -> str:
    """How a stored run ended, in :meth:`RunResultHandle.from_payload` terms.

    Read, not re-derived. ``core.gmsh.manifest.run_disposition`` is the one
    place that decides what a stored manifest says happened to its run, and it
    weighs three fields this module used to weigh two of: the lifecycle
    status, the publication record beneath it, and the gate verdict.

    CP-05 item 4 is where those disagree. The Gmsh pipeline records the gate
    verdict and *then* publishes, so a run whose publication was cancelled or
    failed carries ``accepted: true`` under a status that is not an ending.
    MEASURED again after CP-05b added ``record_publication_started``: a run
    killed mid-publish and then reconciled to ``interrupted`` still read back
    as ``gmsh-a - accepted - 1 cells - native Gmsh mesh``, because this
    module kept its own list of unfinished statuses and neither ``publishing``
    nor ``interrupted`` was on it. Two vocabularies for one question is how
    that gap opened; there is now one.

    Imported inside the function on purpose. This module is deliberately pure
    -- no Qt, no VTK, no facade -- and reads run manifests as plain JSON, so
    the engine package is a dependency of this one function and not of the
    module.
    """
    from foammesh.core.gmsh.manifest import run_disposition

    verdict = verdict_for_disposition(run_disposition(document), built=True)
    if verdict == ACCEPTED:
        return 'accepted'
    return 'refused' if verdict == REFUSED else 'candidate'


def _handle_from_manifest(case: Path, run_root: Path,
                          document: dict) -> RunResultHandle:
    """One stored run, as the handle the viewport would have been given."""
    payload = {
        'engine': str(document.get('engine_id') or ''),
        'run_id': str(document.get('run_id') or run_root.name),
        'run_path': str(run_root),
        'artifact_path': str(run_root / 'mesh.msh'),
        'artifact_fingerprint': fingerprint_of(run_root / 'mesh.msh'),
        'publication': _reopened_publication(case, document),
        'quality_verdict': document.get('quality_verdict') or {},
        'run_manifest': document,
    }
    return RunResultHandle.from_payload(
        payload, status=_run_status(document),
        case_path=case, engine=str(document.get('engine_id') or ''))


def case_results(case_path) -> list:
    """Every run this case still holds a manifest for, newest first."""
    case = Path(case_path)
    runs = case / 'foammesh' / 'runs'
    try:
        entries = sorted(runs.iterdir())
    except OSError:
        return []
    found = []
    for run_root in entries:
        if not run_root.is_dir():
            continue
        document = _read_manifest(run_root)
        if not document:
            continue
        stamp = str(document.get('finished_at')
                    or document.get('launched_at')
                    or document.get('created_at') or '')
        found.append((stamp, run_root.name,
                      _handle_from_manifest(case, run_root, document)))
    found.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [handle for _stamp, _name, handle in found]


def accepted_result(case_path, *, exclude_run: str = ''):
    """The run the case root's mesh belongs to, or ``None``.

    The separate accepted-result pointer of section 4.2: the newest run that
    was accepted *and* published. A rejected candidate never becomes this,
    whatever it left behind in its own directory.
    """
    for handle in case_results(case_path):
        if exclude_run and handle.run_id == exclude_run:
            continue
        if handle.verdict == ACCEPTED and handle.artifact_format == POLY_MESH:
            return handle
    return None


def unclaimed_case_mesh(case_path):
    """The mesh a case holds that no run of ours claims, or ``None``.

    An imported case, or one meshed before any of this existed. It is a real
    result and has to be drawable, but it is labelled as what it is rather
    than attributed to a run that did not make it.
    """
    case = Path(case_path)
    if not (case / 'constant' / 'polyMesh').is_dir():
        return None
    return RunResultHandle(
        verdict=ACCEPTED, case_root=str(case),
        artifact_path=str(case / 'constant' / 'polyMesh'),
        fingerprint=fingerprint_of(case / 'constant' / 'polyMesh'),
        artifact_format=POLY_MESH, label='mesh already in this case')


def result_on_open(case_path, *, root_mesh_stale=False):
    """What to draw when a case is opened, named by the run that made it.

    The published mesh when the case has one; otherwise the newest run that
    left a readable native artifact -- which is the whole of a native Gmsh
    case with no solver target, and the whole of a case whose last run was
    refused. ``None`` when the case holds neither, and *that* is when the
    viewport is right to be empty.

    DP-794. ``root_mesh_stale`` says the case's ``constant/polyMesh`` is no
    longer the mesh any run of ours wrote (its content fingerprint differs
    from the authored provenance). A run's publication record cannot tell
    that -- it is honoured whenever *a* polyMesh is there -- so such a mesh
    is drawn on its own account, and never swapped for a run's native file,
    which holds the old mesh.
    """
    if root_mesh_stale:
        unclaimed = unclaimed_case_mesh(case_path)
        if unclaimed is not None:
            return unclaimed
    accepted = accepted_result(case_path)
    if accepted is not None and accepted.drawable:
        return accepted
    for handle in case_results(case_path):
        if handle.drawable:
            return handle
    return unclaimed_case_mesh(case_path)
