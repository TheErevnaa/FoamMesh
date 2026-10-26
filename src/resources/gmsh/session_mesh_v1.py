"""Session operations for the mesh area (Plan 48 seam, owner GM7).

A mixin of ``session_v1.Session``. ``dispatch()`` finds a public method by
name (``session.<name>``), so a method added here is reachable as the
operation ``session.<name>`` with no other change. The mixin must not import
``session_v1``: it is mixed into ``Session``, and uses ``self.gmsh`` (the
recording wrapper) and ``self.raw`` (the bare module) from it.

Plan 48 GM7 adds the mesh actions the Mesh module has beyond Generate:

* ``regularize``: the High-order tools window's second step (optimise,
  elastic analogy, fast curving) with the parameters Gmsh 4.15.2's API can
  carry.
* ``remove_elements`` / ``delete_entity_mesh``: Mesh > Delete, by element
  tags and by entities. Reverse is Gmsh's own ``mesh.reverseElements`` and
  ``mesh.reverse``, called straight from the planner; the module functions
  ``reverse_elements`` / ``reverse_entities`` are for batch jobs.
* ``reclassify_subset``: Mesh > Reclassify 2D. With no subset and no edge
  taken out it is ``classifySurfaces``; with a subset, a threshold angle and
  deletable edges the patches are built here. ``reclassify_edges`` is the
  preview.
* ``quad_layout``: Mesh > Experimental > Compute quad layout, run in a child
  process because Gmsh 4.15.2 aborts or segfaults inside it.
* ``list_mesh_elements`` / ``get_mesh_order``: reads that feed the pickers.

The functions are module level so ``runner_parts/mesh.py`` plays the same
actions in a batch job (``intent['meshActions']``).

MEASURED on Gmsh 4.15.2 (``tests/plan48/gm7``):

* ``mesh.clear`` on an entity that bounds a meshed higher entity does not
  raise and removes nothing; ``mesh.removeElements(dim, tag, [])`` removes
  every element of the entity. Native Delete does the same.
* ``Mesh.HighOrder*`` are read by ``optimize("HighOrder")`` and the Fast
  curving. The strategy, the weight on node displacement, the distance
  factor and the three patch-adaptation values of the native window are
  options only in FoamMesh's Gmsh build (G15 patch); the stock wheel refuses
  them.
* ``mesh.createTopology`` loops for ever when the old surface is removed
  while it still owns the nodes (DP-4256, G15). ``mesh.reclassifyNodes``
  before ``removeEntities`` hands the nodes to the new patches, and
  ``createTopology`` then returns in milliseconds and makes the curves.
  ``reclassifyNodes`` after the removal crashes Gmsh.
* ``computeCrossField`` needs the Jacobi preconditioner (PETSc's ILU
  aborts) and then lays out flat models only; on a curved one Gmsh
  segfaults or stops in ``CutMesh`` (DP-4255).

Nothing here imports FoamMesh: this file runs inside the Gmsh process.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

#: The native High-order tools window (``highOrderToolsWindow.cpp``): the
#: ranges of its fields, and which fields each algorithm leaves active
#: (``chooseopti_cb``). ``None`` as the method means the API has no call.
HIGH_ORDER_RANGES = {
    'Mesh.HighOrderThresholdMin': (0.0, 1.0),
    'Mesh.HighOrderThresholdMax': (1.0, 10.0),
    'Mesh.HighOrderNumLayers': (1, 250),
    'Mesh.HighOrderIterMax': (1, 10000),
    'Mesh.HighOrderPassMax': (1, 100),
    'Mesh.HighOrderFixBoundaryNodes': (0, 1),
    'Mesh.HighOrderStrategy': (0, 2),
    'Mesh.HighOrderDistanceFactor': (1, 20000),
    'Mesh.HighOrderMaxAdaptBlob': (1, 100),
    'Mesh.HighOrderAdaptBlobLayerFactor': (1, 100),
    'Mesh.HighOrderAdaptBlobDistFactor': (1, 100),
}
#: The six patch fields of the window: options of FoamMesh's Gmsh build only
#: (Plan 48 G15 patch, DP-5707). The stock wheel has none of them.
HIGH_ORDER_PATCH_OPTIONS = (
    'Mesh.HighOrderStrategy', 'Mesh.HighOrderWeight',
    'Mesh.HighOrderDistanceFactor', 'Mesh.HighOrderMaxAdaptBlob',
    'Mesh.HighOrderAdaptBlobLayerFactor', 'Mesh.HighOrderAdaptBlobDistFactor')
REGULARIZE_ALGORITHMS = {
    'optimization': ('HighOrder', (
        'Mesh.HighOrderThresholdMin', 'Mesh.HighOrderThresholdMax',
        'Mesh.HighOrderNumLayers', 'Mesh.HighOrderFixBoundaryNodes',
        'Mesh.HighOrderIterMax', 'Mesh.HighOrderPassMax',
        'Mesh.HighOrderPrimSurfMesh', 'Mesh.HighOrderDistCAD')
        + HIGH_ORDER_PATCH_OPTIONS),
    'elastic': ('HighOrderElastic', ()),
    'fast_curving': ('HighOrderFastCurving', ('Mesh.HighOrderNumLayers',
                                              'Mesh.HighOrderCurveOuterBL')),
    # DP-4257: the window's Fast curving with the thickness switch; FoamMesh's
    # own build reads Mesh.HighOrderFastCurvingNewAlgo for it
    # (gmsh-4.15.2-bl-curving.patch).
    'bl_curving': ('HighOrderFastCurving', ('Mesh.HighOrderNumLayers',
                                            'Mesh.HighOrderCurveOuterBL')),
}
#: The switch each fast-curving algorithm sets, as the window's cases 2 and 3.
THICKNESS_OPTION = 'Mesh.HighOrderFastCurvingNewAlgo'
THICKNESS = {'fast_curving': 0.0, 'bl_curving': 1.0}
#: What the native window sets and FoamMesh's Gmsh build cannot be told.
UNREACHABLE: dict = {}


def has_option(gmsh, name) -> bool:
    """Whether this Gmsh knows *name* (a feature test, not a version test:
    an unknown option raises)."""
    try:
        gmsh.option.getNumber(name)
    except Exception:                                    # noqa: BLE001
        return False
    return True

#: Primary (corner) nodes of the surface element types of ``getElements``.
_CORNERS = {2: 3, 3: 4, 9: 3, 10: 4, 16: 4, 20: 3, 21: 3, 22: 3, 23: 3,
            24: 3, 25: 3, 26: 3, 36: 4, 37: 4, 42: 3, 43: 3, 44: 4, 45: 4,
            46: 4, 47: 4, 52: 3, 53: 3}


class MeshActionError(ValueError):
    """An action the model cannot take, with the reason."""


def _pairs(entities):
    return [(int(d), int(t)) for d, t in (entities or [])]


def _ints(tags):
    return [int(t) for t in (tags or [])]


# -- order and quality ------------------------------------------------------ #

def mesh_order(gmsh) -> int:
    """Highest element order in the model (1 when it has no mesh)."""
    order = 1
    for dim, tag in gmsh.model.getEntities():
        for etype in gmsh.model.mesh.getElements(dim, tag)[0]:
            order = max(order, int(
                gmsh.model.mesh.getElementProperties(int(etype))[2]))
    return order


def _volume_quality(gmsh, measure='minSICN'):
    """(worst, inverted) over the highest meshed dimension."""
    for dim in (3, 2):
        tags = []
        for d, t in gmsh.model.getEntities(dim):
            tags += [int(x) for group in gmsh.model.mesh.getElements(d, t)[1]
                     for x in group]
        if tags:
            values = list(gmsh.model.mesh.getElementQualities(tags, measure))
            return {'dim': dim, 'elements': len(tags),
                    'worst': min(values),
                    'inverted': sum(1 for v in values if v <= 0.0)}
    return {'dim': 0, 'elements': 0, 'worst': None, 'inverted': 0}


# -- regularize ------------------------------------------------------------- #

def regularize(gmsh, algorithm='optimization', options=None, force=False,
               niter=1, entities=None) -> dict:
    """High-order tools, step 2: regularise the high-order elements."""
    if algorithm not in REGULARIZE_ALGORITHMS:
        raise MeshActionError(
            f'"{algorithm}" is not a regularisation; they are '
            + ', '.join(REGULARIZE_ALGORITHMS))
    method, allowed = REGULARIZE_ALGORITHMS[algorithm]
    if algorithm == 'bl_curving' and not has_option(
            gmsh, HIGH_ORDER_PATCH_OPTIONS[0]):
        raise MeshActionError(
            'Boundary layer curving (experimental) is Fast curving with the '
            "thickness switch, which only FoamMesh's own Gmsh build reads "
            "(Set up FoamMesh's Gmsh); the stock 4.15.2 wheel declares "
            'Mesh.HighOrderFastCurvingNewAlgo and never reads it; use Fast '
            'curving')
    options = dict(options or {})
    for name in options:
        if name not in allowed:
            raise MeshActionError(
                f'{name} is not used by this algorithm; it takes '
                + (', '.join(allowed) or 'no parameters'))
    if mesh_order(gmsh) < 2:
        raise MeshActionError(
            'there are no high-order elements to regularise; set the '
            'element order to 2 or more first')
    missing = [n for n in options if n in HIGH_ORDER_PATCH_OPTIONS
               and not has_option(gmsh, n)]
    if missing:
        raise MeshActionError(
            ', '.join(missing) + (' is' if len(missing) == 1 else ' are')
            + ' not an option of this Gmsh: the six patch fields of the '
            "High-order tools window need FoamMesh's own Gmsh build (Set up "
            "FoamMesh's Gmsh); the stock wheel runs with the window's "
            'defaults')
    before = _volume_quality(gmsh)
    for name, value in options.items():
        gmsh.option.setNumber(name, float(value))
    # DP-5707: the API runs with General.AbortOnError 2, so the first patch
    # that misses its critical value throws and ends the whole optimisation;
    # the native window (interactive) goes on, grows the failed patch
    # (MaxAdaptBlob, AdaptBlobLayerFactor, AdaptBlobDistFactor) and tries the
    # next ones. Run it as the window does and report what Gmsh said.
    abort = gmsh.option.getNumber('General.AbortOnError')
    last = _last_error(gmsh)
    gmsh.option.setNumber('General.AbortOnError', 0)
    thickness = None
    if algorithm in THICKNESS:
        thickness = gmsh.option.getNumber(THICKNESS_OPTION)
        gmsh.option.setNumber(THICKNESS_OPTION, THICKNESS[algorithm])
    try:
        gmsh.model.mesh.optimize(method, bool(force), int(niter),
                                 _pairs(entities))
    finally:
        gmsh.option.setNumber('General.AbortOnError', abort)
        if thickness is not None:
            gmsh.option.setNumber(THICKNESS_OPTION, thickness)
    error = _last_error(gmsh)
    result = {'algorithm': algorithm, 'method': method,
              'options': {k: float(v) for k, v in options.items()},
              'before': before, 'after': _volume_quality(gmsh),
              'order': mesh_order(gmsh)}
    if error and error != last:
        result['gmsh_error'] = error
    return result


def _last_error(gmsh) -> str:
    try:
        return str(gmsh.logger.getLastError() or '')
    except Exception:                                    # noqa: BLE001
        return ''


# -- elements: list, reverse, delete ---------------------------------------- #

def _entity_of(gmsh, tag):
    try:
        etype, _nodes, dim, entity = gmsh.model.mesh.getElement(int(tag))
    except Exception:                                    # noqa: BLE001
        return None
    return int(dim), int(entity)


def group_elements(gmsh, tags):
    """``{(dim, entity): [tag]}`` and the tags no element carries."""
    groups: dict = {}
    missing = []
    for tag in _ints(tags):
        key = _entity_of(gmsh, tag)
        if key is None:
            missing.append(tag)
            continue
        groups.setdefault(key, []).append(tag)
    return groups, missing


def list_mesh_elements(gmsh, entities=None, dim=None, below=None,
                       measure='minSICN', limit=100000) -> dict:
    """Element tags of *entities* (or of dimension *dim*), optionally only
    those whose *measure* is below *below*: the picker's source of tags."""
    if entities:
        wanted = _pairs(entities)
    else:
        wanted = [(d, t) for d, t in gmsh.model.getEntities(
            -1 if dim is None else int(dim))]
    tags: list = []
    where: dict = {}
    for d, t in wanted:
        for group in gmsh.model.mesh.getElements(d, t)[1]:
            for tag in group:
                tags.append(int(tag))
                where[int(tag)] = [d, t]
    if below is not None and tags:
        values = list(gmsh.model.mesh.getElementQualities(tags, measure))
        tags = [t for t, v in zip(tags, values) if v < float(below)]
    cut = tags[:int(limit)]
    return {'elements': cut, 'count': len(tags), 'truncated': len(tags)
            > len(cut), 'entities': {str(k): where[k] for k in cut[:2000]}}


def reverse_elements(gmsh, tags) -> dict:
    tags = _ints(tags)
    if not tags:
        raise MeshActionError('give the elements to reverse')
    _groups, missing = group_elements(gmsh, tags)
    if missing:
        raise MeshActionError(
            f'{len(missing)} of the elements do not exist (first: '
            f'{missing[0]})')
    gmsh.model.mesh.reverseElements(tags)
    return {'reversed': len(tags)}


def reverse_entities(gmsh, entities) -> dict:
    entities = _pairs(entities)
    if not entities:
        raise MeshActionError('give the curves, surfaces or volumes to '
                              'reverse')
    gmsh.model.mesh.reverse(entities)
    return {'reversed': [list(e) for e in entities]}


def remove_elements(gmsh, tags) -> dict:
    """Mesh > Delete > Elements: each tag is removed from its entity."""
    tags = _ints(tags)
    if not tags:
        raise MeshActionError('give the elements to delete')
    groups, missing = group_elements(gmsh, tags)
    if missing:
        raise MeshActionError(
            f'{len(missing)} of the elements do not exist (first: '
            f'{missing[0]})')
    for (dim, entity), group in groups.items():
        gmsh.model.mesh.removeElements(dim, entity, group)
    return {'removed': len(tags),
            'entities': [list(k) for k in groups]}


def _element_count(gmsh, dim, tag) -> int:
    return sum(len(g) for g in gmsh.model.mesh.getElements(dim, tag)[1])


def delete_entity_mesh(gmsh, entities) -> dict:
    """Mesh > Delete > Curves / Surfaces / Volumes.

    ``mesh.clear`` is tried first, as the API offers it; Gmsh leaves an
    entity that bounds a meshed higher entity alone without an error, so the
    count is read back and the elements are removed directly, as native does.
    """
    entities = _pairs(entities)
    if not entities:
        raise MeshActionError('give the curves, surfaces or volumes whose '
                              'mesh is to be deleted')
    removed = {}
    for dim, tag in sorted(entities, key=lambda e: -e[0]):
        before = _element_count(gmsh, dim, tag)
        if before:
            gmsh.model.mesh.clear([(dim, tag)])
            if _element_count(gmsh, dim, tag):
                gmsh.model.mesh.removeElements(dim, tag, [])
        removed[f'{dim}:{tag}'] = before - _element_count(gmsh, dim, tag)
    return {'removed': removed}


# -- reclassify with a subset ----------------------------------------------- #

def _surface_elements(gmsh):
    """``[(tag, type, corner nodes, entity)]`` for every surface element."""
    out = []
    for dim, entity in gmsh.model.getEntities(2):
        types, tags, nodes = gmsh.model.mesh.getElements(dim, entity)
        for etype, etags, enodes in zip(types, tags, nodes):
            per = int(gmsh.model.mesh.getElementProperties(int(etype))[3])
            corners = _CORNERS.get(int(etype), 3)
            for index, tag in enumerate(etags):
                chunk = enodes[index * per:(index + 1) * per]
                out.append((int(tag), int(etype),
                            [int(n) for n in chunk[:corners]], entity,
                            [int(n) for n in chunk]))
    return out


def _normal(points):
    ax, ay, az = points[0]
    bx, by, bz = points[1]
    cx, cy, cz = points[2]
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
    return (0.0, 0.0, 0.0) if length == 0 else (
        n[0] / length, n[1] / length, n[2] / length)


def _edge_table(gmsh, subset_tags=None):
    """Elements, node positions and edge -> element lists of the subset."""
    import math
    elements = _surface_elements(gmsh)
    if subset_tags is not None:
        wanted = set(subset_tags)
        elements = [e for e in elements if e[0] in wanted]
    node_ids, coords, _p = gmsh.model.mesh.getNodes()
    position = {int(n): (float(coords[3 * i]), float(coords[3 * i + 1]),
                         float(coords[3 * i + 2]))
                for i, n in enumerate(node_ids)}
    normals = {}
    edges: dict = {}
    for tag, _etype, corners, _entity, _all in elements:
        normals[tag] = _normal([position[n] for n in corners[:3]])
        for i in range(len(corners)):
            a, b = corners[i], corners[(i + 1) % len(corners)]
            edges.setdefault((a, b) if a < b else (b, a), []).append(tag)
    return elements, position, normals, edges, math


def _feature_edges(edges, normals, angle, boundary, math):
    """Edges whose two elements meet at more than *angle* degrees."""
    limit = math.cos(math.radians(angle))
    found = set()
    for key, owners in edges.items():
        if len(owners) == 1:
            if boundary:
                found.add(key)
        elif len(owners) > 2:
            found.add(key)
        else:
            n1, n2 = normals[owners[0]], normals[owners[1]]
            if n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2] < limit:
                found.add(key)
    return found


def reclassify_edges(gmsh, angle=40.0, elements=None, boundary=True,
                     limit=20000) -> dict:
    """The edges Reclassify 2D would split along (its edge preview)."""
    subset = _ints(elements) or None
    table, position, normals, edges, math = _edge_table(gmsh, subset)
    if not table:
        raise MeshActionError('there are no surface elements to classify')
    found = sorted(_feature_edges(edges, normals, float(angle), boundary,
                                  math))
    cut = found[:int(limit)]
    return {'count': len(found), 'truncated': len(found) > len(cut),
            'edges': [[list(position[a]), list(position[b])]
                      for a, b in cut],
            'nodes': [[a, b] for a, b in cut]}


def reclassify_subset(gmsh, angle=40.0, elements=None, entities=None,
                      boundary=True, delete_edges=None, curve_angle=180.0,
                      create_geometry=True, export_discrete=True,
                      hide_unselected=False) -> dict:
    """Mesh > Reclassify 2D on a subset of the surface elements.

    The subset is *elements* (tags) and/or the surfaces *entities*; neither
    means all. Elements are cut into patches along the edges whose neighbours
    differ by more than *angle* (and, with *boundary*, along the subset's own
    border), minus the edges in *delete_edges* (node pairs the person took
    out of the selection). Each patch becomes a discrete surface; the
    elements outside the subset keep their surfaces.

    With no subset and no edge taken out the work is Gmsh's own
    ``classifySurfaces`` (what the native button calls), which also makes the
    curves. A subset has no such call; its patches are built here, the nodes
    are handed to them (``reclassifyNodes``, before the old surfaces go) and
    ``createTopology`` makes the curves and points between them.
    """
    wanted = set(_ints(elements))
    pairs = set(_pairs(entities))
    for dim, tag in gmsh.model.getEntities(3):
        if _element_count(gmsh, dim, tag):
            raise MeshActionError(
                'the model has a volume mesh; Reclassify 2D changes the '
                'surfaces that mesh rests on. Delete the volume mesh '
                '(Delete mesh, volumes) first')
    empty = [(d, t) for d, t in gmsh.model.getEntities(3)]
    if empty:
        gmsh.model.removeEntities(empty)
    table, _position, normals, edges, math = _edge_table(gmsh, None)
    if not table:
        raise MeshActionError('there are no surface elements to classify; '
                              'open or generate a surface mesh first')
    if not wanted and not pairs and not delete_edges:
        # The whole model, no edge taken out: this is Gmsh's own call, the
        # one the native Reclassify 2D button makes.
        import math as _m
        before = len(gmsh.model.getEntities(2))
        gmsh.model.mesh.classifySurfaces(
            _m.radians(float(angle)), bool(boundary), bool(create_geometry),
            _m.radians(float(curve_angle)), bool(export_discrete))
        if create_geometry:
            gmsh.model.mesh.createGeometry([])
        return {'subset_elements': len(table), 'held_elements': 0,
                'patches': len(gmsh.model.getEntities(2)),
                'surfaces_before': before,
                'surfaces_after': len(gmsh.model.getEntities(2)),
                'curves': len(gmsh.model.getEntities(1)),
                'method': 'classifySurfaces', 'deleted_edges': 0}
    chosen = {e[0] for e in table
              if (not wanted and not pairs)
              or e[0] in wanted or (2, e[3]) in pairs}
    if not chosen:
        raise MeshActionError('the subset holds no surface element')
    held = [e for e in table if e[0] not in chosen]
    inside = [e for e in table if e[0] in chosen]
    _t, _p, sub_normals, sub_edges, _m = _edge_table(gmsh, chosen)
    feature = _feature_edges(sub_edges, sub_normals, float(angle), boundary,
                             math)
    removed = {tuple(sorted((int(a), int(b)))) for a, b in
               (delete_edges or [])}
    feature -= removed
    parent = {e[0]: e[0] for e in inside}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key, owners in sub_edges.items():
        if len(owners) == 2 and key not in feature:
            parent[find(owners[0])] = find(owners[1])
    patches: dict = {}
    for e in inside:
        patches.setdefault(find(e[0]), []).append(e)
    before = len(gmsh.model.getEntities(2))
    # Take every surface element out, then put it back as a discrete patch.
    originals = sorted({e[3] for e in table})
    for entity in originals:
        gmsh.model.mesh.removeElements(2, entity, [])
    next_tag = max([t for _d, t in gmsh.model.getEntities(2)] or [0]) + 1
    created = []
    groups = [("held", [e for e in held if e[3] == o]) for o in originals]
    groups = [g for g in groups if g[1]]
    for entity_label, members in groups + [("patch", p) for p in
                                           patches.values()]:
        tag = next_tag
        next_tag += 1
        gmsh.model.addDiscreteEntity(2, tag)
        by_type: dict = {}
        for e in members:
            slot = by_type.setdefault(e[1], ([], []))
            slot[0].append(e[0])
            slot[1].extend(e[4])
        gmsh.model.mesh.addElements(
            2, tag, list(by_type), [v[0] for v in by_type.values()],
            [v[1] for v in by_type.values()])
        created.append(tag)
    stale = [(2, o) for o in originals if o not in created]
    # DP-4256: the nodes go to the new patches before the old surfaces are
    # removed; removed first, createTopology never returns.
    gmsh.model.mesh.reclassifyNodes()
    if stale:
        gmsh.model.removeEntities(stale)
        left = [e for e in stale if e in gmsh.model.getEntities(2)]
        if left:
            raise MeshActionError(
                'Gmsh would not remove the old surfaces '
                + ', '.join(f'{d}:{t}' for d, t in left)
                + ', so the classification cannot be kept')
    curves_before = len(gmsh.model.getEntities(1))
    gmsh.model.mesh.createTopology()
    made_curves = len(gmsh.model.getEntities(1)) - curves_before
    if hide_unselected and held:
        keep = {c for c in created}
        for dim, tag in gmsh.model.getEntities(2):
            gmsh.model.setVisibility([(dim, tag)], 1 if tag in keep else 0,
                                     True)
    return {'subset_elements': len(inside), 'held_elements': len(held),
            'patches': len(patches), 'surfaces_before': before,
            'surfaces_after': len(gmsh.model.getEntities(2)),
            'curves': len(gmsh.model.getEntities(1)),
            'curves_made': made_curves, 'method': 'patches',
            'feature_edges': len(feature), 'deleted_edges': len(removed)}


# -- Compute quad layout ---------------------------------------------------- #

#: DP-4255 (G15): PETSc's default ILU cannot factor the cross-field saddle
#: system ("Matrix is missing diagonal entry", abort); Jacobi can. Gmsh
#: leaves no view: the layout is written to ``<model>_QLayoutResults.pos``
#: (and a dozen debug files) in the working folder, the scratch folder.
_CROSS_CHILD = r'''
import os, shutil, sys, gmsh
gmsh.initialize(["", "-noenv", "-pc_type", "jacobi"], readConfigFiles=False,
                run=False)
gmsh.option.setNumber("General.Terminal", 0)
gmsh.merge(sys.argv[1])
gmsh.model.mesh.computeCrossField()
tags = gmsh.view.getTags()
for tag in tags:
    gmsh.view.write(tag, sys.argv[2], True)
if not tags:
    result = gmsh.model.getCurrent() + "_QLayoutResults.pos"
    if not os.path.exists(result):
        print("Gmsh wrote no quad layout")
        sys.exit(3)
    shutil.copyfile(result, sys.argv[2])
sys.exit(0)
'''


def quad_layout(gmsh, raw=None, timeout=600.0, python=None) -> dict:
    """Mesh > Experimental > Compute quad layout, in a child process.

    ``computeCrossField`` returns on a flat model with the Jacobi
    preconditioner and still segfaults or stops in ``CutMesh`` on a curved
    one (Gmsh 4.15.2, DP-4255), and a segfault would take this session with
    it. The mesh is written to a scratch folder, the child merges it and
    computes the field there, and the layout is merged back as a view only
    when the child returns normally. A failure comes back as
    ``{'refused': reason}`` with the exit code and the last line Gmsh
    printed. The scratch folder is removed either way.
    """
    raw = raw or gmsh
    folder = tempfile.mkdtemp(prefix='foammesh-quad-')
    mesh_file = os.path.join(folder, 'mesh.msh')
    views_file = os.path.join(folder, 'field.pos')
    try:
        return _quad_layout_in(raw, folder, mesh_file, views_file, timeout,
                               python)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _quad_layout_in(raw, folder, mesh_file, views_file, timeout, python):
    raw.write(mesh_file)
    try:
        done = subprocess.run(
            [python or sys.executable, '-c', _CROSS_CHILD, mesh_file,
             views_file], capture_output=True, text=True, timeout=timeout,
            cwd=folder)
    except subprocess.TimeoutExpired:
        return {'refused': f'the cross field was not computed in '
                           f'{timeout:g} s and was stopped'}
    tail = [line for line in (done.stdout + done.stderr).splitlines()
            if line.strip()]
    last = tail[-1].strip() if tail else ''
    if done.returncode != 0 or not os.path.exists(views_file):
        return {'refused': (
            f'Gmsh stopped while computing the cross field (exit code '
            f'{done.returncode}): {last or "no message"}. Gmsh 4.15.2 computes '
            'the quad layout of flat surfaces only; on a curved one its '
            'solver crashes or stops in CutMesh. The model was left as it '
            'was'), 'exit_code': done.returncode}
    before = set(raw.view.getTags())
    raw.merge(views_file)
    views = [v for v in raw.view.getTags() if v not in before]
    return {'views': [int(v) for v in views], 'exit_code': 0}


# -- the mixin -------------------------------------------------------------- #

class SessionMeshMixin:
    """Operations of the mesh area (GM7)."""

    __slots__ = ()

    def get_mesh_order(self, **_):
        return {'order': mesh_order(self.gmsh)}

    def list_mesh_elements(self, entities=None, dim=None, below=None,
                           measure='minSICN', limit=100000, **_):
        return list_mesh_elements(self.gmsh, entities, dim, below, measure,
                                  limit)

    def regularize(self, algorithm='optimization', options=None, force=False,
                   niter=1, entities=None, **_):
        return regularize(self.gmsh, algorithm, options, force, niter,
                          entities)

    def remove_elements(self, tags, **_):
        return remove_elements(self.gmsh, tags)

    def delete_entity_mesh(self, entities, **_):
        return delete_entity_mesh(self.gmsh, entities)

    def reclassify_edges(self, angle=40.0, elements=None, boundary=True,
                         limit=20000, **_):
        return reclassify_edges(self.gmsh, angle, elements, boundary, limit)

    def reclassify_subset(self, **kwargs):
        kwargs.pop('kwargs', None)
        return reclassify_subset(self.gmsh, **kwargs)

    def quad_layout(self, limit=600.0, **_):
        return quad_layout(self.gmsh, self.raw, limit)


# -- batch job -------------------------------------------------------------- #

#: ``intent['meshActions']``: a list of ``{'action': id, 'values': {...}}``.
def apply_job_action(gmsh, action, values):
    """Play one job action; the runner part calls this after the last pass."""
    values = dict(values or {})
    if action == 'set_order':
        for name, value in (values.get('options') or {}).items():
            gmsh.option.setNumber(name, float(value))
        gmsh.model.mesh.setOrder(int(values.get('order', 2)))
        return {'order': mesh_order(gmsh)}
    if action == 'regularize':
        return regularize(gmsh, values.get('algorithm', 'optimization'),
                          values.get('options'), values.get('force', False),
                          values.get('niter', 1), values.get('entities'))
    if action == 'reverse_elements':
        return reverse_elements(gmsh, values.get('elements'))
    if action == 'reverse_entities':
        return reverse_entities(gmsh, values.get('entities'))
    if action == 'delete_elements':
        return remove_elements(gmsh, values.get('elements'))
    if action == 'delete_entity_mesh':
        return delete_entity_mesh(gmsh, values.get('entities'))
    if action == 'reclassify_subset':
        return reclassify_subset(gmsh, **values)
    if action in ('optimize_quad_topology', 'untangle_geometry'):
        method = ('QuadQuasiStructured' if action == 'optimize_quad_topology'
                  else 'UntangleMeshGeometry')
        gmsh.model.mesh.optimize(method, False, int(values.get('niter', 1)),
                                 [])
        return {'method': method}
    raise MeshActionError(f'"{action}" is not a mesh action of a job')
