"""Size at points on a Gmsh model (Plan 41 GN7, M-01).

Shared by the batch runner and the resident session, like ``native_fields_v1``.
``gmsh.model.mesh.setSize`` sets the size at a point entity; Gmsh uses it only
while ``Mesh.MeshSizeFromPoints`` is on, and that option is a global one the
user may have turned off for the CAD's own point sizes.  A row here is an
explicit request for a size at a picked point, so applying rows turns the
option on and says so (the caller records the requested and read-back values).

Each row is ``{name, tag, size, enabled, x?, y?, z?}``.  The point is found by
tag; when the tag is absent or its position differs from the picked one (the
model was renumbered), by position.
"""
from __future__ import annotations

import math

#: What ``getSizes`` reads back for a point that was never given a size.
UNSET = 0.0
#: Gmsh's "no size here" (MAX_LC). Setting 0 instead makes the 1D mesher fail
#: with "Wrong mesh element size lc = 0" (MEASURED, Gmsh 4.15.2).
MAX_LC = 1e22


class PointSizeError(RuntimeError):
    """The point sizes could not be applied."""


def _points(gmsh) -> dict:
    out = {}
    for _dim, tag in gmsh.model.getEntities(0):
        out[int(tag)] = tuple(gmsh.model.getValue(0, int(tag), []))
    return out


def _diagonal(gmsh) -> float:
    box = gmsh.model.getBoundingBox(-1, -1)
    return math.dist(box[:3], box[3:]) or 1.0


def resolve(gmsh, row, points=None, tolerance=None):
    """The point tag a row means, or ``(None, reason)``."""
    points = points if points is not None else _points(gmsh)
    tolerance = tolerance if tolerance is not None else _diagonal(gmsh) * 1e-6
    tag = int(row['tag'])
    wanted = None
    if all(row.get(k) is not None for k in ('x', 'y', 'z')):
        wanted = (float(row['x']), float(row['y']), float(row['z']))
    if tag in points:
        if wanted is None or math.dist(points[tag], wanted) <= tolerance:
            return tag, ''
    if wanted is not None:
        near = min(points.items(), key=lambda kv: math.dist(kv[1], wanted),
                   default=None)
        if near is not None and math.dist(near[1], wanted) <= tolerance:
            return near[0], f'found by position (tag {near[0]}, not {tag})'
        return None, (f'no point at ({wanted[0]:g}, {wanted[1]:g}, '
                      f'{wanted[2]:g}) in the model')
    return None, f'the model has no point {tag}'


def fill_size(gmsh):
    """The size a point without one takes: the global ``Mesh.MeshSizeMax``."""
    value = gmsh.option.getNumber('Mesh.MeshSizeMax')
    return value if 0.0 < value < MAX_LC / 1e3 else None


def apply(gmsh, rows, previous=(), fill=True) -> dict:
    """Clear *previous* sizes, then set one size per enabled row.

    MEASURED (Gmsh 4.15.2, unit cube, 0.05 at one corner): Gmsh interpolates
    along a curve between the sizes of its two end points, and a point that
    carries none counts as MAX_LC there *before* the global maximum clamps it,
    so the sized corner came out coarser (first node 0.374 from it) than the
    unsized mesh. With ``fill`` every other point that carries no size takes
    the global maximum size, so the picked size is what the mesh shows at that
    point; those points are returned in ``filled``.

    Returns ``{'applied': [{name, tag, requested, effective, note}],
    'skipped': [{name, reason}], 'tags': [tag, ...]}``; ``effective`` is what
    ``getSizes`` reads back.  Nothing is set for a row that does not resolve.
    """
    for tag in previous:
        try:
            gmsh.model.mesh.setSize([(0, int(tag))], MAX_LC)
        except Exception:                                # noqa: BLE001
            pass
    points = _points(gmsh)
    tolerance = _diagonal(gmsh) * 1e-6
    applied, skipped, used = [], [], []
    for row in rows or ():
        if not row.get('enabled', True):
            continue
        tag, note = resolve(gmsh, row, points, tolerance)
        if tag is None:
            skipped.append({'name': row['name'], 'reason': note})
            continue
        size = float(row['size'])
        gmsh.model.mesh.setSize([(0, tag)], size)
        effective = float(gmsh.model.mesh.getSizes([(0, tag)])[0])
        used.append(tag)
        applied.append({'name': row['name'], 'tag': tag, 'requested': size,
                        'effective': effective, 'note': note})
    filled = []
    size = fill_size(gmsh) if fill and applied else None
    if size is not None:
        for tag in points:
            if tag in used:
                continue
            current = float(gmsh.model.mesh.getSizes([(0, tag)])[0])
            if current == UNSET or current >= MAX_LC / 1e3:
                gmsh.model.mesh.setSize([(0, tag)], size)
                filled.append(tag)
    return {'applied': applied, 'skipped': skipped,
            'tags': used + filled, 'filled': filled,
            'fillSize': size if filled else None}


def switch_on_point_sizes(gmsh) -> tuple:
    """Turn ``Mesh.MeshSizeFromPoints`` on; ``(was, now)``."""
    was = gmsh.option.getNumber('Mesh.MeshSizeFromPoints')
    gmsh.option.setNumber('Mesh.MeshSizeFromPoints', 1.0)
    return was, gmsh.option.getNumber('Mesh.MeshSizeFromPoints')
