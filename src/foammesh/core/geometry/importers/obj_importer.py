#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Headless OBJ import via VTK.

An OBJ says where its sub-surfaces are with ``g``/``o`` lines, and until
plan 30 WP6 the reader threw them away: a two-group OBJ imported as one
boundary, so neither mesher could ever name its pieces. vtkOBJReader keeps
no record of them, so the groups are read from the file text and matched to
the faces by position -- and dropped entirely if the counts disagree, since
a mis-shifted grouping is worse than none.
"""
from __future__ import annotations

from pathlib import Path

from vtkmodules.vtkCommonDataModel import vtkPolyData
from vtkmodules.vtkIOGeometry import vtkOBJReader

from .base import ImportedSurface, ImportResult

#: More groups than this and the file is a scene, not an assembly.
_MAX_GROUPS = 64


def group_names(path) -> tuple[list[str], list[int]]:
    """The ``g``/``o`` group names, and the group index of each face.

    Empty when the file has fewer than two groups. A nameless group is
    called ``group_<n>``, and a repeated name is suffixed, so the list can
    key a patch record.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    face_groups: list[int] = []
    current = -1
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        keyword, _, rest = stripped.partition(' ')
        if keyword in ('g', 'o'):
            name = rest.strip() or f'group_{len(names) + 1}'
            if name in seen:
                seen[name] += 1
                name = f'{name}_{seen[name]}'
            seen.setdefault(name, 1)
            names.append(name)
            current = len(names) - 1
        elif keyword == 'f':
            face_groups.append(current)
    if len(names) < 2 or len(names) > _MAX_GROUPS or any(g < 0 for g in face_groups):
        return [], []
    return names, face_groups


def weld_coincident_points(polydata) -> int:
    """Merge points that sit at exactly the same place; return how many went.

    DP-1158. ``vtkOBJReader`` gives a corner its own point whenever its
    ``f`` entry names a different normal or texture coordinate, so an OBJ
    written with one normal per face (``f a//n b//n c//n``) -- which most
    exporters do for a faceted model -- and a plain triangle soup both
    arrive as one disconnected triangle per face. Every check downstream
    then sees thousands of one-triangle shells: MEASURED, a 1 MB soup OBJ
    peaked at 10.3 GB in the shell-pair check and a 0.56 MB per-face-normal
    OBJ raised MemoryError under 12 GB.

    Only exactly coincident coordinates are merged, so no geometry moves,
    and no cell is added or removed, so the per-face group ids still line up
    with the faces. Point data is dropped when anything merged: a normal or
    texture coordinate that differed between the faces sharing a corner has
    no single value at the welded point, and nothing downstream reads them.
    A file with no coincident points is returned exactly as read.
    """
    import numpy as np
    from vtkmodules.util.numpy_support import (numpy_to_vtk,
                                               numpy_to_vtkIdTypeArray,
                                               vtk_to_numpy)
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray

    points = polydata.GetPoints()
    if points is None or points.GetNumberOfPoints() < 2:
        return 0
    coordinates = vtk_to_numpy(points.GetData())
    _, first, inverse = np.unique(coordinates, axis=0, return_index=True,
                                  return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    if first.size == coordinates.shape[0]:
        return 0
    # Keep the welded points in the order they first appear, so a file that
    # merges only a few points keeps its numbering otherwise.
    order = np.argsort(first, kind='stable')
    rank = np.empty_like(order)
    rank[order] = np.arange(order.size)
    remap = rank[inverse].astype(np.int64)
    welded = vtkPoints()
    welded.SetData(numpy_to_vtk(
        np.ascontiguousarray(coordinates[first[order]]), deep=True))
    # Fresh cell arrays rather than rewriting the reader's in place, which a
    # shallow copy still shares.
    for getter, setter in (('GetVerts', 'SetVerts'), ('GetLines', 'SetLines'),
                           ('GetPolys', 'SetPolys'), ('GetStrips', 'SetStrips')):
        cells = getattr(polydata, getter)()
        if cells is None or cells.GetNumberOfCells() == 0:
            continue
        offsets = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
        connectivity = remap[vtk_to_numpy(cells.GetConnectivityArray())]
        rebuilt = vtkCellArray()
        rebuilt.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                        numpy_to_vtkIdTypeArray(connectivity, deep=True))
        getattr(polydata, setter)(rebuilt)
    polydata.SetPoints(welded)
    polydata.GetPointData().Initialize()
    return int(coordinates.shape[0] - first.size)


def read_obj(path) -> ImportResult:
    path = Path(path)
    reader = vtkOBJReader()
    reader.SetFileName(str(path))
    reader.Update()
    polydata = vtkPolyData()
    polydata.ShallowCopy(reader.GetOutput())
    weld_coincident_points(polydata)
    names, face_groups = group_names(path)
    derived = False
    if names and len(face_groups) == polydata.GetNumberOfCells():
        from vtkmodules.vtkCommonCore import vtkIntArray

        from ..cad.surface_split import FACE_ID_ARRAY
        face_ids = vtkIntArray()
        face_ids.SetName(FACE_ID_ARRAY)
        face_ids.SetNumberOfTuples(len(face_groups))
        for cell_id, group in enumerate(face_groups):
            face_ids.SetValue(cell_id, int(group))
        polydata.GetCellData().RemoveArray(FACE_ID_ARRAY)
        polydata.GetCellData().AddArray(face_ids)
        derived = True
    else:
        names = []
    surface = ImportedSurface(name=path.stem, polydata=polydata,
                              source_file=str(path), solid_names=names,
                              derived_names=derived)
    return ImportResult(surfaces=[surface], source_format='obj')
