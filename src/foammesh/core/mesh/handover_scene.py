"""Draw the resident Gmsh session's hand-over (``mesh.vtu``) in the viewport.

Plan 41 GN13 (§4.2, §4.3). The session writes the mesh it holds as one VTK XML
file whose cell arrays carry Gmsh's own element tag, type, entity, physical
group and partition, and Gmsh's own quality measures (``gmshSICN``,
``gmshSIGE``, ``gmshGamma`` ...). This module turns that file into exactly the
shape ``read_msh_scene`` hands the view layer, so a mesh that exists only in
the session -- no ``.msh`` written, no polyMesh -- reaches the same actors, the
same tree, the same sections and the same quality controls.

The volume cells (dimension 3) become ``internalMesh`` and keep every array,
which is what the quality colouring, the worst-cell ranking, the histogram and
the cell inspector read. The surface cells (dimension 2) are split by physical
group into patches named by ``mesh.names.json`` (written beside the mesh).

No Qt here: a VTK reader and numpy.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .msh_scene import MshScene, MshSceneError

#: The cell arrays every hand-over carries.
IDENTITY_ARRAYS = ('gmshElementTag', 'gmshElementType', 'gmshEntityDim',
                   'gmshEntityTag', 'gmshPhysical', 'gmshPartition')

#: Quality array -> (label for the metric lists, True when the bad cells are
#: the low values). Order is the order they are offered in.
QUALITY_MEASURES = (
    ('gmshSICN', 'SICN (inverse condition number)', True),
    ('gmshSIGE', 'SIGE (gradient error)', True),
    ('gmshGamma', 'Gamma (inscribed / circumscribed radius)', True),
    ('gmshMinIsotropy', 'Isotropy', True),
    ('gmshMinDetJac', 'Smallest Jacobian determinant', True),
    ('gmshVolume', 'Element volume', True),
)
#: Further arrays carried for the inspector and the exports, not coloured.
INSPECT_ARRAYS = ('gmshMinEdge', 'gmshMaxEdge', 'gmshInnerRadius',
                  'gmshOuterRadius', 'gmshMaxDetJac')

#: The name Gmsh knows the quality arrays by (for the cell read-out).
GMSH_NAMES = {
    'gmshSICN': 'minSICN', 'gmshSIGE': 'minSIGE', 'gmshGamma': 'gamma',
    'gmshMinIsotropy': 'minIsotropy', 'gmshMinEdge': 'minEdge',
    'gmshMaxEdge': 'maxEdge', 'gmshVolume': 'volume',
    'gmshInnerRadius': 'innerRadius', 'gmshOuterRadius': 'outerRadius',
    'gmshMinDetJac': 'minDetJac', 'gmshMaxDetJac': 'maxDetJac',
}
#: Gmsh element type -> name, for the read-out.
ELEMENT_TYPE_NAMES = {1: 'line', 2: 'triangle', 3: 'quadrangle',
                      4: 'tetrahedron', 5: 'hexahedron', 6: 'prism',
                      7: 'pyramid', 15: 'point'}


def is_handover(path) -> bool:
    return Path(path).suffix.lower() == '.vtu'


def _names(path: Path) -> dict:
    try:
        text = (path.parent / 'mesh.names.json').read_text(encoding='utf-8')
        return dict(json.loads(text).get('physical') or {})
    except (OSError, ValueError):
        return {}


def read_handover_scene(path, *, surface_only: bool = False) -> MshScene:
    """Read ``mesh.vtu`` into datasets the viewport can draw."""
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkIdList
    from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
    from vtkmodules.vtkFiltersCore import vtkExtractCells
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    path = Path(path)
    if not path.is_file():
        raise MshSceneError(f'{path.name}: the session left no mesh to draw')
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    if grid is None or grid.GetNumberOfCells() < 1:
        raise MshSceneError(f'{path.name} holds no elements to display')

    cell_data = grid.GetCellData()
    dimension_array = cell_data.GetArray('gmshEntityDim')
    if dimension_array is None:
        raise MshSceneError(f'{path.name} carries no gmshEntityDim array')
    dimensions = vtk_to_numpy(dimension_array)
    physical = vtk_to_numpy(cell_data.GetArray('gmshPhysical'))
    entity = vtk_to_numpy(cell_data.GetArray('gmshEntityTag'))
    names = _names(path)

    def take(ids):
        picked = vtkIdList()
        picked.SetNumberOfIds(len(ids))
        for position, cell in enumerate(ids):
            picked.SetId(position, int(cell))
        extractor = vtkExtractCells()
        extractor.SetInputData(grid)
        extractor.SetCellList(picked)
        extractor.Update()
        from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid
        copy = vtkUnstructuredGrid()
        copy.ShallowCopy(extractor.GetOutput())
        return copy

    volume_ids = np.nonzero(dimensions == 3)[0]
    surface_ids = np.nonzero(dimensions == 2)[0]
    if not len(volume_ids) and not len(surface_ids):
        raise MshSceneError(
            f'{path.name} holds no surface or volume elements to display')
    if not surface_only and not len(volume_ids):
        # A 2D mesh, or a surface pass: drawn as the surfaces it is.
        surface_only = True

    boundary = {}
    patches = {}
    keys = {}
    for cell in surface_ids:
        tag = int(physical[cell])
        key = ('p', tag) if tag else ('e', int(entity[cell]))
        keys.setdefault(key, []).append(int(cell))
    for (kind, tag), ids in keys.items():
        name = str(names.get(f'2:{tag}', '') or '').strip() if kind == 'p' \
            else ''
        if not name:
            name = (f'surface{tag}' if kind == 'p' else f'entity{tag}')
        part = take(ids)
        geometry = vtkGeometryFilter()
        geometry.SetInputData(part)
        geometry.Update()
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        surface = vtkPolyData()
        surface.ShallowCopy(geometry.GetOutput())
        boundary[name] = surface
        patches[name] = len(ids)

    internal = None
    volumes = {}
    if not surface_only:
        internal = take(volume_ids)
        group_tags = physical[volume_ids]
        _stamp_colour_arrays(internal, cell_data, volume_ids, names)
        for tag in np.unique(group_tags):
            label = str(names.get(f'3:{int(tag)}', '') or '').strip() \
                or f'volume{int(tag)}'
            volumes[label] = int(np.count_nonzero(group_tags == tag))
    return MshScene(
        vtk_mesh={'': {'boundary': boundary, 'internalMesh': internal,
                       'zones': {'cellZones': {}, 'faceZones': {}}}},
        curves=_curves(grid, dimensions, entity, names),
        points=int(grid.GetNumberOfPoints()),
        cells=int(len(volume_ids) if not surface_only else len(surface_ids)),
        patches=patches, volumes=volumes)


def _curves(grid, dimensions, entity, names) -> dict:
    """Plan 41 GN23. The hand-over's curve entities as polylines
    (``curve<tag>``): the lines the session meshed along the model's edges."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    from .native_mesh import chain_polylines

    lines = np.nonzero(dimensions == 1)[0]
    if not len(lines):
        return {}
    cells = grid.GetCells()
    connectivity = vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
    offsets = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
    first = offsets[lines]
    segments = np.stack([connectivity[first], connectivity[first + 1]], axis=1)
    points = vtk_to_numpy(grid.GetPoints().GetData()).astype(np.float64)
    found = chain_polylines(points, segments, entity[lines])
    return {f'curve{tag}': chains for tag, chains in found.items()}


def _stamp_colour_arrays(volume, cell_data, volume_ids, names) -> None:
    """Plan 41 GN23. ``cellLevel`` and ``cellZone`` (+ ``cellZoneNames``) on
    the drawn volume, so *Refinement level* and *Region / zone* colour a Gmsh
    mesh by its partition (else entity) and its physical group -- the same
    codes `core.mesh.native_mesh.topology` gives the section worker."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkStringArray

    def column(name):
        array = cell_data.GetArray(name)
        return None if array is None else vtk_to_numpy(array)[volume_ids]

    physical = column('gmshPhysical')
    entity = column('gmshEntityTag')
    partition = column('gmshPartition')
    target = volume.GetCellData()
    if entity is not None:
        level = (partition if partition is not None and np.any(partition > 0)
                 else entity)
        array = numpy_to_vtk(np.ascontiguousarray(level, dtype=np.int32),
                             deep=True)
        array.SetName('cellLevel')
        target.AddArray(array)
    if physical is not None:
        tags = [int(tag) for tag in np.unique(physical) if tag]
        if tags:
            codes = np.full(len(physical), -1, dtype=np.int32)
            labels = vtkStringArray()
            labels.SetName('cellZoneNames')
            for index, tag in enumerate(tags):
                codes[physical == tag] = index
                labels.InsertNextValue(
                    str(names.get(f'3:{tag}', '') or '').strip()
                    or f'volume{tag}')
            array = numpy_to_vtk(codes, deep=True)
            array.SetName('cellZone')
            target.AddArray(array)
            volume.GetFieldData().AddArray(labels)
