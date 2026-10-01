"""A closed surface's cross-section on a plane, filled.

Plan 36 RP9. While a region's seed is placed on a section plane, the space
the seed will mesh (RP6's region volume) is shown on the cut as a solid
shape: on the annulus with a Z-normal section, a filled ring. A translucent
volume seen edge-on says nothing about depth; the filled section says exactly
which part of the plane the space occupies, so the seed can be dropped into
it by eye.

The section is the plane cut through the surface (``vtkCutter``: closed
loops of line segments) filled by ``vtkContourTriangulator``, which also
takes a loop inside a loop as a hole. The surface is cleaned first: a
surface whose points were split along sharp edges (``vtkPolyDataNormals``'
default) cuts into loops that do not close where the split is, and the fill
then fails without a word.

`SectionFill` is the hook the region-volume actor calls: one per surface,
told where the plane is (`setPlane`) and when it goes away (`clear`).

Plan 37 UF6 (DP-1037). The section tool caps what it clips when there are no
cells to cut: a boundary-only mesh preview and imported geometry. A cap is a
claim about the inside of a body, so `sectionCap` draws one only from loops
it has checked are closed -- every point of the cut met by exactly two
segments -- and says why when it will not: an open sheet or a non-manifold
crossing gets no cap rather than a triangulated guess. Holes and separate
bodies come out as the loops say (a ring stays a ring, two bodies stay two);
the cap is trimmed by the other planes that are cutting, is never pickable,
and is labelled approximate when the surface was decimated.
"""
from __future__ import annotations

from dataclasses import dataclass

from foammesh.core.section.notices import (
    CAP_APPROXIMATE, CAP_CLOSED, CAP_MISSED, CAP_OPEN)

__all__ = ['CAP_APPROXIMATE', 'CAP_CLOSED', 'CAP_MISSED', 'CAP_OPEN',
           'CapResult', 'FILL_OPACITY', 'SectionFill', 'fillArea',
           'sectionCap', 'sectionFill']

#: The fill is drawn nearly opaque: it is a cut face, not a volume.
FILL_OPACITY = 0.85


def sectionFill(surface, origin, normal):
    """The filled section of closed *surface* on the plane, as triangles.

    An empty ``vtkPolyData`` when the plane misses the surface.
    """
    from vtkmodules.vtkCommonDataModel import vtkPlane, vtkPolyData
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData, vtkCutter
    from vtkmodules.vtkFiltersGeneral import vtkContourTriangulator

    if surface is None or not surface.GetNumberOfCells():
        return vtkPolyData()
    plane = vtkPlane()
    plane.SetOrigin(*(float(value) for value in origin))
    plane.SetNormal(*(float(value) for value in normal))
    clean = vtkCleanPolyData()
    clean.SetInputData(surface)
    clean.PointMergingOn()
    cutter = vtkCutter()
    cutter.SetCutFunction(plane)
    cutter.SetInputConnection(clean.GetOutputPort())
    fill = vtkContourTriangulator()
    fill.SetInputConnection(cutter.GetOutputPort())
    fill.Update()
    result = vtkPolyData()
    result.DeepCopy(fill.GetOutput())
    return result


#: Two cut points closer than this fraction of the model's diagonal are one
#: point. `vtkCutter` makes one point per crossed edge already; this only
#: catches the round-off of an edge crossed from two sides.
_MERGE = 1e-9
#: On a decimated surface, loose ends closer than this fraction of the
#: diagonal are joined: patches decimated one by one no longer meet exactly.
_GAP = 1e-3


@dataclass
class CapResult:
    """A cap, or the reason there is none."""
    #: ``CAP_CLOSED``, ``CAP_APPROXIMATE``, ``CAP_OPEN`` or ``CAP_MISSED``.
    status: str
    #: The filled triangles (empty unless the status is closed/approximate).
    polyData: object
    #: How many closed loops the cut made.
    loops: int = 0


def _empty():
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    return vtkPolyData()


def _vtkPlane(origin, normal):
    from vtkmodules.vtkCommonDataModel import vtkPlane
    plane = vtkPlane()
    plane.SetOrigin(*(float(value) for value in origin))
    plane.SetNormal(*(float(value) for value in normal))
    return plane


def _segments(polyData):
    """Every line segment of *polyData* as ``(a, b)`` point ids (numpy)."""
    import numpy as np
    from vtkmodules.util.numpy_support import vtk_to_numpy

    lines = polyData.GetLines()
    if lines is None or not lines.GetNumberOfCells():
        return np.zeros((0, 2), dtype=np.int64)
    offsets = vtk_to_numpy(lines.GetOffsetsArray()).astype(np.int64)
    ids = vtk_to_numpy(lines.GetConnectivityArray()).astype(np.int64)
    pairs = []
    for start, stop in zip(offsets[:-1], offsets[1:]):
        chain = ids[start:stop]
        if len(chain) >= 2:
            pairs.append(np.stack([chain[:-1], chain[1:]], axis=1))
    if not pairs:
        return np.zeros((0, 2), dtype=np.int64)
    segments = np.concatenate(pairs)
    return segments[segments[:, 0] != segments[:, 1]]


def _joinLooseEnds(points, segments, gap):
    """Join degree-1 ends pairwise, nearest first, when closer than *gap*."""
    import numpy as np

    degree = np.bincount(segments.ravel(), minlength=len(points))
    ends = np.flatnonzero(degree == 1)
    if len(ends) < 2:
        return segments
    where = points[ends]
    distance = np.linalg.norm(where[:, None, :] - where[None, :, :], axis=2)
    np.fill_diagonal(distance, np.inf)
    flat = np.argsort(distance, axis=None)
    rows, cols = np.unravel_index(flat, distance.shape)
    used = set()
    added = []
    for i, j in zip(rows, cols):
        if distance[i, j] > gap:
            break
        if i >= j or i in used or j in used:
            continue
        used.update((int(i), int(j)))
        added.append((ends[i], ends[j]))
    if not added:
        return segments
    return np.concatenate([segments, np.array(added, dtype=np.int64)])


def _loopCount(segments):
    """Connected components of the cut's segments."""
    parent = {}

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for a, b in segments:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return len({find(item) for item in list(parent)})


def sectionCap(surface, origin, normal, trims=(), approximate=False):
    """The cap of *surface* on the plane, drawn only from closed loops.

    *trims* are the other planes cutting (``vtkPlane``; the kept side is the
    side their normal points to, as every clip in the viewport keeps). With
    *approximate* (a decimated surface) loose ends a hair apart are joined,
    and a cap that is drawn says it is approximate.
    """
    import numpy as np
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkFiltersCore import (
        vtkCleanPolyData, vtkClipPolyData, vtkCutter)
    from vtkmodules.vtkFiltersGeneral import vtkContourTriangulator

    if surface is None or not surface.GetNumberOfCells():
        return CapResult(CAP_MISSED, _empty())
    bounds = surface.GetBounds()
    diagonal = float(np.linalg.norm(np.array(bounds[1::2])
                                    - np.array(bounds[0::2]))) or 1.0

    clean = vtkCleanPolyData()
    clean.SetInputData(surface)
    clean.PointMergingOn()
    cutter = vtkCutter()
    cutter.SetCutFunction(_vtkPlane(origin, normal))
    cutter.SetInputConnection(clean.GetOutputPort())
    lines = vtkCleanPolyData()
    lines.SetInputConnection(cutter.GetOutputPort())
    lines.ToleranceIsAbsoluteOn()
    lines.SetAbsoluteTolerance(_MERGE * diagonal)
    lines.ConvertLinesToPointsOff()
    lines.Update()
    cut = lines.GetOutput()

    segments = _segments(cut)
    if not len(segments):
        return CapResult(CAP_MISSED, _empty())
    points = vtk_to_numpy(cut.GetPoints().GetData()).astype(float)
    # The same segment twice (an edge shared by two coplanar faces) is one.
    segments = np.unique(np.sort(segments, axis=1), axis=0)
    if approximate:
        segments = _joinLooseEnds(points, segments, _GAP * diagonal)
    degree = np.bincount(segments.ravel(), minlength=len(points))
    if (degree[degree > 0] != 2).any():
        return CapResult(CAP_OPEN, _empty())

    loopData = vtkPolyData()
    loopPoints = vtkPoints()
    loopPoints.SetData(numpy_to_vtk(np.ascontiguousarray(points), deep=True))
    loopData.SetPoints(loopPoints)
    cells = vtkCellArray()
    for a, b in segments:
        cells.InsertNextCell(2)
        cells.InsertCellPoint(int(a))
        cells.InsertCellPoint(int(b))
    loopData.SetLines(cells)

    fill = vtkContourTriangulator()
    fill.SetInputData(loopData)
    fill.Update()
    if fill.GetTriangulationError() or not fill.GetOutput().GetNumberOfCells():
        return CapResult(CAP_OPEN, _empty())
    last = fill
    for plane in trims or ():
        clip = vtkClipPolyData()
        clip.SetInputConnection(last.GetOutputPort())
        clip.SetClipFunction(plane)
        clip.InsideOutOff()
        last = clip
    last.Update()
    result = vtkPolyData()
    result.DeepCopy(last.GetOutput())
    status = CAP_APPROXIMATE if approximate else CAP_CLOSED
    return CapResult(status, result, _loopCount(segments))


def fillArea(polyData) -> float:
    """The area a fill covers (0 for an empty one)."""
    if polyData is None or not polyData.GetNumberOfCells():
        return 0.0
    from vtkmodules.vtkFiltersCore import vtkMassProperties

    mass = vtkMassProperties()
    mass.SetInputData(polyData)
    mass.Update()
    return float(mass.GetSurfaceArea())


class SectionFill:
    """One surface's filled section, drawn as an actor that follows a plane.

    The actor takes no pick and never widens a camera fit (DP-695's lesson:
    only handles grab). It is hidden until a plane is given.
    """

    def __init__(self, surface, colour='#3f8ae0', opacity=FILL_OPACITY):
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
        from foammesh.view.theming.vtk_theme import rgb

        self._surface = surface
        self._plane = None
        self._data = vtkPolyData()
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(self._data)
        # The fill lies on the cut face of the geometry; pulled forward so
        # the two do not fight for the same depth.
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('sectionFill')
        prop = actor.GetProperty()
        prop.SetColor(*rgb(colour))
        prop.SetOpacity(opacity)
        prop.SetLighting(False)
        actor.PickableOff()
        actor.UseBoundsOff()
        actor.VisibilityOff()
        self._actor = actor

    def actor(self):
        return self._actor

    def polyData(self):
        return self._data

    def plane(self):
        """``(origin, normal)`` the fill was last cut on, or ``None``."""
        return self._plane

    def area(self) -> float:
        return fillArea(self._data) if self._plane is not None else 0.0

    def setColour(self, colour) -> None:
        from foammesh.view.theming.vtk_theme import rgb
        self._actor.GetProperty().SetColor(*rgb(colour))

    def setSurface(self, surface) -> None:
        """A new surface for the same space; re-cut on the current plane."""
        self._surface = surface
        if self._plane is not None:
            self.setPlane(*self._plane)

    def setPlane(self, origin, normal) -> None:
        """Cut the surface on this plane and show the filled section."""
        self._plane = (tuple(float(v) for v in origin),
                       tuple(float(v) for v in normal))
        self._data.DeepCopy(sectionFill(self._surface, *self._plane))
        self._data.Modified()
        self._actor.SetVisibility(self._data.GetNumberOfCells() > 0)

    def clear(self) -> None:
        """No plane: nothing is drawn."""
        self._plane = None
        self._actor.VisibilityOff()


class SectionCapActor:
    """Plan 37 UF6. One cap the section tool draws on a cut face.

    Like `SectionFill`'s actor: no pick, no part in a camera fit, and drawn
    forward of the cut face it lies on. It carries no quality colours -- a
    cap is not cells.
    """

    def __init__(self, colour='#9aa4b2'):
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
        from foammesh.view.theming.vtk_theme import rgb

        self._data = vtkPolyData()
        self._result = None
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(self._data)
        mapper.ScalarVisibilityOff()
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('sectionCap')
        actor.GetProperty().SetColor(*rgb(colour))
        actor.PickableOff()
        actor.UseBoundsOff()
        actor.VisibilityOff()
        self._actor = actor

    def actor(self):
        return self._actor

    def polyData(self):
        return self._data

    def result(self):
        """The `CapResult` shown, or ``None``."""
        return self._result

    def setColour(self, colour) -> None:
        from foammesh.view.theming.vtk_theme import rgb
        self._actor.GetProperty().SetColor(*rgb(colour))

    def show(self, result: CapResult) -> None:
        self._result = result
        self._data.DeepCopy(result.polyData)
        self._data.Modified()
        self._actor.SetVisibility(self._data.GetNumberOfCells() > 0)

    def clear(self) -> None:
        self._result = None
        self._data.Initialize()
        self._data.Modified()
        self._actor.VisibilityOff()
