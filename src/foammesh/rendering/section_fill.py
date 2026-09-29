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
"""
from __future__ import annotations

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
