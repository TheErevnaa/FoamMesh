"""Read one element of a Gmsh session mesh: tag, type, nodes, entity, quality.

Plan 41 GN13 (A-13, I-02). Gmsh's "Inspect element" lists an element's tag,
type, nodes and measures. The hand-over carries all of it as cell arrays
(``gmsh*``) and point data (``gmshNodeTag``), so the answer comes from the
dataset the element was drawn from -- no second trip to the session.

No Qt: numpy and the VTK data model.
"""
from __future__ import annotations

import numpy as np

from .handover_scene import ELEMENT_TYPE_NAMES, GMSH_NAMES

TAG_ARRAY = 'gmshElementTag'


def has_identity(dataSet) -> bool:
    """Whether *dataSet* is a piece of a session mesh (its cells carry tags)."""
    return bool(dataSet is not None
                and dataSet.GetCellData().GetArray(TAG_ARRAY) is not None)


def _value(array, cell):
    if array is None:
        return None
    return array.GetTuple1(int(cell))


def describe_cell(dataSet, cell: int) -> dict:
    """Everything the hand-over knows about cell *cell* of *dataSet*.

    Empty when the dataset carries no element tags (a polyMesh, a plain
    ``.msh`` scene).
    """
    if not has_identity(dataSet) or not (
            0 <= int(cell) < dataSet.GetNumberOfCells()):
        return {}
    data = dataSet.GetCellData()
    type_code = int(_value(data.GetArray('gmshElementType'), cell) or 0)
    measures = {}
    for array_name, gmsh_name in GMSH_NAMES.items():
        value = _value(data.GetArray(array_name), cell)
        if value is not None:
            measures[gmsh_name] = float(value)
    ids = _point_ids(dataSet, cell)
    node_array = dataSet.GetPointData().GetArray('gmshNodeTag')
    nodes = ([int(node_array.GetTuple1(i)) for i in ids]
             if node_array is not None else [])
    coordinates = (np.array([dataSet.GetPoint(i) for i in ids])
                   if ids else np.zeros((0, 3)))
    centroid = coordinates.mean(axis=0) if len(coordinates) else np.zeros(3)
    return {
        'tag': int(_value(data.GetArray(TAG_ARRAY), cell)),
        'cell': int(cell),
        'type': ELEMENT_TYPE_NAMES.get(type_code, f'type {type_code}'),
        'typeCode': type_code,
        'dimension': int(_value(data.GetArray('gmshEntityDim'), cell) or 0),
        'entity': int(_value(data.GetArray('gmshEntityTag'), cell) or 0),
        'physical': int(_value(data.GetArray('gmshPhysical'), cell) or 0),
        'partition': int(_value(data.GetArray('gmshPartition'), cell) or 0),
        'nodes': nodes,
        'centroid': [float(x) for x in centroid],
        'size': (float(np.ptp(coordinates, axis=0).max())
                 if len(coordinates) else 0.0),
        'measures': measures,
    }


def _point_ids(dataSet, cell) -> list:
    from vtkmodules.vtkCommonCore import vtkIdList

    points = vtkIdList()
    dataSet.GetCellPoints(int(cell), points)
    return [points.GetId(i) for i in range(points.GetNumberOfIds())]


def find_cell(dataSets, tag: int):
    """``(dataset, cell)`` holding element *tag* in the first of *dataSets*
    that has it, or ``(None, None)``. Gmsh element tags are unique over the
    whole mesh, so the first hit is the element."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    for dataSet in dataSets:
        if not has_identity(dataSet):
            continue
        tags = vtk_to_numpy(dataSet.GetCellData().GetArray(TAG_ARRAY))
        found = np.flatnonzero(tags == int(tag))
        if len(found):
            return dataSet, int(found[0])
    return None, None


def lines(description: dict) -> list:
    """The read-out, as ``(label, text)`` rows."""
    if not description:
        return []
    rows = [
        ('Element', f'{description["tag"]} ({description["type"]})'),
        ('Entity', f'dimension {description["dimension"]}, '
                   f'tag {description["entity"]}'),
        ('Physical group', str(description['physical'] or 'none')),
        ('Partition', str(description['partition'] or 'none')),
        ('Nodes', ', '.join(str(n) for n in description['nodes']) or '-'),
    ]
    for name, value in description['measures'].items():
        rows.append((name, f'{value:.6g}'))
    return rows
