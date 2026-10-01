"""Which mesh every export was written from (Plan 37 UF5).

An export is a copy of the mesh at one moment. Once an unlock discards that
mesh the export still exists but describes a result the case no longer
holds. This record lets the unlock preview say so, and lets the unlock mark
it -- without touching the exported files. Nothing here deletes or rewrites
an export: an export outside the case folder is the user's, and one inside it
is listed as stale for the user to decide about.

``foammesh/exports/provenance.json``::

    {"schema_version": 1,
     "exports": [{"destination", "operation", "case_managed", "mesh_identity",
                  "exported_at", "stale", "stale_reason", "staled_at"}]}

``case_managed`` is true when the destination lies inside the case folder.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PROVENANCE = ('foammesh', 'exports', 'provenance.json')
LIMIT = 500


def provenance_path(case_path) -> Path:
    return Path(case_path).joinpath(*PROVENANCE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _load(case_path) -> dict:
    try:
        document = json.loads(provenance_path(case_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        document = None
    if not isinstance(document, dict) or not isinstance(document.get('exports'), list):
        document = {'schema_version': SCHEMA_VERSION, 'exports': []}
    return document


def _save(case_path, document: dict) -> None:
    path = provenance_path(case_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def record(case_path, destination, *, operation: str) -> dict:
    """Note that *destination* was just written from the live mesh."""
    from foammesh.core.workflow.task_state_store import mesh_identity

    destination = Path(destination)
    entry = {'destination': str(destination), 'operation': operation,
             'case_managed': _inside(destination, Path(case_path)),
             'mesh_identity': mesh_identity(case_path),
             'exported_at': _now(), 'stale': False}
    document = _load(case_path)
    exports = [item for item in document['exports']
               if item.get('destination') != entry['destination']]
    exports.append(entry)
    document['exports'] = exports[-LIMIT:]
    _save(case_path, document)
    return entry


def exports(case_path) -> list[dict]:
    return list(_load(case_path)['exports'])


def of_live_mesh(case_path) -> list[dict]:
    """The exports still describing the mesh in ``constant/polyMesh``."""
    from foammesh.core.workflow.task_state_store import mesh_identity

    live = mesh_identity(case_path)
    return [item for item in exports(case_path)
            if not item.get('stale') and live
            and item.get('mesh_identity') == live]


def mark_stale(case_path, *, reason: str, operation_id: str = '') -> list[dict]:
    """Mark the live mesh's exports stale; the files are not touched."""
    from foammesh.core.workflow.task_state_store import mesh_identity

    live = mesh_identity(case_path)
    if not live:
        return []
    document = _load(case_path)
    changed = []
    for item in document['exports']:
        if not item.get('stale') and item.get('mesh_identity') == live:
            item.update(stale=True, stale_reason=reason, staled_at=_now(),
                        stale_operation=operation_id)
            changed.append(dict(item))
    if changed:
        _save(case_path, document)
    return changed


def unmark(case_path, operation_id: str) -> list[dict]:
    """Undo of an unlock: its exports describe the live mesh again."""
    if not operation_id:
        return []
    document = _load(case_path)
    changed = []
    for item in document['exports']:
        if item.get('stale') and item.get('stale_operation') == operation_id:
            for key in ('stale_reason', 'staled_at', 'stale_operation'):
                item.pop(key, None)
            item['stale'] = False
            changed.append(dict(item))
    if changed:
        _save(case_path, document)
    return changed
