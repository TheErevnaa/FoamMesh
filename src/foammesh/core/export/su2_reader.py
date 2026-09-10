"""Read a native SU2 mesh back into VTK, so the viewport can show it.

Plan 30 WP-07 (F-08). The Gmsh run writes ``mesh.su2`` and, on the SU2 route,
that file is the mesh: no polyMesh is published for a second-order run, and the
viewport reads polyMesh. Without a reader the user's own mesh was the one thing
they could not look at, on the route that produces it.

The format is plain ASCII and elements carry their VTK type code, which is why
this reader is short: the codes are already the ones VTK uses. Nothing here
needs a window, a render pass or an SU2 install -- it is a parser that returns
a ``vtkUnstructuredGrid``.

Volume elements become the grid's cells. Marker elements are *not* appended as
cells: a surface triangle sitting in the same grid as the tetrahedra it bounds
is a duplicate the viewport would shade over the volume. The marker names and
their face counts are returned beside the grid instead, so a caller that wants
to draw patches can ask for them by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class Su2ReadError(ValueError):
    pass


#: SU2 element type code -> (VTK cell type, node count). The codes *are* VTK's,
#: which is the whole reason this file is readable without a type table of our
#: own; they are restated so a wrong count fails here rather than in VTK.
SU2_TO_VTK = {
    3: (3, 2),      # line
    5: (5, 3),      # triangle
    9: (9, 4),      # quad
    10: (10, 4),    # tetrahedron
    12: (12, 8),    # hexahedron
    13: (13, 6),    # prism / wedge
    14: (14, 5),    # pyramid
}
VOLUME_CODES = frozenset({10, 12, 13, 14})

#: The second-order codes Gmsh's SU2 writer emits for a quadratic mesh, named
#: so the refusal can say what it found. Plan 31 CP-01 (C31-02): this reader
#: implements the linear codes above and nothing else, and a code-24 element
#: used to be reported as "not an SU2 element type" -- which reads as a corrupt
#: file rather than as a mesh order this application has not qualified. The
#: same finding is why ``ELEMENT_ORDER_REFUSALS`` in
#: ``core/gmsh/plan_derivation.py`` refuses ``('su2', 2)`` at derivation: the
#: application must not write a file it cannot open. Names, not readers -- this
#: table is deliberately not a route to reading quadratic SU2.
SU2_SECOND_ORDER_CODES = {
    22: 'triangle', 23: 'quadrilateral', 24: 'tetrahedron',
    25: 'hexahedron', 26: 'prism', 27: 'pyramid',
}


@dataclass
class Su2Mesh:
    """One parsed SU2 file, before it becomes VTK."""

    dimensions: int = 3
    points: list = field(default_factory=list)
    #: ``(vtk_type, (node ids...))`` for each volume element.
    cells: list = field(default_factory=list)
    #: Marker name -> list of ``(vtk_type, (node ids...))``.
    markers: dict = field(default_factory=dict)

    @property
    def marker_counts(self) -> dict:
        return {name: len(faces) for name, faces in self.markers.items()}


def parse_su2(path) -> Su2Mesh:
    """Parse an SU2 file into points, volume cells and named markers."""
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as error:
        raise Su2ReadError(f'could not read {path}: {error}') from error

    mesh = Su2Mesh()
    expect = None            # 'element' | 'point' | 'marker'
    remaining = 0
    marker_name = ''
    seen_points = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('%'):
            continue
        head = line.split('=')[0].strip().upper()
        if head in ('NDIME', 'NELEM', 'NPOIN', 'NMARK', 'MARKER_TAG',
                    'MARKER_ELEMS'):
            value = line.split('=', 1)[1].strip() if '=' in line else ''
            if head == 'NDIME':
                mesh.dimensions = _as_int(value, 3)
            elif head == 'NELEM':
                remaining, expect = _as_int(value, 0), 'element'
            elif head == 'NPOIN':
                # Some writers put a second count on the NPOIN line.
                remaining, expect = _as_int(value.split()[0] if value else '', 0), 'point'
                seen_points = True
            elif head == 'NMARK':
                remaining, expect = 0, None
            elif head == 'MARKER_TAG':
                marker_name = value
                mesh.markers.setdefault(marker_name, [])
                remaining, expect = 0, None
            else:
                remaining, expect = _as_int(value, 0), 'marker'
            continue
        if remaining <= 0 or expect is None:
            continue
        remaining -= 1
        parts = line.split()
        if expect == 'point':
            try:
                coordinates = [float(item) for item in parts[:3]]
            except ValueError as error:
                raise Su2ReadError(
                    f'{path.name} has a point row that is not numeric: '
                    f'{line!r}') from error
            while len(coordinates) < 3:
                coordinates.append(0.0)
            mesh.points.append(tuple(coordinates[:3]))
            continue
        code = _as_int(parts[0], -1)
        entry = SU2_TO_VTK.get(code)
        if entry is None and code in SU2_SECOND_ORDER_CODES:
            raise Su2ReadError(
                f'{path.name} holds second-order elements -- type code {code}, '
                f'a quadratic {SU2_SECOND_ORDER_CODES[code]} -- and this '
                'reader implements the linear SU2 element types only, so the '
                'file cannot be displayed. The mesh itself is intact; it is '
                'this application that has not qualified second-order SU2')
        if entry is None:
            raise Su2ReadError(
                f'{path.name} holds element type code {parts[0]}, which is not '
                'an SU2 element type')
        vtk_type, size = entry
        nodes = tuple(_as_int(item, -1) for item in parts[1:1 + size])
        if len(nodes) != size or min(nodes) < 0:
            raise Su2ReadError(
                f'{path.name} has a {size}-node element with {len(nodes)} '
                f'nodes: {line!r}')
        if expect == 'marker':
            mesh.markers.setdefault(marker_name, []).append((vtk_type, nodes))
        elif vtk_type in VOLUME_CODES or mesh.dimensions == 2:
            mesh.cells.append((vtk_type, nodes))

    if not seen_points:
        raise Su2ReadError(f'{path.name} has no NPOIN section')
    if not mesh.points:
        raise Su2ReadError(f'{path.name} holds no points')
    return mesh


def _as_int(text, default: int) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def read_su2_grid(path):
    """Return the SU2 file at ``path`` as a ``vtkUnstructuredGrid``.

    Raises :class:`Su2ReadError` on a file that cannot be turned into one --
    including a mesh with no volume elements, which would render as nothing and
    is better reported than shown.
    """
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid

    mesh = parse_su2(path)
    if not mesh.cells:
        raise Su2ReadError(
            f'{Path(path).name} holds no volume elements to display')

    points = vtkPoints()
    points.SetNumberOfPoints(len(mesh.points))
    for index, (x, y, z) in enumerate(mesh.points):
        points.SetPoint(index, x, y, z)

    grid = vtkUnstructuredGrid()
    grid.SetPoints(points)
    grid.Allocate(len(mesh.cells))
    limit = len(mesh.points)
    for vtk_type, nodes in mesh.cells:
        if max(nodes) >= limit:
            raise Su2ReadError(
                f'{Path(path).name} references point {max(nodes)} but declares '
                f'{limit} points')
        ids = _id_list(nodes)
        grid.InsertNextCell(vtk_type, ids)
    return grid


def _id_list(nodes):
    from vtkmodules.vtkCommonCore import vtkIdList

    ids = vtkIdList()
    ids.SetNumberOfIds(len(nodes))
    for position, node in enumerate(nodes):
        ids.SetId(position, int(node))
    return ids
