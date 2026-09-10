#!/usr/bin/env python
# -*- coding: utf-8 -*-

# A simple script to demonstrate the vtkCutter function

import math
import platform
import time
from typing import Optional

# noinspection PyUnresolvedReferences
import vtkmodules.vtkInteractionStyle
# noinspection PyUnresolvedReferences
import vtkmodules.vtkRenderingOpenGL2
from PySide6.QtCore import QTimer, Qt, Signal, QObject
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget, QFileDialog, QVBoxLayout
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.vtkCommonCore import vtkCommand
# load implementations for rendering and interaction factory classes
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkInteractionWidgets import vtkLogoRepresentation, vtkLogoWidget, vtkOrientationMarkerWidget
from vtkmodules.vtkIOImage import vtkPNGReader, vtkPNGWriter
from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor, vtkCubeAxesActor
from vtkmodules.vtkRenderingCore import vtkActor, vtkRenderer, vtkPropPicker, vtkLightKit, vtkProp

from foammesh.support.vtk_threads import isRenderingHold

from foammesh.view.theming.vtk_theme import apply_vtk_theme
from app_properties import meshAppProperties
from foammesh.core.branding import watermark_geometry


RENDER_DELAY_TIME = 200
REPAINT_SUPPRESS_TIME = 100

#: Multisampling for the opaque scene. Depth peeling cannot coexist with it on
#: the OpenGL2 backend, so the viewport trades one for the other rather than
#: pretending both are on.
MULTI_SAMPLES = 8
DEPTH_PEELS = 8
DEPTH_PEEL_OCCLUSION = 0.05

#: How many camera positions the back/forward history remembers.
VIEW_HISTORY_LIMIT = 24

#: Dwell before the part under the cursor is named. Long enough that crossing
#: the mesh does not make the readout flicker, short enough to feel immediate.
HOVER_DWELL_TIME = 180

#: Frames per second VTK aims for while the camera is moving, versus at rest.
#: Asking for 15 during interaction is what lets LOD actors shed detail.
INTERACTIVE_UPDATE_RATE = 15.0
STILL_UPDATE_RATE = 0.0001

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


class RenderWindowInteractor(QVTKRenderWindowInteractor):
    def __init__(self, parent=None, **kw):
        self._delayTimer = QTimer()
        self._delayTimer.setInterval(RENDER_DELAY_TIME)
        self._delayTimer.setSingleShot(True)
        self._delayTimer.timeout.connect(self._timeout)

        self._suppressTimer = QTimer()
        self._suppressTimer.setInterval(REPAINT_SUPPRESS_TIME)
        self._suppressTimer.setSingleShot(True)

        super().__init__(parent=parent, **kw)

    def Finalize(self):
        if self._RenderWindow is not None:
            self._RenderWindow.Finalize()
            self._RenderWindow = None

    def paintEvent(self, ev):
        if isRenderingHold():
            self._delayTimer.start()
            return

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


class RenderingWidget(QWidget):
    actorPicked = Signal(vtkActor, bool, bool)
    viewClosed = Signal()
    #: The name of the part under the cursor, or '' when the cursor is over
    #: nothing. The picture is expected to answer "what is this" on its own.
    actorHovered = Signal(str)
    #: True while the viewport is showing less than the full mesh to stay
    #: responsive. Nothing may drop detail without saying so.
    detailReduced = Signal(bool)

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        self._dialog: Optional[QFileDialog] = None

        self._originAxes: Optional[vtkOrientationMarkerWidget] = None
        self._originAxesActor: Optional[vtkAxesActor] = None
        self._cubeAxesActor: Optional[vtkCubeAxesActor] = None
        self._themeTokens = None

        self._actorPicker = vtkPropPicker()

        self._style = vtkInteractorStyleTrackballCamera()
        self._widget = RenderWindowInteractor(self)
        self._widget.GetRenderWindow().SetMultiSamples(MULTI_SAMPLES)
        self._widget.SetInteractorStyle(self._style)

        self._depthPeeling = False
        self._viewHistory = []
        self._viewFuture = []
        self._cameraMovedByUser = False
        self._refitOnResize = False

        self._ambientOcclusion = False
        self._fxaa = False
        self._interactiveDecimation = False

        self._hoverEnabled = True
        self._hoveredName = ''
        self._hoverTimer = QTimer(self)
        self._hoverTimer.setInterval(HOVER_DWELL_TIME)
        self._hoverTimer.setSingleShot(True)
        self._hoverTimer.timeout.connect(self._hoverPick)

        self._renderer = vtkRenderer()
        self._widget.GetRenderWindow().AddRenderer(self._renderer)
        # self._style.SetDefaultRenderer(self._renderer)

        self._widget.Initialize()
        self._widget.Start()

        self._renderer.GradientBackgroundOn()
        self._renderer.SetBackground(0.82, 0.82, 0.82)
        self._renderer.SetBackground2(0.22, 0.24, 0.33)

        self._lightKit = vtkLightKit()
        self._lightKit.AddLightsToRenderer(self._renderer)

        self._logoWidget = vtkLogoWidget()
        self._logoRepresentation = None
        self._logoReader = None
        self._logoTheme = None
        self._showLogo()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)

        self._originalMouseObserver = MouseHandler(self._style)
        self._mouseObserver = self._originalMouseObserver

        self._mouseObserver.mouseClicked.connect(self._mouseClicked)

        # To pick actors
        self._style.AddObserver(vtkCommand.LeftButtonPressEvent, self._leftButtonPressEvent)
        self._style.AddObserver(vtkCommand.LeftButtonReleaseEvent, self._leftButtonReleaseEvent)

        self._style.AddObserver(vtkCommand.MouseMoveEvent, self._mouseMoveEvent)
        self._style.AddObserver(
            vtkCommand.StartInteractionEvent, self._cameraInteractionStarted)

        # Every embedded/temporary viewport participates in live theme changes,
        # not only the main-window instance.
        from foammesh.app import app
        if app.themeManager is not None:
            if app.themeManager.tokens is not None:
                self.applyTheme(app.themeManager.tokens)
            app.themeManager.themeChanged.connect(self._themeChanged)

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

        # A mesh fitted to a narrow viewport used to stay small when the window
        # was widened, because resizeEvent never re-fitted. It does now -- but
        # only until the user moves the camera, after which the framing is
        # theirs and re-fitting would undo their work.
        self._cameraMovedByUser = False
        self._refitOnResize = True
        self._widget.Render()

    def close(self):
        from foammesh.app import app
        if app.themeManager is not None:
            try:
                app.themeManager.themeChanged.disconnect(self._themeChanged)
            except (RuntimeError, TypeError):
                pass
        self.viewClosed.emit()
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

        self._turnCamera(orientation, up)
        self._widget.Render()

    def rollCamera(self):
        self._renderer.GetActiveCamera().Roll(-90)
        self._widget.Render()

    def setViewPreset(self, preset: str):
        """Set one of the six axis views or a stable isometric view."""
        presets = {
            '+x': ((-1, 0, 0), (0, 0, 1)),
            '-x': ((1, 0, 0), (0, 0, 1)),
            '+y': ((0, -1, 0), (0, 0, 1)),
            '-y': ((0, 1, 0), (0, 0, 1)),
            '+z': ((0, 0, -1), (0, 1, 0)),
            '-z': ((0, 0, 1), (0, 1, 0)),
            'isometric': ((-1, -1, -1), (0, 0, 1)),
        }
        try:
            orientation, up = presets[preset.lower()]
        except KeyError as error:
            raise ValueError(f'unknown view preset: {preset}') from error
        self.rememberView()
        length = math.sqrt(sum(value * value for value in orientation))
        self._turnCamera(
            tuple(value / length for value in orientation), up)
        self.fitCamera()

    def saveScreenshot(self, path, *, scale: int = 2):
        """Capture the rendered viewport as a PNG at 1x, 2x or 4x resolution."""
        if int(scale) not in {1, 2, 4}:
            raise ValueError('screenshot scale must be 1, 2 or 4')
        self._widget.GetRenderWindow().Render()
        capture = vtkWindowToImageFilter()
        capture.SetInput(self._widget.GetRenderWindow())
        capture.SetScale(int(scale))
        capture.ReadFrontBufferOff()
        capture.Update()
        writer = vtkPNGWriter()
        writer.SetFileName(str(path))
        writer.SetInputConnection(capture.GetOutputPort())
        writer.Write()

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
        enabled = bool(enabled)
        if enabled == self._depthPeeling:
            return

        window = self._widget.GetRenderWindow()
        if enabled:
            window.SetAlphaBitPlanes(1)
            window.SetMultiSamples(0)
            self._renderer.SetUseDepthPeeling(True)
            self._renderer.SetMaximumNumberOfPeels(DEPTH_PEELS)
            self._renderer.SetOcclusionRatio(DEPTH_PEEL_OCCLUSION)
        else:
            self._renderer.SetUseDepthPeeling(False)
            window.SetMultiSamples(MULTI_SAMPLES)

        self._depthPeeling = enabled
        self.refresh()

    def usesOrderIndependentTransparency(self) -> bool:
        return self._depthPeeling

    # -- WP4.2/4.3: effects that cost frames, so nobody pays without asking -- #

    def setAmbientOcclusion(self, enabled: bool) -> bool:
        """Screen-space cavity shading.

        On a mesh with internal passages this is the difference between reading
        depth and guessing it -- and it is not free, which is why it is a
        preference and not a default. Returns whether the renderer took it.
        """
        renderer = self._renderer
        if not hasattr(renderer, 'SetUseSSAO'):
            return False
        enabled = bool(enabled)
        renderer.SetUseSSAO(enabled)
        if enabled:
            radius = self.modelExtent() * 0.05 or 0.1
            renderer.SetSSAORadius(radius)
            renderer.SetSSAOBias(radius * 0.01)
            renderer.SetSSAOKernelSize(32)
            renderer.SetSSAOBlur(True)
        self._ambientOcclusion = enabled
        self.refresh()
        return True

    def usesAmbientOcclusion(self) -> bool:
        return self._ambientOcclusion

    def setFastAntiAliasing(self, enabled: bool) -> bool:
        """FXAA on top of MSAA, for thin lines like the feature outline."""
        renderer = self._renderer
        if not hasattr(renderer, 'SetUseFXAA'):
            return False
        renderer.SetUseFXAA(bool(enabled))
        self._fxaa = bool(enabled)
        self.refresh()
        return True

    def usesFastAntiAliasing(self) -> bool:
        return self._fxaa

    # -- WP6: measure before optimising, and degrade out loud ---------------- #

    def measureFrameTime(self, samples: int = 5) -> float:
        """Mean seconds per frame over ``samples`` forced renders.

        WP6's rule is that no optimisation lands before this number exists, and
        WP4's is that no effect defaults on until its cost is recorded. Both
        need one honest measurement rather than an impression.
        """
        window = self._widget.GetRenderWindow()
        window.Render()  # warm the pipeline; the first frame is not typical
        samples = max(1, samples)
        start = time.perf_counter()
        for _ in range(samples):
            window.Render()
        return (time.perf_counter() - start) / samples

    def setInteractiveDecimation(self, enabled: bool) -> None:
        """Drop detail *while the camera moves*, and say so when it happens.

        A silently decimated mesh that a user reads as the real one is the same
        class of defect as a verdict that grades an unmeasured mesh a pass, so
        this emits rather than acting invisibly.
        """
        enabled = bool(enabled)
        if enabled == self._interactiveDecimation:
            return
        self._interactiveDecimation = enabled
        window = self._widget.GetRenderWindow()
        window.SetStillUpdateRate(STILL_UPDATE_RATE)
        # The interactor is what raises the desired rate during a drag and
        # drops it again at rest; setting it on the window alone would either
        # decimate every frame or none. `vtkQuadricLODActor` reads the rate the
        # window is currently asking for and picks its representation from it.
        interactor = self._widget._Iren
        if interactor is not None:
            interactor.SetDesiredUpdateRate(
                INTERACTIVE_UPDATE_RATE if enabled else STILL_UPDATE_RATE)
            interactor.SetStillUpdateRate(STILL_UPDATE_RATE)
        window.SetDesiredUpdateRate(
            INTERACTIVE_UPDATE_RATE if enabled else STILL_UPDATE_RATE)
        self.detailReduced.emit(enabled)

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
        state = self._cameraState()
        if self._viewHistory and self._viewHistory[-1] == state:
            return
        self._viewHistory.append(state)
        del self._viewHistory[:-VIEW_HISTORY_LIMIT]
        self._viewFuture.clear()

    def canGoBack(self) -> bool:
        return len(self._viewHistory) > 0

    def canGoForward(self) -> bool:
        return len(self._viewFuture) > 0

    def goBack(self):
        if not self._viewHistory:
            return False
        self._viewFuture.append(self._cameraState())
        self._restoreCameraState(self._viewHistory.pop())
        return True

    def goForward(self):
        if not self._viewFuture:
            return False
        self._viewHistory.append(self._cameraState())
        self._restoreCameraState(self._viewFuture.pop())
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
        """Fit the camera to a subset of the scene rather than all of it."""
        bounds = None
        for prop in props:
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
        self._renderer.SetBackground(r, g, b)

    def setBackground2(self, r, g, b):
        self._renderer.SetBackground2(r, g, b)

    def applyTheme(self, tokens):
        """Apply semantic viewport tokens supplied by the runtime theme manager."""
        self._themeTokens = tokens
        self._loadLogo(tokens.name)
        apply_vtk_theme(self._renderer, tokens, cube_axes=self._cubeAxesActor,
                        origin_axes=self._originAxesActor, logo=self._logoRepresentation)
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
        if self._themeTokens is not None:
            apply_vtk_theme(self._renderer, self._themeTokens, cube_axes=self._cubeAxesActor,
                            origin_axes=self._originAxesActor, logo=self._logoRepresentation)

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

    def _hideCubeAxes(self):
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
