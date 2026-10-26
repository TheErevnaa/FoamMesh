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

import numpy as np

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
        self._locator = None
        self._observers: list = []
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
        locator = vtkCellLocator()
        locator.SetDataSet(data)
        locator.BuildLocator()
        self._locator = locator
        bounds = data.GetBounds()
        diagonal = ((bounds[1] - bounds[0]) ** 2 + (bounds[3] - bounds[2]) ** 2
                    + (bounds[5] - bounds[4]) ** 2) ** 0.5
        self._picker.point_tolerance = diagonal * POINT_TOLERANCE_FRACTION
        self._attach()
        if self._settings is not None:
            self._draw_geometry()
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
        self._drop_geometry()
        for actor in (self._body, self._points_actor, self._hover_actor,
                      *self._chosen_actors, *self._label_actors):
            if actor is not None:
                self._view.removeActor(actor)
        self._body = self._points_actor = self._hover_actor = None
        self._chosen_actors, self._label_actors = [], []
        self._polydata = None
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

    def _onPickerChange(self, what: str) -> None:
        if self._body is None:
            return
        if self._settings is not None and what in (
                'begin', 'accept', 'abort', 'filter'):
            self._pick_aid()
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
        return [r for r in self._picker.map.records.values()
                if r.dim == 0 and r.xyz is not None]

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
        self._unfollow_route()
