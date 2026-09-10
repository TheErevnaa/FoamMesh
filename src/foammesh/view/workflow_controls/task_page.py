"""Schema-driven task surface shared by every engine's workflow pages.

The page renders the engine's own workflow descriptor: purpose, prerequisites,
guided and advanced fields, calculated native settings, and the §5.17 task
lifecycle actions (Preview, Update, Revert and Edit).  Every editable control is
built from the AF2 field registry, so a field that the schema does not publish
cannot be rendered - which is how release gate 10 ("no UI-only/no-op field") is
enforced structurally rather than by review.

All mutation goes through the facade (``configuration.patch`` /
``configuration.dry_run``); the page never touches project state directly.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from widgets.fit_to_text import FlowLayout, fit_to_text

from foammesh.core.engine.contracts import FieldClassification
from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.fields import REGISTRY as FIELD_REGISTRY

from .conditional_fields import refresh_applicability
from .field_widgets import FieldEditor
from foammesh.view.facade_client import query, submit


#: Classifications shown in the always-visible guided section. Native and
#: derived values the runner consumes directly live under Advanced.
_GUIDED = {FieldClassification.PRECHECK, FieldClassification.DERIVED}

_STATUS_TEXT = {
    'locked': 'Locked - complete the prerequisite tasks first.',
    'ready': 'Ready to configure.',
    'editing': 'Edited - not yet accepted.',
    'configured': 'Configured.',
    'running': 'Running.',
    'passed': 'Accepted.',
    'warning': 'Accepted with warnings.',
    'failed': 'Failed.',
    'skipped': 'Skipped.',
    'stale': 'Stale - an upstream task changed.',
    # Plan 23 §8.5 and Plan 26 WP2.1: both are real TaskState values and
    # neither is a pass. Without them they rendered as raw enum strings the
    # moment the dynamic channel went live.
    'completed': 'Completed - evidence recorded, not a pass.',
    'waived': 'Waived - accepted by a recorded decision, not a pass.',
}


#: Task states that unblock a dependent task, mirroring
#: :data:`foammesh.core.workflow.dynamic._ACCEPTED`. Held as the state *names*
#: because the page reads them off the wire, never as enums.
_ACCEPTED_STATES = frozenset({
    'passed', 'warning', 'skipped', 'completed', 'waived'})


#: States that still carry an accepted result. Reverting out of one of these
#: is the whole job of "Revert and Edit", so a task left in one afterwards
#: means the press did nothing (R115).
_STILL_ACCEPTED = frozenset({
    'passed', 'warning', 'completed', 'waived', 'skipped'})


class EngineTaskPage(QWidget):
    """Base page for one engine workflow task.

    The engine identity arrives as a constructor argument so the same page
    serves any engine whose workflow descriptor the facade can render.
    """

    #: Task whose page also offers the whole-pipeline run button. Subclasses
    #: for engines with such a task override it.
    run_all_task_id: str | None = None
    #: Engine stage this page can run on its own. Plan 26 WP5.2: a run-gated
    #: task with no run button executes as a side effect of whatever needs it
    #: next, which means it cannot be inspected and its controls have no
    #: visible effect. Distinct from ``run_all_task_id``, which runs the whole
    #: pipeline -- conflating them would make "re-extract feature edges"
    #: silently re-mesh the case.
    run_stage: str | None = None
    #: What that button says. CP-09 item 2 separates configuration from
    #: execution: "Run this step" is right for a stage the whole-pipeline run
    #: does not perform, and wrong for one it already did -- a page whose
    #: stage is part of Generate labels its button as the *re*-run it is.
    run_stage_label: str = 'Run this step'
    #: Why running this stage on its own is unavailable, when it is. Empty
    #: means available.
    run_stage_unavailable: str = ''
    #: Registry fields this page renders that its task descriptor does not
    #: declare. Plan 30 WP-09: `meshing.base_grid.standoff` is written by the
    #: base-grid task and is not a snappy control key, so it is not in the
    #: descriptor's `fields=`; porting the page without naming it here would
    #: have silently dropped a setting the legacy page could edit.
    extra_field_ids: tuple[str, ...] = ()
    engine_id = 'snappy'

    updateRequested = Signal(str)
    #: Something on the page was saved that does not amount to accepting the
    #: task (R134). Deliberately not ``updateRequested``: the branch turns
    #: that into `accept` on any task no run has to judge, and spending the
    #: tick on "settings exist" is the R94 defect over again. A page that has
    #: been filled in but not settled is CONFIGURED, and says so.
    configureRequested = Signal(str)
    revertRequested = Signal(str)
    runRequested = Signal(str)
    #: ``(task_id, stage)`` -- run this one stage.
    stageRunRequested = Signal(str, str)
    dirtyChanged = Signal(bool)

    def __init__(self, facade_client, task_id: str, parent=None, *,
                 engine_id: str | None = None):
        super().__init__(parent)
        self._client = facade_client
        self.task_id = task_id
        if engine_id is not None:
            self.engine_id = engine_id
        self._editors: dict[str, FieldEditor] = {}
        self._pending: dict[str, object] = {}
        self._task: dict = {}
        #: The whole page in one read, kept for the length of one refresh so
        #: the four things the page draws are not four facade calls (F-22).
        self._page: dict | None = None
        #: Set only for the length of one `refresh()`, so the status re-read
        #: it makes reuses the page `refresh()` has already read.
        self._reuse_page_once = False
        self._last_change_set_id: str | None = None
        #: Task state as of the last `refresh_status`, so the revisit note can
        #: be recomputed once the editors it reads exist.
        self._last_status: str = ''
        self.setObjectName(task_id.replace('.', '_') + 'Page')

        outer = QVBoxLayout(self)
        # F8. The task name is the panel heading now (E3), and this label
        # repeated it in a chip too narrow for the words -- `Reference Read`,
        # `Mesh QA - war` -- drawn over the heading it duplicated. It stays as
        # the accessible name of the page and out of the layout.
        self._title = QLabel(self)
        self._title.setObjectName('engineTaskTitle')
        self._title.setVisible(False)
        self._description = QLabel(self)
        self._description.setWordWrap(True)
        self._status = QLabel(self)
        self._status.setObjectName('engineTaskStatus')
        self._prerequisites = QLabel(self)
        self._prerequisites.setWordWrap(True)
        self._warnings = QLabel(self)
        self._warnings.setObjectName('engineTaskWarnings')
        self._warnings.setWordWrap(True)
        self._warnings.setAccessibleName(
            self.tr('Derivation warnings for this task'))
        self._warnings.setVisible(False)
        # F-09. What a pending edit would change used to be a modal, so the
        # fields it named were behind the window that named them and could not
        # be compared with it. It is a line on the page now, above the fields.
        self._preview_note = QLabel(self)
        self._preview_note.setObjectName('engineTaskPreview')
        self._preview_note.setWordWrap(True)
        self._preview_note.setAccessibleName(self.tr('Preview of pending changes'))
        self._preview_note.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._preview_note.setVisible(False)
        # CP-09 item 1: backward navigation with explicit stale-state
        # feedback. Coming back to a task that has already run and editing it
        # invalidates everything downstream, and the only place that was ever
        # said was inside Preview -- a button you have to press *after*
        # deciding to change something. This line says it on arrival.
        self._revisit = QLabel(self)
        self._revisit.setObjectName('engineTaskRevisit')
        self._revisit.setWordWrap(True)
        self._revisit.setProperty('foammeshStatus', 'warning')
        self._revisit.setAccessibleName(
            self.tr('What editing this task would invalidate'))
        self._revisit.setVisible(False)
        for widget in (self._description, self._prerequisites, self._status,
                       self._revisit, self._warnings, self._preview_note):
            outer.addWidget(widget)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        # C5. The page scrolled sideways and so did the table inside it:
        # two horizontal scrollbars stacked a few pixels apart, and the
        # outer one moved the headings along with the row it was meant to
        # reveal. Only the innermost scrollable thing should scroll.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._body)
        outer.addWidget(scroll, 1)

        self._guided = self._make_group(self.tr('Guided'))
        self._advanced = self._make_group(self.tr('Advanced'))
        self._advanced.setCheckable(True)
        self._advanced.setChecked(False)

        self._calculated = QTableWidget(0, 4, self)
        self._calculated.setHorizontalHeaderLabels((
            self.tr('Field'), self.tr('Classification'),
            self.tr('Native mapping'), self.tr('Calculation')))
        self._calculated.setAccessibleName(
            self.tr('Calculated native settings for this task'))
        self._calculated.setEditTriggers(QTableWidget.NoEditTriggers)
        self._calculated.verticalHeader().setVisible(False)
        # Every column used to take a quarter of the width, so the field id
        # was elided to `gmsh.comput...` beside two thirds of a column of
        # white space. Give the three short columns their content and let the
        # calculation description absorb the slack.
        header = self._calculated.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(
            header.fontMetrics().horizontalAdvance(
                self.tr('Native mapping')) + 24)
        self._calculatedBox = QGroupBox(self.tr('Calculated settings'), self)
        QVBoxLayout(self._calculatedBox).addWidget(self._calculated)
        self._body_layout.addWidget(self._calculatedBox)

        self.build_sections(self._body_layout)
        self._body_layout.addStretch(1 if not self._bodyStretchClaimed else 0)
        self._bodySpacerIndex = self._body_layout.count() - 1

        self._preview = QPushButton(self.tr('Preview'), self)
        self._update = QPushButton(self.tr('Update'), self)
        self._revert = QPushButton(self.tr('Revert and Edit'), self)
        # Plan 30 WP-09 / F-23. The whole-pipeline run is not a per-task
        # action: in this row it put "Run this step" and "Run complete mesh"
        # within a button's width of each other, and only one of them meant
        # this task. It was hidden rather than removed, which left a live
        # button no user could reach and a second Run idiom in the source.
        # It is the branch row's "Run to end" now (`EngineBranchView._runAll`)
        # and nowhere else; `runRequested` survives for the branch to drive.
        self._runStage = QPushButton(self.tr(self.run_stage_label), self)
        self._runStage.setObjectName('runTaskStage')
        self._runStage.setVisible(bool(self.run_stage))
        self._runStage.setAccessibleDescription(
            self.tr('Run only this step, leaving the rest of the mesh alone.'))
        self._preview.clicked.connect(self.preview)
        self._update.clicked.connect(self.apply)
        self._revert.clicked.connect(self.revert)
        self._runStage.clicked.connect(
            lambda: self.stageRunRequested.emit(
                self.task_id, str(self.run_stage or '')))
        # C2. Five buttons that each refuse to shrink below their label add
        # up to more width than the panel has, and the page answered with a
        # horizontal scrollbar under the *whole* page -- so reading the last
        # button meant scrolling the form away. The row wraps instead.
        buttons = FlowLayout()
        for button in (self._preview, self._update, self._revert,
                       self._runStage):
            fit_to_text(button)
            buttons.addWidget(button)
        outer.addLayout(buttons)

        self.setRunStageAvailable(not self.run_stage_unavailable,
                                  self.run_stage_unavailable)
        self.refresh()

    def setRunStageAvailable(self, available: bool, reason: str = '') -> None:
        """Gate the per-stage run button and say why when it is shut.

        CP-09 item 2 and the acceptance's "unavailable actions explain why":
        a live button that cannot do anything is worse than an absent one,
        because the user presses it and reads the silence as a failure.
        """
        self._runStage.setEnabled(bool(available))
        self._runStage.setToolTip('' if available else str(reason or ''))
        self._runStage.setAccessibleDescription(
            self.tr('Run only this step, leaving the rest of the mesh alone.')
            if available else str(reason or ''))

    def setRunStageLabel(self, text: str) -> None:
        """Name the action the button performs, when the page knows better.

        CP-09 item 3: which check qualifies a mesh depends on the target
        solver, and "Run this step" names neither of the two things it might
        do. Pages that can read the target say which one they mean.
        """
        self._runStage.setText(str(text))
        fit_to_text(self._runStage)

    def runStageLabel(self) -> str:
        return self._runStage.text()

    def runStageReason(self) -> str:
        """The sentence the run button carries while it is unavailable."""
        return '' if self._runStage.isEnabled() else self._runStage.toolTip()

    # -- construction hooks ------------------------------------------------ #

    #: R186. Whether a section asked to absorb the page's spare height.
    #: The trailing spacer is right for a page of short form rows, and wrong
    #: for one whose whole content is a table the user has to read.
    _bodyStretchClaimed = False
    #: Position of the trailing spacer, so the claim can be handed back.
    _bodySpacerIndex = -1

    def claimBodyStretch(self, widget, claim: bool = True) -> None:
        """Let this section take the page's spare height, not the spacer.

        R186. MEASURED on Geometry Fidelity: a four-row evidence table was
        clipped to a scroll area about one and a half rows tall, with roughly
        three hundred pixels of empty panel underneath it -- because every
        section takes its size hint and `addStretch(1)` takes everything left
        over. Reading a four-section report meant scrolling a viewport barely
        taller than one section, past a screenful of nothing.

        R187. The claim is not permanent, because the reason for it is not.
        MEASURED on Resolution Adequacy before a run: the same box, holding
        one sentence saying the task has not run, stretched to about five
        hundred pixels of empty grey with that sentence marooned in the
        middle of it. A section earns the slack by having something in it
        worth the room; pass `claim=False` when it no longer does and the
        spacer takes the height back.
        """
        self._body_layout.setStretchFactor(widget, 1 if claim else 0)
        self._bodyStretchClaimed = bool(claim)
        if self._bodySpacerIndex >= 0:
            self._body_layout.setStretch(
                self._bodySpacerIndex, 0 if claim else 1)

    def build_sections(self, layout) -> None:
        """Subclasses add task-specific tables or panels here."""

    def _make_group(self, title: str) -> QGroupBox:
        box = QGroupBox(title, self)
        QFormLayout(box)
        self._body_layout.addWidget(box)
        return box

    # -- data -------------------------------------------------------------- #

    def page_model(self, *, refresh: bool = False) -> dict:
        """Everything this page draws, from one facade read (F-22).

        A page refresh used to cost three synchronous facade calls -- the
        workflow descriptor for this task, the descriptor again for the names
        its prerequisites are shown under, and the task state for its status
        and warnings -- and the branch refreshes every page whenever the graph
        moves. ``mesh.workflow.task_page`` answers all three at once.

        A client that has never heard of that operation (an older facade, a
        test double built against the three calls) gets the three calls back;
        the page must not stop drawing because a read got faster.
        """
        if self._page is not None and (not refresh or self._reuse_page_once):
            return self._page
        page = {}
        try:
            page = dict(query(
                self._client, 'mesh.workflow.task_page',
                {'engine_id': self.engine_id,
                 'task_id': self.task_id}).payload or {})
        except Exception:                                    # noqa: BLE001
            page = {}
        # An answer with no task in it is not this task's page: the client is
        # older than the operation, or is a double that answers everything it
        # does not recognise with an empty payload. Ask the long way.
        self._page = page if page.get('task') else self._page_model_the_long_way()
        return self._page

    def _page_model_the_long_way(self) -> dict:
        """The same page model, from the reads it replaced."""
        document = query(
            self._client, 'mesh.engine.workflow',
            {'engine_id': self.engine_id}).payload['workflow']
        tasks = list(document.get('tasks') or ())
        task = next((item for item in tasks
                     if item.get('task_id') == self.task_id), None)
        if task is None:
            raise KeyError(f'unknown {self.engine_id} task: {self.task_id}')
        status, warnings = 'ready', ()
        try:
            payload = query(
                self._client, 'mesh.workflow.task_state',
                {'engine_id': self.engine_id}).payload or {}
        except Exception:                                    # noqa: BLE001
            payload = {}
        states = (payload.get('state') or {}).get('tasks') or {}
        status = str(states.get(self.task_id) or 'ready')
        warnings = tuple((payload.get('warnings') or {}).get(self.task_id) or ())
        return {'engine_id': self.engine_id, 'task_id': self.task_id,
                'task': task, 'status': status, 'warnings': list(warnings),
                'state': payload.get('state') or {},
                'titles': {item['task_id']: item.get('title') or item['task_id']
                           for item in tasks if item.get('task_id')},
                'dependents': [str(item.get('title') or item.get('task_id'))
                               for item in tasks
                               if self.task_id in (item.get('depends_on') or ())]}

    def task_document(self) -> dict:
        task = self.page_model().get('task')
        if not task:
            raise KeyError(f'unknown {self.engine_id} task: {self.task_id}')
        return dict(task)

    def task_state(self) -> tuple[str, tuple[str, ...]]:
        """This task's live status and warnings, from the dynamic channel.

        Plan 26 WP2.1. The descriptor `task_document` returns is the *static*
        module-level workflow constant: `WorkflowTask.to_dict()` emits fifteen
        keys and neither `status` nor `warnings` is among them, so the page
        always fell through to `'ready'` and nine of the ten `_STATUS_TEXT`
        entries -- `'warning'` included -- were unreachable dead code.

        Neither field may be added to the descriptor: its digest is the
        invalidation key for persisted task state, so a value that changes
        whenever a warning appears would wipe the user's progress. Both travel
        on `mesh.workflow.task_state` instead, which already carries status and
        which the engine branch already reads.
        """
        try:
            page = self.page_model()
        except Exception:                                    # noqa: BLE001
            return 'ready', ()
        return (str(page.get('status') or 'ready'),
                tuple(page.get('warnings') or ()))

    def refresh(self) -> None:
        self.page_model(refresh=True)
        self._task = self.task_document()
        self._title.setText(self._task.get('title') or self.task_id)
        self.setAccessibleName(self._title.text())
        self._description.setText(self._task.get('description', ''))
        depends = self._task.get('depends_on') or ()
        titles = self._task_titles()
        self._prerequisites.setText(
            self.tr('Requires: ')
            + ', '.join(titles.get(item, item) for item in depends)
            if depends else self.tr('No prerequisites.'))
        # `refresh_status` re-reads the page, because it is also called on
        # its own every time the graph moves. Reached from here the page has
        # just been read, and reading it twice is the cost this exists to
        # remove (F-22), so the read is suppressed for this one call --
        # including for the four subclasses that override the method.
        self._reuse_page_once = True
        try:
            self.refresh_status()
        finally:
            self._reuse_page_once = False
        fields = list(self._task.get('fields', []))
        declared = {field.get('field_id') for field in fields}
        # Advanced rather than guided: a field the engine does not declare as
        # a task input is by definition not one of the few the guided form is
        # for.
        fields += [{'field_id': field_id,
                    'classification': FieldClassification.NATIVE}
                   for field_id in self.extra_field_ids
                   if field_id not in declared]
        self._populate_fields(fields)
        self._populate_calculated(self._task.get('fields', []))
        self._update_empty_sections()
        # Again, now the editors exist: `refresh_status` above runs before the
        # fields are built, and the note is derived from what those fields
        # declare they invalidate.
        self._set_revisit_note(getattr(self, '_last_status', ''))
        self._set_dirty(False)

    def _task_titles(self) -> dict:
        """Task id to the name the outline shows for it.

        Prerequisites were printed as raw ids -- "Requires: common.fidelity,
        common.resolution, gmsh.qa" -- which name rows the user has never seen
        spelled that way anywhere in the interface.
        """
        try:
            return dict(self.page_model().get('titles') or {})
        except Exception:                                    # noqa: BLE001
            return {}

    def refresh_status(self) -> None:
        """Re-read just this task's live state.

        The status line was written once, when the page was built, and then
        never again: a task whose prerequisites had since passed went on
        saying "Locked - complete the prerequisite tasks first" while its own
        stored state read `ready` and its buttons were live. Cheap enough to
        call whenever the graph moves, and it touches no editor.
        """
        self.page_model(refresh=True)
        status, warnings = self.task_state()
        self._status.setText(self._status_sentence(status))
        self._set_warnings(warnings)
        self._last_status = str(status)
        self._set_revisit_note(status)

    def _status_sentence(self, status: str) -> str:
        """The status line, with a locked one naming what it waits for.

        Plan 31 DP-39. "Locked - complete the prerequisite tasks first."
        names no task, and on Gmsh the prerequisite is one of seven manual
        pages the user has no way to pick out. The page already reads the
        whole task-state map for its own status, so the unaccepted
        prerequisites cost nothing to name -- and the sentence is what tells
        a user why the Update they are about to press cannot mark this step
        done.
        """
        if str(status) != 'locked':
            return _STATUS_TEXT.get(status, status)
        blocking = self._blocking_prerequisites()
        if not blocking:
            return _STATUS_TEXT['locked']
        return self.tr('Locked - complete {0} first.').format(
            ', '.join(blocking))

    def _blocking_prerequisites(self) -> list:
        """Titles of this task's prerequisites that are not accepted yet.

        Falls back to naming every prerequisite when the state map is missing
        -- a page model from an older facade carries no `state` -- because
        naming all of them is still better than naming none.
        """
        try:
            page = self.page_model()
        except Exception:                                    # noqa: BLE001
            return []
        task = self._task if isinstance(getattr(self, '_task', None), dict) else {}
        depends = tuple(task.get('depends_on') or ())
        if not depends:
            depends = tuple((page.get('task') or {}).get('depends_on') or ())
        if not depends:
            return []
        titles = self._task_titles()
        states = ((page.get('state') or {}).get('tasks') or {})
        blocking = [item for item in depends
                    if str(states.get(item) or '') not in _ACCEPTED_STATES]
        if states and not blocking:
            return []
        return [str(titles.get(item, item)) for item in (blocking or depends)]

    #: Task states that mean a run has already happened with these settings,
    #: so editing them costs that result. `stale` is deliberately in: the row
    #: already knows it is out of date, and the page should say what is.
    _ALREADY_RUN = frozenset({
        'passed', 'warning', 'failed', 'completed', 'waived', 'stale'})

    #: What an invalidation token costs the user, in their words. The tokens
    #: come from `FieldDescriptor.invalidates`, which 143 of the registry's
    #: 149 fields carry and which reached the user only as a raw token inside
    #: the Preview text ("Stales: mesh, quality").
    _INVALIDATION_WORDS = {
        'mesh': 'the mesh that has already been generated',
        'quality': 'the quality results recorded for it',
        'engine': 'the engine plan',
        'engine_plan': 'the engine plan',
        'export': 'the files already exported',
        'exports': 'the files already exported',
    }

    def _set_revisit_note(self, status: str) -> None:
        """Say, on arrival, what changing this task would throw away.

        Only on a task that has already run: on a task nobody has run yet
        there is nothing to lose, and a standing warning there would be the
        boy who cried wolf on every page of the workflow.
        """
        note = getattr(self, '_revisit', None)
        if note is None:
            return
        if str(status) not in self._ALREADY_RUN:
            note.clear()
            note.setVisible(False)
            return
        costs = self.invalidation_costs()
        if not costs:
            note.clear()
            note.setVisible(False)
            return
        if len(costs) == 1:
            listed = costs[0]
        else:
            listed = ', '.join(costs[:-1]) + self.tr(' and ') + costs[-1]
        note.setText(self.tr(
            'You can change anything here. This task has already run, so '
            'applying a change discards {0}, and the steps after it will '
            'need running again.').format(listed))
        note.setVisible(True)

    def invalidation_costs(self) -> list:
        """What this page's own fields declare they invalidate, in words.

        Read off the shared metadata rather than listed per page, so a field
        moving between tasks cannot leave a page describing the wrong cost.
        """
        tokens: list[str] = []
        for editor in self._editors.values():
            for token in getattr(editor.descriptor, 'invalidates', ()) or ():
                token = str(token)
                if token not in tokens:
                    tokens.append(token)
        words: list[str] = []
        for token in tokens:
            word = self._INVALIDATION_WORDS.get(token)
            if word is None:
                continue
            if word not in words:
                words.append(word)
        return words

    def _update_empty_sections(self) -> None:
        """Hide the boxes that have nothing in them.

        The qualification tasks publish no editable fields at all, so the page
        drew an empty Guided box, an empty Advanced box and an empty
        four-column table -- some 350px of chrome around no content, above a
        blank page. What the task does is in its description; the boxes only
        earn their space once they hold something.
        """
        self._guided.setVisible(self._guided.layout().count() > 0)
        self._advanced.setVisible(self._advanced.layout().count() > 0)
        self._calculatedBox.setVisible(self._calculated.rowCount() > 0)

    def _set_warnings(self, warnings) -> None:
        """Show what the derivation changed about this task's request.

        Hidden when empty rather than left as a blank line: an always-present
        warning area reads as "checked, nothing wrong" even before anything has
        been checked.
        """
        texts = [str(text) for text in warnings if str(text).strip()]
        self._warnings.setText(
            '' if not texts else
            self.tr('Applied with changes:') + '\n'
            + '\n'.join(f'• {text}' for text in texts))
        self._warnings.setVisible(bool(texts))

    def _populate_fields(self, fields) -> None:
        for layout in (self._guided.layout(), self._advanced.layout(),
                       *self.field_forms()):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            # A QFormLayout emptied item by item keeps its row structure: the
            # rows are still there, now blank, and the next addRow lands below
            # them. Left alone, a page whose fields land in a form of its own
            # grew a row of blank space per refresh.
            if isinstance(layout, QFormLayout):
                while layout.rowCount():
                    layout.removeRow(0)
        self._editors.clear()
        for field in fields:
            field_id = field['field_id']
            # Repeatable child definitions carry an "/{id}/" template segment;
            # they are edited through the child-control table, not this form.
            if '{id}' in field_id:
                continue
            # So are whole collections. A task that declares one -- castellation
            # declares two, layers one -- has a `ChildControlPanel` for it, and
            # the registry publishes no scalar descriptor for the collection
            # itself, so asking for one produced an "unbacked field" notice
            # about a field that is in fact fully editable a few rows below.
            if field_id in FIELD_REGISTRY.collections:
                continue
            try:
                descriptor = self._client.descriptor(field_id)
            except (KeyError, ValidationFailedError):
                # A published task field with no registry descriptor would be a
                # no-op control, which gate 10 forbids. Surface it instead of
                # silently rendering something the schema cannot back.
                self._add_unbacked_notice(field_id)
                continue
            editor = FieldEditor(descriptor, self)
            editor.valueChanged.connect(self._on_field_changed)
            classification = _classification(field.get('classification'))
            box = self._guided if classification in _GUIDED else self._advanced
            form = self.field_form(field_id, classification)
            parent = box
            if form is None:
                form = box.layout()
            else:
                parent = form.parentWidget() or box
            row = QHBoxLayout()
            row.addWidget(editor.editor, 1)
            if descriptor.unit:
                row.addWidget(editor.unit_label)
            container = QWidget(parent)
            container.setLayout(row)
            form.addRow(editor.label, container)
            self._editors[field_id] = editor
        self.reload_values()

    def field_form(self, field_id: str, classification):
        """Where this page wants one field's control to land, or ``None``.

        ``None`` -- the default, and what every page did before FS-B -- keeps
        the guided/advanced split the classification implies. A page returning
        a ``QFormLayout`` of its own claims that field instead. The base grid
        needed it: each background face carries a name, a type and a category
        that the writer couples -- a category on a face still carrying the
        generated name is refused -- and the default routing put the category
        in Guided and the other two behind a collapsed Advanced box, which is
        the one arrangement in which the coupling cannot be seen. The editor is
        still registered in ``self._editors``, so dirty tracking, applicability
        and apply are unchanged wherever it is drawn.
        """
        return None

    def field_forms(self):
        """Forms this page claims fields into, emptied on every refresh.

        The guided and advanced boxes are cleared before each repopulation; a
        page's own form has to be cleared with them or a second refresh draws
        every claimed control twice.
        """
        return ()

    def _add_unbacked_notice(self, field_id: str) -> None:
        label = QLabel(
            self.tr('%s is published by the engine but has no schema '
                    'descriptor and cannot be edited.') % field_id, self)
        label.setWordWrap(True)
        label.setObjectName('unbackedFieldNotice')
        self._guided.layout().addRow(label)

    def reload_values(self) -> None:
        if not self._editors:
            return
        values = self._client.field_values(tuple(self._editors))
        for field_id, editor in self._editors.items():
            editor.set_value(values.get(field_id, editor.descriptor.default))
        self._pending.clear()
        # CP-09 item 4. After the values, because what a field's condition
        # reads may be one of the values just written.
        self._inactive_fields = refresh_applicability(
            self._client, self._editors, self._pending)

    def inactive_fields(self) -> dict:
        """``{field_id: why it is inactive}`` for this page's editors."""
        return dict(getattr(self, '_inactive_fields', {}) or {})

    def _populate_calculated(self, fields) -> None:
        self._calculated.setRowCount(len(fields))
        for row, field in enumerate(fields):
            values = (
                field['field_id'],
                _classification_name(field.get('classification')),
                field.get('native_name') or '-',
                field.get('calculation_version') or '-',
            )
            for column, value in enumerate(values):
                self._calculated.setItem(row, column, QTableWidgetItem(str(value)))

    # -- editing ----------------------------------------------------------- #

    def _on_field_changed(self, field_id: str, value) -> None:
        self._pending[field_id] = value
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._update.setEnabled(dirty)
        self._preview.setEnabled(dirty)
        self.dirtyChanged.emit(dirty)

    @property
    def is_dirty(self) -> bool:
        return bool(self._pending)

    def pending_patch(self) -> dict:
        return dict(self._pending)

    def preview(self) -> None:
        if not self._pending:
            return
        result = query(
            self._client, 'configuration.dry_run',
            {'patch': self.pending_patch()})
        payload = dict(result.payload or {})
        # Invalidation travels on the OperationResult envelope, not inside the
        # dry-run payload; merge it so the stale summary can actually render.
        payload.setdefault('invalidates', list(
            getattr(result, 'invalidated_outputs', ()) or ()))
        self.show_preview(payload)

    def show_preview(self, payload: dict) -> None:
        changes = payload.get('diff') or []
        if not changes:
            text = self.tr('No effective change.')
        else:
            text = '\n'.join(
                f"{item.get('field_id', item)}: {item.get('before')} -> {item.get('after')}"
                if isinstance(item, dict) else str(item) for item in changes)
        stale = payload.get('invalidates') or []
        if stale:
            text += '\n\n' + self.tr('Stales: ') + ', '.join(str(x) for x in stale)
        self._preview_note.setText(text)
        self._preview_note.setVisible(True)

    def preview_text(self) -> str:
        """What the inline preview is currently saying ('' when there is none)."""
        return self._preview_note.text()

    def clear_preview(self) -> None:
        self._preview_note.clear()
        self._preview_note.setVisible(False)

    def apply(self):
        if not self._pending:
            return None

        def applied(result):
            if getattr(result, 'status', 'accepted') == 'accepted':
                payload = getattr(result, 'payload', {}) or {}
                self._last_change_set_id = payload.get('change_set_id')
                self._pending.clear()
                self.clear_preview()
                self._set_dirty(False)
                self.updateRequested.emit(self.task_id)
                self.refresh()
            else:
                QMessageBox.warning(
                    self, self.tr('Update failed'),
                    str(getattr(result, 'message', '')
                        or self.tr('The facade rejected the edit.')))

        # C31-12. The patch is scheduled rather than run on the GUI thread;
        # everything that used to follow it runs in `applied` when it lands,
        # so the order is unchanged and the window stays live meanwhile.
        return submit(self._client, 'configuration.patch',
                      {'patch': self.pending_patch()}, then=applied)

    def revert(self):
        """Discard pending values or revert only this page's accepted edit."""
        if self._pending:
            self._pending.clear()
            self.clear_preview()
            self.reload_values()
            self._set_dirty(False)
            return None

        def reopen():
            """Reopen the task for editing, whatever the revert did."""
            self.revertRequested.emit(self.task_id)
            self.refresh()
            # R115. The press is only visible if the task actually reopens.
            # When the host refuses (or silently drops) the lifecycle
            # transition the page went on reading "Accepted." with every
            # control unchanged, so the one control that offers to reopen a
            # finished task was indistinguishable from a dead one. Say what
            # happened instead.
            if self.task_state()[0] in _STILL_ACCEPTED:
                self._status.setText(
                    self._status.text() + ' '
                    + self.tr('Revert and Edit did not reopen this task - its '
                              'recorded result still stands.'))

        def reverted(result):
            # R115. The facade *raises* when a change set is no longer the
            # latest reversible one (`PlanStaleError`), so the status check
            # below was unreachable and the exception escaped the clicked()
            # slot instead: on an accepted Boundary Layers task the button
            # left the page reading "Accepted." with nothing else changed,
            # which is indistinguishable from a dead button. A stale change
            # set is not a reason to refuse to reopen the task -- reopening
            # it for editing is the half of "Revert and Edit" that still
            # works -- so it is reported and the lifecycle revert goes ahead.
            # C31-12 keeps that: a refusal now arrives as a failed result
            # rather than as an exception, and `reopen()` still runs after it.
            if getattr(result, 'status', 'accepted') != 'accepted':
                message = str(getattr(result, 'message', '') or '') or self.tr(
                    'This task edit is no longer the latest reversible '
                    'change; no unrelated change was undone.')
                QMessageBox.warning(
                    self, self.tr('Revert failed'),
                    self.tr('The earlier values could not be restored, so the '
                            'task keeps the ones it has. It is reopened for '
                            'editing.\n\n{0}').format(message))
            self._last_change_set_id = None
            reopen()

        if not self._last_change_set_id:
            reopen()
            return None
        return submit(self._client, 'history.revert_change_set',
                      {'change_set_id': self._last_change_set_id},
                      then=reverted)


def _classification(value):
    if isinstance(value, FieldClassification):
        return value
    try:
        return FieldClassification(str(value))
    except ValueError:
        return FieldClassification.PRECHECK


def _classification_name(value) -> str:
    resolved = _classification(value)
    return getattr(resolved, 'value', str(resolved))
