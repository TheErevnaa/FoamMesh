"""Gmsh plugins on the resident session (Plan 41 GN20; T-02, P-02, M-16).

Runs one of Gmsh's post-processing plugins with the options it was given and
turns every view the plugin created into a ``.vtu`` the viewport draws.  The
runner keeps those views alive, so a later plugin (``CutPlane``, ``Isosurface``,
``ExtractElements`` ...) can take one as its input and a ``PostView`` size field
can read one.

Everything here takes the ``gmsh`` module as an argument, like
``export_session_v1``; ``write_vtk`` is the runner's own writer.
"""
import os

import numpy as np

#: List-based view data: the kind letter after S/V/T, its node count and VTK type.
_LIST_SHAPES = {'P': (1, 1), 'L': (2, 3), 'T': (3, 5), 'Q': (4, 9),
                'S': (4, 10), 'H': (8, 12), 'I': (6, 13), 'Y': (5, 14)}
_COMPONENTS = {'S': 1, 'V': 3, 'T': 9}
#: VTK cell type by (dimension, number of corner nodes).
_VTK_BY_CORNERS = {(0, 1): 1, (1, 2): 3, (2, 3): 5, (2, 4): 9, (3, 4): 10,
                   (3, 8): 12, (3, 6): 13, (3, 5): 14}


def _magnitude(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim == 1 or values.shape[-1] == 1:
        return values.reshape(values.shape[0])
    return np.linalg.norm(values, axis=-1)


def _is_list_based(gmsh, tag: int):
    """``(True, data)`` for a list-based view, ``(False, None)`` otherwise.

    ``getModelData`` on a list-based view does not raise: it aborts the whole
    process (measured on 4.15.2), so the list reader always goes first and the
    model reader only runs once it has refused.
    """
    try:
        kinds, counts, data = gmsh.view.getListData(tag)
    except Exception:                                    # noqa: BLE001
        return False, None
    return True, (kinds, counts, data)


def _from_list(kinds, counts, data):
    """Points, cells ``(connectivity, offsets, types)`` and a value per point."""
    points, values, conn, offs, types = [], [], [], [], []
    base = 0
    for kind, count, block in zip(kinds, counts, data):
        count = int(count)
        if not count or len(kind) != 2 or kind[0] not in _COMPONENTS \
                or kind[1] not in _LIST_SHAPES:
            continue
        comp = _COMPONENTS[kind[0]]
        nodes, vtk = _LIST_SHAPES[kind[1]]
        table = np.asarray(block, dtype=np.float64).reshape(count, -1)
        xyz = np.stack([table[:, :nodes], table[:, nodes:2 * nodes],
                        table[:, 2 * nodes:3 * nodes]], axis=-1)
        rest = table[:, 3 * nodes:].reshape(count, nodes, -1)[:, :, :comp]
        points.append(xyz.reshape(-1, 3))
        values.append(_magnitude(rest.reshape(count * nodes, comp)))
        conn.append(base + np.arange(count * nodes, dtype=np.int64))
        offs.append(base + nodes * np.arange(1, count + 1, dtype=np.int64))
        types.append(np.full(count, vtk, dtype=np.uint8))
        base += count * nodes
    if not points:
        return None
    return (np.concatenate(points), np.concatenate(values),
            (np.concatenate(conn), np.concatenate(offs),
             np.concatenate(types)))


def _model_elements(gmsh):
    """Every element of the model as ``(vtk type, tags, corner node tags)``."""
    groups = []
    kinds, tag_lists, node_lists = gmsh.model.mesh.getElements()
    for kind, tags, nodes in zip(kinds, tag_lists, node_lists):
        _name, dim, _order, per_element, _local, corners = \
            gmsh.model.mesh.getElementProperties(int(kind))
        vtk = _VTK_BY_CORNERS.get((int(dim), int(corners)))
        if vtk is None or not len(tags):
            continue
        nodes = np.asarray(nodes, dtype=np.int64).reshape(
            len(tags), int(per_element))[:, :int(corners)]
        groups.append((int(dim), vtk, np.asarray(tags, dtype=np.int64), nodes))
    return groups


def _from_model(gmsh, tag: int):
    """Points, cells and values of a model-based view (node or element data)."""
    kind, view_tags, data, _time, comp = gmsh.view.getModelData(tag, 0)
    view_tags = np.asarray(view_tags, dtype=np.int64)
    values = np.asarray([np.asarray(item, dtype=np.float64).ravel()
                         for item in data], dtype=object)
    node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
    node_tags = np.asarray(node_tags, dtype=np.int64)
    coordinates = np.asarray(coordinates, dtype=np.float64).reshape(-1, 3)
    row_of = np.full(int(node_tags.max()) + 1 if len(node_tags) else 1, -1,
                     dtype=np.int64)
    row_of[node_tags] = np.arange(len(node_tags))
    groups = _model_elements(gmsh)
    if not groups:
        return None
    if kind == 'NodeData':
        top = 2 if any(g[0] == 2 for g in groups) else max(g[0] for g in groups)
        groups = [g for g in groups if g[0] == top]
        per_node = np.zeros(len(node_tags))
        flat = np.stack([np.resize(item, comp) for item in values]) \
            if len(values) else np.zeros((0, comp))
        rows = row_of[view_tags]
        per_node[rows[rows >= 0]] = _magnitude(flat)[rows >= 0]
        conn, offs, types = [], [], []
        cursor = 0
        for _dim, vtk, _tags, nodes in groups:
            width = nodes.shape[1]
            conn.append(row_of[nodes].reshape(-1))
            offs.append(cursor + width * np.arange(1, len(nodes) + 1))
            types.append(np.full(len(nodes), vtk, dtype=np.uint8))
            cursor += width * len(nodes)
        return (coordinates, per_node,
                (np.concatenate(conn), np.concatenate(offs),
                 np.concatenate(types)), 'point')
    # ElementData and ElementNodeData: one number per element.
    order = np.argsort(view_tags)
    sorted_tags = view_tags[order]
    per_element = []
    for item in values:
        item = np.asarray(item, dtype=np.float64).ravel()
        if kind == 'ElementNodeData' and comp:
            item = item.reshape(-1, comp).mean(axis=0)
        per_element.append(_magnitude(item.reshape(1, -1))[0])
    per_element = np.asarray(per_element, dtype=np.float64)
    conn, offs, types, cell_values = [], [], [], []
    cursor = 0
    for _dim, vtk, tags, nodes in groups:
        at = np.searchsorted(sorted_tags, tags)
        at[at >= len(sorted_tags)] = 0
        hit = (sorted_tags[at] == tags) if len(sorted_tags) else \
            np.zeros(len(tags), bool)
        if not hit.any():
            continue
        nodes, at = nodes[hit], at[hit]
        width = nodes.shape[1]
        conn.append(row_of[nodes].reshape(-1))
        offs.append(cursor + width * np.arange(1, len(nodes) + 1))
        types.append(np.full(len(nodes), vtk, dtype=np.uint8))
        cell_values.append(per_element[order][at])
        cursor += width * len(nodes)
    if not conn:
        return None
    return (coordinates, np.concatenate(cell_values),
            (np.concatenate(conn), np.concatenate(offs),
             np.concatenate(types)), 'cell')


def describe(gmsh, tag: int, directory, write_vtk) -> dict:
    """Name, extremes and the ``.vtu`` of view *tag*; ``path`` is None if empty."""
    name = gmsh.view.option.getString(tag, 'Name')
    row = {'tag': int(tag), 'name': name, 'path': None, 'cells': 0,
           'min': None, 'avg': None, 'max': None}
    listed, payload = _is_list_based(gmsh, tag)
    built = None
    if listed is True:
        got = _from_list(*payload)
        if got is not None:
            points, values, cells = got
            built = (points, values, cells, 'point')
    elif listed is False:
        built = _from_model(gmsh, tag)
    if built is None:
        return row
    points, values, cells, where = built
    path = os.path.join(directory, 'plugin_view_%d.vtu' % tag)
    os.makedirs(directory, exist_ok=True)
    kwargs = ({'point_data': {'value': values}} if where == 'point'
              else {'cell_data': {'value': values}})
    write_vtk(path, 'vtu', points=points, cells=cells, **kwargs)
    row.update(path=path, cells=int(len(cells[2])), min=float(values.min()),
               avg=float(values.mean()), max=float(values.max()))
    return row


def describe_entity(gmsh, dim: int, tag: int, directory, write_vtk,
                    plugin: str = '') -> dict:
    """The mesh elements of a model entity a plugin created, as a ``.vtu``.

    ``Skin`` run on the mesh adds a discrete surface carrying the boundary
    triangles instead of a view.  The value written per cell is the entity tag.
    """
    row = {'tag': None, 'entity': [int(dim), int(tag)], 'path': None,
           'name': '%s entity %d:%d' % (plugin or 'plugin', dim, tag),
           'cells': 0, 'min': None, 'avg': None, 'max': None}
    node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
    node_tags = np.asarray(node_tags, dtype=np.int64)
    coordinates = np.asarray(coordinates, dtype=np.float64).reshape(-1, 3)
    row_of = np.full(int(node_tags.max()) + 1 if len(node_tags) else 1, -1,
                     dtype=np.int64)
    row_of[node_tags] = np.arange(len(node_tags))
    conn, offs, types = [], [], []
    cursor = 0
    kinds, tag_lists, node_lists = gmsh.model.mesh.getElements(dim, tag)
    for kind, tags, nodes in zip(kinds, tag_lists, node_lists):
        _n, edim, _o, per_element, _l, corners =             gmsh.model.mesh.getElementProperties(int(kind))
        vtk = _VTK_BY_CORNERS.get((int(edim), int(corners)))
        if vtk is None or not len(tags):
            continue
        nodes = np.asarray(nodes, dtype=np.int64).reshape(
            len(tags), int(per_element))[:, :int(corners)]
        conn.append(row_of[nodes].reshape(-1))
        offs.append(cursor + int(corners) * np.arange(1, len(tags) + 1))
        types.append(np.full(len(tags), vtk, dtype=np.uint8))
        cursor += int(corners) * len(tags)
    if not conn:
        return row
    cells = (np.concatenate(conn), np.concatenate(offs),
             np.concatenate(types))
    values = np.full(len(cells[2]), float(tag))
    path = os.path.join(directory, 'plugin_entity_%d_%d.vtu' % (dim, tag))
    os.makedirs(directory, exist_ok=True)
    write_vtk(path, 'vtu', points=coordinates, cells=cells,
              cell_data={'value': values})
    row.update(path=path, cells=int(len(cells[2])), min=float(tag),
               avg=float(tag), max=float(tag))
    return row


def listing(gmsh) -> list:
    """Every view in the session: a plugin result, a loaded data view, a
    quality view.  Any of them can be the input view of a plugin (Plan 48
    GM9); ``kind`` says whether it is list-based or model-based."""
    rows = []
    for i, t in enumerate(gmsh.view.getTags()):
        listed, _payload = _is_list_based(gmsh, t)
        rows.append({'tag': int(t),
                     'name': gmsh.view.option.getString(t, 'Name'),
                     'index': i, 'kind': 'list' if listed else 'model'})
    return rows


def remove(gmsh, tag=None) -> dict:
    tags = [int(tag)] if tag is not None else list(gmsh.view.getTags())
    for item in tags:
        if item in gmsh.view.getTags():
            gmsh.view.remove(item)
    return {'removed': tags, 'left': listing(gmsh)}


def _why(name, error, lines) -> str:
    """The reason a plugin run failed, from Gmsh's own log lines."""
    said = [line.split(': ', 1)[-1].strip() for line in lines
            if line.startswith(('Error', 'Warning'))]
    text = str(error)
    if text.startswith('Exception: '):
        text = text[len('Exception: '):]
    return '%s failed: %s' % (name, '; '.join(said[-3:]) if said else text)


def run(gmsh, name, numbers, strings, directory, write_vtk, *,
        view=None, other_view=None, keep=True) -> dict:
    """Run plugin *name*; describe every view it made."""
    tags = list(gmsh.view.getTags())
    for key, tag in (('View', view), ('OtherView', other_view)):
        if tag is not None:
            if int(tag) not in tags:
                raise ValueError('there is no view %s to give %s' % (tag, name))
            numbers = dict(numbers or {}, **{key: tags.index(int(tag))})
    for key, value in (numbers or {}).items():
        try:
            gmsh.plugin.setNumber(name, key, float(value))
        except Exception:                                # noqa: BLE001
            # A plugin with no View option acts on the last view made.
            if key != 'View':
                raise
    for key, value in (strings or {}).items():
        gmsh.plugin.setString(name, key, str(value))
    entities = set(gmsh.model.getEntities())
    # Plugins write side files (VoroMetal, Bubbles) next to the process: keep
    # them in the plugin folder, not wherever the session started.
    here = os.getcwd()
    try:
        os.makedirs(directory, exist_ok=True)
        os.chdir(directory)
    except OSError:
        here = None
    gmsh.logger.start()
    try:
        try:
            returned = gmsh.plugin.run(name)
        except Exception as error:                       # noqa: BLE001
            # Gmsh answers "Unknown plugin or plugin action" for any error
            # during a run: say what its log said instead.
            raise RuntimeError(_why(name, error, gmsh.logger.get())) from None
        log = [line for line in gmsh.logger.get()]
    finally:
        gmsh.logger.stop()
        if here is not None:
            os.chdir(here)
    after = list(gmsh.view.getTags())
    made = [t for t in after if t not in tags]
    views = [describe(gmsh, t, directory, write_vtk) for t in made]
    created = sorted(set(gmsh.model.getEntities()) - entities)
    views += [describe_entity(gmsh, d, t, directory, write_vtk, name)
              for d, t in created if d >= 1]
    if not keep:
        for t in made:
            gmsh.view.remove(t)
    return {'plugin': name, 'returned': int(returned) if returned is not None
            else None, 'views': views, 'log': log[-40:],
            'kept': listing(gmsh) if keep else [],
            'entities': [[int(d), int(t)] for d, t in created]}
