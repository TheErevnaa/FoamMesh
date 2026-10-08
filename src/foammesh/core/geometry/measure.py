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


def union_bounds(boxes) -> tuple | None:
    """The box around every ``(x0, x1, y0, y1, z0, z1)`` in *boxes*.

    Empty, inverted and non-finite boxes are left out, so a caller can pass
    what its readers returned without filtering; ``None`` when none is left.
    """
    import math

    found = None
    for box in boxes or ():
        try:
            box = tuple(float(value) for value in box)
        except (TypeError, ValueError):
            continue
        if len(box) != 6 or not all(math.isfinite(value) for value in box):
            continue
        if box[0] > box[1] or box[2] > box[3] or box[4] > box[5]:
            continue
        if found is None:
            found = list(box)
            continue
        for axis in range(3):
            found[2 * axis] = min(found[2 * axis], box[2 * axis])
            found[2 * axis + 1] = max(found[2 * axis + 1], box[2 * axis + 1])
    return tuple(found) if found is not None else None


def format_size(bounds, scale: float = 1.0) -> str:
    """"300 × 50 × 50 mm, diagonal 308.2 mm" -- a whole model's size in one line.

    DP-1182. Only a per-surface X/Y/Z range was ever on screen (the Edit
    Surface dialog's line above), and the import dialog said only which
    unit the diagonal suggested; nothing said how big the whole model was.
    *scale* converts the numbers in *bounds* to metres -- the import unit's
    factor before import, 1.0 for what the case already holds -- and the
    unit shown is the one the window's own ladder picks for the result
    (``foammesh.core.quantities``), so it reads like every other length.
    """
    import math

    from foammesh.core.quantities import format_group

    box = union_bounds([bounds]) if bounds is not None else None
    if box is None:
        return ''
    sizes = [(box[2 * axis + 1] - box[2 * axis]) * float(scale)
             for axis in range(3)]
    if max(sizes) <= 0:
        return ''
    diagonal = math.sqrt(sum(size * size for size in sizes))
    return '{0}, diagonal {1}'.format(format_group(sizes),
                                      format_group([diagonal]))
