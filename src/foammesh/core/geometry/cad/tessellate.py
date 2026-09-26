#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Controlled CAD tessellation to a tri-surface for snappyHexMesh.

``TessellationParams`` (pure, tested) exposes the OCCT ``BRepMesh`` controls so
the user drives facet density. ``tessellate()`` runs OCCT lazily and emits a
vtkPolyData with a per-triangle face-id array (so patches map to CAD faces). The
CAD shape stays the source; the tri-surface is a regenerable artifact.
"""
from __future__ import annotations

from dataclasses import dataclass

from .availability import require

#: DP-508. The most triangles one tessellation may build. MEASURED on
#: `helical_pipe.step` (about 1 m across, read in millimetres): 0.1 built
#: 71,600 triangles in 1.2 s at 0.22 GiB; 0.01 built 631,096 in 22 s at
#: 1.25 GiB -- about 2 KiB per triangle at the peak -- and 0.001 passed 8 GiB
#: in 60 s and was killed. Two million is about 4 GiB at that rate, and is
#: already more facets than a snappy or Gmsh surface wants.
MAX_TRIANGLES = 2_000_000

#: The trial deflection, as a fraction of the bounding-box diagonal (or, for
#: a relative deflection, of each edge). MEASURED on `helical_pipe.step`: a
#: chord-only trial there builds 5,958 triangles in 0.05 s and projects
#: 59,580 and 595,800 for 0.1 and 0.01, against 71,600 and 631,096 built. A
#: trial at 1 % of the diagonal is too far from the regime it projects to --
#: the same projection comes out a factor of two low.
PILOT_FRACTION = 1e-3
PILOT_RELATIVE = 0.05
#: The angle the chord-only trial runs at, so that the chord alone binds.
PILOT_CHORD_ANGLE_DEG = 90.0
#: The finest angle a trial runs at; a finer request is projected from it.
PILOT_MIN_ANGLE_DEG = 20.0


class TessellationTooFine(ValueError):
    """The deflection asked for would build more triangles than allowed."""


#: DP-520. The chord tolerance an import is faceted at when nobody says, in
#: metres. It was ``0.1`` "in model units", and the model unit of a STEP or
#: IGES read is the millimetre the importer pins the reader to, so every
#: default CAD import was faceted at 0.1 mm; keeping that value keeps every
#: part faceted exactly as it was.
DEFAULT_LINEAR_DEFLECTION_M = 1e-4


@dataclass
class TessellationParams:
    #: DP-520. The chord tolerance, in METRES -- the unit the CAD panel labels
    #: it in, the repair route applies it in and the fixture catalogue names
    #: it in (``linear_deflection_m``). It used to be "in model units", which
    #: nobody could see: the panel said m, a STEP import applied it to a
    #: shape the reader hands back in millimetres, and the panel's
    #: Re-tessellate applied the same number to that shape scaled to metres
    #: -- one field, a factor of a thousand apart depending on the button.
    #: :func:`tessellate` converts it into the shape's own units.
    #: A ``relative`` deflection is a fraction of each edge and has no unit.
    linear_deflection: float = DEFAULT_LINEAR_DEFLECTION_M
    angular_deflection_deg: float = 20.0
    relative: bool = False              # linear deflection relative to edge length
    parallel: bool = True

    def validate(self) -> None:
        if self.linear_deflection <= 0:
            raise ValueError('linear_deflection must be > 0')
        if not (0 < self.angular_deflection_deg < 180):
            raise ValueError('angular_deflection_deg must be in (0, 180)')


def estimate_triangles(pilot_count: int, pilot_deflection: float,
                       deflection: float) -> float:
    """Triangles a finer chord tolerance will build, from a trial's count.

    Where the chord tolerance binds, the edge length goes as the square root
    of the deflection and the triangle count as its inverse. MEASURED on
    `helical_pipe.step` with the angle out of the way: 294 at 10.49, 1,652
    at 3, 5,958 at 1, then 71,600 at 0.1 and 631,096 at 0.01.
    """
    if deflection <= 0:
        return float('inf')
    return float(pilot_count) * float(pilot_deflection) / float(deflection)


def estimate_angular(pilot_count: int, pilot_angle_deg: float,
                     angle_deg: float) -> float:
    """Triangles a finer angular limit will build, from a trial's count.

    A doubly curved face refines in both directions, so the count goes as the
    inverse square of the angle; a singly curved one goes as the inverse, and
    the square only overstates it.
    """
    if angle_deg <= 0:
        return float('inf')
    ratio = max(1.0, float(pilot_angle_deg) / float(angle_deg))
    return float(pilot_count) * ratio * ratio


def _diagonal(shape) -> float:
    from OCC.Core.Bnd import Bnd_Box
    from OCC.Core.BRepBndLib import brepbndlib

    box = Bnd_Box()
    brepbndlib.Add(shape, box)
    if box.IsVoid():
        return 0.0
    x0, y0, z0, x1, y1, z1 = box.Get()
    return ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5


def _triangle_count(shape) -> int:
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.TopAbs import TopAbs_FACE
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopLoc import TopLoc_Location
    from OCC.Core.TopoDS import topods

    count = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        triangulation = BRep_Tool.Triangulation(
            topods.Face(explorer.Current()), TopLoc_Location())
        if triangulation is not None:
            count += triangulation.NbTriangles()
        explorer.Next()
    return count


def applied_deflection(params: TessellationParams,
                       unit_factor: float = 1.0) -> float:
    """The chord tolerance to hand OCCT for a shape in *unit_factor* metres
    per unit (DP-520): ``params.linear_deflection`` is in metres, OCCT wants
    the shape's own units. A relative deflection is a ratio and passes as is.
    """
    if params.relative or not unit_factor or unit_factor == 1.0:
        return float(params.linear_deflection)
    return float(params.linear_deflection) / float(unit_factor)


def check_budget(shape, params: TessellationParams,
                 max_triangles: int = MAX_TRIANGLES, *,
                 unit_factor: float = 1.0) -> None:
    """Refuse a deflection whose tessellation would exceed *max_triangles*.

    DP-508 (MA-12). The deflection is in the shape's own units and nothing
    related it to the part's size, so one number could be a caricature of a
    2 mm part and an unbounded job on a 1 m one. `helical_pipe.step` held the
    desktop at "Not Responding" for four minutes with its working set past
    56 GiB, and ``BRepMesh`` is one native call that nothing can interrupt.
    So the cost is measured before it is paid: two cheap trials at a
    deflection tied to the bounding box -- one where only the chord binds,
    one where only the angle does -- projected to what was asked.

    *unit_factor* is metres per unit of the shape's coordinates; the trials
    run in those units and the refusal speaks in the metres the user typed.
    """
    import math
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.BRepTools import breptools

    if _triangle_count(shape):
        # Already faceted -- a re-read of a meshed shape, or a STEP that
        # carries its own triangles. The trial would have to clear them.
        return
    if params.relative:
        pilot, coarse, size = PILOT_RELATIVE, 1.0, None
    else:
        size = _diagonal(shape)
        # A chord as long as the part: only the angle binds.
        pilot, coarse = size * PILOT_FRACTION, size
    if pilot <= 0:
        return

    def trial(deflection, angle_deg):
        BRepMesh_IncrementalMesh(shape, deflection, params.relative,
                                 math.radians(angle_deg), params.parallel)
        count = _triangle_count(shape)
        # The trial's triangles would otherwise be kept as good enough for
        # any deflection OCCT judges them to satisfy.
        breptools.Clean(shape)
        return count

    chord = trial(pilot, PILOT_CHORD_ANGLE_DEG)
    angle = max(params.angular_deflection_deg, PILOT_MIN_ANGLE_DEG)
    angular = trial(coarse, angle)
    unit_factor = float(unit_factor or 1.0)
    deflection = applied_deflection(params, unit_factor)
    by_chord = estimate_triangles(chord, pilot, deflection)
    by_angle = estimate_angular(angular, angle, params.angular_deflection_deg)
    estimate = max(by_chord, by_angle)
    if estimate <= max_triangles:
        return
    if by_chord >= by_angle:
        unit = '' if params.relative else ' m'
        scale = (f' (the part is {size * unit_factor:.4g} m across, so that '
                 f'is {deflection / size:.1e} of its size)'
                 if size else ' of each edge')
        asked = (f'a linear deflection of '
                 f'{params.linear_deflection:g}{unit}{scale}')
        least = chord * pilot / max_triangles
        if not params.relative:
            least *= unit_factor
        remedy = f'Use a linear deflection of at least {least:.3g}{unit}.'
    else:
        asked = (f'an angular deflection of '
                 f'{params.angular_deflection_deg:g} deg')
        remedy = (f'Use an angular deflection of at least '
                  f'{angle * math.sqrt(angular / max_triangles):.3g} deg.')
    raise TessellationTooFine(
        f'Faceting at {asked} would build about {estimate:,.0f} triangles, '
        f'more than the {max_triangles:,} an import allows. {remedy}')


def tessellate(shape, params: TessellationParams | None = None, *,
               max_triangles: int = MAX_TRIANGLES, unit_factor: float = 1.0):
    """Tessellate an OCCT shape into a vtkPolyData. Requires OCCT ([cad] extra).

    *unit_factor* is metres per unit of the shape's coordinates -- 0.001 for
    a STEP or IGES read, which comes back in millimetres -- and converts the
    metre deflection in *params* into them (DP-520). The coordinates of the
    result are left in the shape's units.

    Raises :class:`TessellationTooFine` rather than start a tessellation that
    would build more than *max_triangles* triangles (DP-508).
    """
    require()
    params = params or TessellationParams()
    params.validate()
    check_budget(shape, params, max_triangles, unit_factor=unit_factor)
    deflection = applied_deflection(params, unit_factor)

    import math
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCC.Core.TopoDS import topods
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.TopLoc import TopLoc_Location
    from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
    from vtkmodules.vtkCommonCore import vtkPoints, vtkIntArray

    BRepMesh_IncrementalMesh(
        shape, deflection, params.relative,
        math.radians(params.angular_deflection_deg), params.parallel)

    points = vtkPoints()
    triangles = vtkCellArray()
    face_ids = vtkIntArray()
    face_ids.SetName('cadFaceId')

    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    face_index = 0
    base = 0
    while explorer.More():
        face = topods.Face(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation(face, location)
        if triangulation is not None:
            trsf = location.Transformation()
            n = triangulation.NbNodes()
            for i in range(1, n + 1):
                p = triangulation.Node(i).Transformed(trsf)
                points.InsertNextPoint(p.X(), p.Y(), p.Z())
            for i in range(1, triangulation.NbTriangles() + 1):
                t = triangulation.Triangle(i)
                a, b, c = t.Get()
                if face.Orientation() == TopAbs_REVERSED:
                    b, c = c, b
                triangles.InsertNextCell(3)
                triangles.InsertCellPoint(base + a - 1)
                triangles.InsertCellPoint(base + b - 1)
                triangles.InsertCellPoint(base + c - 1)
                face_ids.InsertNextValue(face_index)
            base += n
        face_index += 1
        explorer.Next()

    polydata = vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetPolys(triangles)
    polydata.GetCellData().AddArray(face_ids)
    # OCCT triangulates per face, so shared CAD vertices otherwise appear as
    # duplicate VTK points and a closed solid is falsely diagnosed as open.
    # An explicit scale tied to the requested chord tolerance keeps this
    # deterministic while preserving the per-cell cadFaceId array.
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData
    clean = vtkCleanPolyData()
    clean.SetInputData(polydata)
    clean.PointMergingOn()
    clean.ToleranceIsAbsoluteOn()
    clean.SetAbsoluteTolerance(max(deflection * 1e-6, 1e-12))
    clean.Update()
    output = vtkPolyData()
    output.DeepCopy(clean.GetOutput())
    return output
