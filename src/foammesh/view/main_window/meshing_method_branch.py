"""Mount Meshing Method and the selected engine's task branch in the shell.

Plan 17 §5.1 places Meshing Method immediately after the repair decision, and
populates only the selected engine's downstream tasks beneath it. SH8 forbids
giving those tasks numeric ``Step`` values, so the node and its children are
token-routed and this module owns them end to end: tree nodes, content pages,
and the routing between them.

The pages are built lazily. ``EngineBranchView`` queries the facade on
construction, and no case exists when ``MainWindow`` is first assembled.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from foammesh.core.engine.contracts import TaskState
from foammesh.db.configurations_schema import Step
from foammesh.view.workflow_controls.field_group_page import (
    ExecutionPreferencesPage, MeshIntentPage,
)
from .naviagtion_view import BASE_LABEL_ROLE, TOKEN_ROLE, WorkflowRowState


#: Token for the Meshing Method node itself.
METHOD_TOKEN = 'meshing_method'
#: Prefix for engine task child routes.
TASK_TOKEN_PREFIX = 'engine_task:'

#: How an engine task state renders in the navigation tree.
#:
#: Module level rather than inline so it can be asserted exhaustive: the lookup
#: falls back to ``AVAILABLE``, which for an evidenced or waived gate would
#: render finished work as unstarted and leave the row enabled. A state missing
#: here is a silent presentation bug, so a test checks the whole enum is
#: covered.
TASK_ROW_STATE = {
    TaskState.LOCKED: WorkflowRowState.LOCKED,
    TaskState.READY: WorkflowRowState.AVAILABLE,
    TaskState.EDITING: WorkflowRowState.CURRENT,
    # R94. Not COMPLETED: the settings exist, the stage has not run.
    TaskState.CONFIGURED: WorkflowRowState.CONFIGURED,
    # A running stage has its own mark. It used to share CURRENT with "the
    # row you are standing on", so the outline could not say which of the two
    # it meant.
    TaskState.RUNNING: WorkflowRowState.RUNNING,
    TaskState.PASSED: WorkflowRowState.COMPLETED,
    TaskState.WARNING: WorkflowRowState.WARNING,
    TaskState.FAILED: WorkflowRowState.FAILED,
    TaskState.SKIPPED: WorkflowRowState.SKIPPED,
    TaskState.STALE: WorkflowRowState.STALE,
    # Evidence exists; the mesh was measured, not approved (Plan 23 §8.5).
    TaskState.COMPLETED: WorkflowRowState.EVIDENCED,
    TaskState.WAIVED: WorkflowRowState.WAIVED,
}


class MeshingMethodBranch(QObject):
    """Owns the Meshing Method node, its engine pages, and their routing."""

    pageRequested = Signal(object)

    def __init__(self, ui, navigation, client_factory, *, parent=None):
        super().__init__(parent)
        self._ui = ui
        self._navigation = navigation
        self._client_factory = client_factory
        #: Engine id -> awaitable running that engine's complete mesh; handed
        #: to the branch view when it is built (see EngineBranchView).
        self.pipeline_runners: dict = {}
        self._methodPage = None
        self._branch = None
        self._currentToken = METHOD_TOKEN
        # Anchored on Repair, the last numbered stage the outline owns, and
        # numbered in sequence with it: an unnumbered row wedged between step 2
        # and step 3 is a numbering that describes no order.
        #
        # R10/R13. These used to be 4/5/6, anchored on a top-level `3. Region`
        # row that the wizard skipped and that stayed lock-greyed for a whole
        # run while regions were being created on this branch's own
        # `Domain & Regions` child. That row is gone, so the numbering now
        # counts the stages the wizard actually walks.
        # Plan 26 WP4. Eleven fields declared a ui_location and no view module
        # rendered any of them -- including maxCpuCores, which the meshing run
        # reads for its rank count. They are engine-agnostic, so they mount
        # beside Meshing Method rather than inside either engine's branch.
        #
        # R209. They mount *first*. Mesh Intent says what the mesh is for, and
        # since Plan 28 the target solver is what chooses the engine, so
        # numbering it 4 -- below the engine branch it decides, and below the
        # thirteen task rows that branch unrolls -- described the reverse of
        # the order the work happens in. Both rows sit above Geometry now.
        self._fieldPages: dict[str, object] = {}
        self._fieldPageClasses = {
            page_class.token: page_class
            for page_class in (MeshIntentPage, ExecutionPreferencesPage)
        }
        for label, token in ((self.tr('1. Mesh Intent'),
                              MeshIntentPage.token),
                             (self.tr('2. Execution'),
                              ExecutionPreferencesPage.token)):
            navigation.installBranchNode(label, token, Step.GEOMETRY_REPAIR,
                                         atTop=True)
        self._node = navigation.installBranchNode(
            self.tr('5. Meshing Method'), METHOD_TOKEN, Step.GEOMETRY_REPAIR)
        navigation.branchRequested.connect(self._onBranchRequested)

    # -- lazy construction ------------------------------------------------- #

    @property
    def methodPage(self):
        return self._methodPage

    @property
    def branch(self):
        return self._branch

    def isLoaded(self) -> bool:
        return self._branch is not None

    @property
    def currentToken(self) -> str:
        return self._currentToken

    def load(self) -> bool:
        """Build or refresh the pages once a case is open."""
        client = self._client_factory()
        if client is None:
            return False
        try:
            # A client exists from startup, but its case session attaches only
            # when a project opens; clicking the node before that must leave a
            # usable shell rather than raise.
            client.case_id
        except Exception:
            return False
        if self._methodPage is None:
            from foammesh.view.meshing_method.method_page import MeshingMethodPage
            from .engine_branch import EngineBranchView
            self._methodPage = MeshingMethodPage(client, self._ui.content)
            self._ui.content.addWidget(self._methodPage)
            self._branch = EngineBranchView(client, self._ui.content)
            self._branch.pipeline_runners = dict(self.pipeline_runners)
            self._ui.content.addWidget(self._branch)
            self._methodPage.engineChanged.connect(self._onEngineChanged)
            # Task acceptance/revert must re-sync the tree labels and lock
            # state; without this the navigation goes stale until an engine
            # switch or full reload.
            self._branch.taskChanged.connect(lambda _tid: self._syncChildren())
        else:
            # force=False: a runtime probe is a fact about the machine and
            # is already cached; re-opening this node must not re-boot WSL.
            self._methodPage.refresh(force=False)
            self._branch.refresh()
        self._syncChildren()
        return True

    def _onEngineChanged(self, _engine_id: str) -> None:
        # Switching engines replaces the visible branch; the previous engine's
        # state is retained by the facade, not by these widgets.
        self._branch.refresh()
        self._syncChildren()

    def _syncChildren(self) -> None:
        if self._branch is None:
            return
        entries = []
        graph = self._branch.graph
        for task in self._branch.workflow_tasks:
            task_id = task.get('task_id')
            # Plan 30 WP-09 (F-17). One page system: a task is in the tree if
            # and only if the engine branch has a page for it. This used to
            # consult a hand-written table in StepManager as well, which meant
            # the tree could disagree with the workflow descriptor in two
            # directions at once; that table is gone.
            if self._branch.page(task_id) is None:
                continue
            label = task.get('title') or task_id
            state = TaskState.READY
            if graph is not None:
                try:
                    state = graph.state(task_id)
                except Exception:
                    state = TaskState.READY
            row_state = TASK_ROW_STATE.get(state, WorkflowRowState.AVAILABLE)
            enabled = state is not TaskState.LOCKED
            entries.append((
                label, TASK_TOKEN_PREFIX + task_id, enabled,
                row_state.value))
        self._navigation.setBranchChildren(METHOD_TOKEN, entries)

    def _fieldPage(self, token: str):
        """Build a WP4 field page on demand, once per token."""
        page = self._fieldPages.get(token)
        if page is None:
            client = self._client_factory()
            if client is None:
                return None
            page = self._fieldPageClasses[token](client, self._ui.content)
            self._ui.content.addWidget(page)
            self._fieldPages[token] = page
        else:
            page.reload()
        self._navigation.setBranchCurrent(token)
        return page

    # -- routing ----------------------------------------------------------- #

    def _onBranchRequested(self, token: str) -> None:
        self.route(token)

    def route(self, token: str) -> bool:
        """Stand on a branch task, as clicking its outline row would.

        R168 wants this from outside the outline: reopening a case the user
        just named has to put them back on the task they were on, and going
        through the same door the outline uses is what keeps the token, the
        highlight and the page in step with each other.
        """
        widget = self.widgetForToken(token)
        if widget is None:
            return False
        self._currentToken = token
        self.pageRequested.emit(widget)
        return True

    def routeTokens(self) -> tuple[str, ...]:
        """Every token the outline can currently route to."""
        return self._navigation.routeTokens()

    def firstAvailableTaskToken(self) -> str | None:
        node = self._navigation.branchNode(METHOD_TOKEN)
        if node is None:
            return None
        for row in range(node.rowCount()):
            item = node.child(row)
            if item.isEnabled():
                return str(item.data(TOKEN_ROLE) or '')
        return None

    def nextAvailableTaskToken(self, current: str) -> str | None:
        """The next row after ``current`` that still wants something.

        A7. This used to answer "the first enabled row after this one", and an
        enabled row is not the same as an unfinished one: settling a late task
        re-opened an early one the user had already accepted, walking the
        outline backwards. Accepted rows are skipped now, and when ``current``
        is not a row of this branch at all -- a legacy step page carries a
        stale token -- the walk starts at the top rather than returning
        nothing. It stops at the end instead of wrapping.
        """
        node = self._navigation.branchNode(METHOD_TOKEN)
        if node is None:
            return None
        tokens = [str(node.child(row).data(TOKEN_ROLE) or '')
                  for row in range(node.rowCount())]
        try:
            start = tokens.index(current) + 1
        except ValueError:
            start = 0
        for row in range(start, node.rowCount()):
            item = node.child(row)
            if not item.isEnabled() or self.isAcceptedToken(tokens[row]):
                continue
            return tokens[row]
        return None

    def isAcceptedToken(self, token: str) -> bool:
        """Whether the task a branch token names is already settled."""
        if self._branch is None or not token.startswith(TASK_TOKEN_PREFIX):
            return False
        try:
            return bool(self._branch.is_accepted(token[len(TASK_TOKEN_PREFIX):]))
        except Exception:                                    # noqa: BLE001
            return False

    def firstBlockedTask(self) -> tuple[str, str] | None:
        """The first row the branch will not let the user open.

        A8. Proceed used to do nothing at all when the next task was locked --
        no dialog, no status line, no outline change -- so a blocked button and
        a broken button looked identical. The caller uses this to say which row
        is in the way, and to offer to open it.
        """
        node = self._navigation.branchNode(METHOD_TOKEN)
        if node is None:
            return None
        for row in range(node.rowCount()):
            item = node.child(row)
            if not item.isEnabled():
                return (str(item.data(TOKEN_ROLE) or ''),
                        str(item.data(BASE_LABEL_ROLE) or item.text()))
        return None

    def taskTitles(self) -> dict:
        """Task id to the title the outline shows for it."""
        if self._branch is None:
            return {}
        titles = {}
        for task in self._branch.workflow_tasks:
            task_id = task.get('task_id')
            if task_id:
                titles[task_id] = task.get('title') or task_id
        return titles

    def widgetForToken(self, token: str):
        """Resolve a navigation token to the content widget that serves it."""
        if not self.isLoaded() and not self.load():
            return None
        if token in self._fieldPageClasses:
            return self._fieldPage(token)
        if token == METHOD_TOKEN:
            self._navigation.setBranchCurrent(token)
            return self._methodPage
        if token.startswith(TASK_TOKEN_PREFIX):
            task_id = token[len(TASK_TOKEN_PREFIX):]
            if self._branch.select(task_id):
                self._navigation.setBranchCurrent(token)
                return self._branch
        return None
