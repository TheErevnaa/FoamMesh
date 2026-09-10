"""Pure engineering helpers for background sizing and boundary layers."""
from __future__ import annotations

import math


def derive_background_counts(bounds, target_cell_size: float) -> tuple[int, int, int]:
    if len(bounds) != 6 or target_cell_size <= 0 or not math.isfinite(target_cell_size):
        raise ValueError('six bounds and a positive finite target cell size are required')
    lengths = (bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4])
    if any(length <= 0 or not math.isfinite(length) for length in lengths):
        raise ValueError('bounds must have positive finite extents')
    return tuple(max(2, math.ceil(length / target_cell_size)) for length in lengths)


def stand_off_bounds(bounds, standoff: float):
    """The block the base grid derives from a geometry extent (R167/R175).

    The standoff is a fraction of the geometry's largest span and goes on all
    six faces, so a thin geometry gets a real standoff on its thin axis too
    rather than a fraction of nothing. One definition, because two pages read
    this block and a second copy is a second answer: the Base Grid page draws
    it, and Castellation divides it by the cell counts to say what a
    refinement level costs.
    """
    values = [float(value) for value in bounds]
    if len(values) != 6:
        raise ValueError('six bounds are required')
    spans = (values[1] - values[0], values[3] - values[2], values[5] - values[4])
    margin = float(standoff or 0.0) * max(spans + (0.0,))
    if not math.isfinite(margin) or margin <= 0:
        return tuple(values)
    return (values[0] - margin, values[1] + margin,
            values[2] - margin, values[3] + margin,
            values[4] - margin, values[5] + margin)


def background_estimate(bounds, counts) -> dict:
    lengths = (bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4])
    cells = tuple(lengths[i] / int(counts[i]) for i in range(3))
    ratio = max(cells) / min(cells)
    return {'cell_count': math.prod(int(value) for value in counts),
            'cell_sizes': cells, 'aspect_ratio': ratio, 'anisotropic': ratio > 3.0}


def first_layer_height(target_y_plus: float, velocity: float, density: float,
                       dynamic_viscosity: float, reference_length: float = 1.0) -> float:
    values = (target_y_plus, velocity, density, dynamic_viscosity, reference_length)
    if any(value <= 0 or not math.isfinite(value) for value in values):
        raise ValueError('all y-plus inputs must be positive and finite')
    reynolds = density * velocity * reference_length / dynamic_viscosity
    skin_friction = 0.026 / reynolds ** (1 / 7)
    friction_velocity = velocity * math.sqrt(0.5 * skin_friction)
    return target_y_plus * dynamic_viscosity / (density * friction_velocity)


def refinement_region_levels(mode, distance: float, level: int) -> list[list[float | int]]:
    """Return Foundation-v13 region levels for distance/inside/outside modes."""
    token = getattr(mode, 'value', mode)
    if token == 'distance':
        return [[float(distance), int(level)]]
    if token in {'inside', 'outside'}:
        return [[1e15, int(level)]]
    raise ValueError(f'unsupported refinement-region mode: {token}')


def validate_fluid_seed(polydata, point, *, tolerance: float | None = None) -> dict:
    """Validate a Snappy locationInMesh point against a closed surface."""
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints
    if len(point) != 3 or any(not math.isfinite(float(value)) for value in point):
        raise ValueError('fluid seed must contain three finite coordinates')
    bounds = polydata.GetBounds()
    diagonal = math.sqrt(sum((bounds[i * 2 + 1] - bounds[i * 2]) ** 2
                             for i in range(3)))
    tolerance = float(tolerance if tolerance is not None else max(1e-12, diagonal * 1e-8))
    distance = vtkImplicitPolyDataDistance()
    distance.SetInput(polydata)
    surface_distance = abs(float(distance.EvaluateFunction(tuple(map(float, point)))))
    if surface_distance <= tolerance:
        return {'valid': False, 'inside': False, 'on_surface': True,
                'distance_to_surface': surface_distance,
                'reason': 'fluid seed lies on or too near the geometry surface'}
    candidates = vtkPoints()
    candidates.InsertNextPoint(*map(float, point))
    probe = vtkPolyData()
    probe.SetPoints(candidates)
    enclosed = vtkSelectEnclosedPoints()
    enclosed.SetInputData(probe)
    enclosed.SetSurfaceData(polydata)
    enclosed.SetTolerance(tolerance)
    enclosed.Update()
    inside = bool(enclosed.IsInside(0))
    return {'valid': inside, 'inside': inside, 'on_surface': False,
            'distance_to_surface': surface_distance,
            'reason': '' if inside else 'fluid seed is outside the intended closed region'}
