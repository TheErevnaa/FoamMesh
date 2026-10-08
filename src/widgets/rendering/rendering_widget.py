#!/usr/bin/env python
# -*- coding: utf-8 -*-

# A simple script to demonstrate the vtkCutter function

import logging
import math
import platform
import threading
import time
from typing import Optional

# noinspection PyUnresolvedReferences
import vtkmodules.vtkInteractionStyle
# noinspection PyUnresolvedReferences
import vtkmodules.vtkRenderingOpenGL2
from PySide6.QtCore import QTimer, Qt, Signal, QObject
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout,
    QVBoxLayout, QWidget)
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.util.misc import calldata_type
from vtkmodules.vtkCommonCore import VTK_STRING, vtkCommand, vtkStringArray
# load implementations for rendering and interaction factory classes
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkInteractionWidgets import vtkLogoRepresentation, vtkLogoWidget, vtkOrientationMarkerWidget
from vtkmodules.vtkIOImage import vtkPNGReader, vtkPNGWriter
from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor, vtkCubeAxesActor
from vtkmodules.vtkRenderingCore import vtkActor, vtkRenderer, vtkPropPicker, vtkLightKit, vtkProp

from foammesh.support import disposal, heartbeat, lifecycle, safe_mode
from foammesh.support.vtk_threads import isRenderingHold

from foammesh.rendering import (
    gl_health, gpu_profile, interaction_lod, render_style)
from foammesh.view.theming.metrics import apply_prose_measure
from foammesh.view.theming.vtk_theme import apply_vtk_theme
from foammesh.core.quantities import format_group
from app_properties import meshAppProperties
from foammesh.core.branding import watermark_geometry


logger = logging.getLogger(__name__)

RENDER_DELAY_TIME = 200
REPAINT_SUPPRESS_TIME = 100

#: Multisampling for the opaque scene at start-up -- the Balanced preset's.
#: Depth peeling cannot coexist with it on the OpenGL2 backend; how the view
#: trades one for the other now lives in foammesh.rendering.render_style.
MULTI_SAMPLES = render_style.quality(render_style.DEFAULT_PRESET).multiSamples

#: How many camera positions the back/forward history remembers.
VIEW_HISTORY_LIMIT = 24

#: The axis views and the isometric view: (direction of projection, view up).
_VIEW_PRESETS = {
    '+x': ((-1, 0, 0), (0, 0, 1)),
    '-x': ((1, 0, 0), (0, 0, 1)),
    '+y': ((0, -1, 0), (0, 0, 1)),
    '-y': ((0, 1, 0), (0, 0, 1)),
    '+z': ((0, 0, -1), (0, 1, 0)),
    '-z': ((0, 0, 1), (0, 1, 0)),
    'isometric': ((-1, -1, -1), (0, 0, 1)),
}


def _presetDirections(preset: str):
    try:
        return _VIEW_PRESETS[preset.lower()]
    except KeyError as error:
        raise ValueError(f'unknown view preset: {preset}') from error


def _sameModel(old, new) -> bool:
    """DP-736. Whether two scene bounds frame the same model.

    A reload, a new time step or geometry-to-mesh on the same part keeps the
    centre and the size within half of the model; another project's model
    does not. A view recorded on the one is still a view of the other.
    """
    def diagonal(bounds):
        return math.sqrt(sum((bounds[i + 1] - bounds[i]) ** 2
                             for i in (0, 2, 4)))

    def centre(bounds):
        return [(bounds[i] + bounds[i + 1]) / 2 for i in (0, 2, 4)]

    a, b = diagonal(old), diagonal(new)
    size = max(a, b)
    if size <= 0:
        return centre(old) == centre(new)
    if min(a, b) < 0.5 * size:
        return False
    return math.dist(centre(old), centre(new)) <= 0.5 * size

#: Dwell before the part under the cursor is named. Long enough that crossing
#: the mesh does not make the readout flicker, short enough to feel immediate.
HOVER_DWELL_TIME = 180

#: VTK's own interactive frame rate (the interactor's default). It is no
#: longer asked for: at 15 the style and every 3D widget made
#: ``vtkQuadricLODActor`` (the volume's MeshActor) build its decimated copy on
#: the GUI thread at the first moving frame -- MEASURED 1.29 s for 20 M quads
#: on this machine's iGPU, and 1.0 s more for the frame on release -- and
#: rebuild it after every cut. Drags use ``interaction_lod`` instead.
INTERACTIVE_UPDATE_RATE = 15.0
STILL_UPDATE_RATE = 0.0001


def _holdRenderRate(widget) -> None:
    """Keep VTK's requested frame rate at the still rate for every frame."""
    interactor = getattr(widget, '_Iren', None)
    if interactor is not None:
        interactor.SetDesiredUpdateRate(STILL_UPDATE_RATE)
        interactor.SetStillUpdateRate(STILL_UPDATE_RATE)
    widget.GetRenderWindow().SetDesiredUpdateRate(STILL_UPDATE_RATE)
#: Quiet time after the last frame before the reduced copies are made, so a
#: scene being built part by part is reduced once, when it is complete.
INTERACTION_DETAIL_DELAY = 750

#: Pixels one cube-axis label needs before the next one starts touching it.
#: Measured against the default `%-#6.3g` format at twelve points: "-0.0123"
#: is about sixty pixels wide, and two of them need a gap to read as two.
LABEL_SPACING = 72
#: What `vtkCubeAxesActor` draws per axis when nothing constrains it.
CUBE_AXES_LABELS = 6


def cubeAxesPolicy(width: int, height: int) -> dict:
    """How many axis labels fit on a viewport this size, and how to draw them.

    F-44: the cube axes were built with a fixed twelve-point label size and no
    tick policy at all, so the same six labels per axis were drawn whether the
    viewport was 1600 pixels across or 320, and on a docked pane they overlaid
    each other. The actor has no label-count setter, so the budget is spent on
    what it does expose -- label size, significant digits, and whether the
    labels are drawn at all.

    Pure, and in pixels, so it can be checked without a render window.
    """
    extent = max(1, min(int(width or 0), int(height or 0)))
    labels = min(CUBE_AXES_LABELS, extent // LABEL_SPACING)
    if labels >= CUBE_AXES_LABELS:
        return {'labels': labels, 'screen_size': 12,
                'label_format': '%-#6.3g', 'gridlines': True}
    if labels >= 3:
        # Fewer pixels per label: the same numbers in a smaller face, with one
        # significant figure dropped so each one is narrower than its slot.
        return {'labels': labels, 'screen_size': 10,
                'label_format': '%-#5.2g', 'gridlines': True}
    if labels >= 2:
        return {'labels': labels, 'screen_size': 9,
                'label_format': '%-#4.2g', 'gridlines': False}
    # Two labels cannot be told apart on an axis this short. The box and its
    # ticks still say where the model sits; numbers that overlap say less
    # than none.
    return {'labels': 0, 'screen_size': 9,
            'label_format': '%-#4.2g', 'gridlines': False}


#: DP-704. Width of one label character, as a fraction of the actor's
#: screen size: a twelve-point "0.20" measured 39 pixels offscreen.
LABEL_CHAR_WIDTH = 0.8
#: Clear space between two labels on the same axis, in pixels.
LABEL_GAP = 8


def cubeAxisTicks(lo: float, hi: float) -> list[float]:
    """Roughly the values `vtkCubeAxesActor` labels along one axis.

    A 1-2-5 step giving at most six intervals, which is what the actor's own
    tick adjustment lands on for the ranges a mesh has. Only the count and
    the widest label matter here, so near enough is enough.
    """
    span = float(hi) - float(lo)
    if not math.isfinite(span) or span <= 0:
        return [float(lo)]
    magnitude = 10 ** math.floor(math.log10(span / 6))
    step = next(factor * magnitude for factor in (1, 2, 2.5, 5, 10, 20)
                if span / (factor * magnitude) <= 6)
    first = math.ceil(float(lo) / step - 1e-9) * step
    ticks = []
    value = first
    while value <= float(hi) + step * 1e-9:
        ticks.append(0.0 if abs(value) < step * 1e-9 else value)
        value += step
    return ticks or [float(lo)]


def cubeAxisLabels(lo: float, hi: float) -> list[str]:
    """Roughly the strings `vtkCubeAxesActor` draws along one axis.

    The actor ignores the label format for the text it draws: it scales a
    range below 10^-1.5 or above 10^3 by a power of a thousand, puts the
    "(x10^-3)" on the title, and prints each tick with only the decimals the
    step needs -- "-30" for a 60 mm pipe, "0.20" for a 200 mm one.
    """
    ticks = cubeAxisTicks(lo, hi)
    largest = max(abs(float(lo)), abs(float(hi)))
    exponent = 0
    if largest > 0 and not 10 ** -1.5 <= largest <= 1e3:
        exponent = 3 * math.floor(math.log10(largest) / 3)
    scaled = [value / 10 ** exponent for value in ticks]
    step = abs(scaled[1] - scaled[0]) if len(scaled) > 1 else 1.0
    decimals = max(0, -math.floor(math.log10(step) + 1e-9)) if step > 0 else 0
    return [f'{value:.{decimals}f}' for value in scaled]


def cubeAxisLabelsFit(pixels: float, lo: float, hi: float,
                      screen_size: float) -> bool:
    """Whether one axis's labels fit along the length it has on screen.

    DP-704 (viewport audit 0925 F13). `cubeAxesPolicy` budgets labels by the
    size of the viewport, but the labels lie along each axis, and an axis can
    be short on screen in a large viewport: the S5 pipe seen from the side
    drew seven labels down a 125-pixel Y axis, and they ran into one block.
    The labels an axis cannot fit are better not drawn; the box and the
    other axes still say where the model sits.
    """
    labels = cubeAxisLabels(lo, hi)
    widest = max(len(label) for label in labels)
    width = widest * LABEL_CHAR_WIDTH * float(screen_size)
    return float(pixels) >= len(labels) * (width + LABEL_GAP)


def _blankAxisLabels():
    """A label list the actor repeats for every tick: one empty string."""
    labels = vtkStringArray()
    labels.InsertNextValue('')
    return labels


#: DP-1180. An axis shorter than this on screen is seen end-on: it has no
#: room for even its two end values, and its title says how long it is.
END_LABEL_MIN_PIXELS = 40


def cubeAxisTitle(axis: str, lo: float, hi: float, mode: str) -> str:
    """The title one cube axis carries: its name, its span, and maybe its ends.

    DP-1180. An axis whose tick labels did not fit used to lose every
    number it had, so a 1 m pipe seen at a normal size said nothing about
    how thick it was, and a 1 m cube in a docked pane said nothing at all.
    The span is now always in the title, in the one unit ladder the rest
    of the window measures with (``foammesh.core.quantities``); an axis
    whose ticks do not fit (``mode == 'ends'``) carries its two end values
    there instead of dropping them.
    """
    lo, hi = float(lo), float(hi)
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi < lo:
        return axis
    span = hi - lo
    if span <= 0:
        return f'{axis}  {format_group([lo])}' if lo else axis
    title = f'{axis}  {format_group([span])}'
    if mode == 'ends':
        title += f':  {format_group([lo, hi], " to ")}'
    return title


def fitCubeAxesLabels(actor, renderer, policy: dict) -> dict:
    """Label each axis as fully as it has room for; returns axis -> mode.

    ``'ticks'`` -- the actor's own tick labels fit along the axis and are
    drawn. ``'ends'`` -- they would run into one block (DP-704), so they are
    off and the title carries the two end values instead (DP-1180; an axis
    used to drop every number it had here). ``'span'`` -- the axis is seen
    end-on, or the viewport has no room for labels at all: the title still
    says how long the axis is.

    Measured on screen, from the actor's bounds through the renderer's
    current camera, so it has to run again whenever the camera moves --
    the rendering widget runs it at the start of every render.
    """
    bounds = actor.GetBounds()
    modes = {}
    origin = (bounds[0], bounds[2], bounds[4])

    def display(point):
        renderer.SetWorldPoint(point[0], point[1], point[2], 1.0)
        renderer.WorldToDisplay()
        return renderer.GetDisplayPoint()

    start = display(origin)
    for index, axis in enumerate('XYZ'):
        end = list(origin)
        end[index] = bounds[2 * index + 1]
        tip = display(end)
        pixels = math.hypot(tip[0] - start[0], tip[1] - start[1])
        lo, hi = bounds[2 * index], bounds[2 * index + 1]
        if not policy.get('labels') or pixels < END_LABEL_MIN_PIXELS:
            mode = 'span'
        elif cubeAxisLabelsFit(pixels, lo, hi, policy['screen_size']):
            mode = 'ticks'
        else:
            mode = 'ends'
        # The actor draws an axis's title only while its labels are
        # visible (measured offscreen, VTK 9.5), so an axis that cannot fit
        # its ticks keeps its labels on and blanks them instead.
        if not getattr(actor, f'Get{axis}AxisLabelVisibility')():
            getattr(actor, f'Set{axis}AxisLabelVisibility')(1)
        blank = actor.GetAxisLabels(index) is not None
        if blank != (mode != 'ticks'):
            actor.SetAxisLabels(index, None if mode == 'ticks'
                                else _blankAxisLabels())
        title = cubeAxisTitle(axis, lo, hi, mode)
        if getattr(actor, f'Get{axis}Title')() != title:
            getattr(actor, f'Set{axis}Title')(title)
        modes[axis] = mode
    return modes


def visibleBoundsWithout(renderer, excluded) -> Optional[tuple]:
    """The bounds of the visible props in *renderer*, leaving *excluded* out.

    DP-1181. ``ComputeVisiblePropBounds`` counts the cube axes themselves,
    which would keep the box at the size it was first given; this is the
    same union without them, or None when nothing visible has bounds.
    """
    props = renderer.GetViewProps()
    props.InitTraversal()
    found = None
    for _ in range(props.GetNumberOfItems()):
        prop = props.GetNextProp()
        if prop is None or prop is excluded:
            continue
        if not prop.GetVisibility() or not prop.GetUseBounds():
            continue
        bounds = prop.GetBounds()
        if bounds is None or len(bounds) != 6:
            continue
        if not all(math.isfinite(value) for value in bounds):
            continue
        if bounds[0] > bounds[1] or abs(bounds[0]) >= 1e299:
            continue
        if found is None:
            found = list(bounds)
        else:
            for i in range(3):
                found[2 * i] = min(found[2 * i], bounds[2 * i])
                found[2 * i + 1] = max(found[2 * i + 1], bounds[2 * i + 1])
    return tuple(found) if found is not None else None


def refreshCubeAxesBounds(actor, renderer) -> bool:
    """Fit the cube axes to what is visible now; True when they moved.

    DP-1181. The box was given its bounds once, when it was switched on or
    the camera was fitted after an import, so hiding a part, showing one
    again or adding one left the axes measuring a model no longer on
    screen. The rendering widget runs this at the start of every render.
    """
    bounds = visibleBoundsWithout(renderer, actor)
    if bounds is None:
        return False
    if tuple(actor.GetBounds()) == tuple(bounds):
        return False
    actor.SetBounds(bounds)
    return True


def _quietly(call, *args):
    """Call `call` if there is one; a teardown step must not stop the next."""
    if not callable(call):
        return None
    try:
        return call(*args)
    except Exception:                                       # noqa: BLE001
        return None


class RenderWindowInteractor(QVTKRenderWindowInteractor):
    def __init__(self, parent=None, **kw):
        self._delayTimer = QTimer()
        self._delayTimer.setInterval(RENDER_DELAY_TIME)
        self._delayTimer.setSingleShot(True)
        self._delayTimer.timeout.connect(self._timeout)

        self._suppressTimer = QTimer()
        self._suppressTimer.setInterval(REPAINT_SUPPRESS_TIME)
        self._suppressTimer.setSingleShot(True)

        #: Plan 35 CR8. The owner's guarded render: ``guard(draw, what)``
        #: runs ``draw`` and turns a failure into the viewport placeholder.
        self.renderGuard = None

        super().__init__(parent=parent, **kw)

    def Finalize(self):
        if self._RenderWindow is not None:
            self._RenderWindow.Finalize()
            self._RenderWindow = None

    def paintEvent(self, ev):
        if isRenderingHold():
            self._delayTimer.start()
            return

        # Plan 35 CR8. A paint is where QVTK really draws (its Render() only
        # schedules one), so it goes through the owner's guarded render.
        guard = self.renderGuard
        if guard is not None:
            guard(lambda: self._paint(ev), 'paint')
            return
        self._paint(ev)

    def _paint(self, ev):
        if platform.system() == 'Darwin':
            if not self._suppressTimer.isActive():
                self._suppressTimer.start()
                super().paintEvent(ev)
        else:
            super().paintEvent(ev)

    def _timeout(self):
        self.Render()  # Render() just calls QWidget.update(), which just schedules repaint

    def mouseDoubleClickEvent(self, ev: QMouseEvent):
        ctrl, shift = self._GetCtrlShift(ev)

        x, y = ev.position().x(), ev.position().y()

        self._setEventInformation(x, y,
                                  ctrl, shift, chr(0), 0, None)

        if self._ActiveButton == Qt.MouseButton.LeftButton:
            self._Iren.InvokeEvent(vtkCommand.LeftButtonDoubleClickEvent, None)
        elif self._ActiveButton == Qt.MouseButton.RightButton:
            self._Iren.InvokeEvent(vtkCommand.RightButtonDoubleClickEvent, None)
        elif self._ActiveButton == Qt.MiddleButton:
            self._Iren.InvokeEvent(vtkCommand.MiddleButtonDoubleClickEvent, None)


class MouseHandler(QObject):
    mouseClicked = Signal(float, float, bool, bool)

    def __init__(self, style):
        super().__init__()

        self._style = style
        self._pressPos = None
        self._pressed = False

    def leftButtonPressed(self, obj, event):
        self._pressed = True

        x, y = self._style.GetInteractor().GetEventPosition()
        self._pressPos = (x, y)

        handled = self._leftButtonPressed(x, y)

        # The style does not run its own handler if observer is registered
        if not handled:
            self._style.OnLeftButtonDown()

    def leftButtonReleased(self, obj, event):
        self._pressed = False

        x, y = self._style.GetInteractor().GetEventPosition()

        handled = self._leftButtonReleased(x, y)

        # The style does not run its own handler if observer is registered
        if not handled:
            self._style.OnLeftButtonUp()

    def mouseMoved(self, obj, event):
        x, y = self._style.GetInteractor().GetEventPosition()
        px, py = self._style.GetInteractor().GetLastEventPosition()

        handled = self._mouseMoved(x, y, px, py)

        # The style does not run its own handler if observer is registered
        if not handled:
            self._style.OnMouseMove()

    def _leftButtonPressed(self, x, y):
        return False

    def _leftButtonReleased(self, x, y):
        if (x, y) == self._pressPos:
            self._leftButtonClicked(x, y)
        return False

    def _leftButtonClicked(self, x, y):
        interactor = self._style.GetInteractor()
        # Shift is what asks for the whole volume rather than the face
        # under the cursor, so the picker has to carry it too.
        self.mouseClicked.emit(x, y, interactor.GetControlKey(),
                               interactor.GetShiftKey())
        return False

    def _mouseMoved(self, x, y, px, py):
        return False


class ViewportPlaceholder(QFrame):
    """Plan 35 CR8. What the viewport shows while it is not drawing.

    In graphics safe mode until the user asks for the view, and after the
    graphics driver reset the context under it. The rest of the window --
    the task list, the console, the panels -- works either way.
    """
    actionRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('viewportPlaceholder')
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        # DP-220. The heading, the paragraph under it and the button start
        # at one edge, as on the empty-case page: centred, every wrapped
        # line of the explanation began at a different place.
        ranged = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        self._title = QLabel(self)
        self._title.setObjectName('viewportPlaceholderTitle')
        self._title.setAlignment(ranged)
        self._title.setWordWrap(True)
        self._detail = QLabel(self)
        self._detail.setObjectName('viewportPlaceholderDetail')
        self._detail.setAlignment(ranged)
        self._detail.setWordWrap(True)
        apply_prose_measure(self._detail)
        self._detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._button = QPushButton(self)
        self._button.setObjectName('viewportPlaceholderAction')
        self._button.clicked.connect(self.actionRequested)
        row = QHBoxLayout()
        row.addWidget(self._button)
        row.addStretch(1)
        layout.addWidget(self._title)
        layout.addWidget(self._detail)
        layout.addLayout(row)
        layout.addStretch(1)

    def showMessage(self, title: str, detail: str, action: str) -> None:
        self._title.setText(title)
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))
        self._button.setText(action)
        self._button.setAccessibleName(action)
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)

    def title(self) -> str:
        return self._title.text()

    def detail(self) -> str:
        return self._detail.text()

    def actionText(self) -> str:
        return self._button.text()

    def actionButton(self) -> QPushButton:
        return self._button


def _currentOperation():
    """This process's last op in the CR1 heartbeat block, or None."""
    shared = heartbeat.block()
    if shared is None:
        return None
    try:
        return heartbeat.read(shared).get('last_op')
    except Exception:                                       # noqa: BLE001
        return None


def _applyQuality(view, name: str) -> bool:
    """Apply preset ``name`` to ``view``'s renderer and window as drawn now."""
    chosen = render_style.quality(name)
    view._renderQuality = chosen.name
    renderer = view._renderer
    window = view._widget.GetRenderWindow()
    view._fxaa = render_style.applyTransparency(
        renderer, window, chosen,
        view._depthPeeling and not getattr(view, '_peelingDropped', False))
    applied = render_style.applyAmbientOcclusion(
        renderer, chosen.ssao, view.modelExtent())
    view._ambientOcclusion = applied and chosen.ssao
    # Plan 35 CR8: the next frame is attributed to drawing (`render:<scene>`).
    view._renderOpPending = True
    view.refresh()
    return applied or not chosen.ssao


class RenderingWidget(QWidget):
    actorPicked = Signal(vtkActor, bool, bool)
    viewClosed = Signal()
    #: The name of the part under the cursor, or '' when the cursor is over
    #: nothing. The picture is expected to answer "what is this" on its own.
    actorHovered = Signal(str)
    #: True while the viewport is showing less than the full mesh to stay
    #: responsive. Nothing may drop detail without saying so.
    detailReduced = Signal(bool)
    #: 2026-10-01. A reduced copy made in the worker thread, for the GUI
    #: thread to put in the scene (queued across threads).
    _interactionDetailMade = Signal(object, object)
    #: DP-696. The camera history gained or lost an entry. Back and Forward
    #: used to be refreshed only by the handful of callers that remembered to
    #: ask, so a view preset recorded history the Back button never showed.
    historyChanged = Signal()
    #: Plan 35 CR8. The view stopped drawing (the reason); the placeholder
    #: with [Recreate viewport] is showing.
    viewportReset = Signal(str)
    #: The placeholder gave way to a drawing view again.
    viewportRestored = Signal()
    #: Something about how the scene is drawn the user should know (depth
    #: peeling dropped for a large scene, a weak OpenGL); '' clears it.
    renderNote = Signal(str)

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        # Plan 35 CR8: the guarded render's state. Set before anything can
        # paint, since every paint goes through `_guardedRender`.
        self._disposed = False
        self._decision = safe_mode.current()
        self._safeMode = self._decision.active
        #: None while drawing; 'safe' or 'reset' while the placeholder shows.
        self._suspended = None
        self._interactorStarted = False
        self._inRender = False
        self._renderError = None
        self._glChecked = False
        self._glInfo = {}
        self._renderNoteText = ''
        self._sceneName = ''
        self._sceneStamp = None
        self._renderOpPending = True
        self._requestedQuality = render_style.DEFAULT_PRESET
        self._stack = None

        self._dialog: Optional[QFileDialog] = None

        self._originAxes: Optional[vtkOrientationMarkerWidget] = None
        self._originAxesActor: Optional[vtkAxesActor] = None
        self._cubeAxesActor: Optional[vtkCubeAxesActor] = None
        self._themeTokens = None
        # DP-701. The gradient ends the user picked, which a theme pass must
        # not paint over; see `_keepUserBackground`.
        self._userBackground = {}

        self._actorPicker = vtkPropPicker()

        self._style = vtkInteractorStyleTrackballCamera()
        self._widget = RenderWindowInteractor(self)
        self._widget.GetRenderWindow().SetMultiSamples(
            0 if self._safeMode else MULTI_SAMPLES)
        self._widget.SetInteractorStyle(self._style)
        self._widget.renderGuard = self._guardedRender
        self._watchRenderWindow(self._widget.GetRenderWindow())
        _holdRenderRate(self._widget)

        self._depthPeeling = False
        #: Plan 35 CR8 step 2. Translucency wanted, peeling dropped (budget).
        self._peelingDropped = False
        self._viewHistory = []
        self._viewFuture = []
        #: DP-736. The visible bounds the camera history was recorded on.
        self._historyBounds = None
        self._cameraMovedByUser = False
        self._refitOnResize = False

        self._ambientOcclusion = False
        self._fxaa = False
        # render0925. One preset for anti-aliasing, peeling and cavity
        # shading; the View menu's Render quality submenu sets it.
        self._renderQuality = (render_style.SAFE_PRESET if self._safeMode
                               else render_style.DEFAULT_PRESET)
        self._interactiveDecimation = False

        self._hoverEnabled = True
        self._hoveredName = ''
        self._hoverTimer = QTimer(self)
        self._hoverTimer.setInterval(HOVER_DWELL_TIME)
        self._hoverTimer.setSingleShot(True)
        self._hoverTimer.timeout.connect(self._hoverPick)

        self._renderer = vtkRenderer()
        self._widget.GetRenderWindow().AddRenderer(self._renderer)

        # 2026-10-01. Reduced copies of the large parts, made off the GUI
        # thread and drawn only while the camera is dragged (interaction_lod).
        self._interactionDetail = interaction_lod.InteractionDetail(
            self._renderer)
        self._interactionDetailBuilding = False
        self._interactionDetailTried = False
        self._interactionDetailTimer = QTimer(self)
        self._interactionDetailTimer.setSingleShot(True)
        self._interactionDetailTimer.setInterval(INTERACTION_DETAIL_DELAY)
        self._interactionDetailTimer.timeout.connect(
            self._prepareInteractionDetail)
        self._interactionDetailMade.connect(self._installInteractionDetail)
        # self._style.SetDefaultRenderer(self._renderer)

        self._renderer.GradientBackgroundOn()
        self._renderer.SetBackground(0.82, 0.82, 0.82)
        self._renderer.SetBackground2(0.22, 0.24, 0.33)

        self._lightKit = vtkLightKit()
        # render0925 DP-727. VTK's default kit lights a curved part almost
        # evenly; the tuned one gives it form (measured in render_style).
        render_style.tuneLightKit(self._lightKit)
        self._lightKit.AddLightsToRenderer(self._renderer)

        self._logoWidget = vtkLogoWidget()
        self._logoRepresentation = None
        self._logoReader = None
        self._logoTheme = None
        self._showLogo()

        # Plan 35 CR8. The view, or the placeholder that stands in for it.
        # The stack is this widget's own layout: a parentless layout added
        # to another is owned twice (Qt's and the wrapper's) and freed twice.
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._widget)
        self._placeholder = ViewportPlaceholder(self)
        self._placeholder.actionRequested.connect(self.recreateViewport)
        self._stack.addWidget(self._placeholder)

        self._originalMouseObserver = MouseHandler(self._style)
        self._mouseObserver = self._originalMouseObserver

        self._mouseObserver.mouseClicked.connect(self._mouseClicked)

        # To pick actors
        self._style.AddObserver(vtkCommand.LeftButtonPressEvent, self._leftButtonPressEvent)
        self._style.AddObserver(vtkCommand.LeftButtonReleaseEvent, self._leftButtonReleaseEvent)

        self._style.AddObserver(vtkCommand.MouseMoveEvent, self._mouseMoveEvent)
        self._style.AddObserver(
            vtkCommand.StartInteractionEvent, self._cameraInteractionStarted)
        self._style.AddObserver(
            vtkCommand.InteractionEvent, self._cameraMoving)
        self._style.AddObserver(
            vtkCommand.EndInteractionEvent, self._cameraInteractionEnded)

        # Every embedded/temporary viewport participates in live theme changes,
        # not only the main-window instance.
        from foammesh.app import app
        if app.themeManager is not None:
            if app.themeManager.tokens is not None:
                self.applyTheme(app.themeManager.tokens)
            app.themeManager.themeChanged.connect(self._themeChanged)

        disposal.track(self, 'RenderingWidget')

        # Plan 35 CR8. Initialising the interactor makes the GL context and
        # draws, so it is the last step; in safe mode it waits for the user,
        # and until then no OpenGL at all is asked of the driver.
        if self._safeMode:
            self._suspend(
                'safe', self.tr('Graphics safe mode'),
                self.tr('{0} The viewport is off so that FoamMesh starts '
                        'safely; everything else works. Once shown it draws '
                        'with the simplest settings.').format(
                            self._decision.reason),
                self.tr('Show viewport'))
        else:
            self._startInteractor()

        # To adjust origin axes size on zoom
        # self._style.AddObserver(vtkCommand.MouseWheelForwardEvent, self._mouseWheelForwardEvent)
        # self._style.AddObserver(vtkCommand.MouseWheelBackwardEvent, self._mouseWheelBackwardEvent)
        # self._style.AddObserver(vtkCommand.InteractionEvent, self._interactionEvent)

    def interactor(self):
        return self._widget._Iren

    def renderer(self):
        return self._renderer

    def addActor(self, actor: vtkProp):
        self._renderer.AddActor(actor)

    def removeActor(self, actor: vtkProp):
        self._renderer.RemoveActor(actor)

    def refresh(self):
        self._widget.Render()

    def fitCamera(self):
        cubeAxesOn = False
        if self._cubeAxesActor is not None:
            self._hideCubeAxes()
            cubeAxesOn = True

        # `ResetCamera` fits the bounding *sphere*, which for anything longer
        # than it is wide -- a duct, a pipe, an aerofoil box -- is far larger
        # than the shape's projection. That is why a mesh used to sit small in
        # a sea of background. `ResetCameraScreenSpace` fits the projected
        # bounds to the viewport instead, so the framing follows the window's
        # aspect ratio rather than a worst-case sphere.
        #
        # Cube axes keep the sphere fit: their ticks and labels are drawn
        # outside the mesh, and a tight screen-space fit of the mesh alone
        # would crop them off the edge of the viewport.
        if cubeAxesOn or not hasattr(self._renderer, 'ResetCameraScreenSpace'):
            self._renderer.ResetCamera()
        else:
            self._renderer.ResetCameraScreenSpace()

        if cubeAxesOn:
            self._showCubeAxes()

        if self._ambientOcclusion:
            # The cavity-shading radius is a fraction of the model, and the
            # model is whatever was just loaded -- not what was on screen when
            # Quality was chosen (often nothing, which gave a 0.1 m radius).
            render_style.applyAmbientOcclusion(
                self._renderer, True, self.modelExtent())

        # A mesh fitted to a narrow viewport used to stay small when the window
        # was widened, because resizeEvent never re-fitted. It does now -- but
        # only until the user moves the camera, after which the framing is
        # theirs and re-fitting would undo their work.
        self._cameraMovedByUser = False
        self._refitOnResize = True
        self._widget.Render()

    def renderWindow(self):
        """The VTK render window this view draws into; None once finalised."""
        widget = getattr(self, '_widget', None)
        if widget is None:
            return None
        try:
            return widget.GetRenderWindow()
        except Exception:                                   # noqa: BLE001
            return None

    def releaseGraphicsResources(self):
        """Release everything this window holds on the GPU, in its own context.

        Plan 35 CR3 step 9 (F6). Done on the GUI thread before the window is
        finalised, so that no buffer, texture or shader outlives the context
        it was made in and is left for a destructor on some other thread.
        The window can still draw afterwards; it rebuilds what it needs.
        """
        window = self.renderWindow()
        if window is None or not disposal.make_current(window):
            return
        try:
            window.ReleaseGraphicsResources(window)
        except Exception:                                   # noqa: BLE001
            pass

    # -- Plan 35 CR8: every draw goes through one guarded render ----------- #

    def _watchRenderWindow(self, window) -> None:
        """Hear the window's errors ourselves, and check it after each frame.

        With an ErrorEvent observer VTK no longer prints the error, so
        `_renderWindowError` logs it.
        """
        try:
            window.AddObserver(vtkCommand.ErrorEvent, self._renderWindowError)
            window.AddObserver(vtkCommand.EndEvent, self._renderWindowEnded)
        except Exception:                                   # noqa: BLE001
            pass

    @calldata_type(VTK_STRING)
    def _renderWindowError(self, _caller, _event, message=None):
        text = ' '.join(str(message or 'the render window reported an error').split())
        logger.error('Render window error: %s', text)
        self._renderError = f'The render window reported: {text[:400]}'
        if not self._inRender:
            # Raised outside a guarded draw (a render some other code asked
            # the window for): react once control is back in the event loop.
            QTimer.singleShot(0, self._checkAfterError)

    def _checkAfterError(self):
        if self._renderError and self._suspended is None and not self._disposed:
            self._contextLost(self._renderError)

    def _renderWindowEnded(self, _caller=None, _event=None):
        if self._inRender or self._suspended is not None or self._disposed:
            return
        window = self.renderWindow()
        reason = self._renderError or (
            gl_health.lost_context(window, self._interactorStarted)
            if window is not None else None)
        if reason:
            QTimer.singleShot(0, lambda: self._contextLost(reason))

    def _guardedRender(self, draw, what: str = 'render') -> bool:
        """Run ``draw`` (anything that renders this view); False if it failed.

        A draw that raises, a window error raised while it ran, or a
        context found lost or out of memory afterwards all end the same
        way: the view is taken down and the placeholder offers to recreate
        it. Nothing reaches the event loop. Before the first frame of a new
        scene -- and the first after a style change -- ``render:<scene>``
        is this process's last operation, so that a death inside the
        driver is attributed to drawing at the next start.
        """
        if self._disposed or self._suspended is not None:
            return False
        if self._inRender:
            draw()
            return True
        window = self.renderWindow()
        if window is None:
            return False
        try:
            wasInitialized = bool(window.GetInitialized())
        except Exception:                                   # noqa: BLE001
            wasInitialized = True
        operation = self._renderOperation()
        previous = None
        if operation:
            previous = _currentOperation()
            heartbeat.set_last_op(operation)
            lifecycle.note_operation(operation)
        self._renderError = None
        self._inRender = True
        reason = None
        try:
            draw()
        except Exception as error:                          # noqa: BLE001
            logger.exception('The viewport failed to draw (%s)', what)
            reason = f'{type(error).__name__}: {error}'
        finally:
            self._inRender = False
        if reason is None:
            reason = self._renderError or gl_health.lost_context(
                window, wasInitialized)
        if operation:
            heartbeat.set_last_op(previous or '')
        if reason:
            self._contextLost(reason)
            return False
        if not self._glChecked:
            self._checkGl(window)
        self._scheduleInteractionDetail()
        return True

    def _renderOperation(self):
        """``render:<scene>`` when this draw is a new scene's first, or None."""
        try:
            stamp = self._renderer.GetViewProps().GetMTime()
        except Exception:                                   # noqa: BLE001
            stamp = None
        if stamp != self._sceneStamp:
            self._sceneStamp = stamp
            self._renderOpPending = True
            if self._depthPeeling:
                # A new scene may be far larger than the one peeling was
                # judged on (step 2's face budget).
                self._applyPeeling(True)
        if not self._renderOpPending:
            return None
        self._renderOpPending = False
        name = self._sceneName or self.objectName() or 'viewport'
        return f'{safe_mode.RENDER_OP_PREFIX}{name}'

    def _styleChanged(self):
        """The next frame is drawn differently: attribute it to drawing."""
        self._renderOpPending = True

    def setSceneName(self, name: str) -> None:
        """What ``render:<scene>`` calls the scene now shown (a stage, a file)."""
        name = str(name or '').strip()
        if name != self._sceneName:
            self._sceneName = name
            self._renderOpPending = True

    def sceneName(self) -> str:
        return self._sceneName

    def _startInteractor(self) -> None:
        """Initialise (make the GL context, first frame) and start QVTK."""
        if self._interactorStarted or self._disposed:
            return
        self._interactorStarted = True
        widget = self._widget

        def start():
            widget.Initialize()
            widget.Start()

        self._guardedRender(start, 'initialize')

    def _suspend(self, kind: str, title: str, detail: str, action: str) -> None:
        self._suspended = kind
        self._placeholder.showMessage(title, detail, action)
        if self._stack is not None:
            self._stack.setCurrentWidget(self._placeholder)

    def _contextLost(self, reason: str) -> None:
        """The view cannot draw: take it down and offer to make a new one."""
        if self._disposed or self._suspended == 'reset':
            return
        reason = str(reason or 'the render window stopped drawing')
        logger.error('The viewport stopped drawing: %s', reason)
        lifecycle.record(f'viewport reset: {reason}')
        self._suspended = 'reset'
        try:
            self._replaceInteractor()
        except Exception:                                   # noqa: BLE001
            logger.exception('Replacing the viewport after a reset failed')
        self._suspend(
            'reset', self.tr('The graphics driver reset.'),
            self.tr('The viewport stopped drawing ({0}). The model and the '
                    'rest of FoamMesh are unaffected.').format(reason),
            self.tr('Recreate viewport'))
        self.viewportReset.emit(reason)

    def _replaceInteractor(self) -> None:
        """Swap in a fresh QVTK widget and render window; keep the scene.

        The renderer -- actors, camera, lights, background -- moves to the
        new window. The old window is finalised while the renderers are
        still in it, so their GPU resources are released in the context
        they were made in (CR3's rule), then the old widget goes.
        """
        old = self._widget
        oldWindow = self.renderWindow()
        oldInteractor = getattr(old, '_Iren', None)
        _quietly(getattr(oldInteractor, 'SetEnableRender', None), False)
        old.renderGuard = None
        if oldWindow is not None and disposal.make_current(oldWindow):
            _quietly(getattr(oldWindow, 'Finalize', None))
        originAxesOn = False
        if self._originAxes is not None:
            originAxesOn = bool(_quietly(self._originAxes.GetEnabled))
            _quietly(self._originAxes.EnabledOff)
        logoOn = self._logoRepresentation is not None
        _quietly(self._logoWidget.Off)
        if oldWindow is not None:
            _quietly(getattr(oldWindow, 'RemoveRenderer', None), self._renderer)
            _quietly(getattr(oldWindow, 'RemoveAllObservers', None))

        widget = RenderWindowInteractor(self)
        window = widget.GetRenderWindow()
        window.SetMultiSamples(0 if self._safeMode else MULTI_SAMPLES)
        widget.SetInteractorStyle(self._style)
        widget.renderGuard = self._guardedRender
        _holdRenderRate(widget)
        window.AddRenderer(self._renderer)
        self._watchRenderWindow(window)
        self._widget = widget
        try:
            self._fxaa = render_style.applyTransparency(
                self._renderer, window, self._renderQuality,
                self._depthPeeling and not self._peelingDropped)
        except Exception:                                   # noqa: BLE001
            logger.exception('Re-applying the render style to the new window')
        if self._interactiveDecimation:
            self._interactiveDecimation = False
            self.setInteractiveDecimation(True)
        if logoOn:
            self._logoWidget.SetInteractor(widget)
            self._logoWidget.On()
        if self._originAxes is not None:
            self._originAxes.SetInteractor(widget)
            if originAxesOn:
                self._originAxes.EnabledOn()

        if self._stack is not None:
            self._stack.insertWidget(0, widget)
            self._stack.removeWidget(old)
        for name in ('_delayTimer', '_suppressTimer', '_Timer'):
            timer = old.__dict__.get(name) if hasattr(old, '__dict__') else None
            _quietly(getattr(timer, 'stop', None))
        _quietly(getattr(old, 'hide', None))
        _quietly(getattr(old, 'deleteLater', None))
        self._interactorStarted = False
        self._glChecked = False

    def recreateViewport(self) -> bool:
        """The placeholder's button: draw again. True if the view is back."""
        if self._disposed or self._suspended is None:
            return self._suspended is None and not self._disposed
        kind = self._suspended
        self._suspended = None
        self._renderError = None
        self._renderOpPending = True
        if self._stack is not None:
            self._stack.setCurrentWidget(self._widget)
        self._startInteractor()
        if self._suspended is not None:
            return False
        lifecycle.record('viewport shown in safe mode' if kind == 'safe'
                         else 'viewport recreated')
        self.refresh()
        self.viewportRestored.emit()
        return True

    def isSafeMode(self) -> bool:
        return self._safeMode

    def isViewportSuspended(self) -> bool:
        """True while the placeholder stands in for the view."""
        return self._suspended is not None

    def placeholder(self) -> ViewportPlaceholder:
        return self._placeholder

    def glInformation(self) -> dict:
        """The GL vendor, renderer and version once the view has drawn."""
        return dict(self._glInfo)

    def renderNoteText(self) -> str:
        return self._renderNoteText

    def _setRenderNote(self, text: str, part: str = 'scene') -> None:
        """Set one part of the note; the parts are joined, so the GPU note
        (which stays) and the scene's peeling note do not clear each other."""
        parts = self.__dict__.setdefault('_renderNoteParts', {})
        parts[part] = str(text or '')
        text = ' '.join(parts[key] for key in ('gl', 'gpu', 'scene')
                        if parts.get(key))
        if text != self._renderNoteText:
            self._renderNoteText = text
            if text:
                logger.info('Viewport: %s', text)
            self.renderNote.emit(text)

    def _checkGl(self, window) -> None:
        """Log the GL strings once there is a context; judge the driver."""
        info = gl_health.gl_strings(window)
        if not info:
            return
        self._glChecked = True
        # 2026-10-01. Which GPU drew it, and so how much to ask of it: the
        # display budgets (preview triangles, peeling, interaction detail)
        # follow the adapter actually in use, whatever its vendor.
        try:
            if 'gpu_memory_bytes' not in info and window.IsCurrent():
                memory = gpu_profile.gl_memory_bytes()
                if memory:
                    info['gpu_memory_bytes'] = memory
        except Exception:                                  # noqa: BLE001
            pass
        profile = gpu_profile.set_current(info)
        info['tier'] = profile.tier
        if profile.gpu_memory_bytes:
            info['gpu_memory_bytes'] = profile.gpu_memory_bytes
        self._glInfo = info
        gl_health.record(info)
        hint = gpu_profile.switch_hint(profile)
        if hint:
            logger.warning('Viewport GPU: %s', hint)
            self._setRenderNote(hint, part='gpu')
        reason = gl_health.weak_reason(info)
        if not reason:
            return
        logger.warning('Weak OpenGL: %s', reason)
        # Every start that finds it asks for the next one in safe mode, so a
        # machine without a proper driver stays there.
        safe_mode.remember_next_start(lifecycle.log_directory(), reason)
        if self._safeMode:
            return
        lifecycle.record(f'weak OpenGL, drawing simply: {reason}')
        self._safeMode = True
        requested = self._requestedQuality
        self._applyQuality(render_style.SAFE_PRESET)
        self._requestedQuality = requested
        self._setRenderNote(self.tr(
            '{0} The viewport now draws with the simplest settings, and '
            'FoamMesh will start in graphics safe mode next time.').format(reason),
            part='gl')

    def dispose(self):
        """Take this view apart on the GUI thread, ahead of `Finalize`.

        Plan 35 CR3 step 6. Releases the GPU resources, empties the renderer
        and breaks every Python reference cycle the view is part of -- the
        style's, the renderer's and the interactor's observers are bound
        methods of this object or of the interactor widget -- so that
        nothing here is left for the cyclic collector to destroy on another
        thread. Idempotent; the view does not draw again afterwards.
        """
        if self._disposed:
            return
        self._disposed = True
        disposal.untrack(self, 'RenderingWidget')
        from foammesh.app import app
        if app.themeManager is not None:
            try:
                app.themeManager.themeChanged.disconnect(self._themeChanged)
            except (RuntimeError, TypeError):
                pass
        self._hoverTimer.stop()
        self._interactionDetailTimer.stop()
        _quietly(self._interactionDetail.clear)
        widget = self._widget
        # Plan 35 CR8. The interactor widget's guard is a bound method of
        # this view: a cycle the collector would otherwise have to break.
        widget.renderGuard = None
        for name in ('_delayTimer', '_suppressTimer', '_Timer'):
            timer = widget.__dict__.get(name) if hasattr(widget, '__dict__') else None
            _quietly(getattr(timer, 'stop', None))
        disposal.disconnect_all(self._originalMouseObserver.mouseClicked)
        if self._mouseObserver is not self._originalMouseObserver:
            disposal.disconnect_all(getattr(self._mouseObserver, 'mouseClicked', None))
        self.releaseGraphicsResources()
        # Plan 35 CR8 (DP-986). The watermark and the origin-axes widgets
        # hold the interactor; left bound, their destructors ran against an
        # interactor Qt had already deleted, and dropping the last reference
        # to a disposed view corrupted the heap (0xC0000374).
        if getattr(self, '_originAxes', None) is not None:
            _quietly(self._originAxes.EnabledOff)
            _quietly(self._originAxes.SetInteractor, None)
        _quietly(self._logoWidget.Off)
        _quietly(self._logoWidget.SetInteractor, None)
        _quietly(getattr(self._renderer, 'RemoveAllViewProps', None))
        for owner in (self._style, self._renderer, getattr(widget, '_Iren', None),
                      self.renderWindow()):
            _quietly(getattr(owner, 'RemoveAllObservers', None))

    def close(self):
        self.viewClosed.emit()
        # F6. The GPU resources go before `Finalize` destroys the context
        # they live in -- see `dispose`.
        self.dispose()
        self._widget.close()

        return super().close()

    def pickActor(self, x, y):
        self._actorPicker.PickProp(x, y, self._renderer)
        actor = self._actorPicker.GetActor()

        return actor

    def clear(self):
        # vtkLogoWidget installs its representation as a renderer prop. Turn
        # it off before clearing so the following On() call actually
        # re-registers the watermark instead of returning as already enabled.
        self._logoWidget.Off()
        self._renderer.RemoveAllViewProps()
        self._showLogo()
        # DP-791. Only a project closing clears the view, and its views go
        # with it. DP-736's bounds test cannot tell two small models near
        # the origin apart, so the next project's Back walked into these.
        self.clearViewHistory()
        self._historyBounds = None

    def _turnCamera(self, orientation: tuple[float, float, float], up: tuple[float, float, float]):
        camera = self._renderer.GetActiveCamera()
        d = camera.GetDistance()
        fx, fy, fz = camera.GetFocalPoint()
        camera.SetPosition(fx-orientation[0]*d, fy-orientation[1]*d, fz-orientation[2]*d)
        camera.SetViewUp(up[0], up[1], up[2])

    def _getClosestAxis(self, u: tuple[float, float, float]) -> tuple[float, float, float]:
        axis = [0, 0, 0]
        i = u.index(max(u, key=abs))
        v = 1 if u[i] > 0 else -1
        axis[i] = v
        return axis

    def alignCamera(self):
        camera = self._renderer.GetActiveCamera()

        orientation = camera.GetDirectionOfProjection()
        orientation = self._getClosestAxis(orientation)

        up = camera.GetViewUp()
        up = self._getClosestAxis(up)

        self.rememberView()
        self._turnCamera(orientation, up)
        self._widget.Render()

    def rollCamera(self):
        self.rememberView()
        self._renderer.GetActiveCamera().Roll(-90)
        self._widget.Render()

    def setViewPreset(self, preset: str):
        """Set one of the six axis views or a stable isometric view."""
        orientation, up = _presetDirections(preset)
        self.rememberView()
        self._turnToPreset(orientation, up)

    def _turnToPreset(self, orientation, up):
        length = math.sqrt(sum(value * value for value in orientation))
        self._turnCamera(
            tuple(value / length for value in orientation), up)
        self.fitCamera()

    def saveScreenshot(self, path, *, scale: int = 2) -> bool:
        """Capture the rendered viewport as a PNG at 1x, 2x or 4x resolution.

        Plan 35 CR8: drawn through the render hold and the guarded render;
        False, and no file, while the view cannot draw.
        """
        if int(scale) not in {1, 2, 4}:
            raise ValueError('screenshot scale must be 1, 2 or 4')
        if isRenderingHold() or self._suspended is not None:
            return False
        window = self._widget.GetRenderWindow()
        capture = vtkWindowToImageFilter()

        def draw():
            window.Render()
            capture.SetInput(window)
            capture.SetScale(int(scale))
            capture.ReadFrontBufferOff()
            capture.Update()

        if not self._guardedRender(draw, 'screenshot'):
            return False
        writer = vtkPNGWriter()
        writer.SetFileName(str(path))
        writer.SetInputConnection(capture.GetOutputPort())
        writer.Write()
        return True

    def setOrderIndependentTransparency(self, enabled: bool):
        """Turn depth peeling on exactly while something translucent is drawn.

        The UI has offered per-actor opacity for as long as it has existed, and
        the renderer had no order-independent transparency behind it: two
        overlapping translucent patches rendered in whatever order their props
        happened to sit in. The control was lying about what it showed.

        Peeling and multisampling are mutually exclusive here, so this is a
        trade rather than a free win. Multisampling stays on for the opaque
        scene -- which is almost always -- and yields only while it must.
        """
        if self._applyPeeling(bool(enabled)):
            self.refresh()

    def _applyPeeling(self, enabled: bool) -> bool:
        """Set the translucency state; True if the drawing changed.

        Plan 35 CR8 step 2: above `gl_health.face_budget()` visible faces
        the scene is not depth-peeled (each peel redraws all of it), and
        the view says so through `renderNote`.
        """
        peel, note = gl_health.peeling_decision(
            enabled, gl_health.visible_faces(self._renderer), self._renderQuality)
        dropped = bool(note)
        if enabled == self._depthPeeling and dropped == self._peelingDropped:
            return False
        # render0925 DP-725. Dropping MSAA for peeling used to leave the
        # frame with no anti-aliasing at all -- and imported geometry is
        # drawn at 0.9 opacity, so that was every geometry view. FXAA,
        # which works alongside peeling, now takes over.
        self._fxaa = render_style.applyTransparency(
            self._renderer, self._widget.GetRenderWindow(),
            self._renderQuality, enabled and not dropped)

        self._depthPeeling = enabled
        self._peelingDropped = dropped
        self._setRenderNote(note)
        self._styleChanged()
        return True

    def usesOrderIndependentTransparency(self) -> bool:
        return self._depthPeeling

    def peelingDropped(self) -> bool:
        """True while translucency is wanted but not peeled (face budget)."""
        return self._peelingDropped

    # -- WP4.2/4.3: effects that cost frames, so nobody pays without asking -- #

    def setRenderQuality(self, name: str) -> bool:
        """render0925 DP-726. Performance, Balanced or Quality, in one step.

        Replaces the separate SSAO and FXAA check boxes (and the
        ``setAmbientOcclusion`` / ``setFastAntiAliasing`` setters behind
        them). Ticked over the 8x multisampling, FXAA drew a black frame on
        this backend, and SSAO quietly cancelled the multisampling; a preset
        cannot ask for either. Returns whether this VTK build could apply
        all of it.
        """
        chosen = render_style.quality(name)
        self._requestedQuality = chosen.name
        # Plan 35 CR8. Graphics safe mode keeps the simplest preset; the
        # choice is remembered for the next normal start.
        return _applyQuality(
            self, render_style.SAFE_PRESET if getattr(self, '_safeMode', False)
            else chosen.name)

    def _applyQuality(self, name: str) -> bool:
        return _applyQuality(self, name)

    def requestedRenderQuality(self) -> str:
        """The preset the user chose, which safe mode may be overriding."""
        return self._requestedQuality

    def renderQuality(self) -> str:
        return self._renderQuality

    def usesAmbientOcclusion(self) -> bool:
        return self._ambientOcclusion

    def usesFastAntiAliasing(self) -> bool:
        return self._fxaa

    def renderStyleReport(self) -> dict:
        """Plan 35 CR8. What this view draws with, read back from VTK."""
        reported = render_style.report(self._renderer, self.renderWindow())
        reported['preset'] = self._renderQuality
        reported['safe_mode'] = self._safeMode
        return reported

    # -- WP6: measure before optimising, and degrade out loud ---------------- #

    def measureFrameTime(self, samples: int = 5) -> float:
        """Mean seconds per frame over ``samples`` forced renders.

        WP6's rule is that no optimisation lands before this number exists, and
        WP4's is that no effect defaults on until its cost is recorded. Both
        need one honest measurement rather than an impression.
        """
        if isRenderingHold() or self._suspended is not None:
            return math.nan
        window = self._widget.GetRenderWindow()
        samples = max(1, samples)
        # render0925. Render() returns once the commands are queued, so
        # without waiting for the GPU this measured ~0.2 ms for any scene.
        wait = getattr(window, 'WaitForCompletion', None)
        measured = []

        def draw():
            window.Render()  # warm the pipeline; the first frame is not typical
            start = time.perf_counter()
            for _ in range(samples):
                window.Render()
                if wait is not None:
                    wait()
            measured.append((time.perf_counter() - start) / samples)

        if not self._guardedRender(draw, 'frame time') or not measured:
            return math.nan
        return measured[0]

    def setInteractiveDecimation(self, enabled: bool) -> None:
        """Drop detail *while the camera moves*, and say so when it happens.

        A silently decimated mesh that a user reads as the real one is the same
        class of defect as a verdict that grades an unmeasured mesh a pass, so
        this emits rather than acting invisibly.

        2026-10-01. On, every large part gets a reduced copy for drags,
        whatever the scene's size; off, only a scene above the GPU's budget
        does (``interaction_lod``). The copies are made in a worker thread.
        The render rate is no longer raised for a drag: that made
        ``vtkQuadricLODActor`` build its own copy of the volume on the GUI
        thread at the first moving frame -- seconds on a large mesh.
        """
        enabled = bool(enabled)
        if enabled == self._interactiveDecimation:
            return
        self._interactiveDecimation = enabled
        self._interactionDetail.forced = enabled
        # vtkRenderWindow has no still rate of its own (the interactor
        # holds it): calling one raised AttributeError, so this menu
        # entry failed every time it was ticked.
        window = self._widget.GetRenderWindow()
        interactor = self._widget._Iren
        if interactor is not None:
            interactor.SetDesiredUpdateRate(STILL_UPDATE_RATE)
            interactor.SetStillUpdateRate(STILL_UPDATE_RATE)
        window.SetDesiredUpdateRate(STILL_UPDATE_RATE)
        self.detailReduced.emit(enabled)
        self._scheduleInteractionDetail()

    # -- 2026-10-01: reduced copies while the camera moves ------------------ #

    def interactionDetail(self):
        """The reduced copies drawn while the camera moves (interaction_lod)."""
        return self._interactionDetail

    def _scheduleInteractionDetail(self) -> None:
        if self._disposed or self._interactionDetail.active():
            return
        timer = self.__dict__.get('_interactionDetailTimer')
        if timer is not None:
            timer.start()

    def _prepareInteractionDetail(self) -> bool:
        """Start making the copies the scene lacks; True if a job started."""
        if self._disposed or self._interactionDetailBuilding:
            return False
        if self._interactionDetail.active():
            self._scheduleInteractionDetail()
            return False
        try:
            jobs = self._interactionDetail.jobs()
        except Exception:                                   # noqa: BLE001
            logger.exception('Planning the interaction detail')
            return False
        if not jobs:
            return False
        self._interactionDetailBuilding = True
        made = self._interactionDetailMade

        def work():
            for job in jobs:
                try:
                    reduced = job.run()
                except Exception:                           # noqa: BLE001
                    logger.exception('Reducing a part for interaction')
                    continue
                made.emit(job, reduced)
            made.emit(None, None)

        threading.Thread(target=work, name='foammesh-interaction-detail',
                         daemon=True).start()
        return True

    def _installInteractionDetail(self, job, reduced) -> None:
        if job is None:
            self._interactionDetailBuilding = False
            return
        if self._disposed:
            return
        try:
            self._interactionDetail.install(job, reduced)
        except Exception:                                   # noqa: BLE001
            logger.exception('Adding a reduced copy to the scene')

    def _cameraMoving(self, obj, event):
        """The first moving frame of a drag: draw the reduced copies."""
        if self._interactionDetailTried:
            return
        self._interactionDetailTried = True
        try:
            swapped = self._interactionDetail.begin()
        except Exception:                                   # noqa: BLE001
            logger.exception('Swapping in the interaction detail')
            swapped = 0
        if swapped:
            self.detailReduced.emit(True)

    def _cameraInteractionEnded(self, obj, event):
        """Full detail back before the frame the style draws at release."""
        self._interactionDetailTried = False
        if self._interactionDetail.end():
            self.detailReduced.emit(False)
        self._scheduleInteractionDetail()

    def usesInteractiveDecimation(self) -> bool:
        return self._interactiveDecimation

    # -- camera history ---------------------------------------------------- #

    def _cameraState(self):
        camera = self._renderer.GetActiveCamera()
        return (tuple(camera.GetPosition()), tuple(camera.GetFocalPoint()),
                tuple(camera.GetViewUp()), camera.GetParallelScale())

    def _restoreCameraState(self, state):
        position, focal, up, scale = state
        camera = self._renderer.GetActiveCamera()
        camera.SetPosition(*position)
        camera.SetFocalPoint(*focal)
        camera.SetViewUp(*up)
        camera.SetParallelScale(scale)
        self._renderer.ResetCameraClippingRange()
        self._widget.Render()

    def rememberView(self):
        """Push the current camera onto the history before something moves it.

        Back/forward between recent views is the fastest recovery from an
        accidental drag, and the reason a cautious user stops being afraid to
        touch the picture at all.
        """
        self._pushView(self._cameraState())

    def _pushView(self, state):
        if self._viewHistory and self._viewHistory[-1] == state:
            return
        self._viewHistory.append(state)
        del self._viewHistory[:-VIEW_HISTORY_LIMIT]
        self._viewFuture.clear()
        self.historyChanged.emit()

    def fitCameraFromUser(self):
        """The Fit button: a fit the user asked for is a move Back can undo.

        DP-696. Fit was the one camera move Back could not undo, so a stray
        press lost a carefully set-up view. Only a fit that actually moved
        the camera is recorded, and only this entry point records it: the
        fits the program makes on load and resize are not the user's moves.
        """
        before = self._cameraState()
        self.fitCamera()
        if self._cameraState() != before:
            self._pushView(before)

    def frameScene(self, preset: Optional[str] = None):
        """The fit the program makes after the scene changed (a load).

        DP-736. Only the user's Fit was recorded (DP-696); a load's first
        frame went through the isometric preset, which pushed the camera it
        replaced, so after opening another model Back aimed at the old one,
        and every later refit of the same model was not recorded at all. Now
        a scene fit asks whether the model is the one the history was
        recorded on. Another model clears Back and Forward: those views look
        at something that is no longer there. The same model records the
        view the refit replaced, so Back undoes a refit the user did not ask
        for.
        """
        direction = None if preset is None else _presetDirections(preset)
        bounds = self._visibleBounds()
        record = (bounds is not None and self._historyBounds is not None
                  and _sameModel(self._historyBounds, bounds))
        if not record:
            self.clearViewHistory()
        before = self._cameraState()
        if direction is None:
            self.fitCamera()
        else:
            self._turnToPreset(*direction)
        if record and self._cameraState() != before:
            self._pushView(before)
        self._historyBounds = bounds

    def clearViewHistory(self):
        """Forget Back and Forward, e.g. when the model on screen changed."""
        if not self._viewHistory and not self._viewFuture:
            return
        self._viewHistory.clear()
        self._viewFuture.clear()
        self.historyChanged.emit()

    def _visibleBounds(self):
        compute = getattr(self._renderer, 'ComputeVisiblePropBounds', None)
        if compute is None:
            return None
        bounds = tuple(compute())
        if len(bounds) != 6 or bounds[0] > bounds[1]:
            return None
        return bounds

    def canGoBack(self) -> bool:
        return len(self._viewHistory) > 0

    def canGoForward(self) -> bool:
        return len(self._viewFuture) > 0

    def goBack(self):
        if not self._viewHistory:
            return False
        self._viewFuture.append(self._cameraState())
        self._restoreCameraState(self._viewHistory.pop())
        self.historyChanged.emit()
        return True

    def goForward(self):
        if not self._viewFuture:
            return False
        self._viewHistory.append(self._cameraState())
        self._restoreCameraState(self._viewFuture.pop())
        self.historyChanged.emit()
        return True

    def cameraState(self):
        """Serialisable camera description, for named views and captures."""
        camera = self._renderer.GetActiveCamera()
        return {
            'position': list(camera.GetPosition()),
            'focal_point': list(camera.GetFocalPoint()),
            'view_up': list(camera.GetViewUp()),
            'parallel_scale': camera.GetParallelScale(),
            'parallel_projection': bool(camera.GetParallelProjection()),
        }

    def restoreCameraState(self, state):
        camera = self._renderer.GetActiveCamera()
        camera.SetPosition(*state['position'])
        camera.SetFocalPoint(*state['focal_point'])
        camera.SetViewUp(*state['view_up'])
        camera.SetParallelScale(state.get('parallel_scale', 1.0))
        if state.get('parallel_projection'):
            camera.ParallelProjectionOn()
        else:
            camera.ParallelProjectionOff()
        self._renderer.ResetCameraClippingRange()
        self._widget.Render()

    def zoomToProps(self, props):
        """Fit the camera to a subset of the scene rather than all of it.

        DP-697. Hidden props are left out of the frame: a selection that
        still names a part the user hid framed empty space around it. Only
        when every prop is hidden does the fit fall back to all of them, so
        the button never silently does nothing.
        """
        props = list(props)
        shown = [prop for prop in props
                 if not hasattr(prop, 'GetVisibility') or prop.GetVisibility()]
        bounds = None
        for prop in shown or props:
            propBounds = prop.GetBounds()
            if propBounds is None:
                continue
            if bounds is None:
                bounds = list(propBounds)
            else:
                for index in (0, 2, 4):
                    bounds[index] = min(bounds[index], propBounds[index])
                for index in (1, 3, 5):
                    bounds[index] = max(bounds[index], propBounds[index])
        if bounds is None:
            return False
        self.rememberView()
        self._renderer.ResetCamera(bounds)
        self._widget.Render()
        return True

    def modelExtent(self) -> float:
        """The model's largest dimension, for the scale readout.

        Unit mistakes are the most common silent error in a meshing workflow
        and cost a whole run to discover. A persistent number is cheap.
        """
        bounds = self.getBounds()
        if bounds is None:
            return 0.0
        spans = [bounds[1] - bounds[0], bounds[3] - bounds[2],
                 bounds[5] - bounds[4]]
        largest = max(spans)
        return largest if largest > 0 else 0.0

    def setParallelProjection(self, checked):
        if checked:
            self._renderer.GetActiveCamera().ParallelProjectionOn()
        else:
            self._renderer.GetActiveCamera().ParallelProjectionOff()
        self._widget.Render()

    def setAxisVisible(self, checked):
        if checked:
            self._showOriginAxes()
        else:
            self._hideOriginAxes()

        # self._resizeOriginAxis()
        self._widget.Render()

    def setCubeAxisVisible(self, checked):
        if checked:
            self._showCubeAxes()
        else:
            self._hideCubeAxes()
        self._widget.Render()

    def getBounds(self):
        return self._renderer.ComputeVisiblePropBounds()

    def setBackground1(self, r, g, b):
        self._userBackground['bottom'] = (r, g, b)
        self._renderer.SetBackground(r, g, b)

    def setBackground2(self, r, g, b):
        self._userBackground['top'] = (r, g, b)
        self._renderer.SetBackground2(r, g, b)

    def _keepUserBackground(self):
        """Put back the gradient ends the user picked after a theme pass.

        DP-701 (viewport audit 0925 F15). ``apply_vtk_theme`` paints the
        background from the theme, and it runs again whenever the cube axes
        or the origin axes are first shown, so a colour picked from the
        swatches lasted only until the next of those.
        """
        bottom = self._userBackground.get('bottom')
        if bottom is not None:
            self._renderer.SetBackground(*bottom)
        top = self._userBackground.get('top')
        if top is not None:
            self._renderer.SetBackground2(*top)

    def hasUserBackground(self) -> bool:
        """Whether either gradient end is one the user picked."""
        return bool(self._userBackground)

    def resetBackground(self):
        """Forget the picked gradient ends and paint the theme's again.

        DP-739. DP-701 kept a pick across every theme pass, which left no way
        back to the theme's gradient short of picking its two colours by
        hand, and a theme switch went on painting the old pick over the new
        theme.
        """
        self._userBackground.clear()
        if self._themeTokens is not None:
            apply_vtk_theme(self._renderer, self._themeTokens,
                            cube_axes=self._cubeAxesActor,
                            origin_axes=self._originAxesActor,
                            logo=self._logoRepresentation)
        self.refresh()

    def applyTheme(self, tokens):
        """Apply semantic viewport tokens supplied by the runtime theme manager."""
        self._themeTokens = tokens
        self._loadLogo(tokens.name)
        apply_vtk_theme(self._renderer, tokens, cube_axes=self._cubeAxesActor,
                        origin_axes=self._originAxesActor, logo=self._logoRepresentation)
        self._keepUserBackground()
        self.refresh()

    def _themeChanged(self, _name):
        from foammesh.app import app
        if app.themeManager is not None and app.themeManager.tokens is not None:
            self.applyTheme(app.themeManager.tokens)

    def background1(self):
        return self._renderer.GetBackground()

    def background2(self):
        return self._renderer.GetBackground2()

    def setMouseHandler(self, observer):
        self._mouseObserver = observer

    def resetMouseHandler(self):
        self._mouseObserver = self._originalMouseObserver

    def _showCubeAxes(self):
        if self._cubeAxesActor is not None:
            return

        self._cubeAxesActor = vtkCubeAxesActor()
        self._cubeAxesActor.SetBounds(self.getBounds())
        self._cubeAxesActor.SetCamera(self._renderer.GetActiveCamera())

        self._cubeAxesActor.DrawXGridlinesOn()
        self._cubeAxesActor.DrawYGridlinesOn()
        self._cubeAxesActor.DrawZGridlinesOn()
        self._cubeAxesActor.SetGridLineLocation(self._cubeAxesActor.VTK_GRID_LINES_FURTHEST)

        self._cubeAxesActor.XAxisMinorTickVisibilityOff()
        self._cubeAxesActor.YAxisMinorTickVisibilityOff()
        self._cubeAxesActor.ZAxisMinorTickVisibilityOff()

        self._cubeAxesActor.SetFlyModeToOuterEdges()
        self._applyCubeAxesPolicy()

        self._renderer.AddActor(self._cubeAxesActor)
        # DP-704. Whether an axis's labels fit depends on how long it is on
        # screen, which every camera move changes; re-measure every render.
        addObserver = getattr(self._renderer, 'AddObserver', None)
        if addObserver is not None:
            self._cubeAxesObserver = addObserver(
                'StartEvent', lambda *_args: self._fitCubeAxesLabels())
        if self._themeTokens is not None:
            apply_vtk_theme(self._renderer, self._themeTokens, cube_axes=self._cubeAxesActor,
                            origin_axes=self._originAxesActor, logo=self._logoRepresentation)
            self._keepUserBackground()

    def _applyCubeAxesPolicy(self):
        """Size the axis labels to the viewport they have to fit in (F-44).

        The labels were drawn at a fixed twelve points with the default
        ``%-#6.3g`` format, whatever the pane was doing. `vtkCubeAxesActor`
        chooses its ticks from the coordinate *range* and knows nothing about
        how many pixels an axis occupies, so a viewport docked down to a few
        hundred pixels drew the same six labels per axis in the same size and
        they ran into each other.

        There is no label-count setter on the actor, so the policy works with
        what it does expose: the label size, the number of significant digits
        and, below the point where even two labels can be told apart, the
        labels themselves. `cubeAxesPolicy` is a pure function of the pixel
        extent so it can be tested without a render window.
        """
        if self._cubeAxesActor is None:
            return
        policy = cubeAxesPolicy(self.width(), self.height())
        actor = self._cubeAxesActor
        actor.SetScreenSize(policy['screen_size'])
        for axis in ('X', 'Y', 'Z'):
            getattr(actor, f'Set{axis}LabelFormat')(policy['label_format'])
            getattr(actor, f'Set{axis}AxisLabelVisibility')(
                1 if policy['labels'] else 0)
        # Gridlines behind labels that are already tight is the other half of
        # the crowding, so they follow the same budget.
        for axis in ('X', 'Y', 'Z'):
            getattr(actor, f'Draw{axis}Gridlines'
                    f'{"On" if policy["gridlines"] else "Off"}')()

    def _fitCubeAxesLabels(self):
        """DP-704/DP-1180/DP-1181. Refit the box and label each axis."""
        if self._cubeAxesActor is None:
            return
        try:
            # DP-1181. Parts hidden, shown, added or removed since the box
            # was built change what it should measure.
            refreshCubeAxesBounds(self._cubeAxesActor, self._renderer)
            fitCubeAxesLabels(self._cubeAxesActor, self._renderer,
                              cubeAxesPolicy(self.width(), self.height()))
        except (AttributeError, TypeError):
            # A renderer or actor without a camera projection has nothing
            # to measure; the viewport-wide policy stands.
            pass

    def _hideCubeAxes(self):
        observer = getattr(self, '_cubeAxesObserver', None)
        if observer is not None:
            self._renderer.RemoveObserver(observer)
            self._cubeAxesObserver = None
        if self._cubeAxesActor is not None:
            self._renderer.RemoveActor(self._cubeAxesActor)
            self._cubeAxesActor = None

    def _showOriginAxes(self):
        if self._originAxes is None:
            self._originAxes = vtkOrientationMarkerWidget()
            self._originAxes.SetViewport(0.0, 0.0, 0.2, 0.2)
            self._originAxesActor = vtkAxesActor()
            self._originAxes.SetOrientationMarker(self._originAxesActor)
            self._originAxes.SetInteractor(self._widget)
            if self._themeTokens is not None:
                apply_vtk_theme(self._renderer, self._themeTokens,
                                cube_axes=self._cubeAxesActor,
                                origin_axes=self._originAxesActor,
                                logo=self._logoRepresentation)
                self._keepUserBackground()

        self._originAxes.EnabledOn()

    def _hideOriginAxes(self):
        if self._originAxes is not None:
            self._originAxes.EnabledOff()

    # def _resizeOriginAxis(self):
    #     if self._originActor:
    #         camera = self._renderer.GetActiveCamera()

    #         d = camera.GetDirectionOfProjection()
    #         p = camera.GetPosition()
    #         distance = abs(-p[0]*d[0]-p[1]*d[1]-p[2]*d[2])

    #         degree = camera.GetViewAngle()
    #         radian = math.radians(degree/3.0)
    #         length = distance * math.tan(radian)

    #         # length = self._style.getOriginActorLength()
    #         self._originActor.SetTotalLength(length, length, length)

    def _leftButtonPressEvent(self, obj, event):
        self._mouseObserver.leftButtonPressed(obj, event)

    def _leftButtonReleaseEvent(self, obj, event):
        self._mouseObserver.leftButtonReleased(obj, event)

    def _mouseMoveEvent(self, obj, event):
        self._mouseObserver.mouseMoved(obj, event)
        self._scheduleHoverPick()

    def _scheduleHoverPick(self):
        """Name the part under the cursor after a short dwell.

        Throttled rather than picked on every move: a prop pick is not free,
        and a name that flickers as the cursor crosses the mesh is worse than
        no name at all.
        """
        if not self._hoverEnabled:
            return
        self._hoverTimer.start()

    def setHoverPickEnabled(self, enabled: bool):
        self._hoverEnabled = bool(enabled)
        if not self._hoverEnabled:
            self._hoverTimer.stop()
            if self._hoveredName:
                self._hoveredName = ''
                self.actorHovered.emit('')

    def _hoverPick(self):
        interactor = self._widget._Iren
        x, y = interactor.GetEventPosition()
        actor = self.pickActor(x, y)
        name = actor.GetObjectName() if actor is not None else ''
        if name != self._hoveredName:
            self._hoveredName = name
            self.actorHovered.emit(name)

    def _cameraInteractionStarted(self, obj, event):
        # The first drag after a programmatic framing is exactly the view a
        # user wants "back" to return to; later drags would only flood history.
        if not self._cameraMovedByUser:
            self.rememberView()
        self._cameraMovedByUser = True

    def _mouseClicked(self, x, y, controlKeyPressed, shiftKeyPressed=False):
        self.actorPicked.emit(self.pickActor(x, y), controlKeyPressed,
                              shiftKeyPressed)

    # def _mouseWheelForwardEvent(self, obj, event):
    #     # The style does not run its own handler if observer is registered
    #     self._style.OnMouseWheelForward()

    #     self._resizeOriginAxis()
    #     self._widget.Render()

    # def _mouseWheelBackwardEvent(self, obj, event):
    #     # The style does not run its own handler if observer is registered
    #     self._style.OnMouseWheelBackward()

    #     self._resizeOriginAxis()
    #     self._widget.Render()

    # # This is a true observer calling.
    # # No need to call style's method
    # def _interactionEvent(self, obj, event):
    #     self._resizeOriginAxis()
    #     self._widget.Render()

    def _showLogo(self):
        if self._logoRepresentation is None:
            self._logoRepresentation = vtkLogoRepresentation()
            # The cached theme belongs to the representation that owns the
            # image. A newly-created representation must always load it.
            self._logoTheme = None
            self._logoWidget.SetInteractor(self._widget)
            # An empty scene gives vtkLogoWidget no picked prop from which to
            # infer a renderer, so bind the viewport explicitly.
            self._logoWidget.SetCurrentRenderer(self._renderer)
            self._logoWidget.SetRepresentation(self._logoRepresentation)
            self._logoWidget.ProcessEventsOff()
        theme = self._themeTokens.name if self._themeTokens is not None else 'light'
        self._loadLogo(theme)
        self._updateLogoLayout()
        self._logoRepresentation.GetImageProperty().SetOpacity(0.34)
        self._logoWidget.On()
        self.refresh()

    def _loadLogo(self, theme):
        if self._logoRepresentation is None or self._logoTheme == theme:
            return
        self._logoReader = vtkPNGReader()
        self._logoReader.SetFileName(str(meshAppProperties.watermark(theme)))
        self._logoReader.Update()
        self._logoRepresentation.SetImage(self._logoReader.GetOutput())
        self._logoTheme = theme

    def _updateLogoLayout(self):
        if self._logoRepresentation is None:
            return
        dpr = self.devicePixelRatioF()
        position, size = watermark_geometry(
            round(self.width() * dpr), round(self.height() * dpr), dpr)
        self._logoRepresentation.SetPosition(*position)
        self._logoRepresentation.SetPosition2(*size)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._updateLogoLayout()
        self._logoWidget.On()
        # F-44. The label budget is a function of the pixel extent, so it is
        # re-spent whenever the extent changes -- docking the pane is the
        # commonest way to arrive at a viewport too narrow for six numbers.
        self._applyCubeAxesPolicy()

        if self._refitOnResize and not self._cameraMovedByUser:
            # Re-fit only while the framing is still the one this widget chose.
            self._refitOnResize = False
            self.fitCamera()
            return

        self.refresh()
