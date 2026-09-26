#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Drive the section planes from the panel and from the viewport handles.

Two faults kept this tool from working on the meshes people most often open:

* ``show()`` read ``geometryManager.getBounds()``, which is ``None`` for a case
  opened with a mesh and no imported geometry, and handed it straight to
  ``PlaneWidget.setBounds`` -- which calls ``.toTuple()`` on it. The mesh bounds
  were right there and correct. The scene's bounds are now the union of both.
* only one ``PlaneWidget`` existed, so with more than one plane raised only one
  could be seen or moved. Each plane owns a handle now.
"""

from PySide6.QtCore import QObject, Signal
from vtkmodules.vtkCommonDataModel import vtkPlane

from foammesh.app import app
from foammesh.rendering.actor_info import CutMode
from foammesh.rendering.plane_widget import PlaneWidget
from foammesh.support.mesh import Bounds

from .section_panel import MAX_PLANES, CutType, SectionPanel


#: Above this many displayed cells a live re-cut on every mouse move stops
#: being live and starts being a stutter, so the cut waits for the drag to end
#: -- and the panel says so rather than quietly behaving differently.
LIVE_CELL_BUDGET = 400_000


def sceneBounds():
    """Where the model on screen is, or ``None`` for an empty scene.

    DP-813. This was the union of *every* actor either manager held. A
    manager keeps its actors when it hides them -- ``meshManager.unload()``
    and the geometry that a volume mesh hides both only take the props out
    of the renderer -- so a mesh the user had left, or an STL in millimetres
    hidden under a mesh in metres, still decided where the plane went, and
    the plane was placed somewhere the user could not see anything. Only
    what is drawn counts now; the full union is the fallback for a scene
    with nothing drawn, so the tool still has something to stand on.
    """
    displayed = _mergedBounds('getDisplayedBounds')
    if displayed is not None:
        return displayed
    return _mergedBounds('getBounds')


def _mergedBounds(method):
    window = app.window
    merged = None
    for name in ('geometryManager', 'meshManager'):
        manager = getattr(window, name, None)
        if manager is None:
            continue
        getBounds = (getattr(manager, method, None)
                     or getattr(manager, 'getBounds', None))
        if getBounds is None:
            continue
        bounds = getBounds()
        if bounds is None:
            continue
        if merged is None:
            merged = Bounds(*bounds.toTuple())
        else:
            merged.merge(bounds)

    return merged


def _holds(bounds, point) -> bool:
    """Whether ``point`` lies in ``bounds``, faces included.

    ``Bounds.includes`` is strict, which a flat model -- zero thickness on
    one axis -- can never satisfy.
    """
    size = bounds.size()
    slack = 1e-9 * max(max(size), 1e-30)
    low = bounds.toTuple()[0::2]
    high = bounds.toTuple()[1::2]
    return all(low[axis] - slack <= point[axis] <= high[axis] + slack
               for axis in range(3))


class CutTool(QObject):
    #: Emitted whenever the applied section changes, so anything mirroring the
    #: section (the viewport overlay) can follow without owning the state.
    sectionApplied = Signal()

    def __init__(self, ui):
        super().__init__()

        self._widget = ui.cutTool
        self._header = ui.cutHeader
        self._view = ui.renderingView

        self._panel = SectionPanel(ui.sectionHost)
        ui.sectionHost.layout().addWidget(self._panel)

        self._bounds = None
        self._option = None
        self._planeWidgets = [PlaneWidget(self._view) for _ in range(MAX_PLANES)]
        self._mirrors = []

        self._header.setContents(ui.cut)

        self._connectSignalsSlots()

    # -- public surface ---------------------------------------------------- #

    def panel(self):
        return self._panel

    def option(self):
        return self._option

    def isVisible(self):
        return self._widget.isVisible()

    def addMirror(self, panel: SectionPanel):
        """Let a second panel (the viewport overlay) drive the same section."""
        self._mirrors.append(panel)
        panel.setBounds(self._bounds)
        panel.sectionChanged.connect(self._mirrorChanged)
        panel.gizmosChanged.connect(self._mirrorGizmos)
        panel.viewNormalRequested.connect(self._useViewNormal)

    def isSectionActive(self) -> bool:
        """Whether a section is currently cutting the model.

        The panel already knew; nothing outside it could ask, so the toolbar
        button that raises a section in one press had no way to tell whether
        it was looking at a cut model or a whole one.
        """
        return bool(self._panel.enabledPlanes())

    def clearSection(self):
        """Take every plane down and put the whole model back.

        The same three steps `hide` takes, without hiding the panel: the
        toolbar can turn a section off while the panel stays open, which is
        what a user watching the picture expects a second press to do.
        """
        self._panel.clear()
        self._apply()
        self._handlesOff()

    def hide(self):
        self.clearSection()
        self._widget.hide()

    def show(self):
        self._header.setChecked(False)
        self.updateBounds()
        self._widget.show()

    def updateBounds(self):
        """Re-place the handles against whatever is currently in the scene."""
        bounds = sceneBounds()
        previous = self._bounds
        self._bounds = bounds
        moved = False
        if bounds is not None:
            moved = self._placePlanes(bounds, previous)
        self._panel.setBounds(bounds)
        for panel in self._mirrors:
            panel.setBounds(bounds)
        if bounds is None:
            self._handlesOff()
            return False

        for widget in self._planeWidgets:
            widget.setBounds(bounds)
        if moved:
            self._apply()
        else:
            self._syncGizmos()
        return True

    def _placePlanes(self, bounds, previous) -> bool:
        """Put every plane the scene has left behind back through the model.

        DP-812. Plane origins were set to the model's centre once -- on the
        very first bounds the tool ever saw -- and never again. The first
        scene is often not the model (a region seed, the first of several
        parts), and a second model, a second project or a mesh scaled from
        millimetres to metres never moved them; the toolbar Section then
        raised its plane at the old centre, off the model, where it cut
        everything or nothing. A lowered plane follows the scene whenever
        the scene changes, or whenever it lies off the model (swept out and
        dropped); a raised one is moved only when the scene changed under it
        and left it outside. Returns whether a raised plane moved, because
        the cut on screen is then stale.
        """
        changed = (previous is None
                   or tuple(previous.toTuple()) != tuple(bounds.toTuple()))
        centre = [float(value) for value in bounds.center()]
        moved = False
        for plane in self._panel.planes():
            outside = not _holds(bounds, plane.origin)
            if plane.enabled:
                if changed and outside:
                    plane.origin = list(centre)
                    moved = True
            elif changed or outside:
                plane.origin = list(centre)
        return moved

    def applyTheme(self, tokens):
        for widget in self._planeWidgets:
            widget.applyTheme(tokens)

    # -- wiring ------------------------------------------------------------ #

    def _connectSignalsSlots(self):
        self._panel.sectionChanged.connect(self._apply)
        self._panel.gizmosChanged.connect(self._syncGizmos)
        self._panel.viewNormalRequested.connect(self._useViewNormal)
        self._panel.activePlaneChanged.connect(lambda _index: self._syncGizmos())
        for index, widget in enumerate(self._planeWidgets):
            widget.planeChanged.connect(
                lambda origin, normal, i=index: self._handleMoved(i, origin, normal))
            widget.interactionFinished.connect(self._dragFinished)

    def _mirrorChanged(self):
        source = self.sender()
        self._panel.adoptFrom(source)
        self._apply()

    def _mirrorGizmos(self):
        self._panel.adoptFrom(self.sender())
        self._syncGizmos()

    def _syncPanels(self, source=None):
        for panel in self._mirrors:
            if panel is not source:
                panel.adoptFrom(self._panel)

    def _handlesOff(self):
        for widget in self._planeWidgets:
            widget.off()

    def _syncGizmos(self):
        """Show a handle for every plane the user asked to see one for."""
        wanted = set(self._panel.gizmoIndexes())
        active = self._panel.activeIndex()
        dragAxis = self._panel.dragAxis()
        locked = self._panel.isLocked()
        for index, widget in enumerate(self._planeWidgets):
            plane = self._panel.planes()[index]
            if index in wanted and plane.enabled:
                if self._bounds is not None:
                    widget.setBounds(self._bounds)
                widget.on(plane.normalised())
                widget.setOrigin(plane.origin)
                widget.setDragAxis(dragAxis if index == active else None)
            else:
                widget.off()
            widget.setLocked(locked)
        self._syncPanels()
        self._view.refresh()

    def _handleMoved(self, index, origin, normal):
        self._panel.setPlaneGeometry(index, origin, normal)
        self._syncPanels()
        if self._panel.isLive():
            if self._displayedCells() <= LIVE_CELL_BUDGET:
                self._panel.setDegradedNote('')
                self._apply()
            else:
                self._panel.setDegradedNote(
                    self.tr('Large mesh — cutting when you let go'))

    def _dragFinished(self):
        if self._panel.isLive():
            self._apply()

    def _displayedCells(self):
        manager = getattr(app.window, 'meshManager', None)
        if manager is None:
            return 0
        try:
            return manager.getNumberOfDisplayedCells()
        except Exception:
            return 0

    def _useViewNormal(self):
        """Cut along whatever the user is currently looking down.

        The normal points *away* from the camera, so the half that gets removed
        is the half between the user and the interior. Pointing it the other
        way keeps the near half and leaves the cut face behind the viewer --
        technically a section, and useless.
        """
        camera = self._view.renderer().GetActiveCamera()
        self._panel.setViewNormal(list(camera.GetDirectionOfProjection()))
        self._syncPanels()

    # -- application ------------------------------------------------------- #

    def _vtkPlanes(self):
        planes = []
        for state in self._panel.enabledPlanes():
            plane = vtkPlane()
            plane.SetOrigin(*state.origin)
            plane.SetNormal(*state.normalised())
            planes.append(plane)
        return planes

    def _apply(self):
        cutType = self._panel.cutType()
        mode = CutMode.CRINKLE if self._panel.isCrinkle() else CutMode.SMOOTH
        planes = self._vtkPlanes()

        managers = [getattr(app.window, name, None)
                    for name in ('geometryManager', 'meshManager')]
        for manager in managers:
            if manager is not None and hasattr(manager, 'setCutMode'):
                manager.setCutMode(mode)

        # No planes is not a section of nothing, it is no section. Storing
        # (CLIP, []) left every actor believing it was clipped, which is what
        # Clear looked like it had failed to do.
        if not planes:
            self._option = None
        else:
            self._option = (cutType, planes)
        if cutType == CutType.CLIP:
            for manager in managers:
                if manager is not None:
                    manager.clip(planes)
        else:
            for manager in managers:
                if manager is not None:
                    manager.slice(planes)

        self._syncGizmos()
        self.sectionApplied.emit()
