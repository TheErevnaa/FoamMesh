#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Split a tessellated CAD tri-surface into per-CAD-face sub-surfaces.

Tessellation tags every triangle with a ``cadFaceId`` cell array. This splits the
polydata by that id so each CAD face becomes its own surface (named from the
CadModel), which downstream becomes a boundary patch. Pure VTK (headless, tested);
no OCCT needed — the input polydata can come from anywhere.
"""
from __future__ import annotations

from vtkmodules.vtkCommonDataModel import vtkDataObject
from vtkmodules.vtkFiltersCore import vtkThreshold
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

FACE_ID_ARRAY = 'cadFaceId'


def face_id_range(polydata) -> tuple[int, int] | None:
    arr = polydata.GetCellData().GetArray(FACE_ID_ARRAY)
    if arr is None:
        return None
    lo, hi = arr.GetRange()
    return int(lo), int(hi)


def split_by_face_id(polydata, names: dict[int, str] | None = None) -> list[tuple[str, object]]:
    """Return [(name, polydata)] — one entry per distinct cadFaceId."""
    return [(name, piece) for _fid, name, piece in split_by_face_id_indexed(polydata, names)]


def split_by_face_id_indexed(polydata, names: dict[int, str] | None = None
                             ) -> list[tuple[int, str, object]]:
    """Return [(face_id, name, polydata)] — one entry per distinct cadFaceId.

    The id is what lets a caller put a face back with the body it came from;
    names alone cannot, because every body numbers its faces from zero.
    """
    names = names or {}
    rng = face_id_range(polydata)
    if rng is None:
        # no tags -> single surface
        return [(0, names.get(0, 'face0'), polydata)]

    lo, hi = rng
    out = []
    for fid in range(lo, hi + 1):
        threshold = vtkThreshold()
        threshold.SetInputData(polydata)
        threshold.SetLowerThreshold(fid - 0.5)
        threshold.SetUpperThreshold(fid + 0.5)
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
        threshold.SetInputArrayToProcess(
            0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, FACE_ID_ARRAY)
        threshold.Update()
        if threshold.GetOutput().GetNumberOfCells() == 0:
            continue
        geom = vtkGeometryFilter()
        geom.SetInputData(threshold.GetOutput())
        geom.Update()
        out.append((fid, names.get(fid, f'face{fid}'), geom.GetOutput()))
    return out
