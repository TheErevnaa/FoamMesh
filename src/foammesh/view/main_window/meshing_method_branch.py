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

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QMenu

from foammesh.core.engine.contracts import TaskState
from foammesh.db.configurations_schema import Step
from .naviagtion_view import BASE_LABEL_ROLE, TOKEN_ROLE, WorkflowRowState


#: Token for the Mesh setup node itself. The string is unchanged from when the
#: row was called Meshing Method: it is persisted in every saved case through
#: `routeMemo`, so renaming it would strand them.
METHOD_TOKEN = 'meshing_method'
#: Prefix for engine task child routes.
TASK_TOKEN_PREFIX = 'engine_task:'

#: Plan 32 W1. Routes that used to be outline rows of their own and are now
#: bands of a shared page. A saved `routeMemo`, a reopened case and the
#: strict-GUI harness all address rows by token, so the tokens keep answering
#: -- with the page that now asks what the deleted row asked.
#:
#: A `None` value means the token is served by a numbered step page rather
#: than by this branch; it is not a route of this branch and `route` refuses
#: it, which is what sends the caller to `Step.GEOMETRY_REPAIR` instead.
LEGACY_TOKEN_ALIASES = {
    'workflow.mesh.intent': METHOD_TOKEN,        # was `1. Mesh intent`
    'preferences.execution': METHOD_TOKEN,       # was `2. Execution`
    # Hosted on `3. Preparation` (Plan 32 section 4.4).
    TASK_TOKEN_PREFIX + 'gmsh.describe_geometry': None,
    # Plan 33 CURVE-06. Hosted on `Size fields` as the edge control section:
    # one question -- where is the mesh finer than the global size -- asked
    # on one step. A case saved while the curve row was open reopens on the
    # step that now holds the table.
    TASK_TOKEN_PREFIX + 'gmsh.curve_controls':
        TASK_TOKEN_PREFIX + 'gmsh.size_fields',
}

#: Tasks whose settings are hosted on a shared page, so they get no outline
#: row of their own. The task still exists, still has prerequisites and still
#: has to be settled -- it is settled from the page that hosts it.
HOSTED_TASKS = frozenset({'gmsh.describe_geometry'})

#: Plan 32 §4.2/§4.3. One outline row, in order, settles these tasks.
#:
#: The first entry is the row's own task -- the one whose page the row shows
#: and whose id the row's token carries -- and the rest are substeps that go
#: behind the same press. A task that appears as a substep gets no row of its
#: own: readiness, fidelity, resolution and summary ask the user nothing, they
#: read what the step above them produced and write a report, and listing them
#: as steps made a workflow of eight decisions read as thirteen.
#:
#: Written out rather than inferred. "Every task between this row and the next
#: visible one" would swallow a task added later into whichever row happened to
#: precede it with nobody deciding that, and the gap between two rows is not a
#: fact about anything: `common.export` is order 80 on snappy and order 110 on
#: Gmsh. The map is the decision; `test_every_task_belongs_to_exactly_one_row`
#: is the check that no descriptor task is missing from it.
ROW_TASKS: dict[str, dict[str, tuple[str, ...]]] = {
    'snappy': {
        'snappy.domain_regions': ('snappy.domain_regions',
                                  'common.reference_readiness'),
        'snappy.base_grid': ('snappy.base_grid',),
        'snappy.surface_features': ('snappy.surface_features',),
        'snappy.castellation': ('snappy.castellation',),
        'snappy.snap': ('snappy.snap', 'snappy.fidelity_snap'),
        'snappy.layers': ('snappy.layers',),
        'snappy.qa': ('snappy.qa', 'common.fidelity',
                      'common.resolution', 'common.summary'),
        'common.export': ('common.export',),
    },
    'gmsh': {
        'gmsh.global_sizing': ('gmsh.global_sizing',
                               'common.reference_readiness'),
        'gmsh.size_fields': ('gmsh.size_fields', 'gmsh.curve_controls'),
        'gmsh.volume_controls': ('gmsh.volume_controls',),
        'gmsh.boundary_layers': ('gmsh.boundary_layers',),
        'gmsh.periodic': ('gmsh.periodic',),
        'gmsh.compute': ('gmsh.compute', 'gmsh.fidelity_native',
                         'gmsh.publish'),
        'gmsh.qa': ('gmsh.qa', 'common.fidelity',
                    'common.resolution', 'common.summary'),
        'common.export': ('common.export',),
    },
}

#: What the first engine row is numbered. The three rows above it are the
#: shared ones -- `1. Geometry`, `2. Mesh setup`, `3. Preparation` -- so the
#: engine rows continue the count the outline already started rather than
#: sitting under it unnumbered.
ROW_NUMBER_BASE = 4

#: Plan 32 §4.2/§4.3's other column: what the forward button says on each row,
#: and what it says about itself when the pointer rests on it.
#:
#: Beside ROW_TASKS because it is the same decision seen from the footer: a row
#: is the set of tasks one press settles, and the label is the name of that
#: press. Keeping the two apart is how `Run this step` came to stand over nine
#: different acts -- the footer had its own vocabulary, three words wide, and
#: nothing tied it to what the press actually did.
#:
#: The ampersands are written singly. `&` is Qt's mnemonic marker, so the
#: reader of this table sees the label as the user reads it and the doubling
#: happens once, at the button, in `StepManager._updateWizardActions`.
PROCEED_LABELS: dict[str, dict[str, tuple[str, str]]] = {
    'snappy': {
        'snappy.domain_regions': (
            'Proceed',
            'Saves the domain and its regions, then opens the next step'),
        'snappy.base_grid': (
            'Generate grid & Proceed',
            'Runs blockMesh, then opens the next step'),
        'snappy.surface_features': (
            'Extract features & Proceed',
            'Runs surface feature extraction, then opens the next step'),
        'snappy.castellation': (
            'Castellate & Proceed',
            'Runs the castellation pass, then opens the next step'),
        'snappy.snap': (
            'Snap & Proceed',
            'Runs the snap pass and measures snap fidelity, then opens the '
            'next step'),
        'snappy.layers': (
            'Apply layers & Proceed',
            'Runs the layer addition pass, then opens the next step'),
        'snappy.qa': (
            'Check & Proceed',
            'Runs checkMesh and the qualification checks, then opens the '
            'next step'),
        'common.export': (
            'Export',
            'Writes the mesh to the destination this step names'),
    },
    'gmsh': {
        'gmsh.global_sizing': (
            'Proceed',
            'Saves the global sizing, then opens the next step'),
        'gmsh.size_fields': (
            'Proceed',
            'Saves the sizing on this step, or records a skip where there '
            'is none'),
        'gmsh.volume_controls': (
            'Proceed',
            'Saves these volume controls, or records a skip when there are '
            'none'),
        'gmsh.boundary_layers': (
            'Proceed',
            'Saves this layer specification, or records a skip when layers '
            'are off'),
        'gmsh.periodic': (
            'Proceed',
            'Saves these periodic pairs, or records a skip when there are '
            'none'),
        'gmsh.compute': (
            'Generate & Proceed',
            'Runs Gmsh and publishes the mesh, then opens the next step'),
        'gmsh.qa': (
            'Check & Proceed',
            'Runs checkMesh and the qualification checks, then opens the '
            'next step'),
        'common.export': (
            'Export',
            'Writes the mesh to the destination this step names'),
    },
}

#: What the footer says where this table has nothing to say -- the method
#: page, the three shared rows above the engine, a token for a task no row
#: owns. `Proceed` is the honest name for a press that saves a form and opens
#: the next one, which is what all of those do.
DEFAULT_PROCEED: tuple[str, str] = (
    'Proceed', 'Save this step and open the next one')


def row_tasks(engine_id: str, task_id: str) -> tuple[str, ...]:
    """Every task one press on ``task_id``'s row settles, in order.

    Asked of a substep it answers the row that owns it, because that is the
    press that settles the substep. Asked of anything this map does not know
    -- an engine with no map, a task added to a descriptor and to no row --
    it answers with the task alone, which is what the outline did for every
    task before the map existed.
    """
    rows = ROW_TASKS.get(engine_id) or {}
    entry = rows.get(task_id)
    if entry is not None:
        return entry
    owner = owning_row(engine_id, task_id)
    if owner is not None:
        return rows[owner]
    return (task_id,)


def owning_row(engine_id: str, task_id: str) -> str | None:
    """The row whose press settles ``task_id``, or ``None`` if no row does."""
    for row, entry in (ROW_TASKS.get(engine_id) or {}).items():
        if task_id in entry:
            return row
    return None


def substep_tasks(engine_id: str) -> frozenset[str]:
    """Every task that is settled by another row's press, so has no row."""
    rows = ROW_TASKS.get(engine_id) or {}
    return frozenset(task_id
                     for entry in rows.values()
                     for task_id in entry[1:])


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

#: How bad a row state is, for the fold that carries a member state up into
#: the row that owns it. DP-259 folded FAILED and nothing else, so a member
#: left STALE or WARNING never reached the line the reader reads (DP-272).
#:
#: RUNNING is in the table without being foldable, and that is the whole
#: reason the table exists. MEASURED: `EngineWorkflowGraph.configure`
#: invalidates the descendants of the task it configures, so the press that
#: re-runs a settled row stales every member of it before the run starts --
#: `10. Generate mesh` read `running` with `gmsh.publish` reading `stale` at
#: the same instant. What is happening now outranks a record of what was.
#: FAILED stays above it: DP-259 folded a failure past every row state but
#: LOCKED, and nothing measured here is an argument for folding it less.
ROW_STATE_FOLD_RANK = {
    WorkflowRowState.WARNING: 1,
    WorkflowRowState.STALE: 2,
    WorkflowRowState.RUNNING: 3,
    WorkflowRowState.FAILED: 4,
}

#: The member states a row repeats. A member that is merely running, ready or
#: locked is the ordinary middle of a press and says nothing the row has to
#: carry; these three are news.
FOLDED_MEMBER_STATES = (TaskState.FAILED, TaskState.STALE, TaskState.WARNING)


class MeshingMethodBranch(QObject):
    """Owns the Meshing Method node, its engine pages, and their routing."""

    pageRequested = Signal(object)
    #: Plan 37 UF5 DP-1084. The engine branch's set of result-locked tasks
    #: changed (an unlock, an undo, or a run that published a stage).
    resultLocksChanged = Signal()

    #: Plan 32 §7.3. Off, for everybody, unless something turns it on.
    #:
    #: The strict-GUI harness and the UX audit walk every row of a fresh case
    #: on purpose, so they get a door through the lock; nothing under `src`
    #: touches it, and `tests/unit/test_the_harness_bypass_is_the_only_bypass`
    #: is what keeps that true. A class attribute rather than a constructor
    #: argument because the two call sites arm it around one act and put it
    #: straight back, and a window that was never told anything must refuse.
    lockBypass = False

    def __init__(self, ui, navigation, client_factory, *, parent=None):
        super().__init__(parent)
        self._ui = ui
        self._navigation = navigation
        self._client_factory = client_factory
        #: Engine id -> awaitable running that engine's complete mesh; handed
        #: to the branch view when it is built (see EngineBranchView).
        self.pipeline_runners: dict = {}
        #: Engine id -> awaitable writing that engine's dictionaries; handed
        #: to the branch view with the runners above (DP-256).
        self.stage_dictionaries: dict = {}
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
        # Plan 32 section 4.1. The one decision that shapes the workflow is
        # asked once, on this node, and it is anchored on Geometry rather than
        # on Repair: you choose the method for a geometry you have loaded, and
        # what you then have to prepare depends on the method you chose. So
        # the outline reads Geometry, Mesh setup, Preparation.
        #
        # R209 put two further rows above Geometry -- `1. Mesh intent`, which
        # asked which solver the mesh was for, and `2. Execution`, which asked
        # how much of the machine a run could have. The first asked the same
        # question this node asks, with a second radio group writing the same
        # field; the second held thirteen settings that all have working
        # defaults. Both are bands of this node's page now (see
        # LEGACY_TOKEN_ALIASES), and neither is a step.
        self._node = navigation.installBranchNode(
            self.tr('2. Mesh setup'), METHOD_TOKEN, Step.GEOMETRY)
        # DP-271. `3. Preparation` is walked between this row and the engine
        # rows, and the engine rows are children of this node -- so while it
        # stayed at the root the outline painted it *below* all of them and
        # read 1, 2, 4..11 or 4..12, 3. It leads this band instead, so the
        # band reads 3, 4, 5 downwards and the paint is the walk.
        navigation.nestStepInBranch(Step.GEOMETRY_REPAIR, METHOD_TOKEN)
        navigation.branchRequested.connect(self._onBranchRequested)
        # Plan 37 UF5 DP-1043. A locked step is unlocked from its own row:
        # right-click, "Unlock and discard later results...". The outline is
        # the navigation's widget; the menu is this branch's, because the
        # rows it acts on are.
        tree = getattr(navigation, '_tree', None)
        if tree is not None and hasattr(tree, 'customContextMenuRequested'):
            tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tree.customContextMenuRequested.connect(self._onOutlineMenu)

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

    def proceedLabel(self, token: str) -> tuple[str, str]:
        """What the forward button says on ``token``'s row, and why.

        Answered here because the row is a fact about the engine's workflow,
        not about the wizard shell: the same token has to give the same answer
        whether the footer, a test or a screenshot harness asks. A substep is
        answered by the row whose press settles it -- `row_tasks` does that --
        so a token left pointing at a folded-away task still labels the button
        with the act the press performs rather than falling back to the
        generic word.

        Unknown engine, unknown task, the method page: `DEFAULT_PROCEED`.
        Refusing to answer is not available to a button that has to carry
        some text.
        """
        if not token.startswith(TASK_TOKEN_PREFIX):
            return DEFAULT_PROCEED
        engine_id = str(getattr(self._branch, 'engine_id', '') or '')
        rows = PROCEED_LABELS.get(engine_id)
        if not rows:
            return DEFAULT_PROCEED
        task_id = token[len(TASK_TOKEN_PREFIX):]
        return rows.get(owning_row(engine_id, task_id) or task_id,
                        DEFAULT_PROCEED)

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
            self._branch.stage_dictionaries = dict(self.stage_dictionaries)
            # Plan 32 §7.3. The branch enforces the same lock on its own task
            # list, so it has to be told the same answer about the bypass. A
            # harness that armed the flag before the pages existed would
            # otherwise have armed only half of the door.
            self._branch.lockBypass = self.lockBypass
            self._ui.content.addWidget(self._branch)
            self._methodPage.engineChanged.connect(self._onEngineChanged)
            # Task acceptance/revert must re-sync the tree labels and lock
            # state; without this the navigation goes stale until an engine
            # switch or full reload.
            self._branch.taskChanged.connect(lambda _tid: self._syncChildren())
            self._branch.resultLocksChanged.connect(self.resultLocksChanged)
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
        # An engine with no map in `ROW_TASKS` -- none is chosen yet, or one
        # arrived after this module -- folds no rows away and numbers what it
        # has, rather than emptying the outline or raising inside a repaint.
        engine_id = str(getattr(self._branch, 'engine_id', '') or '')
        substeps = substep_tasks(engine_id)
        entries = []
        graph = self._branch.graph
        number = ROW_NUMBER_BASE
        for task in self._branch.workflow_tasks:
            task_id = task.get('task_id')
            # Plan 30 WP-09 (F-17). One page system: a task is in the tree if
            # and only if the engine branch has a page for it. This used to
            # consult a hand-written table in StepManager as well, which meant
            # the tree could disagree with the workflow descriptor in two
            # directions at once; that table is gone.
            if self._branch.page(task_id) is None:
                continue
            # Plan 32 section 4.4. A hosted task keeps its page, its
            # prerequisites and its place in the workflow graph; what it loses
            # is a row of its own, because its settings are asked on a page
            # the user already has to visit.
            if task_id in HOSTED_TASKS:
                continue
            # Plan 32 §4.2/§4.3. A substep is settled by the row above it and
            # has no row of its own; the descriptor's own `order` still
            # decides the sequence of the rows that remain, so the number
            # below is a rank over the survivors rather than a second
            # hand-kept list to fall out of step with the first.
            if task_id in substeps:
                continue
            title = task.get('title') or task_id
            label = f'{number}. {title}'
            number += 1
            state = TaskState.READY
            if graph is not None:
                try:
                    state = graph.state(task_id)
                except Exception:
                    state = TaskState.READY
            row_state = TASK_ROW_STATE.get(state, WorkflowRowState.AVAILABLE)
            # Plan 32 §5.4. `WorkflowRowState.OPTIONAL` has had its own mark
            # since R166 and nothing emitted it, so five of the nine Gmsh rows
            # drew the same hollow circle as `4. Global sizing` and read as
            # work the user had to do. Only while the row is unsettled: once
            # something has happened to it -- run, skipped, configured, locked
            # behind a prerequisite -- that is what the row says, because what
            # happened outranks what was once allowed not to happen.
            if (row_state is WorkflowRowState.AVAILABLE
                    and task.get('optional')):
                row_state = WorkflowRowState.OPTIONAL
            # Plan 32 §4.2/§4.3. A row is the set of tasks one press settles,
            # so the row carries the worst news any of them has. This was
            # painted from the row task alone, and the substeps were folded
            # away in W1, so a failure inside a press had nowhere left to
            # show: MEASURED with the row task passed and one substep failed,
            # `5. Quality`, `8. Snap` and `Generate mesh` all painted the
            # green completed mark over a press that did not finish, on both
            # engines.
            #
            # DP-272. Only FAILED was folded, and a member has no row of its
            # own, so a member left STALE by an upstream change or left
            # WARNING by its own run was a state the reader never saw at
            # all: MEASURED on the same three rows of both engines, six
            # states for six, the row went on painting `completed`. The
            # worst news is now read by rank rather than by one name, and
            # only the paint moves -- the member keeps its own state in the
            # graph, which is where the evidence for it lives.
            if graph is not None and row_state is not WorkflowRowState.LOCKED:
                rank = ROW_STATE_FOLD_RANK.get(row_state, 0)
                for member in row_tasks(engine_id, task_id)[1:]:
                    try:
                        member_state = graph.state(member)
                    except Exception:                        # noqa: BLE001
                        continue
                    if member_state not in FOLDED_MEMBER_STATES:
                        continue
                    folded = TASK_ROW_STATE[member_state]
                    if ROW_STATE_FOLD_RANK[folded] > rank:
                        row_state = folded
                        rank = ROW_STATE_FOLD_RANK[folded]
            enabled = state is not TaskState.LOCKED
            entries.append((
                label, TASK_TOKEN_PREFIX + task_id, enabled,
                row_state.value))
        self._navigation.setBranchChildren(METHOD_TOKEN, entries)
        self._restoreBranchHighlight()

    def _restoreBranchHighlight(self) -> None:
        """Put the outline's highlight back on the page that is on screen.

        DP-138. `setBranchChildren` replaces every child row, which throws
        away Qt's current index, and Qt falls back to the parent -- the
        section header. That rebuild runs on `taskChanged`, which is to say
        every time the page inside this branch changes, so walking to the
        next task or letting "Run to end" carry the branch through to Export
        left the outline highlighting `2. Mesh setup` over a page titled
        `Export`. MEASURED on the `45db9031` sweep: both the snappy
        `two_solid_block` layers frame and the `perforated_plate`
        castellation frame show exactly that.

        Only when the branch is the widget on screen. A field page -- Domain
        / Regions, Reference / Readiness -- owns the highlight while it is
        showing, and re-selecting a task under it would take the highlight
        off the page the user is on to fix the case where it was already
        off it.
        """
        currentWidget = getattr(self._ui.content, 'currentWidget', None)
        if not callable(currentWidget) or currentWidget() is not self._branch:
            return
        task_id = self._branch.currentTask()
        if task_id:
            self._navigation.setBranchCurrent(TASK_TOKEN_PREFIX + task_id)

    # -- Plan 37 UF5: unlock and undo from the outline --------------------- #

    UNLOCK_ACTION = 'Unlock and discard later results\u2026'
    UNDO_ACTION = 'Restore previous mesh and settings\u2026'

    def unlockTarget(self, token: str) -> str | None:
        """The locked task a row's unlock would reopen, or ``None``.

        The row's own task when it is locked; otherwise the first locked
        substep the row's press settles (a row carries its substeps).
        """
        token = self.resolveToken(token)
        if (self._branch is None or token is None
                or not str(token).startswith(TASK_TOKEN_PREFIX)):
            return None
        task_id = token[len(TASK_TOKEN_PREFIX):]
        engine_id = str(getattr(self._branch, 'engine_id', '') or '')
        for member in row_tasks(engine_id, task_id):
            if self._branch.resultLocked(member):
                return member
        return None

    def rowActions(self, token: str) -> list:
        """``(object name, label, enabled, tooltip, callback)`` per action."""
        token = self.resolveToken(token)
        if (self._branch is None or token is None
                or not str(token).startswith(TASK_TOKEN_PREFIX)):
            return []
        target = self.unlockTarget(token)
        actions = [(
            'outlineUnlockStep', self.tr(self.UNLOCK_ACTION), target is not None,
            '' if target else self.tr(
                'This step is not locked: its settings can be changed as '
                'they are.'),
            (lambda: self._branch.requestUnlock(target)) if target else None)]
        undo = self._branch.undoAvailable()
        actions.append((
            'outlineUndoUnlock', self.tr(self.UNDO_ACTION), bool(undo),
            '' if undo else self.tr('There is no unlock to undo.'),
            self._branch.requestUndoUnlock if undo else None))
        return actions

    def _onOutlineMenu(self, position) -> None:
        tree = getattr(self._navigation, '_tree', None)
        if tree is None:
            return
        index = tree.indexAt(position)
        token = str(index.data(TOKEN_ROLE) or '') if index.isValid() else ''
        actions = self.rowActions(token) if token else []
        if not actions:
            return
        menu = QMenu(tree)
        menu.setObjectName('outlineRowMenu')
        for name, label, enabled, tooltip, callback in actions:
            action = menu.addAction(label)
            action.setObjectName(name)
            action.setEnabled(bool(enabled))
            action.setToolTip(tooltip)
            if callback is not None:
                action.triggered.connect(lambda _checked=False, cb=callback: cb())
        menu.setToolTipsVisible(True)
        menu.exec(tree.viewport().mapToGlobal(position))

    # -- routing ----------------------------------------------------------- #

    def fieldPageToken(self, _widget) -> str | None:
        """Always ``None``: this branch hosts no field page of its own.

        DP-227 added this so the wizard could recognise the two field-group
        pages installed above Geometry by identity rather than by token. Plan
        32 W1 folded both into the Mesh setup page, which the wizard already
        recognises by identity, so there is nothing left for this to answer.
        The method stays because the wizard asks for it by name, and a missing
        attribute there would read as a page the wizard cannot place.
        """
        return None

    def _onBranchRequested(self, token: str) -> None:
        # Plan 32 §7.2. One door. An outline click and a `restoreRouteMemo`
        # both end up in `route`, so the lock is asked once and answers both.
        self.route(token)

    # -- the lock ---------------------------------------------------------- #

    def isLocked(self, token: str) -> bool:
        """Whether the workflow has not reached the row ``token`` names.

        Plan 32 §7.1. The lock is the task graph's own `LOCKED`, read live.
        This branch keeps no second frontier of its own: a row is shut when
        the prerequisites of its task are unmet and open the moment they are
        met, which is the same fact the outline paints and the same fact the
        facade refuses on. Anything that is not an engine task row -- Mesh
        setup, a legacy alias, a token this branch does not serve -- is not
        locked, because nothing in the workflow holds it.
        """
        token = self.resolveToken(token)
        if token is None or not str(token).startswith(TASK_TOKEN_PREFIX):
            return False
        if self._branch is None:
            return False
        try:
            return bool(self._branch.taskIsLocked(
                token[len(TASK_TOKEN_PREFIX):]))
        except Exception:                                    # noqa: BLE001
            return False

    def lockRefusal(self, token: str) -> str:
        """Why the row ``token`` names will not open, or ``''`` if it will.

        Named, not numbered. A refusal that says "step 9 is locked" tells the
        reader what they already pressed; what they need is the row that is
        in the way, by the title the outline shows for it.
        """
        token = self.resolveToken(token)
        if token is None or not str(token).startswith(TASK_TOKEN_PREFIX):
            return ''
        if self._branch is None:
            return ''
        try:
            return str(self._branch.lockRefusal(
                token[len(TASK_TOKEN_PREFIX):]))
        except Exception:                                    # noqa: BLE001
            return ''

    def _reportLocked(self, token: str) -> None:
        """Say why the press did nothing, in the place the shell says things.

        The status bar and not a dialog. A modal box is the right weight for
        a refusal the user asked for -- pressing Proceed on a blocked task --
        and the wrong weight for a click on a row that is drawn grey, which
        they may well have made by accident on the way somewhere else.
        """
        message = self.lockRefusal(token)
        if not message:
            return
        statusbar = getattr(self._ui, 'statusbar', None)
        if statusbar is not None:
            statusbar.showMessage(message, 8000)

    def frontierToken(self) -> str:
        """The furthest row the case may currently open.

        Plan 32 §4.2 item 4 (DP-237). Read off the rows every time it is
        asked, so that going back cannot retreat it: where the reader is
        standing is not part of the answer. When no engine row is open yet --
        a fresh Gmsh case, whose only runnable task is hosted on the shared
        Preparation row -- the frontier is the Mesh setup node above them.
        """
        node = self._navigation.branchNode(METHOD_TOKEN)
        frontier = METHOD_TOKEN
        if node is None:
            return frontier
        for row in range(node.rowCount()):
            token = str(node.child(row).data(TOKEN_ROLE) or '')
            if token and not self.isLocked(token):
                frontier = token
        return frontier

    def setLockBypass(self, flag: bool) -> None:
        """Open every row regardless of the workflow. Harnesses only.

        Plan 32 §7.3. `scripts/run_dual_pipeline_strict_gui.py` and
        `scripts/run_workflow_ux_audit.py` exist to walk every page of a
        fresh case; a lock that stopped them would turn the two scripts that
        measure this product into two scripts that measure row 4. Both arm
        this around one act and put it back in a `finally`.
        """
        self.lockBypass = bool(flag)
        if self._branch is not None:
            self._branch.lockBypass = self.lockBypass

    def route(self, token: str) -> bool:
        """Stand on a branch task, as clicking its outline row would.

        R168 wants this from outside the outline: reopening a case the user
        just named has to put them back on the task they were on, and going
        through the same door the outline uses is what keeps the token, the
        highlight and the page in step with each other.

        Plan 32 W1. The legacy aliases are resolved here, at the one door
        both `restoreRouteMemo` and an outline click come through, so a case
        saved on a row this release deleted opens on the page that replaced
        it rather than on nothing at all.

        Plan 32 §4.4/§5.1 (DP-236). And here is where the lock is enforced.
        The outline has drawn locked rows grey since R10 and the workflow
        graph has always known which rows they are, but the only question
        this method asked was whether a widget existed for the token -- and
        one exists for every task the engine publishes a page for. MEASURED
        on a fresh case: seven of snappy's eight engine rows and all nine of
        Gmsh's opened. A drawn lock that opens when pressed is not a lock.
        """
        token = self.resolveToken(token)
        if token is None:
            return False
        if self.isLocked(token) and not self.lockBypass:
            self._reportLocked(token)
            return False
        widget = self.widgetForToken(token)
        if widget is None:
            # The call above builds the pages the first time a case is
            # opened, so a row that was not yet knowable as locked when this
            # method started can be knowable now -- and `select` will have
            # refused it. A refusal with a reason, rather than a token that
            # looks to the caller like one nobody serves.
            if self.isLocked(token) and not self.lockBypass:
                self._reportLocked(token)
            return False
        self._currentToken = token
        self.pageRequested.emit(widget)
        return True

    @staticmethod
    def resolveToken(token):
        """The token that serves ``token`` now, or ``None`` for no route.

        Plan 32 W1. A token this branch never retired is returned unchanged,
        so the caller still refuses what it does not recognise; only a
        retired one is answered with ``None``.
        """
        if token in LEGACY_TOKEN_ALIASES:
            return LEGACY_TOKEN_ALIASES[token]
        return token

    def routeTokens(self) -> tuple[str, ...]:
        """Every token this branch can currently route to.

        Wider than `NavigationView.routeTokens`, deliberately: the outline
        answers which rows exist, and this answers what can be opened. A case
        saved on `preferences.execution` has to stay routable although no row
        carries that token any more, and `StepManager.restoreRouteMemo` asks
        this before it routes.
        """
        tokens = list(self._navigation.routeTokens())
        for legacy, target in LEGACY_TOKEN_ALIASES.items():
            if target is not None and legacy not in tokens:
                tokens.append(legacy)
        return tuple(tokens)

    def firstAvailableTaskToken(self) -> str | None:
        """The first task row of the band that is open.

        DP-271. The band is led by `3. Preparation`, which is a numbered
        stage of the shell and carries no task token, so the rows are told
        apart by their token and not by their position.
        """
        node = self._navigation.branchNode(METHOD_TOKEN)
        if node is None:
            return None
        for row in range(node.rowCount()):
            item = node.child(row)
            token = str(item.data(TOKEN_ROLE) or '')
            if item.isEnabled() and token.startswith(TASK_TOKEN_PREFIX):
                return token
        return None

    def nextAvailableTaskToken(self, current: str) -> str | None:
        """The next row after ``current`` that still wants something.

        A7 made this skip every row it found already accepted, so that
        settling a late task would not re-open an early one. Plan 32 §7.4
        (DP-237) takes that back out. On a first pass "the next row" and "the
        next unfinished row" are the same row, which is why the difference
        went unseen; they part the moment a reader goes back to check a
        number. MEASURED with rows 4 to 8 settled: Proceed from row 5 landed
        on row 9 -- `snappy.layers`, `gmsh.periodic` -- carrying the reader
        past four rows of their own work with nothing said. A finished row is
        a progress indicator, it re-opens read-write, and its Proceed moves
        exactly one row. Skipping ahead is what the lock refuses; the row
        under Proceed is never locked, because a locked row is disabled.

        When ``current`` is not a row of this branch at all -- a legacy step
        page carries a stale token -- the walk starts at the top rather than
        returning nothing. It stops at the end instead of wrapping.
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
            if not item.isEnabled():
                continue
            # DP-271. The band is led by `3. Preparation`, a numbered stage
            # of the shell rather than a task this engine published, and it
            # carries no task token. It is a row of the walk but it is not
            # the answer to *this* question, which is which task row comes
            # next; answering with its empty token read as "no next task".
            if not tokens[row].startswith(TASK_TOKEN_PREFIX):
                continue
            return tokens[row]
        return None

    def firstBlockedTask(self) -> tuple[str, str] | None:
        """The first row the branch will not let the user open.

        A8. Proceed used to do nothing at all when the next task was locked --
        no dialog, no status line, no outline change -- so a blocked button and
        a broken button looked identical. The caller uses this to say which row
        is in the way, and to offer to open it.

        Plan 32 §7.1. The question is asked of `isLocked` and of nothing else.
        This used to read `item.isEnabled()`, which is a *painting* of the
        lock written by `_syncChildren`, so the two answered the same question
        from two sources and agreed only while the painting was fresh.
        MEASURED on the unfixed tree, both engines, with the first row settled
        and no repaint yet -- the state the press is in when `_reportNoNextTask`
        is reached -- this named `5. Base grid` and `4. Global sizing`, rows
        the lock had already opened, under a dialog that says they are locked
        and offers to open them. The label still comes off the outline,
        because the label is what the reader saw.
        """
        node = self._navigation.branchNode(METHOD_TOKEN)
        if node is None:
            return None
        for row in range(node.rowCount()):
            item = node.child(row)
            token = str(item.data(TOKEN_ROLE) or '')
            if self.isLocked(token):
                return (token,
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
        token = self.resolveToken(token)
        if token is None:
            return None
        if token == METHOD_TOKEN:
            self._navigation.setBranchCurrent(token)
            return self._methodPage
        if token.startswith(TASK_TOKEN_PREFIX):
            task_id = token[len(TASK_TOKEN_PREFIX):]
            if self._branch.select(task_id):
                self._navigation.setBranchCurrent(token)
                return self._branch
        return None
