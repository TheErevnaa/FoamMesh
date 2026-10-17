"""What the last ``checkMesh`` run reported, as a panel for a QA page.

R208. This panel was written for R100 -- the run finished `warning`, and the
1,451 concave cells behind it appeared on no surface at all -- and it was built
inside the snappy workflow package, where only the snappy QA page could reach
it. MEASURED on tee_gmsh_r2: the Gmsh Quality page read "Accepted." above an
empty grey rectangle, while `foammesh/quality/latest.json` held the whole
checkMesh report and the dock at the bottom of the window was showing it. Both
engines run the same checkMesh on the same published mesh and write the same
file; the page that reports it now lives where both of them can mount it.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QGroupBox, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from foammesh.view.facade_client import query
from foammesh.view.theming.metrics import GAP_TIGHT
from foammesh.view.widgets.folded_note import FoldedNote


def fit_table_to_rows(table, *, max_rows: int = 4) -> None:
    """Draw a findings table as tall as its rows, to *max_rows* of them.

    Plan 44 CHR2 (G-FITS-1 / G-FITG-1). A ``QTableWidget`` asks for the Qt
    default viewport, about 192 px, whatever it holds. MEASURED live at
    1280x800: a one-finding table took 192 px of the 300 px sheet, and two of
    them (checkMesh and the SU2 readiness check) pushed Gmsh Quality 631 px
    past its scroller. Every row is still there (a longer table scrolls
    inside its own box); an empty space is not.
    """
    rows = table.rowCount()
    shown = max(1, min(rows, max_rows))
    height = table.horizontalHeader().height() or table.horizontalHeader().sizeHint().height()
    for row in range(shown):
        height += table.rowHeight(row)
    height += 2 * table.frameWidth() + table.horizontalScrollBar().sizeHint().height()
    table.setMinimumHeight(0)
    table.setMaximumHeight(height)
    policy = table.sizePolicy()
    policy.setVerticalPolicy(policy.Policy.Maximum)
    table.setSizePolicy(policy)

def fold_findings_table(inner, table, title: str, name: str):
    """Put a findings table behind a closed fold header; return the header.

    Plan 44 CHR2 (G-FITS-1 / G-FITG-1). MEASURED live at 1280x800: the
    findings table is 192 px of a 233 px column whatever it holds (one
    wrapped finding is 140 px of it), and Gmsh Quality draws two. The verdict
    headline stays in sight; the rows (every one of them) are one press away,
    and the header counts them. ``refresh`` calls :func:`count_findings`.
    """
    from foammesh.view.widgets.folder_header import FolderHeader

    header = FolderHeader(title)
    header.setObjectName(name)
    header.setProperty('foammeshFoldTitle', title)
    header.setProperty('foammeshSectionFold', True)
    header.setChecked(False)
    inner.insertWidget(inner.indexOf(table), header)
    header.setContents(table)
    return header


def count_findings(header, table, rows: int) -> None:
    """Say how many rows are behind the fold; draw none when there are none."""
    title = str(header.property('foammeshFoldTitle') or '')
    header.setText(f'{title}  ({rows})' if rows else title)
    header.setVisible(rows > 0)
    table.setVisible(rows > 0 and header.isChecked())


class CheckMeshFindings(QGroupBox):
    """What the last ``checkMesh`` run on this case actually reported.

    R100/R143. The report is written to `foammesh/quality/latest.json` with
    every metric and every classified finding, the QA row is set from it, and
    the page that set the row showed none of it. The projection is
    :func:`core.quality.readout.checkmesh_readout`, so this panel and any other
    surface reading the same file cannot disagree about it.
    """

    def __init__(self, facade_client, parent=None, *, check: str = ''):
        super().__init__('checkMesh findings', parent)
        self._client = facade_client
        #: Plan 41 GN16. Which check's report this panel reads. Empty means
        #: the report the project's own preset asks for (the snappy page);
        #: the Gmsh Quality page names its check, so a panel shows its own
        #: check's report whatever the preset is.
        self._check = str(check or '')
        inner = QVBoxLayout(self)
        inner.setSpacing(GAP_TIGHT)  # Plan 44 CHR2: the fold and verdict sit close
        # Plan 44 CHR2: the verdict is one line until opened (FX2 idiom).
        self._headline = FoldedNote(self)
        self._headline.setObjectName('qaFindingsHeadline')
        self._headline.setWordWrap(True)
        self._headline.setAccessibleName(
            self.tr('Verdict from the last mesh check'))
        inner.addWidget(self._headline)
        self._caveat = QLabel(self)
        self._caveat.setObjectName('qaFindingsCaveat')
        self._caveat.setWordWrap(True)
        self._caveat.setProperty('foammeshStatus', 'warning')
        self._caveat.setVisible(False)
        inner.addWidget(self._caveat)
        self._table = QTableWidget(0, 3, self)
        self._table.setObjectName('qaFindings')
        self._table.setHorizontalHeaderLabels(
            (self.tr('Finding'), self.tr('Severity'), self.tr('Detail')))
        self._table.setAccessibleName(self.tr('checkMesh findings and metrics'))
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
            inner, self._table, self.tr('Findings and metrics'), 'qaFindingsFold')
        self.refresh()

    def report(self) -> dict:
        try:
            payload = query(
                self._client, 'quality.report',
                {'check': self._check} if self._check else {}
            ).payload or {}
        except Exception:                                    # noqa: BLE001
            return {}
        return dict(payload.get('report') or {})

    def refresh(self) -> None:
        from foammesh.core.quality.readout import checkmesh_readout

        report = self.report()
        readout = checkmesh_readout(report)
        self._headline.setText(readout.headline)
        # W-O1. A verdict is a measured result and stays; the sentence that
        # says there is no verdict yet is the state of the case, and it is
        # said by the panel itself rather than by a label standing in the
        # settings column where a reading will later be.
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
        # Plan 40 UXA4 (#53). A box with nothing in it is not drawn.
        self.setVisible(bool(report) or bool(readout.rows))


__all__ = ['CheckMeshFindings', 'count_findings', 'fit_table_to_rows',
           'fold_findings_table']
