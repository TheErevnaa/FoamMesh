"""One run listing for both engines (Plan 42 SG16, D9).

``mesh.gmsh.runs`` lists only the Gmsh batch runs that have a manifest.  A
snappy stage and a Gmsh session action had nothing, so a cancelled stage
vanished.  Each now ends by writing a small manifest here, and
:func:`list_runs` returns them beside the Gmsh run manifests in one ordered
list with one shape.

Entries live in ``<case>/foammesh/run-listing/`` and not under
``foammesh/runs/``: the latter holds the Gmsh runs whose artifacts are
published, and nothing there may be mistaken for a mesh to accept.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from foammesh.core.naming import EXACT_NAMES

SCHEMA_VERSION = 1
DIRECTORY = ('foammesh', 'run-listing')
#: The newest entries kept; older ones are removed when a new one is written.
KEEP = 200


def directory(case_path) -> Path:
    return Path(case_path).joinpath(*DIRECTORY)


def _safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', name)[:80] or 'run'


def write_entry(case_path, *, engine_id: str, kind: str, name: str,
                status: str, started_at: str, ended_at: str,
                message: str = '', run_id: str, **extra) -> Path:
    root = directory(case_path)
    root.mkdir(parents=True, exist_ok=True)
    document = {
        'schema_version': SCHEMA_VERSION, 'run_id': run_id,
        'engine_id': engine_id, 'kind': kind, 'name': name, 'status': status,
        'started_at': started_at, 'ended_at': ended_at, 'message': message,
        **extra}
    target = root / f'{_safe(run_id)}.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(document, indent=1, sort_keys=True),
                         encoding='utf-8')
    os.replace(temporary, target)
    _prune(root)
    return target


def _prune(root: Path) -> None:
    try:
        files = sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime)
        for stale in files[:-KEEP]:
            stale.unlink()
    except OSError:
        pass


def list_entries(case_path) -> list[dict]:
    root = directory(case_path)
    if not root.is_dir():
        return []
    entries = []
    for path in root.glob('*.json'):
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict):
            entries.append(document)
    return entries


#: DP-2855. What a run of each engine is called in the listing.
RUN_NAMES = {engine: f'{EXACT_NAMES[engine]} mesh run'
             for engine in ('gmsh', 'snappy')}

#: DP-2855. A run manifest's disposition as the listing's status, in the
#: words its stage and session-action rows use. ``accepted`` is the quality
#: gate's word for a kept mesh -- read as "accepted, not yet run" it said a
#: finished run had not ended -- so it stays, as ``disposition``, beside it.
RUN_STATUS = {'accepted': 'done', 'rejected': 'done', 'failed': 'failed',
              'cancelled': 'cancelled', 'publication_interrupted': 'failed',
              'incomplete': 'incomplete'}


def _from_gmsh_manifest(document: dict, case_path=None) -> dict:
    """One row of a run manifest (either engine; the manifest is Gmsh's)."""
    from foammesh.core.jobs.run_log import active

    engine_id = str(document.get('engine_id') or 'gmsh')
    run_id = str(document.get('run_id') or '')
    disposition = str(document.get('disposition') or '')
    log_path = document.get('log_path')
    if not log_path and case_path is not None and run_id:
        # A Gmsh run's runner writes its log in the run directory.
        runner = Path(case_path) / 'foammesh' / 'runs' / run_id / 'runner.log'
        log_path = str(runner) if runner.is_file() else None
    status = RUN_STATUS.get(disposition, disposition
                            or str(document.get('status') or ''))
    if status == 'incomplete' and log_path and active(log_path):
        status = 'running'
    publication = document.get('publication')
    publication_status = (str(publication.get('status') or '')
                          if isinstance(publication, dict) else '')
    if (not publication_status and isinstance(publication, dict)
            and str(document.get('status') or '') == 'published'):
        # DP-2857. A Gmsh publication records the publisher's report, which
        # carries no status word; `record_publication` marks the run itself
        # published. The snappy pipeline writes the word; read both alike.
        publication_status = 'published'
    return {
        'run_id': run_id,
        'engine_id': engine_id,
        'kind': 'run',
        'name': RUN_NAMES.get(engine_id, f'{engine_id} mesh run'),
        'status': status,
        'disposition': disposition,
        'run_status': str(document.get('status') or ''),
        'publication_status': publication_status,
        'job_id': document.get('job_id') or None,
        'log_path': str(log_path) if log_path else None,
        'started_at': document.get('started_at')
        or document.get('created_at', ''),
        'ended_at': document.get('finished_at') or document.get('ended_at', ''),
        'message': str(document.get('error') or ''),
    }


def run_row(case_path, run_id: str) -> dict | None:
    """DP-2856. The listing's row of one mesh run, or ``None``."""
    from foammesh.core.gmsh.manifest import list_runs as gmsh_runs

    for item in gmsh_runs(case_path):
        if item.get('run_id') == run_id and item.get('engine_id'):
            return _from_gmsh_manifest(item, case_path)
    return None


def list_runs(case_path) -> list[dict]:
    """Every run of the case, both engines, oldest first."""
    from foammesh.core.gmsh.manifest import list_runs as gmsh_runs

    rows = [_from_gmsh_manifest(item, case_path)
            for item in gmsh_runs(case_path)
            if item.get('engine_id')]
    rows += list_entries(case_path)
    return sorted(rows, key=lambda row: (str(row.get('started_at') or ''),
                                         str(row.get('run_id') or '')))


#: Programs of the OpenFOAM route's stages.
_SNAPPY_PROGRAMS = ('snappyHexMesh', 'blockMesh', 'surfaceFeatures',
                    'surfaceFeatureConvert')


def record_stage_job(case_path, spec, job) -> Path | None:
    """Write the entry of a finished snappy stage job; ``None`` for any other.

    A stage is a job whose command is one of the OpenFOAM mesh programs.  The
    status is the job's own (``done``, ``failed``, ``cancelled``), so a stage
    the user stopped is listed as stopped.
    """
    argv = ' '.join(str(part) for part in (getattr(spec, 'argv', None) or ()))
    program = next((item for item in _SNAPPY_PROGRAMS if item in argv), '')
    if not program or not getattr(spec, 'mutation', False):
        return None
    status = getattr(getattr(job, 'status', None), 'value',
                     str(getattr(job, 'status', '')))
    status = {'done': 'done', 'cancelled': 'cancelled'}.get(status, 'failed')
    try:
        return write_entry(
            case_path, engine_id='snappy', kind='stage',
            name=str(spec.operation), status=status,
            started_at=str(getattr(job, 'started_at', '') or ''),
            ended_at=str(getattr(job, 'finished_at', '') or ''),
            message=str(getattr(job, 'error', '') or ''),
            run_id=str(getattr(job, 'job_id', '') or spec.operation),
            program=program)
    except OSError:
        return None
