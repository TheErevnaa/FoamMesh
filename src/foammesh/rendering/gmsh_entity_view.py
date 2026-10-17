"""The Gmsh model in the viewport, picked entity by entity (Plan 41 GN4).

``geometry.vtp`` (written by the resident session's hand-over) carries the
Gmsh entity dimension and tag on every cell.  This view draws it, lets a
:class:`~foammesh.core.gmsh.session_entities.EntityPicker` resolve what is under
the cursor, highlights the hovered entity and the picked ones, and draws the
entity labels.  Hover and click arrive through the interactor; Enter or ``e``
accepts and Escape or ``q`` aborts, as in Gmsh's own GUI.

It touches the scene through the viewport widget only (``addActor``,
``removeActor``, ``refresh``, ``renderer``, ``interactor``), like the cell
focus and the region labels.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PRIORITY = 1.5
_HOVER = (1.0, 0.78, 0.1)
_CHOSEN = (0.15, 0.7, 1.0)
_BODY = (0.62, 0.66, 0.72)
_POINT_SIZE = 8.0
_LABEL_FONT = 12
#: A click on a surface picks the point entity nearer than this fraction of
#: the model's diagonal.
POINT_TOLERANCE_FRACTION = 0.03


class GmshEntityView:
    """Draw, pick and label the entities of one Gmsh model."""

    def __init__(self, view, picker):
        self._view = view
        self._picker = picker
        self._polydata = None
        self._body = None
        self._points_actor = None
        self._hover_actor = None
        self._chosen_actors: list = []
        self._label_actors: list = []
        self._locator = None
        self._observers: list = []
        self._labels_on = False
        picker.subscribe(self._onPickerChange)

    # -- scene -------------------------------------------------------------- #

    def show(self, vtp_path) -> int:
        """Load ``geometry.vtp`` and draw it; returns the number of cells."""
        from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
        from vtkmodules.vtkCommonDataModel import vtkCellLocator
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        self.clear()
        reader = vtkXMLPolyDataReader()
        reader.SetFileName(str(vtp_path))
        reader.Update()
        data = reader.GetOutput()
        self._polydata = data
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('gmshEntityBody')
        prop = actor.GetProperty()
        prop.SetColor(*_BODY)
        prop.SetLineWidth(2.0)
        prop.SetOpacity(0.9)
        prop.EdgeVisibilityOff()
        self._body = actor
        self._view.addActor(actor)
        self._drawPoints()
        # The model's own cells are intersected with the pick ray, so the id
        # that comes back is a cell of ``geometry.vtp`` and nothing else
        # (labels, the section plane and the seed gizmo are never hit).
        locator = vtkCellLocator()
        locator.SetDataSet(data)
        locator.BuildLocator()
        self._locator = locator
        bounds = data.GetBounds()
        diagonal = ((bounds[1] - bounds[0]) ** 2 + (bounds[3] - bounds[2]) ** 2
                    + (bounds[5] - bounds[4]) ** 2) ** 0.5
        self._picker.point_tolerance = diagonal * POINT_TOLERANCE_FRACTION
        self._attach()
        self._view.refresh()
        return data.GetNumberOfCells()

    def _drawPoints(self) -> None:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        records = [r for r in self._picker.map.records.values()
                   if r.dim == 0 and r.xyz is not None]
        if not records:
            return
        points, cells = vtkPoints(), vtkCellArray()
        for record in records:
            cells.InsertNextCell(1)
            cells.InsertCellPoint(points.InsertNextPoint(*record.xyz))
        data = vtkPolyData()
        data.SetPoints(points)
        data.SetVerts(cells)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('gmshEntityPoints')
        actor.PickableOff()
        actor.GetProperty().SetPointSize(_POINT_SIZE)
        actor.GetProperty().SetColor(*_BODY)
        actor.GetProperty().SetRenderPointsAsSpheres(True)
        self._points_actor = actor
        self._view.addActor(actor)

    def clear(self) -> None:
        self._detach()
        for actor in (self._body, self._points_actor, self._hover_actor,
                      *self._chosen_actors, *self._label_actors):
            if actor is not None:
                self._view.removeActor(actor)
        self._body = self._points_actor = self._hover_actor = None
        self._chosen_actors, self._label_actors = [], []
        self._polydata = None

    # -- highlight ---------------------------------------------------------- #

    def cellsFor(self, entity) -> list:
        """The rendered cells that stand for *entity* (a volume: its faces)."""
        entity = tuple(entity)
        entity_map = self._picker.map
        if entity[0] == 3:
            cells = []
            for key, record in entity_map.records.items():
                if key[0] == 2 and entity[1] in record.up:
                    cells.extend(entity_map.cells_of(key))
            return cells
        return entity_map.cells_of(entity)

    def _highlight(self, entity, colour, name):
        from vtkmodules.vtkCommonCore import vtkIdList
        from vtkmodules.vtkFiltersCore import vtkAppendFilter
        from vtkmodules.vtkRenderingCore import vtkActor, vtkDataSetMapper
        from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid

        if self._polydata is None:
            return None
        entity = tuple(entity)
        if entity[0] == 0:
            record = self._picker.map.records.get(entity)
            if record is None or record.xyz is None:
                return None
            return self._pointHighlight(record.xyz, colour, name)
        ids = self.cellsFor(entity)
        if not ids:
            return None
        grid = vtkUnstructuredGrid()
        grid.SetPoints(self._polydata.GetPoints())
        id_list = vtkIdList()
        for cell in ids:
            self._polydata.GetCellPoints(int(cell), id_list)
            grid.InsertNextCell(self._polydata.GetCellType(int(cell)), id_list)
        mapper = vtkDataSetMapper()
        mapper.SetInputData(grid)
        mapper.ScalarVisibilityOff()
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName(name)
        actor.PickableOff()
        actor.GetProperty().SetColor(*colour)
        actor.GetProperty().SetLineWidth(5.0)
        return actor

    def _pointHighlight(self, xyz, colour, name):
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        points, cells = vtkPoints(), vtkCellArray()
        cells.InsertNextCell(1)
        cells.InsertCellPoint(points.InsertNextPoint(*xyz))
        data = vtkPolyData()
        data.SetPoints(points)
        data.SetVerts(cells)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName(name)
        actor.PickableOff()
        actor.GetProperty().SetColor(*colour)
        actor.GetProperty().SetPointSize(_POINT_SIZE * 1.8)
        actor.GetProperty().SetRenderPointsAsSpheres(True)
        return actor

    def _onPickerChange(self, what: str) -> None:
        if self._body is None:
            return
        if what in ('hover', 'begin', 'accept', 'abort', 'filter',
                    'selection'):
            if self._hover_actor is not None:
                self._view.removeActor(self._hover_actor)
                self._hover_actor = None
            hovered = self._picker.hovered
            if hovered is not None and hovered not in self._picker.selection:
                self._hover_actor = self._highlight(hovered, _HOVER,
                                                    'gmshEntityHover')
                if self._hover_actor is not None:
                    self._view.addActor(self._hover_actor)
        if what in ('selection', 'begin', 'accept', 'abort', 'filter'):
            for actor in self._chosen_actors:
                self._view.removeActor(actor)
            self._chosen_actors = []
            for entity in self._picker.selection:
                actor = self._highlight(entity, _CHOSEN, 'gmshEntityChosen')
                if actor is not None:
                    self._chosen_actors.append(actor)
                    self._view.addActor(actor)
        self._view.refresh()

    # -- labels ------------------------------------------------------------- #

    def setLabels(self, on: bool, kinds=None) -> int:
        """Show or hide the entity labels; returns how many are drawn."""
        from vtkmodules.vtkRenderingCore import vtkTextActor
        from foammesh.core.gmsh.session_entities import entity_labels

        for actor in self._label_actors:
            self._view.removeActor(actor)
        self._label_actors = []
        self._labels_on = bool(on)
        if not on:
            self._view.refresh()
            return 0
        args = (kinds,) if kinds else ()
        for label in entity_labels(self._picker.map, *args):
            actor = vtkTextActor()
            actor.SetInput(label.text)
            anchor = actor.GetPositionCoordinate()
            anchor.SetCoordinateSystemToWorld()
            anchor.SetValue(*label.xyz)
            actor.SetObjectName('gmshEntityLabel')
            prop = actor.GetTextProperty()
            prop.SetFontSize(_LABEL_FONT)
            prop.SetColor(1.0, 1.0, 1.0)
            prop.SetBackgroundColor(0.1, 0.12, 0.15)
            prop.SetBackgroundOpacity(0.7)
            prop.SetJustificationToCentered()
            actor.PickableOff()
            self._label_actors.append(actor)
            self._view.addActor(actor)
        self._view.refresh()
        return len(self._label_actors)

    @property
    def labelsOn(self) -> bool:
        return self._labels_on

    # -- the interactor ----------------------------------------------------- #

    def _interactor(self):
        interactor = getattr(self._view, 'interactor', None)
        return interactor() if callable(interactor) else None

    def _attach(self) -> None:
        interactor = self._interactor()
        if interactor is None or not hasattr(interactor, 'AddObserver'):
            return
        for event, callback in (('MouseMoveEvent', self._moved),
                                ('LeftButtonPressEvent', self._pressed),
                                ('KeyPressEvent', self._key)):
            self._observers.append((interactor, interactor.AddObserver(
                event, callback, _PRIORITY)))

    def _detach(self) -> None:
        for subject, tag in self._observers:
            try:
                subject.RemoveObserver(tag)
            except Exception:                                 # noqa: BLE001
                pass
        self._observers = []

    def _take(self, interactor) -> None:
        for subject, tag in self._observers:
            if subject is interactor:
                command = interactor.GetCommand(tag)
                if command is not None:
                    command.SetAbortFlag(1)

    def pickAt(self, x: float, y: float):
        """``(cell, world)`` under a display point, ``(None, None)`` if none."""
        from vtkmodules.vtkCommonCore import mutable

        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is None or self._locator is None:
            return None, None
        ends = []
        for depth in (0.0, 1.0):
            renderer.SetDisplayPoint(float(x), float(y), depth)
            renderer.DisplayToWorld()
            w = renderer.GetWorldPoint()
            ends.append([w[i] / (w[3] or 1.0) for i in range(3)])
        # Lines are thin: widen the tolerance to a few pixels' worth.
        span = sum((ends[1][i] - ends[0][i]) ** 2 for i in range(3)) ** 0.5
        tolerance = (self._polydata.GetLength() * 0.004) if span else 0.0
        t, point, pcoords = mutable(0.0), [0.0] * 3, [0.0] * 3
        sub, cell = mutable(0), mutable(0)
        hit = self._locator.IntersectWithLine(
            ends[0], ends[1], tolerance, t, point, pcoords, sub, cell)
        if not hit:
            return None, None
        return int(cell), tuple(point)

    def _moved(self, interactor, event) -> None:
        if not self._picker.active:
            return
        x, y = interactor.GetEventPosition()
        cell, world = self.pickAt(x, y)
        self._picker.hover(cell, world)

    def _pressed(self, interactor, event) -> None:
        if not self._picker.active:
            return
        x, y = interactor.GetEventPosition()
        cell, world = self.pickAt(x, y)
        shift = bool(interactor.GetShiftKey())
        if self._picker.click(cell, world, shift=shift) is not None:
            self._take(interactor)

    def _key(self, interactor, event) -> None:
        if not self._picker.active:
            return
        key = interactor.GetKeySym()
        if self._picker.key(key):
            self._take(interactor)

    def dispose(self) -> None:
        self.clear()
