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


class CheckMeshFindings(QGroupBox):
    """What the last ``checkMesh`` run on this case actually reported.

    R100/R143. The report is written to `foammesh/quality/latest.json` with
    every metric and every classified finding, the QA row is set from it, and
    the page that set the row showed none of it. The projection is
    :func:`core.quality.readout.checkmesh_readout`, so this panel and any other
    surface reading the same file cannot disagree about it.
    """

    def __init__(self, facade_client, parent=None):
        super().__init__('checkMesh findings', parent)
        self._client = facade_client
        inner = QVBoxLayout(self)
        self._headline = QLabel(self)
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
        self.refresh()

    def report(self) -> dict:
        try:
            payload = query(self._client, 'quality.report').payload or {}
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
        self._table.setVisible(bool(readout.rows))


__all__ = ['CheckMeshFindings']
