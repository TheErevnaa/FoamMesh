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
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

logger = logging.getLogger(__name__)

#: Plan 50 IP12 (DP-6346). How long the pointer rests before a hover casts
#: its ray. Every move event used to cast one and rebuild the highlight; 0
#: picks on the move, as before.
HOVER_DWELL_MS = 40
#: Plan 50 IP12 (DP-6345). Hover highlights kept, so hovering back and forth
#: between neighbours does not walk their cells again.
_HOVER_CACHE = 16

_PICK_POOL = None
_PICK_POOL_LOCK = threading.Lock()


def _pick_pool() -> ThreadPoolExecutor:
    global _PICK_POOL
    with _PICK_POOL_LOCK:
        if _PICK_POOL is None:
            _PICK_POOL = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix='foammesh-gmsh-pick')
        return _PICK_POOL


def pick_snapshot(data, entity_map, hidden) -> dict:
    """What :func:`build_pick_structures` reads, taken on the GUI thread.

    Plan 50 IP12 (DP-6347). Views of the model's own arrays (no copy); the
    data set is held so they stay alive while the worker reads them. The
    worker builds new VTK objects from them and touches nothing the
    renderer holds.
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy

    points = data.GetPoints()
    blocks = []
    for getter in (data.GetVerts, data.GetLines, data.GetPolys,
                   data.GetStrips):
        cells = getter()
        if cells is None or cells.GetNumberOfCells() == 0:
            blocks.append(None)
            continue
        blocks.append((vtk_to_numpy(cells.GetOffsetsArray()),
                       vtk_to_numpy(cells.GetConnectivityArray())))
    return {
        'source': data,
        'points': (vtk_to_numpy(points.GetData())
                   if points is not None and points.GetNumberOfPoints()
                   else None),
        'blocks': blocks,
        'count': int(data.GetNumberOfCells()),
        'cell_dim': np.asarray(entity_map.cell_dim),
        'cell_tag': np.asarray(entity_map.cell_tag),
        'hidden': tuple(tuple(int(v) for v in entity) for entity in hidden),
    }


def _subset(block, local):
    """Offsets and connectivity of the cells *local* of one cell array."""
    offsets, connectivity = block
    starts = offsets[local]
    lengths = offsets[local + 1] - starts
    new_offsets = np.zeros(len(local) + 1, dtype=np.int64)
    np.cumsum(lengths, out=new_offsets[1:])
    total = int(new_offsets[-1])
    index = (np.repeat(starts - new_offsets[:-1], lengths)
             + np.arange(total, dtype=np.int64))
    return new_offsets, np.asarray(connectivity[index], dtype=np.int64)


def _polydata_of(points, blocks, starts, chosen):
    """A new polydata of the cells *chosen* (a mask over every cell), and
    the original id of each of its cells in its own order."""
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import VTK_ID_TYPE
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    poly = vtkPolyData()
    poly.SetPoints(points)
    order = []
    for block, start, setter in zip(
            blocks, starts, ('SetVerts', 'SetLines', 'SetPolys', 'SetStrips')):
        if block is None:
            continue
        size = len(block[0]) - 1
        local = np.flatnonzero(chosen[start:start + size])
        if not len(local):
            continue
        offsets, connectivity = _subset(block, local)
        cells = vtkCellArray()
        cells.SetData(
            numpy_to_vtk(offsets, deep=1, array_type=VTK_ID_TYPE),
            numpy_to_vtk(connectivity, deep=1, array_type=VTK_ID_TYPE))
        getattr(poly, setter)(cells)
        order.append(local + start)
    ids = np.concatenate(order) if order else np.zeros(0, dtype=np.int64)
    return poly, ids


def build_pick_structures(snapshot) -> dict:
    """The pick ray's locators, built from :func:`pick_snapshot`.

    Plan 50 IP12 (DP-6347): runs on a worker thread. Every VTK object made
    here is new and is not in the renderer; the GUI thread adopts them. The
    same structures as before: the visible curve cells and the visible
    surface cells apart (DP-6154), the visible body (DP-6153) when anything
    is hidden, and the whole model.
    """
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellLocator

    count = snapshot['count']
    points = vtkPoints()
    if snapshot['points'] is not None:
        points.SetData(numpy_to_vtk(
            np.ascontiguousarray(snapshot['points']), deep=1))
    blocks = snapshot['blocks']
    starts, start = [], 0
    for block in blocks:
        starts.append(start)
        start += 0 if block is None else len(block[0]) - 1
    dims = np.full(count, -1, dtype=np.int64)
    tags = np.full(count, -1, dtype=np.int64)
    known = min(count, len(snapshot['cell_dim']), len(snapshot['cell_tag']))
    dims[:known] = snapshot['cell_dim'][:known]
    tags[:known] = snapshot['cell_tag'][:known]
    visible = np.ones(count, dtype=bool)
    if snapshot['hidden']:
        shift = np.int64(1) << np.int64(40)
        hidden = np.array([d * shift + t for d, t in snapshot['hidden']],
                          dtype=np.int64)
        visible = ~np.isin(dims * shift + tags, hidden)
    locators = {}
    for dim in (1, 2):
        chosen = visible & (dims == dim)
        if not chosen.any():
            continue
        part, ids = _polydata_of(points, blocks, starts, chosen)
        locator = vtkCellLocator()
        locator.SetDataSet(part)
        locator.BuildLocator()
        locators[dim] = (locator, part, ids)
    body = None
    if not visible.all():
        body, _ids = _polydata_of(points, blocks, starts, visible)
    whole, _ids = _polydata_of(points, blocks, starts,
                               np.ones(count, dtype=bool))
    locator = vtkCellLocator()
    locator.SetDataSet(whole)
    locator.BuildLocator()
    return {'locators': locators, 'whole': locator, 'body': body}


def _qt_running() -> bool:
    try:
        from PySide6.QtCore import QCoreApplication
    except Exception:                                       # noqa: BLE001
        return False
    return QCoreApplication.instance() is not None


def _bridge_class():
    from PySide6.QtCore import QObject, Signal

    class _PickBridge(QObject):
        """Carries "the worker is done" back to the GUI thread (a signal
        emitted on another thread is queued to the receiver's)."""
        ready = Signal(int)

    return _PickBridge

_PRIORITY = 1.5
#: Pixels the pointer may move between press and release and still be a click.
_CLICK_SLOP = 4
_HOVER = (1.0, 0.78, 0.1)
_CHOSEN = (0.15, 0.7, 1.0)
_BODY = (0.62, 0.66, 0.72)
_POINT_SIZE = 8.0
_LABEL_FONT = 12
#: A click on a surface picks the point entity nearer than this fraction of
#: the model's diagonal.
POINT_TOLERANCE_FRACTION = 0.03
#: Plan 48 GM11a. Gmsh sizes its normals, tangents and 3D curves in pixels;
#: one pixel is this fraction of the model's diagonal here (the model is taken
#: to fill about 800 pixels).
PIXEL_FRACTION = 1.0 / 800.0
#: A surface's normals are drawn at up to this many of its cells.
NORMALS_PER_SURFACE = 9
#: The size of a surface cross and a volume marker, in pixels.
MARKER_PIXELS = 12.0
_LABEL_KIND = {0: 'Points', 1: 'Curves', 2: 'Surfaces', 3: 'Volumes'}
_LABEL_OPTION = {0: 'Geometry.PointLabels', 1: 'Geometry.CurveLabels',
                 2: 'Geometry.SurfaceLabels', 3: 'Geometry.VolumeLabels'}


def _unit(rgba) -> tuple:
    """A Gmsh colour (0-255) as the 0-1 triple VTK takes."""
    return tuple(float(c) / 255.0 for c in rgba[:3])


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
        self._order_actors: list = []
        self._tip_actor = None
        self._cursor = (0, 0)
        self._locator = None
        #: dim -> (vtkCellLocator, original cell ids) for the curve cells and
        #: the surface cells apart (DP-6154).
        self._locators: dict = {}
        self._observers: list = []
        self._press = None
        self._band_actor = None
        self._labels_on = False
        #: Plan 48 GM11a. The values of the Gmsh ``Geometry.*`` options and
        #: the planes of ``Geometry.Clip``; ``None`` draws the legacy way.
        self._settings = None
        self._planes: list = []
        self._geo: dict = {}
        self.report: dict = {}
        #: Plan 48 OP. A Surface opacity set on this view; ``None`` follows
        #: the route-wide value of the viewport rail's Surface opacity tool.
        self._surface_opacity = None
        self._route_subscribed = False
        #: Plan 50 IP12. The one hover highlight and the one tip actor, kept
        #: while out of the scene (``_hover_actor`` / ``_tip_actor`` are set
        #: only while they are in it); hover data by entity; the dwell.
        self._hover_spare = None
        self._tip_spare = None
        self._hover_data: dict = {}
        self._hover_timer = None
        self._hover_at = None
        #: ``(generation, future)`` of the pick structures being built.
        self._pick_future = None
        self._pick_generation = 0
        self._pick_bridge = None
        picker.subscribe(self._onPickerChange)
        self._follow_route()

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
        self._rebuild_pick()
        bounds = data.GetBounds()
        diagonal = ((bounds[1] - bounds[0]) ** 2 + (bounds[3] - bounds[2]) ** 2
                    + (bounds[5] - bounds[4]) ** 2) ** 0.5
        self._picker.point_tolerance = diagonal * POINT_TOLERANCE_FRACTION
        self._attach()
        if self._settings is not None:
            self._draw_geometry()
        self._view.refresh()
        return data.GetNumberOfCells()

    def _rebuild_pick(self) -> None:
        """The locators the pick ray is cast against: the visible curve cells
        and the visible surface cells apart (DP-6154, so a curve under a
        surface can be picked), plus the plain body without the hidden
        entities (DP-6153).

        Plan 50 IP12 (DP-6347). Built by :func:`build_pick_structures` on a
        worker thread when a Qt application runs (the result comes back to
        the GUI thread, which hands the body its new input); in place
        otherwise. Until they land a hover uses the last structures (the
        picker refuses what is hidden) or finds nothing, and a click waits.
        """
        data = self._polydata
        if data is None:
            return
        self._pick_generation += 1
        generation = self._pick_generation
        snapshot = pick_snapshot(data, self._picker.map, self._picker.hidden)
        if not _qt_running():
            self._pick_future = None
            self._adopt_pick(generation, build_pick_structures(snapshot))
            return
        bridge = self._bridge()
        future = _pick_pool().submit(build_pick_structures, snapshot)
        self._pick_future = (generation, future)

        def done(_future, generation=generation):
            try:
                bridge.ready.emit(generation)
            except RuntimeError:            # the view went first
                pass

        future.add_done_callback(done)

    def _bridge(self):
        if self._pick_bridge is None:
            self._pick_bridge = _bridge_class()()
            self._pick_bridge.ready.connect(self._pick_ready)
        return self._pick_bridge

    def _pick_ready(self, generation: int) -> None:
        pending = self._pick_future
        if pending is None or pending[0] != generation:
            return
        if self._collect_pick(wait=False):
            self._view.refresh()

    def _collect_pick(self, wait: bool) -> bool:
        """Adopt the structures the worker built; True when they changed."""
        pending = self._pick_future
        if pending is None:
            return False
        generation, future = pending
        if not wait and not future.done():
            return False
        self._pick_future = None
        try:
            built = future.result()
        except Exception:                                   # noqa: BLE001
            logger.exception('building the Gmsh pick structures failed')
            return False
        return self._adopt_pick(generation, built)

    def _adopt_pick(self, generation: int, built: dict) -> bool:
        """On the GUI thread: the locators, and the body's input."""
        if generation != self._pick_generation or self._polydata is None:
            return False                    # a newer build or a cleared model
        self._locators = built['locators']
        self._locator = built['whole']
        if self._body is not None:
            self._body.GetMapper().SetInputData(
                built['body'] if built['body'] is not None
                else self._polydata)
        return True

    def setHidden(self) -> None:
        """Draw and pick again without the entities the picker hides."""
        if self._polydata is None:
            return
        self._rebuild_pick()
        if self._points_actor is not None:
            self._view.removeActor(self._points_actor)
            self._points_actor = None
        self._drawPoints()
        if self._settings is not None:
            self._draw_geometry()
        self._view.refresh()

    def _show_tip(self, entity) -> None:
        """DP-6160. The entity under the cursor, as ``Curve 12 (inlet)``,
        beside the cursor. Plan 50 IP12 (DP-6345): one text actor, kept."""
        from vtkmodules.vtkRenderingCore import vtkTextActor
        from foammesh.core.gmsh.session_entities import chip_text

        if entity is None:
            if self._tip_actor is not None:
                self._view.removeActor(self._tip_actor)
                self._tip_actor = None
            return
        actor = self._tip_spare
        if actor is None:
            actor = vtkTextActor()
            actor.SetObjectName('gmshEntityTip')
            prop = actor.GetTextProperty()
            prop.SetFontSize(_LABEL_FONT)
            prop.SetColor(1.0, 1.0, 1.0)
            prop.SetBackgroundColor(0.1, 0.12, 0.15)
            prop.SetBackgroundOpacity(0.85)
            actor.PickableOff()
            self._tip_spare = actor
        actor.SetInput(chip_text(entity, self._picker.map.name_of(entity)))
        x, y = self._cursor
        actor.SetPosition(x + 14, y + 14)
        if self._tip_actor is None:
            self._tip_actor = actor
            self._view.addActor(actor)

    def _number_picks(self) -> None:
        """DP-6158. Number the picked entities 1, 2, 3 ... in the order they
        were picked, which is the order a transfinite surface's corners and a
        loop's curves are taken in."""
        from vtkmodules.vtkRenderingCore import vtkTextActor

        for actor in self._order_actors:
            self._view.removeActor(actor)
        self._order_actors = []
        if self._polydata is None:
            return
        entity_map = self._picker.map
        for number, entity in enumerate(self._picker.selection, start=1):
            where = entity_map.anchor(entity)
            if where is None:
                continue
            actor = vtkTextActor()
            actor.SetInput(str(number))
            actor.SetObjectName('gmshEntityOrder')
            position = actor.GetPositionCoordinate()
            position.SetCoordinateSystemToWorld()
            position.SetValue(*where)
            prop = actor.GetTextProperty()
            prop.SetFontSize(_LABEL_FONT + 4)
            prop.SetBold(True)
            prop.SetColor(1.0, 0.95, 0.4)
            prop.SetBackgroundColor(0.1, 0.12, 0.15)
            prop.SetBackgroundOpacity(0.8)
            prop.SetJustificationToCentered()
            actor.PickableOff()
            self._order_actors.append(actor)
            self._view.addActor(actor)

    def _drawPoints(self) -> None:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        records = self._point_records()
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
        self._drop_geometry()
        for actor in (self._body, self._points_actor, self._hover_actor,
                      *self._chosen_actors, *self._label_actors,
                      *self._order_actors, self._tip_actor):
            if actor is not None:
                self._view.removeActor(actor)
        self._body = self._points_actor = self._hover_actor = None
        self._chosen_actors, self._label_actors = [], []
        self._order_actors, self._tip_actor = [], None
        self._hover_spare = self._tip_spare = None
        self._hover_data = {}
        if self._hover_timer is not None:
            self._hover_timer.stop()
        self._hover_at = None
        self._polydata = None
        self._locator, self._locators = None, {}
        # A build still on the worker is for this model: drop what it brings.
        self._pick_generation += 1
        self._pick_future = None
        from foammesh.rendering import actor_info

        actor_info.setSourceTranslucent(self, False)
        actor_info.refreshTranslucency(self._view)

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
        actor.GetProperty().SetLineWidth(self._select_width())
        self._place(actor)
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
        actor.GetProperty().SetPointSize(self._select_size())
        actor.GetProperty().SetRenderPointsAsSpheres(True)
        self._place(actor)
        return actor

    def _hover_dataset(self, entity):
        """The cells (or the one point) the hover highlight draws for
        *entity*, built once per entity and kept (DP-6345)."""
        from vtkmodules.vtkCommonCore import vtkIdList, vtkPoints
        from vtkmodules.vtkCommonDataModel import (
            VTK_VERTEX, vtkUnstructuredGrid)

        entity = tuple(entity)
        grid = self._hover_data.get(entity)
        if grid is not None:
            return grid
        if entity[0] == 0:
            record = self._picker.map.records.get(entity)
            if record is None or record.xyz is None:
                return None
            points = vtkPoints()
            points.InsertNextPoint(*record.xyz)
            grid = vtkUnstructuredGrid()
            grid.SetPoints(points)
            single = vtkIdList()
            single.InsertNextId(0)
            grid.InsertNextCell(VTK_VERTEX, single)
        else:
            ids = self.cellsFor(entity)
            if not ids:
                return None
            grid = vtkUnstructuredGrid()
            grid.SetPoints(self._polydata.GetPoints())
            id_list = vtkIdList()
            for cell in ids:
                self._polydata.GetCellPoints(int(cell), id_list)
                grid.InsertNextCell(self._polydata.GetCellType(int(cell)),
                                    id_list)
        if len(self._hover_data) >= _HOVER_CACHE:
            self._hover_data.pop(next(iter(self._hover_data)))
        self._hover_data[entity] = grid
        return grid

    def _show_hover(self, entity) -> bool:
        """Light *entity* with the one hover actor; False if it has nothing
        to draw. Plan 50 IP12 (DP-6345): the actor and its mapper are made
        once and take each hovered entity as new input."""
        from vtkmodules.vtkRenderingCore import vtkActor, vtkDataSetMapper

        if self._polydata is None:
            return False
        grid = self._hover_dataset(entity)
        if grid is None:
            return False
        actor = self._hover_spare
        if actor is None:
            mapper = vtkDataSetMapper()
            mapper.ScalarVisibilityOff()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.SetObjectName('gmshEntityHover')
            actor.PickableOff()
            actor.GetProperty().SetColor(*_HOVER)
            actor.GetProperty().SetRenderPointsAsSpheres(True)
            self._hover_spare = actor
        actor.GetMapper().SetInputData(grid)
        prop = actor.GetProperty()
        prop.SetLineWidth(self._select_width())
        prop.SetPointSize(self._select_size())
        self._place(actor)
        if self._hover_actor is None:
            self._hover_actor = actor
            self._view.addActor(actor)
        return True

    def _onPickerChange(self, what: str) -> None:
        if self._body is None:
            return
        if what == 'hidden':
            self.setHidden()
            return
        if self._settings is not None and what in (
                'begin', 'accept', 'abort', 'filter'):
            self._pick_aid()
        if what in ('hover', 'begin', 'accept', 'abort', 'filter',
                    'selection'):
            hovered = self._picker.hovered
            self._show_tip(hovered)
            shown = (hovered is not None
                     and hovered not in self._picker.selection
                     and self._show_hover(hovered))
            if not shown and self._hover_actor is not None:
                self._view.removeActor(self._hover_actor)
                self._hover_actor = None
        if what in ('selection', 'begin', 'accept', 'abort', 'filter'):
            self._number_picks()
            for actor in self._chosen_actors:
                self._view.removeActor(actor)
            self._chosen_actors = []
            lit = self._picker.selection or self._picker.last_accepted
            for entity in lit:
                actor = self._highlight(entity, self._chosen_colour(),
                                        'gmshEntityChosen')
                if actor is not None:
                    self._chosen_actors.append(actor)
                    self._view.addActor(actor)
        self._view.refresh()

    # -- Geometry.* display options (Plan 48 GM11a) --------------------------- #

    def setSettings(self, settings, planes=None) -> dict:
        """Draw by the values of the Gmsh ``Geometry.*`` options.

        *settings* is a
        :class:`~foammesh.core.gmsh.session_display.GmshDisplaySettings`
        (``None`` goes back to the plain body); *planes* are the Section
        tool's :class:`~foammesh.core.gmsh.session_display.ClipPlane` s, which
        the bits of ``Geometry.Clip`` switch on and off.  Returns what was
        drawn: point, curve and surface cells, crosses, volume markers,
        arrows, labels.
        """
        self._settings = settings
        self._planes = list(planes or [])
        return self._draw_geometry()

    def settings(self):
        return self._settings

    # -- Surface opacity (Plan 48 OP) ------------------------------------------ #

    def _follow_route(self) -> None:
        import weakref

        from foammesh.rendering import actor_info

        if self._route_subscribed:
            return
        me = weakref.ref(self)

        def changed(*_args):
            view = me()
            if view is not None:
                view._route_changed()

        self._route_slot = changed
        actor_info.surfaceOpacityNotifier().changed.connect(changed)
        self._route_subscribed = True

    def _unfollow_route(self) -> None:
        if not self._route_subscribed:
            return
        from foammesh.rendering import actor_info

        self._route_subscribed = False
        try:
            actor_info.surfaceOpacityNotifier().changed.disconnect(
                self._route_slot)
        except (RuntimeError, TypeError):
            pass

    def _route_changed(self, *_args) -> None:
        if self._settings is not None and self._body is not None:
            self.setSurfaceOpacity(self._surface_opacity)

    def surfaceOpacity(self):
        """The opacity the model's surfaces are drawn with: the one set on
        this view, else the Gmsh route's Surface opacity, else ``None`` (the
        alpha of ``Geometry.Color.Surfaces`` alone)."""
        if self._surface_opacity is not None:
            return self._surface_opacity
        from foammesh.rendering import actor_info

        return actor_info.activeSurfaceOpacity('gmsh')

    def setSurfaceOpacity(self, value) -> None:
        """Draw the surfaces *value* (0-1) opaque; ``None`` follows the route.

        The ``Geometry.Color.Surfaces`` option is not rewritten: its alpha
        multiplies this, and every redraw applies it again. While a value is
        in force the surfaces are always shown (``Geometry.Surfaces`` only
        chooses cross, wireframe or solid when it is on).
        """
        from foammesh.rendering import actor_info

        self._surface_opacity = (None if value is None else min(
            1.0, max(actor_info.SURFACE_OPACITY_MIN, float(value))))
        if self._settings is None or self._body is None:
            return
        actor = self._geo.get('gmshGeoSurfaces')
        forced = self._surfaces_forced()
        on = bool(self._get('Geometry.Surfaces'))
        if (actor is None and forced) or (actor is not None and not on
                                          and not forced):
            self._draw_geometry()
            return
        if actor is not None:
            alpha = float(self._settings.colour('Geometry.Color.Surfaces')[3])
            current = self.surfaceOpacity()
            actor.GetProperty().SetOpacity(
                alpha / 255.0 * (1.0 if current is None else current))
        self._pick_aid()
        self._sync_translucency()
        self._view.refresh()

    def _surfaces_forced(self) -> bool:
        """True when the route's Surface opacity shows surfaces the Gmsh
        option has off."""
        return (self._settings is not None
                and self.surfaceOpacity() is not None
                and not self._get('Geometry.Surfaces'))

    def _sync_translucency(self) -> None:
        """Tell the viewport whether a translucent surface is drawn, so it
        depth-peels (or stops) and the points and curves in and behind the
        surfaces are blended in order, not hidden by draw order."""
        from foammesh.rendering import actor_info

        actor = self._geo.get('gmshGeoSurfaces')
        translucent = (actor is not None and actor.GetVisibility()
                       and actor.GetProperty().GetOpacity() < 1.0)
        actor_info.setSourceTranslucent(self, bool(translucent))
        actor_info.refreshTranslucency(self._view)

    def geoActor(self, name: str):
        """One of the actors the Geometry options draw, by its name."""
        return self._geo.get(name)

    def geoActorNames(self) -> list:
        return sorted(self._geo)

    def _get(self, name: str):
        return self._settings.get(name)

    def _select_width(self) -> float:
        if self._settings is None:
            return 5.0
        return float(self._get('Geometry.CurveSelectWidth'))

    def _select_size(self) -> float:
        if self._settings is None:
            return _POINT_SIZE * 1.8
        return float(self._get('Geometry.PointSelectSize'))

    def _chosen_colour(self) -> tuple:
        if self._settings is None:
            return _CHOSEN
        return _unit(self._settings.colour('Geometry.Color.Selection'))

    def _offset(self) -> tuple:
        if self._settings is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(self._get('Geometry.Offset' + axis))
                     for axis in 'XYZ')

    def _place(self, actor) -> None:
        """``Geometry.OffsetX/Y/Z``: the model's display offset."""
        if not actor.IsA('vtkActor2D'):       # a label moves with its points
            actor.SetPosition(*self._offset())

    def _diagonal(self) -> float:
        if self._polydata is None:
            return 1.0
        return float(self._polydata.GetLength()) or 1.0

    def _pixel(self) -> float:
        return self._diagonal() * PIXEL_FRACTION

    def _drop_geometry(self) -> None:
        for actor in self._geo.values():
            self._view.removeActor(actor)
        self._geo = {}

    def _put(self, name: str, actor, report: dict | None = None):
        actor.SetObjectName(name)
        actor.PickableOff()
        self._place(actor)
        self._geo[name] = actor
        self._view.addActor(actor)
        return actor

    def _pick_aid(self) -> None:
        """With the surfaces switched off the body still shows while a pick
        wants surfaces or volumes, so there is something to click; it takes
        ``Geometry.Color.Projection``."""
        if self._body is None or self._settings is None:
            return
        wanted = (not self._get('Geometry.Surfaces')
                  and not self._surfaces_forced()
                  and self._picker.active
                  and self._picker.filter in ('Surfaces', 'Volumes'))
        self._body.SetVisibility(bool(wanted))
        self._body.GetProperty().SetColor(
            *_unit(self._settings.colour('Geometry.Color.Projection')))
        self._place(self._body)
        self._view.refresh()

    def _active_planes(self) -> list:
        from foammesh.core.gmsh import session_display as sd

        if self._settings is None or not self._planes and not int(
                self._get('Geometry.Clip')):
            return []
        planes = sd.planes_with_mask(self._planes, self._get('Geometry.Clip'))
        return [plane for plane in planes if plane.on and plane.valid()]

    def _clip(self, data):
        """The data kept by the planes of ``Geometry.Clip``."""
        planes = self._active_planes()
        if not planes:
            return data
        from vtkmodules.vtkCommonDataModel import vtkPlane
        from vtkmodules.vtkFiltersCore import vtkClipPolyData

        for plane in planes:
            normal = plane.normal() / np.linalg.norm(plane.normal())
            implicit = vtkPlane()
            implicit.SetNormal(*normal)
            implicit.SetOrigin(*(-plane.d / np.linalg.norm(
                plane.normal()) * normal))
            clip = vtkClipPolyData()
            clip.SetInputData(data)
            clip.SetClipFunction(implicit)
            clip.GenerateClipScalarsOff()
            clip.Update()
            data = clip.GetOutput()
        return data

    def _inside(self, where: np.ndarray) -> np.ndarray:
        keep = np.ones(len(where), dtype=bool)
        for plane in self._active_planes():
            keep &= plane.distance(where) >= 0.0
        return keep

    def _classes(self) -> dict:
        """``Geometry.HighlightOrphans``: 0 orphan, 1 boundary (one parent),
        2 more than two parents (a curve three surfaces share), else None.
        Gmsh paints the first two ``Highlight0`` and ``Highlight1``."""
        out = {}
        for key, record in self._picker.map.records.items():
            dim = key[0]
            if dim == 3:
                continue
            parents = len(record.up)
            if parents == 0:
                out[key] = 0
            elif parents == 1:
                out[key] = 1
            elif dim == 1 and parents > 2:
                out[key] = 2
        return out

    def _highlight_colours(self) -> dict:
        s = self._settings
        return {cls: np.array(s.colour('Geometry.Color.Highlight' + word)[:3],
                              dtype=np.uint8)
                for cls, word in enumerate(('Zero', 'One', 'Two'))}

    def _cell_colours(self, dim: int, rows: slice, base) -> np.ndarray | None:
        """RGB per cell with orphans and boundaries painted, ``None`` when
        ``Geometry.HighlightOrphans`` is off."""
        if not self._get('Geometry.HighlightOrphans'):
            return None
        entity_map = self._picker.map
        tags = entity_map.cell_tag[rows]
        colours = np.tile(np.array(base, dtype=np.uint8), (len(tags), 1))
        classes = self._classes()
        palette = self._highlight_colours()
        for tag in np.unique(tags):
            cls = classes.get((dim, int(tag)))
            if cls is not None:
                colours[tags == tag] = palette[cls]
        return colours

    def _draw_geometry(self) -> dict:
        """Rebuild every actor of the Geometry options from the settings."""
        self._drop_geometry()
        report = {}
        self.report = report
        if self._body is None:
            return report
        settings = self._settings
        if settings is None:
            self._body.SetVisibility(True)
            if self._points_actor is not None:
                self._points_actor.SetVisibility(True)
                self._place(self._points_actor)
            self._body.SetPosition(0.0, 0.0, 0.0)
            self._view.refresh()
            return report
        # The plain body stands down; it returns as the pick aid.
        if self._points_actor is not None:
            self._points_actor.SetVisibility(False)
        self._pick_aid()
        for actor in (self._hover_actor, *self._chosen_actors):
            if actor is not None:
                self._place(actor)
        data = self._polydata
        self._draw_points(report)
        self._draw_curves(data, report)
        self._draw_surfaces(data, report)
        self._sync_translucency()
        self._draw_volumes(report)
        self._draw_vectors(data, report)
        self._draw_entity_labels(report)
        self._light(report)
        # Picked entities take the selection colour and size again.
        self._onPickerChange('selection')
        self._view.refresh()
        return report

    # -- points --------------------------------------------------------------- #

    def _point_records(self) -> list:
        hidden = self._picker.hidden
        return [r for r in self._picker.map.records.values()
                if r.dim == 0 and r.xyz is not None
                and (0, r.tag) not in hidden]

    def _draw_points(self, report: dict) -> None:
        if not self._get('Geometry.Points'):
            report['points'] = 0
            return
        from vtkmodules.util.numpy_support import numpy_to_vtk
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        records = self._point_records()
        report['points'] = 0
        if not records:
            return
        where = np.array([r.xyz for r in records], dtype=float)
        keep = self._inside(where)
        records = [r for r, ok in zip(records, keep) if ok]
        where = where[keep]
        if not len(where):
            return
        points = vtkPoints()
        points.SetData(numpy_to_vtk(np.ascontiguousarray(where), deep=True))
        cells = vtkCellArray()
        for index in range(len(where)):
            cells.InsertNextCell(1)
            cells.InsertCellPoint(index)
        data = vtkPolyData()
        data.SetPoints(points)
        data.SetVerts(cells)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        base = self._settings.colour('Geometry.Color.Points')[:3]
        classes = self._classes() if self._get(
            'Geometry.HighlightOrphans') else {}
        palette = self._highlight_colours() if classes else {}
        if classes:
            from vtkmodules.util.numpy_support import numpy_to_vtk as to_vtk

            rgb = np.tile(np.array(base, dtype=np.uint8), (len(records), 1))
            for row, record in enumerate(records):
                cls = classes.get((0, record.tag))
                if cls is not None:
                    rgb[row] = palette[cls]
            colours = to_vtk(np.ascontiguousarray(rgb), deep=True)
            colours.SetName('gmshRGB')
            data.GetCellData().SetScalars(colours)
            mapper.SetColorModeToDirectScalars()
            mapper.SetScalarModeToUseCellData()
            mapper.ScalarVisibilityOn()
        else:
            mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*_unit(base))
        prop.SetPointSize(float(self._get('Geometry.PointSize')))
        # Geometry.PointType: 0 colour dot, 1 3D sphere.
        prop.SetRenderPointsAsSpheres(int(self._get('Geometry.PointType')) == 1)
        prop.SetLighting(bool(self._get('Geometry.Light')))
        self._put('gmshGeoPoints', actor)
        report['points'] = len(records)

    # -- curves --------------------------------------------------------------- #

    def _sub(self, data, kind: str):
        """The lines or polygons of the model as their own polydata, and
        their cell range in the model's cell numbering."""
        from vtkmodules.vtkCommonDataModel import vtkPolyData

        sub = vtkPolyData()
        sub.SetPoints(data.GetPoints())
        first = data.GetNumberOfVerts()
        if kind == 'lines':
            sub.SetLines(data.GetLines())
            return sub, slice(first, first + data.GetNumberOfLines())
        first += data.GetNumberOfLines()
        sub.SetPolys(data.GetPolys())
        return sub, slice(first, first + data.GetNumberOfPolys())

    def _coloured(self, sub, rgb) -> None:
        from vtkmodules.util.numpy_support import numpy_to_vtk

        array = numpy_to_vtk(np.ascontiguousarray(rgb, dtype=np.uint8),
                             deep=True)
        array.SetName('gmshRGB')
        sub.GetCellData().SetScalars(array)

    def _draw_curves(self, data, report: dict) -> None:
        report['curves'] = 0
        if not self._get('Geometry.Curves') or not data.GetNumberOfLines():
            return
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        sub, rows = self._sub(data, 'lines')
        base = self._settings.colour('Geometry.Color.Curves')[:3]
        rgb = self._cell_colours(1, rows, base)
        if rgb is not None:
            self._coloured(sub, rgb)
        sub = self._clip(sub)
        tube = int(self._get('Geometry.CurveType')) == 1
        width = float(self._get('Geometry.CurveWidth'))
        mapper = vtkPolyDataMapper()
        if tube:
            # Geometry.CurveType = 1: 3D cylinders.
            from vtkmodules.vtkFiltersCore import vtkTubeFilter

            filt = vtkTubeFilter()
            filt.SetInputData(sub)
            filt.SetRadius(width * self._pixel() / 2.0)
            filt.SetNumberOfSides(12)
            filt.CappingOn()
            filt.Update()
            sub = filt.GetOutput()
        mapper.SetInputData(sub)
        if rgb is not None:
            mapper.SetColorModeToDirectScalars()
            mapper.SetScalarModeToUseCellData()
            mapper.ScalarVisibilityOn()
        else:
            mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*_unit(base))
        prop.SetLineWidth(width)
        prop.SetLighting(bool(tube and self._get('Geometry.Light')))
        self._put('gmshGeoCurves', actor)
        report['curves'] = int(sub.GetNumberOfCells())
        report['curve_tube'] = tube

    # -- surfaces ------------------------------------------------------------- #

    def _marker(self, where: np.ndarray, source, colour, name: str,
                scale: float, report_key: str, report: dict) -> None:
        """Glyphs of *source* at *where*: the crosses of a surface, the
        spheres and diamonds of a volume."""
        if not len(where):
            report[report_key] = 0
            return
        from vtkmodules.util.numpy_support import numpy_to_vtk
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkFiltersCore import vtkGlyph3D
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        points = vtkPoints()
        points.SetData(numpy_to_vtk(np.ascontiguousarray(where, float),
                                    deep=True))
        centres = vtkPolyData()
        centres.SetPoints(points)
        glyph = vtkGlyph3D()
        glyph.SetInputData(centres)
        glyph.SetSourceConnection(source.GetOutputPort())
        glyph.SetScaleModeToDataScalingOff()
        glyph.SetScaleFactor(scale)
        glyph.Update()
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(glyph.GetOutputPort())
        mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*colour)
        prop.SetLighting(bool(self._get('Geometry.Light')))
        prop.SetLineWidth(float(self._get('Geometry.CurveWidth')))
        self._put(name, actor)
        report[report_key] = int(len(where))

    def _draw_surfaces(self, data, report: dict) -> None:
        report['surfaces'] = 0
        report['crosses'] = 0
        forced = self._surfaces_forced()
        if not self._get('Geometry.Surfaces') and not forced:
            return
        base = self._settings.colour('Geometry.Color.Surfaces')
        kind = 2 if forced else int(self._get('Geometry.SurfaceType'))
        if kind == 0:
            from vtkmodules.vtkFiltersSources import vtkGlyphSource2D

            records = [k for k in self._picker.map.entities(2)]
            where = np.array([self._picker.map.anchor(k) for k in records
                              if self._picker.map.anchor(k) is not None],
                             dtype=float).reshape(-1, 3)
            where = where[self._inside(where)] if len(where) else where
            cross = vtkGlyphSource2D()
            cross.SetGlyphTypeToCross()
            cross.FilledOff()
            cross.SetScale(1.0)
            self._marker(where, cross, _unit(base),
                         'gmshGeoSurfaceCrosses',
                         MARKER_PIXELS * self._pixel(), 'crosses', report)
            return
        if not data.GetNumberOfPolys():
            return
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        sub, rows = self._sub(data, 'polys')
        rgb = self._cell_colours(2, rows, base[:3])
        if rgb is not None:
            self._coloured(sub, rgb)
        sub = self._clip(sub)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(sub)
        if rgb is not None:
            mapper.SetColorModeToDirectScalars()
            mapper.SetScalarModeToUseCellData()
            mapper.ScalarVisibilityOn()
        else:
            mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*_unit(base))
        shown = self.surfaceOpacity()
        prop.SetOpacity(float(base[3]) / 255.0
                        * (1.0 if shown is None else shown))
        prop.SetLighting(bool(self._get('Geometry.Light')))
        if kind == 1:
            prop.SetRepresentationToWireframe()
            prop.SetLineWidth(float(self._get('Geometry.CurveWidth')))
        else:
            prop.SetRepresentationToSurface()
        self._put('gmshGeoSurfaces', actor)
        report['surfaces'] = int(sub.GetNumberOfCells())
        report['surface_type'] = kind

    # -- volumes -------------------------------------------------------------- #

    def _draw_volumes(self, report: dict) -> None:
        report['volumes'] = 0
        if not self._get('Geometry.Volumes'):
            return
        records = [r for r in self._picker.map.records.values()
                   if r.dim == 3]
        where = np.array([r.centre for r in records],
                         dtype=float).reshape(-1, 3)
        if len(where):
            where = where[self._inside(where)]
        kind = int(self._get('Geometry.VolumeType'))
        if kind == 0:
            from vtkmodules.vtkFiltersSources import vtkSphereSource

            source = vtkSphereSource()
            source.SetRadius(0.5)
            source.SetThetaResolution(16)
            source.SetPhiResolution(16)
        else:
            from vtkmodules.vtkFiltersSources import vtkPlatonicSolidSource

            source = vtkPlatonicSolidSource()
            source.SetSolidTypeToOctahedron()
        colour = _unit(self._settings.colour('Geometry.Color.Volumes'))
        self._marker(where, source, colour, 'gmshGeoVolumes',
                     MARKER_PIXELS * self._pixel(), 'volumes', report)
        report['volume_type'] = kind

    # -- normals and tangents ------------------------------------------------- #

    def _arrows(self, where: np.ndarray, direction: np.ndarray, length: float,
                colour, name: str, report_key: str, report: dict) -> None:
        report[report_key] = int(len(where))
        if not len(where):
            return
        from vtkmodules.util.numpy_support import numpy_to_vtk
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkPolyData
        from vtkmodules.vtkFiltersCore import vtkGlyph3D
        from vtkmodules.vtkFiltersSources import vtkArrowSource
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        points = vtkPoints()
        points.SetData(numpy_to_vtk(np.ascontiguousarray(where, float),
                                    deep=True))
        data = vtkPolyData()
        data.SetPoints(points)
        vectors = numpy_to_vtk(np.ascontiguousarray(direction, float),
                               deep=True)
        vectors.SetName('gmshVector')
        data.GetPointData().SetVectors(vectors)
        glyph = vtkGlyph3D()
        glyph.SetInputData(data)
        arrow = vtkArrowSource()
        glyph.SetSourceConnection(arrow.GetOutputPort())
        glyph.SetVectorModeToUseVector()
        glyph.OrientOn()
        glyph.SetScaleModeToDataScalingOff()
        glyph.SetScaleFactor(length)
        glyph.Update()
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(glyph.GetOutputPort())
        mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*colour)
        prop.SetLighting(bool(self._get('Geometry.Light')))
        self._put(name, actor)

    def _surface_samples(self, data) -> tuple:
        """Up to :data:`NORMALS_PER_SURFACE` cells of every surface: their
        centres and unit normals, without a loop over cells."""
        from vtkmodules.util.numpy_support import vtk_to_numpy

        polys = data.GetPolys()
        if not polys.GetNumberOfCells():
            return np.zeros((0, 3)), np.zeros((0, 3))
        offsets = vtk_to_numpy(polys.GetOffsetsArray()).astype(np.int64)
        conn = vtk_to_numpy(polys.GetConnectivityArray()).astype(np.int64)
        points = vtk_to_numpy(data.GetPoints().GetData()).astype(float)
        first = data.GetNumberOfVerts() + data.GetNumberOfLines()
        tags = self._picker.map.cell_tag[first:first + len(offsets) - 1]
        dims = self._picker.map.cell_dim[first:first + len(offsets) - 1]
        order = np.argsort(tags, kind='stable')
        picked = []
        for tag in np.unique(tags[dims == 2]):
            cells = order[np.searchsorted(tags[order], tag):
                          np.searchsorted(tags[order], tag, side='right')]
            cells = cells[dims[cells] == 2]
            if len(cells) > NORMALS_PER_SURFACE:
                cells = cells[np.linspace(0, len(cells) - 1,
                                          NORMALS_PER_SURFACE).astype(int)]
            picked.append(cells)
        if not picked:
            return np.zeros((0, 3)), np.zeros((0, 3))
        cells = np.concatenate(picked)
        base = offsets[cells]
        p0, p1, p2 = (points[conn[base + i]] for i in range(3))
        normal = np.cross(p1 - p0, p2 - p0)
        size = np.linalg.norm(normal, axis=1)
        good = size > 0
        normal = normal[good] / size[good][:, None]
        size_cells = offsets[cells + 1] - base
        centre = np.zeros((len(cells), 3))
        for index in range(int(size_cells.max())):
            has = size_cells > index
            centre[has] += points[conn[base[has] + index]]
        centre /= size_cells[:, None]
        return centre[good], normal

    def _curve_samples(self, data) -> tuple:
        """One tangent per curve: at the middle cell, along it."""
        from vtkmodules.util.numpy_support import vtk_to_numpy

        lines = data.GetLines()
        if not lines.GetNumberOfCells():
            return np.zeros((0, 3)), np.zeros((0, 3))
        offsets = vtk_to_numpy(lines.GetOffsetsArray()).astype(np.int64)
        conn = vtk_to_numpy(lines.GetConnectivityArray()).astype(np.int64)
        points = vtk_to_numpy(data.GetPoints().GetData()).astype(float)
        first = data.GetNumberOfVerts()
        count = len(offsets) - 1
        tags = self._picker.map.cell_tag[first:first + count]
        middle = []
        for tag in np.unique(tags):
            cells = np.flatnonzero(tags == tag)
            middle.append(cells[len(cells) // 2])
        cells = np.array(middle, dtype=np.int64)
        p0, p1 = points[conn[offsets[cells]]], points[conn[offsets[cells] + 1]]
        direction = p1 - p0
        size = np.linalg.norm(direction, axis=1)
        good = size > 0
        return (0.5 * (p0 + p1))[good], direction[good] / size[good][:, None]

    def _draw_vectors(self, data, report: dict) -> None:
        s = self._settings
        normals = float(self._get('Geometry.Normals'))
        tangents = float(self._get('Geometry.Tangents'))
        report['normals'] = report['tangents'] = 0
        if normals > 0:
            where, direction = self._surface_samples(data)
            keep = self._inside(where) if len(where) else np.zeros(0, bool)
            self._arrows(where[keep], direction[keep],
                         normals * self._pixel(),
                         _unit(s.colour('Geometry.Color.Normals')),
                         'gmshGeoNormals', 'normals', report)
        if tangents > 0:
            where, direction = self._curve_samples(data)
            keep = self._inside(where) if len(where) else np.zeros(0, bool)
            self._arrows(where[keep], direction[keep],
                         tangents * self._pixel(),
                         _unit(s.colour('Geometry.Color.Tangents')),
                         'gmshGeoTangents', 'tangents', report)

    # -- labels --------------------------------------------------------------- #

    def _label_text(self, entity, label_type: int) -> str:
        """``Geometry.LabelType``: 0 description, 1 elementary entity tag,
        2 physical group tag, 3 elementary name, 4 physical name,
        5 coordinates."""
        from foammesh.core.gmsh.session_entities import chip_text

        entity_map = self._picker.map
        record = entity_map.records.get(entity)
        if label_type == 0:
            return chip_text(entity, entity_map.name_of(entity))
        if label_type == 1:
            return str(entity[1])
        if label_type == 2:
            return ','.join(str(t) for t in record.physical)                 if record is not None and record.physical else '-'
        if label_type == 3:
            return entity_map.name_of(entity) or '-'
        if label_type == 4:
            return ','.join('physical %d' % t for t in record.physical)                 if record is not None and record.physical else '-'
        where = entity_map.anchor(entity)
        return '%.4g, %.4g, %.4g' % tuple(where) if where else '-'

    def _draw_entity_labels(self, report: dict) -> None:
        from vtkmodules.vtkCommonCore import vtkStringArray
        from vtkmodules.vtkRenderingCore import vtkActor2D
        from vtkmodules.vtkRenderingLabel import vtkLabeledDataMapper

        label_type = int(self._get('Geometry.LabelType'))
        entity_map = self._picker.map
        report['labels'] = 0
        for dim, option in _LABEL_OPTION.items():
            if not self._get(option):
                continue
            entities, where = [], []
            for entity in entity_map.entities(dim):
                at = entity_map.anchor(entity)
                if at is not None:
                    entities.append(entity)
                    where.append(at)
            if not entities:
                continue
            where = np.asarray(where, dtype=float)
            keep = self._inside(where)
            from vtkmodules.util.numpy_support import numpy_to_vtk
            from vtkmodules.vtkCommonCore import vtkPoints
            from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

            texts = vtkStringArray()
            texts.SetName('gmshLabel')
            used = []
            for entity, ok in zip(entities, keep):
                if ok:
                    texts.InsertNextValue(self._label_text(entity, label_type))
                    used.append(entity)
            if not used:
                continue
            points = vtkPoints()
            points.SetData(numpy_to_vtk(np.ascontiguousarray(
                where[keep] + np.asarray(self._offset())), deep=True))
            cells = vtkCellArray()
            for index in range(len(used)):
                cells.InsertNextCell(1)
                cells.InsertCellPoint(index)
            data = vtkPolyData()
            data.SetPoints(points)
            data.SetVerts(cells)
            data.GetPointData().AddArray(texts)
            mapper = vtkLabeledDataMapper()
            mapper.SetInputData(data)
            mapper.SetLabelModeToLabelFieldData()
            mapper.SetFieldDataName('gmshLabel')
            prop = mapper.GetLabelTextProperty()
            prop.SetFontSize(_LABEL_FONT)
            prop.SetColor(1.0, 1.0, 1.0)
            prop.SetBackgroundColor(0.1, 0.12, 0.15)
            prop.SetBackgroundOpacity(0.7)
            prop.SetJustificationToCentered()
            actor = vtkActor2D()
            actor.SetMapper(mapper)
            self._put('gmshGeoLabels' + _LABEL_KIND[dim], actor)
            report['labels'] += len(used)
            report['labels_' + _LABEL_KIND[dim].lower()] = len(used)

    # -- light ---------------------------------------------------------------- #

    def _light(self, report: dict) -> None:
        """``Geometry.Light`` is the lighting of each actor (set where the
        actor is made); ``Geometry.LightTwoSide`` lights both sides of a
        surface: off gives the back of a surface its own unlit property."""
        two_side = bool(self._get('Geometry.LightTwoSide'))
        report['two_side'] = two_side
        surfaces = self._geo.get('gmshGeoSurfaces')
        if surfaces is None:
            return
        if two_side:
            surfaces.SetBackfaceProperty(None)
        else:
            from vtkmodules.vtkRenderingCore import vtkProperty

            back = vtkProperty()
            back.DeepCopy(surfaces.GetProperty())
            back.SetLighting(False)
            back.SetOpacity(0.35)
            surfaces.SetBackfaceProperty(back)

    # -- labels (the plain toggle) -------------------------------------------- #

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
                                ('LeftButtonReleaseEvent', self._released),
                                ('KeyPressEvent', self._key)):
            self._observers.append((interactor, interactor.AddObserver(
                event, callback, _PRIORITY)))
        # Once the camera style takes a press it grabs the interactor's focus
        # and the release never reaches the observer above; the style's own
        # end-of-interaction event is how a click (no drag) is seen.
        style = (interactor.GetInteractorStyle()
                 if hasattr(interactor, 'GetInteractorStyle') else None)
        if style is not None and hasattr(style, 'AddObserver'):
            self._observers.append((style, style.AddObserver(
                'EndInteractionEvent', self._released, _PRIORITY)))

    def _detach(self) -> None:
        self._press = None
        self._clear_band()
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

    def _renderer(self):
        renderer = getattr(self._view, 'renderer', None)
        return renderer() if callable(renderer) else None

    def _world_ends(self, renderer, x, y) -> list:
        ends = []
        for depth in (0.0, 1.0):
            renderer.SetDisplayPoint(float(x), float(y), depth)
            renderer.DisplayToWorld()
            w = renderer.GetWorldPoint()
            ends.append([w[i] / (w[3] or 1.0) for i in range(3)])
        return ends

    def pickAt(self, x: float, y: float, dim: int | None = None,
               wait: bool = True):
        """``(cell, world)`` under a display point, ``(None, None)`` if none.

        *dim* 1 casts the ray at the curve cells alone and *dim* 2 at the
        surface cells alone, so a curve behind or under a surface can still be
        picked (DP-6154). A curve has a tolerance of a few pixels, wherever
        the model sits in the view.

        Plan 50 IP12 (DP-6347). While the worker builds the structures, a
        pick with *wait* waits for them (a click must name what is there);
        without it (a hover) the last structures answer, or nothing."""
        from vtkmodules.vtkCommonCore import mutable

        self._collect_pick(wait=wait)
        renderer = self._renderer()
        if renderer is None or self._locator is None:
            return None, None
        near, far = self._world_ends(renderer, x, y)
        side = self._world_ends(renderer, x + 1.0, y)[0]
        pixel = sum((side[i] - near[i]) ** 2 for i in range(3)) ** 0.5
        t, point, pcoords = mutable(0.0), [0.0] * 3, [0.0] * 3
        sub, cell = mutable(0), mutable(0)
        if dim is None:
            hit = self._locator.IntersectWithLine(
                near, far, pixel * 4.0, t, point, pcoords, sub, cell)
            return (int(cell), tuple(point)) if hit else (None, None)
        if dim not in self._locators:
            return None, None
        locator, _part, cells = self._locators[dim]
        hit = locator.IntersectWithLine(
            near, far, pixel * (5.0 if dim == 1 else 1.0), t, point, pcoords,
            sub, cell)
        if not hit:
            return None, None
        return int(cells[int(cell)]), tuple(point)

    def _pick_target(self, x, y, wait: bool = True):
        """``(cell, world)`` for the picker's current filter."""
        from foammesh.core.gmsh.session_entities import FILTERS

        dim = FILTERS.get(self._picker.filter)
        if dim == 0:
            return None, self._point_at(x, y)
        if dim == 1:
            return self.pickAt(x, y, 1, wait=wait)
        if dim in (2, 3):
            return self.pickAt(x, y, 2, wait=wait)
        return self.pickAt(x, y, wait=wait)

    def _point_at(self, x, y, pixels: float = 10.0):
        """The nearest point entity within a few pixels of the cursor, in
        screen space, so a corner behind a surface can still be picked
        (DP-6154)."""
        renderer = self._renderer()
        if renderer is None:
            return None
        best, best_distance = None, pixels
        for record in self._point_records():
            renderer.SetWorldPoint(record.xyz[0], record.xyz[1], record.xyz[2],
                                   1.0)
            renderer.WorldToDisplay()
            dx, dy, _ = renderer.GetDisplayPoint()
            distance = ((dx - x) ** 2 + (dy - y) ** 2) ** 0.5
            if distance <= best_distance:
                best, best_distance = tuple(record.xyz), distance
        return best

    def _moved(self, interactor, event) -> None:
        if not self._picker.active:
            return
        x, y = interactor.GetEventPosition()
        self._cursor = (x, y)
        if self._press is not None and self._press[4]:
            self._draw_band(self._press[0], self._press[1], x, y)
            self._take(interactor)
            return
        if self._tip_actor is not None:
            self._tip_actor.SetPosition(x + 14, y + 14)
            self._view.refresh()
        self._hover_at = (x, y)
        timer = self._dwell_timer()
        if timer is None:
            self._hover_now()
        else:
            timer.start()                   # restarts: the pointer moved on

    def _dwell_timer(self):
        """Plan 50 IP12 (DP-6346). The single-shot dwell timer, or ``None``
        to pick on the move (no dwell set, or no Qt event loop to wait in)."""
        if HOVER_DWELL_MS <= 0 or not _qt_running():
            return None
        if self._hover_timer is None:
            from PySide6.QtCore import QTimer

            self._hover_timer = QTimer()
            self._hover_timer.setSingleShot(True)
            self._hover_timer.timeout.connect(self._hover_now)
        self._hover_timer.setInterval(int(HOVER_DWELL_MS))
        return self._hover_timer

    def _hover_now(self) -> None:
        """The hover pick where the pointer rested; never waits for the pick
        structures (DP-6347)."""
        where, self._hover_at = self._hover_at, None
        if where is None or not self._picker.active or self._body is None:
            return
        if self._press is not None and self._press[4]:
            return                          # a box selection is being drawn
        cell, world = self._pick_target(where[0], where[1], wait=False)
        self._picker.hover(cell, world)

    def _pressed(self, interactor, event) -> None:
        """Remember where the button went down. DP-6156: the selection changes
        on release, and only when the pointer has not moved, so an orbit or pan
        that starts over the model leaves the pick alone. Ctrl+drag is a box
        selection (DP-6152), so the camera does not take it."""
        if not self._picker.active:
            return
        x, y = interactor.GetEventPosition()
        box = bool(interactor.GetControlKey())
        self._press = (x, y, bool(interactor.GetShiftKey()),
                       bool(interactor.GetControlKey()), box)
        if box:
            self._take(interactor)

    def _released(self, caller, event) -> None:
        interactor = self._interactor()
        press, self._press = self._press, None
        if press is None or interactor is None or not self._picker.active:
            return
        x0, y0, shift, ctrl, box = press
        x, y = interactor.GetEventPosition()
        moved = abs(x - x0) > _CLICK_SLOP or abs(y - y0) > _CLICK_SLOP
        self._clear_band()
        if box and moved:
            entities = self._entities_in_rect(x0, y0, x, y)
            # Gmsh: Ctrl+drag selects, Ctrl+Shift+drag unselects.
            self._picker.select_many(entities, remove=shift, extend=True)
            self._take(interactor)
            return
        if moved:
            return
        cell, world = self._pick_target(x0, y0)
        if self._picker.click(cell, world, shift=shift, toggle=ctrl) is not None:
            self._take(interactor)

    def _entities_in_rect(self, x0, y0, x1, y1) -> list:
        """Entities whose anchor projects inside the display rectangle."""
        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is None:
            return []
        lo_x, hi_x = sorted((x0, x1))
        lo_y, hi_y = sorted((y0, y1))
        found = []
        entity_map = self._picker.map
        for entity in entity_map.entities():
            anchor = entity_map.anchor(entity)
            if anchor is None:
                continue
            renderer.SetWorldPoint(anchor[0], anchor[1], anchor[2], 1.0)
            renderer.WorldToDisplay()
            dx, dy, _ = renderer.GetDisplayPoint()
            if lo_x <= dx <= hi_x and lo_y <= dy <= hi_y:
                found.append(entity)
        return found

    def _draw_band(self, x0, y0, x1, y1) -> None:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor2D, vtkPolyDataMapper2D

        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is None:
            return
        self._clear_band()
        points = vtkPoints()
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            points.InsertNextPoint(px, py, 0.0)
        lines = vtkCellArray()
        lines.InsertNextCell(5)
        for index in (0, 1, 2, 3, 0):
            lines.InsertCellPoint(index)
        data = vtkPolyData()
        data.SetPoints(points)
        data.SetLines(lines)
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(data)
        actor = vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.1, 0.4, 0.9)
        actor.GetProperty().SetLineWidth(1.5)
        renderer.AddActor2D(actor)
        self._band_actor = actor
        self._view.refresh()

    def _clear_band(self) -> None:
        actor, self._band_actor = self._band_actor, None
        if actor is None:
            return
        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is not None:
            renderer.RemoveActor2D(actor)
        self._view.refresh()

    def _key(self, interactor, event) -> None:
        if not self._picker.active:
            return
        key = interactor.GetKeySym()
        if self._picker.key(key):
            self._take(interactor)

    def dispose(self) -> None:
        self.clear()
        self._unfollow_route()
        timer, self._hover_timer = self._hover_timer, None
        if timer is not None:
            timer.stop()
            try:
                timer.timeout.disconnect(self._hover_now)
            except (RuntimeError, TypeError):
                pass
