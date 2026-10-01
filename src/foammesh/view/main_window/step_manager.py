#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import math
from contextlib import contextmanager
from enum import Enum, auto
from functools import partial

from PySide6.QtCore import QObject, Signal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QWidget

import qasync

from foammesh.app import app
from foammesh.core.case import ExternalMeshSummary, WorkflowMode
from foammesh.core.engine.registry import ENGINE_REGISTRY
from foammesh.core.naming import humanise_option
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
    DEFAULT_PROCEED, HOSTED_TASKS, METHOD_TOKEN, TASK_TOKEN_PREFIX,
    MeshingMethodBranch, row_tasks)
from widgets.async_message_box import AsyncMessageBox
from foammesh.view.facade_client import FailedResult, query, submit
from foammesh.view.outside_task import say

from .run_narration import describe_start


logger = logging.getLogger(__name__)


async def record_transition(branch, task_id: str, transition: str) -> None:
    """Send one lifecycle transition and wait for it to land.

    DP-253. `_on_task_transition` schedules the write and returns before the
    facade has taken it (C31-12), so every settle below it read the task
    state from before its own transition, answered "not accepted" and stopped
    the press without a word. MEASURED on the guided walk, `pipe` for Gmsh:
    the press on `3. Preparation` accepted the task hosted there, refused to
    move, said nothing, and the screenshot taken afterwards shows the row it
    would not open already unlocked -- the write landed just after the press
    gave up.

    A branch that offers no awaitable transition is a stand-in with no facade
    behind it, and `submit` runs the write synchronously there, so the state
    it reports next is already the new one.
    """
    recorder = getattr(branch, 'record_transition_async', None)
    if recorder is None:
        branch._on_task_transition(task_id, transition)
        return
    await recorder(task_id, transition)


async def commit_page_edits(page, branch=None) -> bool:
    """Write what the page is holding, and wait to be told whether it landed.

    DP-254. The press used to call `page.apply()` and read `is_dirty` on the
    very next line. `apply` is a button handler whose patch C31-12 made a
    scheduled write, so the flag it read was still the one from before its
    own press: the press returned in silence, on a page whose edits were
    about to be accepted. MEASURED on the guided walk, `pipe` for Gmsh: the
    press on `4. Global sizing`, with ten of ten fields authored by the walk,
    did nothing, said nothing and left the reader on that row.

    A page that offers `save` is awaited, because that is the same write with
    an answer (DP-227). A stand-in with only `apply` has no facade behind it,
    `submit` runs its write synchronously, and the flag it reports next is
    already the new one.

    Plan 37 UF3 DP-1034/DP-1035. Landing the patch is not the end of the
    save: the page announces it with `updateRequested`, and the branch
    answers with a scheduled `configure` that nobody awaited, so the settle
    after this read the task as still PASSED and moved on without meshing.
    The table rows the page hosts are writes of their own, also scheduled.
    So this waits for the page's row writes first (a refused one stops the
    press, the table has said why), then the page's own patch, then every
    transition those sent -- and only then does the press read a state.
    """
    settle_children = getattr(page, 'settle_child_writes', None)
    if settle_children is not None and not await settle_children():
        return False
    if getattr(page, 'is_dirty', False):
        saver = getattr(page, 'save', None)
        if saver is None:
            page.apply()
            if getattr(page, 'is_dirty', False):
                return False
        elif not await saver():
            return False
    landed = getattr(branch, 'transitions_landed', None)
    if landed is not None:
        await landed()
    return True



def _one_press_at_a_time(press):
    """Let a second press of the same control join the first one.

    Plan 37 UF3 DP-1034. DP-240's guard holds the controls dead once a run
    starts, but the save and the transitions before it are awaited too, and a
    second press in that window started a second save, a second configure and
    then a second run of the same stage. A press that arrives while one is in
    flight now waits for that one and does nothing of its own: it is the same
    request, made twice.
    """
    import asyncio
    import functools

    @functools.wraps(press)
    async def shared(self, *args, **kwargs):
        request = self.__dict__.get('_proceedRequest')
        if request is not None and not request.done():
            await asyncio.shield(request)
            return None
        request = asyncio.ensure_future(press(self, *args, **kwargs))
        self.__dict__['_proceedRequest'] = request
        try:
            return await request
        finally:
            if self.__dict__.get('_proceedRequest') is request:
                self.__dict__['_proceedRequest'] = None

    return shared


def unpublished_mesh_reason(payload: dict) -> str:
    """Why an accepted run left no ``constant/polyMesh``, or '' if it did not.

    DP-483. Only a publication that *failed* counts. A run that published
    nothing by design -- an SU2-only target -- records ``skipped`` and has
    nothing to apologise for.
    """
    publication = (payload or {}).get('publication') or {}
    if publication.get('status') != 'failed':
        return ''
    return str(publication.get('reason') or '').strip() or (
        'The mesh could not be written as an OpenFOAM polyMesh.')

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

    def _restoreCancel(self) -> None:
        """Put Cancel back to the word it had before it was pressed."""
        text = getattr(self, '_cancelText', None)
        if text is not None:
            self._buttons[ButtonID.CANCEL].setText(text)
            self._cancelText = None

    def showButton(self, id_, enabled=True):
        self._cancelClicked = False
        self._restoreCancel()

        for i, button in self._buttons.items():
            if i == id_ and i not in self._retired:
                button.show()
                button.setEnabled(enabled)
            else:
                button.hide()

    def hideAll(self):
        self._cancelClicked = False
        self._restoreCancel()
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
        # Plan 37 UF20 follow-up, MEASURED live (BUDGETS.md section 3): the
        # footer Cancel on a Gmsh run stayed enabled and nothing read
        # "cancelling" until the run had ended, 2.42 s later (budget 1 s).
        # Taken at once, and painted before the cancel is sent.
        button = self._buttons[ButtonID.CANCEL]
        if getattr(self, '_cancelText', None) is None:
            self._cancelText = button.text()
        button.setEnabled(False)
        button.setText(self.tr('Cancelling…'))
        if button.isVisible():
            button.repaint()
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
            self.tr('Guidance shown when no case is open'))
        self._emptyCasePage.setGuidance(
            getattr(ui, 'actionNew', None), getattr(ui, 'actionOpen', None),
            getattr(ui, 'actionLoadGeometry', None))
        self._contentStack.addWidget(self._emptyCasePage)

        self._batchRunning = False
        self._externalMeshMode = False
        # DP-464. Which step the content panel is actually showing, so that a
        # lock taken on the way in can be reconsidered without navigating.
        self._displayedStep = None
        # DP-240. Whether a press of Proceed that runs a stage is in flight.
        self._proceedBusy = False
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
        # DP-256. A single stage run from a branch row is the same run as a
        # single stage run from the legacy page, and only the legacy page
        # wrote the dictionaries it meshes from. The rule for which engines
        # need them and the domain they are written over both live here, so
        # the branch is handed the finished act rather than a second copy of
        # the rule.
        self._methodBranch.stage_dictionaries = {
            engine_id: self._generateDictionaries
            for engine_id in ENGINE_REGISTRY.ids()
            if self._needsGeneratedDictionaries(engine_id)
        }
        self._methodBranch.pageRequested.connect(self._showBranchPage)
        # DP-1084. A lock that changes under a shown legacy page reaches it.
        locksChanged = getattr(self._methodBranch, 'resultLocksChanged', None)
        if locksChanged is not None:
            locksChanged.connect(self.refreshResultLock)
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
        banner.linkActivated.connect(self._onBannerLink)
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

    def _onBannerLink(self, link: str) -> None:
        link = str(link or '')
        if link.startswith('#unlock:'):
            branch = self._methodBranch.branch
            if branch is not None:
                branch.requestUnlock(link[len('#unlock:'):])
            return
        self._navigation.requestBranch(METHOD_TOKEN)

    def _resultLockedTask(self, step) -> str | None:
        """The task a legacy snappy page stands for, when its result is locked.

        Plan 37 UF5 DP-1043. The branch pages make themselves read-only;
        the legacy step pages are Designer forms that know nothing of the
        lock, so the banner above them says it -- and the facade refuses
        their save either way (DP-1040).
        """
        task_id = self._SNAPPY_STEP_TASKS.get(step)
        branch = self._methodBranch.branch
        if task_id is None or branch is None or branch.engine_id != 'snappy':
            return None
        locked = getattr(branch, 'resultLocked', None)
        try:
            return task_id if callable(locked) and locked(task_id) else None
        except Exception:                                    # noqa: BLE001
            return None

    def _updatePrerequisiteBanner(self, step) -> None:
        banner = getattr(self, '_prerequisiteBanner', None)
        if banner is None:
            return
        blocked = (step in self._ENGINE_SPECIFIC_STEPS
                   and not self._engineChosen())
        locked = None if blocked else self._resultLockedTask(step)
        if blocked:
            banner.setText(self.tr(
                'This page belongs to a specific meshing engine, and no '
                'engine has been chosen yet. Its settings cannot be written '
                'until you pick one on '
                '<a href="#method">Mesh setup</a>.'))
        elif locked:
            banner.setText(self.tr(
                'Locked: the mesh on disk was made from these settings, so '
                'they cannot be saved. Opening this step does not bring back '
                'its own mesh — the mesh on screen is still the latest '
                'result. <a href="#unlock:{0}">Unlock and discard later '
                'results…</a>').format(locked))
        banner.setVisible(bool(blocked or locked))
        self._applyResultLock(step, bool(locked))

    def _applyResultLock(self, step, locked: bool) -> None:
        """Plan 37 UF5 DP-1063. A locked legacy page is read-only, not only
        captioned: its editors -- the Domain & Regions seed, which the
        viewport drags -- would otherwise open, take a placement and have the
        save refused. Only what this lock disabled is enabled again."""
        page = (getattr(self, '_pages', None) or {}).get(step)
        if page is None or not hasattr(page, 'lock'):
            return
        held = getattr(self, '_resultLockedPages', None)
        if held is None:
            held = self._resultLockedPages = set()
        if locked:
            page.lock()
            held.add(step)
        elif step in held:
            held.discard(step)
            stepLockHeld = getattr(self, '_stepLockHeld', None)
            if not (callable(stepLockHeld) and stepLockHeld(step)):
                page.unlock()

    def load(self, *, preserveCurrentPage: bool = False):
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

        # DP-363. `unload()` clears the page's loaded flag and `load()`
        # repaints every control from the stored values, so rebuilding the
        # page the user is filling in replaces their input with the last
        # saved state. A background refresh must not do that; a history
        # move must, because changing what the form shows is the point of
        # it. The caller says which kind of refresh this is.
        keep = (self._pages.get(self._navigation.currentStep())
                if preserveCurrentPage else None)
        for page in self._pages.values():
            if page is keep:
                continue
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
        self._paintSetupRows()

    def _paintSetupRows(self) -> None:
        """Tick `1. Geometry`, `2. Mesh setup` and `3. Preparation` from their records.

        DP-762. None of the three is an engine task, so nothing painted them
        anything but ready -- after an export on both engines the outline
        still read ``○`` against all three. The records asked are the ones
        Proceed already gates on: a geometry in the case, a meshing method
        that is no longer ``unselected``, and a geometry-preparation
        decision that is current for this geometry.
        """
        if self._externalMeshMode or app.facadeClient is None:
            return
        try:
            engine = app.facadeClient.checkout().getValue('mesh/engine')
            engine = str(getattr(engine, 'value', engine) or '')
            records = (
                (Step.GEOMETRY, self._pages[Step.GEOMETRY].isNextStepAvailable()),
                (Step.GEOMETRY_REPAIR,
                 self._pages[Step.GEOMETRY_REPAIR].isNextStepAvailable()),
            )
        except Exception:                                   # noqa: BLE001
            logger.debug('setup rows left as they were', exc_info=True)
            return
        for step, settled in records:
            self._navigation.setStepSettled(step, bool(settled))
        self._navigation.setBranchNodeSettled(
            METHOD_TOKEN, engine not in ('', 'unselected'))

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
                self.tr('Choose a meshing method first — the next page '
                        'belongs to one.'), 5000)
            return
        if engine:
            while (step < Step.LAST_STEP
                   and self._STEP_ENGINE.get(step, engine) != engine):
                step = Step(step + 1)
        self._open(step)

    def _stepLockHeld(self, step) -> bool:
        """Whether any of the three conditions that shut a page still holds.

        The predicate itself is unchanged and was already written twice in
        `_moveToStep`; it is named here so that the answer can be asked for
        again later, which is the whole of DP-464.
        """
        try:
            mutating = app.facadeClient.session().jobs.has_mutating_job
        except Exception:       # noqa: BLE001 - no case open is not a lock
            mutating = False
        return bool(step < self._workingStep or self._batchRunning or mutating)

    def refreshStepLock(self):
        """Reopen the current page once the job that shut it has finished.

        DP-464. `_moveToStep` decides the lock once, on the way in, from a
        one-shot read of `has_mutating_job` -- and nothing ever asked again.
        A page opened while the case was mutating therefore stayed shut after
        the mutation ended, and the only way back was to navigate away and
        return, which re-runs the same one-shot read against a quiet case.

        MEASURED 21 September 2026 on the `buildings` snappy leg: blockMesh
        ran 317 s, the base grid submitted its `decomposePar` behind it, the
        castellation page was opened inside that window and its run button
        never came back -- `the castellation run button is disabled (a job is
        still mutating the case)`. The two earlier attempts at this both tried
        to guess from outside when the stage had finished, which is the wrong
        question: the lock is not a race to be timed, it is a condition that
        stops holding, and the job manager already says so through
        `subscribe_state`.

        Only unlocking is done here, never locking. A page that was open when
        a job started keeps whatever the user was doing on it; taking a page
        away mid-interaction would be a second defect wearing this one's
        clothes, and entry is still the moment a lock is applied.
        """
        step = self._displayedStep
        if step is None or self._externalMeshMode:
            return
        page = self._pages.get(step)
        if page is None or self._stepLockHeld(step):
            return
        # Plan 37 UF5 DP-1084. The job ending is not the result lock ending:
        # a page the published result holds read-only stays so.
        if step not in (getattr(self, '_resultLockedPages', None) or ()):
            page.unlock()
        self._updateControlButtons(step)

    def _heldByResultLock(self) -> set:
        return set(getattr(self, '_resultLockedPages', None) or ())

    def refreshResultLock(self) -> None:
        """Plan 37 UF5 DP-1084. Re-read the result lock for the shown page.

        The banner and the read-only state were applied only when a legacy
        page was opened. An unlock from the outline, or a run that published
        the stage while its page was on screen, changed the lock underneath
        and left the page as it was: editable under a lock the facade then
        refused, or read-only after it was unlocked. Pages this lock holds
        that are no longer shown are released when it no longer applies.
        """
        if getattr(self, '_externalMeshMode', False):
            return
        stack = getattr(self, '_contentStack', None)
        widget = stack.currentWidget() if hasattr(stack, 'currentWidget') else None
        step, _page = self._legacyPageFor(widget)
        for held in self._heldByResultLock() - {step}:
            if not self._resultLockedTask(held):
                self._applyResultLock(held, False)
        if step is not None:
            self._updatePrerequisiteBanner(step)

    def retranslatePages(self):
        for page in self._pages.values():
            page.retranslate()

    def _connectSignalsSlots(self):
        self._navigation.currentStepChanged.connect(self._moveToStep)
        self._navigation.currentStepReactivated.connect(self._showWorkflowStep)
        # GEO-07. The outline no longer carries a `Scene / display` row, so
        # there is no navigation signal to route here. `_showScene` stays:
        # `_showExternalMesh` calls it, and an opened mesh reaching the
        # display panel is the route that never had a workflow behind it.
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
        # DP-301. The controls that finish `3. Preparation` live on the page;
        # the route off a step lives here. This is the one wire between them.
        self._connectPreparationProceed()

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
        self._leaveBranchRoute()
        self._sceneVisible = False
        self._contentStack.show()
        self._contentStack.setCurrentIndex(step)
        current_widget = (
            self._contentStack.currentWidget()
            if hasattr(self._contentStack, 'currentWidget')
            else getattr(self._pages[step], '_widget', None)
        )
        self._rememberPage(current_widget)
        self._syncOutlineToStep(step)
        self._updateControlButtons(step)

    def _leaveBranchRoute(self) -> None:
        """Stop holding the branch task the panel is moving off (DP-561).

        `_restoreBranchRoute` puts a refresh back on `_branchWidget`, which is
        right while the reader is on that task (R149) and wrong once a
        numbered step has been chosen. `_open` let go of it (DP-250); a click
        on a numbered row goes through `_moveToStep` or `_showWorkflowStep`
        instead, and neither did. MEASURED on the 0924 rerun, G6: from
        `4. Global sizing` a click on `1. Geometry` showed the Geometry page
        while a refresh inside the move put the highlight back on row 4.
        """
        self._branchPage = None
        self._branchWidget = None

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
            app.window, self.tr('Start meshing workflow'),
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
            app.window, self.tr('Return to external mesh'),
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
        # DP-1084. Moving the working step onto a page does not lift the
        # result lock that holds it read-only.
        if step not in (getattr(self, '_resultLockedPages', None) or ()):
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
        # DP-249. Opening a step is the decision that the workflow has
        # reached it, so the row stops being locked here -- before the
        # outline is asked to stand on it, not after. MEASURED on the
        # footer-only walk: `setCurrentStep` was handed `3. Preparation`
        # while that row was still disabled (a fresh case enables Geometry
        # alone), and `QAbstractItemView.setCurrentIndex` drops a disabled
        # index in silence. `_setWorkingStep` below enables the row two
        # lines too late, so the press that accepted the meshing method
        # left the highlight on `2. Mesh setup` and the walk stopped there.
        self._navigation.enableStep(step)
        # DP-250. And let go of the branch route on the way past. Every
        # applied transaction runs `load()`, which ends on
        # `_restoreBranchRoute()`; the press that opens this step commits
        # one, so the refresh it triggered put the panel straight back on
        # the branch page the press had just left. A redraw must not eject
        # a reader from a branch task (R149) -- but after a navigation the
        # workflow is no longer standing on that route, and holding the
        # widget is what says otherwise.
        self._branchPage = None
        self._branchWidget = None
        self._navigation.setCurrentStep(step)
        self._syncOutlineToStep(step)
        # R93. `setCurrentStep` moves the outline synchronously and leaves the
        # panel to `_moveToStep`, which is an async slot with several early
        # returns. Opening a case from File > Open recent painted the outline
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
            # DP-561. A step with a row of its own was left to the mouse
            # press that chose it, and a refresh landing inside the move
            # re-selected the branch task being left. Say it here instead.
            setStepRow = getattr(self._navigation, 'setStepRowCurrent', None)
            if setStepRow is not None:
                setStepRow(step)
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
            self._leaveBranchRoute()
            await page.show(self._isWorkingStep(step), self._batchRunning)
            if self._routeSuperseded(generation):
                return
            self._displayedStep = step
            if self._stepLockHeld(step):
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
        hidden = await self._saveLegacyPage(prev, self._pages[prev].hide)
        if self._routeSuperseded(generation):
            return
        if not hidden:
            self._navigation.setCurrentStep(prev)
            return
        # DP-561. The page being left is gone, so the branch task it may
        # have been standing on goes with it -- before the next await, where
        # a refresh would otherwise put the panel back on that task.
        self._leaveBranchRoute()

        page = self._pages[step]
        await page.show(self._isWorkingStep(step), self._batchRunning)
        if self._routeSuperseded(generation):
            return
        self._displayedStep = step
        if self._stepLockHeld(step):
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
        self._dropStrandedBaseGridOutline(widget)

    def _dropStrandedBaseGridOutline(self, widget):
        """The domain box belongs to the page that draws it (DP-139).

        DP-106 dropped the base-cell note whenever the displayed *step*
        changed. Every task of the Meshing Method branch is one step, so
        walking from Base Grid to Castellation, Snap, Layers or Export
        changed nothing the note keyed on: the blue domain box stayed drawn
        around a castellated mesh it had nothing to do with, and the chip
        beside the model extent went on naming a base cell for a page that
        was no longer on screen. This is the one funnel every route move
        goes through, and the page that owns the box is the only one it may
        stay on.
        """
        owner = self._pages.get(Step.BASE_GRID)
        if owner is None or widget is getattr(owner, '_widget', None):
            return
        drop = getattr(owner, 'dropOutline', None)
        if drop is not None:
            drop()

    def _currentRouteToken(self) -> str:
        """The branch row the footer is standing on, or nothing.

        DP-255. The footer used to read `self._methodBranch.currentToken`
        whatever page was on screen, and that token is the last branch task
        this *window* visited -- it survives a numbered step, a `Back`, and
        the closing of the whole case, because nothing resets it. MEASURED on
        the guided walk: leg one meshed `pipe` with Gmsh and exported it,
        leaving the token at `engine_task:common.export`; the window then
        opened a second case, and the footer on `1. Geometry` of that new
        case was graded by the export rule below -- no mesh in a case seconds
        old, so the button was dead, on the first row of a fresh workflow,
        with a tooltip that talked about saving this step. The walk had
        nowhere to go and the case could not be started at all.

        The row a press belongs to is a fact about the page in front of the
        reader. Only the branch shows task rows, so when the panel is on any
        other page there is no task token to grade, and the footer falls back
        to the plain forward label the numbered steps use.

        A stand-in panel that cannot say what it is showing -- the Region B
        harnesses put a plain object there -- is taken at its word that the
        branch is the page, which is what it was before this fix.
        """
        token = str(getattr(self._methodBranch, 'currentToken', '') or '')
        branch = getattr(self._methodBranch, 'branch', None)
        showing = getattr(self._contentStack, 'currentWidget', None)
        if branch is None or showing is None:
            return token
        return token if showing() is branch else ''

    def _updateWizardActions(self):
        if not hasattr(self._ui, 'wizardBackButton'):
            return
        self._ui.wizardBackButton.setEnabled(len(self._routeHistory) > 1)
        enabled = (
            not self._batchRunning and not self._externalMeshMode
            # DP-240. A press that started a mesher used to leave its own
            # button live for the whole of the run, so the ordinary reflex
            # when nothing visibly happens -- click again -- queued a second
            # run of the same stage. Any of the half-dozen signals that repaint
            # this bar would switch it back on mid-run, so the guard has to be
            # read here and not only set once by the press.
            and not getattr(self, '_proceedBusy', False)
            and self._contentStack.isEnabled())
        self._ui.wizardProceedButton.setEnabled(enabled)
        token = self._currentRouteToken()
        # DP-238. Plan 32 §4.2/§4.3. One table, keyed by the row, read here.
        # This button used to write its own labels, four names for one act
        # until Plan 30 WP-09 cut them to three names for nine acts. That
        # vocabulary was private to the footer, so the only cure anyone could
        # apply was to make it smaller. What the press does is a fact about
        # the row, and the row is where it is now written.
        ask = getattr(self._methodBranch, 'proceedLabel', None)
        label, tip = ask(token) if ask is not None else DEFAULT_PROCEED
        # `&` is Qt's mnemonic marker: handed over singly it vanishes and
        # underlines whatever follows it.
        self._ui.wizardProceedButton.setText(self.tr(label).replace('&', '&&'))
        self._ui.wizardProceedButton.setToolTip(self.tr(tip))
        if token.endswith('common.export'):
            # R50. It was live on a case with no geometry, no region and no
            # mesh, where it can only lead to a failure or an empty case
            # directory. The page knows whether there is a mesh to write.
            canExport = getattr(self._pages.get(Step.EXPORT), 'canExport', None)
            if canExport is not None:
                self._ui.wizardProceedButton.setEnabled(enabled and canExport())

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
        if not self._taskIsConfigured(task_id):
            return False
        return True

    @staticmethod
    def _preparationVerdict(page):
        """What the Preparation page says Proceed may do, or None.

        Plan 32 W3. The page holds the readiness report and the recorded
        decision; asking it, rather than re-deriving either here, is what
        keeps the wizard from refusing a geometry the page calls ready or
        naming a finding the table does not show. None means it cannot say
        -- no case, no report, a read that raised -- and the caller falls
        back to what it did before rather than treating silence as a no.
        """
        ask = getattr(page, 'preparationVerdict', None)
        if ask is None:
            return None
        try:
            return ask()
        except Exception:
            return None

    @staticmethod
    def _repairIsPrepared(page, verdict=None) -> bool:
        """Whether Proceed may leave Preparation (R203, Plan 32 W3).

        Two ways to be true, and both end with a prepared revision the
        meshers can read. Either one exists already, or the checks found
        nothing that needs a written reason, in which case this press is
        enough to record and freeze one -- repair is optional (Plan 32
        section 2), so a clean geometry does not owe the user a visit to a
        repair tab.

        The page answers this for the outline already. A page that cannot
        answer -- no facade session, a readiness call that raises -- does not
        get to block the wizard; the run refuses on its own and says why.

        ``verdict`` is the page's own answer when the caller already has it,
        so the question is asked once per press; left out, the page is asked
        here. The predicate is the same either way.
        """
        if verdict is None:
            verdict = StepManager._preparationVerdict(page)
        if verdict is not None:
            return bool(verdict.can_proceed or verdict.accept_as_is)
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

    # DP-239. `_proceedRunsPipeline` used to live here, and its one reader was
    # the footer tooltip that offered to run every remaining stage of the
    # engine -- the promise `Run to end` exists to make, made a second time by
    # a control standing next to it. The promise went back to the branch
    # heading; the predicate had nothing left to answer.

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
        if not self._taskIsConfigured(task_id):
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

    def _findRunToEnd(self):
        """The branch heading's `Run to end`, found the way the branch names it.

        By object name rather than by attribute, so the wizard can reach a
        control it does not own without either file importing the other, and
        so a branch stand-in that is not a real widget simply has none.
        """
        branch = getattr(self._methodBranch, 'branch', None)
        find = getattr(branch, 'findChild', None)
        if find is None:
            return None
        return find(QPushButton, 'branchRunCompleteMesh')

    @contextmanager
    def _proceedInFlight(self):
        """Hold both forward controls dead for the length of one press.

        DP-240. Proceed on a generation row starts a mesher and then awaits
        it; the button stayed live for the whole of that wait, and so did
        `Run to end` beside it, so the ordinary reflex when nothing visibly
        happens -- click again -- queued a second run of the same stage
        against the same case directory. The graph refuses nothing here: while
        the first run is in flight the task is still runnable.

        A context manager, because the restore has to happen on every way out
        of the press: a refusal, and a mesher that dies raising through the
        whole stack. A failed run is when the button matters most -- left dead
        it turns a failure into a hang and the case into a dead end.

        `Run to end` is restored by asking the branch to grade it again, not
        by switching it on: it is disabled for its own reasons too, and
        handing back a control the branch had refused would be the same defect
        with the sign flipped.
        """
        self._proceedBusy = True
        button = getattr(self._ui, 'wizardProceedButton', None)
        if button is not None:
            button.setEnabled(False)
        runToEnd = self._findRunToEnd()
        if runToEnd is not None:
            runToEnd.setEnabled(False)
        try:
            yield
        finally:
            self._proceedBusy = False
            self._updateWizardActions()
            grade = getattr(getattr(self._methodBranch, 'branch', None),
                            '_gradeRunAll', None)
            if grade is not None:
                grade()
            elif runToEnd is not None:
                runToEnd.setEnabled(True)

    @qasync.asyncSlot()
    @_one_press_at_a_time
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
        # DP-240. The dead button is the guard a user sees; this is the one
        # that holds when the press arrives another way -- a shortcut, a
        # repaint race that re-enabled the button, a queued click delivered
        # after the run began.
        if getattr(self, '_proceedBusy', False):
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
            # Plan 33 SETUP-05. This press is the whole commit of Mesh setup,
            # and the page owns what that means: the target solver, the
            # meshing method and the resource settings under them, in one
            # order, awaited. The window's part is to press it and to say
            # what came back -- three copies of the page's own rules used to
            # live here, and the one that mattered (DP-251: the settings are
            # saved before the method is applied, because applying it
            # refreshes the page over whatever was pending) is now beside the
            # settings it protects.
            accepted, reason = await method_page.commitSelection()
            if not accepted and reason:
                self._ui.statusbar.showMessage(str(reason), 8000)
            return

        if widget is self._scenePage:
            self._open(Step.GEOMETRY_REPAIR)
            return

        # A stand-in for the branch (the Region B harnesses use one) has no
        # field pages to offer, and being asked for one must not be an error --
        # the same reason `methodPage` is read through `getattr` above.
        field_page_token = getattr(self._methodBranch, 'fieldPageToken', None)
        field_token = field_page_token(widget) if field_page_token else None
        if field_token is not None:
            await self._proceedFromFieldPage(widget, field_token)
            return

        page_by_widget = {
            getattr(page, '_widget', None): (step, page)
            for step, page in self._pages.items()
        }
        mapped = page_by_widget.get(widget)
        if mapped is not None:
            step, page = mapped
            # DP-251, the other band DP-157 left without a commit: the import
            # and healing section of `3. Preparation`. It is saved before the
            # page is, because the page's own Proceed prepares the geometry
            # the section describes how to read. `getattr`, because only one
            # step page carries such a section.
            saveHealing = getattr(page, 'savePendingHealing', None)
            if saveHealing is not None and not await saveHealing():
                self._ui.statusbar.showMessage(self.tr(
                    'The import and healing settings were not saved, so the '
                    'geometry was not prepared with them.'), 8000)
                return
            if not await self._saveLegacyPage(step, page.save):
                self._ui.statusbar.showMessage(
                    self.tr('Resolve the validation errors before proceeding.'),
                    5000)
                return
            if step == Step.GEOMETRY:
                # Plan 32 section 4.1. Geometry used to walk straight on to
                # the repair page, which then asked the user to prepare a
                # geometry for a method nobody had chosen -- and the method
                # decides what preparing it means, because only Gmsh reads
                # the import and healing settings that page now carries. The
                # order is Geometry, Mesh setup, Preparation.
                self._navigation.requestBranch(METHOD_TOKEN)
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
                #
                # DP-246/DP-247. A geometry the checks call ready is not
                # that geometry. MEASURED on the unit cube: the refusal
                # below fired on a surface with no finding at all, and
                # reaching the first engine row from here took three more
                # controls -- the "Use as-is" tab, its button, and Proceed
                # again -- to record a decision nobody was being asked to
                # weigh. And when there was something to weigh, the same
                # sentence named none of it. The page is asked once, and
                # its answer decides both: prepare here and go on, or stay
                # and say what must be fixed, in the words the findings
                # table above is already using.
                #
                # DP-301. The body of this branch is now `_proceedFromPreparation`,
                # because the page grew controls that finish this step --
                # `Use as-is and proceed`, `Apply repairs and proceed`,
                # `Apply wrap and proceed` -- and a second copy of the route
                # is a second answer to "which row comes next".
                refusal = await self._proceedFromPreparation(page)
                if refusal:
                    self._ui.statusbar.showMessage(refusal, 12000)
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
                    self.tr('Choose a meshing method first — the rest of the '
                            'workflow belongs to one.'), 5000)
                return
            # DP-240. The legacy snappy pages run their stage through
            # `runInBatchMode`, so this press is a run too and is held the
            # same way.
            with self._proceedInFlight():
                if not await self._settleLegacyTask(step, page):
                    return
                await self._advanceFromLegacyStep(step)
            return

        branch = self._methodBranch.branch
        if widget is branch and branch is not None:
            task_page = branch.stack.currentWidget()
            if not await commit_page_edits(task_page, branch):
                # DP-254. A press that will not move says why. It used to
                # return here without a word, which read as a dead button.
                self._ui.statusbar.showMessage(
                    self.tr('The edits on this step were not saved, so the '
                            'workflow stayed here. Correct them and press '
                            'Proceed again.'), 8000)
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
            # DP-240. Everything past this line can start a mesher, so both
            # forward controls stay dead until the press settles -- whichever
            # way out of it the press takes.
            with self._proceedInFlight():
                settled = not task_id or await self._settleRow(
                    branch, task_page, task_id)
                # The outline is repainted whichever way the press went. It
                # used to be repainted only on the way forward, so a press
                # that refused left the row painted as it had been before --
                # and a row whose substep had just failed was painted green
                # by the settle that preceded it. The reader was left with a
                # button that did not move, a sentence that expires in eight
                # seconds, and an outline saying the step was done.
                self._methodBranch._syncChildren()
                if not settled:
                    return
                token = self._methodBranch.currentToken
                next_token = self._methodBranch.nextAvailableTaskToken(token)
                if next_token:
                    self._navigation.requestBranch(next_token)
                    return
                self._reportNoNextTask()

    async def _advanceFromLegacyStep(self, step) -> None:
        """Open whatever follows a legacy snappy step page."""
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

    async def _proceedFromFieldPage(self, page, token: str) -> None:
        """Save a field-group page, then open the outline row beneath it.

        UX-11 / DP-227. This method knew the method page, the scene page,
        the `_pages` table keyed by Step, and the engine branch. The two
        field-group rows the outline now opens with -- `1. Mesh intent` and
        `2. Execution` -- were in none of them, so Proceed on either
        returned without a transition and without a message: on the first
        two screens of the workflow the only forward control did nothing at
        all, and nothing said why.

        The route is asked of the outline, which is the one place that
        knows what row follows what, so replacing these two rows with a
        single Mesh setup page later changes nothing here.
        """
        if not await page.save():
            self._ui.statusbar.showMessage(
                self.tr('Resolve the validation errors before proceeding.'),
                5000)
            return
        route = self._navigation.nextOutlineRoute(token)
        if route is None:
            # Never silent. `_reportNoNextTask` is the sentence the engine
            # branch already uses when Proceed has nowhere to go.
            self._reportNoNextTask()
            self._updateWizardActions()
            return
        kind, value = route
        if kind == 'token':
            self._navigation.requestBranch(str(value))
        else:
            # Not `_open`: that records a new working step in the case, and
            # a reopened case standing on a later step would be regressed to
            # Geometry by a click on Proceed. This is exactly what clicking
            # the outline row does.
            self._navigation.setCurrentStep(Step(int(value)))
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

        def offer() -> None:
            answer = QMessageBox.question(
                app.window, self.tr('Nothing to proceed to'),
                self.tr('"{0}" is still locked, so there is no next task to '
                        'open.\n\nOpen it anyway to see what it is waiting '
                        'for?').format(label),
                QMessageBox.StandardButton.Open
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Open)
            if answer == QMessageBox.StandardButton.Open and token:
                self._navigation.requestBranch(token)

        # Plan 37 UF20 follow-up: Proceed's task is current here, and a box
        # opened inside it has asyncio refuse (and drop) the tasks its nested
        # loop steps. The offer is opened on the loop's next turn instead.
        say(offer)

    def _reportBlockedTask(self, branch, task_id: str) -> None:
        """Name the prerequisite that is holding this task shut (A8)."""
        titles = self._methodBranch.taskTitles()
        # DP-144. The graph is the one that knows which prerequisites still
        # count, and an optional step nobody configured no longer does. A
        # branch double that predates the accessor still gets the old count.
        ask = getattr(branch, 'blocking_prerequisites', None)
        blocking = list(ask(task_id)) if callable(ask) else []
        if not blocking:
            info = branch.task_info(task_id) or {}
            blocking = [dep for dep in (info.get('depends_on') or ())
                        if not branch.is_accepted(dep)]
        if not blocking:
            self._ui.statusbar.showMessage(
                self.tr('Finish the earlier tasks before this one.'), 5000)
            return
        names = ', '.join(titles.get(dep, dep) for dep in blocking)

        def offer() -> None:
            answer = QMessageBox.question(
                app.window, self.tr('Earlier task unfinished'),
                self.tr('"{0}" is waiting on {1}.\n\nOpen the first one now?')
                .format(titles.get(task_id, task_id), names),
                QMessageBox.StandardButton.Open
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Open)
            if answer == QMessageBox.StandardButton.Open:
                self._navigation.requestBranch(
                    TASK_TOKEN_PREFIX + blocking[0])

        # Opened with no task current, as `_reportNoNextTask`'s is.
        say(offer)

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
            self.tr('{0} was skipped — nothing is configured for it, so '
                    'nothing ran.').format(title), 8000)

    # -- one route off Preparation (DP-301) ---------------------------------- #

    async def _proceedFromPreparation(self, page):
        """Finish `3. Preparation` and open whichever row comes next.

        DP-301, PREP-04/PREP-06. The footer's Proceed owned this walk, and the
        page's own controls owned half of it: `Use geometry as-is` recorded a
        decision, froze a revision, refreshed the page and stopped -- leaving
        the task hosted on this page unsettled and the reader on the step they
        had just finished, with a second press to find. Both controls call
        this, so there is one answer to what finishing preparation means.

        Returns ``None`` when the reader was moved on, and the sentence to say
        otherwise; ``''`` means the refusal was already reported where it
        happened.
        """
        verdict = self._preparationVerdict(page)
        if not self._repairIsPrepared(page, verdict):
            return (verdict.message if verdict is not None and verdict.message
                    else self.tr(
                        'Prepare the geometry before proceeding — the '
                        'meshers read the prepared revision, not the '
                        'import. Apply a repair plan, wrap it, or accept '
                        'it on the "Use as-is" tab.'))
        # Plan 32 check 1. The visible step never decreases, and this sent
        # Proceed on `3. Preparation` up to `2. Mesh setup` -- the row above
        # it, and the question the user answered to get here. R203 is about
        # geometry nobody prepared; a prepared geometry is owed the work that
        # reads it. Which row that is belongs to the engine, so the branch is
        # asked rather than told: the walk starts at the node itself, which is
        # how it answers with the first row that still wants something.
        if self._engineId() == 'unselected':
            # No method, no engine rows to open. Opening Mesh setup would be
            # the same backwards move under a better reason, so the row is
            # named instead. DP-188. The row is named, not numbered: a
            # sentence that spells the number goes stale the next time the
            # outline gains or loses a row.
            return self.tr(
                'Choose a meshing method on the Mesh setup row first — the '
                'tasks that follow preparation belong to one.')
        if verdict is not None and verdict.accept_as_is:
            # The same two facade operations the "Use as-is" tab submits, in
            # the same order, with the same parameters: there is one way to
            # prepare a geometry, and nothing downstream can tell which
            # control asked for it.
            failure = await page.acceptGeometryAsIs()
            if failure is not None:
                return self.tr(
                    'The geometry was not prepared: {0}. Record a decision '
                    'on the "Use as-is" tab.').format(failure)
            # Said out loud. A decision recorded on someone's behalf that
            # they never see is not one they made.
            self._ui.statusbar.showMessage(verdict.message, 8000)
        # DP-252. The row this press opens is locked until the task hosted on
        # this page is settled, and nothing settled it.
        if not await self._settleHostedTasks():
            return ''
        next_token = self._methodBranch.nextAvailableTaskToken(METHOD_TOKEN)
        if next_token:
            self._navigation.requestBranch(next_token)
        else:
            self._reportNoNextTask()
        return None

    @qasync.asyncSlot()
    async def _preparationProceedRequested(self):
        """A control on the page asked for the route the footer takes."""
        page = self._pages.get(Step.GEOMETRY_REPAIR)
        if page is None:
            return
        # The footer saves the import and healing band and then the page
        # before it prepares anything (DP-251); a control on the page that
        # finishes the same step owes the same two saves.
        saveHealing = getattr(page, 'savePendingHealing', None)
        if saveHealing is not None and not await saveHealing():
            page.showProceedRefusal(self.tr(
                'The import and healing settings were not saved, so the '
                'geometry was not prepared with them.'))
            return
        if not await page.save():
            page.showProceedRefusal(self.tr(
                'Resolve the validation errors before proceeding.'))
            return
        refusal = await self._proceedFromPreparation(page)
        if refusal:
            # Beside the control that was pressed, not in the status bar at
            # the bottom of a window the reader is not looking at.
            page.showProceedRefusal(refusal)

    def _connectPreparationProceed(self) -> None:
        """Let the Preparation page ask for the footer's own route."""
        page = self._pages.get(Step.GEOMETRY_REPAIR)
        requested = getattr(page, 'proceedRequested', None)
        if requested is not None:
            requested.connect(self._preparationProceedRequested)

    async def _settleHostedTasks(self) -> bool:
        """Settle the tasks that live on `3. Preparation` and have no row.

        DP-252. `HOSTED_TASKS` says it in words -- the task still exists,
        still has prerequisites and still has to be settled -- and nothing
        settled it. MEASURED by the footer-only walk on `pipe` for Gmsh:
        Preparation prepared the geometry, the press asked for the next row,
        and the status bar answered "Global sizing opens once Describe
        geometry is finished." Every Gmsh row depends on that task, the row
        that used to accept it was folded into this page, so the guided
        workflow ended on row 3 and no Gmsh case could be meshed from it.

        The band those questions now live in is already written by the press
        that gets here (DP-251), so this is the press that settles them: the
        page that asks a task's questions is the page that finishes it.

        A refusal stops the press. `_settleTask` has already said why, and
        this is the last thing standing between the reader and a row that
        will refuse to open with a sentence naming a step they cannot see.

        A branch that cannot answer `is_accepted` is a stand-in with no task
        graph behind it (the Region B harnesses use one), and asking it to
        settle anything is not a refusal -- it has nothing to settle.
        """
        branch = self._methodBranch.branch
        if branch is None or not hasattr(branch, 'is_accepted'):
            return True
        declared = [str(task.get('task_id') or '')
                    for task in getattr(branch, 'workflow_tasks', ())]
        for task_id in declared:
            if task_id not in HOSTED_TASKS:
                continue
            # No page is passed: a hosted task has no row and no page of its
            # own, and the one thing `_settleTask` reads a page for is the
            # stage a run-gated task runs. Hosting the questions of a task
            # that runs something is a different problem than this one.
            if not await self._settleTask(branch, None, task_id):
                return False
        self._methodBranch._syncChildren()
        return True

    async def _settleRow(self, branch, task_page, row_task_id: str) -> bool:
        """Settle every task the outline row owning ``row_task_id`` folds in.

        Plan 32 §4.2/§4.3 fold thirteen tasks into eight rows: `5. Quality` is
        one press and four tasks, `Generate mesh` one press and three. One
        press therefore has to settle all of them, in the order `ROW_TASKS`
        lists -- the row task first, because the substeps grade what it
        produced and settling one of them first would grade the previous run.

        A refusal stops the row where it happened and does not advance. When
        the refusal came from a substep the sentence names it: folding the row
        away took the substep's own red row with it, so without the name the
        reader is left with a button that did not move and no reason -- the
        dead-button symptom A8 exists to kill, one level down. A refusal from
        the row task itself is left alone, because `_settleTask` has already
        said why and a second sentence would overwrite the first.
        """
        engine_id = str(getattr(branch, 'engine_id', '') or '')
        tasks = row_tasks(engine_id, row_task_id)
        # Asked with a substep token -- a stale `currentToken` pointing at a
        # task the fold hid -- `row_tasks` answers the owning row, so the row
        # task is the head of that tuple and not the id this was called with.
        head = tasks[0] if tasks else row_task_id
        for task_id in tasks:
            page = branch.page(task_id) or task_page
            if await self._settleTask(branch, page, task_id):
                continue
            if task_id != head:
                title = str(branch.task_info(task_id).get('title') or task_id)
                # Plan 37 UF4 DP-1025: and what to press once it is fixed,
                # named by the label the footer button carries on this row.
                try:
                    label = self._methodBranch.proceedLabel(
                        TASK_TOKEN_PREFIX + head)[0]
                except Exception:                           # noqa: BLE001
                    label = ''
                label = str(label or self.tr('Proceed'))
                self._ui.statusbar.showMessage(
                    self.tr('This step did not finish: {0} could not be '
                            'completed. The message shown says why; fix '
                            'that, then press "{1}" again.').format(
                                title, label), 8000)
            return False
        return True

    def _reportRunOnThisPage(self, task_id: str) -> None:
        """Say what to press once the run this page owns has finished.

        Plan 32 §4.2/§4.3. Both settle paths reach a row whose run the wizard
        cannot start for the reader, and both used to answer with a sentence
        naming a control by a name nothing on screen carries: Item E retired
        the three-word footer label for standing over nine different acts, so
        the one sentence that says what to press was pointing at a button that
        had been renamed per row. They said it two different ways as well,
        which is two sentences to keep in step with one table.

        One wording, and the name comes from the same table the footer reads,
        so neither the two paths nor the sentence and the button can part
        again.
        """
        label = self._methodBranch.proceedLabel(
            TASK_TOKEN_PREFIX + task_id)[0]
        self._ui.statusbar.showMessage(
            self.tr('Run this stage on this page, then press '
                    '"{0}".').format(label), 5000)

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
        if (str(info.get('cardinality') or '').endswith('optional')
                and not self._taskIsConfigured(task_id)):
            # Plan 32 §5.2. This question used to be asked inside the
            # `run_gated` arm below, and Gmsh's five optional rows are not
            # run-gated, so none of them ever reached it: an empty Size
            # fields, Curve controls, Volume controls, Boundary layers or
            # Periodic pairs fell through to `accept`, and the outline drew a
            # finished row over a step that produced nothing. Optionality is
            # a property of the task, not of how the task is started, so it
            # is asked before the dispatch and not inside one arm of it.
            await record_transition(branch, task_id, 'skip')
            self._reportSkippedTask(task_id)
            branch.refresh_states()
            return branch.is_accepted(task_id)
        if task_id in CHECK_TASK_OPERATIONS:
            await record_transition(branch, task_id, 'run')
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
                await record_transition(branch, task_id, 'skip')
                self._reportSkippedTask(task_id)
            else:
                # Plan 30 WP-09 / F-23: this used to click the task page's
                # hidden `Run complete mesh` button, which was never visible,
                # so the branch was unreachable in both senses.
                self._reportRunOnThisPage(task_id)
                return False
        else:
            await record_transition(branch, task_id, 'accept')
        branch.refresh_states()
        return branch.is_accepted(task_id)

    def _configurationFingerprint(self):
        """A digest of the authored configuration, or None if unreadable."""
        import hashlib
        import json
        client = getattr(app, 'facadeClient', None)
        reader = getattr(client, 'configuration', None)
        if reader is None:
            return None
        try:
            text = json.dumps(reader(), sort_keys=True, default=str)
        except Exception:                                    # noqa: BLE001
            return None
        return hashlib.sha1(text.encode('utf-8')).hexdigest()

    async def _saveLegacyPage(self, step, save) -> bool:
        """Save a legacy step page, and tell its task if the settings moved.

        Plan 37 UF3 DP-1036. The legacy snappy pages -- Base grid,
        Castellation, Snap, Boundary layer, Region -- commit their own
        working copies and sent no transition at all. A task that had
        already run stayed PASSED, `_settleLegacyTask` found it accepted and
        walked on, and the mesh on screen was the one made from the settings
        before the edit. The page cannot say whether its save changed
        anything, so the configuration is compared across it: a save that
        changed nothing keeps the result, one that did configures the task,
        which stales it and everything after it, so Proceed meshes again.
        """
        task_id = self._SNAPPY_STEP_TASKS.get(step)
        branch = getattr(getattr(self, '_methodBranch', None), 'branch', None)
        watched = (task_id is not None and branch is not None
                   and getattr(branch, 'engine_id', None) == 'snappy')
        before = self._configurationFingerprint() if watched else None
        saved = await save()
        if not saved or before is None:
            return saved
        after = self._configurationFingerprint()
        if after is None or after == before:
            return saved
        if branch.is_accepted(task_id):
            await record_transition(branch, task_id, 'configure')
            branch.refresh_states()
        return saved

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
            await record_transition(branch, task_id, 'accept')
        elif not self._taskIsConfigured(task_id):
            await record_transition(branch, task_id, 'skip')
            self._reportSkippedTask(task_id)
        else:
            runner = getattr(page, 'runInBatchMode', None)
            if runner is None:
                self._reportRunOnThisPage(task_id)
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
            await record_transition(branch, 'common.export', 'accept')
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
        from foammesh.view.workflow_controls.configured import CONFIGURED_PATHS

        if branch is None or branch.task_state(task_id) != 'skipped':
            return False
        # Plan 32 §4.5. This used to end by naming the snappy layers row, so
        # the five Gmsh rows carrying the same question -- size fields,
        # curve controls, volume controls, boundary layers, periodic pairs --
        # answered `False` here however much the user had since put into them,
        # and every one of them kept its earlier refusal for the life of the
        # case. The row is looked up in a table now, so a sixth row is an
        # entry and not a sixth special case in a third method. A skip on a
        # task the table says nothing about is left alone: there is no
        # configuration of it that could contradict anything.
        if task_id not in CONFIGURED_PATHS:
            return False
        return self._taskIsConfigured(task_id)

    def _taskIsConfigured(self, task_id: str) -> bool:
        """Whether the optional row ``task_id`` has anything in it.

        The question is asked of storage and not of the page, because a page
        that was never opened has no dirty state, and "the user did not visit
        the tab" is not the same statement as "the user has nothing to put in
        it" -- which is exactly the difference a `Run to end` or a reload
        walks into. A task the table says nothing about is not optional and
        answers ``True``: a missing entry must never read as an empty row, or
        a required stage would be skipped by the press meant to run it.
        """
        from foammesh.view.workflow_controls.configured import (
            CONFIGURED_PATHS, task_is_configured)

        if task_id not in CONFIGURED_PATHS:
            return True
        try:
            db = app.facadeClient.checkout()
        except Exception:  # noqa: BLE001 - no case open means nothing in it
            return False
        return task_is_configured(db, task_id)

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
        self._paintSetupRows()

    async def _generateDictionaries(self) -> None:
        """Write this case's engine dictionaries over the current domain.

        One act, two callers: the window's own pipeline and (DP-256) a single
        stage started from a branch row.
        """
        await app.facadeClient.run(
            'workflow.generate_dictionaries',
            {'bbox': self._snappyDomainBounds()})

    def _snappyDomainBounds(self) -> list:
        """The geometry extent the base grid is derived from.

        DP-576. The raw extent, like every other frontend: the case builder
        applies the saved standoff and a chosen bounding Hex6 itself. This
        used to hand over the hidden legacy page's cached, already stood-off
        box, so the same project wrote a different blockMeshDict here than
        from the CLI or the facade.

        DP-821. The surfaces' extent: the seed glyphs are held by the same
        manager, and a seed off the geometry grew the written block.
        """
        manager = app.window.geometryManager
        bounds = getattr(manager, 'getSurfaceBounds',
                         manager.getBounds)().toTuple()
        values = [float(value) for value in bounds]
        if (len(values) != 6
                or any(math.isnan(v) or math.isinf(v) for v in values)
                or any(values[i + 1] <= values[i] for i in (0, 2, 4))):
            raise ValueError(self.tr(
                'The base grid has no valid domain. Set it on the Base grid page.'))
        return values

    def _onWizardMethodAccepted(self, _engine_id: str):
        """Publish the engine rows, then open the row that comes next.

        DP-248. This asked the branch for `firstAvailableTaskToken()`, which
        is an engine row, and section 4.1 puts `3. Preparation` between Mesh
        setup and the engine rows. MEASURED by the footer-only walk: on Gmsh
        every engine row depends on `gmsh.describe_geometry`, which is hosted
        on Preparation and publishes no row, so every row was locked, the
        token was empty and the press moved nothing -- the guided workflow
        ended at row 2. On snappy `snappy.domain_regions` has no
        prerequisites, so the press opened row 4 and skipped the row that
        prepares the geometry those rows read.

        The rows are still published first: the reader has to see where the
        walk goes, and `3. Preparation` is followed by `4. ...`, not by an
        empty branch node.
        """
        self._methodBranch._syncChildren()
        self._open(Step.GEOMETRY_REPAIR)

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
                await AsyncMessageBox().information(self._contentStack, self.tr('Process completed'),
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

    # DP-226. A modal here called the second mesher `Snappy` while the
    # page that offers it called it `Snappy Hex Mesh`. The one rule
    # that spells a stored name names both meshers now, so a reader
    # who chose a mesher meets the same word when the run reports.
    def _engineDisplayName(self, engine_id: str) -> str:
        return (humanise_option(engine_id) if str(engine_id)
                else self.tr('The meshing engine'))

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
                await self._generateDictionaries()
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
            # Plan 37 UF20 follow-up: so does the user's own cancel of a
            # Gmsh run -- it stops before the publisher, the only writer of
            # the case mesh -- which used to clear the viewport over an
            # accepted mesh still on disk.
            cancelled = self._userCancelled(payload)
            if cancelled and self._caseMeshUntouched(payload):
                pass
            elif built or handle.run_id:
                await self._drawFinishedMesh(handle)
            unpublished = unpublished_mesh_reason(payload)
            if result.status == 'accepted' and unpublished:
                # DP-483. With no target solver chosen, a Gmsh mesh the
                # publisher refused still leaves the run accepted -- the
                # native mesh is kept -- and this box said "completed" over
                # a case with no constant/polyMesh at all. MEASURED on
                # tee_with_plug: the refusal was in the run manifest and
                # nowhere on screen.
                await AsyncMessageBox().warning(
                    self._contentStack,
                    self.tr('{0} mesh was built but not published').format(
                        name),
                    unpublished)
            elif result.status == 'accepted':
                await AsyncMessageBox().information(
                    self._contentStack, self.tr('Process completed'),
                    self.tr('The %s pipeline completed.') % name)
            elif cancelled:
                await self._reportPipelineCancelled(name, payload, handle)
            else:
                await self._reportPipelineFailure(
                    name, str(payload.get('reason') or ''), built=built,
                    log=str(payload.get('log') or ''),
                    details=str(payload.get('details') or ''))
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
            return
        # DP-133. Last, and only when the volume mesh is actually on screen:
        # the offer replaces the result line, and replacing it with an offer
        # to see the surface of a mesh that could not be drawn would be an
        # offer about a picture that is not there.
        offer = getattr(app.window, 'offerSurfacePass', None)
        if offer is not None:
            offer(handle)

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

    @staticmethod
    def _userCancelled(payload: dict) -> bool:
        """Whether this run ended because the user stopped it.

        The Gmsh compute says so in ``cancelled``; a snappy node's job
        carries ``status='cancelled'`` and the facade's cancel sentence.
        """
        if payload.get('cancelled'):
            return True
        if str((payload.get('job') or {}).get('status') or '') == 'cancelled':
            return True
        return (str(payload.get('reason') or '')
                == 'the run was cancelled before it finished')

    @staticmethod
    def _caseMeshUntouched(payload: dict) -> bool:
        """Whether a cancelled run left the case mesh as it was.

        ``cancelled`` is set only by the Gmsh compute, which stops before the
        publisher -- the one writer of the case mesh -- so whatever the case
        held is still there and still what the viewport should show. A snappy
        stage rewrites the case mesh in place, so its cancel is not this.
        """
        return bool(payload.get('cancelled'))

    async def _reportPipelineCancelled(self, engine: str, payload: dict,
                                       handle) -> None:
        """Say the user's own cancel landed, worded as a cancel.

        Plan 37 UF20 follow-up, MEASURED live (BUDGETS.md section 3): a
        cancelled Gmsh run was reported in a box titled "Gmsh pipeline did
        not run" -- the heading for a run that was refused -- over a viewport
        cleared although the accepted mesh was still on disk.
        """
        headline = self.tr('{0} run cancelled').format(engine)
        detail = self.tr('The run was cancelled before it finished, as you '
                         'asked.')
        if self._caseMeshUntouched(payload):
            detail = '{0} {1}'.format(detail, self.tr(
                'The case mesh is unchanged.'))
        console = app.consoleView
        if console is not None:
            console.append('{0}: {1}'.format(headline, detail))
        self._ui.statusbar.showMessage(detail, 10000)
        status = getattr(getattr(app, 'window', None), 'showRunStatus', None)
        if callable(status):
            status(self.tr('{0} · cancelled · {1}').format(
                getattr(handle, 'run_id', '') or engine, detail),
                log=str(payload.get('log') or ''))
        await AsyncMessageBox().information(
            self._contentStack, headline, detail)

    async def _reportPipelineFailure(self, engine: str, reason: str,
                                     *, built: bool = False, log: str = '',
                                     details: str = '') -> None:
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
            if log:
                console.appendError(self.tr('Log: %s') % log)
        self._ui.statusbar.showMessage(detail, 10000)
        # DP-506 (MA-02). The log the failure is written in, and the block of
        # it that says why, travel with the failure: the Gmsh compute and the
        # snappy whole-pipeline run report the way a single stage does.
        extra = {}
        if log:
            extra['informativeText'] = self.tr('Log: %s') % log
        if details.strip():
            extra['detailedText'] = details.strip()
        # Plan 37 UF20 follow-up (DP-1130 path), MEASURED on GU3: a footer
        # run refused for boundary layers with nothing ticked left the run
        # strip reading "Meshing on a single worker." in the running state,
        # Cancel and all, for a run that never started. The plan's line is
        # replaced with the refusal -- unless the draw already put up its own
        # line or offer, which is about this run too.
        window = getattr(app, 'window', None)
        strip = getattr(window, '_runStatusStrip', None)
        status = getattr(window, 'showRunStatus', None)
        if callable(status) and getattr(strip, 'state', 'running') in (
                'running', 'cancelling'):
            status('{0}: {1}'.format(headline, detail), failed=True, log=log)
        await AsyncMessageBox().warning(
            self._contentStack, headline, detail, **extra)

    @qasync.asyncSlot()
    async def _cancelFinishSteps(self):
        # The footer Cancel is the one a Gmsh run offers on the page; the
        # run strip's Cancel is the same request, so the strip says it was
        # taken too rather than still reading "running".
        strip = getattr(getattr(app, 'window', None), '_runStatusStrip', None)
        mark = getattr(strip, 'markCancelling', None)
        if callable(mark) and getattr(strip, 'state', '') == 'running':
            mark()
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
            await AsyncMessageBox().warning(
                self._contentStack,
                self.tr('Permission error'),
                self.tr('A file in the project folder might be open in another program.\n'
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
