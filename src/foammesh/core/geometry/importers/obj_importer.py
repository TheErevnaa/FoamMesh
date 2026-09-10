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


def read_obj(path) -> ImportResult:
    path = Path(path)
    reader = vtkOBJReader()
    reader.SetFileName(str(path))
    reader.Update()
    polydata = reader.GetOutput()
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
