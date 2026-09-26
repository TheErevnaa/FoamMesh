"""Dynamic engine task branch beneath the shared Meshing Method step.

An engine's task list is never built as fixed widgets: the selected engine
publishes a workflow descriptor and the shell creates the matching task cards
and pages.  Engine stages are also kept out of the top-level numeric ``Step``
enum, so this branch owns its own navigation and lives beneath the Meshing
Method step rather than inside the legacy step machine.

Switching engines removes the previous branch from view, keeps its state, and
marks its artifacts stale - the widget itself only reflects that decision.
"""
from __future__ import annotations

import asyncio
import logging

import hashlib
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from widgets.progress_dialog import ProgressDialog

from foammesh.core.engine.contracts import TaskState
from foammesh.core.workflow.dynamic import EngineWorkflowGraph
from foammesh.view.gmsh_workflow import GMSH_TASK_PAGES
from foammesh.view.snappy_workflow import SNAPPY_TASK_PAGES
from foammesh.view.facade_client import query, submit

from .run_narration import describe_result, newest_log


#: Engine id -> {task_id: page class}. An engine with no entry renders no
#: branch pages, which is how Snappy keeps its existing legacy step pages.
BRANCH_PAGES: dict[str, dict] = {
    'gmsh': GMSH_TASK_PAGES,
    # Plan 26 WP5. Snappy's mature Designer pages stay where they are; these
    # are the tasks that had no page of their own -- the five qualification
    # gates that never reached the tree, and the two nodes that resolved to
    # another task's widget.
    'snappy': SNAPPY_TASK_PAGES,
}

_STATE_SEPARATOR = ' — '

_STATE_SUFFIX = {
    TaskState.LOCKED: 'locked',
    TaskState.READY: '',
    TaskState.EDITING: 'edited',
    TaskState.CONFIGURED: 'configured',
    TaskState.RUNNING: 'running',
    TaskState.PASSED: 'done',
    TaskState.WARNING: 'warning',
    TaskState.FAILED: 'failed',
    TaskState.SKIPPED: 'skipped',
    TaskState.STALE: 'stale',
    # Plan 23 §8.5: neither is a pass. 'done' stays reserved for PASSED.
    TaskState.COMPLETED: 'evidence',
    TaskState.WAIVED: 'waived',
}


logger = logging.getLogger(__name__)


def _stage_writer(view):
    """The generate a stage run on ``view`` has to make first, or None.

    Read off the view with `getattr` rather than as an attribute: both run
    routes are also called unbound on plain stand-ins (the Region B
    harnesses), and a stand-in with no window behind it has no domain to
    write dictionaries over and nothing to write them for.
    """
    writers = getattr(view, 'stage_dictionaries', None) or {}
    return writers.get(getattr(view, '_engine_id', ''))


class _ShownPageStack(QStackedWidget):
    """A task stack as wide at least as the task it shows, and no wider.

    DP-572 (0924 rerun2, S6 and G6). A `QStackedWidget` asks for the widest
    minimum of every page it holds, shown or not. DP-534 took that floor off
    the settings column's own stack, but the page that stack shows is this
    branch, and this stack still carried it: every stage page of both
    engines asked 449 px, all of it from the Qualification summary page, so
    the 360 px column scrolled 89 px sideways and cut each page on its
    right -- `Maximum level` read `Maximun`. The width now follows the page
    on screen; the height is left as Qt measures it.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        page = self.currentWidget()
        if page is not None:
            frame = 2 * self.frameWidth()
            hint.setWidth(max(0, page.minimumSizeHint().width()) + frame)
        return hint


class EngineBranchView(QWidget):
    """Region B task-page stack for the selected engine.

    ``tasks`` is retained as a hidden state model for controller compatibility;
    Region A is the one and only visible task selector.
    """

    taskSelected = Signal(str)
    engineChanged = Signal(str)
    #: A task page accepted or reverted an edit; listeners re-sync task labels.
    taskChanged = Signal(str)

    #: Plan 32 §7.3. Mirrors `MeshingMethodBranch.lockBypass`, which is the
    #: only thing that ever writes it, so both doors into a task page answer
    #: the same about the lock. Off unless a harness turns it on.
    lockBypass = False

    def __init__(self, facade_client, parent=None):
        super().__init__(parent)
        self._client = facade_client
        self._pages: dict[str, QWidget] = {}
        self._order: list[str] = []
        self._graph: EngineWorkflowGraph | None = None
        self._engine_id = ''
        #: Engine name, shown when no task is open. E3: the panel used to be
        #: titled with this whatever page was on screen, so every task page
        #: was headed `Snappy Hex Mesh` while its body said `Surface Features
        #: & Refinement`.
        self._engineName = ''
        self._workflow_digest = ''
        self._workflow: dict = {}
        self.setObjectName('engineBranchView')
        self.setAccessibleName(self.tr('Selected meshing engine tasks'))

        layout = QVBoxLayout(self)
        self._heading = QLabel(self)
        self._heading.setObjectName('engineBranchHeading')
        self._runAll = QPushButton(self.tr('Run to end'), self)
        self._runAll.setObjectName('branchRunCompleteMesh')
        self._runAll.setAccessibleDescription(self.tr(
            'Run every stage of the selected engine in one go.'))
        self._runAll.setVisible(False)
        self._runAll.clicked.connect(
            lambda: self._on_run_requested(self._run_all_task_id()))
        # A whole-pipeline run belongs to the engine, not to whichever task
        # happens to be open. On the task page it sat a button's width from
        # "Run this step", and the two read as variants of one action.
        heading_row = QHBoxLayout()
        heading_row.addWidget(self._heading, 1)
        heading_row.addWidget(self._runAll, 0)
        layout.addLayout(heading_row)

        # R159. Saved task progress can be discarded behind the user's back --
        # a workflow digest bump, an unreadable file, states inconsistent with
        # the current workflow -- and the store already builds the sentence
        # that says which. Nothing displayed it: MEASURED, Native Mesh
        # Fidelity and Geometry Fidelity both went from the completed tick
        # back to not-run inside one session, with no STALE mark and no
        # message, and the only hint was Qualification Summary refusing to
        # open. This label is where that reason lands.
        self._stateNotice = QLabel(self)
        self._stateNotice.setObjectName('engineBranchStateNotice')
        self._stateNotice.setWordWrap(True)
        self._stateNotice.setVisible(False)
        layout.addWidget(self._stateNotice)

        self.tasks = QListWidget(self)
        self.tasks.setObjectName('engineBranchTaskStateModel')
        self.tasks.setAccessibleName(self.tr('Hidden engine task state model'))
        self.tasks.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tasks.currentRowChanged.connect(self._on_row_changed)
        self.tasks.hide()
        self.stack = _ShownPageStack(self)
        self.stack.setObjectName('engineBranchPages')
        self.stack.setAccessibleName(
            self.tr('Selected engine task settings page'))
        layout.addWidget(self.stack, 1)

        self._empty = QLabel(
            self.tr('Select a meshing method to see its tasks.'), self)
        self._empty.setObjectName('engineBranchEmpty')
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)

        self.refresh()

    # -- state ------------------------------------------------------------- #

    @property
    def engine_id(self) -> str:
        return self._engine_id

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    @property
    def graph(self) -> EngineWorkflowGraph | None:
        return self._graph

    @property
    def workflow_tasks(self) -> tuple[dict, ...]:
        return tuple(self._workflow.get('tasks') or ())

    def page(self, task_id: str):
        return self._pages.get(task_id)

    # -- construction ------------------------------------------------------ #

    def refresh(self) -> None:
        result = query(self._client, 'mesh.engine.workflow')
        payload = result.payload
        engine_id = str(payload.get('engine_id') or '')
        workflow = payload.get('workflow') or {}
        self._workflow = dict(workflow)
        changed = engine_id != self._engine_id
        digest = hashlib.sha256(json.dumps(
            {'engine_id': engine_id, 'workflow': workflow},
            sort_keys=True, separators=(',', ':'), default=str,
        ).encode('utf-8')).hexdigest()
        self._engine_id = engine_id
        self._engineName = (
            workflow.get('display_name') or engine_id or self.tr('No engine'))
        self._updateHeading()
        if digest != self._workflow_digest:
            self._workflow_digest = digest
            self._rebuild(engine_id, workflow)
        else:
            # State-only refreshes must not destroy an in-progress editor.
            self._graph = self._build_graph(engine_id)
            self._apply_states()
        if changed:
            self.engineChanged.emit(engine_id)

    def _rebuild(self, engine_id: str, workflow: dict) -> None:
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self.tasks.clear()
        self._pages.clear()
        self._order.clear()

        page_classes = BRANCH_PAGES.get(engine_id, {})
        self._graph = self._build_graph(engine_id)
        for task in workflow.get('tasks', ()):
            task_id = task.get('task_id')
            page_class = page_classes.get(task_id)
            if page_class is None:
                # Plan 30 WP-09 (F-17): both engine registries now name every
                # task their workflow declares, so nothing should land here.
                # It stays as a guard against a task added to a descriptor
                # without a page, which would otherwise raise while the tree
                # is being rebuilt.
                continue
            # Plan 23 WP7B: the `common.*` qualification pages serve both
            # engines, so the branch supplies the engine rather than the page
            # assuming one. Without this they inherit the base default and a
            # Gmsh case would send its transitions to the snappy workflow --
            # which fails as a *missing* task rather than a wrong one, so it
            # would have been read as an unregistered page.
            try:
                page = page_class(self._client, self, engine_id=engine_id)
            except TypeError:
                page = page_class(self._client, self)
            # R155. `updateRequested` no longer maps straight to `accept`:
            # see _on_task_updated. A run-gated task refuses `accept`, and the
            # refusal used to vanish into the bare except below.
            update_signal = getattr(page, 'updateRequested', None)
            if update_signal is not None:
                update_signal.connect(self._on_task_updated)
            # R134. A page that records a setting no run has to judge asks
            # for CONFIGURED rather than the tick, so the outline can show
            # the half-filled glyph R94 added for exactly that case.
            configure_signal = getattr(page, 'configureRequested', None)
            if configure_signal is not None:
                configure_signal.connect(
                    lambda tid: self._on_task_transition(tid, 'configure'))
            revert_signal = getattr(page, 'revertRequested', None)
            if revert_signal is not None:
                revert_signal.connect(
                    lambda tid: self._on_task_transition(tid, 'revert'))
            run_signal = getattr(page, 'runRequested', None)
            if run_signal is not None:
                run_signal.connect(self._on_run_requested)
            stage_signal = getattr(page, 'stageRunRequested', None)
            if stage_signal is not None:
                stage_signal.connect(self._on_stage_run_requested)
            # DP-123. A page that can refuse the whole-pipeline run says so
            # here, because the button that starts it belongs to this row.
            refusal_signal = getattr(page, 'runAllRefusalChanged', None)
            if refusal_signal is not None:
                refusal_signal.connect(lambda _text='': self._gradeRunAll())
            self.stack.addWidget(page)
            self._pages[task_id] = page
            self._order.append(task_id)
            item = QListWidgetItem(self._label(task, task_id), self.tasks)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setToolTip(task.get('description') or '')
        self._apply_states()
        empty = not self._order
        self._empty.setVisible(empty)
        # R78. This is the root cause of R34/R58 -- the grey chip reading
        # `Reference Read`, `Describe Geom`, `Surface Feature` painted across
        # every task page's heading. ``self.tasks`` is the hidden state model:
        # it is built with ``self`` as its parent but is deliberately never
        # added to a layout, so the moment something makes it visible Qt draws
        # it at (0, 0) of the branch view at its own sizeHint -- directly over
        # the heading row. It was hidden in the constructor and then shown
        # again right here on every workflow load. It stays hidden.
        self.stack.setVisible(not empty)
        self._update_run_all(engine_id)
        if self._order:
            self.tasks.setCurrentRow(0)

    def _run_all_task_id(self) -> str:
        """The task the whole-pipeline run is recorded against."""
        for task_id, page in self._pages.items():
            if getattr(page, 'run_all_task_id', None) == task_id:
                return task_id
        return self._order[0] if self._order else ''

    def _update_run_all(self, engine_id: str) -> None:
        """Show the whole-pipeline button for any engine that has one.

        Plan 26 WP5.3 put it here because snappy's equivalent lived on the
        legacy step bar, which ``_showBranchPage`` hides. It used to stand
        down whenever a mounted page carried its own copy; that copy has since
        moved off the task page, so this is now the only one.
        """
        self._runAll.setVisible(
            bool(engine_id) and engine_id in self.RUN_OPERATIONS)
        self._gradeRunAll()

    #: What "Run to end" says about itself when nothing refuses it.
    RUN_ALL_DESCRIPTION = 'Run every stage of the selected engine in one go.'

    def runAllRefusal(self) -> str:
        """Why the whole-pipeline run cannot start, asked of the page.

        DP-123. The pages had the answer and shut ``_runStage`` with it --
        a button that is hidden on every Gmsh task page, because none of them
        declares a ``run_stage``. MEASURED on the Gmsh leg of
        `two_solid_block`: the Boundary Layers banner said the run would be
        refused, the Compute note said it in the same words, and "Run to end"
        was live; the run booted WSL, imported the geometry and refused 76 s
        later. The page is asked here instead, so the sentence reaches the
        button the user presses.
        """
        ask = getattr(self._pages.get(self._run_all_task_id()),
                      'runAllRefusal', None)
        if not callable(ask):
            return ''
        try:
            return str(ask() or '')
        except Exception:                                    # noqa: BLE001
            return ''

    def _gradeRunAll(self) -> None:
        """Shut "Run to end" when the run would refuse, and say why."""
        refusal = self.runAllRefusal()
        self._runAll.setEnabled(not refusal)
        self._runAll.setToolTip(refusal)
        self._runAll.setAccessibleDescription(
            refusal or self.tr(self.RUN_ALL_DESCRIPTION))

    def _build_graph(self, engine_id: str):
        if not engine_id:
            return None
        try:
            from foammesh.core.engine.registry import ENGINE_REGISTRY
            descriptor = ENGINE_REGISTRY.get(engine_id).workflow_descriptor()
        except Exception:
            return None
        graph = EngineWorkflowGraph(descriptor)
        # Restore the persisted task states so reopening a project lands on
        # the same visible stage (§4.2) instead of everything reading LOCKED.
        #
        # R159. Every failure here used to be swallowed, and a graph that
        # restored nothing paints the outline as a branch that has never been
        # run -- completed rows silently back to `not run`, everything after
        # them locked, and no reason anywhere. Whatever went wrong is now said
        # out loud, because the outline is the workflow's memory and a memory
        # that loses entries without saying so cannot be read as a record.
        self._setStateNotice('')
        try:
            snapshot = query(
                self._client, 'mesh.workflow.task_state',
                {'engine_id': engine_id}).payload.get('state') or {}
        except Exception:                                    # noqa: BLE001
            self._setStateNotice(self.tr(
                'Saved task progress for this engine could not be read, so '
                'the tasks below show as not yet run. Nothing on disk has '
                'been changed.'))
            return graph
        notice = snapshot.get('workflow_reset_notice') or {}
        message = str(notice.get('message') or '')
        if message:
            self._setStateNotice(message)
        if snapshot.get('tasks'):
            try:
                graph.load(snapshot)
            except Exception:                                # noqa: BLE001
                self._setStateNotice(self.tr(
                    'Saved task progress for this engine did not match the '
                    'current workflow, so the tasks below show as not yet '
                    'run. Meshes and reports are untouched.'))
        return graph

    def _setStateNotice(self, message: str) -> None:
        """Say why the task states below are not the ones last recorded (R159)."""
        notice = getattr(self, '_stateNotice', None)
        if notice is None:
            return
        notice.setText(message)
        notice.setVisible(bool(message))

    def taskTitles(self) -> dict:
        """Task id to the human title of the task."""
        titles = {}
        for task in self.workflow_tasks:
            task_id = task.get('task_id')
            if task_id:
                titles[task_id] = task.get('title') or task_id
        return titles

    def _label(self, task: dict, task_id: str) -> str:
        title = task.get('title') or task_id
        if task.get('optional'):
            title = f'{title} ({self.tr("optional")})'
        return title

    def _apply_states(self) -> None:
        """Disable tasks whose prerequisites are not satisfied (§5.17)."""
        self._refresh_page_status()
        if self._graph is None:
            return
        for row, task_id in enumerate(self._order):
            item = self.tasks.item(row)
            if item is None:
                continue
            try:
                state = self._graph.state(task_id)
            except Exception:
                continue
            suffix = _STATE_SUFFIX.get(state, '')
            base = item.text().split(_STATE_SEPARATOR)[0]
            item.setText(f'{base}{_STATE_SEPARATOR}{suffix}' if suffix else base)
            flags = item.flags()
            if state is TaskState.LOCKED:
                item.setFlags(flags & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled)

    def _refresh_page_status(self) -> None:
        """Tell the visible page that the graph moved under it.

        Its status line is read once, when the page is built. Without this a
        task went on saying "Locked - complete the prerequisite tasks first"
        long after the prerequisite passed -- the text and the buttons on the
        same page disagreeing about whether the task could be run.

        One page must not be able to take the other nine down with it, so a
        page that raises here is skipped rather than propagated. DP-142: it
        used to be skipped *silently*, and the Export page had been raising
        `RecursionError` on every graph move for as long as the page had
        existed. Two fixes were written for the stale sentence it left on
        screen; both were correct, both were swallowed here, and nothing
        anywhere said so. The page is still skipped -- it is still not worth
        a broken branch -- but now it says which page and why.
        """
        for task_id, page in self._pages.items():
            refresh = getattr(page, 'refresh_status', None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:                            # noqa: BLE001
                    logger.exception(
                        'the %s page could not re-read its status; the rest '
                        'of the branch was refreshed without it', task_id)
        self._gradeRunAll()

    def _on_task_updated(self, task_id: str) -> None:
        """Record settings that were saved without a run (R134/R155).

        MEASURED on the Gmsh branch's Compute Mesh: Update was pressed, the
        facade accepted the field patch, and the page header stayed on 'Ready
        to configure.' while the outline row stayed at the not-run circle --
        the `partially filled` glyph R94 added for exactly this case had never
        been seen in six runs. The page emits one signal meaning "these
        settings are accepted", and the branch turned it into the `accept`
        transition for every task. A run-gated task refuses `accept` -- only a
        recorded run may accept it -- and the refusal raised out of `run_sync`
        into a bare `except`, so nothing moved and nothing was said. Settings
        entered against a task that a run must accept are precisely
        CONFIGURED, so that is what gets recorded.
        """
        run_gated = bool(self.task_info(task_id).get('run_gated'))
        self._on_task_transition(
            task_id, 'configure' if run_gated else 'accept', after_save=True)

    def _locked_sentence(self, task_id: str) -> str:
        """What a user must do before this task can be marked done.

        Plan 31 DP-39. The prerequisite ids come from the graph, which is the
        only thing that knows which of them are still unaccepted; the names
        come from the workflow document, which is what the outline shows.
        """
        title = str(self.task_info(task_id).get('title') or task_id)
        blocking = ()
        graph = self._graph
        if graph is not None:
            try:
                blocking = graph.blocking_prerequisites(task_id)
            except Exception:                                # noqa: BLE001
                blocking = ()
        names = ', '.join(str(self.task_info(item).get('title') or item)
                          for item in blocking)
        if not names:
            return self.tr(
                '{0} is not marked done yet: an earlier step is still '
                'incomplete.').format(title)
        return self.tr(
            '{0} is not marked done yet, because {1} has still to be '
            'completed.').format(title, names)

    def _on_task_transition(self, task_id: str, transition: str, *,
                            after_save: bool = False) -> None:
        """Record the §5.17 lifecycle transition through the facade.

        C31-12: scheduled, not blocking. The transition rebuilds the graph and
        repaints the outline, and all of that still happens after the facade
        has answered -- `_transition_recorded` is the tail of the old method,
        unchanged.

        The answer is therefore not available to the caller: this returns as
        soon as the write is scheduled. A caller that has to know whether the
        task is settled before it does the next thing awaits
        `record_transition_async` instead (DP-253).
        """
        submit(self._client, 'mesh.workflow.task_transition', {
            'engine_id': self._engine_id, 'task_id': task_id,
            'transition': transition},
            then=lambda result: self._transition_recorded(
                task_id, result, after_save=after_save))

    def _transition_recorded(self, task_id: str, result, *,
                             after_save: bool = False) -> bool:
        """Repaint on the facade's answer, and say whether it took it."""
        accepted = getattr(result, 'status', 'accepted') == 'accepted'
        if not accepted:
            # R180. The facade refuses a transition by raising, so the
            # `except` this used to have was the path every refusal
            # actually took -- and it discarded the sentence saying why.
            # A refusal now arrives as a result carrying that sentence,
            # so both shapes of refusal are said out loud in one place.
            # A row that will not move says what stopped it.
            message = str(getattr(result, 'message', '')
                          or self.tr('The transition was rejected.'))
            # Plan 31 DP-39, MEASURED on every Gmsh run of the `t3` and
            # `t3-redo` legs. `after_save` is reached only from
            # `updateRequested`, which a page emits *after* the facade
            # accepted its field patch -- so the settings are on disk and
            # the only thing refused is the tick. Reporting that as
            # `Task state / task is locked by prerequisites:
            # gmsh.boundary_layers` told the user their save had failed
            # when it had not, named the task they were already looking
            # at rather than the one holding it, and offered no way on.
            if after_save and 'locked by prerequisites' in message:
                QMessageBox.warning(
                    self, self.tr('Settings saved'),
                    self.tr('Your settings were saved. ')
                    + self._locked_sentence(task_id))
            else:
                QMessageBox.warning(self, self.tr('Task state'), message)
        self._graph = self._build_graph(self._engine_id)
        self._apply_states()
        self.taskChanged.emit(task_id)
        return accepted

    async def record_transition_async(self, task_id: str, transition: str, *,
                                      after_save: bool = False) -> bool:
        """Record the transition and wait for the facade to answer it.

        DP-253. MEASURED on the guided walk, `pipe` for Gmsh: the press on
        `3. Preparation` accepted the task hosted there, asked one line later
        whether it was accepted, was told no, and stopped without a word --
        and the outline then drew the row it had refused to open as unlocked,
        because the write it had not waited for landed a moment later.

        `_on_task_transition` schedules the write (C31-12) so a Qt slot does
        not freeze on it, which is right for a slot and wrong for a caller
        that has to branch on the outcome: the state it reads is the state
        from before the transition. The wizard is a coroutine and can wait, so
        this is the same write awaited, with the same repaint after it, and
        the answer handed back the way `run_stage_async` hands back its own.

        A client with no `run` is a test double or a window with no loop
        (`submit` says so in as many words); there the scheduled write has
        already run synchronously by the time this returns, so the recorded
        state is the one to report.
        """
        from foammesh.core.facade.errors import FacadeError
        from foammesh.view.facade_client import FailedResult

        runner = getattr(self._client, 'run', None)
        if runner is None:
            self._on_task_transition(task_id, transition,
                                     after_save=after_save)
            return self.is_accepted(task_id)
        try:
            result = await runner('mesh.workflow.task_transition', {
                'engine_id': self._engine_id, 'task_id': task_id,
                'transition': transition})
        except FacadeError as error:
            result = FailedResult(error)
        return self._transition_recorded(task_id, result,
                                         after_save=after_save)

    #: Engine id -> the facade operation that runs its whole pipeline. An
    #: engine absent here renders no run button.
    #:
    #: Plan 26 WP5.3. Snappy's whole-pipeline run already existed and worked --
    #: ``StepManager._finishPipeline`` calls ``generate_dictionaries`` then
    #: ``run_pipeline``, fired by "Finish all steps" and the wizard's
    #: "Run & Proceed". The defect was placement: that trigger lives on the
    #: legacy step bar, and ``_showBranchPage`` calls ``hideAll()``, so the
    #: button vanished the moment a user navigated by the snappy tree tokens.
    #: This is additive -- the per-stage buttons and the Finish path both stay.
    #:
    #: Plan 30 WP-03 (F-03). Both entries are the same operation now. Gmsh's
    #: run button used to press ``mesh.gmsh.run``, a second orchestrator that
    #: ran Gmsh whatever the case held; that name survives only as a
    #: deprecated alias, and nothing shipped should still be calling it.
    RUN_OPERATIONS: dict[str, str] = {
        'gmsh': 'workflow.run_pipeline',
        'snappy': 'workflow.run_pipeline',
    }
    #: Extra parameters the whole-pipeline run needs per engine. Snappy's
    #: pipeline operation takes an execution mode; Gmsh's atomic run does not.
    RUN_PARAMETERS: dict[str, dict] = {'snappy': {'mode': 'auto'}}
    #: Engine id -> awaitable that runs that engine's *complete* mesh the way
    #: the window does it (dictionaries, the qualified DAG, verdict, reload).
    #: Installed by the step manager; a branch without one falls back to the
    #: bare facade operation.
    pipeline_runners: dict = {}
    #: Engine id -> awaitable writing the dictionaries that engine's stages
    #: mesh from, over the domain the window reads off the Base grid page.
    #: Installed by the step manager for the engines that need them; a branch
    #: without an entry runs the stage on what is already on disk.
    #:
    #: DP-256. A stage started from a branch row is the same run as a stage
    #: started from the legacy page, and only the legacy page generated
    #: first. One engine needs it, the window is the only surface that knows
    #: the domain, so the window hands the run in rather than this widget
    #: keeping a second opinion about where the block is.
    stage_dictionaries: dict = {}
    ACCEPTED_STATES = frozenset(
        {'passed', 'warning', 'skipped', 'completed', 'waived'})

    async def _write_stage_dictionaries(self, stage: str) -> str:
        """Write the dictionaries ``stage`` meshes from; '' when it worked.

        DP-256. MEASURED by the footer-only walk on `pipe` for snappy: the
        press of `Generate grid & Proceed` on `5. Base grid` committed the
        fields, ran nothing and did not advance, and the case was left with
        no `system/blockMeshDict` and no run log. `workflow.run_stage`
        refuses a stage whose dictionary is not on disk -- "blockMeshDict is
        required; generate dictionaries first" -- and every other route to a
        snappy run writes them first: the legacy Base grid page does it in
        `_generate`, the window's pipeline does it in `_finishPipeline`.
        This route did not, so the guided snappy workflow could not mesh at
        all.

        The dictionaries are rewritten on every run rather than only when
        one is missing: the press that gets here is the press that saved the
        fields it is about to mesh with, and a dictionary from the run
        before is exactly what `_on_run_requested` already refuses to mesh
        against.
        """
        from foammesh.core.facade.errors import FacadeError

        writer = _stage_writer(self)
        # checkMesh reads a mesh, it does not mesh from a dictionary.
        if writer is None or stage == 'checkMesh':
            return ''
        try:
            await writer()
        except (FacadeError, OSError, TypeError, ValueError) as error:
            # A domain that cannot be read is a sentence about the Base grid
            # page, not a traceback: `_snappyDomainBounds` raises it in those
            # words and this is the one place it can be said out loud.
            return str(error)
        return ''

    def _on_run_requested(self, task_id: str) -> None:
        """Run the selected engine's atomic stage; failures stay actionable."""
        from foammesh.view.meshing_method.method_page import _submit

        # DP-123. A shut button is the first answer; this is the second, for
        # a state that changed after it was graded. Refusing here costs
        # nothing, and the run it replaces costs a WSL boot and an import.
        refusal = self.runAllRefusal()
        if refusal and task_id == self._run_all_task_id():
            self._gradeRunAll()
            self._report_run(refusal, failed=True)
            return

        runner = self.pipeline_runners.get(self._engine_id)
        if runner is not None:
            # The window's run generates the dictionaries first and shows the
            # verdict after. The tree's button used to call the pipeline
            # directly, so a snappy run from here meshed against stale
            # dictionaries or none at all.
            self._run_task = asyncio.ensure_future(
                self._run_pipeline(runner, task_id))
            return

        operation = self.RUN_OPERATIONS.get(self._engine_id)
        if operation is None:
            return

        def on_result(result):
            payload = getattr(result, 'payload', {}) or {}
            if getattr(result, 'status', '') == 'accepted':
                # F-09. This used to be a modal raised over the viewport that
                # was, at that moment, drawing the mesh the sentence was about.
                # `_reload_mesh` below shows the mesh; the strip says which run
                # made it, in the bottom bar, without taking the window away.
                #
                # Plan 31 CP-07 item 6. It also says whether the run was
                # serial or parallel and on how many workers, and offers the
                # log. Both were in this payload already; the only place the
                # split had ever been visible was the processor* directories.
                self._report_run(describe_result(payload),
                                 log=newest_log(payload))
            elif self._was_cancelled(payload):
                self._report_cancelled_stage(self.tr('The run'))
            else:
                self._report_run(describe_result(payload, failed=True),
                                 failed=True, log=newest_log(payload))
                self._warn_failure(
                    self.tr('Mesh run'), payload, result,
                    self.tr('The run could not start.'))
            # The gate's verdict rides back on the payload. It was being
            # dropped here, so the only path that ever reached the strip was
            # accepting a refusal -- and a run that passed left the strip
            # on its dormant no-mesh line over the mesh it had just made.
            # Published
            # on refusal too: that is precisely when a user needs the numbers.
            self._publish_verdict(payload)
            if getattr(result, 'status', '') == 'accepted':
                self._reload_mesh()
            self._graph = self._build_graph(self._engine_id)
            self._apply_states()
            self.taskChanged.emit(task_id)

        # DP-506. The Console shows the run while it runs, as the window's
        # own pipeline run does; the subscription ends with the result.
        unsubscribe = self._stream_to_console()
        finish = on_result

        def on_result(result):
            unsubscribe()
            finish(result)

        # The run operations have async facade handlers; run_sync raises.
        task = _submit(self._client, operation,
                       dict(self.RUN_PARAMETERS.get(self._engine_id) or {}),
                       on_result)
        if task is not None:
            self._run_task = task

    def _on_stage_run_requested(self, task_id: str, stage: str) -> None:
        """Run one engine stage. WP5.2's alternative to implicit execution."""
        from foammesh.view.meshing_method.method_page import _submit

        if not stage:
            return
        # checkMesh is not a `workflow.run_stage` stage: it is `mesh.check`,
        # which writes the report the QA row and the summary read, and (D23)
        # records the QA task. A `failed` status there is a verdict on the
        # mesh, not a run that did not happen.
        is_check = stage == 'checkMesh'
        operation = (self._qa_operation_name() if is_check
                     else 'workflow.run_stage')
        parameters = {} if is_check else {'stage': stage}
        # R101. "Run this step" runs exactly what `Run & Proceed` runs and was
        # just as silent about it, so it gets the same modal and the same
        # one-press-one-run guard.
        if self._stage_running:
            return
        self._stage_running = True
        progress = self._open_stage_progress(task_id, stage)
        unsubscribe = self._stream_to_console()

        def released():
            """Give the window back. Idempotent: two callers may reach it."""
            self._stage_running = False
            unsubscribe()
            progress.close()

        def on_result(result):
            released()
            payload = getattr(result, 'payload', {}) or {}
            if getattr(result, 'status', '') == 'accepted':
                self._report_run(self.tr('%s completed.') % stage)
            elif self._was_cancelled(payload):
                self._report_cancelled_stage(stage)
            elif is_check and isinstance(payload.get('parsed'), dict):
                QMessageBox.warning(
                    self, self.tr('Mesh check'),
                    self.tr('checkMesh reported problems with this mesh; '
                            'see the Quality tab.'))
            else:
                self._warn_failure(
                    self.tr('Stage run'), payload, result,
                    self.tr('The stage could not run.'), stage=stage)
            self._publish_verdict(payload)
            if getattr(result, 'status', '') == 'accepted' and not is_check:
                self._reload_mesh()
            page = self._pages.get(task_id)
            if page is not None:
                page.refresh()
            self._graph = self._build_graph(self._engine_id)
            self._apply_states()
            self.taskChanged.emit(task_id)

        def start() -> None:
            task = _submit(self._client, operation, parameters, on_result)
            if task is None:
                # R101. `_submit` returns None only when it already ran the
                # operation synchronously, so the run is over by the time we
                # get here -- give the window back rather than leaving the
                # guard set for the life of the page. Idempotent with
                # `on_result`.
                released()
                return
            self._stage_task = task
            # R101. A run that dies without ever producing a result must not
            # leave a modal with no way out and a button that never re-arms.
            task.add_done_callback(lambda _task: released())

        # DP-256. "Run this step" is the other door onto the same run, and it
        # had the same hole: the stage was submitted against whatever
        # dictionaries happened to be on disk, which on a case that has never
        # been meshed is none at all.
        if is_check or _stage_writer(self) is None:
            start()
            return

        async def generate_then_run() -> None:
            failure = await self._write_stage_dictionaries(stage)
            if failure:
                released()
                QMessageBox.warning(self, self.tr('Stage run'), failure)
                return
            start()

        self._stage_task = asyncio.ensure_future(generate_then_run())

    async def _run_pipeline(self, runner, task_id: str) -> None:
        """Run the window's own pipeline. It draws its own result.

        DP-128. This used to call `_reload_mesh` here as well, and the two
        draws fought: the runner drew the artifact *this* run produced, named
        it, and then this line re-read `constant/polyMesh` over the top of it
        -- twice the read, and the viewport blank for the whole of the second
        one. Worse on a refusal, where the runner deliberately takes the
        failed run's mesh down and offers the previous one *by name*: this
        line put that previous mesh straight back up, silently, underneath
        the new run's verdict. That is the F-37 confusion `loadResult` exists
        to prevent, reintroduced by its own caller.

        `_reload_mesh` still runs for the two paths that genuinely have no
        other draw -- the bare facade run and a single stage run.
        """
        try:
            await runner()
        finally:
            self.refresh_states()
            self.taskChanged.emit(task_id)

    # -- task state queries for the wizard --------------------------------- #

    def task_info(self, task_id: str) -> dict:
        for task in self.workflow_tasks:
            if task.get('task_id') == task_id:
                return dict(task)
        return {}

    def task_state(self, task_id: str) -> str:
        if self._graph is None:
            return ''
        try:
            return str(self._graph.state(task_id).value)
        except Exception:  # noqa: BLE001 - an unknown task has no state
            return ''

    def is_accepted(self, task_id: str) -> bool:
        return self.task_state(task_id) in self.ACCEPTED_STATES

    def is_runnable(self, task_id: str) -> bool:
        if self._graph is None:
            return False
        try:
            return bool(self._graph.is_runnable(task_id))
        except Exception:  # noqa: BLE001
            return False

    def blocking_prerequisites(self, task_id: str) -> tuple:
        """The tasks holding this one shut, as the graph counts them.

        DP-144. The step manager counted them itself, from `depends_on` and
        `is_accepted`, and so named the optional steps the graph stopped
        counting -- "Compute Mesh is waiting on Periodic Pairs".
        """
        if self._graph is None:
            return ()
        try:
            return tuple(self._graph.blocking_prerequisites(task_id))
        except Exception:  # noqa: BLE001
            return ()

    def refresh_states(self) -> None:
        """Re-read the persisted task states and repaint the rows."""
        self._graph = self._build_graph(self._engine_id)
        self._apply_states()

    #: R101. Whether a stage run started here is still in flight. A stage that
    #: shows nothing while it works is pressed again, and the second press
    #: used to start a second run of the same stage on the same case.
    _stage_running = False

    def _open_stage_progress(self, task_id: str, stage: str):
        """Put a stage run on screen the way every other long stage does.

        R101. MEASURED on Surface Features & Refinement: `Run & Proceed` ran
        surfaceFeatures for about a minute with no modal, no busy cursor and
        nothing but small status text in the bottom-right corner, while Base
        Grid, Mesh quality, Export and Load Mesh all raise this same dialog.
        A minute of an apparently idle window reads as a dead button.

        Cancelable (F-22). It was not, because `workflow.run_stage` was said
        to have no cancel path -- but the stage reaches the machine through the
        job manager, which owns the process group it launched, so stopping it
        is one facade call for snappy and for Gmsh alike. A modal with no way
        out over a stage that runs for minutes is the worse defect.

        `autoCloseOnCancel` is off: cancelling is a request, and the dialog
        stays up saying so until the run actually ends and `released()` closes
        it, rather than vanishing over a process that is still running.
        """
        title = str(self.task_info(task_id).get('title') or stage)
        progress = ProgressDialog(self, self.tr('Running %s') % title,
                                  cancelable=True, autoCloseOnCancel=False)
        progress.setLabelText(
            self.tr('%s is running. This can take a few minutes.') % stage)
        progress.cancelClicked.connect(
            lambda: self._cancel_running_stage(progress))
        progress.open()
        return progress

    def _cancel_running_stage(self, progress=None) -> None:
        """Stop the running stage from the surface that is reporting it.

        Everything this branch runs -- a snappy stage, a Gmsh run, a mesh
        check -- goes to the machine through the job manager, so one cancel
        reaches the WSL process group and the Gmsh runner both.
        """
        if progress is not None:
            progress.setLabelText(self.tr('Stopping the run…'))
        client = self._client
        canceller = getattr(client, 'cancel_active_jobs', None)
        if canceller is None:
            canceller = getattr(client, 'cancel_active_job', None)
        if canceller is None:
            return
        try:
            self._cancel_task = asyncio.ensure_future(canceller())
        except RuntimeError:
            # No running loop (bare construction in tests).
            pass

    @staticmethod
    def _was_cancelled(payload: dict) -> bool:
        """Whether this run ended because the user stopped it (C31-12).

        MEASURED against the facade: a cancelled stage comes back as
        `status='failed'` with `payload['job']['status'] == 'cancelled'` and
        no `reason` -- the same shape a crashed utility produces minus the
        message. The job's own status is the only thing that distinguishes
        them, so it is what is read.
        """
        job = payload.get('job') or {}
        return str(job.get('status') or '') == 'cancelled'

    @staticmethod
    def _console():
        """The window's Console, or ``None`` when there is no window."""
        from foammesh.app import app

        try:
            return getattr(app, 'consoleView', None)
        except (AttributeError, RuntimeError):
            # `app.consoleView` reads through a window that may not exist.
            return None

    def _stream_to_console(self):
        """Put the running job's output in the Console; returns the undo.

        DP-506 (MA-02). The window's whole-pipeline run subscribes the Console
        to ``JOB_OUTPUT``; the branch's stage runs did not, so a stage the
        guided route ran left the Console empty. MEASURED on S1_box_cavity:
        castellation died with a ``FOAM FATAL ERROR`` in its log and nothing
        at all in the pane beside the failure.
        """
        from foammesh.core.project import Event

        subscribe = getattr(self._client, 'subscribe', None)
        console = self._console()
        if not callable(subscribe) or console is None:
            return lambda: None
        try:
            unsubscribe = subscribe(
                Event.JOB_OUTPUT,
                lambda **event: console.append(str(event.get('line', ''))))
        except Exception:                                   # noqa: BLE001
            # No session to listen to (a client double, a closed case).
            return lambda: None
        return unsubscribe if callable(unsubscribe) else (lambda: None)

    def _warn_failure(self, title: str, payload: dict, result,
                      fallback: str, *, stage: str = '') -> None:
        """Say why a run failed: the stage, its cause, and where its log is.

        DP-506 (MA-02). This was one sentence -- the payload's ``reason`` or
        "The stage could not run." -- and a failed stage carried no reason,
        so the S1 and S4 castellation failures said exactly that over a log
        naming the unknown region and the valid ones. The facade now reads
        the cause out of the log; this puts it in the modal, names the log
        and its job, keeps the long diagnostic behind **Details**, and writes
        the same cause into the Console so it outlives the modal.
        """
        reason = str(payload.get('reason') or getattr(result, 'message', '')
                     or fallback)
        job = payload.get('job') if isinstance(payload.get('job'), dict) else {}
        details = str(payload.get('details') or '').strip()
        log = str(payload.get('log') or job.get('log_path') or '')
        job_id = str(job.get('job_id') or payload.get('job_id') or '')
        cause = str(payload.get('cause') or '').strip()
        text = (self.tr('%s failed.') % stage + '\n\n' + cause
                if stage and cause else reason)
        console = self._console()
        if console is not None:
            write = getattr(console, 'appendError', None) or console.append
            write('{0}: {1}'.format(title, reason))
            if log:
                write(self.tr('Log: %s') % log)
        if not details and not log:
            QMessageBox.warning(self, title, text)
            return
        where = []
        if job_id:
            where.append(self.tr('Job %s') % job_id)
        if log:
            where.append(self.tr('Log: %s') % log)
        box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                          QMessageBox.StandardButton.Ok, self)
        box.setInformativeText('\n'.join(where))
        if details:
            box.setDetailedText(details)
        box.exec()

    def _report_cancelled_stage(self, what: str) -> None:
        """Say the run was stopped, in the words of the thing that happened.

        C31-12. Pressing Cancel raised `The stage could not run.` in a
        warning box -- the same sentence, in the same alarming shape, as a
        missing dictionary or a snappyHexMesh core dump, over an outcome the
        user had just asked for.

        It goes to the status strip rather than a modal because a cancel is
        not an error and because F-09 already put run outcomes there: a modal
        would take the window away to report something the user did on
        purpose. The state of the case is said out loud, since the one thing
        a stopped mesher leaves behind is a question about what is on disk.
        """
        self._report_run(
            self.tr('%s was cancelled. The mesh is as it was before the run '
                    'started.') % what)

    async def run_stage_async(self, task_id: str, stage: str) -> bool:
        """Run one stage and wait for it. The wizard needs the answer."""
        from foammesh.core.facade.errors import FacadeError

        runner = getattr(self._client, 'run', None)
        if not stage or runner is None:
            return False
        # R101. One press, one run: the modal below blocks the button, and
        # this refuses any press that reaches here another way.
        if self._stage_running:
            return False
        self._stage_running = True
        progress = self._open_stage_progress(task_id, stage)
        unsubscribe = self._stream_to_console()
        result = None
        failure = ''
        try:
            # DP-256. The dictionaries this stage meshes from, written the
            # way every other route to a run writes them, before the run.
            failure = await self._write_stage_dictionaries(stage)
            if not failure:
                result = await runner('workflow.run_stage', {'stage': stage})
        except FacadeError as error:
            failure = str(error)
        finally:
            self._stage_running = False
            unsubscribe()
            # The progress surface goes before anything is said about the
            # run, so a warning is never raised behind a modal still claiming
            # the stage is running (the D6 stacked-dialog shape).
            progress.close()
        if failure:
            QMessageBox.warning(self, self.tr('Stage run'), failure)
        payload = getattr(result, 'payload', {}) or {}
        accepted = getattr(result, 'status', '') == 'accepted'
        if result is not None and not accepted:
            if self._was_cancelled(payload):
                self._report_cancelled_stage(stage)
            else:
                self._warn_failure(
                    self.tr('Stage run'), payload, result,
                    self.tr('The stage could not run.'), stage=stage)
        self._publish_verdict(payload)
        if accepted:
            # DP-763. `Run & Proceed` on a stage page comes here, and the
            # stage's mesh stayed on disk: the viewport kept the STL through
            # base grid, castellation, snap and layers. The tree row's own
            # run (`_on_stage_run_requested`) has always drawn it.
            self._reload_mesh()
        page = self._pages.get(task_id)
        if page is not None:
            page.refresh()
        self.refresh_states()
        self.taskChanged.emit(task_id)
        return accepted

    def _qa_operation_name(self) -> str:
        """Which check this project's QA row runs, per its target solver.

        Read off the facade's own answer rather than re-derived here (F-24):
        this view, the main window and the facade each carried a copy of the
        rule, and three surfaces deciding separately which check judges a mesh
        is three chances to run the wrong one. checkMesh is the answer for
        OpenFOAM and for anything unreadable or unset, because it is what this
        row has always run.
        """
        from foammesh.core.engine.base import qa_operation

        try:
            result = query(self._client, 'mesh.engine.list')
            payload = getattr(result, 'payload', None) or {}
        except Exception:
            return qa_operation(None)
        return str(payload.get('qa_operation')
                   or qa_operation(payload.get('target_solver')))

    async def run_mesh_check_async(self, task_id: str) -> bool:
        """Run checkMesh on the case-root mesh and wait for it.

        The QA row is run-gated, so Proceed has to run something. ``failed``
        here means the mesh has problems, not that the check did not run:
        the report exists either way and the task advances (as a warning
        for a poor mesh), and the verdict strip shows what checkMesh said.
        """
        from foammesh.core.facade.errors import FacadeError

        runner = getattr(self._client, 'run', None)
        if runner is None:
            return False
        result = None
        try:
            result = await runner(self._qa_operation_name())
        except FacadeError as error:
            QMessageBox.warning(self, self.tr('Mesh check'), str(error))
        payload = getattr(result, 'payload', {}) or {}
        self._publish_verdict(payload)
        page = self._pages.get(task_id)
        if page is not None:
            page.refresh()
        self.refresh_states()
        self.taskChanged.emit(task_id)
        return result is not None and self.is_accepted(task_id)

    def _reload_mesh(self) -> None:
        """Draw the mesh a stage has just written (F10).

        Nothing did this for a branch run: Gmsh finished 29,273 cells and the
        toolbar still read `0 cells` over the imported STL, so the only place
        the mesh existed was on disk. The legacy snappy pages have always
        reloaded after their own stage; this is the same call, made from the
        one place every branch run passes through.

        A mesh that cannot be drawn is not a reason to report the run as
        failed -- but it is not nothing either, and it used to be discarded in
        silence (R95/R156). The failure is logged and put on the status bar
        instead, so "the run worked and the viewport is empty" is a statement
        the product makes rather than one the user has to infer.
        """
        from foammesh.app import app

        window = getattr(app, 'window', None)
        manager = getattr(window, 'meshManager', None)
        if manager is None:
            return
        try:
            self._reload_task = asyncio.ensure_future(self._load_mesh(manager))
        except RuntimeError:
            # No running loop (bare construction in tests).
            pass

    async def _load_mesh(self, manager) -> None:
        from foammesh.app import app

        try:
            await manager.load(0)
        except Exception as error:                          # noqa: BLE001
            logger.warning('could not draw the mesh that was just written',
                           exc_info=True)
            window = getattr(app, 'window', None)
            statusbar = getattr(getattr(window, '_ui', None), 'statusbar', None)
            if statusbar is not None:
                statusbar.showMessage(
                    self.tr('The mesh was written but could not be drawn: %s')
                    % error, 10000)

    def _report_run(self, message: str, *, failed: bool = False,
                    log: str = '') -> None:
        """Say what a run did on the window's status strip, never in a modal.

        Reached through ``app.window`` for the same reason `_publish_verdict`
        is: this view is built by the step manager and holds no window handle,
        and a branch that cannot find a window must still run meshes.
        """
        from foammesh.app import app

        window = getattr(app, 'window', None)
        show = getattr(window, 'showRunStatus', None)
        if not callable(show):
            return
        try:
            show(message, failed=failed, log=log)
        except TypeError:
            # An older window (or a test double) that predates the log.
            show(message, failed=failed)

    def _publish_verdict(self, payload: dict) -> None:
        """Hand a run's quality verdict to the window's strip and Quality tab.

        Looked up through ``app.window`` rather than held as a reference: this
        view is constructed by the step manager and has no window handle, and a
        branch that cannot find one must still run meshes.
        """
        verdict = payload.get('quality_verdict')
        if not verdict:
            return
        # Plan 31 CP-05 item 4. Which candidate this verdict is about. The
        # strip's **Accept anyway** hands it straight back to the facade, and
        # without it the only thing acceptance could name was "the case", so
        # it re-meshed to get a run to name.
        verdict = dict(verdict, run_id=str(payload.get('run_id') or ''))
        from foammesh.app import app

        window = getattr(app, 'window', None)
        show = getattr(window, 'showMeshVerdict', None)
        if callable(show):
            show(dict(verdict))

    # -- navigation -------------------------------------------------------- #

    def currentTask(self) -> str:
        """The task whose page is on screen, or ``''`` for none.

        DP-138. The outline has to put its highlight back after it rebuilds
        its rows, and it cannot do that without being able to ask what is on
        screen. Without an answer the highlight stayed where Qt dropped it,
        which is the section header.
        """
        row = self.tasks.currentRow()
        return self._order[row] if 0 <= row < len(self._order) else ''

    def taskIsLocked(self, task_id: str) -> bool:
        """Whether the workflow has not reached ``task_id`` yet.

        Plan 32 §7.1. The lock is the graph's own `LOCKED` and nothing else:
        a second frontier kept beside it would be a second answer to drift
        out of step with the first.
        """
        return self.task_state(task_id) == TaskState.LOCKED.value

    def lockRefusal(self, task_id: str) -> str:
        """Why ``task_id`` will not open, or ``''`` if it will.

        By title, never by id. DP-39 already found what an id-shaped refusal
        reads like to the person holding it, and the graph knows which
        prerequisite is actually in the way -- `blocking_prerequisites` steps
        through the optional parents the run would skip, so the name here is
        a step the reader really does have to do.
        """
        if not self.taskIsLocked(task_id):
            return ''
        titles = {task.get('task_id'): (task.get('title') or task.get('task_id'))
                  for task in self.workflow_tasks}
        mine = titles.get(task_id, task_id)
        blocking = self.blocking_prerequisites(task_id)
        if not blocking:
            return self.tr('{0} is not available yet.').format(mine)
        return self.tr('{0} opens once {1} is finished.').format(
            mine, titles.get(blocking[0], blocking[0]))

    def selectRefusal(self, task_id: str) -> str:
        """Why standing on ``task_id`` is refused, or ``''`` to allow it.

        Plan 32 fills the seam it left here with the frontier lock. Both
        doors into a task page come through `select` -- the outline row, via
        `MeshingMethodBranch.widgetForToken`, and the branch's own task list
        beside the page -- so the refusal has one place to live rather than
        two that can disagree about which step is open. A lock enforced on
        one door and not the other is a lock with a handle beside it.
        """
        if self.lockBypass:
            return ''
        return self.lockRefusal(task_id)

    def select(self, task_id: str) -> bool:
        if task_id not in self._pages:
            return False
        if self.selectRefusal(task_id):
            return False
        self.tasks.setCurrentRow(self._order.index(task_id))
        return True

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._order):
            return
        self.stack.setCurrentIndex(row)
        self._updateHeading()
        self._gradeRunAll()
        self.taskSelected.emit(self._order[row])

    def _updateHeading(self) -> None:
        """Title the panel with the task on screen, not with the engine."""
        row = self.tasks.currentRow()
        task_id = self._order[row] if 0 <= row < len(self._order) else ''
        title = self.taskTitles().get(task_id, '') if task_id else ''
        self._heading.setText(title or self._engineName)
        self._heading.setToolTip(
            self.tr('{0} task of the {1} workflow').format(
                title, self._engineName)
            if title else self._engineName)
