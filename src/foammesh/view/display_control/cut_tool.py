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
    """Union of the geometry and mesh bounds, or ``None`` for an empty scene."""
    window = app.window
    merged = None
    for name in ('geometryManager', 'meshManager'):
        manager = getattr(window, name, None)
        if manager is None:
            continue
        getBounds = getattr(manager, 'getBounds', None)
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

    def hide(self):
        self._panel.clear()
        self._apply()
        self._handlesOff()
        self._widget.hide()

    def show(self):
        self._header.setChecked(False)
        self.updateBounds()
        self._widget.show()

    def updateBounds(self):
        """Re-place the handles against whatever is currently in the scene."""
        bounds = sceneBounds()
        first = self._bounds is None
        self._bounds = bounds
        self._panel.setBounds(bounds)
        for panel in self._mirrors:
            panel.setBounds(bounds)
        if bounds is None:
            self._handlesOff()
            return False

        for widget in self._planeWidgets:
            widget.setBounds(bounds)
        if first:
            for plane in self._panel.planes():
                plane.origin = list(bounds.center())
            self._panel.setBounds(bounds)
        self._syncGizmos()
        return True

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
