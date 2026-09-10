#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import math
from enum import Enum, auto
from functools import partial

from PySide6.QtCore import QObject, Signal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

import qasync

from foammesh.app import app
from foammesh.core.case import ExternalMeshSummary, WorkflowMode
from foammesh.core.engine.registry import ENGINE_REGISTRY
from foammesh.core.project import Event
from foammesh.core.run_result import RunResultHandle
from foammesh.db.configurations_schema import Step
from foammesh.view.step_page import StepPage
from foammesh.view.geometry.geometry_page import GeometryPage
from foammesh.view.geometry.repair_page import GeometryRepairPage
from foammesh.view.base_grid.base_grid_page import BaseGridPage
from foammesh.view.castellation.castellation_page import CastellationPage
from foammesh.view.snap.snap_page import SnapPage
from foammesh.view.boundaryLayer.boundary_layer_page import BoundaryLayerPage
from foammesh.view.export.export_page import ExportPage
from foammesh.view.region.region_page import RegionPage
from .external_mesh_page import ExternalMeshPage
from .empty_case_page import EmptyCasePage
from .meshing_method_branch import (
    METHOD_TOKEN, TASK_TOKEN_PREFIX, MeshingMethodBranch)
from widgets.async_message_box import AsyncMessageBox
from foammesh.view.facade_client import FailedResult, query, submit

from .run_narration import describe_start


logger = logging.getLogger(__name__)


class ButtonID(Enum):
    NEXT    = auto()
    FINISH  = auto()
    CANCEL  = auto()
    UNLOCK  = auto()


class StepControlButtons(QObject):
    nextButtonClicked = Signal()
    finishButtonClicked = Signal()
    cancelButtonClicked = Signal()
    unlockButtonClicked = Signal()

    def __init__(self, ui):
        super().__init__()

        self._cancelClicked = False

        self._buttons = {
            ButtonID.NEXT:      ui.next,
            ButtonID.FINISH:    ui.finishSteps,
            ButtonID.CANCEL:    ui.finishCancel,
            ButtonID.UNLOCK:    ui.unlock
        }
        # R2. The wizard bar added `Proceed`, and this row's `Next` was never
        # retired, so two advance controls sat 35 px apart and disagreed:
        # MEASURED on Geometry with five named boundaries loaded, `Next` was
        # grey while `Proceed` was live. They are not the same control --
        # `Next` walks the legacy numeric steps, `Proceed` settles the task
        # and opens the next one -- and the greyed one reads as "you are not
        # finished here" over a step that is finished. Where the wizard bar
        # exists, `Next` is the duplicate and it never appears. Finish, Cancel
        # and Unlock stay: they name actions Proceed does not perform.
        self._retired = (
            frozenset({ButtonID.NEXT})
            if hasattr(ui, 'wizardProceedButton') else frozenset())
        for id_ in self._retired:
            self._buttons[id_].hide()

        ui.next.clicked.connect(self.nextButtonClicked)
        ui.finishSteps.clicked.connect(self.finishButtonClicked)
        ui.finishCancel.clicked.connect(self._onCancelButtonClicked)
        ui.unlock.clicked.connect(self.unlockButtonClicked)

    def isCancelClicked(self):
        return self._cancelClicked

    def showButton(self, id_, enabled=True):
        self._cancelClicked = False

        for i, button in self._buttons.items():
            if i == id_ and i not in self._retired:
                button.show()
                button.setEnabled(enabled)
            else:
                button.hide()

    def hideAll(self):
        self._cancelClicked = False
        for button in self._buttons.values():
            button.hide()

    def isRetired(self, id_) -> bool:
        return id_ in self._retired

    def enableNextButton(self):
        if ButtonID.NEXT in self._retired:
            return
        self._buttons[ButtonID.NEXT].setEnabled(True)

    def disableNextButton(self):
        if ButtonID.NEXT in self._retired:
            return
        self._buttons[ButtonID.NEXT].setEnabled(False)

    def _onCancelButtonClicked(self):
        self._cancelClicked = True
        self.cancelButtonClicked.emit()


class StepManager(QObject):
    displayStepChanged = Signal(Step)
    workingStepChanged = Signal(Step)
    batchStarted = Signal()
    batchStopped = Signal()

    def __init__(self, navigation, ui):
        super().__init__()

        self._ui = ui
        self._navigation = navigation
        self._workingStep = Step.NONE
        self._contentStack = ui.content
        self._buttons = StepControlButtons(ui)
        self._contentPages = {}
        self._scenePage = ui.sceneSidebarPage
        self._externalMeshPage = ExternalMeshPage(self._contentStack)
        self._contentStack.addWidget(self._externalMeshPage)
        self._emptyCasePage = EmptyCasePage(
            self._contentStack
            if isinstance(self._contentStack, QWidget) else None)
        self._emptyCasePage.setObjectName('emptyCaseRegionBPage')
        self._emptyCasePage.setAccessibleName(
            self.tr('Region B guidance when no case is open'))
        self._emptyCasePage.setGuidance(
            getattr(ui, 'actionNew', None), getattr(ui, 'actionOpen', None),
            getattr(ui, 'actionLoadGeometry', None))
        self._contentStack.addWidget(self._emptyCasePage)

        self._batchRunning = False
        self._externalMeshMode = False
        self._sceneVisible = False
        # R195. Routes, not widgets: `(widget, branch token or None)`.
        # Every task inside an engine branch is served by the one branch
        # widget with a different form selected in it, so a history of
        # widgets recorded nine visited Gmsh tasks as a single entry.
        self._routeHistory = []
        # R149. Every route into the content panel awaits the page it is
        # opening, and two clicks in quick succession interleaved: clicking
        # 4. Meshing Method and then 3. Region left the outline on Region and
        # the panel on Meshing Method, because the first navigation resumed
        # after the second had finished and set the stack last. A navigation
        # claims this counter on entry and gives up the moment another one
        # claims it.
        self._routeGeneration = 0
        self._methodProceedConnected = False
        # The legacy StepPage behind the branch widget on screen, so that
        # leaving the node can still save it.
        self._branchPage = None
        self._branchWidget = None

        self._pages = {
            Step.NONE: StepPage(ui, None),
            Step.GEOMETRY: GeometryPage(ui),
            Step.GEOMETRY_REPAIR: GeometryRepairPage(ui),
            Step.REGION: RegionPage(ui),
            Step.BASE_GRID: BaseGridPage(ui),
            Step.CASTELLATION: CastellationPage(ui),
            Step.SNAP: SnapPage(ui),
            Step.BOUNDARY_LAYER: BoundaryLayerPage(ui),
            Step.EXPORT: ExportPage(ui),
        }

        # The visible workflow is token-routed after Meshing Method.
        #
        # Plan 30 WP-09 / F-17. This is where a hand-written table used to map
        # six snappy task ids onto Designer widgets, so those six tasks were
        # routed here and skipped by `EngineBranchView._rebuild`: two page
        # systems, two refresh paths and two Run idioms for one workflow, and
        # a table that had to be kept in step with the engine's workflow
        # descriptor by hand. Every task both engines declare now has a
        # registry page (`SNAPPY_TASK_PAGES`, `GMSH_TASK_PAGES`), and the
        # branch is the one thing that routes them.
        self._methodBranch = MeshingMethodBranch(
            ui, navigation, lambda: getattr(app, 'facadeClient', None),
            parent=self)
        # Plan 30 F-03. One finisher, one route. The branch still asks by
        # engine id because that is what the page it is on knows; what it gets
        # back is the same function every time.
        self._methodBranch.pipeline_runners = {
            engine_id: partial(self._finishPipeline, engine_id)
            for engine_id in ENGINE_REGISTRY.ids()
        }
        self._methodBranch.pageRequested.connect(self._showBranchPage)
        # R104/R162. See `_acceptExportTask`: an export that succeeded is the
        # event that finishes the workflow, and it was going nowhere.
        exported = getattr(self._pages[Step.EXPORT], 'exported', None)
        if exported is not None:
            exported.connect(self._acceptExportTask)
        if hasattr(ui, 'wizardBackButton'):
            ui.wizardBackButton.clicked.connect(self._wizardBack)
            ui.wizardProceedButton.clicked.connect(self._wizardProceed)

        self._installPrerequisiteBanner()
        self._connectSignalsSlots()

    #: Steps whose settings only mean anything once an engine has been chosen.
    #:
    #: A1. Base Grid is snappy's, and the wizard walked straight into it with
    #: the engine still unselected; the only thing that ever said so was a
    #: refusal from `Generate`, after the page had been filled in. On the Gmsh
    #: run the page is not part of the pipeline at all.
    #:
    #: R52. Which engine each of those steps belongs to, so the wizard can
    #: walk past the ones this run will never reach. A step absent from the
    #: map is common ground and belongs to every engine.
    _STEP_ENGINE = {
        Step.BASE_GRID: 'snappy',
        Step.CASTELLATION: 'snappy',
        Step.SNAP: 'snappy',
        Step.BOUNDARY_LAYER: 'snappy',
    }

    _ENGINE_SPECIFIC_STEPS = frozenset(_STEP_ENGINE)

    def _installPrerequisiteBanner(self):
        """A line above the content that says why a page cannot be used yet."""
        layout = getattr(self._ui, 'verticalLayout_2', None)
        owner = getattr(self._contentStack, 'parentWidget', None)
        parent = owner() if callable(owner) else None
        if parent is None or not hasattr(layout, 'insertWidget'):
            self._prerequisiteBanner = None
            return
        banner = QLabel(parent)
        banner.setObjectName('stepPrerequisiteBanner')
        banner.setWordWrap(True)
        banner.setTextFormat(Qt.TextFormat.RichText)
        banner.setOpenExternalLinks(False)
        banner.setVisible(False)
        banner.linkActivated.connect(
            lambda _link: self._navigation.requestBranch(METHOD_TOKEN))
        layout.insertWidget(0, banner)
        self._prerequisiteBanner = banner

    def _engineId(self) -> str:
        """The engine this case is committed to, or '' when it cannot be read.

        R52. '' is not 'unselected': one means the probe had no answer, and
        neither the banner nor the wizard walk may act on that.
        """
        from foammesh.core.engine import configured_engine_id
        try:
            return configured_engine_id(app.facadeClient.checkout())
        except Exception:                                    # noqa: BLE001
            # No case, no engine question -- and never block on a probe that
            # could not answer.
            return ''

    def _engineChosen(self) -> bool:
        return self._engineId() != 'unselected'

    def _updatePrerequisiteBanner(self, step) -> None:
        banner = getattr(self, '_prerequisiteBanner', None)
        if banner is None:
            return
        blocked = (step in self._ENGINE_SPECIFIC_STEPS
                   and not self._engineChosen())
        if blocked:
            banner.setText(self.tr(
                'This page belongs to a specific meshing engine, and no '
                'engine has been chosen yet. Its settings cannot be written '
                'until you pick one on '
                '<a href="#method">3. Meshing Method</a>.'))
        banner.setVisible(blocked)

    def load(self):
        self._contentStack.show()
        self._sceneVisible = False
        self._externalMeshMode = app.workflowResolution.workflow is WorkflowMode.MESH_EXTERNAL
        self._navigation.setExternalMeshMode(self._externalMeshMode)
        if self._externalMeshMode:
            classification = query(app.facadeClient, 'case.classify').payload
            resolution = app.workflowResolution
            summary = ExternalMeshSummary(
                case_path=classification['case_path'],
                poly_mesh_path=classification['poly_mesh_path'],
                origin=resolution.mesh_origin.value.replace('_', ' '),
                artifact_state=resolution.artifact_state.value,
                reason=resolution.reason,
                has_mesh=classification['has_mesh'])
            status = query(app.facadeClient, 'workflow.status').payload
            self._externalMeshPage.setSummary(
                summary, can_return=status['can_return_to_external'])
            self._contentStack.setEnabled(True)
            self._contentStack.setCurrentWidget(self._externalMeshPage)
            self._buttons.hideAll()
            self._ui.statusbar.showMessage(
                self.tr('External mesh mode: generated-workflow steps are unavailable for this mesh.'))
            return

        self._contentStack.setEnabled(True)
        self._navigation.setExternalMeshMode(False)

        for page in self._pages.values():
            page.unload()
            page.load()

        # The engine branch needs an open case, so it is built here rather than
        # in __init__. A failure to build it must not block the Snappy journey.
        try:
            self._methodBranch.load()
            method_page = getattr(self._methodBranch, 'methodPage', None)
            if (not self._methodProceedConnected
                    and method_page is not None):
                method_page.engineChanged.connect(
                    self._onWizardMethodAccepted)
                self._methodProceedConnected = True
        except Exception:
            logger.exception('failed to build the meshing-method branch')

        savedStep = app.facadeClient.checkout().getEnum('step')

        step = Step.GEOMETRY
        while step < savedStep and self._pages[step].isNextStepAvailable():
            self._navigation.enableStep(step)
            step += 1

        for s in range(step + 1, Step.LAST_STEP + 1):
            self._navigation.disableStep(s)
            self._pages[Step(s)].clearResult()

        case_root = app.facadeClient.case_root
        times = [path.name for path in case_root.iterdir()
                 if path.is_dir() and path.name.replace('.', '', 1).isdigit()]
        for value in times:
            if float(value) > self._pages[Step.LAST_STEP].OUTPUT_TIME:
                # C31-12. Scheduled, not blocking. `load()` runs on every
                # applied transaction, so a blocking clear here charged every
                # commit one facade round trip per stray time directory.
                submit(app.facadeClient, 'artifact.stage.clear',
                       {'output_time': int(float(value))})

        # R8/R12/R56/R96/R132/R149. `load()` runs on every TRANSACTION_APPLIED,
        # and it used to finish by opening a numeric step unconditionally.
        # Every commit therefore ejected the user from whatever branch task
        # they were editing and dropped them on 2. Repair or 3. Region --
        # measured on the target-solver radio, on adding a region, on Set
        # tolerance and on Update, for both engines. A refresh is a redraw of
        # the case, not a navigation: if the panel is standing on a branch
        # route that still exists, it stays there.
        if not self._restoreBranchRoute():
            self._open(step)

    def _restoreBranchRoute(self) -> bool:
        """Put the panel back on the branch task it was on before a refresh.

        Answers whether it did. The route has to still be reachable -- an
        engine switch replaces the whole child set, and a task that is no
        longer in the outline cannot be stood on -- so this re-resolves the
        token rather than trusting the retained widget.
        """
        token = getattr(self._methodBranch, 'currentToken', None)
        if not token or self._contentStack.currentWidget() is self._emptyCasePage:
            return False
        if self._branchWidget is None:
            return False
        if token not in self._methodBranch.routeTokens():
            return False
        widget = self._methodBranch.widgetForToken(token)
        if widget is None:
            return False
        self._sceneVisible = False
        self._contentStack.show()
        self._contentStack.setCurrentWidget(widget)
        # R96/R132. `_syncChildren` rebuilds the branch's rows with
        # `removeRows`, which takes the outline's highlight with them, so
        # even when the panel stayed put the tree stopped saying where the
        # user was. Put the highlight back on the route being restored.
        setCurrent = getattr(self._navigation, 'setBranchCurrent', None)
        if setCurrent is not None:
            setCurrent(token)
        self._buttons.hideAll()
        self._updateWizardActions()
        return True

    def routeMemo(self):
        """Where the user is standing, in a form a reopen can restore (R168).

        `_restoreBranchRoute` survives a refresh because the panel never let
        go of the branch widget. Saving an untitled case is not a refresh: it
        copies the case, opens the copy, and `unload()` drops the widget on
        the way through -- so pressing Generate on Base Grid answered the save
        prompt and came back on 2. Repair, watching a progress dialog for a
        step that was no longer on screen. This is the token to come back to.
        """
        if (self._externalMeshMode or self._branchWidget is None
                or self._contentStack.currentWidget() is self._emptyCasePage):
            return None
        return str(getattr(self._methodBranch, 'currentToken', '') or '') or None

    def restoreRouteMemo(self, token) -> bool:
        """Stand back on a route remembered by `routeMemo`. Answers whether
        it could: a token the reopened case does not publish is not a place to
        put the user."""
        if not token or self._externalMeshMode:
            return False
        if token not in self._methodBranch.routeTokens():
            return False
        return self._methodBranch.route(token)

    def unload(self):
        self._externalMeshMode = False
        self._sceneVisible = False
        self._navigation.setExternalMeshMode(False)
        self._ui.navigation.setEnabled(False)
        self._contentStack.show()
        self._contentStack.setEnabled(True)
        self._contentStack.setCurrentWidget(self._emptyCasePage)
        self._buttons.hideAll()
        self._routeHistory.clear()
        self._branchPage = None
        self._branchWidget = None
        self._updateWizardActions()
        for page in self._pages.values():
            page.unload()

    def currentPage(self):
        if self._externalMeshMode:
            return self._pages[Step.NONE]
        return self._pages[self._navigation.currentStep()]

    async def saveCurrentPage(self):
        return await self.currentPage().save()

    async def openGeometryImport(self):
        await self._pages[Step.GEOMETRY].openImportDialog()

    def open2DExtrude(self, mode: str):
        if self._externalMeshMode:
            raise ValueError('2D extrusion requires an authored workflow mesh')
        self._pages[Step.EXPORT].open2DExtrudeDialog(mode)

    def openNextStep(self):
        """Advance to the next step this run actually uses (R52).

        MEASURED: Next on Region landed on Base Grid, a page whose own banner
        was at that moment saying its settings could not be written because no
        engine had been chosen -- so the one linear control the wizard offers
        walked into a page the wizard itself declared unusable, and the user
        had to work out unaided that Meshing Method came first. Base Grid is
        also snappy's: on a run headed for Gmsh it is not on the path at all.
        The banner is right; the walk was wrong.
        """
        if self._externalMeshMode:
            return
        if self._workingStep >= Step.LAST_STEP:
            return
        step = Step(self._workingStep + 1)
        engine = self._engineId()
        if engine == 'unselected' and step in self._ENGINE_SPECIFIC_STEPS:
            # Do not strand the user in front of a dead page: open the one
            # question that has to be answered before it can be used.
            self._navigation.requestBranch(METHOD_TOKEN)
            self._ui.statusbar.showMessage(
                self.tr('Choose a meshing method first - the next page '
                        'belongs to one.'), 5000)
            return
        if engine:
            while (step < Step.LAST_STEP
                   and self._STEP_ENGINE.get(step, engine) != engine):
                step = Step(step + 1)
        self._open(step)

    def retranslatePages(self):
        for page in self._pages.values():
            page.retranslate()

    def _connectSignalsSlots(self):
        self._navigation.currentStepChanged.connect(self._moveToStep)
        self._navigation.currentStepReactivated.connect(self._showWorkflowStep)
        self._navigation.sceneRequested.connect(self._showScene)
        self._buttons.nextButtonClicked.connect(self.openNextStep)
        self._buttons.finishButtonClicked.connect(self._finishSteps)
        self._buttons.cancelButtonClicked.connect(self._cancelFinishSteps)
        self._buttons.unlockButtonClicked.connect(self._unlockCurrentStep)
        self._externalMeshPage.showMeshRequested.connect(self._showExternalMesh)
        self._externalMeshPage.meshInfoRequested.connect(self._showExternalMeshInfo)
        self._externalMeshPage.startWorkflowRequested.connect(self._startAuthoredWorkflow)
        self._externalMeshPage.returnExternalRequested.connect(self._returnExternalMesh)

        for step in range(Step.GEOMETRY, Step.CASTELLATION):
            self._pages[step].stepCompleted.connect(self._buttons.enableNextButton)
            self._pages[step].stepReset.connect(self._buttons.disableNextButton)

        for step in range(Step.CASTELLATION, Step.EXPORT):
            self._pages[step].stepCompleted.connect(self._onIntermediateStepCompleted)
            self._pages[step].stepReset.connect(lambda: self._buttons.showButton(ButtonID.FINISH))

        self._pages[Step.GEOMETRY].geometryRemoved.connect(self._geometryRemoved)

        # R86. The task rows paint from a graph snapshot the engine branch
        # holds in memory, and only the branch's own buttons refreshed it. A
        # page that moved a task through the facade left the tree showing the
        # state before the change: after Base Grid's Reset the store read
        # `configured` with every later stage `stale`, while four checkmarks
        # stayed on screen -- MEASURED on venturi.stl, and surviving a
        # navigate-away-and-back, because nothing re-read the file. Both
        # signals every StepPage already emits now say so.
        for page in self._pages.values():
            page.stepReset.connect(self._refreshBranch)
            page.stepCompleted.connect(self._refreshBranch)

    def _beginRoute(self) -> int:
        """Claim the content panel for one navigation, and say which (R149)."""
        self._routeGeneration += 1
        return self._routeGeneration

    def _routeSuperseded(self, generation: int) -> bool:
        """Whether a later navigation has taken the panel over (R149)."""
        return self._routeGeneration != generation

    def _isWorkingStep(self, step):
        return step == self._workingStep

    def _legacyPageFor(self, widget):
        """The StepPage that owns ``widget``, and the step it sits at."""
        for step, page in self._pages.items():
            if getattr(page, '_widget', None) is widget:
                return step, page
        return None, None

    @qasync.asyncSlot(object)
    async def _showBranchPage(self, widget):
        if self._externalMeshMode or not self._ui.navigation.isEnabled():
            return
        # A snappy task node routes to the Designer widget that a legacy
        # StepPage owns. Raising that widget is not the same as opening the
        # page: `show()` is what loads it. Without this, Base Grid arrived
        # with its entire Grid Span still reading the designer's "TextLabel",
        # no domain outline drawn, and every field showing a constructor
        # default rather than what the case holds.
        #
        # The mirror-image `hide()` is deliberately *not* called here. On a
        # task node the page's own action button is what saves -- and a blind
        # save on the way out writes the task's fields, which `configure()`
        # answers by moving a PASSED task to CONFIGURED and re-locking
        # everything downstream. Merely looking at a finished Base Grid would
        # have un-finished it.
        generation = self._beginRoute()
        step, page = self._legacyPageFor(widget)
        self._sceneVisible = False
        self._contentStack.show()
        if page is not None:
            # H8. `show()` gates the page's own action button on "is this the
            # working step", and branch navigation never advances
            # `_workingStep` -- so Snap arrived through its own tree node with
            # its Snap button greyed out, and the panel had no way to run the
            # stage it exists to run. Routing to a task node is the act of
            # standing on it.
            await page.show(not self._batchRunning, self._batchRunning)
            if self._routeSuperseded(generation):
                return
        self._branchPage = page
        self._branchWidget = widget
        self._updatePrerequisiteBanner(step)

        self._contentStack.setCurrentWidget(widget)
        self._buttons.hideAll()
        self._rememberPage(widget, self._methodBranch.currentToken)

    def _showScene(self):
        """Show the scene and display controls.

        Deliberately reachable in external-mesh mode: an opened mesh *is* a
        scene, and this is the only route to the actor tree, the section plane
        and the quality controls. Bailing out here used to leave a loaded
        polyMesh with no way to inspect it at all.
        """
        if not self._externalMeshMode and not self._ui.navigation.isEnabled():
            return
        # Synchronous, but it still has to take the panel away from any
        # awaiting navigation, or that one lands on top of the scene (R149).
        self._beginRoute()
        self._sceneVisible = True
        self._contentStack.show()
        self._contentStack.setCurrentWidget(self._scenePage)
        self._buttons.hideAll()
        self._rememberPage(self._scenePage)

    def _showWorkflowStep(self, step):
        if self._externalMeshMode:
            return
        self._sceneVisible = False
        self._contentStack.show()
        self._contentStack.setCurrentIndex(step)
        current_widget = (
            self._contentStack.currentWidget()
            if hasattr(self._contentStack, 'currentWidget')
            else getattr(self._pages[step], '_widget', None)
        )
        self._rememberPage(current_widget)
        self._updateControlButtons(step)

    @qasync.asyncSlot()
    async def _showExternalMesh(self):
        if not self._externalMeshMode or app.window.meshManager is None:
            return
        await app.window.meshManager.load(0)
        # "Show mesh" means show the mesh *and* what you need to look at it.
        self._showScene()

    def _showExternalMeshInfo(self):
        app.window.showMeshInfo()

    def _startAuthoredWorkflow(self):
        # The retained mesh is snapshotted by the transition service before
        # authored navigation becomes available.
        confirmation = QMessageBox.question(
            app.window, self.tr('Start Meshing Workflow'),
            self.tr('The external mesh will be retained as a recovery copy. '
                    'A future generated-mesh run may replace the current mesh. Continue?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        # C31-12. Scheduled: this copies the external polyMesh aside before it
        # answers, so it was one of the longer freezes in the window. The
        # resolution refresh and the reload still happen after it, and only
        # when it succeeded, exactly as the `try` did.
        def started(result):
            if isinstance(result, FailedResult):
                self._ui.statusbar.showMessage(result.message, 8000)
                return
            app.refreshWorkflowResolution()
            self.load()

        submit(app.facadeClient, 'workflow.start_authored', {}, then=started)

    def _returnExternalMesh(self):
        confirmation = QMessageBox.question(
            app.window, self.tr('Return to External Mesh'),
            self.tr('Restore the retained external polyMesh and suspend authored navigation?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        # C31-12. Scheduled for the same reason as the transition above: it
        # restores a retained polyMesh from disk before it answers.
        def returned(result):
            if isinstance(result, FailedResult):
                self._ui.statusbar.showMessage(result.message, 8000)
                return
            app.refreshWorkflowResolution()
            self.load()

        submit(app.facadeClient, 'workflow.return_to_external', {},
               then=returned)

    def _setWorkingStep(self, step):
        if self._externalMeshMode:
            return
        self._pages[step].unlock()
        self._navigation.setWorkingStep(step)
        self._workingStep = step

        db = app.facadeClient.checkout()
        # Only write when the step actually moves. An unconditional commit is a
        # closed loop: the window refreshes on TRANSACTION_APPLIED, its refresh
        # calls load(), and load() lands here -- so re-recording the step the
        # case is already on re-entered the same refresh many times a second.
        if int(db.getEnum('step')) != int(step):
            db.setValue('step', step)
            # C31-12. Scheduled. This is reached from `load()`, which runs on
            # every applied transaction, so a blocking commit here put a
            # facade round trip inside the window's own refresh.
            submit(app.facadeClient, 'configuration.commit_working_copy',
                   {'working_copy': db, 'action': 'advance workflow step',
                    'reason': None, 'target': None})

        self.workingStepChanged.emit(step)

    def _open(self, step):
        if self._externalMeshMode:
            return
        self._pages[step].open()
        self._navigation.setCurrentStep(step)
        self._syncOutlineToStep(step)
        # R93. `setCurrentStep` moves the outline synchronously and leaves the
        # panel to `_moveToStep`, which is an async slot with several early
        # returns. Opening a case from File > Open Recent painted the outline
        # with every step and the title bar with the case name while the panel
        # still read "No Case Open" and told the user to create one -- until
        # any outline row was clicked. Whether a case is open is not a
        # navigation decision, so the empty-case page goes as soon as one is.
        if self._contentStack.currentWidget() is self._emptyCasePage:
            self._contentStack.setCurrentIndex(int(step))
        self._setWorkingStep(step)

    def _syncOutlineToStep(self, step) -> None:
        """Point the outline at the row whose page is now on screen.

        R53. Only Geometry and Repair have top-level rows; every later step
        page is reached through the engine task that owns it, and
        `NavigationView.setCurrentStep` looks the step up in that top-level
        table. So moving the panel to Base Grid moved nothing in the outline
        and the highlight stayed on the row before it -- MEASURED as the
        outline reading `3. Region` beside a panel titled `Base Grid`. The
        outline is the only map of where the user is, so it names the page
        that is actually showing.

        `setBranchCurrent` answers False for a token the current engine does
        not publish, which is the right outcome: on Gmsh there is no snappy
        task row to stand on.
        """
        task_id = self._SNAPPY_STEP_TASKS.get(step)
        if task_id is None and step == Step.EXPORT:
            task_id = 'common.export'
        if task_id is None:
            return
        setCurrent = getattr(self._navigation, 'setBranchCurrent', None)
        if setCurrent is not None:
            setCurrent(TASK_TOKEN_PREFIX + task_id)

    @qasync.asyncSlot()
    async def _moveToStep(self, step, prev):
        if self._externalMeshMode:
            return
        generation = self._beginRoute()
        if step == prev:
            if self._sceneVisible:
                self._showWorkflowStep(step)
                return
            if self._contentStack.currentIndex() == step:
                return
            # F13. The content stack only ever moved off the empty-case page
            # as a side effect of a *step change*. Re-opening a case at the
            # step it was left on -- what Save-As does when the copy is
            # opened -- is not a change, so this returned early and the pane
            # still read "No Case Open" while the title bar named the case
            # and the outline listed every finished task. Showing the page is
            # this method's job whether or not the step moved.
            page = self._pages[step]
            await page.show(self._isWorkingStep(step), self._batchRunning)
            if self._routeSuperseded(generation):
                return
            if (step < self._workingStep or self._batchRunning
                    or app.facadeClient.session().jobs.has_mutating_job):
                page.lock()
            else:
                page.unlock()
            self._contentStack.setCurrentIndex(step)
            self._syncOutlineToStep(step)
            self._rememberPage(self._contentStack.currentWidget())
            self._updateControlButtons(step)
            self._updatePrerequisiteBanner(step)
            self.displayStepChanged.emit(step)
            return

        self._sceneVisible = False
        hidden = await self._pages[prev].hide()
        if self._routeSuperseded(generation):
            return
        if not hidden:
            self._navigation.setCurrentStep(prev)
            return

        page = self._pages[step]
        await page.show(self._isWorkingStep(step), self._batchRunning)
        if self._routeSuperseded(generation):
            return
        if (step < self._workingStep or self._batchRunning
                or app.facadeClient.session().jobs.has_mutating_job):
            page.lock()
        else:
            page.unlock()

        self._contentStack.setCurrentIndex(step)
        self._syncOutlineToStep(step)
        current_widget = (
            self._contentStack.currentWidget()
            if hasattr(self._contentStack, 'currentWidget')
            else getattr(self._pages[step], '_widget', None)
        )
        self._rememberPage(current_widget)

        self._updateControlButtons(step)
        self._updatePrerequisiteBanner(step)
        self.displayStepChanged.emit(step)

    def _rememberPage(self, widget, token=None):
        """Record where the panel now stands, as a route (R195).

        `token` is the branch task this widget is showing, when it is
        showing one. Without it two different tasks are the same history
        entry, because they are the same widget.
        """
        if widget is None:
            return
        entry = (widget, token or None)
        if not self._routeHistory or self._routeHistory[-1] != entry:
            self._routeHistory.append(entry)
        self._updateWizardActions()

    def _updateWizardActions(self):
        if not hasattr(self._ui, 'wizardBackButton'):
            return
        self._ui.wizardBackButton.setEnabled(len(self._routeHistory) > 1)
        enabled = (
            not self._batchRunning and not self._externalMeshMode
            and self._contentStack.isEnabled())
        self._ui.wizardProceedButton.setEnabled(enabled)
        token = self._methodBranch.currentToken
        if token.endswith('common.export'):
            # Plan 28 WP3. It used to say "Run & Proceed" and mean it: the
            # last row of the wizard re-ran the entire mesh. What this row
            # does is write a file.
            self._ui.wizardProceedButton.setText(self.tr('Export'))
            # R50. It was live on a case with no geometry, no region and no
            # mesh, where it can only lead to a failure or an empty case
            # directory. The page knows whether there is a mesh to write.
            canExport = getattr(self._pages.get(Step.EXPORT), 'canExport', None)
            if canExport is not None:
                self._ui.wizardProceedButton.setEnabled(enabled and canExport())
            return
        if not self._proceedRuns(token):
            self._ui.wizardProceedButton.setText(self.tr('Proceed'))
            self._ui.wizardProceedButton.setToolTip(self.tr(
                'Save this task and open the next one.'))
            return
        # Plan 30 WP-09, §7.1. There is one forward idiom now. This button
        # used to invent two more of its own -- `Run pipeline && Proceed` and
        # `Run && Proceed` -- for the same act the task page calls "Run this
        # step", so the same action had three names depending on where the
        # user's eye landed. The three the bottom bar may say are exactly
        # `Run this step`, `Proceed` and `Export`; what else the press does is
        # in the tooltip, not in a fourth label.
        self._ui.wizardProceedButton.setText(self.tr('Run this step'))
        if self._proceedRunsPipeline(token):
            # A10. One press of this ran compute, publish and QA. It still
            # does -- that is what the task is -- and it says so here.
            self._ui.wizardProceedButton.setToolTip(self.tr(
                'Runs every remaining stage of this engine, not just this '
                'one, and then opens the next task.'))
            return
        self._ui.wizardProceedButton.setToolTip(self.tr(
            'Runs this task, then opens the next one.'))

    def _proceedWillRun(self) -> bool:
        """Whether pressing Proceed now runs a utility rather than just saving.

        Only a Proceed that produces something has to have somewhere to put
        it; a Proceed that opens the next form does not (H10, H11).
        """
        widget = self._contentStack.currentWidget()
        branch = self._methodBranch.branch
        if branch is not None and widget is branch:
            return self._proceedRuns(self._methodBranch.currentToken)
        step, _page = self._legacyPageFor(widget)
        if step is None:
            return False
        if step == Step.EXPORT:
            return True
        task_id = self._SNAPPY_STEP_TASKS.get(step)
        if (task_id is None or branch is None or branch.engine_id != 'snappy'
                or task_id == 'snappy.domain_regions'
                or (branch.is_accepted(task_id)
                    and not self._skipIsContradicted(branch, task_id))):
            return False
        if task_id == 'snappy.layers' and not self._layersConfigured():
            return False
        return True

    @staticmethod
    def _repairIsPrepared(page) -> bool:
        """Whether a prepared geometry exists for the meshers to read (R203).

        The page answers this for the outline already. A page that cannot
        answer -- no facade session, a readiness call that raises -- does not
        get to block the wizard; the run refuses on its own and says why.
        """
        ask = getattr(page, 'isNextStepAvailable', None)
        if ask is None:
            return True
        try:
            return bool(ask())
        except Exception:
            return True

    async def _requireSavedCaseBeforeProceed(self) -> bool:
        """Settle where the case lives before Proceed runs anything (H10)."""
        if not app.isScratchCase() or not self._proceedWillRun():
            return True
        return await app.window._requireSavedCase(self.tr('meshing'))

    def _proceedRunsPipeline(self, token: str) -> bool:
        """Whether Proceed on this task runs the whole rest of the engine."""
        branch = self._methodBranch.branch
        if branch is None or not token.startswith(TASK_TOKEN_PREFIX):
            return False
        task_id = token[len(TASK_TOKEN_PREFIX):]
        page = branch.page(task_id)
        return getattr(page, 'run_all_task_id', None) == task_id

    def _proceedRuns(self, token: str) -> bool:
        """Whether Proceed on this task has to run something before moving on."""
        branch = self._methodBranch.branch
        if branch is None or not token.startswith(TASK_TOKEN_PREFIX):
            return False
        task_id = token[len(TASK_TOKEN_PREFIX):]
        if branch.is_accepted(task_id) and not self._skipIsContradicted(
                branch, task_id):
            return False
        # R181. `_proceedWillRun` already knew this row will not run, and the
        # label asked a second method that did not -- so the button read
        # `Run & Proceed` on a task the settle path was about to skip. Two
        # answers to the same question is how a button comes to promise
        # something the code below it never intended to do.
        if task_id == 'snappy.layers' and not self._layersConfigured():
            return False
        from foammesh.core.facade.domain_operations import CHECK_TASK_OPERATIONS
        return (task_id in CHECK_TASK_OPERATIONS
                or bool(branch.task_info(task_id).get('run_gated')))

    def _wizardBack(self):
        """Return to the preceding visited page without touching Region C."""
        if len(self._routeHistory) < 2:
            return
        self._routeHistory.pop()
        widget, token = self._routeHistory[-1]
        if token:
            # R195. Back into an engine task goes through the door the
            # outline uses. Raising the branch widget on its own left the
            # branch standing on the task the user had just left, the
            # outline highlighting it, and the panel showing something
            # else -- and Proceed then answered for whichever of the two
            # it asked. Routing re-records the entry, so drop it first.
            self._routeHistory.pop()
            if self._methodBranch.route(token):
                self._updateWizardActions()
                return
            self._routeHistory.append((widget, token))
        self._contentStack.setCurrentWidget(widget)
        self._updateWizardActions()

    @qasync.asyncSlot()
    async def _wizardProceed(self):
        """Settle the current task, then advance to the next one that is open.

        Settling is what was missing. Every task starts locked until its
        prerequisites are accepted, and Proceed only ever saved the page: the
        next row never unlocked and the button silently did nothing, for
        both engines. Now a manual task is accepted, a check task is run and
        a run-gated task is run -- and only a task that came out of that
        accepted lets the wizard move on.
        """
        if self._batchRunning or self._externalMeshMode:
            return
        # H10. The save this app demands before it will mesh used to be asked
        # for from inside the run -- after this method had already taken hold
        # of the branch widget, the task page and the task id. Saving relocates
        # the case and re-opens it, which rebuilds all three, so the settle
        # that followed was answered against dead objects: it failed, nothing
        # visible happened, and the click had to be made a second time. Ask
        # before anything is captured, and the first click is the one that
        # runs.
        if not await self._requireSavedCaseBeforeProceed():
            return
        widget = self._contentStack.currentWidget()
        method_page = self._methodBranch.methodPage
        if widget is method_page:
            checked = method_page._group.checkedButton()
            if checked is None or not checked.isEnabled():
                self._ui.statusbar.showMessage(
                    self.tr('Select an available meshing method first.'), 5000)
                return
            method_page._apply_selection()
            return

        if widget is self._scenePage:
            self._open(Step.GEOMETRY_REPAIR)
            return

        page_by_widget = {
            getattr(page, '_widget', None): (step, page)
            for step, page in self._pages.items()
        }
        mapped = page_by_widget.get(widget)
        if mapped is not None:
            step, page = mapped
            if not await page.save():
                self._ui.statusbar.showMessage(
                    self.tr('Resolve the validation errors before proceeding.'),
                    5000)
                return
            if step == Step.GEOMETRY:
                self._open(Step.GEOMETRY_REPAIR)
                return
            if step == Step.GEOMETRY_REPAIR:
                # R203. MEASURED on tee_gmsh_r2: Proceed walked off this page
                # having prepared nothing, nine tasks ran green on the
                # unprepared geometry, and Compute Mesh was then refused with
                # "prepare the geometry before running Gmsh" -- a sentence
                # that named no page and lived for ten seconds in the status
                # bar. The engines read PreparedGeometryStore.current(), which
                # only the Repair tabs write; the page already knows whether
                # one exists, and Proceed never asked it. Preparing on the
                # user's behalf is not the fix: accepting geometry the checks
                # call blocked is a decision with a written reason attached.
                if not self._repairIsPrepared(page):
                    self._ui.statusbar.showMessage(self.tr(
                        'Prepare the geometry before proceeding - the meshers '
                        'read the prepared revision, not the import. Apply a '
                        'repair plan, wrap it, or accept it on the "Use '
                        'as-is" tab.'), 12000)
                    return
                self._navigation.requestBranch(METHOD_TOKEN)
                return
            if self._engineId() == 'unselected':
                # R52. Same walk as `openNextStep`, on the control that
                # actually survives on this shell. MEASURED: Proceed on Region
                # with no engine chosen settled nothing, found no next task
                # (the facade publishes an empty task list until a method is
                # picked) and reported "Every task in this pipeline has been
                # addressed" -- on a case where none of them had been. Send
                # the user to the one question that unblocks the rest instead.
                self._navigation.requestBranch(METHOD_TOKEN)
                self._ui.statusbar.showMessage(
                    self.tr('Choose a meshing method first - the rest of the '
                            'workflow belongs to one.'), 5000)
                return
            if not await self._settleLegacyTask(step, page):
                self._updateWizardActions()
                return
            self._methodBranch._syncChildren()
            # A7. `currentToken` is whatever branch node was last routed to,
            # and a legacy step page reached through the outline never routes
            # one -- so Proceed resolved "the next task" against a stale row
            # and re-opened work the user had already finished. The step knows
            # which task it stands for; ask that.
            own_task = self._SNAPPY_STEP_TASKS.get(step)
            token = (TASK_TOKEN_PREFIX + own_task if own_task
                     else self._methodBranch.currentToken)
            next_token = self._methodBranch.nextAvailableTaskToken(token)
            if next_token:
                self._navigation.requestBranch(next_token)
            elif step == Step.EXPORT:
                await self._proceedFromExport()
            else:
                self._reportNoNextTask()
            return

        branch = self._methodBranch.branch
        if widget is branch and branch is not None:
            task_page = branch.stack.currentWidget()
            if getattr(task_page, 'is_dirty', False):
                task_page.apply()
                if getattr(task_page, 'is_dirty', False):
                    return
            task_id = self._taskIdForToken(
                self._methodBranch.currentToken, task_page)
            if task_id == 'common.export':
                # Plan 30 WP-09. Export is a branch task on both engines now,
                # so its Proceed comes through here rather than through the
                # legacy step path -- and it writes a file, it does not mesh.
                await self._proceedFromExport()
                self._updateWizardActions()
                return
            if task_id and not await self._settleTask(branch, task_page, task_id):
                self._updateWizardActions()
                return
            self._methodBranch._syncChildren()
            token = self._methodBranch.currentToken
            next_token = self._methodBranch.nextAvailableTaskToken(token)
            if next_token:
                self._navigation.requestBranch(next_token)
                return
            self._reportNoNextTask()
            self._updateWizardActions()

    def _reportNoNextTask(self) -> None:
        """Say why Proceed stopped, and offer the row that is in the way.

        A8. When the next task's dependency was unaddressed, Proceed did
        nothing at all -- no dialog, no status line, no outline change -- so a
        blocked button and a dead button were indistinguishable.
        """
        blocked = self._methodBranch.firstBlockedTask()
        if blocked is None:
            self._ui.statusbar.showMessage(
                self.tr('Every task in this pipeline has been addressed.'),
                5000)
            return
        token, label = blocked
        answer = QMessageBox.question(
            app.window, self.tr('Nothing to proceed to'),
            self.tr('"{0}" is still locked, so there is no next task to '
                    'open.\n\nOpen it anyway to see what it is waiting '
                    'for?').format(label),
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Open)
        if answer == QMessageBox.StandardButton.Open and token:
            self._navigation.requestBranch(token)

    def _reportBlockedTask(self, branch, task_id: str) -> None:
        """Name the prerequisite that is holding this task shut (A8)."""
        titles = self._methodBranch.taskTitles()
        info = branch.task_info(task_id) or {}
        blocking = [dep for dep in (info.get('depends_on') or ())
                    if not branch.is_accepted(dep)]
        if not blocking:
            self._ui.statusbar.showMessage(
                self.tr('Finish the earlier tasks before this one.'), 5000)
            return
        names = ', '.join(titles.get(dep, dep) for dep in blocking)
        answer = QMessageBox.question(
            app.window, self.tr('Earlier task unfinished'),
            self.tr('"{0}" is waiting on {1}.\n\nOpen the first one now?')
            .format(titles.get(task_id, task_id), names),
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Open)
        if answer == QMessageBox.StandardButton.Open:
            self._navigation.requestBranch(TASK_TOKEN_PREFIX + blocking[0])

    #: Legacy snappy step pages and the tree task each one owns.
    _SNAPPY_STEP_TASKS = {
        Step.REGION: 'snappy.domain_regions',
        Step.BASE_GRID: 'snappy.base_grid',
        Step.CASTELLATION: 'snappy.castellation',
        Step.SNAP: 'snappy.snap',
        Step.BOUNDARY_LAYER: 'snappy.layers',
    }

    @staticmethod
    def _taskIdForToken(token: str, task_page) -> str | None:
        if token.startswith(TASK_TOKEN_PREFIX):
            return token[len(TASK_TOKEN_PREFIX):]
        return getattr(task_page, 'task_id', None)

    async def _runEnginePipeline(self, engine_id: str) -> None:
        if engine_id in ENGINE_REGISTRY.ids():
            await self._finishPipeline(engine_id)

    def _reportSkippedTask(self, task_id: str) -> None:
        """Say that a stage was skipped rather than run (R181).

        Skipping layers nobody configured is the right call, but the press
        that did it came from a button and the row it leaves behind is a thin
        dash in the outline that reads as a tick at a glance. MEASURED on
        tee_snappy_v2: Proceed advanced from Boundary Layers to Geometry
        Fidelity with the cell count unchanged at 15,197, no layers log and
        `snappy.layers: skipped` on disk -- and nothing on screen said so. A
        user who wanted layers and simply did not notice the empty
        Configurations table would ship a layerless mesh believing otherwise.
        """
        title = task_id
        branch = self._methodBranch.branch
        if branch is not None:
            title = str(branch.task_info(task_id).get('title') or task_id)
        self._ui.statusbar.showMessage(
            self.tr('{0} was skipped -- nothing is configured for it, so '
                    'nothing ran.').format(title), 8000)

    async def _settleTask(self, branch, task_page, task_id: str) -> bool:
        """Bring an engine-branch task to an accepted state, or say why not."""
        from foammesh.core.facade.domain_operations import CHECK_TASK_OPERATIONS

        if branch.is_accepted(task_id) and not self._skipIsContradicted(
                branch, task_id):
            return True
        if not branch.is_runnable(task_id):
            self._reportBlockedTask(branch, task_id)
            return False
        info = branch.task_info(task_id)
        if task_id in CHECK_TASK_OPERATIONS:
            branch._on_task_transition(task_id, 'run')
        elif str(info.get('engine_stage') or '') == 'checkMesh':
            # The QA row. It used to be hand-accepted here, which passed it
            # on whatever report happened to exist -- on the live snappy walk
            # the one from the base grid, three mutating stages earlier.
            await branch.run_mesh_check_async(task_id)
        elif info.get('run_gated'):
            stage = getattr(task_page, 'run_stage', None)
            if getattr(task_page, 'run_all_task_id', None) == task_id:
                await self._runEnginePipeline(branch.engine_id)
            elif stage:
                await branch.run_stage_async(task_id, stage)
            elif str(info.get('cardinality') or '').endswith('optional'):
                branch._on_task_transition(task_id, 'skip')
                self._reportSkippedTask(task_id)
            else:
                # Plan 30 WP-09 / F-23: this used to click the task page's
                # hidden `Run complete mesh` button, which was never visible,
                # so the branch was unreachable in both senses.
                self._ui.statusbar.showMessage(
                    self.tr('Run this task, then proceed once it has finished.'),
                    5000)
                return False
        else:
            branch._on_task_transition(task_id, 'accept')
        branch.refresh_states()
        return branch.is_accepted(task_id)

    async def _settleLegacyTask(self, step, page) -> bool:
        """Advance the tree task a legacy snappy step page stands for.

        The legacy pages never sent a transition, so twelve of thirteen snappy
        rows stayed grey however far the user got. Region settings are a
        manual task and are accepted; the stage pages run their stage, and
        the facade records what ran.
        """
        task_id = self._SNAPPY_STEP_TASKS.get(step)
        branch = self._methodBranch.branch
        if task_id is None or branch is None or branch.engine_id != 'snappy':
            return True
        if branch.is_accepted(task_id) and not self._skipIsContradicted(
                branch, task_id):
            return True
        if not branch.is_runnable(task_id):
            self._reportBlockedTask(branch, task_id)
            return False
        if task_id == 'snappy.domain_regions':
            branch._on_task_transition(task_id, 'accept')
        elif task_id == 'snappy.layers' and not self._layersConfigured():
            branch._on_task_transition(task_id, 'skip')
            self._reportSkippedTask(task_id)
        else:
            runner = getattr(page, 'runInBatchMode', None)
            if runner is None:
                self._ui.statusbar.showMessage(
                    self.tr('Run this step, then proceed once it has finished.'),
                    5000)
                return False
            if not await runner():
                return False
        branch.refresh_states()
        return branch.is_accepted(task_id)

    def exportPage(self):
        """The object that owns the format chooser and the export writers.

        Plan 30 WP-09. `common.export` is a registry page in both engine
        branches now; the writers are not duplicated onto it, they are called
        here, so the app keeps one implementation of "which operation writes
        which format" and one set of dialogs.
        """
        return self._pages.get(Step.EXPORT)

    async def _proceedFromExport(self) -> bool:
        """Proceed on the Export row exports. It does not re-mesh.

        This row used to fall through to ``_finishSteps``, which runs the
        selected engine's pipeline from the top -- so the last click of the
        wizard re-ran the mesh the user had just spent minutes producing,
        and still wrote no file. The row asks for one thing, so Proceed opens
        the format chooser and runs the writer that was picked.

        Accepting ``common.export`` is what completes the workflow, and it is
        only claimed when an export really succeeded.
        """
        page = self._pages[Step.EXPORT]
        opener = getattr(page, 'openExportDialog', None)
        if opener is None:                                # pragma: no cover
            return False
        exported = await opener()
        if not exported:
            return False
        branch = self._methodBranch.branch
        if branch is not None and not branch.is_accepted('common.export'):
            branch._on_task_transition('common.export', 'accept')
            branch.refresh_states()
            self._methodBranch._syncChildren()
        self._updateWizardActions()
        return True

    def _acceptExportTask(self) -> None:
        """A finished export finishes the Export task (R104/R162).

        Measured on both engines: the dialog wrote a complete case and said
        "Export completed", and the Export row stayed grey -- the sixteen-task
        workflow had no reachable finished state. Only the wizard's own
        Proceed ever accepted the row, and pressing that re-opened the dialog
        with Project Name blank and Project Location back at the home folder,
        inviting a second copy in the wrong place. The page now says when a
        file has been written, whichever button asked for it.
        """
        branch = getattr(self._methodBranch, 'branch', None)
        if branch is None or branch.is_accepted('common.export'):
            return
        branch._on_task_transition('common.export', 'accept')
        branch.refresh_states()
        self._methodBranch._syncChildren()
        self._updateWizardActions()

    def _skipIsContradicted(self, branch, task_id: str) -> bool:
        """Whether a skipped task now carries the configuration it lacked.

        R185. A skip is an answer to "is there anything to do here?", and it
        is the right answer while nothing is configured. It stops being an
        answer the moment something is. MEASURED on the live tee: Boundary
        Layers was proceeded past unconfigured and recorded `skipped`; a layer
        group was then added on that same page and Proceed pressed again --
        and because `SKIPPED` counts as accepted, the settle path returned
        `True` on its first line, ran nothing, said nothing, and moved on. The
        user configured a stage, asked for it, and was silently given the
        earlier refusal back.

        `is_accepted` is right everywhere else it is asked: a skipped optional
        task must not lock the rows behind it, must not hold up Export, and
        must not be re-asked on every repaint. The one place it is the wrong
        question is here, where the user has just pressed the button that
        means "do this task".
        """
        if branch is None or branch.task_state(task_id) != 'skipped':
            return False
        return task_id == 'snappy.layers' and self._layersConfigured()

    def _layersConfigured(self) -> bool:
        try:
            db = app.facadeClient.checkout()
            return bool(db.getElements('addLayers/layers'))
        except Exception:  # noqa: BLE001 - no layer table means no layers
            return False

    def _showPipelineVerdict(self, payload: dict) -> None:
        verdict = (payload or {}).get('quality_verdict')
        show = getattr(getattr(app, 'window', None), 'showMeshVerdict', None)
        if verdict and callable(show):
            # Plan 31 CP-05 item 4. Carries which candidate the verdict is
            # about, so accepting it does not have to re-mesh to find one.
            show(dict(verdict, run_id=str((payload or {}).get('run_id') or '')))

    def _refreshBranch(self) -> None:
        branch = self._methodBranch.branch
        if branch is not None and hasattr(branch, 'refresh_states'):
            branch.refresh_states()
        self._methodBranch._syncChildren()
        self._updateWizardActions()

    def _snappyDomainBounds(self) -> list:
        """The base-grid domain, from the page that owns it."""
        page = self._pages.get(Step.BASE_GRID) if hasattr(self._pages, 'get') else None
        bounds = page.boundingBox() if hasattr(page, 'boundingBox') else None
        if bounds is None:
            bounds = app.window.geometryManager.getBounds().toTuple()
        values = [float(value) for value in bounds]
        if (len(values) != 6
                or any(math.isnan(v) or math.isinf(v) for v in values)
                or any(values[i + 1] <= values[i] for i in (0, 2, 4))):
            raise ValueError(self.tr(
                'The base grid has no valid domain. Set it on the Base Grid page.'))
        return values

    def _onWizardMethodAccepted(self, _engine_id: str):
        self._methodBranch._syncChildren()
        token = self._methodBranch.firstAvailableTaskToken()
        if token:
            self._navigation.requestBranch(token)

    @qasync.asyncSlot()
    async def _finishSteps(self):
        if self._externalMeshMode:
            return
        if self._batchRunning:
            return

        # Commit any uncommitted UI state on the current (working) page first.
        # The load() call inside the loop checks out a fresh self._db from app.db,
        # which would otherwise drop refinements/layers the user just added via a
        # dialog but hasn't saved yet — leaving zombie list items pointing at
        # keys that no longer exist in self._db.
        if not await self._pages[self._workingStep].save():
            return
        try:
            selected_engine = app.facadeClient.checkout().getValue(
                'mesh/engine')
            selected_engine = getattr(
                selected_engine, 'value', selected_engine)
        except Exception:
            selected_engine = None
        # The step loop below is the snappy page sequence. A Gmsh case used
        # to fall into it, run castellation pages against a mesh it did not
        # have, and leave `_batchRunning` set when that raised. Any engine the
        # build registers runs its own pipeline instead.
        if selected_engine in ENGINE_REGISTRY.ids():
            await self._finishPipeline(str(selected_engine))
            return

        self._batchRunning = True
        self._buttons.showButton(ButtonID.CANCEL)

        self.batchStarted.emit()

        try:
            while self._workingStep < Step.EXPORT:
                # Only change workingStep
                self._pages[self._workingStep].load()
                self._navigation.setWorkingStep(self._workingStep)

                if self._buttons.isCancelClicked() or not await self._pages[self._workingStep].runInBatchMode():
                    break

                self._workingStep += 1
            else:
                await AsyncMessageBox().information(self._contentStack, self.tr('Process Completed'),
                                                    self.tr('All steps complete.'))

            if self._workingStep > Step.LAST_STEP:
                self._workingStep = Step.LAST_STEP

            # Apply current workingStep
            self._setWorkingStep(self._workingStep)
        finally:
            self._batchRunning = False

        displayStep = self._navigation.currentStep()
        self._updateControlButtons(displayStep)
        if displayStep < self._workingStep:
            self.currentPage().lock()

        self.batchStopped.emit()

        self.currentPage().updateWorkingStatus()

    #: How each engine is named in a modal or a status line. An engine the
    #: build registers but nobody has named here is called by its own id
    #: rather than nothing at all.
    ENGINE_DISPLAY_NAMES = {'snappy': 'Snappy', 'gmsh': 'Gmsh'}

    def _engineDisplayName(self, engine_id: str) -> str:
        return self.tr(self.ENGINE_DISPLAY_NAMES.get(
            str(engine_id), str(engine_id) or 'The meshing engine'))

    async def _finishPipeline(self, engine_id: str) -> None:
        """Run this case's whole mesh and report it, whichever engine it is.

        Plan 30 F-03. There were two of these, one per engine, and they
        differed in three things: the operation they submitted, the name in
        the modal, and whether they generated dictionaries first. All three
        are now answers the seam gives -- one operation
        (``workflow.run_pipeline``, which dispatches on the registry), a name
        read off the handle the run hands back, and a flag the engine
        declares. Everything else -- the console subscription, the verdict
        strip, drawing the artifact this run produced, the failure report and
        the button state -- was duplicated line for line, which is how the two
        drifted: only one of them drew a refused mesh until R210, and only one
        of them reported a failure anywhere but the status bar until R204.
        """
        # An untitled case chooses its home before it produces a mesh: the
        # result would be real and the folder would not, and nothing would say
        # so until the temporary directory was swept.
        if not await app.window._requireSavedCase(self.tr('meshing')):
            return
        self._batchRunning = True
        self._buttons.showButton(ButtonID.CANCEL)
        self.batchStarted.emit()
        console = app.consoleView
        unsubscribe = None
        try:
            if self._needsGeneratedDictionaries(engine_id):
                await app.facadeClient.run(
                    'workflow.generate_dictionaries',
                    {'bbox': self._snappyDomainBounds()})
            unsubscribe = app.facadeClient.subscribe(
                Event.JOB_OUTPUT,
                lambda **payload: console.append(payload.get('line', '')))
            # Plan 31 CP-07 item 6. Say what this run is about to run on --
            # serial or parallel, on how many workers, and whether the CPU
            # ceiling cut the request -- before it starts, and put Cancel
            # beside the sentence. Until now the strip's Cancel had no caller
            # at all, and the only record of the split was the processor
            # directories the run left on disk.
            await self._announceRunPlan()
            result = await app.facadeClient.run(
                'workflow.run_pipeline', {
                    'timeout_seconds':
                        app.settings.getOpenFoamRuntime()['stage_timeout'],
                })
            payload = getattr(result, 'payload', {}) or {}
            self._showPipelineVerdict(payload)
            # R210. A run that built a mesh and then failed it on quality is
            # the run whose mesh most needs looking at, and it was the one run
            # that never drew it: the viewport kept the STL while the user was
            # asked to judge cells they could not see. A quality verdict in
            # the payload means a mesh exists to draw, whatever the verdict
            # says about it.
            #
            # F-37. *Which* mesh is the run's own question, not the viewport's
            # guess. snappyHexMesh mutates the case in place, so the handle
            # names `constant/polyMesh` for that engine whatever the verdict;
            # the Gmsh gate returns before the publication step, so a refused
            # run leaves `constant/polyMesh` holding whatever the last
            # *accepted* run published and the handle names this run's own
            # artifact instead. Either way the handle names it, rather than
            # the finisher assuming it.
            handle = self._runResult(payload, result, engine_id)
            name = self._engineDisplayName(handle.engine or engine_id)
            built = handle.produced_a_mesh
            # CP-02. A run that *ran* and produced nothing is also this
            # method's business: leaving the previous mesh on screen under
            # this run's verdict is F-37 whether the run was refused on
            # quality or died before it wrote anything. A refusal that
            # happened before any run started has no run id, and changes
            # nothing about the mesh on disk, so it leaves the viewport alone.
            if built or handle.run_id:
                await self._drawFinishedMesh(handle)
            if result.status == 'accepted':
                await AsyncMessageBox().information(
                    self._contentStack, self.tr('Process Completed'),
                    self.tr('The %s pipeline completed.') % name)
            else:
                await self._reportPipelineFailure(
                    name, str(payload.get('reason') or ''), built=built)
        except Exception as error:
            await self._reportPipelineFailure(
                self._engineDisplayName(engine_id), str(error))
        finally:
            if unsubscribe is not None:
                unsubscribe()
            self._batchRunning = False
            self._updateControlButtons(self._navigation.currentStep())
            self.batchStopped.emit()
            self._refreshBranch()

    async def _announceRunPlan(self) -> None:
        """Put the run's own plan on the status strip, with its Cancel.

        Read from the facade rather than re-derived here: the ceiling
        precedence is one rule, and a second copy of it in the view is how
        the dialog and ``decomposeParDict`` came to disagree.  A plan that
        cannot be read is not worth guessing at, so the strip says the
        neutral sentence instead of inventing a worker count.
        """
        window = getattr(app, 'window', None)
        announce = getattr(window, 'showRunStarted', None)
        if not callable(announce):
            return
        document = {}
        try:
            result = await app.facadeClient.run('mesh.execution.plan', {})
            document = getattr(result, 'payload', None) or {}
        except Exception:                                   # noqa: BLE001
            document = {}
        allocation = document.get('allocation') or {}
        message = describe_start(allocation)
        reason = str(document.get('reason') or '')
        if reason:
            message = f'{message} {reason[0].upper()}{reason[1:]}.'
        try:
            announce(message, allocation=allocation)
        except TypeError:
            announce(message)

    @staticmethod
    def _needsGeneratedDictionaries(engine_id: str) -> bool:
        """Whether this engine meshes from dictionaries somebody has to write.

        snappy does -- blockMeshDict, surfaceFeaturesDict and
        snappyHexMeshDict have to be on disk before its run starts. Gmsh
        writes its own job at the moment it runs, and generating snappy's
        dictionaries for it would fail on a case that has no bounding hex.
        """
        try:
            engine = ENGINE_REGISTRY.get(str(engine_id))
        except Exception:                                   # noqa: BLE001
            return False
        return bool(getattr(engine, 'needs_generated_dictionaries', False))

    @staticmethod
    def _runResult(payload: dict, result, engine: str) -> RunResultHandle:
        """The handle for the run that just finished (F-37)."""
        return RunResultHandle.from_payload(
            payload, status=getattr(result, 'status', ''),
            case_path=app.facadeClient.case_root, engine=engine)

    async def _drawFinishedMesh(self, handle: RunResultHandle) -> None:
        """Draw the artifact *this* run produced, and name it.

        A mesh that cannot be drawn does not turn a finished run into a failed
        one, but it is reported rather than discarded: six recorded runs ended
        with a mesh on disk, an empty viewport, and nothing anywhere saying why
        (R95/R156).

        F-37. The handle decides which case root the loader opens. A refused
        Gmsh candidate is not in the case root at all, and when nothing in its
        run directory can be read the previous mesh comes *off* the screen:
        showing one run's cells under another run's verdict is a worse failure
        than showing none, because nothing on screen says which is which.
        """
        manager = getattr(app.window, 'meshManager', None)
        loadResult = getattr(manager, 'loadResult', None)
        report = getattr(app.window, 'showRunResult', None)
        if report is not None:
            report(handle)
        if loadResult is None:
            return
        if not handle.drawable:
            self._clearAndOfferPrevious(manager, handle)
            return
        try:
            problem = await loadResult(handle)
        except Exception as error:                          # noqa: BLE001
            logger.warning('the mesh was written but could not be drawn',
                           exc_info=True)
            problem = str(error)
        if problem:
            # CP-02. The actual read failure, said out loud. An unreadable
            # artifact is not the same news as a run that made nothing, and
            # neither is the same as a mesh that is fine -- and all three used
            # to leave the same empty viewport.
            self._ui.statusbar.showMessage(
                self.tr('The mesh was written but could not be drawn: %s')
                % problem, 10000)
            status = getattr(app.window, 'showRunStatus', None)
            if status is not None:
                status(self.tr('%s · could not be drawn: %s')
                       % (handle.run_id or handle.engine, problem),
                       failed=True)

    def _clearAndOfferPrevious(self, manager, handle) -> None:
        """Take the failed run down, and name what the case still has.

        CP-02 item 6. Clearing is not optional: leaving the previous mesh up
        under this run's verdict is F-37. But the earlier accepted mesh is
        still a real result the user may want back, so it is offered by name
        and drawn only if asked for -- never restored silently, and never
        presented as this run's own.
        """
        from foammesh.core.run_result import accepted_result

        unload = getattr(manager, 'unload', None)
        if unload is not None:
            unload()
        message = (self.tr('No mesh for this run: %s left nothing this '
                           'viewport can read, so the viewport was cleared '
                           'rather than left showing an earlier run.')
                   % (handle.run_id or handle.engine))
        self._ui.statusbar.showMessage(message, 10000)
        previous = None
        try:
            previous = accepted_result(app.facadeClient.case_root,
                                       exclude_run=handle.run_id)
        except (OSError, AttributeError):
            logger.debug('earlier results could not be read', exc_info=True)
        offer = getattr(app.window, 'offerPreviousResult', None)
        if offer is not None:
            offer(message, previous)

    async def _reportPipelineFailure(self, engine: str, reason: str,
                                     *, built: bool = False) -> None:
        """Say a run did not finish, where the user is already looking (R204).

        MEASURED on tee_gmsh_r2: Compute Mesh refused because no prepared
        geometry existed, and the whole report was a status-bar line that
        expired in ten seconds. The Console sat open and empty beside it --
        empty because the refusal came before any job output, and the
        fallback sentence sent the user to read it. A finished run gets a
        modal; a run that never started was quieter than one that worked.
        """
        # R205. MEASURED on tee_gmsh_r2: Gmsh built 39,921 cells, the
        # quality limits rejected 42 of them, and the box that carried
        # that number was titled 'Gmsh pipeline did not run' -- printed in
        # the Console directly beneath '[100%] mesh complete'. Both
        # outcomes end in the same else-branch, so one sentence covered a
        # run that was refused and a mesh that was built and measured.
        # They are not the same news and do not lead to the same next
        # step: one asks the user to go back and prepare something, the
        # other to loosen a limit, refine, or accept with a reason.
        detail = reason.strip() or (
            self.tr('The mesh was measured and gave no reason.') if built
            else self.tr('The run was refused and gave no reason.'))
        headline = (self.tr('{0} mesh was not accepted') if built
                    else self.tr('{0} pipeline did not run')).format(engine)
        console = app.consoleView
        if console is not None:
            console.appendError('{0}: {1}'.format(headline, detail))
        self._ui.statusbar.showMessage(detail, 10000)
        await AsyncMessageBox().warning(
            self._contentStack, headline, detail)

    @qasync.asyncSlot()
    async def _cancelFinishSteps(self):
        await app.facadeClient.cancel_active_job()

    @qasync.asyncSlot()
    async def _unlockCurrentStep(self):
        if self._externalMeshMode:
            return
        currentStep = self._navigation.currentStep()

        try:
            for step in range(currentStep + 1, self._workingStep + 1):
                self._navigation.disableStep(step)
                self._pages[Step(step)].clearResult()
        except PermissionError:
            await AsyncMessageBox().information(
                self._contentStack,
                self.tr('Permission Error'),
                self.tr('Permission Error:\n'
                        'A file in the project folder might be open in another program.\n'
                        'Close the file and try again.'))

            return

        self._setWorkingStep(currentStep)

        # self._buttons.nextButton.setEnabled(True)
        # self._buttons.setToOpenedMode()
        self._updateControlButtons(currentStep)

    def _geometryRemoved(self):
        self._pages[Step.CASTELLATION].unload()
        self._pages[Step.BOUNDARY_LAYER].unload()

    def _onIntermediateStepCompleted(self):
        if self._navigation.currentStep() < Step.LAST_STEP:
            self._buttons.showButton(ButtonID.NEXT)

    def _updateControlButtons(self, step):
        if self._batchRunning or self._externalMeshMode:
            return

        if self._pages[step].isNextStepAvailable():
            if self._isWorkingStep(step):
                self._buttons.showButton(ButtonID.NEXT)
            else:
                self._buttons.showButton(ButtonID.UNLOCK)
        elif self._isWorkingStep(step) and step in (Step.CASTELLATION, Step.SNAP, Step.BOUNDARY_LAYER):
            self._buttons.showButton(ButtonID.FINISH)
        else:
            self._buttons.showButton(ButtonID.NEXT, False)
