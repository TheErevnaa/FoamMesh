"""Drawing what the Gmsh panels preview (Plan 48 GM11b; DP-4216, DP-4262).

Three panels of the Gmsh route compute something to look at and leave the
viewport to the window:

* the Geometry build panel's ``sweepPreview`` -- the segments an extrusion or
  a sweep would sweep out (GM6);
* the Structure panel's ``qualificationChanged`` -- the colour of each volume
  or surface that can or cannot be made transfinite (GM6);
* Reclassify 2D's edge preview -- the surface-mesh edges above the feature
  angle (GM7).

Each is a layer of actors on the viewport, named so a test finds it, that
leaves the model's own actors alone and is removed whole.  The functions
that turn a payload into points and colours need no window.
"""
from __future__ import annotations

import numpy as np

SWEEP_NAME = 'gmshSweepPreview'
QUALIFY_NAME = 'gmshQualificationPaint'
RECLASSIFY_NAME = 'gmshReclassifyEdges'
SWEEP_COLOUR = (0.95, 0.55, 0.10)
RECLASSIFY_COLOUR = (0.90, 0.15, 0.55)


def segment_arrays(segments) -> np.ndarray:
    """``(n, 2, 3)`` float array from ``[[p0, p1], ...]``; bad rows dropped."""
    rows = []
    for item in segments or ():
        try:
            a, b = item
            row = [[float(v) for v in a], [float(v) for v in b]]
        except (TypeError, ValueError):
            continue
        if len(row[0]) == 3 and len(row[1]) == 3 \
                and np.isfinite(row).all():
            rows.append(row)
    return np.asarray(rows, dtype=float).reshape(-1, 2, 3)


def unit_colour(rgb) -> tuple:
    """RGB 0-255 (or 0-1) as three floats 0-1."""
    values = [float(v) for v in rgb][:3]
    if max(values) > 1.0:
        values = [v / 255.0 for v in values]
    return tuple(min(1.0, max(0.0, v)) for v in values)


def surfaces_of(entity_map, tag: int) -> tuple:
    """``(dim, [surface tags])`` painted for the entity numbered *tag*.

    A volume is painted through the surfaces that bound it (their ``up``
    lists the volumes); a tag that is no volume is taken as a surface.
    """
    records = entity_map.records
    if (3, tag) in records:
        return 3, [key[1] for key, record in records.items()
                   if key[0] == 2 and tag in (record.up or ())]
    return 2, [tag] if (2, tag) in records or entity_map.cells_of((2, tag)) \
        else []


class _Layer:
    """Actors kept on a view under one name."""

    def __init__(self, view_provider, name: str):
        self._view = view_provider
        self.name = name
        self._actors: list = []

    def actors(self) -> list:
        return list(self._actors)

    def clear(self) -> None:
        view = self._view()
        for actor in self._actors:
            if view is not None:
                view.removeActor(actor)
        had = bool(self._actors)
        self._actors = []
        self._forget()
        if had and view is not None:
            view.refresh()

    def _forget(self) -> None:
        """What a layer remembers of the drawing it just removed."""

    def _add(self, actor) -> None:
        actor.SetObjectName(self.name)
        actor.PickableOff()
        view = self._view()
        if view is not None:
            view.addActor(actor)
        self._actors.append(actor)

    def _finish(self) -> None:
        view = self._view()
        if view is not None:
            view.refresh()


def _line_actor(segments: np.ndarray, colour, width: float):
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

    points = vtkPoints()
    points.SetData(numpy_to_vtk(segments.reshape(-1, 3).copy(), deep=True))
    cells = vtkCellArray()
    for index in range(len(segments)):
        cells.InsertNextCell(2)
        cells.InsertCellPoint(2 * index)
        cells.InsertCellPoint(2 * index + 1)
    data = vtkPolyData()
    data.SetPoints(points)
    data.SetLines(cells)
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(data)
    actor = vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    prop.SetColor(*colour)
    prop.SetLineWidth(width)
    prop.SetLighting(False)
    return actor


class SegmentLayer(_Layer):
    """Line segments in one colour: the sweep preview, the reclassify edges."""

    def __init__(self, view_provider, name: str, colour, width: float = 2.0):
        super().__init__(view_provider, name)
        self._colour = colour
        self._width = width
        self.count = 0

    def _forget(self) -> None:
        self.count = 0

    def show(self, segments) -> int:
        """Draw *segments* in place of the last ones; how many were drawn."""
        self.clear()
        rows = segment_arrays(segments)
        self.count = len(rows)
        if len(rows):
            self._add(_line_actor(rows, self._colour, self._width))
        self._finish()
        return self.count


class QualificationLayer(_Layer):
    """The Structure panel's green and red: each entity's surfaces painted."""

    def __init__(self, view_provider, polydata, entity_map):
        super().__init__(view_provider, QUALIFY_NAME)
        self._polydata = polydata
        self._map = entity_map
        self.painted: dict = {}

    def _forget(self) -> None:
        self.painted = {}

    def show(self, colours) -> dict:
        """Paint ``{tag: rgb}``; ``{tag: [cell ids]}`` of what was painted."""
        self.clear()
        self.painted = {}
        data = self._polydata()
        entity_map = self._map()
        if data is None or entity_map is None:
            self._finish()
            return {}
        for tag, rgb in sorted((colours or {}).items()):
            _dim, surfaces = surfaces_of(entity_map, int(tag))
            cells = [c for s in surfaces
                     for c in entity_map.cells_of((2, s))]
            if not cells:
                continue
            self.painted[int(tag)] = cells
            self._add(_cells_actor(data, cells, unit_colour(rgb)))
        self._finish()
        return dict(self.painted)


def _cells_actor(data, cells, colour):
    from vtkmodules.vtkCommonCore import vtkIdList
    from vtkmodules.vtkFiltersCore import vtkExtractCells
    from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

    ids = vtkIdList()
    for cell in cells:
        ids.InsertNextId(int(cell))
    extract = vtkExtractCells()
    extract.SetInputData(data)
    extract.SetCellList(ids)
    extract.Update()
    surface = vtkGeometryFilter()
    surface.SetInputConnection(extract.GetOutputPort())
    surface.Update()
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(surface.GetOutput())
    mapper.SetResolveCoincidentTopologyToPolygonOffset()
    mapper.ScalarVisibilityOff()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*colour)
    actor.GetProperty().SetOpacity(0.85)
    return actor
