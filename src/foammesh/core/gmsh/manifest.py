"""Run manifest: what was asked for, what was run, and what came back.

A run directory is immutable once written. The manifest is the record that
makes a mesh traceable to the exact job, runner and profile that produced it,
and it carries the requested-against-achieved ledger rather than a summary of
it, so a later reader can see what the mesher actually did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time

MANIFEST_NAME = 'run-manifest.json'
SCHEMA_VERSION = 1


class ManifestError(ValueError):
    pass


def utc_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class RunLayout:
    """Where one run keeps its inputs and outputs."""

    root: Path

    @property
    def job(self) -> Path:
        return self.root / 'job.json'

    @property
    def result(self) -> Path:
        return self.root / 'result.json'

    @property
    def progress(self) -> Path:
        return self.root / 'progress.jsonl'

    @property
    def log(self) -> Path:
        return self.root / 'runner.log'

    @property
    def mesh(self) -> Path:
        return self.root / 'mesh.msh'

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def su2(self) -> Path:
        return self.root / 'mesh.su2'

    def outputs(self, formats=('msh',)) -> dict:
        names = {'msh': self.mesh, 'su2': self.su2,
                 'med': self.root / 'mesh.med',
                 'cgns': self.root / 'mesh.cgns', 'vtk': self.root / 'mesh.vtk'}
        return {key: names[key] for key in formats if key in names}


def _artifact_rows(layout: RunLayout, formats) -> list[dict]:
    """The files this run was asked to produce, named in host paths.

    Plan 30 WP-07 (F-08). ``mesh.su2`` was written by the runner and then
    existed only as a file nobody had recorded: the manifest listed the ``.msh``
    by hash and said nothing about the SU2 file, so the Export page could not
    know it was there and re-derived a different one through the VTK writer.
    The job's own ``output`` block is the list of what was asked for, and it is
    kept here in the run's own namespace rather than the runtime's -- a WSL
    path in a manifest a Windows GUI reads is not a path.
    """
    return [{'format': key, 'name': path.name, 'path': str(path),
             'exists': path.is_file()}
            for key, path in layout.outputs(tuple(formats)).items()]


def _refresh_artifacts(layout: RunLayout, rows) -> list[dict]:
    """Re-state each recorded artifact against what is actually on disk."""
    refreshed = []
    for row in rows or ():
        entry = dict(row)
        path = Path(entry.get('path') or '')
        try:
            exists = path.is_file()
        except OSError:
            exists = False
        entry['exists'] = exists
        if exists:
            entry['bytes'] = path.stat().st_size
            entry['sha256'] = sha256_of(path)
        refreshed.append(entry)
    return refreshed


@dataclass
class RunManifest:
    run_id: str
    layout: RunLayout
    document: dict = field(default_factory=dict)

    def write(self) -> Path:
        """Persist atomically; a half-written manifest is worse than none."""
        target = self.layout.manifest
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(
            json.dumps(self.document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, target)
        return target

    @classmethod
    def read(cls, root: Path) -> 'RunManifest':
        layout = RunLayout(Path(root))
        if not layout.manifest.is_file():
            raise ManifestError(f'no run manifest in {root}')
        return cls(Path(root).name, layout,
                   json.loads(layout.manifest.read_text(encoding='utf-8')))


class RunBuilder:
    """Create a run directory and record its lifecycle."""

    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path)

    def root(self, run_id: str) -> Path:
        return self.case_path / 'foammesh' / 'runs' / run_id

    def create(self, run_id: str, *, job: dict, profile=None,
               runner_path: Path | None = None,
               engine_id: str = 'gmsh') -> RunManifest:
        """Open a run directory and its manifest.

        Plan 30 F-15. ``engine_id`` was the string ``'gmsh'`` because this was
        the only engine that recorded a run at all; a snappy pipeline left
        nothing here, so the Runs list had to filter to Gmsh and the two
        engines could not be shown side by side. It is a parameter now, and
        the caller says which engine's run this is.
        """
        layout = RunLayout(self.root(run_id))
        if layout.root.exists():
            raise ManifestError(f'run {run_id} already exists')
        layout.root.mkdir(parents=True)
        layout.job.write_text(
            json.dumps(job, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        document = {
            'schema_version': SCHEMA_VERSION,
            'run_id': run_id,
            'engine_id': str(engine_id or 'gmsh'),
            'created_at': utc_now(),
            'status': 'created',
            'job_digest': job.get('job_digest', ''),
            'job_sha256': sha256_of(layout.job),
            'runner_sha256': sha256_of(runner_path) if runner_path else '',
            'profile': profile.to_dict() if profile is not None else {},
            'warnings': list(job.get('intent', {}).get('warnings', ())),
            'artifacts': _artifact_rows(
                layout, tuple((job.get('output') or {}).keys())),
        }
        manifest = RunManifest(run_id, layout, document)
        manifest.write()
        return manifest

    @staticmethod
    def record_launch(manifest: RunManifest, command) -> RunManifest:
        manifest.document.update({
            'status': 'running',
            'launched_at': utc_now(),
            'command': command.to_dict() if hasattr(command, 'to_dict')
            else {'argv': list(command)},
        })
        manifest.write()
        return manifest

    @staticmethod
    def record_result(manifest: RunManifest, result: dict, *,
                      exit_code: int | None = None) -> RunManifest:
        """Fold the runner's own result into the manifest, verbatim.

        The ledger is kept whole rather than summarised: a reader who wants to
        know whether a control took effect should not have to trust a summary
        written by the thing being audited.
        """
        statistics = dict(result.get('statistics') or {})
        manifest.document.update({
            'status': result.get('status', 'failed'),
            'finished_at': result.get('finished_at') or utc_now(),
            'exit_code': exit_code,
            'error': result.get('error', ''),
            'statistics': statistics,
            'controls': list(result.get('controls') or ()),
            'control_mismatches': list(result.get('controlMismatches') or ()),
            'unresolved_scopes': list(result.get('unresolvedScopes') or ()),
            'runner_warnings': list(result.get('warnings') or ()),
        })
        manifest.document['meshed_by'] = _meshed_by(statistics)
        if manifest.layout.mesh.is_file():
            manifest.document['mesh_sha256'] = sha256_of(manifest.layout.mesh)
        rows = manifest.document.get('artifacts')
        if not rows:
            # An older run, or one created before the artifact list existed.
            rows = _artifact_rows(manifest.layout, ('msh', 'su2'))
            rows = [row for row in rows if row['exists']]
        manifest.document['artifacts'] = _refresh_artifacts(
            manifest.layout, rows)
        manifest.write()
        return manifest

    @staticmethod
    def record_quality_verdict(manifest: RunManifest, verdict) -> RunManifest:
        manifest.document['quality_verdict'] = (
            verdict.to_dict() if hasattr(verdict, 'to_dict') else dict(verdict))
        manifest.write()
        return manifest

    @staticmethod
    def record_quality_report(manifest: RunManifest,
                              document: dict) -> RunManifest:
        """Keep this run's own copy of the report its gate wrote.

        Plan 31 CP-05 item 2. The gate report is filed once per *case*, so
        every run overwrote the last one and there was no way to ask what an
        earlier candidate had been judged on -- which is how inspecting an
        accepted mesh came to be served a later candidate's report and the
        waiver granted against it. The copy is small (one verdict and its five
        evidence keys) and it is what :mod:`core.quality.binding` reads.
        """
        manifest.document['quality_report'] = dict(document or {})
        manifest.write()
        return manifest

    @staticmethod
    def record_publication_started(manifest: RunManifest,
                                   destination) -> RunManifest:
        """Say, before the mesh moves, that this run is publishing.

        Plan 31 CP-05 item 7. Publication ends with one of three records --
        published, skipped, failed -- and every one of them is written *after*
        the work. A process killed between the swap and the record therefore
        left a manifest saying ``succeeded`` with no publication key at all,
        which is byte-for-byte the manifest of a run that deliberately
        published nothing: MEASURED on a reconstructed run, the reopened case
        described it as "accepted -- native Gmsh mesh", the same sentence a
        complete SU2-route run gets. Writing the intent first is what makes
        the interruption knowable afterwards; nothing else on disk records it.
        """
        manifest.document['publication'] = {
            'status': 'in_progress',
            'started_at': utc_now(),
            'attempted_destination': str(destination),
        }
        manifest.document['status'] = 'publishing'
        manifest.write()
        return manifest

    @staticmethod
    def record_publication(manifest: RunManifest, payload: dict) -> RunManifest:
        manifest.document['publication'] = dict(payload)
        manifest.document['status'] = 'published'
        manifest.write()
        return manifest

    @staticmethod
    def record_publication_failure(manifest: RunManifest, reason: str) -> RunManifest:
        manifest.document['publication'] = {'status': 'failed', 'reason': reason}
        manifest.document['status'] = 'publication_failed'
        manifest.write()
        return manifest

    @staticmethod
    def record_cancelled(manifest: RunManifest, reason: str = '') -> RunManifest:
        """The user stopped this run; say so rather than calling it a failure.

        Plan 31 CP-05 item 7. A cancelled job writes no ``result.json``, so
        the pipeline read the runner's absence and recorded
        ``status: failed`` with "the Gmsh runner produced no result file" --
        MEASURED: on disk, a cancellation and a WSL distribution that failed
        to start are the same record, and the Runs list has no way to tell a
        user which of the two happened.
        """
        manifest.document.update({
            'status': 'cancelled',
            'finished_at': manifest.document.get('finished_at') or utc_now(),
            'error': str(reason or 'the run was cancelled'),
        })
        manifest.write()
        return manifest


def run_artifacts(document, fmt: str = '') -> tuple[dict, ...]:
    """The artifact rows a manifest document carries, optionally one format."""
    rows = [dict(row) for row in (document or {}).get('artifacts') or ()
            if isinstance(row, dict)]
    if fmt:
        rows = [row for row in rows if row.get('format') == fmt]
    return tuple(rows)


def latest_artifact(case_path: str | Path, fmt: str) -> dict | None:
    """The newest run's artifact of one format, if that file is still there.

    Plan 30 WP-07 (F-08). This is how a caller outside the Gmsh package finds
    the mesh Gmsh actually wrote, instead of deriving a second one of its own.
    """
    root = Path(case_path) / 'foammesh' / 'runs'
    if not root.is_dir():
        return None
    runs = sorted((item for item in root.iterdir()
                   if (item / MANIFEST_NAME).is_file()),
                  key=lambda item: item.stat().st_mtime, reverse=True)
    for item in runs:
        try:
            document = json.loads(
                (item / MANIFEST_NAME).read_text(encoding='utf-8'))
        except ValueError:
            continue
        for row in run_artifacts(document, fmt):
            path = Path(row.get('path') or '')
            if path.is_file():
                return {**row, 'run_id': document.get('run_id', item.name),
                        'run_path': str(item)}
    return None


# -- what state a stored run is in (Plan 31 CP-05, items 5 and 7) ----------- #
#
# Every field read below was checked against the 42 real manifests under
# `test_cases/gmsh` before it was relied on (the DP-12 lesson: a manifest key
# can be dead for the repository's whole history). Populated there:
# `status` (42), `quality_verdict` (34), `publication` (33), `mesh_sha256`
# (34), `artifacts` rows with `sha256`/`bytes` (20 of 28 rows -- the eight
# without are runs that never finished). `quality_override` is written by
# `_record_quality_override` when a human keeps a mesh the gate refused.

#: The run was stopped by the user.
CANCELLED = 'cancelled'
#: The run stopped before it recorded a result -- the process is gone and no
#: `finished_at` was ever written.
INCOMPLETE = 'incomplete'
#: The run failed.
FAILED = 'failed'
#: A mesh was built and refused, with no human override.
REJECTED = 'rejected'
#: Publication began and never recorded an outcome.
PUBLICATION_INTERRUPTED = 'publication_interrupted'
#: A mesh was built and kept: the gate passed, or a human overrode it.
ACCEPTED = 'accepted'

#: Dispositions whose artifacts an export or a snapshot may be built from.
EXPORTABLE = (ACCEPTED,)


def run_disposition(document) -> str:
    """One word for what a stored run's manifest says happened to it.

    The order matters: an interrupted publication is reported as such even
    though the quality gate had already passed, because "the gate accepted it"
    and "the mesh reached the case" are different claims and only the first
    one is true. A publication that recorded a *failure* is the same two
    claims with the second one answered no, which is why it is read here and
    not left to the gate verdict beneath it.
    """
    document = document or {}
    status = str(document.get('status') or '')
    if status == CANCELLED:
        return CANCELLED
    publication = document.get('publication')
    publication_status = (str(publication.get('status') or '')
                          if isinstance(publication, dict) else '')
    if publication_status in ('in_progress', 'interrupted'):
        return PUBLICATION_INTERRUPTED
    if status in ('created', 'running', 'publishing') or not document.get(
            'finished_at'):
        # Read after the fact -- from a reopened case, or from a Runs list --
        # there is no process left to be waiting for.
        return INCOMPLETE
    # CP-05 item 4. `record_publication_failure` writes both of these, and a
    # gate verdict of `accepted` sits underneath them either way: the gate
    # ran before the publication was attempted. MEASURED: a run whose
    # publication failed reported `accepted` here, and the acceptance page
    # printed the completed badge over a mesh that never reached the case.
    if status in ('failed', 'publication_failed') or (
            publication_status == 'failed'):
        return FAILED
    verdict = document.get('quality_verdict')
    if isinstance(verdict, dict) and not verdict.get('accepted'):
        if not document.get('quality_override'):
            return REJECTED
    return ACCEPTED


def reconcile_interrupted_publications(case_path: str | Path) -> tuple[str, ...]:
    """Settle any publication left mid-flight, and say which runs those were.

    Plan 31 CP-05 item 7. ``record_publication_started`` writes the intent
    before the mesh moves; if the process comes back, the run whose intent was
    never answered is still marked ``in_progress`` and would otherwise read as
    a publication still in progress with no process behind it. On reopen it is
    settled to ``interrupted``, which is what it is: the quality gate accepted
    this run, and whether its mesh reached ``constant/polyMesh`` is not
    recorded either way.

    Nothing on the case is touched. Deciding on the user's behalf whether the
    mesh in the case root belongs to this run is exactly the guess that made
    the state unreadable in the first place; the run is re-run or re-accepted
    by someone who can see it.
    """
    settled = []
    for root, document in run_documents(case_path):
        publication = document.get('publication')
        if not isinstance(publication, dict):
            continue
        if str(publication.get('status') or '') != 'in_progress':
            continue
        publication = dict(publication)
        publication.update({
            'status': 'interrupted',
            'reconciled_at': utc_now(),
            'reason': ('this run was publishing its mesh when the application '
                       'stopped, so whether constant/polyMesh is this run\'s '
                       'mesh was never recorded'),
        })
        document['publication'] = publication
        document['status'] = 'interrupted'
        document['finished_at'] = document.get('finished_at') or utc_now()
        try:
            manifest = RunManifest.read(root)
        except (ManifestError, ValueError, OSError):
            continue
        manifest.document.update(document)
        try:
            manifest.write()
        except OSError:
            continue
        settled.append(str(document.get('run_id') or root.name))
    return tuple(settled)


def run_documents(case_path: str | Path) -> tuple[tuple[Path, dict], ...]:
    """Every readable run manifest in this case, newest first.

    Newest by directory modification time, which is what
    :func:`latest_artifact` has always ordered by; a run directory is written
    once and then only appended to.
    """
    root = Path(case_path) / 'foammesh' / 'runs'
    if not root.is_dir():
        return ()
    found = []
    for item in sorted((entry for entry in root.iterdir()
                        if (entry / MANIFEST_NAME).is_file()),
                       key=lambda entry: entry.stat().st_mtime, reverse=True):
        try:
            document = json.loads(
                (item / MANIFEST_NAME).read_text(encoding='utf-8'))
        except ValueError:
            continue
        if isinstance(document, dict):
            found.append((item, document))
    return tuple(found)


def accepted_artifact(case_path: str | Path, fmt: str) -> dict | None:
    """The artifact of *fmt* belonging to the newest run that was kept.

    Plan 31 CP-05 item 5. :func:`latest_artifact` answers a different
    question -- "which run wrote one most recently" -- and MEASURED with an
    accepted run A followed by a rejected candidate B, that is what the SU2
    exporter consumed: the file it handed the user came out of B, the mesh the
    quality gate had just refused. An export is built from the result the user
    accepted, so a rejected, cancelled, failed or unfinished run is passed
    over here whatever it left in its own directory.
    """
    for root, document in run_documents(case_path):
        if run_disposition(document) not in EXPORTABLE:
            continue
        for row in run_artifacts(document, fmt):
            path = Path(row.get('path') or '')
            if path.is_file():
                return {**row, 'run_id': document.get('run_id', root.name),
                        'run_path': str(root), 'run_manifest': document}
    return None


def artifact_provenance_error(row) -> str:
    """Why *row*'s file is not the artifact the run recorded, or ``''``.

    The recorded ``sha256`` and ``bytes`` are what make a copy a copy of *that
    mesh* rather than of whatever now sits at the same path. A run directory
    is not read-only to the world: it is an ordinary folder in the user's case,
    and a file replaced or truncated there would otherwise be exported under
    the accepted run's name.
    """
    row = dict(row or {})
    path = Path(row.get('path') or '')
    name = path.name or str(row.get('name') or 'the artifact')
    if not path.is_file():
        return (f'the run recorded {name} but the file is no longer there; '
                're-run the mesher or export from the case')
    recorded = str(row.get('sha256') or '')
    if not recorded:
        return (f'the run recorded no content hash for {name}, so the file '
                'on disk cannot be shown to be the one it produced')
    size = int(row.get('bytes') or 0)
    if size and path.stat().st_size != size:
        return (f'{name} is {path.stat().st_size} bytes and the run recorded '
                f'{size}; the file has changed since it was written')
    if sha256_of(path) != recorded:
        return (f'{name} no longer matches the hash the run recorded; the '
                'file has been replaced since it was written')
    return ''


def published_mesh_is(document, case_path: str | Path) -> bool:
    """Whether this case's ``constant/polyMesh`` is still this run's.

    The publication record carries a ``checksums`` map of the five polyMesh
    files it wrote -- VERIFIED against the live mesh of
    ``test_cases/gmsh/duct_stl``, where all five still match the run that
    published them. A later run publishing over the top changes them, which is
    exactly the case a snapshot of the earlier run has to notice.
    """
    publication = (document or {}).get('publication')
    if not isinstance(publication, dict):
        return False
    checksums = ((publication.get('export') or {}).get('checksums')
                 if isinstance(publication.get('export'), dict) else None)
    if not isinstance(checksums, dict) or not checksums:
        return False
    mesh = Path(case_path) / 'constant' / 'polyMesh'
    for name, expected in checksums.items():
        candidate = mesh / str(name)
        if not candidate.is_file() or sha256_of(candidate) != str(expected):
            return False
    return True


def list_runs(case_path: str | Path) -> tuple[dict, ...]:
    root = Path(case_path) / 'foammesh' / 'runs'
    if not root.is_dir():
        return ()
    runs = []
    for item in sorted(root.iterdir()):
        manifest_path = item / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            document = json.loads(manifest_path.read_text(encoding='utf-8'))
        except ValueError:
            runs.append({'run_id': item.name, 'status': 'unreadable',
                         'disposition': INCOMPLETE})
            continue
        # The recorded status is left exactly as written; the disposition is
        # the reading of it, and it is what a reopened case shows. Without it
        # a run interrupted mid-publication is listed as `publishing` forever
        # and every reader has to know what that means.
        runs.append(dict(document, disposition=run_disposition(document)))
    return tuple(runs)


def _meshed_by(statistics: dict) -> dict:
    """Which algorithm actually produced this mesh.

    Plan 31 FC-B. ``Mesh.AlgorithmSwitchOnFailure`` is on by default in Gmsh,
    so a surface the chosen algorithm cannot mesh is meshed by a different one
    and the option still reads back as the algorithm that failed. The runner
    reads the algorithm out of Gmsh's own log per surface; this lifts it to the
    top of the manifest, where a reader asking "what made this mesh" looks,
    because a silent fallback is the case worth surfacing.
    """
    record = dict((statistics.get('algorithms') or {}))
    if not record:
        # An older run, or one that failed before it meshed anything.
        return {'observed': False, 'requested': '', 'used': [],
                'switched': [], 'fellBack': False, 'pipeline': False}
    return {
        'observed': bool(record.get('observed')),
        'requested': record.get('requestedName') or record.get('requested', ''),
        'used': list(record.get('used') or ()),
        'switched': list(record.get('switched') or ()),
        'fellBack': bool(record.get('switched')),
        # True when the chosen algorithm meshes through other algorithms by
        # design -- quasi-structured quad does -- so a reader can tell "used
        # names differ from the one requested because that is how it works"
        # from "used names differ because the one requested failed".
        'pipeline': bool(record.get('pipeline')),
        'bySurface': dict(record.get('bySurface') or {}),
    }
