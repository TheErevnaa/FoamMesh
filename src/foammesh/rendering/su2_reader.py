"""SU2 mesh reader for the viewport (Plan 48 GM10).

The Gmsh route writes ``.su2`` (R10). Until now an SU2-only export could not
be shown or inspected: this reads the file and builds VTK data from it, the
volume as one unstructured grid and each marker as another, so the viewport
can draw the mesh, its boundary markers and the usual inspections (quality,
sections) on it.

The format (SU2 v8): ``NDIME= 2|3``, ``NELEM= n`` followed by one line per
element ``<vtk type> <nodes...> <index>``, ``NPOIN= n [n_domain]`` followed
by ``x y [z] [index]``, ``NMARK= m`` and, per marker, ``MARKER_TAG= name``,
``MARKER_ELEMS= k`` and ``k`` lines ``<vtk type> <nodes...>``. Lines starting
with ``%`` are comments. The element codes are VTK's, so no mapping is
needed: line 3, triangle 5, quadrilateral 9, tetrahedron 10, hexahedron 12,
prism 13, pyramid 14.

Parsing needs only numpy; ``to_vtk`` imports VTK when it is called.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: VTK cell type -> number of nodes (the SU2 element codes).
NODES_PER_TYPE = {3: 2, 5: 3, 9: 4, 10: 4, 12: 8, 13: 6, 14: 5}
TYPE_NAMES = {3: 'line', 5: 'triangle', 9: 'quadrilateral', 10: 'tetrahedron',
              12: 'hexahedron', 13: 'prism', 14: 'pyramid'}
VOLUME_TYPES = {2: (5, 9), 3: (10, 12, 13, 14)}


class Su2Error(ValueError):
    """The file is not a readable SU2 mesh, with the line that shows it."""


@dataclass
class Cells:
    """Cells as VTK's offsets / connectivity pair, plus the type of each."""

    types: np.ndarray
    offsets: np.ndarray            # length n + 1
    connectivity: np.ndarray

    @property
    def count(self) -> int:
        return int(self.types.size)

    def census(self) -> dict:
        kinds, counts = np.unique(self.types, return_counts=True)
        return {TYPE_NAMES.get(int(k), str(int(k))): int(c)
                for k, c in zip(kinds, counts)}


@dataclass
class Su2Mesh:
    dimension: int
    points: np.ndarray             # N x 3 (z is 0 for a 2D mesh)
    cells: Cells
    markers: dict = field(default_factory=dict)    # name -> Cells
    path: str = ''
    periodic: int = 0

    def summary(self) -> dict:
        return {
            'dimension': self.dimension, 'points': int(len(self.points)),
            'cells': self.cells.count, 'cell_types': self.cells.census(),
            'markers': {name: {'elements': cells.count,
                               'types': cells.census()}
                        for name, cells in self.markers.items()},
            'bounds': (self.points.min(axis=0).tolist()
                       + self.points.max(axis=0).tolist())
            if len(self.points) else [],
            'periodic_pairs': self.periodic,
        }


def _cells(rows: list, where: str) -> Cells:
    types = np.empty(len(rows), dtype=np.uint8)
    offsets = np.zeros(len(rows) + 1, dtype=np.int64)
    flat = []
    for number, tokens in enumerate(rows):
        try:
            kind = int(tokens[0])
            size = NODES_PER_TYPE[kind]
        except (ValueError, KeyError):
            raise Su2Error(f'{where}: element {number + 1} has the type '
                           f'code {tokens[0]!r}, which SU2 does not use') \
                from None
        if len(tokens) < 1 + size:
            raise Su2Error(f'{where}: element {number + 1} has '
                           f'{len(tokens) - 1} nodes, a '
                           f'{TYPE_NAMES[kind]} needs {size}')
        flat.extend(tokens[1:1 + size])
        types[number] = kind
        offsets[number + 1] = offsets[number] + size
    connectivity = np.asarray(flat, dtype=np.int64)
    return Cells(types, offsets, connectivity)


def read_su2(path) -> Su2Mesh:
    """Read an SU2 mesh file."""
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as error:
        raise Su2Error(f'{path} cannot be read: {error}') from error
    lines = [line.split('%', 1)[0].strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    dimension = None
    cells = None
    points = None
    markers: dict = {}
    periodic = 0
    index = 0
    total = len(lines)

    def keyword(line):
        if '=' not in line:
            return None, None
        key, value = line.split('=', 1)
        return key.strip().upper(), value.strip()

    while index < total:
        key, value = keyword(lines[index])
        index += 1
        if key is None:
            raise Su2Error(f'{path.name}: line "{lines[index - 1][:40]}" is '
                           'not a keyword of the format')
        if key == 'NDIME':
            dimension = int(value)
            if dimension not in (2, 3):
                raise Su2Error(f'NDIME= {value}: SU2 meshes are 2 or 3 '
                               'dimensional')
        elif key == 'NELEM':
            count = int(value.split()[0])
            rows = [lines[index + i].split() for i in range(count)]
            index += count
            cells = _cells(rows, 'NELEM')
        elif key == 'NPOIN':
            count = int(value.split()[0])
            if dimension is None:
                raise Su2Error('NPOIN= comes before NDIME=')
            block = np.array([lines[index + i].split()[:dimension]
                              for i in range(count)], dtype=float)
            index += count
            if dimension == 2:
                block = np.hstack([block, np.zeros((count, 1))])
            points = block
        elif key == 'NMARK':
            for _ in range(int(value)):
                tag_key, tag = keyword(lines[index])
                if tag_key != 'MARKER_TAG':
                    raise Su2Error(f'expected MARKER_TAG=, found '
                                   f'"{lines[index][:40]}"')
                count_key, count = keyword(lines[index + 1])
                if count_key != 'MARKER_ELEMS':
                    raise Su2Error(f'marker {tag}: MARKER_ELEMS= is missing')
                count = int(count)
                rows = [lines[index + 2 + i].split() for i in range(count)]
                index += 2 + count
                markers[tag] = _cells(rows, f'marker {tag}')
        elif key == 'NPERIODIC':
            periodic = int(value)
            # each pair: PERIODIC_INDEX= n, then 3 lines of transformation
            index += periodic * 4
        # other keywords (IZONE, NZONE, ...) carry no geometry
    if dimension is None or cells is None or points is None:
        raise Su2Error(f'{path.name}: NDIME=, NELEM= and NPOIN= are all '
                       'needed')
    largest = int(cells.connectivity.max()) if cells.connectivity.size else -1
    if largest >= len(points):
        raise Su2Error(f'an element uses point {largest}, but there are '
                       f'only {len(points)} points')
    for tag, group in markers.items():
        if group.connectivity.size and \
                int(group.connectivity.max()) >= len(points):
            raise Su2Error(f'marker {tag} uses a point that does not exist')
    return Su2Mesh(dimension, points, cells, markers, str(path), periodic)


def _grid(points: np.ndarray, cells: Cells):
    import vtk
    from vtkmodules.util import numpy_support

    grid = vtk.vtkUnstructuredGrid()
    coords = vtk.vtkPoints()
    coords.SetData(numpy_support.numpy_to_vtk(
        np.ascontiguousarray(points, dtype=np.float64), deep=True))
    grid.SetPoints(coords)
    array = vtk.vtkCellArray()
    array.SetData(
        numpy_support.numpy_to_vtkIdTypeArray(cells.offsets, deep=True),
        numpy_support.numpy_to_vtkIdTypeArray(cells.connectivity, deep=True))
    grid.SetCells(numpy_support.numpy_to_vtk(
        cells.types, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR), array)
    return grid


def to_vtk(mesh: Su2Mesh) -> dict:
    """``{'volume': vtkUnstructuredGrid, 'markers': {name: vtkUnstructuredGrid}}``.

    Every marker grid shares the volume's points, so a node index means the
    same thing in all of them. Each marker's grid carries a cell array
    ``marker`` (its index in the file) for colouring.
    """
    import vtk
    from vtkmodules.util import numpy_support

    volume = _grid(mesh.points, mesh.cells)
    grids = {}
    for number, (name, cells) in enumerate(mesh.markers.items()):
        grid = _grid(mesh.points, cells)
        ids = numpy_support.numpy_to_vtk(
            np.full(cells.count, number, dtype=np.int32), deep=True,
            array_type=vtk.VTK_INT)
        ids.SetName('marker')
        grid.GetCellData().AddArray(ids)
        grids[name] = grid
    return {'volume': volume, 'markers': grids}


def load_su2(path) -> dict:
    """Read *path* and build the VTK data, with the summary the Statistics
    panel and the local API report."""
    mesh = read_su2(path)
    built = to_vtk(mesh)
    built['summary'] = mesh.summary()
    built['mesh'] = mesh
    return built


def boundary_patches(mesh: Su2Mesh) -> list:
    """What the viewport lists as boundary parts: marker name and its
    element count, in file order."""
    return [(name, cells.count) for name, cells in mesh.markers.items()]
