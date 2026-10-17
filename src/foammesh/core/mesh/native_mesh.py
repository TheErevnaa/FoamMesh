"""A native Gmsh mesh as arrays, and the polyhedral topology made from it.

Plan 41 GN23 (FM-02..FM-11). FoamMesh's own tools -- the section worker, the
deviation and feature measurements, named selections, the colour-by-entity and
-group choices, stage compare and the failed-cells overlay -- were written for
an OpenFOAM polyMesh. A Gmsh mesh that exists only as the resident session's
hand-over (``mesh.vtu``) or as a native ``.msh`` has no polyMesh, and the tools
refused it. This module reads either file into plain numpy arrays and builds
from them what the tools read:

* :func:`boundary_faces` -- the surface elements, patch by patch, in the order
  the viewport draws each patch's cells. A face's label is its position in that
  list, so a patch's ``start_face`` is the running total of the patches before
  it (the labels of a stored deviation field).
* :func:`topology` -- a :class:`~foammesh.core.mesh.poly_mesh_topology.MeshTopology`
  (faces, owner, neighbour, cell zones from the physical groups, a "level" from
  the Gmsh entity or partition) that the section worker cuts exactly as it cuts
  a polyMesh read from disk.

No Qt, no window. First-order elements only (tetrahedra, hexahedra, prisms,
pyramids; triangles and quadrangles on the surface), the elements the viewport
draws.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .poly_mesh_boundary import BoundaryPatch, Zone

__all__ = ['NativeMeshError', 'NativeMesh', 'read_native_mesh',
           'header_counts', 'boundary_poly_mesh', 'curve_polylines',
           'chain_polylines',
           'boundary_faces', 'topology', 'digest', 'LEVEL_BY_PARTITION',
           'LEVEL_BY_ENTITY']

#: VTK cell type -> the faces of the cell (local node numbers). Orientation is
#: fixed up per cell from its signed volume, so only the node sets matter.
_CELL_FACES = {
    10: ((0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)),                  # tet
    12: ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5),
         (2, 3, 7, 6), (3, 0, 4, 7)),                                  # hex
    13: ((0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4),
         (2, 0, 3, 5)),                                                # prism
    14: ((0, 3, 2, 1), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)),    # pyramid
}
_NODES = {10: 4, 12: 8, 13: 6, 14: 5, 5: 3, 9: 4, 3: 2}

#: ``MeshTopology.cell_level`` carries the Gmsh partition when the mesh is
#: partitioned and the Gmsh entity otherwise (the "refinement level" colour of
#: a snappy mesh has no Gmsh counterpart; these are the nearest facts).
LEVEL_BY_PARTITION = 'partition'
LEVEL_BY_ENTITY = 'entity'


class NativeMeshError(ValueError):
    """The file cannot be read as a first-order Gmsh mesh."""


@dataclass
class NativeMesh:
    """The elements of a native Gmsh mesh, split by dimension."""

    path: Path
    points: np.ndarray                       # (n, 3) float64
    #: Volume cells in the order the viewport draws them.
    cell_types: np.ndarray                   # (nc,) VTK type
    cell_nodes: np.ndarray                   # flat point ids
    cell_offsets: np.ndarray                 # (nc + 1,)
    cell_physical: np.ndarray                # (nc,) int
    cell_entity: np.ndarray
    cell_partition: np.ndarray
    cell_tags: np.ndarray                    # Gmsh element tags (or 1..nc)
    #: Surface elements, in file order (the viewport groups them by patch).
    surf_nodes: np.ndarray
    surf_offsets: np.ndarray
    surf_physical: np.ndarray
    surf_entity: np.ndarray
    surf_types: np.ndarray
    #: Curve elements (dimension 1), when the file carries them.
    curve_nodes: np.ndarray = field(
        default_factory=lambda: np.empty(0, np.int64))
    curve_entity: np.ndarray = field(
        default_factory=lambda: np.empty(0, np.int64))
    #: ``{(dim, tag): name}``
    names: dict = field(default_factory=dict)
    #: Per-cell Gmsh arrays carried by a hand-over (quality, volume ...).
    cell_arrays: dict = field(default_factory=dict)
    #: ``'vtu'`` or ``'msh'``; a ``.msh`` cell is a block-grouped one.
    source: str = 'vtu'

    @property
    def n_cells(self) -> int:
        return int(len(self.cell_types))

    @property
    def n_points(self) -> int:
        return int(len(self.points))

    def patch_name(self, key) -> str:
        kind, tag = key
        if kind == 'p':
            found = str(self.names.get((2, tag), '') or '').strip()
            return found or f'surface{tag}'
        return f'entity{tag}'

    def patch_order(self) -> list:
        """Patch keys in the order the viewport lists them."""
        keys: dict = {}
        for physical, entity in zip(self.surf_physical, self.surf_entity):
            tag = int(physical)
            key = ('p', tag) if (tag or self.source == 'msh') else (
                'e', int(entity))
            keys.setdefault(key, None)
        return list(keys)

    def surface_keys(self) -> list:
        tags = self.surf_physical
        if self.source == 'msh':
            return [('p', int(t)) for t in tags]
        return [('p', int(t)) if t else ('e', int(e))
                for t, e in zip(tags, self.surf_entity)]

    def volume_group_names(self) -> dict:
        """``physical tag -> name`` for the volume groups present."""
        out = {}
        for tag in np.unique(self.cell_physical):
            tag = int(tag)
            if tag:
                out[tag] = (str(self.names.get((3, tag), '') or '').strip()
                            or f'volume{tag}')
        return out


# -- reading --------------------------------------------------------------- #

def digest(path) -> str:
    """The sha256 of the file, which names the mesh a stored result is of."""
    sha = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            sha.update(block)
    return sha.hexdigest()


def header_counts(path) -> dict:
    """What the memory estimate needs, from the file's header only.

    A hand-over names its point and cell counts in the first bytes; a ``.msh``
    is estimated from its size (about 70 bytes an element).
    """
    import re

    path = Path(path)
    size = path.stat().st_size
    cells = points = None
    if path.suffix.lower() == '.vtu':
        with open(path, 'rb') as handle:
            head = handle.read(8192).decode('latin-1')
        found = re.search(r'NumberOfPoints="(\d+)"\s+NumberOfCells="(\d+)"',
                          head)
        if found:
            points, cells = int(found.group(1)), int(found.group(2))
    if cells is None:
        cells = max(size // 70, 1)
        points = max(cells // 5, 1)
    return {'points': points, 'cells': cells, 'faces': 3 * cells,
            'internal_faces': 2 * cells, 'boundary_faces': cells // 10,
            'file_bytes': {'faces': size}, 'formats': {'native': 'native'},
            'layout': 'native'}


def read_native_mesh(path) -> NativeMesh:
    path = Path(path)
    if path.suffix.lower() == '.vtu':
        return _read_vtu(path)
    if path.suffix.lower() == '.msh':
        return _read_msh(path)
    raise NativeMeshError(f'{path.name}: not a Gmsh hand-over or .msh file')


def _names_of(path: Path) -> dict:
    try:
        text = (path.parent / 'mesh.names.json').read_text(encoding='utf-8')
        raw = json.loads(text).get('physical') or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for key, value in raw.items():
        try:
            dim, tag = key.split(':')
            out[(int(dim), int(tag))] = str(value)
        except ValueError:
            continue
    return out


def _read_vtu(path: Path) -> NativeMesh:
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    if not path.is_file():
        raise NativeMeshError(f'{path.name}: the session left no mesh')
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    if grid is None or grid.GetNumberOfCells() < 1:
        raise NativeMeshError(f'{path.name} holds no elements')
    data = grid.GetCellData()
    if data.GetArray('gmshEntityDim') is None:
        raise NativeMeshError(f'{path.name} carries no gmshEntityDim array')

    def column(name, dtype=np.int64):
        array = data.GetArray(name)
        return (vtk_to_numpy(array).astype(dtype) if array is not None
                else np.zeros(grid.GetNumberOfCells(), dtype))

    points = vtk_to_numpy(grid.GetPoints().GetData()).astype(np.float64)
    cells = grid.GetCells()
    connectivity = vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
    offsets = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
    types = vtk_to_numpy(grid.GetCellTypesArray()).astype(np.int64)
    dims = column('gmshEntityDim')
    physical, entity = column('gmshPhysical'), column('gmshEntityTag')
    partition, tags = column('gmshPartition'), column('gmshElementTag')

    def gather(selection):
        starts, ends = offsets[selection], offsets[selection + 1]
        sizes = ends - starts
        if not len(selection):
            return np.empty(0, np.int64), np.zeros(1, np.int64)
        index = np.repeat(starts, sizes) + (
            np.arange(int(sizes.sum())) - np.repeat(
                np.cumsum(sizes) - sizes, sizes))
        return connectivity[index], np.concatenate(([0], np.cumsum(sizes)))

    volume = np.nonzero(dims == 3)[0]
    surface = np.nonzero(dims == 2)[0]
    curves = np.nonzero(dims == 1)[0]
    cell_nodes, cell_offsets = gather(volume)
    surf_nodes, surf_offsets = gather(surface)
    curve_nodes, _ = gather(curves)
    extras = {}
    skip = {'gmshElementTag', 'gmshElementType', 'gmshEntityDim',
            'gmshEntityTag', 'gmshPhysical', 'gmshPartition'}
    for index in range(data.GetNumberOfArrays()):
        name = data.GetArrayName(index)
        if name and name not in skip:
            extras[name] = vtk_to_numpy(data.GetArray(name))[volume].astype(
                np.float64)
    return NativeMesh(
        path=path, points=points, cell_types=types[volume],
        cell_nodes=cell_nodes, cell_offsets=cell_offsets,
        cell_physical=physical[volume], cell_entity=entity[volume],
        cell_partition=partition[volume], cell_tags=tags[volume],
        surf_nodes=surf_nodes, surf_offsets=surf_offsets,
        surf_physical=physical[surface], surf_entity=entity[surface],
        surf_types=types[surface], curve_nodes=curve_nodes,
        curve_entity=entity[curves], names=_names_of(path),
        cell_arrays=extras, source='vtu')


_MSH_VOLUME = {4: (10, 4), 5: (12, 8), 6: (13, 6), 7: (14, 5)}
_MSH_SURFACE = {2: (5, 3), 3: (9, 4)}


def _read_msh(path: Path) -> NativeMesh:
    from foammesh.core.gmsh.publish import PublishError, read_msh

    try:
        document = read_msh(path)
    except PublishError as error:
        raise NativeMeshError(f'{path.name}: {error}') from error
    index = document.node_index
    volume: dict = {}
    surface: list = []
    lines: list = []
    for element_type, physical, nodes in document.elements:
        if element_type in _MSH_VOLUME:
            vtk_type, size = _MSH_VOLUME[element_type]
            volume.setdefault(vtk_type, []).append(
                (int(physical), [index[n] for n in nodes[:size]]))
        elif element_type in _MSH_SURFACE:
            vtk_type, size = _MSH_SURFACE[element_type]
            surface.append((vtk_type, int(physical),
                            [index[n] for n in nodes[:size]]))
        elif element_type == 1:
            lines.append([index[n] for n in nodes[:2]])
    if not volume:
        raise NativeMeshError(f'{path.name} holds no first-order volume '
                              'elements to cut')
    types, nodes, offsets, physical = [], [], [0], []
    for vtk_type, rows in volume.items():          # the viewport's block order
        for tag, row in rows:
            types.append(vtk_type)
            nodes.extend(row)
            offsets.append(len(nodes))
            physical.append(tag)
    # The viewport groups a patch's cells by type (first appearance), too.
    blocks: dict = {}
    for vtk_type, tag, row in surface:
        blocks.setdefault(tag, {}).setdefault(vtk_type, []).append(row)
    s_types, s_nodes, s_offsets, s_phys = [], [], [0], []
    for tag, by_type in blocks.items():
        for vtk_type, rows in by_type.items():
            for row in rows:
                s_types.append(vtk_type)
                s_nodes.extend(row)
                s_offsets.append(len(s_nodes))
                s_phys.append(tag)
    n_cells = len(types)
    return NativeMesh(
        path=path, points=np.asarray(document.points, dtype=np.float64),
        cell_types=np.asarray(types, np.int64),
        cell_nodes=np.asarray(nodes, np.int64),
        cell_offsets=np.asarray(offsets, np.int64),
        cell_physical=np.asarray(physical, np.int64),
        cell_entity=np.zeros(n_cells, np.int64),
        cell_partition=np.zeros(n_cells, np.int64),
        cell_tags=np.arange(1, n_cells + 1, dtype=np.int64),
        surf_nodes=np.asarray(s_nodes, np.int64),
        surf_offsets=np.asarray(s_offsets, np.int64),
        surf_physical=np.asarray(s_phys, np.int64),
        surf_entity=np.zeros(len(s_phys), np.int64),
        surf_types=np.asarray(s_types, np.int64),
        curve_nodes=np.asarray(lines, np.int64).reshape(-1),
        curve_entity=np.zeros(len(lines), np.int64),
        names={key: value for key, value in document.physical_names.items()},
        source='msh')


# -- the boundary, patch by patch ----------------------------------------- #

def boundary_faces(mesh: NativeMesh):
    """``(face_vertices, face_offsets, patches)`` of the surface elements.

    Faces are grouped by patch in the viewport's order and, inside a patch, in
    the order the viewport draws its cells; ``patches`` are
    :class:`BoundaryPatch` with ``start_face`` the running total.
    """
    keys = mesh.surface_keys()
    order_of = {key: position for position, key in enumerate(mesh.patch_order())}
    # Within a patch the viewport keeps a vtk type block order (first
    # appearance) for a .msh and file order for a hand-over.
    taken: dict = {key: [] for key in order_of}
    for position, key in enumerate(keys):
        taken[key].append(position)
    sequence: list = []
    patches: list = []
    start = 0
    for key in order_of:
        members = taken[key]
        if mesh.source == 'msh':
            blocks: dict = {}
            for position in members:
                blocks.setdefault(int(mesh.surf_types[position]),
                                  []).append(position)
            members = [p for block in blocks.values() for p in block]
        sequence.extend(members)
        patches.append(BoundaryPatch(mesh.patch_name(key), 'patch', start,
                                     len(members)))
        start += len(members)
    sizes = np.diff(mesh.surf_offsets)[np.asarray(sequence, np.int64)] \
        if sequence else np.empty(0, np.int64)
    flat = (np.concatenate([mesh.surf_nodes[mesh.surf_offsets[p]:
                                            mesh.surf_offsets[p + 1]]
                            for p in sequence])
            if sequence else np.empty(0, np.int64))
    return flat, np.concatenate(([0], np.cumsum(sizes))), tuple(patches)


def boundary_poly_mesh(mesh: NativeMesh):
    """The surface elements as the boundary-only mesh the fidelity checks
    measure (`geometry_fidelity.run.measure_section` triangulates its faces).

    A face's id is its position in :func:`boundary_faces`, so the ids of a
    stored deviation field are the viewport's labels (``patchStartFaces``).
    """
    from .poly_mesh_boundary import BoundaryPolyMesh

    flat, offsets, patches = boundary_faces(mesh)
    return BoundaryPolyMesh(
        path=mesh.path, points=np.ascontiguousarray(mesh.points),
        face_vertices=flat.astype(np.int64), face_offsets=offsets,
        patches=patches, face_base=0, total_face_count=len(offsets) - 1,
        point_ids=np.arange(mesh.n_points, dtype=np.int64),
        total_point_count=mesh.n_points)


def chain_polylines(points, segments, entity) -> dict:
    """``entity -> [polyline points (k, 3)]`` from line elements.

    *segments* is ``(n, 2)`` point ids and *entity* the curve each belongs to;
    the segments of one curve are chained end to end (a closed curve comes back
    with its first point repeated at the end).
    """
    segments = np.asarray(segments, np.int64).reshape(-1, 2)
    entity = np.asarray(entity, np.int64)
    out: dict = {}
    for tag in (np.unique(entity) if len(entity) else ()):
        rows = segments[entity == tag]
        adjacent: dict = {}
        for position, (a, b) in enumerate(rows):
            adjacent.setdefault(int(a), []).append((position, int(b)))
            adjacent.setdefault(int(b), []).append((position, int(a)))
        used = np.zeros(len(rows), bool)
        chains = []
        # Open chains start at an end point (one segment); then any loops.
        starts = [n for n, links in adjacent.items() if len(links) == 1]
        starts += list(adjacent)
        for start in starts:
            for position, _next in adjacent[start]:
                if used[position]:
                    continue
                chain, here = [start], start
                while True:
                    step = next(((q, n) for q, n in adjacent[here]
                                 if not used[q]), None)
                    if step is None:
                        break
                    used[step[0]] = True
                    here = step[1]
                    chain.append(here)
                chains.append(np.asarray(points)[np.asarray(chain, np.int64)])
        out[int(tag)] = chains
    return out


def curve_polylines(mesh: NativeMesh) -> dict:
    """``curve entity tag -> [polyline points (k, 3)]``: the model's curves
    as chains of the line elements that mesh them."""
    return chain_polylines(mesh.points,
                           np.asarray(mesh.curve_nodes, np.int64
                                      ).reshape(-1, 2),
                           mesh.curve_entity)


# -- the polyhedral topology ---------------------------------------------- #

def _cell_faces(mesh: NativeMesh):
    """Every cell face, outward from its cell.

    Returns ``(cell, nodes (n, 4) with -1 padding, size)`` arrays.
    """
    cells, nodes, sizes = [], [], []
    for vtk_type, faces in _CELL_FACES.items():
        which = np.nonzero(mesh.cell_types == vtk_type)[0]
        if not len(which):
            continue
        width = _NODES[vtk_type]
        conn = mesh.cell_nodes[mesh.cell_offsets[which][:, None]
                               + np.arange(width)]
        # Signed volume from the listed orientation; flip a cell that is
        # inside out so every face points away from it.
        centre = mesh.points[conn].mean(axis=1)
        volume = np.zeros(len(which))
        for local in faces:
            ring = mesh.points[conn[:, list(local)]] - centre[:, None, :]
            for k in range(1, len(local) - 1):
                volume += np.einsum(
                    'ij,ij->i', ring[:, 0],
                    np.cross(ring[:, k], ring[:, k + 1]))
        inside_out = volume < 0
        for local in faces:
            face = conn[:, list(local)]
            flipped = face[:, ::-1]
            face = np.where(inside_out[:, None], flipped, face)
            padded = np.full((len(which), 4), -1, np.int64)
            padded[:, :len(local)] = face
            cells.append(which)
            nodes.append(padded)
            sizes.append(np.full(len(which), len(local), np.int64))
    return (np.concatenate(cells), np.concatenate(nodes),
            np.concatenate(sizes))


def _void(rows: np.ndarray) -> np.ndarray:
    rows = np.ascontiguousarray(np.sort(rows, axis=1))
    return rows.view(np.dtype((np.void, rows.dtype.itemsize * rows.shape[1]))
                     ).ravel()


def topology(mesh: NativeMesh, *, level: str | None = None):
    """The mesh as a polyMesh-style topology the section worker can cut.

    Interior faces first, ordered by owner then neighbour; then the boundary
    faces, patch by patch (a boundary face with no surface element joins a
    final ``defaultFaces`` patch). Cell zones are the volume physical groups;
    the cell "level" is the Gmsh partition when the mesh is partitioned and
    the Gmsh entity otherwise (``level`` forces one).
    """
    from .poly_mesh_topology import MeshIdentity, MeshTopology

    if mesh.n_cells == 0:
        raise NativeMeshError('the mesh has no volume cells')
    cell, nodes, size = _cell_faces(mesh)
    keys = _void(nodes)
    surf_sizes = np.diff(mesh.surf_offsets)
    surf_rows = np.full((len(surf_sizes), 4), -1, np.int64)
    for width in (3, 4):
        which = np.nonzero(surf_sizes == width)[0]
        if len(which):
            surf_rows[which, :width] = mesh.surf_nodes[
                mesh.surf_offsets[which][:, None] + np.arange(width)]
    all_keys = np.concatenate([keys, _void(surf_rows)]) if len(surf_sizes) \
        else keys
    unique, inverse = np.unique(all_keys, return_inverse=True)
    inverse = inverse.ravel()
    n_cf = len(keys)
    face_of = inverse[:n_cf]
    count = np.bincount(face_of, minlength=len(unique))
    if (count > 2).any():
        raise NativeMeshError('a face is shared by more than two cells; the '
                              'mesh is not a manifold volume mesh')
    order = np.argsort(face_of, kind='stable')
    sorted_faces = face_of[order]
    first = np.searchsorted(sorted_faces, np.arange(len(unique)))
    entry_a = order[np.clip(first, 0, n_cf - 1)]
    second = np.where(count == 2, first + 1, first)
    entry_b = order[np.clip(second, 0, n_cf - 1)]
    present = count > 0
    # The owner is the lower cell; its entry gives the outward face.
    swap = (count == 2) & (cell[entry_b] < cell[entry_a])
    owner_entry = np.where(swap, entry_b, entry_a)
    other_entry = np.where(swap, entry_a, entry_b)
    interior = present & (count == 2)
    boundary = present & (count == 1)

    ids = np.nonzero(interior)[0]
    owner_i = cell[owner_entry[ids]]
    neigh_i = cell[other_entry[ids]]
    order_i = np.lexsort((neigh_i, owner_i))
    ids, owner_i, neigh_i = ids[order_i], owner_i[order_i], neigh_i[order_i]
    interior_entry = owner_entry[ids]

    # boundary faces: patch of the surface element that sits on the face
    b_ids = np.nonzero(boundary)[0]
    patch_of_face = {}
    keys_surface = mesh.surface_keys()
    patch_index = {key: i for i, key in enumerate(mesh.patch_order())}
    for position in range(len(surf_sizes)):
        face_id = int(inverse[n_cf + position])
        if boundary[face_id] and face_id not in patch_of_face:
            patch_of_face[face_id] = patch_index[keys_surface[position]]
    default = len(patch_index)
    b_patch = np.asarray([patch_of_face.get(int(f), default) for f in b_ids],
                         np.int64)
    b_order = np.lexsort((owner_entry[b_ids], b_patch))
    b_ids, b_patch = b_ids[b_order], b_patch[b_order]
    boundary_entry = owner_entry[b_ids]

    entries = np.concatenate([interior_entry, boundary_entry])
    owner = np.concatenate([owner_i, cell[boundary_entry]]).astype(np.int64)
    sizes = size[entries]
    flat = np.concatenate([nodes[e, :s] for e, s in zip(entries, sizes)]) \
        if len(entries) else np.empty(0, np.int64)
    face_offsets = np.concatenate(([0], np.cumsum(sizes))).astype(np.int64)

    names = [mesh.patch_name(key) for key in mesh.patch_order()]
    patches, start = [], len(ids)
    for position in range(len(names) + 1):
        n = int(np.count_nonzero(b_patch == position))
        if not n:
            continue
        name = names[position] if position < len(names) else 'defaultFaces'
        patches.append(BoundaryPatch(name, 'patch', start, n))
        start += n

    zones = []
    for tag, name in mesh.volume_group_names().items():
        zones.append(Zone(name, 'cellZone',
                          np.nonzero(mesh.cell_physical == tag)[0]
                          .astype(np.int64)))
    partitioned = bool(np.any(mesh.cell_partition > 0))
    by = level or (LEVEL_BY_PARTITION if partitioned else LEVEL_BY_ENTITY)
    values = (mesh.cell_partition if by == LEVEL_BY_PARTITION
              else mesh.cell_entity).astype(np.int32)
    stamp = digest(mesh.path) if mesh.path.is_file() else ''
    identity = MeshIdentity(mesh.path.parent, stamp, ())
    result = MeshTopology(
        identity=identity, points=np.ascontiguousarray(mesh.points),
        face_vertices=flat.astype(np.int64), face_offsets=face_offsets,
        owner=owner, neighbour=neigh_i.astype(np.int64),
        patches=tuple(patches), n_cells=mesh.n_cells,
        encodings={'native': mesh.source},
        cell_zones=tuple(zones),
        cell_zones_status='read' if zones else 'absent',
        cell_level=values, cell_level_status='read')
    for array in (result.face_vertices, result.face_offsets, result.owner,
                  result.neighbour):
        array.setflags(write=False)
    return result
