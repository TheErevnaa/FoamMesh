"""Plan 23 WP7B: the pages for the geometry-qualification tasks.

Four of these tasks are `common.*` — they mean the same thing whichever engine
produced the mesh, and §9's parity rule says the same evidence must be visible
from the GUI, the CLI and the API. So they live here rather than inside either
engine's page package, and both engines' registries point at the same classes.
An engine-local copy would be two places to fix a wording change, and the two
would drift.

Each page is deliberately thin. The verdict, its reasons and its numbers all
come from a report the facade already produced; a page that recomputed anything
could disagree with the artifact it displays, and then the operator would have
two answers and no way to choose. What the page adds is the part §9 requires a
human to see: which of the three verdicts this is, whether it blocks, and — for
a non-pass — that a waiver is an explicit decision rather than a dismissal.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer

from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from widgets.fit_to_text import FlowLayout, fit_to_text

from foammesh.core.quality.layer_report import coverage_rows
from foammesh.core.quality.readout import absent
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query, submit
from foammesh.view.theming.metrics import UnitLabel
from foammesh.view.widgets.folder_header import FolderHeader


#: Task states that mean a prerequisite is settled. Same set the task page uses
#: for "still accepted"; a prerequisite in any other state is one the user has
#: to go back to.
_SETTLED = frozenset({'passed', 'warning', 'completed', 'waived', 'skipped'})

#: W-O1. What the fidelity tolerance is, on the field that takes it. A module
#: constant rather than a literal at the call site because the state of the
#: store is appended to it at runtime and the two halves are written in
#: different places.
TOLERANCE_HELP = (
    'How far the mesh may sit from the prepared surface before a section '
    'counts as lost. Nothing is rated until one is set.')


class QualificationTaskPage(EngineTaskPage):
    """One geometry-qualification task, for either engine.

    ``engine_id`` is supplied by the branch that builds the page, because the
    same class serves both. It is not defaulted to either engine: a page that
    silently assumed ``snappy`` would send a Gmsh case's transitions to the
    wrong workflow, and the failure would look like a missing task rather than
    a wrong one.

    Every page here shows the report its task produced. It used to show
    nothing at all: five of the six classes below were docstrings with no body,
    so Snap Fidelity, Native Mesh Fidelity, Geometry Fidelity, Resolution
    Adequacy and Qualification Summary all rendered the same empty grey panel
    before *and* after running (R31, R41, R68, R90, R103, R123, R160). The
    numbers existed the whole time -- in `foammesh/quality/*.json`, read by the
    export preflight -- and the one surface built to display them displayed
    none of it.

    The panel never recomputes and never upgrades a verdict: the projection is
    :mod:`core.quality.readout`, shared with the CLI and the API so the three
    cannot disagree about the same file.
    """

    task_id_default = ''
    #: Whether a non-pass here stops the pipeline. Display only -- enforcement
    #: is the facade's, and duplicating the rule in a widget is how the two
    #: come to disagree.
    blocking = False
    #: Whether this task has a stored report to render. GF0 is the exception:
    #: it runs before a mesh exists and has nothing to measure.
    has_evidence = True
    #: What the panel calls itself, so four consecutive pages are no longer
    #: distinguishable only by their titles (R123).
    evidence_title = 'Measured evidence'
    #: W-O1. Anything else this page's panel has to explain about itself.
    #: Appended to the gate sentence and rendered with it, on the panel and
    #: in the step help -- never as a paragraph over the controls.
    PANEL_HELP = ''

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id)

    # -- evidence ---------------------------------------------------------- #

    def build_sections(self, layout) -> None:
        if not self.has_evidence:
            return
        box = QGroupBox(self.tr(self.evidence_title), self)
        inner = QVBoxLayout(box)

        self._gateNote = QLabel(self)
        self._gateNote.setObjectName('qualificationGateNote')
        self._gateNote.setWordWrap(True)
        # W-O1. What a non-pass does is reasoning about the panel, not a
        # setting, so the sentence is carried rather than drawn:
        # `refreshGateNote` renders it on the panel it describes, as that
        # panel's tooltip and accessible description, and appends it to the
        # step help. Same arrangement as the hidden `_description` label that
        # feeds the Help menu.
        self._gateNote.setVisible(False)
        inner.addWidget(self._gateNote)

        self._evidenceHeadline = QLabel(self)
        self._evidenceHeadline.setObjectName('qualificationHeadline')
        self._evidenceHeadline.setWordWrap(True)
        self._evidenceHeadline.setAccessibleName(
            self.tr('Verdict recorded for this task'))
        # W-O1. A verdict is a measured result and stays. "No report has been
        # read for this task." is the state of the case before there is one,
        # and the outline row beside this page already paints exactly that,
        # so the line appears when there is a verdict to put on it.
        self._evidenceHeadline.setVisible(False)
        inner.addWidget(self._evidenceHeadline)

        self._evidenceCaveat = QLabel(self)
        self._evidenceCaveat.setObjectName('qualificationCaveat')
        self._evidenceCaveat.setWordWrap(True)
        # Rendered as a warning rather than body text: it is the sentence that
        # says a verdict does not mean what it looks like, and it is the one
        # this whole group of defects exists because nobody could read.
        self._evidenceCaveat.setProperty('foammeshStatus', 'warning')
        self._evidenceCaveat.setVisible(False)
        inner.addWidget(self._evidenceCaveat)

        self._evidenceTable = QTableWidget(0, 3, self)
        self._evidenceTable.setObjectName('qualificationEvidence')
        self._evidenceTable.setHorizontalHeaderLabels(
            (self.tr('Section'), self.tr('Verdict'), self.tr('Measured')))
        self._evidenceTable.setAccessibleName(
            self.tr('Measured evidence for this task'))
        self._evidenceTable.setEditTriggers(QTableWidget.NoEditTriggers)
        self._evidenceTable.verticalHeader().setVisible(False)
        self._evidenceTable.setWordWrap(True)
        header = self._evidenceTable.horizontalHeader()
        for column in (0, 1):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # R190. The measured column is a stretch column, so its width is not
        # known when the rows are first sized -- and a row sized for a wider
        # column loses the tail of its sentence to an ellipsis when the column
        # narrows. Re-sizing on every width change keeps the height honest.
        header.sectionResized.connect(lambda *_: self.scheduleRowResize())
        # R186. A row of this table is a wrapped sentence, so four sections
        # need real height; without a floor the caveat above can squeeze the
        # grid down to a single visible row.
        self._evidenceTable.setMinimumHeight(
            self._evidenceTable.horizontalHeader().height()
            + 4 * self._evidenceTable.fontMetrics().height() * 3)
        inner.addWidget(self._evidenceTable, 1)
        layout.addWidget(box)
        self._evidenceBox = box
        # W-O1. After the box exists, because that is what the note is drawn
        # on now.
        self.refreshGateNote()
        # R187. Claimed at construction only so the first paint of a page that
        # does have rows is right; refreshEvidence() hands it back when the
        # report turns out to be empty.
        self.claimBodyStretch(box)

    def evidence_document(self) -> dict:
        """The stored readout for this task, or an empty mapping.

        A failure to read is not a pass: the caller renders the empty case as
        "not run", which is what an unreadable report means for the question
        this page asks.
        """
        try:
            payload = query(
                self._client, 'quality.evidence.read',
                {'task_id': self.task_id}).payload
        except Exception:                                    # noqa: BLE001
            return {}
        payload = payload or {}
        self._qualificationMode = str(payload.get('qualification_mode') or '')
        return dict(payload.get('readout') or {})

    def refreshGateNote(self) -> None:
        """Say what a non-pass on this page will actually do.

        R183. `blocking` is a property of the task; whether a non-pass stops
        anything is a property of the qualification mode, and one
        unconditional sentence conflated the two. MEASURED on the live tee:
        Snap Fidelity announced that a non-pass stops the pipeline, recorded
        `unrated` on a boundary 1.32x past its tolerance, and the walk carried
        straight on into Boundary Layers -- which is correct under the
        shipping default, report-only, and the opposite of what the page had
        just promised. A gate that overstates itself is worse than one that
        does not gate, because the next non-pass reads as safe to ignore.

        W-O1. The label is hidden and the sentence is drawn on the panel it
        is about, as its tooltip and its accessible description, and on the
        step help beside them. The words are unchanged; only the surface is.
        """
        label = getattr(self, '_gateNote', None)
        if label is None:
            return
        if not self.blocking:
            label.setText(self.tr('Diagnostic: this reading is recorded and '
                                  'does not stop the pipeline on its own.'))
        elif getattr(self, '_qualificationMode', '') in ('', 'enforcing'):
            label.setText(self.tr('A non-pass here stops the pipeline.'))
        else:
            label.setText(self.tr(
                'This is a blocking gate, but qualification is in report-only '
                'mode: a non-pass is recorded and does not stop the pipeline '
                'until thresholds are promoted.'))
        self.showGateNote(label.text())

    def showGateNote(self, said: str) -> None:
        """Draw the gate sentence on the panel and on the step help.

        W-O1. Two surfaces, neither of them a paragraph in the settings
        column: the box that holds the verdict carries the sentence for
        anyone who asks it, and the step help carries it for anyone reading
        the step. Composed at runtime from whichever branch above applied,
        so neither surface can hold a stale copy of the other branch.
        """
        if self.PANEL_HELP:
            said = (said + ' ' + self.tr(self.PANEL_HELP)).strip()
        for name in ('_evidenceBox', '_checkBox'):
            box = getattr(self, name, None)
            if box is not None:
                box.setToolTip(said)
                box.setAccessibleDescription(said)
        described = getattr(self, '_description', None)
        help_control = getattr(self, '_help', None)
        # `build_sections` runs from inside the base page's constructor, so
        # the first call arrives before the rest of the page exists and the
        # help control has nothing to read yet. The spacer index is the last
        # thing the constructor sets after the sections are built; until it
        # is there, only the panel is painted, and the first `refresh_status`
        # a moment later brings the help along.
        if (described is None or help_control is None
                or getattr(self, '_bodySpacerIndex', -1) < 0):
            return
        text = described.text().strip()
        # The branch can change under the page -- a promoted threshold moves
        # a gate from report-only to enforcing -- so the sentence that was
        # appended last time comes out before this one goes in. Otherwise the
        # help would end up holding both readings of the same gate.
        previous = getattr(self, '_gateSaid', '')
        if previous and previous != said:
            text = ' '.join(text.replace(previous, '').split())
        if said and said not in text:
            text = (text + ' ' + said).strip()
        self._gateSaid = said
        if text != described.text().strip():
            described.setText(text)
        prerequisites = getattr(self, '_prerequisites', None)
        help_control.setDetail(
            text, prerequisites.text() if prerequisites is not None else '')

    #: R190. Whether a row re-size is already queued for the next turn of
    #: the event loop.
    _rowResizePending = False

    def scheduleRowResize(self) -> None:
        """Size the rows again once the layout has finished arguing with itself.

        R190. Sizing rows to their contents changes the table height, which can
        bring the vertical scrollbar in, which narrows the viewport, which
        narrows the stretch column -- by which point the rows are too short for
        the text again and the delegate elides the tail. Doing it from the
        event loop instead of inside the resize lets each pass settle, and the
        pass that changes nothing ends the chain.
        """
        if self._rowResizePending:
            return
        self._rowResizePending = True
        QTimer.singleShot(0, self._applyRowResize)

    def _applyRowResize(self) -> None:
        self._rowResizePending = False
        table = getattr(self, '_evidenceTable', None)
        if table is not None:
            table.resizeRowsToContents()

    def refreshEvidence(self) -> None:
        table = getattr(self, '_evidenceTable', None)
        if table is None:
            return
        readout = self.evidence_document()
        self.refreshGateNote()
        rows = list(readout.get('rows') or ())
        verdict = str(readout.get('headline') or '')
        self._evidenceHeadline.setText(
            verdict or self.tr('No report has been read for this task.'))
        # W-O1. The verdict when there is one; nothing when there is not.
        # The absent case is the state of the case and the outline row is
        # already drawing it, two columns to the left of this one.
        self._evidenceHeadline.setVisible(bool(verdict))
        caveat = str(readout.get('caveat') or '')
        self._evidenceCaveat.setText(caveat)
        self._evidenceCaveat.setVisible(bool(caveat))
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate((row.get('name'), row.get('verdict'),
                                            row.get('detail'))):
                item = QTableWidgetItem(str(value or ''))
                # R190. A measured reading that is cut off mid-sentence is
                # worse than no reading, because it looks complete. The cell
                # carries its own full text so the number is reachable even
                # when the column is too narrow to lay all of it out.
                item.setToolTip(str(value or ''))
                table.setItem(index, column, item)
        table.resizeRowsToContents()
        self.scheduleRowResize()
        # An empty table with headers reads as "checked, nothing found"; the
        # headline already says the report is absent, so the grid goes away.
        table.setVisible(bool(rows))
        # R187. And with the grid gone the box is one sentence, which has no
        # use for the page's spare height and should not be stretched into a
        # screenful of empty panel around it.
        box = getattr(self, '_evidenceBox', None)
        if box is not None:
            self.claimBodyStretch(box, bool(rows))

    # -- prerequisites ----------------------------------------------------- #

    def refreshPrerequisites(self) -> None:
        """Name the prerequisites that are actually outstanding.

        R30. The line read "Requires: Snap, Reference Readiness" beside
        "Locked - complete the prerequisite tasks first" while Snap already
        carried a tick and Reference Readiness -- configured, with a "Set for
        this project" confirmation on it -- was the one thing missing. Listing
        every dependency says where to look only if the user already knows
        which one is unfinished.
        """
        label = getattr(self, '_prerequisites', None)
        if label is None:
            return
        titles = self._task_titles()
        try:
            page = self.page_model()
        except Exception:                                    # noqa: BLE001
            return
        # DP-144. On snappy this page depended on Boundary Layers, which is
        # optional and which the run skips, so a mesh made without layers was
        # told its Geometry Fidelity was waiting on them.
        depends = tuple(page.get('requires')
                        or (self._task or {}).get('depends_on') or ())
        if not depends:
            return
        states = (page.get('state') or {}).get('tasks') or {}
        outstanding = [item for item in depends
                       if str(states.get(item) or 'ready') not in _SETTLED]
        if not outstanding:
            label.setText(self.tr('Every prerequisite is settled: ')
                          + ', '.join(titles.get(item, item)
                                      for item in depends))
            return
        label.setText(
            self.tr('Waiting on: ')
            + ', '.join(f'{titles.get(item, item)} '
                        f'({states.get(item) or "ready"})'
                        for item in outstanding))

    def refresh_status(self) -> None:
        super().refresh_status()
        self.refreshEvidence()
        self.refreshPrerequisites()


class ReferenceReadinessPage(QualificationTaskPage):
    """GF0. Is there something to measure against?

    Not a verdict about the mesh — it runs before one exists. It answers
    whether a validation reference and feature manifest were built for the
    prepared geometry, which is why §8.6 does not let it be waived: waiving
    "we have no reference" would waive the ability to measure anything at all.

    It is also where the project's fidelity tolerance is set (Plan 28 WP6).
    That belongs here for the same reason the rest of the page does: a
    tolerance is the other half of "something to measure against", and until
    one is chosen every fidelity and resolution section reports `unrated` and
    the summary cannot leave report-only.
    """

    task_id_default = 'common.reference_readiness'
    #: GF0 runs before a mesh exists, so there is no report to project. The
    #: tolerance panel below is what this page has to show.
    has_evidence = False

    def build_sections(self, layout) -> None:
        layout.addWidget(self._buildGateBox())
        layout.addWidget(self._buildToleranceBox())
        self.refreshTolerance()

    # -- the confirmation this task is ------------------------------------- #

    def _buildGateBox(self) -> QGroupBox:
        """The control that settles the task, and the sentence that says so.

        R88. Nothing on this page runs and nothing on it is an engineering
        setting, so a row left at the not-started circle is indistinguishable
        from one that has already passed -- and a user walking the outline
        walks straight past it. The refusal then arrives two tasks later, on
        Snap Fidelity, naming a row they have no reason to think they missed.
        The page now carries the press that settles it and says what stays
        locked until it happens.
        """
        box = QGroupBox(self.tr('Confirmation'), self)
        inner = QVBoxLayout(box)
        self._gateState = QLabel(self)
        self._gateState.setObjectName('readinessGateState')
        self._gateState.setWordWrap(True)
        self._gateState.setAccessibleName(
            self.tr('Whether this readiness task has been confirmed'))
        # W-O1. Carried, not drawn. Half of this sentence explains what the
        # press does, which belongs on the press; the other half reports
        # whether it has happened, which the outline row already shows.
        # `refreshReadinessGate` puts the whole of it on the box and on the
        # button, and the label stays off the column.
        self._gateState.setVisible(False)
        inner.addWidget(self._gateState)

        self._confirm = QPushButton(self.tr('Confirm readiness'), self)
        self._confirm.setObjectName('readinessConfirm')
        # R200. This read "Record that a validation reference exists" --
        # and pressing it records nothing of the kind. The page never opens
        # the stored reference, so on the live tee case it reported
        # "Confirmed" over a reference whose own file said
        # `rated: false, sources: []`, and the consequence arrived two tasks
        # later as "0 of 4 sections were measured". The button is an
        # engineer's attestation, not a check; it now says so, and the
        # sentence beside it says what would prove it.
        self._confirm.setAccessibleDescription(
            self.tr('Record your confirmation that this geometry has a '
                    'reference to measure against. This is an attestation, '
                    'not a check: it unlocks the tasks waiting on it, and '
                    'they are what actually measure.'))
        fit_to_text(self._confirm)
        row = FlowLayout()
        row.addWidget(self._confirm)
        inner.addLayout(row)
        self._confirm.clicked.connect(self.confirmReadiness)
        self._gateBox = box
        return box

    def dependentTaskTitles(self) -> tuple:
        """The tasks this one unlocks, under the names the outline shows.

        Read from the workflow rather than listed here: snappy's dependant is
        Snap Fidelity and Gmsh's is Native Mesh Fidelity, so a hard-coded pair
        would name the wrong row on one of the two engines.
        """
        try:
            return tuple(self.page_model().get('dependents') or ())
        except Exception:                                   # noqa: BLE001
            return ()

    def refreshReadinessGate(self) -> None:
        """Say whether the confirmation has been given, and what waits on it.

        W-O1. On the press and on the box around it rather than in a
        paragraph above them. The sentence names the button by its label and
        names the rows that stay locked, so the control it is about is the
        one place it can be read without standing between the reader and
        anything.
        """
        note = getattr(self, '_gateState', None)
        if note is None:
            return
        settled = self.task_state()[0] in _SETTLED
        self._confirm.setEnabled(not settled)
        if settled:
            note.setText(self.tr(
                'Confirmed. The tasks that wait on this one are unlocked. '
                'This records your confirmation; it does not verify the '
                'reference. If a fidelity task reports that no section was '
                'measured, the reference was not built — set a tolerance '
                'below and prepare the geometry again.'))
            self.showReadinessGate(note.text())
            return
        waiting = self.dependentTaskTitles()
        text = self.tr(
            'Not confirmed yet. Nothing on this page runs and nothing on it '
            'is an engineering setting, so this task stays at the not-started '
            'circle until you press Confirm readiness.')
        if waiting:
            text += ' ' + self.tr('Until you do, {0} stays locked.').format(
                ', '.join(waiting))
        note.setText(text)
        self.showReadinessGate(text)

    def showReadinessGate(self, said: str) -> None:
        """Put the readiness sentence where the press is.

        The button is disabled once the task is settled, and a disabled
        control is not reliably asked for its tooltip, so the box carries the
        sentence as well. Composed at runtime because it names the rows this
        engine's workflow actually unlocks.
        """
        button = getattr(self, '_confirm', None)
        if button is not None:
            button.setToolTip(said)
        box = getattr(self, '_gateBox', None)
        if box is not None:
            box.setToolTip(said)
            box.setAccessibleDescription(said)

    def confirmReadiness(self) -> None:
        """Settle the task. The branch turns this into the accept transition."""
        self.updateRequested.emit(self.task_id)
        self.refresh_status()

    # -- tolerance panel --------------------------------------------------- #

    def _buildToleranceBox(self) -> QGroupBox:
        box = QGroupBox(self.tr('Fidelity tolerance'), self)
        inner = QVBoxLayout(box)
        # W-O1. This was a wrapped paragraph standing above the field it
        # describes. It explains one setting, so it is that setting's
        # tooltip; `refreshTolerance` adds the state of the store to it.

        # B8/B9. Field and two buttons on one line left none of them room:
        # the placeholder read `met...` and the button `Set toleranc`. The
        # entry keeps its own line, and the buttons wrap onto the next one
        # rather than squeezing each other.
        row = QHBoxLayout()
        self._tolerance = QLineEdit(self)
        self._tolerance.setObjectName('qualificationTolerance')
        self._tolerance.setAccessibleName(
            self.tr('Project fidelity tolerance in metres'))
        # DP-164. This one row named its unit three times and spelled it
        # three ways: `Tolerance (m)` on the label, `metres` as the
        # placeholder inside the box, `in metres` in the accessible name.
        # The unit is said once, beside the box, as `m`.
        self._tolerance.setPlaceholderText(self.tr('0.001'))
        self._tolerance.setMinimumWidth(
            self._tolerance.fontMetrics().horizontalAdvance(
                '0.0000001') + 32)
        self._suggest = QPushButton(self.tr('Use suggestion'), self)
        self._suggest.setObjectName('qualificationToleranceSuggest')
        self._apply = QPushButton(self.tr('Set tolerance'), self)
        self._apply.setObjectName('qualificationToleranceApply')
        name = QLabel(self.tr('Tolerance'), self)
        # W-O1. Hand-built rows are laid out by hand, so nothing had told Qt
        # which control this word names. It is a field label and now says so,
        # to a screen reader and to the census alike.
        name.setBuddy(self._tolerance)
        row.addWidget(name)
        row.addWidget(self._tolerance, 1)
        row.addWidget(UnitLabel('m'))
        inner.addLayout(row)

        buttons = FlowLayout()
        for button in (self._suggest, self._apply):
            fit_to_text(button)
            buttons.addWidget(button)
        inner.addLayout(buttons)

        self._toleranceNote = QLabel(self)
        self._toleranceNote.setObjectName('qualificationToleranceNote')
        self._toleranceNote.setWordWrap(True)
        # W-O1. Plan 33 section 1 keeps a specific validation error beside
        # the input it concerns, and takes the routine readout out. This
        # label is the refusal now: `showToleranceNote` shows it when the
        # entry was rejected or the store refused, and keeps it off the form
        # for "set" and "not set", which are the state of the case.
        self._toleranceNote.setVisible(False)
        inner.addWidget(self._toleranceNote)

        self._suggest.clicked.connect(self.useSuggestedTolerance)
        self._apply.clicked.connect(self.applyTolerance)
        return box

    def toleranceState(self) -> dict:
        """What the project stores, and what it would be offered instead."""
        try:
            return dict(query(self._client, 'quality.tolerance.get').payload)
        except Exception:                                   # noqa: BLE001
            return {}

    def refreshTolerance(self, *, keepEntry: bool = False) -> None:
        state = self.toleranceState()
        stored = state.get('tolerance_m')
        suggested = float(state.get('suggested_m') or 0.0)
        self._suggest.setEnabled(suggested > 0)
        # ``keepEntry`` is for the periodic re-read added for R14: a number
        # the user is part-way through typing is not the store's to overwrite.
        if keepEntry and self._tolerance.text().strip():
            pass
        elif stored:
            self._tolerance.setText(f'{float(stored):.6g}')
        else:
            self._tolerance.clear()
        if suggested > 0:
            diagonal = float(state.get('diagonal_m') or 0.0)
            offer = self.tr(
                'Suggested {0:.6g} m, a thousandth of the {1:.6g} m prepared '
                'bounding-box diagonal.').format(suggested, diagonal)
        else:
            offer = self.tr(
                'No geometry is prepared yet, so there is nothing to base a '
                'suggestion on.')
        if stored:
            said = self.tr('Set for this project. ') + offer
        else:
            said = (self.tr('Not set, so fidelity and resolution stay '
                            'unrated. ') + offer)
        self._toleranceNote.setText(said)
        # W-O1. Not on the form. The field it is about carries what the
        # setting is and where the store stands; the suggestion button
        # carries the offer, because pressing it is what takes the offer up.
        self._toleranceNote.setVisible(False)
        self._tolerance.setToolTip(self.tr(TOLERANCE_HELP) + ' ' + said)
        self._suggest.setToolTip(offer)

    def showToleranceNote(self, said: str) -> None:
        """Draw a refusal beside the entry that earned it.

        W-O1. The only thing this label says on the form now: a number that
        was not a number, or a store that would not take one. Both are the
        specific validation errors Plan 33 section 1 keeps beside the input,
        and both go away on the next successful pass through
        `refreshTolerance`.
        """
        self._toleranceNote.setText(said)
        self._toleranceNote.setVisible(True)

    def useSuggestedTolerance(self) -> None:
        suggested = float(self.toleranceState().get('suggested_m') or 0.0)
        if suggested > 0:
            self._tolerance.setText(f'{suggested:.6g}')

    def applyTolerance(self) -> None:
        """Record what is in the box. Empty clears it, which is a real choice."""
        text = self._tolerance.text().strip()
        try:
            value = 0.0 if not text else float(text)
        except ValueError:
            self.showToleranceNote(self.tr('That is not a number of metres.'))
            return

        # C31-12. Scheduled rather than blocking. Everything the press used to
        # do after the write is `stored`, in the same order: re-read the
        # panel, then emit the lifecycle signal, then refresh the status.
        def stored(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                self.showToleranceNote(
                    str(getattr(result, 'message', '')
                        or self.tr('The tolerance was not stored.')))
                return
            self.refreshTolerance()
            # R134. MEASURED: 0.0005 typed in and **Set tolerance** pressed
            # turned the note under the field into 'Set for this project.'
            # while the status line at the top of the same page still read
            # 'Ready to configure.' and the outline row stayed at the not-run
            # circle, so two lines of one page disagreed and the outline sided
            # with the wrong one. The press stored a real project setting and
            # told nothing but its own label about it, because the page
            # emitted no lifecycle signal: only the base page's Update did,
            # and this panel is not a schema field it knows about. A stored
            # tolerance is precisely CONFIGURED, so that is what gets
            # recorded. Clearing it is not, and neither is re-setting one on a
            # task already accepted: demoting a settled row would invalidate
            # everything downstream of it for a value that has not changed
            # what the row asserts.
            if value and self.task_state()[0] not in _SETTLED:
                self.configureRequested.emit(self.task_id)
            self.refresh_status()

        submit(self._client, 'quality.tolerance.set', {'tolerance_m': value},
               then=stored)

    # -- live state -------------------------------------------------------- #

    def refresh_status(self) -> None:
        """Re-read the tolerance panel too, whenever the graph moves.

        R14. The Repair page recorded `as is` and said in as many words 'The
        prepared geometry matches what is loaded, so meshing can proceed.',
        and the very next task still read 'No geometry is prepared yet, so
        there is nothing to base a suggestion on' with **Use suggestion**
        greyed out. Nothing was wrong with the suggestion or with the as-is
        decision: ``refreshTolerance`` ran exactly once, from
        ``build_sections``, and the branch builds every task page the moment a
        meshing method is chosen -- before any geometry has been prepared. The
        panel never asked the facade again for the life of the page, so a
        preparation decision taken afterwards could not reach it.
        """
        super().refresh_status()
        if getattr(self, '_tolerance', None) is not None:
            self.refreshTolerance(keepEntry=True)
        self.refreshReadinessGate()


class SnapFidelityPage(QualificationTaskPage):
    """GF1 on snappy — the blocking gate, and the only one.

    It measures the snapped boundary *before* layer addition mutates it in
    place. After layers the surface that was snapped no longer exists to
    measure, so this is the last moment the question can be asked at all.
    """

    task_id_default = 'snappy.fidelity_snap'
    blocking = True
    evidence_title = 'Snapped boundary against the reference'


class NativeFidelityPage(QualificationTaskPage):
    """GF1 on Gmsh — diagnostic, and deliberately not a gate.

    Gmsh meshes the CAD volume directly, so a boundary face is a face of that
    volume and cannot be dropped the way snappy can drop one. The same
    measurement therefore carries no equivalent risk, and blocking on it would
    stop the engine for a reading that never indicates the failure the gate
    exists to catch.
    """

    task_id_default = 'gmsh.fidelity_native'
    evidence_title = 'Native boundary against the reference'


class GeometryFidelityPage(QualificationTaskPage):
    """GF2. Fidelity of the published mesh, on both engines."""

    task_id_default = 'common.fidelity'
    evidence_title = 'Published mesh against the reference'


class ResolutionAdequacyPage(QualificationTaskPage):
    """RA. Whether the mesh resolves the geometry it did capture.

    Independent of fidelity on purpose: a mesh can sit exactly on the surface
    and still put two cells across a channel that needs ten. Fidelity would
    pass it.
    """

    task_id_default = 'common.resolution'
    evidence_title = 'Cells across the geometry'


class QualificationSummaryPage(QualificationTaskPage):
    """Q. The three verdicts, and the one disposition they compose to.

    The only page where `qualified` appears, and it is never green on its own
    account — it reports what the blocks earned. A waiver shows here as
    `waived`, never as a pass, because §8.6's whole point is that the two
    remain distinguishable to whoever reads the case next.

    DP-245. Plan 32 §4.4 asks for the automatic quality substeps to be read
    together. `common.fidelity`, `common.resolution` and `common.summary` are
    three rows of the outline, three pages and three separate visits, and
    nothing on any of them is an engineering setting: the only thing a reader
    does there is look. MEASURED before this change, this class was
    `task_id_default` and `evidence_title` and nothing else, so the composed
    disposition sat beside no fidelity number and no resolution number, and a
    reader who wanted the three had to walk the outline. Every check, every
    page and every gate stays where it was; this is a second reading of
    reports the facade already wrote, and it decides nothing.

    It is also where the layer question is answered (Plan 32 check 5). The
    achieved counts have been parsed, persisted and served for two plans --
    `core/quality/layer_report.py`, `foammesh/quality/layer-coverage.json`,
    `mesh.layer_coverage` -- and MEASURED before this change the only
    consumers in the whole view were a viewport colouring mode. Asking for
    three layers and being handed a coloured mesh is not an answer.
    """

    task_id_default = 'common.summary'
    evidence_title = 'Composed disposition'
    #: W-O1. The paragraph that used to stand above the three panels, said
    #: where a reader asks for it instead of before they have asked.
    PANEL_HELP = (
        'Every automatic check of this mesh, read together. Each one is '
        'still its own task with its own page and its own gate; this is '
        'where they can be compared.')
    #: The three reports below include this task's own, so the base
    #: single-box evidence panel would render the summary twice on one page.
    has_evidence = False

    #: The automatic checks Plan 32 §4.4 names, in the order the pipeline
    #: produces them. Fixed here rather than derived from the workflow: these
    #: three are the ones the plan consolidates, and a page that guessed from
    #: the graph would quietly pick up or drop one.
    CONSOLIDATED_TASKS = ('common.fidelity', 'common.resolution',
                          'common.summary')

    #: One check's detail grid, with the same three columns the per-task
    #: pages use, so a row means the same thing in both places.
    _DETAIL_COLUMNS = ('Section', 'Verdict', 'Measured')

    #: The layer grid. Two of these columns carry numbers of different kinds
    #: -- one is a length, one is a share of a request -- and each names its
    #: own unit rather than leaving the heading to carry it.
    _LAYER_COLUMNS = ('Patch', 'Faces', 'Requested', 'Achieved',
                      'Overall thickness', 'Of requested thickness',
                      'Verdict')

    # -- construction ------------------------------------------------------ #

    def build_sections(self, layout) -> None:
        self._gateNote = QLabel(self)
        self._gateNote.setObjectName('qualificationGateNote')
        self._gateNote.setWordWrap(True)
        # W-O1. As on every other qualification page: carried, and drawn on
        # the panel it is about. See `QualificationTaskPage.showGateNote`.
        self._gateNote.setVisible(False)
        layout.addWidget(self._gateNote)

        box = QGroupBox(self.tr('Automatic checks'), self)
        inner = QVBoxLayout(box)
        self._checkPanels = {
            task_id: self._buildCheckPanel(inner, task_id)
            for task_id in self.CONSOLIDATED_TASKS}
        layout.addWidget(box)
        self._checkBox = box
        # W-O1. The paragraph that used to head this box said what the box
        # is, which is what a title and a help entry are for. The title is
        # above it and `PANEL_HELP` reaches the step help through the same
        # route the gate sentence takes.
        self.refreshGateNote()
        self.claimBodyStretch(box)

        layout.addWidget(self._buildLayerBox())

    def _buildCheckPanel(self, inner, task_id: str) -> dict:
        """One check: its verdict, its caveat, and its rows behind a folder."""
        name = task_id.rsplit('.', 1)[-1]
        headline = QLabel(self)
        headline.setObjectName('qualificationHeadline_' + name)
        headline.setWordWrap(True)
        headline.setAccessibleName(
            self.tr('Verdict recorded for this check'))
        # W-O1. Shown once the check has a verdict. Before that the folder
        # below says "nothing measured to show" in the row's own name, which
        # is the same fact without a paragraph of it.
        headline.setVisible(False)
        inner.addWidget(headline)

        caveat = QLabel(self)
        caveat.setObjectName('qualificationCaveat_' + name)
        caveat.setWordWrap(True)
        # Same rendering as the per-task page: this is the sentence that says
        # a verdict does not mean what it looks like.
        caveat.setProperty('foammeshStatus', 'warning')
        caveat.setVisible(False)
        inner.addWidget(caveat)

        folder = FolderHeader(self.tr('Details'), self)
        folder.setObjectName('qualificationDetails_' + name)
        # Not a settings fold: what is behind it is the measured evidence
        # table, which section 1.1 puts in an on-demand view. A fold opens
        # open where it holds editors (W-O2, `FolderHeader`); this one holds
        # none, and the evidence is the reason for the press.
        folder.setChecked(False)
        inner.addWidget(folder)

        table = QTableWidget(0, len(self._DETAIL_COLUMNS), self)
        table.setObjectName('qualificationEvidence_' + name)
        table.setHorizontalHeaderLabels(
            tuple(self.tr(column) for column in self._DETAIL_COLUMNS))
        table.setAccessibleName(self.tr('Measured evidence for this check'))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setWordWrap(True)
        header = table.horizontalHeader()
        for column in (0, 1):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        inner.addWidget(table)
        # The disclosure owns the grid's visibility from here on: details on
        # request (§4.4). A refresh fills the rows and never opens the folder
        # over the reader.
        folder.setContents(table)
        return {'headline': headline, 'caveat': caveat, 'folder': folder,
                'table': table}

    def _buildLayerBox(self) -> QGroupBox:
        box = QGroupBox(self.tr('Boundary layers'), self)
        inner = QVBoxLayout(box)
        self._layerNote = QLabel(self)
        self._layerNote.setObjectName('qualificationLayerNote')
        self._layerNote.setWordWrap(True)
        inner.addWidget(self._layerNote)

        self._layerTable = QTableWidget(0, len(self._LAYER_COLUMNS), self)
        self._layerTable.setObjectName('qualificationLayers')
        self._layerTable.setHorizontalHeaderLabels(
            tuple(self.tr(column) for column in self._LAYER_COLUMNS))
        self._layerTable.setAccessibleName(
            self.tr('Layers requested against layers achieved, per patch'))
        self._layerTable.setEditTriggers(QTableWidget.NoEditTriggers)
        self._layerTable.verticalHeader().setVisible(False)
        self._layerTable.setWordWrap(True)
        header = self._layerTable.horizontalHeader()
        last = len(self._LAYER_COLUMNS) - 1
        for column in range(last):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(last, QHeaderView.ResizeMode.Stretch)
        inner.addWidget(self._layerTable)
        self._layerBox = box
        return box

    # -- the three checks, read together ----------------------------------- #

    def checkReadouts(self) -> dict:
        """The stored report for each consolidated check, by task id.

        One read per check, through the same operation each check's own page
        uses, so the panel and the page cannot disagree about one report. A
        read that fails is an empty mapping: the caller renders that as "not
        run", which is what an unreadable report means for the question.
        """
        stored = {}
        for task_id in self.CONSOLIDATED_TASKS:
            try:
                payload = query(self._client, 'quality.evidence.read',
                                {'task_id': task_id}).payload or {}
            except Exception:                                # noqa: BLE001
                payload = {}
            stored[task_id] = dict(payload.get('readout') or {})
        return stored

    def refreshChecks(self) -> None:
        """Paint the three verdicts side by side, details left shut.

        The panel is a reader and nothing else: it emits no signal, sends no
        command and settles nothing. Where a non-pass stops the pipeline is
        the facade's rule, and it stays there.
        """
        panels = getattr(self, '_checkPanels', None)
        if not panels:
            return
        titles = self._task_titles()
        stored = self.checkReadouts()
        for task_id, panel in panels.items():
            title = str(titles.get(task_id) or task_id)
            readout = stored.get(task_id) or {}
            reported = bool(readout)
            rows = tuple(readout.get('rows') or ())
            if not reported:
                # Not a pass. A check with no report has measured nothing,
                # and silence in this row would read as agreement.
                readout = absent(task_id.rsplit('.', 1)[-1], title).to_dict()
                rows = ()
            panel['headline'].setText(str(readout.get('headline') or ''))
            # W-O1. The verdict when one has been recorded. The composed
            # "not run" sentence goes on the folder header below, which is a
            # control and names its own row.
            panel['headline'].setVisible(reported)
            panel['headline'].setToolTip(str(readout.get('headline') or ''))
            caveat = str(readout.get('caveat') or '')
            panel['caveat'].setText(caveat)
            panel['caveat'].setVisible(bool(caveat))
            folder = panel['folder']
            # DP-573 (0924 rerun2). A fold header is a check box and cannot
            # wrap, and "Qualification summary -- nothing measured to show"
            # asked 329 px in the 326 px the compact settings column leaves
            # it, so its last word was cut. The whole sentence is the
            # tooltip below; the header only has to say which row is empty.
            folder.setText(
                self.tr('{0} — details').format(title) if rows
                else self.tr('{0} — not measured').format(title))
            folder.setEnabled(bool(rows))
            # W-O1. Where the absent sentence lands: on the row it is about,
            # for a reader who wants the whole of it.
            folder.setToolTip(str(readout.get('headline') or ''))
            table = panel['table']
            table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                for column, value in enumerate(
                        (row.get('name'), row.get('verdict'),
                         row.get('detail'))):
                    item = QTableWidgetItem(str(value or ''))
                    item.setToolTip(str(value or ''))
                    table.setItem(index, column, item)
            table.resizeRowsToContents()

    # -- layers ------------------------------------------------------------ #

    def layerCoverage(self) -> dict:
        """What the run recorded per patch, out of the run artifact."""
        try:
            payload = query(self._client, 'mesh.layer_coverage').payload or {}
        except Exception:                                    # noqa: BLE001
            return {}
        return dict(payload)

    def refreshLayers(self) -> None:
        """Requested against achieved, per patch, with both units named.

        The projection is :func:`core.quality.layer_report.coverage_rows`,
        which the HTML mesh report reads as well. Two surfaces describing one
        run in two ways is the shape of defect this page exists to close, so
        neither of them keeps a second opinion about what the numbers mean.
        """
        table = getattr(self, '_layerTable', None)
        note = getattr(self, '_layerNote', None)
        if table is None or note is None:
            return
        document = self.layerCoverage()
        rows = coverage_rows(document)
        if not rows:
            table.setRowCount(0)
            table.setVisible(False)
            note.setText(self.tr(
                'This run recorded no per-patch layer measurement, so there '
                'is nothing to hold a request against. Either no boundary '
                'layers were asked for, or the run that produced this mesh '
                'wrote no layer coverage.'))
            # W-O1. Off the form. There is no measurement to annotate, so
            # this is the state of the case; it goes on the box, which still
            # names itself Boundary layers, and the empty grid beside it.
            note.setVisible(False)
            box = getattr(self, '_layerBox', None)
            if box is not None:
                box.setToolTip(note.text())
                box.setAccessibleDescription(note.text())
            return
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (row['patch'], str(row['faces']), row['requested_text'],
                      row['achieved_text'], row['thickness_text'],
                      row['coverage_text'], row['verdict'])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(index, column, item)
        table.resizeRowsToContents()
        table.setVisible(True)
        warnings = [str(line) for line in (document.get('warnings') or ())]
        measured = self.tr(
            'Read from the layer coverage this run wrote. Overall thickness '
            'is a length in metres; the share beside it is a percentage of '
            'the thickness that was asked for.')
        # W-O1. The sentence about where the numbers come from and what
        # their units are describes the grid, so it goes on the grid's box.
        # What is left on the form is what the run itself recorded -- a
        # warning is a measured finding, and Plan 33 keeps those.
        box = getattr(self, '_layerBox', None)
        if box is not None:
            box.setToolTip(measured)
            box.setAccessibleDescription(measured)
        note.setText(' '.join(warnings) if warnings else measured)
        note.setVisible(bool(warnings))

    def refresh_status(self) -> None:
        super().refresh_status()
        self.refreshChecks()
        self.refreshLayers()


#: The `common.*` pages, shared by every engine.
COMMON_QUALIFICATION_PAGES = {
    'common.reference_readiness': ReferenceReadinessPage,
    'common.fidelity': GeometryFidelityPage,
    'common.resolution': ResolutionAdequacyPage,
    'common.summary': QualificationSummaryPage,
}

__all__ = [
    'COMMON_QUALIFICATION_PAGES',
    'GeometryFidelityPage',
    'NativeFidelityPage',
    'QualificationSummaryPage',
    'QualificationTaskPage',
    'ReferenceReadinessPage',
    'ResolutionAdequacyPage',
    'SnapFidelityPage',
]
