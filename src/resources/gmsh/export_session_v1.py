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
        'nodes': len(gmsh.model.mesh.getNodes()[0]),
        'order': order, 'families': sorted(families),
        'family_counts': families, 'dimension': dimension,
        'partitions': partitions,
        'physical_groups': len(gmsh.model.getPhysicalGroups()),
        'entities': len(gmsh.model.getEntities()),
    }


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
           check_expected=True, **_):
    """Write the model to *path*; return what was written and what it holds."""
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
