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

import asyncio
import logging
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit,
    QTextEdit, QWidget)
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkCommonDataModel import vtkPlane

from foammesh.app import app
from foammesh.core.section import (
    frozen_layer, named_sections, section_colour, stage_compare)
from foammesh.core.quantities import agreeing, count_text
from foammesh.core.section.cut_cells import (
    projected_ranges, scale_step_from_ranges, tolerance_for)
from foammesh.core.section.modes import SectionMode
from foammesh.core.section.notices import (
    EXPLANATIONS, WORKER_CURRENT, WORKER_PREVIOUS, WORKER_REFUSED,
    sectionNotices)
from foammesh.core.section.section_jobs import (
    SectionJobs, SectionRequest, is_current, manifest_key)
from foammesh.core.section.plane_state import local_basis
from foammesh.rendering.actor_info import (
    BoundaryActor, CutMode, GeometryActor, MeshActor)
from foammesh.rendering.cut_cells import cellLayout
from foammesh.rendering.plane_widget import AXIS_NORMAL, PlaneWidget
from foammesh.rendering.preview_clip import (
    clearPreviewClipping, setPreviewClipping)
from foammesh.rendering.section_colour_map import (
    MapperColouring, cellArray, stampCellIdentity)
from foammesh.rendering.section_fill import (
    CAP_APPROXIMATE, CAP_CLOSED, SectionCapActor, sectionCap)
from foammesh.rendering.worker_section import (
    WorkerSectionActor, fieldString, readSectionSurface)
from foammesh.support.mesh import Bounds
from foammesh.support import resource_budget

from . import section_case_fields as case_fields
from .section_panel import (
    MAX_PLANES, CutType, SectionPanel, SectionPlaneState, _modeFor)

logger = logging.getLogger(__name__)

#: Above this many displayed cells a live re-cut on every mouse move stops
#: being live and starts being a stutter, so the cut waits for the drag to end
#: -- and the panel says so rather than quietly behaving differently.
LIVE_CELL_BUDGET = 400_000
#: Plan 37 UF8. A frame of the renderer-clipped preview (planes handed to the
#: mappers and one render) must fit in this; a slower one falls back to the
#: plane outline for the rest of the drag.
PREVIEW_FRAME_BUDGET = 0.050
#: 2026-10-01. A surface (the boundary preview, a geometry) is re-cut and
#: re-capped on every move only up to this many faces: above it the drag is
#: previewed by the renderer like a large volume. MEASURED offscreen on this
#: machine: the live clip and caps of a 173,400-quad boundary took 113-168 ms
#: a move (about 0.9 us a face), so 40,000 faces keep a move near 35 ms.
LIVE_FACE_BUDGET = 40_000
#: The clip modes a renderer clip can preview; a slice and a cut-cells band
#: are not a half-space, so they keep their last result and move the outline.
PREVIEW_MODES = (SectionMode.CLIP, SectionMode.CLIP_WHOLE_CELLS)

#: Plan 37 UF7. How each section mode is drawn: the cut type the actors are
#: given (display control dispatches on it) and the cut mode they cut with.
MODE_CUTS = {
    SectionMode.SLICE: (CutType.SLICE, CutMode.SMOOTH),
    SectionMode.CUT_CELLS: (CutType.CLIP, CutMode.CUT_CELLS),
    SectionMode.CLIP: (CutType.CLIP, CutMode.SMOOTH),
    SectionMode.CLIP_WHOLE_CELLS: (CutType.CLIP, CutMode.CRINKLE),
}

#: The keys that step the active plane (Shift: a tenth of a step).
NUDGE_KEYS = {Qt.Key.Key_PageUp: 1, Qt.Key.Key_PageDown: -1}

#: Widgets that own their own PgUp/PgDn: a nudge never takes the key from them.
_TEXT_ENTRY = (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)


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
                 locked, gizmos, mode=None):
        self._planes = [plane.copy() if isinstance(plane, SectionPlaneState)
                        else SectionPlaneState(plane.enabled,
                                               list(plane.origin),
                                               list(plane.normal))
                        for plane in planes]
        self._active = active
        self._mode = mode if mode is not None else _modeFor(cutType, crinkle)
        self._cutType = MODE_CUTS[self._mode][0]
        self._crinkle = self._mode is SectionMode.CLIP_WHOLE_CELLS
        self._live = bool(live)
        self._dragAxis = dragAxis
        self._locked = bool(locked)
        self._gizmos = list(gizmos)

    @classmethod
    def of(cls, panel):
        mode = getattr(panel, 'mode', None)
        return cls(panel.planes(), panel.activeIndex(), panel.cutType(),
                   panel.isCrinkle(), panel.isLive(), panel.dragAxis(),
                   panel.isLocked(), panel.gizmoIndexes(),
                   mode() if callable(mode) else None)

    def planes(self):
        return self._planes

    def activeIndex(self):
        return self._active

    def mode(self):
        return self._mode

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
        return (tuple((plane.enabled, tuple(plane.origin), tuple(plane.normal),
                       tuple(plane.reference), plane.keep)
                      for plane in self._planes),
                self._active, self._mode, self._live,
                self._dragAxis, self._locked, tuple(self._gizmos))


class _NotWholeMesh(Exception):
    """No volume on screen is the whole mesh a case field is read for."""


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

    # Plan 37 UF6 state, defaulted on the class as well: a tool assembled
    # without its constructor (a test double) still answers.
    _caps = ()
    _picture = (False, None, ())
    _computing = False
    _surfaceCache = None
    # Plan 37 UF7 likewise.
    _savedCamera = None
    _mode = None
    _stepCache = None
    _shortcuts = ()
    # Plan 37 UF10 likewise.
    _jobs = None
    _generation = 0
    _cellsCase = None
    _workerActor = None
    _workerState = None
    _workerMessage = ''
    _dragging = False
    _enabledCount = 0
    _liveSlow = False
    # Plan 37 UF8 likewise.
    _moving = None
    _previewProps = ()
    _previewSlow = False
    _previewTimes = ()
    # Plan 37 UF9 likewise.
    _colourKey = section_colour.NONE
    _colourRange = None
    _colouring = None
    _colourMapping = {'kind': 'none'}
    _colourReason = ''
    _colourSkipped = 0
    _caseMesh = None
    _caseFields = {}
    _caseReads = frozenset()

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
        #: Plan 37 UF6. The caps on the cut faces, the picture the section
        #: is of (cells or not, the preview's kind, each cap's outcome) and
        #: whether a cut is waiting for a large-mesh drag to end.
        self._caps: list[SectionCapActor] = []
        self._picture = (False, None, [])
        self._computing = False
        self._surfaceCache = None
        #: Plan 37 UF7. The camera *Look along plane* will put back, the mode
        #: the cut on screen was made in, and the cells the step is read from.
        self._savedCamera = None
        self._mode = None
        self._stepCache = None
        self._shortcuts = []
        #: Plan 37 UF10. The section worker: its jobs, the plane generation
        #: (bumped on every change of the cut), the case the user asked to
        #: load cells for (``None``: not asked), the actor its answer is
        #: drawn in, what that answer is (``notices.WORKER_*``) and why a
        #: refusal refused. Whether a plane is being dragged, and how many
        #: planes were cutting (fewer is a plane deleted).
        self._jobs = SectionJobs(self._sectionDelivered)
        self._generation = 0
        self._cellsCase = None
        self._workerActor = None
        self._workerState = None
        self._workerMessage = ''
        self._dragging = False
        self._enabledCount = 0
        #: Plan 37 UF8. What a large drag shows: ``'preview'`` (the preview
        #: clipped by the renderer), ``'outline'`` (the plane moves, the last
        #: result stays) or ``None`` (no large drag). The props the renderer
        #: clips, whether this drag's preview was too slow, and every
        #: admitted preview frame's time in seconds.
        self._moving = None
        self._previewProps = []
        self._previewSlow = False
        self._previewTimes = []
        #: Plan 37 UF9. What the cut is coloured by (a `section_colour`
        #: key), its range (held still during a drag), the mappers coloured
        #: (and what they had), the legend's mapping and why not coloured.
        self._colourKey = section_colour.NONE
        self._colourRange = section_colour.ColourRange()
        self._colouring = MapperColouring()
        self._colourMapping = {'kind': 'none'}
        self._colourReason = ''
        #: The open case's polyMesh folder (probed once per case), the
        #: levels and zones read from it for the local volume, by
        #: ``(folder, key)``, and the reads under way.
        self._caseMesh = None
        self._caseFields = {}
        self._caseReads = set()

        self._header.setContents(ui.cut)

        self._connectSignalsSlots()
        self._panel.setStepProvider(self.cellScaleStep)
        self._installNudgeKeys(self._panel)
        if isinstance(self._view, QWidget):
            self._installNudgeKeys(self._view)
            self._view.destroyed.connect(self._viewDestroyed)

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
        panel.loadVolumeRequested.connect(self._loadVolume)
        panel.loadCellsRequested.connect(self.loadCellsForCut)
        panel.lookAlongRequested.connect(self.lookAlongPlane)
        panel.colourChanged.connect(self.setSectionColour)
        panel.planesChanged.connect(self._mirrorGizmos)
        panel.saveSectionRequested.connect(self.saveNamedSection)
        panel.loadSectionRequested.connect(self.loadNamedSection)
        panel.deleteSectionRequested.connect(self.deleteNamedSection)
        panel.compareRequested.connect(self.requestStageCompare)
        panel.compareCleared.connect(self.clearStageCompare)
        panel.freezeRequested.connect(self.freezeCells)
        panel.unfreezeRequested.connect(self.unfreezeCells)
        panel.setColourKey(self._colourKey)
        panel.adoptFrom(self._panel)
        panel.setCompareStale(self._compareStale)
        self._publishSaved()
        panel.setStepProvider(self.cellScaleStep)
        if not (isinstance(self._view, QWidget)
                and self._view.isAncestorOf(panel)):
            # Inside the viewport the view's keys already reach it.
            self._installNudgeKeys(panel)
        self._publishStatus()

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
        if self._savedCamera is not None:
            self.lookAlongPlane(False)
        self._panel.clear()
        self._apply()
        self._handlesOff()

    def hide(self):
        # A case switch clears the display through here: the camera saved
        # for the old case must not come back over the new one.
        self.forgetLookAlong()
        self.clearSection()
        # UF10. The case's section worker goes with it.
        self.closeSectionJobs()
        # UF11. And a stage comparison of it, and its frozen cells.
        self.clearStageCompare()
        self.unfreezeCells()
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
        self._followSavedCase()
        moved = False
        if bounds is not None:
            moved = self._placePlanes(bounds, previous)
        self._panel.setBounds(bounds)
        for panel in self._mirrors:
            panel.setBounds(bounds)
        self._followFrozen()
        if bounds is None:
            self._handlesOff()
            return False

        for widget in self._planeWidgets:
            widget.setBounds(bounds)
        if moved:
            self._apply()
        else:
            # UF6. The scene changed under a standing cut (the full volume
            # came in, a part was hidden): the caps and what the section
            # says about itself follow it.
            self._refreshCaps()
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
                    plane.placeAt(centre)
                    moved = True
            elif changed or outside:
                plane.placeAt(centre)
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
        planes = [SectionPlaneState()
                  for _ in range(max(len(self._panel.planes()), 1))]
        planes[self.SEED_PLANE] = SectionPlaneState(True, origin, normal)
        wanted = SectionSnapshot(
            planes, self.SEED_PLANE, CutType.CLIP, False, True, AXIS_NORMAL,
            snapshot.isLocked(), [self.SEED_PLANE], SectionMode.CLIP)
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
        self._apply((snapshot.mode(), option[1] if option else []))

    # -- wiring ------------------------------------------------------------ #

    def _connectSignalsSlots(self):
        self._panel.sectionChanged.connect(self._apply)
        self._panel.loadVolumeRequested.connect(self._loadVolume)
        self._panel.loadCellsRequested.connect(self.loadCellsForCut)
        self._panel.gizmosChanged.connect(self._syncGizmos)
        self._panel.viewNormalRequested.connect(self._useViewNormal)
        self._panel.activePlaneChanged.connect(lambda _index: self._syncGizmos())
        self._panel.lookAlongRequested.connect(self.lookAlongPlane)
        self._panel.colourChanged.connect(self.setSectionColour)
        self._panel.planesChanged.connect(self._planesChanged)
        self._panel.saveSectionRequested.connect(self.saveNamedSection)
        self._panel.loadSectionRequested.connect(self.loadNamedSection)
        self._panel.deleteSectionRequested.connect(self.deleteNamedSection)
        self._panel.compareRequested.connect(self.requestStageCompare)
        self._panel.compareCleared.connect(self.clearStageCompare)
        self._panel.freezeRequested.connect(self.freezeCells)
        self._panel.unfreezeRequested.connect(self.unfreezeCells)
        for index, widget in enumerate(self._planeWidgets):
            widget.planeChanged.connect(
                lambda origin, normal, i=index: self._handleMoved(i, origin, normal))
            widget.interactionFinished.connect(self._dragFinished)

    def _planesChanged(self):
        """UF11. A plane was added, named or deleted: every panel and
        handle follows (a deleted cutting plane re-cuts on its own)."""
        self._syncGizmos()

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
        planes = self._panel.planes()
        for index, widget in enumerate(self._planeWidgets):
            plane = planes[index] if index < len(planes) else None
            if plane is not None and index in wanted and plane.enabled:
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
        # 2026-10-01. The step note waits for the release: finding it reads
        # every cell on screen, and did so on every move of the drag.
        self._holdStep(True)
        if seedPlane:
            # RP9. The seed's plane is pushed, never turned.
            normal = self._seedSection[1]
            self._planeWidgets[index].setNormal(normal)
        self._panel.setPlaneGeometry(index, origin, normal)
        self._checkCompareStale()
        if seedPlane:
            self.seedSectionPushed.emit(tuple(origin))
        if not self._dragging:
            # UF9. The legend holds still while the plane moves.
            self._colourRange.freeze()
        self._dragging = True
        self._syncPanels()
        if self._panel.isLive():
            if self._liveFits():
                self._panel.setDegradedNote('')
                started = time.perf_counter()
                self._apply()
                if time.perf_counter() - started > PREVIEW_FRAME_BUDGET:
                    # 2026-10-01. Too slow to re-cut on every move: the
                    # rest of this drag is previewed instead.
                    self._liveSlow = True
            else:
                self._panel.setDegradedNote(
                    self.tr('Large mesh — cutting when you let go'))
                self._computing = True
                # UF8. A clip follows the mouse on the preview, clipped by
                # the renderer; anything else moves the outline only.
                if not self._previewMove():
                    self._moving = 'outline'
                # UF10. The cut moved: what the worker's answer is for
                # changed, though nothing is re-cut until the drag ends.
                self._generation += 1
                self._followWorker()
                self._publishStatus()

    def _dragFinished(self):
        self._dragging = False
        self._previewSlow = False
        self._liveSlow = False
        self._colourRange.thaw()
        if self._panel.isLive():
            self._apply()
        elif self._computing:
            self._endPreview()
            self._computing = False
            self._publishStatus()
            self._recolour()
        if self._seedSection is not None:
            self.seedSectionReleased.emit()
        self._holdStep(False)

    def _holdStep(self, held):
        for panel in [self._panel, *self._mirrors]:
            hold = getattr(panel, 'holdStep', None)
            if hold is None:
                continue
            try:
                hold(held)
            except RuntimeError:
                pass

    def _liveFits(self) -> bool:
        """Whether a move may re-cut what is on screen (2026-10-01): the
        volume cells shown, the surface faces shown, and this drag's live
        frames so far, all within budget."""
        if self._liveSlow:
            return False
        if self._displayedCells() > LIVE_CELL_BUDGET:
            return False
        return self._surfaceFaces() <= LIVE_FACE_BUDGET

    def _surfaceFaces(self) -> int:
        """Faces of the surfaces on screen a cut re-cuts and caps (the
        boundary preview, geometry); a volume is counted in cells."""
        faces = 0
        for name in ('geometryManager', 'meshManager'):
            manager = getattr(app.window, name, None)
            if manager is None:
                continue
            for info in self._shown(manager):
                if isinstance(info, MeshActor):
                    continue
                try:
                    data = info.dataSet()
                    faces += int(data.GetNumberOfCells()) if data else 0
                except Exception:                       # noqa: BLE001
                    continue
        return faces

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

    def _vtkPlanes(self, mode=None):
        states = self._panel.enabledPlanes()
        if mode is SectionMode.CUT_CELLS:
            # Cut cells are the active plane's; the others only mask them.
            active = self._panel.planes()[self._panel.activeIndex()]
            if any(state is active for state in states):
                states = [active] + [state for state in states
                                     if state is not active]
        planes = []
        for state in states:
            plane = vtkPlane()
            plane.SetOrigin(*state.origin)
            plane.SetNormal(*state.normalised())
            planes.append(plane)
        return planes

    def _apply(self, restore=None):
        """Cut with the controls; *restore* is ``(mode, planes)`` to put back
        instead (RP9: the cut that was on screen)."""
        self._checkCompareStale()
        # UF8. The renderer's clip was a stand-in; the filters cut now.
        self._endPreview()
        if restore is None:
            sectionMode = self._panel.mode()
            planes = self._vtkPlanes(sectionMode)
        else:
            sectionMode, planes = restore
        cutType, mode = MODE_CUTS[sectionMode]
        self._mode = sectionMode

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

        self._computing = False
        # UF10. Every applied cut is a new generation of the section.
        self._generation += 1
        self._followWorker()
        self._refreshCaps()
        self._syncGizmos()
        self.sectionApplied.emit()

    # -- Plan 37 UF6: caps and what the section is ------------------------ #

    def caps(self) -> list:
        """The cap actors, shown or not."""
        return list(self._caps)

    @staticmethod
    def _shown(manager):
        """The actors of *manager* that are on screen and take a cut."""
        infos = getattr(manager, '_actorInfos', None) or {}
        isShown = getattr(getattr(app.window, 'displayControl', None),
                          'isShown', None)
        shown = []
        for info in list(infos.values()):
            try:
                if not info.isVisible():
                    continue
                if isShown is not None and not isShown(info):
                    continue
                properties = info.properties()
                if properties is not None and properties.cutEnabled is False:
                    continue
            except Exception:
                continue
            shown.append(info)
        return shown

    def _scenePicture(self):
        """``(hasVolume, previewKind, surfaces)``: *surfaces* are
        ``(polyData, approximate)`` to cap where no cells are cut."""
        window = app.window
        mesh = getattr(window, 'meshManager', None)
        geometry = getattr(window, 'geometryManager', None)
        meshActors = self._shown(mesh) if mesh is not None else []
        hasVolume = any(isinstance(info, MeshActor) for info in meshActors)
        kind = None
        previewNotice = getattr(mesh, 'previewNotice', None)
        if callable(previewNotice):
            try:
                notice = previewNotice()
            except Exception:
                notice = None
            if isinstance(notice, dict):
                kind = notice.get('kind')
        surfaces = []
        if hasVolume:
            return hasVolume, kind, surfaces
        boundary = [info.dataSet() for info in meshActors
                    if isinstance(info, BoundaryActor)
                    and info.dataSet() is not None]
        if boundary:
            # Together the patches are the closed boundary; cut one by one
            # they are open sheets and every cap would be refused.
            surfaces.append((self._merged(boundary), kind == 'decimated'))
        if geometry is not None:
            for info in self._shown(geometry):
                if isinstance(info, GeometryActor) \
                        and info.dataSet() is not None:
                    surfaces.append((info.dataSet(), False))
        return hasVolume, kind, surfaces

    def _merged(self, dataSets):
        key = tuple((id(data), data.GetMTime()) for data in dataSets)
        if self._surfaceCache is not None and self._surfaceCache[0] == key:
            return self._surfaceCache[1]
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData
        append = vtkAppendPolyData()
        for data in dataSets:
            append.AddInputData(data)
        append.Update()
        merged = append.GetOutput()
        self._surfaceCache = (key, merged)
        return merged

    def _capActor(self, index):
        caps = list(self._caps)
        while len(caps) <= index:
            cap = SectionCapActor()
            add = getattr(self._view, 'addActor', None)
            if callable(add):
                add(cap.actor())
            caps.append(cap)
        self._caps = caps
        return caps[index]

    def _refreshCaps(self):
        """Cap every cut face that has no cells behind it, and say so.

        With cells loaded the cut is exact and nothing is capped. Otherwise
        each surface on screen -- the mesh boundary as one, each geometry on
        its own -- is capped on every plane from closed loops only, trimmed
        by the other planes when they clip.
        """
        option = self._option
        planes = list(option[1]) if option else []
        cutType = option[0] if option else None
        results = []
        hasVolume, kind = False, None
        if planes:
            hasVolume, kind, surfaces = self._scenePicture()
            if self._workerState == WORKER_CURRENT:
                # UF10. The worker cut the cells: nothing is capped over them.
                surfaces = []
            if self._mode is SectionMode.CUT_CELLS:
                # Whole cells either side: there is no cut face to cap.
                surfaces = []
            for index, plane in enumerate(planes):
                trims = ([other for position, other in enumerate(planes)
                          if position != index]
                         if cutType == CutType.CLIP else [])
                for surface, approximate in surfaces:
                    results.append(sectionCap(
                        surface, plane.GetOrigin(), plane.GetNormal(),
                        trims, approximate))
        drawn = [result for result in results
                 if result.status in (CAP_CLOSED, CAP_APPROXIMATE)]
        for index, result in enumerate(drawn):
            self._capActor(index).show(result)
        for cap in self._caps[len(drawn):]:
            cap.clear()
        self._picture = (hasVolume, kind,
                         [result.status for result in results])
        self._publishStatus()
        self._recolour()

    def _publishStatus(self):
        hasVolume, kind, caps = self._picture
        running = self._workerRunning()
        notices = (sectionNotices(hasVolume=hasVolume, previewKind=kind,
                                  caps=caps,
                                  computing=self._computing or running,
                                  worker=self._workerState,
                                  moving=self._moving == 'preview',
                                  previous=self._moving == 'outline')
                   if self._option else [])
        texts = [self.tr(notice) for notice in notices]
        explanation = '\n'.join(self.tr(EXPLANATIONS[notice])
                                for notice in notices)
        if self._workerMessage and self._option:
            explanation = '\n'.join(filter(None, [explanation,
                                                  self._workerMessage]))
        offered = bool(self._option) and not hasVolume and kind in (
            'surface', 'decimated')
        # UF10. Cells for the cut: on a boundary-only picture, until the
        # worker's section of this cut is on screen or on its way.
        cellsOffered = (offered and self._workerState != WORKER_CURRENT
                        and not running)
        _case, why = self._workerTarget()
        cells = self._totalCells()
        needed, free = self._volumeMemory(cells)
        allowed = needed <= free
        if allowed:
            reason = self.tr(
                'Read every cell ({0:,}) so the section cuts cells instead '
                'of the boundary. This can take a while and a lot of '
                'memory.').format(cells)
        else:
            reason = self.tr(
                'This mesh has {0:,} cells; reading the full volume needs '
                'about {1} of RAM and {2} is free.').format(
                    cells, resource_budget.format_bytes(needed),
                    resource_budget.format_bytes(free))
        for panel in [self._panel, *self._mirrors]:
            panel.setSectionStatus(texts, explanation)
            panel.setLoadVolume(offered, allowed, reason)
            setLoadCells = getattr(panel, 'setLoadCells', None)
            if setLoadCells is not None:
                setLoadCells(cellsOffered, why is None, why or '')

    def _totalCells(self) -> int:
        manager = getattr(app.window, 'meshManager', None)
        count = getattr(manager, 'getNumberOfCells', None)
        if count is None:
            return 0
        try:
            return int(count() or 0)
        except Exception:
            return 0

    @staticmethod
    def _volumeMemory(cells: int) -> tuple[int, int]:
        """``(needed, free)`` bytes of RAM for "Load full volume" of a mesh
        of ``cells`` cells -- the only thing that decides whether it is
        offered; there is no fixed cell cap."""
        needed = resource_budget.estimate_peak_bytes(
            'mesh.volume', {'cells': int(cells)}).peak_bytes
        try:
            free = resource_budget.quick_snapshot().budget
        except Exception:                               # noqa: BLE001
            free = 0
        return int(needed), int(free)

    def _loadVolume(self):
        """Load full volume: the only way a section reads the volume."""
        manager = getattr(app.window, 'meshManager', None)
        load = getattr(manager, 'loadFullVolume', None)
        if load is None:
            return
        needed, free = self._volumeMemory(self._totalCells())
        if needed > free:
            return
        work = load()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop to run it on (a test, a shutdown).
            close = getattr(work, 'close', None)
            if close is not None:
                close()
            return
        asyncio.ensure_future(work)

    # -- Plan 37 UF8: a large drag ------------------------------------------ #

    def previewTimings(self) -> list:
        """Seconds taken by each renderer-clipped preview frame so far."""
        return list(self._previewTimes)

    def movingState(self):
        """``'preview'``, ``'outline'`` or ``None`` (see `_moving`)."""
        return self._moving

    def _previewCandidates(self):
        """``(infos, props)`` a renderer clip previews: every shown actor and
        the worker's section, all of them bounded (the preview is)."""
        infos = []
        for name in ('geometryManager', 'meshManager'):
            manager = getattr(app.window, name, None)
            if manager is not None:
                infos.extend(self._shown(manager))
        props = [prop for info in infos for prop in info.renderProps()
                 if prop is not None]
        if self._workerActor is not None and self._workerActor.isShown():
            props.append(self._workerActor.actor())
        return infos, props

    def _previewMove(self) -> bool:
        """Clip the preview on screen by the planes, in the renderer.

        ``False`` when it cannot or must not: not a clip, or this drag's
        preview was already too slow. Nothing is read or re-cut: the
        boundary and geometry surfaces are drawn whole (they are bounded by
        the preview) and a loaded volume keeps its cut, clipped further.
        """
        mode = self._panel.mode()
        if mode not in PREVIEW_MODES or self._previewSlow:
            return False
        planes = self._vtkPlanes(mode)
        if not planes:
            return False
        started = time.perf_counter()
        first = self._moving != 'preview'
        if first:
            infos, props = self._previewCandidates()
            for info in infos:
                if not isinstance(info, MeshActor):
                    info.clip([])
            for cap in self._caps:
                cap.clear()
            self._previewProps = props
            self._moving = 'preview'
        setPreviewClipping(self._previewProps, planes)
        self._view.refresh()
        elapsed = time.perf_counter() - started
        if first:
            # The first frame also uncuts the surfaces; it is not a frame
            # of the drag, so it neither counts nor decides.
            return True
        if elapsed > PREVIEW_FRAME_BUDGET:
            # Too slow to follow the mouse: the outline moves instead and
            # the last cut comes back until the handle is let go.
            logger.info('section preview frame took %.0f ms; outline only',
                        elapsed * 1000)
            self._previewSlow = True
            self._endPreview(restore=True)
            return False
        self._previewTimes.append(elapsed)
        return True

    def _endPreview(self, restore=False):
        """Take the renderer's clip off; with *restore* put the last cut
        back (the filters, the caps) as it was before the drag."""
        if self._moving != 'preview':
            self._moving = None
            return
        clearPreviewClipping(self._previewProps)
        self._previewProps = []
        self._moving = None
        if restore and self._option:
            cutType, planes = self._option
            for name in ('geometryManager', 'meshManager'):
                manager = getattr(app.window, name, None)
                if manager is None:
                    continue
                if cutType == CutType.CLIP:
                    manager.clip(planes)
                else:
                    manager.slice(planes)
            self._refreshCaps()
            self._view.refresh()

    # -- Plan 37 UF9: colour the cut ---------------------------------------- #

    def sectionColour(self) -> str:
        return self._colourKey

    def sectionColourMapping(self) -> dict:
        """The legend's mapping (`section_colour.mapping`), with ``source``
        (`section_colour.SOURCE_*`) and ``reason`` (why not coloured)."""
        return dict(self._colourMapping, reason=self._colourReason)

    def setSectionColour(self, key):
        """Colour the cut by *key* (a `section_colour` choice key)."""
        item = section_colour.choice(key)
        self._colourKey = item.key
        self._colourRange.reset()
        for panel in [self._panel, *self._mirrors]:
            panel.setColourKey(item.key)
        if self._cellsCase is not None and self._option and item.worker \
                and not self._workerHas(item):
            # The worker's section has no such array: ask it again.
            self._followWorker()
        if item.key.startswith(section_colour.QUALITY_PREFIX) \
                and self._colourSource() == section_colour.SOURCE_VOLUME:
            self._ensureQuality()
        self._recolour()

    def _workerHas(self, item) -> bool:
        actor = self._workerActor
        return bool(actor is not None and actor.isShown()
                    and cellArray(actor.polyData(), item.array) is not None)

    def _workerUnavailable(self) -> dict:
        actor = self._workerActor
        completion = actor.completion() if actor is not None else None
        manifest = getattr(completion, 'manifest', None) or {}
        return dict(manifest.get('unavailable_arrays') or {})

    def _colourSource(self):
        if not self._option:
            return section_colour.SOURCE_NONE
        actor = self._workerActor
        if actor is not None and actor.isShown():
            return section_colour.SOURCE_WORKER
        if self._picture[0]:
            return section_colour.SOURCE_VOLUME
        return section_colour.SOURCE_BOUNDARY

    def _ensureQuality(self):
        manager = getattr(app.window, 'meshManager', None)
        ensure = getattr(manager, 'ensureQualityFields', None)
        if ensure is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _compute():
            try:
                await ensure()
            except Exception:                              # noqa: BLE001
                logger.debug('quality fields for the section', exc_info=True)
            self._recolour()
            self._view.refresh()

        asyncio.ensure_future(_compute())

    def _volumeActors(self):
        manager = getattr(app.window, 'meshManager', None)
        return [info for info in
                (self._shown(manager) if manager is not None else [])
                if isinstance(info, MeshActor)]

    def _colourTargets(self, source, field=None):
        """``[(prop, mapper, the cut's data)]`` a colour is drawn on.

        With *field* (a `section_case_fields.CaseCellField`, Plan 37 UF9)
        its values are put on each volume first; a volume that is not the
        whole mesh takes none and is left out (`_colourSkipped` counts
        them).
        """
        self._colourSkipped = 0
        if source == section_colour.SOURCE_WORKER:
            actor = self._workerActor
            return [(actor.actor(), actor.mapper(), actor.polyData())]
        if source != section_colour.SOURCE_VOLUME:
            return []
        targets = []
        for info in self._volumeActors():
            # Every piece keeps its cell's id and type through the cut.
            stampCellIdentity(info.dataSet())
            if field is not None and not case_fields.attach(info.dataSet(),
                                                            field):
                self._colourSkipped += 1
                continue
            prop = info.actor()
            mapper = prop.GetMapper()
            mapper.Update()
            targets.append((prop, mapper, mapper.GetInput()))
        return targets

    def _recolour(self):
        """Colour the cut on screen by the choice, or say why not.

        A choice this section has no values for is disabled with its reason;
        an array missing from what is drawn leaves the cut uncoloured and
        says so -- never zeros. Caps are never coloured: they have no cells.
        """
        if self._colouring is None:
            return
        source = self._colourSource()
        unavailable = (self._workerUnavailable()
                       if source == section_colour.SOURCE_WORKER else None)
        states = {item.key: section_colour.availability(
                      item.key, source, unavailable=unavailable)
                  for item in section_colour.CHOICES}
        item = section_colour.choice(self._colourKey)
        field = None
        if source == section_colour.SOURCE_VOLUME:
            # Plan 37 UF9: levels and zones of a local volume are read
            # from the case, bound to the mesh the cells came from.
            for key in case_fields.MEMBERS:
                states[key], read = self._caseFieldState(key,
                                                         key == item.key)
                if key == item.key:
                    field = read
        allowed, reason = states[item.key]
        self._colouring.restore()
        mapped = {'kind': 'none'}
        caseField = case_fields.is_case_field(item.key) \
            and source == section_colour.SOURCE_VOLUME
        if caseField and allowed and field is None:
            # Being read: the choice stays, the cut waits uncoloured.
            reason = self.tr('Reading {0} from the case…').format(
                case_fields.MEMBERS[item.key])
            allowed = False
        if item.key != section_colour.NONE and allowed:
            try:
                targets = self._colourTargets(source, field)
                if caseField and not targets:
                    raise _NotWholeMesh()
                values = []
                for _prop, _mapper, data in targets:
                    array = cellArray(data, item.array)
                    if array is None:
                        raise section_colour.MissingField(item.label)
                    values.append(array)
                names = None
                if item.key == section_colour.CELL_ZONE and field is not None:
                    names = field.names
                elif item.key == section_colour.CELL_ZONE and targets:
                    names = fieldString(targets[0][2], 'cellZoneNames')
                mapped = section_colour.mapping(
                    item.key, np.concatenate(values) if values else None,
                    colourRange=self._colourRange, names=names)
                for prop, mapper, _data in targets:
                    self._colouring.colour(mapper, mapped)
                    prop.Modified()
            except section_colour.MissingField:
                mapped = {'kind': 'none'}
                reason = self.tr('The section has no {0} values yet.').format(
                    item.label)
                if item.key.startswith(section_colour.QUALITY_PREFIX) \
                        and source == section_colour.SOURCE_VOLUME:
                    reason = self.tr('Quality fields are being computed.')
                states[item.key] = (source != section_colour.SOURCE_WORKER,
                                    reason)
            except _NotWholeMesh:
                mapped = {'kind': 'none'}
                reason = self.tr(
                    '{0} is read for the whole mesh, and none of the cells '
                    'on screen are the whole mesh (a cell zone or region '
                    'shown on its own is not coloured).').format(item.label)
        self._colourMapping = dict(mapped, source=source)
        self._colourReason = '' if mapped['kind'] != 'none' else reason
        legend = section_colour.legendText(mapped) or self._colourReason
        tooltip = self._colourReason
        if caseField and mapped['kind'] != 'none' and self._colourSkipped:
            tooltip = self.tr(
                '{0} is read for the whole mesh, so it does not colour {1} '
                'on screen (a cell zone or region shown on its own).').format(
                    item.label, count_text(self._colourSkipped, 'part'))
        for panel in [self._panel, *self._mirrors]:
            panel.setColourChoices(states)
            panel.setColourLegend(legend, tooltip)

    def _caseMeshDir(self):
        """``(constant/polyMesh, None)`` of the open case, or
        ``(None, why not)``; the layout is probed once per case."""
        case, why = self._workerTarget()
        if case is None:
            return None, why
        cached = self._caseMesh
        if cached is None or cached[0] != case:
            meshDir, why = case_fields.mesh_directory(case)
            cached = self._caseMesh = (case, meshDir, why)
        return cached[1], cached[2]

    def _caseFieldState(self, key, wanted):
        """``((allowed, reason), field)`` for case field *key* on the local
        volume. *field* is the read `CaseCellField` when it is this mesh's,
        else ``None``; when *wanted* and not read yet, a read starts."""
        label = section_colour.choice(key).label
        meshDir, why = self._caseMeshDir()
        if meshDir is None:
            return (False, self.tr(
                '{0} is read from the polyMesh of an OpenFOAM case. {1}')
                .format(label, why or '').strip()), None
        try:
            if not case_fields.has_member(meshDir, key):
                return (False, case_fields.absent_reason(key)), None
            revision = case_fields.mesh_revision(meshDir)
        except OSError as error:
            return (False, str(error)), None
        for info in self._volumeActors():
            data = info.dataSet()
            if data is not None and case_fields.bindVolume(
                    data, revision) != revision:
                return (False, self.tr(
                    'The mesh on disk changed after these cells were '
                    'loaded. Reload the mesh to colour by {0}.').format(
                        label)), None
        slot = (str(meshDir), key)
        field = self._caseFields.get(slot)
        if wanted and (field is None or field.revision != revision):
            self._readCaseField(meshDir, key)
            field = self._caseFields.get(slot)
        if field is not None and field.revision == revision:
            if field.status != case_fields.READ:
                return (False, field.reason), None
            return (True, ''), field
        return (True, ''), None

    def _readCaseField(self, meshDir, key):
        """Read *key* from *meshDir*: off the GUI thread when an event
        loop runs (the cut is recoloured when it lands), else now."""
        slot = (str(meshDir), key)
        if slot in self._caseReads:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._caseFields[slot] = case_fields.read_case_field(meshDir,
                                                                 key)
            return
        self._caseReads.add(slot)

        async def _read():
            try:
                field = await asyncio.to_thread(case_fields.read_case_field,
                                                meshDir, key)
            except Exception as error:                     # noqa: BLE001
                logger.debug('case field for the section', exc_info=True)
                field = case_fields.CaseCellField(
                    key, case_fields.REFUSED,
                    case_fields.mesh_revision(meshDir), reason=str(error))
            finally:
                self._caseReads.discard(slot)
            self._caseFields[slot] = field
            if self._colouring is not None:
                self._recolour()
                self._view.refresh()

        asyncio.ensure_future(_read())

    # -- Plan 37 UF10: the section worker ------------------------------- #

    def sectionJobs(self):
        return self._jobs

    def workerSection(self):
        """The actor the worker's section is drawn in (``None``: never)."""
        return self._workerActor

    def workerState(self):
        """``notices.WORKER_*`` for the worker's answer, or ``None``."""
        return self._workerState

    def _workerTarget(self):
        """``(case directory, None)``, or ``(None, why not)``."""
        manager = getattr(app.window, 'meshManager', None)
        if manager is None:
            return None, self.tr('No mesh is open.')
        native = getattr(manager, 'nativePath', None)
        if callable(native) and native() is not None:
            return None, self.tr(
                'This mesh was opened from a mesh file; the section worker '
                'cuts the polyMesh of an OpenFOAM case.')
        caseRoot = getattr(manager, 'caseRoot', None)
        root = caseRoot() if callable(caseRoot) else None
        if not root:
            return None, self.tr('No case mesh is open.')
        return str(Path(root)), None

    # -- Plan 37 UF11: named sections saved with the case ------------------ #

    #: The case the saved-section list was read for, and what it said.
    _savedCase = None
    _savedDocument = None
    _savedCurrent = None

    def _followSavedCase(self):
        case, _why = self._workerTarget()
        if case != self._savedCase:
            self._savedCase = case
            self._savedCurrent = None
            self.refreshSavedSections()

    def refreshSavedSections(self):
        """Read the case's ``foammesh/sections.json`` again and list it."""
        case, why = self._workerTarget()
        self._savedCase = case
        if case is None:
            self._savedDocument = None
            self._publishSaved(why or '')
            return None
        self._savedDocument = named_sections.load_sections(case)
        self._publishSaved()
        return self._savedDocument

    def savedSections(self):
        document = self._savedDocument
        return tuple(document.sections) if document is not None else ()

    def _publishSaved(self, refusal=''):
        document = self._savedDocument
        items = [(section.id, section.name)
                 for section in (document.sections if document else ())]
        readOnly = refusal or (document.reason
                               if document and document.read_only else '')
        note = ''
        if document is not None and not document.read_only:
            note = document.reason
            if document.skipped:
                note = ' '.join(filter(None, [note, self.tr(
                    '{0} could not be read and {1} kept unchanged.').format(
                        count_text(len(document.skipped), 'saved section'),
                        agreeing(len(document.skipped), 'is', 'are'))]))
        for panel in [self._panel, *self._mirrors]:
            panel.setSavedSections(items, readOnly, note, self._savedCurrent)

    def sectionDefinition(self, name, sectionId=None):
        """The section on screen as a `named_sections.SectionDefinition`."""
        panel = self._panel
        planes = tuple(
            named_sections.NamedPlane(panel.planeName(index), plane.state,
                                      tuple(plane.origin), plane.enabled)
            for index, plane in enumerate(panel.planes()))
        fields = dict(active=panel.activeIndex(),
                      mode=self._mode or panel.mode(),
                      units='model', colour=self._colourKey,
                      mask={'caps': True},
                      target=self._targetPolicy(),
                      frozen=self._frozenDefinition())
        if sectionId is None:
            return named_sections.SectionDefinition.new(name, planes,
                                                        **fields)
        return named_sections.SectionDefinition(sectionId, name, planes,
                                                **fields)

    def _targetPolicy(self):
        return named_sections.StagePolicy()

    def _frozenDefinition(self):
        return self._frozen

    def applySectionDefinition(self, section):
        """Put a saved section's planes, mode and colour on screen."""
        planes = []
        for named in section.planes:
            plane = SectionPlaneState(name=named.name)
            plane.setState(named.state, named.pivot)
            plane.enabled = named.enabled
            planes.append(plane)
        gizmos = [index for index, named in enumerate(section.planes)
                  if named.enabled]
        wanted = SectionSnapshot(
            planes, section.active, CutType.CLIP, False,
            self._panel.isLive(), self._panel.dragAxis(),
            self._panel.isLocked(), gizmos, section.mode)
        self._panel.adoptFrom(wanted)
        self.setSectionColour(section.colour)
        self._apply()
        self._syncGizmos()
        # The frozen cells come back bound to the mesh they were frozen on
        # -- drawn from it if it is still here or kept, otherwise not.
        self.unfreezeCells()
        if section.frozen is not None:
            self._frozen = section.frozen
            self._frozenSeen = None
            self._followFrozen()

    def saveNamedSection(self, name):
        """Save the section on screen as *name* (replacing one so named).

        ``(True, '')`` or ``(False, reason)``; the reason is shown too.
        """
        case, why = self._workerTarget()
        if case is None:
            self._publishSaved(why)
            return False, why
        name = str(name or '').strip()
        try:
            document = named_sections.load_sections(case)
            existing = next((s for s in document.sections
                             if s.name == name), None)
            section = self.sectionDefinition(
                name, existing.id if existing else None)
            self._savedDocument = named_sections.put_section(case, section)
        except (ValueError, OSError,
                named_sections.SectionStoreError) as error:
            self._savedDocument = named_sections.load_sections(case)
            reason = str(error)
            self._publishSaved()
            for panel in [self._panel, *self._mirrors]:
                panel._savedNote.setText(reason)
                panel._savedNote.setVisible(True)
            return False, reason
        self._savedCurrent = section.id
        self._publishSaved()
        return True, ''

    def loadNamedSection(self, sectionId):
        document = self._savedDocument
        section = document.by_id(sectionId) if document else None
        if section is None:
            return False
        self._savedCurrent = section.id
        self.applySectionDefinition(section)
        self._publishSaved()
        return True

    def deleteNamedSection(self, sectionId):
        case, why = self._workerTarget()
        if case is None:
            return False, why
        try:
            self._savedDocument = named_sections.delete_section(
                case, sectionId)
        except (OSError, named_sections.SectionStoreError) as error:
            return False, str(error)
        if self._savedCurrent == sectionId:
            self._savedCurrent = None
        self._publishSaved()
        return True, ''

    def _workerRunning(self) -> bool:
        case = self._cellsCase
        return bool(self._jobs is not None and case is not None
                    and self._jobs.notice(case))

    def _workerRequest(self, case) -> SectionRequest:
        planes = self._panel.planes()
        active = self._panel.activeIndex()
        if not planes[active].enabled:
            active = next((index for index, plane in enumerate(planes)
                           if plane.enabled), active)
        mode = self._mode or self._panel.mode()
        return SectionRequest(
            case_dir=case, case_id=case, generation=self._generation,
            mode=mode.value, planes=tuple(plane.to_dict() for plane in planes),
            active=active, arrays=tuple(self._workerArrays()))

    def _workerArrays(self):
        """The source arrays the worker is asked for (UF9 colours)."""
        item = section_colour.choice(self._colourKey)
        return (item.worker,) if item.worker else ()

    def loadCellsForCut(self):
        """*Load cells for the cut*: the worker cuts the cells the planes
        meet, exactly, and the answer follows the planes from then on."""
        if self._jobs is None or not self._option:
            return
        case, _why = self._workerTarget()
        if case is None:
            return
        if self._cellsCase not in (None, case):
            self._jobs.close(self._cellsCase)
        self._cellsCase = case
        self._submitWorker(self._workerRequest(case))

    def _submitWorker(self, request):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop to run it on (a test, a shutdown): still say
            # what is wanted, so a late answer is not shown.
            self._jobs.want(request.case_id, request.key())
            return
        self._jobs.submit(request)
        self._markPrevious()
        self._publishStatus()

    def _followWorker(self):
        """Keep the worker on the cut on screen (after `_apply`)."""
        case = self._cellsCase
        enabled = len(self._panel.enabledPlanes()) if self._option else 0
        fewer = enabled < self._enabledCount
        self._enabledCount = enabled
        if case is None or self._jobs is None:
            return
        if not self._option:
            # Every plane down: no section, so no worker and no answer.
            self._jobs.cancel(case, 'section cleared')
            self._dropWorkerSection()
            self._cellsCase = None
            return
        if fewer:
            self._jobs.cancel(case, 'plane deleted')
        request = self._workerRequest(case)
        if self._dragging:
            # UF8. A drag never runs the worker; it only says what the
            # answer on screen is no longer for.
            self._jobs.want(case, request.key())
            self._markPrevious()
            return
        self._submitWorker(request)

    def _markPrevious(self):
        actor = self._workerActor
        shown = actor.completion() if actor is not None else None
        if shown is None or self._cellsCase is None:
            return
        wanted = self._jobs.wanted(self._cellsCase)
        if not is_current(manifest_key(shown.manifest), wanted):
            self._workerState = WORKER_PREVIOUS

    def _sectionDelivered(self, completion):
        """A wanted answer from the worker (`SectionJobs` delivers it)."""
        if completion.request.case_id != self._cellsCase:
            self._jobs.release(completion)
            return
        if completion.manifest is None:
            # A refusal: the section on screen stays, and says why.
            self._workerState = WORKER_REFUSED
            self._workerMessage = completion.message or completion.reason
            self._publishStatus()
            return
        asyncio.ensure_future(self._drawDelivered(completion))

    async def _drawDelivered(self, completion):
        manifest = completion.manifest
        case = completion.request.case_id
        surface = ((manifest.get('files') or {}).get('surface') or {}).get(
            'path')
        try:
            if surface:
                # MEASURED: a 2 M-polygon section.vtp takes ~100 ms to read,
                # six frames on the GUI thread; off it the loop stalls 16 ms.
                polyData = await asyncio.to_thread(readSectionSurface,
                                                   surface)
            else:
                from vtkmodules.vtkCommonDataModel import vtkPolyData
                polyData = vtkPolyData()
        except Exception as error:                          # noqa: BLE001
            logger.warning('could not read the section: %s', error)
            self._jobs.release(completion)
            if case == self._cellsCase:
                self._workerState = WORKER_REFUSED
                self._workerMessage = str(error)
                self._publishStatus()
            return
        if case != self._cellsCase or not is_current(
                manifest_key(manifest), self._jobs.wanted(case)):
            # The planes moved on while it was read.
            self._jobs.release(completion)
            return
        actor = self._workerSectionActor()
        replaced = actor.clear()
        actor.show(polyData, completion)
        if replaced is not None:
            self._jobs.release(replaced)
        self._workerState = WORKER_CURRENT
        empty = manifest.get('empty')
        self._workerMessage = (empty.get('message', '')
                               if isinstance(empty, dict) else '')
        self._refreshCaps()
        self._view.refresh()

    def _workerSectionActor(self):
        if self._workerActor is None:
            self._workerActor = WorkerSectionActor()
            add = getattr(self._view, 'addActor', None)
            if callable(add):
                add(self._workerActor.actor())
        return self._workerActor

    def _dropWorkerSection(self):
        actor = self._workerActor
        if actor is not None:
            shown = actor.clear()
            if shown is not None and self._jobs is not None:
                self._jobs.release(shown)
        self._workerState = None
        self._workerMessage = ''

    def cancelSectionJobs(self, reason=''):
        """The mesh under the section changed (unlock, remesh): stop the
        worker and take its answer down; the user asks again."""
        case = self._cellsCase
        if case is None or self._jobs is None:
            return
        self._jobs.cancel(case, reason)
        self._dropWorkerSection()
        self._cellsCase = None
        self._refreshCaps()
        self._followFrozen()

    def closeSectionJobs(self):
        """The case closed: cancel and remove all of its section scratch."""
        case = self._cellsCase
        if case is None or self._jobs is None:
            return
        self._jobs.close(case)
        self._dropWorkerSection()
        self._cellsCase = None

    # -- Plan 37 UF11: the same section through the kept stages ----------- #

    #: The comparison on screen (`stage_compare.CompareResult`), its view,
    #: the running comparison's task, and the runner (tests hand one in;
    #: ``None`` is the real ``mesh.section`` worker).
    _compare = None
    _compareView = None
    _compareTask = None
    _compareRunner = None
    #: Where the planes were when the comparison on screen (or running)
    #: was cut (`_compareKey`), and whether they have moved since.
    _compareAt = None
    _compareStale = False

    def stageCompare(self):
        return self._compare

    def stageCompareView(self):
        return self._compareView

    def _comparePanels(self, lines, legend='', busy=False, shown=False):
        for panel in [self._panel, *self._mirrors]:
            try:
                panel.setCompareResults(lines, legend, busy, shown)
            except RuntimeError:
                pass

    def _compareKey(self):
        """Where the planes are: each plane's on/off, origin and normal,
        rounded so a re-typed equal value is no move."""
        key = []
        for plane in self._panel.planes():
            key.append((bool(plane.enabled),
                        tuple(round(float(v), 9) for v in plane.origin),
                        tuple(round(float(v), 9) for v in plane.normal)))
        return tuple(key)

    def isStageCompareStale(self) -> bool:
        """Whether the comparison on screen was cut at planes that have
        moved since (Plan 37 UF11)."""
        return self._compareStale

    def _checkCompareStale(self):
        """Mark the comparison stale when a plane moved after it was cut --
        never left on screen as if current -- or current again when the
        planes are back where it was cut."""
        if self._compareAt is None:
            stale = False
        else:
            stale = self._compareKey() != self._compareAt
        if stale == self._compareStale:
            return
        self._compareStale = stale
        for panel in [self._panel, *self._mirrors]:
            setStale = getattr(panel, 'setCompareStale', None)
            if setStale is None:
                continue
            try:
                setStale(stale)
            except RuntimeError:
                pass

    def requestStageCompare(self, stages):
        """*Compare*: start the comparison on the event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._compareTask is not None and not self._compareTask.done():
            return self._compareTask
        self._compareTask = asyncio.ensure_future(
            self.compareStages(list(stages)))
        return self._compareTask

    async def compareStages(self, stages):
        """Cut the kept snapshot of each of *stages* with the planes on
        screen and draw them side by side under one legend.

        Returns the `CompareResult`, or ``None`` with the reason shown.
        """
        case, why = self._workerTarget()
        if case is None:
            self._comparePanels([why])
            return None
        planes = self._panel.planes()
        enabled = [index for index, plane in enumerate(planes)
                   if plane.enabled]
        if not enabled:
            self._comparePanels([self.tr('Turn a plane on to compare.')])
            return None
        active = self._panel.activeIndex()
        if active not in enabled:
            active = enabled[0]
        mode = self._mode or self._panel.mode()
        item = section_colour.choice(self._colourKey)
        self.clearStageCompare()
        self._compareAt = self._compareKey()
        self._comparePanels([self.tr('Cutting {0}…').format(
            count_text(len(stages), 'kept stage'))], busy=True)
        try:
            result = await stage_compare.run_compare(
                case, stages, planes=[plane.to_dict() for plane in planes],
                mode=mode, active=active,
                arrays=(item.worker,) if item.worker else (),
                generation=self._generation, runner=self._compareRunner)
            drawn = await self._readCompare(result)
        except Exception as error:                          # noqa: BLE001
            logger.warning('stage comparison failed', exc_info=True)
            self._compareAt = None
            self._checkCompareStale()
            self._comparePanels([self.tr('The comparison failed: {0}')
                                 .format(error)])
            return None
        self._compare = result
        self._drawCompare(result, drawn, item, planes[active].normal)
        # A plane moved while the stages were being cut: say so at once.
        self._checkCompareStale()
        return result

    async def _readCompare(self, result):
        drawn = []
        for section in result.sections:
            if not section.shown:
                continue
            data = await asyncio.to_thread(readSectionSurface,
                                           section.surface)
            drawn.append((section, data))
        return drawn

    def _drawCompare(self, result, drawn, item, normal):
        from foammesh.rendering.stage_compare_view import StageCompareView

        names = None
        values = []
        if item.key != section_colour.NONE:
            for _section, data in drawn:
                values.append(cellArray(data, item.array))
                names = names or fieldString(data, 'cellZoneNames')
        missing = [section.label for (section, _d), array
                   in zip(drawn, values) if array is None]
        mapped = {'kind': 'none'}
        legend = ''
        if item.key != section_colour.NONE and drawn and not missing:
            mapped = stage_compare.shared_legend(item.key, values,
                                                 names=names)
            legend = self.tr('One legend for every stage — {0}').format(
                section_colour.legendText(mapped))
        elif missing:
            legend = self.tr('Not coloured: {0} has no {1} values.').format(
                ', '.join(missing), item.label)
        if self._compareView is None:
            self._compareView = StageCompareView()
        props = self._compareView.show(
            [(section.label, data) for section, data in drawn], normal,
            mapped)
        add = getattr(self._view, 'addActor', None)
        if callable(add):
            for prop in props:
                add(prop)
        lines = [section.note for section in result.sections]
        if len(drawn) > 1:
            lines.append(self.tr(
                'Shown side by side, each moved {0:.4g} along the plane from '
                'the one before; every stage was cut at the same place.')
                .format(self._compareView.spacing()))
        self._compareMapping = mapped
        self._comparePanels(lines, legend, shown=bool(drawn))
        refresh = getattr(self._view, 'refresh', None)
        if callable(refresh):
            refresh()

    # -- Plan 37 UF11: a frozen cell layer -------------------------------- #

    #: The frozen selection (`named_sections.FrozenSelection`), where it is
    #: drawn from now (`frozen_layer.FrozenStatus`), the mesh identity it was
    #: last judged against, its actor, the running redraw, the worker's
    #: drawing on screen (`frozen_layer.FrozenRun`, its files) and the
    #: runner (``None``: the real worker).
    _frozen = None
    _frozenStatus = None
    _frozenSeen = None
    _frozenActor = None
    _frozenTask = None
    _frozenRun = None
    _frozenRunner = None
    _frozenScratch = None

    #: The colour the frozen cells are drawn in.
    FROZEN_COLOUR = '#e0a040'

    def frozenSelection(self):
        return self._frozen

    def frozenStatus(self):
        return self._frozenStatus

    def frozenActor(self):
        return self._frozenActor

    def _frozenPanels(self, note, frozen):
        for panel in [self._panel, *self._mirrors]:
            try:
                panel.setFrozenState(note, frozen)
            except RuntimeError:
                pass

    def freezeCells(self):
        """*Freeze cells*: bind the cells the worker's section shows to the
        mesh they are cells of, and keep them on screen while the planes
        move. ``(True, '')`` or ``(False, reason)``; the reason is shown."""
        case, why = self._workerTarget()
        actor = self._workerActor
        shown = actor.completion() if actor is not None else None
        manifest = getattr(shown, 'manifest', None) or {}
        cellsPath = ((manifest.get('files') or {}).get('cells') or {}).get(
            'path')
        reason = ''
        if case is None:
            reason = why
        elif (shown is None or self._cellsCase != case or not cellsPath
              or self._workerState != WORKER_CURRENT):
            reason = self.tr('Load cells for the cut first: only cells the '
                             'section worker cut, for the planes on screen, '
                             'can be frozen.')
        elif not frozen_layer.worker_read_is_live(case, manifest):
            reason = self.tr('The mesh changed after this cut was read; load '
                             'cells for the cut again before freezing.')
        if not reason:
            try:
                cells = np.load(cellsPath)
                frozen = frozen_layer.freeze(
                    case, cells, plane=self._panel.activeIndex(),
                    mode=manifest.get('key', {}).get('mode')
                    or SectionMode.CUT_CELLS)
            except (OSError, ValueError) as error:
                reason = str(error)
        if reason:
            self._frozenPanels(reason, self._frozen is not None)
            return False, reason
        self.unfreezeCells()
        self._frozen = frozen
        self._frozenSeen = None
        self._followFrozen()
        return True, ''

    def unfreezeCells(self):
        """Let the frozen cells go (and remove their files)."""
        task, self._frozenTask = self._frozenTask, None
        if task is not None and not task.done():
            task.cancel()
        self._frozen = None
        self._frozenStatus = None
        self._frozenSeen = None
        self._dropFrozenActor()
        self._frozenPanels('', False)

    def _dropFrozenActor(self):
        if self._frozenActor is not None:
            self._frozenActor.clear()
        run, self._frozenRun = self._frozenRun, None
        if run is not None:
            run.release()

    def _followFrozen(self):
        """The mesh under the frozen cells may have changed: judge them
        again when it has (a cheap stat identity), and redraw."""
        if self._frozen is None:
            return None
        case, _why = self._workerTarget()
        from foammesh.core.workflow.task_state_store import mesh_identity
        seen = (case, mesh_identity(case) if case else None)
        if seen == self._frozenSeen:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._frozenSeen = seen
        if self._frozenTask is not None and not self._frozenTask.done():
            self._frozenTask.cancel()
        self._frozenTask = asyncio.ensure_future(self.showFrozen())
        return self._frozenTask

    async def showFrozen(self):
        """Draw the frozen cells from the mesh they belong to -- the live
        one, or the kept stage snapshot of it -- or say why not.

        Returns the `frozen_layer.FrozenStatus`.
        """
        frozen = self._frozen
        if frozen is None:
            return None
        case, why = self._workerTarget()
        if case is None:
            state = frozen_layer.FrozenStatus(frozen_layer.INVALID,
                                              reason=why)
        else:
            state = await asyncio.to_thread(frozen_layer.status, case,
                                            frozen)
        if frozen is not self._frozen:
            return state
        if not state.drawable:
            self._frozenStatus = state
            self._dropFrozenActor()
            self._frozenPanels(state.reason, True)
            self._view.refresh()
            return state
        self._frozenPanels(self.tr('Reading {0} frozen cells…').format(
            len(frozen.cells)), True)
        run = await frozen_layer.run_frozen(
            frozen, state, runner=self._frozenRunner,
            scratch_root=self._frozenScratch)
        refusal = run.reason
        polyData = None
        if not refusal:
            try:
                polyData = await asyncio.to_thread(readSectionSurface,
                                                   run.surface)
            except Exception as error:                      # noqa: BLE001
                refusal = str(error)
        if frozen is not self._frozen:
            run.release()
            return state
        if refusal:
            # A snapshot or mesh that no longer holds these cells: nothing
            # is drawn rather than other cells under the same numbers.
            state = frozen_layer.FrozenStatus(frozen_layer.INVALID,
                                              reason=refusal)
            self._frozenStatus = state
            run.release()
            self._dropFrozenActor()
            self._frozenPanels(refusal, True)
            self._view.refresh()
            return state
        self._dropFrozenActor()
        self._frozenRun = run
        actor = self._frozenCellsActor()
        actor.show(polyData, state)
        self._frozenStatus = state
        self._frozenPanels(state.label, True)
        self._view.refresh()
        return state

    def _frozenCellsActor(self):
        if self._frozenActor is None:
            self._frozenActor = WorkerSectionActor(self.FROZEN_COLOUR)
            self._frozenActor.actor().SetObjectName('frozenCells')
            add = getattr(self._view, 'addActor', None)
            if callable(add):
                add(self._frozenActor.actor())
        return self._frozenActor

    def clearStageCompare(self):
        """Take the comparison down and remove its files."""
        if self._compareView is not None:
            remove = getattr(self._view, 'removeActor', None)
            for prop in self._compareView.clear():
                if callable(remove):
                    remove(prop)
        if self._compare is not None:
            self._compare.release()
            self._compare = None
        self._compareAt = None
        self._checkCompareStale()
        self._comparePanels([])

    # -- Plan 37 UF7: the step, the keys, the camera ------------------------ #

    def mode(self):
        """The section mode the cut on screen was made in (None: no cut)."""
        return self._mode if self._option else None

    def _stepSources(self):
        """``(points, offsets, connectivity)`` of the cells on screen.

        The volume when it is shown, otherwise the boundary faces.
        """
        mesh = getattr(app.window, 'meshManager', None)
        if mesh is None:
            return []
        shown = self._shown(mesh)
        chosen = ([info for info in shown if isinstance(info, MeshActor)]
                  or [info for info in shown if isinstance(info, BoundaryActor)])
        cache = dict(self._stepCache or {})
        sources, keys = [], set()
        for info in chosen:
            data = info.dataSet()
            if (data is None or not data.GetNumberOfCells()
                    or data.GetPoints() is None):
                continue
            key = (id(data), data.GetMTime())
            keys.add(key)
            if key not in cache:
                offsets, connectivity = cellLayout(data)
                points = vtk_to_numpy(data.GetPoints().GetData()).astype(float)
                # 2026-10-01. The tolerance and, per normal, every cell's
                # span along it are kept: a new position along the same
                # normal is then two comparisons, not a read of every cell.
                cache[key] = (points, offsets, connectivity,
                              tolerance_for(points), {})
            sources.append(cache[key])
        # Only what is on screen stays cached.
        self._stepCache = {key: cache[key] for key in keys}
        return sources

    def cellScaleStep(self, plane):
        """The median thickness along n of the cells *plane* passes through.

        Metres, or None when it passes through none (the panel then keeps its
        last step or says it fell back to the model's size). It is read on
        the GUI thread from the arrays already on screen.
        """
        spans = []
        normal = np.asarray(plane.state.normal, dtype=float)
        length = float(np.linalg.norm(normal))
        if not length or not np.isfinite(length):
            return None
        normal = normal / length
        offset = float(np.dot(np.asarray(plane.origin, dtype=float), normal))
        normalKey = tuple(round(float(value), 12) for value in normal)
        for points, offsets, connectivity, tolerance, ranges                 in self._stepSources():
            if normalKey not in ranges:
                if len(ranges) >= 3:
                    ranges.pop(next(iter(ranges)))
                ranges[normalKey] = projected_ranges(
                    points, offsets, connectivity, normal)
            low, high = ranges[normalKey]
            step = scale_step_from_ranges(low, high, offset, tolerance)
            if step is not None:
                spans.append(step)
        if not spans:
            return None
        return float(np.median(spans))

    def _installNudgeKeys(self, widget):
        for key, direction in NUDGE_KEYS.items():
            for fine in (False, True):
                modifier = (Qt.KeyboardModifier.ShiftModifier if fine
                            else Qt.KeyboardModifier.NoModifier)
                shortcut = QShortcut(QKeySequence(modifier | key), widget)
                shortcut.setContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.activated.connect(
                    lambda d=direction, f=fine, w=widget:
                    self._shortcutNudge(d, f, w))
                self._shortcuts.append(shortcut)

    def _shortcutNudge(self, direction, fine, widget):
        key = Qt.Key.Key_PageUp if direction > 0 else Qt.Key.Key_PageDown
        modifiers = (Qt.KeyboardModifier.ShiftModifier if fine
                     else Qt.KeyboardModifier.NoModifier)
        self.nudgeFromKey(key, modifiers,
                          QApplication.focusWidget() or widget)

    def _owners(self):
        owners = [self._panel, *self._mirrors]
        if isinstance(self._view, QWidget):
            owners.append(self._view)
        return owners

    def ownsFocus(self, focus) -> bool:
        """Whether a key pressed in *focus* is the section tool's to take."""
        if not isinstance(focus, QWidget):
            return False
        if isinstance(focus, _TEXT_ENTRY):
            return False
        if isinstance(focus, QComboBox) and focus.isEditable():
            return False
        return any(owner is focus or owner.isAncestorOf(focus)
                   for owner in self._owners())

    def nudgeFromKey(self, key, modifiers=None, focus=None) -> bool:
        """PgUp/PgDn steps the active plane, Shift a tenth as far.

        Only while the viewport or the section tool holds the keyboard, and
        never from a field that edits text. True when the plane moved.
        """
        direction = NUDGE_KEYS.get(key)
        if direction is None or not self.ownsFocus(focus):
            return False
        fine = bool(modifiers is not None
                    and modifiers & Qt.KeyboardModifier.ShiftModifier)
        panel = self._panel
        for mirror in self._mirrors:
            if mirror is focus or mirror.isAncestorOf(focus):
                panel = mirror
        # The panel's sectionChanged re-cuts (a mirror's via _mirrorChanged).
        return panel.nudge(direction, fine)

    def _camera(self):
        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        return None if renderer is None else renderer.GetActiveCamera()

    def isLookingAlong(self) -> bool:
        return self._savedCamera is not None

    def lookAlongPlane(self, on=True):
        """Look square at the active plane, or put the camera back."""
        camera = self._camera()
        if not on:
            saved, self._savedCamera = self._savedCamera, None
            if saved is not None and camera is not None:
                position, focal, up, parallel, scale = saved
                camera.SetPosition(*position)
                camera.SetFocalPoint(*focal)
                camera.SetViewUp(*up)
                camera.SetParallelProjection(parallel)
                camera.SetParallelScale(scale)
                self._resetClipping()
            self._showLooking(False)
            return
        plane = self._panel.planes()[self._panel.activeIndex()]
        if camera is None or not plane.enabled:
            self._showLooking(False)
            return
        if self._savedCamera is None:
            self._savedCamera = (camera.GetPosition(), camera.GetFocalPoint(),
                                 camera.GetViewUp(),
                                 camera.GetParallelProjection(),
                                 camera.GetParallelScale())
        _u, v, n = local_basis(plane.normalised())
        size = max(self._bounds.size()) if self._bounds is not None else 1.0
        size = size or 1.0
        focal = plane.origin
        # From the removed side, looking into the kept half, square on.
        camera.SetFocalPoint(*focal)
        camera.SetPosition(*[f - n[axis] * 2.0 * size
                             for axis, f in enumerate(focal)])
        camera.SetViewUp(*v)
        camera.ParallelProjectionOn()
        camera.SetParallelScale(0.6 * size)
        self._resetClipping()
        self._showLooking(True)

    def _resetClipping(self):
        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if renderer is not None:
            renderer.ResetCameraClippingRange()
        refresh = getattr(self._view, 'refresh', None)
        if callable(refresh):
            refresh()

    def _showLooking(self, looking):
        for panel in [self._panel, *self._mirrors]:
            try:
                panel.setLookingAlong(looking)
            except RuntimeError:
                pass        # The panel went with its window.

    def forgetLookAlong(self):
        """Drop the camera *Look along plane* would restore, without it."""
        self._savedCamera = None
        self._showLooking(False)

    def _viewDestroyed(self, *_args):
        self._savedCamera = None
