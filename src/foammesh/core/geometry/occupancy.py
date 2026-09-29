"""Which voxels of a grid are wall: one distance-occupancy implementation.

Plan 36 RP5. Two callers sample ``vtkImplicitPolyDataDistance`` over a
regular grid and call a voxel *wall* when the surface passes within a band of
its centre. The surface wrapper (:mod:`foammesh.core.geometry.wrap`) samples
every voxel. The fluid-space field (:mod:`foammesh.core.mesh.fluid_spaces`)
cannot afford that: MEASURED on the annulus (Plan 36 §3.4), the full-grid
sample is 2.3 s at 0.71 M voxels and is the only expensive step. So it samples
a coarse grid first and evaluates the fine voxels only where the coarse
distance says the surface can be within the band.

Both readings share the grid convention of ``vtkSampleFunction``: *dimensions*
points spanning *model_bounds* inclusively, point id ``i + nx * (j + ny * k)``,
so a numpy view of the scalars is shaped ``(nz, ny, nx)``.
"""
from __future__ import annotations

import math


def sampled_distance(polydata, model_bounds, dimensions, *, implicit=None):
    """The signed distance to *polydata* at every grid point, as image data."""
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    from vtkmodules.vtkImagingHybrid import vtkSampleFunction

    if implicit is None:
        implicit = vtkImplicitPolyDataDistance()
        implicit.SetInput(polydata)
    sample = vtkSampleFunction()
    sample.SetImplicitFunction(implicit)
    sample.SetModelBounds(*model_bounds)
    sample.SetSampleDimensions(*dimensions)
    sample.ComputeNormalsOff()
    sample.Update()
    return sample.GetOutput()


def wall_occupancy(polydata, model_bounds, dimensions, band, *,
                   sampled=None):
    """1 where ``|d| <= band``, else 0, over the full grid (``uint8`` image).

    *sampled* is an already computed :func:`sampled_distance` image, so a
    caller that checkpoints between the two steps does not sample twice.
    """
    from vtkmodules.vtkImagingCore import vtkImageThreshold
    from vtkmodules.vtkImagingMath import vtkImageMathematics

    if sampled is None:
        sampled = sampled_distance(polydata, model_bounds, dimensions)
    absolute = vtkImageMathematics()
    absolute.SetInputData(sampled)
    absolute.SetOperationToAbsoluteValue()
    absolute.Update()
    occupancy = vtkImageThreshold()
    occupancy.SetInputConnection(absolute.GetOutputPort())
    occupancy.ThresholdBetween(0.0, float(band))
    occupancy.SetInValue(1)
    occupancy.SetOutValue(0)
    occupancy.SetOutputScalarTypeToUnsignedChar()
    occupancy.Update()
    return occupancy.GetOutput()


def _grid_axes(model_bounds, dimensions):
    axes = []
    for axis in range(3):
        lower, upper = model_bounds[2 * axis:2 * axis + 2]
        count = int(dimensions[axis])
        step = (upper - lower) / (count - 1) if count > 1 else 0.0
        axes.append((float(lower), float(step), count))
    return axes


def evaluate_distance(implicit, points, *, chunk=262144, cancelled=None,
                      progress=None, seconds=None):
    """Signed distances at an ``(n, 3)`` array of points, in chunks.

    One ``FunctionValue`` call per chunk keeps the per-point Python cost out;
    the chunks are where *cancelled* is honoured and *progress* reported.

    With *seconds* the chunks are sized to take about that long each, *chunk*
    being the most (Plan 36 RP13 #7). A distance costs from about 1 to 15
    microseconds with the surface and the point (MEASURED: 32 k points 0.5 s
    against a 36 k-triangle surface), so a fixed chunk cannot bound how long
    a cancel waits.
    """
    import time

    import numpy as np
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkDoubleArray

    points = np.ascontiguousarray(points, dtype=np.float64)
    out = np.empty(len(points), dtype=np.float64)
    size = chunk if seconds is None else min(chunk, 1024)
    start = 0
    while start < len(points):
        if cancelled is not None and cancelled():
            raise InterruptedError('distance sampling cancelled')
        block = np.ascontiguousarray(points[start:start + size])
        values = vtkDoubleArray()
        began = time.perf_counter()
        implicit.FunctionValue(numpy_to_vtk(block, deep=1), values)
        took = time.perf_counter() - began
        out[start:start + len(block)] = vtk_to_numpy(values)
        start += len(block)
        if progress is not None:
            progress(start, len(points))
        if seconds is not None:
            rate = took / max(1, len(block))
            size = int(min(chunk, max(256, seconds / max(rate, 1e-9))))
    return out


def narrow_band_distance(polydata, model_bounds, dimensions, band, *,
                         coarse_factor=4, implicit=None, cancelled=None,
                         progress=None, chunk=262144, seconds=None):
    """Wall voxels and their signed distances, sampled only near the surface.

    Returns ``(wall, distance, evaluated)``, numpy arrays shaped
    ``(nz, ny, nx)``: *wall* is ``uint8`` 1 where ``|d| <= band``; *distance*
    is ``float32``, the signed distance where it was evaluated finely and NaN
    elsewhere; *evaluated* is the count of fine evaluations.

    The distance to a surface is 1-Lipschitz, so a fine voxel whose nearest
    coarse point has ``|d| >= r + band`` -- *r* being the furthest a fine voxel
    can be from its nearest coarse point -- cannot be wall, and is never
    evaluated. The result is the full-grid :func:`wall_occupancy` exactly, up
    to the last bit of the distance evaluation.
    """
    import numpy as np
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance

    if implicit is None:
        implicit = vtkImplicitPolyDataDistance()
        implicit.SetInput(polydata)
    axes = _grid_axes(model_bounds, dimensions)
    factor = max(1, int(coarse_factor))
    nx, ny, nz = (count for _lower, _step, count in axes)

    # The coarse grid shares the fine grid's origin, every *factor*-th point,
    # extended past the far side so every fine point has a coarse neighbour.
    coarse_dims, coarse_bounds = [], []
    for lower, step, count in axes:
        cells = int(math.ceil((count - 1) / factor)) if count > 1 else 0
        coarse_dims.append(cells + 1)
        coarse_bounds.extend((lower, lower + cells * factor * step))
    if seconds is None:
        coarse = sampled_distance(polydata, coarse_bounds, coarse_dims,
                                  implicit=implicit)
        coarse_d = np.abs(vtk_to_numpy(coarse.GetPointData().GetScalars()))
    else:
        # RP13 #7: the coarse pass alone is 0.47 s at 2 M voxels (MEASURED),
        # so a caller that bounds its cancel latency samples it in chunks.
        # The grid points are vtkSampleFunction's; the reach below is
        # conservative, so the last bit of a coarse point cannot matter.
        coarse_axes = [np.linspace(coarse_bounds[2 * axis],
                                   coarse_bounds[2 * axis + 1],
                                   coarse_dims[axis]) for axis in range(3)]
        zz, yy, xx = np.meshgrid(coarse_axes[2], coarse_axes[1],
                                 coarse_axes[0], indexing='ij')
        coarse_d = np.abs(evaluate_distance(
            implicit, np.stack((xx.ravel(), yy.ravel(), zz.ravel()), axis=1),
            chunk=chunk, cancelled=cancelled, seconds=seconds))
    coarse_d = coarse_d.reshape(coarse_dims[2], coarse_dims[1],
                                coarse_dims[0])
    if cancelled is not None and cancelled():
        raise InterruptedError('distance sampling cancelled')
    reach = 0.5 * factor * math.sqrt(sum(step * step
                                         for _lower, step, _count in axes))
    flagged = coarse_d < reach + float(band)

    nearest = [np.minimum((np.arange(count) + factor // 2) // factor,
                          coarse_dims[axis] - 1)
               for axis, (_lower, _step, count) in enumerate(axes)]
    candidate = flagged[np.ix_(nearest[2], nearest[1], nearest[0])]
    kk, jj, ii = np.nonzero(candidate)
    points = np.empty((len(ii), 3), dtype=np.float64)
    for column, (index, (lower, step, _count)) in enumerate(
            zip((ii, jj, kk), axes)):
        points[:, column] = lower + index * step
    # *chunk* and *seconds* bound the work between polls of *cancelled*.
    values = evaluate_distance(implicit, points, chunk=chunk,
                               cancelled=cancelled, progress=progress,
                               seconds=seconds)
    distance = np.full((nz, ny, nx), np.nan, dtype=np.float32)
    distance[kk, jj, ii] = values
    wall = np.zeros((nz, ny, nx), dtype=np.uint8)
    wall[kk, jj, ii] = np.abs(values) <= float(band)
    return wall, distance, int(len(values))
