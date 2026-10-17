#!/usr/bin/env python3
"""Structured-mesh tooling for any Gmsh runtime (Plan 41 GN8).

Shared by the batch runner and the resident session, like ``entity_controls_v1``.
Input is the list the host derives from the stored rows
(``core/gmsh/structure.py``)::

    {"name": "block", "kind": "transfiniteSurface", "dim": 2, "tags": [3],
     "corners": [1, 2, 5, 6], "enabled": true,
     "params": [["arrangement", 2.0], ["curve_elements", 8.0]]}

Every call reaches Gmsh as written.  Gmsh has no getter for a transfinite
constraint, so the proof that it took is made on the mesh afterwards
(:func:`measure`), never by trusting that a call returned.

Three kinds: ``transfiniteSurface`` (``setTransfiniteSurface`` with the
arrangement and the picked corners), ``transfiniteVolume``
(``setTransfiniteVolume`` with the picked corners and, optionally, the bounding
faces made transfinite first) and ``transfiniteAutomatic``
(``setTransfiniteAutomatic`` with its corner angle and recombine flag).  The
options ``Mesh.FlexibleTransfinite``, ``Mesh.Recombine3D*`` and ``Mesh.Quadqs*``
are catalogue options (Plan 41 GN2), set from the Options dock.
"""
from __future__ import annotations

#: Gmsh 4.15.2's own spellings of the triangle arrangement
#: (``setTransfiniteSurface`` docstring).
ARRANGEMENTS = ('Left', 'Right', 'AlternateLeft', 'AlternateRight')

#: Gmsh element type numbers.
TRIANGLE, QUADRANGLE, TETRAHEDRON, HEXAHEDRON, PRISM, PYRAMID = 2, 3, 4, 5, 6, 7

_WORDS = ('point', 'curve', 'surface', 'volume')


class BuildError(ValueError):
    """Gmsh rejected a constraint, or an entity it names is not in the model."""


def _params(op) -> dict:
    return {n: v for n, v in op.get('params') or ()}


def _words(dim: int) -> str:
    return _WORDS[dim]


def _present(gmsh) -> set:
    return {(int(d), int(t)) for d, t in gmsh.model.getEntities()}


def _is_discrete(gmsh, dim: int, tag: int) -> bool:
    try:
        return 'discrete' in str(gmsh.model.getType(dim, tag)).lower()
    except Exception:                                    # noqa: BLE001
        return False


def boundary_curves(gmsh, dim: int, tag: int) -> list[int]:
    """Every curve on the boundary of one surface or volume, once."""
    if dim == 2:
        found = gmsh.model.getBoundary([(2, tag)], combined=False,
                                       oriented=False)
        return sorted({abs(int(t)) for d, t in found if d == 1})
    faces = gmsh.model.getBoundary([(3, tag)], combined=False, oriented=False)
    curves = set()
    for d, face in faces:
        for dd, curve in gmsh.model.getBoundary([(2, abs(int(face)))],
                                                combined=False,
                                                oriented=False):
            if dd == 1:
                curves.add(abs(int(curve)))
    return sorted(curves)


def boundary_faces(gmsh, tag: int) -> list[int]:
    return sorted({abs(int(t)) for d, t in gmsh.model.getBoundary(
        [(3, tag)], combined=False, oriented=False) if d == 2})


def _check_entities(gmsh, ops) -> None:
    present = _present(gmsh)
    for op in ops:
        if not op.get('enabled', True):
            continue
        dim = int(op['dim'])
        wanted = [(dim, int(t)) for t in op['tags']]
        wanted += [(0, int(t)) for t in op.get('corners') or ()]
        missing = [d for d in wanted if d not in present]
        if missing:
            raise BuildError(
                f'structure {op["name"]!r}: the model has no '
                + ', '.join(f'{_words(d)} {t}' for d, t in missing)
                + ' (the entities come from the geometry as it was imported '
                'or built)')
        if op['kind'] in ('transfiniteSurface', 'transfiniteVolume'):
            discrete = [t for t in op['tags'] if _is_discrete(gmsh, dim, int(t))]
            if discrete:
                raise BuildError(
                    f'structure {op["name"]!r}: a transfinite '
                    f'{_words(dim)} interpolates between the corner points of '
                    'a CAD entity, and '
                    + ', '.join(f'{_words(dim)} {t}' for t in discrete)
                    + (' is' if len(discrete) == 1 else ' are')
                    + ' a discrete (tessellated) entity with no corners; '
                    're-import the geometry as STEP/IGES/BREP, or leave it '
                    'unstructured')


def _set_curve_elements(gmsh, curves, elements: int, done: set) -> int:
    """``setTransfiniteCurve`` on the curves that have none from this call.

    ``setTransfiniteCurve`` counts nodes; the control counts elements.
    """
    count = 0
    for curve in curves:
        if curve in done:
            continue
        gmsh.model.mesh.setTransfiniteCurve(int(curve), int(elements) + 1)
        done.add(curve)
        count += 1
    return count


def apply(gmsh, ops, *, skip_curves=()) -> dict:
    """Set every enabled constraint.  Returns ``applied``: one entry per
    call, ``(name, option, requested, effective)``, and ``count``.

    *skip_curves* are curve tags that already carry an explicit transfinite
    constraint elsewhere; ``curve_elements`` never overwrites them.
    """
    ops = [dict(op) for op in ops or ()]
    _check_entities(gmsh, ops)
    mesh = gmsh.model.mesh
    applied: list = []
    done = {int(c) for c in skip_curves}
    for op in ops:
        if not op.get('enabled', True):
            continue
        name, kind, dim = op['name'], op['kind'], int(op['dim'])
        tags = [int(t) for t in op['tags']]
        corners = [int(t) for t in op.get('corners') or ()]
        params = _params(op)
        try:
            if kind == 'transfiniteSurface':
                arrangement = ARRANGEMENTS[int(params['arrangement'])]
                elements = int(params.get('curve_elements', 0))
                for tag in tags:
                    if elements:
                        _set_curve_elements(
                            gmsh, boundary_curves(gmsh, 2, tag), elements,
                            done)
                    mesh.setTransfiniteSurface(tag, arrangement, corners)
                    if int(params.get('recombine', 0)):
                        mesh.setRecombine(2, tag)
                applied.append((
                    name, 'transfiniteSurface',
                    f'surface {tags} {arrangement}'
                    + (f' corners {corners}' if corners else ''),
                    'set (Gmsh has no getter; proved on the mesh)'))
            elif kind == 'transfiniteVolume':
                elements = int(params.get('curve_elements', 0))
                for tag in tags:
                    faces = boundary_faces(gmsh, tag)
                    if elements:
                        _set_curve_elements(
                            gmsh, boundary_curves(gmsh, 3, tag), elements,
                            done)
                    if int(params.get('faces', 1)):
                        # MEASURED (Plan 30 WP12): setTransfiniteVolume alone
                        # is ineffective; Gmsh builds the block from
                        # transfinite faces.
                        for face in faces:
                            mesh.setTransfiniteSurface(face)
                    if int(params.get('recombine', 1)):
                        for face in faces:
                            mesh.setRecombine(2, face)
                    mesh.setTransfiniteVolume(tag, corners)
                applied.append((
                    name, 'transfiniteVolume',
                    f'volume {tags}'
                    + (f' corners {corners}' if corners else ''),
                    'set (Gmsh has no getter; proved on the mesh)'))
            elif kind == 'transfiniteAutomatic':
                angle = float(params['corner_angle'])
                recombine = bool(int(params['recombine']))
                dimtags = [(dim, t) for t in tags]
                mesh.setTransfiniteAutomatic(dimtags, angle, recombine)
                applied.append((
                    name, 'transfiniteAutomatic',
                    (f'{_words(dim)} {tags}' if tags else 'every entity')
                    + f' corner angle {angle:g} rad, recombine {recombine}',
                    'set (Gmsh has no getter; proved on the mesh)'))
            else:
                raise BuildError(f'structure {name!r}: unknown kind {kind!r}')
        except BuildError:
            raise
        except Exception as error:                          # noqa: BLE001
            raise BuildError(
                f'structure {name!r} ({kind}): Gmsh refused: {error}'
            ) from error
    return {'applied': applied,
            'count': len([o for o in ops if o.get('enabled', True)])}


# -- measuring what Gmsh made -------------------------------------------------- #

def family_counts(gmsh, dim: int, tag: int | None = None) -> dict:
    """Elements by family, over one entity or over the whole model."""
    entities = ([(dim, tag)] if tag is not None
                else [(d, t) for d, t in gmsh.model.getEntities(dim)])
    counts: dict = {}
    for d, t in entities:
        types, ids, _nodes = gmsh.model.mesh.getElements(int(d), int(t))
        for etype, items in zip(types, ids):
            counts[int(etype)] = counts.get(int(etype), 0) + len(items)
    return {'triangles': counts.get(TRIANGLE, 0),
            'quadrangles': counts.get(QUADRANGLE, 0),
            'tetrahedra': counts.get(TETRAHEDRON, 0),
            'hexahedra': counts.get(HEXAHEDRON, 0),
            'prisms': counts.get(PRISM, 0),
            'pyramids': counts.get(PYRAMID, 0)}


def _diagonals(gmsh, tag: int):
    """``[(midpoint, sign)]`` of every triangle edge that is a diagonal of
    the grid, in the plane of the two axes the surface spreads over most."""
    types, _ids, nodes = gmsh.model.mesh.getElements(2, tag)
    coords: dict = {}
    node_tags, xyz, _ = gmsh.model.mesh.getNodes(2, tag, True, False)
    for i, t in enumerate(node_tags):
        coords[int(t)] = (xyz[3 * i], xyz[3 * i + 1], xyz[3 * i + 2])
    spread = [max(c[a] for c in coords.values())
              - min(c[a] for c in coords.values()) for a in range(3)]
    axes = sorted(range(3), key=lambda a: -spread[a])[:2]
    found = {}
    for etype, conn in zip(types, nodes):
        if int(etype) != TRIANGLE:
            continue
        for k in range(0, len(conn), 3):
            tri = [coords[int(n)] for n in conn[k:k + 3]]
            for a in range(3):
                p, q = tri[a], tri[(a + 1) % 3]
                du, dv = q[axes[0]] - p[axes[0]], q[axes[1]] - p[axes[1]]
                if abs(du) > 1e-9 and abs(dv) > 1e-9:
                    mid = (round((p[axes[0]] + q[axes[0]]) / 2, 6),
                           round((p[axes[1]] + q[axes[1]]) / 2, 6))
                    found[mid] = 1 if du * dv > 0 else -1
    return found


def diagonal_signs(gmsh, tag: int) -> dict:
    """How the triangles of a structured surface are cut: counts of
    diagonals leaning ``+`` and ``-``.  Left and Right give one sign each,
    Alternate gives both."""
    found = _diagonals(gmsh, tag)
    return {'plus': sum(1 for v in found.values() if v > 0),
            'minus': sum(1 for v in found.values() if v < 0)}


def diagonal_map(gmsh, tag: int) -> dict:
    """The sign of each cell's diagonal, keyed by the cell's centre as
    ``"u,v"``: it tells AlternateLeft from AlternateRight."""
    return {f'{u:g},{v:g}': sign
            for (u, v), sign in sorted(_diagonals(gmsh, tag).items())}


def _curve_elements(gmsh, curve: int) -> int:
    types, ids, _nodes = gmsh.model.mesh.getElements(1, int(curve))
    return sum(len(x) for x in ids)


def measure(gmsh, ops) -> list:
    """What Gmsh made of each enabled constraint, read off the mesh.

    One entry per entity: ``{name, kind, dim, tag, families, requested,
    structured, note}``.  ``structured`` is ``True`` when the mesh is what the
    row asked for, ``False`` when it is not (Gmsh accepts a transfinite
    constraint it cannot honour and meshes unstructured without raising), and
    ``None`` when the mesh holds nothing for the entity yet.

    * a transfinite surface is structured when it holds exactly
      ``a*b`` quadrangles (recombined) or ``2*a*b`` triangles, ``a`` and ``b``
      being the element counts of two adjacent sides;
    * a transfinite volume when every cell is a hexahedron (recombined) and
      there are as many as the three directions of the block give;
    * automatic transfinite is read per volume: a volume that came out in
      hexahedra took it.
    """
    found: list = []
    for op in ops or ():
        if not op.get('enabled', True):
            continue
        params = {n: v for n, v in op.get('params') or ()}
        name, kind = op['name'], op['kind']
        if kind == 'transfiniteSurface':
            for tag in op['tags']:
                fam = family_counts(gmsh, 2, int(tag))
                total = fam['triangles'] + fam['quadrangles']
                sides = [_curve_elements(gmsh, c) for c in
                         boundary_curves(gmsh, 2, int(tag))]
                expected = None
                structured = None
                distinct = sorted(set(sides))
                if len(sides) == 4 and len(distinct) == 1:
                    expected = distinct[0] ** 2
                elif len(sides) == 4 and len(distinct) == 2:
                    expected = distinct[0] * distinct[1]
                if total and expected:
                    structured = (fam['quadrangles'] == expected
                                  if int(params.get('recombine', 0))
                                  else (fam['triangles'] == 2 * expected
                                        or fam['quadrangles'] == expected))
                found.append({
                    'name': name, 'kind': kind, 'dim': 2, 'tag': int(tag),
                    'families': fam, 'requested': expected,
                    'structured': structured,
                    'note': 'cells counted against the element counts of the '
                            'sides' if expected else
                            'the sides do not give a count to compare with'})
        elif kind == 'transfiniteVolume':
            for tag in op['tags']:
                fam = family_counts(gmsh, 3, int(tag))
                total = sum(fam.values())
                structured = None
                if total:
                    structured = (fam['hexahedra'] == total
                                  if int(params.get('recombine', 1))
                                  else fam['hexahedra'] + fam['prisms'] == 0
                                  or fam['hexahedra'] == total)
                found.append({
                    'name': name, 'kind': kind, 'dim': 3, 'tag': int(tag),
                    'families': fam, 'requested': None,
                    'structured': structured,
                    'note': 'every cell a hexahedron' if int(
                        params.get('recombine', 1)) else 'structured tetrahedra'})
        elif kind == 'transfiniteAutomatic':
            dims = [int(op['dim'])] if op['tags'] else [3]
            tags = [int(t) for t in op['tags']] or [
                int(t) for _d, t in gmsh.model.getEntities(3)]
            for dim in dims:
                for tag in tags:
                    fam = family_counts(gmsh, dim, tag)
                    total = sum(fam.values())
                    wanted = 'hexahedra' if dim == 3 else 'quadrangles'
                    took = (fam[wanted] == total) if total else None
                    found.append({
                        'name': name, 'kind': kind, 'dim': dim, 'tag': tag,
                        'families': fam, 'requested': None,
                        'structured': took if int(params['recombine'])
                        else None,
                        'note': ('Gmsh structures volume by volume; this one '
                                 + ('took it' if took else 'did not'))
                        if int(params['recombine']) else
                        'recombine is off: structured cells are '
                        'tetrahedra or triangles, compare node counts'})
    return found


# -- the block grid, before meshing ------------------------------------------- #

def _ordered_curve_points(gmsh, curve: int) -> list:
    """The nodes of one meshed curve, from its first end point to its last."""
    tags, xyz, param = gmsh.model.mesh.getNodes(1, curve, True, True)
    order = sorted(range(len(tags)), key=lambda i: param[i])
    points = [(xyz[3 * i], xyz[3 * i + 1], xyz[3 * i + 2]) for i in order]
    # include_boundary returns the end points but not always first and last
    return points


def _chain(gmsh, surface: int):
    """The four sides of a surface as ``[(curve, forward)]`` in loop order,
    or ``None`` when the surface does not have exactly four."""
    sides = [abs(int(t)) for d, t in gmsh.model.getBoundary(
        [(2, surface)], combined=True, oriented=False) if d == 1]
    if len(sides) != 4:
        return None
    ends = {}
    for curve in sides:
        # the oriented, uncombined boundary of a curve is (start, end)
        raw = [abs(int(t)) for d, t in gmsh.model.getBoundary(
            [(1, curve)], combined=False, oriented=True)]
        if len(raw) != 2:
            return None
        ends[curve] = (raw[0], raw[1])
    order = [sides[0]]
    forward = [True]
    here = ends[sides[0]][1]
    remaining = set(sides[1:])
    while remaining:
        for curve in sorted(remaining):
            a, b = ends[curve]
            if a == here:
                order.append(curve), forward.append(True), remaining.discard(curve)
                here = b
                break
            if b == here:
                order.append(curve), forward.append(False), remaining.discard(curve)
                here = a
                break
        else:
            return None
    return list(zip(order, forward))


def coons_grid(bottom, right, top, left) -> list:
    """Linear Coons patch through the four meshed sides; the interior grid
    points as ``grid[i][j]`` with ``i`` along bottom/top and ``j`` along
    left/right.  Each side is a list of points from its own first corner."""
    nu, nv = len(bottom), len(left)
    grid = []
    p00, p10, p01, p11 = bottom[0], bottom[-1], top[0], top[-1]
    for i in range(nu):
        s = i / (nu - 1) if nu > 1 else 0.0
        row = []
        for j in range(nv):
            t = j / (nv - 1) if nv > 1 else 0.0
            point = []
            for a in range(3):
                value = ((1 - t) * bottom[i][a] + t * top[i][a]
                         + (1 - s) * left[j][a] + s * right[j][a]
                         - ((1 - s) * (1 - t) * p00[a] + s * (1 - t) * p10[a]
                            + (1 - s) * t * p01[a] + s * t * p11[a]))
                point.append(value)
            row.append(tuple(point))
        grid.append(row)
    return grid


def block_grid(gmsh, surfaces) -> dict:
    """The grid lines of the transfinite surfaces, from the 1D mesh.

    Meshes the curves (the only part of the mesh the grid depends on), reads
    the nodes of the four sides of each surface and interpolates the interior
    with a linear Coons patch.  The mesh is cleared again.  Returns
    ``{'surfaces': [{'tag', 'nu', 'nv', 'lines': [[[x, y, z], ...], ...]}],
    'skipped': [{'tag', 'reason'}]}``.
    """
    gmsh.model.mesh.clear()
    gmsh.model.mesh.generate(1)
    shown, skipped = [], []
    try:
        for tag in surfaces:
            tag = int(tag)
            chain = _chain(gmsh, tag)
            if chain is None:
                skipped.append({'tag': tag, 'reason': (
                    'the surface does not have four sides, so there is no '
                    'block grid to draw')})
                continue
            sides = []
            for curve, forward in chain:
                pts = _ordered_curve_points(gmsh, curve)
                sides.append(pts if forward else pts[::-1])
            bottom, right, top, left = sides
            top, left = top[::-1], left[::-1]
            if len(bottom) != len(top) or len(left) != len(right):
                skipped.append({'tag': tag, 'reason': (
                    f'opposite sides carry {len(bottom)} and {len(top)} '
                    f'nodes, {len(left)} and {len(right)} nodes: a '
                    'transfinite surface needs equal counts on opposite '
                    'sides (or Mesh.QuasiTransfinite)')})
                continue
            grid = coons_grid(bottom, right, top, left)
            lines = []
            for row in grid:
                lines.append([list(p) for p in row])
            for j in range(len(grid[0])):
                lines.append([list(grid[i][j]) for i in range(len(grid))])
            shown.append({'tag': tag, 'nu': len(bottom), 'nv': len(left),
                          'lines': lines})
    finally:
        gmsh.model.mesh.clear()
    return {'surfaces': shown, 'skipped': skipped}
