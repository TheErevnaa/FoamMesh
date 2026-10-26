"""Schema-driven task surface shared by every engine's workflow pages.

The page renders the engine's own workflow descriptor: purpose, prerequisites,
guided and advanced fields, calculated native settings, and the §5.17 task
lifecycle actions (Preview, Update, Revert and edit).  Every editable control is
built from the AF2 field registry, so a field that the schema does not publish
cannot be rendered - which is how release gate 10 ("no UI-only/no-op field") is
enforced structurally rather than by review.

All mutation goes through the facade (``configuration.patch`` /
``configuration.dry_run``); the page never touches project state directly.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QScrollArea, QStyledItemDelegate, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


from foammesh.view.theming.metrics import (align_form_columns,
                                           align_unit_column,
                                           apply_form_metrics)

from foammesh.core.engine.contracts import FieldClassification
from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.fields import REGISTRY as FIELD_REGISTRY

from foammesh.view.menu.help.step_help_dialog import (StepDetailsDialog,
                                                      StepHelpDialog)
from foammesh.view.theming.sheet_form import (LabelWidthWatcher, SheetForm,
                                               guard_cards, share_label_width)
from foammesh.view.widgets.folder_header import FolderHeader
from .conditional_fields import follows_master, refresh_applicability
from foammesh.core.facade.field_tiers import (
    COMMON, ESSENTIAL, EXPERT, tier_of)
from .field_widgets import FieldEditor, add_editor_row, same_as_default
from .page_action import PageAction
from .page_filter import make_filter_box
from .lock_chip import (CONFIRM, LOCKED, FilterRow, LockChip, ReadOnlyGuard,
                        unlock_ask_text)
from foammesh.view.widgets.notice_card import (NoticeCard,
                                               WARNING as NOTICE_WARNING)
from .tiers import expert_visibility, make_expert_toggle
from foammesh.view.facade_client import query, submit


#: Classifications shown in the always-visible guided section. Native and
#: derived values the runner consumes directly live under Advanced.
_GUIDED = {FieldClassification.PRECHECK, FieldClassification.DERIVED}

#: The three states the page says anything about, and what to do about
#: each. Plan 33 FORM-03/SIZE-01/LAYER-05/QA-03: the other nine entries --
#: `Ready to configure.`, `Accepted.`, `Configured.`, `Running.` and the
#: rest -- restated what the outline row beside the page already paints, in
#: a full line at the top of every one of the twenty-nine task pages.
#: MEASURED: twenty-nine of twenty-nine pages opened with a status sentence,
#: and on twenty-nine of them it said `Ready to configure.`.
#:
#: What is left is the three a reader has to act on, and each names the act.
#: A state not in here paints no line at all.
_STATUS_TEXT = {
    'locked': 'Locked — complete the prerequisite tasks first.',
    'failed': 'Failed — change a setting below and run this step again.',
    'stale': 'Stale — an upstream task changed, so run this step again.',
}


#: Task states that unblock a dependent task, mirroring
#: :data:`foammesh.core.workflow.dynamic._ACCEPTED`. Held as the state *names*
#: because the page reads them off the wire, never as enums.
_ACCEPTED_STATES = frozenset({
    'passed', 'warning', 'skipped', 'completed', 'waived'})


#: States that still carry an accepted result. Reverting out of one of these
#: is the whole job of "Revert and edit", so a task left in one afterwards
#: means the press did nothing (R115).
_STILL_ACCEPTED = frozenset({
    'passed', 'warning', 'completed', 'waived', 'skipped'})


class _ElideLeftDelegate(QStyledItemDelegate):
    """Cut the head off a cell that does not fit, not the tail.

    DP-161. `QAbstractItemView.setTextElideMode` is a property of the whole
    view, and only one column of the Calculated table wants it.
    """

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideLeft


#: DP-166. What `_populate_calculated` writes where a field has no value
#: for a column. A column that holds only these says nothing, so it is not
#: shown and does not take width off the columns that do.
_PLACEHOLDERS = frozenset({'-', ''})


class _StepHelpSink:
    """Where a page hands its own words once it has added to them.

    Plan 33 FORM-03 took away the header band and the `?` button in it. The
    three pages that write their own extra paragraph still have to say "the
    words changed, read them again", which is what `setDetail` always meant,
    so the seam survives the control: it re-reads the page and refreshes the
    tooltip that is now one of the two routes to the text.

    Not a widget, and nothing a reader can reach -- it holds no words of its
    own. `_description` and `_prerequisites` on the page are still the only
    place the words live.
    """

    def __init__(self, page) -> None:
        self._page = page

    def setDetail(self, purpose: str = '', prerequisites: str = '') -> None:
        """Re-read the page's labels and refresh both help routes."""
        page = self._page
        page.setToolTip(page.stepHelpText())
        page._title.setToolTip(page.toolTip())
        page._title.setAccessibleDescription(page.toolTip())
        page.setAccessibleDescription(page.accessibleStepDescription())

    def detail(self) -> tuple:
        """What the two routes would show, as the page has it now."""
        return self._page.stepHelpDetail()


class CalculatedTable(QTableWidget):
    """The Calculated settings table, sized to fit rather than to scroll.

    DP-158. A `QTableWidget` defaults to a viewport about 157 px tall and
    scrolls whatever does not fit, in both directions. Measured at the width
    the task panel actually gets, that put 1009 px of rows behind a 171 px
    window on `snappy.base_grid` and made nine of the ten pages that have
    this table carry a horizontal scrollbar as well -- a nested pair of
    scrollbars inside a page that already scrolls, which is the fault
    DP-155 closed for the field panels.

    So the table never scrolls. It takes the height of its rows and the page
    does the scrolling, and the columns are fitted into the width there is:
    each asks for its content, and when they ask for more than the panel has
    the widest give way first, down to the header floor, so the text elides
    with the full value on the item tooltip instead of running off an edge.
    """

    def __init__(self, columns: int, parent=None) -> None:
        super().__init__(0, columns, parent)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # DP-161. The field id is a dotted path whose head is shared with its
        # neighbours and whose tail is what tells one row from the next, so
        # it is the only column that elides from the left.
        self._elide_left = _ElideLeftDelegate(self)
        self.setItemDelegateForColumn(0, self._elide_left)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fitColumns()

    def fitContents(self) -> None:
        """Take the height of every row, so the page scrolls and not this."""
        self.resizeRowsToContents()
        header = self.horizontalHeader()
        rows = self.verticalHeader()
        height = header.height() + 2 * self.frameWidth()
        height += sum(rows.sectionSize(row) for row in range(self.rowCount()))
        self.setFixedHeight(height)
        self.fitColumns()

    def _headingWidth(self, column: int) -> int:
        """What this column's heading needs to be read."""
        item = self.horizontalHeaderItem(column)
        text = item.text() if item is not None else ''
        return self.horizontalHeader().fontMetrics().horizontalAdvance(
            text) + 20

    def hideEmptyColumns(self) -> None:
        """Drop a column that says nothing on this page.

        DP-166. Five of the ten tables carry `-` in every row of
        `Calculation`, and it cost them width twice over: its own heading is
        an 85 px floor it never gives up, and `fitColumns` handed it the
        slack on top -- 145 of 575 px on `snappy.snap`, for nine hyphens.
        On `snappy.castellation` the four columns wanted 653 px in a 575 px
        table, and because the hyphen column sat at its floor the three that
        carry real text paid the whole 78 px deficit: that is what cut
        `allowFreeStandingZoneFaces` short.

        Column 0 is never hidden. It is the column that says which row this
        is, and a table with no rows has nothing to judge either way.
        """
        rows = self.rowCount()
        for column in range(self.columnCount()):
            blank = bool(rows) and column > 0 and all(
                (self.item(row, column) is None
                 or self.item(row, column).text().strip() in _PLACEHOLDERS)
                for row in range(rows))
            self.setColumnHidden(column, blank)

    def fitColumns(self) -> None:
        """Share the width there is between the columns that want it."""
        columns = [column for column in range(self.columnCount())
                   if not self.isColumnHidden(column)]
        if not columns:
            return
        header = self.horizontalHeader()
        available = self.viewport().width()
        # A column is never squeezed below its own heading. One global floor
        # taken from the widest heading was the old rule, and it gave
        # `Classification`, whose values are one word, the same 117 px as a
        # column of dotted field ids that wanted 336.
        floors = [self._headingWidth(column) for column in columns]
        hints = [self.sizeHintForColumn(column) for column in columns]
        wanted = [max(floor, hint) for floor, hint in zip(floors, hints)]
        slack = available - sum(wanted)
        if slack > 0:
            # DP-166. To whichever column holds the longest text, which is
            # the one a reader is most likely to want more of. The old rule
            # said "the last column, it reads as prose" -- but the last
            # column is `Calculation`, and what it holds is a version stamp
            # like `snappy.derivation.v1`, the same string down the whole
            # column wherever it is not a hyphen. It was the one column
            # that could not use the room.
            wanted[hints.index(max(hints))] += slack
        else:
            # Every column gives up the same share of what it holds above
            # the floor. Cutting the widest first instead would take it all
            # out of the field id, which is the column that says which row
            # this is, to leave white space in the three beside it.
            over = -slack
            room = [width - floors[index]
                    for index, width in enumerate(wanted)]
            spare = sum(room)
            if spare > 0:
                taken = 0
                for index, have in enumerate(room):
                    cut = min(have, over * have // spare)
                    wanted[index] -= cut
                    taken += cut
                over -= taken
            while over > 0:
                widest = wanted.index(max(wanted))
                if wanted[widest] <= floors[widest]:
                    break
                cut = min(over, wanted[widest] - floors[widest])
                wanted[widest] -= cut
                over -= cut
        for index, column in enumerate(columns):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(column, wanted[index])


_DUPLICATE = 'foammeshDuplicateToggle'


class _HoldHidden(QObject):
    """Hide a duplicate switch again whenever something shows it."""

    def __init__(self, toggle) -> None:
        super().__init__(toggle)
        self._toggle = toggle
        toggle.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and watched is self._toggle:
            QTimer.singleShot(0, self._hide)
        return False

    def _hide(self) -> None:
        from shiboken6 import isValid
        if isValid(self._toggle) and self._toggle.property(_DUPLICATE):
            self._toggle.hide()


def _hold_duplicate(toggle) -> None:
    if not toggle.property(_DUPLICATE):
        toggle.setProperty(_DUPLICATE, True)
        _HoldHidden(toggle)
    toggle.hide()


def _release_duplicate(toggle) -> None:
    toggle.setProperty(_DUPLICATE, False)


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
    #: Whether that button is on the page at all. Plan 32 section 4.5: the
    #: footer is the one forward control, and since the fold its press runs
    #: exactly the stage this button ran and then opens the next row --
    #: `Generate grid & Proceed`, `Castellate & Proceed`, `Snap & Proceed`.
    #: MEASURED across all 29 task pages of both engines, eight showed this
    #: button and seven of them duplicated their own row's footer press, six
    #: of those under the label the footer had just retired. Opt in, not opt
    #: out: a page added later gets the footer and nothing else until
    #: somebody decides its press is an act no row performs. `run_stage`
    #: itself is untouched -- that is what the footer runs.
    run_stage_on_page: bool = False
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
    #: DP-123. What this page now says about starting the *whole* pipeline.
    #: The branch owns that button, so a page that can refuse the run says so
    #: rather than shutting a button of its own -- see ``runAllRefusal``.
    runAllRefusalChanged = Signal(str)
    #: Plan 37 UF5 DP-1043. The user asked to unlock this (locked) task.
    unlockRequested = Signal(str)
    #: Whether this task's published result is the mesh on disk.
    _resultLockedFlag = False
    #: DP-1220. The future an Update press resolves when its patch has
    #: landed (accepted or refused), so a run that saves the page first waits
    #: for it instead of racing it. ``None`` when no Update is in flight.
    _applying = None
    #: DP-1220. Why the last `save()` was refused, in the facade's words, for
    #: the caller that has to say so ('' after a save that was accepted).
    last_save_refusal = ''

    def __init__(self, facade_client, task_id: str, parent=None, *,
                 engine_id: str | None = None):
        super().__init__(parent)
        self._client = facade_client
        self.task_id = task_id
        if engine_id is not None:
            self.engine_id = engine_id
        self._editors: dict[str, FieldEditor] = {}
        #: Field ids whose editor sits behind the Advanced disclosure.
        self._advanced_fields: list[str] = []
        #: Plan 40 UXD4. Field ids in the Expert tier, whichever form holds
        #: them: hidden unless *Show expert settings* is on.
        self._expert_fields: list[str] = []
        #: Whether the reader opened or closed the Common fold themselves, so
        #: a refresh does not undo it.
        self._advancedTouched = False
        self._pending: dict[str, object] = {}
        #: `FieldGroupPage` panels this page has adopted (DP-157). Their
        #: edits are this page's edits: they have no commit control left.
        self._panels: list = []
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
        # DP-230. Both labels stay built and stay filled -- they are where the
        # words are authored, and everything that reads
        # `page._description.text()` still gets them -- and both stay out of
        # the layout, like `_title` above. The help control repeats them
        # verbatim, one press away.
        self._description = QLabel(self)
        self._description.setWordWrap(True)
        self._description.setVisible(False)
        self._status = QLabel(self)
        self._status.setObjectName('engineTaskStatus')
        # DP-572 (0924 rerun2). The locked sentence names every prerequisite
        # still open -- on Qualification summary most of the workflow -- and
        # on one line it asked 417 px, wider than the settings column.
        self._status.setWordWrap(True)
        # Plan 44 MF18 (mockup `.hint`): a quiet 12.5 px line, not a banner.
        self._status.setProperty('foammeshRole', 'hint')
        self._prerequisites = QLabel(self)
        self._prerequisites.setWordWrap(True)
        self._prerequisites.setVisible(False)
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
        # Plan 44 MF18 (VS9). A notice card: one line and "More…", not a
        # wrapped orange paragraph above the first setting.
        self._revisit = NoticeCard(self, kind=NOTICE_WARNING,
                                   name='engineTaskRevisit')
        self._revisit.setAccessibleName(
            self.tr('What editing this task would invalidate'))
        self._revisit.setVisible(False)
        # Plan 33 FORM-03. DP-230 put the page's two paragraphs behind a `?`
        # in a header band of its own. The band is the fault this time: a row
        # of chrome the height of a control, at the top of all twenty-nine
        # pages, holding one button and a badge -- an empty header band above
        # the first thing the page asks for. The words did not move again;
        # they are on the page tooltip and in the Help menu's `What this step
        # is for`, which is where a reader looks for an explanation they did
        # not ask for. `_description` and `_prerequisites` are still built,
        # still filled, and still where the words are authored.
        # Plan 37 UF5 DP-1043. A task whose result is the mesh on disk is
        # read-only, and the page says so where the settings start -- with
        # the one way out beside it. It also says what selecting the row did
        # *not* do: an earlier step's page shows the settings that step was
        # run with, not a mesh of its own.
        #
        # Plan 44 MF18 (VS5, K1-K3, B14). The lock is one 24 px chip in the
        # filter row (mockup `.frow` / `.lockt`), not a paragraph that wrapped
        # to three or four lines beside a full-size button. Its words are the
        # chip's tooltip and accessible description; `_resultLockText` keeps
        # them, out of the layout, as `_description` keeps the page's. The
        # chip asks inline before it unlocks: the first press turns it red
        # with one line saying what goes, the second press is the unlock (the
        # same `unlockRequested`), and the cross or Escape keeps the lock.
        self._resultLockText = QLabel(self.tr(
            'Locked: this step is finished, so its settings are read-only and '
            'the mesh always matches them. Unlocking deletes the mesh and the '
            'quality results; later steps keep their values but go back to '
            'not done.'),
            self)
        self._resultLockText.setObjectName('engineTaskResultLockText')
        self._resultLockText.setWordWrap(True)
        self._resultLockText.setVisible(False)
        self._resultLock = LockChip(self, chip_name='engineTaskUnlock',
                                    ask_provider=self._unlock_ask)
        self._resultLock.setObjectName('engineTaskResultLock')
        self._resultLock.setDescription(LOCKED, ' '.join((
            self._resultLockText.text(), self.tr(
                'A copy is kept so the previous result can be restored.'))))
        self._unlock = self._resultLock.chip
        self._unlock.setAccessibleName(self.tr('Unlock to edit'))
        self._resultLock.unlockConfirmed.connect(
            lambda: self.unlockRequested.emit(self.task_id))
        self._resultLock.confirmingChanged.connect(self._on_lock_confirming)
        self._resultLock.setVisible(False)
        # Plan 41 GN18. A refused write is a line on the page, not a box that
        # holds the window: the fields it names stay in reach.
        self._failureNote = NoticeCard(self, kind=NOTICE_WARNING,
                                       name='engineTaskFailure')
        self._failureNote.setVisible(False)
        for widget in (self._status, self._failureNote, self._revisit,
                       self._warnings, self._preview_note):
            outer.addWidget(widget)
        # Three pages -- Gmsh Size Fields, snappy Domain Regions, snappy Mesh
        # QA -- append a paragraph of their own to `_description` after
        # `refresh` has read it, and then tell the help control to re-read.
        # That call is still exactly right; only the thing it used to reach
        # has gone, so the seam stays and lands the words where the two
        # routes now read them.
        self._help = _StepHelpSink(self)

        # Plan 40 UXD3 (F7a). Typing narrows the page to the settings whose
        # name has the words; shown only when the page has settings.
        self._filter = make_filter_box(self)
        self._filter.setVisible(False)
        self._filter.textChanged.connect(self._on_filter_text)
        # Plan 44 MF18 (B14). One row: the filter box and the lock chip, gap
        # 6 (mockup `.frow`). The row shows while either of them does.
        self._filterRow = FilterRow(self._filter, self._resultLock, self)
        self._filterRow.setObjectName('engineTaskFilterRow')
        outer.addWidget(self._filterRow)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        # Plan 44 F4 (MF14 `SHEET_INSET`: one 8 px page margin a side). The
        # page's own layout already insets the scroller, and the body inset
        # its cards 8 px again, so a 1280 px window's sheet gave the cards
        # 233 px of a 275 px column and Base grid, Castellation and Gmsh
        # Quality were cut on the right.
        body_margins = self._body_layout.contentsMargins()
        self._body_layout.setContentsMargins(
            0, body_margins.top(), 0, body_margins.bottom())
        scroll = QScrollArea(self)
        self._scroll = scroll
        scroll.setWidgetResizable(True)
        # C5. The page scrolled sideways and so did the table inside it:
        # two horizontal scrollbars stacked a few pixels apart, and the
        # outer one moved the headings along with the row it was meant to
        # reveal. Only the innermost scrollable thing should scroll.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._body)
        outer.addWidget(scroll, 1)
        # Plan 44 MF18 (B10). A locked page is read-only without being
        # disabled: the text stays at full contrast and the editors carry the
        # dashed look, while this guard refuses the input and the focus.
        self._readOnly = ReadOnlyGuard(
            self._body, exempt=self._stays_live_when_locked,
            focus_target=lambda: self._unlock,
            wheel_target=lambda: self._scroll.verticalScrollBar())

        self._guided = self._make_group(self.tr('Guided'))
        # DP-149. Advanced used to be a checkable QGroupBox, which in Qt
        # disables everything inside it when unchecked -- and unchecked was
        # the default.  The settings behind it are not off: `_populate_fields`
        # registers their editors in `self._editors` exactly like the guided
        # ones, and nothing anywhere reads the box's checked state, so a
        # greyed-out `Gamma` is the quality measure the mesh is built with.
        # The rest of the product already had the right idiom for this -- the
        # Castellation page's own Advanced section is a `FolderHeader` that
        # shows and hides its contents -- so two Advanced sections meant two
        # opposite things.  Now both hide, and the header says when there is
        # something behind it that is not at its default.
        self._advancedTitle = self.tr('Advanced')
        self._advancedHeader = FolderHeader(self._advancedTitle, self)
        self._advancedHeader.setObjectName('advancedDisclosure')
        # Plan 33 FORM-01. DP-149 made this a disclosure instead of a switch
        # and left it closed, which was right about the mechanism and wrong
        # about the default: MEASURED, nine of the twenty-nine task pages
        # opened with applicable, editable settings folded away while a
        # read-only table of native mappings sat open below them. Every
        # setting behind it applies -- an inapplicable one is not on the form
        # at all now (FIELD-02, `FieldEditor.setApplicability`) -- so it opens.
        # The header stays a `FolderHeader`: the reader can still fold it.
        #
        # W-O2 moved the `setChecked(True)` that used to sit here into
        # `FolderHeader` itself, because a page outside this class inherits
        # nothing from it and one such page had already opened closed.
        self._body_layout.addWidget(self._advancedHeader)
        self._advanced = self._make_group('')
        self._advancedHeader.setContents(self._advanced)
        # Plan 40 UXD4 (F3f). Common is folded unless something in it is off
        # its default; the reader's own press is kept.
        self._applyingTiers = False
        self._advancedHeader.toggled.connect(self._on_advanced_touched)
        # Expert settings: one switch for the whole application.
        self._expertToggle = make_expert_toggle(self)
        self._expertToggle.setVisible(False)
        self._body_layout.addWidget(self._expertToggle)
        self._expert = self._make_group('')
        self._expert.setObjectName('expertSettings')
        self._expert.setVisible(False)
        expert_visibility().changed.connect(self._on_expert_visibility)

        self._calculated = CalculatedTable(4, self)
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
        # calculation description absorb the slack. DP-158 does that fitting
        # in `CalculatedTable.fitColumns`, because `ResizeToContents` answers
        # with the content's width whether or not the panel has it. The one
        # global minimum this used to carry, the width of `Native mapping`,
        # is per-column now: a column's own heading is its floor.
        # Plan 33 FORM-02/SIZE-02/VOLUME-01/LAYER-04. The table is built and
        # filled exactly as before and is not in the form: it is mounted in
        # the Details dialog, reached from the Help menu and from the link at
        # the foot of the form. Field id, classification, native dictionary
        # key and calculation version are what the engine does with the
        # settings, not settings; ten of the twenty-nine pages spent the foot
        # of their one narrow column on them.
        self._calculated.setVisible(False)
        self._detailsLink = QPushButton(self.tr('Details…'), self)
        self._detailsLink.setObjectName('stepDetailsLink')
        self._detailsLink.setFlat(True)
        # DP-186: the words on the control are its name; the explanation
        # is its description.
        self._detailsLink.setAccessibleDescription(
            self.tr('Open the calculated settings for this step.'))
        self._detailsLink.setVisible(False)
        self._detailsLink.clicked.connect(self.showDetails)
        self._body_layout.addWidget(self._detailsLink)

        self.build_sections(self._body_layout)
        # Plan 44 MF14. A card with nothing visible in it is not drawn; the
        # three the page manages itself keep their own rules.
        guard_cards(self, skip=(self._guided, self._advanced, self._expert))
        self._body_layout.addStretch(1 if not self._bodyStretchClaimed else 0)
        self._bodySpacerIndex = self._body_layout.count() - 1

        # Plan 43 LY13 (D4, rows D-008 and D-009). Preview, Update, Revert
        # and edit and the stage run are acts of this page, pressed from the
        # footer drop-down (`footerActions`) and the header menu
        # (`headerActions`). The page draws no button for any of them: they
        # are `PageAction` objects, which hold the enabled state, the label
        # and the press, and cannot be drawn.
        self._preview = PageAction(self.tr('Preview'), self)
        self._update = PageAction(self.tr('Update'), self)
        self._revert = PageAction(self.tr('Revert and edit'), self)
        # Plan 30 WP-09 / F-23. The whole-pipeline run is not a per-task
        # action; it is the branch row's "Run to end"
        # (`EngineBranchView._runAll`) and `runRequested` survives for the
        # branch to drive. The one stage a footer press does not perform --
        # `gmsh.publish`, *Publish again* -- is offered by `footerActions`
        # when the page sets `run_stage_on_page`.
        self._runStage = PageAction(self.tr(self.run_stage_label), self)
        self._runStage.setAccessibleDescription(
            self.tr('Run only this step, leaving the rest of the mesh alone.'))
        self._preview.clicked.connect(self.preview)
        self._update.clicked.connect(self.apply)
        self._revert.clicked.connect(self.revert)
        self._runStage.clicked.connect(
            lambda: self.stageRunRequested.emit(
                self.task_id, str(self.run_stage or '')))
        self._commitButtons = (self._preview, self._update, self._revert)

        self.setRunStageAvailable(not self.run_stage_unavailable,
                                  self.run_stage_unavailable)
        self.refresh()

    def setResultLocked(self, locked: bool) -> None:
        """Make the page read-only while its result is the mesh on disk.

        Plan 37 UF5 DP-1043. The facade refuses the edit either way
        (DP-1040); this is so the page does not offer one it will refuse.
        The whole settings body goes read-only -- editors and adopted panels
        alike -- and so do the commit buttons. Plan 44 MF18: read-only, not
        disabled; see :class:`ReadOnlyGuard`.
        """
        locked = bool(locked)
        self._resultLockedFlag = locked
        if not locked or self._resultLock.state() != CONFIRM:
            self._resultLock.setState(LOCKED)
        self._resultLock.setVisible(locked)
        # Plan 44 MF18 (B10). Not `self._body.setEnabled(False)`: that greyed
        # every label and value on the page (DP-148 dims a disabled label on
        # purpose). The guard holds the body read-only at full contrast.
        self._readOnly.setLocked(locked)
        self._revert.setEnabled(not locked)
        if locked:
            self._update.setEnabled(False)
            self._preview.setEnabled(False)
        self._lockChildTables(locked)
        if getattr(self, '_expertToggle', None) is not None:
            self._apply_tiers()

    def _lockChildTables(self, locked: bool) -> None:
        """DP-1190. Tell this page's row tables -- and an editor open over
        one of them -- that the step locked (or unlocked) under them."""
        from .child_controls import ChildControlPanel
        from .lock_refusal import locked_sentence

        task = self._task if isinstance(getattr(self, '_task', None), dict) else {}
        title = str(task.get('title') or self.task_id or '')
        note = locked_sentence([title] if title else [])
        for panel in self.findChildren(ChildControlPanel):
            try:
                panel.setRowsLocked(locked, note)
            except Exception:                                # noqa: BLE001
                pass

    def isResultLocked(self) -> bool:
        return self._resultLockedFlag

    def isReadOnly(self) -> bool:
        """Whether the settings body refuses input (the step is locked)."""
        return self._readOnly.isLocked()

    def _stays_live_when_locked(self, widget) -> bool:
        """A locked page still folds, unfolds and shows its details: those
        change what is on screen, never a setting."""
        return (isinstance(widget, FolderHeader)
                or widget is getattr(self, '_detailsLink', None)
                or widget is getattr(self, '_expertToggle', None))

    def _on_lock_confirming(self, confirming: bool) -> None:
        """The ask line takes the filter box's place while it is up."""
        if confirming:
            self._filter.setVisible(False)
        else:
            self._filter.setVisible(bool(self._editors or self._panels))

    def _unlock_ask(self) -> tuple:
        """``(short, whole)``: what unlocking this step discards.

        Read from the same preview the confirmation reads, so the line names
        the later steps that go back to not done (mockup `.ask`).
        """
        try:
            preview = query(self._client, 'mesh.workflow.unlock_preview',
                            {'engine_id': self.engine_id,
                             'task_id': self.task_id}).payload or {}
        except Exception:                                    # noqa: BLE001
            preview = {}
        return unlock_ask_text(preview, self.task_id)

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

    # -- Plan 40 UXA4: where the page's secondary acts live ---------------- #

    def hasEditsToCommit(self) -> bool:
        """Whether this page has anything an edit could be committed to."""
        return bool(self._editors or self._panels)

    def footerActions(self) -> list:
        """``(object name, label, tooltip, callback)`` for the footer's menu.

        Only the acts that can be pressed now: a disabled Preview is not
        offered. Preview only shows what an edit would change; Update
        preview saves the edit without leaving the page.
        """
        actions = []
        # Plan 43 LY13 (D4, D-009). The one stage run no footer press
        # performs (`gmsh.publish`, *Publish again*) is offered here, where
        # the page used to draw a button for it.
        if (self.run_stage and self.run_stage_on_page
                and self._runStage.isEnabled()):
            actions.append((
                'footerPublishAgain' if self.run_stage == 'publish'
                else 'footerRunStage',
                self.tr(self.run_stage_label),
                self._runStage.accessibleDescription(),
                self._runStage.click))
        if not self.hasEditsToCommit():
            return actions
        if self._preview.isEnabled():
            actions.append((
                'footerPreviewOnly', self.tr('Preview only'),
                self.tr('Show what the pending edit would change, without '
                        'saving it.'), self.preview))
        if self._update.isEnabled():
            actions.append((
                'footerUpdatePreview', self.tr('Update preview'),
                self.tr('Save the pending edit and stay on this page.'),
                self.apply))
        return actions

    def headerActions(self) -> list:
        """``(object name, label, enabled, tooltip, callback)`` per act.

        The same four rows on every page, in the same order; the branch adds
        *Restore previous mesh*, which belongs to the case, not the page.
        """
        commits = self.hasEditsToCommit()
        unlocked = not self._resultLockedFlag
        # Plan 40 UXD4: a saved setting off its default is something to reset
        # as much as a pending one is.
        resettable = any(editor.canReset() for editor in self._all_editors())
        details = self.detailRows() > 0
        return [
            ('taskRevertAndEdit', self.tr('Revert and edit'),
             commits and unlocked,
             '' if commits and unlocked else self.tr(
                 'Nothing on this page can be reverted.'),
             self.revert),
            ('taskDetails', self.tr('Details…'), details,
             '' if details else self.tr(
                 'This step has no calculated settings to show.'),
             self.showDetails),
            ('taskResetDefaults', self.tr('Reset page to defaults'),
             resettable and unlocked,
             '' if resettable and unlocked else self.tr(
                 'Every setting on this page is already at its default.'),
             self.resetToDefaults),
        ]

    def _all_editors(self) -> list:
        """This page's editors and those of the panels it adopted."""
        found = list(self._editors.values())
        for panel in self._panels:
            found.extend(getattr(panel, '_editors', {}).values())
        return found

    def resetToDefaults(self) -> int:
        """Put every editable field back to its registry default.

        Pending only: nothing is saved until the page is updated, so the
        press can be undone with *Revert and edit*. Returns how many fields
        moved.
        """
        moved = 0
        for editor in list(self._all_editors()):
            try:
                # `resetToDefault` sets the default and announces the edit
                # through `valueChanged`, which is `_on_field_changed` here.
                if not editor.resetToDefault():
                    continue
            except Exception:                                # noqa: BLE001
                continue
            moved += 1
        return moved

    def runAllRefusal(self) -> str:
        """Why the whole-pipeline run cannot start, or `''` if it can.

        DP-123. ``setRunStageAvailable`` gates ``_runStage``, which is only
        shown for a page that declares ``run_stage`` -- and MEASURED, no Gmsh
        task page declares one, so on that engine it gates a button that is
        never on screen. The control a Gmsh run actually starts from is the
        branch heading's "Run to end"
        (:attr:`~foammesh.view.main_window.engine_branch.EngineBranchView._runAll`),
        which nothing asked. This is the question the branch asks it, so a
        refusal the page already knows reaches the button that would have
        ignored it.
        """
        return ''

    def setRunStageLabel(self, text: str) -> None:
        """Name the action the button performs, when the page knows better.

        CP-09 item 3: which check qualifies a mesh depends on the target
        solver, and "Run this step" names neither of the two things it might
        do. Pages that can read the target say which one they mean.
        """
        self._runStage.setText(str(text))

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
        # Plan 44 MF14. The form is the sheet's grid -- label | editor | unit
        # on one row, 6 px apart -- and not a QFormLayout, which sizes its
        # label column per form and wraps a label above its editor in a
        # narrow column. DP-105 (one scale for Guided and Advanced) is kept:
        # `_align_field_columns` shares ONE label width between every form
        # on the page, capped at 44 % of the page.
        form = SheetForm(box)
        form.setContentsMargins(0, 0, 0, 0)
        # An untitled card (Advanced and Expert sit under their own fold
        # header) does not reserve the title's 26 px.
        box.setProperty('foammeshUntitled', not title)
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
        # Plan 32 §5.4 put an `Optional` badge on the six task pages the
        # workflow declares optional. Plan 33 FORM-03/FIELD-01/LAYER-05 takes
        # the badge off the form: it is workflow state, the outline row
        # beside the page already carries it, and it sat in the header band
        # that has gone. The sentence itself is not lost -- it is the page's
        # accessible description, so a screen reader still hears it, and the
        # page is still the only thing that knows.
        self._optional = bool(self._task.get('optional'))
        self.setAccessibleName(self._title.text())
        self._description.setText(self._task.get('description', ''))
        depends = self._prerequisite_ids()
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
        # DP-230. After `refresh_status`, not before: the qualification pages
        # rewrite the prerequisite line in there ("Waiting on: ..."), and the
        # help has to carry the sentence the page settled on, not the one it
        # started with. Plan 33 FORM-03: the two routes to it are this
        # tooltip and the Help menu, both reading `stepHelpText()`.
        self.setToolTip(self.stepHelpText())
        self._title.setToolTip(self.toolTip())
        self._title.setAccessibleDescription(self.toolTip())
        self.setAccessibleDescription(self.accessibleStepDescription())
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
        # Plan 41 GN25c. "field: 4" beside every control the size field has
        # proposed a value for.
        from foammesh.view.sizing import badges as sizing_badges
        sizing_badges.apply_to_page(self)
        # Plan 33 SIZE-01. The note is not a standing paragraph any more, so
        # a refresh clears it: it is written at the moment of an edit that
        # has a downstream cost and taken away again when the edit is gone.
        self._clear_revisit_note()
        self._set_dirty(False)

    def refresh_keeping_edits(self, *_args) -> None:
        """Re-read the page after one of its own tables wrote a row.

        Plan 37 UF20. MEASURED live (snappy elbow): a concave angle and a
        merge-faces choice typed on Boundary layers, and the two cell limits
        typed on Castellation, were gone by Proceed -- each page's table
        wrote a row, its ``childrenChanged`` re-read the whole page, and the
        re-read put the stored values back over the typed ones and dropped
        them from the page's unsaved edits. A row the user adds is not a
        reason to throw away what they typed above it; a history move, which
        calls ``refresh`` itself, still is (DP-363).
        """
        pending = dict(self._pending)
        focused = _focused_field(self)
        self.refresh()
        if not pending:
            _refocus(self, focused)
            return
        for field_id, value in pending.items():
            editor = self._editors.get(field_id)
            if editor is None:
                continue
            editor.set_value(value)
            self._pending[field_id] = value
        self._inactive_fields = refresh_applicability(
            self._client, self._editors, self._pending)
        self._refresh_advanced_header()
        self._set_dirty(bool(self._pending))
        # DP-1222. `refresh` worked its derived values out from the stored
        # ones -- `reload_values` had just cleared the edits -- so a page
        # whose estimate follows what is typed showed the stored sizing's
        # count beside the typed sizing until the next keystroke.
        derived = getattr(self, 'refresh_derived', None)
        if callable(derived):
            derived()
        _refocus(self, focused)

    def refresh_derived(self) -> None:
        """Work out again what this page computes from the case and the view.

        DP-1221. MEASURED offscreen (audit probe, snappy Base grid): built
        before any geometry was on screen, the page's background estimate
        stayed hidden after a surface was imported, and after the surface
        was replaced with one ten times the size it went on reading the old
        count. The estimate was only worked out when the page re-read itself,
        and nothing that changes the geometry asked it to. The engine branch
        calls this when the case changes under the page and when the page is
        opened; the default recomputes the background estimate on the pages
        that carry one (Base grid and Castellation share it).
        """
        estimate = getattr(self, 'refresh_estimate', None)
        if callable(estimate):
            try:
                estimate()
            except Exception:                                # noqa: BLE001
                # A sentence the page cannot work out is one it does not say;
                # it is never a reason to take the page down with it.
                pass

    @property
    def save_pending(self) -> bool:
        """Edits that a run must wait for: unsaved, or an Update in flight."""
        applying = self._applying
        return self.is_dirty or bool(
            applying is not None and not applying.done())

    def _prerequisite_ids(self) -> tuple:
        """The tasks this line should name: what the user must settle first.

        Plan 31 DP-144, MEASURED on the `dp143-labels` leg. `depends_on` is
        the chain the state machine walks, and Gmsh threads its five optional
        tasks through that chain in a line, so Compute Mesh read "Requires:
        Periodic Pairs" -- a step the run skips on its own, and one this
        interface offers no control to decline. Eight pages across the two
        engines named an optional task this way.

        `requires` comes from `WorkflowDescriptor.required_prerequisites`, and
        is absent from a page model built by an older facade or by a test
        double; `depends_on` is then still the better answer than silence.
        """
        try:
            page = self.page_model()
        except Exception:                                    # noqa: BLE001
            page = {}
        requires = page.get('requires')
        if requires is not None:
            return tuple(requires)
        return tuple(self._task.get('depends_on') or ())

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

    def tellFailure(self, title: str, text: str) -> None:
        """Plan 41 GN18. Say why a write was refused, without blocking."""
        self._failureNote.setText(f'{title}: {text}')
        self._failureNote.setVisible(True)

    def clearFailure(self) -> None:
        self._failureNote.setVisible(False)

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
        sentence = self._status_sentence(status)
        self._status.setText(sentence)
        # Plan 33 FORM-03. An empty line is still a line: a `QLabel` with no
        # text takes its own height and the layout's spacing above and below
        # it, which on a page that says nothing about its status is the space
        # the first setting should be in.
        self._status.setVisible(bool(sentence))
        self._set_warnings(warnings)
        self._last_status = str(status)

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
            # Plan 33 FORM-03. `''` for every state the page has nothing to
            # ask of the reader about, which is every state but these three.
            return _STATUS_TEXT.get(status, '')
        blocking = self._blocking_prerequisites()
        if not blocking:
            return _STATUS_TEXT['locked']
        return self.tr('Locked — complete {0} first.').format(
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
        # DP-144. The required ones, for the same reason the "Requires:" line
        # above names those: "Locked - complete Periodic Pairs first." told a
        # user to complete an optional step to reach Compute Mesh, when what
        # was actually holding it was Global Sizing.
        depends = tuple(page.get('requires') or ())
        if not depends:
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

    def _clear_revisit_note(self) -> None:
        """Take the consequence sentence away: no edit is pending."""
        note = getattr(self, '_revisit', None)
        if note is None:
            return
        note.clear()
        note.setVisible(False)

    def _set_revisit_note(self, field_id: str = '') -> None:
        """Say what the edit just made would cost, at the moment it is made.

        CP-09 wrote this on arrival, as a standing paragraph above the
        settings of every page whose task had already run. Plan 33 SIZE-01
        measures what that came to: a three-line warning on a page where
        nothing had been changed and nothing was therefore at risk -- the boy
        who cried wolf, which is the failure the previous docstring named and
        then committed by firing from `refresh` rather than from an edit.

        It fires from the pending-patch path now, and only for a field whose
        own descriptor declares a downstream cost. One sentence, and it goes
        again when the edit does.
        """
        note = getattr(self, '_revisit', None)
        if note is None:
            return
        if str(getattr(self, '_last_status', '')) not in self._ALREADY_RUN:
            self._clear_revisit_note()
            return
        costs = self.invalidation_costs(field_id)
        if not costs:
            self._clear_revisit_note()
            return
        if len(costs) == 1:
            listed = costs[0]
        else:
            listed = ', '.join(costs[:-1]) + self.tr(' and ') + costs[-1]
        note.setText(
            self.tr('Applying this change discards {0}.').format(listed))
        note.setVisible(True)

    def invalidation_costs(self, field_id: str = '') -> list:
        """What this page's fields declare they invalidate, in words.

        Read off the shared metadata rather than listed per page, so a field
        moving between tasks cannot leave a page describing the wrong cost.
        Plan 33 SIZE-01: named one field, it answers for that field alone,
        so the reader is told the cost of the edit they just made rather than
        the cost of every edit the page could carry.
        """
        editors = list(self._editors.values())
        if field_id:
            one = self._editors.get(field_id)
            editors = [] if one is None else [one]
        tokens: list[str] = []
        for editor in editors:
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

    def stepHelpDetail(self) -> tuple:
        """This page's own two paragraphs: what it is for, what it needs.

        The single place both routes read from. The words are still authored
        on the page, in `_description` and `_prerequisites`, exactly as they
        were when a `?` button in the header band showed them.
        """
        return (self._description.text(), self._prerequisites.text())

    def stepHelpText(self) -> str:
        """The same two paragraphs as one piece of prose."""
        parts = [part.strip() for part in self.stepHelpDetail()
                 if str(part).strip()]
        return '\n\n'.join(parts)

    def accessibleStepDescription(self) -> str:
        """What a screen reader is told about the step, before its fields.

        Plan 32 §5.4 put an `Optional` badge on the header band; Plan 33
        FORM-03 took the band away. The sentence is not lost -- a reader who
        cannot see a badge never had it, and both the words the badge said
        and the words the `?` said are here, on the page itself.
        """
        parts = [self.stepHelpText()]
        if self._optional:
            parts.append(self.tr('This step is optional.'))
        return '\n\n'.join(part for part in parts if part)

    def derived_quantities(self) -> tuple:
        """`(label, value, unit)` rows this page derives from its settings.

        Plan 33 FORM-02. The allow list is per page and it is empty here: a
        page says what it has worked out only if it has decided the number is
        worth a reader's attention, which is the opposite of the table this
        replaced -- that one published every native mapping the engine
        happened to have.
        """
        return ()

    def detailRows(self) -> int:
        """How many rows the Details dialog would have to show."""
        return self._calculated.rowCount() + len(self.derived_quantities())

    def showStepHelp(self):
        """Open `What this step is for` for this page.

        Returns the dialog so a caller -- the Help menu, a gate -- can read
        what it put on screen.
        """
        dialog = StepHelpDialog(self._title.text(), self._description.text(),
                                self._prerequisites.text(), self)
        self._helpDialog = dialog
        dialog.open()
        return dialog

    def showDetails(self):
        """Open `Calculated settings for this step`, or nothing to open."""
        if not self.detailRows():
            return None
        dialog = StepDetailsDialog(self._title.text(), self._calculated,
                                   self.derived_quantities(), self)
        self._detailsDialog = dialog
        dialog.open()
        return dialog

    def _box_has_live_row(self, box) -> bool:
        """False only when the box holds editors and none of them applies."""
        mine = [editor for editor in self._editors.values()
                if box.isAncestorOf(editor.editor)]
        if not mine:
            return True
        layout = box.layout()
        rows = layout.rowCount() if hasattr(layout, 'rowCount')             else layout.count()
        return rows > sum(1 for editor in mine if not editor.isRowShown())

    def _update_empty_sections(self) -> None:
        """Hide the boxes that have nothing in them.

        The qualification tasks publish no editable fields at all, so the page
        drew an empty Guided box, an empty Advanced box and an empty
        four-column table -- some 350px of chrome around no content, above a
        blank page. What the task does is in its description; the boxes only
        earn their space once they hold something.
        """
        # Plan 40 UXB6 (#53). A box whose every row is taken away by its
        # clause is as empty as one with no rows.
        self._guided.setVisible(
            self._guided.layout().count() > 0
            and self._box_has_live_row(self._guided))
        has_advanced = (self._advanced.layout().count() > 0
                        and self._box_has_live_row(self._advanced))
        self._advancedHeader.setVisible(has_advanced)
        # Visible only when there is something to show *and* the disclosure is
        # open; setVisible(True) here would re-open a section the user closed.
        # Plan 40 UXD3: while the filter has text it decides, not the fold.
        self._advanced.setVisible(
            has_advanced and (self._advancedHeader.isChecked()
                              or bool(self.filterText())))
        self._update_expert_chrome()
        # Plan 33 FORM-02. The link, not the table: the table belongs to the
        # Details dialog now, and the link is only worth a row on the form
        # when that dialog would have something in it.
        # Plan 40 UXA4 (#25). Details moved to the header's ... menu.
        self._detailsLink.setVisible(False)
        # Plan 33 SIZE-05. Preview, Update and Revert and edit commit an edit
        # to this page's own settings. A page with no editor and no adopted
        # panel has no edit to commit -- MEASURED, the qualification and
        # report pages of both engines are in that position -- and on those
        # pages `apply` returned at its first line and `preview` at its own,
        # so three live-looking buttons did nothing at all.
        # Plan 40 UXA4: these stay out of the page; see ``headerActions``.

    def _set_warnings(self, warnings) -> None:
        """Keep what the derivation changed, without a banner over the fields.

        Plan 40 UXB2 (#50). The fields say what was derived ("Auto (15.4 mm)"),
        so nothing sits above them. The sentences are still the status line's
        tooltip and ``warningTexts()``, for a reader who wants the reasons.
        """
        texts = [str(text) for text in warnings if str(text).strip()]
        self._warningTexts = tuple(texts)
        self._warnings.setText('')
        self._warnings.setVisible(False)
        status = getattr(self, '_status', None)
        if status is not None:
            status.setToolTip(
                '\n'.join('- ' + text for text in texts))

    def warningTexts(self) -> tuple:
        """The derivation sentences the last status read carried."""
        return tuple(getattr(self, '_warningTexts', ()))

    def effective_values(self) -> dict:
        """``{field id: 'words'}`` for the unset fields this page can resolve.

        Plan 40 UXB2 (#38). The default asks the one sizing source; a page that
        resolves more overrides it. A field missing here shows whose default
        applies.
        """
        from foammesh.view.workflow_controls.effective_values import (
            size_effective_texts,
        )
        return size_effective_texts(self._client, tuple(self._editors))

    def _apply_effective_values(self) -> None:
        try:
            effective = self.effective_values()
        except Exception:                                    # noqa: BLE001
            effective = {}
        for field_id, editor in self._editors.items():
            editor.set_effective(effective.get(field_id))

    def _populate_fields(self, fields) -> None:
        for layout in (self._guided.layout(), self._advanced.layout(),
                       self._expert.layout(), *self.field_forms()):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            # A QFormLayout emptied item by item keeps its row structure: the
            # rows are still there, now blank, and the next addRow lands below
            # them. Left alone, a page whose fields land in a form of its own
            # grew a row of blank space per refresh.
            if isinstance(layout, (QFormLayout, SheetForm)):
                while layout.rowCount():
                    layout.removeRow(0)
        self._editors.clear()
        self._advanced_fields.clear()
        self._expert_fields.clear()
        # A page with no Essential field has nothing for a fold to sit under:
        # its Common settings are the form, and are drawn as such.
        has_essential = any(
            tier_of(item['field_id']) == ESSENTIAL for item in fields
            if '{id}' not in item['field_id']
            and item['field_id'] not in FIELD_REGISTRY.collections
            and self.renders_field(item['field_id']))
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
            if not self.renders_field(field_id):
                continue
            try:
                descriptor = self._client.descriptor(field_id)
            except (KeyError, ValidationFailedError):
                # A published task field with no registry descriptor would be a
                # no-op control, which gate 10 forbids. Surface it instead of
                # silently rendering something the schema cannot back.
                self._add_unbacked_notice(field_id)
                continue
            editor = FieldEditor(descriptor, self,
                                 choices=self.field_choices(field_id))
            editor.valueChanged.connect(self._on_field_changed)
            classification = _classification(field.get('classification'))
            # Plan 40 UXD4. The registry's tier, not the classification,
            # decides where a field sits: Essential in the form, Common
            # behind the fold, Expert behind the switch.
            tier = editor.tier
            if tier == EXPERT:
                box = self._expert
                self._expert_fields.append(field_id)
            elif tier == COMMON and has_essential:
                box = self._advanced
                self._advanced_fields.append(field_id)
            else:
                box = self._guided
            form = self.field_form(field_id, classification)
            parent = box
            if form is None:
                form = box.layout()
            else:
                parent = form.parentWidget() or box
            row = QHBoxLayout()
            # DP-518. The form's own spacing separates rows. Left on the
            # style's 8 px margins, a 30 px spin box took a 46 px row --
            # MEASURED on the snappy Castellation page, 16 px of nothing under
            # each of its twenty-odd Advanced rows.
            row.setContentsMargins(0, 0, 0, 0)
            # DP-156. The unit is added whether or not there is one: the
            # column alignment gives every one of them the same width, so a
            # row with `deg` and a row with nothing end their editors at the
            # same x. DP-2873: a number box keeps a compact width.
            add_editor_row(row, editor)
            container = QWidget(parent)
            container.setLayout(row)
            form.addRow(editor.label, container)
            # Plan 33 FORM-01. The editor needs its layout and its row widget
            # to take its own row away when the field does not apply: hiding
            # the three widgets alone leaves `QFormLayout` holding the line.
            editor.setRow(form, container)
            self._editors[field_id] = editor
        self.reload_values()
        # Plan 40 UXD3. A rebuild keeps what the reader typed in the filter.
        text = self.filterText()
        for editor in self._editors.values():
            editor.setFilterText(text)
        self._filter.setVisible(bool(self._editors or self._panels))
        self._apply_tiers()

    def field_choices(self, field_id: str):
        """Options to offer *field_id* as a picker, or ``None`` for its own editor.

        Plan 43 D1. A page whose field names another row of the case (the
        background box names a Hex6 volume) offers those rows by name,
        ``[(label, value, status, enabled), ...]``, rather than a typed id.
        """
        return None

    def renders_field(self, field_id: str) -> bool:
        """Whether this page draws its own editor for one of its task's fields.

        DP-153. A task binds the fields it owns, and the page renders every
        one of them. A page that also mounts a panel of its own over the same
        ids therefore drew each of them twice: the QA page bound the twenty-
        seven ``meshQuality`` limits *and* mounted ``_MeshQualityGroup``,
        whose ``field_ids`` are the same twenty-seven, so every limit appeared
        in two editors -- two `Max Non Orthogonality` rows, two `Min Vol`
        rows, fifty-four controls where there are twenty-seven values. The
        two sets are independent: typing in one leaves the other showing the
        number that was there before, and each has its own Apply, so the mesh
        is decided by whichever button was pressed last while the other copy
        still reads a value that is no longer true.

        Returning ``False`` says the panel owns the field. The binding stays
        -- it is what makes the field part of the task -- and only the
        duplicate editor goes.
        """
        return True

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
        if self._editors:
            values = self._client.field_values(tuple(self._editors))
            for field_id, editor in self._editors.items():
                editor.set_value(
                    values.get(field_id, editor.descriptor.default))
            self._pending.clear()
            # CP-09 item 4. After the values, because what a field's condition
            # reads may be one of the values just written.
            self._inactive_fields = refresh_applicability(
                self._client, self._editors, self._pending)
            self._refresh_advanced_header()
        self._apply_effective_values()
        # After applicability, not before: CP-09 appends " (inactive)"
        # to a label it rules out, and a column measured before that
        # would be re-measured the moment the text changed. Outside the
        # guard, because DP-153 leaves a page whose fields are all owned by
        # panels with no editors of its own and those panels still need
        # aligning with each other (DP-154).
        self._align_field_columns()

    #: Plan 44 MF14: the one label column of the page.
    _labelWidths = None
    _all_forms_cache = ()

    def _align_field_columns(self) -> None:
        """Guided and Advanced are two halves of one page: one column.

        DP-151. `apply_form_metrics` has given both groups the same
        alignment and pitch since DP-105, and the DP-105 comment claims
        that puts their editors at the same x. It does not -- each
        `QFormLayout` sizes its label column from its own longest label,
        so Compute Mesh drew Guided's editors 25 px right of Advanced's
        and Global Sizing drew them 69 px apart. Any form the page has
        claimed fields into is aligned with them, because it is on the
        same page and reads as part of the same form.
        """
        forms = [self._guided.layout(), self._advanced.layout(),
                 self._expert.layout()]
        forms.extend(self.field_forms())
        forms.extend(self.aligned_forms())
        forms = [form for form in forms if form is not None]
        # Plan 44 MF14. One label column for every form on the page: the
        # widest label, capped at 44 % of the width the page is given. The
        # watcher re-measures when the page is resized.
        if self._labelWidths is None:
            self._labelWidths = LabelWidthWatcher(
                self._body, lambda: self._all_forms_cache)
        self._all_forms_cache = forms
        self._labelWidths.run()
        # DP-156. The same set of forms, the other end of the row: one width
        # for the unit suffixes, so every editor in the column ends at the
        # same x whether its unit is `deg`, `cells` or nothing.
        align_unit_column(forms)
        self._dedupe_expert_toggles()

    def aligned_forms(self):
        """Forms the page does not own but which read as part of its form.

        DP-154. A page that mounts `FieldGroupPage` panels one under the
        other gets a form per panel, and each sizes its label column from
        its own longest label -- which is DP-151 again, one level down and
        out of reach of the fix, because the panels are separate widgets
        with separate layouts. The QA page put its two panels' editors 20 px
        apart and Surface Features put its two 32 px apart, down a single
        scrolling column. Unlike `field_forms`, these are not emptied on
        refresh: the panel owns its own rows and rebuilds them itself.
        """
        return ()

    def adoptPanel(self, panel):
        """Mount a `FieldGroupPage` as a section of this page.

        DP-157. The panel was written to be a page and brought a page's
        furniture with it. DP-155 took its scroller; this takes its commit
        controls. MEASURED before: the snappy QA page carried Revert/Apply
        for checkMesh's settings, Revert/Apply for the mesh quality limits
        and then Preview/Update/Revert plus Edit/Run this step for the page
        -- three commit controls, seven buttons -- and because the page has
        held no editors of its own since DP-153, its own Update returned at
        the first line of `apply` and committed nothing at all. The obvious
        button did nothing and the two that worked looked like part of the
        form.

        Adopted, a panel's pending values join this page's patch, its
        dirtiness lights this page's Update, and Revert here reloads it.
        """
        panel.setEmbedded()
        panel.dirtyChanged.connect(self._on_panel_dirty)
        self._panels.append(panel)
        if hasattr(panel, 'setFilterText'):
            panel.setFilterText(self.filterText())
        self._filter.setVisible(True)
        return panel

    # -- page filter (Plan 40 UXD3) ----------------------------------------- #

    def filterText(self) -> str:
        """What the filter box holds, one space between the words."""
        return ' '.join(self._filter.text().split())

    def setFilterText(self, text: str) -> None:
        """Type into the filter box, as the reader would."""
        self._filter.setText(str(text or ''))

    def _on_filter_text(self, _text: str = '') -> None:
        text = self.filterText()
        for editor in self._editors.values():
            editor.setFilterText(text)
        for panel in self._panels:
            if hasattr(panel, 'setFilterText'):
                panel.setFilterText(text)
        self._apply_tiers()
        self._update_empty_sections()

    def filterBox(self):
        return self._filter

    def visibleFieldIds(self) -> tuple:
        """Field ids whose row is on screen now, this page and its panels."""
        found = [field_id for field_id, editor in self._editors.items()
                 if editor.isRowShown()]
        for panel in self._panels:
            found.extend(getattr(panel, 'visibleFieldIds', lambda: ())())
        return tuple(found)

    def panels(self):
        """The panels this page has adopted, in mounting order."""
        return tuple(self._panels)

    def _on_panel_dirty(self, dirty: bool) -> None:
        self._set_dirty(bool(self._pending))

    # -- tiers (Plan 40 UXD4) ---------------------------------------------- #

    def _on_advanced_touched(self, *_args) -> None:
        # Only a change the page did not make itself is the reader's.
        if not self._applyingTiers:
            self._advancedTouched = True

    def _on_expert_visibility(self, _shown=None) -> None:
        self._apply_tiers()
        self._update_empty_sections()

    def _apply_tiers(self) -> None:
        """Show or take away Expert rows, and fold Common unless it is live.

        Hidden rows keep their editors and their values: a field the reader
        cannot see is still the value the run reads, and still in `_editors`.
        """
        shown = expert_visibility().shown()
        for field_id in self._expert_fields:
            editor = self._editors.get(field_id)
            if editor is not None:
                editor.setTierHidden(not shown)
        if not self._advancedTouched:
            # Open when a Common setting is off its default (a fold that hides
            # a setting somebody changed is as misleading as a greyed one);
            # folded otherwise.
            self._applyingTiers = True
            try:
                # Plan 44 FIT (DP-2667). A finished step is read, not set:
                # its Advanced stays folded, and the header still counts what
                # is off its default, so nothing is hidden unsaid.
                self._advancedHeader.setChecked(
                    self._common_changed() > 0
                    and not self._resultLockedFlag)
            finally:
                self._applyingTiers = False
            self._advanced.setVisible(
                self._advanced.layout().count() > 0
                and self._advancedHeader.isChecked())
        self._refresh_advanced_header()
        self._update_expert_chrome()

    def _off_default(self, field_id: str) -> bool:
        """Whether a field, as the page now has it, is off its default."""
        editor = self._editors.get(field_id)
        if editor is None or not editor.applies():
            return False
        if field_id in self._pending:
            return not same_as_default(
                self._pending[field_id], editor.descriptor.default)
        return not editor.isAtDefault()

    def _common_changed(self) -> int:
        return sum(1 for field_id in self._advanced_fields
                   if self._off_default(field_id))

    def _expert_changed(self) -> int:
        return sum(1 for field_id in self._expert_fields
                   if self._off_default(field_id))

    def _update_expert_chrome(self) -> None:
        """The switch, and the Expert box, only where there is something."""
        live = [self._editors[field_id] for field_id in self._expert_fields
                if self._editors.get(field_id) is not None
                and self._editors[field_id].applies()]
        filtering = bool(self.filterText())
        self._expertToggle.setVisible(bool(live) and not filtering)
        shown = expert_visibility().shown()
        box_rows = [editor for editor in live
                    if self._expert.isAncestorOf(editor.editor)]
        if filtering:
            self._expert.setVisible(
                any(editor.isRowShown() for editor in box_rows))
        else:
            self._expert.setVisible(bool(box_rows) and shown)
        changed = self._expert_changed()
        base = self.tr('Show expert settings')
        self._expertToggle.setText(
            base if shown or not changed
            else f'{base} ({changed} not at default)')
        self._dedupe_expert_toggles()

    def expertToggles(self) -> list:
        """Every *Show expert settings* switch this page draws, page and panels.

        Plan 44 MF14 (audit B12). The switch is one application-wide setting,
        and a page that adopts a panel carrying expert settings of its own drew
        two of them (snappy Snap: the page's, and the Mesher limits panel's).
        """
        from shiboken6 import isValid
        toggles = [self._expertToggle]
        for panel in self._panels:
            toggles.append(getattr(panel, '_expertToggle', None))
        return [toggle for toggle in toggles
                if toggle is not None and isValid(toggle)]

    def _dedupe_expert_toggles(self) -> None:
        """Keep exactly one switch on the page: the page's, else a panel's.

        The panel's switch is hidden while the page's is showing, and kept
        hidden: the panel re-shows it each time its tiers are applied, so the
        hide is a filter on the switch rather than a one-off ``setVisible``.
        """
        toggles = self.expertToggles()
        if len(toggles) < 2:
            return
        own = self._expertToggle
        keep = own if own in toggles and not own.isHidden() else None
        if keep is None:
            # The page has no expert setting of its own: the first panel's
            # switch is the page's switch; the rest are duplicates of it.
            keep = next((t for t in toggles if t is not own), None)
        for toggle in toggles:
            if toggle is keep:
                _release_duplicate(toggle)
            else:
                _hold_duplicate(toggle)

    def tierCounts(self) -> dict:
        """``{tier: number of fields on this page}``, for a gate."""
        counts = {ESSENTIAL: 0, COMMON: 0, EXPERT: 0}
        for editor in self._editors.values():
            counts[editor.tier] = counts.get(editor.tier, 0) + 1
        return counts

    def _refresh_advanced_header(self) -> None:
        """Say on the closed header when something behind it is not default.

        A disclosure that hides a setting the user changed is as misleading as
        one that greys a setting that is live.  The count is what the header
        is for: it is read without opening anything.
        """
        changed = self._common_changed()
        self._advancedHeader.setText(
            self._advancedTitle if not changed
            else f'{self._advancedTitle}  ({changed} not at default)')

    def inactive_fields(self) -> dict:
        """``{field_id: why it is inactive}`` for this page's editors."""
        return dict(getattr(self, '_inactive_fields', {}) or {})

    def _populate_calculated(self, fields) -> None:
        self._calculated.setRowCount(len(fields))
        # DP-158. Every id in this table belongs to this one task, so every
        # id begins with the same twelve to twenty characters. Spending the
        # column on them left `gmsh.compute.seco...` twice over, two rows
        # that read alike and name different fields. The shared head goes on
        # the heading's tooltip and the whole id on the cell's.
        ids = [str(field['field_id']) for field in fields]
        shared = _shared_field_prefix(ids)
        heading = self._calculated.horizontalHeaderItem(0)
        if heading is not None:
            heading.setToolTip(
                self.tr('Every field id here begins with {0}').format(shared)
                if shared else self.tr('Field id'))
        for row, field in enumerate(fields):
            values = (
                str(field['field_id'])[len(shared):],
                _classification_name(field.get('classification')),
                field.get('native_name') or '-',
                field.get('calculation_version') or '-',
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                # DP-158. A fitted column elides what it cannot show, so the
                # whole value has to stay reachable somewhere.
                if column == 0:
                    item.setToolTip(ids[row])
                elif column == 1:
                    # DP-159. The word is short; what it means is not.
                    item.setToolTip(_classification_help(
                        field.get('classification')) or str(value))
                else:
                    item.setToolTip(str(value))
                self._calculated.setItem(row, column, item)
        # DP-166. Before the fit, so the fit shares the width between the
        # columns that are going to be on screen.
        self._calculated.hideEmptyColumns()
        self._calculated.fitContents()

    # -- editing ----------------------------------------------------------- #

    def _on_field_changed(self, field_id: str, value) -> None:
        self._pending[field_id] = value
        self._set_dirty(True)
        # Plan 40 UXB6. A dependant follows its master as it is ticked.
        if follows_master(self._editors, field_id):
            self._inactive_fields = refresh_applicability(
                self._client, self._editors, None,
                overrides=dict(self._pending))
            self._update_empty_sections()
        # Plan 33 SIZE-01. Here and nowhere else: the moment of an edit with
        # a downstream consequence is the moment that consequence is worth a
        # line, and this is the path every edit on this page already takes.
        self._set_revisit_note(field_id)
        if field_id in self._advanced_fields:
            self._refresh_advanced_header()

    def _set_dirty(self, dirty: bool) -> None:
        # DP-157. An adopted panel has no Apply of its own, so an edit made in
        # one is an edit made on this page and has to light this page's
        # Update.
        dirty = bool(dirty) or any(panel.is_dirty for panel in self._panels)
        dirty = dirty and not self._resultLockedFlag
        self._update.setEnabled(dirty)
        self._preview.setEnabled(dirty)
        self.dirtyChanged.emit(dirty)

    @property
    def is_dirty(self) -> bool:
        return bool(self._pending) or any(
            panel.is_dirty for panel in self._panels)

    def pending_patch(self) -> dict:
        # DP-157. The page's own fields are written last: a field can only be
        # on one surface, so there is nothing to collide, and if that ever
        # stops being true the page the user is looking at should win.
        patch: dict = {}
        for panel in self._panels:
            patch.update(panel.pending_patch())
        patch.update(
            (field_id, value) for field_id, value in self._pending.items()
            if self._editors.get(field_id) is None
            or self._editors[field_id].applies())
        return patch

    def preview(self) -> None:
        if not self.is_dirty:
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

    def _patch_accepted(self, result, carried=None) -> bool:
        """Take an accepted patch, and say whether it was accepted.

        ``carried`` is the patch that was sent. DP-1220: only the edits it
        carried are spent. MEASURED by reading the code through: this
        cleared every unsaved edit and re-read the page, so a field typed
        while an Update or a run's save was on its way went back to its
        stored value and out of the next patch, without a word. ``None`` (a
        caller that does not say) keeps the old answer: everything is spent.
        """
        if getattr(result, 'status', 'accepted') != 'accepted':
            return False
        payload = getattr(result, 'payload', {}) or {}
        self._last_change_set_id = payload.get('change_set_id')
        if carried is None:
            self._pending.clear()
        else:
            for field_id in [key for key, value in self._pending.items()
                             if key in carried
                             and _same_value(carried[key], value)]:
                self._pending.pop(field_id, None)
        # DP-157. The panels' values went out in this patch, so their
        # pending sets are spent; reloading clears them and re-reads
        # what the facade now holds.
        for panel in self._panels:
            panel.reload()
        self.clear_preview()
        self._set_dirty(False)
        # Plan 37 UF3 DP-1034. A patch the facade answered `no_op` changed
        # nothing, so there is nothing to configure: sending the transition
        # anyway staled a finished mesh that the settings still describe,
        # and Proceed then meshed it again for no reason.
        if not payload.get('no_op'):
            self.updateRequested.emit(self._configured_task(payload))
        # DP-1220. What the patch did not carry is still on the page.
        if self._pending:
            self.refresh_keeping_edits()
            return True
        self.refresh()
        return True

    def _configured_task(self, payload) -> str:
        """The task an accepted patch made the next one to run.

        Plan 37 UF15. Usually this page's own. A field the engine reads at an
        earlier stage -- snappy's mesh-quality limits, read by Snap and
        Layers but set on Quality -- comes back with the stage tasks the
        facade staled for it, earliest first, and the run has to start from
        the earliest of those: configuring this page's task instead would
        ask for a check of a mesh that is about to be remade.
        """
        staled = [str(task) for task in (payload.get('staled_tasks') or ())]
        return staled[0] if staled else self.task_id

    def child_edit_committed(self, panel=None) -> None:
        """A table on this page wrote a row: the task's settings changed.

        Plan 37 UF3 DP-1035. Refinement groups, layer groups and size fields
        are written by their own tables, one command per row, and none of
        them told the workflow. MEASURED in the user's report: back to a
        meshed step, add a size field, Proceed -- the step still read done
        and the mesh on screen was the one made without it. A committed row
        is the same news as a saved field, so it is announced the same way.
        """
        self.updateRequested.emit(self.task_id)

    def _child_panels(self) -> list:
        from .child_controls import ChildControlPanel
        return list(self.findChildren(ChildControlPanel))

    async def settle_child_writes(self) -> bool:
        """Wait for this page's table writes; False if one was refused.

        Plan 37 UF3 DP-1035. A row written a moment before Proceed is still
        in flight when the press saves the page, and the settle that follows
        would read the graph from before it.
        """
        settled = True
        for panel in self._child_panels():
            waiter = getattr(panel, 'settle_writes', None)
            if waiter is not None and not await waiter():
                settled = False
        return settled

    def apply(self):
        if not self.is_dirty:
            return None
        import asyncio

        # DP-1220. The patch this press sends, kept, so that when it lands
        # only the edits it carried are taken off the page: a value typed
        # while it was on its way is the user's next edit, not this one.
        carried = self.pending_patch()
        self._failureNote.setVisible(False)
        try:
            landed = asyncio.get_running_loop().create_future()
        except RuntimeError:
            landed = None                   # no loop: the write runs in place

        def applied(result):
            try:
                if not self._patch_accepted(result, carried):
                    self.tellFailure(
                        self.tr('Update failed'),
                        str(getattr(result, 'message', '')
                            or self.tr('The facade rejected the edit.')))
            finally:
                if landed is not None and not landed.done():
                    landed.set_result(None)
                if self._applying is landed:
                    self._applying = None

        # DP-1220. MEASURED by reading the press through: Update returned
        # before the facade had the patch, and a Run pressed in that window
        # wrote the dictionaries from the values before it. `save()` -- which
        # every run now goes through first -- waits on this.
        self._applying = landed
        # C31-12. The patch is scheduled rather than run on the GUI thread;
        # everything that used to follow it runs in `applied` when it lands,
        # so the order is unchanged and the window stays live meanwhile.
        return submit(self._client, 'configuration.patch',
                      {'patch': carried}, then=applied)

    async def save(self) -> bool:
        """Write this page's pending edits and wait to be told the answer.

        DP-254. `apply` is a button handler: C31-12 made its patch a
        scheduled write, so it returns before the facade has taken it and the
        `is_dirty` flag on the next line is still the one from before the
        press. MEASURED on the guided walk, `pipe` for Gmsh: the press on
        `4. Global sizing` applied the ten fields the page had authored, read
        `is_dirty` one line later, was told the page was still dirty and
        returned in silence -- the press did nothing, said nothing, and the
        walk sat on that row until its budget ran out.

        A caller that has to know before it moves the outline awaits this
        instead: the same command, awaited, with the same tail once it lands,
        answering True only when the facade accepted it. The refusal belongs
        to whoever asked, so there is no dialog here (DP-227); its words are
        left in ``last_save_refusal`` for that caller (DP-1220).
        """
        import asyncio

        self.last_save_refusal = ''
        # DP-1220. An Update still on its way is part of this save: wait for
        # it to land, then send whatever it did not carry.
        applying = self._applying
        if applying is not None and not applying.done():
            try:
                await asyncio.shield(applying)
            except Exception:                                # noqa: BLE001
                pass
        if not self.is_dirty:
            return True
        carried = self.pending_patch()
        runner = getattr(self._client, 'run', None)
        try:
            if runner is None:
                # Plan 37 UF3. A client with no loop -- the headless shell,
                # a scripted walk -- still saves; there is nothing to await,
                # so the write simply runs where it is.
                blocking = getattr(self._client, 'run_sync', None)
                if blocking is None:
                    return False
                result = blocking('configuration.patch', {'patch': carried})
            else:
                result = await runner('configuration.patch',
                                      {'patch': carried})
        except Exception as error:                           # noqa: BLE001
            self.last_save_refusal = str(error)
            return False
        accepted = self._patch_accepted(result, carried)
        if not accepted:
            self.last_save_refusal = str(getattr(result, 'message', '') or '')
        return accepted

    def revert(self):
        """Discard pending values or revert only this page's accepted edit."""
        if self.is_dirty:
            self._pending.clear()
            for panel in self._panels:
                panel.revert()
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
                # Plan 33 FORM-03 leaves the routine states silent, so
                # there is no sentence to append to; this one is the whole
                # line, and it is here because a press that changed nothing
                # has to say so.
                self._status.setText(
                    self.tr('Revert and edit did not reopen this task — its '
                            'recorded result still stands.'))
                self._status.setVisible(True)

        def reverted(result):
            # R115. The facade *raises* when a change set is no longer the
            # latest reversible one (`PlanStaleError`), so the status check
            # below was unreachable and the exception escaped the clicked()
            # slot instead: on an accepted Boundary Layers task the button
            # left the page reading "Accepted." with nothing else changed,
            # which is indistinguishable from a dead button. A stale change
            # set is not a reason to refuse to reopen the task -- reopening
            # it for editing is the half of "Revert and edit" that still
            # works -- so it is reported and the lifecycle revert goes ahead.
            # C31-12 keeps that: a refusal now arrives as a failed result
            # rather than as an exception, and `reopen()` still runs after it.
            if getattr(result, 'status', 'accepted') != 'accepted':
                message = str(getattr(result, 'message', '') or '') or self.tr(
                    'This task edit is no longer the latest reversible '
                    'change; no unrelated change was undone.')
                self.tellFailure(
                    self.tr('Revert failed'),
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


def _same_value(left, right) -> bool:
    """Whether a pending edit is still the value a patch carried (DP-1220)."""
    try:
        return bool(left == right)
    except Exception:                                        # noqa: BLE001
        return left is right


def _focused_field(page):
    """The field id whose editor holds the keyboard focus, or ``None``.

    DP-1221. A re-read rebuilds the editors, so the one being typed into is
    deleted under the cursor; the id is what survives the rebuild.
    """
    from PySide6.QtWidgets import QApplication

    try:
        # No application, no focus (a stand-in page built without one).
        focus = (QApplication.focusWidget() if QApplication.instance()
                 else None)
    except Exception:                                        # noqa: BLE001
        return None
    if focus is None:
        return None
    for field_id, editor in (getattr(page, '_editors', None) or {}).items():
        if not isinstance(editor, QWidget):
            continue
        try:
            if editor is focus or editor.isAncestorOf(focus):
                return field_id
        except RuntimeError:
            continue
    return None


def _refocus(page, field_id) -> None:
    """Give the focus back to ``field_id``'s new editor (DP-1221)."""
    if field_id is None:
        return
    editor = (getattr(page, '_editors', None) or {}).get(field_id)
    target = getattr(editor, '_editor', editor)
    if isinstance(target, QWidget):
        try:
            target.setFocus()
        except RuntimeError:
            pass


def _shared_field_prefix(field_ids) -> str:
    """The dotted head every one of these ids shares, trailing dot included.

    DP-158. Empty when there is nothing to share, when a single id would be
    swallowed whole, or when stripping would leave any row with no name.
    """
    ids = [str(field_id) for field_id in field_ids]
    if len(ids) < 2:
        return ''
    parts = ids[0].split('.')[:-1]
    for field_id in ids[1:]:
        other = field_id.split('.')[:-1]
        keep = 0
        while (keep < len(parts) and keep < len(other)
               and parts[keep] == other[keep]):
            keep += 1
        parts = parts[:keep]
        if not parts:
            return ''
    return '.'.join(parts) + '.'


def _classification(value):
    if isinstance(value, FieldClassification):
        return value
    try:
        return FieldClassification(str(value))
    except ValueError:
        return FieldClassification.PRECHECK


#: What each classification is called on screen, and what it means. DP-159:
#: the Calculated settings table printed the enum's own value, so the column
#: read `foammesh_precheck` -- a token from the schema, in the one column of
#: that table whose values are words rather than identifiers.
_CLASSIFICATION_LABELS = {
    FieldClassification.NATIVE: 'Native',
    FieldClassification.DERIVED: 'Derived',
    FieldClassification.PRECHECK: 'Pre-check',
    FieldClassification.EXPERIMENTAL: 'Experimental',
    FieldClassification.DEFERRED: 'Deferred',
}

_CLASSIFICATION_HELP = {
    FieldClassification.NATIVE:
        'Written straight into the engine dictionary named beside it.',
    FieldClassification.DERIVED:
        'Computed from the other settings on this page before the run.',
    FieldClassification.PRECHECK:
        'Checked by FoamMesh before the run; the engine never sees it.',
    FieldClassification.EXPERIMENTAL:
        'Available, but not yet covered by the acceptance runs.',
    FieldClassification.DEFERRED:
        'Recorded and carried, but not acted on by this release.',
}


def _classification_name(value) -> str:
    """What to call this classification on screen (DP-159)."""
    resolved = _classification(value)
    return _CLASSIFICATION_LABELS.get(
        resolved, getattr(resolved, 'value', str(resolved)))


def _classification_help(value) -> str:
    """One line saying what this classification means (DP-159)."""
    return _CLASSIFICATION_HELP.get(_classification(value), '')
