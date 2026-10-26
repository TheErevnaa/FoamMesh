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
from PySide6.QtCore import QRect, QTimer, Qt, Signal, QObject
from PySide6.QtGui import (
    QColor, QLinearGradient, QMouseEvent, QPainter)
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
from vtkmodules.vtkRenderingAnnotation import vtkAnnotatedCubeActor, vtkAxesActor, vtkCubeAxesActor
from vtkmodules.vtkRenderingCore import (
    vtkActor, vtkCellPicker, vtkLightKit, vtkProp, vtkPropAssembly, vtkPropPicker,
    vtkRenderer)

from foammesh.support import disposal, heartbeat, lifecycle, safe_mode
from foammesh.support.vtk_threads import isRenderingHold
from foammesh.rendering.pick_alone import pick_prop

from foammesh.rendering import hover_readout, label_pass, label_style, measure
from foammesh.rendering import (
    gl_health, gpu_profile, interaction_lod, render_style)
from foammesh.view.theming.metrics import apply_prose_measure
from foammesh.view.theming.vtk_theme import (
    WATERMARK_OPACITY_DARK, apply_triad_theme, apply_vtk_theme,
    watermark_opacity)
from foammesh.core.quantities import format_group
from app_properties import meshAppProperties
from foammesh.core.branding import watermark_geometry
from foammesh.view.display_control.overlay_layout import OverlayLayout, Slot, apply_to_scalar_bar


logger = logging.getLogger(__name__)

#: Plan 45 shut-down. Set by the application once its event loop has
#: returned for good; render windows finalized after it leave their GL
#: context to the process exit (they are kept here, referenced).
_processExiting = False
_contextsLeftToTheProcessExit = []


def leaveContextsToProcessExit() -> None:
    """The process is ending: do not delete GL contexts one by one.

    MEASURED (Plan 45, 2026-10-17): after ``main()`` returned, the viewport's
    ``Finalize`` -- run when the QApplication's widgets are taken down --
    sat 1-2 s in the GPU driver's ``wglDeleteContext`` (atio6axx) on about
    one exit in four; a standalone VTK window shows the same. Left to the
    process exit, 35 of 35 standalone exits ended in 0.19-0.41 s.
    """
    global _processExiting
    _processExiting = True

RENDER_DELAY_TIME = 200
REPAINT_SUPPRESS_TIME = 100
#: UXB5 (N4). A paint that arrives while a hold is on is dropped, and the
#: native surface behind it is then whatever the window system left: black.
#: If the hold is still on this long after such a paint, the view is covered
#: with its own background and a line saying so, until the first real frame.
HOLD_COVER_DELAY = 120
#: DP-2705. How often a raised cover asks whether the hold is over (ms).
HOLD_COVER_WATCH = 50
#: DP-2705. A second real frame this long after the cover comes down (ms),
#: in case the window system's own repaint of the uncovered area lands after
#: the first one.
HOLD_COVER_SETTLE = 150

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


#: Plan 44 MF15 (F25, VV13). The orientation triad is a fixed 52 logical px
#: square, 20 px right of the clear area's left edge and 22 px above its
#: bottom (the console's top): the mockup's size, not a share of the window.
#: It was a 0.2 share between 96 and 160 px in Plan 39 VI15.
CORNER_SIZE = 52
CORNER_LEFT = 20
CORNER_BOTTOM = 22
#: DP-2651. 2.4 drew 54 saturated pixels in the live render window (60 wanted;
#: the offscreen test read 71); 3.4 draws about 76 live.
TRIAD_AXIS_WIDTH = 3.4
#: MF27. The corner axes run from the cube's centre to its faces.
CORNER_AXES_LENGTH = (0.5, 0.5, 0.5)
#: DP-2651. The axes run inside the cube, so an opaque face hid all but a
#: stub of them (36 saturated pixels in the live read, the mockup draws three
#: 16 px arms on top of its hexagon). The faces are translucent so the axes
#: show through, as the mockup's lines do.
TRIAD_FACE_OPACITY = 0.3
#: Plan 45 B (P10). The world-origin triad's arms are this share of the
#: model's SMALLEST non-zero bounding-box side.
ORIGIN_ARM_SHARE = 0.4
ORIGIN_ARM_FALLBACK = 1.0


def origin_arm_length(bounds) -> float:
    """Arm of the world-origin triad: 0.4 x the smallest non-zero box side.

    ``bounds`` is (xmin, xmax, ymin, ymax, zmin, zmax) of the model, or None /
    inverted for an empty scene (then the fixed fallback).
    """
    if bounds is None or len(bounds) < 6:
        return ORIGIN_ARM_FALLBACK
    sides = [float(bounds[i + 1]) - float(bounds[i]) for i in (0, 2, 4)]
    sides = [side for side in sides if side > 0.0]
    if not sides:
        return ORIGIN_ARM_FALLBACK
    return ORIGIN_ARM_SHARE * min(sides)


#: DP-2721. The origin triad's X, Y, Z: a fixed 14 px in the axis colours.
#: VTK's default caption is white and scales with the window (60 px letters
#: at 1920 x 960, unreadable on the light ground).
ORIGIN_LABEL_PX = 14


def facePresetAt(position) -> str | None:
    """The view preset for a point on the orientation cube's surface.

    The cube is centred on the corner's origin, so the face a point is on is
    the axis it is furthest along, and clicking a face looks at that face.
    """
    values = [float(value) for value in position]
    if len(values) != 3 or max(abs(value) for value in values) == 0.0:
        return None
    axis = max(range(3), key=lambda index: abs(values[index]))
    return ('+' if values[axis] > 0 else '-') + 'xyz'[axis]


def cornerSize(width: int, height: int, device_pixel_ratio: float = 1.0) -> int:
    """Device pixels on a side of the orientation triad for a view this size.

    ``width`` and ``height`` are the view's own (logical) size; the result is
    52 logical pixels times the device-pixel ratio, never more than the view.
    """
    if width <= 0 or height <= 0:
        return 0
    dpr = max(float(device_pixel_ratio), 1.0)
    return int(min(round(CORNER_SIZE * dpr), width * dpr, height * dpr))


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


def styleOriginLabels(actor, tokens) -> None:
    """DP-2721. The origin triad's captions at ``ORIGIN_LABEL_PX``, unscaled,
    plain, each in its axis's colour once a theme is known."""
    captions = (actor.GetXAxisCaptionActor2D(), actor.GetYAxisCaptionActor2D(),
                actor.GetZAxisCaptionActor2D())
    colours = None
    if tokens is not None:
        from foammesh.view.theming.vtk_theme import rgb
        colours = [rgb(tokens.value(name)) for name in
                   ('status.error', 'status.success', 'status.info')]
    for index, caption in enumerate(captions):
        caption.GetTextActor().SetTextScaleModeToNone()
        caption.BorderOff()
        text = caption.GetCaptionTextProperty()
        text.SetFontSize(ORIGIN_LABEL_PX)
        text.BoldOn()
        text.ItalicOff()
        text.ShadowOff()
        if colours is not None:
            text.SetColor(*colours[index])


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


def blockVtkCharacterKeys(style):
    """DP-1469 (VP-181). Stop the trackball style answering w, s, e, q, f, r
    and 3 itself; the app's own keys are Qt shortcuts and stay."""
    def _swallow(caller, event):
        # MF27: nothing to do. A vtkInteractorStyle with a CharEvent observer
        # (and HandleObservers on) calls the observers *instead of* OnChar,
        # so the observer's presence is the block. The old body called
        # caller.SetAbortFlag(1), which the style does not have: every
        # blocked key raised into the app's excepthook.
        return None
    # An observer on the style: vtkInteractorStyle::ProcessEvents runs it in
    # place of the style's own OnChar.
    style.AddObserver(vtkCommand.CharEvent, _swallow, 10.0)
    return style


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
        #: UXB5. The owner's hooks: a paint was dropped because a hold is on,
        #: and a real frame was drawn.
        self.onHeldPaint = None
        self.onDrawn = None

        super().__init__(parent=parent, **kw)

    def Finalize(self):
        window = self._RenderWindow
        if window is None:
            return
        self._RenderWindow = None
        if _processExiting:
            # Plan 45 shut-down (see leaveContextsToProcessExit): the context
            # goes with the process. The extra reference keeps a late
            # destructor from deleting it either.
            window.Register(None)
            _contextsLeftToTheProcessExit.append(window)
            return
        window.Finalize()

    def paintEvent(self, ev):
        if isRenderingHold():
            self._delayTimer.start()
            held = self.onHeldPaint
            if held is not None:
                held()
            return

        # Plan 35 CR8. A paint is where QVTK really draws (its Render() only
        # schedules one), so it goes through the owner's guarded render.
        guard = self.renderGuard
        if guard is not None:
            guard(lambda: self._paint(ev), 'paint')
        else:
            self._paint(ev)
        drawn = self.onDrawn
        if drawn is not None:
            drawn()

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


def isClick(pressed, released, slop: int = 3) -> bool:
    """Whether a press and a release ``slop`` pixels apart or closer are a
    click rather than the end of a drag (DP-2747)."""
    if pressed is None or released is None:
        return False
    return (abs(float(released[0]) - float(pressed[0])) <= slop
            and abs(float(released[1]) - float(pressed[1])) <= slop)


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

    #: DP-2747. How far, in render-window pixels, the pointer may move
    #: between press and release for the pair still to be a click. Exact
    #: equality lost every click whose hand moved one pixel -- on a
    #: high-resolution mouse or a touchpad, most of them -- and a lost click
    #: while measuring is a point that silently never arrives.
    CLICK_SLOP = 3

    def _leftButtonReleased(self, x, y):
        if isClick(self._pressPos, (x, y), self.CLICK_SLOP):
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


class HoldCover(QWidget):
    """UXB5 (N4). What the viewport shows while a hold dropped its paints.

    The last frame the view drew, with a small "updating" pill over it, so a
    tool's job (a section cut, a reload) does not blank the model and bring it
    back (DP-2880). Without a frame to show (the view never drew, or reading
    it back failed) it falls back to the view's own background, top to bottom,
    with the line of text in the middle.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('viewportHoldCover')
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._top = QColor(56, 61, 84)
        self._bottom = QColor(209, 209, 209)
        self._radial = None
        self._text = ''
        #: DP-2880. The view's last frame, or None.
        self._frame = None
        self.hide()

    def setFrame(self, image):
        """Show *image* (a ``QImage`` of the view's last frame), or None."""
        self._frame = image if image is not None and not image.isNull() else None
        self.update()

    def frame(self):
        return self._frame

    def setColors(self, top, bottom):
        self._top, self._bottom = QColor(top), QColor(bottom)
        self._radial = None
        self.update()

    def setRadial(self, centre, mid, edge):
        """MF15. Paint the same off-centre radial the VTK background shows."""
        self._radial = (QColor(centre), QColor(mid), QColor(edge))
        self._top, self._bottom = QColor(edge), QColor(mid)
        self.update()

    def radial(self):
        return self._radial

    def _radialImage(self):
        import numpy as np
        from PySide6.QtGui import QImage
        from foammesh.view.theming.vtk_theme import radial_background_array
        key = tuple(color.name() for color in self._radial)
        if getattr(self, '_radialKey', None) != key:
            pixels = np.ascontiguousarray(radial_background_array(*key)[::-1])
            size = pixels.shape[0]
            self._radialPixels = QImage(
                pixels.data, size, size, size * 3,
                QImage.Format.Format_RGB888).copy()
            self._radialKey = key
        return self._radialPixels

    def setText(self, text):
        self._text = str(text or '')
        self.update()

    def text(self) -> str:
        return self._text

    def colors(self):
        return self._top, self._bottom

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._frame is not None:
            # DP-2880. The picture stays; only a pill says it is updating.
            painter.drawImage(self.rect(), self._frame)
            if self._text:
                self._paintPill(painter)
            painter.end()
            return
        if self._radial is not None:
            # The very pixels the VTK background shows: the same texture.
            painter.drawImage(self.rect(), self._radialImage())
        else:
            gradient = QLinearGradient(0, 0, 0, self.height())
            gradient.setColorAt(0.0, self._top)
            gradient.setColorAt(1.0, self._bottom)
            painter.fillRect(self.rect(), gradient)
        if self._text:
            middle = QColor(self._top)
            middle = QColor(
                (middle.red() + self._bottom.red()) // 2,
                (middle.green() + self._bottom.green()) // 2,
                (middle.blue() + self._bottom.blue()) // 2)
            # Whichever of black and white reads against the middle of the
            # gradient, so the line is legible in both themes.
            luminance = (0.299 * middle.red() + 0.587 * middle.green()
                         + 0.114 * middle.blue())
            painter.setPen(QColor(0, 0, 0, 170) if luminance > 140
                           else QColor(255, 255, 255, 200))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self._text)
        painter.end()

    def _paintPill(self, painter):
        """The text in a small rounded pill at the top centre of the view."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(self._text) + 24
        height = metrics.height() + 10
        pill = QRect((self.width() - width) // 2, 12, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 30, 170))
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setPen(QColor(255, 255, 255, 230))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self._text)


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


#: Plan 45 BOOT2. While the main window is built in stages, a call that serves
#: the event queue; the viewport calls it between its own stages (it was
#: ~0.45 s in one piece, MEASURED live 17 October 2026). None otherwise.
constructionTurn = None


def _constructionTurn() -> None:
    turn = constructionTurn
    if turn is not None:
        turn()


class RenderingWidget(QWidget):
    actorPicked = Signal(vtkActor, bool, bool)
    #: Plan 43 LY8. The labels font/size or the legends switch changed, from
    #: whichever control (Snapshot popover, Preferences, View menu).  The View
    #: menu state follows these.
    labelStyleChanged = Signal(object)
    legendsShownChanged = Signal(bool)
    #: Plan 44 F1 (MF15, Q9). The watermark was shown (True) or hidden
    #: (False), by View > Overlays and rendering > Watermark or by code.
    watermarkShownChanged = Signal(bool)
    viewClosed = Signal()
    #: The name of the part under the cursor, or '' when the cursor is over
    #: nothing. The picture is expected to answer "what is this" on its own.
    actorHovered = Signal(str)
    #: Plan 39 VI15 (I2). What is under the cursor, as text, and where (view
    #: pixels, origin top-left); '' takes the readout down.
    hoverReadout = Signal(str, int, int)
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

        #: Plan 39 VI15 (I10). The viewport triad in the corner: always on,
        #: an axes triad inside an annotated cube whose faces are the view
        #: presets. (Named `_originAxes` before; the world-origin triad is
        #: `_originTriad` now.)
        self._originAxes: Optional[vtkOrientationMarkerWidget] = None
        self._originAxesActor: Optional[vtkAxesActor] = None
        self._cornerCube = None
        self._cornerPixels = 0
        self._cornerObserver = None
        #: The world-origin triad (the toolbar's Origin axes): an axes actor at
        #: (0, 0, 0) on a layer of its own, drawn over the model.
        self._originTriad = None
        self._originRenderer = None
        self._originTriadOn = False
        #: One layout for everything drawn over the picture (I4).
        self._overlayLayout = OverlayLayout()
        self._measureState = measure.MeasureState()
        self._measureActors = []
        self._legends = {}
        #: Plan 44 FX3 (L2). Legends drawn only into a saved picture: the
        #: screen has the one themed Qt legend (Plan 43 LY11), so a scalar
        #: bar kept for the VTK-only snapshot must never show on screen.
        self._pictureOnlyLegends = set()
        # Plan 43 LY15. One font and size for every label; the groups
        # ('worst', 'caption') the Include boxes switch; the sizes the labels
        # were built with, so that Large is a multiple of them and Normal
        # puts them back.
        self._labelStyle = label_style.load()
        self._labelGroups = {}
        self._labelGroupShown = {'worst': True, 'caption': True}
        self._labelBase = {}
        self._cubeAxesActor: Optional[vtkCubeAxesActor] = None
        self._themeTokens = None
        # DP-701. The gradient ends the user picked, which a theme pass must
        # not paint over; see `_keepUserBackground`.
        self._userBackground = {}

        self._actorPicker = vtkPropPicker()

        self._style = blockVtkCharacterKeys(vtkInteractorStyleTrackballCamera())
        self._widget = RenderWindowInteractor(self)
        self._widget.GetRenderWindow().SetMultiSamples(
            0 if self._safeMode else MULTI_SAMPLES)
        self._widget.SetInteractorStyle(self._style)
        self._widget.renderGuard = self._guardedRender
        self._watchRenderWindow(self._widget.GetRenderWindow())
        _holdRenderRate(self._widget)
        self._holdCover = None
        self._holdCoverTimer = QTimer(self)
        self._holdCoverTimer.setSingleShot(True)
        self._holdCoverTimer.setInterval(HOLD_COVER_DELAY)
        self._holdCoverTimer.timeout.connect(self._showHoldCover)
        # DP-2705. The cover is a native window over the native view, so
        # once it is up Windows sends the view no paint and the "first real
        # frame" that was to take it down never comes. This watch takes it
        # down when the hold ends.
        self._holdCoverWatch = QTimer(self)
        self._holdCoverWatch.setInterval(HOLD_COVER_WATCH)
        self._holdCoverWatch.timeout.connect(self._watchHoldCover)
        #: DP-2705. A paint was dropped under a hold (or a frame was drawn
        #: while the cover was up, clipped by it): a real frame is owed.
        self._frameOwed = False
        self._presentNowTimer = QTimer(self)
        self._presentNowTimer.setSingleShot(True)
        self._presentNowTimer.setInterval(0)
        self._presentNowTimer.timeout.connect(self._presentFrame)
        self._presentSettleTimer = QTimer(self)
        self._presentSettleTimer.setSingleShot(True)
        self._presentSettleTimer.setInterval(HOLD_COVER_SETTLE)
        self._presentSettleTimer.timeout.connect(self._presentFrame)
        self._widget.onHeldPaint = self._heldPaint
        self._widget.onDrawn = self._frameDrawn
        _constructionTurn()

        self._depthPeeling = False
        #: Plan 35 CR8 step 2. Translucency wanted, peeling dropped (budget).
        self._peelingDropped = False
        self._viewHistory = []
        self._viewFuture = []
        #: DP-736. The visible bounds the camera history was recorded on.
        self._historyBounds = None
        self._cameraMovedByUser = False
        self._modelFitted = False
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
        #: I2. On by default; View > Hover readout turns it off.
        self._readoutEnabled = True
        self._hoverInfo = None
        self._hoverVolume = None
        self._readoutText = ''
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
        #: Plan 39 VI3. A camera drag is between its start and end events.
        self._interactionInFlight = False
        #: perf_counter at the start of the frame being drawn.
        self._frameStart = None
        #: Closing round 2026-10-12 (VI14). GPU timer queries of frames not
        #: yet read, and the finished timings not yet handed to the detail.
        self._gpuTimers = []
        self._gpuSeconds = []
        self._gpuTimerSupported = None
        self._gpuTimerPoll = QTimer(self)
        self._gpuTimerPoll.setSingleShot(True)
        self._gpuTimerPoll.setInterval(20)
        self._gpuTimerPoll.timeout.connect(self._pollGpuTimers)
        #: Hover picks not made because a frame was in flight.
        self.hoverPicksSkipped = 0
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
        self._holdCover = HoldCover(self)
        # Plan 39 VI14d. Its window handle is made now, hidden; made at the
        # first show it held the GUI thread for about 30 ms in a section raise.
        self._holdCover.winId()
        _constructionTurn()

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

        self._installViewportOverlays()
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
            _constructionTurn()
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

    def setClearInsets(self, left=0, top=0, right=0, bottom=0):
        """Plan 43 LY5. Centre the model in the clear area.

        The sheet and the console take ``left``/``top``/``right``/``bottom``
        pixels of the viewport; the camera's window centre moves the
        projection so the model sits in what is left. Nothing in the scene,
        the camera position or the picker changes.
        """
        self._clearInsets = (int(left), int(top), int(right), int(bottom))
        self._applyClearInsets()
        self._layoutCorner()
        self.arrangeOverlays()
        if self._modelFitted and not self._cameraMovedByUser:
            # Plan 44 MF7. Still the framing this view chose: it follows the
            # clear area, so the model keeps 80 % of what is left.
            self.fitCamera()
            return
        self._renderUnlessHeld()

    def _centredForCapture(self):
        """A saved picture is of the whole window, so its window centre goes
        back to the middle for the length of the capture and the on-screen
        centring (in the clear area) is restored after it. Without this the
        model sits off-centre in every PNG, SVG and clipboard image."""
        import contextlib

        @contextlib.contextmanager
        def centred():
            camera = self._renderer.GetActiveCamera()
            if camera is None:
                yield
                return
            was = tuple(camera.GetWindowCenter())
            camera.SetWindowCenter(0.0, 0.0)
            # The triad sits in the clear area's corner on screen; a saved
            # picture is of the whole window, so it goes to the window's own
            # corner for the length of the capture, as the window centre does.
            marker = self._originAxes
            viewport = None
            if marker is not None and self._cornerPixels > 0:
                viewport = _quietly(marker.GetViewport)
                viewport = None if viewport is None else tuple(viewport)
                if viewport is not None:
                    _quietly(marker.SetViewport, 0.0, 0.0,
                             self._cornerPixels / max(self.width(), 1),
                             self._cornerPixels / max(self.height(), 1))
            try:
                yield
            finally:
                camera.SetWindowCenter(*was)
                if viewport is not None:
                    _quietly(marker.SetViewport, *viewport)
        return centred()

    def clearRect(self):
        """Plan 44 MF7. The clear area in the view's pixels (a ``QRect``, y
        down): the one rectangle the model fit, the triad, the legends and
        every overlay slot are laid out against."""
        from foammesh.view.main_window.clear_area import Insets, clear_rect_int
        return clear_rect_int(self.width(), self.height(),
                              Insets(*self.clearInsets()))

    def _renderUnlessHeld(self):
        """Render, except while a hold is on (DP-347) or the view is gone."""
        if (self._disposed or self._suspended is not None
                or isRenderingHold()):
            return
        self._widget.Render()

    def _projectedExtent(self, bounds):
        """``(x0, y0, x1, y1)`` in view pixels (y down) of the projected
        corners of *bounds*, or None while the renderer has no size."""
        renderer = self._renderer
        size = renderer.GetSize()
        if not size or self.width() <= 0 or self.height() <= 0:
            return None
        rw, rh = size
        if rw <= 0 or rh <= 0:
            return None
        xs, ys = [], []
        for x in bounds[0:2]:
            for y in bounds[2:4]:
                for z in bounds[4:6]:
                    renderer.SetWorldPoint(x, y, z, 1.0)
                    renderer.WorldToDisplay()
                    dx, dy, _ = renderer.GetDisplayPoint()
                    xs.append(dx * self.width() / rw)
                    ys.append((rh - dy) * self.height() / rh)
        return min(xs), min(ys), max(xs), max(ys)

    def _fitToClearArea(self):
        """Plan 44 MF7. Size the model to 80 % of the clear area's height
        (and width, whichever binds), centred on it. The window centre is
        already on the clear centre, so only the scale is left to set."""
        from foammesh.view.main_window.clear_area import fit_factor
        bounds = self._visibleBounds()
        camera = self._renderer.GetActiveCamera()
        if bounds is None or camera is None:
            return
        clear = self.clearRect()
        self._applyClearInsets()
        for _ in range(8):
            box = self._projectedExtent(bounds)
            if box is None:
                return
            factor = fit_factor(box[2] - box[0], box[3] - box[1],
                                clear.width(), clear.height())
            if abs(factor - 1.0) < 0.001 or factor <= 0:
                return
            if camera.GetParallelProjection():
                camera.SetParallelScale(camera.GetParallelScale() / factor)
            else:
                camera.Dolly(factor)
            self._renderer.ResetCameraClippingRange()

    def clearInsets(self):
        return getattr(self, '_clearInsets', (0, 0, 0, 0))

    def _applyClearInsets(self):
        from foammesh.view.main_window.clear_area import Insets, window_center
        camera = self._renderer.GetActiveCamera()
        if camera is None:
            return
        width, height = self._widget.width(), self._widget.height()
        camera.SetWindowCenter(
            *window_center(width, height, Insets(*self.clearInsets())))

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

        # Plan 44 MF7: the model is sized to the clear area, not the view.
        self._fitToClearArea()

        if cubeAxesOn:
            self._showCubeAxes()

        self._resizeOriginTriad()

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
        self._modelFitted = True
        self._renderUnlessHeld()

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
            window.AddObserver(vtkCommand.StartEvent, self._renderWindowStarted)
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

    def _renderWindowStarted(self, _caller=None, _event=None):
        self._frameStart = time.perf_counter()
        self._startGpuTimer()

    # -- closing round 2026-10-12 (VI14): a frame is timed, not waited for --- #

    def _gpuTimingWanted(self) -> bool:
        """Whether this frame is one the interaction detail will read."""
        detail = self.__dict__.get('_interactionDetail')
        try:
            return (detail is not None and not detail.active()
                    and detail.worthTiming())
        except Exception:                                   # noqa: BLE001
            return False

    def _startGpuTimer(self) -> None:
        """Put a GPU timer query round the frame about to be drawn.

        The query is read later, from the event loop, when the GPU has
        finished the frame: the GUI thread never waits for it. (Waiting for
        the frame, ``glFinish``, held the thread for the whole GPU time of
        every frame of a large scene: 150 to 330 ms each on the integrated
        GPU of the reference host.)
        """
        if self._gpuTimerSupported is False or not self._gpuTimingWanted():
            return
        try:
            from vtkmodules.vtkRenderingOpenGL2 import vtkOpenGLRenderTimer
            if self._gpuTimerSupported is None:
                self._gpuTimerSupported = bool(
                    vtkOpenGLRenderTimer.IsSupported())
                if not self._gpuTimerSupported:
                    return
            if len(self._gpuTimers) > 8:
                return            # the GPU is far behind; do not queue more
            timer = vtkOpenGLRenderTimer()
            timer.Start()
            self._gpuTimers.append(timer)
        except Exception:                                   # noqa: BLE001
            self._gpuTimerSupported = False
            logger.debug('GPU frame timer unavailable', exc_info=True)

    def _stopGpuTimer(self) -> None:
        timers = self.__dict__.get('_gpuTimers')
        if not timers:
            return
        timer = timers[-1]
        try:
            if timer.Started() and not timer.Stopped():
                timer.Stop()
        except Exception:                                   # noqa: BLE001
            timers.remove(timer)
            return
        poll = self.__dict__.get('_gpuTimerPoll')
        if poll is not None and not poll.isActive():
            poll.start()

    def _dropGpuTimers(self) -> None:
        """Release the queries against the window's own context, then forget
        them (a query object is a GL object: it is freed with its context
        current, or not at all)."""
        timers, self._gpuTimers = self._gpuTimers, []
        self._gpuSeconds = []
        if not timers:
            return
        if disposal.make_current(self.renderWindow()):
            for timer in timers:
                try:
                    timer.ReleaseGraphicsResources()
                except Exception:                           # noqa: BLE001
                    pass

    def _pollGpuTimers(self) -> None:
        """Collect the timers the GPU has finished; ask again for the rest."""
        if not self._gpuTimers:
            return
        if self._disposed:
            self._gpuTimers = []
            return
        if self._inRender or self._suspended is not None \
                or isRenderingHold():
            self._gpuTimerPoll.start()
            return
        if not disposal.make_current(self.renderWindow()):
            self._gpuTimers = []
            return
        pending = []
        for timer in self._gpuTimers:
            try:
                if not timer.Stopped():
                    pending.append(timer)
                    continue
                if timer.Ready():
                    seconds = float(timer.GetElapsedSeconds())
                    timer.ReleaseGraphicsResources()
                    if seconds > 0.0 and math.isfinite(seconds):
                        self._gpuSeconds.append(seconds)
                else:
                    pending.append(timer)
            except Exception:                               # noqa: BLE001
                continue
        self._gpuTimers = pending
        detail = self.__dict__.get('_interactionDetail')
        while self._gpuSeconds and detail is not None:
            detail.noteFrame(self._gpuSeconds.pop(0))
        if detail is not None:
            try:
                if detail.slow() and not detail.active():
                    timer = self.__dict__.get('_interactionDetailTimer')
                    if timer is not None and not timer.isActive():
                        timer.start()
            except Exception:                               # noqa: BLE001
                logger.debug('Noting a GPU frame time failed', exc_info=True)
        if self._gpuTimers and not self._disposed:
            self._gpuTimerPoll.start()

    def _lastFrameSeconds(self) -> float:
        """The oldest finished GPU frame time not yet used; 0.0 if none.

        MEASURED 2026-10-09 on the AMD Radeon iGPU: for a 5 M-quad surface
        ``vtkRenderer.GetLastRenderTimeInSeconds`` and the window's
        start-to-end time both read about 1 ms, because ``Render`` returns
        once the commands are queued; the frame took 32 ms to finish. The
        first version waited for the GPU here (``glFinish``) and so held
        the GUI thread for each frame's whole GPU time (VI14 closing round:
        150 to 330 ms per frame of the 6.6 M-cell impeller). The frame is
        now timed by a GPU timer query (``_startGpuTimer``), read when the
        GPU has finished it, and this returns the oldest such reading. Where
        the query is not supported the first frames of a scene are timed by
        waiting, as before, and no more than the detail needs to decide.
        """
        if self._gpuSeconds:
            return self._gpuSeconds.pop(0)
        if self._gpuTimerSupported is not False:
            return 0.0
        start = self._frameStart
        detail = self.__dict__.get('_interactionDetail')
        if (start is None or detail is None
                or detail.frameSeconds() is not None):
            return 0.0
        wait = getattr(self.renderWindow(), 'WaitForCompletion', None)
        if wait is not None:
            wait()
        return time.perf_counter() - start

    def _noteFrame(self) -> None:
        """Plan 39 VI3. Tell the interaction detail how long this frame took.

        Three full-detail frames averaging over 33 ms make the scene want its
        reduced copies whatever its face count; they are then made once, in
        the background, and used from the next drag on.
        """
        detail = self.__dict__.get('_interactionDetail')
        if detail is None or self._disposed or detail.active():
            return
        try:
            if not detail.worthTiming():
                return
            seconds = self._lastFrameSeconds()
            if seconds > 0.0:
                detail.noteFrame(seconds)
            if detail.slow() and not detail.active():
                timer = self.__dict__.get('_interactionDetailTimer')
                if timer is not None and not timer.isActive():
                    timer.start()
        except Exception:                                   # noqa: BLE001
            logger.debug('Noting a frame time failed', exc_info=True)

    def _renderWindowEnded(self, _caller=None, _event=None):
        self._stopGpuTimer()
        self._noteFrame()
        if self._inRender or self._suspended is not None or self._disposed:
            return
        window = self.renderWindow()
        reason = self._renderError or (
            gl_health.lost_context(window, self._interactorStarted,
                                   check_error=False)
            if window is not None else None)
        if reason:
            QTimer.singleShot(0, lambda: self._contextLost(reason))
        else:
            self._scheduleGlErrorCheck()

    #: Quiet time before the GL error flag is read (see `_checkGlErrorFlag`).
    GL_ERROR_CHECK_DELAY = 250

    def _scheduleGlErrorCheck(self) -> None:
        """Read the GL error flag once the viewport has been quiet.

        Plan 39 VI14c. ``glGetError`` finishes the frame in flight, so asking
        after every frame put the GPU's whole frame time (1.5 s at worst on
        the 6.6 M-cell impeller) inside the GUI thread's hold. Each frame
        restarts the wait, so the flag is read when the GPU has nothing left
        to finish. A lost or out-of-memory context is still found, a quarter
        of a second late.
        """
        if self.__dict__.get('_disposed'):
            return
        timer = self.__dict__.get('_glErrorTimer')
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.GL_ERROR_CHECK_DELAY)
            timer.timeout.connect(self._checkGlErrorFlag)
            self._glErrorTimer = timer
        timer.start()

    def _checkGlErrorFlag(self) -> None:
        if (self._disposed or self._inRender or self._suspended is not None):
            return
        window = self.renderWindow()
        if window is None:
            return
        reason = gl_health.lost_context(window, self._interactorStarted)
        if reason:
            self._contextLost(reason)

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
            # Plan 39 VI14c. The first draw of a new scene is checked
            # at once (a scene too big for the card is found here); any other
            # draw is checked when the viewport is quiet.
            reason = self._renderError or gl_health.lost_context(
                window, wasInitialized, check_error=bool(operation))
        if operation:
            heartbeat.set_last_op(previous or '')
        if reason:
            self._contextLost(reason)
            return False
        if not self._glChecked:
            self._checkGl(window)
        if not operation:
            self._scheduleGlErrorCheck()
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
        self._showCornerTriad()

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
            if self._originRenderer is not None:
                _quietly(getattr(oldWindow, 'RemoveRenderer', None),
                         self._originRenderer)
            _quietly(getattr(oldWindow, 'RemoveRenderer', None), self._renderer)
            _quietly(getattr(oldWindow, 'RemoveAllObservers', None))

        widget = RenderWindowInteractor(self)
        window = widget.GetRenderWindow()
        window.SetMultiSamples(0 if self._safeMode else MULTI_SAMPLES)
        widget.SetInteractorStyle(self._style)
        widget.renderGuard = self._guardedRender
        widget.onHeldPaint = self._heldPaint
        widget.onDrawn = self._frameDrawn
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
        self._cornerObserver = None
        if self._originAxes is not None:
            self._installCornerClick()
        self._attachOriginRenderer(window)
        self._attachMeasureLayer(window)

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
        # GPUPREF: the first context is up. Clear the start marker, remember
        # the adapter for the diagnostics, fall back from a software renderer.
        try:
            from foammesh.rendering import gpu_preference
            from foammesh.app import app as _app
            gpu_preference.confirm_render(
                info, _app.settings, lifecycle.log_directory())
            notice = gpu_preference.take_notice()
            if notice:
                self._setRenderNote(notice, part='gpu')
        except Exception:                                  # noqa: BLE001
            logger.debug('GPU preference check failed', exc_info=True)
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
        for timer in ('_holdCoverTimer', '_holdCoverWatch',
                      '_presentNowTimer', '_presentSettleTimer'):
            _quietly(getattr(self.__dict__.get(timer), 'stop', None))
        from foammesh.app import app
        if app.themeManager is not None:
            try:
                app.themeManager.themeChanged.disconnect(self._themeChanged)
            except (RuntimeError, TypeError):
                pass
        self._hoverTimer.stop()
        self._interactionDetailTimer.stop()
        glTimer = self.__dict__.get('_glErrorTimer')
        if glTimer is not None:
            glTimer.stop()
        self._gpuTimerPoll.stop()
        _quietly(self._dropGpuTimers)
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
        if self._cornerObserver is not None:
            old, tag = self._cornerObserver
            _quietly(old.RemoveObserver, tag)
            self._cornerObserver = None
        if self._originRenderer is not None:
            _quietly(self._originRenderer.RemoveAllViewProps)
            _quietly(getattr(self.renderWindow(), 'RemoveRenderer', None),
                     self._originRenderer)
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

    #: Plan 39 VI4 (F1e). ``resolver(actor, x, y, renderer) -> actor`` hands a
    #: pick on a prop back to the part it stands for: a patch drawn by the
    #: volume's exterior is picked as the exterior, and the click is the
    #: patch's. None leaves every pick as it is.
    _pickResolver = None

    def setPickResolver(self, resolver) -> None:
        self._pickResolver = resolver

    #: DP-2713 / DP-2749. A mapper input with at least this many cells gets
    #: a cell locator, kept until the data changes. MEASURED (dev build,
    #: 2026-10-20): a linear ray cast costs 6 ms at 79 k faces, 76 ms at
    #: 977 k and 442 ms at 3.9 M; a vtkStaticCellLocator builds once in
    #: 83 ms at 1 M (314 ms at 4 M) and then answers in about 0.04 ms.
    PICK_LOCATOR_MIN_CELLS = 100_000

    def _cellPick(self, x, y):
        """A CPU cell pick at a display point: the picker, or ``None``.

        DP-2713 / DP-2749. Picking a part used to be a `vtkPropPicker`, which
        draws the scene once more in hardware-selection mode. In VTK 9.5.2
        that pass reads a prop id the selector never initialised, so hover
        picks logged "vtkOpenGLHardwareSelector: Too many props" at random --
        MEASURED 27 errors in 300 hover picks on a 20 k-cell elbow, every one
        from the prop pick. A ray cast through the renderer's own props draws
        nothing, cannot reach that code, and (with the locators above) costs
        a fraction of a millisecond on any mesh size.
        """
        picker = vtkCellPicker()
        picker.SetTolerance(0.0005)
        for locator in self._pickLocators():
            picker.AddLocator(locator)
        if not picker.Pick(float(x), float(y), 0.0, self._renderer):
            return None
        return picker

    def _pickLocators(self) -> list:
        renderer = getattr(self, '_renderer', None)
        if renderer is None:
            return []
        from vtkmodules.vtkCommonDataModel import vtkStaticCellLocator

        getProps = getattr(renderer, 'GetViewProps', None)
        if not callable(getProps):
            return []
        cache = self.__dict__.get('_pickLocatorCache', {})
        live = {}
        props = getProps()
        if props is None:
            return []
        props.InitTraversal()
        for _ in range(props.GetNumberOfItems()):
            prop = props.GetNextProp()
            if (prop is None or not prop.GetVisibility()
                    or not prop.GetPickable()):
                continue
            getMapper = getattr(prop, 'GetMapper', None)
            mapper = getMapper() if callable(getMapper) else None
            getInput = getattr(mapper, 'GetInput', None)
            data = getInput() if callable(getInput) else None
            if (data is None or not hasattr(data, 'GetNumberOfCells')
                    or data.GetNumberOfCells() < self.PICK_LOCATOR_MIN_CELLS):
                continue
            key = id(data)
            entry = cache.get(key)
            if (entry is None or entry[0] is not data
                    or entry[1] != data.GetMTime()):
                locator = vtkStaticCellLocator()
                locator.SetDataSet(data)
                locator.BuildLocator()
                entry = (data, data.GetMTime(), locator)
            live[key] = entry
        # Only what is in the scene now is held: a replaced mesh lets go.
        self._pickLocatorCache = live
        return [entry[2] for entry in live.values()]

    def _resolvedActor(self, picker, x, y):
        actor = picker.GetActor() if picker is not None else None
        resolver = self._pickResolver
        if actor is not None and resolver is not None:
            actor = resolver(actor, x, y, self._renderer) or actor
        return actor

    def cellPickedActor(self, x, y):
        """The actor the cell picker finds at a display point, or ``None``."""
        return self._resolvedActor(self._cellPick(x, y), x, y)

    def pickActor(self, x, y):
        """The part at a display point, or ``None``.

        DP-2713 / DP-2749: a CPU cell pick (`_cellPick`), not a hardware
        selection pass; the same pick the hover readout and Measure use, so
        the part named, the cell read and the point measured always agree.
        """
        return self._resolvedActor(self._cellPick(x, y), x, y)

    def clear(self):
        # vtkLogoWidget installs its representation as a renderer prop. Turn
        # it off before clearing so the following On() call actually
        # re-registers the watermark instead of returning as already enabled.
        self._logoWidget.Off()
        self._renderer.RemoveAllViewProps()
        layer = self.__dict__.get('_measureLayer')
        if layer is not None:
            layer.RemoveAllViewProps()
        self._measureActors = []
        self._measureState.clear()
        if getattr(self, '_measureReadout', None) is not None:
            self._measureReadout.show_lines([])
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
        self.rollCameraBy(90.0)

    def rollCameraBy(self, degrees: float):
        """Turn the view about the line of sight, the toolbar's way round."""
        self.rememberView()
        self._renderer.GetActiveCamera().Roll(-float(degrees))
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

    def saveScreenshot(self, path, *, scale: int = 2, includes=None) -> bool:
        """Capture the rendered viewport as a PNG at 1x, 2x or 4x resolution.

        Plan 35 CR8: drawn through the render hold and the guarded render;
        False, and no file, while the view cannot draw.
        """
        if int(scale) not in {1, 2, 4}:
            raise ValueError('screenshot scale must be 1, 2 or 4')
        if isRenderingHold() or self._suspended is not None:
            return False
        with self._pictureLegends(includes):
            rgb = self._captureArray(int(scale),
                                     hide=self._hiddenFor(includes))
        if rgb is None:
            return False
        label_pass.write_array(rgb, path, 'PNG')
        return True

    def saveImage(self, path, fmt: str = 'PNG', *, scale: int = 2,
                  includes=None):
        """Write the viewport as PNG, JPEG, SVG, PDF, EPS or PS (Plan 42 SG10).

        One writer for both engines.  Returns the path written (the suffix is
        set to the format) or None while the view cannot draw.
        """
        from pathlib import Path

        from foammesh.core.gmsh import session_display as sd

        fmt = str(fmt).upper()
        if fmt == 'JPG':
            fmt = 'JPEG'
        if fmt not in sd.RASTER_FORMATS and fmt not in sd.VECTOR_FORMATS:
            raise ValueError(f'{fmt} is not an image format')
        if isRenderingHold() or self._suspended is not None:
            return None
        window = self._widget.GetRenderWindow()
        if not self._guardedRender(window.Render, 'save image'):
            return None
        with self._withoutTriad(includes), self._centredForCapture(), \
                self._pictureLegends(includes):
            return sd.export_view(
                window, Path(path), fmt, scale=int(scale),
                hide=self._hiddenFor(includes), font=self._labelStyle.font,
                transparent=bool((includes or {}).get('transparent')))

    # -- a capture as an image in memory (VI15, I3) -------------------------- #

    def _captureArray(self, scale: int, hide=()):
        """The rendered window at ``scale`` x as an (h, w, 3) uint8 array.

        Labels are drawn at the picture's size (``label_pass``), not tiled.
        """
        window = self._widget.GetRenderWindow()
        found = []

        def draw():
            found.append(label_pass.capture(window, self._renderer,
                                            int(scale), hide=hide))

        with self._centredForCapture():
            drawn = self._guardedRender(draw, 'capture')
        if not drawn or not found:
            return None
        return found[0]

    # -- label font, size and groups (Plan 43 LY15) -------------------------- #

    @staticmethod
    def _announce(signal, value) -> None:
        """Emit, unless the widget is a bare stand-in without its QObject."""
        try:
            signal.emit(value)
        except RuntimeError:
            pass

    def labelStyle(self):
        return self._labelStyle

    def setLabelStyle(self, style=None, *, font=None, size=None) -> None:
        """Set the font and size of every label on the view."""
        if style is None:
            current = self._labelStyle
            style = label_style.normalise(font or current.font,
                                          size or current.size)
        self._labelStyle = style
        self.applyLabelStyle()
        self._announce(self.labelStyleChanged, style)

    def applyLabelStyle(self) -> None:
        """Put the current font and size on every label prop of the view.

        A label's own size is remembered the first time it is touched, so
        Normal puts it back and the others are multiples of it.
        """
        for prop in label_pass.label_props(self._renderer, visible_only=False):
            for text in label_pass.text_properties(prop):
                key = text.__this__
                base = self._labelBase.setdefault(key, text.GetFontSize())
                label_style.apply(text, self._labelStyle, base)
        self.refresh()

    def registerLabelGroup(self, group: str, prop) -> None:
        """Make *prop* one of the labels an Include box switches ('worst', 'caption')."""
        self._labelGroups.setdefault(group, [])
        if all(prop is not other for other in self._labelGroups[group]):
            self._labelGroups[group].append(prop)
        prop.SetVisibility(bool(self._labelGroupShown.get(group, True)))
        self.applyLabelStyle()

    def unregisterLabelGroup(self, group: str, prop=None) -> None:
        items = self._labelGroups.get(group, [])
        self._labelGroups[group] = [] if prop is None else [
            other for other in items if other is not prop]

    def setLabelGroupShown(self, group: str, shown: bool) -> None:
        self._labelGroupShown[group] = bool(shown)
        for prop in self._labelGroups.get(group, []):
            prop.SetVisibility(bool(shown))
        self.refresh()

    def isLabelGroupShown(self, group: str) -> bool:
        return bool(self._labelGroupShown.get(group, True))

    def setLegendsShown(self, shown: bool) -> None:
        self._legendsShown = bool(shown)
        for key, bar in self._legends.items():
            bar.SetVisibility(bool(shown)
                              and key not in self._pictureOnlyLegends)
        self.refresh()
        self._announce(self.legendsShownChanged, bool(shown))

    def areLegendsShown(self) -> bool:
        return bool(getattr(self, '_legendsShown', True))

    def _hiddenFor(self, includes) -> list:
        """The label props an Include box that is off leaves out."""
        if not includes:
            return []
        hidden = []
        if includes.get('legend') is False:
            hidden += list(self._legends.values())
        for group in ('worst', 'caption'):
            if includes.get(group) is False:
                hidden += list(self._labelGroups.get(group, []))
        return hidden

    def isPictureOnlyLegend(self, key: str) -> bool:
        """Plan 44 FX3. Whether legend *key* is drawn only into a saved picture."""
        return key in self._pictureOnlyLegends

    def _pictureLegends(self, includes):
        """Plan 44 FX3. Show the picture-only scalar bars for one VTK capture.

        A saved PNG/JPEG/SVG/PDF is the render window alone, so the legend the
        screen draws in Qt is not in it; the picture-only bar stands in for
        it there, under the same switches (*Show legend*, the standard's
        legend, the Include box). On screen it stays hidden.
        """
        import contextlib

        from foammesh.support import colour_standard

        wanted = (self.areLegendsShown()
                  and bool(colour_standard.current().legend)
                  and (includes or {}).get('legend') is not False)
        bars = [self._legends[key] for key in self._pictureOnlyLegends
                if key in self._legends] if wanted else []
        if not bars:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def shown():
            for bar in bars:
                bar.SetVisibility(True)
            try:
                yield
            finally:
                for bar in bars:
                    bar.SetVisibility(False)

        return shown()

    def _withoutTriad(self, includes):
        """The corner triad is left out for the length of a capture when asked."""
        import contextlib

        marker = self._originAxes
        if not includes or includes.get('triad') is not False or marker is None:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def hidden():
            was = bool(_quietly(marker.GetEnabled))
            marker.SetEnabled(0)
            try:
                yield
            finally:
                if was:
                    marker.SetEnabled(1)

        return hidden()

    def grabImage(self, scale: int = 2, *, transparent: bool = False,
                  legends: bool = True, includes=None):
        """The picture as a ``QImage`` at 1x, 2x or 4x, or None if it cannot draw.

        ``transparent`` leaves the background clear: the scene is drawn once
        on black and once on white and the difference is the opacity of each
        pixel, which needs no alpha buffer in the window. ``legends=False``
        takes the scalar bars out of the picture.
        """
        import numpy as np
        import numpy as np
        from PySide6.QtGui import QImage

        if int(scale) not in {1, 2, 4}:
            raise ValueError('capture scale must be 1, 2 or 4')
        if isRenderingHold() or self._suspended is not None:
            return None
        hidden = []
        includes = dict(includes or {})
        if not legends:
            includes['legend'] = False
        # The labels an Include box leaves out are dropped inside the capture;
        # what is hidden here is the logo, which is not the model.
        omit = self._hiddenFor(includes)
        renderer = self._renderer
        gradient = renderer.GetGradientBackground()
        textured = renderer.GetTexturedBackground()
        low, high = renderer.GetBackground(), renderer.GetBackground2()
        try:
            if not transparent:
                rgb = self._captureArray(scale, hide=omit)
                alpha = None
            else:
                # The logo is a picture of its own: it is not the model.
                logo = self._logoRepresentation
                if logo is not None and logo.GetVisibility():
                    logo.SetVisibility(False)
                    hidden.append(logo)
                renderer.GradientBackgroundOff()
                renderer.TexturedBackgroundOff()
                renderer.SetBackground(0.0, 0.0, 0.0)
                black = self._captureArray(scale, hide=omit)
                renderer.SetBackground(1.0, 1.0, 1.0)
                white = self._captureArray(scale, hide=omit)
                if black is None or white is None:
                    return None
                coverage = 1.0 - (white.astype(np.float32)
                                  - black.astype(np.float32)).mean(axis=2) / 255.0
                coverage = np.clip(coverage, 0.0, 1.0)
                safe = np.where(coverage > 0.003, coverage, 1.0)[:, :, None]
                rgb = np.clip(black.astype(np.float32) / safe, 0, 255).astype(np.uint8)
                alpha = (coverage * 255.0 + 0.5).astype(np.uint8)
        finally:
            renderer.SetBackground(*low)
            renderer.SetBackground2(*high)
            renderer.SetGradientBackground(gradient)
            renderer.SetTexturedBackground(textured)
            for bar in hidden:
                bar.SetVisibility(True)
            self._widget.Render()
        if rgb is None:
            return None
        height, width = rgb.shape[:2]
        if alpha is None:
            data = np.ascontiguousarray(rgb)
            image = QImage(data.data, width, height, 3 * width,
                           QImage.Format.Format_RGB888)
        else:
            data = np.ascontiguousarray(np.dstack([rgb, alpha]))
            image = QImage(data.data, width, height, 4 * width,
                           QImage.Format.Format_RGBA8888)
        return image.copy()

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
        """A moving frame of a drag: draw the reduced copies once they apply.

        The first moving frame tries; a scene that was not slow then is tried
        again when three full-detail frames have shown it is (Plan 39 VI3), so
        the copies engage within three frames of a drag that stutters.
        """
        detail = self._interactionDetail
        if detail.active() or detail.ready() == 0:
            return
        if self._interactionDetailTried and not detail.slow():
            return
        self._interactionDetailTried = True
        try:
            swapped = detail.begin()
        except Exception:                                   # noqa: BLE001
            logger.exception('Swapping in the interaction detail')
            swapped = 0
        if swapped:
            self.detailReduced.emit(True)

    def beginHandleDrag(self) -> bool:
        """A handle (a section plane) is being dragged: draw the reduced
        copies while it moves, as for a camera drag.

        Closing round 2026-10-12 (VI14). Only the camera's own drag used to
        swap them in, so every step of a plane drag drew the full scene:
        150 ms of GPU each on the 6.6 M-cell impeller. Returns whether the
        copies stand in now.
        """
        self._interactionInFlight = True
        self._hoverTimer.stop()
        if not self.__dict__.get('_handleDragHeld'):
            self._handleDragHeld = True
            from foammesh.rendering.actor_info import hold_cut_installs
            hold_cut_installs(True)
        detail = self._interactionDetail
        if detail.active():
            return True
        if detail.ready() == 0:
            self._scheduleInteractionDetail()
            return False
        try:
            swapped = detail.begin()
        except Exception:                                   # noqa: BLE001
            logger.exception('Swapping in the interaction detail')
            swapped = 0
        if swapped:
            self.detailReduced.emit(True)
        return bool(swapped)

    def endHandleDrag(self) -> None:
        """The handle was let go: the full parts are drawn again."""
        self._interactionInFlight = False
        if self.__dict__.get('_handleDragHeld'):
            self._handleDragHeld = False
            from foammesh.rendering.actor_info import hold_cut_installs
            hold_cut_installs(False)
        if self._interactionDetail.end():
            self.detailReduced.emit(False)
        self._scheduleInteractionDetail()

    def _cameraInteractionEnded(self, obj, event):
        """Full detail back before the frame the style draws at release."""
        self._interactionInFlight = False
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

    def flyToPoint(self, centre, distance: float) -> bool:
        """Put the camera ``distance`` from ``centre``, looking at it.

        Plan 39 VI8 (F5b). The direction the user is looking along is kept --
        turning the model under someone who asked to go to a cell is a second
        thing they did not ask for -- and the move is recorded, so Back returns
        to where they were. Parallel projection has no distance, so the scale
        is set to show the same width.
        """
        import math

        centre = tuple(float(value) for value in centre)
        distance = float(distance)
        if len(centre) != 3 or not (distance > 0.0) or not all(
                math.isfinite(value) for value in centre + (distance,)):
            return False
        camera = self._renderer.GetActiveCamera()
        position = camera.GetPosition()
        focal = camera.GetFocalPoint()
        direction = [position[i] - focal[i] for i in range(3)]
        length = math.sqrt(sum(value * value for value in direction))
        if length <= 0.0:
            direction, length = [1.0, 1.0, 1.0], math.sqrt(3.0)
        self.rememberView()
        camera.SetFocalPoint(*centre)
        camera.SetPosition(*[centre[i] + direction[i] / length * distance
                             for i in range(3)])
        camera.SetParallelScale(
            distance * math.tan(math.radians(camera.GetViewAngle()) / 2.0))
        self._renderer.ResetCameraClippingRange()
        self._widget.Render()
        return True

    def flyToBounds(self, bounds) -> bool:
        """Frame ``bounds`` (xmin, xmax, ymin, ymax, zmin, zmax), Back-able."""
        import math

        bounds = tuple(float(value) for value in bounds)
        if len(bounds) != 6 or not all(math.isfinite(v) for v in bounds) \
                or bounds[0] > bounds[1]:
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
        """Show or hide the axes at the world origin (Plan 39 VI15, I10).

        The viewport triad in the corner is not this: it is always on.
        """
        if checked:
            self._showOriginTriad()
        else:
            self._hideOriginTriad()
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
        self._renderer.TexturedBackgroundOff()

    def setBackground2(self, r, g, b):
        self._userBackground['top'] = (r, g, b)
        self._renderer.SetBackground2(r, g, b)
        self._renderer.TexturedBackgroundOff()

    def _keepUserBackground(self):
        """Put back the gradient ends the user picked after a theme pass.

        DP-701 (viewport audit 0925 F15). ``apply_vtk_theme`` paints the
        background from the theme, and it runs again whenever the cube axes
        or the origin axes are first shown, so a colour picked from the
        swatches lasted only until the next of those.
        """
        if self._userBackground:
            # MF15. A picked gradient replaces the theme's radial texture.
            self._renderer.TexturedBackgroundOff()
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
        apply_triad_theme(self._cornerCube, self._originAxesActor, tokens)
        if self._originTriad is not None:
            apply_vtk_theme(self._renderer, tokens, origin_axes=self._originTriad)
            styleOriginLabels(self._originTriad, tokens)
        self._keepUserBackground()
        self.refresh()

    def _themeChanged(self, _name):
        from foammesh.app import app
        if app.themeManager is not None and app.themeManager.tokens is not None:
            self.applyTheme(app.themeManager.tokens)

    # -- UXB5 (N4): no black frame while a hold drops the paints ----------- #

    def holdCover(self):
        return self._holdCover

    def _heldPaint(self):
        """A paint was dropped because a VTK job or a scene swap holds
        rendering. A frame is owed from now on, whether or not the hold
        lasts long enough to raise the cover (DP-2705)."""
        self._frameOwed = True
        if not self._holdCoverWatch.isActive():
            self._holdCoverWatch.start()
        cover = self._holdCover
        if cover is None or not cover.isHidden():
            return
        if not self._holdCoverTimer.isActive():
            self._holdCoverTimer.start()

    def _showHoldCover(self):
        cover = self._holdCover
        if (cover is None or not isRenderingHold() or self._disposed
                or self._suspended is not None):
            # The placeholder stands in for a suspended view: nothing to
            # cover, and nothing would ever draw over the cover.
            return
        top = QColor.fromRgbF(*self._renderer.GetBackground2())
        bottom = QColor.fromRgbF(*self._renderer.GetBackground())
        cover.setColors(top, bottom)
        tokens = self._themeTokens
        if tokens is not None and not self._userBackground:
            try:
                cover.setRadial(tokens.value('viewport.centre'),
                                tokens.value('viewport.mid'),
                                tokens.value('viewport.edge'))
            except (KeyError, AttributeError):
                pass
        cover.setText(self.tr('Updating the view…'))
        # DP-2880. The model stays on screen under the hold.
        cover.setFrame(self._lastFrameImage())
        cover.setGeometry(self.rect())
        cover.show()
        cover.raise_()
        self._holdCoverWatch.start()

    def _lastFrameImage(self):
        """DP-2880. The frame on screen, as a ``QImage``, or None.

        Read from the render window's display framebuffer (``front``), which
        holds the last finished frame whatever the window system did to the
        native surface since. Nothing is rendered: the job holding rendering
        may be changing the scene's data, and only pixels are read. A frame
        that is all black (the view never drew) is not worth showing.
        """
        window = self.renderWindow()
        if window is None or self._inRender:
            return None
        try:
            import numpy as np
            from PySide6.QtGui import QImage
            from vtkmodules.util.numpy_support import vtk_to_numpy
            from vtkmodules.vtkCommonCore import vtkUnsignedCharArray
            width, height = (int(v) for v in window.GetSize())
            if width <= 0 or height <= 0:
                return None
            data = vtkUnsignedCharArray()
            window.GetRGBACharPixelData(0, 0, width - 1, height - 1, 1, data)
            pixels = vtk_to_numpy(data)
            if pixels.size != width * height * 4:
                return None
            pixels = pixels.reshape(height, width, 4)[::-1]
            if not pixels[..., :3].any():
                return None
            pixels = np.ascontiguousarray(pixels)
            pixels[..., 3] = 255
            return QImage(pixels.data, width, height, width * 4,
                          QImage.Format.Format_RGBA8888).copy()
        except Exception:                                   # noqa: BLE001
            logger.debug('reading the last frame back', exc_info=True)
            return None

    def _watchHoldCover(self):
        """DP-2705. The hold is over: take the cover down and draw.

        The view under the cover cannot paint while the cover is up, so the
        hold ending is what decides, not a frame. The watch runs while the
        cover is up or a frame is owed, and stops only here, once both are
        settled."""
        cover = self._holdCover
        covered = cover is not None and not cover.isHidden()
        if covered and (self._disposed or self._suspended is not None):
            self._lowerHoldCover()
            covered = False
        if not covered and not self._frameOwed:
            self._holdCoverWatch.stop()
            return
        if isRenderingHold() and not self._disposed:
            return
        self._holdCoverWatch.stop()
        self._holdCoverTimer.stop()
        if covered:
            self._lowerHoldCover()
        self._presentFrame()
        # The window system repaints the area the cover leaves; if that
        # lands after this frame the view draws once more.
        self._presentSettleTimer.start()

    def _lowerHoldCover(self):
        cover = self._holdCover
        if cover is None:
            return
        cover.hide()
        cover.setFrame(None)
        # Below the view in the sibling order as well, so a native window
        # left mapped (or shown by anything else) cannot stay over it.
        cover.lower()

    def _presentFrame(self) -> bool:
        """DP-2705. Draw a real frame into the native window now.

        Not ``Render()`` on the QVTK widget: that only calls ``update()``,
        and the native window the cover was over gets no paint for it (the
        live view kept the cover's pixels 4 to 30 s after Qt hid it). The
        render window's own ``Render`` draws and swaps buffers into the
        window, through the guard. Returns True if a frame was drawn."""
        if self._disposed or self._suspended is not None:
            self._frameOwed = False
            return False
        cover = self._holdCover
        if isRenderingHold() or (cover is not None and not cover.isHidden()):
            # Owed still: the watch draws it when the hold ends.
            self._frameOwed = True
            if not self._holdCoverWatch.isActive():
                self._holdCoverWatch.start()
            return False
        window = self.renderWindow()
        if window is None or self._inRender:
            return False
        if not self._guardedRender(window.Render, 'hold over'):
            return False
        self._frameOwed = False
        return True

    def _frameDrawn(self):
        """A real frame reached the window.

        If the cover was still up the frame was drawn under it -- the cover
        is a native sibling, so it clipped the frame -- and the pixels the
        cover leaves are its own. It comes down and the view draws again."""
        self._holdCoverTimer.stop()
        cover = self._holdCover
        if cover is not None and not cover.isHidden():
            self._holdCoverWatch.stop()
            self._lowerHoldCover()
            self._frameOwed = True
            self._presentNowTimer.start()
            self._presentSettleTimer.start()
            return
        self._frameOwed = False

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

    # -- the corner: one triad, one cube, always on (VI15, I5, I10) --------- #

    def _showCornerTriad(self) -> None:
        """Put the viewport triad in the bottom-left corner, once.

        An axes triad inside an annotated cube, drawn by one orientation
        marker; a click on a cube face is a view preset (`cornerFaceAt`).
        Decoration must never take the viewport down, so a failure is logged.
        """
        if self._disposed or self._originAxes is not None:
            return
        try:
            axes = vtkAxesActor()
            axes.AxisLabelsOff()
            axes.PickableOff()
            axes.SetShaftTypeToLine()
            axes.SetConeRadius(0.0)
            # MF27. The marker fits the assembly's bounds; unit axes ran a
            # half cube past the cube's faces, so the cube drew 22 px in the
            # 52 px corner (the mockup's is about 45). Axes from the centre
            # to the faces, as the mockup draws them, let the cube fill it.
            axes.SetTotalLength(*CORNER_AXES_LENGTH)
            dpr = max(self.devicePixelRatioF(), 1.0)
            for shaft in (axes.GetXAxisShaftProperty(),
                          axes.GetYAxisShaftProperty(),
                          axes.GetZAxisShaftProperty()):
                shaft.SetLineWidth(TRIAD_AXIS_WIDTH * dpr)
            cube = vtkAnnotatedCubeActor()
            for face, text in (('XPlus', '+X'), ('XMinus', '-X'),
                               ('YPlus', '+Y'), ('YMinus', '-Y'),
                               ('ZPlus', '+Z'), ('ZMinus', '-Z')):
                getattr(cube, f'Set{face}FaceText')(text)
            cube.SetFaceTextScale(0.4)
            cube.GetCubeProperty().SetColor(0.78, 0.8, 0.86)
            cube.GetCubeProperty().SetOpacity(0.55)
            cube.GetTextEdgesProperty().SetColor(0.3, 0.3, 0.34)
            cube.GetTextEdgesProperty().SetLineWidth(1)
            # 52 px is too small to read a face's name; the cube is a flat
            # fill with a stroke (MF15), the faces still pick as presets.
            cube.SetTextEdgesVisibility(False)
            cube.SetCubeVisibility(True)
            cube.SetFaceTextVisibility(False)
            cube.GetCubeProperty().EdgeVisibilityOn()
            cube.GetCubeProperty().SetLineWidth(max(self.devicePixelRatioF(), 1.0))
            for face, colour in (('X', (0.85, 0.3, 0.3)), ('Y', (0.3, 0.7, 0.35)),
                                 ('Z', (0.3, 0.45, 0.9))):
                for sign in ('Plus', 'Minus'):
                    prop = getattr(cube, f'Get{face}{sign}FaceProperty')()
                    prop.SetColor(*colour)
                    prop.SetOpacity(TRIAD_FACE_OPACITY)
            cube.GetCubeProperty().SetOpacity(TRIAD_FACE_OPACITY)
            assembly = vtkPropAssembly()
            assembly.AddPart(cube)
            assembly.AddPart(axes)
            marker = vtkOrientationMarkerWidget()
            marker.SetOrientationMarker(assembly)
            marker.SetViewport(0.0, 0.0, 0.2, 0.2)
            marker.SetInteractor(self._widget)
            marker.EnabledOn()
            marker.InteractiveOff()
            self._originAxes = marker
            self._originAxesActor = axes
            self._cornerCube = cube
            if self._themeTokens is not None:
                self._themeAxes()
            self._layoutCorner()
            self._installCornerClick()
        except Exception:                                   # noqa: BLE001
            logger.exception('The viewport triad could not be drawn')

    def isCornerTriadShown(self) -> bool:
        marker = self._originAxes
        return marker is not None and bool(_quietly(marker.GetEnabled))

    def _themeAxes(self) -> None:
        apply_vtk_theme(self._renderer, self._themeTokens,
                        cube_axes=self._cubeAxesActor,
                        origin_axes=self._originAxesActor,
                        logo=self._logoRepresentation)
        apply_triad_theme(self._cornerCube, self._originAxesActor,
                          self._themeTokens)
        if self._originTriad is not None:
            apply_vtk_theme(self._renderer, self._themeTokens,
                            origin_axes=self._originTriad)
            styleOriginLabels(self._originTriad, self._themeTokens)
        self._keepUserBackground()

    def _layoutCorner(self) -> None:
        """Size the corner to the clear area, put the triad in its bottom
        left, and give the overlay layout the same rectangle (MF7)."""
        from foammesh.view.main_window.clear_area import (
            OVERLAY_INSET, Insets, clear_rect)
        width, height = self.width(), self.height()
        x0, y0, x1, y1 = clear_rect(width, height, Insets(*self.clearInsets()))
        inset = OVERLAY_INSET
        # The triad's box in logical pixels (y down): 52 square, 20 right of
        # the clear area's left edge, 22 above its bottom.
        room = int(min(x1 - x0 - CORNER_LEFT, y1 - y0 - CORNER_BOTTOM))
        size = max(min(CORNER_SIZE, room), 0) if width > 0 and height > 0 else 0
        self._cornerPixels = size
        layout = self._overlayLayout
        layout.setBounds((x0, y0, x1, y1), inset)
        layout.setCorner(CORNER_BOTTOM + size if size > 0 else 0)
        marker = self._originAxes
        if marker is not None and size > 0:
            left = x0 + CORNER_LEFT
            bottom = height - (y1 - CORNER_BOTTOM)   # GL origin: bottom left
            marker.SetViewport(left / width, bottom / height,
                               (left + size) / width, (bottom + size) / height)
        self._updateLogoLayout()

    def cornerPixels(self) -> int:
        return self._cornerPixels

    def cornerFaceAt(self, x, y) -> str | None:
        """The view preset of the cube face under a display point, or None.

        ``x``, ``y`` are render-window pixels (origin bottom-left), as the
        interactor reports them.
        """
        marker = self._originAxes
        if marker is None or not self.isCornerTriadShown():
            return None
        renderer = marker.GetRenderer()
        if renderer is None:
            return None
        viewport = marker.GetViewport()
        window = self.renderWindow().GetSize()
        if (x < viewport[0] * window[0] or x > viewport[2] * window[0]
                or y < viewport[1] * window[1] or y > viewport[3] * window[1]):
            return None
        picker = vtkPropPicker()
        if not pick_prop(picker, float(x), float(y), renderer):
            return None
        return facePresetAt(picker.GetPickPosition())

    def cornerFaceDisplayPoint(self, preset: str):
        """Display point (render-window pixels) of a cube face's centre."""
        index = 'xyz'.index(preset[1])
        world = [0.0, 0.0, 0.0, 1.0]
        world[index] = 0.5 if preset[0] == '+' else -0.5
        renderer = self._originAxes.GetRenderer()
        renderer.SetWorldPoint(*world)
        renderer.WorldToDisplay()
        point = renderer.GetDisplayPoint()
        return point[0], point[1]

    def _installCornerClick(self) -> None:
        """A press on a cube face turns the view and is not a rotation."""
        interactor = getattr(self._widget, '_Iren', None)
        if interactor is None or not hasattr(interactor, 'AddObserver'):
            return
        if self._cornerObserver is not None:
            old, tag = self._cornerObserver
            _quietly(old.RemoveObserver, tag)
        tag = interactor.AddObserver(
            'LeftButtonPressEvent', self._cornerPressed, 2.0)
        self._cornerObserver = (interactor, tag)

    def _cornerPressed(self, interactor, _event) -> None:
        if self._disposed or self._suspended is not None:
            return
        x, y = interactor.GetEventPosition()
        preset = self.cornerFaceAt(x, y)
        if preset is None:
            return
        command = interactor.GetCommand(self._cornerObserver[1])
        if command is not None:
            command.SetAbortFlag(1)
        self.setViewPreset(preset)

    # -- the world origin: the toolbar's Origin axes (VI15, I10) ------------- #

    def _showOriginTriad(self) -> None:
        if self._originTriad is None:
            actor = vtkAxesActor()
            actor.SetPosition(0.0, 0.0, 0.0)
            actor.PickableOff()
            actor.SetShaftTypeToCylinder()
            styleOriginLabels(actor, None)
            self._originTriad = actor
            renderer = vtkRenderer()
            renderer.SetLayer(1)
            renderer.InteractiveOff()
            renderer.AddActor(actor)
            # The same camera: the triad is in the model's own space, and
            # nothing in it counts toward the main scene's bounds or fit.
            renderer.SetActiveCamera(self._renderer.GetActiveCamera())
            self._originRenderer = renderer
            if self._themeTokens is not None:
                self._themeAxes()
        self._originTriadOn = True
        self._attachOriginRenderer(self.renderWindow())
        self._originTriad.SetVisibility(True)
        self._resizeOriginTriad()

    def _hideOriginTriad(self) -> None:
        self._originTriadOn = False
        if self._originTriad is not None:
            self._originTriad.SetVisibility(False)

    def _attachOriginRenderer(self, window) -> None:
        """Put the origin triad's layer in *window* (a new one after a reset)."""
        renderer = self._originRenderer
        if renderer is None or window is None:
            return
        _quietly(window.SetNumberOfLayers, 2)
        if not window.HasRenderer(renderer):
            window.AddRenderer(renderer)

    def isOriginTriadShown(self) -> bool:
        return bool(self._originTriadOn)

    def originTriadInfo(self) -> dict:
        """Where the world-origin triad is and how long its arms are."""
        actor = self._originTriad
        if actor is None:
            return {'visible': False, 'position': (0.0, 0.0, 0.0),
                    'arm': 0.0, 'span': 0.0}
        return {'visible': bool(self._originTriadOn),
                'position': tuple(actor.GetPosition()),
                'arm': float(actor.GetTotalLength()[0]),
                'span': self._sceneSpan()}

    def _sceneSpan(self) -> float:
        bounds = self._renderer.ComputeVisiblePropBounds()
        if bounds is None or bounds[0] > bounds[1]:
            return 0.0
        return float(max(bounds[1] - bounds[0], bounds[3] - bounds[2],
                         bounds[5] - bounds[4]))

    def _resizeOriginTriad(self) -> None:
        """Arms of 40 % of the model's smallest non-zero side, on every fit."""
        if self._originTriad is None:
            return
        arm = origin_arm_length(self._renderer.ComputeVisiblePropBounds())
        self._originTriad.SetTotalLength(arm, arm, arm)

    # -- one layout for the overlays (VI15, I4) ------------------------------ #

    def overlayLayout(self) -> OverlayLayout:
        return self._overlayLayout

    def addLegend(self, key: str, bar, size=(190, 260), *,
                  onScreen: bool = True) -> None:
        """Put a scalar bar in the stack of legends at the bottom left.

        Plan 44 FX3 (L2). ``onScreen=False`` keeps the bar for saved pictures
        only (`_pictureLegends`): it is never shown on the screen, where the
        themed Qt legend is the one scale of the field.
        """
        if key in self._legends:
            self.removeLegend(key)
        self._legends[key] = bar
        if onScreen:
            self._pictureOnlyLegends.discard(key)
        else:
            self._pictureOnlyLegends.add(key)
        self._renderer.AddActor2D(bar)
        self._applyLegendStandard(bar)
        self._styleLabels(bar)
        self._overlayLayout.register(
            key, Slot.BOTTOM_LEFT, size,
            lambda rect, bar=bar: apply_to_scalar_bar(
                bar, rect, max(self.width(), 1), max(self.height(), 1)))
        self.arrangeOverlays()

    def setLegendOnScreen(self, key: str, onScreen: bool) -> None:
        """Plan 44 FX3 (L2). Whether legend *key* may show on the screen.

        Off, the bar is kept for saved pictures only (see `addLegend`).
        """
        bar = self._legends.get(key)
        if bar is None:
            return
        if onScreen:
            self._pictureOnlyLegends.discard(key)
        else:
            self._pictureOnlyLegends.add(key)
        self._applyLegendStandard(bar)

    def _styleLabels(self, prop) -> None:
        for text in label_pass.text_properties(prop):
            base = self._labelBase.setdefault(text.__this__, text.GetFontSize())
            label_style.apply(text, self._labelStyle, base)

    def removeLegend(self, key: str) -> None:
        bar = self._legends.pop(key, None)
        self._pictureOnlyLegends.discard(key)
        if bar is not None:
            _quietly(self._renderer.RemoveActor2D, bar)
        self._overlayLayout.unregister(key)
        self.arrangeOverlays()

    def _applyLegendStandard(self, bar) -> None:
        """Plan 43 LY12. *Show legend* off hides the scales, not the keys."""
        from foammesh.support import colour_standard
        table = bar.GetLookupTable() if hasattr(bar, 'GetLookupTable') else None
        if any(bar is self._legends.get(key)
               for key in self._pictureOnlyLegends):
            bar.SetVisibility(False)
            return
        if table is not None and table.GetIndexedLookup():
            return
        bar.SetVisibility(bool(colour_standard.current().legend)
                          and self.areLegendsShown())

    def applyColourStandard(self) -> None:
        """The colour standard changed: legends follow, the picture redraws."""
        for bar in self._legends.values():
            self._applyLegendStandard(bar)
        self.refresh()

    def legendKeys(self) -> list:
        return list(self._legends)

    def _installViewportOverlays(self) -> None:
        """The progress strip, the preset bar and the hover tip (VI15).

        They are children of the view and sit in the overlay layout's slots.
        The view is a native window, so each is one too, or it would be drawn
        under it.
        """
        from foammesh.view.display_control.viewport_extras import (
            CameraPresetBar, HoverTip, MeasureReadout,
            ProgressStrip)
        from widgets.progress_dialog import localTasks

        self._strip = ProgressStrip(self)
        self._presetBar = CameraPresetBar(self)
        self._hoverTip = HoverTip(self)
        self._measureReadout = MeasureReadout(self)
        for widget in (self._strip, self._presetBar, self._hoverTip,
                       self._measureReadout):
            widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        # Plan 43 LY7. The progress strip stands in the notice strip (top
        # centre), which takes the TOP slot; the preview notice joins it.
        from foammesh.view.display_control.notice_strip import NoticeStrip
        self._noticeStrip = NoticeStrip(self)
        self._noticeStrip.adoptStrip(self._strip)
        self._noticeStrip.install(self)
        self.registerOverlay('camera-presets', Slot.TOP_RIGHT, self._presetBar)
        self.registerOverlay(
            'measure-readout', Slot.BOTTOM_RIGHT, self._measureReadout,
            size=lambda: ((0, 0) if self._measureReadout.isHidden() else (
                self._measureReadout.sizeHint().width(),
                self._measureReadout.sizeHint().height())))
        self._presetBar.measureToggled.connect(self.setMeasureActive)
        self._measureReadout.clearRequested.connect(self.clearMeasure)
        localTasks().changed.connect(self.arrangeOverlays)
        self._presetBar.copyViewRequested.connect(self._copyView)
        self._presetBar.saveViewRequested.connect(self._saveViewAs)
        self.hoverReadout.connect(self._hoverTip.show_at)
        self._installResourceOverlay()

    def _installResourceOverlay(self) -> None:
        """The CPU / RAM panel (VI13): a Qt widget, off until asked for."""
        from foammesh.view.display_control.resource_overlay import (
            ResourceOverlay)

        def gpuMemory():
            return (self._glInfo or {}).get('gpu_memory_bytes')

        self._resource = ResourceOverlay(self, gpu=gpuMemory)
        self._resource.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.registerOverlay(
            'resource-usage', Slot.TOP_RIGHT, self._resource,
            size=lambda: (0, 0) if self._resource.isHidden() else (
                self._resource.sizeHint().width(),
                self._resource.sizeHint().height()))

    def resourceOverlay(self):
        return self._resource

    def noticeStrip(self):
        """Plan 43 LY7. The Viewport notice strip (top centre)."""
        return self._noticeStrip

    def presetBar(self):
        """Measure and the view-capture menu (the top bar takes them, LY6)."""
        return self._presetBar

    def measureReadout(self):
        """The Measure tool's answer and Clear (the bottom bar takes it, LY6)."""
        return self._measureReadout

    def setResourceOverlayVisible(self, on: bool) -> None:
        """View > Show resource usage."""
        self._resource.setShown(on)
        self.arrangeOverlays()

    def resourceOverlayVisible(self) -> bool:
        return self._resource.isShown()

    # -- the Measure tool (VI17, I8) ---------------------------------------- #

    def isMeasuring(self) -> bool:
        return self._measureState.active

    def measureLines(self) -> list:
        return self._measureState.lines()

    def setMeasureActive(self, on: bool) -> None:
        """Clicks measure instead of picking a part while this is on."""
        self._measureState.setActive(on)
        button = self._presetBar.measureButton()
        if button.isChecked() != bool(on):
            button.blockSignals(True)
            button.setChecked(bool(on))
            button.blockSignals(False)
        self._showMeasure()

    def clearMeasure(self) -> None:
        self._measureState.clear()
        self._showMeasure()

    def measureBetween(self, start, end) -> None:
        """DP-2745. Measure two given points, as two clicks would: Measure
        turns on, the line and its length are drawn and the readout shows."""
        self._measureState.setActive(True)
        self._measureState.clear()
        self._measureState.click(start)
        self._measureState.click(end)
        self.setMeasureActive(True)

    def measureAt(self, x, y) -> bool:
        """Measure what is at a display point; False if nothing is there.

        DP-2747. A click that found nothing used to return False and say
        nothing, so a measurement that never got its second point looked
        like a tool that does not work. The readout now says the click
        missed and what to do instead.
        """
        picker = self._cellPick(x, y)
        if picker is None:
            self._measureState.miss()
            self._showMeasure()
            return False
        try:
            info = measure.describe_cell(picker.GetDataSet(),
                                         int(picker.GetCellId()))
        except Exception:                                   # noqa: BLE001
            info = {}
        self._measureState.click(picker.GetPickPosition(), info)
        self._showMeasure()
        return True

    def _showMeasure(self) -> None:
        """Draw the line between the two points, labelled, and the readout."""
        from vtkmodules.vtkFiltersSources import vtkLineSource
        from vtkmodules.vtkRenderingCore import (
            vtkBillboardTextActor3D, vtkPolyDataMapper)

        layer = self._measureOverlay()
        for actor in self._measureActors:
            _quietly(layer.RemoveViewProp, actor)
        self._measureActors = []
        state = self._measureState
        if state.active and state.points:
            # DP-2747. Each picked point is marked where it landed, the first
            # one at once, so the click visibly arrived before the second.
            self._measureActors.append(self._measurePointsActor(state.points))
        if len(state.points) == 2:
            line = vtkLineSource()
            line.SetPoint1(*state.points[0])
            line.SetPoint2(*state.points[1])
            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(line.GetOutputPort())
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1.0, 0.8, 0.1)
            actor.GetProperty().SetLineWidth(3.0)
            actor.SetPickable(False)
            layer.AddActor(actor)
            label = vtkBillboardTextActor3D()
            label.SetInput(measure.length_text(state.distance()))
            middle = [(a + b) / 2.0 for a, b in zip(*state.points)]
            label.SetPosition(*middle)
            label.GetTextProperty().SetFontSize(16)
            label.GetTextProperty().SetColor(1.0, 0.8, 0.1)
            label.GetTextProperty().SetBold(True)
            label.SetPickable(False)
            layer.AddActor(label)
            self._measureActors += [actor, label]
        self._measureReadout.show_lines(state.lines() if state.active else [])
        self.arrangeOverlays()
        self.refresh()

    def _measurePointsActor(self, points):
        """The measured points, drawn as dots over the model."""
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkPolyDataMapper

        coordinates = vtkPoints()
        vertices = vtkCellArray()
        for point in points:
            vertices.InsertNextCell(1)
            vertices.InsertCellPoint(coordinates.InsertNextPoint(*point))
        data = vtkPolyData()
        data.SetPoints(coordinates)
        data.SetVerts(vertices)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('measure:points')
        prop = actor.GetProperty()
        prop.SetColor(1.0, 0.8, 0.1)
        # Square points: MEASURED on this machine's AMD driver, points drawn
        # as spheres in the overlay layer put no pixel on screen.
        prop.SetPointSize(9.0)
        prop.LightingOff()
        actor.SetPickable(False)
        self._measureOverlay().AddActor(actor)
        return actor

    def _measureOverlay(self):
        """The layer the measurement is drawn in, over the model.

        DP-2747. MEASURED live 2026-10-20: with the marks, line and label in
        the scene's own renderer, two clicks on the elbow measured 0.3559 m
        and nothing showed -- the points lie *on* the surface (a depth tie
        the volume's depth bias loses or wins by chance), and the straight
        line between two surface points, with its label at the midpoint,
        runs *inside* the solid. Drawn in a layer of its own over the scene,
        with the scene's camera (as the origin triad is), the measurement is
        always in front.
        """
        layer = self.__dict__.get('_measureLayer')
        if layer is None:
            layer = vtkRenderer()
            layer.SetLayer(1)
            layer.InteractiveOff()
            layer.SetActiveCamera(self._renderer.GetActiveCamera())
            self._measureLayer = layer
        self._attachMeasureLayer(self.renderWindow())
        return layer

    def _attachMeasureLayer(self, window) -> None:
        layer = self.__dict__.get('_measureLayer')
        if layer is None or window is None:
            return
        _quietly(window.SetNumberOfLayers, max(2, window.GetNumberOfLayers()))
        if not window.HasRenderer(layer):
            window.AddRenderer(layer)
        # A reset view gets a new camera; the layer follows it.
        if layer.GetActiveCamera() is not self._renderer.GetActiveCamera():
            layer.SetActiveCamera(self._renderer.GetActiveCamera())

    def _copyView(self) -> None:
        from foammesh.view.main_window.view_capture import copyView
        copyView(self)

    def _saveViewAs(self) -> None:
        from foammesh.view.main_window.view_capture import saveViewAs
        saveViewAs(self, self.window())

    def registerOverlay(self, key: str, slot: Slot, widget, size=None) -> None:
        """Place a Qt widget in a slot; it is moved and sized by the layout."""
        wanted = size if size is not None else (
            lambda: (widget.sizeHint().width(), widget.sizeHint().height()))
        self._overlayLayout.register(
            key, slot, wanted,
            lambda rect, widget=widget: widget.setGeometry(
                rect.x, rect.y, rect.w, rect.h))
        self.arrangeOverlays()

    def unregisterOverlay(self, key: str) -> None:
        self._overlayLayout.unregister(key)
        self.arrangeOverlays()

    def arrangeOverlays(self) -> dict:
        if self.width() <= 0 or self.height() <= 0:
            return {}
        return self._overlayLayout.arrange(self.width(), self.height())

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
        if not self._hoverEnabled or self._interactionInFlight:
            return
        self._hoverTimer.start()

    def setHoverPickEnabled(self, enabled: bool):
        self._hoverEnabled = bool(enabled)
        if not self._hoverEnabled:
            self._hoverTimer.stop()
            if self._hoveredName:
                self._hoveredName = ''
                self.actorHovered.emit('')

    def _hoverPickSuppressed(self) -> bool:
        """Plan 39 VI3 (F1e). No pick render while a frame is in flight.

        A pick draws the scene once more. During a drag, while the reduced
        copies stand in for the parts, or inside another draw, that is a
        frame the user would wait for and learn nothing from.
        """
        return (self._interactionInFlight or self._inRender
                or self._interactionDetail.active()
                or self._hoverPickCostsAFrame())

    def _hoverPickCostsAFrame(self) -> bool:
        """Plan 39 VI14d. A prop pick draws the scene in selection mode
        (MEASURED 110 ms on the 6.6 M-cell impeller) on the GUI thread, so
        not while a load or cut holds the view: the dwell lands on a scene
        that is being replaced. Once released, hover picks again."""
        return bool(isRenderingHold())

    def _hoverPick(self):
        if self._hoverPickSuppressed():
            self.hoverPicksSkipped += 1
            return
        interactor = self._widget._Iren
        x, y = interactor.GetEventPosition()
        # VI14 (LIVEV) named the part by the pick that reads its cell; since
        # DP-2713 / DP-2749 that one CPU pick is the only pick, and the
        # readout is built from it rather than casting the ray again.
        picker = self._cellPick(x, y)
        actor = self._resolvedActor(picker, x, y)
        name = actor.GetObjectName() if actor is not None else ''
        if name != self._hoveredName:
            self._hoveredName = name
            self.actorHovered.emit(name)
        self._hoverReadoutAt(x, y, name, picker=picker)

    def setHoverReadoutEnabled(self, enabled: bool) -> None:
        self._readoutEnabled = bool(enabled)
        if not self._readoutEnabled:
            self._clearReadout()

    def hoverReadoutEnabled(self) -> bool:
        return self._readoutEnabled

    def setHoverInfoProvider(self, provider) -> None:
        """``provider(name)`` -> ``{'type', 'deviation', 'unit'}`` for a patch."""
        self._hoverInfo = provider

    def setHoverVolumeProvider(self, provider) -> None:
        """``provider(name)`` -> the whole volume dataset a picked actor was
        cut from, or ``None``. Lets a cut's surface answer with the values of
        the volume cell it stands for."""
        self._hoverVolume = provider

    def hoverReadoutText(self) -> str:
        return self._readoutText

    def _clearReadout(self) -> None:
        if self._readoutText:
            self._readoutText = ''
            self.hoverReadout.emit('', 0, 0)

    def readoutAt(self, x, y, name: str = '', picker=False) -> list:
        """The readout's lines for the cell or face at a display point.

        ``picker`` is a pick already made at that point (``None`` when it
        found nothing); left out, the point is picked here."""
        if picker is False:
            picker = self._cellPick(x, y)
        if picker is None:
            return []
        info = {}
        if self._hoverInfo is not None and name:
            try:
                info = self._hoverInfo(name) or {}
            except Exception:                               # noqa: BLE001
                info = {}
        volume = None
        if self._hoverVolume is not None and name:
            try:
                volume = self._hoverVolume(name)
            except Exception:                               # noqa: BLE001
                volume = None
        return hover_readout.describe(
            picker.GetDataSet(), int(picker.GetCellId()), name, volume=volume,
            patch_type=str(info.get('type', '') or ''),
            deviation_array=info.get('deviation'),
            deviation_unit=str(info.get('unit', '') or ''))

    def _hoverReadoutAt(self, x, y, name: str, picker=False) -> None:
        """Show the cell or face under the cursor; off with the pick itself."""
        if not self._readoutEnabled:
            return
        # Off above the LOD threshold: a scene that needs reduced copies to
        # turn is not one to ray-cast on every dwell.
        if self._interactionDetail.wanted():
            self._clearReadout()
            return
        lines = self.readoutAt(x, y, name, picker=picker)
        if not lines:
            self._clearReadout()
            return
        text = '\n'.join(lines)
        dpr = self.devicePixelRatioF() or 1.0
        self._readoutText = text
        self.hoverReadout.emit(text, int(x / dpr), int(self.height() - y / dpr))

    def _cameraInteractionStarted(self, obj, event):
        self._interactionInFlight = True
        self._hoverTimer.stop()
        self._clearReadout()
        # The first drag after a programmatic framing is exactly the view a
        # user wants "back" to return to; later drags would only flood history.
        if not self._cameraMovedByUser:
            self.rememberView()
        self._cameraMovedByUser = True

    def _mouseClicked(self, x, y, controlKeyPressed, shiftKeyPressed=False):
        if self._measureState.active:
            self.measureAt(x, y)
            return
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
        self._logoRepresentation.GetImageProperty().SetOpacity(
            watermark_opacity(self._themeTokens)
            if self._themeTokens is not None else WATERMARK_OPACITY_DARK)
        self._applyWatermarkShown()
        self._logoWidget.On()
        self.refresh()

    def isWatermarkShown(self) -> bool:
        """Plan 44 F1 (MF15, decision Q9): whether the watermark is drawn."""
        return bool(getattr(self, '_watermarkShown', True))

    def setWatermarkShown(self, shown: bool) -> None:
        """Show or hide the watermark (View > Overlays and rendering).

        Only the logo's visibility changes: the widget stays on and keeps its
        place in the clear area's corner, so showing it again needs no
        re-layout.
        """
        shown = bool(shown)
        if shown == self.isWatermarkShown():
            return
        self._watermarkShown = shown
        self._applyWatermarkShown()
        self.refresh()
        self._announce(self.watermarkShownChanged, shown)

    def _applyWatermarkShown(self) -> None:
        set_visibility = getattr(self._logoRepresentation, 'SetVisibility', None)
        if set_visibility is not None:
            set_visibility(self.isWatermarkShown())

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
        from foammesh.view.main_window.clear_area import Insets, clear_rect
        clear = clear_rect(self.width(), self.height(),
                           Insets(*self.clearInsets()))
        position, size = watermark_geometry(
            round(self.width() * dpr), round(self.height() * dpr), dpr, clear)
        self._logoRepresentation.SetPosition(*position)
        self._logoRepresentation.SetPosition2(*size)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._holdCover is not None and not self._holdCover.isHidden():
            self._holdCover.setGeometry(self.rect())
        self._updateLogoLayout()
        self._logoWidget.On()
        self._layoutCorner()
        self.arrangeOverlays()
        # F-44. The label budget is a function of the pixel extent, so it is
        # re-spent whenever the extent changes -- docking the pane is the
        # commonest way to arrive at a viewport too narrow for six numbers.
        self._applyCubeAxesPolicy()
        self._applyClearInsets()

        if self._refitOnResize and not self._cameraMovedByUser:
            # Re-fit only while the framing is still the one this widget chose.
            self._refitOnResize = False
            self.fitCamera()
            return

        self.refresh()
