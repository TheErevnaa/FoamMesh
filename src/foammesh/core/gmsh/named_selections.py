"""Named selections of a Gmsh mesh: entities and elements (Plan 41 GN23, FM-04).

A selection is a name plus either Gmsh entities (``[dim, tag]`` pairs) or
element tags. It is kept with the session's case under
``<case>/foammesh/gmsh/selections.json`` and survives the mesh being
regenerated: an element selection records the node count of the mesh it was
made on, and says so when the mesh now on screen has fewer elements.

No Qt, no Gmsh. Names are unique, trimmed and non-empty; saving over a name
replaces it (the caller asks first).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

__all__ = ['SelectionError', 'path', 'load', 'names', 'save', 'delete',
           'get', 'ENTITY', 'ELEMENT', 'GMSH', 'OPENFOAM', 'origin_notice',
           'source_of']

ENTITY = 'entity'
ELEMENT = 'element'
#: Plan 42 SG14 (D8). The kind of mesh a selection was made on.
GMSH = 'gmsh'
OPENFOAM = 'openfoam'


class SelectionError(ValueError):
    """The selection cannot be saved or found; the message says why."""


def path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / 'selections.json'


def load(case_path) -> dict:
    """``{name: record}``; a missing or unreadable file is no selections."""
    try:
        document = json.loads(path(case_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    items = document.get('selections') if isinstance(document, dict) else None
    return {str(k): v for k, v in (items or {}).items()
            if isinstance(v, dict) and v.get('kind') in (ENTITY, ELEMENT)}


def names(case_path) -> list[str]:
    return sorted(load(case_path))


def get(case_path, name: str) -> dict:
    found = load(case_path).get(str(name or '').strip())
    if found is None:
        raise SelectionError(f'there is no selection named "{name}"')
    return found


def _write(case_path, items: dict) -> None:
    target = path(case_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix('.json.tmp')
    scratch.write_text(json.dumps({'schema_version': 1, 'selections': items},
                                  indent=2), encoding='utf-8')
    os.replace(scratch, target)


def save(case_path, name: str, *, entities=None, elements=None,
         mesh: dict | None = None) -> dict:
    """Keep a selection of *entities* (``[(dim, tag), ...]``) or *elements*
    (tags) under *name*; returns the record."""
    name = str(name or '').strip()
    if not name:
        raise SelectionError('a selection needs a name')
    if entities and elements:
        raise SelectionError('save entities or elements, not both')
    if not entities and not elements:
        raise SelectionError('nothing is selected')
    if entities:
        pairs = sorted({(int(d), int(t)) for d, t in entities})
        if any(d not in (0, 1, 2, 3) or t < 1 for d, t in pairs):
            raise SelectionError('an entity is [dimension 0-3, tag >= 1]')
        record = {'kind': ENTITY, 'entities': [list(p) for p in pairs]}
    else:
        tags = sorted({int(t) for t in elements})
        if tags[0] < 1:
            raise SelectionError('an element tag is 1 or more')
        record = {'kind': ELEMENT, 'elements': tags}
    record['mesh'] = dict(mesh or {})
    record['saved_at'] = datetime.now(timezone.utc).isoformat()
    items = load(case_path)
    items[name] = record
    _write(case_path, items)
    return record


def delete(case_path, name: str) -> bool:
    items = load(case_path)
    if str(name or '').strip() not in items:
        return False
    del items[str(name).strip()]
    _write(case_path, items)
    return True


def source_of(record: dict) -> str:
    """The kind of mesh a record was made on. A record from before Plan 42
    carries a digest and no source: it was made on a Gmsh mesh."""
    mesh = (record or {}).get('mesh') or {}
    return str(mesh.get('source') or GMSH)


def origin_notice(record: dict, current: dict | None) -> str:
    """Plan 42 SG14 (D8). What to tell the user when a saved element
    selection is opened on a mesh other than the one it was made on ('' when
    it is the same mesh).

    An element number means the Gmsh element tag on a Gmsh mesh and the cell
    position on an OpenFOAM mesh, so a selection carried across is applied by
    number and says so.
    """
    if record.get('kind') != ELEMENT:
        return ''
    current = current or {}
    made, here = source_of(record), str(current.get('source') or '')
    if here and made != here:
        names = {GMSH: 'a Gmsh mesh', OPENFOAM: 'an OpenFOAM mesh'}
        numbers = {GMSH: 'Gmsh element tags', OPENFOAM: 'cell numbers'}
        return (f'Made on {names.get(made, made)}; its numbers are '
                f'{numbers.get(made, made)} and are read here as '
                f'{numbers.get(here, here)}.')
    saved = str((record.get('mesh') or {}).get('digest') or '')
    now = str(current.get('digest') or '')
    if saved and now and saved != now:
        return 'Made on an earlier mesh of this case.'
    return ''
