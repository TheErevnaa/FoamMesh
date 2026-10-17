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


def _from_gmsh_manifest(document: dict) -> dict:
    return {
        'run_id': document.get('run_id', ''),
        'engine_id': document.get('engine_id') or 'gmsh',
        'kind': 'run', 'name': 'Gmsh mesh run',
        'status': document.get('disposition') or document.get('status', ''),
        'started_at': document.get('started_at')
        or document.get('created_at', ''),
        'ended_at': document.get('finished_at') or document.get('ended_at', ''),
        'message': '',
    }


def list_runs(case_path) -> list[dict]:
    """Every run of the case, both engines, oldest first."""
    from foammesh.core.gmsh.manifest import list_runs as gmsh_runs

    rows = [_from_gmsh_manifest(item) for item in gmsh_runs(case_path)
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
