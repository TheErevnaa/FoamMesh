"""Plan 41 GN9: embedded entities, per-surface recombine, algorithm, smoothing,
reverse, outward orientation, compounds and size from boundary.

The panel edits ``gmsh.entityControls.rows`` (``core/gmsh/entity_controls``).
Every parameter is a control generated from the catalogue; the entities are
typed as tags (the viewport picker of GN4 fills the same box through
``set_tags``), and the reason a row cannot be applied is shown beside the
rows, not after a run.

Adoptable by a task page (``pending_patch``/``dirtyChanged``/``reload``).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, CompactSpinBox
from foammesh.core.gmsh import entity_controls as catalogue
from foammesh.core.naming import humanise_option
from foammesh.core.quantities import count_text
from widgets.fit_to_text import FlowLayout

COLUMN_NAME, COLUMN_WHAT, COLUMN_ON = range(3)
DIM_WORDS = (('Point', 0), ('Curve', 1), ('Surface', 2), ('Volume', 3))


class EntityControlsPanel(QWidget):
    """Edits ``gmsh.entityControls.rows``."""

    dirtyChanged = Signal(bool)

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self._client = client
        self._embedded = False
        self._rows: list = []
        self._stored: list = []
        self._dirty = False
        self._filling = False
        self.setObjectName('gmshEntityControls')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._summary = QLabel(self)
        self._summary.setObjectName('gmshEntityControlsSummary')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, 3, self)
        self._table.setObjectName('gmshEntityControlsTable')
        self._table.setAccessibleName(self.tr('Entity controls'))
        self._table.setHorizontalHeaderLabels(
            [self.tr('Name'), self.tr('Control'), self.tr('On')])
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_NAME,
                                    QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COLUMN_WHAT,
                                    QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COLUMN_ON,
                                    QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(90)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(self._on_current)
        layout.addWidget(self._table)

        # DP-573: a row of controls wraps rather than standing wider than
        # the settings column (the compact column is 314 px).
        add = FlowLayout()
        self._kind = QComboBox(self)
        self._kind.setObjectName('gmshEntityControlKind')
        self._kind.setAccessibleName(self.tr('Kind of control to add'))
        for kind in catalogue.KINDS.values():
            self._kind.addItem(kind.title, kind.id)
        add.addWidget(self._kind)
        self._add = QPushButton(self.tr('Add control'), self)
        self._add.setObjectName('gmshEntityControlAdd')
        self._add.clicked.connect(lambda _c=False: self.add_row())
        add.addWidget(self._add)
        self._remove = QPushButton(self.tr('Remove'), self)
        self._remove.setObjectName('gmshEntityControlRemove')
        self._remove.clicked.connect(lambda _c=False: self.remove_row())
        add.addWidget(self._remove)
        layout.addLayout(add)

        self._help = QLabel(self)
        self._help.setObjectName('gmshEntityControlHelp')
        self._help.setWordWrap(True)
        layout.addWidget(self._help)

        self._form_host = QWidget(self)
        self._form_host.setObjectName('gmshEntityControlForm')
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_host)

        self._problem = QLabel(self)
        self._problem.setObjectName('gmshEntityControlProblem')
        self._problem.setWordWrap(True)
        self._problem.setProperty('foammeshStatus', 'error')
        self._problem.setVisible(False)
        layout.addWidget(self._problem)
        self.reload()

    # -- the FieldGroupPage-shaped surface a task page adopts --------------- #

    def setEmbedded(self, embedded: bool = True) -> None:
        self._embedded = bool(embedded)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def rows(self) -> list:
        return [dict(r, params=dict(r.get('params') or {}),
                     tags=list(r.get('tags') or [])) for r in self._rows]

    def pending_patch(self) -> dict:
        if not self._dirty:
            return {}
        return {catalogue.FIELD_ID: catalogue.rows_to_text(self._rows)}

    def reload(self, *, discard_pending: bool = True) -> None:
        if discard_pending:
            self._dirty = False
        self._stored = self._read_stored()
        if discard_pending:
            self._rows = [dict(r, params=dict(r.get('params') or {}),
                               tags=list(r.get('tags') or []))
                          for r in self._stored]
        self._fill()
        self.dirtyChanged.emit(self._dirty)

    def revert(self) -> None:
        self.reload(discard_pending=True)

    def _read_stored(self) -> list:
        try:
            values = self._client.field_values((catalogue.FIELD_ID,))
            raw = getattr(values.get(catalogue.FIELD_ID), 'value',
                          values.get(catalogue.FIELD_ID))
            return catalogue.rows_from_text(raw)
        except catalogue.EntityRefusal as error:
            self._show_problem(str(error))
            return []
        except Exception:                                # noqa: BLE001
            return []

    # -- editing ----------------------------------------------------------- #

    def _changed(self) -> None:
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._fill_table()
        self._check()

    def add_row(self, kind: str | None = None, tags=None) -> dict:
        kind = kind or self._kind.currentData()
        names = {r['name'] for r in self._rows}
        number = 1
        while f'{kind}{number}' in names:
            number += 1
        row = catalogue.new_row(kind, f'{kind}{number}')
        if tags:
            row['tags'] = list(tags)
        self._rows.append(row)
        self._changed()
        self._table.setCurrentCell(len(self._rows) - 1, COLUMN_NAME)
        return row

    def remove_row(self) -> None:
        index = self._table.currentRow()
        if 0 <= index < len(self._rows):
            self._rows.pop(index)
            self._changed()

    def set_tags(self, tags, row_index: int | None = None) -> None:
        """Fill the entity box of a row (the viewport picker uses this)."""
        index = self._table.currentRow() if row_index is None else row_index
        if 0 <= index < len(self._rows):
            self._rows[index]['tags'] = [int(t) for t in tags]
            self._changed()

    def _fill(self) -> None:
        self._fill_table()
        self._check()

    def _fill_table(self) -> None:
        self._filling = True
        current = self._table.currentRow()
        self._table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            name = QTableWidgetItem(row['name'])
            what = QTableWidgetItem(catalogue.describe(row))
            what.setFlags(what.flags() & ~Qt.ItemFlag.ItemIsEditable)
            on = QTableWidgetItem('')
            on.setFlags((on.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable)
            on.setCheckState(Qt.CheckState.Checked if row.get('enabled', True)
                             else Qt.CheckState.Unchecked)
            for column, item in enumerate((name, what, on)):
                self._table.setItem(index, column, item)
        self._filling = False
        if self._rows:
            chosen = min(max(current, 0), len(self._rows) - 1)
            self._table.setCurrentCell(chosen, COLUMN_NAME)
            self._build_form(self._rows[chosen])
        else:
            self._build_form(None)
        off = sum(1 for r in self._rows if not r.get('enabled', True))
        self._summary.setText(
            count_text(len(self._rows), self.tr('entity control'))
            + (self.tr(', %1 switched off').replace('%1', str(off))
               if off else ''))

    def _on_item_changed(self, item) -> None:
        if self._filling:
            return
        row = self._rows[item.row()]
        if item.column() == COLUMN_NAME and item.text().strip():
            row['name'] = item.text().strip()
        elif item.column() == COLUMN_ON:
            row['enabled'] = item.checkState() == Qt.CheckState.Checked
        self._changed()

    def _on_current(self, row, *_args) -> None:
        if self._filling:
            return
        self._build_form(self._rows[row] if 0 <= row < len(self._rows)
                         else None)

    # -- the form, from the catalogue ---------------------------------------- #

    def _build_form(self, row) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        if row is None:
            self._help.setText('')
            return
        kind = catalogue.KINDS[row['kind']]
        self._help.setText(kind.help)
        if len(kind.dims) > 1:
            combo = QComboBox(self)
            combo.setObjectName('gmshEntityControlDim')
            for word, dim in DIM_WORDS:
                if dim in kind.dims:
                    combo.addItem(self.tr(word + ' tags'), dim)
            combo.setCurrentIndex(max(combo.findData(row.get('dim')), 0))
            combo.currentIndexChanged.connect(
                lambda _i, r=row, c=combo: self._set_dim(r, c.currentData()))
            self._form.addRow(self.tr('Entities are'), combo)
        tags = QLineEdit(self)
        tags.setObjectName('gmshEntityControlTags')
        tags.setAccessibleName(self.tr('Entity tags'))
        tags.setPlaceholderText(self.tr('1, 2, 3'))
        tags.setText(', '.join(str(t) for t in row.get('tags') or []))
        tags.editingFinished.connect(
            lambda r=row, e=tags: self._set_tags_text(r, e.text()))
        self._form.addRow(self.tr('Entity tags'), tags)
        for spec in kind.params:
            editor = self._editor(row, spec)
            editor.setToolTip(spec.help)
            editor.setAccessibleName(f'{row["name"]} {spec.name}')
            self._form.addRow(humanise_option(spec.name), editor)

    def _editor(self, row, spec):
        value = row['params'].get(spec.name, spec.default)
        if spec.kind == 'bool':
            box = QCheckBox(self)
            box.setChecked(bool(value))
            box.toggled.connect(
                lambda state, s=spec, r=row: self._set(r, s, bool(state)))
            return box
        if spec.kind == 'enum':
            combo = QComboBox(self)
            for code, label in spec.choices:
                combo.addItem(f'{label} ({code})', code)
            combo.setCurrentIndex(max(combo.findData(int(value)), 0))
            combo.currentIndexChanged.connect(
                lambda _i, s=spec, r=row, c=combo: self._set(
                    r, s, c.currentData()))
            return combo
        if spec.kind == 'int':
            box = CompactSpinBox(self)
            box.setRange(int(spec.low if spec.low is not None else 0),
                         int(spec.high if spec.high is not None else 10**6))
            box.setValue(int(value))
            box.valueChanged.connect(
                lambda v, s=spec, r=row: self._set(r, s, int(v)))
            return box
        box = CompactDoubleSpinBox(self)
        box.setDecimals(6)
        box.setRange(spec.low if spec.low is not None else -1e9,
                     spec.high if spec.high is not None else 1e9)
        box.setValue(float(value))
        box.valueChanged.connect(
            lambda v, s=spec, r=row: self._set(r, s, float(v)))
        return box

    def _set(self, row, spec, value) -> None:
        if row['params'].get(spec.name) != value:
            row['params'][spec.name] = value
            self._dirty = True
            self.dirtyChanged.emit(True)
            self._check()

    def _set_dim(self, row, dim) -> None:
        if dim is not None and row.get('dim') != dim:
            row['dim'] = int(dim)
            self._changed()

    def _set_tags_text(self, row, text) -> None:
        try:
            tags = catalogue.parse_tags(text)
        except catalogue.EntityRefusal as error:
            self._show_problem(str(error))
            return
        if tags != list(row.get('tags') or []):
            row['tags'] = tags
            self._changed()

    # -- checking ----------------------------------------------------------- #

    def check(self) -> str:
        """The reason the controls cannot be applied, or ''."""
        try:
            catalogue.validate(self._rows)
        except catalogue.EntityRefusal as error:
            return str(error)
        return ''

    def _check(self) -> None:
        self._show_problem(self.check())

    def _show_problem(self, text: str) -> None:
        self._problem.setText(text)
        self._problem.setVisible(bool(text))
