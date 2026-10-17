"""The further quality limits as a table, on the Generate mesh page.

Plan 41 GN14 (section 4.3).  The gate judges the primary measure (the
``Quality measure`` and ``Worst cell quality allowed`` controls above) and any
number of further measures, each on its own limit and allowance, so passing one
cannot hide failing another.  The derivation always accepted such rows
(``extraLimits``); nothing let a user write one.  This is that table.

Stored as ``gmsh.compute.extra_limits`` (JSON text).  The panel has the surface
a task page adopts: ``is_dirty``, ``pending_patch``, ``reload``, ``revert``,
``setEmbedded`` and ``dirtyChanged``.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QVBoxLayout, QWidget,
)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, CompactSpinBox
from foammesh.core.gmsh.quality import MEASURES, QualityError, derive_limit

FIELD = 'gmsh.compute.extra_limits'
PRIMARY_FIELD = 'gmsh.compute.quality_type'
COLUMNS = ('Measure', 'Minimum', 'Share allowed below', 'Count allowed below',
           'Hard floor')


def _measure_word(value) -> str:
    return str(getattr(value, 'value', value) or 'sicn').split('.')[-1].lower()


class QualityLimitsPanel(QWidget):
    """Add, change and remove the further measures the gate judges."""

    dirtyChanged = Signal(bool)

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self._client = client
        self._embedded = False
        self._stored: list = []
        self._rows: list = []
        self._dirty = False
        self._filling = False
        self.setObjectName('gmshQualityLimitsPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName('gmshQualityLimitsTable')
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self._table.setAccessibleName(self.tr('Further quality limits'))
        # Plan 33 section 6 check 1: what the table means is said on the
        # table, not as a paragraph standing in the settings column.
        explanation = self.tr(
            'Each further measure is judged on its own limit and allowance, '
            'beside the measure chosen above. A mesh must meet every one.')
        self._table.setToolTip(explanation)
        self._table.setAccessibleDescription(explanation)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(110)
        layout.addWidget(self._table)

        self._error = QLabel(self)
        self._error.setObjectName('gmshQualityLimitsError')
        self._error.setWordWrap(True)
        self._error.setProperty('foammeshStatus', 'error')
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QHBoxLayout()
        self._add = QPushButton(self.tr('Add limit'), self)
        self._add.setObjectName('gmshQualityLimitAdd')
        self._add.clicked.connect(self.add_row)
        self._remove = QPushButton(self.tr('Remove selected'), self)
        self._remove.setObjectName('gmshQualityLimitRemove')
        self._remove.clicked.connect(self.remove_selected)
        buttons.addWidget(self._add)
        buttons.addWidget(self._remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.reload()

    # -- the FieldGroupPage-shaped surface a task page adopts --------------- #

    def setEmbedded(self, embedded: bool = True) -> None:
        self._embedded = bool(embedded)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def pending_patch(self) -> dict:
        if not self._dirty:
            return {}
        rows = self.rows()
        return {FIELD: json.dumps(rows) if rows else ''}

    def reload(self, *, discard_pending: bool = True) -> None:
        if not discard_pending and self._dirty:
            return
        self._stored = self._read_stored()
        self._fill(self._stored)
        self._set_dirty(False)

    def revert(self) -> None:
        self.reload(discard_pending=True)

    # -- reading and writing the table -------------------------------------- #

    def _read_stored(self) -> list:
        try:
            values = self._client.field_values((FIELD,))
            raw = values.get(FIELD)
            raw = getattr(raw, 'value', raw)
            return json.loads(raw) if str(raw or '').strip() else []
        except Exception:                                # noqa: BLE001
            return []

    def primary_measure(self) -> str:
        try:
            values = self._client.field_values((PRIMARY_FIELD,))
            return _measure_word(values.get(PRIMARY_FIELD))
        except Exception:                                # noqa: BLE001
            return 'sicn'

    def rows(self) -> list:
        """The table as the stored rows."""
        out = []
        for row in range(self._table.rowCount()):
            cells = [self._table.cellWidget(row, column)
                     for column in range(len(COLUMNS))]
            out.append({
                'measure': str(cells[0].currentData()),
                'minQuality': float(cells[1].value()),
                'allowedFraction': float(cells[2].value()),
                'allowedCount': int(cells[3].value()),
                'hardFloor': float(cells[4].value()),
            })
        return out

    def _fill(self, rows) -> None:
        self._filling = True
        try:
            self._table.setRowCount(0)
            for row in rows:
                self._append(dict(row))
        finally:
            self._filling = False
        self._check()

    def _append(self, row: dict) -> None:
        index = self._table.rowCount()
        self._table.insertRow(index)
        measure = QComboBox(self._table)
        for name in sorted(MEASURES):
            measure.addItem(name.upper() if name != 'gamma' else 'Gamma',
                            name)
        word = str(row.get('measure') or row.get('qualityType') or 'gamma')
        measure.setCurrentIndex(max(0, measure.findData(word.lower())))
        measure.setAccessibleName(self.tr('Measure'))
        measure.currentIndexChanged.connect(self._edited)
        self._table.setCellWidget(index, 0, measure)
        for column, key, default in ((1, 'minQuality', 0.1),
                                     (2, 'allowedFraction', 0.0),
                                     (4, 'hardFloor', 0.0)):
            box = CompactDoubleSpinBox(self._table)
            box.setRange(0.0, 1.0)
            box.setDecimals(4)
            box.setSingleStep(0.01)
            value = row.get(key)
            if value is None and key == 'minQuality':
                value = row.get('minimum')
            box.setValue(float(default if value is None else value))
            box.setAccessibleName(self.tr(COLUMNS[column]))
            box.valueChanged.connect(self._edited)
            self._table.setCellWidget(index, column, box)
        count = CompactSpinBox(self._table)
        count.setRange(0, 2_000_000_000)
        count.setValue(int(row.get('allowedCount', 0) or 0))
        count.setAccessibleName(self.tr(COLUMNS[3]))
        count.valueChanged.connect(self._edited)
        self._table.setCellWidget(index, 3, count)

    def add_row(self) -> None:
        """A new row, on a measure not already limited where one is left."""
        taken = {self.primary_measure()} | {r['measure'] for r in self.rows()}
        free = [name for name in sorted(MEASURES) if name not in taken]
        self._append({'measure': free[0] if free else 'gamma',
                      'minQuality': 0.1})
        self._edited()

    def remove_selected(self) -> None:
        rows = sorted({item.row() for item in self._table.selectedIndexes()},
                      reverse=True)
        current = self._table.currentRow()
        if not rows and current >= 0:
            rows = [current]
        for row in rows:
            self._table.removeRow(row)
        if rows:
            self._edited()

    def _edited(self, *_args) -> None:
        if self._filling:
            return
        self._check()
        self._set_dirty(True)

    def problem(self) -> str:
        """Why the rows cannot be judged, or ``''``."""
        seen = {self.primary_measure()}
        for index, row in enumerate(self.rows()):
            if row['measure'] in seen:
                return self.tr(
                    '%s is limited twice; each measure carries one limit '
                    '(row %d).') % (row['measure'].upper(), index + 1)
            seen.add(row['measure'])
            try:
                derive_limit(row, context=f'row {index + 1}')
            except QualityError as error:
                return str(error)
        return ''

    def _check(self) -> None:
        text = self.problem()
        self._error.setText(text)
        self._error.setVisible(bool(text))

    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirtyChanged.emit(dirty)
