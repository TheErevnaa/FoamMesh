"""Gmsh workflow page: gmsh.qa."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.engine.base import SU2_QA_OPERATION, qa_operation
from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.checkmesh_findings import (
    CheckMeshFindings,
)

from .base import GmshTaskPage


class Su2ReadinessFindings(QGroupBox):
    """What the last SU2 readiness check on this case reported.

    Plan 33 QA-05. The SU2 route publishes no ``constant/polyMesh``, so the
    checkMesh panel beside it stayed empty forever and was hidden -- which
    left the route that has the least evidence on screen showing none of it,
    under a paragraph about a check that does not run. The readiness check had
    measured the cells, the boundary markers and the metrics SU2 is sensitive
    to the whole time.

    The projection is :func:`core.quality.su2_readiness.su2_readiness_readout`
    and the report is the one ``quality.report`` hands every other surface, so
    this panel cannot disagree with the strip or the export about what the
    check found.
    """

    def __init__(self, facade_client, parent=None):
        super().__init__('SU2 readiness findings', parent)
        self._client = facade_client
        inner = QVBoxLayout(self)
        self._headline = QLabel(self)
        self._headline.setObjectName('su2ReadinessHeadline')
        self._headline.setWordWrap(True)
        self._headline.setAccessibleName(
            self.tr('Verdict from the last readiness check'))
        inner.addWidget(self._headline)
        self._caveat = QLabel(self)
        self._caveat.setObjectName('su2ReadinessCaveat')
        self._caveat.setWordWrap(True)
        self._caveat.setProperty('foammeshStatus', 'warning')
        self._caveat.setVisible(False)
        inner.addWidget(self._caveat)
        self._table = QTableWidget(0, 3, self)
        self._table.setObjectName('su2ReadinessFindings')
        self._table.setHorizontalHeaderLabels(
            (self.tr('Check'), self.tr('Verdict'), self.tr('Detail')))
        self._table.setAccessibleName(
            self.tr('SU2 readiness findings and measurements'))
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(True)
        header = self._table.horizontalHeader()
        for column in (0, 1):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        inner.addWidget(self._table)
        self.refresh()

    def report(self) -> dict:
        try:
            payload = query(self._client, 'quality.report').payload or {}
        except Exception:                                    # noqa: BLE001
            return {}
        return dict(payload.get('report') or {})

    def refresh(self) -> None:
        from foammesh.core.quality.su2_readiness import su2_readiness_readout

        report = self.report()
        readout = su2_readiness_readout(report)
        self._headline.setText(readout.headline)
        # W-O1. Same rule as the checkMesh panel beside it: the verdict is a
        # measurement and stays, and the sentence that says no check has run
        # is said by the panel rather than by a label standing in the column.
        self._headline.setVisible(bool(report))
        self.setToolTip(readout.headline)
        self.setAccessibleDescription(readout.headline)
        self._caveat.setText(readout.caveat)
        self._caveat.setVisible(bool(readout.caveat))
        self._table.setRowCount(len(readout.rows))
        for index, row in enumerate(readout.rows):
            for column, value in enumerate(
                    (row.name, row.verdict, row.detail)):
                self._table.setItem(index, column,
                                    QTableWidgetItem(str(value)))
        self._table.resizeRowsToContents()


class GmshQaPage(GmshTaskPage):
    """The check that qualifies this mesh, and what it found.

    R208. The page carried the run button and nothing else: "Accepted." above
    an empty rectangle, on a case whose checkMesh report held four metrics and
    a failing sicn count. Same run, same report file and same panel as the
    snappy QA page -- there is no reason for the two engines to tell the user
    different amounts about the same check.

    Plan 31 CP-09 item 3. Which check that is depends on the target solver,
    and this page said "checkMesh" either way. On an SU2 case checkMesh never
    runs -- ``core.engine.base.qa_operation``, the seam the menu, the QA row,
    the CLI and the facade all route through, answers
    ``quality.su2_readiness`` instead, because SU2 reads the ``.msh`` Gmsh
    wrote and the case publishes no ``constant/polyMesh`` for checkMesh to
    open -- so the page described a mandatory step that does not exist on
    that route, above a findings table that would stay empty forever. The
    page now names the check it will actually run, for the solver it will run
    it for, and shows the checkMesh table only where checkMesh is the check.

    Plan 33 QA-05. That explanation had grown to five sentences and 336
    characters, and it was the only thing on the SU2 route: prose where the
    other route has evidence. Each route now carries the findings panel for
    its own check, and the prose is one status sentence with a ``Details``
    control beside it for the run log.
    """

    task_id_default = 'gmsh.qa'
    SHOWS_EVERY_RUN_WARNING = True
    #: Run-gated (D23): the button runs the qualifying check on this mesh.
    run_stage = 'checkMesh'

    def build_sections(self, layout) -> None:
        # Parentless: the layout reparents it, and CP-09's page tests build
        # `build_sections` on a bare instance whose QWidget base is not up.
        # A container widget rather than a nested layout: `build_sections` is
        # handed the page's own layout, and the pages' tests hand it a stub
        # that takes widgets. One row of the page is one widget either way.
        row = QWidget()
        status = QHBoxLayout(row)
        status.setContentsMargins(0, 0, 0, 0)
        self._qaNote = QLabel()
        self._qaNote.setObjectName('gmshQaTargetNote')
        self._qaNote.setWordWrap(True)
        self._qaNote.setAccessibleName('Which check qualifies this mesh')
        # W-O1. Which check qualifies this mesh is said by the run button,
        # which is named after the check it runs, and by the findings panel
        # the check fills. The label carries the sentence for both; it does
        # not stand on the form saying it a third time.
        self._qaNote.setVisible(False)
        status.addWidget(self._qaNote, 1)
        self._details = QPushButton(self.tr('Details'))
        self._details.setObjectName('gmshQaDetails')
        self._details.setAccessibleDescription(
            'Open the run details for this check.')
        self._details.clicked.connect(self._showDetails)
        status.addWidget(self._details, 0)
        layout.addWidget(row)
        self._findings = CheckMeshFindings(self._client, self)
        layout.addWidget(self._findings)
        # Parentless for the same reason the note above is: the layout adopts
        # it, and the pages' tests call `build_sections` on a bare instance
        # whose QWidget base has not been constructed to be a parent.
        self._su2Findings = Su2ReadinessFindings(self._client)
        layout.addWidget(self._su2Findings)

    def refresh(self) -> None:
        super().refresh()
        self._describeCheck()

    def refresh_status(self) -> None:
        """Re-read the task's state *and* the report that produced it."""
        super().refresh_status()
        for name in ('_findings', '_su2Findings'):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.refresh()

    # -- the run log, wherever the window keeps it -------------------------- #

    def _showDetails(self) -> None:
        """Hand the request to the window, if this window answers it yet.

        ``getattr`` rather than a direct call because the slot arrives with
        W-C and this page has to work either side of it: a page that raised
        here would take the whole Quality page down on a window that has not
        grown the slot, which is a worse answer than a button that does
        nothing once.
        """
        slot = getattr(self.window(), 'showRunDetails', None)
        if callable(slot):
            slot()

    # -- which check, for which solver ------------------------------------- #

    def _describeCheck(self) -> None:
        note = getattr(self, '_qaNote', None)
        if note is None:
            return
        target = self.target_solver()
        # The rule is read off the engine seam, not re-decided here: a page
        # that made its own judgement could describe one check while the
        # button ran the other.
        su2 = qa_operation(target) == SU2_QA_OPERATION
        if su2:
            note.setText(self.tr('The SU2 readiness check qualifies this '
                                 'mesh.'))
            self.setRunStageLabel(self.tr('Run the SU2 readiness check'))
            self.setRunStageAvailable(True)
        else:
            note.setText(self.tr('checkMesh qualifies this mesh for '
                                 '{0}.').format(
                self.target_solver_name() if target != 'unselected'
                else self.tr('OpenFOAM by default')))
            self.setRunStageLabel(self.tr('Run checkMesh'))
            publishes, reason = self.publication_plan()
            self.setRunStageAvailable(publishes, reason + self.tr(
                ' checkMesh reads a polyMesh, so it has nothing to open.'))
        # One panel per route, and only the one whose check can fill it: a
        # visible empty table is the same claim of nothing-to-report that the
        # hidden one made, made louder.
        self._findings.setVisible(not su2)
        self._su2Findings.setVisible(su2)
        for panel in (self._findings, self._su2Findings):
            panel.refresh()
        # W-O1. The sentence lands on the panel this check fills, so the
        # reader who asks what produced the rows is told by the rows. After
        # the refresh above, which writes the panel's own verdict there.
        shown = self._su2Findings if su2 else self._findings
        said = ' '.join((note.text(), shown.toolTip())).strip()
        shown.setToolTip(said)
        shown.setAccessibleDescription(said)
