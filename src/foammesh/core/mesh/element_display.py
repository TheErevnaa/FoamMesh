"""What the shared viewport can show of the elements of the mesh on screen.

Plan 42 SG9 (D2, D3).  ``GmshMeshView`` is a producer: it reads flat arrays
(:class:`foammesh.core.gmsh.session_display.MeshArrays`) and draws them.  The
arrays used to come only from a Gmsh hand-over.  This module makes the same
arrays from a snappyHexMesh / OpenFOAM scene (the datasets the viewport
already draws) so every Elements control works on either mesh, and decides
which controls the mesh on screen can offer:

* **Colour by** -- Element type, Entity, Physical group, Partition on Gmsh;
  Cell type, Patch, Zone, Processor on snappy.  A choice is offered only when
  the arrays hold more than one value for it.
* **Cell types** -- the kinds the mesh has (snappy adds Polyhedra, drawn from
  their own face streams like any other cell: Plan 42 SG24, DP-1428).
* **Labels** -- the same label types under the snappy names.

No Qt.  VTK only to read the datasets.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from foammesh.core.gmsh import session_display as sd

GMSH = 'gmsh'
FOAM = 'foam'

#: The kind snappy meshes add; drawn from its ``VTK_POLYHEDRON`` face streams.
POLYHEDRA = sd.POLYHEDRA

#: Mode names by source, in ``Mesh.ColorCarousel`` order.
COLOUR_NAMES = {
    GMSH: ('Element type', 'Entity', 'Physical group', 'Partition'),
    FOAM: ('Cell type', 'Patch', 'Zone', 'Processor'),
}
LABEL_NAMES = {
    GMSH: sd.LABEL_TYPES,
    FOAM: ('Number', 'Patch', 'Zone', 'Processor', 'Coordinates'),
}

#: VTK cell type -> the Gmsh element type the producer's tables know.
_VTK_TO_GMSH = {5: 2, 9: 3, 10: 4, 12: 5, 13: 6, 14: 7, 3: 1}
#: Cell zones are coded apart from patches in the physical-group column.
ZONE_BASE = 1000


@dataclass
class ElementSource:
    """Arrays for the producer, and the facts the controls need."""

    arrays: sd.MeshArrays
    kind: str
    #: physical code -> name (patches 1.., zones ``ZONE_BASE``..).
    names: dict
    #: how many cells of each kind (Polyhedra included on snappy).
    kind_counts: dict
    #: Plan 42 SG24: the distinct colour codes of each colouring, found once
    #: (a sort of every cell) and reused by every legend.
    _codes: dict = field(default_factory=dict, repr=False, compare=False)

    def colour_codes_unique(self, mode: int) -> np.ndarray:
        if mode not in self._codes:
            self._codes[mode] = np.unique(sd.colour_codes(self.arrays, mode))
        return self._codes[mode]

    def colour_names(self) -> tuple:
        return COLOUR_NAMES[self.kind]

    def label_names(self) -> tuple:
        return LABEL_NAMES[self.kind]


# --------------------------------------------------------------------------- #
# The two sources
# --------------------------------------------------------------------------- #

def source_from_gmsh(arrays: sd.MeshArrays, names=None) -> ElementSource:
    return ElementSource(arrays, GMSH, dict(names or {}),
                         sd.kinds_present(arrays))


def _volume_columns(grid, count: int):
    """``(types, zone code, zone names, processor)`` of a volume grid."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    getTypes = getattr(grid, 'GetCellTypesArray', None)
    vtk_types = (vtk_to_numpy(getTypes()).astype(np.int64)
                 if getTypes is not None and getTypes() is not None
                 else np.array([grid.GetCellType(i) for i in range(count)]))
    data = grid.GetCellData()

    def column(*names):
        for name in names:
            array = data.GetArray(name)
            if array is not None and array.GetNumberOfComponents() == 1:
                return vtk_to_numpy(array).astype(np.int64)
        return None

    zone = column('cellZone')
    zone_names = []
    labels = grid.GetFieldData().GetAbstractArray('cellZoneNames')
    if labels is not None:
        zone_names = [labels.GetValue(i)
                      for i in range(labels.GetNumberOfValues())]
    return vtk_types, zone, zone_names, column('procId', 'processor')


def _polyhedra_of(volume, vtk_types) -> 'sd.PolyhedraFaces | None':
    """The face streams of the ``VTK_POLYHEDRON`` cells of *volume*."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    cells = np.flatnonzero(vtk_types == 42)
    getFaces = getattr(volume, 'GetPolyhedronFaces', None)
    getLocations = getattr(volume, 'GetPolyhedronFaceLocations', None)
    if not len(cells) or getFaces is None or getLocations is None:
        return None
    faces, locations = getFaces(), getLocations()
    if faces is None or locations is None:
        return None
    face_off = vtk_to_numpy(faces.GetOffsetsArray()).astype(np.int64)
    face_conn = vtk_to_numpy(faces.GetConnectivityArray()).astype(np.int64)
    loc_off = vtk_to_numpy(locations.GetOffsetsArray()).astype(np.int64)
    loc_conn = vtk_to_numpy(locations.GetConnectivityArray()).astype(np.int64)
    counts = loc_off[cells + 1] - loc_off[cells]
    first = np.repeat(loc_off[cells], counts)
    within = np.arange(int(counts.sum())) - np.repeat(
        np.cumsum(counts) - counts, counts)
    face_ids = loc_conn[first + within]
    sizes = face_off[face_ids + 1] - face_off[face_ids]
    node_first = np.repeat(face_off[face_ids], sizes)
    node_within = np.arange(int(sizes.sum())) - np.repeat(
        np.cumsum(sizes) - sizes, sizes)
    return sd.PolyhedraFaces(cells.astype(np.int64), counts, sizes,
                             face_conn[node_first + node_within])


def _fan(offsets, connectivity, sizes):
    """Polygons of more than four nodes as triangle fans (the producer draws
    triangles and quadrangles); three- and four-node faces are kept."""
    if not np.any(sizes > 4):
        return connectivity, sizes
    pieces, widths = [], []
    for first, size in zip(offsets[:-1], sizes):
        nodes = connectivity[first:first + size]
        if size <= 4:
            pieces.append(nodes)
            widths.append(size)
            continue
        for k in range(1, size - 1):
            pieces.append(np.array([nodes[0], nodes[k], nodes[k + 1]]))
            widths.append(3)
    return np.concatenate(pieces), np.array(widths, dtype=np.int64)


def source_from_foam(volume, patches) -> ElementSource:
    """Arrays from the datasets of an OpenFOAM scene.

    *volume* is the internal mesh (a ``vtkUnstructuredGrid``) or ``None`` for
    a surface-only scene; *patches* is ``{name: vtkPolyData}`` of the boundary.
    Polyhedral cells are given element type 0 and keep their face streams
    (``arrays.polyhedra``), so the producer draws them like any other cell.
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy

    point_blocks = []
    columns = {name: [] for name in (
        'gmshElementType', 'gmshEntityDim', 'gmshEntityTag', 'gmshPhysical',
        'gmshPartition')}
    names, kind_counts = {}, {kind: 0 for kind in sd.ELEMENT_KINDS}
    kind_counts[POLYHEDRA] = 0
    base = 0
    offsets, connectivity = [np.zeros(1, dtype=np.int64)], []
    done = 0
    polyhedra = None

    def add_cells(sizes, flat, element_type, dim, entity, physical, part):
        nonlocal done
        count = len(sizes)
        offsets.append(done + np.cumsum(sizes, dtype=np.int64))
        connectivity.append(flat)
        done += int(np.sum(sizes))
        for name, value in (('gmshElementType', element_type),
                            ('gmshEntityDim', dim),
                            ('gmshEntityTag', entity),
                            ('gmshPhysical', physical),
                            ('gmshPartition', part)):
            columns[name].append(np.broadcast_to(
                np.asarray(value, dtype=np.int64), (count,)).copy())

    if volume is not None and volume.GetNumberOfCells() > 0:
        count = volume.GetNumberOfCells()
        points = vtk_to_numpy(volume.GetPoints().GetData()).astype(np.float64)
        point_blocks.append(points)
        cells = volume.GetCells()
        off = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
        conn = vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
        vtk_types, zone, zone_names, processor = _volume_columns(volume, count)
        types = np.zeros(count, dtype=np.int64)
        for vtk_type, gmsh_type in _VTK_TO_GMSH.items():
            types[vtk_types == vtk_type] = gmsh_type
        for element_type in np.unique(types):
            kind = sd.kind_of_type(int(element_type)) if element_type else ''
            if kind:
                kind_counts[kind] += int(np.count_nonzero(types == element_type))
        kind_counts[POLYHEDRA] = int(np.count_nonzero(types == 0))
        polyhedra = _polyhedra_of(volume, vtk_types)
        if zone is not None:
            physical = np.where(zone >= 0, ZONE_BASE + zone, 0)
            for index, label in enumerate(zone_names):
                names[ZONE_BASE + index] = label
        else:
            physical = np.zeros(count, dtype=np.int64)
        entity = np.maximum(physical - ZONE_BASE + 1, 1)
        add_cells(np.diff(off), conn, types, 3, entity, physical,
                  processor if processor is not None else 0)
        base += len(points)
    for index, (name, poly) in enumerate((patches or {}).items(), start=1):
        if poly is None or poly.GetNumberOfPolys() < 1                 or poly.GetPoints() is None:
            continue
        points = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
        cells = poly.GetPolys()
        off = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
        conn = vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
        sizes = np.diff(off)
        conn, sizes = _fan(off, conn, sizes)
        point_blocks.append(points)
        types = np.where(sizes == 3, 2, 3)
        add_cells(sizes, conn + base, types, 2, index, index, 0)
        names[index] = str(name)
        kind_counts['Triangles'] += int(np.count_nonzero(sizes == 3))
        kind_counts['Quadrangles'] += int(np.count_nonzero(sizes == 4))
        base += len(points)
    if not connectivity:
        raise ValueError('the scene holds no elements to show')
    arrays = sd.MeshArrays(
        points=np.concatenate(point_blocks),
        offsets=np.concatenate(offsets),
        connectivity=np.concatenate(connectivity),
        columns={name: np.concatenate(parts)
                 for name, parts in columns.items()},
        polyhedra=polyhedra)
    return ElementSource(arrays, FOAM, names, kind_counts)


# --------------------------------------------------------------------------- #
# What the controls offer
# --------------------------------------------------------------------------- #

def colour_modes(source: ElementSource) -> list:
    """``[(mode index, label)]`` for the colourings this mesh can show.

    A colouring with one value everywhere says nothing, so it is not offered;
    the element type is offered whenever the mesh has any element.
    """
    names = source.colour_names()
    offered = []
    for mode in range(len(names)):
        values = source.colour_codes_unique(mode)
        if mode == 0 or len(values) > 1:
            offered.append((mode, names[mode]))
    return offered


def kinds_on_screen(source: ElementSource) -> list:
    """The kinds the mesh has, in the order the checks are drawn."""
    kinds = [kind for kind in sd.ELEMENT_KINDS if source.kind_counts.get(kind)]
    if source.kind_counts.get(POLYHEDRA):
        kinds.append(POLYHEDRA)
    return kinds


def polyhedra_note(source: ElementSource) -> str:
    """Polyhedral cells are drawn from their own face streams: say how many."""
    count = source.kind_counts.get(POLYHEDRA, 0)
    if not count:
        return ''
    return ('%d polyhedral cells, drawn face by face from their own face '
            'lists.' % count)


def legend_rows(source: ElementSource, mode: int) -> list:
    """``[(label, (r, g, b))]``: every colour in use, with no row cap."""
    arrays = source.arrays
    codes = source.colour_codes_unique(mode)
    rows = []
    for index, code in enumerate(codes):
        rgb = tuple(int(c) for c in sd.CAROUSEL[index % len(sd.CAROUSEL)])
        code = int(code)
        if source.kind == GMSH:
            rows.append((sd.colour_legend_label(arrays, mode, code,
                                                source.names), rgb))
            continue
        if mode == 0:
            label = {0: POLYHEDRA, 2: 'Triangle', 3: 'Quadrangle',
                     4: 'Tetrahedron', 5: 'Hexahedron', 6: 'Prism',
                     7: 'Pyramid'}.get(code, 'type %d' % code)
        elif mode == 1:
            dim, tag = code // 100000, code % 100000
            label = (source.names.get(tag) if dim == 2
                     else 'Volume zone %d' % tag) or 'entity %d' % tag
        elif mode == 2:
            label = source.names.get(code) or ('no zone' if not code
                                               else 'zone %d' % code)
        else:
            label = 'processor %d' % code if code else 'not decomposed'
        rows.append((label, rgb))
    return rows
