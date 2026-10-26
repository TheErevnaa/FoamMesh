"""Save the session mesh in any Gmsh format (Plan 41 GN15).

Runs inside ``session_v1.py``.  ``facts`` describes the mesh in hand so the
host can refuse a format before anything is written; ``export`` sets the
options the user chose, writes, and puts the options back.  Reading the file
back is ``readback_v1.py``, in a process of its own: a reader may merge into
a model of the resident session (MED does, by the name stored in the file).
"""
import os
import time

_FAMILY = {
    'point': 'point', 'line': 'line', 'triangle': 'triangle',
    'quadrangle': 'quadrangle', 'quadrilateral': 'quadrangle',
    'tetrahedron': 'tetrahedron',
    'hexahedron': 'hexahedron', 'prism': 'prism', 'pyramid': 'pyramid',
}


def _family(name):
    return _FAMILY.get(str(name).split()[0].lower(), str(name).lower())


def _types_by_entity(gmsh):
    """``{(dim, tag): [(element type, [tags], [node tags]), ...]}``."""
    out = {}
    for dim, tag in gmsh.model.getEntities():
        types, tags, nodes = gmsh.model.mesh.getElements(dim, tag)
        if len(types):
            out[(dim, tag)] = list(zip(types, tags, nodes))
    return out


def facts(gmsh):
    families = {}
    order = 1
    dimension = 0
    for (dim, _tag), blocks in _types_by_entity(gmsh).items():
        for element_type, tags, _nodes in blocks:
            name, element_dim, element_order, _n, _c, _p = (
                gmsh.model.mesh.getElementProperties(int(element_type)))
            families[_family(name)] = families.get(_family(name), 0) + len(tags)
            order = max(order, int(element_order))
            dimension = max(dimension, int(element_dim))
    try:
        partitions = int(gmsh.model.getNumberOfPartitions())
    except Exception:
        partitions = 0
    try:
        cad = len(gmsh.model.occ.getEntities())
    except Exception:
        cad = None
    return {
        'cad': cad,
        'bounds': _bounds(gmsh),
        'periodic': periodic_entities(gmsh),
        'periodic_pairs': periodic_pairs(gmsh, dimension) if dimension >= 2
        else [],
        'nodes': len(gmsh.model.mesh.getNodes()[0]),
        'order': order, 'families': sorted(families),
        'family_counts': families, 'dimension': dimension,
        'partitions': partitions,
        'physical_groups': len(gmsh.model.getPhysicalGroups()),
        'entities': len(gmsh.model.getEntities()),
    }


def _periodic_info(gmsh, dim, tag):
    """``(master tag, affine)`` when the entity is the slave of a periodic pair."""
    try:
        master, _nodes, _masters, affine = gmsh.model.mesh.getPeriodicNodes(
            dim, tag)
    except Exception:
        return None
    if int(master) > 0 and int(master) != tag:
        return int(master), list(affine)
    return None


def _bounds(gmsh):
    coords = gmsh.model.mesh.getNodes()[1]
    if not len(coords):
        return []
    return [min(coords[i::3]) for i in range(3)] + [
        max(coords[i::3]) for i in range(3)]


def periodic_pairs(gmsh, dimension):
    """The periodic pairs a publisher can name: ``{slave, master, translation}``.

    One entry per pair of physical groups holding the slave and the master
    faces (curves in 2D); a pair whose entities are in no named group, or
    whose transform is not a translation, is left out (it stays a plain
    ``$Periodic`` entry of the native file).
    """
    pairs = {}
    for tag in [t for d, t in gmsh.model.getEntities(dimension - 1)]:
        info = _periodic_info(gmsh, dimension - 1, tag)
        if not info or len(info[1]) != 16:
            continue
        master, affine = info
        names = []
        for entity in (tag, master):
            group = [gmsh.model.getPhysicalName(dimension - 1, g)
                     for g in gmsh.model.getPhysicalGroupsForEntity(
                         dimension - 1, entity)]
            group = [name for name in group if name]
            names.append(group[0] if group else '')
        diagonal = (affine[0], affine[5], affine[10], affine[15])
        if not all(names) or any(abs(v - 1.0) > 1e-9 for v in diagonal):
            continue
        key = (names[0], names[1])
        if key not in pairs:
            pairs[key] = {'slave': names[0], 'master': names[1],
                          'translation': [affine[3], affine[7], affine[11]]}
    return list(pairs.values())


def periodic_entities(gmsh):
    """How many surfaces, curves and volumes are the slave of a periodic pair."""
    return sum(1 for dim, tag in gmsh.model.getEntities()
               if dim and _periodic_info(gmsh, dim, tag))


def _included(gmsh, dim, tag, save_all, has_groups):
    if save_all or not has_groups:
        return True
    return bool(gmsh.model.getPhysicalGroupsForEntity(dim, tag))


def expected(gmsh, save_all):
    """What a writer is given: the nodes and the cells of the entities that are saved."""
    import numpy as np

    has_groups = bool(gmsh.model.getPhysicalGroups())
    top = 0
    blocks = []
    for (dim, tag), items in _types_by_entity(gmsh).items():
        if _included(gmsh, dim, tag, save_all, has_groups):
            blocks.append((dim, tag, items))
            top = max(top, dim)
    volume = {}
    surface = 0
    triangles = 0
    nodes = []
    for _dim, _tag, items in blocks:
        for element_type, tags, node_tags in items:
            name, element_dim, _o, _n, _c, _p = (
                gmsh.model.mesh.getElementProperties(int(element_type)))
            if element_dim == top:
                volume[int(element_type)] = (
                    volume.get(int(element_type), 0) + len(tags))
                nodes.append(np.asarray(node_tags))
            if element_dim == 2:
                surface += len(tags)
                triangles += len(tags) * (
                    2 if _family(name) == 'quadrangle' else 1)
    unique = int(len(np.unique(np.concatenate(nodes)))) if nodes else 0
    return {'nodes': unique, 'volume': volume, 'cells': sum(volume.values()),
            'surface_elements': surface, 'surface_triangles': triangles,
            'dimension': top}


#: The corner-only element of each family (Gmsh element type).
_LINEAR_TYPE = {'point': 15, 'line': 1, 'triangle': 2, 'quadrangle': 3,
                'tetrahedron': 4, 'hexahedron': 5, 'prism': 6, 'pyramid': 7}

FIRST_ORDER_COPY = 'first_order_copy'


def first_order_copy(gmsh, name=None):
    """Add a model that is the corner-node mesh of the current one; leave it current.

    Only the corner nodes of every element are kept, each entity keeps its
    boundary, its elements (as the linear element of the same family) and its
    physical groups, and the periodic pairs of the original are declared
    again.  The original model is not touched.  Returns ``(original, copy)``
    model names; the caller writes the copy and then calls :func:`drop_copy`.
    """
    import numpy as np

    original = gmsh.model.getCurrent()
    copy = name or (original + '__first_order')
    entities = list(gmsh.model.getEntities())
    boundary = {}
    elements = {}
    corners = set()
    for dim, tag in entities:
        boundary[(dim, tag)] = [abs(t) for _d, t in gmsh.model.getBoundary(
            [(dim, tag)], combined=False, oriented=False)] if dim else []
        types, _tags, node_blocks = gmsh.model.mesh.getElements(dim, tag)
        rows = []
        for element_type, nodes in zip(types, node_blocks):
            props = gmsh.model.mesh.getElementProperties(int(element_type))
            family = _family(props[0])
            linear = _LINEAR_TYPE.get(family)
            if linear is None:
                raise ValueError(
                    f'a {props[0]} element has no first order form to copy')
            width = int(props[3])
            primary = int(props[5])
            block = np.asarray(nodes, dtype=np.int64).reshape(-1, width)
            kept = block[:, :primary].reshape(-1)
            corners.update(int(n) for n in np.unique(kept))
            rows.append((linear, kept))
        elements[(dim, tag)] = rows
    entity_nodes = {}
    for dim, tag in entities:
        ids, coords, _p = gmsh.model.mesh.getNodes(dim, tag, False, False)
        ids = np.asarray(ids, dtype=np.int64)
        use = np.isin(ids, np.fromiter(corners, dtype=np.int64,
                                       count=len(corners)))
        entity_nodes[(dim, tag)] = (
            ids[use], np.asarray(coords, dtype=float).reshape(-1, 3)[use])
    groups = []
    for dim, group in gmsh.model.getPhysicalGroups():
        groups.append((dim, group, gmsh.model.getPhysicalName(dim, group),
                       list(gmsh.model.getEntitiesForPhysicalGroup(dim, group))))
    periodic = []
    for dim, tag in entities:
        info = _periodic_info(gmsh, dim, tag) if dim else None
        if info:
            periodic.append((dim, tag, info[0], info[1]))
    gmsh.model.add(copy)
    try:
        for dim in range(4):
            for edim, tag in entities:
                if edim != dim:
                    continue
                gmsh.model.addDiscreteEntity(dim, tag, boundary[(dim, tag)])
        for (dim, tag), (ids, coords) in entity_nodes.items():
            if len(ids):
                gmsh.model.mesh.addNodes(dim, tag, ids.tolist(),
                                         coords.reshape(-1).tolist())
        for (dim, tag), rows in elements.items():
            for linear, kept in rows:
                gmsh.model.mesh.addElementsByType(tag, linear, [],
                                                  kept.tolist())
        for dim, group, label, tags in groups:
            gmsh.model.addPhysicalGroup(dim, tags, group, label)
        for dim, tag, master, affine in periodic:
            gmsh.model.mesh.setPeriodic(dim, [tag], [master], affine)
    except Exception:
        drop_copy(gmsh, original, copy)
        raise
    return original, copy


def drop_copy(gmsh, original, copy):
    """Remove the model *copy* and make *original* current again."""
    try:
        gmsh.model.setCurrent(copy)
        gmsh.model.remove()
    finally:
        gmsh.model.setCurrent(original)


def _set(gmsh, name, value):
    if isinstance(value, str):
        gmsh.option.setString(name, value)
    else:
        gmsh.option.setNumber(name, float(value))


def _get(gmsh, name):
    try:
        return gmsh.option.getNumber(name)
    except Exception:
        return gmsh.option.getString(name)


def export(gmsh, path, options=None, kind='mesh', outputs=None,
           check_expected=True, convert=None, **_):
    """Write the model to *path*; return what was written and what it holds.

    ``convert='first_order_copy'`` (Plan 48 GM4) writes the corner-node copy
    of the mesh instead; the model in the session and the files already
    written are untouched.
    """
    if convert:
        if convert != FIRST_ORDER_COPY:
            raise ValueError(f'there is no export conversion called {convert!r}')
        original, copy = first_order_copy(gmsh)
        try:
            answer = export(gmsh, path, options, kind, outputs,
                            check_expected)
        finally:
            drop_copy(gmsh, original, copy)
        answer['converted'] = convert
        return answer
    options = dict(options or {})
    saved = {name: _get(gmsh, name) for name in options}
    started = time.time()
    want = None
    directory = os.path.dirname(path) or '.'
    before = set(os.listdir(directory)) if os.path.isdir(directory) else set()
    try:
        for name, value in options.items():
            _set(gmsh, name, value)
        if kind != 'geometry' and check_expected:
            want = expected(gmsh, bool(gmsh.option.getNumber('Mesh.SaveAll')))
        gmsh.write(path)
    finally:
        for name, value in saved.items():
            _set(gmsh, name, value)
    names = set(outputs or [path])
    stem, suffix = os.path.splitext(os.path.basename(path))
    files = []
    if os.path.isdir(directory):
        for entry in sorted(os.listdir(directory)):
            full = os.path.join(directory, entry)
            if full in names or (
                    entry not in before and entry.startswith(stem + '_')
                    and entry.endswith(suffix)
                    and os.path.getmtime(full) >= started - 1):
                files.append({'path': full, 'bytes': os.path.getsize(full)})
    return {'path': path, 'files': files, 'expected': want,
            'seconds': time.time() - started}
