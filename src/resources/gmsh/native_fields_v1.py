#!/usr/bin/env python3
"""Build the size fields the user made, in any Gmsh runtime (Plan 41 GN6).

Shared by the batch runner and the resident session, so a field is built the
same way whichever of them meshes.  Input is the list of nodes the host
derives from the stored rows (``core/gmsh/field_catalogue.NativeFields``)::

    {"name": "wall", "type": "Distance", "enabled": true, "background": false,
     "numbers": [["Sampling", 40.0]], "strings": [], "lists": [["SurfacesList",
     [1, 2]]], "refs": [], "refLists": []}

A reference is the *name* of another node.  Nothing here knows what a
Threshold is: every option reaches Gmsh as written and is read back, so the
ledger can show requested against effective.

The preview samples a field where Gmsh itself asks for it.  Gmsh has no
function that evaluates a field at a point, but it calls the mesh-size
callback at every point it needs a size, with the size it has worked out from
the background field; a callback that records those and returns them
unchanged reads the field exactly as the mesher does.
"""
from __future__ import annotations

import random

#: Combiners for the background, by the family of the fields combined.
COMBINERS = {'min': 'Min', 'max': 'Max'}
#: A size above this is Gmsh's "no value" (1e22).
NO_VALUE = 1.0e21


class BuildError(ValueError):
    """Gmsh rejected a field, or the nodes cannot be built."""


def build_order(nodes) -> list:
    """Enabled nodes, each after every node it reads."""
    live = {n['name']: n for n in nodes if n.get('enabled', True)}
    needs = {}
    for name, node in live.items():
        deps = {t for _o, t in node.get('refs') or ()}
        for _o, targets in node.get('refLists') or ():
            deps.update(targets)
        missing = sorted(d for d in deps if d not in live)
        if missing:
            raise BuildError(
                f'size field {name!r} reads {", ".join(missing)}, which is '
                'not an enabled field')
        needs[name] = deps
    order = []
    ready = sorted(n for n, deps in needs.items() if not deps)
    while ready:
        name = ready.pop(0)
        order.append(name)
        needs.pop(name)
        freed = []
        for other, deps in needs.items():
            if name in deps:
                deps.discard(name)
                if not deps:
                    freed.append(other)
        ready = sorted(ready + freed)
    if needs:
        raise BuildError('these size fields depend on each other in a cycle: '
                         + ', '.join(sorted(needs)))
    return [live[name] for name in order]


def build(gmsh, nodes) -> dict:
    """Create the fields.  Returns tags, the background tags and a ledger.

    ``applied`` has one entry per option set: ``(field, option, requested,
    effective)`` where *effective* is what Gmsh reads back.
    """
    field = gmsh.model.mesh.field
    tags: dict = {}
    applied: list = []
    for node in build_order(nodes):
        name = node['name']
        try:
            tag = field.add(node['type'])
        except Exception as error:                    # noqa: BLE001
            raise BuildError(
                f'size field {name!r}: Gmsh has no field type '
                f'{node["type"]!r} ({_reason(error)})') from error
        tags[name] = tag
        try:
            for option, value in node.get('numbers') or ():
                field.setNumber(tag, option, float(value))
                applied.append((name, option, float(value),
                                field.getNumber(tag, option)))
            for option, value in node.get('strings') or ():
                field.setString(tag, option, str(value))
                applied.append((name, option, str(value),
                                field.getString(tag, option)))
            for option, values in node.get('lists') or ():
                field.setNumbers(tag, option, [float(v) for v in values])
                applied.append((name, option, [int(v) for v in values],
                                [int(v) for v in field.getNumbers(
                                    tag, option)]))
            for option, target in node.get('refs') or ():
                field.setNumber(tag, option, float(tags[target]))
                applied.append((name, option, target,
                                int(field.getNumber(tag, option))))
            for option, targets in node.get('refLists') or ():
                field.setNumbers(tag, option,
                                 [float(tags[t]) for t in targets])
                applied.append((name, option, list(targets),
                                [int(v) for v in field.getNumbers(
                                    tag, option)]))
        except Exception as error:                    # noqa: BLE001
            raise BuildError(
                f'size field {name!r} ({node["type"]}) was rejected by '
                f'Gmsh: {_reason(error)}') from error
    background = [tags[n['name']] for n in nodes
                  if n.get('enabled', True) and n.get('background')
                  and n['type'] != 'BoundaryLayer']
    layer = [tags[n['name']] for n in nodes
             if n.get('enabled', True) and n.get('background')
             and n['type'] == 'BoundaryLayer']
    return {'tags': tags, 'background': background, 'layer': layer,
            'applied': applied,
            'anisotropic': [tags[n['name']] for n in nodes
                            if n.get('enabled', True)
                            and n.get('background')
                            and n['type'] in ANISOTROPIC]}


ANISOTROPIC = frozenset({'AttractorAnisoCurve', 'MathEvalAniso', 'MinAniso',
                         'IntersectAniso'})


def set_background(gmsh, tags, *, combiner: str = 'min',
                   anisotropic: bool = False) -> int | None:
    """Make the given fields the one background mesh.  Returns its tag."""
    if not tags:
        return None
    field = gmsh.model.mesh.field
    if len(tags) == 1 and not anisotropic:
        chosen = tags[0]
    elif len(tags) == 1:
        chosen = tags[0]
    else:
        kind = 'MinAniso' if anisotropic else COMBINERS.get(
            str(combiner).lower(), 'Min')
        chosen = field.add(kind)
        field.setNumbers(chosen, 'FieldsList', [float(t) for t in tags])
    field.setAsBackgroundMesh(chosen)
    return chosen


def remove(gmsh, tags) -> None:
    field = gmsh.model.mesh.field
    for tag in tags:
        try:
            field.remove(tag)
        except Exception:                             # noqa: BLE001
            pass


def preview(gmsh, *, dim: int = 2, limit: int = 20000, seed: int = 1) -> dict:
    """Sample the background field where Gmsh asks for a size.

    The model must have no mesh (the preview meshes curves, and surfaces for
    ``dim`` 2, with the field alone deciding the size).  Returns
    ``{"points": [[x, y, z], ...], "sizes": [...], "asked": n, "error": ""}``.
    The mesh is cleared again, and every option touched is put back.
    """
    option = gmsh.option
    names = ('Mesh.MeshSizeFromPoints', 'Mesh.MeshSizeExtendFromBoundary',
             'Mesh.MeshSizeFromCurvature', 'Mesh.MeshSizeMin',
             'Mesh.MeshSizeMax', 'Mesh.MeshSizeFactor')
    saved = {name: option.getNumber(name) for name in names}
    rng = random.Random(seed)
    points: list = []
    sizes: list = []
    asked = [0]

    def record(_dim, _tag, x, y, z, size):
        asked[0] += 1
        if size < NO_VALUE:
            if len(points) < limit:
                points.append((x, y, z))
                sizes.append(size)
            else:
                slot = rng.randrange(asked[0])
                if slot < limit:
                    points[slot] = (x, y, z)
                    sizes[slot] = size
        return size

    error = ''
    try:
        option.setNumber('Mesh.MeshSizeFromPoints', 0)
        option.setNumber('Mesh.MeshSizeExtendFromBoundary', 0)
        option.setNumber('Mesh.MeshSizeFromCurvature', 0)
        option.setNumber('Mesh.MeshSizeMin', 0)
        option.setNumber('Mesh.MeshSizeMax', 1.0e22)
        option.setNumber('Mesh.MeshSizeFactor', 1)
        gmsh.model.mesh.setSizeCallback(record)
        try:
            gmsh.model.mesh.generate(max(1, min(int(dim), 2)))
        except Exception as caught:                   # noqa: BLE001
            error = _reason(caught)
    finally:
        try:
            gmsh.model.mesh.removeSizeCallback()
        except Exception:                             # noqa: BLE001
            pass
        for name, value in saved.items():
            option.setNumber(name, value)
        try:
            gmsh.model.mesh.clear()
        except Exception:                             # noqa: BLE001
            pass
    return {'points': [list(p) for p in points], 'sizes': list(sizes),
            'asked': asked[0], 'error': error}


def mesh_exists(gmsh) -> bool:
    nodes, _coords, _par = gmsh.model.mesh.getNodes()
    return len(nodes) > 0


def _reason(error) -> str:
    text = str(error).strip().splitlines()
    return text[-1][:200] if text else error.__class__.__name__
