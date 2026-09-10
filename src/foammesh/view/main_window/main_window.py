#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
import platform

import qasync
from filelock import Timeout
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (QDialog, QMainWindow, QFileDialog, QMessageBox,
                               QInputDialog)
from PySide6.QtCore import Signal, QEvent, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QGuiApplication

from analytics import Analytics
from app_properties import meshAppProperties

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.support.utils import getFit
from foammesh.db.configurations_schema import Shape
from widgets.async_dialog import asyncExec
from widgets.async_message_box import AsyncMessageBox
from widgets.new_project_dialog import NewProjectDialog
from widgets.parallel.parallel_environment_dialog import ParallelEnvironmentDialog
from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.core.facade import FacadeError
from foammesh.core.documentation import tutorial_url
from foammesh.core.case import CaseConflictError, WorkflowMode
from foammesh.core.shell import (
    ACTION_OBJECT_NAMES, ActionId, ActionPolicy, AppSnapshot, ContentState,
    job_state_from_manager, terminal_capability,
)
from foammesh.core.mesh import (
    MeshInfoService, MeshRecoveryService, MeshRepairService,
    RepairOperation, RepairRequest,
)
from foammesh.core.quality import MeshCheckService
from foammesh.core.quality import discover_cell_set_files
from foammesh.core.quality import (QualityReport, apply_waivers,
                                   verdict_from_report)
from foammesh.core.project import Event
from foammesh.core.import_export import (
    ConverterFormat, ConverterImportService, extract_converter_warnings,
)
from foammesh.view.theming import ThemeMode
from foammesh.view.display_control.display_control import DisplayControl
from foammesh.view.display_control import view_modes
from foammesh.view.widgets.job_progress import JobProgressWidget
from foammesh.view.widgets.justification_dialog import JustificationDialog
from foammesh.view.widgets.mesh_import_dialog import MeshImportDialog
from foammesh.view.menu.mesh_quality.mesh_quality_parameters_dialog import MeshQualityParametersDialog
from foammesh.view.menu.help.about_dialog import AboutDialog
from foammesh.view.menu.help.license_dialog import LicenseDialog
from foammesh.view.menu.mesh import MeshInfoDialog, QualityDashboardDialog, TransformDialog
from foammesh.view.geometry.geometry_manager import GeometryManager
from widgets.themed_icon import load_themed_icon
from .recent_files_menu import RecentFilesMenu
from .naviagtion_view import NavigationView
from .rendering_tool import RenderingTool
from .console_view import Console
from .mesh_composition import cell_count_text, composition_text
from .mesh_manager import MeshManager
from .step_manager import StepManager
from .main_window_ui import Ui_MainWindow
from .three_region_shell import install_three_region_shell, reveal_output_band
from .output_tabs import OutputTabRegistry
from .capture_manager import CaptureManager
from .captures_page import CapturesPage
from foammesh.core.capture import captures_dir, delete_capture
from foammesh.core.quality.geometry_fidelity import hotspot
from foammesh.rendering import feature_overlay
from foammesh.core.quality.geometry_fidelity import report as fidelity_report
from foammesh.core.viewport_state import (
    ViewStateError, delete_view, load_views, save_view)
from foammesh.view.display_control.viewport_overlay import ViewportOverlay
from .mesh_quality_tab import MeshQualityTab
from foammesh.core.geometry.diagnostics.repair import (
    REPAIR_BANDS, TESSELLATED_ACTIONS, apply_action, write_surface,
)
from foammesh.view.facade_client import query, submit

from .run_narration import describe_cancellation, describe_start
from .run_status_strip import RunStatusStrip


logger = logging.getLogger(__name__)

#: How long the verdict strip waits for a stored quality report before it
#: settles for "not checked". Reading a persisted report is a filesystem
#: operation; anything slower than this is a fault, not a slow disk.
QUALITY_READ_TIMEOUT = 5.0


class MainWindow(QMainWindow):
    _vtkReaderProgress = Signal(str)
    _closeTriggered = Signal(bool)

    def __init__(self):
        super().__init__()
        self._ui = Ui_MainWindow()
        self._ui.setupUi(self)
        self._threeRegionShell = install_three_region_shell(self._ui)
        # Non-visual compatibility alias for controller/tests that inspect the
        # former member. It is permanent and has no hide/toggle action.
        self._sidebar = self._ui.regionAHost
        layout_state = getattr(
            app.settings, 'getThreeRegionLayout', lambda: {})()
        self._savedRegionSizes = layout_state.get('sizes')
        self._regionSplitterUserMoved = False
        self._threeRegionShell.apply_sizes(
            max(self.width(), 1280), self._savedRegionSizes)
        self._ui.regionSplitter.splitterMoved.connect(
            lambda *_args: setattr(
                self, '_regionSplitterUserMoved', True))
        QTimer.singleShot(
            0, lambda: self._threeRegionShell.apply_sizes(
                max(self.width(), 1280), self._savedRegionSizes))
        self.setMinimumWidth(self._threeRegionShell.minimum_window_width)

        self._applyThemedIcons()
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(self._applyVtkTheme)
            if app.themeManager.tokens is not None:
                self._applyVtkTheme(app.themeManager.resolved_name)

        self._ui.renderingSplitter.setStretchFactor(0, 0)
        self._ui.renderingSplitter.setStretchFactor(1, 1)
        self._ui.regionSplitter.setStretchFactor(0, 0)
        self._ui.regionSplitter.setStretchFactor(1, 0)
        self._ui.regionSplitter.setStretchFactor(2, 1)
        self._ui.regionSplitter.adjustSize()

        self._recentFilesMenu = RecentFilesMenu(self._ui.menuOpen_Recent)
        self._recentFilesMenu.setRecents(app.settings.getRecentCases())

        self._navigationView = NavigationView(self._ui)
        self._displayControl = DisplayControl(self._ui)
        self._renderingTool = RenderingTool(self._ui)
        self._outputTabs = OutputTabRegistry(self._ui.regionCTabHost)
        # Plan 26 WP3: the mesh viewport is no longer one of these tabs. It is
        # a permanent sibling above them, so watching the console during a run
        # no longer hides the mesh and inspecting the mesh no longer hides the
        # log. Registering it here again would restore exactly that defect.
        self._consoleView = Console()
        self._consoleView.setObjectName('regionCConsole')
        self._consoleView.setAccessibleName(self.tr('Meshing job console'))
        self._outputTabs.register_permanent(
            'console', self._consoleView, self.tr('Console'))
        self._meshQualityTab = MeshQualityTab()
        self._outputTabs.register_permanent(
            'quality', self._meshQualityTab, self.tr('Mesh Quality'))
        self._verdictStrip = self._ui.meshVerdictStrip
        self._verdictStrip.activated.connect(self.showQualityReport)
        self._meshQualityTab.acceptRequested.connect(self._acceptMeshQuality)
        self._meshQualityTab.elementSelected.connect(self._focusQualityElement)
        #: WP3.3. Auto-raise Quality when a run finishes -- console is what a
        #: user wants *during* a run, quality *after* -- but never once the user
        #: has chosen a tab themselves, or it steals focus mid-read.
        self._outputTabSelectedByUser = False
        self._ui.regionCTabHost.tabBarClicked.connect(
            lambda _index: setattr(self, '_outputTabSelectedByUser', True))
        active_output = str(layout_state.get('active_output') or 'console')
        self._outputTabs.show(
            active_output if self._outputTabs.contains(active_output)
            else 'console')
        if app.themeManager is not None and app.themeManager.tokens is not None:
            self._applyVtkTheme(app.themeManager.resolved_name)

        self._geometryManager: Optional[GeometryManager] = None
        #: Which of the five view modes the viewport is in. Empty until the
        #: user picks one: an empty case is not "Geometry mode with nothing
        #: in it", it is a viewport that has not been asked for anything yet.
        self._viewMode: str = ''
        #: Where the failed-cell walk is. -1 means "not started".
        self._failedCellSetIndex: int = -1
        self._meshManager = None
        self._stepManager = StepManager(self._navigationView, self._ui)
        self.setTabOrder(
            self._ui.workflowStepTree, self._ui.wizardBackButton)
        self.setTabOrder(
            self._ui.wizardBackButton, self._ui.wizardProceedButton)
        self.setTabOrder(
            self._ui.wizardProceedButton, self._ui.regionCTabHost)
        self._actionPolicy = ActionPolicy()
        self._policyActions = {}
        self._menuRefreshPending = False
        self._projectRefreshPending = False
        self._jobStateUnsubscribe = app.jobManager.subscribe_state(
            self._scheduleMenuRefresh)
        self._capabilityStateUnsubscribe = app.capabilities.subscribe_state(
            self._scheduleMenuRefresh)
        # C3. The designed window is 1065 px tall; on a 1080 px screen the
        # status bar -- the only progress surface a task-page run has, and the
        # only place its Cancel lives -- was drawn below the bottom edge.
        self._jobProgress = JobProgressWidget(
            app.events, app.jobManager,
            lambda: self.showOutputTab('console'), self)
        self.statusBar().addPermanentWidget(self._jobProgress)

        #: Feature-line actors while the overlay is shown.
        self._featureActors = []
        self._captureManager = CaptureManager(
            self._ui.renderingView, self._displayControl)
        self._viewportOverlay = ViewportOverlay(self._ui.renderingView)
        self._viewportOverlay.move(12, 12)
        self._viewportOverlay.show()
        self._displayControl.cutTool().addMirror(
            self._viewportOverlay.sectionPanel())
        self._connectViewportControls()

        self._dialog = None

        self._readyToQuit = False
        #: True only while an untitled case is being given a permanent home.
        #: The close guard must stay quiet for that one close.
        self._relocatingScratch = False

        self.setWindowIcon(meshAppProperties.icon())

        # OEM variants with analytics disabled don't need this entry.
        self._privacyConfigured = Analytics().configured
        self._ui.actionPrivacySettings.setVisible(self._privacyConfigured)

        self._setupShortcuts()

        self._connectSignalsSlots()

        self._ui.regionValidationMessage.hide()

        # Plan 30 WP-08 (F-09). What a run did is said here, in the bottom bar
        # under the step page, instead of in a modal raised over the viewport
        # that is drawing the mesh the sentence is about. Inserted rather than
        # designed into the .ui so the bar's own layout stays where it is: it
        # goes directly above the Unlock / Finish / Next row.
        #: The allocation of the run currently on the strip, so cancelling it
        #: can name the workers it stopped rather than only the job count.
        self._lastRunAllocation = None
        self._runStatusStrip = RunStatusStrip(self)
        self._runStatusStrip.cancelRequested.connect(self._cancelRunningJobs)
        self._runStatusStrip.offerAccepted.connect(self._drawOfferedResult)
        self._runStatusStrip.logRequested.connect(self._openRunLog)
        #: CP-02. The earlier accepted result the strip is currently offering,
        #: set when a run ends with no mesh of its own. Never drawn until the
        #: user takes the offer.
        self._offeredResult = None
        bar = self._ui.regionValidationMessage.parentWidget().layout()
        if bar is not None:
            bar.insertWidget(
                bar.indexOf(self._ui.regionValidationMessage) + 1,
                self._runStatusStrip)

        # C3. `availableVirtualGeometry()` is the union of every attached
        # monitor, so a window opening on a 1080 px screen was clamped against
        # the tallest screen in the set -- and the bottom of the window, which
        # is where the status bar lives, ended up under the taskbar. The status
        # bar is the only progress surface a task-page run has and the only
        # place its Cancel button lives, so losing it is not cosmetic.
        geometry = app.settings.getLastMainWindowGeometry()
        screen = (QGuiApplication.screenAt(geometry.center())
                  or app.qApplication.primaryScreen())
        fit = getFit(geometry, screen.availableGeometry())
        self.setGeometry(fit)

    @property
    def renderingView(self):
        return self._ui.renderingView

    @property
    def consoleView(self):
        return self._consoleView

    def showOutputTab(self, key: str):
        """Select one existing Region C output without changing Regions A/B."""
        return self._outputTabs.show(key)

    # -- WP7/WP8: the viewport's own controls ------------------------------ #

    def _connectViewportControls(self):
        tool = self._renderingTool
        tool.sectionRequested.connect(self._quickSection)
        tool.captureRequested.connect(self.captureViewport)
        tool.galleryRequested.connect(self.showCaptures)
        tool.isolateRequested.connect(lambda: self._displayControl.isolate())
        tool.zoomSelectionRequested.connect(self._zoomToSelection)
        tool.showAllRequested.connect(self._displayControl.showAll)
        tool.poorCellsRequested.connect(self._highlightPoorCells)
        tool.failedCellsRequested.connect(self._showFailedCellsFromViewport)
        tool.fidelityRequested.connect(self.showGeometryFidelity)
        tool.featuresRequested.connect(self.toggleFeatureCapture)
        tool.viewModeRequested.connect(self.applyViewMode)
        tool.layerCoverageRequested.connect(self.showLayerCoverage)

        self._displayControl.meshQualityInfo().statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 8000))

        # F-44. Not `setChip` directly: what the chip counts is a question
        # about the mesh, and only the window can see both the mesh and the
        # scene.
        self._displayControl.visibilityChanged.connect(
            lambda *_args: self._refreshOverlayChip())
        self._displayControl.visibilityChanged.connect(
            lambda *_args: self._refreshOverlayParts())
        self._viewportOverlay.visibilityToggled.connect(
            self._setPartVisible)
        self._viewportOverlay.soloRequested.connect(
            lambda key: self._displayControl.isolate([key]))
        self._viewportOverlay.showAllRequested.connect(
            self._displayControl.showAll)

        tool.explodeChanged.connect(self._setExplode)
        tool.orbitRequested.connect(self._recordOrbit)
        tool.sweepRequested.connect(self._recordSectionSweep)
        tool.saveViewRequested.connect(self._saveNamedView)
        tool.namedViewRequested.connect(self._recallNamedView)
        tool.deleteViewRequested.connect(self._forgetNamedView)
        self._viewportOverlay.explodeChanged.connect(self._setExplode)
        # One binding instead of a call at every site that happens to change
        # the scene; a site that forgets is a stale overlay.
        self._displayControl.partsChanged.connect(self.rebuildOverlayParts)

        # Rule 2: the picture answers "what is this" without a trip to a panel.
        self._ui.renderingView.actorHovered.connect(self._hoveredActorChanged)
        # Rule 3: nothing drops detail without saying so.
        self._ui.renderingView.detailReduced.connect(self._detailReduced)

    # -- WP5.2 / WP7.5 / WP8.3 --------------------------------------------- #

    def _installViewportEffectActions(self):
        """WP4.2/4.3 and WP6, as preferences that all default off.

        The plan's own rule is that nothing costing frames is enabled until its
        cost has been measured on a real case, and that number does not exist
        yet. So these are offered, off, each saying what it trades -- rather
        than switched on for everyone on the strength of a screenshot.
        """
        view = self._ui.renderingView
        self._effectActions = {}
        entries = (
            (self.tr('Cavity Shading (SSAO)'),
             self.tr('Depth cues in internal passages. Costs frame time.'),
             view.setAmbientOcclusion),
            (self.tr('Smooth Thin Edges (FXAA)'),
             self.tr('Cleans up outlines and wireframes. Costs frame time.'),
             view.setFastAntiAliasing),
            (self.tr('Reduce Detail While Moving'),
             self.tr('Keeps large meshes responsive during a drag, and says '
                     'so on screen while it does.'),
             view.setInteractiveDecimation),
        )
        for label, tip, setter in entries:
            action = self._ui.menuView.addAction(label)
            action.setCheckable(True)
            action.setChecked(False)
            action.setToolTip(tip)
            action.setStatusTip(tip)
            action.toggled.connect(
                lambda checked, apply=setter, item=action:
                self._applyViewportEffect(apply, item, checked))
            self._effectActions[label] = action

    def _applyViewportEffect(self, setter, action, checked):
        """Apply an effect, and untick it if this VTK build cannot do it.

        A checked box over a renderer that ignored the request is the same
        silent lie as any other control that does nothing when pressed.
        """
        accepted = setter(checked)
        if accepted is False:
            action.setChecked(False)
            action.setEnabled(False)
            action.setToolTip(
                self.tr('This VTK build does not provide it.'))
            self.statusBar().showMessage(
                self.tr('{0} is not available in this VTK build.').format(
                    action.text()), 6000)

    def _detailReduced(self, reduced: bool):
        if reduced:
            self.statusBar().showMessage(
                self.tr('Showing reduced detail while the view moves.'), 4000)

    def _zoomToSelection(self):
        """WP5.3. Fit the camera to the selected parts rather than the scene.

        On a mesh large enough that dragging to find one patch is hopeless,
        this is the only practical way to get to it -- which is exactly the
        size of mesh where it was never wired to anything.
        """
        control = self._displayControl
        props = [prop
                 for info in control.actorInfosFor(control.selectedActorIds())
                 for prop in info.renderProps()]
        if not props:
            self.statusBar().showMessage(
                self.tr('Select a part first, then fit the view to it.'), 5000)
            return
        if not self._ui.renderingView.zoomToProps(props):
            self.statusBar().showMessage(
                self.tr('The selected parts have no extent to fit to.'), 5000)
            return
        self._renderingTool.updateHistoryButtons()

    def _setExplode(self, factor: float):
        if self._meshManager is not None:
            self._meshManager.setExplode(factor)
        if self._geometryManager is not None:
            self._geometryManager.setExplode(factor)

    def refreshNamedViews(self):
        root = self._captureManager.caseRoot()
        names = load_views(root).keys() if root is not None else ()
        self._renderingTool.setNamedViews(names)

    def _saveNamedView(self):
        root = self._captureManager.caseRoot()
        if root is None:
            self.statusBar().showMessage(
                self.tr('Open a case before saving a view.'), 5000)
            return
        name, accepted = QInputDialog.getText(
            self, self.tr('Save Current View'), self.tr('Name this view:'))
        if not accepted or not name.strip():
            return
        try:
            save_view(root, name, self._ui.renderingView.cameraState())
        except (OSError, ViewStateError) as error:
            self.statusBar().showMessage(str(error), 8000)
            return
        self.refreshNamedViews()
        self.statusBar().showMessage(
            self.tr('Saved the view "{0}".').format(name.strip()), 5000)

    def _recallNamedView(self, name: str):
        root = self._captureManager.caseRoot()
        camera = load_views(root).get(name) if root is not None else None
        if not camera:
            self.statusBar().showMessage(
                self.tr('That view is no longer saved.'), 5000)
            self.refreshNamedViews()
            return
        self._ui.renderingView.rememberView()
        self._ui.renderingView.restoreCameraState(camera)
        self._renderingTool.updateHistoryButtons()

    def _forgetNamedView(self, name: str):
        root = self._captureManager.caseRoot()
        if root is not None:
            delete_view(root, name)
        self.refreshNamedViews()

    def _recordOrbit(self):
        frames = self._captureManager.renderOrbit()
        self._reportSequence(self.tr('Orbit'), frames)

    def _recordSectionSweep(self):
        tool = self._displayControl.cutTool()
        if not tool.panel().enabledPlanes():
            self.statusBar().showMessage(
                self.tr('Raise a section plane before sweeping it.'), 6000)
            return
        frames = self._captureManager.renderSectionSweep(tool)
        self._reportSequence(self.tr('Section sweep'), frames)

    def _reportSequence(self, label: str, frames):
        """Say where the frames went, and why there is no video if there isn't.

        Announced after the frames exist rather than before, so a machine with
        no encoder still gets its deliverable instead of an error at the end of
        a long render.
        """
        if not frames:
            self.statusBar().showMessage(
                self.tr('{0}: no frames were written.').format(label), 6000)
            return
        available, detail = self._captureManager.encoderAvailable()
        message = self.tr('{0}: {1} frames in the Captures tab.').format(
            label, len(frames))
        if not available:
            message = f'{message}  {detail}'
        self.statusBar().showMessage(message, 12000)
        self.showCaptures()

    def _hoveredActorChanged(self, actorId: str):
        """Name the part under the cursor, with its size when it has one."""
        item = self._displayControl._items.get(actorId)
        if item is None:
            self._ui.renderingView.setToolTip('')
            return
        info = item.actorInfo()
        dataSet = info.dataSet()
        cells = dataSet.GetNumberOfCells() if dataSet is not None else 0
        self._ui.renderingView.setToolTip(
            self.tr('{0} — {1:,} cells').format(info.name(), cells)
            if cells else info.name())

    # -- CP-09 item 5: what the viewport is for, right now ----------------- #

    def viewScene(self) -> view_modes.Scene:
        """The entities a view mode can be built from, in the mesh's own terms.

        Read off what the two managers already publish -- the geometry's
        actors, the mesh's patches, zones and internal volume -- so a mode
        cannot drift from what is actually in the scene.
        """
        geometry = ()
        manager = getattr(self, '_geometryManager', None)
        if manager is not None:
            geometry = tuple(manager.actorIds())
        mesh = getattr(self, '_meshManager', None)
        if mesh is None or mesh.isEmpty():
            return view_modes.Scene(geometry=geometry)
        patches = tuple(mesh.patchIds())
        zones = tuple(mesh.zoneIds())
        named = set(patches) | set(zones)
        volume = tuple(key for key in mesh.actorIds() if key not in named)
        return view_modes.Scene(geometry=geometry, patches=patches,
                                volume=volume, zones=zones)

    def applyViewMode(self, modeId: str) -> bool:
        """Enter a view mode, or say in words why this case has no such view.

        A mode that cannot be shown leaves the picture alone: blanking the
        viewport to honour a request is worse than not honouring it, because
        the user then has to work out whether the mesh or the button is
        broken.
        """
        plan = view_modes.plan(modeId, self.viewScene())
        if not plan.is_available():
            self.statusBar().showMessage(self.tr(plan.unavailable), 10000)
            self._renderingTool.showViewMode(self._viewMode)
            return False
        self._displayControl.applyViewPlan(plan)
        self._setSectionEnabled(plan.section)
        if plan.quality:
            self._highlightPoorCells()
        self._viewMode = plan.mode
        self._renderingTool.showViewMode(plan.mode)
        self.statusBar().showMessage(
            self.tr('{0}: showing {1}.').format(
                self.tr(view_modes.mode(plan.mode).label), plan.summary), 8000)
        return True

    def _setSectionEnabled(self, enabled: bool):
        """Raise or drop the section plane the Slice mode is made of."""
        tool = self._displayControl.cutTool()
        panel = tool.panel()
        if enabled:
            if not panel.enabledPlanes():
                self._quickSection()
            return
        for index, plane in enumerate(panel.planes()):
            if plane.enabled:
                panel._planeToggled(index, False)

    def highlightScope(self, names) -> view_modes.ScopeMatch:
        """Light up the entities a control is scoped to.

        CP-09 item 5's second half. A refinement or layer setting names
        patches; until now the only way to find out which parts of the picture
        it governed was to read the names and hunt for them in the tree.
        """
        mesh = getattr(self, '_meshManager', None)
        if mesh is None or mesh.isEmpty():
            return view_modes.ScopeMatch(missing=tuple(str(n) for n in names))
        match = view_modes.resolve_scope(
            names, patches=mesh.patchIds(), zones=mesh.zoneIds(),
            regions=mesh.regions())
        self._displayControl.setSelectedActors(list(match.found))
        if match.missing:
            self.statusBar().showMessage(
                self.tr('This setting names parts that are not in the '
                        'current mesh: {0}').format(', '.join(match.missing)),
                10000)
        return match

    def _quickSection(self):
        """One click: a plane along the current view normal, already cutting."""
        tool = self._displayControl.cutTool()
        if not tool.updateBounds():
            self.statusBar().showMessage(
                self.tr('Nothing is loaded to cut yet.'), 5000)
            return
        panel = tool.panel()
        index = panel.activeIndex()
        tool._useViewNormal()
        panel._planeToggled(index, True)
        panel._gizmoButtons[index].setChecked(True)
        self._renderingTool.updateHistoryButtons()

    def _setPartVisible(self, key: str, visible: bool):
        item = self._displayControl._items.get(key)
        if item is None:
            return
        item.setActorVisible(visible)
        self._displayControl._visibilityChanged()

    def _refreshOverlayParts(self):
        """Keep the overlay a view of the actor state, never a second copy."""
        parts = []
        for key, item in self._displayControl._items.items():
            info = item.actorInfo()
            parts.append((key, info.name(), info.color(), info.isVisible()))
        self._viewportOverlay.updateParts(
            [(key, color, visible) for key, _name, color, visible in parts])

    def rebuildOverlayParts(self):
        parts = []
        for key, item in self._displayControl._items.items():
            info = item.actorInfo()
            parts.append((key, info.name(), info.color(), info.isVisible()))
        self._viewportOverlay.setParts(parts)
        self._refreshOverlayChip()
        self._renderingTool.updateScaleReadout()
        self._refreshQualityActions()

    def _countedParts(self):
        """Which parts the chip counts, and the noun for them (F-44).

        The rows list every prop, because every prop is something a user can
        hide. The *count* is a different question: "how many parts is this
        mesh", and the answer is its patches and its zones. A duct read "14
        parts" because the STL surfaces it was meshed from, the four patches,
        the internal volume and the zones are all props in one scene.
        """
        partIds = getattr(getattr(self, '_meshManager', None),
                          'meshPartIds', None)
        ids = list(partIds()) if partIds is not None else []
        if ids:
            return ids, self.tr('mesh parts')
        return list(self._displayControl._items), self.tr('parts')

    def _refreshOverlayChip(self):
        keys, noun = self._countedParts()
        items = self._displayControl._items
        shown = sum(1 for key in keys
                    if key in items and items[key].actorInfo().isVisible())
        self._viewportOverlay.setChip(shown, len(keys), noun,
                                      self._compositionText())

    def _compositionText(self) -> str:
        """What the mesh on screen is made of, by name (CP-09 item 6).

        A count answers "how many"; the question people actually bring to a
        finished mesh is "is my inlet there, and did the porous zone survive".
        ``MeshManager`` has grouped the scene by region, patch and zone since
        it was written and nothing has ever read the names back out.
        """
        manager = getattr(self, '_meshManager', None)
        if manager is None or manager.isEmpty():
            return ''
        try:
            return composition_text(sorted(manager.regions()),
                                    manager.patchIds(), manager.zoneIds())
        except Exception:                                    # noqa: BLE001
            logger.debug('mesh composition unavailable', exc_info=True)
            return ''

    def showRunResult(self, handle):
        """Name the run whose mesh the viewport is showing (F-37).

        Run id, how the run ended, and the cell count of *that* artifact --
        not of whatever the case root happens to hold.

        The same sentence goes to the bottom-bar strip (F-09), which is what
        replaced the completion modal: the viewport is already showing the
        mesh by the time this is called, so the words belong beside it rather
        than over it.
        """
        description = handle.describe() if handle is not None else ''
        self._offeredResult = None
        self._viewportOverlay.setResult(description)
        self.showRunStatus(description)

    def offerPreviousResult(self, message: str, handle) -> bool:
        """Say a run left no mesh, and name the one the case still has.

        CP-02. A run that produced nothing used to leave the previous mesh on
        screen with the new run's verdict beside it, which is F-37 exactly.
        Clearing the viewport is right; leaving the user with nothing and no
        route back to the mesh the case still holds is not. So the earlier
        accepted result is named here and drawn only when asked for, and when
        it is drawn it is labelled as the earlier run's, not this one's.
        """
        strip = getattr(self, '_runStatusStrip', None)
        if handle is None or strip is None:
            self._offeredResult = None
            self.showRunStatus(message, failed=True)
            return False
        self._offeredResult = handle
        self._viewportOverlay.setResult(message)
        strip.showOffer(
            f'{message} {self.tr("The case still holds")} '
            f'{handle.describe()}.',
            self.tr('Show that mesh'))
        return True

    @qasync.asyncSlot()
    async def _drawOfferedResult(self):
        """Draw the earlier result the strip offered, on request."""
        handle = self._offeredResult
        self._offeredResult = None
        if handle is None:
            return
        problem = await self._meshManager.loadResult(handle)
        if problem:
            self.showRunStatus(problem, failed=True)
            return
        # Named as the earlier run's mesh, deliberately shown -- not as the
        # result of the run that just failed.
        description = (f'{self.tr("Showing earlier result")} · '
                       f'{handle.describe()}')
        self._viewportOverlay.setResult(description)
        self.showRunStatus(description)

    async def drawStoredResult(self) -> str:
        """Draw what this case already holds, named by the run that made it.

        CP-02 item 7. Reopening asked `meshManager.load(0)`, which reads
        `constant/polyMesh` and nothing else -- so a native Gmsh case, which
        since CP-01 publishes no polyMesh at all unless a solver target asks
        for one, reopened empty over a mesh that was on disk, and a case that
        did have a polyMesh drew it with no idea which run it belonged to.

        Returns '' when something was drawn or there was nothing to draw, and
        the read failure otherwise.
        """
        from foammesh.core.run_result import result_on_open

        handle = None
        try:
            handle = result_on_open(app.project.path)
        except OSError:
            logger.debug('stored results could not be read', exc_info=True)
        if handle is not None:
            problem = await self._meshManager.loadResult(handle)
            if problem:
                self.showRunStatus(problem, failed=True)
                return problem
            self.showRunResult(handle)
            return ''
        # A mesh this resolver cannot name -- multi-region, a time directory,
        # or one the user pointed the case at. Still a mesh, still drawn.
        if self._shouldDrawMeshOnOpen():
            await self._meshManager.load(0)
        return ''

    def showRunStatus(self, message: str, *, failed: bool = False,
                      log: str = '') -> None:
        """Say what a run did, inline. Replaces the completion modal (F-09).

        Plan 31 CP-07 item 6: ``log`` puts the run's own log one click away,
        so the answer to "what went wrong on four workers" is not "go and
        read the case directory", which is where the processor directories
        are.
        """
        strip = getattr(self, '_runStatusStrip', None)
        if strip is not None:
            strip.showResult(message, failed=failed, log=log)

    def showRunStarted(self, message: str, *, allocation=None) -> None:
        """A run is under way, and this is where its Cancel lives (F-09).

        The allocation is kept so that cancelling can say what it stopped:
        the cancel payload counts jobs, not workers, and "1 job stopped" over
        a four-worker run is the kind of half-truth this work package is
        supposed to remove.
        """
        self._lastRunAllocation = allocation
        strip = getattr(self, '_runStatusStrip', None)
        if strip is not None:
            strip.showRunning(message)

    def _openRunLog(self, path: str) -> None:
        """Open the log the strip is offering, in whatever reads text here.

        A log that cannot be opened says so on the strip rather than doing
        nothing: a dead button is indistinguishable from a broken product.
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        target = Path(path)
        if not target.is_file():
            self.showRunStatus(
                self.tr('That log is no longer on disk: {0}').format(path),
                failed=True)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            self.showRunStatus(
                self.tr('This system offered nothing that opens {0}').format(
                    path), failed=True)

    @qasync.asyncSlot()
    async def _cancelRunningJobs(self):
        """Stop whatever this case is running, from the strip's Cancel.

        One call reaches both engines: a snappy stage and a Gmsh run are the
        same kind of thing to the job manager, which owns their process groups
        and kills the group -- the WSL child tree included.
        """
        client = getattr(app, 'facadeClient', None)
        if client is None:
            return
        try:
            result = await client.cancel_active_jobs()
        except Exception as error:                            # noqa: BLE001
            logger.warning('cancel could not be delivered: %s', error)
            self.showRunStatus(
                self.tr('Cancel could not be delivered: {0}').format(error),
                failed=True)
            return
        # Plan 31 CP-07 item 6. Cancelling used to end in silence: the button
        # greyed out, the strip kept the sentence from before, and a user who
        # had just stopped a four-worker run had no way to learn whether half
        # a mesh had been adopted as the case mesh. Say how many jobs stopped
        # and that the case mesh is untouched, in those words.
        self.showRunStatus(
            describe_cancellation(getattr(result, 'payload', None),
                                  self._lastRunAllocation))

    def _refreshQualityActions(self):
        """Every disabled control on the toolbar says why it cannot act.

        Poor-cells is gated on there being a mesh, not on the mesh already
        carrying the fields. It used to be gated on the fields, which disabled
        it on every mesh FoamMesh produces and explained that the user should
        "run a mesh check that writes them" -- a command Foundation v13 does
        not have. The fields are computed from the polyMesh on first use.
        """
        manager = self._meshManager
        hasMesh = manager is not None and not manager.isEmpty()
        failed = bool(getattr(self, '_lastFailedCellSets', None))
        self._renderingTool.setQualityActionsEnabled(
            hasMesh,
            self.tr('No mesh is loaded'),
            failed,
            self.tr('No mesh check has written a failed-cell set yet'))
        # Recording an orbit or a section sweep needs something in the scene
        # to orbit around. The gate was written and never called, so the menu
        # offered both on an empty viewport and the recording came out blank.
        self._renderingTool.setSequencesEnabled(
            hasMesh or getattr(self, '_geometryManager', None) is not None,
            self.tr('Load a geometry or a mesh to record a sequence'))
        self._refreshInspectionActions(hasMesh)
        self._refreshLayerAction(hasMesh)

    def _refreshInspectionActions(self, hasMesh: bool):
        """Fidelity and feature overlays only exist where a run wrote them.

        Both buttons were permanently live. Pressing either on a case that had
        never run the geometry-fidelity task -- every case, until it does --
        produced a status-bar sentence that expired in nine seconds and no
        change to the picture, which reads as a broken button rather than an
        unavailable one.
        """
        report = self._latestFidelityReport() if hasMesh else None
        noMesh = self.tr('No mesh is loaded')
        noRun = self.tr(
            'This mesh has not been checked against its reference geometry. '
            'Run the geometry fidelity task to colour deviation here.')
        fidelityReason = noMesh if not hasMesh else noRun
        features = False
        featuresReason = noMesh if not hasMesh else noRun
        if report is not None:
            task_id = str(report.get('task_id') or '')
            root = self._captureManager.caseRoot()
            sections = {}
            if task_id and root is not None:
                try:
                    sections = hotspot.read_features(root, task_id) or {}
                except Exception:                             # noqa: BLE001
                    logger.debug('no readable feature measurements',
                                 exc_info=True)
            features = bool(sections) or bool(self._featureActors)
            if not features:
                featuresReason = self.tr(
                    'The fidelity run recorded no feature measurements, so '
                    'there are no declared feature lines to draw. Declare '
                    'features on the geometry and run it again.')
        self._renderingTool.setInspectionActionsEnabled(
            report is not None, fidelityReason, features, featuresReason)

    def _refreshLayerAction(self, hasMesh: bool):
        """Gate the layer-coverage entry on a layer run having happened.

        Separate from the fidelity gate above because it asks a different
        question of a different source -- the stored coverage document rather
        than the fidelity report -- and because a layer stage is a snappy
        thing that a Gmsh case will never have.
        """
        coverage = (self.layerCoverage() if hasMesh
                    else view_modes.LayerCoverage())
        self._renderingTool.setLayerCoverageEnabled(
            coverage.measured,
            self.tr('No mesh is loaded') if not hasMesh else self.tr(
                'No boundary-layer run has been measured on this case, so '
                'there is no coverage to show. Run the layer stage first.'))

    def layerCoverage(self) -> view_modes.LayerCoverage:
        """What the layer stage achieved, read through the facade.

        Empty rather than raising when the operation is unknown or the case
        has none: the answer "nobody measured" is the one the caller needs,
        and it is the same answer either way.
        """
        try:
            document = query(app.facadeClient, 'mesh.layer_coverage')
        except (FacadeError, RuntimeError, OSError, ValueError, KeyError):
            logger.debug('no readable layer coverage', exc_info=True)
            return view_modes.LayerCoverage()
        return view_modes.summarise_layers(document)

    def showLayerCoverage(self) -> bool:
        """Put the patches whose prism layers fell short on the screen.

        CP-09 item 8. `mesh.layer_coverage` has written achieved per-patch
        layers on every layer run since Plan 26 WP6.1 and no view module read
        it, so the one place the numbers could not be seen was the picture
        they describe -- and a layer run in which every layer was rejected
        still exits successfully, so silence was indistinguishable from
        success.
        """
        coverage = self.layerCoverage()
        if not coverage.measured or not coverage.rows:
            self.statusBar().showMessage(self.tr(coverage.describe()), 10000)
            return False
        self.applyViewMode(view_modes.BOUNDARY)
        if coverage.short:
            self.highlightScope(coverage.short)
        self.statusBar().showMessage(self.tr(coverage.describe()), 15000)
        return True

    def showGeometryFidelity(self):
        """WP3.3. Colour the surface by how far it left the reference geometry.

        Two resolutions, and the viewport says which one it is showing. The
        per-face field only exists where a fidelity run produced it; the
        per-section verdict comes from the stored report and works on any case
        that has ever been qualified. Falling back silently would leave a user
        reading a patch-level colour as if it located the problem.
        """
        root = self._captureManager.caseRoot()
        if root is None or self._meshManager is None:
            self.statusBar().showMessage(
                self.tr('Open a case with a mesh first.'), 5000)
            return

        report = self._latestFidelityReport()
        if report is None:
            self.statusBar().showMessage(self.tr(
                'This mesh has not been checked against its reference '
                'geometry. Run the geometry fidelity task first.'), 9000)
            return

        refusal = self.caseOverlayRefusal()
        if refusal:
            self.statusBar().showMessage(refusal, 12000)
            return

        task_id = str(report.get('task_id') or '')
        fields = hotspot.read(root, task_id) if task_id else {}
        if fields:
            painted = self._meshManager.showFidelityHotspots(fields)
            self.statusBar().showMessage(self.tr(
                'Deviation from the reference geometry, per face, on {0} '
                'patches.').format(painted), 12000)
            return

        verdicts = {
            str(section.get('name') or ''): str(section.get('verdict') or '')
            for section in report.get('sections') or ()
        }
        painted = self._meshManager.showSectionFidelity(verdicts)
        self.statusBar().showMessage(self.tr(
            'Fidelity verdict per patch on {0} patches — no per-face field was '
            'recorded for this run, so this shows which patch, not where.'
        ).format(painted), 12000)

    def toggleFeatureCapture(self):
        """WP3.1. Draw the declared features, coloured by whether they survived.

        "Did the mesher capture my feature" was previously answerable only by
        exporting and eyeballing, and before that not at all -- the feature
        measurement existed and nothing ever called it.
        """
        if self._featureActors:
            for actor in self._featureActors:
                self._displayControl.removeOverlay(actor)
            self._featureActors = []
            self.statusBar().showMessage(self.tr('Feature lines hidden.'), 4000)
            return

        root = self._captureManager.caseRoot()
        report = self._latestFidelityReport() if root is not None else None
        task_id = str((report or {}).get('task_id') or '')
        sections = hotspot.read_features(root, task_id) if task_id else {}
        if not sections:
            self.statusBar().showMessage(self.tr(
                'No feature measurements for this mesh. Run the geometry '
                'fidelity task on a case whose geometry declares features.'),
                9000)
            return

        tokens = (app.themeManager.tokens
                  if app.themeManager is not None else None)
        self._featureActors = feature_overlay.build_actors(sections, tokens)
        for actor in self._featureActors:
            self._displayControl.addOverlay(actor)

        counts = feature_overlay.summarise(sections)
        self.statusBar().showMessage(self.tr(
            'Declared features: {0}.').format(', '.join(
                f'{count} {verdict}' for verdict, count
                in sorted(counts.items()))), 12000)

    def _latestFidelityReport(self):
        """The most recent stored fidelity report for this case, or None."""
        root = self._captureManager.caseRoot()
        if root is None:
            return None
        try:
            return fidelity_report.latest(root)
        except Exception:
            logger.debug('no readable fidelity report', exc_info=True)
            return None

    def _highlightPoorCells(self):
        info = self._displayControl.meshQualityInfo()
        if not info.highlightWorst():
            self.statusBar().showMessage(
                self.tr('No mesh is loaded to measure.'), 6000)

    def caseOverlayRefusal(self) -> str:
        """Why a case-root overlay must not be drawn over what is on screen.

        CP-09 item 8, and the honest half of the CP-02 seam. `checkMesh
        -writeSets` writes cell *indices* into `constant/polyMesh`, and the
        viewport may be showing a native `.msh` from a run directory -- a
        refused Gmsh candidate, drawn straight from the mesher's own file.
        Those indices address a different mesh, so painting them there colours
        arbitrary cells and calls them failures.

        CP-05 owns binding a run id into the QA report; until it lands the
        report carries a *task* id and no artifact id, so this cannot check
        identity. It checks the one thing it can see: whether the artifact on
        screen is the published case at all.
        """
        mesh = getattr(self, '_meshManager', None)
        getter = getattr(mesh, 'nativePath', None)
        if getter is None or getter() is None:
            return ''
        return self.tr(
            'The viewport is showing the mesh file this run wrote, and the '
            'mesh check wrote its cell sets against the published case. The '
            'cell numbers do not mean the same thing in both, so they are '
            'not drawn here. Accept this result to inspect its failed cells.')

    def failedCellSetNames(self) -> list:
        return list(getattr(self, '_lastFailedCellSets', None) or {})

    def _showFailedCellsFromViewport(self):
        """Step to the next failed-cell set, rather than only ever the first.

        MEASURED: this took `next(iter(sets))` and stopped. checkMesh routinely
        writes several -- skewFaces, nonOrthoFaces, wrongOrientedFaces -- and
        the toolbar could reach exactly one of them, chosen by dictionary
        order, with nothing on screen saying the others existed.
        """
        sets = getattr(self, '_lastFailedCellSets', None)
        if not sets:
            self.statusBar().showMessage(
                self.tr('No mesh check has written a failed-cell set yet.'), 6000)
            return
        refusal = self.caseOverlayRefusal()
        if refusal:
            self.statusBar().showMessage(refusal, 12000)
            return
        names = list(sets)
        index = getattr(self, '_failedCellSetIndex', -1) + 1
        if index >= len(names):
            index = 0
        self._failedCellSetIndex = index
        name = names[index]
        ids = list(sets[name])
        self._meshManager.showFailedCells(ids, name)
        position = self.tr('  ({0} of {1} sets — press again for the next)'
                           ).format(index + 1, len(names)) if len(names) > 1 else ''
        self.statusBar().showMessage(
            self.tr('{0}: {1:,} cells.').format(name, len(ids)) + position,
            10000)

    def captureViewport(self, *, scale: int = 2):
        """Write a PNG plus its view record into the case, with no dialog."""
        if self._meshManager is None and self._geometryManager is None:
            self.statusBar().showMessage(
                self.tr('Open a case before capturing the viewport.'), 5000)
            return None
        path = self._captureManager.capture(scale=scale)
        if path is None:
            self.statusBar().showMessage(
                self.tr('Open a case before capturing the viewport.'), 5000)
            return None
        self.statusBar().showMessage(
            self.tr('Captured {0}').format(path.name), 6000)
        # The capture is only useful if the user can see it. This used to
        # refresh the tab when one happened to be open and do nothing at all
        # otherwise, so the first capture of a session looked like a no-op.
        self.showCaptures()
        return path

    def showCaptures(self):
        page = self._outputTabs.show(
            'captures', self.tr('Captures'), CapturesPage)
        if page is None:
            return None
        try:
            page.restoreRequested.disconnect()
            page.deleteRequested.disconnect()
        except (RuntimeError, TypeError):
            pass
        page.restoreRequested.connect(self._restoreCapture)
        page.deleteRequested.connect(self._deleteCapture)
        root = self._captureManager.caseRoot()
        if root is not None:
            page.setCaptures(
                self._captureManager.captures(), captures_dir(root),
                self._captureManager.meshFingerprint())
        return page

    def _restoreCapture(self, record):
        if self._captureManager.restore(record):
            self._renderingTool.updateHistoryButtons()
            self.statusBar().showMessage(
                self.tr('View restored from the capture.'), 5000)

    def _deleteCapture(self, record):
        root = self._captureManager.caseRoot()
        if root is None:
            return
        delete_capture(root, record.image)
        self.showCaptures()

    @staticmethod
    def _selectConsole(window):
        """Route legacy job call sites to the Region C Console tab."""
        if hasattr(window, 'showOutputTab'):
            window.showOutputTab('console')
            return
        console = getattr(window, '_consoleView', None)
        toggle = getattr(console, 'toggleViewAction', None)
        if toggle is not None:
            toggle().setChecked(True)

    @staticmethod
    def _appendConsole(window, text: str):
        console = getattr(window, '_consoleView', None)
        if console is None:
            return
        target = console
        if not hasattr(target, 'append') and hasattr(target, 'widget'):
            target = target.widget()
        append = getattr(target, 'append', None)
        if append is not None:
            append(text)

    def showEffectiveSetup(self, payload: dict):
        """Show immutable runtime-derived setup in one keyed Region C tab."""
        from .output_pages import EffectiveSetupPage
        return self._outputTabs.show(
            'effective_setup', self.tr('Effective Setup'),
            lambda: EffectiveSetupPage(payload, self._ui.regionCTabHost))

    def showEffectiveEnginePlan(self, payload: dict):
        from .output_pages import JsonOutputPage
        return self._outputTabs.show(
            'effective_engine_plan', self.tr('Effective Engine Plan'),
            lambda: JsonOutputPage(
                self.tr('Immutable Engine Plan'), payload,
                self._ui.regionCTabHost))

    def setScaleNote(self, note):
        """Let the current step page annotate the viewport scale readout."""
        self._renderingTool.setScaleNote(note)

    @property
    def displayControl(self):
        return self._displayControl

    @property
    def geometryManager(self) -> GeometryManager:
        return self._geometryManager

    @property
    def meshManager(self):
        return self._meshManager

    def closeEvent(self, event):
        if not self._readyToQuit:
            self._closeTriggered.emit(True)
            event.ignore()
            return

        app.settings.updateLastMainWindowGeometry(self.geometry())
        sizes = self._ui.regionSplitter.sizes()
        getattr(app.settings, 'updateThreeRegionLayout', lambda **_kw: None)(
            sizes=sizes, active_output=self._outputTabs.key_for_current())
        if self._jobStateUnsubscribe is not None:
            self._jobStateUnsubscribe()
            self._jobStateUnsubscribe = None
        if self._capabilityStateUnsubscribe is not None:
            self._capabilityStateUnsubscribe()
            self._capabilityStateUnsubscribe = None
        self._jobProgress.shutdown()

        super().closeEvent(event)

    def resizeEvent(self, event):
        """Clamp the permanent regions while leaving user-adjusted A/B intact."""
        if not hasattr(self, '_threeRegionShell'):
            super().resizeEvent(event)
            return
        sizes = self._ui.regionSplitter.sizes()
        if len(sizes) == 3:
            self._threeRegionShell.apply_sizes(
                event.size().width(),
                sizes if self._regionSplitterUserMoved
                else self._savedRegionSizes)
        self._ui.regionAHost.setProperty(
            'compactMode', event.size().width() < 1000)
        super().resizeEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self._ui.retranslateUi(self)
            self._stepManager.retranslatePages()
            self._updateMenuStates()

        super().changeEvent(event)

    async def start(self, initial_case=None):
        self.show()
        self._clear()
        self._updateMenuStates()
        self._pruneStaleScratchCases()
        self.statusBar().showMessage(self.tr('Open a case or create a new case to begin.'))
        if initial_case is not None:
            # The window is intentionally visible before an optional case is
            # inspected.  A bad path therefore leaves a usable empty shell.
            # _openProject is an @asyncSlot: calling it schedules the task
            # itself and returns that Task. Wrapping it in create_task raises
            # TypeError ("a coroutine was expected"), which silently broke
            # every `foammesh <case>` launch.
            self._openProject(str(initial_case))

    def _setupShortcuts(self):
        self._ui.actionNew.setShortcut('Ctrl+N')
        self._ui.actionOpen.setShortcut('Ctrl+O')
        self._ui.actionSave.setShortcut('Ctrl+S')
        self._ui.actionSaveProjectAs.setShortcut('Ctrl+Shift+S')
        self._ui.actionClose.setShortcut('Ctrl+E')
        self._ui.actionExit.setShortcut('Ctrl+Q')
        self._ui.actionUndo.setShortcut('Ctrl+Z')
        self._ui.actionRedo.setShortcut('Ctrl+Shift+Z')
        self._ui.actionParallelEnvironment.setShortcut('Ctrl+P')

    def _applyVtkTheme(self, _name):
        self._applyThemedIcons()
        if app.themeManager is not None and app.themeManager.tokens is not None:
            tokens = app.themeManager.tokens
            self._ui.renderingView.applyTheme(tokens)
            if hasattr(self, '_renderingTool'):
                self._renderingTool.applyTheme(tokens)
            if hasattr(self, '_displayControl'):
                self._displayControl.applyTheme(tokens)
            if getattr(self, '_geometryManager', None) is not None:
                self._geometryManager.applyTheme(tokens)
            if getattr(self, '_meshManager', None) is not None:
                self._meshManager.applyTheme(tokens)
        if getattr(self, '_meshManager', None) is not None:
            self._meshManager.rethemeFailedCells()

    def _applyThemedIcons(self):
        """Repaint SVG toolbar icons after each runtime palette change."""
        # One drawing language across the whole viewport toolbar. The buttons
        # used to borrow from three different icon sets -- imported artwork at
        # mixed stroke weights plus two general-purpose `:/icons` glyphs -- and
        # adding well-formed ones only made the mismatch obvious.
        icons = {
            self._ui.axis: ':/graphicsIcons/originAxes.svg',
            self._ui.cubeAxis: ':/graphicsIcons/cubeAxes.svg',
            self._ui.ruler: ':/graphicsIcons/measure.svg',
            self._ui.perspective: ':/graphicsIcons/projection.svg',
            self._ui.fit: ':/graphicsIcons/fit.svg',
            self._ui.alignAxis: ':/graphicsIcons/alignAxis.svg',
            self._ui.rotate: ':/graphicsIcons/roll.svg',
            self._ui.rotationCenter: ':/graphicsIcons/rotationCenter.svg',
            self._ui.regionAdd: ':/icons/add-circle-outline.svg',
            self._ui.surfaceRefinementAdd: ':/icons/add-circle-outline.svg',
            self._ui.volumeRefinementAdd: ':/icons/add-circle-outline.svg',
            self._ui.boundaryLayerConfigurationsAdd: ':/icons/add-circle-outline.svg',
            self._ui.loadCastellationDefaults: ':/icons/arrow-undo-outline.svg',
            self._ui.loadSnapDefaults: ':/icons/arrow-undo-outline.svg',
            self._ui.loadBoundaryLayerDefaults: ':/icons/arrow-undo-outline.svg',
        }
        for widget, path in icons.items():
            widget.setIcon(load_themed_icon(path))
        self._applyAccessibleNames()

    def _applyAccessibleNames(self):
        """U7.4: icon-only controls must expose screen-reader names.

        Covers the viewport toolbar, the cut-plane/slice handles and the
        icon-only page buttons. A live a11y sweep of the real window found the
        latter two groups unnamed, so the check below is enforced by
        tests/unit/test_u7_accessibility.py rather than left to inspection.
        """
        names = {
            self._ui.axis: self.tr('Toggle origin axes'),
            self._ui.cubeAxis: self.tr('Toggle cube axes'),
            self._ui.ruler: self.tr('Measure distance'),
            self._ui.perspective: self.tr('Toggle parallel projection'),
            self._ui.fit: self.tr('Fit view to model'),
            self._ui.alignAxis: self.tr('Align view to nearest axis'),
            self._ui.rotate: self.tr('Roll view 90 degrees'),
            self._ui.rotationCenter: self.tr('Set rotation center'),
            self._ui.loadBoundaryLayerDefaults: self.tr('Load boundary-layer defaults'),
            self._ui.boundaryLayerConfigurationsAdd: self.tr('Add boundary-layer configuration'),
            self._ui.loadCastellationDefaults: self.tr('Load castellation defaults'),
            self._ui.surfaceRefinementAdd: self.tr('Add surface refinement group'),
            self._ui.volumeRefinementAdd: self.tr('Add volume refinement group'),
            self._ui.regionAdd: self.tr('Add region'),
            self._ui.loadSnapDefaults: self.tr('Load snap defaults'),
        }
        for widget, name in names.items():
            widget.setAccessibleName(name)
            if not widget.toolTip():
                widget.setToolTip(name)

    def _connectSignalsSlots(self):
        app.renderingToggled.connect(self._setRenderingEnabled)

        self._consoleAction = self._ui.menuView.addAction(self.tr('Console'))
        self._consoleAction.setShortcut('Ctrl+Shift+C')
        self._consoleAction.triggered.connect(
            lambda: self.showOutputTab('console'))
        self._installViewportEffectActions()

        self._recentFilesMenu.projectSelected.connect(self._openRecent)
        self._recentFilesMenu.clearRequested.connect(self._clearRecents)

        self._stepManager.workingStepChanged.connect(self._displayControl.openedStepChanged)
        self._stepManager.displayStepChanged.connect(self._displayControl.currentStepChanged)

        self._stepManager.batchStarted.connect(self._disableMenubar)
        self._stepManager.batchStopped.connect(self._enableMenubar)

        self._closeTriggered.connect(self._closeProject)
        self._bindPolicyActions()

    def _setRenderingEnabled(self, enabled):
        self._displayControl.setEnabled(enabled)
        if self._geometryManager is None or self._meshManager is None:
            return
        if enabled:
            self._renderingTool.enable()
            self._geometryManager.show()
            if app.workflowResolution.workflow is WorkflowMode.MESH_EXTERNAL:
                asyncio.create_task(self._meshManager.load(0))
                return
            self._stepManager.currentPage().updateMesh()    # This call meshManger.load()
        else:
            self._renderingTool.disable()
            self._geometryManager.hide()
            self._meshManager.unload()

    def _bindPolicyActions(self):
        """Bind every shell action once; policy decides whether it may run."""
        self._policyActions = {
            action_id: getattr(self._ui, object_name)
            for action_id, object_name in ACTION_OBJECT_NAMES.items()
        }
        handlers = {
            ActionId.NEW_CASE: self._actionNew,
            ActionId.NEW_SCRATCH_CASE: self._actionNewUntitled,
            ActionId.OPEN_PROJECT: self._actionOpen,
            ActionId.SAVE: self._actionSave,
            ActionId.SAVE_AS: self._actionSaveAs,
            ActionId.SAVE_PROJECT_AS: self._saveProjectAs,
            ActionId.LOAD_GEOMETRY: self._loadGeometry,
            ActionId.LOAD_MESH: self._loadNativeMesh,
            ActionId.CLOSE_PROJECT: self._closeProject,
            ActionId.EXIT: lambda: self.close(),
            ActionId.UNDO: self._undo,
            ActionId.REDO: self._redo,
            ActionId.TRANSACTION_HISTORY: self._showTransactionHistory,
            ActionId.MESH_INFO: self.showMeshInfo,
            ActionId.MESH_SCALE: lambda: self._runMeshTransformV13('scale'),
            ActionId.MESH_TRANSLATE: lambda: self._runMeshTransformV13('translate'),
            ActionId.MESH_ROTATE: lambda: self._runMeshTransformV13('rotate'),
            ActionId.MESH_QUALITY: self._actionParameters,
            ActionId.MESH_CHECK: self._runMeshCheckDashboard,
            ActionId.MESH_REPAIR: self._runMeshRepair,
            ActionId.MESH_RESTORE: self._restorePreviousMesh,
            ActionId.VIEW_FIT: lambda: self._ui.fit.click(),
            ActionId.VIEW_AXIS: lambda: self._ui.axis.click(),
            ActionId.VIEW_CUBE_AXIS: lambda: self._ui.cubeAxis.click(),
            ActionId.VIEW_RULER: lambda: self._ui.ruler.click(),
            ActionId.VIEW_PERSPECTIVE: lambda: self._ui.perspective.click(),
            ActionId.VIEW_ALIGN_AXIS: lambda: self._ui.alignAxis.click(),
            ActionId.VIEW_ROLL: lambda: self._ui.rotate.click(),
            ActionId.VIEW_ROTATION_CENTER: lambda: self._ui.rotationCenter.click(),
            ActionId.PARALLEL_ENVIRONMENT: self._openParallelEnvironmentDialog,
            ActionId.THEME: self._chooseTheme,
            ActionId.TERMINAL_HERE: self._openTerminalHere,
            ActionId.TUTORIALS: self._openTutorials,
            ActionId.LICENSE: self._openLicense,
            ActionId.PRIVACY: self._openPrivacySettings,
            ActionId.ABOUT: self._actionAbout,
        }
        if set(handlers) != set(self._policyActions):
            missing = set(self._policyActions) - set(handlers)
            extra = set(handlers) - set(self._policyActions)
            raise RuntimeError(f'invalid shell action registry; missing={missing}, extra={extra}')
        for action_id, handler in handlers.items():
            self._policyActions[action_id].triggered.connect(handler)
        for action_id, button in (
                (ActionId.VIEW_AXIS, self._ui.axis),
                (ActionId.VIEW_CUBE_AXIS, self._ui.cubeAxis),
                (ActionId.VIEW_RULER, self._ui.ruler),
                (ActionId.VIEW_PERSPECTIVE, self._ui.perspective),
                (ActionId.VIEW_ROTATION_CENTER, self._ui.rotationCenter)):
            action = self._policyActions[action_id]
            action.setChecked(button.isChecked())
            button.toggled.connect(action.setChecked)
        for action in self._policyActions.values():
            description = action.toolTip().strip() or action.text().replace('&', '')
            action.setProperty('foammeshDescription', description)

    def _hasMeshOnDisk(self) -> bool:
        """Is there a complete polyMesh anywhere this case is allowed to keep one?

        R116. MEASURED: editing Boundary Layers on a case that had just meshed
        reset the quality strip to "No mesh yet." and the Mesh Quality tab to
        "No mesh has been produced in this case yet.", while the mesh from
        ninety seconds earlier was still on disk and still the only mesh in the
        case. `clearMeshVerdict` -- the one thing that writes those two lines --
        is reached from exactly one place, `refreshMeshVerdict`, gated on this
        answer, and every edit re-asks it. So a false "no" here is the app
        reporting data loss.

        The two probes it used to run were `classify_case`, which looks at
        `constant/polyMesh` and nothing else, and a time-directory sweep that
        never looked in `constant/` at all. Between them they miss a
        multi-region case, whose mesh lives in `constant/<region>/polyMesh`,
        and they miss a complete mesh in a case `classify_case` refused for a
        reason that has nothing to do with the mesh -- an unreadable sidecar,
        say. Invalidating downstream tasks after an upstream edit is right;
        saying the mesh was never made is not, so the question asked here is
        the plain one: is a mesh on the disk.
        """
        from foammesh.support.openfoam.polymesh import isPolyMesh

        try:
            constant = app.project.path / 'constant'
            if isPolyMesh(constant / 'polyMesh'):
                return True
            if constant.is_dir():
                for child in constant.iterdir():
                    if child.is_dir() and isPolyMesh(child / 'polyMesh'):
                        return True
            return any(app.fileSystem.hasPolyMesh(time)
                       for time in app.fileSystem.times())
        except OSError:
            return False

    def _actionSnapshot(self):
        global_capabilities = (
            frozenset({ActionId.PRIVACY.value}) if self._privacyConfigured else frozenset())
        if app.project is None:
            return AppSnapshot(capabilities=global_capabilities)
        content = ContentState.EMPTY
        try:
            from foammesh.core.case import classify_case
            has_mesh = classify_case(app.project.path).has_mesh or self._hasMeshOnDisk()
            geometry_root = app.project.path / 'constant' / 'triSurface'
            has_geometry = (app.db.elementCount('geometry') > 0 or
                            (geometry_root.is_dir() and any(
                                item.is_file() for item in geometry_root.iterdir())))
            if has_mesh and has_geometry:
                content = ContentState.GEOMETRY_AND_MESH
            elif has_mesh:
                content = ContentState.MESH
            elif has_geometry:
                content = ContentState.GEOMETRY
        except OSError:
            pass
        capabilities = global_capabilities | frozenset({
            ActionId.SAVE_AS.value,
            ActionId.CLOSE_PROJECT.value,
            ActionId.MESH_QUALITY.value,
            ActionId.MESH_INFO.value,
            ActionId.PARALLEL_ENVIRONMENT.value,
            ActionId.TRANSACTION_HISTORY.value,
            ActionId.THEME.value,
            ActionId.LOAD_MESH.value,
            ActionId.SAVE_PROJECT_AS.value,
            ActionId.LOAD_GEOMETRY.value,
        })
        capability_reasons = {}
        terminal_available, terminal_reason = terminal_capability()
        if terminal_available:
            capabilities = capabilities | frozenset({ActionId.TERMINAL_HERE.value})
        else:
            capability_reasons[ActionId.TERMINAL_HERE.value] = terminal_reason
        check_mesh = app.capabilities.utility('checkMesh')
        if check_mesh.available:
            capabilities = capabilities | frozenset({ActionId.MESH_CHECK.value})
        else:
            capability_reasons[ActionId.MESH_CHECK.value] = check_mesh.reason
        transform_points = app.capabilities.utility('transformPoints')
        if transform_points.available:
            capabilities = capabilities | frozenset({
                ActionId.MESH_SCALE.value, ActionId.MESH_TRANSLATE.value,
                ActionId.MESH_ROTATE.value,
            })
        else:
            for action_id in (ActionId.MESH_SCALE, ActionId.MESH_TRANSLATE, ActionId.MESH_ROTATE):
                capability_reasons[action_id.value] = transform_points.reason
        if self._repairUtilities() or content in (ContentState.GEOMETRY, ContentState.GEOMETRY_AND_MESH):
            capabilities = capabilities | frozenset({ActionId.MESH_REPAIR.value})
        else:
            capability_reasons[ActionId.MESH_REPAIR.value] = (
                'No supported mesh repair utility was found in the configured environment')
        if MeshRecoveryService().has_available(app.project.path):
            capabilities = capabilities | frozenset({ActionId.MESH_RESTORE.value})
        else:
            capability_reasons[ActionId.MESH_RESTORE.value] = (
                'No previous-mesh recovery point exists for this case yet')
        history = app.facadeClient.history_status()
        return AppSnapshot(
            project_ready=True,
            content=content,
            workflow=app.workflowResolution.workflow,
            dirty=app.project.isDirty,
            job=job_state_from_manager(app.jobManager),
            undo_available=history['can_undo'],
            redo_available=history['can_redo'],
            undo_label=history['undo_label'],
            redo_label=history['redo_label'],
            rendering_available=self._meshManager is not None,
            capabilities=capabilities,
            capability_reasons=capability_reasons,
        )

    @staticmethod
    def _repairUtilities():
        """Return only utility paths that were positively discovered."""
        return {
            operation.utility_name: capability.executable
            for operation in RepairOperation
            if (capability := app.capabilities.utility(operation.utility_name)).available
            and capability.executable is not None
        }

    def _scheduleMenuRefresh(self, *_args, **_kwargs):
        """Coalesce noisy state notifications into one next-turn refresh."""
        if self._menuRefreshPending:
            return
        self._menuRefreshPending = True
        QTimer.singleShot(0, self._flushMenuRefresh)

    def _flushMenuRefresh(self):
        self._menuRefreshPending = False
        self._updateMenuStates()

    def _scheduleProjectRefresh(self, *_args, **_kwargs):
        """Coalesce artifact/history events into one truthful scene refresh."""
        self._scheduleMenuRefresh()
        if self._projectRefreshPending:
            return
        self._projectRefreshPending = True
        QTimer.singleShot(0, self._flushProjectRefresh)

    def _flushProjectRefresh(self):
        self._projectRefreshPending = False
        if self._geometryManager is not None:
            self._geometryManager.load()
        self._stepManager.load()
        if self._meshManager is not None:
            asyncio.create_task(self._meshManager.reload())
        self.refreshMeshVerdictSoon()

    # -- WP3: the verdict strip and its tab ------------------------------- #

    def _meshVerdictSuperseded(self, *_args, **_kwargs):
        """The configuration changed after this mesh was produced."""
        self._verdictStrip.mark_stale()

    def _focusQualityElement(self, element: dict) -> None:
        """Answer a click on an offending element in the Quality tab.

        The tab has emitted this since it was written and nothing listened,
        so the worst-cell table was a list one could read but not act on.
        The viewport colours the worst decile of the active metric, and the
        status bar names the element and where it is.
        """
        centroid = element.get('centroid') or ()
        where = ', '.join('{:.4g}'.format(float(axis)) for axis in centroid)
        try:
            self._displayControl.meshQualityInfo().highlightWorst()
        except Exception as error:  # noqa: BLE001 - the viewport must not take the tab down
            logger.warning('could not highlight the selected element: %s', error)
        self.statusBar().showMessage(
            self.tr('Element {0} at ({1}).').format(element.get('tag', ''), where),
            8000)

    def showMeshVerdict(self, verdict: dict) -> None:
        """Publish a fresh verdict to the strip and the Quality tab.

        Called when a run finishes. Raising Quality is deliberate and bounded:
        the console is what a user wants *during* a run and the quality report
        is what they want *after*, but a tab the user picked themselves is
        never taken away from them mid-read.
        """
        self._verdictStrip.show_verdict(verdict)
        self._meshQualityTab.show_verdict(verdict)
        if verdict.get('stale'):
            # A stored check whose fingerprint no longer matches the loaded
            # polyMesh. It describes a different mesh, so it must not read as
            # a live verdict -- and the settings-changed wording would be a
            # different, wrong explanation.
            self._verdictStrip.mark_stale(self.tr(
                'This check was run against a different mesh'))
        if verdict and not self._outputTabSelectedByUser:
            self.showQualityReport()

    def showQualityReport(self) -> None:
        """Raise the Mesh Quality tab *and* make it big enough to read.

        R62/R114. Raising the tab was the whole of `Details`, and the band it
        raised into was about 40 px tall: the headline clipped to one line and
        the Metrics and Offending-elements tables showed their headers with no
        room for a single row, so the report read as two empty tables under a
        sentence. Region C's own splitter is the thing that has to move, and
        the user should not have to find its handle to see the report the app
        is asking them to accept.
        """
        self._outputTabs.show('quality')
        splitter = getattr(self._ui, 'regionCSplitter', None)
        if splitter is not None:
            reveal_output_band(splitter)

    def refreshMeshVerdictSoon(self) -> None:
        """Schedule :meth:`refreshMeshVerdict` from synchronous code.

        The implementation used to carry ``@qasync.asyncSlot()`` and be invoked
        bare from two synchronous methods. In this environment that wrapper did
        not schedule anything -- Python reported the coroutine as never awaited
        -- so the refresh had never once run on opening a case, and the strip
        sat on its dormant "No mesh yet." over a mesh that was on screen.

        One implementation, one scheduler, and every entry point uses one of
        the two rather than guessing which calling convention applies.
        """
        asyncio.create_task(self.refreshMeshVerdict())

    async def refreshMeshVerdict(self) -> None:
        """Publish whatever quality is already known about the loaded mesh.

        The quality surfaces used to be filled by exactly one path -- accepting
        a Gmsh gate refusal -- so opening a case whose mesh had already been
        checked left the strip reading "No mesh yet." above a mesh that was
        drawn on screen and a stored report that said `warning`. `quality.report`
        is a READ of that persisted report, so this costs nothing and is the
        difference between a surface that is empty and one that is honest.
        """
        if app.project is None or not hasattr(self, '_outputTabs'):
            return
        # "Not checked" is only honest about a mesh that exists. Said about an
        # empty case it invites the user to run checkMesh on nothing, so the
        # dormant "No mesh yet." stands until there is something to measure.
        if not self._actionSnapshot().has_mesh:
            self.clearMeshVerdict()
            return

        # The strip's dormant state means "there is no mesh". Once a mesh
        # exists it must never read that way again, whatever happens below --
        # a mesh drawn on screen under a line saying "No mesh yet." is the
        # plainest kind of lying surface. Every failure path therefore lands on
        # the `unrated` verdict, which names the command that would produce a
        # real one, rather than on a bare `return`.
        report = None
        waivers: list = []
        try:
            # `quality.report` is a READ of a persisted file. Routed through
            # the async job path it never returned at all -- traced on an
            # opened mesh, the await simply never resumed -- so the strip sat
            # on its dormant text forever. Reads go through `query`, which
            # skips the write queue instead of waiting behind a mesh run.
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: query(app.facadeClient, 'quality.report'))
            data = (result.payload or {}).get('report')
            report = QualityReport.from_dict(data) if data else None
            # R119. The waivers bound to the mesh-quality gate report on disk.
            # checkMesh and that gate measure different things, so this re-read
            # -- which runs immediately after **Accept anyway** -- was
            # repainting `Quality limits: pass · quality: good` over a recorded
            # override of a sicn failure.
            waivers = list((result.payload or {}).get('waivers') or ())
        except Exception:
            # A case with no stored report is the common case, not an error,
            # and the operation signals it differently depending on how the
            # case was opened. None of those variants may leave the strip
            # dormant, so the class of failure does not change what is shown.
            logger.debug('no readable quality report for this case',
                         exc_info=True)

        published = self._verdictStrip.verdict or {}
        if report is None and published.get('verdict') not in (None, 'unrated'):
            # A real verdict is already on screen for this mesh; a failed
            # re-read must not downgrade it to "not checked".
            return

        verdict = apply_waivers(verdict_from_report(report), waivers)
        self._verdictStrip.show_verdict(verdict)
        self._meshQualityTab.show_verdict(verdict)
        if verdict.get('stale'):
            self._verdictStrip.mark_stale(self.tr(
                'This check was run against a different mesh'))

    def clearMeshVerdict(self) -> None:
        self._verdictStrip.clear()
        self._meshQualityTab.clear()

    def _acceptMeshQuality(self, verdict: dict) -> None:
        """Re-run the publication with the user's recorded acceptance.

        The decision is not applied here: the facade records it as a waiver
        bound to this mesh, refuses anything the verdict does not permit, and
        publishes only then. A GUI that flipped a flag locally would produce
        exactly the untraceable override WP1.4 exists to prevent.
        """
        if not verdict:
            return
        confirmed = QMessageBox.question(
            self, self.tr('Accept this mesh?'),
            self.tr('This mesh did not meet its quality limits:\n\n%s\n\n'
                    'Accepting it records the decision against this mesh. The '
                    'record lapses if the mesh or the limits change.')
            % str(verdict.get('reason') or ''),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        # R124. The waiver used to store the app's own failure sentence played
        # back as the reason -- every record read "1107 of 90418 elements are
        # below the requested sicn 0.1", which says what the gate found and
        # nothing about why a human shipped it anyway. The Repair page's
        # `Use as-is` has always asked for written justification first; this is
        # the more consequential of the two decisions. Refusing an empty answer
        # follows that page exactly.
        # R197. `QInputDialog.getMultiLineText` gives a six-line editor
        # that does not wrap: MEASURED here, a typed paragraph became one
        # long line behind a horizontal scrollbar showing its tail. The
        # sentence above promises a reviewer will read this.
        reason = JustificationDialog.ask(
            self, self.tr('Why is this mesh acceptable?'),
            self.tr('Explain why this mesh may be used despite failing its '
                    'quality limits. This is recorded against the mesh and is '
                    'what a reviewer will read.'))
        if not reason:
            return
        asyncio.create_task(self._runAcceptedMesh(
            reason, str(verdict.get('run_id') or '')))

    async def _runAcceptedMesh(self, reason: str = '',
                               run_id: str = '') -> None:
        # Plan 30 F-02. The operation was always `mesh.gmsh.run`: accepting a
        # snappy mesh meshed the case with Gmsh instead, or failed outright on
        # a case with no prepared geometry -- either way the user's decision
        # was recorded against a mesh nobody had accepted. Gmsh's gate refuses
        # *before* the mesh is published, so its whole run is what an
        # acceptance re-runs; snappy has already published, so only its QA
        # check is. Each engine names its own route; a listing that cannot be
        # read lands on the engine-neutral check rather than on a guess.
        from foammesh.core.engine.base import accept_quality_route

        try:
            listing = await app.facadeClient.run('mesh.engine.list')
            engines = dict(getattr(listing, 'payload', None) or {})
        except (FacadeError, OSError, ValueError, TypeError):
            logger.debug('the engine listing could not be read', exc_info=True)
            engines = {}
        operation = accept_quality_route(
            engines.get('current_engine'),
            target_solver=engines.get('target_solver'))
        # Plan 31 CP-05 item 4. The Gmsh route accepts a *candidate*, so it
        # has to be told which one. The verdict on screen names it; a verdict
        # that predates this field (a reopened case) is matched to the newest
        # judged run rather than being turned into a fresh mesh.
        parameters = {'accept_quality': True, 'accept_reason': reason}
        if operation == 'mesh.run.accept':
            run_id = run_id or await self._candidateRunId()
            if not run_id:
                QMessageBox.warning(
                    self, self.tr('Not accepted'),
                    self.tr('This case holds no meshing run to accept. Run '
                            'the mesh again and accept the result of that '
                            'run.'))
                return
            parameters = {'run_id': run_id, 'accept_reason': reason}
        # R198. The Gmsh route re-runs `mesh.gmsh.run` end to end -- mesh,
        # publish, checkMesh -- because the decision is recorded by the facade
        # rather than flipped here. MEASURED on tee / gmsh, that is about 90 s
        # during which the Console showed the *previous* run's last line and
        # the only sign of life was a counter in the status bar. The wizard's
        # own run subscribes to JOB_OUTPUT for exactly this reason; so does
        # this one. The snappy route runs only its QA check, which is quick --
        # and is the one route that does not re-mesh anything.
        console = self.consoleView
        unsubscribe = None
        try:
            unsubscribe = app.facadeClient.subscribe(
                Event.JOB_OUTPUT,
                lambda **event: console.append(event.get('line', '')))
            result = await app.facadeClient.run(operation, parameters)
        except Exception as error:                           # noqa: BLE001
            QMessageBox.warning(self, self.tr('Not accepted'), str(error))
            return
        finally:
            if unsubscribe is not None:
                unsubscribe()
        payload = getattr(result, 'payload', {}) or {}
        if getattr(result, 'status', '') != 'accepted':
            QMessageBox.warning(
                self, self.tr('Not accepted'),
                str(payload.get('reason') or self.tr('The mesh was refused.')))
            return
        # R198. The accepted run publishes a mesh. Leaving the viewport on the
        # STL with `0 cells` beside it is the same defect R95/R156 fixed for
        # the pipeline runs -- a mesh on disk that the app does not show.
        await self._drawPublishedMesh()
        # R119. The gate verdict this run produced is still the failing one --
        # accepting never rewrote it -- so the strip is shown the override
        # beside it rather than the bare verdict, and reads `waived` from the
        # moment the decision is taken instead of flipping green.
        override = payload.get('quality_override')
        self.showMeshVerdict(apply_waivers(
            payload.get('quality_verdict') or {},
            [override] if override else []))

    async def _candidateRunId(self) -> str:
        """The newest run that was judged, for a verdict that names none.

        Only a fallback. A verdict published by a run carries its own
        ``run_id`` (Plan 31 CP-05 item 4) and that is always preferred; this
        covers a case reopened before the run that produced the verdict on
        screen, where the newest judged run *is* the one the strip is
        describing. Returning nothing is a refusal, not a re-mesh.
        """
        try:
            listing = await app.facadeClient.run('mesh.gmsh.runs')
            runs = list((getattr(listing, 'payload', None) or {}).get('runs')
                        or ())
        except (FacadeError, OSError, ValueError, TypeError):
            logger.debug('the run listing could not be read', exc_info=True)
            return ''
        judged = [item for item in runs if item.get('quality_verdict')]
        if not judged:
            return ''
        judged.sort(key=lambda item: str(item.get('started_at') or ''))
        return str(judged[-1].get('run_id') or '')

    async def _drawPublishedMesh(self) -> None:
        """Draw the mesh that was just written to `constant/polyMesh` (time 0).

        A mesh that cannot be drawn does not turn a published run into a failed
        one, so this reports and returns rather than raising.
        """
        manager = getattr(self, 'meshManager', None)
        load = getattr(manager, 'load', None)
        if load is None:
            return
        try:
            await load(0)
        except Exception as error:                           # noqa: BLE001
            logger.warning('the accepted mesh could not be drawn',
                           exc_info=True)
            self.statusBar().showMessage(
                self.tr('The mesh was written but could not be drawn: %s')
                % error, 10000)

    def _updateMenuStates(self):
        snapshot = self._actionSnapshot()
        for action_id, presentation in self._actionPolicy.evaluate(snapshot).items():
            action = self._policyActions.get(action_id)
            if action is None:
                continue
            action.setEnabled(presentation.enabled)
            action.setVisible(presentation.visible)
            description = action.property('foammeshDescription') or action.text().replace('&', '')
            message = presentation.reason or description
            action.setToolTip(message)
            action.setStatusTip(message)
        self._ui.actionUndo.setText(
            self.tr('&Undo {0}').format(snapshot.undo_label)
            if snapshot.undo_label else self.tr('&Undo'))
        self._ui.actionRedo.setText(
            self.tr('&Redo {0}').format(snapshot.redo_label)
            if snapshot.redo_label else self.tr('&Redo'))
        self._updateWindowTitle()

    def _pruneStaleScratchCases(self):
        """Sweep abandoned untitled cases at launch.

        Long after they stop being useful, not immediately: a crash must not
        cost the user yesterday's work before they have noticed it is gone. A
        locked case is left for whichever FoamMesh still holds it.
        """
        from foammesh.core.case import prune_stale

        try:
            removed = prune_stale()
        except OSError:
            # Housekeeping never fails a launch.
            logger.debug('could not sweep scratch cases', exc_info=True)
            return
        if removed:
            logger.info('removed %d stale untitled case(s)', len(removed))

    def _updateWindowTitle(self):
        if app.project is None:
            self.setWindowTitle(meshAppProperties.fullName)
            self.setToolTip('')
            return
        dirty = ' *' if app.project.isDirty else ''
        # An untitled case says so in the one place a user always sees. Its
        # folder name is a temporary one they never chose, so showing it as
        # though it were a case name would read as saved work.
        name = (self.tr('Untitled') if app.isScratchCase()
                else app.project.name())
        self.setWindowTitle(f'{meshAppProperties.fullName} — {name}{dirty}')
        self.setToolTip(str(app.project.path))

    def _undo(self):
        # C31-12. Undo used to call `undo_sync`, which reached
        # `facade.execute_sync` directly -- it neither queued behind whatever
        # write was in flight nor gave the window back. It is a scheduled
        # write like every other one now, and the status line is written when
        # the facade says what it undid.
        def undone(result):
            if (getattr(result, 'payload', None) or {}).get('undone'):
                self.statusBar().showMessage(
                    self.tr('Undid the last state edit.'), 3000)
            self._updateMenuStates()

        submit(app.facadeClient, 'history.undo', {}, then=undone)

    def _redo(self):
        # C31-12. See `_undo`.
        def redone(result):
            if (getattr(result, 'payload', None) or {}).get('redone'):
                self.statusBar().showMessage(
                    self.tr('Redid the last state edit.'), 3000)
            self._updateMenuStates()

        submit(app.facadeClient, 'history.redo', {}, then=redone)

    def _showTransactionHistory(self):
        result = query(app.facadeClient, 'history.query', {'limit': 50})
        history = ([{'kind': 'state', **item} for item in result.payload['transactions']] +
                   [{'kind': 'artifact', **item} for item in result.payload['artifacts']])
        history.sort(key=lambda item: item.get('timestamp', ''))
        lines = []
        for item in history[-50:]:
            timestamp = item.get('timestamp', '').replace('T', ' ')[:19]
            if item.get('kind') == 'artifact':
                recovery = item.get('recovery_status') or 'none'
                fingerprint = ''
                if item.get('before_fingerprint') or item.get('after_fingerprint'):
                    fingerprint = '  {0} â†’ {1}'.format(
                        (item.get('before_fingerprint') or 'none')[:10],
                        (item.get('after_fingerprint') or 'none')[:10])
                lines.append('{0}  Artifact  {1}  [{2}; recovery: {3}]{4}'.format(
                    timestamp, item.get('operation', ''), item.get('status', ''),
                    recovery, fingerprint))
            else:
                lines.append('{0}  State  {1}  [{2}]'.format(
                    timestamp, item.get('action', ''), item.get('status', '')))
        details = '\n'.join(lines)
        QMessageBox.information(
            self, self.tr('Transaction History'),
            details or self.tr('No state or artifact transactions have been recorded.'))

    @qasync.asyncSlot()
    async def showMeshInfo(self):
        if app.project is None:
            return
        try:
            result = await app.facadeClient.run('mesh.info')
            from foammesh.core.mesh import MeshInfo
            info = MeshInfo.from_dict(result.payload)
        except (FacadeError, OSError, ValueError) as error:
            self.statusBar().showMessage(str(error), 5000)
            return
        self._dialog = MeshInfoDialog(info, self)
        if hasattr(self, '_outputTabs'):
            self._dialog.setWindowFlags(Qt.WindowType.Widget)
            # `replace`: this builds a fresh dialog from a fresh `mesh.info`
            # run every time. Without it the registry kept the first page and
            # dropped this one, so re-opening Mesh Info showed the numbers from
            # whenever it was first opened -- and did nothing visible at all
            # when its tab was already in front.
            self._outputTabs.show(
                'mesh_info', self.tr('Mesh Info'), lambda: self._dialog,
                replace=True)
        else:
            self._dialog.open()

    async def _qaOperationName(self) -> str:
        """Which QA operation this project's mesh answers to.

        The rule lives on the engine seam (F-24) so the menu, the QA row, the
        CLI and the facade cannot disagree about which check judges a mesh;
        the facade's own answer is preferred when it can be asked.
        """
        from foammesh.core.engine.base import qa_operation

        try:
            listing = await app.facadeClient.run('mesh.engine.list')
            payload = dict(getattr(listing, 'payload', None) or {})
        except (FacadeError, OSError, ValueError, TypeError):
            return qa_operation(None)
        return str(payload.get('qa_operation')
                   or qa_operation(payload.get('target_solver')))

    @qasync.asyncSlot()
    async def _runMeshCheckDashboard(self):
        if app.project is None:
            return
        # Plan 28 WP4. Which check depends on the solver the mesh is for: an
        # SU2 project is usually meshed on a machine with no OpenFOAM, and
        # this menu item then only ever produced a runtime error. Both checks
        # file the same report, so everything below is unchanged.
        operation = await self._qaOperationName()
        self.statusBar().showMessage(
            self.tr('Checking the mesh is readable by SU2...')
            if operation == 'quality.su2_readiness'
            else self.tr('Running checkMesh...'))
        MainWindow._selectConsole(self)
        unsubscribe = app.facadeClient.subscribe(
            Event.JOB_OUTPUT,
            lambda **payload: MainWindow._appendConsole(
                self, payload.get('line', '')))
        try:
            execution = await app.facadeClient.run(operation)
        except (FacadeError, OSError, ValueError) as error:
            await AsyncMessageBox().information(self, self.tr('Mesh Check Error'), str(error))
            return
        finally:
            unsubscribe()
        report_result = await app.facadeClient.run('quality.report')
        report_data = report_result.payload.get('report')
        from foammesh.core.quality import QualityReport
        report = QualityReport.from_dict(report_data) if report_data else None
        job = execution.payload.get('job', {})
        # Only a missing report means checkMesh did not finish. The operation
        # reports 'failed' whenever the mesh fails a check, and that case has
        # a report and a verdict -- which is exactly what the user asked for.
        if report is None:
            self.statusBar().showMessage(
                self.tr('checkMesh did not complete: {0}').format(
                    job.get('error') or job.get('status', 'failed')), 8000)
            return
        # The verdict says whether the mesh will *run*, then how good it is.
        # Reporting a bare severity called a runnable mesh "fail" whenever the
        # exhaustive checks found anything, which reads as "unusable" and is
        # not what checkMesh means.
        self.statusBar().showMessage(
            report.result.verdict or self.tr('checkMesh {0}. Report saved.').format(
                report.result.severity), 15000)
        MainWindow._appendConsole(self, '')
        MainWindow._appendConsole(self, report.result.verdict)
        MainWindow._appendConsole(self, '    {0:<26} {1:>14}  {2:>12}  {3:<9} {4}'.format(
            'indicator', 'critical', 'average', 'grade', 'ideal'))
        for indicator in report.result.quality_indicators():
            if indicator['critical'] is None:
                continue
            critical = '{0} {1:g}'.format(
                indicator['critical_kind'], indicator['critical'])
            average = ('{0:g}'.format(indicator['average'])
                       if indicator['average'] is not None else '-')
            MainWindow._appendConsole(self, '    {0:<26} {1:>14}  {2:>12}  {3:<9} {4}'.format(
                indicator['name'], critical, average, indicator['grade'],
                indicator['ideal']))
            MainWindow._appendConsole(self, ' ' * 8 + indicator['note'])
        for finding in report.result.advisory_findings:
            MainWindow._appendConsole(self, f'    quality: {finding}')
        for finding in report.result.blocking_findings:
            MainWindow._appendConsole(self, f'    BLOCKING: {finding}')
        cell_sets = discover_cell_set_files(app.project.path)
        # What checkMesh -writeSets just wrote is what the toolbar's
        # failed-cells button acts on. Nothing recorded it, so the button was
        # permanently disabled with "no mesh check has written a set yet"
        # immediately after a mesh check had written several.
        self._lastFailedCellSets = cell_sets
        # A fresh check is a fresh list; leaving the cursor where the last
        # run left it would open on whichever set happens to sit at that
        # position now.
        self._failedCellSetIndex = -1
        self._refreshQualityActions()
        if self._meshManager is not None:
            self._meshManager.clearFailedCells()
        raw_log = job.get('output', '')
        if job.get('output_truncated'):
            raw_log += '\n\n[Output truncated; the complete log remains in the case journal.]'
        self._dialog = QualityDashboardDialog(report, raw_log, self)
        self._dialog.setSelected.connect(
            lambda name: self._highlightFailedSet(name, cell_sets))
        if hasattr(self, '_outputTabs'):
            self._dialog.setWindowFlags(Qt.WindowType.Widget)
            # A key of its own. This used to ask for 'quality', which WP3 had
            # since taken for the permanent Mesh Quality tab -- so the registry
            # returned that tab, this dashboard was discarded, and running
            # checkMesh looked like it did nothing whatever. The two are
            # different surfaces: this is one run's full log and set list, that
            # is the standing verdict for the loaded mesh.
            self._outputTabs.show(
                'mesh_check', self.tr('Mesh Check'), lambda: self._dialog,
                replace=True)
        else:
            self._dialog.open()
        # The run that just finished is also the freshest answer for the strip.
        self.showMeshVerdict(verdict_from_report(report))
        self._updateMenuStates()

    def _highlightFailedSet(self, name, cell_sets):
        if self._meshManager is None or name not in cell_sets:
            self.statusBar().showMessage(
                self.tr('{0} is not a cell set that can be highlighted.').format(name), 5000)
            return
        if self._meshManager.showFailedCells(cell_sets[name], name):
            self.statusBar().showMessage(
                self.tr('Highlighted {0} failed cell(s) from {1}.').format(
                    len(cell_sets[name]), name), 8000)

    @qasync.asyncSlot()
    async def _runMeshTransformV13(self, operation):
        if app.project is None:
            return
        utility = app.capabilities.utility('transformPoints').executable
        # The first transform of a session waits on a cold WSL `transformPoints
        # -help` probe -- measured at 2.6s -- before its dialog can be built.
        # Nothing said so, so the click was indistinguishable from a dead menu
        # item, and a user who moved on never saw the dialog arrive.
        self.statusBar().showMessage(
            self.tr('Preparing mesh {0}...').format(operation))
        try:
            app.project.assertUnchanged()
            info_result, help_result = await asyncio.gather(
                app.facadeClient.run('mesh.info'),
                asyncio.to_thread(app.capabilities.help, 'transformPoints'))
            from foammesh.core.mesh import MeshInfo
            info = MeshInfo.from_dict(info_result.payload)
            if not help_result.available:
                raise ValueError(help_result.reason)
            fields_present = self._caseHasResultFields(app.project.path)
            field_support = '-rotateFields' in help_result.output
            if fields_present and operation != 'rotate':
                proceed = QMessageBox.warning(
                    self, self.tr('Existing Result Fields'),
                    self.tr('This case contains result fields. The selected transform changes mesh points '
                            'but does not transform those fields. Continue?'),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if proceed != QMessageBox.StandardButton.Yes:
                    self.statusBar().clearMessage()
                    return
            dialog = TransformDialog(
                operation, info, self, fields_present=fields_present,
                field_transform_supported=field_support)
            if await asyncExec(dialog) != dialog.DialogCode.Accepted:
                # "Preparing..." must not outlive the preparation it describes.
                self.statusBar().clearMessage()
                return
            request = dialog.request()
            request.argv(utility, app.project.path)
        except (OSError, ValueError, CaseConflictError) as error:
            self.statusBar().clearMessage()
            await AsyncMessageBox().information(self, self.tr('Invalid Transform'), str(error))
            return
        self.statusBar().showMessage(self.tr('Running mesh {0}...').format(operation))
        MainWindow._selectConsole(self)
        parameters = {
            'vector': list(request.vector),
            'angle_degrees': request.angle_degrees,
            'pivot': list(request.pivot) if request.pivot else None,
            'transform_fields': request.transform_fields,
        }
        unsubscribe = app.facadeClient.subscribe(
            Event.JOB_OUTPUT,
            lambda **payload: MainWindow._appendConsole(
                self, payload.get('line', '')))
        try:
            completed = await app.facadeClient.run(
                f'mesh.transform.{operation}', parameters)
        except FacadeError as error:
            await AsyncMessageBox().information(self, self.tr('Mesh Transform Error'), str(error))
            return
        finally:
            unsubscribe()
        if completed.status != 'accepted':
            job = completed.payload.get('job', {})
            detail = job.get('error') or job.get('status', 'failed')
            self.statusBar().showMessage(
                self.tr('Mesh {0} failed; the previous mesh was restored: {1}').format(
                    operation, detail), 8000)
        else:
            app.refreshWorkflowResolution()
            self.statusBar().showMessage(self.tr('Mesh {0} completed.').format(operation), 5000)
            if self._meshManager is not None:
                await self._meshManager.load(0)
        self._updateMenuStates()

    @staticmethod
    def _caseHasResultFields(case_path):
        case = Path(case_path)
        for directory in case.iterdir():
            try:
                float(directory.name)
            except (ValueError, TypeError):
                continue
            if directory.is_dir() and any(path.is_file() for path in directory.iterdir()):
                return True
        return False

    @qasync.asyncSlot()
    async def _loadGeometry(self):
        """Import a model, creating somewhere to put it if there is nowhere.

        This used to return silently with no case open, and the menu entry was
        disabled anyway -- so the most common way to start, "I have a CAD file",
        was the one thing the app would not let you do until you had already
        chosen a folder. And the folder a user actually had was refused by New
        for not being empty and by Open for not being a case.
        """
        if app.project is None and not await self._startScratchCase():
            return
        try:
            await self._stepManager.openGeometryImport()
        except (OSError, RuntimeError, ValueError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Geometry Import Error'), str(error))

    async def _startScratchCase(self, name='untitled') -> bool:
        """Open a case in the temporary directory. The one backend for it.

        Every door that opens without a directory comes through here -- File →
        New Untitled, importing a model with nothing open, and the empty-case
        page -- rather than each making its own case a slightly different way.
        """
        if not await self._closeProject():
            return False
        self._clear()
        try:
            if app.createScratchCase(name) is None:
                return False
        except (OSError, ValueError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Case Create Error'), str(error))
            return False
        self._projectOpened()
        self.statusBar().showMessage(self.tr(
            'Untitled case in a temporary folder. Save it somewhere before '
            'you close FoamMesh.'), 12000)
        return True

    @qasync.asyncSlot()
    async def _actionNewUntitled(self):
        await self._startScratchCase()

    @qasync.asyncSlot()
    async def _openTerminalHere(self):
        if app.project is None:
            return
        try:
            await app.facadeClient.run('client_shell.terminal')
        except (FacadeError, OSError, RuntimeError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Terminal Error'), str(error))

    @qasync.asyncSlot()
    async def _runMeshRepair(self):
        if app.project is None:
            return
        if not self._actionSnapshot().has_mesh:
            await self._runSurfaceRepair()
            return
        utilities = self._repairUtilities()
        check_utility = app.capabilities.utility('checkMesh').executable
        service = MeshRepairService(utilities, app.jobManager, check_utility=check_utility)
        operations = service.available_operations(app.project.path)
        if not operations:
            self.statusBar().showMessage(
                self.tr('No configured mesh repair utility is available for this case.'), 6000)
            return
        labels = [operation.label for operation in operations]
        selected, accepted = QInputDialog.getItem(
            self, self.tr('Mesh Repair'), self.tr('Repair operation:'), labels, 0, False)
        if not accepted:
            return
        operation = operations[labels.index(selected)]
        cell_set = None
        subset_destination = None
        if operation is RepairOperation.SUBSET_CELLS:
            cell_set, accepted = QInputDialog.getText(
                self, self.tr('Subset Mesh'), self.tr('Existing cell-set name:'))
            if not accepted:
                return
            parent = QFileDialog.getExistingDirectory(
                self, self.tr('Select Parent for Subset Case'), app.settings.getRecentLocation(),
                QFileDialog.Option.ShowDirsOnly)
            if not parent:
                return
            name, accepted = QInputDialog.getText(
                self, self.tr('Subset Mesh'), self.tr('New subset case directory name:'),
                text=f'{app.project.name()}-subset')
            if not accepted or not name.strip():
                return
            subset_destination = Path(parent) / name.strip()

        try:
            preview = service.preview(
                app.project.path, RepairRequest(operation, cell_set))
        except (OSError, ValueError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Mesh Repair Unavailable'), str(error))
            return
        warning = self.tr(
            'This operation creates a verified recovery point before changing the mesh.\n'
            'Command: {0}').format(' '.join(preview.command))
        if preview.configuration_path:
            warning += self.tr('\nConfiguration: {0}').format(preview.configuration_path)
        if operation is RepairOperation.SUBSET_CELLS:
            warning = self.tr('This destructive operation runs only in a new copied case.')
        confirmation = QMessageBox.question(
            self, self.tr('Run Mesh Repair'), warning,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if confirmation != QMessageBox.StandardButton.Ok:
            return
        # Repair raised the Console and then left it empty for the whole run.
        # `mesh.repair` publishes JOB_OUTPUT like every other job -- nothing
        # here was listening -- so a thirty-second renumber was indis-
        # tinguishable from a click that did nothing at all.
        unsubscribe = app.facadeClient.subscribe(
            Event.JOB_OUTPUT,
            lambda **payload: MainWindow._appendConsole(
                self, payload.get('line', '')))
        try:
            app.project.assertUnchanged()
            MainWindow._selectConsole(self)
            prior_report = await asyncio.to_thread(
                MeshCheckService.load_report, app.project.path)
            completed = await app.facadeClient.run('mesh.repair', {
                'repair': operation.value, 'cell_set': cell_set,
                'subset_destination': (str(subset_destination)
                                       if subset_destination else None),
            })
        except (FacadeError, OSError, ValueError, RuntimeError, CaseConflictError) as error:
            await AsyncMessageBox().information(self, self.tr('Mesh Repair Error'), str(error))
            return
        finally:
            unsubscribe()
        if completed.status != 'accepted':
            # Say *why*. A rollback with no reason is what made a repair that
            # ran, failed validation and restored itself look like a dead menu
            # item; the service already carries the sentence that explains it.
            detail = (completed.payload.get('validation_error') or
                      (completed.payload.get('job') or {}).get('error') or '')
            self.statusBar().showMessage(
                self.tr('Mesh repair failed; the previous mesh was restored: {0}').format(detail)
                if detail else
                self.tr('Mesh repair failed; the previous mesh was restored.'), 12000)
            return
        before = completed.payload['before']
        after = completed.payload['after']
        summary = self.tr(
            '{0} completed. Mesh bytes: {1:,} â†’ {2:,}; patches: {3} â†’ {4}.').format(
                operation.label, before['total_bytes'], after['total_bytes'],
                len(before['boundary_patches']), len(after['boundary_patches']))
        quality = completed.payload.get('quality')
        if quality is not None:
            prior = self.tr('unchecked')
            if prior_report is not None:
                prior = prior_report.result.severity
                if prior_report.stale:
                    prior = self.tr('{0} (stale)').format(prior)
            summary += self.tr(' Readiness: {0} â†’ {1}.').format(
                prior, quality['severity'])
            # Per-metric before/after delta from the post-repair re-check.
            if prior_report is not None and completed.payload.get('copied_from') is None:
                try:
                    comparison = await app.facadeClient.run(
                        'quality.compare', {'baseline': prior_report.to_dict()})
                    changed = [
                        self.tr('{0} {1}â†’{2}').format(
                            metric.replace('_', ' '), item['baseline'], item['current'])
                        for metric, item in comparison.payload['metrics'].items()
                        if item['trend'] in ('improved', 'regressed')]
                    if changed:
                        summary += self.tr(' Quality delta: {0}.').format('; '.join(changed))
                except (FacadeError, OSError, ValueError, RuntimeError, CaseConflictError):
                    pass
        if completed.payload.get('copied_from') is not None:
            QMessageBox.information(
                self, self.tr('Subset Case Created'),
                summary + '\n' + self.tr('New case: {0}').format(
                    completed.payload['case_path']))
        else:
            app.refreshWorkflowResolution()
            if self._meshManager is not None:
                await self._meshManager.load(0)
            self.statusBar().showMessage(summary, 10000)
        self._updateMenuStates()

    @qasync.asyncSlot()
    async def _restorePreviousMesh(self):
        if app.project is None:
            return
        recovery = await app.facadeClient.run('mesh.recovery.list')
        points = recovery.payload.get('recovery_points', ())
        if not points:
            await AsyncMessageBox().information(
                self, self.tr('Restore Previous Mesh'),
                self.tr('No verified previous-mesh recovery point is available.'))
            self._updateMenuStates()
            return
        payload = points[-1]
        confirmation = QMessageBox.question(
            self, self.tr('Restore Previous Mesh'),
            self.tr('Replace the current mesh with the recovery copy saved before '
                    '"{0}" ({1})?\nThis is an artifact restore, not Undo; the '
                    'current mesh will be replaced and its quality results become stale.').format(
                payload.get('operation', 'unknown'), payload.get('created_at', 'unknown time')),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        try:
            app.project.assertUnchanged()
            outcome = await app.facadeClient.run('mesh.restore')
        except (FacadeError, OSError, ValueError, CaseConflictError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Restore Previous Mesh Failed'), str(error))
            return
        app.refreshWorkflowResolution()
        if self._meshManager is not None:
            self._meshManager.clearFailedCells()
            await self._meshManager.load(0)
        self.statusBar().showMessage(
            self.tr('Restored the mesh saved before "{0}".').format(
                outcome.payload.get('operation', payload.get('operation', 'unknown'))), 8000)
        self._updateMenuStates()

    async def _runSurfaceRepair(self):
        surfaces = {
            g_id: geometry for g_id, geometry in app.db.getElements('geometry').items()
            if geometry.value('shape') == Shape.TRI_SURFACE_MESH.value
            and geometry.value('path')
        }
        if not surfaces or self._geometryManager is None:
            self.statusBar().showMessage(
                self.tr('No imported surface geometry is available for repair.'), 6000)
            return
        labels = [f"{geometry.value('name')} [{g_id}]" for g_id, geometry in surfaces.items()]
        selected, accepted = QInputDialog.getItem(
            self, self.tr('Geometry Repair'), self.tr('Surface:'), labels, 0, False)
        if not accepted:
            return
        g_id = list(surfaces)[labels.index(selected)]
        # Plan 26 WP7.1. This menu offered three operations; the catalogue it
        # now uses has eight, ordered by risk band so a user can tell what is
        # safe to apply blindly from what moves geometry.
        operations = sorted(
            TESSELLATED_ACTIONS,
            key=lambda name: (TESSELLATED_ACTIONS[name].band, name))
        labels = [
            f'{name}  -  {REPAIR_BANDS[TESSELLATED_ACTIONS[name].band][0]}'
            for name in operations]
        operation_label, accepted = QInputDialog.getItem(
            self, self.tr('Geometry Repair'), self.tr('Operation:'),
            labels, 0, False)
        if not accepted:
            return
        operation = operations[labels.index(operation_label)]
        hole_size = 1e6
        if operation == 'tess.fill_holes':
            hole_size, accepted = QInputDialog.getDouble(
                self, self.tr('Fill Surface Holes'), self.tr('Maximum hole size:'),
                1e6, 1e-12, 1e18, 6)
            if not accepted:
                return
        try:
            result = await asyncio.to_thread(
                apply_action,
                self._geometryManager.polyData(g_id), operation,
                hole_size=hole_size)
        except (OSError, ValueError) as error:
            await AsyncMessageBox().information(self, self.tr('Geometry Repair Error'), str(error))
            return
        before_counts = {item.kind: item.count for item in result.before.findings}
        after_counts = {item.kind: item.count for item in result.after.findings}
        summary = self.tr(
            'Before score: {0}/100\nAfter score: {1}/100\n'
            'Open edges: {2} â†’ {3}\nNon-manifold edges: {4} â†’ {5}\n'
            'Duplicate points: {6} â†’ {7}\n\n'
            'Yes: replace the working surface\nNo: save a repaired copy\nCancel: keep unchanged').format(
                result.before.score, result.after.score,
                before_counts.get('open_edges', 0), after_counts.get('open_edges', 0),
                before_counts.get('non_manifold_edges', 0),
                after_counts.get('non_manifold_edges', 0),
                before_counts.get('duplicate_points', 0),
                after_counts.get('duplicate_points', 0))
        choice = QMessageBox.question(
            self, self.tr('Geometry Repair Preview'), summary,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Cancel:
            return
        if choice == QMessageBox.StandardButton.No:
            path, _ = QFileDialog.getSaveFileName(
                self, self.tr('Save Repaired Geometry Copy'),
                str(app.project.path / f"{surfaces[g_id].value('name')}-repaired.stl"),
                self.tr('STL surface (*.stl);;OBJ surface (*.obj)'))
            if path:
                try:
                    await asyncio.to_thread(write_surface, result.polydata, path)
                    self.statusBar().showMessage(
                        self.tr('Saved repaired geometry copy to {0}.').format(path), 8000)
                except (OSError, ValueError) as error:
                    await AsyncMessageBox().information(
                        self, self.tr('Save Repaired Geometry Error'), str(error))
            return
        data = app.facadeClient.checkout()
        data.updateGeometryPolyData(surfaces[g_id].value('path'), result.polydata)
        await app.facadeClient.commit_working_copy(
            data, action=f'repair geometry: {operation}', target=str(g_id))
        self._geometryManager.update(g_id, result.polydata)
        self._geometryManager.applyToDisplay()
        self.statusBar().showMessage(self.tr('Working surface replaced with repaired geometry.'), 6000)
        self._updateMenuStates()

    @qasync.asyncSlot()
    async def _loadNativeMesh(self):
        if app.project is None:
            return
        utilities = self._converterUtilities()
        service = ConverterImportService(utilities, app.jobManager)
        dialog = MeshImportDialog(service.import_entries(), self)
        if await asyncExec(dialog) != dialog.DialogCode.Accepted:
            return
        entry = dialog.selected_entry()
        if entry is None:
            return
        if entry.entry_id == 'native':
            await self._loadNativeMeshFromCase()
            return

        fmt = ConverterFormat(entry.entry_id)
        source, _ = QFileDialog.getOpenFileName(
            self, self.tr('Select {0}').format(fmt.label), app.settings.getRecentLocation(), fmt.file_filter())
        if not source:
            return
        confirmation = QMessageBox.question(
            self, self.tr('Convert and Replace Mesh'),
            self.tr('Convert this file into the current case? The current mesh will be retained as a recovery copy.'),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if confirmation != QMessageBox.StandardButton.Ok:
            return
        try:
            app.project.assertUnchanged()
            MainWindow._selectConsole(self)
            completed = await app.facadeClient.run(
                'mesh.import.converter', {'format': fmt.value, 'source': source})
        except (FacadeError, OSError, RuntimeError, ValueError, CaseConflictError) as error:
            await AsyncMessageBox().information(self, self.tr('Mesh Conversion Error'), str(error))
            return
        if completed.status != 'accepted':
            detail = (completed.payload.get('validation_error') or
                      completed.payload.get('job', {}).get('error') or 'conversion failed')
            recovery = (self.tr('The previous mesh was restored.')
                        if completed.payload.get('restored') else
                        self.tr('No replacement mesh was committed.'))
            await AsyncMessageBox().information(
                self, self.tr('Mesh Conversion Failed'), f'{detail}\n{recovery}')
            return
        app.refreshWorkflowResolution()
        self._stepManager.load()
        if self._meshManager is not None:
            await self._meshManager.load(0)
        await self._showConversionSummary(fmt, completed)
        self._updateMenuStates()

    async def _showConversionSummary(self, fmt, completed):
        """Â§13.2 step 8: conversion warnings plus a patch summary after commit."""
        lines = [self.tr('{0} conversion completed.').format(fmt.label)]
        try:
            info = await asyncio.to_thread(MeshInfoService().inspect, app.project.path)
        except (OSError, ValueError):
            info = None
        if info is not None:
            names = ', '.join(info.boundary_patches[:8])
            if len(info.boundary_patches) > 8:
                names += ', ...'
            lines.append(self.tr('Patches ({0}): {1}').format(len(info.patches), names))
            if info.cells is not None:
                lines.append(self.tr('Cells: {0:,}').format(info.cells))
        warnings = extract_converter_warnings(
            completed.payload.get('job', {}).get('output', ''))
        if warnings:
            lines.append('')
            lines.append(self.tr('Converter warnings:'))
            lines.extend(warnings)
        await AsyncMessageBox().information(self, self.tr('Mesh Conversion'), '\n'.join(lines))

    @staticmethod
    def _converterUtilities():
        return {
            fmt.utility_name: capability.executable
            for fmt in ConverterFormat
            if (capability := app.capabilities.utility(fmt.utility_name)).available
            and capability.executable is not None
        }

    async def _loadNativeMeshFromCase(self):
        source = QFileDialog.getExistingDirectory(
            self, self.tr('Select OpenFOAM Mesh Case'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not source:
            return
        # Â§13.1 native import contract: ask copy versus replace explicitly.
        box = QMessageBox(self)
        box.setWindowTitle(self.tr('Import Native Mesh'))
        box.setText(self.tr('How should the selected polyMesh be imported?'))
        replace = box.addButton(self.tr('Replace Current Mesh'), QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton(self.tr('Import into New Case Copy'), QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)  # destructive choice is never the default
        box.setInformativeText(self.tr(
            'Replace keeps the current mesh as a recovery copy. '
            'A new case copy leaves this case untouched.'))
        await asyncExec(box)
        if box.clickedButton() is replace:
            await self._replaceNativeMesh(source)
        elif box.clickedButton() is copy:
            await self._importNativeMeshIntoCopy(source)

    async def _replaceNativeMesh(self, source):
        try:
            app.project.assertUnchanged()
            await app.facadeClient.run('mesh.import.native', {'source': source})
        except (FacadeError, OSError, ValueError, CaseConflictError) as error:
            await AsyncMessageBox().information(self, self.tr('Mesh Import Error'), str(error))
            return
        app.refreshWorkflowResolution()
        self._stepManager.load()
        if self._meshManager is not None:
            await self._meshManager.load(0)
        self.statusBar().showMessage(self.tr('Native mesh import completed.'), 5000)
        self._updateMenuStates()

    async def _importNativeMeshIntoCopy(self, source):
        parent = QFileDialog.getExistingDirectory(
            self, self.tr('Select Parent for New Case Copy'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not parent:
            return
        name, accepted = QInputDialog.getText(
            self, self.tr('Import into New Case Copy'), self.tr('New case directory name:'),
            text=f'{app.project.name()}-imported')
        if not accepted or not name.strip():
            return
        try:
            destination = Path(parent) / name.strip()
            result = await app.facadeClient.run('mesh.import.native', {
                'source': source, 'copy_destination': str(destination)})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            await AsyncMessageBox().information(self, self.tr('Mesh Import Error'), str(error))
            return
        open_copy = QMessageBox.question(
            self, self.tr('Import Complete'),
            self.tr('Imported the mesh into {0}. Open that case now?').format(
                result.payload['target_case']),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if open_copy == QMessageBox.StandardButton.Yes:
            await self._openProject(result.payload['target_case'])
        else:
            self.statusBar().showMessage(
                self.tr('Imported mesh into new case {0}.').format(
                    result.payload['target_case']), 8000)

    @qasync.asyncSlot()
    async def _saveProjectAs(self):
        if app.project is None or not await self._stepManager.saveCurrentPage():
            return
        if app.isScratchCase():
            await self._relocateScratchCase()
            return
        await app.facadeClient.run('case.save')
        parent = QFileDialog.getExistingDirectory(
            self, self.tr('Select Copy Parent Directory'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not parent:
            return
        name, accepted = QInputDialog.getText(
            self, self.tr('Save Project As'), self.tr('New case directory name:'),
            text=f'{app.project.name()}-copy')
        if not accepted or not name.strip():
            return
        try:
            result = await app.facadeClient.run(
                'case.copy', {'destination': str(Path(parent) / name.strip())})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            await AsyncMessageBox().information(self, self.tr('Copy Error'), str(error))
            return
        open_copy = QMessageBox.question(
            self, self.tr('Copy Complete'), self.tr('Open the copied case now?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if open_copy == QMessageBox.StandardButton.Yes:
            await self._openProject(result.payload['destination'])
        else:
            self.statusBar().showMessage(self.tr('Copied full case to {0}.').format(
                result.payload['destination']), 8000)

    async def _relocateScratchCase(self, note: str = None) -> bool:
        """Give an untitled case a permanent home. Returns whether it moved.

        A *move*, not a copy: this is the same case arriving at the address it
        should have had, so it takes its history with it -- a case that forgot
        everything it did on the way there has lost real provenance. The
        temporary original is removed once the destination is open, and only
        then, so a failure anywhere leaves the work exactly where it was.
        """
        from foammesh.core.case import discard_scratch_case, suggested_name

        if self._relocatingScratch:
            return False
        scratch = Path(app.project.path)
        # D7. Folder chooser, then name prompt, then a confirmation about the
        # save that had already happened. Folder and name belong on one sheet
        # -- the same sheet Save As already uses -- and it is the sheet that
        # tells the user where the case will land.
        dialog = NewProjectDialog(
            self, self.tr('Save case'),
            Path(app.settings.getRecentLocation()).resolve(),
            note=note or self.tr(
                'This case is untitled and lives in a temporary folder that '
                'is cleaned up automatically.'),
            name=suggested_name(scratch))
        if await asyncExec(dialog) != QDialog.DialogCode.Accepted:
            return False
        destination = Path(dialog.projectPath())

        await app.facadeClient.run('case.save')
        try:
            result = await app.facadeClient.run('case.copy', {
                'destination': str(destination), 'carry_history': True})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Save Error'), str(error))
            return False

        # Opening the destination closes the scratch case, which would put the
        # "this case has never been saved" guard in front of a case that is
        # being saved right now -- a second prompt whose only honest answers
        # are the one already taken. Measured, not reasoned about: it deadlocks
        # the relocation outright, because nothing ever answers it.
        # R168. Reopening the case rebuilds the panel from the saved step, so
        # a save asked for by Base Grid's own Generate button landed the user
        # on 2. Repair for the whole run. The save moves the case, not the
        # user.
        route = self._stepManager.routeMemo()
        self._relocatingScratch = True
        try:
            await self._openProject(result.payload['destination'])
        finally:
            self._relocatingScratch = False
        if Path(app.project.path if app.project else '') != destination.resolve():
            # The copy is on disk but did not open. Say so and keep the
            # temporary original, which is now the only openable copy.
            self.statusBar().showMessage(self.tr(
                'Saved to {0}, but it could not be opened; the untitled case '
                'is still open.').format(destination), 15000)
            return False

        discard_scratch_case(scratch)
        self._stepManager.restoreRouteMemo(route)
        self.statusBar().showMessage(
            self.tr('Case saved to {0}.').format(destination), 8000)
        return True

    async def _requireSavedCase(self, what: str) -> bool:
        """Make an untitled case choose a home before it produces something.

        Meshing into a temporary directory is how a user loses an afternoon:
        the result is real, the folder is not, and nothing said so at the time.
        The prompt is offered once, at the point the work starts to matter,
        rather than as a modal on the way in.
        """
        if not app.isScratchCase():
            return True
        # D7. This used to be a message box whose only content was the reason,
        # in front of a folder chooser, in front of a name prompt: three
        # dialogs to answer one question. The save sheet now carries the
        # reason, and Cancel on it is the same "no" the message box offered.
        return await self._relocateScratchCase(
            self.tr('This case is untitled and lives in a temporary folder, '
                    'which is cleaned up automatically. Choose somewhere to '
                    'keep it before {0}.').format(what))

    async def _saveConflictCopy(self):
        parent = QFileDialog.getExistingDirectory(
            self, self.tr('Select Save Copy Parent Directory'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not parent:
            return False
        name, accepted = QInputDialog.getText(
            self, self.tr('Save Copy'), self.tr('New case directory name:'),
            text=f'{app.project.name()}-conflict-copy')
        if not accepted or not name.strip():
            return False
        try:
            result = await app.facadeClient.run(
                'case.copy', {'destination': str(Path(parent) / name.strip())})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            await AsyncMessageBox().information(self, self.tr('Save Copy Error'), str(error))
            return False
        self.statusBar().showMessage(
            self.tr('Saved a conflict-safe copy to {0}.').format(
                result.payload['destination']), 8000)
        return True

    def _chooseTheme(self):
        current = app.themeManager.mode.value.title()
        choices = [ThemeMode.SYSTEM.value.title(), ThemeMode.LIGHT.value.title(), ThemeMode.DARK.value.title()]
        selected, accepted = QInputDialog.getItem(self, self.tr('Theme'), self.tr('Appearance'),
                                                   choices, choices.index(current), False)
        if accepted:
            app.themeManager.set_mode(selected.lower())

    def _openRecent(self, path):
        self._openProject(path)

    def _clearRecents(self):
        app.settings.clearRecents()
        self._recentFilesMenu.setRecents([])
        self.statusBar().showMessage(self.tr('Recent cases cleared.'), 3000)

    def _actionNew(self):
        path = QFileDialog.getExistingDirectory(
            self, self.tr('Select Empty Case Directory'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if path:
            self._createInPlaceCase(Path(path))

    def _actionOpen(self):
        self._dialog = QFileDialog(self, self.tr('Select Project Directory'), app.settings.getRecentLocation())
        if platform.system() == 'Darwin':  # "show()" for native File dialog does not seem to work on macOS
            self._dialog.setOption(QFileDialog.Option.DontUseNativeDialog)

        self._dialog.setFileMode(QFileDialog.FileMode.Directory)
        self._dialog.fileSelected.connect(self._openProject)
        self._dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._dialog.show()

    @qasync.asyncSlot()
    async def _actionSave(self):
        if app.project is not None and await self._stepManager.saveCurrentPage():
            try:
                # The facade owns the write, so save-time conflict detection has
                # to be asserted here; otherwise a case edited outside FoamMesh
                # would be overwritten without ever offering reload/save-copy.
                app.project.assertUnchanged()
                await app.facadeClient.run('case.save')
            except CaseConflictError as error:
                choice = await AsyncMessageBox().question(
                    self, self.tr('Case Changed Outside FoamMesh'),
                    self.tr('{0}\n\nYes: Reload case\nNo: Save Copy\nCancel: return without saving.').format(error),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                    QMessageBox.StandardButton.Cancel)
                if choice == QMessageBox.StandardButton.Yes:
                    await self._reloadAfterConflict()
                elif choice == QMessageBox.StandardButton.No:
                    await self._saveConflictCopy()
                return
            self._updateMenuStates()

    async def _reloadAfterConflict(self):
        if app.project is None:
            return
        path = app.project.path
        app.closeProject()  # Reload explicitly discards the conflicted in-memory state.
        self._projectClosed()
        self._clear()
        try:
            app.openCase(path)
            self._projectOpened()
        except (OSError, ValueError, Timeout) as error:
            await AsyncMessageBox().information(self, self.tr('Reload Error'), str(error))

    def _actionSaveAs(self):
        self._dialog = NewProjectDialog(self, self.tr('Save as new project'),
                                        Path(app.settings.getRecentLocation()).resolve(), meshAppProperties.projectSuffix)
        self._dialog.pathSelected.connect(self._saveAs)
        self._dialog.open()

    def _actionParameters(self):
        self._dialog = MeshQualityParametersDialog(self)
        self._dialog.open()

    def _openParallelEnvironmentDialog(self):
        self._dialog = ParallelEnvironmentDialog(self, app.project.parallelEnvironment())
        if app.fileSystem.timePathExists(1, app.project.parallelCores() > 1):
            self._dialog.setReadOnly()
        self._dialog.accepted.connect(self._updateParallelEnvironment)
        self._dialog.open()

    def _actionAbout(self):
        self._dialog = AboutDialog(self)
        self._dialog.open()

    def _openLicense(self):
        self._dialog = LicenseDialog(self)
        self._dialog.open()

    def _openTutorials(self):
        if not QDesktopServices.openUrl(tutorial_url()):
            self.statusBar().showMessage(self.tr('Could not open the FoamMesh tutorials.'), 6000)

    @qasync.asyncSlot()
    async def _openPrivacySettings(self):
        await Analytics().editConsent(parent=self)

    @qasync.asyncSlot()
    async def _createInPlaceCase(self, path):
        if not await self._closeProject():
            return
        self._clear()

        # H5. This refused every folder that was not brand new -- including
        # one holding a case made in the same folder minutes earlier, which
        # came back as `new case directory is not empty` or, through the
        # chooser, as the flatly wrong `is not a FoamMesh project`. A folder
        # that already holds a case is not an obstruction; it is the case the
        # user meant. Offer it instead of refusing it.
        from foammesh.core.case import CaseKind, classify_case

        path = Path(path)
        kind = None
        reasons = ''
        if path.is_dir():
            try:
                classification = classify_case(path)
            except (OSError, ValueError):
                classification = None
            if classification is not None:
                kind = classification.kind
                reasons = '; '.join(classification.reasons)

        if kind is CaseKind.FOAMMESH_CASE:
            answer = await AsyncMessageBox().question(
                self, self.tr('Case Already Here'),
                self.tr('{0} already holds a FoamMesh case.\n\n'
                        'Open it instead?').format(path.name))
            if answer == QMessageBox.StandardButton.Yes:
                await self._openProject(str(path))
            return
        if kind in {CaseKind.OPENFOAM_CASE, CaseKind.RAW_POLY_MESH_CASE}:
            answer = await AsyncMessageBox().question(
                self, self.tr('Case Already Here'),
                self.tr('{0} already holds an OpenFOAM case.\n\n'
                        'Adopt it instead? Its mesh and dictionaries are '
                        'read where they are; nothing is copied or '
                        'overwritten.').format(path.name))
            if answer == QMessageBox.StandardButton.Yes:
                await self._openProject(str(path))
            return
        if kind is CaseKind.INVALID:
            await AsyncMessageBox().information(
                self, self.tr('Case Create Error'),
                self.tr('{0} cannot hold a new case: {1}').format(
                    path, reasons or self.tr('it is not empty')))
            return

        try:
            if app.createInPlaceCase(path):
                self._recentFilesMenu.addRecentest(app.project.path)
                self._projectOpened()
        except (FileExistsError, OSError, ValueError) as error:
            await AsyncMessageBox().information(
                self, self.tr('Case Create Error'), str(error))

    # Compatibility entry point for callers that still invoke the old name.
    async def _createProject(self):
        if self._dialog is None:
            return
        path = getattr(self._dialog, 'projectPath', lambda: None)()
        if path:
            await self._createInPlaceCase(path)

    @qasync.asyncSlot()
    async def _openProject(self, file):
        if not await self._closeProject():
            return
        self._clear()

        path = Path(file)
        try:
            app.openCase(path)
            self._recentFilesMenu.addRecentest(app.project.path)
            self._projectOpened()
        except FileNotFoundError:
            app.settings.removeRecent(path)
            self._recentFilesMenu.setRecents(app.settings.getRecentCases())
            # H5. This said `is not a FoamMesh project` for a folder that had
            # simply been moved, renamed or deleted -- a diagnosis of the
            # wrong thing entirely, and one that sent people looking for a
            # case format problem that was not there.
            await AsyncMessageBox().information(
                self, self.tr('Project Open Error'),
                self.tr('{0} is no longer there. It has been moved, renamed '
                        'or deleted since it was last opened.').format(path))
        except Timeout:
            await AsyncMessageBox().information(self, self.tr('Project Open Error'),
                                                self.tr('{0} is already open in another program.').format(path.name))
        except ValidationError as e:
            await AsyncMessageBox().information(self, self.tr('Project Open Error'),
                                                self.tr(f'configurations error : {e.path} - {e.name}'))
        except ValueError as error:
            await AsyncMessageBox().information(
                self, self.tr('Project Open Error'), str(error))

    @qasync.asyncSlot()
    async def _saveAs(self, path):
        if await self._stepManager.saveCurrentPage():
            progressDialog = ProgressDialog(self, self.tr('Save FoamMesh State As'))
            progressDialog.open()

            progressDialog.setLabelText(self.tr(
                'Saving FoamMesh state only; native geometry and mesh files are not copied'))
            try:
                await asyncio.to_thread(app.project.saveStateAsCase, path)
            except (OSError, ValueError, FileExistsError) as error:
                progressDialog.close()
                await AsyncMessageBox().information(self, self.tr('Save State Error'), str(error))
                return
            progressDialog.close()
            open_copy = QMessageBox.question(
                self, self.tr('State Copy Complete'),
                self.tr('FoamMesh state was saved without native artifacts. Open it now?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if open_copy == QMessageBox.StandardButton.Yes:
                await self._openProject(str(path))
            else:
                self.statusBar().showMessage(
                    self.tr('Saved FoamMesh state to {0}.').format(path), 8000)

    def _disableMenubar(self):
        self._ui.menuFile.setEnabled(False)
        self._ui.menuMesh.setEnabled(False)
        self._ui.menuParallel.setEnabled(False)

    def _enableMenubar(self):
        self._ui.menuFile.setEnabled(True)
        self._ui.menuMesh.setEnabled(True)
        self._ui.menuParallel.setEnabled(True)
        self._updateMenuStates()

    @qasync.asyncSlot()
    async def _closeProject(self, toQuit=False):
        # A queued close signal can outlive the native QMainWindow when a
        # second, already-authorized close arrives before this coroutine is
        # scheduled. Never dereference a deleted Qt wrapper.
        from shiboken6 import isValid
        if not isValid(self):
            return False
        if app.project is None:
            if toQuit:
                if self._readyToQuit:
                    return True
                self._readyToQuit = True
                self.close()
            return True

        if app.jobManager.active_job_ids:
            choice = await AsyncMessageBox().question(
                self, self.tr('Active Operation'),
                self.tr('A case operation is running.\n\nYes: Cancel Operation\n'
                        'No: Keep Running and keep this case open\nCancel: Return'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                QMessageBox.StandardButton.Cancel)
            if choice == QMessageBox.StandardButton.Yes:
                await app.facadeClient.cancel_all_jobs()
                for _ in range(100):
                    if not app.jobManager.active_job_ids:
                        break
                    await asyncio.sleep(0.05)
                if app.jobManager.active_job_ids:
                    self.statusBar().showMessage(
                        self.tr('Cancellation is still completing; the case remains open.'), 5000)
                    return False
            else:
                return False

        if not await self._stepManager.saveCurrentPage():
            return False

        # D7. A relocation has just written the case; asking again whether
        # to save it is the fourth dialog of a four-dialog save, and its only
        # honest answer is the one already given.
        if app.project.isDirty and not self._relocatingScratch:
            confirm = await AsyncMessageBox().question(self, self.tr('Save Changed'),
                                                       self.tr('Do you want to save your changes?'),
                                                       QMessageBox.StandardButton.Ok
                                                       | QMessageBox.StandardButton.Cancel
                                                       | QMessageBox.StandardButton.Discard)

            if confirm == QMessageBox.StandardButton.Ok:
                try:
                    await app.facadeClient.run('case.save')
                except CaseConflictError as error:
                    await AsyncMessageBox().information(
                        self, self.tr('Case Changed Outside FoamMesh'), str(error))
                    return False
            elif confirm == QMessageBox.StandardButton.Cancel:
                return False

        # The one guard that actually prevents loss. An untitled case is in a
        # directory that gets swept, and "save" above only wrote it back to
        # that same doomed folder -- so closing is the last moment anyone can
        # be told, and it must name the folder rather than say "unsaved".
        if app.isScratchCase() and not self._relocatingScratch:
            scratch = Path(app.project.path)
            confirm = await AsyncMessageBox().question(
                self, self.tr('Keep this untitled case?'),
                self.tr('This case has never been saved anywhere permanent. '
                        'It is in a temporary folder that is cleaned up '
                        'automatically:\n\n{0}\n\n'
                        'Ok: choose where to keep it\n'
                        'Discard: close and let it be deleted\n'
                        'Cancel: go back').format(scratch),
                QMessageBox.StandardButton.Ok
                | QMessageBox.StandardButton.Cancel
                | QMessageBox.StandardButton.Discard)
            if confirm == QMessageBox.StandardButton.Cancel:
                return False
            if confirm == QMessageBox.StandardButton.Ok:
                if not await self._relocateScratchCase():
                    return False
                # Relocation opened the saved case, so the scratch one this
                # call was asked to close no longer exists. Close that.
                return await self._closeProject(toQuit)

        app.closeProject()
        self._projectClosed()
        self._clear()

        if toQuit:
            self._readyToQuit = True
            self.close()

        return True

    @qasync.asyncSlot()
    async def _updateParallelEnvironment(self):
        environment = self._dialog.environment()

        progressDialog = ProgressDialog(self._dialog, self.tr('Case Redistribution'))
        progressDialog.setLabelText(self.tr('Redistributing the case...'))
        progressDialog.open()
        await app.facadeClient.run('case.parallel.configure', {
            'parallel': {
                'cores': environment.np(), 'type': environment.type().name,
                'hosts': environment.hosts(),
            },
            'on_progress': progressDialog.setLabelText})
        if self._meshManager is not None:
            await self._meshManager.reload()
        app.capabilities.refresh()
        app.events.publish(
            Event.CAPABILITIES_CHANGED, case_id=str(app.project.path))
        progressDialog.finish('Parallel Environment was Applied.')

    def _projectOpened(self):
        self._recentFilesMenu.updateRecentest(app.project.path)
        # MEASURED 2026-09-03: the start-up prompt "Open a case or create a
        # new case to begin." stayed in the status bar through the whole
        # meshing route, because nothing ever cleared a message that has no
        # timeout. An open project is the answer to that prompt.
        self.statusBar().clearMessage()

        # 10MB(=10,485,760=1024*1024*10)
        self._handler = RotatingFileHandler(app.project.storagePath / 'log.txt', maxBytes=10485760, backupCount=5)
        self._handler.setFormatter(logging.Formatter("[%(asctime)s][%(name)s] ==> %(message)s"))
        logging.getLogger().addHandler(self._handler)
        scene_events = (
            Event.TRANSACTION_APPLIED, Event.UNDONE, Event.REDONE,
            Event.ARTIFACT_GEOMETRY_CHANGED, Event.ARTIFACT_MESH_CHANGED,
            Event.ARTIFACT_RESTORED,
        )
        menu_events = (
            Event.ARTIFACT_QUALITY_CHANGED, Event.ARTIFACT_STALE,
            Event.WORKFLOW_MODE_CHANGED, Event.WORKFLOW_STEP_STALE,
            Event.PROJECT_SAVED, Event.CAPABILITIES_CHANGED,
        )
        self._projectEventUnsubscribes = [
            app.events.subscribe(event, self._scheduleProjectRefresh)
            for event in scene_events
        ] + [
            app.events.subscribe(event, self._scheduleMenuRefresh)
            for event in menu_events
        ] + [
            # WP3.1. A verdict for a mesh the settings have moved on from must
            # not keep reading as current.
            app.events.subscribe(
                Event.MESH_VERDICT_STALE, self._meshVerdictSuperseded),
        ] + [
            # R143. MEASURED: Snappy QA ran checkMesh -allTopology
            # -allGeometry -writeSets on the 67,662-cell mesh, wrote every
            # metric plus `failed_checks: 1` and the concave-cell finding to
            # foammesh/quality/latest.json, and set the outline row to
            # warning -- and the Mesh Quality tab underneath still read "This
            # mesh has not been checked", the strip "Quality limits: not
            # checked". Both surfaces read that exact file, through
            # `quality.report`; nothing ever asked them to read it again.
            # ARTIFACT_QUALITY_CHANGED is what every writer of that report
            # publishes -- mesh.check, the pipeline's own final check, the
            # SU2 readiness pass -- and it was subscribed to the menu refresh
            # alone, which repaints menu enablement and touches no verdict.
            # The mesh-changed events that do refresh the verdict all fire
            # *before* the check runs, so the surfaces were showing the
            # pre-check answer and inviting a two-minute re-run of the check
            # that had just finished.
            app.events.subscribe(
                Event.ARTIFACT_QUALITY_CHANGED,
                lambda **_payload: self.refreshMeshVerdictSoon()),
        ]

        self._updateWindowTitle()

        self._geometryManager = GeometryManager()
        self._meshManager = MeshManager()
        self._meshManager.cellCountChanged.connect(self._cellCountChanged)

        self._geometryManager.load()
        self._stepManager.load()
        asyncio.create_task(self.drawStoredResult())
        # An opened case may already carry a checkMesh report. Reading it here
        # is what makes the quality surfaces reflect the mesh a user just
        # loaded rather than only one this session generated.
        self.refreshMeshVerdictSoon()
        self.refreshNamedViews()
        self._updateMenuStates()

    def _shouldDrawMeshOnOpen(self) -> bool:
        """Does this case have a mesh the viewport should be showing?

        R210. This asked `workflow is MESH_EXTERNAL` -- true only for a mesh
        somebody else produced -- so a case this application meshed itself
        reopened showing the STL it started from, with the mesh sitting in
        `constant/polyMesh` untouched.

        MEASURED on tee_gmsh_r2, booted through the real window: zero actors,
        zero cells, over a polyMesh holding 39,921 cells, four patches and
        three cell zones. `meshManager.load(0)` on that same window returned
        eight actors and the whole count. The load path was never broken.
        Nothing called it.

        An externally sourced mesh still answers yes on its own account: its
        polyMesh may be somewhere `_hasMeshOnDisk` does not look, and that mesh
        is the entire content of the case.
        """
        if app.workflowResolution.workflow is WorkflowMode.MESH_EXTERNAL:
            return True
        return self._hasMeshOnDisk()

    def _projectClosed(self):
        if hasattr(self, '_projectEventUnsubscribes'):
            for unsubscribe in self._projectEventUnsubscribes:
                unsubscribe()
            del self._projectEventUnsubscribes
        if hasattr(self, '_handler'):
            logging.getLogger().removeHandler(self._handler)
            self._handler.close()
            del self._handler
        self._updateMenuStates()

    def _clear(self):
        self.setWindowTitle(f'{meshAppProperties.fullName}')
        self.setToolTip('')
        self._stepManager.unload()
        # The actors go first: the toolbar readout is derived from what the
        # view is holding, so clearing it before the view leaves the closing
        # case's model extent on screen (R49).
        self._displayControl.clear()
        self._renderingTool.clear()
        self._consoleView.clear()
        self._viewportOverlay.setParts([])
        # The run line named a mesh from the case that is closing.
        self._viewportOverlay.setResult('')
        # The sets belong to the case that is closing; carrying them into the
        # next one would enable a button that acts on cells that are gone.
        self._lastFailedCellSets = {}
        self._geometryManager = None
        self._meshManager = None

        self._cellCountChanged(0)
        self._updateMenuStates()

    def _cellCountChanged(self, visible: int, total: int = 0):
        """The cell readout, and *which mesh* it counts.

        F-44. The number changed the moment a section plane was dragged, with
        nothing to say whether the mesh had been cut or was simply that size.

        CP-09 item 6 finishes the sentence. "12,004 / 39,921 shown" is honest
        about the fraction and silent about what follows from it: while a
        section is cut, every statistic on screen -- the quality range, the
        worst-cell colouring, the ruler -- is a statistic about the section.
        The readout is labelled, so a slice-only number is never read as a
        whole-mesh one.
        """
        text, tip = cell_count_text(visible, total)
        self._ui.cellCount.setText(text)
        self._ui.cellCount.setToolTip(tip)
