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
from foammesh.rendering.plane_widget import AXIS_NORMAL, PlaneWidget
from foammesh.support.mesh import Bounds

from .section_panel import MAX_PLANES, CutType, SectionPanel, SectionPlaneState


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


class SectionSnapshot:
    """Everything the section controls hold, frozen, to be handed back later.

    Plan 36 RP9. It answers the same questions a `SectionPanel` does, so
    `SectionPanel.adoptFrom` takes it back without a second copy of the
    rules for what a section is.
    """

    def __init__(self, planes, active, cutType, crinkle, live, dragAxis,
                 locked, gizmos):
        self._planes = [SectionPlaneState(plane.enabled, list(plane.origin),
                                          list(plane.normal))
                        for plane in planes]
        self._active = active
        self._cutType = cutType
        self._crinkle = bool(crinkle)
        self._live = bool(live)
        self._dragAxis = dragAxis
        self._locked = bool(locked)
        self._gizmos = list(gizmos)

    @classmethod
    def of(cls, panel):
        return cls(panel.planes(), panel.activeIndex(), panel.cutType(),
                   panel.isCrinkle(), panel.isLive(), panel.dragAxis(),
                   panel.isLocked(), panel.gizmoIndexes())

    def planes(self):
        return self._planes

    def activeIndex(self):
        return self._active

    def cutType(self):
        return self._cutType

    def isCrinkle(self):
        return self._crinkle

    def isLive(self):
        return self._live

    def dragAxis(self):
        return self._dragAxis

    def isLocked(self):
        return self._locked

    def gizmoIndexes(self):
        return list(self._gizmos)

    def key(self):
        """A plain value to compare two snapshots by."""
        return (tuple((plane.enabled, tuple(plane.origin), tuple(plane.normal))
                      for plane in self._planes),
                self._active, self._cutType, self._crinkle, self._live,
                self._dragAxis, self._locked, tuple(self._gizmos))


def nearestAxis(direction) -> int:
    """The world axis (0, 1, 2) *direction* runs along most."""
    return max(range(3), key=lambda axis: abs(direction[axis]))


class CutTool(QObject):
    #: Emitted whenever the applied section changes, so anything mirroring the
    #: section (the viewport overlay) can follow without owning the state.
    sectionApplied = Signal()
    #: Plan 36 RP9. The seed's section plane was pushed to this origin.
    seedSectionPushed = Signal(tuple)
    #: Plan 36 RP9. A push of the seed's section plane was let go.
    seedSectionReleased = Signal()

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
        #: Plan 36 RP9. While a region seed is placed on a section: what the
        #: controls held before (to be given back) and the plane's normal.
        self._seedSection = None

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

    # -- Plan 36 RP9: one plane through a region's seed --------------------- #

    #: The plane the seed is placed on is plane 1; the others are lowered.
    SEED_PLANE = 0

    def viewDirection(self):
        """The direction the camera looks along, or ``None`` with no view."""
        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is None:
            return None
        return tuple(renderer.GetActiveCamera().GetDirectionOfProjection())

    def isSeedSectionActive(self) -> bool:
        return self._seedSection is not None

    def seedSectionPlane(self):
        """``(origin, normal)`` of the seed's plane, or ``None``."""
        if self._seedSection is None:
            return None
        plane = self._panel.planes()[self.SEED_PLANE]
        return tuple(plane.origin), tuple(self._seedSection[1])

    def beginSeedSection(self, point, axis: int) -> bool:
        """Raise one plane through *point*, normal to world *axis*, cutting.

        What the section controls held is kept, and `endSeedSection` gives
        it back exactly. The plane is the only one raised; its handle is
        shown and moves along the normal only, and the lock is the user's
        (a locked plane still takes a Ctrl+drag, DP-735). The normal points
        away from the camera, so the half between the viewer and the seed
        is the half taken away. False, and nothing changes, with nothing on
        screen to cut.
        """
        if self._seedSection is not None:
            self.endSeedSection()
        snapshot = SectionSnapshot.of(self._panel)
        option = self._option
        if self._bounds is None and not self.updateBounds():
            return False
        direction = self.viewDirection() or (0.0, 0.0, 1.0)
        normal = [0.0, 0.0, 0.0]
        normal[axis] = -1.0 if direction[axis] < 0 else 1.0
        # On the model's middle across the plane, so the plane's own origin
        # handle does not sit under the seed's ball.
        origin = [float(value) for value in self._bounds.center()]
        origin[axis] = float(point[axis])
        planes = [SectionPlaneState() for _ in range(MAX_PLANES)]
        planes[self.SEED_PLANE] = SectionPlaneState(True, origin, normal)
        wanted = SectionSnapshot(
            planes, self.SEED_PLANE, CutType.CLIP, False, True, AXIS_NORMAL,
            snapshot.isLocked(), [self.SEED_PLANE])
        self._seedSection = (snapshot, tuple(normal), option)
        self._panel.adoptFrom(wanted)
        self._apply()
        return True

    def moveSeedSection(self, point) -> None:
        """Carry the seed's plane to *point* along its normal (a typed move)."""
        if self._seedSection is None:
            return
        normal = self._seedSection[1]
        axis = nearestAxis(normal)
        plane = self._panel.planes()[self.SEED_PLANE]
        if plane.origin[axis] == float(point[axis]):
            return
        origin = list(plane.origin)
        origin[axis] = float(point[axis])
        self._panel.setPlaneGeometry(self.SEED_PLANE, origin, normal)
        self._apply()

    def endSeedSection(self) -> None:
        """Give the section controls back exactly as they were."""
        if self._seedSection is None:
            return
        snapshot, _normal, option = self._seedSection
        self._seedSection = None
        self._panel.adoptFrom(snapshot)
        self._panel.setDegradedNote('')
        # The cut that was on screen, not a re-reading of the controls: a
        # section edited with Live off and not yet applied stays unapplied.
        if option is None:
            self._apply((snapshot.cutType(), snapshot.isCrinkle(), []))
        else:
            self._apply((option[0], snapshot.isCrinkle(), option[1]))

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
        seedPlane = (self._seedSection is not None
                     and index == self.SEED_PLANE)
        if seedPlane:
            # RP9. The seed's plane is pushed, never turned.
            normal = self._seedSection[1]
            self._planeWidgets[index].setNormal(normal)
        self._panel.setPlaneGeometry(index, origin, normal)
        if seedPlane:
            self.seedSectionPushed.emit(tuple(origin))
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
        if self._seedSection is not None:
            self.seedSectionReleased.emit()

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

    def _apply(self, restore=None):
        """Cut with the controls; *restore* is ``(cutType, crinkle, planes)``
        to put back instead (RP9: the cut that was on screen)."""
        if restore is None:
            cutType = self._panel.cutType()
            crinkle = self._panel.isCrinkle()
            planes = self._vtkPlanes()
        else:
            cutType, crinkle, planes = restore
        mode = CutMode.CRINKLE if crinkle else CutMode.SMOOTH

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
