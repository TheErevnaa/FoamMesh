"""Gmsh's items on the one Quality page.

Plan 42 SG18. The SU2 readiness check and its findings, the quality gate's
further limits and the run's warnings are what the
Gmsh route adds to the Quality page both engines share. The mesh
statistics are not here: they are items of the Mesh quality tab (SG7). The page asks the
registry (:data:`foammesh.view.quality_page.QUALITY_ENGINE_ITEMS`) for them
while the Gmsh engine is active, so a snappy project never sees them.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QGroupBox, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.view.facade_client import query
from foammesh.view.theming.metrics import GAP_TIGHT
from foammesh.view.widgets.folded_note import FoldedNote
from foammesh.view.quality_page import QualityItems

from foammesh.view.workflow_controls.checkmesh_findings import (
    count_findings, fit_table_to_rows, fold_findings_table)
from .folds import add_fold, clip_header, count_fold, fold_rows


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
        inner.setSpacing(GAP_TIGHT)  # Plan 44 CHR2: the fold and verdict sit close
        # Plan 44 CHR2: the verdict is one line until opened (FX2 idiom).
        self._headline = FoldedNote(self)
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
        self._fold = fold_findings_table(
            inner, self._table, self.tr('Readiness checks and measurements'),
            'su2ReadinessFindingsFold')
        self.refresh()

    def report(self) -> dict:
        try:
            payload = query(self._client, 'quality.report',
                            {'check': 'su2-readiness'}).payload or {}
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
        fit_table_to_rows(self._table)
        count_findings(self._fold, self._table, len(readout.rows))


class GmshQualityItems(QualityItems):
    """What the Gmsh route adds to the Quality page."""

    run_stage_label = 'Run the quality check'

    def findings_check(self) -> str:
        # Plan 41 GN16: a panel shows its own check's report whatever the
        # preset is.
        return 'checkmesh'

    def after_init(self) -> None:
        page = self.page
        # Above the page's own content: a warning the run threw away has to
        # be read before the settings it belongs to.
        # Plan 44 FX2. Each run read-out is one line until opened.
        page._runWarnings = FoldedNote(page)
        page._runWarnings.setObjectName('gmshRunWarnings')
        page._runWarnings.setWordWrap(True)
        page._runWarnings.setProperty('foammeshStatus', 'warning')
        page._runWarnings.setAccessibleName(
            page.tr('Warnings from the last Gmsh run'))
        page._runWarnings.setVisible(False)
        page._body_layout.insertWidget(0, page._runWarnings)
        page._runNotes = FoldedNote(page)
        page._runNotes.setObjectName('gmshRunNotes')
        page._runNotes.setWordWrap(True)
        page._runNotes.setProperty('foammeshStatus', 'info')
        page._runNotes.setAccessibleName(
            page.tr('Notes from the last Gmsh run'))
        page._runNotes.setVisible(False)
        page._body_layout.insertWidget(1, page._runNotes)
        # DP-2595. The two rows above moved the trailing spacer down.
        from foammesh.view.gmsh_run_readout import resync_body_spacer
        resync_body_spacer(page)
        page.updateRunWarnings()
        page.mountOptionsTier()

    def mount_beside_checkmesh(self, row, layout) -> None:
        page = self.page
        page._runReadiness = QPushButton(page.tr('Run SU2 readiness'))
        page._runReadiness.setObjectName('gmshQaRunSu2Readiness')
        page._runReadiness.setAccessibleDescription(
            'Run the SU2 readiness check on the mesh Gmsh wrote. Optional: '
            'it needs no OpenFOAM runtime.')
        page._runReadiness.clicked.connect(
            lambda: page._runCheck('quality.su2_readiness'))
        # Plan 44 FITG2 (DP-2594). The readiness check is the optional,
        # secondary run of the row, so it is an MF20 text link beside the
        # Run checkMesh button: same button, name, slot and enabled rule.
        # MEASURED at 435d1ded in the 300 px sheet of a 1280x800 window: as
        # a padded button (139 px) beside Run checkMesh (120 px) it asked
        # 265 px of a 249 px row, the F4 flow layout wrapped it onto a second
        # line and the page ran 38 px past its 491 px scroller.
        from foammesh.view.widgets.link_button import as_link
        as_link(page._runReadiness)
        row.addWidget(page._runReadiness, 0)

    def mount_findings_extras(self, layout) -> None:
        # Parentless: the layout reparents it, and the pages' tests call
        # `build_sections` on a bare instance.
        page = self.page
        page._su2Findings = Su2ReadinessFindings(page._client)
        # Plan 44 GFIT (DP-2668, user decision 2026-10-16). The readiness
        # report is a secondary section whose defaults let the user proceed;
        # it stands behind a closed fold whose header says the verdict. The
        # panel keeps its own rows, folds and controls.
        page._foldSu2Findings = add_fold(
            page, layout, name='Su2Findings',
            title=page.tr('SU2 readiness findings'),
            description=page.tr(
                'What the last SU2 readiness check reported on this mesh.'),
            panel=page._su2Findings)
        clip_header(page._foldSu2Findings)
        page._foldSu2Findings.setVisible(False)
        page._su2Findings.setVisible(False)

    def mount_check(self, layout, group) -> None:
        page = self.page
        if not isinstance(group, QWidget):       # a test's stand-in panel
            layout.addWidget(group)
            return
        # Plan 44 FX2 (G-FITG-1). MEASURED in the real engine branch at
        # 8f45b33f, 300 px sheet of a 1280x800 window, on a meshed case: the
        # result strip and the run's warnings and notes over the page ran Gmsh
        # Quality 68 px past its scroller, and the thresholds panel alone is
        # 281 px of it. The thresholds re-judge a mesh rather than make one,
        # so they are a secondary section: the same panel, every field in
        # it, behind a section fold (one press). The panel's own Advanced
        # header still counts the thresholds off their default once open.
        page._foldCheckThresholds = add_fold(
            page, layout, name='CheckThresholds',
            title=page.tr('checkMesh report thresholds'),
            description=page.tr(
                'Where checkMesh starts reporting a face. Changing these '
                're-judges the mesh you have; it does not change the mesh.'),
            panel=group)

    def mount_below(self, layout) -> None:
        from .quality_limits_panel import QualityLimitsPanel

        page = self.page
        # Parentless, as in `mount_findings_extras`: the layout reparents
        # both, and the pages' tests call `build_sections` on a bare instance.
        title = QLabel(page.tr('Further quality limits'))
        title.setObjectName('gmshQualityLimitsTitle')
        # Plan 44 FIT-G (MF27). MEASURED at b59ac794 in the 300 px sheet of a
        # 1280x800 window: the title and the 222 px table ran the page 235 px
        # past its 491 px scroller (106 px at 1920 with Advanced open). The
        # limits are one press away behind a fold that counts them; the fold
        # header says the title, so the label is built and hidden.
        title.setVisible(False)
        layout.addWidget(title)
        page._qualityLimits = page.adoptPanel(
            QualityLimitsPanel(page._client))
        page._foldQualityLimits = add_fold(
            page, layout, name='QualityLimits',
            title=page.tr('Further quality limits'),
            description=page.tr(
                'Limits on any Gmsh quality measure, beyond the worst cell '
                'quality above.'),
            panel=page._qualityLimits)

    def renders_field(self, field_id: str) -> bool:
        from foammesh.core.gmsh.option_catalogue import OVERRIDES_FIELD

        # The table (QualityLimitsPanel) draws extra_limits and the Options
        # panel draws the overrides; a text box over either would be a
        # second editor.
        return field_id not in (OVERRIDES_FIELD, 'gmsh.compute.extra_limits')

    def checkmesh_availability(self) -> tuple[bool, str]:
        return self.page.publication_plan()

    def set_busy(self, busy: bool) -> None:
        runner = getattr(self.page, '_runReadiness', None)
        if runner is not None:
            runner.setEnabled(not busy)

    def refresh(self) -> None:
        page = self.page
        fold = getattr(page, '_foldQualityLimits', None)
        if fold is not None:
            count_fold(fold, fold_rows(page._qualityLimits))
        su2 = getattr(page, '_su2Findings', None)
        if su2 is not None:
            su2.refresh()
            # The readiness panel appears once that check has a report, behind
            # its fold; the fold's header carries the verdict.
            has_report = bool(su2.report())
            fold = getattr(page, '_foldSu2Findings', None)
            if fold is not None:
                title = str(fold.property('foammeshFoldTitle') or '')
                said = su2._headline.text() if has_report else ''
                fold.setText(f'{title}: {said}' if said else title)
                fold.setToolTip(said or title)
                fold.setVisible(has_report)
                su2.setVisible(has_report and fold.isChecked())
            else:
                su2.setVisible(has_report)
        if hasattr(page, '_runWarnings'):
            page.updateRunWarnings()
