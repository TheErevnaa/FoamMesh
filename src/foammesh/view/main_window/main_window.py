#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
import platform
import threading
import warnings
from functools import partial

import qasync
from filelock import Timeout
from foammesh.core.quantities import aligned
from logging.handlers import RotatingFileHandler
from foammesh.support.deferred_log import DeferredHandler
from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QMainWindow, QMenu, QFileDialog,
                               QMessageBox, QInputDialog, QVBoxLayout,
                               QTabWidget, QPlainTextEdit, QSizePolicy,
                               QWidget, QPushButton)
from PySide6.QtCore import QObject, QPoint, QRect, Signal, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction, QDesktopServices, QGuiApplication, QKeySequence, QShortcut)

from analytics import Analytics
from app_properties import meshAppProperties

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.support.utils import getFit
from foammesh.support import gc_policy
from foammesh.db.configurations_schema import Shape, Step
from widgets.async_dialog import asyncExec
from widgets.rendering import rendering_widget as rendering_widget_module
from widgets.async_message_box import AsyncMessageBox
from widgets.new_project_dialog import NewProjectDialog
from widgets.progress_dialog import ProgressDialog, closeProgressWindowsOf
from foammesh.db.project import StateSaveCancelled

from foammesh.app import app
from foammesh.core.facade import FacadeError
from foammesh.core.documentation import tutorial_url
from foammesh.core.case import CaseConflictError, WorkflowMode
from foammesh.core.shell import (
    ACTION_OBJECT_NAMES, ActionId, ActionPolicy, AppSnapshot, Capability,
    ContentState, JobState, job_state_from_manager, mesh_quality_capability,
    terminal_capability,
)
from foammesh.core.shell.action_policy import SAVE_WHILE_RUNNING
from foammesh.core.mesh import (
    MeshInfoService, MeshRecoveryService, MeshRepairService,
    RepairOperation, RepairRequest,
)
from foammesh.core.quality import MeshCheckService
from foammesh.core.quality import discover_cell_set_files, sets_of_this_mesh
from foammesh.rendering import layers_grown
from foammesh.core.gmsh import quality as gmsh_quality

#: The legend of the volume's exterior colouring in the viewport's stack.
VOLUME_LEGEND_KEY = 'meshColour'
from foammesh.core.quality import (QualityReport, apply_layer_coverage,
                                   apply_waivers,
                                   verdict_from_report, verdict_with_gate)
from foammesh.core.project import Event
from foammesh.core.import_export import (
    ConverterFormat, ConverterImportService, extract_converter_warnings,
)
from foammesh.view.display_control.display_control import (
    DisplayControl, countedPartIds)
from foammesh.view.display_control import view_modes
from foammesh.view.engine_context import (
    configured_engine, engine_from_listing, refresh_engine_context,
    refresh_mesh_provenance,
    wire_mesh_provenance)
from foammesh.view.widgets.job_progress import JobProgressWidget
from foammesh.view.widgets.justification_dialog import JustificationDialog
from foammesh.view.widgets.mesh_import_dialog import MeshImportDialog
from foammesh.view.menu.help.about_dialog import AboutDialog
from foammesh.view.menu.help.license_dialog import LicenseDialog
from foammesh.support import lifecycle
from .crash_notice import CrashNoticeBanner, CrashReportDialog
from foammesh.view.menu.settings.preferences_dialog import PreferencesDialog
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.menu.mesh import MeshInfoDialog, QualityDashboardDialog, TransformDialog
from foammesh.view.geometry.geometry_manager import GeometryManager
from widgets.themed_icon import load_themed_icon
from foammesh.view.widget_factory import compose, make
from foammesh.view.theming.fonts import warm_application_font
from .recent_files_menu import RecentFilesMenu
from .navigation_view import NavigationView
from .step_ribbon import StepRibbon
from .rendering_tool import RenderingTool
from .console_view import Console
from .run_panel import install_run_panel
from foammesh.core.mesh.presentation import count_text
from .mesh_composition import cell_count_text, composition_text
from .mesh_manager import MeshManager
from .step_manager import StepManager
from .history_panel import HistoryPanel
from .notification_centre import install_notification_centre
from .main_window_ui import Ui_MainWindow
from .recovery_offer import offer_recovery
from .wsl_health_bar import WslHealthBar, show_runtime_diagnostics
from .retry_prompt import RetryPrompt
from .dock_layout import OUTLINE_DOCK, dock_the_regions
from .settings_sheet import NOTE as SETTINGS_SHEET_NOTE, SettingsSheet
from .three_region_shell import (
    REGION_A_MINIMUM, install_three_region_shell, layout_worth_recording,
    reveal_output_band,
)
from .output_tabs import OutputTabRegistry
from .capture_manager import CaptureManager
from .captures_page import CapturesPage
from foammesh.core.capture import captures_dir, delete_capture
from foammesh.core.quality.geometry_fidelity import hotspot
from foammesh.rendering import boundary_feature_edges, feature_overlay
from foammesh.core.quality.geometry_fidelity import report as fidelity_report
from foammesh.core.viewport_state import (
    ViewStateError, delete_view, load_views, save_view)
from foammesh.view.display_control.region_picker import (
    RegionFilter, picker_entries)
from foammesh.view.display_control.viewport_overlay import (
    ViewportOverlay, part_role)
from foammesh.view.main_window.shortcut_registry import (
    REGISTRY as SHORTCUTS, adopt as adoptShortcut)
from foammesh.view.main_window.viewport_rail import ViewportRail
from foammesh.view.main_window.viewport_tool_edges import ViewportToolEdges
from foammesh.view.main_window.legend_controller import LegendController
from foammesh.view.display_control.deviation_panel import (
    DeviationPanel, deviationScalarBar, markDeviationBar)
from foammesh.core.quality.geometry_fidelity import live_distance
from .mesh_quality_tab import MeshQualityTab
from foammesh.core.geometry.diagnostics.repair import (
    REPAIR_BANDS, TESSELLATED_ACTIONS, apply_action, write_surface,
)
from foammesh.view.facade_client import FailedResult, query, submit
from foammesh.view.outside_task import modal

from .run_narration import describe_cancellation, describe_start
from .run_status_strip import RunStatusStrip
from widgets import async_message_box as messages


logger = logging.getLogger(__name__)

#: How long the verdict strip waits for a stored quality report before it
#: settles for "not checked". Reading a persisted report is a filesystem
#: operation; anything slower than this is a fault, not a slow disk.
QUALITY_READ_TIMEOUT = 5.0


def with_layer_coverage(verdict: dict) -> dict:
        """DP-664. The verdict, with any requested layers that were not grown.

        checkMesh grades the cells that exist, so a layer stage that grew
        nothing left the strip and Export reading `Every metric grades good`
        while the layer record said `no prism layers were added`. A stale
        verdict describes a different mesh and is left as it is.
        """
        if not verdict or verdict.get('stale'):
            return verdict
        try:
            result = query(app.facadeClient, 'mesh.layer_coverage')
        except (FacadeError, RuntimeError, OSError, ValueError, KeyError,
                AttributeError):
            logger.debug('no readable layer coverage', exc_info=True)
            return verdict
        payload = getattr(result, 'payload', result)
        return apply_layer_coverage(
            verdict, payload if isinstance(payload, dict) else {})


def holdBatchLock(ui, locked: bool) -> None:
    """DP-756. While a batch runs, Undo and Redo stay off whatever else
    refreshes the menus, so neither the menu nor Ctrl+Z can reach them."""
    if locked:
        ui.actionUndo.setEnabled(False)
        ui.actionRedo.setEnabled(False)
        # DP-5711. The File menu stays open during a batch; its entries are
        # what is held, and Save says why it is off.
        for action in fileMenuEntries(ui) or ():
            action.setEnabled(False)
        save = getattr(ui, 'actionSave', None)
        if save is not None and hasattr(save, 'setToolTip'):
            save.setToolTip(SAVE_WHILE_RUNNING)
            save.setStatusTip(SAVE_WHILE_RUNNING)


def fileMenuEntries(ui):
    """The File menu's entries, separators aside; None for a stand-in menu
    that cannot list them (DP-5711)."""
    menu = getattr(ui, 'menuFile', None)
    actions = getattr(menu, 'actions', None)
    if not callable(actions):
        return None
    return [action for action in actions() if not action.isSeparator()]


class _ChipClick(QObject):
    """Turns a left click on the collapsed verdict chip into a signal."""

    clicked = Signal()

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and watched.rect().contains(event.position().toPoint())):
            # A click on Details has its own handler; leave it be.
            child = watched.childAt(event.position().toPoint())
            if not isinstance(child, QPushButton):
                self.clicked.emit()
        return False


class MainWindow(QMainWindow):
    _vtkReaderProgress = Signal(str)
    _closeTriggered = Signal(bool)

    def __init__(self, *, staged: bool = False):
        super().__init__()
        #: Plan 45 start-up. Building the window held the GUI thread ~2.5 s in
        #: one piece before the event loop ran (measured live, 17 October
        #: 2026). With ``staged`` -- the application's own start -- the event
        #: queue is served between the stages below, so no single stretch
        #: holds the thread long; ``MainWindow()`` builds it all in one go, as
        #: it always did. The window stays hidden until it is shown by
        #: :meth:`start`.
        self._stagedConstruction = staged
        #: Zero-delay calls the stages ask for, held until the last stage has
        #: run so none of them meets a half-built window.
        self._afterConstruction = [] if staged else None
        if staged:
            # The first text in the application font costs ~0.3 s once; it
            # is paid in a turn of its own instead of inside setupUi's.
            warm_application_font()
            self._constructionTurn()
        self._ui = Ui_MainWindow()
        if staged:
            # Plan 45 BOOT2. The viewport is ~0.45 s of setupUi's ~0.5 s
            # (MEASURED live, 17 October 2026); it serves the queue between
            # its own stages while the window is built in stages.
            rendering_widget_module.constructionTurn = self._constructionTurn
        try:
            self._ui.setupUi(self)
        finally:
            rendering_widget_module.constructionTurn = None
        # Plan 40 UXE1 (F7b). The keys the user rebound apply to every
        # shortcut made from here on.
        SHORTCUTS.attach(app.settings)
        # Plan 40 UXE4 (F7e): a model dragged from the file manager imports.
        self.setAcceptDrops(True)
        self._constructionTurn()
        self._threeRegionShell = install_three_region_shell(self._ui)
        # Non-visual compatibility alias for controller/tests that inspect the
        # former member. It is permanent and has no hide/toggle action.
        self._sidebar = self._ui.regionAHost
        layout_state = getattr(
            app.settings, 'getThreeRegionLayout', lambda: {})()
        self._savedRegionSizes = layout_state.get('sizes')
        self._regionSplitterUserMoved = False
        # Plan 40 UXC3. The regions live in Qt-ADS docks from here on; the
        # saved arrangement (which dock floated, where, on which screen) is
        # restored before the sizes are applied.
        self._dockLayout = dock_the_regions(self._ui, self._threeRegionShell)
        # Plan 45 BOOT2. Docking (~0.2 s), restoring the saved arrangement
        # (~0.3 s) and the settings sheet were one ~0.55 s stage.
        self._constructionTurn()
        # Plan 43 LY3. A layout saved before the settings sheet is not
        # applied (it is kept under a backup key for a release); whoever had
        # one is told once.
        self._layoutNoteDue = bool(getattr(
            app.settings, 'migrateLayout', lambda: False)())
        saved_docks = getattr(app.settings, 'getDockLayout', lambda: None)()
        if saved_docks is not None:
            self._dockLayout.restore(saved_docks)
            self._constructionTurn()
        self._settingsSheet = SettingsSheet(
            layout=self._dockLayout, region_b=self._ui.regionBHost,
            host=self._ui.regionCHost, stack=self._ui.content,
            store=app.settings, step_name=self._currentStepName,
            parent=self)
        self._ui.settingsSheet = self._settingsSheet
        if self._layoutNoteDue:
            self._soon(self._showLayoutNote)
        self._dockLayout.regions.moved.connect(
            lambda: setattr(self, '_regionSplitterUserMoved', True))
        self._dockLayout.arrangementChanged.connect(
            lambda: self._applyRegionSizes())
        self._threeRegionShell.apply_sizes(
            max(self.width(), 1280), self._savedRegionSizes)
        self._soon(self._applyRegionSizes)
        # Plan 43 LY2. The outline dock is retired, so its minimum width is
        # not a width the window has to keep.
        self.setMinimumWidth(
            self._threeRegionShell.minimum_window_width - REGION_A_MINIMUM
            - self._dockLayout.regions.handleWidth()
            if self._dockLayout.isRetired(OUTLINE_DOCK)
            else self._threeRegionShell.minimum_window_width)
        self._constructionTurn()

        self._applyThemedIcons()
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(self._applyVtkTheme)
            if app.themeManager.tokens is not None:
                self._applyVtkTheme(app.themeManager.resolved_name)

        self._ui.renderingSplitter.setStretchFactor(0, 0)
        self._ui.renderingSplitter.setStretchFactor(1, 1)

        self._recentFilesMenu = RecentFilesMenu(self._ui.menuOpen_Recent)
        self._recentFilesMenu.setRecents(app.settings.getRecentCases())

        self._navigationView = NavigationView(self._ui)
        # DP-134. The outline learns how wide it needs to be only once it has
        # rows -- the engine branch installs thirteen of them when a meshing
        # method is chosen -- so the pane has to be able to follow it.
        self._navigationView.paneWidthChanged.connect(
            lambda _width: self._applyRegionSizes())
        # MF27. The sheet header read the outline's current row only when the
        # page stack changed, which happens before an engine route moves the
        # current row: snappy "5. Base grid" was headed "4. Domain & regions".
        # Follow the outline itself, as the ribbon does.
        self._navigationView.outlineChanged.connect(
            self._settingsSheet.refresh_step_name)
        self._installOutlineRail()
        self._displayControl = DisplayControl(self._ui)
        self._renderingTool = RenderingTool(self._ui)
        # DP-2745. Presentation ops over the local API move this window.
        setRenderer = getattr(app, 'setPresentationRenderer', None)
        if callable(setRenderer):
            from foammesh.view.presentation_bridge import (
                DesktopPresentationRenderer)
            setRenderer(DesktopPresentationRenderer(
                view=self._ui.renderingView, tool=self._renderingTool,
                display=self._displayControl, ui=self._ui))
        self._constructionTurn()
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
            'quality', self._meshQualityTab, self.tr('Mesh quality'))
        # Plan 40 UXD1: the live run panel, fed by the same job events.
        self._runPanel = install_run_panel(self._outputTabs)
        self._verdictStrip = self._ui.meshVerdictStrip
        self._verdictStrip.activated.connect(self.showQualityReport)
        # Plan 43 LY9. A failed check names the step to look at, and the strip
        # opens it.
        from foammesh.view.engine_context import read_engine
        self._verdictStrip.engineProvider = lambda: read_engine(
            app.facadeClient.checkout())
        self._verdictStrip.stepRequested.connect(self.openLikelyStep)
        self._installOutputBandControls()
        # Plan 40 UXC4: taskbar progress, flash, opt-in notification.
        from foammesh.view.main_window.job_attention import (
            install_job_attention)
        self._jobAttention = install_job_attention(
            self, app.events, self._ui.menuView, settings=app.settings)
        self._meshQualityTab.acceptRequested.connect(self._acceptMeshQuality)
        self._meshQualityTab.runCheckRequested.connect(
            self._runMeshCheckDashboard)
        self._meshQualityTab.elementSelected.connect(self._focusQualityElement)
        # Plan 39 VI8: the ranked worst-cell list is asked for from the tab.
        self._meshQualityTab.rankRequested.connect(
            lambda metric: self._displayControl.meshQualityInfo()
            .requestRanking(self._meshQualityTab, metric,
                            self._meshQualityTab.rank_count()))
        self._wireQualityInspector()
        self._meshQualityTab.saveSelectionRequested.connect(
            self._saveElementSelection)
        self._meshQualityTab.showSelectionRequested.connect(
            self._showElementSelection)
        self._meshQualityTab.deleteSelectionRequested.connect(
            self._deleteElementSelection)
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
        # Plan 40 UXC5 (F6d). The status bar keeps what it is told, behind a
        # bell with a *Go to* for each entry.
        install_notification_centre(
            self, self._notificationTarget, self._goToNotification)
        self._installTopBand()
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
        self._apiGeometryPending = False
        #: DP-2851/DP-2852. Draws of a mesh still under way: an API run's
        #: result and the stored result of a case just opened.
        self._meshDraws = set()
        # DP-363. Set when a pending refresh is a history move, which has
        # to repaint the forms; cleared when it is flushed.
        self._projectRefreshRepaintsForms = False
        # DP-397. What the pending refresh is *for*. `_projectRefreshWide` is
        # set by anything this window cannot read as a rename, and a wide
        # refresh rebuilds the scene as it always did; the list holds the
        # (id, name) pairs of the renames seen since the last flush.
        self._projectRefreshWide = False
        self._projectRefreshRenames = []
        self._capabilityWarmPending = False
        self._capabilityProbeFailure = None
        self._capabilityWanted = set()
        self._jobStateUnsubscribe = app.jobManager.subscribe_state(
            self._scheduleMenuRefresh)
        # DP-464. The menu is not the only thing a finished job frees.
        self._stepLockUnsubscribe = app.jobManager.subscribe_state(
            self._stepManager.refreshStepLock)
        self._capabilityStateUnsubscribe = app.capabilities.subscribe_state(
            self._capabilitiesReprobed)
        # C3. The designed window is 1065 px tall; on a 1080 px screen the
        # status bar -- the only progress surface a task-page run has, and the
        # only place its Cancel lives -- was drawn below the bottom edge.
        self._jobProgress = JobProgressWidget(
            app.events, app.jobManager,
            lambda: self.showOutputTab('console'), self)
        self.statusBar().addPermanentWidget(self._jobProgress)
        # Plan 35 CR6. Up only while the WSL runtime is unreachable:
        # [Retry connection] [Restart WSL runtime] [Open diagnostics].
        self._wslHealthBar = self._installWslHealthBar()
        # A run lost to the WSL transport or to memory offers [Retry] (and
        # [Retry with fewer cores]) once, before its failure is reported.
        client = getattr(app, 'facadeClient', None)
        if client is not None and hasattr(client, 'retry_prompt'):
            client.retry_prompt = RetryPrompt(self)
        self._constructionTurn()

        #: Feature-line actors while the overlay is shown.
        self._featureActors = []
        #: True while the fidelity colouring sits on the patches.
        self._fidelityPainted = False
        self._captureManager = CaptureManager(
            self._ui.renderingView, self._displayControl)
        self._viewportOverlay = ViewportOverlay(self._ui.renderingView)
        # FX1 (DP-2620). Placed by the clear area (`_placeViewportOverlay`),
        # not once in view coordinates: at (12, 12) it sat under the sheet.
        self._viewportOverlay.show()
        # DP-713. The deviation colouring's histogram, figures and legend.
        self._deviationPanel = DeviationPanel(self._ui.renderingView)
        self._deviationPanel.toleranceChanged.connect(
            self._deviationToleranceEdited)
        self._deviationPanel.scaleChanged.connect(
            self._deviationScaleEdited)
        self._deviationPanel.surfaceToggled.connect(
            self._deviationSurfaceToggled)
        self._deviationLegend = None
        self._deviationToleranceMm = None
        # Plan 43 LY11. One legend: Patches / Quality / Deviation.
        self._legendController = LegendController(
            self, self._ui.renderingView)
        qualityInfo = self._displayControl.meshQualityInfo()
        qualityInfo.highlightingChanged.connect(
            lambda _on: self._legendController.refresh())
        qualityInfo.scaleChanged.connect(self._legendController.refresh)
        # Plan 45 BOOT2. The rail (~0.25-0.35 s) is a stage of its own.
        self._constructionTurn()
        self._installViewportRail()
        self._mountGmshTools()
        self._prepareSectionPopover()
        self._installClearAreaFloats()
        self._adoptPreviewNotice()
        self._installResourceUsageAction()
        self._connectViewportControls()
        self._constructionTurn()

        self._dialog = None

        self._readyToQuit = False
        #: True only while an untitled case is being given a permanent home.
        #: The close guard must stay quiet for that one close.
        self._relocatingScratch = False

        self.setWindowIcon(meshAppProperties.icon())

        # OEM variants with analytics disabled get no Privacy page in
        # Preferences (DP-760: it is no longer a Help entry as well).
        self._privacyConfigured = Analytics().configured

        self._installStepHelpActions()
        self._installDiagnosticsActions()
        self._installShortcutsHelp()
        #: Plan 35 CR0 step 6: the banner about a session that died, if any.
        self._crashNotice = None

        self._setupShortcuts()

        self._connectSignalsSlots()
        self._installConsoleMenu()
        self._installMeshQualityViews()
        self._wireClearAreaConsumers()
        # Plan 43 LY8. The View actions made above, laid out in the groups of
        # the plan's section 6.
        from foammesh.view.main_window.view_menu import install_view_menu
        install_view_menu(self)
        # Plan 43 LY3 / LY8. The settings column is a sheet; View > Settings
        # sheet (LY8's entries) is read by it.
        self._settingsSheet.bind_view_state(
            self._viewState, self._viewMenu)
        # The tool edges read the View menu's choices (pins, bottom bar,
        # Parts panel and chip, how the edges are revealed).
        self._viewportToolEdges.bindViewState(self._viewState)
        self._qualityViews.bind(self._viewState,
                                show_report=self.showQualityReport)

        # Plan 30 WP-08 (F-09). What a run did is said in one line, without
        # taking the window away, instead of in a modal raised over the
        # viewport that is drawing the mesh the sentence is about.
        #
        # QA-04. That line used to sit in the page footer, directly above the
        # Unlock / Finish / Next row, where it competed with the control that
        # moves the job forward and pushed the page up every time a run ended.
        # The footer is for walking the workflow. The sentence and its Cancel,
        # its offer and its log button are kept exactly as they are and moved
        # into a view of their own, reached from Help and from the quality
        # page, so a run can be read about deliberately rather than read over.
        #: The allocation of the run currently on the strip, so cancelling it
        #: can name the workers it stopped rather than only the job count.
        self._lastRunAllocation = None
        self._runStatusStrip = RunStatusStrip(self)
        self._runStatusStrip.cancelRequested.connect(self._cancelRunningJobs)
        self._runStatusStrip.offerAccepted.connect(self._drawOfferedResult)
        self._runStatusStrip.logRequested.connect(self._openRunLog)
        # VI12 (F6c). The strip's Cancel used to be reachable only through
        # Help > Run details. While a run is going, a copy of the strip is
        # docked under the page footer, where the Cancel of the run is looked
        # for; the Run details view keeps the strip itself.
        self._runDockStrip = self._dockRunStrip()
        #: CP-02. The earlier accepted result the strip is currently offering,
        #: set when a run ends with no mesh of its own. Never drawn until the
        #: user takes the offer.
        self._offeredResult = None
        #: The run details view. Not modal, and never raised by the product:
        #: it opens when the user asks for it, from Help > Run details or
        #: from the Details control on the quality page.
        self._runDetailsView = QDialog(self)
        self._runDetailsView.setObjectName('runDetailsView')
        self._runDetailsView.setWindowTitle(self.tr('Run details'))
        self._runDetailsView.setModal(False)
        # DP-753. What the case has recorded is read here too, on a History
        # tab, instead of in a message box off the Edit menu.
        runDetailsOuter = QVBoxLayout(self._runDetailsView)
        self._runDetailsTabs = QTabWidget(self._runDetailsView)
        self._runDetailsTabs.setObjectName('runDetailsTabs')
        runDetailsOuter.addWidget(self._runDetailsTabs)
        runDetailsPage = QWidget(self._runDetailsTabs)
        self._runDetailsTabs.addTab(runDetailsPage, self.tr('Last run'))
        # UXD7. The tab is the History panel: every step Undo and Redo walk
        # (settings changes and mesh stage runs), with a jump to any of them;
        # the case's recorded transactions stay readable beneath it.
        historyPage = QWidget(self._runDetailsTabs)
        historyLayout = QVBoxLayout(historyPage)
        self._historyPanel = HistoryPanel(historyPage)
        self._historyPanel.jumpRequested.connect(self._jumpHistory)
        historyLayout.addWidget(self._historyPanel, 2)
        self._runHistoryText = QPlainTextEdit(historyPage)
        self._runHistoryText.setObjectName('runHistoryText')
        self._runHistoryText.setReadOnly(True)
        historyLayout.addWidget(self._runHistoryText, 1)
        self._runDetailsTabs.addTab(historyPage, self.tr('History'))
        self._historyTab = historyPage
        self._historyAction = QAction(self.tr('&History…'), self)
        self._historyAction.setObjectName('actionHistory')
        self._historyAction.setToolTip(self.tr(
            'List the steps undo and redo walk, and go to any of them'))
        self._historyAction.triggered.connect(lambda *_: self.showHistory())
        self._ui.menuEdit.addAction(self._historyAction)
        # Undo and Redo name the newest step as the menu opens, whatever
        # recorded it (a mesh stage run records one without an event).
        self._ui.menuEdit.aboutToShow.connect(self._updateMenuStates)
        runDetailsLayout = QVBoxLayout(runDetailsPage)
        # The strip hides itself when there is nothing to say, so the view
        # says that in words rather than opening on an empty rectangle.
        self._runDetailsEmpty = QLabel(
            self.tr('No run has been made in this case yet.'),
            self._runDetailsView)
        self._runDetailsEmpty.setObjectName('runDetailsEmpty')
        self._runDetailsEmpty.setWordWrap(True)
        runDetailsLayout.addWidget(self._runDetailsEmpty)
        runDetailsLayout.addWidget(self._runStatusStrip)
        runDetailsLayout.addStretch(1)

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
        self._finishConstruction()

    def _constructionTurn(self) -> None:
        """Plan 45 start-up: serve the event queue between two stages.

        Only for the application's staged start, which builds the window
        before its event loop runs; nothing is shown yet, so no input reaches
        the half-built window, and calls it queues are held by :meth:`_soon`.
        """
        if self._stagedConstruction:
            QApplication.processEvents()

    def _finishConstruction(self) -> None:
        self._stagedConstruction = False
        held, self._afterConstruction = self._afterConstruction, None
        for call in held or ():
            QTimer.singleShot(0, call)

    def _soon(self, call) -> None:
        """``QTimer.singleShot(0, call)``, held while the window is built."""
        if self._afterConstruction is not None:
            self._afterConstruction.append(call)
        else:
            QTimer.singleShot(0, call)

    def _dockRunStrip(self):
        """Mount the run strip's docked copy under the footer, or None."""
        bar = getattr(self._ui, 'wizardActionBar', None)
        host = bar.parentWidget() if bar is not None else None
        layout = host.layout() if host is not None else None
        if layout is None:
            return None
        dock = RunStatusStrip(host, docked=True)
        layout.insertWidget(layout.indexOf(bar) + 1, dock, 0)
        dock.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._runStatusStrip.addMirror(dock)
        return dock

    @property
    def renderingView(self):
        return self._ui.renderingView

    @property
    def consoleView(self):
        return self._consoleView

    def showOutputTab(self, key: str):
        """Select one existing Region C output without changing Regions A/B."""
        self.openOutputBand()
        return self._outputTabs.show(key)

    # -- Plan 40 UXC2: the output band on demand, and focus mode -------------- #

    # -- Plan 43 LY5: the clear area and the centring ---------------------- #

    @property
    def clearArea(self):
        """The controller that centres the model between the sheet and the
        console. The settings sheet registers itself with
        ``clearArea.add_occluder(sheet, 'left' | 'right')``."""
        return self._clearArea

    # -- Plan 44 MF3: the top band, the status strip, the clear area ---------- #

    #: The top band is 60 px: the menu bar and the step ribbon.
    MENU_HEIGHT = 24

    def _installTopBand(self) -> None:
        """One 60 px band over the viewport: a 24 px menu above a 36 px
        ribbon, with no margin, gap or gutter between it and the view."""
        self.menuBar().setFixedHeight(self.MENU_HEIGHT)
        central = self.centralWidget()
        layout = central.layout() if central is not None else None
        if layout is not None:
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
        # The viewport's frame: no gutter at its right or bottom either.
        box = getattr(self._ui, 'groupBox_24', None)
        if box is not None and box.layout() is not None:
            box.layout().setContentsMargins(0, 0, 0, 0)
        # The run and progress strip lives in the status strip (U7): the
        # viewport's own copy of it stays off the picture. Its Cancel is the
        # status strip's, and reaches every wait the same way.
        self._runHostInStatusBar = True
        self._syncStripToStatusBar()

    def _syncStripToStatusBar(self) -> None:
        view = getattr(getattr(self, '_ui', None), 'renderingView', None)
        strip = getattr(view, '_strip', None)
        setter = getattr(strip, 'setSuppressed', None)
        if callable(setter):
            setter(bool(getattr(self, '_runHostInStatusBar', False)))

    def _syncSizeGrip(self) -> None:
        """No size grip when maximised; a grip when the window is a window."""
        bar = self.statusBar()
        if hasattr(bar, 'setSizeGripEnabled'):
            bar.setSizeGripEnabled(
                not (self.isMaximized() or self.isFullScreen()))

    def _applyCaption(self) -> None:
        """Q5: the caption in the chrome colour (a no-op off Windows)."""
        try:
            from foammesh.view.theming import window_frame
            manager = getattr(app, 'themeManager', None)
            tokens = getattr(manager, 'tokens', None)
            if tokens is None:
                return
            window_frame.apply_caption(
                self, dark=(manager.resolved_name == 'dark'),
                caption=tokens.value('background.chrome'),
                border=tokens.value('border.default'))
        except Exception:                                    # noqa: BLE001
            logger.debug('the caption colour was not applied', exc_info=True)

    def showEvent(self, event):
        super().showEvent(event)
        self._applyCaption()
        self._syncSizeGrip()

    def clearRectOfView(self):
        """The clear area in the viewport's pixels: the view's own
        ``clearRect()`` (Plan 44 MF7), else the controller's."""
        view = self._ui.renderingView
        accessor = getattr(view, 'clearRect', None)
        rect = accessor() if callable(accessor) else None
        if isinstance(rect, QRect):
            return rect
        controller = getattr(self, '_clearArea', None)
        return controller.clearRect() if controller is not None else None

    def _wireClearAreaConsumers(self) -> None:
        """What lays itself out against the clear area is told when it moves.

        * MF23: the layout-C inspector card is hosted on the view and placed
          in the clear area; it shows while the model is coloured by quality.
        * MF16: the worst-value labels keep inside the clear rectangle.
        """
        self._meshQualityTab.attach_inspector_card(
            self._ui.renderingView, self.clearRectOfView)
        self._syncInspectorOnModel()
        card = self._meshQualityTab.inspector_card()
        if card is not None:
            # DP-2685: the layout-C inspector card is a floating item too, so
            # the viewport overlay (placed last) stands off it.
            from foammesh.view.main_window import clear_area as ca
            self._clearArea.add_float(card, ca.FLOAT_RANK_PANEL, card.place)
            self._placeViewportOverlay()
        self._clearArea.changed.connect(self._clearRectChanged)
        info = self._displayControl.meshQualityInfo()
        info.highlightingChanged.connect(
            lambda _on: self._syncInspectorOnModel())

    def _clearRectChanged(self, _rect=None) -> None:
        layer = getattr(getattr(self, '_qualityViews', None), '_layer', None)
        if layer is not None:
            self.feedClearRect(layer)

    def feedClearRect(self, layer) -> None:
        """Give a ``WorstLabelLayer`` the clear rectangle: render pixels,
        y down, as ``clear_area.clear_rect`` returns it."""
        from foammesh.view.main_window.clear_area import clear_rect
        controller = self._clearArea
        view = self._ui.renderingView
        setKeepOut = getattr(layer, 'set_keep_out', None)
        if callable(setKeepOut):
            setKeepOut(self.overlayKeepOut)
        layer.set_clear_rect(clear_rect(
            int(view.width()), int(view.height()), controller.insets()))

    def modelScreenRect(self):
        """Plan 45 V6. The model on screen as a QRect in the view's pixels
        (y down): the projected box of the parts drawn, or None."""
        boxes = []
        for name in ('_meshManager', '_geometryManager'):
            manager = getattr(self, name, None)
            getter = getattr(manager, 'getDisplayedBounds', None)
            try:
                bounds = getter() if callable(getter) else None
            except Exception:                                # noqa: BLE001
                bounds = None
            if bounds is not None:
                boxes.append(tuple(bounds.toTuple()))
        view = self._ui.renderingView
        getRenderer = getattr(view, 'renderer', None)
        renderer = getRenderer() if callable(getRenderer) else None
        if not boxes or renderer is None:
            return None
        width, height = renderer.GetSize()
        if width <= 0 or height <= 0 or view.width() <= 0:
            return None
        scale = view.width() / float(width)
        xs, ys = [], []
        for box in boxes:
            for x in box[0:2]:
                for y in box[2:4]:
                    for z in box[4:6]:
                        renderer.SetWorldPoint(x, y, z, 1.0)
                        renderer.WorldToDisplay()
                        dx, dy, _dz = renderer.GetDisplayPoint()
                        xs.append(dx * scale)
                        ys.append((height - dy) * scale)
        left, top = int(min(xs)), int(min(ys))
        return QRect(left, top, max(1, int(max(xs)) - left),
                     max(1, int(max(ys)) - top))

    def overlayKeepOut(self) -> list:
        """Plan 45 V3/V10. ``[(x0, y0, x1, y1)]`` in the view's pixels (y
        down): the widgets standing over the picture -- the clear area's
        floating items (the overlay card, the legend, the deviation panel,
        an open popover) and the bottom bar. The worst-value tags and the
        statistics caption are placed off them."""
        rects = []
        controller = getattr(self, '_clearArea', None)
        floats = getattr(controller, 'float_rects', None)
        if callable(floats):
            rects.extend(floats())
        edges = getattr(self, '_viewportToolEdges', None)
        frame = getattr(edges, '_bottomFrame', None)
        view = self._ui.renderingView
        try:
            if frame is not None and frame.isVisible():
                corner = view.mapFromGlobal(frame.mapToGlobal(QPoint(0, 0)))
                rects.append(QRect(corner, frame.size()))
        except RuntimeError:
            pass
        return [(r.x(), r.y(), r.x() + r.width(), r.y() + r.height())
                for r in rects if r is not None and not r.isEmpty()]

    def _syncInspectorOnModel(self) -> None:
        """The inspector card follows the colouring: on while the model is
        coloured by quality (the Quality view mode, or the poor cells
        highlighted)."""
        control = getattr(self, '_displayControl', None)
        getter = getattr(control, 'meshQualityInfo', None)
        info = getter() if callable(getter) else None
        highlighting = bool(info is not None and info.isHighlighting())
        colour_mode = (view_modes.QUALITY
                       if self._viewMode == view_modes.QUALITY
                       or highlighting else '')
        tab = getattr(self, '_meshQualityTab', None)
        if tab is None:
            return
        tab.set_inspector_on_model(
            colour_mode == view_modes.QUALITY)

    def _installClearArea(self, band) -> None:
        from foammesh.view.main_window.clear_area import ClearAreaController
        self._clearArea = ClearAreaController(self._ui.renderingView)
        self._clearArea.add_occluder(band.band_widget, 'bottom')
        band.subscribe_geometry(self._clearArea.update)
        # The settings sheet (Plan 43 LY3) is an occluder while it floats or
        # is minimised to its tab; pinned, it sits in the dock beside the
        # viewport and takes nothing from it (its frame is hidden then).
        sheet = getattr(self, '_settingsSheet', None)
        if sheet is not None:
            self.registerSheet(sheet.frame, lambda: sheet.side, tab=sheet.tab)
            sheet.changed.connect(self._clearArea.update)
            # Plan 45 PINQ: one re-centre per pin or unpin, at its end.
            hold = getattr(sheet, 'add_arrangement_hold', None)
            if hold is not None:
                hold(self._clearArea.held)
        # Plan 43 section 1: re-centre on every sheet, console and tab change.
        # A tab or page change can resize either without a Resize reaching the
        # watcher first (the tab's content sets the band's wanted height).
        for source, signal in (
                (getattr(self._ui, 'regionCTabHost', None), 'currentChanged'),
                (getattr(self._ui, 'content', None), 'currentChanged')):
            if source is not None and hasattr(source, signal):
                getattr(source, signal).connect(
                    lambda *_a: self._clearArea.update())

    def _installClearAreaFloats(self) -> None:
        """FX1 (DP-2620, DP-2621). The floating items of the clear area keep
        off one another by one rule (`ClearAreaController.add_float`): the
        rail's popovers and the notice strip first, then the Parts chip, the
        legend (bound by its controller) and last the viewport overlay, which
        stands at the clear area's top left and follows it."""
        from foammesh.view.main_window import clear_area as ca
        controller = self._clearArea
        rail = getattr(self, '_viewportRail', None)
        if rail is not None:
            for key in rail.tools():
                popover = rail.popover(key)
                if popover is not None:
                    controller.add_float(popover, ca.FLOAT_RANK_POPOVER)
        getter = getattr(self._ui.renderingView, 'noticeStrip', None)
        strip = getter() if callable(getter) else None
        if isinstance(strip, QWidget):
            controller.add_float(strip, ca.FLOAT_RANK_NOTICE)
        edges = getattr(self, '_viewportToolEdges', None)
        chip = edges.chip() if edges is not None else None
        if chip is not None:
            controller.add_float(chip, ca.FLOAT_RANK_CHIP)
        panel = getattr(self, '_deviationPanel', None)
        if panel is not None:
            # V16 (DP-2650): the deviation panel stands in the clear area.
            controller.add_float(panel, ca.FLOAT_RANK_PANEL, panel.place)
            panel.setClearArea(controller)
        controller.add_float(self._viewportOverlay, ca.FLOAT_RANK_OVERLAY,
                             self._placeViewportOverlay)
        self._placeViewportOverlay()

    def viewportOverlayRect(self) -> QRect:
        """Where the viewport overlay should stand, in the view's pixels: the
        clear area's top left, 6 px in, below the top pill when the two
        share columns (the Parts chip's rule), and off every floating item
        placed before it."""
        from foammesh.view.main_window.clear_area import OVERLAY_INSET
        overlay = self._viewportOverlay
        controller = self._clearArea
        clear = controller.clearRect()
        wanted = overlay.rect().translated(clear.x() + OVERLAY_INSET,
                                           clear.y() + OVERLAY_INSET)
        edges = getattr(self, '_viewportToolEdges', None)
        top_rect = getattr(edges, '_topRect', None)
        if callable(top_rect):
            try:
                pill = top_rect().adjusted(-OVERLAY_INSET, -OVERLAY_INSET,
                                           OVERLAY_INSET, OVERLAY_INSET)
            except (RuntimeError, AttributeError, TypeError):
                pill = None
            if pill is not None and wanted.intersects(pill):
                wanted.moveTop(pill.bottom() + 1)
        return controller.place_clear_of(overlay, wanted)

    def _placeViewportOverlay(self) -> None:
        overlay = getattr(self, '_viewportOverlay', None)
        if overlay is None or getattr(self, '_clearArea', None) is None:
            return
        rect = self.viewportOverlayRect()
        if overlay.pos() != rect.topLeft():
            overlay.move(rect.topLeft())

    def registerSheet(self, sheet, side='left', *, tab=None) -> None:
        """Make the settings sheet (floating or pinned, left or right) and
        its minimised tab occluders of the viewport (Plan 43 LY5)."""
        self._clearArea.register_sheet(sheet, side, tab=tab)

    def openOutputBand(self) -> None:
        """Open the band under the viewport (a run, a failure, a click)."""
        band = getattr(self._ui, 'regionCBandController', None)
        if band is not None:
            band.open()

    def _installOutputBandControls(self) -> None:
        """Wire Ctrl+J, F11 / Esc, the chip click and the run triggers."""
        from foammesh.view.main_window.console_band import ConsoleLayout
        band = self._ui.regionCBandController.configure(
            layout=ConsoleLayout.from_dict(
                getattr(app.settings, 'getConsoleLayout', lambda: {})()),
            window_height=self.height,
            current_tab_key=lambda: self._outputTabs.key_for_current(),
            save=lambda value: getattr(
                app.settings, 'updateConsoleLayout',
                lambda _v: None)(value.to_dict()))
        self._ui.regionCBandController = band
        # Plan 43 LY4 (DP-1660): the console is docked at the sheet's edge.
        band.follow_sheet(self._settingsSheet)
        band.subscribe_geometry(self._applyRegionSizes)
        self._installClearArea(band)
        self._bandAction = self._ui.menuView.addAction(
            self.tr('Output pa&nel'))
        self._bandAction.setObjectName('actionOutputBand')
        self._bandAction.setShortcut('Ctrl+J')
        adoptShortcut('view.outputBand', self._bandAction)
        self._bandAction.setCheckable(True)
        self._bandAction.setToolTip(self.tr(
            'Show or hide the console and the Mesh quality report under '
            'the viewport'))
        self._bandAction.triggered.connect(lambda _checked=False: self._toggleBand())
        self._arrangeAction = self._ui.menuView.addAction(
            self.tr('Arran&ge panels'))
        self._arrangeAction.setObjectName('actionArrangePanels')
        self._arrangeAction.setCheckable(True)
        self._arrangeAction.setToolTip(self.tr(
            'Show the title bars of the outline and the settings column so '
            'they can be dragged, tabbed together or floated'))
        self._arrangeAction.toggled.connect(
            lambda on: self._dockLayout.set_title_bars(on))
        self._resetLayoutAction = self._ui.menuView.addAction(
            self.tr('Standard la&yout'))
        self._resetLayoutAction.setObjectName('actionResetLayout')
        self._resetLayoutAction.setToolTip(self.tr(
            'Put the outline, the settings column, the console and the '
            'viewport back where they start, and forget the saved '
            'arrangement'))
        self._resetLayoutAction.setShortcut('Ctrl+0')
        adoptShortcut('view.standardLayout', self._resetLayoutAction)
        self._resetLayoutAction.triggered.connect(
            lambda _checked=False: self.resetLayout())
        self._focusAction = self._ui.menuView.addAction(
            self.tr('Focu&s mode'))
        self._focusAction.setObjectName('actionFocusMode')
        self._focusAction.setShortcut('F11')
        adoptShortcut('view.focusMode', self._focusAction)
        self._focusAction.setCheckable(True)
        self._focusAction.setToolTip(self.tr(
            'Hide the outline, the page and the output panel so the model '
            'fills the window. Esc brings them back.'))
        self._focusAction.triggered.connect(
            lambda _checked=False: self._toggleFocusMode())
        # Esc only means something while focus mode is on; off, it is left to
        # dialogs, popups and the console's search bar.
        self._focusEscape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._focusEscape.setContext(Qt.ShortcutContext.WindowShortcut)
        adoptShortcut('view.focusExit', self._focusEscape)
        self._focusEscape.setEnabled(False)
        self._focusEscape.activated.connect(self._toggleFocusMode)
        band.subscribe(self._bandStateChanged)
        # The chip is a button as a whole: a click anywhere on it opens the band.
        self._bandChipFilter = _ChipClick(self)
        self._verdictStrip.installEventFilter(self._bandChipFilter)
        self._bandChipFilter.clicked.connect(self._toggleBand)
        # A run starting and a run failing both bring the console up.
        self._bandUnsubscribes = [
            app.events.subscribe(Event.JOB_STARTED, self._onJobOpensBand),
            app.events.subscribe(Event.JOB_FAILED, self._onJobFailsBand),
            app.events.subscribe(Event.JOB_FINISHED, self._onJobEndsBand),
            app.events.subscribe(Event.JOB_CANCELLED, self._onJobEndsBand),
        ]
        self._bandStateChanged(band.is_open, band.in_focus_mode)

    def _installConsoleMenu(self) -> None:
        """Plan 43 LY4: View > Console, the console's choices in one place.

        The existing View actions (Output panel, Ctrl+J; Console, Ctrl+Shift+C;
        Notify when a run ends) stay where they are; the submenu holds them
        too, with the new ones beside them."""
        from foammesh.view.main_window.console_band import (
            HEIGHT_PRESETS, WIDTH_PRESETS, exclusive_group)
        from foammesh.view.main_window.shortcut_registry import (
            REGISTRY as _SHORTCUTS)
        band = self._ui.regionCBandController
        menu = self._ui.menuView.addMenu(self.tr('Conso&le'))
        menu.setObjectName('menuConsole')
        self._consoleMenu = menu
        menu.addAction(self._bandAction)
        self._minimiseAction = menu.addAction(self.tr('&Minimised'))
        self._minimiseAction.setObjectName('actionConsoleMinimised')
        self._minimiseAction.setCheckable(True)
        self._minimiseAction.setShortcut('Ctrl+`')
        adoptShortcut('view.consoleMinimise', self._minimiseAction)
        self._minimiseAction.setToolTip(self.tr(
            'Fold the console to a 28 px header, or open it again'))
        self._minimiseAction.triggered.connect(
            lambda _checked=False: band.toggle_minimised())
        # Ctrl+Shift+C stays on the View > Console action it always was on;
        # this entry reaches the same tab from the submenu. The key is shown
        # as a hint after the tab (DP-1659); it is not bound a second time.
        openTab = menu.addAction(
            self.tr('Open at the &console tab') + '\t'
            + ', '.join(_SHORTCUTS.keys('view.console')))
        openTab.setObjectName('actionConsoleOpenAtTab')
        openTab.triggered.connect(lambda _c=False: self.showOutputTab('console'))
        self._openOnRunAction = menu.addAction(
            self.tr('Open when a run &starts'))
        self._openOnRunAction.setObjectName('actionConsoleOpenOnRun')
        self._openOnRunAction.setCheckable(True)
        self._openOnRunAction.setChecked(band.layout.open_on_run)
        self._openOnRunAction.toggled.connect(band.set_open_on_run)
        notify = getattr(self._jobAttention, 'action', None)
        if notify is not None:
            menu.addAction(notify)
        menu.addSeparator()
        heightGroup = exclusive_group(self)
        height_menu = menu.addMenu(self.tr('&Height'))
        height_menu.setObjectName('menuConsoleHeight')
        self._consoleHeightActions = {}
        for value in (None,) + HEIGHT_PRESETS:
            text = (self.tr('Standard') if value is None
                    else self.tr('%1 px').replace('%1', str(value)))
            action = height_menu.addAction(text)
            action.setObjectName(
                'actionConsoleHeight' + ('Standard' if value is None
                                         else str(value)))
            action.setCheckable(True)
            heightGroup.addAction(action)
            action.triggered.connect(
                lambda _c=False, v=value: band.set_height(v))
            self._consoleHeightActions[value] = action
        widthGroup = exclusive_group(self)
        width_menu = menu.addMenu(self.tr('&Width'))
        width_menu.setObjectName('menuConsoleWidth')
        self._consoleWidthActions = {}
        for value in (None,) + WIDTH_PRESETS:
            text = (self.tr('To the right edge') if value is None
                    else self.tr('%1 px').replace('%1', str(value)))
            action = width_menu.addAction(text)
            action.setObjectName(
                'actionConsoleWidth' + ('Edge' if value is None
                                        else str(value)))
            action.setCheckable(True)
            widthGroup.addAction(action)
            action.triggered.connect(
                lambda _c=False, v=value: band.set_width(v))
            self._consoleWidthActions[value] = action
        band.subscribe_geometry(self._consoleMenuState)
        band.subscribe(lambda _open, _focus: self._consoleMenuState())
        self._consoleMenuState()

    def _installMeshQualityViews(self) -> None:
        """Plan 43 LY10: the Mesh quality tab's layout, the worst values on
        the model and the statistics caption, and their View menus."""
        from foammesh.view.main_window.quality_views import (
            install_quality_views)

        views = install_quality_views(self, self._meshQualityTab, app.settings)
        self._qualityViews = views
        info = self._displayControl.meshQualityInfo()
        for signal in (info.scaleChanged, info.highlightingChanged):
            signal.connect(lambda *_a: views.refresh())
        info.availabilityChanged.connect(
            lambda available: None if available else views.clear())

    def _consoleMenuState(self) -> None:
        """Keep the Console submenu's checks true to the band."""
        band = self._ui.regionCBandController
        if not hasattr(self, '_consoleHeightActions'):
            return
        self._minimiseAction.blockSignals(True)
        self._minimiseAction.setChecked(band.is_minimised)
        self._minimiseAction.blockSignals(False)
        for table, current in ((self._consoleHeightActions,
                                band.layout.height),
                               (self._consoleWidthActions,
                                band.layout.width)):
            match = current if current in table else 'custom'
            for key, action in table.items():
                action.setChecked(key == match)

    def _installOutlineRail(self) -> None:
        """Plan 43 LY2. The step ribbon replaces the Outline dock.

        The outline tree stays as the model the ribbon reads; the ribbon is
        one line under the menu bar. Ctrl+B keeps its key and means the
        ribbon's compact form (numbers only); Ctrl+PageUp and Ctrl+PageDown
        step between the usable steps.
        """
        ribbon = StepRibbon(self._navigationView, self)
        self._stepRibbon = ribbon
        self._dockLayout.host_box.insertWidget(0, ribbon)
        # The ribbon is one 36 px line; a gap under it would be a second line.
        self._dockLayout.host_box.setSpacing(0)
        self._dockLayout.host_box.setContentsMargins(0, 0, 0, 0)
        ribbon.setOptions(getattr(
            app.settings, 'getStepRibbon', lambda: {})())
        ribbon.optionsChanged.connect(self._onRibbonOptionsChanged)
        # Plan 44 F4. A tab that cannot be used yet says why on the status
        # bar when it is clicked (the mockup's flash); the step stays shut.
        ribbon.unavailableClicked.connect(
            lambda text: self.statusBar().showMessage(text, 8000))

        view = self._ui.menuView
        self._railAction = view.addAction(self.tr('Step ribbon: &numbers only'))
        self._railAction.setObjectName('actionOutlineRail')
        self._railAction.setShortcut('Ctrl+B')
        adoptShortcut('view.outlineRail', self._railAction)
        self._railAction.setCheckable(True)
        self._railAction.setToolTip(self.tr(
            'Show the step ribbon as step numbers only, or with full labels '
            'again'))
        self._railAction.triggered.connect(
            lambda _checked=False: self.toggleOutlineRail())
        self._navigationView.railToggleRequested.connect(
            self.toggleOutlineRail)

        self._ribbonColourAction = view.addAction(
            self.tr('Step ribbon: colour by &state'))
        self._ribbonColourAction.setObjectName('actionStepRibbonColour')
        self._ribbonColourAction.setCheckable(True)
        self._ribbonColourAction.toggled.connect(ribbon.setColourByState)
        self._ribbonLocksAction = view.addAction(
            self.tr('Step ribbon: show &locks'))
        self._ribbonLocksAction.setObjectName('actionStepRibbonLocks')
        self._ribbonLocksAction.setCheckable(True)
        self._ribbonLocksAction.toggled.connect(ribbon.setShowLocks)
        self._stepPreviousAction = view.addAction(self.tr('Previous step'))
        self._stepPreviousAction.setObjectName('actionStepPrevious')
        self._stepPreviousAction.setShortcut('Ctrl+PgUp')
        adoptShortcut('view.stepPrevious', self._stepPreviousAction)
        self._stepPreviousAction.triggered.connect(
            lambda _checked=False: ribbon.step(-1))
        self._stepNextAction = view.addAction(self.tr('Next step'))
        self._stepNextAction.setObjectName('actionStepNext')
        self._stepNextAction.setShortcut('Ctrl+PgDown')
        adoptShortcut('view.stepNext', self._stepNextAction)
        self._stepNextAction.triggered.connect(
            lambda _checked=False: ribbon.step(1))
        self._syncRibbonActions()
        # The page column tells the window how wide the page on screen needs
        # to be, so a wide table page gets its room in a large window.
        self._ui.regionBPageColumn.wantedWidthChanged.connect(
            lambda _width: self._applyRegionSizes())

    def _syncRibbonActions(self) -> None:
        ribbon = self._stepRibbon
        for action, checked in ((self._railAction, ribbon.numbersOnly()),
                                (self._ribbonColourAction, ribbon.colourByState()),
                                (self._ribbonLocksAction, ribbon.showLocks())):
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)

    def _onRibbonOptionsChanged(self, record: dict) -> None:
        self._syncRibbonActions()
        getattr(app.settings, 'setStepRibbon', lambda _r: None)(record)
    def _currentStepName(self) -> str:
        """The step on screen, for the minimised settings sheet's tab."""
        try:
            tree = self._navigationView._tree
            return str(tree.currentIndex().data(Qt.ItemDataRole.DisplayRole)
                       or '').strip()
        except (AttributeError, RuntimeError):
            return ''

    def _showLayoutNote(self) -> None:
        """Plan 43 LY3. The one-time note after the saved layout was retired."""
        try:
            self.statusBar().showMessage(SETTINGS_SHEET_NOTE, 30000)
        except RuntimeError:                # the window is already gone
            pass

    def resetLayout(self) -> None:
        """View > Reset layout: the default arrangement and default sizes."""
        band = getattr(self._ui, 'regionCBandController', None)
        if band is not None and band.in_focus_mode:
            band.leave_focus()
        if band is not None and hasattr(band, 'standard'):
            band.standard()
        self._restoreColourStandard()
        self._threeRegionShell.set_rail(False)
        self._stepRibbon.setOptions({})
        getattr(app.settings, 'setStepRibbon', lambda _r: None)(
            self._stepRibbon.options())
        self._syncRibbonActions()
        self._arrangeAction.setChecked(False)
        self._dockLayout.reset()
        # Plan 43 LY8. Ctrl+0: the View menu's own choices go back too.
        viewState = getattr(self, '_viewState', None)
        if viewState is not None:
            viewState.reset_layout()
        self._settingsSheet.reset()
        self._regionSplitterUserMoved = False
        self._savedRegionSizes = None
        getattr(app.settings, 'updateThreeRegionLayout', lambda **_kw: None)(
            sizes=None, active_output=self._outputTabs.key_for_current())
        getattr(app.settings, 'updateDockLayout', lambda _r: None)(None)
        self._applyRegionSizes()
        clear_area = getattr(self, '_clearArea', None)
        if clear_area is not None:
            clear_area.reset()

    def _restoreColourStandard(self) -> None:
        """Ctrl+0 puts the colour back to the user's standard (Plan 43 section
        4). The standard itself arrives with LY11/LY12; until it is saved
        there is nothing to restore and the colour is left as it is."""
        standard = getattr(app.settings, 'getColourStandard', lambda: None)()
        apply = getattr(self._ui.renderingView, 'applyColourStandard', None)
        if standard and apply is not None:
            apply(standard)

    def toggleOutlineRail(self) -> bool:
        """Ctrl+B. Returns whether the step ribbon shows numbers only after."""
        band = getattr(self._ui, 'regionCBandController', None)
        if band is not None and band.in_focus_mode:
            # The ribbon is hidden in focus mode; Ctrl+B waits for Esc.
            return self._stepRibbon.numbersOnly()
        self._stepRibbon.setNumbersOnly(not self._stepRibbon.numbersOnly())
        self._syncRibbonActions()
        return self._stepRibbon.numbersOnly()

    def _onJobOpensBand(self, **_kwargs) -> None:
        try:
            self._ui.regionCBandController.run_started()
        except RuntimeError:        # the window is being torn down
            pass

    def _onJobFailsBand(self, **_kwargs) -> None:
        try:
            self._ui.regionCBandController.run_failed()
        except RuntimeError:
            pass

    def _onJobEndsBand(self, **_kwargs) -> None:
        """UXR: a band a run opened folds once the last run has ended well.

        The wait lets a pipeline's next job start before the band flickers
        shut and open again."""
        try:
            band = self._ui.regionCBandController
            band.run_ended()
            QTimer.singleShot(1500, self._settleBand)
            # A stage run is a step in the undo history; the Edit menu's Undo
            # (label, and whether it is enabled at all) must say so.
            QTimer.singleShot(1500, self._refreshMenusAfterRun)
        except RuntimeError:
            pass

    def _refreshMenusAfterRun(self) -> None:
        try:
            self._updateMenuStates()
        except RuntimeError:        # the window is being torn down
            pass

    def _settleBand(self) -> None:
        try:
            self._ui.regionCBandController.settle_after_runs()
        except RuntimeError:
            pass

    def _toggleBand(self) -> None:
        self._ui.regionCBandController.toggle()

    def _toggleFocusMode(self) -> None:
        self._ui.regionCBandController.toggle_focus()

    def inFocusMode(self) -> bool:
        return self._ui.regionCBandController.in_focus_mode

    def _bandStateChanged(self, is_open: bool, in_focus: bool) -> None:
        for action, checked in ((self._bandAction, is_open),
                                (self._focusAction, in_focus)):
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)
        self._focusEscape.setEnabled(in_focus)
        ribbon = getattr(self, '_stepRibbon', None)
        if ribbon is not None:
            ribbon.setVisible(not in_focus)
        sheet = getattr(self, '_settingsSheet', None)
        if sheet is not None:
            sheet.set_focus_mode(in_focus)
        # The 8 px frame round the regions is the page's breathing room; with
        # the page gone it is only wasted model.
        layout = self.centralWidget().layout()
        if layout is not None:
            if in_focus:
                if not hasattr(self, '_marginsBeforeFocus'):
                    self._marginsBeforeFocus = layout.contentsMargins()
                layout.setContentsMargins(0, 0, 0, 0)
            elif hasattr(self, '_marginsBeforeFocus'):
                layout.setContentsMargins(self._marginsBeforeFocus)
                del self._marginsBeforeFocus
        if not in_focus:
            self._applyRegionSizes()

    # -- WP7/WP8: the viewport's own controls ------------------------------ #

    def _previewActionRequested(self, action: str):
        """Plan 35 CR3. A button under the viewport's preview notice."""
        manager = getattr(self, '_meshManager', None)
        if manager is None:
            return
        if action == 'retry':
            asyncio.create_task(manager.retryPreview())
        elif action == 'load_volume':
            asyncio.create_task(manager.loadFullVolume())
        elif action == 'build_anyway' and self._confirmBuildPreviewAnyway():
            asyncio.create_task(manager.buildPreviewAnyway())

    def _confirmBuildPreviewAnyway(self) -> bool:
        """"Build anyway" warns first: the worker may take most of the memory."""
        from foammesh.support import resource_budget

        total, _available = resource_budget.physical_memory()
        confirmed = messages.warning(
            self, self.tr('Build the preview anyway?'),
            self.tr('The preview worker will be allowed up to {0} (90% of '
                    'this computer\'s memory). Other programs may slow down '
                    'or be paged out while it runs, and the worker is still '
                    'stopped if it goes over. FoamMesh itself is not at '
                    'risk.').format(resource_budget.format_bytes(
                        int(total * 0.9))),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return confirmed == QMessageBox.StandardButton.Yes

    def _adoptPreviewNotice(self):
        """Plan 43 LY7. The preview notice (Try again, Build anyway, Load
        full volume) stands in the Viewport notice strip at the top centre of
        the view, with the progress strip, not in the Parts card."""
        getter = getattr(self._ui.renderingView, 'noticeStrip', None)
        strip = getter() if callable(getter) else None
        if strip is not None:
            strip.adoptNotice(self._viewportOverlay.previewNoticeWidget())

    def _installViewportRail(self):
        """Plan 40 UXE2. The tool rail and its popovers (section 4.4 item 6).

        The lists that used to open on top of the model -- the parts, the cut
        plane, the section, the display switches -- are popovers hung from a
        narrow rail at the viewport's right edge. The widgets are the ones
        they always were, moved, so each control still has one copy.
        """
        overlay = self._viewportOverlay
        tool = self._renderingTool
        rail = ViewportRail(self._ui.renderingView)
        self._viewportRail = rail
        overlay.setRailMode(True)
        # A short rail folds the highest ``fold`` first (Plan 45 N): the
        # background and the axes go first, Parts and the Inspector last.
        rail.addTool('parts', self.tr('Parts'), 'volume.svg',
                     overlay.takePartsPanel(),
                     tip=self.tr('Parts: show, hide, isolate and explode'))
        # Plan 45 N (P9, Q3 b). The display switches are rail tools of their
        # own, one button each, in a group under a rule after Parts; Show
        # elements stays a panel, opened by its own button. The Display
        # popover that held them all is gone.
        for key, title, widget, button, tip, fold in tool.railDisplayTools():
            rail.adoptTool(key, title, widget, button=button, tip=tip,
                           group=1, fold=fold)
        rail.addTool('elements', self.tr('Show elements'), 'face.svg',
                     tool.displayPanel(), group=1, fold=4,
                     tip=self.tr('Show elements: colour by, cell types, '
                                 'nodes, shrink, labels'))
        # Plan 45 N (Q1 a). No Cut plane or Section tool on the rail: the top
        # bar's Section button opens the one section panel, with its Cut
        # plane and Section tabs (Plan 45 L, ``openSectionPanel``).
        rail.addTool('inspector', self.tr('Inspector'), 'poorCells.svg', None,
                     group=2, fold=1,
                     tip=self.tr('Inspector: the mesh quality report'))
        tool.sendOverflowToRail(rail)
        overlay.partsRequested.connect(lambda: rail.open('parts'))
        rail.actionRequested.connect(self._railToolPressed)
        overlay.sceneEmptyChanged.connect(
            lambda empty: rail.setSceneEmpty(empty, ('parts',)))
        rail.setSceneEmpty(overlay.isSceneEmpty(), ('parts',))
        settings = app.settings
        if hasattr(settings, 'getViewportRail'):
            rail.setPreferences(settings.getViewportRail())
            rail.preferencesChanged.connect(settings.updateViewportRail)
        # Customise: right-click the horizontal toolbar as well as the rail.
        toolbar = self._ui.toolbar
        toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        toolbar.customContextMenuRequested.connect(
            lambda point: rail.showCustomiseMenu(toolbar.mapToGlobal(point)))
        action = self._ui.menuView.addAction(self.tr('&Viewport tool rail'))
        action.setObjectName('actionViewportRail')
        action.setCheckable(True)
        action.setChecked(rail.preferences()['rail'])
        action.setToolTip(self.tr(
            'Show or hide the tool rail at the right edge of the viewport'))
        action.toggled.connect(
            lambda on: rail.setPreferences(
                {**rail.preferences(), 'rail': on}, emit=True))
        rail.preferencesChanged.connect(
            lambda prefs: action.setChecked(prefs['rail']))
        # Its own name: `_railAction` is UXC1's outline rail (Ctrl+B), whose
        # check mark `toggleOutlineRail` keeps. Sharing the name left Ctrl+B
        # without its action and ticked this one when the outline folded.
        self._viewportRailAction = action
        # Plan 43 LY6, LY7. The top bar, the bottom bar and the rail fold to
        # nubs; the Parts chip opens the Parts panel.
        self._viewportToolEdges = ViewportToolEdges(self, rail)
        # Plan 44 MF3: the pills stand on the clear area's centre line and
        # follow it (a sheet or the console moved, a resize).
        self._viewportToolEdges.attachClearArea(self._clearArea)
        self._installViewActions(rail)

    def _installViewActions(self, rail) -> None:
        """Plan 39 VI15. View > Copy view, Save view as..., Hover readout; the
        tool rail's width is the overlay layout's right inset."""
        view = self._ui.renderingView
        menu = self._ui.menuView

        def applyInset(pixels=None):
            layout = view.overlayLayout()
            if layout is None:      # a view without the overlay layout
                return
            layout.setRightInset(int(rail.extent() if pixels is None
                                     else pixels))
            view.arrangeOverlays()
        rail.extentChanged.connect(applyInset)
        applyInset()

        copy = menu.addAction(self.tr('&Copy view'))
        copy.setObjectName('actionCopyView')
        copy.setToolTip(self.tr(
            'Copy the viewport to the clipboard as an image at twice the '
            'window size'))
        copy.triggered.connect(lambda _c=False: view._copyView())
        save = menu.addAction(self.tr('Save &view as…'))
        save.setObjectName('actionSaveViewAs')
        save.setToolTip(self.tr(
            'Save the viewport as an image, optionally with a transparent '
            'background'))
        save.triggered.connect(lambda _c=False: view._saveViewAs())
        hover = menu.addAction(self.tr('&Hover readout'))
        hover.setObjectName('actionHoverReadout')
        hover.setCheckable(True)
        hover.setChecked(bool(view.hoverReadoutEnabled()))
        hover.setToolTip(self.tr(
            'Show the cell or face under the cursor with its field values'))
        hover.toggled.connect(view.setHoverReadoutEnabled)
        # VI14. A cell picked on a cut says what the volume cell it stands
        # for is, not what the cut surface kept.
        view.setHoverVolumeProvider(self._hoverVolume)
        self._copyViewAction = copy
        self._saveViewAction = save
        self._hoverReadoutAction = hover

    def _hoverVolume(self, name: str):
        """The whole volume the viewport actor *name* was cut from."""
        from foammesh.rendering.actor_info import MeshActor
        info = self._meshManager.actorInfo(name)
        return info.dataSet() if isinstance(info, MeshActor) else None

    def _installResourceUsageAction(self) -> None:
        """Plan 39 VI13. View > Show resource usage, remembered."""
        settings = app.settings
        view = self._ui.renderingView
        action = self._ui.menuView.addAction(self.tr('Show resource &usage'))
        action.setObjectName('actionShowResourceUsage')
        action.setCheckable(True)
        action.setToolTip(self.tr(
            'Show CPU load and memory over the viewport, updated each '
            'second'))
        on = bool(settings.getResourceUsage()) if hasattr(
            settings, 'getResourceUsage') else False
        action.setChecked(on)
        view.setResourceOverlayVisible(on)

        def toggled(checked):
            view.setResourceOverlayVisible(checked)
            if hasattr(settings, 'updateResourceUsage'):
                settings.updateResourceUsage(checked)
        action.toggled.connect(toggled)
        self._resourceAction = action

    def _railToolPressed(self, key: str) -> None:
        if key == 'inspector':
            self.showQualityReport()

    def _prepareSectionPopover(self):
        """Plan 45 L (P4, P5, Q1 a). The top bar's Section button is the one
        home of slicing: the cut tool's own `SectionPanel` (one instance,
        taken off the Display Control page) in a popover docked under the
        button, with its *Cut plane* and *Section* tabs.

        The overlay's copy is mirrored onto the same section only while the
        rail still carries a *Cut plane* or *Section* tool (package N retires
        them); with neither left there is one panel and no mirror.

        Plan 45 start-up: the popover itself is made the first time it is
        asked for (:meth:`sectionPopover`). It is hidden until the Section
        button opens it, and moving the whole panel into it held the start
        ~0.14 s. :meth:`_installSectionPopover` makes it."""
        cut = self._displayControl.cutTool()
        rail = getattr(self, '_viewportRail', None)
        if rail is not None and any(
                key in rail.tools() for key in ('cut', 'section')):
            cut.addMirror(self._viewportOverlay.sectionPanel())
        cut.hidePageCopy()
        self._sectionPopover = None
        cut.panel().sectionChanged.connect(self._syncSectionButton)

    def _installSectionPopover(self):
        """Make the popover and hang it in the clear area (on first use)."""
        from .section_popover import SectionPopover
        cut = self._displayControl.cutTool()
        rail = getattr(self, '_viewportRail', None)
        view = self._ui.renderingView
        clearArea = getattr(self, '_clearArea', None)

        def clearRect():
            # The clear area, less the rail at its right edge.
            if clearArea is None:
                return None
            rect = clearArea.clearRect()
            if rail is not None:
                rect.setRight(rect.right() - rail.extent())
            return rect

        popover = SectionPopover(
            cut.panel(), view,
            anchor=getattr(self._renderingTool, '_sectionButton', None),
            clearRect=clearRect)
        self._sectionPopover = popover
        if clearArea is not None:
            changed = getattr(clearArea, 'changed', None)
            if changed is not None:
                changed.connect(lambda *_a: popover.place())
            from foammesh.view.main_window import clear_area as ca
            clearArea.add_float(popover, ca.FLOAT_RANK_POPOVER)
        return popover

    def _syncSectionButton(self):
        """The top bar's Section reads on while a plane cuts the model."""
        tool = self._displayControl.cutTool()
        self._renderingTool.setInspectionActive(
            'section', tool.isSectionActive())

    def sectionPopover(self):
        """Plan 45 L. The popover under the top bar's Section button, made
        on the first call (Plan 45 start-up)."""
        popover = getattr(self, '_sectionPopover', None)
        if popover is None and hasattr(self, '_displayControl'):
            popover = self._installSectionPopover()
        return popover

    def openSectionPanel(self, tab: str = 'cut') -> bool:
        """Plan 45 L. Open the section panel under the top bar's Section
        button on its *Cut plane* (``'cut'``) or *Section* (``'section'``)
        tab. The entry point every other way into slicing uses (the rail's
        former Cut plane and Section tools, View mode > Slice). False when
        there is no popover to open."""
        popover = self.sectionPopover()
        if popover is None:
            return False
        popover.open('section' if tab == 'section' else 'cut')
        return True

    def closeSectionPanel(self) -> None:
        """Fold the section panel away; the planes stay."""
        popover = getattr(self, '_sectionPopover', None)
        if popover is not None:
            popover.dismiss()

    def _connectViewportControls(self):
        tool = self._renderingTool
        tool.sectionRequested.connect(self._quickSection)
        # Plan 43 LY15. The camera button, Ctrl+Shift+P and View > Camera >
        # Save view as... are one popover; `captureViewport` stays as the
        # no-dialog writer the tests and sequences call.
        tool.captureRequested.connect(self.showSnapshot)
        tool.galleryRequested.connect(self._showCaptureGallery)
        tool.isolateRequested.connect(self._isolateSelection)
        tool.zoomSelectionRequested.connect(self._zoomToSelection)
        tool.fitSelectionOrAllRequested.connect(self._fitSelectionOrAll)
        tool.showAllRequested.connect(self._showEveryPart)
        tool.poorCellsRequested.connect(self._highlightPoorCells)
        tool.failedCellsRequested.connect(self._showFailedCellsFromViewport)
        tool.fidelityRequested.connect(self.toggleGeometryFidelity)
        tool.featuresRequested.connect(self.toggleFeatureCapture)
        tool.viewModeRequested.connect(self.applyViewMode)
        tool.layerCoverageRequested.connect(self.showLayerCoverage)
        tool.colourMeshRequested.connect(self.colourMeshBy)

        self._displayControl.meshQualityInfo().statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 8000))
        self._viewportOverlay.previewActionRequested.connect(
            self._previewActionRequested)

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
        # UXB5. The toolbar's model tools follow whether the viewport holds
        # anything: the overlay says so, the tool gates its controls.
        self._viewportOverlay.sceneEmptyChanged.connect(
            self._renderingTool.setSceneEmpty)
        self._renderingTool.setSceneEmpty(self._viewportOverlay.isSceneEmpty())
        self._viewportOverlay.regionFilterChanged.connect(
            self._applyRegionFilter)
        # DP-712. A row click selects the part; Fit and Isolate follow.
        self._viewportOverlay.partSelectionRequested.connect(
            lambda key, additive: self._displayControl.selectParts(
                [key], additive))
        self._displayControl.selectedActorsChanged.connect(
            lambda *_args: self._viewportOverlay.setSelectedParts(
                self._displayControl.selectedActorIds()))
        self._displayControl.visibilityChanged.connect(
            lambda *_args: self._syncRegionPicker())

        tool.explodeChanged.connect(self._setExplode)
        tool.orbitRequested.connect(self._recordOrbit)
        tool.sweepRequested.connect(self._recordSectionSweep)
        tool.saveViewRequested.connect(self._saveNamedView)
        tool.saveNamedViewRequested.connect(self.saveNamedView)
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
        # Plan 35 CR8: nor draws differently, nor stops drawing, silently.
        view = self._ui.renderingView
        for name, slot in (('renderNote', self._viewportRenderNote),
                           ('viewportReset', self._viewportReset)):
            signal = getattr(view, name, None)
            if signal is not None and hasattr(signal, 'connect'):
                signal.connect(slot)

    # -- WP5.2 / WP7.5 / WP8.3 --------------------------------------------- #

    def _installViewportEffectActions(self):
        """The Render quality presets, and WP6's reduce-detail preference.

        render0925 DP-726. Cavity shading and FXAA used to be two loose check
        boxes, defaulting off because their cost had not been measured. It
        has now (plans/evidence/viewport-audit-20260925/render-after), and
        FXAA over the viewport's 8x multisampling turned out to draw a black
        frame -- so they became one exclusive choice that cannot ask for
        that, each option saying what it costs. Balanced is the default.
        """
        from PySide6.QtGui import QActionGroup
        from foammesh.rendering import render_style

        view = self._ui.renderingView
        self._effectActions = {}
        menu = self._ui.menuView.addMenu(self.tr('Render &quality'))
        menu.setObjectName('menuRenderQuality')
        self._renderQualityMenu = menu
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._renderQualityActions = {}
        for name in render_style.PRESETS:
            label, tip = render_style.LABELS[name]
            action = menu.addAction(self.tr(label))
            action.setCheckable(True)
            action.setToolTip(self.tr(tip))
            action.setStatusTip(self.tr(tip))
            action.setData(name)
            group.addAction(action)
            self._renderQualityActions[name] = action
        menu.setToolTipsVisible(True)
        settings = getattr(app, '_settings', None)
        saved = render_style.DEFAULT_PRESET
        if settings is not None and hasattr(settings, 'getRenderQuality'):
            saved = settings.getRenderQuality()
        self._renderQualityActions[saved].setChecked(True)
        view.setRenderQuality(saved)
        group.triggered.connect(self._renderQualityChosen)
        # Plan 35 CR8: in graphics safe mode the preset is not the user's;
        # the saved choice stays and applies at the next normal start.
        isSafeMode = getattr(view, 'isSafeMode', None)
        if callable(isSafeMode) and isSafeMode() is True:
            menu.setTitle(self.tr('Render &quality (safe mode)'))
            menu.setEnabled(False)
            menu.menuAction().setStatusTip(self.tr(
                'Graphics safe mode draws with the simplest settings; your '
                'choice applies again at the next normal start.'))

        entries = (
            (self.tr('Re&duce detail while moving'),
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

    def _buildColourScaleMenu(self, app):
        """Plan 43 LY12. The colour standard redraws the picture.

        The controls are View > Colour scale (the View menu's group, bound to
        the standard in ``view_menu.bind_colour``) and Preferences >
        Appearance > Colour scale. A change lasts for the session until
        *Make this my standard*.
        """
        from foammesh.support import colour_standard

        settings = getattr(app, '_settings', None)
        if settings is not None and not hasattr(settings, 'getColourStandard'):
            settings = None
        self._colourStandardFollower = self._colourStandardChanged
        colour_standard.subscribe(self._colourStandardFollower)
        self._colourStandardChanged(colour_standard.current(settings))

    def _colourStandardChanged(self, _standard=None):
        """The legends show or hide, and the picture redraws, at once."""
        view = self._ui.renderingView
        apply = getattr(view, 'applyColourStandard', None)
        if callable(apply):
            apply()

    def _renderQualityChosen(self, action):
        name = action.data()
        self._applyViewportEffect(
            lambda _checked: self._ui.renderingView.setRenderQuality(name),
            action, True)
        settings = getattr(app, '_settings', None)
        if settings is not None and hasattr(settings, 'updateRenderQuality'):
            settings.updateRenderQuality(name)

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
                self.tr('This VTK build does not provide it'))
            self.statusBar().showMessage(
                self.tr('{0} is not available in this VTK build.').format(
                    action.text()), 6000)

    def _viewportRenderNote(self, text: str):
        if text:
            self.statusBar().showMessage(text, 10000)

    def _viewportReset(self, reason: str):
        self.statusBar().showMessage(self.tr(
            'The viewport stopped drawing ({0}). Use Recreate viewport to '
            'draw it again.').format(reason), 15000)

    def _detailReduced(self, reduced: bool):
        if reduced:
            self.statusBar().showMessage(
                self.tr('Showing reduced detail while the view moves.'), 4000)

    def _isolateSelection(self):
        """DP-353. Isolate says why it did nothing, like its neighbour does.

        `DisplayControl.isolate()` answers False when the selection is empty,
        and this was wired as `lambda: self._displayControl.isolate()`, which
        threw the answer away: the button was enabled, the press was accepted
        and the window neither changed nor spoke. MEASURED by
        `verify_plan31_v_cp09.py` V15.2 on a meshed tee -- `isolate` enabled,
        `scene_changed: false`, `status_message: ""`.

        `Zoom to selection`, two buttons along, has always answered the same
        condition with a sentence. CP-09 item 2 asks that every enabled action
        have an observable effect; saying "there is nothing selected" is one.
        """
        if not self._displayControl.isolate():
            self.statusBar().showMessage(
                self.tr('Select a part first, then show only it.'), 5000)

    def _showEveryPart(self):
        """DP-353. And the way back says what it did.

        Pressing `Show all` when nothing is hidden is a legitimate press with
        no picture to change, which is exactly the case that used to leave the
        window silent. It now confirms the state it put the scene in, which is
        also the answer to "did that press land?".
        """
        control = self._displayControl
        mode = getattr(self, '_viewMode', '')
        plan = (view_modes.plan(mode, self.viewScene())
                if mode in view_modes.MODE_IDS else None)
        if plan is None or not plan.is_available() or not plan.hidden:
            hidden = control.hiddenActorCount()
            control.showAll()
            # DP-2744: one part "was" hidden.
            self.statusBar().showMessage(
                self.tr('Showing every part again (1 was hidden).')
                if hidden == 1 else
                self.tr('Showing every part again ({0} were hidden).').format(
                    hidden) if hidden else
                self.tr('Every part is already shown.'), 5000)
            return
        # DP-705 (viewport audit 0925 F12). In Boundary mode Show all also
        # brought the geometry back, so the picture mixed two views. Within a
        # mode it now means every part *of that mode*; the rest stay hidden.
        hidden = sum(1 for info in control.actorInfosFor(plan.visible)
                     if not info.isVisible())
        control.setVisibilities({**{key: False for key in plan.hidden},
                                 **{key: True for key in plan.visible}})
        label = self.tr(view_modes.mode(plan.mode).label)
        self.statusBar().showMessage(
            self.tr('{0}: showing every part again (1 was hidden).').format(
                label) if hidden == 1 else
            self.tr('{0}: showing every part again ({1} were hidden).').format(
                label, hidden) if hidden else
            self.tr('{0}: every part is already shown.').format(label), 5000)

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

    def _fitSelectionOrAll(self):
        """DP-697. `F`: frame the selection, or the whole model without one."""
        control = self._displayControl
        if control.selectedActorIds():
            self._zoomToSelection()
            return
        self._renderingTool._fitCamera()
        self.statusBar().showMessage(
            self.tr('Nothing is selected, so the whole model is framed.'), 5000)

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
            self, self.tr('Save current view'), self.tr('Name this view'))
        if not accepted or not name.strip():
            return
        self.saveNamedView(name)

    def saveNamedView(self, name: str) -> bool:
        """DP-2765. Save the view on screen as ``name``: what *Save current
        view…* does once its dialog is answered."""
        root = self._captureManager.caseRoot()
        if root is None:
            self.statusBar().showMessage(
                self.tr('Open a case before saving a view.'), 5000)
            return False
        if not str(name or '').strip():
            self.statusBar().showMessage(
                self.tr('A saved view needs a name.'), 5000)
            return False
        try:
            save_view(root, name, self._ui.renderingView.cameraState())
        except (OSError, ViewStateError) as error:
            self.statusBar().showMessage(str(error), 8000)
            return False
        self.refreshNamedViews()
        self.statusBar().showMessage(
            self.tr('Saved the view "{0}".').format(name.strip()), 5000)
        return True

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
        deviation = getattr(self._renderingTool, 'DEVIATION_MODE', 'deviation')
        if modeId == deviation:
            # DP-2746. The box's Deviation entry is the fidelity toggle.
            if not (getattr(self, '_fidelityPainted', False)
                    or self.isLiveDeviationPending()):
                self.toggleGeometryFidelity()
            self._syncViewModeBox()
            return bool(getattr(self, '_fidelityPainted', False)
                        or self.isLiveDeviationPending())
        if self.isLiveDeviationPending():
            self._cancelLiveDeviation()
        if getattr(self, '_fidelityPainted', False):
            # Modes are exclusive: leaving Deviation takes its colouring off,
            # so the box and the legend's tab name the same view.
            self.toggleGeometryFidelity()
        plan = view_modes.plan(modeId, self.viewScene())
        if not plan.is_available():
            self.statusBar().showMessage(self.tr(plan.unavailable), 10000)
            self._renderingTool.showViewMode(self._viewMode)
            return False
        self._displayControl.applyViewPlan(plan)
        self._setSectionEnabled(plan.section)
        getter = getattr(self._displayControl, 'meshQualityInfo', None)
        info = getter() if callable(getter) else None
        if plan.quality:
            # Poor cells is a toggle: entering Quality while its colouring is
            # already on must not take it off.
            if info is None or not info.isHighlighting():
                self._highlightPoorCells()
                self._qualityByMode = True
        elif getattr(self, '_qualityByMode', False):
            # Plan 45 V6. Leaving Quality takes its colouring off: the cell
            # filter, the legend's Quality page and the worst-cell tags
            # stayed on screen over the Boundary mesh.
            # Unconditionally: a measurement still running for it is
            # abandoned too, so it cannot paint after the mode is left.
            self._qualityByMode = False
            if info is not None:
                info.clearHighlight()
            self._renderingTool.setInspectionActive('poorCells', False)
        self._viewMode = plan.mode
        # Plan 45 V5. The Parts count follows the mode: what the mode hides
        # is not a part the user hid.
        if (getattr(self, '_viewportOverlay', None) is not None
                and hasattr(self._displayControl, '_items')):
            self._refreshOverlayChip()
        self._syncInspectorOnModel()
        self._renderingTool.showViewMode(plan.mode)
        self.statusBar().showMessage(
            self.tr('{0}: showing {1}.').format(
                self.tr(view_modes.mode(plan.mode).label), plan.summary), 8000)
        return True

    def _setSectionEnabled(self, enabled: bool):
        """Raise or drop the section plane the Slice mode is made of.

        Plan 45 L (P5): Slice opens the same panel the top bar's Section
        does, on its Cut plane tab."""
        tool = self._displayControl.cutTool()
        panel = tool.panel()
        if enabled:
            self._quickSection(toggle=False)
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

    def _quickSection(self, toggle: bool = True):
        """The top bar's Section button: a plane along the view, and the
        section panel under the button.

        VIEW-02 raised a plane along the current view normal in one click.
        Plan 45 L (P5, Q1 a): the click also opens the section panel docked
        under the button, so the controls of the plane just raised are where
        the hand already is. A second click (*toggle*) folds the panel away
        and leaves the planes cutting: the panel's *Clear* takes them down.
        A plane already cutting is not raised again.
        """
        tool = self._displayControl.cutTool()
        popover = getattr(self, '_sectionPopover', None)
        if toggle and popover is not None and popover.isOpen():
            popover.dismiss()
            self._renderingTool.setInspectionActive(
                'section', tool.isSectionActive())
            return
        if not tool.isSectionActive():
            if not tool.updateBounds():
                self._renderingTool.setInspectionActive('section', False)
                self.statusBar().showMessage(
                    self.tr('Nothing is loaded to cut yet.'), 5000)
                return
            panel = tool.panel()
            index = panel.activeIndex()
            tool._useViewNormal()
            # VI10 (F2a, F3a). The plane comes up the way the user left it:
            # not locked (a locked plane takes no drag, which read as a plane
            # that will not move; Lock stays a choice the user makes and
            # keeps), and with its handle shown unless they hid it.
            panel._planeToggled(index, True)
        self._renderingTool.setInspectionActive(
            'section', tool.isSectionActive())
        self._renderingTool.updateHistoryButtons()
        # The popover is made the first time it is wanted (Plan 45 start-up).
        make = getattr(self, 'sectionPopover', None)
        popover = make() if callable(make) else popover
        if popover is not None:
            popover.open('cut')

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
        if getattr(self, '_overlayRebuildHeld', False):
            # DP-2717: `_clear` is removing every actor; it rebuilds once.
            self._overlayRebuildPending = True
            return
        parts = []
        groups = {}
        roles = {}
        groupPath = getattr(self._displayControl, '_groupPath', None)
        for key, item in self._displayControl._items.items():
            info = item.actorInfo()
            parts.append((key, info.name(), info.color(), info.isVisible()))
            # DP-712. The rows file under the tree's own headings.
            if groupPath is not None:
                groups[key] = ' / '.join(groupPath(info))
            # Plan 44 F1 (MF21). The row's small role text.
            roles[key] = part_role(info)
        # DP-2717. `setParts` builds a row of widgets per part (1.1-1.25 s
        # per call MEASURED on a loaded host), and a load announces the
        # scene more than once with nothing changed in between.
        shown = (tuple(parts), tuple(groups.items()),
                 tuple(roles.items()))
        if shown != getattr(self, '_overlayPartsShown', None):
            self._viewportOverlay.setParts(parts, groups, roles)
            self._overlayPartsShown = shown
        # DP-713. A reload drops the colouring with the actors it was on;
        # the legend and the button have to drop it too.
        if (getattr(self, '_fidelityPainted', False)
                and getattr(self, '_deviationLegend', None) is not None):
            readout = getattr(self._meshManager, 'deviationReadout', None)
            if readout is None or readout() is None:
                self._hideDeviationReadout()
                self._fidelityPainted = False
                self._renderingTool.setInspectionActive('fidelity', False)
                self._syncViewModeBox()
        # Plan 39 VI16. Nor does a reload keep the Layers grown colouring.
        if (getattr(self, '_layersGrownLegend', None) is not None
                and (self._meshManager is None
                     or self._meshManager.layersGrownReadout() is None)):
            self._hideLayersGrownLegend()
            self._renderingTool.setColourMeshChoice(layers_grown.SOLID)
        # Plan 39 VI16. Nor does a reload keep the volume's exterior colour.
        if (getattr(self, '_volumeLegend', None) is not None
                and (self._meshManager is None
                     or not self._meshManager.volumeColoured())):
            self._hideVolumeLegend()
            self._renderingTool.setColourMeshChoice(layers_grown.SOLID)
        # Plan 45 P11. Nor do feature lines drawn on a mesh no longer there.
        MainWindow._dropStaleFeatureActors(self)
        # DP-711. A new scene gets a fresh Region picker.
        refreshPicker = getattr(self, '_refreshRegionPicker', None)
        if refreshPicker is not None:
            refreshPicker()
        self._refreshOverlayChip()
        self._renderingTool.updateScaleReadout()
        self._refreshQualityActions()

    def _regionPickerScope(self):
        """``(regions, volume parts, picker rows)`` for the scene on screen."""
        manager = getattr(self, '_meshManager', None)
        if manager is None or manager.isEmpty():
            return {}, [], []
        regions = manager.regions()
        volumes = manager.volumePartIds()
        return regions, volumes, picker_entries(regions, volumes)

    def _refreshRegionPicker(self):
        """DP-711. A new scene gets a fresh picker: everything ticked."""
        filter_ = getattr(self, '_regionFilter', None)
        if filter_ is None:
            filter_ = self._regionFilter = RegionFilter()
        filter_.reset()
        _regions, _volumes, entries = self._regionPickerScope()
        self._viewportOverlay.setRegionEntries(entries)

    def _syncRegionPicker(self):
        """Show all brings every part back, so the picker ticks All again."""
        filter_ = getattr(self, '_regionFilter', None)
        if filter_ is None or not filter_.isFiltering():
            return
        if all(item.actorInfo().isVisible()
               for item in self._displayControl._items.values()):
            self._refreshRegionPicker()

    def _applyRegionFilter(self, checked):
        """DP-711 (viewport audit 0925 F1): show only the ticked regions."""
        regions, volumes, entries = self._regionPickerScope()
        if not entries:
            return
        filter_ = getattr(self, '_regionFilter', None)
        if filter_ is None:
            filter_ = self._regionFilter = RegionFilter()
        items = self._displayControl._items
        current = {key: item.actorInfo().isVisible()
                   for key, item in items.items()}
        self._displayControl.setVisibilities(
            filter_.apply(regions, volumes, entries, checked, current))

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
        # DP-679. Not every row: a region seed marker is not a part.
        return countedPartIds(self._displayControl._items), self.tr('parts')

    def _modeHiddenParts(self) -> set:
        """Plan 45 V5. The parts the view mode on screen hides by itself.

        Quality draws the volume and hides the boundary patches; that is the
        mode drawing differently, not the user hiding parts, so those parts
        are not counted as hidden ("2 of 2 hidden" with nothing hidden)."""
        mode = getattr(self, '_viewMode', '')
        if mode not in view_modes.MODE_IDS:
            return set()
        try:
            plan = view_modes.plan(mode, self.viewScene())
        except Exception:                                    # noqa: BLE001
            logger.debug('view mode plan unavailable', exc_info=True)
            return set()
        return set(plan.hidden) if plan.is_available() else set()

    def _refreshOverlayChip(self):
        keys, noun = self._countedParts()
        items = self._displayControl._items
        modeHidden = getattr(self, '_modeHiddenParts', None)
        byMode = modeHidden() if callable(modeHidden) else set()
        hidden = sum(1 for key in keys
                     if key not in byMode and not (
                         key in items and items[key].actorInfo().isVisible()))
        self._viewportOverlay.setChip(len(keys) - hidden, len(keys), noun,
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

        The same sentence goes to the run details view (F-09), which is what
        replaced the completion modal.

        GEO-05. A result is news about one case. Reading a stored result is
        asynchronous, so a handle read for the case that is closing can
        arrive after the next case is open, and this method used to draw
        whatever it was handed. MEASURED on the 16 September campaign: a new
        empty SU2 case carried `gmsh-ae088d48b38041e9 · accepted · 21,952
        cells · polyMesh`, a sentence about a case that was no longer open.
        So the handle is asked where it came from, and one that did not come
        from under the open case is not drawn at all.

        The handle carries its own case root, and falls back to the artifact
        it names, because a handle rebuilt from an older record may carry
        only the path of the file. A result with neither is let through: a
        result that cannot say where it came from is not evidence that it
        came from somewhere else.
        """
        home = str(getattr(getattr(app, 'project', None), 'path', '') or '')
        origin = str(getattr(handle, 'case_root', '')
                     or getattr(handle, 'artifact_path', '') or '')
        if handle is not None and home and origin:
            try:
                root = Path(home).resolve()
                came_from = Path(origin).resolve()
                mine = came_from == root or root in came_from.parents
            except (OSError, ValueError):
                mine = False
            if not mine:
                logger.debug('run result %s is not from the open case',
                             getattr(handle, 'run_id', ''))
                return
        description = handle.describe() if handle is not None else ''
        self._offeredResult = None
        self._historicalResult = None
        self._viewportOverlay.setResult(description)
        self.showRunStatus(description)

    #: Plan 37 I2. The run line as it read before an unlock labelled the mesh
    #: historical, or None while no such label is up.
    _historicalResult = None

    def showHistoricalResult(self, stage: str, revision: int = 0) -> None:
        """Label the mesh on screen as the output an unlock kept (I2).

        After "Unlock and discard later results" the viewport still draws
        the mesh on disk, and that mesh is the previous result: none of the
        edits about to be made on the unlocked step are in it. Plan 37 I2
        forbids showing it as though they were, and a page notice is not
        where the mesh is. So the run line over the picture says so, until a
        run publishes (:meth:`clearHistoricalResult`) or the unlock is
        undone.
        """
        manager = getattr(self, '_meshManager', None)
        empty = getattr(manager, 'isEmpty', None)
        if callable(empty) and empty():
            return
        if self._historicalResult is None:
            self._historicalResult = self._viewportOverlay.resultText()
        label = self.tr('Previous mesh — edits to {0} not applied').format(
            stage)
        if revision:
            label = f'{label} · {self.tr("revision {0}").format(int(revision))}'
        self._viewportOverlay.setResult(label)

    def openLikelyStep(self, task_id: str) -> None:
        """Plan 43 LY9. Open the step a failed quality check pointed at."""
        from foammesh.view.main_window.meshing_method_branch import (
            TASK_TOKEN_PREFIX)
        setup = {'setup.geometry': Step.GEOMETRY,
                 'setup.preparation': Step.GEOMETRY_REPAIR}
        if task_id in setup:
            self._navigationView.openStep(setup[task_id])
        else:
            self._navigationView.requestBranch(TASK_TOKEN_PREFIX + task_id)

    def discardResultsOnScreen(self) -> None:
        """Plan 43 LY9. An unlock deleted the mesh and the quality results.

        The viewport, the cell count, the verdict strip and the Mesh quality
        tab show a mesh that is no longer in the case, so they are emptied
        with it (the same as a Base Grid reset, R87). Nothing is labelled
        "previous mesh": there is none.
        """
        self._historicalResult = None
        manager = getattr(self, '_meshManager', None)
        unload = getattr(manager, 'unload', None)
        if callable(unload):
            unload()
        self._viewportOverlay.setResult('')
        self.clearMeshVerdict()

    def restoreResultsOnScreen(self) -> None:
        """Plan 43 LY9. Undo put the mesh and the quality results back: draw
        them again, and show the stored verdict."""
        manager = getattr(self, '_meshManager', None)
        load = getattr(manager, 'load', None)
        if callable(load):
            try:
                asyncio.ensure_future(load(0))
            except Exception:                                # noqa: BLE001
                logger.debug('the restored mesh could not be drawn',
                             exc_info=True)
        refresh = getattr(self, 'refreshMeshVerdictSoon', None)
        if callable(refresh):
            try:
                refresh()
            except Exception:                                # noqa: BLE001
                logger.debug('the restored verdict could not be shown',
                             exc_info=True)

    def clearHistoricalResult(self, restore: bool = False) -> None:
        """Take the historical label down (I2).

        ``restore`` puts back the run line it covered, for an undo that made
        that run's mesh current again. Without it the line is left empty for
        the run that has just published to name.
        """
        previous = self._historicalResult
        if previous is None:
            return
        self._historicalResult = None
        self._viewportOverlay.setResult(previous if restore else '')

    def historicalResultShown(self) -> bool:
        return self._historicalResult is not None

    def offerSurfacePass(self, handle) -> bool:
        """Say a run kept its surface mesh, and draw it only if asked.

        DP-133. The surface mesh exists for the length of the volume pass and
        was then discarded unshown, which is why a Gmsh leg of the sweep
        captured two stages against snappy's five. The runner keeps it now,
        and this is where it becomes reachable.

        Offered, never substituted. The volume mesh is what the run produced
        and it stays on screen; swapping it for the surface would be F-37 in
        a new coat -- one run's artifact shown under another artifact's
        sentence. The same one-slot offer the earlier-result path uses, so
        there is no second way for a mesh to reach the viewport.

        Returns whether an offer was made.
        """
        strip = getattr(self, '_runStatusStrip', None)
        surface = (handle.surface_result()
                   if hasattr(handle, 'surface_result') else None)
        if strip is None or surface is None:
            return False
        self._offeredResult = surface
        strip.showOffer(
            f'{handle.describe()} · '
            f'{self.tr("this run also kept its surface mesh")}',
            self.tr('Show the surface mesh'), failed=False,
            # The finished run's log is still the thing a user reaches for
            # next; the offer must not take it away to put a button there.
            log=strip.logPath())
        return True

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
        self._historicalResult = None
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
        self._historicalResult = None
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
        from foammesh.core.case import ArtifactState
        from foammesh.core.run_result import result_on_open

        # DP-794. A root mesh changed since the run that wrote it is not that
        # run's result, whatever its publication record says.
        resolution = getattr(app, 'workflowResolution', None)
        stale = getattr(resolution, 'artifact_state', None) is ArtifactState.STALE
        handle = None
        try:
            handle = result_on_open(app.project.path, root_mesh_stale=stale)
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
        # A run that ended has just recorded its step in the undo history.
        QTimer.singleShot(0, self._refreshMenusAfterRun)

    def showHistory(self) -> None:
        """Open the History panel (Edit > History)."""
        self.showRunDetails()
        self._runDetailsTabs.setCurrentWidget(self._historyTab)

    def _refreshHistoryPanel(self) -> None:
        panel = getattr(self, '_historyPanel', None)
        if panel is None or app.project is None:
            return
        result = query(app.facadeClient, 'history.query', {'limit': 1})
        panel.setEntries(result.payload.get('undo_history') or {})

    def _jumpHistory(self, direction: str, steps: int) -> None:
        """Go to a step the History panel chose: as many Undos or Redos."""
        def done(result):
            if (getattr(result, 'payload', None) or {}).get('steps'):
                self.statusBar().showMessage(
                    self.tr('Went to the chosen step.'), 3000)
            self._updateMenuStates()
            self._refreshHistoryPanel()

        submit(app.facadeClient, f'history.{direction}', {'steps': steps},
               then=done)

    def showRunDetails(self) -> None:
        """Open the run details view (QA-04).

        The one route to the run sentence now that it has left the page
        footer. Reached from Help > Run details, and from the Details
        control on the quality page, which W-I wires to this slot.

        Never raised by the product itself: a run that ends still says so
        without taking the window away, and this is where a user goes to
        read it, cancel what is still running, take the offer of an earlier
        mesh, or open the log.
        """
        view = getattr(self, '_runDetailsView', None)
        if view is None:
            return
        strip = getattr(self, '_runStatusStrip', None)
        empty = getattr(self, '_runDetailsEmpty', None)
        if empty is not None:
            empty.setVisible(strip is None or strip.state == 'idle')
        history = getattr(self, '_runHistoryText', None)
        if history is not None:
            history.setPlainText(self._historyText())
        self._refreshHistoryPanel()
        view.show()
        view.raise_()
        view.activateWindow()

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
        failed = bool(MainWindow._failedSets(self))
        self._renderingTool.setQualityActionsEnabled(
            hasMesh,
            self.tr('No mesh is loaded'),
            failed,
            MainWindow._noFailedSetsReason(self))
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
        # DP-713. With the reference geometry loaded, deviation is measured
        # on the spot, so only a mesh with neither is refused -- and told
        # both ways out.
        geometry = getattr(self, '_geometryManager', None)
        hasReference = False
        if hasMesh and geometry is not None:
            try:
                hasReference = geometry.referenceSurface() is not None
            except Exception:                                 # noqa: BLE001
                logger.debug('no reference surface', exc_info=True)
        fidelityReason = noMesh if not hasMesh else self.tr(
            'This mesh has not been checked against its reference geometry, '
            'and none is loaded to measure it against. Run the geometry '
            'fidelity task, or import the geometry the mesh was made from, '
            'to colour deviation here.')
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
            if sections and self.nativeMeasurementRefusal(report):
                sections = {}
            features = bool(sections) or bool(self._featureActors)
            if not features:
                featuresReason = self.tr(
                    'The fidelity run recorded no feature measurements, so '
                    'there are no declared feature lines to draw. Declare '
                    'features on the geometry and run it again.')
        if not features and hasMesh and MainWindow._nativeCurveFeatures(self):
            # Plan 41 GN23. A Gmsh mesh always has its curve entities to show.
            features = True
        if not features and hasMesh and (
                MainWindow._boundaryFeatureDataSets(self)
                or getattr(self, '_featureActors', None)):
            # Plan 45 P11 (Q5 a). Any boundary on screen has edges of its own
            # to draw, and drawn edges keep their way off.
            features = True
        # A painted viewport keeps its own way back: the button that put the
        # colouring on is the button that takes it off, so disabling it while
        # the colour is still there would strand the reader.
        fidelity = (report is not None or self._fidelityPainted
                    or hasReference)
        # Plan 48 GM12 (DP-4366). The one rule for the button, shared with
        # the local API: a session reference or a CAD file enables it too,
        # and what is missing is the tooltip when it is not.
        native = hasMesh and MainWindow._nativeMeshFile(self) is not None
        sessionReference = False
        if native and not fidelity:
            try:
                sessionReference = MainWindow.gmshSessionReference(
                    self) is not None
            except Exception:                                 # noqa: BLE001
                logger.debug('no Gmsh session reference', exc_info=True)
        enabled, why = gmsh_quality.deviation_availability(
            has_mesh=hasMesh, session_reference=sessionReference,
            stored_report=report is not None,
            geometry_reference=hasReference,
            cad_reference=MainWindow._nativeCadReference(self) is not None)
        fidelity = fidelity or enabled
        if not enabled and native:
            # The snappy route keeps its own sentence.
            fidelityReason = self.tr(why)
        self._renderingTool.setInspectionActionsEnabled(
            fidelity, fidelityReason, features, featuresReason)

    def _refreshLayerAction(self, hasMesh: bool):
        """Gate the layer-coverage entry on a layer run having happened.

        Separate from the fidelity gate above because it asks a different
        question of a different source -- the stored coverage document rather
        than the fidelity report -- and because a layer stage is a snappy
        thing that a Gmsh case will never have.
        """
        native = MainWindow._nativeMeshFile(self) if hasMesh else None
        if native is not None:
            # Plan 48 GM12 (DP-4358). A Gmsh mesh is measured on the file when
            # the entry is used; the gate only reads what was measured, and
            # stays open until a measurement says there are no layer cells.
            held = MainWindow._gmshLayerCached(self, native)
            self._renderingTool.setLayerCoverageEnabled(
                held is None or bool(held.rows),
                self.tr('This mesh has no prism or hexahedral layer cells, '
                        'so there is no coverage to show.'))
            return
        coverage = (self.layerCoverage() if hasMesh
                    else view_modes.LayerCoverage())
        self._renderingTool.setLayerCoverageEnabled(
            coverage.measured,
            self.tr('No mesh is loaded') if not hasMesh else self.tr(
                'No boundary-layer run has been measured on this case, so '
                'there is no coverage to show. Run the layer stage first.'))

    def _gmshLayerCached(self, native):
        """The layer coverage already measured for *native*, else None."""
        held = getattr(self, '_gmshLayerHeld', None)
        if held is None:
            return None
        try:
            stamp = Path(native).stat().st_mtime_ns
        except OSError:
            return None
        return held[2] if held[:2] == (str(native), stamp) else None

    def gmshLayerCoverage(self, native=None) -> view_modes.LayerCoverage:
        """Plan 48 GM12 (DP-4358). Layers counted on the Gmsh mesh file.

        The rows are what ``layers_grown.gmsh_layer_coverage`` measures, in
        the shape the snappy path hands the same viewport tools; a patch the
        project record gave no request for reads its layers as grown.
        """
        native = native or MainWindow._nativeMeshFile(self)
        if native is None:
            return view_modes.LayerCoverage()
        cached = MainWindow._gmshLayerCached(self, native)
        if cached is not None:
            return cached
        try:
            measured = layers_grown.gmsh_layer_coverage(
                native, MainWindow._resultRoots(self))
        except Exception:                                     # noqa: BLE001
            logger.debug('no layer coverage on the Gmsh mesh', exc_info=True)
            return view_modes.LayerCoverage()
        rows = []
        for row in measured.get('objects') or ():
            full = getattr(row, 'full', None)
            faces = int(getattr(row, 'faces', 0) or 0)
            share = (100.0 * full / faces if full is not None and faces
                     else 0.0)
            rows.append(view_modes.LayerRow(
                row.patch, float(row.layers),
                int(row.requested) if row.requested_known else 0,
                share, bool(row.frozen)))
        coverage = view_modes.LayerCoverage(True, tuple(rows))
        try:
            self._gmshLayerHeld = (str(native), Path(native).stat().st_mtime_ns,
                                   coverage)
        except OSError:
            pass
        return coverage

    def _nativeCadReference(self):
        """Plan 48 GM12. The CAD file the Gmsh mesh on screen was made from,
        from the project record, or None."""
        native = MainWindow._nativeMeshFile(self)
        if native is None:
            return None
        try:
            from foammesh.core import provenance
            found = provenance.cad_reference(provenance.find(
                MainWindow._resultRoots(self), native))
        except Exception:                                     # noqa: BLE001
            return None
        path = (found.value or {}).get('path') if found.is_measured else None
        return path if path and Path(path).is_file() else None

    def layerCoverage(self) -> view_modes.LayerCoverage:
        """What the layer stage achieved, read through the facade.

        Empty rather than raising when the operation is unknown or the case
        has none: the answer "nobody measured" is the one the caller needs,
        and it is the same answer either way.
        """
        if MainWindow._nativeMeshFile(self) is not None:
            # Plan 48 GM12 (DP-4358). A Gmsh mesh: counted on the file.
            return MainWindow.gmshLayerCoverage(self)
        try:
            result = query(app.facadeClient, 'mesh.layer_coverage')
        except (FacadeError, RuntimeError, OSError, ValueError, KeyError):
            logger.debug('no readable layer coverage', exc_info=True)
            return view_modes.LayerCoverage()
        # DP-667. The document is the result's payload. Handing on the result
        # itself read as "not measured" on every case, measured or not.
        return view_modes.summarise_layers(getattr(result, 'payload', None))

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

    def colourMeshBy(self, key: str) -> bool:
        """Plan 39 VI16 (I6). *Colour mesh by*: Solid, or the layers grown.

        Layers grown paints each patch with the count the stored layer report
        says the run achieved on it; patches that asked for none are grey. The
        legend sits in the viewport's stack with the others, and Solid (or a
        new mesh) takes both away.
        """
        tool = self._renderingTool
        manager = self._meshManager
        if manager is None:
            tool.setColourMeshChoice(layers_grown.SOLID)
            return False
        if layers_grown.is_volume_choice(key):
            return self._colourVolume(key)
        self._dropVolumeColour()
        if key != layers_grown.LAYERS_GROWN:
            self._dropLayersGrown()
            return True
        coverage = self.layerCoverage()
        if not coverage.measured or not coverage.rows:
            tool.setColourMeshChoice(layers_grown.SOLID)
            self.statusBar().showMessage(self.tr(coverage.describe()), 10000)
            return False
        if self._fidelityPainted:
            # One colouring on the patches at a time.
            self.toggleGeometryFidelity()
            tool.setInspectionActive('fidelity', False)
        painted = manager.showLayersGrown(coverage.rows)
        self._showLayersGrownLegend()
        self.statusBar().showMessage(self.tr(
            'Layers grown on {0}; patches that asked for none are grey.'
        ).format(count_text(painted, 'patch', 'patches')), 12000)
        return True

    def _colourVolume(self, key: str) -> bool:
        """Plan 39 VI16 (I6). Colour the volume's exterior by *key*.

        The same choices and mapping as *Colour cut by* in the section: cell
        type, refinement level, region / zone and quality. A level or a zone
        is read from the case off the GUI thread, so with an event loop the
        paint lands when the read does.
        """
        tool = self._renderingTool
        self._dropLayersGrown()
        tool.setColourMeshChoice(key)
        work = self._paintVolume(key)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return bool(asyncio.run(work))
        asyncio.ensure_future(work)
        return True

    async def _paintVolume(self, key: str) -> bool:
        tool = self._renderingTool
        manager = self._meshManager
        try:
            mapped, reason = await manager.colourVolumeBy(key)
        except Exception as error:  # noqa: BLE001 - the viewport must not take the window down
            logger.warning('could not colour the volume by %s: %s', key, error)
            mapped, reason = {'kind': 'none'}, str(error)
        if tool.colourMeshChoice() != key:
            # Another choice was made while this one was being worked out.
            return False
        if mapped.get('kind') == 'none':
            self._hideVolumeLegend()
            manager.clearVolumeColour()
            tool.setColourMeshChoice(layers_grown.SOLID)
            self.statusBar().showMessage(self.tr(reason), 12000)
            return False
        self._showVolumeLegend(mapped)
        self.statusBar().showMessage(self.tr(
            'The volume is coloured by {0}. Solid takes the colour off.'
        ).format(mapped.get('label', '')), 10000)
        return True

    def _showVolumeLegend(self, mapped):
        from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

        from foammesh.rendering.section_colour_map import lookupTable

        display = getattr(self, '_displayControl', None)
        self._hideVolumeLegend()
        if display is None:
            return
        bar = vtkScalarBarActor()
        bar.SetObjectName('volumeColourLegend')
        bar.SetLookupTable(lookupTable(mapped))
        bar.SetTitle(str(mapped.get('label', '')))
        if mapped.get('kind') == 'categorical':
            bar.SetNumberOfLabels(0)
        else:
            bar.SetNumberOfLabels(5)
            bar.SetLabelFormat('%.3g')
        bar.UnconstrainedFontSizeOn()
        bar.SetTitleRatio(0.5)
        bar.SetBarRatio(0.25)
        bar.SetWidth(0.2)
        bar.SetHeight(0.45)
        if app.themeManager is not None and app.themeManager.tokens is not None:
            from foammesh.view.theming.vtk_theme import apply_scalar_bar_theme
            apply_scalar_bar_theme(bar, app.themeManager.tokens)
        self._volumeLegend = bar
        display.addLegend(VOLUME_LEGEND_KEY, bar)

    def _hideVolumeLegend(self):
        if getattr(self, '_volumeLegend', None) is None:
            return
        display = getattr(self, '_displayControl', None)
        if display is not None:
            display.removeLegend(VOLUME_LEGEND_KEY)
        self._volumeLegend = None

    def _dropVolumeColour(self):
        """Take the volume's exterior colour and its legend off."""
        manager = self._meshManager
        clear = getattr(manager, 'clearVolumeColour', None)
        if clear is not None:
            clear()
        self._hideVolumeLegend()

    def _volumeColourDropped(self):
        """The volume's colour went (a cut began): the menu and legend follow."""
        self._hideVolumeLegend()
        tool = self._renderingTool
        if layers_grown.is_volume_choice(tool.colourMeshChoice()):
            tool.setColourMeshChoice(layers_grown.SOLID)

    def _showLayersGrownLegend(self):
        from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

        readout = self._meshManager.layersGrownReadout()
        display = getattr(self, '_displayControl', None)
        self._hideLayersGrownLegend()
        if readout is None or display is None:
            return
        bar = vtkScalarBarActor()
        bar.SetObjectName('layersGrownLegend')
        bar.SetLookupTable(readout['table'])
        bar.SetTitle(layers_grown.legend_title())
        bar.SetNumberOfLabels(int(readout['range'][1]) + 1
                              if readout['range'][1] <= 8 else 5)
        bar.SetLabelFormat('%.3g')
        bar.UnconstrainedFontSizeOn()
        bar.SetTitleRatio(0.5)
        bar.SetBarRatio(0.25)
        bar.DrawNanAnnotationOn()
        bar.SetNanAnnotation('asked for none')
        bar.SetWidth(0.2)
        bar.SetHeight(0.45)
        if app.themeManager is not None and app.themeManager.tokens is not None:
            from foammesh.view.theming.vtk_theme import apply_scalar_bar_theme
            apply_scalar_bar_theme(bar, app.themeManager.tokens)
        self._layersGrownLegend = bar
        display.addLegend(layers_grown.LEGEND_KEY, bar)

    def _hideLayersGrownLegend(self):
        if getattr(self, '_layersGrownLegend', None) is None:
            return
        display = getattr(self, '_displayControl', None)
        if display is not None:
            display.removeLegend(layers_grown.LEGEND_KEY)
        self._layersGrownLegend = None

    def _dropLayersGrown(self):
        """Take the Layers grown colouring and its legend off."""
        manager = self._meshManager
        if manager is not None and manager.layersGrownReadout() is not None:
            manager.clearLayersGrown()
        self._hideLayersGrownLegend()
        self._renderingTool.setColourMeshChoice(layers_grown.SOLID)

    def toggleGeometryFidelity(self):
        """WP3.3. Colour the surface by how far it left the reference geometry.

        Two resolutions, and the viewport says which one it is showing. The
        per-face field only exists where a fidelity run produced it; the
        per-section verdict comes from the stored report and works on any case
        that has ever been qualified. Falling back silently would leave a user
        reading a patch-level colour as if it located the problem.

        DP-200: the same press takes the colouring off again. Painting is a
        door, and a door a user cannot walk back through is a trap -- the
        only way out of the deviation colours used to be reloading the mesh.
        """
        wasPainted = bool(getattr(self, '_fidelityPainted', False))
        if getattr(self, '_liveDeviationPending', False):
            # DP-2741. The same press takes back a measurement in flight.
            self._cancelLiveDeviation()
            self.statusBar().showMessage(self.tr(
                'Deviation measurement cancelled.'), 4000)
            self._renderingTool.setInspectionActive('fidelity', False)
            self._syncViewModeBox()
            return
        try:
            if not self._fidelityPainted and getattr(
                    self, '_layersGrownLegend', None) is not None:
                self._dropLayersGrown()
            if self._fidelityPainted:
                if self._meshManager is not None:
                    self._meshManager.clearFidelityColouring()
                self._hideDeviationReadout()
                self._fidelityPainted = False
                self.statusBar().showMessage(self.tr(
                    'Fidelity colouring cleared.'), 4000)
                return

            root = self._captureManager.caseRoot()
            if root is None and self._meshManager is not None:
                # Plan 42 SG2. A mesh on screen that no case holds (a native
                # Gmsh mesh from the session) keeps its results beside itself.
                root = self._meshManager.nativeStoreRoot()
            if root is None or self._meshManager is None:
                self.statusBar().showMessage(
                    self.tr('Open a case with a mesh first.'), 5000)
                return

            report = self._latestFidelityReport()
            if report is None:
                # DP-713. No stored run: measure against the geometry.
                if self._paintLiveDeviation() is not None:
                    return
                self.statusBar().showMessage(self.tr(
                    'This mesh has not been checked against its reference '
                    'geometry. Run the geometry fidelity task first.'), 9000)
                return

            refusal = self.nativeMeasurementRefusal(report)
            if refusal:
                # Plan 42 SG2. A stored result that is not of this mesh is no
                # reason not to measure it now.
                if self._paintLiveDeviation() is not None:
                    return
                self.statusBar().showMessage(refusal, 12000)
                return

            task_id = str(report.get('task_id') or '')
            fields = hotspot.read(root, task_id) if task_id else {}
            if fields:
                painted = self._meshManager.showFidelityHotspots(
                    fields, unit=self._caseLengthUnit(),
                    tolerance=self._deviationTolerance(report))
                self._fidelityPainted = painted > 0
                self._showDeviationReadout()
                self.statusBar().showMessage(self.tr(
                    'Deviation from the reference geometry, per face, on {0}.'
                ).format(count_text(painted, 'patch', 'patches')), 12000)
                return

            # DP-713. A run with no per-face field says which patch; the
            # geometry, when it is loaded, says where.
            if self._paintLiveDeviation():
                return
            verdicts = {
                str(section.get('name') or ''): str(section.get('verdict') or '')
                for section in report.get('sections') or ()
            }
            painted = self._meshManager.showSectionFidelity(verdicts)
            self._fidelityPainted = painted > 0
            self.statusBar().showMessage(self.tr(
                'Fidelity verdict per patch on {0} — no per-face field was '
                'recorded for this run, so this shows which patch, not where.'
            ).format(count_text(painted, 'patch', 'patches')), 12000)
        finally:
            # VIEW-04. Every exit above either paints, clears or refuses,
            # and the toolbar icon has to end up saying which -- a refusal
            # used to leave it looking exactly like a success.
            if self._fidelityPainted and not wasPainted:
                self._enterDeviationView()
            self._renderingTool.setInspectionActive(
                'fidelity', bool(self._fidelityPainted))
            self._syncViewModeBox()

    #: DP-2736. The modes that hide the boundary patches the deviation is
    #: painted on: Quality draws the poor cells of the volume instead and
    #: Geometry no mesh at all. Any other (Boundary, Volume, Slice, or none
    #: chosen yet) draws them.
    _DEVIATION_HIDDEN_IN = (view_modes.QUALITY, view_modes.GEOMETRY)

    def _enterDeviationView(self) -> None:
        """DP-2736. Show the patches the deviation was painted on.

        Measured on the dev build: Deviation turned on in Quality mode
        painted the elbow patch while the mode kept it hidden; the model on
        screen was the volume's poor cells coloured by non-orthogonality
        (mostly blue), under the deviation legend and the deviation labels.
        The Poor cells colouring is taken off too: its cells are drawn in
        front of the patches (DP-737's bias).
        """
        getter = getattr(getattr(self, '_displayControl', None),
                         'meshQualityInfo', None)
        info = getter() if callable(getter) else None
        mode = getattr(self, '_viewMode', '') or ''
        if mode in self._DEVIATION_HIDDEN_IN:
            # Not painted yet as far as applyViewMode is concerned: it must
            # not take the colouring straight back off.
            painted, self._fidelityPainted = self._fidelityPainted, False
            try:
                self.applyViewMode(view_modes.BOUNDARY)
            finally:
                self._fidelityPainted = painted
        if info is not None and info.isHighlighting():
            self._qualityByMode = False
            info.clearHighlight()
            self._renderingTool.setInspectionActive('poorCells', False)

    def _syncViewModeBox(self) -> None:
        """DP-2746. The View mode box names the view on screen: Deviation
        while the model is graded by deviation, else the mode entered."""
        tool = getattr(self, '_renderingTool', None)
        show = getattr(tool, 'showViewMode', None)
        if show is None:
            return
        if getattr(self, '_fidelityPainted', False):
            show(getattr(tool, 'DEVIATION_MODE', 'deviation'))
        else:
            show(getattr(self, '_viewMode', '') or '')

    def _paintLiveDeviation(self):
        """DP-713. Colour deviation measured now against the loaded geometry.

        Viewport audit 0925 F8. ``None`` when there is no geometry to
        measure against, else how many patches were coloured.
        """
        geometry = getattr(self, '_geometryManager', None)
        manager = self._meshManager
        if manager is None:
            return None
        cad = MainWindow._nativeCadReference(self)
        if cad is not None and MainWindow._liveDeviationOffThread():
            # Plan 48 GM12 (DP-4366). A Gmsh mesh made from a CAD file is
            # measured to the trimmed faces of that file, on the file.
            return MainWindow._paintNativeDeviation(self, cad)
        reference = (geometry.referenceSurface()
                     if geometry is not None else None)
        source = self.tr('the loaded geometry')
        if reference is not None:
            reference = self._referenceInMeshUnit(reference)
        else:
            # Plan 42 SG2 (D13). A model opened only in the Gmsh session has
            # no geometry in the shared step: its own surface is the
            # reference.
            reference = self.gmshSessionReference()
            source = self.tr('the open Gmsh model')
        if reference is None:
            return None
        measure = getattr(manager, 'measureLiveDeviation', None)
        if callable(measure) and MainWindow._liveDeviationOffThread():
            # DP-2741: measured in a worker process; painted when it answers.
            self._liveDeviationPending = True
            task = asyncio.ensure_future(measure(
                reference, unit=self._caseLengthUnit(),
                tolerance=self._deviationTolerance(),
                identity=self._patchIdentity()))
            task.add_done_callback(
                lambda done: self._liveDeviationMeasured(done, source))
            self.statusBar().showMessage(self.tr(
                'Measuring the deviation from {0}…').format(source), 60000)
            return MainWindow.LIVE_DEVIATION_PENDING
        painted = manager.showLiveDeviation(
            reference, unit=self._caseLengthUnit(),
            tolerance=self._deviationTolerance(),
            identity=self._patchIdentity())
        # Called on stand-ins in the tests: the helper by the class.
        MainWindow._deviationPainted(self, painted, source)
        return painted

    #: DP-2741. What `_paintLiveDeviation` answers while the worker measures:
    #: true (something will be shown) and not ``None`` (there is geometry).
    LIVE_DEVIATION_PENDING = -1

    @staticmethod
    def _liveDeviationOffThread() -> bool:
        """DP-2741. Measure in a worker only with an event loop to paint on."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def isLiveDeviationPending(self) -> bool:
        return bool(getattr(self, '_liveDeviationPending', False))

    def _cancelLiveDeviation(self) -> None:
        self._liveDeviationPending = False
        cancel = getattr(self._meshManager, 'cancelLiveDeviation', None)
        if callable(cancel):
            cancel()

    def _paintNativeDeviation(self, cad):
        """Plan 48 GM12 (DP-4366). Measure the Gmsh mesh file to its CAD file in
        a worker thread; ``_nativeDeviationMeasured`` paints the answer."""
        import functools

        native = MainWindow._nativeMeshFile(self)
        if native is None:
            return None
        loop = asyncio.get_running_loop()
        job = functools.partial(
            gmsh_quality.surface_deviation, native, cad,
            tolerance_mm=self._deviationTolerance() or None,
            unit=self._caseLengthUnit())
        source = self.tr('the CAD file {0}').format(Path(cad).name)
        self._liveDeviationPending = True
        task = asyncio.ensure_future(loop.run_in_executor(None, job))
        task.add_done_callback(
            lambda done: self._nativeDeviationMeasured(done, source))
        self.statusBar().showMessage(self.tr(
            'Measuring the deviation from {0}…').format(source), 60000)
        return MainWindow.LIVE_DEVIATION_PENDING

    def _nativeDeviationMeasured(self, task, source) -> None:
        """Plan 48 GM12 (DP-4366). Draw ``surface_deviation`` in the shared
        panel: on the patches whose faces match the elements, and always as
        the readout."""
        if not getattr(self, '_liveDeviationPending', False):
            return
        self._liveDeviationPending = False
        try:
            result = task.result()
        except asyncio.CancelledError:
            return
        except Exception as error:                          # noqa: BLE001
            logger.warning('the Gmsh deviation measurement failed: %s', error)
            self._renderingTool.setInspectionActive('fidelity', False)
            self.statusBar().showMessage(self.tr(
                'The deviation could not be measured: {0}').format(error),
                12000)
            return
        painted = MainWindow._paintNativeFields(self, result)
        readout = gmsh_quality.deviation_readout(result)
        if painted:
            self._deviationPainted(painted, source)
        else:
            self._fidelityPainted = True
            panel = getattr(self, '_deviationPanel', None)
            if panel is not None:
                panel.setReferenceName(source)
            self._showDeviationReadout(readout)
            self.statusBar().showMessage(self.tr(
                'Deviation from {0}: {1}').format(
                    source, readout.notice), 12000)
        if self._fidelityPainted:
            self._enterDeviationView()
        self._renderingTool.setInspectionActive(
            'fidelity', bool(self._fidelityPainted))
        self._syncViewModeBox()

    def _paintNativeFields(self, result) -> int:
        """Colour each patch whose face count equals its element count."""
        import numpy as np

        manager = self._meshManager
        paint = getattr(manager, '_paintDeviation', None)
        if not callable(paint):
            return 0
        fields = {}
        surfaces = {}
        for actor_id in getattr(manager, '_patchIds', ()):
            info = manager._actorInfos.get(actor_id)
            data = info.dataSet() if info is not None else None
            if data is None:
                continue
            surfaces[actor_id] = data
            values = result['fields'].get(str(actor_id).rsplit(':', 1)[-1])
            if values is not None and len(values) == data.GetNumberOfCells():
                fields[actor_id] = (np.arange(len(values), dtype=np.int64),
                                    np.asarray(values, dtype=np.float64))
        if not fields:
            return 0
        readout = gmsh_quality.deviation_readout(result)
        return paint(
            fields, {name: 0 for name in surfaces}, 'live', signed=False,
            tolerance=readout.tolerance, scale=readout.scale,
            universe=[(name, name in fields, '') for name in surfaces],
            ticked=set(fields), notice=readout.notice)

    def _liveDeviationMeasured(self, task, source) -> None:
        """DP-2741. The worker answered: paint, as the press would have."""
        if not getattr(self, '_liveDeviationPending', False):
            return
        self._liveDeviationPending = False
        try:
            painted = task.result()
        except asyncio.CancelledError:
            return
        except Exception:                                   # noqa: BLE001
            logger.exception('the live deviation measurement failed')
            painted = 0
        if painted is None:
            return
        self._deviationPainted(painted, source)
        if self._fidelityPainted:
            self._enterDeviationView()
        self._renderingTool.setInspectionActive(
            'fidelity', bool(self._fidelityPainted))
        self._syncViewModeBox()

    def _deviationPainted(self, painted, source) -> None:
        self._fidelityPainted = painted > 0
        if painted:
            panel = getattr(self, '_deviationPanel', None)
            if panel is not None:
                # The panel says what the measurement is against, as the
                # status line does (Plan 42 SG2).
                panel.setReferenceName(source)
            self._showDeviationReadout()
            self.statusBar().showMessage(self.tr(
                'Deviation from {0}, measured now per face, on {1}.'
            ).format(source, count_text(painted, 'patch', 'patches')), 12000)
        else:
            self.statusBar().showMessage(self.tr(
                'No patch of this mesh could be measured against {0}.'
            ).format(source), 9000)

    def _referenceInMeshUnit(self, reference):
        """Plan 42 SG2. The imported geometry is in metres; a native mesh in
        another unit is measured in its own, so the reference is scaled to it."""
        manager = self._meshManager
        scale = getattr(manager, 'nativeScaleToMetres', None)
        scale = scale() if callable(scale) else 1.0
        native = getattr(manager, 'nativePath', None)
        if native is None or native() is None or abs(scale - 1.0) < 1e-12:
            return reference
        from vtkmodules.vtkCommonTransforms import vtkTransform
        from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter

        transform = vtkTransform()
        transform.Scale(1.0 / scale, 1.0 / scale, 1.0 / scale)
        moved = vtkTransformPolyDataFilter()
        moved.SetTransform(transform)
        moved.SetInputData(reference)
        moved.Update()
        return moved.GetOutput()

    def gmshSessionReference(self):
        """Plan 42 SG2 (D13). The open Gmsh model's own surface as polydata,
        or ``None`` when no session holds a model.

        The session tessellates its CAD finely without touching its mesh
        (``reference_surface``); the surface is in the model's own unit,
        which is the unit of the mesh it made.
        """
        import tempfile

        from foammesh.view.gmsh_workflow.compute_page import (
            residentGenerateController)
        client = getattr(app, 'facadeClient', None)
        controller = (residentGenerateController(client)
                      if client is not None else None)
        if controller is None:
            return None
        try:
            with tempfile.TemporaryDirectory(
                    prefix='foammesh-reference-') as directory:
                written = controller.reference_surface(directory)
                path = written.get('host_path')
                if not path or not written.get('cells'):
                    return None
                from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
                reader = vtkXMLPolyDataReader()
                reader.SetFileName(str(path))
                reader.Update()
                surface = reader.GetOutput()
                return surface if surface.GetNumberOfCells() else None
        except Exception:                                     # noqa: BLE001
            logger.debug('no Gmsh session reference', exc_info=True)
            return None

    def _showDeviationReadout(self, readout=None):
        """DP-713. The legend, histogram and figures for the colouring.

        Plan 48 GM12: *readout* is given when a Gmsh measurement drew no patch
        and the numbers are all there is to show.
        """
        manager = self._meshManager
        if readout is None:
            readout = getattr(manager, 'deviationReadout', None)
            readout = readout() if readout is not None else None
        if readout is None:
            return
        self._hideDeviationReadout()
        tokens = (app.themeManager.tokens
                  if app.themeManager is not None else None)
        display = getattr(self, '_displayControl', None)
        if display is not None:
            self._deviationLegend = deviationScalarBar(
                readout.lookup_table, tokens,
                signed=bool(getattr(readout, 'signed', True)))
            # Plan 43 LY11. The one Qt legend draws the bar; this actor stays
            # for the picture-taking path and is not shown on screen.
            # DP-2740. As a bare hidden overlay it was in no picture either:
            # only the view's picture-only legends are shown for a capture.
            self._deviationLegend.SetVisibility(False)
            # DP-2738/2739: its ends and the tolerance, as on screen.
            markDeviationBar(self._deviationLegend, readout)
            display.addLegend('deviation', self._deviationLegend)
            pictureOnly = getattr(self._ui.renderingView,
                                  'setLegendOnScreen', None)
            if pictureOnly is not None:
                pictureOnly('deviation', False)
        panel = getattr(self, '_deviationPanel', None)
        if panel is not None:
            panel.setReadout(
                readout, getattr(readout, 'tolerance', None)
                if getattr(readout, 'tolerance', None) is not None
                else self._deviationTolerance())
        self._refreshWorstValues()
        self._legendController.refresh()

    def _hideDeviationReadout(self):
        legend = getattr(self, '_deviationLegend', None)
        display = getattr(self, '_displayControl', None)
        if legend is not None and display is not None:
            display.removeLegend('deviation')
        self._deviationLegend = None
        panel = getattr(self, '_deviationPanel', None)
        if panel is not None:
            panel.clearReadout()
        self._refreshWorstValues()
        controller = getattr(self, '_legendController', None)
        if controller is not None:
            controller.refresh()

    def _refreshWorstValues(self) -> None:
        views = getattr(self, '_qualityViews', None)
        if views is not None:
            views.refresh()

    def _deviationTolerance(self, report=None) -> float:
        """The tolerance the figures count against, in mm.

        The user's own once they have set one -- zero included, a value like
        any other (Plan 39 VI6, F4g). Until then a stored run's own tolerance
        (``report``, F4e), else a tenth of the base cell, or a thousandth of
        the model when there is no cell size. A proposed value never replaces
        one the user typed.
        """
        chosen = getattr(self, '_deviationToleranceMm', None)
        if chosen is not None:
            return chosen
        run = self._runToleranceMm(report)
        if run is not None:
            return run
        cellSize = extent = None
        geometry = getattr(self, '_geometryManager', None)
        try:
            cellSize = geometry.getCellSize() if geometry is not None else None
        except Exception:                                     # noqa: BLE001
            logger.debug('no base cell size', exc_info=True)
        try:
            bounds = self._meshManager.getBounds().toTuple()
            extent = max(bounds[1] - bounds[0], bounds[3] - bounds[2],
                         bounds[5] - bounds[4])
        except Exception:                                     # noqa: BLE001
            logger.debug('no mesh extent', exc_info=True)
        return live_distance.default_tolerance(
            cellSize, extent, self._caseLengthUnit())

    def _caseLengthUnit(self) -> float:
        """Metres in one unit of the mesh on screen (Plan 39 VI5, F4c).

        The geometry and a generated mesh are in metres; Mesh > Scale makes
        one unit ``1 / factor`` metres, and the case records that scale.
        """
        from foammesh.core.mesh import transform_record
        # Plan 42 SG2. A native mesh is in the unit its session recorded, not
        # the case's.
        manager = getattr(self, '_meshManager', None)
        native = getattr(manager, 'nativePath', None)
        if native is not None and native() is not None:
            return float(manager.nativeScaleToMetres())
        try:
            return transform_record.metres_per_unit(
                transform_record.load(app.project.path))
        except Exception:                                     # noqa: BLE001
            return 1.0

    def _runToleranceMm(self, report):
        """The smallest tolerance a stored run rated its sections by, in mm."""
        if not report:
            return None
        found = []
        for section in report.get('sections') or ():
            try:
                tolerance = float(section.get('tolerance'))
            except (TypeError, ValueError, AttributeError):
                continue
            if tolerance > 0.0:
                found.append(tolerance)
        if not found:
            return None
        return min(found) * live_distance.unit_to_mm(self._caseLengthUnit())

    def _patchIdentity(self):
        """The patch-identity map written beside this mesh, or None."""
        from foammesh.core.quality import patch_identity
        try:
            # Plan 42 SG2. The sidecar sits beside the mesh on screen: a
            # native mesh's own store, else the case.
            manager = getattr(self, '_meshManager', None)
            native = getattr(manager, 'nativePath', None)
            root = (manager.nativeStoreRoot()
                    if native is not None and native() is not None
                    else self._captureManager.caseRoot())
        except Exception:                                     # noqa: BLE001
            return None
        if root is None:
            return None
        quality = Path(root) / 'foammesh' / 'quality'
        for sidecar in (quality / 'geometry' / patch_identity.IDENTITY_FILENAME,
                        quality / patch_identity.IDENTITY_FILENAME):
            try:
                return patch_identity.read(sidecar)
            except (OSError, ValueError):
                continue
        return None

    def _deviationToleranceEdited(self, value):
        """Plan 39 VI6 (F4a, F4g). The tolerance moves the neutral band.

        The value is kept whatever it is (zero too); the model's table is
        refilled without measuring again, and the panel's histogram, drawn in
        that table's colours, follows.
        """
        self._deviationToleranceMm = float(value)
        manager = getattr(self, '_meshManager', None)
        change = getattr(manager, 'setDeviationTolerance', None)
        if change is not None:
            change(float(value))
        legend = getattr(self, '_deviationLegend', None)
        readout = getattr(manager, 'deviationReadout', None)
        if legend is not None and readout is not None:
            markDeviationBar(legend, readout(), float(value))
        panel = getattr(self, '_deviationPanel', None)
        if panel is not None:
            panel.refresh()
        controller = getattr(self, '_legendController', None)
        if controller is not None:
            controller.syncTolerance(float(value))
            controller.refresh()

    def _deviationScaleEdited(self, value):
        """The scale the ramp reaches changed (0 = automatic): repaint."""
        manager = getattr(self, '_meshManager', None)
        change = getattr(manager, 'setDeviationScale', None)
        if change is not None and change(float(value) or None):
            self._refreshDeviationReadout()

    def _deviationSurfaceToggled(self, name, ticked):
        """A patch ticked or unticked in the Surfaces list: re-measure it only."""
        manager = getattr(self, '_meshManager', None)
        change = getattr(manager, 'setDeviationSurface', None)
        if change is not None and change(str(name), bool(ticked)):
            self._refreshDeviationReadout()

    def _refreshDeviationReadout(self):
        """Follow a repaint with the same legend and panel, not new ones."""
        readout = getattr(self._meshManager, 'deviationReadout', None)
        readout = readout() if readout is not None else None
        if readout is None:
            self._hideDeviationReadout()
            return
        legend = getattr(self, '_deviationLegend', None)
        if legend is not None:
            legend.SetLookupTable(readout.lookup_table)
            markDeviationBar(legend, readout)
        panel = getattr(self, '_deviationPanel', None)
        if panel is not None:
            panel.setReadout(readout, readout.tolerance)
        self._refreshWorstValues()
        self._legendController.refresh()

    def toggleFeatureCapture(self):
        """WP3.1. Draw the declared features, coloured by whether they survived.

        "Did the mesher capture my feature" was previously answerable only by
        exporting and eyeballing, and before that not at all -- the feature
        measurement existed and nothing ever called it.
        """
        if self._featureActors or getattr(self, '_featureJob', None):
            for actor in self._featureActors:
                self._displayControl.removeOverlay(actor)
            self._featureActors = []
            self._featureJob = None
            self._featureKey = None
            self._renderingTool.setInspectionActive('features', False)
            self.statusBar().showMessage(self.tr('Feature lines hidden.'), 4000)
            return

        root = self._captureManager.caseRoot()
        report = self._latestFidelityReport() if root is not None else None
        task_id = str((report or {}).get('task_id') or '')
        sections = {}
        if task_id and not self.nativeMeasurementRefusal(report):
            sections = hotspot.read_features(root, task_id)
        curves = MainWindow._nativeCurveFeatures(self) if not sections else {}
        if curves:
            # Plan 41 GN23. A native mesh with no measured features shows the
            # model's own curves; they are drawn as what they are, unmeasured.
            sections = curves
        if not sections:
            # Plan 45 P11 (Q5 a). Nothing declared and no Gmsh curves: the
            # boundary's own creases, rims and non-manifold edges.
            MainWindow._drawBoundaryFeatures(self)
            return

        tokens = (app.themeManager.tokens
                  if app.themeManager is not None else None)
        self._featureActors = feature_overlay.build_actors(sections, tokens)
        for actor in self._featureActors:
            self._displayControl.addOverlay(actor)
        self._featureKey = MainWindow._boundaryFeatureKey(self)
        self._renderingTool.setInspectionActive(
            'features', bool(self._featureActors))

        counts = feature_overlay.summarise(sections)
        self.statusBar().showMessage(self.tr(
            'Declared features: {0}.').format(', '.join(
                f'{count} {verdict}' for verdict, count
                in sorted(counts.items()))), 12000)

    def _boundaryFeatureDataSets(self) -> list:
        """Plan 45 P11. The boundary patches' datasets on screen, or ``[]``."""
        mesh = getattr(self, '_meshManager', None)
        patchIds = getattr(mesh, 'patchIds', None)
        actorInfo = getattr(mesh, 'actorInfo', None)
        if not callable(patchIds) or not callable(actorInfo):
            return []
        try:
            if mesh.isEmpty():
                return []
            found = []
            for key in patchIds():
                info = actorInfo(key)
                data = info.dataSet() if info is not None else None
                if data is not None and data.GetNumberOfCells() > 0:
                    found.append(data)
            return found
        except Exception:                                     # noqa: BLE001
            logger.debug('no readable boundary datasets', exc_info=True)
            return []

    def _boundaryFeatureKey(self) -> tuple:
        """Which boundary the drawn feature lines belong to."""
        return tuple(id(data) for data in
                     MainWindow._boundaryFeatureDataSets(self))

    def _dropFeatureActors(self):
        """Take the feature lines down: unload, or a different mesh."""
        control = getattr(self, '_displayControl', None)
        for actor in getattr(self, '_featureActors', None) or ():
            if control is not None:
                control.removeOverlay(actor)
        self._featureActors = []
        self._featureJob = None
        self._featureKey = None
        tool = getattr(self, '_renderingTool', None)
        if tool is not None:
            tool.setInspectionActive('features', False)

    def _dropStaleFeatureActors(self):
        """Drop feature lines drawn over a boundary no longer on screen."""
        if ((getattr(self, '_featureActors', None)
             or getattr(self, '_featureJob', None))
                and getattr(self, '_featureKey', None)
                != MainWindow._boundaryFeatureKey(self)):
            MainWindow._dropFeatureActors(self)

    def _drawBoundaryFeatures(self):
        """Plan 45 P11 (Q5 a). Draw the boundary's own feature edges.

        The angle is the project feature angle (the one Surface features'
        includedAngle is derived from), 30 degrees by default. A large
        boundary is read on a worker thread; a press while it runs, or a
        new mesh, abandons it.
        """
        data = MainWindow._boundaryFeatureDataSets(self)
        if not data:
            self._renderingTool.setInspectionActive('features', False)
            self.statusBar().showMessage(self.tr(
                'No boundary is on screen to draw feature edges from.'), 9000)
            return
        from foammesh.view.geometry.feature_angle_panel import (
            project_feature_angle)
        angle = project_feature_angle()
        key = tuple(id(item) for item in data)
        if (boundary_feature_edges.face_count(data)
                > boundary_feature_edges.SYNC_FACE_LIMIT):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                job = object()
                self._featureJob = job
                self._renderingTool.setInspectionActive('features', True)
                self.statusBar().showMessage(self.tr(
                    "Finding the boundary's feature edges…"), 9000)
                asyncio.create_task(MainWindow._drawBoundaryFeaturesLater(
                    self, job, key, data, angle))
                return
        MainWindow._showBoundaryFeatures(
            self, boundary_feature_edges.extract_edges(data, angle), key,
            angle)

    async def _drawBoundaryFeaturesLater(self, job, key, data, angle):
        try:
            edges = await asyncio.to_thread(
                boundary_feature_edges.extract_edges, data, angle)
        except Exception:                                     # noqa: BLE001
            logger.exception('could not extract the boundary feature edges')
            edges = None
        if getattr(self, '_featureJob', None) is not job:
            return
        self._featureJob = None
        if MainWindow._boundaryFeatureKey(self) != key:
            self._renderingTool.setInspectionActive('features', False)
            return
        MainWindow._showBoundaryFeatures(self, edges, key, angle)

    def _showBoundaryFeatures(self, edges, key, angle):
        tokens = (app.themeManager.tokens
                  if app.themeManager is not None else None)
        actor = boundary_feature_edges.build_actor(edges, tokens)
        if actor is None:
            self._renderingTool.setInspectionActive('features', False)
            self.statusBar().showMessage(self.tr(
                'The boundary has no edge sharper than {0:g} degrees.'
            ).format(angle), 9000)
            return
        self._featureActors = [actor]
        self._featureKey = key
        self._displayControl.addOverlay(actor)
        self._renderingTool.setInspectionActive('features', True)
        self.statusBar().showMessage(self.tr(
            'Boundary feature edges at {0:g} degrees: {1}.').format(
                angle, count_text(edges.GetNumberOfLines(), 'edge', 'edges')),
            12000)

    def _nativeCurveFeatures(self) -> dict:
        """Plan 41 GN23. The curve entities of the native mesh on screen as
        feature records (``unrated``: nothing measured them), or ``{}``."""
        mesh = getattr(self, '_meshManager', None)
        curves = getattr(mesh, 'nativeCurves', None)
        found = curves() if callable(curves) else {}
        return {
            name: [{'feature_uuid': f'{name}#{index}', 'verdict': 'unrated',
                    'critical': False, 'max_distance': 0.0,
                    'length_coverage': 0.0,
                    'points': [[float(v) for v in point]
                               for point in chain]}
                   for index, chain in enumerate(chains)]
            for name, chains in found.items() if chains}

    def _latestFidelityReport(self):
        """The most recent stored fidelity report for this case, or None.

        Plan 48 GM12 (DP-4351). A Gmsh project keeps its reports in the
        native store beside the mesh, so both roots are looked in and the
        newer report wins.
        """
        roots = MainWindow._resultRoots(self)
        if not roots:
            return None
        try:
            from foammesh.core.gmsh import quality as gmsh_quality
            return gmsh_quality.latest_report(roots)
        except Exception:
            logger.debug('no readable fidelity report', exc_info=True)
            return None

    def _resultRoots(self) -> list:
        """Plan 48 GM12. ``[case root, native store root]``, those that exist."""
        roots = []
        try:
            case = self._captureManager.caseRoot()
        except Exception:                                     # noqa: BLE001
            case = None
        if case is not None:
            roots.append(case)
        manager = getattr(self, '_meshManager', None)
        store = getattr(manager, 'nativeStoreRoot', None)
        try:
            store = store() if callable(store) else None
        except Exception:                                     # noqa: BLE001
            store = None
        if store is not None and all(
                Path(store) != Path(root) for root in roots):
            roots.append(store)
        return roots

    def _nativeMeshFile(self):
        """Plan 48 GM12. The Gmsh ``.msh`` on screen, or None (a case mesh)."""
        manager = getattr(self, '_meshManager', None)
        getter = getattr(manager, 'nativePath', None)
        try:
            found = getter() if callable(getter) else None
        except Exception:                                     # noqa: BLE001
            found = None
        return Path(found) if found else None

    def _highlightPoorCells(self):
        """Colour the worst tenth of the metric range, and uncolour it.

        VIEW-01 and VIEW-04. The press painted and never unpainted, and the
        control said nothing either way -- a mesh with no measurement yet
        paints seconds later, so the icon follows the colouring rather than
        the click, through the observer installed here.
        """
        tool = self._renderingTool
        info = self._displayControl.meshQualityInfo()
        info.setHighlightObserver(
            lambda active: tool.setInspectionActive('poorCells', active))
        if info.isHighlighting():
            info.clearHighlight()
            return
        if not info.highlightWorst():
            tool.setInspectionActive('poorCells', False)
            self.statusBar().showMessage(
                self.tr('No mesh is loaded to measure.'), 6000)
            return
        # True can also mean "a measurement is running"; the observer says
        # when it actually paints, so the icon is never lit on a promise.
        tool.setInspectionActive('poorCells', info.isHighlighting())

    def _resetInspectionView(self):
        """Put the plain model back, whatever inspection left on it.

        VIEW-05. Every overlay had its own way off, and nothing put them all
        back at once. The two controls that looked like the way back --
        Previous view and Next view -- walk the camera history and leave
        every colour, cut and overlay exactly where it was, so a user who had
        stacked a section over a quality colouring had no single answer to
        "show me the mesh again".
        """
        tool = self._renderingTool
        info = self._displayControl.meshQualityInfo()
        info.clearHighlight()
        self._displayControl.cutTool().clearSection()
        if self._meshManager is not None:
            self._meshManager.clearFailedCells()
            self._meshManager.clearFidelityColouring()
        self._hideLayersGrownLegend()
        self._dropVolumeColour()
        pick = getattr(tool, 'setColourMeshChoice', None)
        if pick is not None:
            pick(layers_grown.SOLID)
        self._failedCellSetIndex = -1
        self._fidelityPainted = False
        for name in ('poorCells', 'section', 'failedCells', 'fidelity'):
            tool.setInspectionActive(name, False)
        tool.updateHistoryButtons()
        self.statusBar().showMessage(
            self.tr('Inspection overlays cleared.'), 4000)

    def nativeMeasurementRefusal(self, report) -> str:
        """Plan 41 GN23. ``''`` when a stored measurement is of the native
        Gmsh file on screen, else why it is not drawn over it.

        A stored deviation field or feature record is keyed by the labels of
        one mesh; the native task records the digest of the file it measured,
        and only that file is painted.
        """
        mesh = getattr(self, '_meshManager', None)
        getter = getattr(mesh, 'nativePath', None)
        if getter is None or getter() is None:
            return ''
        recorded = str((report or {}).get('native_mesh_digest') or '')
        if recorded and recorded == mesh.nativeDigest():
            return ''
        if recorded:
            return self.tr(
                'This measurement is of another Gmsh mesh than the one on '
                'screen (the mesh was meshed again, or another file is '
                'open). Run the native mesh fidelity task again.')
        return self.tr(
            'This stored measurement does not say which Gmsh mesh it is of, '
            'so it is not drawn over the one on screen. Run the native mesh '
            'fidelity task again.')

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

    def _caseMeshOnScreen(self) -> bool:
        """The viewport shows, or will show, the case's own polyMesh, not a
        native mesh file whose cell numbers the sets do not address."""
        manager = getattr(self, '_meshManager', None)
        getter = getattr(manager, 'nativePath', None)
        return getter is None or getter() is None

    def _noFailedSetsReason(self) -> str:
        manager = getattr(self, '_meshManager', None)
        getter = getattr(manager, 'nativePath', None)
        if getter is not None and getter() is not None:
            return self.tr('The quality gate has named no offending elements '
                           'of this Gmsh mesh')
        return self.tr('No mesh check has written a failed-cell set yet')

    # -- Plan 41 GN23 (FM-04): named selections of elements ---------------- #

    SELECTION_PREFIX = 'selection: '

    def _selectionStore(self):
        """Plan 42 SG14 (D8). Where saved selections are kept: beside the
        native mesh on screen, else in the case of the mesh read from it."""
        manager = getattr(self, '_meshManager', None)
        getter = getattr(manager, 'nativeStoreRoot', None)
        root = getter() if callable(getter) else None
        if root is None:
            case = getattr(manager, 'caseRoot', None)
            root = case() if callable(case) else None
        return root

    def _meshIdentity(self) -> dict:
        getter = getattr(getattr(self, '_meshManager', None),
                         'meshIdentity', None)
        return getter() if callable(getter) else {}

    def refreshElementSelections(self) -> None:
        from foammesh.core.gmsh import named_selections
        store = self._selectionStore()
        here = MainWindow._meshIdentity(self)
        marked = {}
        for name, record in (named_selections.load(store).items()
                             if store else ()):
            if record.get('kind') == named_selections.ELEMENT:
                marked[name] = named_selections.origin_notice(record, here)
        self._meshQualityTab.set_selections(sorted(marked), marked)

    def _saveElementSelection(self, name, tags) -> None:
        from foammesh.core.gmsh import named_selections
        store = self._selectionStore()
        if store is None:
            self.statusBar().showMessage(self.tr(
                'Selections are kept with the project; load a mesh '
                'first.'), 6000)
            return
        try:
            named_selections.save(store, name, elements=tags,
                                  mesh=MainWindow._meshIdentity(self))
        except named_selections.SelectionError as error:
            self.statusBar().showMessage(
                self.tr('Not saved: {0}.').format(error), 6000)
            return
        self.refreshElementSelections()
        self.statusBar().showMessage(self.tr(
            'Saved "{0}": {1:,} cells.').format(str(name).strip(), len(tags)),
            6000)

    def _showElementSelection(self, name) -> None:
        sets = MainWindow._failedSets(self)
        key = self.SELECTION_PREFIX + str(name)
        cells = sets.get(key)
        tool = self._renderingTool
        from foammesh.core.gmsh import named_selections
        store = self._selectionStore()
        record = (named_selections.load(store).get(str(name), {})
                  if store else {})
        notice = named_selections.origin_notice(
            record, MainWindow._meshIdentity(self))
        if not cells:
            # WALK-2 (SG14). A selection made on the other engine's mesh can
            # number past every cell here; the note is why it found none.
            self.statusBar().showMessage(self.tr(
                '"{0}" has no cells in the mesh on screen.').format(name)
                + (' ' + self.tr(notice) if notice else ''), 10000)
            return
        self._meshManager.showFailedCells(list(cells), key)
        tool.setInspectionActive('failedCells', True)
        tool.setFailedCellSetShown(key)
        self.statusBar().showMessage(
            self.tr('{0}: {1:,} cells.').format(key, len(cells))
            + (' ' + self.tr(notice) if notice else ''), 10000)

    def _deleteElementSelection(self, name) -> None:
        from foammesh.core.gmsh import named_selections
        store = self._selectionStore()
        if store is not None:
            named_selections.delete(store, name)
        self.refreshElementSelections()

    def failedCellSetNames(self) -> list:
        return list(MainWindow._failedSets(self))

    def _failedSets(self) -> dict:
        """``{set name: cell positions}`` the failed-cells button steps
        through: checkMesh's written sets, or for a native Gmsh mesh the
        elements the quality gate named as offending, one set per measure."""
        manager = getattr(self, '_meshManager', None)
        getter = getattr(manager, 'nativePath', None)
        native = getter is not None and getter() is not None
        strip = getattr(self, '_verdictStrip', None)
        rows = ((getattr(strip, 'verdict', None) or {}).get('offending') or ()
                if native else ())
        by_measure: dict = {}
        for row in rows:
            if isinstance(row, dict) and row.get('tag') is not None:
                by_measure.setdefault(
                    str(row.get('measure') or 'quality'), []).append(
                        int(row['tag']))
        found = (dict(getattr(self, '_lastFailedCellSets', None) or {})
                 if not native else {})
        for measure, tags in by_measure.items():
            cells = manager.nativeCellsForTags(tags)
            if cells:
                found[self.tr('{0} below the limit').format(measure)] = cells
        store = MainWindow._selectionStore(self)
        if store is not None:
            from foammesh.core.gmsh import named_selections
            for name, record in sorted(named_selections.load(store).items()):
                if record.get('kind') != named_selections.ELEMENT:
                    continue
                cells = manager.cellsForElements(record.get('elements', ()))
                if cells:
                    found[MainWindow.SELECTION_PREFIX + name] = cells
        return found

    def _showFailedCellsFromViewport(self):
        """Step to the next failed-cell set, rather than only ever the first.

        MEASURED: this took `next(iter(sets))` and stopped. checkMesh routinely
        writes several -- skewFaces, nonOrthoFaces, wrongOrientedFaces -- and
        the toolbar could reach exactly one of them, chosen by dictionary
        order, with nothing on screen saying the others existed.

        VIEW-03 and VIEW-04. Stepping wrapped round to the first set, so the
        overlay had no off state at all; the press after the last set now
        takes it off, the menu names every set and carries an `Off` entry of
        its own, and a request that is refused or finds nothing leaves the
        control dark rather than lit over an unchanged picture.
        """
        tool = self._renderingTool
        requested = tool.requestedFailedCellSet()
        sets = MainWindow._failedSets(self)
        names = list(sets or ())
        stepped = getattr(self, '_failedCellSetIndex', -1) + 1
        # Off: asked for by name from the menu, or reached by stepping past
        # the last set. Wrapping to the first set again meant the only exit
        # from the overlay was reloading the mesh.
        if requested == '' or (requested not in names and names
                               and stepped >= len(names)):
            if self._meshManager is not None:
                self._meshManager.clearFailedCells()
            self._failedCellSetIndex = -1
            tool.setInspectionActive('failedCells', False)
            self.statusBar().showMessage(self.tr('Failed cells hidden.'), 4000)
            return
        if not sets:
            tool.setInspectionActive('failedCells', False)
            # checkMesh's sets index the polyMesh; with a native mesh on
            # screen they are refused in words, not silently dropped.
            case_sets = getattr(self, '_lastFailedCellSets', None)
            text = (self.caseOverlayRefusal() if case_sets
                    else MainWindow._noFailedSetsReason(self) + '.')
            self.statusBar().showMessage(text, 6000)
            return
        refusal = ('' if getattr(self._meshManager, 'nativePath', None)
                   and self._meshManager.nativePath() is not None
                   else self.caseOverlayRefusal())
        if refusal:
            tool.setInspectionActive('failedCells', False)
            self.statusBar().showMessage(refusal, 12000)
            return
        index = names.index(requested) if requested in names else stepped
        self._failedCellSetIndex = index
        name = names[index]
        ids = list(sets[name])
        self._meshManager.showFailedCells(ids, name)
        tool.setInspectionActive('failedCells', True)
        tool.setFailedCellSetShown(name)
        if len(names) <= 1:
            position = ''
        elif index + 1 < len(names):
            position = self.tr('  ({0} of {1} sets — press again for the next)'
                               ).format(index + 1, len(names))
        else:
            # The last set. Saying "press again for the next" here promised a
            # set that does not exist, which is how the wrap-round read.
            position = self.tr('  ({0} of {1} sets — press again to clear)'
                               ).format(index + 1, len(names))
        self.statusBar().showMessage(
            self.tr('{0}: {1:,} cells.').format(name, len(ids)) + position,
            10000)

    # -- the snapshot popover (Plan 43 LY15) -------------------------------- #

    def showSnapshot(self, *_args):
        """Open the Snapshot popover under the camera button."""
        from .snapshot_popover import SnapshotPopover

        view = self._ui.renderingView
        popover = getattr(self, '_snapshotPopover', None)
        if popover is None:
            popover = make(SnapshotPopover, self)
            popover.saveRequested.connect(self._snapshotSave)
            popover.copyRequested.connect(self._snapshotCopy)
            self._snapshotPopover = popover
        popover.attach(view)
        popover.setSuggestedName(self._captureManager.suggestedName(
            popover.options().format))
        popover.setNote('')
        # DP-2742. It opens under the camera button that opened it. The
        # clear-area spot (clear-left + 16, top 56) put it at the far left of
        # the viewport under the cell-count chip, away from the button, with
        # its title cut off.
        button = getattr(self._renderingTool, '_captureButton', None)
        if button is not None and button.isVisible():
            popover.showBelow(button, within=self)
            return popover
        clear = self.clearRectOfView()
        if clear is not None and view.isVisible():
            # The button is in the overflow menu: the clear area, as before.
            popover.showInClearArea(view, clear)
            return popover
        popover.showBelow(view, within=self)
        return popover

    def _snapshotSave(self):
        popover = self._snapshotPopover
        options = popover.options()
        self.snapshotFlash().clear()
        from foammesh.rendering import label_pass

        # DP-2741: the PNG is encoded and written on a worker thread; the
        # GUI held 0.22 s of a 0.71 s Save writing a 2x picture.
        with label_pass.deferred_writes() as pending:
            path = self._captureManager.snapshot(options)
        writer = pending.start()
        message = self._captureManager.snapshotMessage
        if path is None:
            popover.setNote(message)
            self.statusBar().showMessage(message, 8000)
            return None
        popover.hide()
        self._flashViewport()
        self.statusBar().showMessage(message, 10000)
        if options.directory is None:
            self._showCapturesAfter(writer)
        return path

    def _showCapturesAfter(self, writer):
        """The Captures page once *writer* has put the file down (its
        thumbnail reads it), on a later turn of the event loop."""
        if writer is None:
            self.showCaptures()
            return

        def check():
            if writer.is_alive():
                QTimer.singleShot(30, check)
            else:
                self.showCaptures()
        QTimer.singleShot(0, check)

    def _snapshotCopy(self):
        from .view_capture import COPY_SCALE, copyView
        from foammesh.core import snapshot as snapshot_core

        popover = self._snapshotPopover
        options = popover.options().normalised()
        self.snapshotFlash().clear()
        scale = options.scale if snapshot_core.is_raster(options.format) \
            else COPY_SCALE
        image = compose(
            copyView, self._ui.renderingView, scale=scale,
            transparent=bool(options.includes.get('transparent')),
            overlay=options.overlay, includes=dict(options.includes))
        if image is None:
            self.statusBar().showMessage(self.tr(
                'The viewport is not drawing, so there is nothing to copy.'), 6000)
            return None
        popover.hide()
        self._flashViewport()
        self.statusBar().showMessage(self.tr(
            'Copied the view to the clipboard ({0} × {1}).').format(
                image.width(), image.height()), 6000)
        return image

    def snapshotFlash(self):
        """The VTK-drawn flash over the view (Plan 44 MF22), made on first use."""
        from .snapshot_flash import SnapshotFlash

        flash = getattr(self, '_snapshotFlash', None)
        if flash is None:
            flash = SnapshotFlash(self._ui.renderingView, self)
            self._snapshotFlash = flash
        return flash

    def _flashViewport(self) -> bool:
        """A white veil over the view fading .75 -> 0 in 450 ms: the picture
        was taken. Drawn by VTK, because the native viewport paints over a
        plain widget. Skipped under reduced motion or a render hold."""
        return self.snapshotFlash().fire()

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

    def _showCaptureGallery(self):
        """The gallery button, which has to answer even when nothing moves.

        DP-353. `Open the capture gallery` was wired straight at
        `showCaptures`, whose whole effect is to bring the Captures page up in
        the output panel. Press it while that page is already up -- which is
        the state every capture leaves behind, because taking one opens it --
        and the window neither redraws nor says anything. An enabled control
        that answers a press with nothing at all is indistinguishable from a
        broken one. So the press names what it opened and how much is in it.
        """
        page = self.showCaptures()
        if page is None:
            self.statusBar().showMessage(
                self.tr('Open a case before opening the capture gallery.'),
                5000)
            return None
        count = len(self._captureManager.captures())
        self.statusBar().showMessage(
            self.tr('Captures: {0} saved for this case.').format(count)
            if count else
            self.tr('Captures: none yet — press Capture to save the view.'),
            6000)
        return page

    def showCaptures(self):
        page = self._outputTabs.show(
            'captures', self.tr('Captures'), CapturesPage)
        if page is None:
            return None
        # A page that was never connected has nothing to disconnect, and
        # PySide6 says so with a RuntimeWarning rather than an error.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
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
            'effective_setup', self.tr('Effective meshing setup'),
            lambda: EffectiveSetupPage(payload, self._ui.regionCTabHost))

    def showEffectiveEnginePlan(self, payload: dict):
        from .output_pages import JsonOutputPage
        return self._outputTabs.show(
            'effective_engine_plan', self.tr('Effective engine plan'),
            lambda: JsonOutputPage(
                self.tr('Immutable engine plan'), payload,
                self._ui.regionCTabHost))

    def setScaleNote(self, note):
        """Let the current step page annotate the viewport scale readout."""
        self._renderingTool.setScaleNote(note)

    def _dropScaleNoteOffItsPage(self, step):
        """The base-cell clause belongs to the page that computes it.

        DP-106. the legacy base-grid page's `hide` cleared the note, and the note was still
        seen on frames of a Gmsh run -- a route that has no base grid and
        never will -- which means at least one way of leaving that page does
        not go through `hide`. Rather than hunt for it, the note is dropped
        whenever the displayed step is not the one that owns it, so no route
        out of the page can strand it.
        """
        if step is not Step.BASE_GRID:
            self._renderingTool.setScaleNote('')

    @property
    def displayControl(self):
        return self._displayControl

    @property
    def viewportOverlay(self):
        """Plan 42 SG9. The card and Parts list over the viewport."""
        return self._viewportOverlay

    @property
    def geometryManager(self) -> GeometryManager:
        return self._geometryManager

    @property
    def meshManager(self):
        return self._meshManager

    _DROP_SUFFIXES = ('.stl', '.obj', '.step', '.stp', '.iges', '.igs', '.brep')

    @staticmethod
    def droppedGeometryFiles(mime):
        """The geometry files a drag carries, in the order dragged."""
        if mime is None or not mime.hasUrls():
            return []
        found = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() in MainWindow._DROP_SUFFIXES:
                found.append(path)
        return found

    def dragEnterEvent(self, event):
        if MainWindow.droppedGeometryFiles(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        files = MainWindow.droppedGeometryFiles(event.mimeData())
        if not files:
            event.ignore()
            return
        event.acceptProposedAction()
        # Out of the event handler: the import runs, and may ask, on the loop.
        QTimer.singleShot(0, lambda: self._importDropped(files))

    @qasync.asyncSlot()
    async def _importDropped(self, files):
        """Import dropped model files as the Import dialog's OK would."""
        if app.project is None and not await self._startScratchCase(
                files[0].stem):
            return
        try:
            await self._stepManager.importGeometryFiles(files)
        except (OSError, RuntimeError, ValueError) as error:
            await AsyncMessageBox().warning(
                self, self.tr('Geometry import error'), str(error))

    def closeEvent(self, event):
        if not self._readyToQuit:
            self._closeTriggered.emit(True)
            event.ignore()
            return

        # DP-2100.  A second close of a window that has already been taken down
        # (a test, or a shutdown path that closes twice) must not walk the
        # dock layout again: its floating containers are queued for deletion
        # and reading their screen is an access violation.
        if getattr(self, '_teardownDone', False):
            super().closeEvent(event)
            return
        self._teardownDone = True

        app.settings.updateLastMainWindowGeometry(self.geometry())
        # Hidden panes report no width; put them back before the split is read.
        self._ui.regionCBandController.leave_focus()
        for unsubscribe in getattr(self, '_bandUnsubscribes', ()):
            unsubscribe()
        self._bandUnsubscribes = []
        if getattr(self, '_jobAttention', None) is not None:
            self._jobAttention.unsubscribe()
        sizes = (
            self._ui.regionSplitter.sizes()
            if layout_worth_recording(
                self._regionSplitterUserMoved, self._savedRegionSizes)
            else None)
        getattr(app.settings, 'updateThreeRegionLayout', lambda **_kw: None)(
            sizes=sizes, active_output=self._outputTabs.key_for_current())
        getattr(app.settings, 'updateDockLayout', lambda _r: None)(
            self._dockLayout.snapshot())
        self._dockLayout.dispose()
        if self._jobStateUnsubscribe is not None:
            self._jobStateUnsubscribe()
            self._jobStateUnsubscribe = None
        if self._stepLockUnsubscribe is not None:
            self._stepLockUnsubscribe()
            self._stepLockUnsubscribe = None
        if self._capabilityStateUnsubscribe is not None:
            self._capabilityStateUnsubscribe()
            self._capabilityStateUnsubscribe = None
        self._jobProgress.shutdown()
        if self._wslHealthBar is not None:
            self._wslHealthBar.dispose()
            self._wslHealthBar = None
        client = getattr(app, 'facadeClient', None)
        if isinstance(getattr(client, 'retry_prompt', None), RetryPrompt):
            client.retry_prompt = None
        # A finished export window nobody dismissed stayed on screen after
        # this window had closed (the guided walks, 2026-10-11).
        closeProgressWindowsOf(self)
        # DP-2745. Nothing draws presentation ops once the window is gone.
        setRenderer = getattr(app, 'setPresentationRenderer', None)
        if callable(setRenderer):
            setRenderer(None)

        super().closeEvent(event)

    def _applyRegionSizes(self, total_width: int | None = None):
        """Re-apply the A/B/C split, asking the outline what width it needs.

        DP-134. A drag of the handle still wins: once the user has moved it,
        the current sizes are what get re-applied and the outline's request is
        only a starting point it no longer gets to change.
        """
        if not hasattr(self, '_threeRegionShell'):
            return
        band = getattr(self._ui, 'regionCBandController', None)
        if band is not None and band.in_focus_mode:
            return
        if total_width is None:
            total_width = max(self.width(), 1280)
        sizes = self._ui.regionSplitter.sizes()
        saved = (sizes if self._regionSplitterUserMoved and len(sizes) == 3
                 else self._savedRegionSizes)
        navigation = getattr(self, '_navigationView', None)
        # Plan 43 LY2. The outline dock is retired, so no pane waits for room.
        wanted = (navigation.wantedPaneWidth() if navigation is not None
                  and not self._dockLayout.isRetired(OUTLINE_DOCK) else None)
        if (saved is not None and wanted and not self._regionSplitterUserMoved
                and len(saved) == 3 and saved[0] < wanted):
            # Plan 40 UXB4 (#8). A width restored from an earlier session is
            # not one dragged in this one: MEASURED, a saved 190 px kept
            # "4. Domai... region" clipped at 1280 px for ever. The outline's
            # own width is the floor; the settings column gives it up.
            give = wanted - saved[0]
            saved = [wanted, max(saved[1] - give, 0), saved[2]]
        page_column = getattr(self._ui, 'regionBPageColumn', None)
        b_wanted = page_column.wantedWidth() if page_column is not None else None
        sheet = getattr(self, '_settingsSheet', None)
        # Plan 45 I. The sheet sets the three widths itself (it owns the
        # middle one), so the shell only measures when there is a sheet.
        sizes = self._threeRegionShell.apply_sizes(
            total_width, saved, wanted, b_wanted, commit=sheet is None)
        if sheet is not None:
            sheet.apply_to_sizes(sizes)

    def resizeEvent(self, event):
        """Clamp the permanent regions while leaving user-adjusted A/B intact."""
        if not hasattr(self, '_threeRegionShell'):
            super().resizeEvent(event)
            return
        self._applyRegionSizes(event.size().width())
        self._ui.regionAHost.setProperty(
            'compactMode', event.size().width() < 1000)
        super().resizeEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self._ui.retranslateUi(self)
            self._stepManager.retranslatePages()
            self._updateMenuStates()
        elif event.type() == QEvent.Type.WindowStateChange:
            self._syncSizeGrip()

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
        # DP-755. Ctrl+W and Ctrl+Y are what other applications use; the
        # keys this window had before stay, so no one's habit breaks.
        self._ui.actionClose.setShortcuts([QKeySequence('Ctrl+W'), QKeySequence('Ctrl+E')])
        self._ui.actionExit.setShortcut('Ctrl+Q')
        self._ui.actionUndo.setShortcut('Ctrl+Z')
        self._ui.actionRedo.setShortcuts([QKeySequence('Ctrl+Shift+Z'), QKeySequence('Ctrl+Y')])
        # The viewport owns Ctrl+Shift+F. The menu entry shows that key so it
        # can be found, and is scoped to the menu so it is never a second
        # window-wide binding of it.
        self._ui.actionViewZoomSelection.setShortcut(QKeySequence(RenderingTool.ZOOM_SELECTION_SHORTCUT))
        self._ui.actionViewZoomSelection.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        # DP-759. The key every desktop platform uses for its preferences.
        self._ui.actionPreferences.setShortcut(QKeySequence('Ctrl+,'))
        # Plan 40 UXE1. Every key above belongs to the shortcut registry, which
        # applies the user's own keys over these defaults.
        for command_id, action in (
                ('file.new', self._ui.actionNew),
                ('file.open', self._ui.actionOpen),
                ('file.save', self._ui.actionSave),
                ('file.saveAs', self._ui.actionSaveProjectAs),
                ('file.close', self._ui.actionClose),
                ('file.exit', self._ui.actionExit),
                ('edit.undo', self._ui.actionUndo),
                ('edit.redo', self._ui.actionRedo),
                ('viewport.zoomSelection', self._ui.actionViewZoomSelection),
                ('file.preferences', self._ui.actionPreferences)):
            adoptShortcut(command_id, action)

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
            self._ui.rotationCenter: self.tr('Set rotation centre'),
        }
        for widget, name in names.items():
            widget.setAccessibleName(name)
            if not widget.toolTip():
                widget.setToolTip(name)

    def _connectSignalsSlots(self):
        app.renderingToggled.connect(self._setRenderingEnabled)

        # Plan 40 UXD2 (F7a). Ctrl+K finds any action, step or setting by name.
        from foammesh.view.main_window.command_palette import (
            install as installCommandPalette)
        self._commandPalette = installCommandPalette(self)
        # Plan 40 UXE1. The palette's key and the Gmsh route's keys are made
        # elsewhere; they are listed and rebindable all the same.
        palette_key = getattr(self._commandPalette, '_shortcut', None)
        if palette_key is not None:
            adoptShortcut('view.palette', palette_key)
        SHORTCUTS.sweep(self)
        QApplication.instance().focusChanged.connect(self._sweepShortcuts)

        self._consoleAction = self._ui.menuView.addAction(self.tr('&Console'))
        self._consoleAction.setShortcut('Ctrl+Shift+C')
        adoptShortcut('view.console', self._consoleAction)
        self._consoleAction.triggered.connect(
            lambda: self.showOutputTab('console'))
        self._installViewportEffectActions()
        self._buildColourScaleMenu(app)

        self._recentFilesMenu.projectSelected.connect(self._openRecent)
        self._recentFilesMenu.clearRequested.connect(self._clearRecents)
        self._installWelcomePanel()

        self._stepManager.workingStepChanged.connect(self._displayControl.openedStepChanged)
        self._stepManager.displayStepChanged.connect(self._displayControl.currentStepChanged)
        self._stepManager.displayStepChanged.connect(self._dropScaleNoteOffItsPage)

        self._stepManager.batchStarted.connect(self._disableMenubar)
        self._stepManager.batchStopped.connect(self._enableMenubar)
        # DP-5711. Moving to another row saves the case.
        self._stepManager.routeEntered.connect(self._saveAtStepBoundary)

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
        # DP-5711. Every policy action carries its reason as a tooltip, and
        # QMenu shows none unless told to: a greyed Save said nothing.
        for menu_name in ('menuFile', 'menuEdit', 'menuMesh'):
            menu = getattr(self._ui, menu_name, None)
            if hasattr(menu, 'setToolTipsVisible'):
                menu.setToolTipsVisible(True)
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
            ActionId.MESH_INFO: self.showMeshInfo,
            ActionId.MESH_SCALE: lambda: self._runMeshTransformV13('scale'),
            ActionId.MESH_TRANSLATE: lambda: self._runMeshTransformV13('translate'),
            ActionId.MESH_ROTATE: lambda: self._runMeshTransformV13('rotate'),
            ActionId.MESH_QUALITY: self._actionParameters,
            ActionId.MESH_CHECK: self._runMeshCheckDashboard,
            ActionId.MESH_REPAIR: self._runMeshRepair,
            ActionId.MESH_RESTORE: self._restorePreviousMesh,
            ActionId.VIEW_FIT: lambda: self._ui.fit.click(),
            ActionId.VIEW_ZOOM_SELECTION: self._zoomToSelection,
            ActionId.VIEW_AXIS: lambda: self._ui.axis.click(),
            ActionId.VIEW_CUBE_AXIS: lambda: self._ui.cubeAxis.click(),
            ActionId.VIEW_RULER: lambda: self._ui.ruler.click(),
            ActionId.VIEW_PARALLEL_PROJECTION: lambda: self._ui.perspective.click(),
            ActionId.VIEW_ALIGN_AXIS: lambda: self._ui.alignAxis.click(),
            ActionId.VIEW_ROLL: lambda: self._ui.rotate.click(),
            ActionId.VIEW_ROTATION_CENTER: lambda: self._ui.rotationCenter.click(),
            ActionId.PREFERENCES: self._openPreferences,
            ActionId.SETTINGS_MESH_QUALITY: self._actionParameters,
            ActionId.START_WINDOW: self._openStartWindow,
            ActionId.TERMINAL_HERE: self._openTerminalHere,
            ActionId.TUTORIALS: self._openTutorials,
            ActionId.LICENSE: self._openLicense,
            ActionId.ABOUT: self._actionAbout,
            ActionId.RUN_DETAILS: self.showRunDetails,
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
                (ActionId.VIEW_PARALLEL_PROJECTION, self._ui.perspective),
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
        reset the quality strip and the Mesh quality tab to their dormant
        no-mesh lines -- two different sentences then, one since DP-107 --
        while the mesh from
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
        global_capabilities = frozenset()
        if app.project is None:
            return AppSnapshot(capabilities=global_capabilities)
        content = ContentState.EMPTY
        try:
            # The engine is asked in the words it answers in. Both probes
            # this line used to hold -- `classify_case(...).has_mesh` and
            # `_hasMeshOnDisk()` -- look for `constant/polyMesh` and nothing
            # else, and Plan 28 makes a Gmsh run targeting SU2 skip the
            # polyMesh publication on purpose. MEASURED on the 16 September
            # elbow campaign: 21,952 cells in the viewport and `No mesh has
            # been produced in this case yet` on the quality page, because
            # this answer was `False` and `refreshMeshVerdict` clears both
            # quality surfaces unless it is `True`.
            from foammesh.core.facade.mesh_presence import has_engine_mesh
            has_mesh = (has_engine_mesh(app.project.path,
                                        configured_engine(app))
                        or self._hasMeshOnDisk())
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
            ActionId.MESH_INFO.value,
            ActionId.PREFERENCES.value,
            ActionId.START_WINDOW.value,
            ActionId.LOAD_MESH.value,
            ActionId.SAVE_PROJECT_AS.value,
            ActionId.LOAD_GEOMETRY.value,
        })
        capability_reasons = {}
        # DP-752. The thresholds are snappy's; a Gmsh case is told why not.
        quality_available, quality_reason = mesh_quality_capability(
            configured_engine(app))
        if quality_available:
            capabilities = capabilities | frozenset({
                ActionId.MESH_QUALITY.value,
                ActionId.SETTINGS_MESH_QUALITY.value})
        else:
            capability_reasons[ActionId.MESH_QUALITY.value] = quality_reason
            capability_reasons[
                ActionId.SETTINGS_MESH_QUALITY.value] = quality_reason
        terminal_available, terminal_reason = terminal_capability()
        if terminal_available:
            capabilities = capabilities | frozenset({ActionId.TERMINAL_HERE.value})
        else:
            capability_reasons[ActionId.TERMINAL_HERE.value] = terminal_reason
        check_mesh = self._capability('checkMesh')
        if check_mesh.available:
            capabilities = capabilities | frozenset({ActionId.MESH_CHECK.value})
        else:
            capability_reasons[ActionId.MESH_CHECK.value] = check_mesh.reason
        transform_points = self._capability('transformPoints')
        if transform_points.available:
            capabilities = capabilities | frozenset({
                ActionId.MESH_SCALE.value, ActionId.MESH_TRANSLATE.value,
                ActionId.MESH_ROTATE.value,
            })
        else:
            for action_id in (ActionId.MESH_SCALE, ActionId.MESH_TRANSLATE, ActionId.MESH_ROTATE):
                capability_reasons[action_id.value] = transform_points.reason
        if (self._repairUtilities(self._capability)
                or content in (ContentState.GEOMETRY, ContentState.GEOMETRY_AND_MESH)):
            capabilities = capabilities | frozenset({ActionId.MESH_REPAIR.value})
        else:
            capability_reasons[ActionId.MESH_REPAIR.value] = (
                check_mesh.reason if check_mesh.transient else
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
            job=MainWindow._menuJobState(self),
            undo_available=history['can_undo'],
            redo_available=history['can_redo'],
            undo_label=history['undo_label'],
            redo_label=history['redo_label'],
            rendering_available=self._meshManager is not None,
            capabilities=capabilities,
            capability_reasons=capability_reasons,
        )

    def _menuJobState(self):
        """The job state the menus are judged by.

        DP-5711. A batch (Run to end) is one run of several jobs, and between
        two of them the job manager is briefly idle. The batch is writing the
        case the whole time, so the menus treat it as one running mutation:
        Save stays off for all of it and says why, rather than flickering on
        between stages.
        """
        state = job_state_from_manager(app.jobManager)
        if getattr(self, '_batchLocked', False) and state in (
                JobState.IDLE, JobState.RUNNING_READ):
            return JobState.RUNNING_MUTATION
        return state

    @staticmethod
    def _repairUtilities(lookup=None):
        """Return only utility paths that were positively discovered.

        `lookup` defaults to the registry call that answers whatever it
        costs, which is what running a repair wants. Rebuilding a menu
        passes the non-blocking one instead."""
        probe = app.capabilities.utility if lookup is None else lookup
        return {
            operation.utility_name: capability.executable
            for operation in RepairOperation
            if (capability := probe(operation.utility_name)).available
            and capability.executable is not None
        }

    def _capability(self, name):
        """Report what is known about a utility without ever waiting for it.

        R194's sibling on this side of the shell. `app.capabilities.utility()`
        may start a cold WSL runtime to answer, and every path that rebuilds
        the menus called it on the GUI thread -- so opening a project drew the
        window, then froze it for the length of the probe. Here a cached answer
        is used when there is one, and when there is not the action is reported
        unavailable *for now*: transient, so nothing downstream mistakes the
        placeholder for a verdict, and carrying a reason that says the
        environment is being checked rather than that the utility is missing.
        The probe fills the cache for every utility the menus asked about --
        not one chosen name. `CapabilityRegistry` caches per name, so warming
        `checkMesh` leaves `transformPoints` unknown, and a snapshot that asks
        about both warms again on every rebuild. MEASURED as a second 100% CPU
        spin, the same shape as DP-116 and reached by the opposite route: a
        probe that *succeeded*.
        """
        known = app.capabilities.utility_if_known(name)
        if known is not None:
            return known
        failure = self._capabilityProbeFailure
        if failure is not None:
            # The probe already ran and could not reach the runtime. R194
            # leaves that answer uncached on purpose, so `utility_if_known`
            # says `None` again every time it is asked -- and warming again
            # here arms the refresh that asked, which asks again. MEASURED as
            # a permanent 100% CPU spin with the window still drawn and no
            # further output. Report what the probe actually found, and ask
            # again only when something re-probes.
            return Capability(name, False, None, failure, transient=True)
        self._warmCapabilities(name)
        return Capability(
            name, False, None,
            self.tr('Checking the OpenFOAM environment…'), transient=True)

    def _warmCapabilities(self, name):
        """Probe off the GUI thread, every name the menus have asked about."""
        self._capabilityWanted.add(name)
        if self._capabilityWarmPending:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop yet means no window to refresh either.
            return
        self._capabilityWarmPending = True

        async def warm():
            failure = None
            try:
                # Drained rather than iterated: a name the menus ask about
                # while this probe is out belongs to this probe too, and the
                # first answer warms the runtime the rest of them share.
                while self._capabilityWanted:
                    wanted = self._capabilityWanted.pop()
                    try:
                        probed = await asyncio.to_thread(
                            app.capabilities.utility, wanted)
                    except Exception as error:               # noqa: BLE001
                        probed = Capability(
                            wanted, False, None, str(error), transient=True)
                    if probed.transient:
                        # Nothing reached the runtime, so the names still
                        # waiting would each fail the same way, slowly.
                        failure = probed.reason
                        break
            finally:
                self._capabilityWarmPending = False
            # Remember what the probe could not answer, so that the refresh
            # below does not send the next snapshot straight back here.
            self._capabilityProbeFailure = failure
            try:
                self._scheduleMenuRefresh()
            except RuntimeError:
                # The window closed while the probe was out.
                pass

        asyncio.ensure_future(warm())

    def _capabilitiesReprobed(self, *_args, **_kwargs):
        """A re-probe is the one thing that makes a failed probe worth redoing."""
        self._capabilityProbeFailure = None
        self._scheduleMenuRefresh()

    def _scheduleMenuRefresh(self, *_args, **_kwargs):
        """Coalesce noisy state notifications into one next-turn refresh."""
        if self._menuRefreshPending:
            return
        self._menuRefreshPending = True
        QTimer.singleShot(0, self._flushMenuRefresh)

    def _flushMenuRefresh(self):
        self._menuRefreshPending = False
        self._updateMenuStates()

    def _geometryFromApi(self, **_payload):
        """Queue adoption of geometry an API command put in the store."""
        try:
            source = app.facadeClient.session().event_source()
        except (AttributeError, RuntimeError):
            return
        if source != 'rest' or self._apiGeometryPending:
            return
        self._apiGeometryPending = True
        QTimer.singleShot(0, self._adoptApiGeometry)

    # -- DP-2851..2853: work done over the local API, shown as the window's --

    #: The runs whose mesh the window draws when they end.
    API_RUN_OPERATIONS = frozenset({'workflow.run_pipeline', 'mesh.gmsh.run'})

    def _apiJobOutput(self, **payload):
        """DP-2853. A local API job's output, in the Console.

        The window's own runs subscribe the Console to their output while
        they run; a run started over the API had nobody listening, so the
        Console stayed empty beside a running mesher. Only output raised by
        a REST command is taken here, so the window's own runs are not
        printed twice; the session actions print their own (session_activity).
        """
        if payload.get('session_action'):
            return
        try:
            source = app.facadeClient.session().event_source()
        except (AttributeError, RuntimeError):
            return
        if source != 'rest':
            return
        line = payload.get('line', '')
        if line:
            MainWindow._appendConsole(self, str(line))

    @staticmethod
    def importSummary(payload: dict) -> str:
        """DP-2853. The Console line an import leaves, from its own result."""
        name = Path(str(payload.get('source') or payload.get('name') or
                        'geometry')).name
        parts = []
        cells = payload.get('cells')
        if isinstance(cells, int):
            parts.append(f'{cells:,} triangles')
        patches = payload.get('patches')
        if isinstance(patches, list) and patches:
            parts.append(count_text(len(patches), 'patch', 'patches'))
        unit = payload.get('source_unit')
        if unit:
            parts.append(f'unit {unit}')
        line = f'Imported {name}'
        if parts:
            line += ': ' + ', '.join(parts)
        warnings = payload.get('diagnostic_warnings') or ()
        lines = [line + '.']
        lines.extend(f'    {warning}' for warning in warnings)
        return '\n'.join(lines)

    def _commandEnded(self, command, result):
        """DP-2851/DP-2853. A command ended: say and show what it made.

        Called by the session after every command, from the window's own
        loop. An import is written to the Console whoever sent it. A run
        sent over the local API is drawn the way the window draws its own:
        the verdict, then the run's own mesh -- until now the window never
        heard the run end, and Volume, Worst cells and the rest were refused
        over a mesh on disk.
        """
        operation = getattr(command, 'operation', '')
        status = getattr(result, 'status', '')
        payload = getattr(result, 'payload', None)
        payload = payload if isinstance(payload, dict) else {}
        if operation == 'geometry.import' and status == 'accepted':
            MainWindow._appendConsole(self, MainWindow.importSummary(payload))
        source = getattr(getattr(command, 'source', None), 'value',
                         getattr(command, 'source', None))
        if source != 'rest' or operation not in self.API_RUN_OPERATIONS:
            return
        stepManager = getattr(self, '_stepManager', None)
        show = getattr(stepManager, 'showApiRunResult', None)
        if show is None:
            return

        async def draw():
            # The draw is the window's own work, not the API command's: its
            # task inherited the command's event source, and clears it.
            try:
                app.facadeClient.session().clear_event_context()
            except (AttributeError, RuntimeError):
                pass
            await show(result)

        self._trackMeshDraw(draw())

    def _trackMeshDraw(self, coroutine):
        """Run a mesh draw, kept until it ends so `meshSettled` can wait."""
        task = asyncio.ensure_future(coroutine)
        self._meshDraws.add(task)

        def ended(done):
            self._meshDraws.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error('could not draw the mesh',
                             exc_info=done.exception())
        task.add_done_callback(ended)
        return task

    async def meshSettled(self):
        """DP-2851/DP-2852. Return once the mesh on its way is on screen.

        The draw of an API run's result or of an opened case's stored mesh,
        and any read the viewport has under way. A read that waits for a job
        still writing the mesh is not waited for: that mesh is not done.
        """
        while self._meshDraws:
            await asyncio.wait(set(self._meshDraws))
        manager = getattr(self, '_meshManager', None)
        settled = getattr(manager, 'loadsSettled', None)
        if settled is None:
            return
        writing = getattr(manager, '_writerActive', None)
        if callable(writing) and writing():
            return
        await settled()

    def _adoptApiGeometry(self):
        self._apiGeometryPending = False

        async def adopt():
            try:
                await self._stepManager.adoptStoreGeometry()
            except Exception:                            # noqa: BLE001
                logger.exception('could not show geometry imported over the API')

        asyncio.ensure_future(adopt())

    def _scheduleProjectRefresh(self, *_args, **kwargs):
        """Coalesce artifact/history events into one truthful scene refresh."""
        self._scheduleMenuRefresh()
        self._noteRefreshCause(kwargs)
        if self._projectRefreshPending:
            return
        self._projectRefreshPending = True
        QTimer.singleShot(0, self._flushProjectRefresh)

    def _noteRefreshCause(self, payload):
        """Remember what changed, not only that something did (DP-397).

        The publisher names the changed row three times over -- in the
        transaction's target, in the geometry event's `geometry_id`, and in
        the operation's result -- and this subscriber used to discard all
        three and rebuild the whole scene. On a 129-face import that cost a
        minute per rename.

        Anything that cannot be read as a rename widens the refresh back to
        the full rebuild, and the window stays wide until it is flushed: a
        rename that lands in the same event-loop turn as an import must not
        narrow the import's refresh.
        """
        if self._projectRefreshWide:
            return
        renamed = self._renamedGeometry(payload)
        if renamed is None:
            self._projectRefreshWide = True
            self._projectRefreshRenames = []
            return
        self._projectRefreshRenames.append(renamed)

    @staticmethod
    def _renamedGeometry(payload):
        """The (id, new name) a payload describes, or None if it describes more.

        Two events carry one rename -- `geometry.rename` publishes
        ARTIFACT_GEOMETRY_CHANGED and the commit publishes
        TRANSACTION_APPLIED -- so both shapes are read here, and a turn that
        holds both stays narrow. Read conservatively: a payload that is not
        exactly this returns None and pays for the rebuild.
        """
        if not isinstance(payload, dict):
            return None
        if str(payload.get('operation') or '') == 'geometry.rename':
            geometry_id = payload.get('geometry_id')
            after = payload.get('after')
            if geometry_id is not None and after is not None:
                return (str(geometry_id), str(after))
            return None
        transaction = payload.get('transaction')
        if transaction is None:
            return None
        if getattr(transaction, 'action', None) != 'rename geometry':
            return None
        target = getattr(transaction, 'target', None)
        if not isinstance(target, str):
            return None
        parts = target.split('/')
        if len(parts) != 3 or parts[0] != 'geometry' or parts[2] != 'name':
            return None
        # `reason` is 'previous -> new', which is where the committed name is.
        after = str(getattr(transaction, 'reason', '') or '')
        if '->' not in after:
            return None
        after = after.split('->')[-1].strip()
        return (parts[1], after) if after else None

    def _scheduleProjectReload(self, *_args, **_kwargs):
        """A refresh that must repaint the forms too (DP-363).

        Undo, redo and a restored artifact replace the stored values under
        the user, so the page they are looking at has to show what is
        stored now. Every other scene event is a consequence of a write
        and must leave an unapplied edit where the user typed it.
        """
        self._projectRefreshRepaintsForms = True
        self._scheduleProjectRefresh()

    def _flushProjectRefresh(self):
        self._projectRefreshPending = False
        repaintsForms = self._projectRefreshRepaintsForms
        self._projectRefreshRepaintsForms = False
        wide = self._projectRefreshWide or not self._projectRefreshRenames
        renames = self._projectRefreshRenames
        self._projectRefreshWide = False
        self._projectRefreshRenames = []
        if self._geometryManager is not None:
            if wide:
                self._geometryManager.load()
            else:
                # DP-397. A rename moves one label and touches nothing about
                # any geometry, so the other actors are already right. Moving
                # the label is O(1); rebuilding the scene to show it was the
                # whole of the cost.
                for geometry_id, name in renames:
                    self._geometryManager.renameGeometry(geometry_id, name)
                self._geometryManager.applyToDisplay()
        self._stepManager.load(preserveCurrentPage=not repaintsForms)
        if self._meshManager is not None:
            asyncio.create_task(self._meshManager.reload())
        self.refreshMeshVerdictSoon()

    # -- WP3: the verdict strip and its tab ------------------------------- #

    def _meshVerdictSuperseded(self, *_args, **_kwargs):
        """The configuration changed after this mesh was produced."""
        self._verdictStrip.mark_stale()

    def _wireQualityInspector(self) -> None:
        """Plan 39 VI9. The Quality inspector and the colouring it drives.

        One metric, one limit, one picture: the tab's controls and the
        controller that paints share them, and a section coloured by the same
        metric is repainted on the same band.
        """
        tab = self._meshQualityTab
        info = self._displayControl.meshQualityInfo()
        info.bindInspector(tab)
        # The histogram redraws once a drag has settled; the line itself and
        # the field follow the hand at once.
        self._inspectorTimer = QTimer(self)
        self._inspectorTimer.setSingleShot(True)
        self._inspectorTimer.setInterval(120)
        self._inspectorTimer.timeout.connect(self._inspectorRefresh)

        info.availabilityChanged.connect(self._inspectorAvailable)
        info.limitChanged.connect(self._inspectorLimitHeld)
        info.elementInspected.connect(tab.show_inspection)
        tab.pickElementToggled.connect(info.setPickingElements)
        info.highlightingChanged.connect(
            lambda _on: tab.set_show_beyond(info.isShowingBeyondLimit()))
        info.scaleChanged.connect(self._recolourSectionForInspector)
        tab.metricChanged.connect(self._inspectorMetricChanged)
        tab.limitChanged.connect(self._inspectorLimitEdited)
        tab.limitSourceRequested.connect(self._inspectorLimitSource)
        tab.showBeyondToggled.connect(self._inspectorShowBeyond)
        tab.measureRequested.connect(
            lambda _metric: info.refreshDistribution(tab))
        self._inspectorAvailable(info.isVisible())
        self._inspectorShowLimit()

    def _inspectorShowLimit(self) -> None:
        """Put the active metric's limit and its sources in the tab."""
        info = self._displayControl.meshQualityInfo()
        index = info.activeIndex()
        self._meshQualityTab.set_limit(info.limitFor(index))
        self._meshQualityTab.set_limit_sources(
            info.mesherLimitFor(index), info.checkLimitFor(index))

    def _inspectorAvailable(self, available: bool) -> None:
        self._meshQualityTab.set_inspector_available(bool(available))
        MainWindow.refreshElementSelections(self)
        if available:
            self._inspectorShowLimit()

    def _inspectorMetricChanged(self, name: str) -> None:
        from foammesh.rendering.actor_info import MeshQualityIndex

        info = self._displayControl.meshQualityInfo()
        info.setWorstChoice(MeshQualityIndex(name))
        self._inspectorShowLimit()
        # Only what is already measured: a metric change never starts a pass.
        info.refreshDistribution(self._meshQualityTab, mayCompute=False)
        self._meshQualityTab.set_beyond_count(
            info.beyondCount(), info.limitFor(info.activeIndex()))

    def _inspectorLimitHeld(self, metric: str, value) -> None:
        info = self._displayControl.meshQualityInfo()
        if metric == info.activeIndex().value:
            self._meshQualityTab.set_limit(value)

    def _inspectorLimitEdited(self, value) -> None:
        info = self._displayControl.meshQualityInfo()
        info.setLimit(info.activeIndex(), value)
        self._inspectorTimer.start()

    def _inspectorRefresh(self) -> None:
        """Redraw the counts and, if the cells beyond are shown, repaint."""
        info = self._displayControl.meshQualityInfo()
        if not info.isVisible():
            return
        info.refreshDistribution(self._meshQualityTab, mayCompute=False)
        self._meshQualityTab.set_beyond_count(
            info.beyondCount(), info.limitFor(info.activeIndex()))
        if info.isShowingBeyondLimit():
            info.highlightWorst(mode='limit')

    def _inspectorLimitSource(self, source: str) -> None:
        info = self._displayControl.meshQualityInfo()
        if info.useLimit(info.activeIndex(), source) is None:
            self.statusBar().showMessage(
                self.tr('There is no {0} limit for this metric.').format(
                    self.tr('mesher') if source == 'mesher'
                    else self.tr('check')), 6000)
            return
        self._inspectorRefresh()

    def _inspectorShowBeyond(self, on: bool) -> None:
        info = self._displayControl.meshQualityInfo()
        if not info.showBeyondLimit(bool(on)) and on:
            self._meshQualityTab.set_show_beyond(False)

    def _recolourSectionForInspector(self) -> None:
        """A section coloured by the metric the inspector paints follows it."""
        cut = self._displayControl.cutTool()
        key = cut.sectionColour()
        if str(key).startswith('quality.'):
            cut.setSectionColour(key)

    def _focusQualityElement(self, element: dict) -> None:
        """Answer a click on an offending element in the Quality tab.

        Plan 39 VI8 (F5b). The camera goes to the element: it dollies to the
        centroid at twenty cell diameters and outlines the cell, and Back
        returns to where the user was. The status bar names the element and
        where it is.
        """
        centroid = element.get('centroid') or ()
        views = getattr(self, '_qualityViews', None)
        if views is not None:
            views.select(element)
        # DP-165. The same point the quality table shows, to the same
        # precision, and to one precision across its three axes.
        where = ', '.join(aligned(centroid))
        went = False
        try:
            went = self._displayControl.meshQualityInfo().flyToElement(element)
        except Exception as error:  # noqa: BLE001 - the viewport must not take the tab down
            logger.warning('could not go to the selected element: %s', error)
        self.statusBar().showMessage(
            self.tr('Element {0} at ({1}).').format(element.get('tag', ''), where)
            + ('' if went else self.tr(
                '  The viewport could not be moved there.')),
            8000)

    def showMeshVerdict(self, verdict: dict) -> None:
        """Publish a fresh verdict to the strip and the Quality tab.

        Called when a run finishes. Raising Quality is deliberate and bounded:
        the console is what a user wants *during* a run and the quality report
        is what they want *after*, but a tab the user picked themselves is
        never taken away from them mid-read.
        """
        verdict = with_layer_coverage(verdict)
        self._verdictStrip.show_verdict(verdict)
        self._meshQualityTab.show_verdict(verdict)
        self._refreshQualityActions()
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
        """Raise the Mesh quality tab *and* make it big enough to read.

        R62/R114. Raising the tab was the whole of `Details`, and the band it
        raised into was about 40 px tall: the headline clipped to one line and
        the Metrics and Offending-elements tables showed their headers with no
        room for a single row, so the report read as two empty tables under a
        sentence. Region C's own splitter is the thing that has to move, and
        the user should not have to find its handle to see the report the app
        is asking them to accept.
        """
        self.openOutputBand()
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
        sat on its dormant no-mesh line over a mesh that was on screen.

        One implementation, one scheduler, and every entry point uses one of
        the two rather than guessing which calling convention applies.
        """
        asyncio.create_task(self.refreshMeshVerdict())

    async def refreshMeshVerdict(self) -> None:
        """Publish whatever quality is already known about the loaded mesh.

        The quality surfaces used to be filled by exactly one path -- accepting
        a Gmsh gate refusal -- so opening a case whose mesh had already been
        checked left the strip on its dormant no-mesh line above a mesh that was
        drawn on screen and a stored report that said `warning`. `quality.report`
        is a READ of that persisted report, so this costs nothing and is the
        difference between a surface that is empty and one that is honest.
        """
        if app.project is None or not hasattr(self, '_outputTabs'):
            return
        # "Not checked" is only honest about a mesh that exists. Said about an
        # empty case it invites the user to run checkMesh on nothing, so the
        # dormant no-mesh line stands until there is something to measure.
        if not self._actionSnapshot().has_mesh:
            self.clearMeshVerdict()
            return

        # The strip's dormant state means "there is no mesh". Once a mesh
        # exists it must never read that way again, whatever happens below --
        # a mesh drawn on screen under a line saying no mesh exists is the
        # plainest kind of lying surface. Every failure path therefore lands on
        # the `unrated` verdict, which names the command that would produce a
        # real one, rather than on a bare `return`.
        report = None
        gate = None
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
            # Plan 41 GN14 (F5d). The Gmsh element gate's own stored report,
            # whose offending rows a checkMesh verdict cannot carry.
            gate = (result.payload or {}).get('gate') or None
        except Exception:
            # A case with no stored report is the common case, not an error,
            # and the operation signals it differently depending on how the
            # case was opened. None of those variants may leave the strip
            # dormant, so the class of failure does not change what is shown.
            logger.debug('no readable quality report for this case',
                         exc_info=True)

        published = self._verdictStrip.verdict or {}
        if (report is None and not gate
                and published.get('verdict') not in (None, 'unrated')):
            # A real verdict is already on screen for this mesh; a failed
            # re-read must not downgrade it to "not checked".
            return

        verdict = with_layer_coverage(
            apply_waivers(verdict_with_gate(report, gate), waivers))
        self._verdictStrip.show_verdict(verdict)
        self._meshQualityTab.show_verdict(verdict)
        if verdict.get('stale'):
            self._verdictStrip.mark_stale(self.tr(
                'This check was run against a different mesh'))
        # DP-2722. A case opened with its check's sets on disk: the button
        # acts on them, as it does on the sets a check run here wrote.
        if (report is not None and not verdict.get('stale')
                and not getattr(self, '_lastFailedCellSets', None)
                and MainWindow._caseMeshOnScreen(self)):
            path = app.project.path
            sets = await asyncio.get_running_loop().run_in_executor(
                None, lambda: sets_of_this_mesh(path))
            if (sets and app.project is not None and app.project.path == path
                    and not getattr(self, '_lastFailedCellSets', None)):
                self._lastFailedCellSets = sets
                self._failedCellSetIndex = -1
        # Plan 41 GN23. The failed-cells button follows the gate's offending
        # elements on a native mesh, so it follows the verdict.
        self._refreshQualityActions()

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
        confirmed = messages.question(
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
            engine_from_listing(engines),
            target_solver=engines.get('target_solver'))
        # Plan 31 CP-05 item 4. The Gmsh route accepts a *candidate*, so it
        # has to be told which one. The verdict on screen names it; a verdict
        # that predates this field (a reopened case) is matched to the newest
        # judged run rather than being turned into a fresh mesh.
        parameters = {'accept_quality': True, 'accept_reason': reason}
        if operation == 'mesh.run.accept':
            run_id = run_id or await self._candidateRunId()
            if not run_id:
                # Plan 37 UF20 follow-up: every box a coroutine here opens
                # is opened with no task current (`outside_task`), so its
                # nested loop does not refuse -- and drop -- the tasks it
                # steps.
                await modal(
                    messages.warning,
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
            await modal(messages.warning,
                        self, self.tr('Not accepted'), str(error))
            return
        finally:
            if unsubscribe is not None:
                unsubscribe()
        payload = getattr(result, 'payload', {}) or {}
        if getattr(result, 'status', '') != 'accepted':
            await modal(
                messages.warning,
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
        holdBatchLock(self._ui, getattr(self, '_batchLocked', False))
        self._updateWindowTitle()
        view = getattr(self, '_runDetailsView', None)
        if view is not None and view.isVisible():
            self._refreshHistoryPanel()

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
        label = app.facadeClient.history_status()['undo_label']

        def undone(result):
            if (getattr(result, 'payload', None) or {}).get('undone'):
                self.statusBar().showMessage(
                    self.tr('Undid {0}.').format(label) if label
                    else self.tr('Undid the last step.'), 3000)
            self._updateMenuStates()

        submit(app.facadeClient, 'history.undo', {}, then=undone)

    def _redo(self):
        # C31-12. See `_undo`.
        label = app.facadeClient.history_status()['redo_label']

        def redone(result):
            if (getattr(result, 'payload', None) or {}).get('redone'):
                self.statusBar().showMessage(
                    self.tr('Redid {0}.').format(label) if label
                    else self.tr('Redid the last step.'), 3000)
            self._updateMenuStates()

        submit(app.facadeClient, 'history.redo', {}, then=redone)

    def _historyText(self) -> str:
        """The case's state and artifact history, one line per entry (DP-753)."""
        if app.project is None:
            return self.tr('Open a case to see its history.')
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
                    fingerprint = '  {0} → {1}'.format(
                        (item.get('before_fingerprint') or 'none')[:10],
                        (item.get('after_fingerprint') or 'none')[:10])
                lines.append('{0}  Artifact  {1}  [{2}; recovery: {3}]{4}'.format(
                    timestamp, item.get('operation', ''), item.get('status', ''),
                    recovery, fingerprint))
            else:
                lines.append('{0}  State  {1}  [{2}]'.format(
                    timestamp, item.get('action', ''), item.get('status', '')))
        return ('\n'.join(lines)
                or self.tr('No state or artifact transactions have been recorded.'))

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
            # dropped this one, so re-opening Mesh info showed the numbers from
            # whenever it was first opened -- and did nothing visible at all
            # when its tab was already in front.
            self._outputTabs.show(
                'mesh_info', self.tr('Mesh info'), lambda: self._dialog,
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
        from foammesh.view.mesh_check_action import operation_from_listing

        try:
            listing = await app.facadeClient.run('mesh.engine.list')
            payload = dict(getattr(listing, 'payload', None) or {})
        except (FacadeError, OSError, ValueError, TypeError):
            return qa_operation(None)
        return operation_from_listing(payload)

    @qasync.asyncSlot()
    async def _runMeshCheckDashboard(self):
        if app.project is None:
            return
        # Plan 42 SG7. One "Run mesh check" for every mesh. A Gmsh session
        # mesh has no polyMesh behind it, so its check is the judgement of
        # every element against the mesher's limits; any other mesh is
        # checked by the engine's own check below.
        tab = getattr(self, '_meshQualityTab', None)
        if tab is not None and tab.on_session_mesh():
            tab.judge_session_mesh()
            return
        # Plan 28 WP4. Which check depends on the solver the mesh is for: an
        # SU2 project is usually meshed on a machine with no OpenFOAM, and
        # this menu item then only ever produced a runtime error. Both checks
        # file the same report, so everything below is unchanged.
        operation = await self._qaOperationName()
        self.statusBar().showMessage(
            self.tr('Checking the mesh is readable by SU2…')
            if operation == 'quality.su2_readiness'
            else self.tr('Running checkMesh…'))
        MainWindow._selectConsole(self)
        unsubscribe = app.facadeClient.subscribe(
            Event.JOB_OUTPUT,
            lambda **payload: MainWindow._appendConsole(
                self, payload.get('line', '')))
        # Plan 42 SG18. The same submit the Quality page and the footer use;
        # this door only waits for the result so it can draw the console
        # table and the report dialog below.
        from foammesh.view.mesh_check_action import run_mesh_check

        finished = asyncio.get_running_loop().create_future()
        try:
            run_mesh_check(app.facadeClient,
                           lambda result: finished.done()
                           or finished.set_result(result),
                           operation=operation)
            execution = await finished
        finally:
            unsubscribe()
        refusal = getattr(execution, 'error', None)
        if refusal is not None:
            await AsyncMessageBox().warning(
                self, self.tr('Mesh check error'), str(refusal))
            return
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
            # since taken for the permanent Mesh quality tab -- so the registry
            # returned that tab, this dashboard was discarded, and running
            # checkMesh looked like it did nothing whatever. The two are
            # different surfaces: this is one run's full log and set list, that
            # is the standing verdict for the loaded mesh.
            self._outputTabs.show(
                'mesh_check', self.tr('Mesh check'), lambda: self._dialog,
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
                self.tr('Highlighted {0} from {1}.').format(
                    count_text(len(cell_sets[name]), 'failed cell'),
                    name), 8000)

    @qasync.asyncSlot()
    async def _runMeshTransformV13(self, operation):
        if app.project is None:
            return
        # Plan 35 CR7: a cold runtime answers this on a worker thread.
        utility = (await asyncio.to_thread(
            app.capabilities.utility, 'transformPoints')).executable
        # The first transform of a session waits on a cold WSL `transformPoints
        # -help` probe -- measured at 2.6s -- before its dialog can be built.
        # Nothing said so, so the click was indistinguishable from a dead menu
        # item, and a user who moved on never saw the dialog arrive.
        self.statusBar().showMessage(
            self.tr('Preparing mesh {0}…').format(operation))
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
                proceed = await modal(
                    messages.warning,
                    self, self.tr('Existing result fields'),
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
            text, details = messages.reasonOf(error, self.tr('The transform could not be applied.'))
            await AsyncMessageBox().warning(self, self.tr('Invalid transform'), text, detailedText=details)
            return
        self.statusBar().showMessage(self.tr('Running mesh {0}…').format(operation))
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
            text, details = messages.reasonOf(error, self.tr('The mesh could not be transformed.'))
            await AsyncMessageBox().warning(self, self.tr('Mesh transform error'), text, detailedText=details)
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
            await AsyncMessageBox().warning(
                self, self.tr('Geometry import error'), str(error))

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
            await AsyncMessageBox().warning(
                self, self.tr('Case create error'), str(error))
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
            await AsyncMessageBox().warning(
                self, self.tr('Terminal error'), str(error))

    @qasync.asyncSlot()
    async def _runMeshRepair(self):
        if app.project is None:
            return
        if not self._actionSnapshot().has_mesh:
            await self._runSurfaceRepair()
            return
        # Plan 35 CR7: probing a cold runtime waits; the window does not.
        utilities = await asyncio.to_thread(self._repairUtilities)
        check_utility = (await asyncio.to_thread(
            app.capabilities.utility, 'checkMesh')).executable
        service = MeshRepairService(utilities, app.jobManager, check_utility=check_utility)
        operations = service.available_operations(app.project.path)
        if not operations:
            self.statusBar().showMessage(
                self.tr('No configured mesh repair utility is available for this case.'), 6000)
            return
        labels = [operation.label for operation in operations]
        selected, accepted = await modal(
            QInputDialog.getItem,
            self, self.tr('Mesh repair'), self.tr('Repair operation'), labels, 0, False)
        if not accepted:
            return
        operation = operations[labels.index(selected)]
        cell_set = None
        subset_destination = None
        if operation is RepairOperation.SUBSET_CELLS:
            cell_set, accepted = await modal(
                QInputDialog.getText,
                self, self.tr('Subset mesh'), self.tr('Existing cell-set name'))
            if not accepted:
                return
            parent = await modal(
                QFileDialog.getExistingDirectory,
                self, self.tr('Select parent for subset case'), app.settings.getRecentLocation(),
                QFileDialog.Option.ShowDirsOnly)
            if not parent:
                return
            name, accepted = await modal(
                QInputDialog.getText,
                self, self.tr('Subset mesh'), self.tr('New subset case directory name'),
                text=f'{app.project.name()}-subset')
            if not accepted or not name.strip():
                return
            subset_destination = Path(parent) / name.strip()

        try:
            preview = service.preview(
                app.project.path, RepairRequest(operation, cell_set))
        except (OSError, ValueError) as error:
            await AsyncMessageBox().warning(
                self, self.tr('Mesh repair unavailable'), str(error))
            return
        warning = self.tr(
            'This operation creates a verified recovery point before changing the mesh.\n'
            'Command: {0}').format(' '.join(preview.command))
        if preview.configuration_path:
            warning += self.tr('\nConfiguration: {0}').format(preview.configuration_path)
        if operation is RepairOperation.SUBSET_CELLS:
            warning = self.tr('This destructive operation runs only in a new copied case.')
        confirmation = await modal(
            messages.question,
            self, self.tr('Run mesh repair'), warning,
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
            text, details = messages.reasonOf(error, self.tr('The mesh could not be repaired.'))
            await AsyncMessageBox().warning(self, self.tr('Mesh repair error'), text, detailedText=details)
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
            '{0} completed. Mesh bytes: {1:,} → {2:,}; patches: {3} → {4}.').format(
                operation.label, before['total_bytes'], after['total_bytes'],
                len(before['boundary_patches']), len(after['boundary_patches']))
        quality = completed.payload.get('quality')
        if quality is not None:
            prior = self.tr('unchecked')
            if prior_report is not None:
                prior = prior_report.result.severity
                if prior_report.stale:
                    prior = self.tr('{0} (stale)').format(prior)
            summary += self.tr(' Readiness: {0} → {1}.').format(
                prior, quality['severity'])
            # Per-metric before/after delta from the post-repair re-check.
            if prior_report is not None and completed.payload.get('copied_from') is None:
                try:
                    comparison = await app.facadeClient.run(
                        'quality.compare', {'baseline': prior_report.to_dict()})
                    changed = [
                        self.tr('{0} {1} → {2}').format(
                            metric.replace('_', ' '), item['baseline'], item['current'])
                        for metric, item in comparison.payload['metrics'].items()
                        if item['trend'] in ('improved', 'regressed')]
                    if changed:
                        summary += self.tr(' Quality delta: {0}.').format('; '.join(changed))
                except (FacadeError, OSError, ValueError, RuntimeError, CaseConflictError):
                    pass
        if completed.payload.get('copied_from') is not None:
            await modal(
                messages.information,
                self, self.tr('Subset case created'),
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
            await AsyncMessageBox().warning(
                self, self.tr('Restore previous mesh'),
                self.tr('No verified previous-mesh recovery point is available.'))
            self._updateMenuStates()
            return
        payload = points[-1]
        confirmation = await modal(
            messages.question,
            self, self.tr('Restore previous mesh'),
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
            await AsyncMessageBox().warning(
                self, self.tr('Restore previous mesh failed'), str(error))
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
        selected, accepted = await modal(
            QInputDialog.getItem,
            self, self.tr('Geometry repair'), self.tr('Surface'), labels, 0, False)
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
            f'{name}  —  {REPAIR_BANDS[TESSELLATED_ACTIONS[name].band][0]}'
            for name in operations]
        operation_label, accepted = await modal(
            QInputDialog.getItem,
            self, self.tr('Geometry repair'), self.tr('Operation'),
            labels, 0, False)
        if not accepted:
            return
        operation = operations[labels.index(operation_label)]
        hole_size = 1e6
        if operation == 'tess.fill_holes':
            hole_size, accepted = await modal(
                QInputDialog.getDouble,
                self, self.tr('Fill surface holes'),
                self.tr('Maximum hole size (m)'),
                1e6, 1e-12, 1e18, 6)
            if not accepted:
                return
        try:
            result = await asyncio.to_thread(
                apply_action,
                self._geometryManager.polyData(g_id), operation,
                hole_size=hole_size)
        except (OSError, ValueError) as error:
            text, details = messages.reasonOf(error, self.tr('The geometry could not be repaired.'))
            await AsyncMessageBox().warning(self, self.tr('Geometry repair error'), text, detailedText=details)
            return
        before_counts = {item.kind: item.count for item in result.before.findings}
        after_counts = {item.kind: item.count for item in result.after.findings}
        summary = self.tr(
            'Before score: {0}/100\nAfter score: {1}/100\n'
            'Open edges: {2} → {3}\nNon-manifold edges: {4} → {5}\n'
            'Duplicate points: {6} → {7}\n\n'
            'Yes: replace the working surface\nNo: save a repaired copy\nCancel: keep unchanged').format(
                result.before.score, result.after.score,
                before_counts.get('open_edges', 0), after_counts.get('open_edges', 0),
                before_counts.get('non_manifold_edges', 0),
                after_counts.get('non_manifold_edges', 0),
                before_counts.get('duplicate_points', 0),
                after_counts.get('duplicate_points', 0))
        choice = await modal(
            messages.question,
            self, self.tr('Geometry repair preview'), summary,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Cancel:
            return
        if choice == QMessageBox.StandardButton.No:
            path, _ = await modal(
                QFileDialog.getSaveFileName,
                self, self.tr('Save repaired geometry copy'),
                str(app.project.path / f"{surfaces[g_id].value('name')}-repaired.stl"),
                self.tr('STL surface (*.stl);;OBJ surface (*.obj)'))
            if path:
                try:
                    # DP-362. The copy keeps the name the surface is known
                    # by, so an STL saved here can still be keyed on.
                    await asyncio.to_thread(
                        partial(write_surface, result.polydata, path,
                                solid_name=surfaces[g_id].value('name')))
                    self.statusBar().showMessage(
                        self.tr('Saved repaired geometry copy to {0}.').format(path), 8000)
                except (OSError, ValueError) as error:
                    await AsyncMessageBox().warning(
                        self, self.tr('Save repaired geometry error'), str(error))
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
        utilities = await asyncio.to_thread(self._converterUtilities)
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
        source, _ = await modal(
            QFileDialog.getOpenFileName,
            self, self.tr('Select {0}').format(fmt.label), app.settings.getRecentLocation(), fmt.file_filter())
        if not source:
            return
        confirmation = await modal(
            messages.question,
            self, self.tr('Convert and replace mesh'),
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
            text, details = messages.reasonOf(error, self.tr('The mesh could not be converted.'))
            await AsyncMessageBox().warning(self, self.tr('Mesh conversion error'), text, detailedText=details)
            return
        if completed.status != 'accepted':
            detail = (completed.payload.get('validation_error') or
                      completed.payload.get('job', {}).get('error') or 'conversion failed')
            recovery = (self.tr('The previous mesh was restored.')
                        if completed.payload.get('restored') else
                        self.tr('No replacement mesh was committed.'))
            await AsyncMessageBox().warning(
                self, self.tr('Mesh conversion failed'), f'{detail}\n{recovery}')
            return
        app.refreshWorkflowResolution()
        self._stepManager.load()
        if self._meshManager is not None:
            await self._meshManager.load(0)
        await self._showConversionSummary(fmt, completed)
        self._updateMenuStates()

    async def _showConversionSummary(self, fmt, completed):
        """§13.2 step 8: conversion warnings plus a patch summary after commit."""
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
        await AsyncMessageBox().information(self, self.tr('Mesh conversion'), '\n'.join(lines))

    @staticmethod
    def _converterUtilities():
        return {
            fmt.utility_name: capability.executable
            for fmt in ConverterFormat
            if (capability := app.capabilities.utility(fmt.utility_name)).available
            and capability.executable is not None
        }

    async def _loadNativeMeshFromCase(self):
        source = await modal(
            QFileDialog.getExistingDirectory,
            self, self.tr('Select OpenFOAM mesh case'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not source:
            return
        # §13.1 native import contract: ask copy versus replace explicitly.
        box = messages.box(self)
        box.setWindowTitle(self.tr('Import native mesh'))
        box.setText(self.tr('How should the selected polyMesh be imported?'))
        replace = box.addButton(self.tr('Replace current mesh'), QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton(self.tr('Import into new case copy'), QMessageBox.ButtonRole.ActionRole)
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
            text, details = messages.reasonOf(error, self.tr('The mesh could not be imported.'))
            await AsyncMessageBox().warning(self, self.tr('Mesh import error'), text, detailedText=details)
            return
        app.refreshWorkflowResolution()
        self._stepManager.load()
        if self._meshManager is not None:
            await self._meshManager.load(0)
        self.statusBar().showMessage(self.tr('Native mesh import completed.'), 5000)
        self._updateMenuStates()

    async def _importNativeMeshIntoCopy(self, source):
        parent = await modal(
            QFileDialog.getExistingDirectory,
            self, self.tr('Select parent for new case copy'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not parent:
            return
        name, accepted = await modal(
            QInputDialog.getText,
            self, self.tr('Import into new case copy'), self.tr('New case directory name'),
            text=f'{app.project.name()}-imported')
        if not accepted or not name.strip():
            return
        try:
            destination = Path(parent) / name.strip()
            result = await app.facadeClient.run('mesh.import.native', {
                'source': source, 'copy_destination': str(destination)})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            text, details = messages.reasonOf(error, self.tr('The mesh could not be imported.'))
            await AsyncMessageBox().warning(self, self.tr('Mesh import error'), text, detailedText=details)
            return
        open_copy = await modal(
            messages.question,
            self, self.tr('Import complete'),
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
        parent = await modal(
            QFileDialog.getExistingDirectory,
            self, self.tr('Select copy parent directory'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not parent:
            return
        name, accepted = await modal(
            QInputDialog.getText,
            self, self.tr('Save project as'), self.tr('New case directory name'),
            text=f'{app.project.name()}-copy')
        if not accepted or not name.strip():
            return
        try:
            result = await app.facadeClient.run(
                'case.copy', {'destination': str(Path(parent) / name.strip())})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            text, details = messages.reasonOf(error, self.tr('The case could not be copied.'))
            await AsyncMessageBox().warning(self, self.tr('Copy error'), text, detailedText=details)
            return
        open_copy = await modal(
            messages.question,
            self, self.tr('Copy complete'), self.tr('Open the copied case now?'),
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
            await AsyncMessageBox().warning(
                self, self.tr('Save error'), str(error))
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
        parent = await modal(
            QFileDialog.getExistingDirectory,
            self, self.tr('Select save copy parent directory'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if not parent:
            return False
        name, accepted = await modal(
            QInputDialog.getText,
            self, self.tr('Save copy'), self.tr('New case directory name'),
            text=f'{app.project.name()}-conflict-copy')
        if not accepted or not name.strip():
            return False
        try:
            result = await app.facadeClient.run(
                'case.copy', {'destination': str(Path(parent) / name.strip())})
        except (FacadeError, OSError, ValueError, FileExistsError) as error:
            text, details = messages.reasonOf(error, self.tr('The copy could not be saved.'))
            await AsyncMessageBox().warning(self, self.tr('Save copy error'), text, detailedText=details)
            return False
        self.statusBar().showMessage(
            self.tr('Saved a conflict-safe copy to {0}.').format(
                result.payload['destination']), 8000)
        return True

    def _openPreferences(self):
        # DP-759. The theme is one page of it; the runtime, diagnostics and
        # qualification pages write through the setters their readers use.
        self._dialog = PreferencesDialog(
            self, settings=app.settings, themeManager=app.themeManager,
            applyRuntime=app.applyOpenFoamRuntime,
            findRuntime=app.findOpenFoamRuntime,
            privacy=self._openPrivacySettings if self._privacyConfigured else None)
        self._dialog.open()

    def _openRecent(self, path):
        self._openProject(path)

    def _installWelcomePanel(self):
        """Wire the welcome panel and File > Open example (Plan 40 UXA8, #27).

        The panel's buttons and the menu run the same handlers as File > New,
        Open and Open recent: this only gives them a second place to be asked
        from, so there is still one backend for each.
        """
        from foammesh.core.examples import list_examples

        welcome = self._stepManager.welcomePage
        welcome.setRecentsProvider(app.settings.getRecentCases)
        welcome.newRequested.connect(self._actionNew)
        welcome.openRequested.connect(self._actionOpen)
        welcome.recentSelected.connect(self._openRecent)
        welcome.exampleSelected.connect(self._openExample)

        menu = QMenu(self.tr('Open &example'), self._ui.menuFile)
        menu.setObjectName('menuOpenExample')
        menu.setToolTipsVisible(True)
        for example in list_examples():
            action = menu.addAction(example.title)
            action.setObjectName('actionOpenExample_' + example.name)
            action.setToolTip(example.summary)
            action.setStatusTip(example.summary)
            action.triggered.connect(
                lambda _checked=False, n=example.name: self._openExample(n))
        actions = self._ui.menuFile.actions()
        recent = self._ui.menuOpen_Recent.menuAction()
        after = actions.index(recent) + 1 if recent in actions else 0
        before = actions[after] if after < len(actions) else None
        if before is None:
            self._ui.menuFile.addMenu(menu)
        else:
            self._ui.menuFile.insertMenu(before, menu)
        self._exampleMenu = menu

    @qasync.asyncSlot()
    async def _openExample(self, name):
        """Make a new case from an example and stand on Mesh setup.

        The geometry is copied into a new case in the temporary folder (the
        same door File > New Untitled uses) and imported as the Import dialog
        would, then Geometry is settled the way Proceed settles it.
        """
        from foammesh.core.examples import find_example, stage_example
        try:
            example = find_example(name)
        except ValueError as error:
            await AsyncMessageBox().warning(
                self, self.tr('Open example'), str(error))
            return
        if not await self._startScratchCase(example.name):
            return
        try:
            staged = stage_example(example, app.project.path)
            await self._stepManager.importGeometryFiles([staged])
            atMeshSetup = await self._stepManager.openMeshSetup()
        except (OSError, RuntimeError, ValueError) as error:
            await AsyncMessageBox().warning(
                self, self.tr('Open example'), str(error))
            return
        if not atMeshSetup:
            # Plan 42 SG8: Preparation refused it and said why; saying it
            # opened at Mesh setup would write over that.
            return
        self.statusBar().showMessage(self.tr(
            '{0} is open at Mesh setup. Save the case somewhere before you '
            'close FoamMesh.').format(example.title), 12000)

    def _clearRecents(self):
        app.settings.clearRecents()
        self._recentFilesMenu.setRecents([])
        self.statusBar().showMessage(self.tr('Recent cases cleared.'), 3000)

    def _actionNew(self):
        path = QFileDialog.getExistingDirectory(
            self, self.tr('Select empty case directory'), app.settings.getRecentLocation(),
            QFileDialog.Option.ShowDirsOnly)
        if path:
            self._createInPlaceCase(Path(path))

    def _actionOpen(self):
        self._dialog = QFileDialog(self, self.tr('Select project directory'), app.settings.getRecentLocation())
        if platform.system() == 'Darwin':  # "show()" for native File dialog does not seem to work on macOS
            self._dialog.setOption(QFileDialog.Option.DontUseNativeDialog)

        self._dialog.setFileMode(QFileDialog.FileMode.Directory)
        self._dialog.fileSelected.connect(self._openProject)
        self._dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._dialog.show()

    @qasync.asyncSlot()
    async def _actionSave(self):
        await MainWindow.saveCase(self)

    async def saveCase(self, *, ask=True):
        """File > Save, as the menu runs it; says what became of it.

        Plan 48 PA5. The menu asks the person about a case changed outside
        FoamMesh (Reload, Save copy, Cancel). A caller that cannot be asked
        (``ask=False``, the local API) gets the conflict back as
        ``CaseConflictError`` and nothing is reloaded or copied.
        Returns ``'saved'``, ``'not saved'`` (no case, or the page would not
        save) or ``'conflict'`` (the person was asked and chose).
        """
        if app.project is not None and await self._stepManager.saveCurrentPage():
            try:
                # The facade owns the write, so save-time conflict detection has
                # to be asserted here; otherwise a case edited outside FoamMesh
                # would be overwritten without ever offering reload/save-copy.
                app.project.assertUnchanged()
                await app.facadeClient.run('case.save')
            except CaseConflictError as error:
                if not ask:
                    raise
                choice = await AsyncMessageBox().question(
                    self, self.tr('Case changed outside FoamMesh'),
                    self.tr('{0}\n\nYes: Reload case\nNo: Save copy\nCancel: Return without saving.').format(error),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                    QMessageBox.StandardButton.Cancel)
                if choice == QMessageBox.StandardButton.Yes:
                    await self._reloadAfterConflict()
                elif choice == QMessageBox.StandardButton.No:
                    await self._saveConflictCopy()
                return 'conflict'
            self._updateMenuStates()
            # DP-5711. Save is offered on a clean case too; say it was done.
            self.statusBar().showMessage(self.tr('Case saved.'), 4000)
            return 'saved'
        return 'not saved'

    def _saveAtStepBoundary(self):
        """Save the case when the person moves to another row (DP-5711).

        Finished stages were already kept as they finished (R182/R202), but
        what was set on a row reached the disk only on File > Save, so a case
        closed without it came back without those settings. Moving off a row
        is the boundary: the case is saved there through the same `case.save`
        File > Save uses, when something is unsaved and nothing is writing the
        case. A case changed outside FoamMesh is not overwritten in silence;
        the status line points at File > Save, which asks.
        """
        project = getattr(app, 'project', None)
        if project is None or not project.isDirty:
            return False
        if getattr(self, '_stepBoundarySave', None) is not None:
            return False
        if MainWindow._menuJobState(self) is not JobState.IDLE:
            return False
        try:
            if app.facadeClient.session().read_only:
                return False
        except Exception:                                    # noqa: BLE001
            return False
        try:
            project.assertUnchanged()
        except CaseConflictError:
            self.statusBar().showMessage(self.tr(
                'Not saved automatically: the case was changed outside '
                'FoamMesh. Use File > Save to choose what to keep.'), 8000)
            return False

        def saved(result):
            self._stepBoundarySave = None
            if isinstance(result, FailedResult):
                self.statusBar().showMessage(self.tr(
                    'The case could not be saved automatically: {0}').format(
                        result.message), 8000)
            # A success says nothing on the status line, which may be telling
            # the person something about the row they moved to; the title
            # bar's unsaved mark going away says it.
            self._updateMenuStates()

        self._stepBoundarySave = True
        task = submit(app.facadeClient, 'case.save', {}, then=saved)
        if task is not None:
            self._stepBoundarySave = task
        return True

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
            text, details = messages.reasonOf(error, self.tr('The case could not be reloaded.'))
            await AsyncMessageBox().warning(self, self.tr('Reload error'), text, detailedText=details)

    def _actionSaveAs(self):
        self._dialog = NewProjectDialog(self, self.tr('Save as new project'),
                                        Path(app.settings.getRecentLocation()).resolve(), meshAppProperties.projectSuffix)
        self._dialog.pathSelected.connect(self._saveAs)
        self._dialog.open()

    def _actionParameters(self):
        """Mesh > Quality thresholds and Settings > Mesh quality (DP-1470)."""
        self._openQualityThresholds()

    def _openQualityThresholds(self):
        """Plan 43 LY13 (D7, D8). Settings > Mesh quality and Mesh > Quality
        thresholds open the one MeshQualityParametersDialog (DP-1470: the item
        used to stand on a Snap row the outline does not have and did nothing).
        Applying it hands the limits to the Quality inspector, so the legend's
        limit line and the cells beyond the limit follow what was typed."""
        from foammesh.view.menu.mesh_quality.mesh_quality_parameters_dialog import (
            MeshQualityParametersDialog)
        # Plan 44 HNG (DP-2689): every open built a child dialog that was never
        # destroyed (a long pass piled them up on the main window); the one
        # that was closed is released before the next is built.
        from shiboken6 import isValid
        old = getattr(self, '_qualityDialog', None)
        if old is not None and isValid(old) and not old.isVisible():
            old.deleteLater()
        self._qualityDialog = MeshQualityParametersDialog(self)
        self._qualityDialog.accepted.connect(self._qualityThresholdsApplied)
        self._qualityDialog.open()

    def _qualityThresholdsApplied(self):
        info = self._displayControl.meshQualityInfo()
        info.applyThresholds()
        self._legendController.refresh()
        if hasattr(self, '_inspectorTimer'):
            self._inspectorShowLimit()
            self._inspectorRefresh()

    def _openStartWindow(self):
        """Plan 43 LY13 (D7). File > Start window: the launcher, reachable
        again once a project is open (it was shown only at start-up)."""
        from foammesh.view.widgets.project_dialog import ProjectDialog
        dialog = ProjectDialog(self)
        dialog.setRecents(app.settings.getRecentProjects())
        dialog.actionNewSelected.connect(lambda: (dialog.accept(), self._actionNew()))
        dialog.actionOpenSelected.connect(lambda: (dialog.accept(), self._actionOpen()))
        dialog.actionProjectSelected.connect(
            lambda path: (dialog.accept(), self._openRecent(path)))
        self._startDialog = dialog
        dialog.open()

    #: Object names for the two Plan 33 FORM-03/FORM-02 Help entries, so a
    #: gate can find them without matching on the words.
    STEP_HELP_ACTION = 'actionWhatThisStepIsFor'
    STEP_DETAILS_ACTION = 'actionCalculatedSettingsForThisStep'

    def _installStepHelpActions(self):
        """Put the step page's own two explanations in the Help menu.

        Plan 33 FORM-03 and FORM-02. Each task page used to carry a help
        band of its own -- a header row the height of a control, holding one
        `?` -- and ten of them ended their settings column with a read-only
        table of native mappings. Neither is a setting, and both are now
        where a reader looks for an explanation they did not ask for.

        Added here rather than in the form, because the form is generated.
        """
        menu = getattr(self._ui, 'menuHelp', None)
        if menu is None:
            return
        first = menu.actions()[0] if menu.actions() else None
        menu.setToolTipsVisible(True)  # DP-758
        self._stepHelpAction = QAction(self.tr('What this step is for'), self)
        self._stepHelpAction.setObjectName(self.STEP_HELP_ACTION)
        self._stepHelpAction.triggered.connect(self._openStepHelp)
        self._stepDetailsAction = QAction(
            self.tr('Calculated settings for this step'), self)
        self._stepDetailsAction.setObjectName(self.STEP_DETAILS_ACTION)
        self._stepDetailsAction.triggered.connect(self._openStepDetails)
        separator = QAction(self)
        separator.setSeparator(True)
        for action in (self._stepHelpAction, self._stepDetailsAction,
                       separator):
            menu.insertAction(first, action)
        menu.aboutToShow.connect(self._updateStepHelpActions)
        self._updateStepHelpActions()

    def _notificationTarget(self):
        """Where the user stands now: (step, branch route token or None)."""
        manager = getattr(self, '_stepManager', None)
        if manager is None or manager._externalMeshMode:
            return None
        return (manager._navigation.currentStep(), manager.routeMemo())

    def _goToNotification(self, entry):
        """Stand back where a notification was raised."""
        if entry.target is None:
            return
        step, token = entry.target
        manager = self._stepManager
        if token and manager.restoreRouteMemo(token):
            return
        manager._open(step)

    def _currentStepPage(self):
        """The task page the middle region is showing, if it is showing one."""
        for page in self.findChildren(EngineTaskPage):
            if page.isVisible():
                return page
        return None

    def _updateStepHelpActions(self):
        """Offer each entry only where it has something to open.

        DP-758: a disabled entry says why, on hover and in the status bar,
        instead of being grey with no explanation.
        """
        page = self._currentStepPage()
        if page is None:
            reasons = (self.tr('Open a meshing task to see its help'),) * 2
        else:
            reasons = (
                '' if page.stepHelpText()
                else self.tr('This step has no help of its own'),
                '' if page.detailRows()
                else self.tr('This step has no calculated settings'))
        for action, reason in zip(
                (self._stepHelpAction, self._stepDetailsAction), reasons):
            action.setEnabled(not reason)
            action.setToolTip(reason or action.text())
            action.setStatusTip(reason)

    def _openStepHelp(self):
        page = self._currentStepPage()
        if page is not None:
            self._dialog = page.showStepHelp()

    def _openStepDetails(self):
        page = self._currentStepPage()
        if page is not None:
            self._dialog = page.showDetails()

    def _actionAbout(self):
        self._dialog = AboutDialog(self)
        self._dialog.open()

    SHORTCUTS_ACTION = 'actionKeyboardShortcuts'

    def _sweepShortcuts(self, *_args):
        """Pick up the Gmsh pages' keys once they exist (Plan 40 UXE1).

        Called on every focus change, and Qt moves the focus while a focused
        widget is being destroyed: walking the window's children then wraps
        half-destroyed objects and the process dies (DP-5736). So the sweep
        runs on the next turn of the event loop, once per burst of changes,
        from a timer the window owns (it dies with the window).
        """
        timer = getattr(self, '_shortcutSweepTimer', None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(0)
            timer.timeout.connect(lambda: SHORTCUTS.sweep(self))
            self._shortcutSweepTimer = timer
        timer.start()

    def _installShortcutsHelp(self):
        """Help > Keyboard shortcuts... (Plan 40 UXE1). F1 opens it as well."""
        menu = getattr(self._ui, 'menuHelp', None)
        if menu is None:
            return
        self._shortcutsAction = QAction(
            self.tr('Keyboard shortcuts…'), self)
        self._shortcutsAction.setObjectName(self.SHORTCUTS_ACTION)
        self._shortcutsAction.setToolTip(self.tr(
            'Every key of the window in one list. Search it, give a command '
            'other keys, or print the sheet.'))
        self._shortcutsAction.triggered.connect(
            lambda _checked=False: self.showShortcuts())
        first = menu.actions()[0] if menu.actions() else None
        menu.insertAction(first, self._shortcutsAction)

    def showShortcuts(self):
        from foammesh.view.main_window import shortcuts_dialog
        SHORTCUTS.sweep(self)
        return shortcuts_dialog.show(self)

    #: Object names for the Plan 35 CR0 Help entries.
    CRASH_REPORT_ACTION = 'actionCreateCrashReport'
    OPEN_LOGS_ACTION = 'actionOpenLogsFolder'

    def _installDiagnosticsActions(self):
        """Help > "Open logs folder" and "Create crash report..." (Plan 35 CR0)."""
        menu = getattr(self._ui, 'menuHelp', None)
        if menu is None:
            return
        menu.addSeparator()
        self._openLogsAction = QAction(self.tr('Open logs folder'), self)
        self._openLogsAction.setObjectName(self.OPEN_LOGS_ACTION)
        self._openLogsAction.triggered.connect(self._openLogsFolder)
        self._crashReportAction = QAction(self.tr('Create crash report\u2026'), self)
        self._crashReportAction.setObjectName(self.CRASH_REPORT_ACTION)
        self._crashReportAction.setStatusTip(self.tr(
            'Save the logs, crash dumps and system details to a zip on your '
            'Desktop; nothing is sent'))
        self._crashReportAction.triggered.connect(self._createCrashReport)
        menu.addAction(self._openLogsAction)
        menu.addAction(self._crashReportAction)

    @staticmethod
    def _logsFolder() -> Path:
        directory = lifecycle.log_directory()
        return Path(directory) if directory else Path(app.settings.settingsPath()) / 'logs'

    def _openLogsFolder(self):
        # The lifecycle log made the folder at start; nothing is created here.
        folder = self._logsFolder()
        if not folder.is_dir() or not QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(folder))):
            self.statusBar().showMessage(
                self.tr('Could not open {0}.').format(folder), 6000)

    def showPreviousCrash(self, records):
        """Plan 35 CR0 step 6: say, without a modal, that a session died."""
        if not records:
            return None
        if self._crashNotice is not None:
            self._crashNotice.dismiss()
        banner = CrashNoticeBanner(records, self._ui.centralwidget)
        banner.reportRequested.connect(self._createCrashReport)
        banner.openLogsRequested.connect(self._openLogsFolder)
        banner.safeModeRequested.connect(self._safeModeNextTime)
        banner.destroyed.connect(lambda *_: setattr(self, '_crashNotice', None))
        self._ui.verticalLayout_23.insertWidget(0, banner)
        self._crashNotice = banner
        return banner

    def _safeModeNextTime(self):
        """Plan 35 CR8: the crash notice's "Start in safe mode next time"."""
        from foammesh.support import safe_mode

        safe_mode.remember_next_start(self._logsFolder())
        lifecycle.record('graphics safe mode chosen for the next start')
        self.statusBar().showMessage(self.tr(
            'FoamMesh will start in graphics safe mode next time.'), 8000)

    def _glInformation(self) -> dict:
        """The OpenGL vendor/renderer a view that has rendered reports."""
        from foammesh.rendering import gl_health
        from widgets.rendering.rendering_widget import RenderingWidget

        # Plan 35 CR8: the strings each view read once it had a context,
        # else the ones this or the last session recorded in the logs.
        for widget in self.findChildren(RenderingWidget):
            information = getattr(widget, 'glInformation', None)
            info = information() if callable(information) else {}
            if info:
                return info
        info = gl_health.recorded(lifecycle.log_directory())
        if info:
            return info
        for widget in self.findChildren(RenderingWidget):
            try:
                window = widget.interactor().GetRenderWindow()
                # Asking a window with no context yet would create one here.
                if window is None or window.GetNeverRendered():
                    continue
                report = window.ReportCapabilities() or ''
            except Exception:                              # noqa: BLE001
                continue
            info = {}
            for line in report.splitlines():
                key, _, value = line.partition(':')
                key = key.strip().lower()
                for name in ('vendor', 'renderer', 'version'):
                    if key == f'opengl {name} string':
                        info[name] = value.strip()
            if info:
                return info
        return {}

    @qasync.asyncSlot()
    async def _createCrashReport(self):
        """Help > Create crash report...: show the list, then write the zip."""
        from foammesh.support import crash_report

        caseRoot = self._captureManager.caseRoot()
        self.statusBar().showMessage(self.tr('Collecting the crash report\u2026'))
        try:
            manifest = await asyncio.to_thread(
                crash_report.collect, caseRoot, log_dir=self._logsFolder(),
                version=meshAppProperties.version,
                gl_info=self._glInformation())
        except Exception as error:                         # noqa: BLE001
            logger.warning('Could not collect a crash report', exc_info=True)
            self.statusBar().clearMessage()
            await AsyncMessageBox().warning(
                self, self.tr('Crash report'),
                self.tr('The crash report could not be collected: {0}').format(error))
            return
        self.statusBar().clearMessage()
        destination = crash_report.default_destination()
        dialog = CrashReportDialog(
            manifest.describe(), destination,
            crash_report._size_text(manifest.size), self)
        self._dialog = dialog
        if await asyncExec(dialog) != QDialog.DialogCode.Accepted:
            return
        try:
            path = await asyncio.to_thread(crash_report.write, manifest, destination)
        except OSError as error:
            await AsyncMessageBox().warning(
                self, self.tr('Crash report'),
                self.tr('The crash report could not be saved: {0}').format(error))
            return
        logger.info('Crash report written to %s', path)
        self.statusBar().showMessage(
            self.tr('Crash report saved to {0}').format(path), 15000)

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
                self, self.tr('Case already here'),
                self.tr('{0} already holds a FoamMesh case.\n\n'
                        'Open it instead?').format(path.name))
            if answer == QMessageBox.StandardButton.Yes:
                await self._openProject(str(path))
            return
        if kind in {CaseKind.OPENFOAM_CASE, CaseKind.RAW_POLY_MESH_CASE}:
            answer = await AsyncMessageBox().question(
                self, self.tr('Case already here'),
                self.tr('{0} already holds an OpenFOAM case.\n\n'
                        'Adopt it instead? Its mesh and dictionaries are '
                        'read where they are; nothing is copied or '
                        'overwritten.').format(path.name))
            if answer == QMessageBox.StandardButton.Yes:
                await self._openProject(str(path))
            return
        if kind is CaseKind.INVALID:
            await AsyncMessageBox().warning(
                self, self.tr('Case create error'),
                self.tr('{0} cannot hold a new case: {1}').format(
                    path, reasons or self.tr('it is not empty')))
            return

        try:
            # DP-2714: the case's files are written off the GUI thread.
            if await app.createInPlaceCaseAsync(path):
                self._recentFilesMenu.addRecentest(app.project.path)
                self._projectOpened()
        except (FileExistsError, OSError, ValueError) as error:
            await AsyncMessageBox().warning(
                self, self.tr('Case create error'), str(error))

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
            await AsyncMessageBox().warning(
                self, self.tr('Project open error'),
                self.tr('{0} is no longer there. It has been moved, renamed '
                        'or deleted since it was last opened.').format(path))
        except Timeout:
            await AsyncMessageBox().warning(self, self.tr('Project open error'),
                                                self.tr('{0} is already open in another program.').format(path.name))
        except ValidationError as e:
            await AsyncMessageBox().warning(self, self.tr('Project open error'),
                                                self.tr('Configuration error: {0} — {1}.').format(e.path, e.name))
        except ValueError as error:
            await AsyncMessageBox().warning(
                self, self.tr('Project open error'), str(error))

    @qasync.asyncSlot()
    async def _saveAs(self, path):
        await MainWindow.saveStateAs(self, path)

    async def saveStateAs(self, path, *, open_copy=None):
        """File > Save as, as the menu runs it; says what became of it.

        Plan 48 PA5. Written as the menu writes it: FoamMesh state only (the
        native geometry and mesh files are not copied). With ``open_copy``
        left out the person is shown the progress and asked whether to open
        the copy. With ``open_copy`` True or False the question is answered
        and no dialog is raised (the local API): a copy that cannot be
        written raises ``OSError``/``ValueError`` with the reason the dialog
        would give. Returns ``{'saved', 'path', 'opened'}``. Without the
        dialog, Cancel is ``cancelSaveAs`` (the local API's
        ``window.save_as.cancel``), polled between the copy's writes as the
        dialog's Cancel is.
        """
        answered = open_copy is not None
        if not await self._stepManager.saveCurrentPage():
            return {'saved': False, 'path': str(path), 'opened': False}
        progressDialog = None
        cancelEvent = threading.Event()
        self._saveAsCancel = cancelEvent
        if not answered:
            # Plan 39 VI1 (F6b). Cancel is polled between the copy's writes, so a
            # cancelled Save as leaves no destination at all.
            progressDialog = ProgressDialog(
                self, self.tr('Save FoamMesh state as'), cancelable=True,
                autoCloseOnCancel=False)
            progressDialog.open()

            progressDialog.setLabelText(self.tr(
                'Saving FoamMesh state only; native geometry and mesh files are not copied'))
        try:
            await asyncio.to_thread(
                app.project.saveStateAsCase, path,
                cancelled=(progressDialog.isCanceled
                           if progressDialog is not None else cancelEvent.is_set))
        except StateSaveCancelled:
            if progressDialog is not None:
                progressDialog.close()
            self.statusBar().showMessage(
                self.tr('Save as cancelled; nothing was written.'), 8000)
            return {'saved': False, 'path': str(path), 'opened': False}
        except (OSError, ValueError, FileExistsError) as error:
            if answered:
                raise
            progressDialog.close()
            text, details = messages.reasonOf(error, self.tr('The state could not be saved.'))
            await AsyncMessageBox().warning(self, self.tr('Save state error'), text, detailedText=details)
            return {'saved': False, 'path': str(path), 'opened': False}
        finally:
            if self._saveAsCancel is cancelEvent:
                self._saveAsCancel = None
        if progressDialog is not None:
            progressDialog.close()
        if not answered:
            open_copy = await modal(
                messages.question,
                self, self.tr('State copy complete'),
                self.tr('FoamMesh state was saved without native artifacts. Open it now?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            open_copy = open_copy == QMessageBox.StandardButton.Yes
        if open_copy:
            await self._openProject(str(path))
        else:
            self.statusBar().showMessage(
                self.tr('Saved FoamMesh state to {0}.').format(path), 8000)
        return {'saved': True, 'path': str(path), 'opened': bool(open_copy)}

    def cancelSaveAs(self) -> bool:
        """Cancel the Save as that is running without its dialog, as the
        dialog's Cancel does; whether one was running."""
        event = getattr(self, '_saveAsCancel', None)
        if event is None:
            return False
        event.set()
        return True

    def _disableMenubar(self):
        # DP-5711. The File menu used to go grey as a whole, so during a run
        # Save could not even be pointed at to learn why it was off. The menu
        # now stays open and each of its entries is held off instead (the
        # same lock: nothing in it can be chosen), and Save's tooltip says why.
        entries = fileMenuEntries(self._ui)
        if entries is None:
            self._ui.menuFile.setEnabled(False)
        elif not getattr(self, '_batchLocked', False):
            self._batchFileHeld = [action for action in entries
                                   if action.isEnabled()]
        self._ui.menuMesh.setEnabled(False)
        # DP-756. An undo under a running batch rewrites the case the batch
        # is still writing, so Edit goes too, and its keys with it.
        self._ui.menuEdit.setEnabled(False)
        self._batchLocked = True
        holdBatchLock(self._ui, True)

    def _enableMenubar(self):
        self._ui.menuFile.setEnabled(True)
        # DP-5711. Give back what the batch held; the policy refresh below
        # then settles the entries it owns (Save is offered again at once).
        for action in getattr(self, '_batchFileHeld', None) or ():
            action.setEnabled(True)
        self._batchFileHeld = []
        self._ui.menuMesh.setEnabled(True)
        self._ui.menuEdit.setEnabled(True)
        self._batchLocked = False
        self._updateMenuStates()

    @qasync.asyncSlot()
    async def _closeProject(self, toQuit=False, saveChanges=None):
        # DP-2764. ``saveChanges`` answers the close's questions as their
        # buttons would -- True is *Save changed*'s Ok, False its Discard
        # (and the untitled-case question's Discard) -- for a signed client
        # closing over the local API; None asks the person, as a click does.
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
                self, self.tr('Active operation'),
                self.tr('A case operation is running.\n\nYes: Cancel operation\n'
                        'No: Keep running and keep this case open\nCancel: Return'),
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
            if saveChanges is None:
                confirm = await AsyncMessageBox().question(
                    self, self.tr('Save changed'),
                    self.tr('Do you want to save your changes?'),
                    QMessageBox.StandardButton.Ok
                    | QMessageBox.StandardButton.Cancel
                    | QMessageBox.StandardButton.Discard)
            else:
                confirm = (QMessageBox.StandardButton.Ok if saveChanges
                           else QMessageBox.StandardButton.Discard)

            if confirm == QMessageBox.StandardButton.Ok:
                try:
                    await app.facadeClient.run('case.save')
                except CaseConflictError as error:
                    await AsyncMessageBox().warning(
                        self, self.tr('Case changed outside FoamMesh'), str(error))
                    return False
            elif confirm == QMessageBox.StandardButton.Cancel:
                return False

        # The one guard that actually prevents loss. An untitled case is in a
        # directory that gets swept, and "save" above only wrote it back to
        # that same doomed folder -- so closing is the last moment anyone can
        # be told, and it must name the folder rather than say "unsaved".
        if app.isScratchCase() and not self._relocatingScratch:
            scratch = Path(app.project.path)
            confirm = QMessageBox.StandardButton.Discard if saveChanges is False else \
                await AsyncMessageBox().question(
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
                return await self._closeProject(toQuit, saveChanges)

        app.closeProject()
        self._projectClosed()
        self._clear()

        if toQuit:
            self._readyToQuit = True
            self.close()

        return True

    def _cancelSectionJobs(self, reason):
        tool = self._displayControl.cutTool()
        cancel = getattr(tool, 'cancelSectionJobs', None)
        if cancel is not None:
            cancel(reason)

    def _sectionRemeshEvent(self, **_payload):
        # Any rewrite of the mesh stops a section worker still cutting it.
        self._cancelSectionJobs('remesh')

    def _sectionUnlockEvent(self, **payload):
        # `mesh.workflow.unlock` publishes a quality change for its task.
        if payload.get('operation') == 'mesh.workflow.unlock':
            self._cancelSectionJobs('unlock')

    def _mountGmshTools(self):
        # Plan 48 GM11b: the Gmsh tool dock, menus and keys (view/gmsh_tools).
        from foammesh.view.gmsh_tools import mount
        mount.mount(self)

    def _printLoadNotes(self):
        """DP-5712. Say in the Console what the load read differently."""
        try:
            notes = list(getattr(app.project.db(), 'loadNotes', ()) or ())
        except Exception:  # noqa: BLE001 - never block opening a case
            return
        for note in notes:
            MainWindow._appendConsole(self, str(note))

    def _projectOpened(self):
        from foammesh.view.gmsh_tools import mount
        mount.project_opened(self)
        from foammesh.view.main_window import window_api
        window_api.project_opened(self)
        self._recentFilesMenu.updateRecentest(app.project.path)
        # MEASURED 2026-09-03: the start-up prompt "Open a case or create a
        # new case to begin." stayed in the status bar through the whole
        # meshing route, because nothing ever cleared a message that has no
        # timeout. An open project is the answer to that prompt.
        self.statusBar().clearMessage()
        MainWindow._printLoadNotes(self)

        # 10MB(=10,485,760=1024*1024*10)
        # Plan 39 VI14d. Written by a thread of its own: a record logged on this
        # thread waited a second for the file system (virus scanner) once.
        self._handler = DeferredHandler(RotatingFileHandler(
            app.project.storagePath / 'log.txt', maxBytes=10485760,
            backupCount=5))
        self._handler.setFormatter(logging.Formatter("[%(asctime)s][%(name)s] ==> %(message)s"))
        logging.getLogger().addHandler(self._handler)
        scene_events = (
            Event.TRANSACTION_APPLIED,
            Event.ARTIFACT_GEOMETRY_CHANGED, Event.ARTIFACT_MESH_CHANGED,
        )
        # DP-363. These three replace the stored values under the user, so
        # the form they are looking at has to be repainted to match.
        history_events = (
            Event.UNDONE, Event.REDONE, Event.ARTIFACT_RESTORED,
        )
        menu_events = (
            Event.ARTIFACT_QUALITY_CHANGED, Event.ARTIFACT_STALE,
            Event.WORKFLOW_MODE_CHANGED, Event.WORKFLOW_STEP_STALE,
            Event.PROJECT_SAVED,
        )
        self._projectEventUnsubscribes = [
            app.events.subscribe(event, self._scheduleProjectRefresh)
            for event in scene_events
        ] + [
            app.events.subscribe(event, self._scheduleProjectReload)
            for event in history_events
        ] + [
            app.events.subscribe(event, self._scheduleMenuRefresh)
            for event in menu_events
        ] + [
            # A re-probe is what changes a capability answer; the rest of the
            # menu events only change what the answer is being used for.
            app.events.subscribe(
                Event.CAPABILITIES_CHANGED, self._capabilitiesReprobed),
        ] + [
            # WP3.1. A verdict for a mesh the settings have moved on from must
            # not keep reading as current.
            app.events.subscribe(
                Event.MESH_VERDICT_STALE, self._meshVerdictSuperseded),
        ] + [
            # DP-2730. An import over the local API writes the store only;
            # the Geometry page adds its rows so the tree, the viewport and
            # the Preparation lock see it.
            app.events.subscribe(
                Event.ARTIFACT_GEOMETRY_CHANGED, self._geometryFromApi),
        ] + [
            # R143. MEASURED: the snappy Quality row ran checkMesh -allTopology
            # -allGeometry -writeSets on the 67,662-cell mesh, wrote every
            # metric plus `failed_checks: 1` and the concave-cell finding to
            # foammesh/quality/latest.json, and set the outline row to
            # warning -- and the Mesh quality tab underneath still read "This
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
        ] + [
            # Plan 37 UF10. A section worker cutting a mesh that is being
            # remeshed or unlocked is stopped and its answer taken down.
            app.events.subscribe(
                Event.ARTIFACT_MESH_CHANGED, self._sectionRemeshEvent),
            app.events.subscribe(
                Event.ARTIFACT_QUALITY_CHANGED, self._sectionUnlockEvent),
        ] + [
            # DP-2853. Output of a job a local API command started goes to
            # the Console, as the window's own runs put theirs there.
            app.events.subscribe(Event.JOB_OUTPUT, self._apiJobOutput),
        ]
        # DP-2851. A command this window did not send (a local API run) is
        # shown when it ends, the way the window shows its own.
        try:
            self._projectEventUnsubscribes.append(
                app.facadeClient.session().observe_commands(
                    self._commandEnded))
        except Exception:       # noqa: BLE001 - no session, no commands
            logger.debug('no session to observe commands of')

        self._updateWindowTitle()

        self._geometryManager = GeometryManager()
        self._meshManager = MeshManager()
        self._meshManager.cellCountChanged.connect(self._cellCountChanged)
        self._meshManager.volumeColourDropped.connect(
            self._volumeColourDropped)
        # Plan 42 SG1. The mesh on screen is the context's provenance source.
        wire_mesh_provenance(app, self._meshManager)
        # DP-485. A job that rewrites the mesh waits for the viewport to
        # finish reading it.
        try:
            self._projectEventUnsubscribes.append(
                app.facadeClient.session().jobs.add_read_barrier(
                    self._meshManager.readsFinished))
        except Exception:       # noqa: BLE001 - no session, no writers
            logger.debug('no job manager to hold for mesh reads')
        # DP-96. What is on screen and how big it is, said where the user
        # is already looking rather than in a panel they have to open.
        self._meshManager.meshSummaryChanged.connect(
            self._viewportOverlay.setMesh)
        # Plan 35 CR3. A decimated, surface-only or unbuilt preview says so,
        # with Try again / Build anyway / Load full volume under it.
        self._meshManager.previewNoticeChanged.connect(
            self._viewportOverlay.setPreviewNotice)

        self._geometryManager.load()
        self._stepManager.load()
        self._trackMeshDraw(self.drawStoredResult())
        # An opened case may already carry a checkMesh report. Reading it here
        # is what makes the quality surfaces reflect the mesh a user just
        # loaded rather than only one this session generated.
        self.refreshMeshVerdictSoon()
        self.refreshNamedViews()
        self._updateMenuStates()
        self._offerRecovery()

    def _installWslHealthBar(self):
        health = getattr(app, 'wslHealth', None)
        central = self.centralWidget()
        layout = central.layout() if central is not None else None
        if health is None or layout is None:
            return None
        try:
            bar = WslHealthBar(health, app.events,
                               on_diagnostics=self.showRuntimeDiagnostics,
                               parent=layout.parentWidget())
        except Exception:       # noqa: BLE001 - a missing banner must not stop the window
            logger.exception('could not build the WSL health banner')
            return None
        layout.insertWidget(0, bar)
        return bar

    def showRuntimeDiagnostics(self):
        """The runtime diagnostics window: profile, WSL health, shutdown."""
        return show_runtime_diagnostics(
            self, app.facadeClient, getattr(app, 'wslHealth', None))

    def _offerRecovery(self):
        """Plan 35 CR9. Unsaved changes a dead session left, and whether
        this one's are being protected, as bars over the window."""
        previous = getattr(self, '_autosaveBars', None)
        if previous is not None:
            previous.dispose()
        autosave = getattr(app.project, 'autosave', None)
        try:
            self._autosaveBars = offer_recovery(
                self, autosave() if callable(autosave) else None,
                on_restored=self._unsavedChangesRestored,
                on_save=self._ui.actionSave.trigger,
                on_save_as=self._ui.actionSaveAs.trigger)
        except Exception:       # noqa: BLE001 - never block opening a case
            logger.exception('could not offer the unsaved changes')
            self._autosaveBars = None

    def _unsavedChangesRestored(self, _count, _filesRestored):
        # The restore replaced the stored values (and any geometry) under the
        # user: repaint the forms and reload the geometry from the store.
        self._scheduleProjectReload()

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
        from foammesh.view.gmsh_tools import mount
        mount.project_closed(self)
        from foammesh.view.main_window import window_api
        window_api.project_closed(self)
        bars = getattr(self, '_autosaveBars', None)
        if bars is not None:
            bars.dispose()
            self._autosaveBars = None
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
        # Plan 35 CR3. The closing case's actors are released here, on the
        # GUI thread and against the viewport's own context, rather than
        # left for the cyclic collector to destroy on whatever thread trips
        # it once the managers below are dropped.
        # DP-2717. MEASURED on the two-engine walk: 1.85 s on the GUI thread
        # here, because every actor the disposal removed announced
        # `partsChanged` and each announcement rebuilt the whole overlay
        # part list from the parts still left -- quadratic in the parts. The
        # rebuilds are held while the case's actors go and made once after.
        self._overlayRebuildHeld = True
        self._overlayRebuildPending = False
        try:
            for manager in (getattr(self, '_geometryManager', None),
                            getattr(self, '_meshManager', None)):
                dispose = getattr(manager, 'dispose', None)
                if callable(dispose):
                    dispose()
            # Plan 45 P11. The feature lines belong to the closing mesh.
            MainWindow._dropFeatureActors(self)
            # The actors go first: the toolbar readout is derived from what
            # the view is holding, so clearing it before the view leaves the
            # closing case's model extent on screen (R49).
            self._displayControl.clear()
        finally:
            self._overlayRebuildHeld = False
        if self._overlayRebuildPending:
            self._overlayRebuildPending = False
            self.rebuildOverlayParts()
        self._renderingTool.clear()
        self._consoleView.clear()
        self._viewportOverlay.setParts([])
        self._overlayPartsShown = None
        # The run line named a mesh from the case that is closing.
        self._historicalResult = None
        self._viewportOverlay.setResult('')
        self._viewportOverlay.setMesh('')
        # GEO-05. So did the run strip, and nothing here ever cleared it, so
        # the previous case's run id, verdict and cell count were still on
        # screen over a case that held no mesh at all. The offer and the
        # allocation go with it: both name workers and a mesh belonging to
        # the case that is closing.
        strip = getattr(self, '_runStatusStrip', None)
        if strip is not None:
            strip.clear()
        self._offeredResult = None
        self._lastRunAllocation = None
        # The sets belong to the case that is closing; carrying them into the
        # next one would enable a button that acts on cells that are gone.
        self._lastFailedCellSets = {}
        self._geometryManager = None
        self._meshManager = None
        # What the closing case left behind is reaped now, on this thread.
        gc_policy.collect_full('project close')

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
        # Plan 45 V5. Say what took the cells away: a section, or the quality
        # filter that shows only the worst cells (no section is cut then).
        causeOf = getattr(getattr(self, '_meshManager', None),
                          'cellCountCause', None)
        cause = (causeOf() if callable(causeOf) else '') or 'section'
        text, tip = cell_count_text(visible, total, cause)
        # Plan 42 SG1. A new mesh on screen is a new provenance.
        refresh_mesh_provenance(app)
        self._ui.cellCount.setText(text)
        self._ui.cellCount.setToolTip(tip)
        # DP-811. The whole-mesh count is on the overlay card; the toolbar
        # keeps the readout only while a section makes the two numbers differ.
        readout = getattr(self._ui, 'widget_17', None) or self._ui.cellCount
        sectioned = 0 < visible < total
        isHidden = getattr(readout, 'isHidden', None)
        if callable(isHidden) and isHidden() == sectioned:
            readout.setVisible(sectioned)
