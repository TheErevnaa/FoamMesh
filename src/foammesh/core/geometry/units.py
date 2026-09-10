#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Length units and the import unit wizard heuristic.

OpenFOAM works in SI metres. STL files carry no units, so FoamMesh must let the
user declare the import unit (and warn when the geometry looks wrong for SI).
"""
from __future__ import annotations

from dataclasses import dataclass

# conversion factor: 1 <unit> = N metres
UNIT_TO_M: dict[str, float] = {
    'm': 1.0,
    'cm': 0.01,
    'mm': 0.001,
    'um': 1e-6,
    'inch': 0.0254,
    'in': 0.0254,
    'ft': 0.3048,
}


def si_factor(unit: str) -> float:
    try:
        return UNIT_TO_M[unit.lower()]
    except KeyError:
        raise ValueError(f'unknown length unit: {unit!r}')


def convert_length(value: float, from_unit: str, to_unit: str = 'm') -> float:
    return value * si_factor(from_unit) / si_factor(to_unit)


def scale_polydata(polydata, factor: float):
    """Scale a surface about the origin by ``factor``.

    R207. Split out of `to_metres` because the validation reference knows its
    conversion as a number rather than a unit name: it measures the surface it
    just tessellated against the bounding box the prepared manifest recorded.
    Both callers must scale identically, so there is one filter, here.
    """
    if not factor or factor == 1.0:
        return polydata

    from vtkmodules.vtkCommonTransforms import vtkTransform
    from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter

    transform = vtkTransform()
    transform.Scale(factor, factor, factor)
    scaler = vtkTransformPolyDataFilter()
    scaler.SetInputData(polydata)
    scaler.SetTransform(transform)
    scaler.Update()
    # The filter keeps cell data, so the cadFaceId tags that give snappy its
    # per-face regions survive the conversion.
    return scaler.GetOutput()


def to_metres(polydata, unit: str | None):
    """Scale a surface from its declared unit into metres.

    One implementation, used by both the artifact store and the viewport's own
    copy of the same surface. They must agree exactly: the store's artifact is
    what gets meshed and the view's polydata is what the user measures on
    screen, and a case where those two disagree about scale is worse than one
    that is uniformly wrong.

    An unknown unit returns the geometry untouched rather than guessing.
    """
    try:
        factor = si_factor(unit) if unit else 1.0
    except (KeyError, ValueError):
        return polydata
    return scale_polydata(polydata, factor)


@dataclass
class UnitSuggestion:
    unit: str
    confident: bool
    message: str


def suggest_unit(raw_diagonal: float) -> UnitSuggestion:
    """Guess the likely import unit from the raw bounding-box diagonal.

    Heuristic for typical engineering parts (their real size is ~0.01-10 m):
    interpret the raw number so the part lands in that band. e.g. a diagonal of
    500 is most likely millimetres (0.5 m); 0.5 is likely metres.
    """
    d = abs(raw_diagonal)
    if d == 0:
        return UnitSuggestion('m', False, 'Degenerate geometry (zero size).')
    if 0.01 <= d <= 100:
        return UnitSuggestion('m', True,
                              f'Diagonal {d:g} looks like metres (SI).')
    if 10 <= d <= 100_000:
        return UnitSuggestion('mm', d > 100,
                              f'Diagonal {d:g} looks like millimetres (~{d/1000:g} m).')
    if d < 0.01:
        return UnitSuggestion('mm', False,
                              f'Diagonal {d:g} is very small for metres — check units.')
    return UnitSuggestion('mm', False,
                          f'Diagonal {d:g} is very large — assuming mm; verify units.')


def scale_warning(diagonal_m: float) -> str | None:
    """Return a warning if a *post-conversion* (SI) diagonal looks implausible."""
    if diagonal_m <= 0:
        return 'Geometry has zero/negative size.'
    if diagonal_m < 1e-3:
        return f'Geometry is very small ({diagonal_m:g} m) — check the import unit.'
    if diagonal_m > 1e4:
        return f'Geometry is very large ({diagonal_m:g} m) — check the import unit.'
    return None
