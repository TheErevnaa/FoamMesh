"""Draw a native Gmsh mesh, without publishing it as a polyMesh first.

Plan 31 CP-02 (C31-03). MEASURED in the tier-1 strict-GUI sweep: four of ten
Gmsh runs were refused by the quality gate -- duct STL 24 of 78,500 elements
below gamma 0.1, torus STL 11 of 109,905, wedge STL 141 of 207,801, wedge STEP
135 of 206,992 -- and every one of those case directories holds the run's
``mesh.msh`` and no ``constant/polyMesh`` at all. Publication is what did not
run. The mesh was there the whole time, and every reader in the view layer
asked for a polyMesh, found none, and reported an empty case.

The same thing happens to a run that *passed*: since CP-01 a case with no
solver target publishes no polyMesh at all, so the artifact a native Gmsh
workflow produces is the ``.msh``, permanently.

So the viewport needs to read one. This module is that reader. It reuses the
MSH parser publication already uses (``core.gmsh.publish.read_msh``) rather
than adding a second parser, and turns the parsed document into exactly the
shape ``PolyMeshLoader`` hands the view layer::

    {region: {'boundary': {name: vtkPolyData},
              'internalMesh': vtkUnstructuredGrid,
              'zones': {'cellZones': {...}, 'faceZones': {...}}}}

so a native mesh reaches the same actors, the same tree, the same palettes and
the same selection as a published one.

Physical groups are what makes the boundaries selectable: an MSH surface
element carries the physical tag of the group it belongs to, and
``$PhysicalNames`` gives that tag a name. Those groups are the patches -- they
are the same groups the publisher turns into polyMesh patches -- so a boundary
can be picked, hidden and inspected independently of the volume without the
mesh ever being converted.

No Qt here, and no window: this is a parser plus a VTK conversion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .census import MSH_HIGHER_ORDER_VOLUME_TYPES


class MshSceneError(ValueError):
    """The named file could not be turned into something to draw."""


#: MSH element type -> (VTK cell type, node count) for the first-order volume
#: elements. The node orders agree between Gmsh and VTK for all four, which is
#: why no permutation table appears here.
VOLUME_TYPES = {
    4: (10, 4),     # tetrahedron
    5: (12, 8),     # hexahedron
    6: (13, 6),     # prism / wedge
    7: (14, 5),     # pyramid
}
#: The surface elements that become boundary faces.
SURFACE_TYPES = {
    2: (5, 3),      # triangle
    3: (9, 4),      # quadrilateral
}
#: Volume types above order 1, mapped to their order, named so a refusal can
#: say what it found rather than reporting an empty mesh. The publisher's
#: ``build_canonical`` keeps the same table for the same reason (Plan 30 F-36,
#: widened past order 2 by Plan 31 FC-D).
QUADRATIC_VOLUME_TYPES = MSH_HIGHER_ORDER_VOLUME_TYPES

#: Physical-group dimensions.
_SURFACE_DIMENSION = 2
_VOLUME_DIMENSION = 3


@dataclass(frozen=True)
class MshScene:
    """One native ``.msh``, ready for the view layer."""

    #: The ``PolyMeshLoader``-shaped dictionary the view layer consumes.
    vtk_mesh: dict = field(default_factory=dict)
    points: int = 0
    cells: int = 0
    #: Boundary group name -> face count, in file order.
    patches: dict = field(default_factory=dict)
    #: Volume group name -> cell count.
    volumes: dict = field(default_factory=dict)

    @property
    def patch_names(self) -> tuple:
        return tuple(self.patches)


def _group_name(names: dict, dimension: int, tag: int) -> str:
    """The name of a physical group, or a stable label made from its tag.

    An unnamed group is still a group: Gmsh writes the tag whether or not
    ``$PhysicalNames`` names it, and a surface with no name is still a
    boundary the user has to be able to pick. Naming it after its tag is what
    the publisher does too, so the same face carries the same label on both
    routes.
    """
    name = str(names.get((dimension, tag), '') or '').strip()
    if name:
        return name
    prefix = 'volume' if dimension == _VOLUME_DIMENSION else 'surface'
    return f'{prefix}{int(tag)}'


def _collect(document):
    """Sort the parsed elements into volume and surface groups.

    One pass over the element list, because that list is the expensive thing
    in a 200,000-element mesh and the file has already been read once.
    """
    lookup = document.node_index
    volume: dict[int, list] = {}
    volume_tags: list[int] = []
    volume_types: list[int] = []
    surfaces: dict[int, dict[int, list]] = {}
    quadratic = 0
    higher_orders: set[int] = set()

    for element_type, physical, nodes in document.elements:
        if element_type in VOLUME_TYPES:
            vtk_type, size = VOLUME_TYPES[element_type]
            try:
                row = [lookup[node] for node in nodes[:size]]
            except KeyError as error:
                raise MshSceneError(
                    f'an element references undefined node {error}') from error
            volume.setdefault(vtk_type, []).append(row)
            volume_types.append(vtk_type)
            volume_tags.append(int(physical))
        elif element_type in SURFACE_TYPES:
            vtk_type, size = SURFACE_TYPES[element_type]
            try:
                row = [lookup[node] for node in nodes[:size]]
            except KeyError as error:
                raise MshSceneError(
                    f'a face references undefined node {error}') from error
            surfaces.setdefault(int(physical), {}).setdefault(
                vtk_type, []).append(row)
        elif element_type in QUADRATIC_VOLUME_TYPES:
            quadratic += 1
            higher_orders.add(QUADRATIC_VOLUME_TYPES[element_type])

    return volume, volume_tags, surfaces, quadratic, sorted(higher_orders)


def _vtk_points(coordinates):
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints

    points = vtkPoints()
    points.SetData(numpy_to_vtk(
        np.ascontiguousarray(coordinates, dtype=np.float64), deep=True))
    return points


def _cell_array(rows_by_type):
    """A ``vtkCellArray`` plus its per-cell type codes, built without a loop.

    ``InsertNextCell`` per element costs seconds on the meshes this product
    produces (the wedge STL run in the sweep held 207,801 elements), so the
    connectivity is assembled in numpy and handed over once.
    """
    from vtkmodules.util.numpy_support import (
        numpy_to_vtk, numpy_to_vtkIdTypeArray,
    )
    from vtkmodules.util.vtkConstants import VTK_UNSIGNED_CHAR
    from vtkmodules.vtkCommonCore import vtkIdTypeArray
    from vtkmodules.vtkCommonDataModel import vtkCellArray

    connectivity: list = []
    offsets: list = []
    types: list = []
    written = 0
    for vtk_type, rows in rows_by_type.items():
        block = np.asarray(rows, dtype=np.int64)
        count, width = block.shape
        connectivity.append(block.reshape(-1))
        offsets.append(written + np.arange(count, dtype=np.int64) * width)
        written += count * width
        types.append(np.full(count, vtk_type, dtype=np.uint8))

    # vtkIdType is 64-bit in every build this application ships against; the
    # width is asked rather than assumed because `numpy_to_vtkIdTypeArray`
    # refuses a mismatch outright.
    id_dtype = (np.int64 if vtkIdTypeArray().GetDataTypeSize() == 8
                else np.int32)
    flat = np.ascontiguousarray(np.concatenate(connectivity), dtype=id_dtype)
    starts = np.ascontiguousarray(
        np.concatenate(offsets + [np.asarray([written], dtype=np.int64)]),
        dtype=id_dtype)

    cells = vtkCellArray()
    # Deep copies: the numpy buffers go out of scope with this function and a
    # shallow array would leave VTK reading freed memory.
    cells.SetData(numpy_to_vtkIdTypeArray(starts, deep=True),
                  numpy_to_vtkIdTypeArray(flat, deep=True))
    codes = numpy_to_vtk(np.concatenate(types), deep=True,
                         array_type=VTK_UNSIGNED_CHAR)
    return cells, codes


def _grid(points, rows_by_type):
    from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid

    grid = vtkUnstructuredGrid()
    grid.SetPoints(points)
    cells, codes = _cell_array(rows_by_type)
    grid.SetCells(codes, cells)
    return grid


def _poly_data(points, rows_by_type):
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    polys, _codes = _cell_array(rows_by_type)
    surface = vtkPolyData()
    surface.SetPoints(points)
    surface.SetPolys(polys)
    return surface


def read_msh_scene(path) -> MshScene:
    """Read a native Gmsh ``.msh`` into datasets the viewport can draw.

    Raises :class:`MshSceneError` with the actual reason on a file that cannot
    be drawn -- a version this parser does not implement, a second-order mesh,
    a surface mesh with no volume. A viewport that shows nothing and says
    nothing is the defect this replaces (R95/R156), so every refusal here
    carries a sentence a user can act on.
    """
    from foammesh.core.gmsh.publish import PublishError, read_msh

    path = Path(path)
    try:
        document = read_msh(path)
    except PublishError as error:
        raise MshSceneError(f'{path.name}: {error}') from error

    volume, volume_tags, surfaces, quadratic, higher_orders = _collect(document)
    if not volume:
        if quadratic:
            plural = '' if quadratic == 1 else 's'
            if higher_orders == [2]:
                described = f'second-order volume element{plural}'
            else:
                named = ' and '.join(str(order) for order in higher_orders)
                described = (f'volume element{plural} at element order {named}')
            raise MshSceneError(
                f'{path.name} holds {quadratic} {described}; this viewport '
                'draws first-order meshes')
        raise MshSceneError(
            f'{path.name} holds no volume elements to display')

    points = _vtk_points(document.points)
    names = document.physical_names
    internal = _grid(points, volume)

    boundary = {}
    patches = {}
    for tag, rows_by_type in surfaces.items():
        name = _group_name(names, _SURFACE_DIMENSION, tag)
        boundary[name] = _poly_data(points, rows_by_type)
        patches[name] = sum(len(rows) for rows in rows_by_type.values())

    tags = np.asarray(volume_tags, dtype=np.int64)
    volumes = {
        _group_name(names, _VOLUME_DIMENSION, int(tag)):
            int(np.count_nonzero(tags == tag))
        for tag in np.unique(tags)
    }

    zones = {'cellZones': {}, 'faceZones': {}}
    if len(volumes) > 1:
        # Only when the mesh really has several volume groups. A single group
        # *is* the internal mesh, and building a second grid over the same
        # cells doubles the memory of a 200,000-cell draw to say nothing new.
        # The published polyMesh writes exactly these as cellZones, so a
        # native draw and a published draw show the same parts.
        zones['cellZones'] = _volume_zones(points, document, tags)

    return MshScene(
        vtk_mesh={'': {'boundary': boundary, 'internalMesh': internal,
                       'zones': zones}},
        points=int(len(document.points)),
        cells=int(len(volume_tags)),
        patches=patches, volumes=volumes)


def _volume_zones(points, document, tags):
    """One grid per named volume group, in the order the tags appear."""
    lookup = document.node_index
    per_tag: dict[int, dict[int, list]] = {}
    for element_type, physical, nodes in document.elements:
        if element_type not in VOLUME_TYPES:
            continue
        vtk_type, size = VOLUME_TYPES[element_type]
        per_tag.setdefault(int(physical), {}).setdefault(vtk_type, []).append(
            [lookup[node] for node in nodes[:size]])

    zones = {}
    for tag in np.unique(tags):
        rows_by_type = per_tag.get(int(tag))
        if not rows_by_type:
            continue
        name = _group_name(document.physical_names, _VOLUME_DIMENSION,
                           int(tag))
        zones[name] = _grid(points, rows_by_type)
    return zones
