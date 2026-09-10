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

from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query, submit


#: Task states that mean a prerequisite is settled. Same set the task page uses
#: for "still accepted"; a prerequisite in any other state is one the user has
#: to go back to.
_SETTLED = frozenset({'passed', 'warning', 'completed', 'waived', 'skipped'})


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
        self.refreshGateNote()
        inner.addWidget(self._gateNote)

        self._evidenceHeadline = QLabel(self)
        self._evidenceHeadline.setObjectName('qualificationHeadline')
        self._evidenceHeadline.setWordWrap(True)
        self._evidenceHeadline.setAccessibleName(
            self.tr('Verdict recorded for this task'))
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
        self._evidenceHeadline.setText(
            str(readout.get('headline')
                or self.tr('No report has been read for this task.')))
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
        depends = tuple((self._task or {}).get('depends_on') or ())
        if label is None or not depends:
            return
        titles = self._task_titles()
        try:
            page = self.page_model()
        except Exception:                                    # noqa: BLE001
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
        """Say whether the confirmation has been given, and what waits on it."""
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
                'measured, the reference was not built - set a tolerance '
                'below and prepare the geometry again.'))
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

    def confirmReadiness(self) -> None:
        """Settle the task. The branch turns this into the accept transition."""
        self.updateRequested.emit(self.task_id)
        self.refresh_status()

    # -- tolerance panel --------------------------------------------------- #

    def _buildToleranceBox(self) -> QGroupBox:
        box = QGroupBox(self.tr('Fidelity tolerance'), self)
        inner = QVBoxLayout(box)
        note = QLabel(self.tr(
            'How far the mesh may sit from the prepared surface before a '
            'section counts as lost. Nothing is rated until one is set.'),
            self)
        note.setWordWrap(True)
        inner.addWidget(note)

        # B8/B9. Field and two buttons on one line left none of them room:
        # the placeholder read `met...` and the button `Set toleranc`. The
        # entry keeps its own line, and the buttons wrap onto the next one
        # rather than squeezing each other.
        row = QHBoxLayout()
        self._tolerance = QLineEdit(self)
        self._tolerance.setObjectName('qualificationTolerance')
        self._tolerance.setAccessibleName(
            self.tr('Project fidelity tolerance in metres'))
        self._tolerance.setPlaceholderText(self.tr('metres'))
        self._tolerance.setMinimumWidth(
            self._tolerance.fontMetrics().horizontalAdvance(
                self.tr('metres')) + 32)
        self._suggest = QPushButton(self.tr('Use suggestion'), self)
        self._suggest.setObjectName('qualificationToleranceSuggest')
        self._apply = QPushButton(self.tr('Set tolerance'), self)
        self._apply.setObjectName('qualificationToleranceApply')
        row.addWidget(QLabel(self.tr('Tolerance (m)'), self))
        row.addWidget(self._tolerance, 1)
        inner.addLayout(row)

        buttons = FlowLayout()
        for button in (self._suggest, self._apply):
            fit_to_text(button)
            buttons.addWidget(button)
        inner.addLayout(buttons)

        self._toleranceNote = QLabel(self)
        self._toleranceNote.setObjectName('qualificationToleranceNote')
        self._toleranceNote.setWordWrap(True)
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
            self._toleranceNote.setText(
                self.tr('Set for this project. ') + offer)
        else:
            self._toleranceNote.setText(
                self.tr('Not set, so fidelity and resolution stay unrated. ')
                + offer)

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
            self._toleranceNote.setText(
                self.tr('That is not a number of metres.'))
            return

        # C31-12. Scheduled rather than blocking. Everything the press used to
        # do after the write is `stored`, in the same order: re-read the
        # panel, then emit the lifecycle signal, then refresh the status.
        def stored(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                self._toleranceNote.setText(
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
    """

    task_id_default = 'common.summary'
    evidence_title = 'Composed disposition'


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
