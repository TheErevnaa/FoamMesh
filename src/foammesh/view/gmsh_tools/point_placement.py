"""Interactive point placement (Plan 48 GM11b; Gmsh's creation mode).

In Gmsh's geometry creation mode a click makes a point where the cursor is;
``x``, ``y`` and ``z`` freeze that coordinate while the cursor moves, and
``Shift+X``, ``Shift+Y`` and ``Shift+Z`` let the cursor move along that axis
only.  Here the tool is a mode of the Gmsh tool dock:

* :class:`Placement` is the arithmetic -- the frozen axes, the single free
  axis, the work plane and the grid snap -- with no window;
* :func:`world_on_plane` turns a pixel into a point on the work plane, or on
  the model where the pick ray meets it;
* :class:`PlacementTool` listens to the viewport (mouse and keys), shows a
  marker, and calls ``place(x, y, z, size)`` for each click.  The window's
  ``place`` is ``GenerateController.build('point', ...)``, the same operation
  the Geometry build panel runs, so there is one way to make a point.
"""
from __future__ import annotations

import math

AXES = ('x', 'y', 'z')
#: The work planes: the axis the plane is perpendicular to.
PLANES = {'XY': 2, 'XZ': 1, 'YZ': 0}


class PlacementRefusal(ValueError):
    """The point cannot be placed; the message says why."""


class Placement:
    """Where the next point goes, from the cursor and the frozen axes."""

    def __init__(self):
        self.frozen: set = set()
        self.only: str | None = None
        self.plane = 'XY'
        self.offset = 0.0
        self.grid = 0.0
        self.size = 0.0
        #: The last placed point; a frozen axis keeps its value.
        self.anchor = (0.0, 0.0, 0.0)

    # -- the keys --------------------------------------------------------- #

    def freeze(self, axis: str, on: bool | None = None) -> bool:
        """``x``/``y``/``z``: freeze or release *axis*; returns the new state."""
        axis = self._axis(axis)
        now = (axis not in self.frozen) if on is None else bool(on)
        if now:
            self.frozen.add(axis)
            if self.only == axis:
                self.only = None
        else:
            self.frozen.discard(axis)
        return now

    def move_only(self, axis: str | None) -> None:
        """``Shift+X``/``Y``/``Z``: only *axis* follows the cursor (None: all)."""
        if axis is None:
            self.only = None
            return
        axis = self._axis(axis)
        self.only = None if self.only == axis else axis
        if self.only:
            self.frozen.discard(self.only)

    @staticmethod
    def _axis(axis) -> str:
        axis = str(axis).lower()
        if axis not in AXES:
            raise PlacementRefusal(f'{axis!r} is not an axis (x, y or z)')
        return axis

    def set_plane(self, plane: str, offset: float = 0.0) -> None:
        if plane not in PLANES:
            raise PlacementRefusal(
                f'the work plane is one of {", ".join(PLANES)}')
        offset = float(offset)
        if not math.isfinite(offset):
            raise PlacementRefusal('the work plane offset must be a number')
        self.plane, self.offset = plane, offset

    def set_grid(self, step: float) -> None:
        step = float(step)
        if not math.isfinite(step) or step < 0.0:
            raise PlacementRefusal('the snap step must be 0 or more')
        self.grid = step

    def set_size(self, size: float) -> None:
        size = float(size)
        if not math.isfinite(size) or size < 0.0:
            raise PlacementRefusal('the size at the point must be 0 or more')
        self.size = size

    # -- the point -------------------------------------------------------- #

    def point(self, cursor) -> tuple:
        """The point for *cursor* (x, y, z): snapped, frozen and restricted."""
        values = [float(v) for v in cursor]
        if len(values) != 3 or not all(math.isfinite(v) for v in values):
            raise PlacementRefusal('the cursor is not on the model or the '
                                   'work plane')
        if self.grid > 0.0:
            values = [round(v / self.grid) * self.grid for v in values]
        for index, axis in enumerate(AXES):
            if axis in self.frozen:
                values[index] = self.anchor[index]
            elif self.only is not None and axis != self.only:
                values[index] = self.anchor[index]
        return tuple(values)

    def accept(self, cursor) -> dict:
        """A click: the values of the ``point`` build operation; the anchor
        moves to the new point."""
        x, y, z = self.point(cursor)
        self.anchor = (x, y, z)
        return {'x': x, 'y': y, 'z': z, 'size': self.size}

    def state(self) -> dict:
        return {'frozen': sorted(self.frozen), 'only': self.only,
                'plane': self.plane, 'offset': self.offset,
                'grid': self.grid, 'size': self.size,
                'anchor': list(self.anchor)}


def world_on_plane(renderer, x: float, y: float, plane: str = 'XY',
                   offset: float = 0.0):
    """The world point under pixel (*x*, *y*) on the work plane, or ``None``
    when the pick ray is parallel to it.  *y* counts from the bottom, as VTK's
    display coordinates do."""
    from vtkmodules.vtkRenderingCore import vtkCoordinate

    axis = PLANES[plane]
    near = vtkCoordinate()
    near.SetCoordinateSystemToDisplay()
    near.SetValue(float(x), float(y), 0.0)
    far = vtkCoordinate()
    far.SetCoordinateSystemToDisplay()
    far.SetValue(float(x), float(y), 1.0)
    a = near.GetComputedWorldValue(renderer)
    b = far.GetComputedWorldValue(renderer)
    direction = [b[i] - a[i] for i in range(3)]
    if abs(direction[axis]) < 1e-12:
        return None
    t = (float(offset) - a[axis]) / direction[axis]
    return tuple(a[i] + t * direction[i] for i in range(3))


def world_on_model(renderer, x: float, y: float, actors):
    """Where the pick ray meets one of *actors*, or ``None``."""
    from vtkmodules.vtkRenderingCore import vtkCellPicker

    picker = vtkCellPicker()
    picker.SetTolerance(0.0005)
    for actor in actors:
        picker.AddPickList(actor)
    picker.PickFromListOn()
    if not picker.Pick(float(x), float(y), 0.0, renderer):
        return None
    return tuple(picker.GetPickPosition())


class PlacementTool:
    """The viewport side of point placement.

    *view* is the rendering widget (``interactor()``, ``renderer()``,
    ``addActor``, ``removeActor``, ``refresh``); *place* is called with the
    values of the ``point`` build operation for each click and returns a
    ``dict`` (the build's account); *model_actors* returns the actors a click
    may land on.  The tool works on the work plane where no model is hit.
    """

    def __init__(self, view_provider, place, model_actors=lambda: ()):
        self.placement = Placement()
        self._view = view_provider
        self._place = place
        self._model_actors = model_actors
        self._active = False
        self._observers: list = []
        self._marker = None
        self._status = ''
        self._placed: list = []
        self.changed = []           # callables, told after any change

    # -- state ------------------------------------------------------------ #

    def active(self) -> bool:
        return self._active

    def status(self) -> str:
        return self._status

    def placed(self) -> list:
        return list(self._placed)

    def _say(self, text: str) -> None:
        self._status = text
        for listener in list(self.changed):
            listener()

    def start(self) -> bool:
        view = self._view()
        if view is None:
            self._say('There is no viewport to place points in.')
            return False
        if self._active:
            return True
        interactor = view.interactor()
        if interactor is not None:
            self._observers = [
                interactor.AddObserver('LeftButtonPressEvent', self._pressed,
                                       10.0),
                interactor.AddObserver('MouseMoveEvent', self._moved, 10.0),
                interactor.AddObserver('KeyPressEvent', self._key, 10.0)]
        self._active = True
        self._say('Click to place a point; x, y, z freeze a coordinate, '
                  'Shift+X, Y, Z move along one axis; Escape ends.')
        return True

    def stop(self) -> None:
        if not self._active:
            return
        view = self._view()
        interactor = view.interactor() if view is not None else None
        if interactor is not None:
            for tag in self._observers:
                interactor.RemoveObserver(tag)
        self._observers = []
        self._removeMarker()
        self._active = False
        self._say('')

    # -- events ----------------------------------------------------------- #

    def cursor_world(self, x: float, y: float):
        view = self._view()
        if view is None:
            return None
        renderer = view.renderer()
        found = world_on_model(renderer, x, y, list(self._model_actors()))
        if found is None:
            found = world_on_plane(renderer, x, y, self.placement.plane,
                                   self.placement.offset)
        return found

    def click_at(self, x: float, y: float) -> dict | None:
        """Place a point at pixel (*x*, *y*); the build's account, or None."""
        found = self.cursor_world(x, y)
        if found is None:
            self._say('The cursor is not on the model or the work plane.')
            return None
        return self.click_world(found)

    def click_world(self, world) -> dict | None:
        try:
            values = self.placement.accept(world)
        except PlacementRefusal as error:
            self._say(str(error))
            return None
        try:
            account = self._place(values) or {}
        except Exception as error:                    # noqa: BLE001
            self._say(str(error))
            return None
        self._placed.append(values)
        self._say('Placed a point at (%g, %g, %g).' % (
            values['x'], values['y'], values['z']))
        return account

    def key(self, name: str, shift: bool = False) -> bool:
        """A key press; True when the tool used it."""
        name = (name or '').lower()
        if name == 'escape':
            self.stop()
            return True
        if name in AXES:
            if shift:
                self.placement.move_only(name)
            else:
                self.placement.freeze(name)
            self._say('Frozen: %s; moving only along %s.' % (
                ', '.join(sorted(self.placement.frozen)) or 'none',
                self.placement.only or 'all axes'))
            return True
        return False

    def _pressed(self, interactor, _event) -> None:
        x, y = interactor.GetEventPosition()
        self.click_at(x, y)
        self._abort(interactor, 0)

    def _moved(self, interactor, _event) -> None:
        x, y = interactor.GetEventPosition()
        found = self.cursor_world(x, y)
        if found is None:
            return
        try:
            self._showMarker(self.placement.point(found))
        except PlacementRefusal:
            pass

    def _key(self, interactor, _event) -> None:
        symbol = interactor.GetKeySym() or ''
        shift = bool(interactor.GetShiftKey())
        if self.key(symbol, shift):
            self._abort(interactor, 2)

    def _abort(self, interactor, which: int) -> None:
        # Our observers run first (priority 10); the camera must not also see
        # the press, so the event stops here.
        if which < len(self._observers):
            command = interactor.GetCommand(self._observers[which])
            if command is not None:
                command.SetAbortFlag(1)

    # -- the marker ------------------------------------------------------- #

    def _showMarker(self, where) -> None:
        view = self._view()
        if view is None:
            return
        from vtkmodules.vtkFiltersSources import vtkSphereSource
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        if self._marker is None:
            source = vtkSphereSource()
            source.SetThetaResolution(12)
            source.SetPhiResolution(12)
            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(source.GetOutputPort())
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.SetObjectName('gmshPlacementMarker')
            actor.GetProperty().SetColor(1.0, 0.55, 0.1)
            actor.GetProperty().SetLighting(False)
            actor.PickableOff()
            self._marker = (actor, source)
            view.addActor(actor)
        actor, source = self._marker
        bounds = view.renderer().ComputeVisiblePropBounds()
        span = max((bounds[1] - bounds[0], bounds[3] - bounds[2],
                    bounds[5] - bounds[4], 1e-9)) \
            if bounds and bounds[0] <= bounds[1] else 1.0
        source.SetRadius(span * 0.008)
        source.SetCenter(*where)
        view.refresh()

    def _removeMarker(self) -> None:
        view = self._view()
        if self._marker is not None and view is not None:
            view.removeActor(self._marker[0])
            view.refresh()
        self._marker = None
