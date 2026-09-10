"""Canonical mixed-mesh conversion to VTK with stable selection arrays."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from .model import CanonicalMesh, CellType


_VTK_TYPES = {
    CellType.TETRA: 10, CellType.HEXAHEDRON: 12, CellType.PRISM: 13,
    CellType.PYRAMID: 14, CellType.TRIANGLE: 5, CellType.QUADRILATERAL: 9,
}


@dataclass(frozen=True)
class CanonicalVtkData:
    volume: object
    boundary: object


def to_vtk(mesh: CanonicalMesh, *, quality_report=None,
           point_fields: dict[str, np.ndarray] | None = None) -> CanonicalVtkData:
    try:
        import vtk
        from vtk.util.numpy_support import numpy_to_vtk
    except ImportError as error:
        raise RuntimeError('VTK is required for canonical mesh visualization') from error

    vtk_points = vtk.vtkPoints()
    vtk_points.SetData(numpy_to_vtk(mesh.points, deep=True))
    volume = vtk.vtkUnstructuredGrid()
    volume.SetPoints(vtk_points)
    canonical_ids = []
    source_ids = []
    region_ids = []
    cell_types = []
    global_id = 0
    for block in mesh.cell_blocks:
        for row in block.connectivity:
            ids = vtk.vtkIdList()
            for value in row:
                ids.InsertNextId(int(value))
            volume.InsertNextCell(_VTK_TYPES[block.cell_type], ids)
        canonical_ids.extend(range(global_id, global_id + block.count))
        global_id += block.count
        source_ids.extend(block.source_ids.tolist())
        region_ids.extend(block.region_ids.tolist())
        cell_types.extend([block.cell_type.value] * block.count)
    _add_numeric(volume, 'foammesh_canonical_cell_id', canonical_ids, numpy_to_vtk)
    _add_numeric(volume, 'foammesh_source_cell_id', source_ids, numpy_to_vtk)
    _add_numeric(volume, 'foammesh_region_id', region_ids, numpy_to_vtk)
    _add_strings(volume, 'foammesh_cell_type', cell_types, vtk)
    if quality_report is not None:
        _add_quality_fields(volume, mesh.cell_count, quality_report, numpy_to_vtk)

    boundary = vtk.vtkPolyData()
    boundary.SetPoints(vtk_points)
    polygons = vtk.vtkCellArray()
    canonical_faces = []
    source_faces = []
    patch_ids = []
    face_types = []
    global_face = 0
    for block in mesh.boundary_blocks:
        for row in block.connectivity:
            polygons.InsertNextCell(len(row))
            for value in row:
                polygons.InsertCellPoint(int(value))
        canonical_faces.extend(range(global_face, global_face + block.count))
        global_face += block.count
        source_faces.extend(block.source_ids.tolist())
        patch_ids.extend(block.patch_ids.tolist())
        face_types.extend([block.cell_type.value] * block.count)
    boundary.SetPolys(polygons)
    _add_numeric(boundary, 'foammesh_canonical_face_id', canonical_faces, numpy_to_vtk)
    _add_numeric(boundary, 'foammesh_source_face_id', source_faces, numpy_to_vtk)
    _add_numeric(boundary, 'foammesh_patch_id', patch_ids, numpy_to_vtk)
    _add_strings(boundary, 'foammesh_face_type', face_types, vtk)
    for name, values in (point_fields or {}).items():
        values = np.asarray(values)
        if values.shape != (mesh.point_count,):
            raise ValueError(f'point field {name!r} must contain one value per point')
        array = numpy_to_vtk(values, deep=True)
        array.SetName(str(name))
        volume.GetPointData().AddArray(array)
        boundary.GetPointData().AddArray(array)
    return CanonicalVtkData(volume, boundary)


def _add_numeric(dataset, name, values, converter):
    array = converter(np.asarray(values, dtype=np.int64), deep=True)
    array.SetName(name)
    dataset.GetCellData().AddArray(array)


def _add_strings(dataset, name, values, vtk):
    array = vtk.vtkStringArray()
    array.SetName(name)
    for value in values:
        array.InsertNextValue(value)
    dataset.GetCellData().AddArray(array)


def _add_quality_fields(dataset, count, report, converter):
    """Attach sparse failed-set values without inventing values for passing cells."""
    for failed_set in getattr(report, 'failed_sets', ()):
        if getattr(failed_set, 'entity_kind', None) != 'volume_cell':
            continue
        values = np.full(count, np.nan, dtype=np.float64)
        ids = np.asarray(failed_set.entity_ids, dtype=np.int64)
        if np.any(ids < 0) or np.any(ids >= count):
            raise ValueError(f'quality field {failed_set.metric!r} has invalid canonical IDs')
        values[ids] = np.asarray(failed_set.values, dtype=np.float64)
        array = converter(values, deep=True)
        array.SetName(f'foammesh_quality_{failed_set.metric}')
        dataset.GetCellData().AddArray(array)
