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

**Millimetres out, model units in.** Plan 39 VI5 (F4c): a distance is measured
in the mesh's own units, and the case's length unit (metres per model unit,
``unit``: a name such as ``'mm'`` or a number) turns it into the millimetres
the legend states. A mesh modelled in mm reads in mm, not 1000 times that.

**Reference normals and open sheets.** Plan 39 VI6 (F4f). The sign of a
distance comes from the nearest triangle's winding, and a reference made of
several appended surfaces has no consistent winding, so an outside face could
read blue. A closed reference is re-wound outward (`prepare_reference`); an
open one has no inside, so its distances are shown unsigned and the panel says
so.

**Coloured by size, on the colour standard.** Plan 43 LY11/LY12. The colour is
``|d|`` from zero to the scale (by default the 98th percentile of ``|d|``, so
one bad face does not wash out the body), on the app's colour standard (Jet
unless the user chose another). The sign is read from the numbers
(``signed_fields``). The tolerance is a line on the legend bar; it recolours
only when the standard's range is *Around the tolerance*.

**One range for every patch.** The same colour has to mean the same distance
on every patch; a range per patch made 0.1 mm on a good patch and 3 mm on a
bad one the same red.
"""
from __future__ import annotations

from dataclasses import dataclass

import re

import numpy as np

#: Millimetres in a metre: the legend's unit against the metre-based units.
MM_PER_METRE = 1000.0

#: The unit of a mesh nobody has scaled: coordinates in metres.
DEFAULT_UNIT = 'm'


def unit_to_mm(unit=DEFAULT_UNIT) -> float:
    """Millimetres in one model unit. ``unit`` is a name or metres per unit."""
    from foammesh.core.mesh.transform_record import unit_metres
    return unit_metres(unit) * MM_PER_METRE

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


def face_distances_mm(reference, surfaces: dict,
                      unit=DEFAULT_UNIT) -> dict:
    """``name -> signed distance (mm)`` for every surface in ``surfaces``.

    ``reference`` and the surfaces share one frame and ``unit``.
    """
    factor = unit_to_mm(unit)
    return {name: signed_face_distances(reference, surface) * factor
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


def default_tolerance(cell_size=None, extent=None,
                      unit=DEFAULT_UNIT) -> float:
    """A first tolerance in mm: a tenth of the base cell, else of the model.

    ``cell_size`` is the authored base cell in metres (a physical size, which
    a later Mesh > Scale does not change); ``extent`` is in model ``unit``.
    """
    if cell_size:
        sizes = [abs(float(size)) for size in np.ravel(cell_size)
                 if np.isfinite(size) and float(size) > 0.0]
        if sizes:
            return min(sizes) * CELL_TOLERANCE_SHARE * MM_PER_METRE
    if extent:
        return abs(float(extent)) * EXTENT_TOLERANCE_SHARE * unit_to_mm(unit)
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
    low, high = (float(v) for v in value_range)
    # Values past the scale count in the end bars: the scale is a percentile,
    # and the faces beyond it are the ones the user is looking for.
    counts, edges = np.histogram(np.clip(values, low, high), bins=int(bins),
                                 range=(low, high))
    return {'edges': [float(edge) for edge in edges],
            'counts': [int(count) for count in counts]}


# -- Plan 39 VI6: reference, roles, bands ----------------------------------- #

#: The share of |d| the scale spans by default.
SCALE_PERCENTILE = 98.0

class PreparedReference:
    """A reference ready to measure against: triangles, and whether it is closed.

    ``closed`` is true when every edge is shared by exactly two triangles, so
    it has an inside and the sign of a distance means something. ``notice``
    says why not, for the panel.
    """

    def __init__(self, polydata, closed: bool, notice: str = ''):
        self.polydata = polydata
        self.closed = bool(closed)
        self.notice = notice


def prepare_reference(reference) -> PreparedReference:
    """Merge, triangulate and (when closed) orient ``reference`` outward."""
    from vtkmodules.vtkFiltersCore import (
        vtkCleanPolyData, vtkFeatureEdges, vtkPolyDataNormals,
        vtkTriangleFilter)

    if reference is None or reference.GetNumberOfCells() == 0:
        return PreparedReference(reference, False)
    clean = vtkCleanPolyData()
    clean.SetInputData(reference)
    clean.ConvertLinesToPointsOff()
    clean.ConvertPolysToLinesOff()
    clean.ConvertStripsToPolysOn()
    clean.Update()
    triangles = vtkTriangleFilter()
    triangles.SetInputData(clean.GetOutput())
    triangles.PassLinesOff()
    triangles.PassVertsOff()
    triangles.Update()
    surface = triangles.GetOutput()
    if surface.GetNumberOfCells() == 0:
        return PreparedReference(surface, False)

    edges = vtkFeatureEdges()
    edges.SetInputData(surface)
    edges.BoundaryEdgesOn()
    edges.NonManifoldEdgesOn()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.Update()
    if edges.GetOutput().GetNumberOfCells() > 0:
        return PreparedReference(
            surface, False, OPEN_REFERENCE_NOTICE)

    normals = vtkPolyDataNormals()
    normals.SetInputData(surface)
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.SplittingOff()
    normals.ComputePointNormalsOff()
    normals.ComputeCellNormalsOn()
    normals.Update()
    return PreparedReference(normals.GetOutput(), True)


#: What the panel says when the reference has no inside.
OPEN_REFERENCE_NOTICE = 'Open reference: distance shown unsigned.'


def measure_surfaces_mm(reference, surfaces: dict, unit=DEFAULT_UNIT):
    """``(name -> distance mm, prepared)``; unsigned when the reference is open.

    ``reference`` is a vtkPolyData (or an already `PreparedReference`) in the
    surfaces' frame and ``unit``.
    """
    prepared = (reference if isinstance(reference, PreparedReference)
                else prepare_reference(reference))
    factor = unit_to_mm(unit)
    fields = {}
    for name, surface in surfaces.items():
        values = signed_face_distances(prepared.polydata, surface) * factor
        fields[name] = values if prepared.closed else np.abs(values)
    return fields, prepared


def absolute_scale(fields, percentile: float = SCALE_PERCENTILE) -> float:
    """The distance the ramp reaches: the ``percentile`` of ``|d|`` (> 0)."""
    values = _joined(fields)
    if values.size == 0:
        return 1e-6
    reach = float(np.percentile(np.abs(values), percentile))
    return reach if reach > 0.0 else 1e-6


def scale_range(scale: float, signed: bool = True) -> tuple:
    """The colour range for ``scale``: from zero to the largest |d| shown.

    Plan 43 LY12 (§4, decision 3). Deviation is coloured by its size, so the
    range starts at zero whether or not the field is signed; the sign is read
    from the numbers. ``signed`` stays in the signature for the callers that
    still say which kind of field they hold.
    """
    return (0.0, abs(float(scale)) or 1e-6)


def magnitude_fields(fields) -> dict:
    """``name -> |d|`` for a ``name -> signed distance`` mapping (nan kept)."""
    return {name: np.abs(_array(values)) for name, values in fields.items()}


def fill_deviation_lut(lut, scale: float, tolerance: float,
                       signed: bool = True):
    """Fill ``lut`` on the standard scale (in place, so a legend follows it).

    The table runs from 0 to ``scale`` on the colour standard, Rainbow (Jet)
    unless the user chose another. With the standard's range set to *Around
    the tolerance* it runs from 0 to twice the tolerance.
    """
    from foammesh.support import colour_standard

    low, high = scale_range(scale, signed)
    return colour_standard.refill(
        lut, 'deviation', low, high, True,
        limit=max(0.0, abs(float(tolerance))) or None)


def deviation_lut(scale: float, tolerance: float, signed: bool = True):
    """A new magnitude table on the colour standard."""
    from foammesh.support import colour_standard

    low, high = scale_range(scale, signed)
    return colour_standard.lookup_table(
        'deviation', low, high, True,
        limit=max(0.0, abs(float(tolerance))) or None)


#: Boundary types that never stand for a surface of the geometry.
_NO_REFERENCE_TYPES = frozenset({
    'symmetry', 'symmetryplane', 'empty', 'cyclic', 'cyclicami', 'cycliccgi',
    'cyclicslip', 'wedge', 'processor', 'processorcyclic',
    'nonconformalcyclic', 'internal'})
#: Prepared-geometry categories that are openings of the domain.
_NO_REFERENCE_CATEGORIES = frozenset({
    'inlet', 'outlet', 'farfield', 'far_field', 'freestream', 'open',
    'opening', 'symmetry', 'background'})
_NO_REFERENCE_NAME = re.compile(
    r'(inlet|outlet|far[_\-]?field|free[_\-]?stream|atmosphere|symm|empty|'
    r'cyclic|periodic|wedge)', re.IGNORECASE)


def patch_reference(name: str, patch_type=None, identity=None) -> tuple:
    """``(has_reference, why_not)`` for one patch of the mesh.

    A patch is measured by default when it came from geometry: a wall, or a
    named surface the patch-identity sidecar traces to the prepared geometry.
    Far field, inlet, outlet, symmetry, empty, cyclic and wedge patches have
    no surface to be compared with. ``identity`` is a
    `patch_identity.PatchIdentity` or ``None``.
    """
    kind = str(patch_type or '').strip().lower()
    if kind in _NO_REFERENCE_TYPES:
        return False, kind
    if identity is not None:
        origin = str(getattr(identity, 'origin', '') or '')
        if origin and origin != 'prepared':
            return False, origin
        category = str(getattr(identity, 'category', '') or '').lower()
        if category in _NO_REFERENCE_CATEGORIES:
            return False, category
        if getattr(identity, 'patch_uuid', None):
            return True, ''
    if kind == 'wall':
        return True, ''
    if kind:                                  # a plain `patch`: no geometry
        return False, kind
    if _NO_REFERENCE_NAME.search(str(name)):
        return False, 'name'
    return True, ''


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
    #: Signed on a closed reference, unsigned (0 -> scale) otherwise and for
    #: a stored run.
    signed: bool = True
    scale: float = 0.0
    tolerance: float = 0.0
    #: One sentence for the panel, e.g. the open-reference notice.
    notice: str = ''
    #: ``[(name, ticked, has_reference, why_not)]`` for the Surfaces list.
    surfaces: tuple = ()
    #: ``name -> signed mm`` for what is painted (``fields`` is ``|d|``).
    signed_fields: dict = None

    def values(self) -> list:
        return list(self.fields.values())
