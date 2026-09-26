"""Signed deviation of a mesh surface from its reference, measured on demand.

DP-713 (viewport audit 0925 F8). The deviation colouring could only paint what
a stored fidelity run had written, and most cases have never had one: the
button refused on every freshly meshed case although the reference geometry
the mesh was made from was loaded in the same window. This measures the one
thing the colouring needs -- how far each boundary face centre sits from that
reference -- directly, when it is asked for.

**Signed, not absolute.** ``vtkImplicitPolyDataDistance`` is negative inside a
closed reference and positive outside it, so a face that snapped short of the
surface and one that overshot it read as opposite colours rather than the same
one. The sign of a face on an open reference follows the reference's normals.

**Millimetres.** Mesh and geometry coordinates are metres; one factor turns
them into the unit the legend states, and every figure here is in it.

**One range for every patch.** The same colour has to mean the same distance
on every patch; a range per patch made 0.1 mm on a good patch and 3 mm on a
bad one the same red.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Model units (metres) to the unit the legend states.
MODEL_TO_MM = 1000.0

#: A tolerance with no cell size to go on: this share of the model's extent.
EXTENT_TOLERANCE_SHARE = 1e-3

#: The default tolerance, as a share of the base cell.
CELL_TOLERANCE_SHARE = 0.1


def _array(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float64).ravel()


def _joined(fields) -> np.ndarray:
    """Every finite value of ``fields`` (an iterable of arrays) in one array."""
    arrays = [_array(values) for values in fields]
    if not arrays:
        return np.zeros(0, dtype=np.float64)
    joined = np.concatenate(arrays)
    return joined[np.isfinite(joined)]


def signed_face_distances(reference, surface) -> np.ndarray:
    """The signed distance of each cell centre of ``surface`` to ``reference``.

    Both are vtkPolyData in model units; the answer is in model units, one
    value per cell of ``surface`` in its own cell order, ``nan`` where there is
    no reference to measure against.
    """
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkDoubleArray
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    from vtkmodules.vtkFiltersCore import vtkTriangleFilter
    from vtkmodules.vtkFiltersCore import vtkCellCenters

    count = surface.GetNumberOfCells() if surface is not None else 0
    if count == 0:
        return np.zeros(0, dtype=np.float64)
    if reference is None or reference.GetNumberOfCells() == 0:
        return np.full(count, np.nan, dtype=np.float64)

    triangles = vtkTriangleFilter()
    triangles.SetInputData(reference)
    triangles.Update()
    implicit = vtkImplicitPolyDataDistance()
    implicit.SetInput(triangles.GetOutput())

    centres = vtkCellCenters()
    centres.SetInputData(surface)
    centres.VertexCellsOff()
    centres.Update()
    points = centres.GetOutput().GetPoints()
    if points is None or points.GetNumberOfPoints() != count:
        return np.full(count, np.nan, dtype=np.float64)

    inputs = numpy_to_vtk(np.ascontiguousarray(
        vtk_to_numpy(points.GetData()), dtype=np.float64), deep=True)
    output = vtkDoubleArray()
    implicit.FunctionValue(inputs, output)
    return vtk_to_numpy(output).astype(np.float64, copy=True)


def face_distances_mm(reference, surfaces: dict) -> dict:
    """``name -> signed distance (mm)`` for every surface in ``surfaces``."""
    return {name: signed_face_distances(reference, surface) * MODEL_TO_MM
            for name, surface in surfaces.items()}


def symmetric_range(fields) -> tuple[float, float] | None:
    """One range, centred on zero, that every patch is coloured against.

    Centred so white is always "on the surface" and blue and red always mean
    the same side of it. ``None`` when there is nothing finite to colour.
    """
    values = _joined(fields)
    if values.size == 0:
        return None
    reach = float(np.max(np.abs(values)))
    if reach <= 0.0:
        reach = 1e-6
    return (-reach, reach)


def default_tolerance(cell_size=None, extent=None) -> float:
    """A first tolerance in mm: a tenth of the base cell, else of the model."""
    if cell_size:
        sizes = [abs(float(size)) for size in np.ravel(cell_size)
                 if np.isfinite(size) and float(size) > 0.0]
        if sizes:
            return min(sizes) * CELL_TOLERANCE_SHARE * MODEL_TO_MM
    if extent:
        return abs(float(extent)) * EXTENT_TOLERANCE_SHARE * MODEL_TO_MM
    return 0.1


@dataclass(frozen=True)
class DeviationSummary:
    """The figures beside the histogram, all in mm."""

    count: int
    unmeasured: int
    max_abs: float
    mean: float
    rms: float
    tolerance: float
    within: int

    @property
    def within_fraction(self) -> float:
        return self.within / self.count if self.count else 0.0


def summarize(fields, tolerance: float) -> DeviationSummary:
    """Max |d|, mean, RMS and how many faces lie within ``±tolerance``."""
    arrays = [_array(values) for values in fields]
    total = sum(array.size for array in arrays)
    values = _joined(arrays)
    tolerance = abs(float(tolerance))
    if values.size == 0:
        return DeviationSummary(0, total, 0.0, 0.0, 0.0, tolerance, 0)
    return DeviationSummary(
        count=int(values.size),
        unmeasured=int(total - values.size),
        max_abs=float(np.max(np.abs(values))),
        mean=float(np.mean(values)),
        rms=float(np.sqrt(np.mean(values * values))),
        tolerance=tolerance,
        within=int(np.count_nonzero(np.abs(values) <= tolerance)))


def histogram(fields, bins: int = 24, value_range=None) -> dict:
    """``{'edges', 'counts'}`` of the signed deviation, over one range."""
    values = _joined(fields)
    if values.size == 0:
        return {'edges': [], 'counts': []}
    if value_range is None:
        value_range = symmetric_range([values])
    counts, edges = np.histogram(values, bins=int(bins),
                                 range=tuple(float(v) for v in value_range))
    return {'edges': [float(edge) for edge in edges],
            'counts': [int(count) for count in counts]}


@dataclass
class DeviationReadout:
    """What a painted deviation colouring shows, for its legend and panel.

    ``fields`` is ``name -> mm`` in each patch's cell order (``nan`` where a
    face was not measured), ``source`` is ``'run'`` for a stored fidelity
    run's field and ``'live'`` for one measured on demand.
    """

    fields: dict
    value_range: tuple
    lookup_table: object = None
    source: str = 'live'

    def values(self) -> list:
        return list(self.fields.values())
