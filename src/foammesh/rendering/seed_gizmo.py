"""The handle a region seed is dragged by.

Plan 36 RP3. A region on Domain & Regions is a seed point, and it used to be
drawn as a plain sphere: the only way to move it was to type three numbers.
This is the handle the user drags instead:

* a centre ball, which moves the seed in the plane facing the camera;
* three arrows (X red, Y green, Z blue, the viewport's own axis colours),
  each moving it along its axis;
* three small squares (XY, YZ, ZX), each moving it within its plane;
* a ring that shows, under the cursor, what a press will grab.

Why a gizmo of its own (the plan's F3): ``vtkPointWidget`` has no plane
handles and needs Shift for an axis, ``vtkHandleWidget`` drags in the view
plane only, ``vtkBoxWidget2`` moves a box, and VTK 9 has no translate gizmo.

It is drawn in an overlay renderer on layer 1, so the geometry never hides it,
and it is sized every render from the camera so the arrows stay about
:data:`ARROW_PIXELS` long at any zoom, like the axes triad. The overlay is not
interactive: the camera style never pokes it, and only the handles are
pickable, with a picker limited to them.

The interactor observers run at priority 1.0, before the camera style (the
same place the section plane's Ctrl gate sits, ``plane_widget.py``). A press
on a handle is taken -- the camera does not turn -- and a press anywhere else
is left alone, so a camera drag never moves the seed. Mouse moves during a
drag are coalesced to one per frame with a 0 ms timer, and a drag frame builds
no polydata: it moves ten actors and renders.

Every move is clamped to the box the background mesh spans (RP1), inset by a
millionth of its diagonal so a seed is never exactly on a wall, and Ctrl snaps
the moving coordinates to the step (one base-grid cell). The arrow keys and
Page Up / Page Down nudge the seed by one step (Shift: ten, Alt: a tenth).

Plan 36 RP4 adds BaramMesh's depth cue, made to follow the camera: on each of
the three walls of the box behind the model the seed casts a small cross-hair,
with a dotted line dropped to it, in the colour of the axis normal to that
wall. A lone point in 3-D has no depth; three shadows on known walls do. The
walls are chosen again only when the camera moves into another octant of the
box, and the shadows are drawn in the scene's own renderer so the geometry
covers them as it would a real shadow. While a drag is on, the seed's
coordinates follow the cursor.

Plan 36 RP9 adds a plane mode (`setPlaneMode`), used while a section plane is
raised through the seed: the arrow along the plane's normal and the two
squares standing across the plane are put away, the ball drags on the plane
rather than facing the camera, and no drag changes the coordinate along the
normal. The plane itself moves the seed along the normal (`pushPlane`), and
a double-click off the handles drops the seed where the ray meets the plane
(`dropOnPlane`).

Plan 36 RP10 says the verdict in shape as well as colour (`setVerdict`), so a
seed in the wrong place reads as wrong in greyscale, to a colour-blind user
and in a high-contrast theme: inside the fluid the ball is solid; where the
verdict is unknown it is hollow (a wireframe); outside, on a wall or in a
space open to the outside it is hollow and crossed out. The cross is drawn in
the theme's text colour, so it stands out on the light and the dark
background alike. Nothing here animates, so reduced motion needs no case.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QObject, QTimer, Signal

#: How long the arrows are on screen, at any zoom.
ARROW_PIXELS = 90.0

#: The seed never sits exactly on a wall of the box: it is kept this fraction
#: of the box's diagonal inside it.
WALL_INSET = 1e-6

#: The interactor observers run before the camera style's (priority 0).
_PRIORITY = 1.0

#: The overlay's layer. The render window gets at least this many + 1.
_LAYER = 1

#: Grab modes. An axis or a plane is named by its axis index (plane: normal).
CENTRE = 'centre'
AXIS = 'axis'
PLANE = 'plane'

#: Used when no theme is loaded (tests, early start-up): X, Y, Z.
_FALLBACK_AXIS_COLOURS = ('#d9534f', '#3fae5a', '#3f8ae0')
_AXIS_TOKENS = ('status.error', 'status.success', 'status.info')
_FALLBACK_SEED = '#9aa4b2'

#: Plan 37 UF16. What a handle places: a region seed (the ball) or an
#: exclude point (a cube, in the theme's warning colour, its readout saying
#: "Exclude"), so the two can never be mistaken for one another on screen.
SEED, EXCLUDE = 'seed', 'exclude'
_ROLES = (SEED, EXCLUDE)
_EXCLUDE_TOKEN = 'status.warning'
_FALLBACK_EXCLUDE = '#d9822b'

#: Handle proportions, in arrow lengths.
_BALL_RADIUS = 0.12
_SQUARE_NEAR, _SQUARE_FAR = 0.28, 0.48
_RING_RADIUS = 0.2
#: Plan 36 RP10. The cross over a seed in the wrong place, in arrow lengths.
_CROSS_RADIUS = 0.2
_SQUARE_OPACITY = 0.55
_HOVER_WIDTH = 1.6

#: Plan 36 RP4. A shadow's cross-hair spans this fraction of its wall's
#: shorter side; shadows and drop lines are drawn at this opacity, and a drop
#: line is this many dashes.
SHADOW_CROSS = 0.06
SHADOW_OPACITY = 0.6
_DROP_DASHES = 16

#: Where the coordinate readout sits from the cursor, in pixels.
_READOUT_OFFSET = (16, 12)
_READOUT_SIZE = 13
_FALLBACK_TEXT = '#e8ebef'

#: An arrow within 15 degrees of the view direction is hidden (it points at
#: the camera), and a square within 10 degrees of edge-on.
_EDGE_ON = math.cos(math.radians(15.0))
_SQUARE_EDGE_ON = math.cos(math.radians(80.0))

#: Plan 36 RP10. The verdicts a seed must not stay at: crossed out.
WRONG_PLACE = frozenset({'outside', 'on_surface', 'open_to_outside'})

#: The handle's shape by verdict: what `shape()` answers.
SOLID, HOLLOW, CROSSED = 'solid', 'hollow', 'crossed'

#: VTK key names for the nudge keys: (axis, direction).
_NUDGE_KEYS = {
    'Right': (0, 1), 'Left': (0, -1),
    'Up': (1, 1), 'Down': (1, -1),
    'Prior': (2, 1), 'Next': (2, -1),
}


def axisColours() -> tuple[str, str, str]:
    """The viewport's X, Y and Z colours (the axes triad's)."""
    from foammesh.app import app

    try:
        tokens = app.themeManager.tokens if app.themeManager else None
        if tokens is not None:
            colours = tuple(tokens.value(name) for name in _AXIS_TOKENS)
            if all(colours):
                return tuple(str(colour) for colour in colours)
    except Exception:                                         # noqa: BLE001
        pass
    return _FALLBACK_AXIS_COLOURS


def excludeColour() -> str:
    """Plan 37 UF16. The theme's warning colour: an exclude point's handle
    and markers. None of the axes (error, success, info) and none of the seed
    verdicts (success, error) use it."""
    from foammesh.app import app

    try:
        tokens = app.themeManager.tokens if app.themeManager else None
        if tokens is not None:
            value = tokens.values.get(_EXCLUDE_TOKEN)
            if value:
                return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return _FALLBACK_EXCLUDE


def _textColour() -> str:
    """The theme's text colour: the readout's, and RP10's cross.

    Plan 36 RP10 (DP-853). This asked for ``text.primary``, which no theme
    defines, so the readout was always the dark theme's pale grey -- faint on
    the light theme's viewport. ``foreground.primary`` is the theme's text.
    """
    from foammesh.app import app

    try:
        tokens = app.themeManager.tokens if app.themeManager else None
        if tokens is not None:
            for name in ('foreground.primary', 'text.primary'):
                value = tokens.values.get(name)
                if value:
                    return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return _FALLBACK_TEXT


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(axis):
    return tuple(1.0 if index == axis else 0.0 for index in range(3))


def closestOnAxis(origin, axis, rayOrigin, rayDirection):
    """How far along *axis* from *origin* the line comes closest to the ray.

    ``None`` when the ray runs along the axis, where no answer is stable.
    """
    a = _unit(axis)
    w0 = _sub(origin, rayOrigin)
    b = _dot(a, rayDirection)
    dd = _dot(rayDirection, rayDirection)
    denominator = dd - b * b
    if denominator <= 1e-9 * max(dd, 1e-300):
        return None
    return (b * _dot(rayDirection, w0) - dd * _dot(a, w0)) / denominator


def rayPlane(point, normal, rayOrigin, rayDirection):
    """Where the ray meets the plane through *point*, or ``None`` edge-on."""
    facing = _dot(normal, rayDirection)
    length = math.sqrt(_dot(rayDirection, rayDirection)) or 1.0
    if abs(facing) <= 1e-6 * length:
        return None
    s = _dot(normal, _sub(point, rayOrigin)) / facing
    return _add(rayOrigin, _scale(rayDirection, s))


class SeedGizmo(QObject):
    """A draggable seed. Without a viewport it still holds and clamps a point."""

    #: Every step of a drag or a nudge: the seed is now here.
    pointMoved = Signal(tuple)
    #: A drag was released, or a nudge made: one placement (the undo unit).
    placementFinished = Signal(tuple)
    #: A drag started or ended (True, False).
    dragging = Signal(bool)

    def __init__(self, view=None, point=(0.0, 0.0, 0.0), parent=None, *,
                 role=SEED):
        super().__init__(parent)
        if role not in _ROLES:
            raise ValueError(f'a handle places a seed or an exclude point, '
                             f'not {role!r}')
        self._role = role
        self._view = view
        self._point = tuple(float(value) for value in point)
        self._bounds = None
        self._step = None
        # RP13 #2. Where the background mesh starts, and its faces.
        self._snapOrigin = None
        self._faceGrid = None
        self._faceLevel = 0
        self._scaleValue = 1.0
        self._mode = None
        self._axis = None
        self._dragStart = None
        self._grab = None
        self._pending = None
        self._hovered = None
        self._hoverPending = None
        self._enabled = True
        self._observers = []
        self._renderer = None
        self._window = None
        self._handles = {}
        self._actors = []
        self._ring = None
        #: RP10. The cross drawn over a seed in the wrong place.
        self._cross = None
        self._verdict = None
        self._picker = None
        self._colour = excludeColour() if role == EXCLUDE else _FALLBACK_SEED
        self._closed = False
        #: RP4. Per axis: (cross-hair actor, its data, drop actor, its data).
        self._shadows = {}
        self._octant = None
        self._readout = None
        self._main = None
        #: RP9. The axis the section plane is normal to, or None.
        self._planeAxis = None
        self._pushStart = None

        self._frame = QTimer(self)
        self._frame.setSingleShot(True)
        self._frame.setInterval(0)
        self._frame.timeout.connect(self._applyFrame)

        self._build()

    # -- building ----------------------------------------------------------- #

    def _mainRenderer(self):
        renderer = getattr(self._view, 'renderer', None)
        return renderer() if callable(renderer) else None

    def _interactor(self):
        interactor = getattr(self._view, 'interactor', None)
        return interactor() if callable(interactor) else None

    def _build(self) -> None:
        main = self._mainRenderer()
        window = main.GetRenderWindow() if main is not None else None
        if window is None:
            return
        from vtkmodules.vtkRenderingCore import (
            vtkCamera, vtkPropPicker, vtkRenderer)

        overlay = vtkRenderer()
        overlay.SetLayer(_LAYER)
        overlay.InteractiveOff()
        overlay.SetActiveCamera(vtkCamera())
        # The seed always draws over the model; its own parts still occlude
        # one another, since the depth buffer is cleared, not switched off.
        overlay.SetPreserveDepthBuffer(False)
        if window.GetNumberOfLayers() < _LAYER + 1:
            window.SetNumberOfLayers(_LAYER + 1)
        window.AddRenderer(overlay)
        self._renderer, self._window = overlay, window

        colours = axisColours()
        if self._role == EXCLUDE:
            ball = self._part(_cubePolyData(), self._colour,
                              'excludeGizmo:ball')
        else:
            ball = self._part(_ballPolyData(), self._colour, 'seedGizmo:ball')
        self._handles[(CENTRE, None)] = ball
        for axis in range(3):
            self._handles[(AXIS, axis)] = self._part(
                _arrowPolyData(axis), colours[axis], f'seedGizmo:arrow{"XYZ"[axis]}')
        for axis in range(3):
            actor = self._part(_squarePolyData(axis), colours[axis],
                               f'seedGizmo:plane{("YZ", "ZX", "XY")[axis]}')
            actor.GetProperty().SetOpacity(_SQUARE_OPACITY)
            self._handles[(PLANE, axis)] = actor

        self._ring = self._ringActor()
        overlay.AddActor(self._ring)
        self._actors.append(self._ring)
        self._cross = self._crossActor()
        overlay.AddActor(self._cross)
        self._actors.append(self._cross)
        self._applyVerdict()

        self._main = main
        self._buildShadows(main, colours)
        self._readout = self._readoutActor()
        overlay.AddViewProp(self._readout)
        self._observers.append((main, main.AddObserver(
            'StartEvent', self._beforeSceneRender)))

        picker = vtkPropPicker()
        picker.PickFromListOn()
        for actor in self._handles.values():
            picker.AddPickList(actor)
        self._picker = picker

        self._observers.append((overlay, overlay.AddObserver(
            'StartEvent', self._beforeRender)))
        interactor = self._interactor()
        if interactor is not None and hasattr(interactor, 'AddObserver'):
            for event, callback in (
                    ('LeftButtonPressEvent', self._pressed),
                    ('MouseMoveEvent', self._moved),
                    ('LeftButtonReleaseEvent', self._released),
                    ('KeyPressEvent', self._keyPressed)):
                self._observers.append((interactor, interactor.AddObserver(
                    event, callback, _PRIORITY)))
        self._place()

    def _part(self, polyData, colour, name):
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
        from foammesh.view.theming.vtk_theme import rgb

        mapper = vtkPolyDataMapper()
        mapper.SetInputData(polyData)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName(name)
        prop = actor.GetProperty()
        prop.SetColor(*rgb(colour))
        prop.SetAmbient(0.35)
        prop.SetDiffuse(0.75)
        self._renderer.AddActor(actor)
        self._actors.append(actor)
        return actor

    def _ringActor(self):
        from vtkmodules.vtkFiltersSources import vtkRegularPolygonSource
        from vtkmodules.vtkRenderingCore import vtkFollower, vtkPolyDataMapper

        source = vtkRegularPolygonSource()
        source.SetNumberOfSides(48)
        source.SetRadius(_RING_RADIUS)
        source.GeneratePolygonOff()
        source.GeneratePolylineOn()
        source.Update()
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(source.GetOutput())
        ring = vtkFollower()
        ring.SetMapper(mapper)
        ring.SetCamera(self._renderer.GetActiveCamera())
        ring.SetObjectName('seedGizmo:ring')
        prop = ring.GetProperty()
        prop.SetColor(1.0, 1.0, 1.0)
        prop.SetLineWidth(2.0)
        prop.SetLighting(False)
        ring.PickableOff()
        ring.VisibilityOff()
        return ring

    def _crossActor(self):
        """Plan 36 RP10. An X over the ball, facing the camera."""
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkFollower, vtkPolyDataMapper
        from foammesh.view.theming.vtk_theme import rgb

        r = _CROSS_RADIUS
        points = vtkPoints()
        for x, y in ((-r, -r), (r, r), (-r, r), (r, -r)):
            points.InsertNextPoint(x, y, 0.0)
        lines = vtkCellArray()
        lines.InsertNextCell(2, (0, 1))
        lines.InsertNextCell(2, (2, 3))
        polyData = vtkPolyData()
        polyData.SetPoints(points)
        polyData.SetLines(lines)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(polyData)
        cross = vtkFollower()
        cross.SetMapper(mapper)
        cross.SetCamera(self._renderer.GetActiveCamera())
        cross.SetObjectName('seedGizmo:cross')
        prop = cross.GetProperty()
        prop.SetColor(*rgb(_textColour()))
        prop.SetLineWidth(3.0)
        prop.SetLighting(False)
        cross.PickableOff()
        cross.VisibilityOff()
        return cross

    def _buildShadows(self, main, colours) -> None:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
        from foammesh.view.theming.vtk_theme import rgb

        def lines(count, name, colour, width):
            points = vtkPoints()
            points.SetNumberOfPoints(2 * count)
            for index in range(2 * count):
                points.SetPoint(index, *self._point)
            cells = vtkCellArray()
            for index in range(count):
                cells.InsertNextCell(2, (2 * index, 2 * index + 1))
            polyData = vtkPolyData()
            polyData.SetPoints(points)
            polyData.SetLines(cells)
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(polyData)
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.SetObjectName(name)
            prop = actor.GetProperty()
            prop.SetColor(*rgb(colour))
            prop.SetOpacity(SHADOW_OPACITY)
            prop.SetLineWidth(width)
            prop.SetLighting(False)
            actor.PickableOff()
            # Inside the box that is already drawn: never widens a fit.
            actor.UseBoundsOff()
            actor.VisibilityOff()
            main.AddActor(actor)
            return actor, polyData

        for axis in range(3):
            letter = 'XYZ'[axis]
            cross = lines(2, f'seedGizmo:shadow{letter}', colours[axis], 2.0)
            drop = lines(_DROP_DASHES, f'seedGizmo:drop{letter}',
                         colours[axis], 1.0)
            self._shadows[axis] = (*cross, *drop)

    def _readoutActor(self):
        from vtkmodules.vtkRenderingCore import vtkTextActor
        from foammesh.view.theming.vtk_theme import rgb

        colour = _textColour()
        text = vtkTextActor()
        text.SetObjectName('seedGizmo:readout')
        prop = text.GetTextProperty()
        prop.SetFontSize(_READOUT_SIZE)
        prop.SetColor(*rgb(colour))
        prop.ShadowOn()
        text.PickableOff()
        text.VisibilityOff()
        return text

    def close(self) -> None:
        """Take the handle out of the viewport and let go of the interactor."""
        if self._closed:
            return
        self._closed = True
        self._frame.stop()
        for subject, tag in self._observers:
            try:
                subject.RemoveObserver(tag)
            except Exception:                                 # noqa: BLE001
                pass
        self._observers = []
        if self._main is not None:
            for cross, _crossData, drop, _dropData in self._shadows.values():
                self._main.RemoveActor(cross)
                self._main.RemoveActor(drop)
        self._shadows, self._main, self._readout = {}, None, None
        if self._window is not None and self._renderer is not None:
            self._window.RemoveRenderer(self._renderer)
        self._renderer = self._window = None
        self._handles, self._actors, self._ring = {}, [], None
        self._cross = None
        self._refresh()

    # -- state -------------------------------------------------------------- #

    def position(self) -> tuple:
        return self._point

    def setPosition(self, point) -> None:
        """Put the seed at *point* as typed: not clamped, and nothing emitted."""
        self._point = tuple(float(value) for value in point)
        self._place()
        self._updateShadows()
        self._refresh()

    def setBounds(self, bounds) -> None:
        """The box the seed is kept in, ``(xmin, xmax, ..., zmax)`` or None.

        The seed's shadows fall on its walls, so without a box there are none.
        """
        self._bounds = (tuple(float(value) for value in bounds)
                        if bounds is not None else None)
        self._octant = None
        self._updateShadows()
        self._refresh()

    def bounds(self):
        return self._bounds

    def setStep(self, step) -> None:
        """What Ctrl snaps to and a key nudges by: one number or one per axis."""
        if step is None:
            self._step = None
            return
        if isinstance(step, (int, float)):
            step = (step, step, step)
        values = tuple(float(value) for value in step)
        self._step = values if all(value > 0 for value in values) else None

    def step(self):
        if self._step is not None:
            return self._step
        if self._bounds is not None:
            longest = max(self._bounds[1] - self._bounds[0],
                          self._bounds[3] - self._bounds[2],
                          self._bounds[5] - self._bounds[4])
            if longest > 0:
                return (longest / 100.0,) * 3
        return None

    def setColour(self, colour: str) -> None:
        """The ball's colour: the seed verdict's (green in, red out)."""
        self._colour = colour
        ball = self._handles.get((CENTRE, None))
        if ball is not None:
            from foammesh.view.theming.vtk_theme import rgb
            ball.GetProperty().SetColor(*rgb(colour))

    def colour(self) -> str:
        return self._colour

    def role(self) -> str:
        """Plan 37 UF16. `SEED` or `EXCLUDE`: what this handle places."""
        return self._role

    def setVerdict(self, verdict) -> None:
        """Plan 36 RP10. Say the verdict in the handle's shape as well.

        ``'inside'`` draws the ball solid; ``None`` or ``'unknown'`` hollow;
        a seed outside, on a wall or open to the outside hollow and crossed.
        """
        self._verdict = verdict
        self._applyVerdict()

    def verdict(self):
        return self._verdict

    def shape(self) -> str:
        """`SOLID`, `HOLLOW` or `CROSSED`: what the handle's shape says."""
        if self._role == EXCLUDE:
            # Plan 37 UF16. An exclude point may sit in any space; only one
            # on a wall is a point v13 ignores (live S6).
            return CROSSED if self._verdict == 'on_surface' else SOLID
        if self._verdict == 'inside':
            return SOLID
        return CROSSED if self._verdict in WRONG_PLACE else HOLLOW

    def _applyVerdict(self) -> None:
        shape = self.shape()
        ball = self._handles.get((CENTRE, None))
        if ball is not None:
            prop = ball.GetProperty()
            if shape == SOLID:
                prop.SetRepresentationToSurface()
            else:
                prop.SetRepresentationToWireframe()
                prop.SetLineWidth(2.0)
        if self._cross is not None:
            self._cross.SetVisibility(shape == CROSSED)

    def setEnabled(self, enabled: bool) -> None:
        """A disabled handle is drawn but takes no press and no key."""
        self._enabled = bool(enabled)
        if not enabled:
            self._cancelDrag()

    def isDragging(self) -> bool:
        return self._mode is not None

    def cancelDrag(self) -> None:
        """Undo the drag in progress, as Esc does (Plan 36 RP13 #8)."""
        self._cancelDrag()

    def hovered(self):
        """What a press here would grab: ``(mode, axis)`` or ``None``."""
        return self._hovered

    def actors(self) -> list:
        return list(self._actors)

    def renderer(self):
        return self._renderer

    def handleScale(self) -> float:
        """The world length of an arrow at the last render."""
        return self._scaleValue

    # -- RP9: plane mode (the section plane through the seed) --------------- #

    def setPlaneMode(self, axis) -> None:
        """Keep every drag on the plane normal to *axis* (0, 1 or 2).

        ``None`` leaves plane mode. A drag in progress is cancelled either
        way, since its constraint is about to change under it.
        """
        if axis is not None and axis not in (0, 1, 2):
            raise ValueError(f'a section plane is normal to X, Y or Z: {axis!r}')
        self._cancelDrag()
        self._planeAxis = axis
        self._pushStart = None
        if self._hovered is not None and not self._usable(*self._hovered):
            self._hovered = None
            self._highlight()
        self._place()
        self._refresh()

    def planeAxis(self):
        """The axis the section plane is normal to, or ``None``."""
        return self._planeAxis

    def pushPlane(self, coordinate: float, finished: bool = False) -> None:
        """The section plane was pushed to *coordinate* along its normal.

        The seed goes with it, kept in the box; each call is a step of the
        push (`pointMoved`). The call that ends the push passes *finished*,
        which makes the push one placement (`placementFinished`) when the
        seed moved at all.
        """
        axis = self._planeAxis
        if axis is None:
            return
        if self._pushStart is None:
            self._pushStart = self._point
        moved = list(self._point)
        moved[axis] = float(coordinate)
        clamped = self.clamp(tuple(moved))
        self._moveTo(tuple(clamped[i] if i == axis else self._point[i]
                           for i in range(3)))
        if finished:
            start, self._pushStart = self._pushStart, None
            if self._point != start:
                self.placementFinished.emit(self._point)

    def dropOnPlane(self, x, y) -> bool:
        """Put the seed where the ray through pixel (x, y) meets the plane.

        One placement. False, and nothing moves, outside plane mode or when
        the plane is seen edge-on.
        """
        axis = self._planeAxis
        if axis is None or self._renderer is None:
            return False
        self._syncCamera()
        origin, direction = self._ray(x, y)
        hit = rayPlane(self._point, _unit(axis), origin, direction)
        if hit is None:
            return False
        self._moveTo(self._onPlane(self.clamp(hit), self._point))
        self.placementFinished.emit(self._point)
        return True

    def _onPlane(self, target, start):
        """*target* with its normal coordinate put back to *start*'s."""
        axis = self._planeAxis
        if axis is None:
            return tuple(target)
        return tuple(start[i] if i == axis else target[i] for i in range(3))

    def _usable(self, mode, axis) -> bool:
        """RP9. In plane mode only the handles that stay on the plane count."""
        if self._planeAxis is None or mode == CENTRE:
            return True
        if mode == AXIS:
            return axis != self._planeAxis
        return axis == self._planeAxis

    # -- RP4: shadows on the back walls, and the readout -------------------- #

    def shadowWalls(self):
        """The walls the shadows are on, ``((axis, coordinate), ...)``, or ()."""
        if self._bounds is None or self._main is None:
            return ()
        from foammesh.rendering.domain_box_actor import backWalls, cameraOctant

        octant = self._octant or cameraOctant(
            self._bounds, self._main.GetActiveCamera())
        return backWalls(self._bounds, octant)

    def shadowActors(self) -> dict:
        """Per axis: ``(cross-hair actor, drop-line actor)``."""
        return {axis: (parts[0], parts[2])
                for axis, parts in self._shadows.items()}

    def readoutText(self) -> str:
        """What the readout by the cursor says, or '' when it is hidden."""
        if self._readout is None or not self._readout.GetVisibility():
            return ''
        return self._readout.GetInput() or ''

    @staticmethod
    def readoutFor(point) -> str:
        x, y, z = point
        return f'x {x:.4f} \u00b7 y {y:.4f} \u00b7 z {z:.4f} m'

    def readoutLabel(self, point) -> str:
        """What this handle's readout says at *point*; an exclude point's
        says so first (Plan 37 UF16)."""
        text = self.readoutFor(point)
        return f'Exclude \u00b7 {text}' if self._role == EXCLUDE else text

    def _beforeSceneRender(self, *_args) -> None:
        """Choose the walls again, but only when the camera changed octant."""
        if self._bounds is None or self._main is None:
            return
        from foammesh.rendering.domain_box_actor import cameraOctant

        octant = cameraOctant(self._bounds, self._main.GetActiveCamera())
        if octant != self._octant:
            self._octant = octant
            self._updateShadows()

    def _updateShadows(self) -> None:
        if not self._shadows:
            return
        walls = self.shadowWalls()
        if not walls:
            for cross, _crossData, drop, _dropData in self._shadows.values():
                cross.VisibilityOff()
                drop.VisibilityOff()
            return
        if self._octant is None:
            from foammesh.rendering.domain_box_actor import cameraOctant
            self._octant = cameraOctant(self._bounds,
                                        self._main.GetActiveCamera())
        b, p = self._bounds, self._point
        for axis, wall in walls:
            cross, crossData, drop, dropData = self._shadows[axis]
            first, second = [index for index in range(3) if index != axis]
            shorter = min(b[2 * first + 1] - b[2 * first],
                          b[2 * second + 1] - b[2 * second])
            half = 0.5 * SHADOW_CROSS * shorter
            foot = list(p)
            foot[axis] = wall
            points = crossData.GetPoints()
            index = 0
            for along in (first, second):
                for sign in (-1.0, 1.0):
                    end = list(foot)
                    end[along] += sign * half
                    points.SetPoint(index, *end)
                    index += 1
            points.Modified()
            crossData.Modified()
            points = dropData.GetPoints()
            for dash in range(_DROP_DASHES):
                for end, t in enumerate((dash / _DROP_DASHES,
                                         (dash + 0.5) / _DROP_DASHES)):
                    points.SetPoint(2 * dash + end, *(
                        p[i] + t * (foot[i] - p[i]) for i in range(3)))
            points.Modified()
            dropData.Modified()
            cross.VisibilityOn()
            drop.VisibilityOn()

    def _showReadout(self, x, y) -> None:
        if self._readout is None:
            return
        self._readout.SetInput(self.readoutLabel(self._point))
        self._readout.SetDisplayPosition(int(x) + _READOUT_OFFSET[0],
                                         int(y) + _READOUT_OFFSET[1])
        self._readout.VisibilityOn()

    def _hideReadout(self) -> None:
        if self._readout is not None:
            self._readout.VisibilityOff()

    # -- clamping and snapping ---------------------------------------------- #

    def clamp(self, point) -> tuple:
        if self._bounds is None:
            return tuple(point)
        b = self._bounds
        diagonal = math.sqrt((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2
                             + (b[5] - b[4]) ** 2)
        inset = WALL_INSET * diagonal
        clamped = []
        for axis in range(3):
            low, high = b[2 * axis] + inset, b[2 * axis + 1] - inset
            if low > high:
                low = high = 0.5 * (b[2 * axis] + b[2 * axis + 1])
            clamped.append(min(max(point[axis], low), high))
        return tuple(clamped)

    def setSnapOrigin(self, origin) -> None:
        """RP13 #2. The blockMesh origin a snap counts steps from.

        ``None`` counts from the box corner, which is the origin of the
        derived single block.
        """
        self._snapOrigin = (tuple(float(value) for value in origin)
                            if origin is not None else None)

    def snapOrigin(self) -> tuple:
        if self._snapOrigin is not None:
            return self._snapOrigin
        if self._bounds is not None:
            return tuple(self._bounds[0::2])
        return (0.0, 0.0, 0.0)

    def setFaceGuard(self, grid, maxLevel: int = 0) -> None:
        """RP13 #2. The background grid a seed is kept off the faces of.

        *grid* is a `face_clearance.BackgroundGrid` (``None`` for none) and
        *maxLevel* the case's highest refinement level.
        """
        self._faceGrid = grid
        self._faceLevel = max(0, int(maxLevel or 0))

    def faceGuard(self):
        return self._faceGrid, self._faceLevel

    def guard(self, point) -> tuple:
        """*point* moved off any mesh face it is within 1e-3 of a cell of."""
        if self._faceGrid is None:
            return tuple(point)
        from foammesh.core.mesh.face_clearance import nudge_off_faces

        return self.clamp(nudge_off_faces(point, self._faceGrid,
                                          self._faceLevel))

    def snap(self, point, axes=(0, 1, 2)) -> tuple:
        """Snap the coordinates in *axes* to a third of a step past a whole one.

        RP13 #2. Whole steps from the box corner are exactly the faces of a
        uniform background block, and snappy cannot seed on a face. A third
        of a step is never on a face at any refinement level either.
        """
        from foammesh.core.mesh.face_clearance import snap_value

        step = self.step()
        if step is None:
            return tuple(point)
        origin = self.snapOrigin()
        snapped = list(point)
        for axis in axes:
            snapped[axis] = snap_value(point[axis], origin[axis], step[axis])
        return tuple(snapped)

    def wallAt(self, point=None):
        """The wall the seed is held against, e.g. ``'+X'``, or ``None``."""
        if self._bounds is None:
            return None
        point = point or self._point
        b = self._bounds
        diagonal = math.sqrt((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2
                             + (b[5] - b[4]) ** 2)
        tolerance = 2.0 * WALL_INSET * diagonal
        for axis in range(3):
            if point[axis] <= b[2 * axis] + tolerance:
                return '-' + 'XYZ'[axis]
            if point[axis] >= b[2 * axis + 1] - tolerance:
                return '+' + 'XYZ'[axis]
        return None

    # -- keys --------------------------------------------------------------- #

    def nudge(self, axis: int, direction: int, factor: float = 1.0) -> None:
        """Move the seed one step along *axis*; one placement."""
        step = self.step()
        if step is None or axis not in (0, 1, 2):
            return
        moved = list(self._point)
        moved[axis] += direction * factor * step[axis]
        self._moveTo(self.guard(self.clamp(tuple(moved))))
        self.placementFinished.emit(self._point)

    # -- the viewport ------------------------------------------------------- #

    def _refresh(self) -> None:
        refresh = getattr(self._view, 'refresh', None)
        if callable(refresh):
            refresh()

    def _syncCamera(self) -> None:
        main = self._mainRenderer()
        if main is None or self._renderer is None:
            return
        self._renderer.GetActiveCamera().DeepCopy(main.GetActiveCamera())

    def _worldPerPixel(self) -> float:
        camera = self._renderer.GetActiveCamera()
        height = max(1, self._renderer.GetSize()[1])
        if camera.GetParallelProjection():
            return 2.0 * camera.GetParallelScale() / height
        position = camera.GetPosition()
        direction = camera.GetDirectionOfProjection()
        distance = abs(_dot(_sub(self._point, position), direction))
        if distance <= 0:
            distance = camera.GetDistance()
        return (2.0 * distance * math.tan(math.radians(camera.GetViewAngle()) / 2.0)
                / height)

    def _place(self) -> None:
        """Size the handle for the current camera and put it at the seed."""
        if self._renderer is None:
            return
        self._syncCamera()
        scale = ARROW_PIXELS * self._worldPerPixel()
        if not math.isfinite(scale) or scale <= 0:
            scale = self._scaleValue
        self._scaleValue = scale
        facing = self._renderer.GetActiveCamera().GetDirectionOfProjection()
        for (mode, axis), actor in self._handles.items():
            actor.SetPosition(*self._point)
            actor.SetScale(scale, scale, scale)
            if mode == CENTRE or (self._mode, self._axis) == (mode, axis):
                continue
            along = abs(facing[axis])
            # An arrow pointing at the camera covers the ball and cannot be
            # dragged; a square seen edge-on cannot be hit. Both stand aside.
            usable = (along < _EDGE_ON if mode == AXIS
                      else along > _SQUARE_EDGE_ON)
            actor.SetVisibility(usable and self._usable(mode, axis))
        if self._cross is not None:
            self._cross.SetPosition(*self._point)
            self._cross.SetScale(scale, scale, scale)
        self._placeRing()

    def _placeRing(self) -> None:
        if self._ring is None:
            return
        hovered = self._mode_and_axis() if self._mode is not None else self._hovered
        if hovered is None:
            self._ring.VisibilityOff()
            return
        mode, axis = hovered
        s = self._scaleValue
        if mode == CENTRE:
            offset = (0.0, 0.0, 0.0)
        elif mode == AXIS:
            offset = _scale(_unit(axis), 0.85 * s)
        else:
            middle = 0.5 * (_SQUARE_NEAR + _SQUARE_FAR) * s
            offset = tuple(0.0 if index == axis else middle for index in range(3))
        self._ring.SetPosition(*_add(self._point, offset))
        self._ring.SetScale(s, s, s)
        self._ring.VisibilityOn()

    def _mode_and_axis(self):
        return (self._mode, self._axis)

    def _beforeRender(self, *_args) -> None:
        self._place()
        self._renderer.ResetCameraClippingRange()

    def _ray(self, x, y):
        """The mouse ray through display pixel (x, y): origin and direction."""
        renderer = self._renderer
        renderer.SetDisplayPoint(float(x), float(y), 0.0)
        renderer.DisplayToWorld()
        near = renderer.GetWorldPoint()
        renderer.SetDisplayPoint(float(x), float(y), 1.0)
        renderer.DisplayToWorld()
        far = renderer.GetWorldPoint()
        near = tuple(near[index] / near[3] for index in range(3))
        far = tuple(far[index] / far[3] for index in range(3))
        return near, _sub(far, near)

    def pick(self, x, y):
        """The handle under display pixel (x, y): ``(mode, axis)`` or None."""
        if self._renderer is None or self._picker is None:
            return None
        self._syncCamera()
        self._place()
        if not self._picker.Pick(float(x), float(y), 0.0, self._renderer):
            return None
        prop = self._picker.GetViewProp()
        for key, actor in self._handles.items():
            if actor is prop:
                return key
        return None

    # -- interactor observers ----------------------------------------------- #

    def _takeEvent(self, interactor, event) -> None:
        """Stop the camera style from seeing this event."""
        for subject, tag in self._observers:
            if subject is not interactor:
                continue
            command = interactor.GetCommand(tag)
            # VTK clears a command's abort flag before it runs it, so setting
            # it on all of ours stops only the event now being handled.
            if command is not None:
                command.SetAbortFlag(1)

    def _pressed(self, interactor, event) -> None:
        if not self._enabled or self._renderer is None:
            return
        x, y = interactor.GetEventPosition()
        grabbed = self.pick(x, y)
        if grabbed is None:
            # RP9. A double-click off the handles drops the seed on the plane.
            repeat = getattr(interactor, 'GetRepeatCount', None)
            if (self._planeAxis is not None and self._mode is None
                    and repeat is not None and repeat() > 0):
                self._takeEvent(interactor, event)
                self.dropOnPlane(x, y)
            return
        self._takeEvent(interactor, event)
        mode, axis = grabbed
        start = self._point
        ray = self._ray(x, y)
        grab = self._grabValue(mode, axis, start, ray)
        if grab is None:
            return
        self._mode, self._axis = mode, axis
        self._dragStart, self._grab = start, grab
        self._placeRing()
        self._showReadout(x, y)
        self.dragging.emit(True)
        self._refresh()

    def _grabValue(self, mode, axis, start, ray):
        origin, direction = ray
        if mode == AXIS:
            return closestOnAxis(start, axis, origin, direction)
        hit = rayPlane(start, self._dragNormal(mode, axis), origin, direction)
        return None if hit is None else _sub(hit, start)

    def _dragNormal(self, mode, axis):
        """The plane a ball or square drag slides on, by its normal."""
        if mode == PLANE:
            return _unit(axis)
        if self._planeAxis is not None:
            # RP9. The ball slides on the section plane, not the view plane.
            return _unit(self._planeAxis)
        return self._renderer.GetActiveCamera().GetDirectionOfProjection()

    def _moved(self, interactor, event) -> None:
        if self._renderer is None:
            return
        x, y = interactor.GetEventPosition()
        if self._mode is None:
            # Hover: what a press here would grab. The camera keeps the move.
            if self._enabled:
                self._hoverPending = (x, y)
                self._frame.start()
            return
        self._takeEvent(interactor, event)
        self._pending = (x, y, bool(interactor.GetControlKey()),
                         bool(interactor.GetShiftKey()))
        if not self._frame.isActive():
            self._frame.start()

    def _released(self, interactor, event) -> None:
        if self._mode is None:
            return
        self._takeEvent(interactor, event)
        self._applyFrame()
        moved = self._point != self._dragStart
        self._mode = self._axis = None
        self._dragStart = self._grab = None
        self._placeRing()
        self._hideReadout()
        self.dragging.emit(False)
        self._refresh()
        if moved:
            self.placementFinished.emit(self._point)

    def _keyPressed(self, interactor, event) -> None:
        if not self._enabled:
            return
        key = interactor.GetKeySym()
        if key == 'Escape' and self._mode is not None:
            self._takeEvent(interactor, event)
            self._cancelDrag()
            return
        if key in _NUDGE_KEYS and self._mode is None:
            self._takeEvent(interactor, event)
            axis, direction = _NUDGE_KEYS[key]
            factor = (10.0 if interactor.GetShiftKey()
                      else 0.1 if interactor.GetAltKey() else 1.0)
            self.nudge(axis, direction, factor)

    def _cancelDrag(self) -> None:
        """Esc: the drag in progress is undone, and the seed goes back."""
        if self._mode is None:
            return
        start = self._dragStart
        self._frame.stop()
        self._pending = None
        self._mode = self._axis = None
        self._dragStart = self._grab = None
        self._hideReadout()
        self.dragging.emit(False)
        if start is not None:
            self._moveTo(start)

    # -- one frame ---------------------------------------------------------- #

    def _applyFrame(self) -> None:
        if self._closed:
            return
        if self._mode is None:
            hover, self._hoverPending = self._hoverPending, None
            if hover is not None:
                grabbed = self.pick(*hover)
                if grabbed != self._hovered:
                    self._hovered = grabbed
                    self._highlight()
                    self._placeRing()
                    self._refresh()
            return
        pending, self._pending = self._pending, None
        if pending is None:
            return
        x, y, ctrl, shift = pending
        target = self._dragTarget(x, y, shift)
        if target is None:
            return
        if ctrl:
            target = self.snap(target, self._movingAxes(target))
        # RP9. Clamping never takes the seed off the section plane.
        # RP13 #2. ...and never leaves it on a face of the mesh.
        target = self._onPlane(self.guard(self.clamp(target)), self._dragStart)
        if self._readout is not None:
            # Set before the frame is drawn, so the frame is drawn once.
            self._readout.SetInput(self.readoutFor(target))
            self._readout.SetDisplayPosition(int(x) + _READOUT_OFFSET[0],
                                             int(y) + _READOUT_OFFSET[1])
        self._moveTo(target)

    def _movingAxes(self, target):
        if self._mode == AXIS:
            return (self._axis,)
        if self._mode == PLANE:
            return tuple(index for index in range(3) if index != self._axis)
        return tuple(index for index in range(3)
                     if abs(target[index] - self._dragStart[index]) > 0)

    def _dragTarget(self, x, y, shift):
        self._syncCamera()
        origin, direction = self._ray(x, y)
        start = self._dragStart
        if self._mode == AXIS:
            along = closestOnAxis(start, self._axis, origin, direction)
            if along is None:
                return None
            moved = list(start)
            moved[self._axis] = start[self._axis] + (along - self._grab)
            return tuple(moved)
        hit = rayPlane(start, self._dragNormal(self._mode, self._axis),
                       origin, direction)
        if hit is None:
            return None
        target = _sub(hit, self._grab)
        if self._mode == PLANE:
            # Exactly in the plane: the normal coordinate does not drift.
            target = tuple(start[index] if index == self._axis else target[index]
                           for index in range(3))
        target = self._onPlane(target, start)
        if shift:
            # BaramMesh's behaviour: only the axis most along the motion.
            delta = _sub(target, start)
            axis = max(range(3), key=lambda index: abs(delta[index]))
            target = tuple(target[index] if index == axis else start[index]
                           for index in range(3))
        return target

    def _moveTo(self, point) -> None:
        point = tuple(float(value) for value in point)
        if point == self._point:
            return
        self._point = point
        self._place()
        self._updateShadows()
        self.pointMoved.emit(point)
        self._refresh()

    def _highlight(self) -> None:
        for key, actor in self._handles.items():
            width = _HOVER_WIDTH if key == self._hovered else 1.0
            actor.GetProperty().SetAmbient(0.35 * width)


# -- geometry, in arrow lengths ---------------------------------------------- #

def _ballPolyData():
    from vtkmodules.vtkFiltersSources import vtkSphereSource

    source = vtkSphereSource()
    source.SetRadius(_BALL_RADIUS)
    source.SetThetaResolution(24)
    source.SetPhiResolution(16)
    source.Update()
    return source.GetOutput()


def _cubePolyData():
    """Plan 37 UF16. The exclude handle: a cube the seed ball's size."""
    from vtkmodules.vtkFiltersSources import vtkCubeSource

    side = 2.0 * _BALL_RADIUS * 0.85
    source = vtkCubeSource()
    source.SetXLength(side)
    source.SetYLength(side)
    source.SetZLength(side)
    source.Update()
    return source.GetOutput()


def _transformed(polyData, transform):
    from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter

    apply = vtkTransformPolyDataFilter()
    apply.SetInputData(polyData)
    apply.SetTransform(transform)
    apply.Update()
    return apply.GetOutput()


def _arrowPolyData(axis):
    """An arrow of length one from the ball's edge along +axis."""
    from vtkmodules.vtkCommonTransforms import vtkTransform
    from vtkmodules.vtkFiltersSources import vtkArrowSource

    source = vtkArrowSource()
    source.SetTipResolution(20)
    source.SetShaftResolution(12)
    source.SetShaftRadius(0.035)
    source.SetTipRadius(0.09)
    source.SetTipLength(0.28)
    source.Update()
    transform = vtkTransform()
    if axis == 1:
        transform.RotateZ(90.0)
    elif axis == 2:
        transform.RotateY(-90.0)
    return _transformed(source.GetOutput(), transform)


def _squarePolyData(axis):
    """The square in the plane normal to *axis*, off the ball's corner."""
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    near, far = _SQUARE_NEAR, _SQUARE_FAR
    first, second = [index for index in range(3) if index != axis]
    points = vtkPoints()
    for a, b in ((near, near), (far, near), (far, far), (near, far)):
        corner = [0.0, 0.0, 0.0]
        corner[first], corner[second] = a, b
        points.InsertNextPoint(*corner)
    quads = vtkCellArray()
    quads.InsertNextCell(4, (0, 1, 2, 3))
    polyData = vtkPolyData()
    polyData.SetPoints(points)
    polyData.SetPolys(quads)
    return polyData
