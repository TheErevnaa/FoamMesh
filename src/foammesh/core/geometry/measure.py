#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Plain measurements of a surface: how many facets, how big, and where.

R45. The Edit Surface dialog carried a Name field and a Type radio group and
nothing else, so after a feature-angle split -- which delivers surfaces called
``tee_1``..``tee_5`` -- the only way to tell the inlet from the top outlet was
to read the per-solid bounds out of ``foammesh/geometry/<id>/rev2.stl`` from
outside the application. These are the three facts that settle it, computed
from the polydata the dialog already has in hand.
"""
from __future__ import annotations


def surface_measurements(polydata) -> dict | None:
    """Facet count, surface area and bounding box of *polydata*.

    Returns ``None`` for anything that is not a vtkPolyData with cells, so a
    caller can simply hide the readout rather than guard every call.
    """
    try:
        facets = int(polydata.GetNumberOfCells())
        bounds = tuple(float(value) for value in polydata.GetBounds())
    except Exception:  # noqa: BLE001 - not a polydata; there is nothing to show
        return None

    if facets <= 0 or len(bounds) != 6:
        return None

    return {'facets': facets,
            'area': _area(polydata),
            'bounds': bounds}


def _area(polydata) -> float:
    """Summed cell area, or 0.0 when VTK cannot supply one."""
    try:
        from vtkmodules.vtkFiltersVerdict import vtkCellSizeFilter

        sizes = vtkCellSizeFilter()
        sizes.SetInputData(polydata)
        sizes.ComputeSumOn()
        sizes.Update()
        output = sizes.GetOutput()
        array = output.GetCellData().GetArray('Area')
        if array is None:
            return 0.0
        return float(sum(array.GetValue(i)
                         for i in range(array.GetNumberOfTuples())))
    except Exception:  # noqa: BLE001 - measurement is a courtesy, never fatal
        return 0.0


def format_measurements(measurements: dict | None) -> str:
    """One line of text a dialog can show verbatim.

    Deliberately spells the bounding box out per axis: the whole point is to
    let the user say "this one is the flat face at z=0", which a volume figure
    would not answer.
    """
    if not measurements:
        return ''

    bounds = measurements['bounds']
    box = ', '.join(
        '{0}: {1:.4g} to {2:.4g}'.format(axis, bounds[2 * i], bounds[2 * i + 1])
        for i, axis in enumerate(('X', 'Y', 'Z')))
    return '{0} facets, area {1:.4g}, {2}'.format(
        measurements['facets'], measurements['area'], box)
