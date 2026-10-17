"""Plan 41 GN8: structured meshing -- transfinite surfaces and volumes with
corners picked in the viewport and the triangle arrangement, automatic
transfinite with its corner angle and recombine flag, the block grid preview,
what Gmsh made of each row, and the grid file of the Structured size field.

Edits ``gmsh.structure.rows`` (``core/gmsh/structure``).  Every parameter is a
control generated from the catalogue.  Entities and corner points are picked
on the shared entity panel; the reason a row cannot be applied is shown beside
the rows, not after a run.  Adoptable by a task page
(``pending_patch`` / ``dirtyChanged`` / ``reload``).
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, CompactSpinBox
from foammesh.view.workflow_controls.length_box import (
    LengthListEdit, ModelLengthBox, length_cell,
)
from foammesh.core.gmsh import structure as catalogue
from foammesh.core.gmsh import structured_grid as grids
from foammesh.core.naming import humanise_option
from foammesh.core.quantities import count_text
from foammesh.view.widgets.empty_state import attach_empty_state

COLUMN_NAME, COLUMN_WHAT, COLUMN_ON = range(3)
DIM_WORDS = (('Surface', 2), ('Volume', 3))


class _Bridge(QObject):
    done = Signal(str, object)
    failed = Signal(str)


class BlockPreview(QWidget):
    """The block grid, drawn as lines in an oblique projection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lines: list = []
        self.setObjectName('gmshStructurePreview')
        self.setMinimumHeight(140)
        self.setAccessibleName(self.tr('Block grid preview'))

    def setLines(self, lines) -> None:
        self._lines = [[(float(p[0]) + 0.5 * float(p[2]),
                         float(p[1]) + 0.35 * float(p[2])) for p in line]
                       for line in lines]
        self.update()

    def lineCount(self) -> int:
        return len(self._lines)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        points = [p for line in self._lines for p in line]
        if not points:
            painter.end()
            return
        x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
        y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
        scale = min((self.width() - 16) / ((x1 - x0) or 1.0),
                    (self.height() - 16) / ((y1 - y0) or 1.0))
        painter.setPen(QPen(QColor('#1b6ca8'), 1))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for line in self._lines:
            mapped = [QPointF(8 + (x - x0) * scale,
                              self.height() - 8 - (y - y0) * scale)
                      for x, y in line]
            for a, b in zip(mapped, mapped[1:]):
                painter.drawLine(a, b)
        painter.end()


class StructurePanel(QWidget):
    """Edits ``gmsh.structure.rows``."""

    dirtyChanged = Signal(bool)

    def __init__(self, client, provider=None, entity_panel=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._provider = provider
        self._entityPanel = entity_panel
        self._embedded = False
        self._target = None
        self._worker = None
        self._rows: list = []
        self._stored: list = []
        self._extra_patch: dict = {}
        self._dirty = False
        self._filling = False
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self.setObjectName('gmshStructurePanel')
        if entity_panel is not None:
            entity_panel.pickAccepted.connect(self._onPicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._summary = QLabel(self)
        self._summary.setObjectName('gmshStructureSummary')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, 3, self)
        self._table.setObjectName('gmshStructureTable')
        self._table.setAccessibleName(self.tr('Structured meshing rows'))
        self._table.setHorizontalHeaderLabels(
            [self.tr('Name'), self.tr('Structure'), self.tr('On')])
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_NAME,
                                    QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COLUMN_WHAT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COLUMN_ON,
                                    QHeaderView.ResizeMode.ResizeToContents)
        # Plan 44 MF20. An empty table is its header; the line below says why.
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(self._on_current)
        layout.addWidget(self._table)
        self._empty = attach_empty_state(
            self._table, self.tr(
                'No structured rows yet. Add a transfinite surface or volume '
                'below.'), 'gmshStructureEmpty', cap_rows=8)

        add = QHBoxLayout()
        self._kind = QComboBox(self)
        self._kind.setObjectName('gmshStructureKind')
        self._kind.setAccessibleName(self.tr('Kind of structure to add'))
        for kind in catalogue.KINDS.values():
            self._kind.addItem(kind.title, kind.id)
        add.addWidget(self._kind, 1)
        self._add = QPushButton(self.tr('Add'), self)
        self._add.setObjectName('gmshStructureAdd')
        self._add.clicked.connect(lambda _c=False: self.add_row())
        add.addWidget(self._add)
        self._remove = QPushButton(self.tr('Remove'), self)
        self._remove.setObjectName('gmshStructureRemove')
        self._remove.clicked.connect(lambda _c=False: self.remove_row())
        add.addWidget(self._remove)
        layout.addLayout(add)

        self._help = QLabel(self)
        self._help.setObjectName('gmshStructureHelp')
        self._help.setWordWrap(True)
        layout.addWidget(self._help)

        self._form_host = QWidget(self)
        self._form_host.setObjectName('gmshStructureForm')
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_host)

        self._problem = QLabel(self)
        self._problem.setObjectName('gmshStructureProblem')
        self._problem.setWordWrap(True)
        self._problem.setProperty('foammeshStatus', 'error')
        self._problem.setVisible(False)
        layout.addWidget(self._problem)

        actions = QHBoxLayout()
        self._apply = QPushButton(self.tr('Apply to model'), self)
        self._apply.setObjectName('gmshStructureApply')
        self._apply.clicked.connect(lambda _c=False: self.apply())
        self._previewButton = QPushButton(self.tr('Preview block grid'), self)
        self._previewButton.setObjectName('gmshStructurePreviewButton')
        self._previewButton.clicked.connect(lambda _c=False: self.preview())
        self._measureButton = QPushButton(self.tr('Measure result'), self)
        self._measureButton.setObjectName('gmshStructureMeasure')
        self._measureButton.clicked.connect(lambda _c=False: self.measure())
        for button in (self._apply, self._previewButton, self._measureButton):
            actions.addWidget(button)
        layout.addLayout(actions)
        self._preview = BlockPreview(self)
        self._preview.setVisible(False)
        layout.addWidget(self._preview)
        self._status = QLabel(self)
        self._status.setObjectName('gmshStructureStatus')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        # the grid file of the Structured size field (M-15) ----------------- #
        title = QLabel(self.tr('Structured size grid'), self)
        title.setObjectName('gmshStructureGridTitle')
        layout.addWidget(title)
        grid = QFormLayout()
        # UXB3c. Lengths: typed in the project display unit, written to the
        # grid file (and Gmsh) in model units.
        self._bounds = LengthListEdit(self)
        self._bounds.setObjectName('gmshStructureGridBounds')
        self._bounds.setAccessibleName(self.tr('Grid bounds'))
        self._bounds.setPlaceholderText(self.tr(
            'xmin ymin zmin xmax ymax zmax'))
        self._step = ModelLengthBox(self, minimum=1e-9, maximum=1e9)
        self._step.setObjectName('gmshStructureGridStep')
        self._step.setAccessibleName(self.tr('Grid step'))
        self._step.setModelValue(0.1)
        self._expression = QLineEdit(self)
        self._expression.setObjectName('gmshStructureGridExpression')
        self._expression.setAccessibleName(self.tr('Size expression'))
        self._expression.setPlaceholderText(self.tr(
            'a size, or an expression in x, y, z'))
        self._text = QCheckBox(self.tr('ASCII text file'), self)
        self._text.setChecked(True)
        grid.addRow(self.tr('Bounds'), length_cell(self._bounds, self))
        grid.addRow(self.tr('Grid step'), length_cell(self._step, self))
        grid.addRow(self.tr('Size'), self._expression)
        grid.addRow('', self._text)
        layout.addLayout(grid)
        self._writeGrid = QPushButton(self.tr('Write grid and add field'), self)
        self._writeGrid.setObjectName('gmshStructureGridWrite')
        self._writeGrid.clicked.connect(lambda _c=False: self.write_grid())
        layout.addWidget(self._writeGrid)
        self.reload()

    # -- the FieldGroupPage-shaped surface a task page adopts --------------- #

    def setEmbedded(self, embedded: bool = True) -> None:
        self._embedded = bool(embedded)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def rows(self) -> list:
        return [dict(r, params=dict(r.get('params') or {}),
                     tags=list(r.get('tags') or []),
                     corners=list(r.get('corners') or []))
                for r in self._rows]

    def pending_patch(self) -> dict:
        if not self._dirty:
            return {}
        return {catalogue.FIELD_ID: catalogue.rows_to_text(self._rows),
                **self._extra_patch}

    def reload(self, *, discard_pending: bool = True) -> None:
        if discard_pending:
            self._dirty = False
            self._extra_patch = {}
        self._stored = self._read_stored()
        if discard_pending:
            self._rows = [dict(r, params=dict(r.get('params') or {}),
                               tags=list(r.get('tags') or []),
                               corners=list(r.get('corners') or []))
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
        except catalogue.StructureRefusal as error:
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

    def _touch(self) -> None:
        """A change that leaves the form as it is (typed tags): refresh the
        table text and the check, keep the editor and its focus."""
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._filling = True
        for index, row in enumerate(self._rows):
            item = self._table.item(index, COLUMN_WHAT)
            if item is not None:
                item.setText(catalogue.describe(row))
        self._filling = False
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

    def set_tags(self, tags, row_index: int | None = None,
                 corners: bool = False) -> None:
        index = self._table.currentRow() if row_index is None else row_index
        if 0 <= index < len(self._rows):
            self._rows[index]['corners' if corners else 'tags'] = [
                int(t) for t in tags]
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
            count_text(len(self._rows), self.tr('structured row'))
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

    def _tagRow(self, row, key: str, filter_name: str, accessible: str):
        host = QWidget(self)
        box = QHBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(host)
        # An object name, not a screen name: the stored key joins it in
        # camel case (corners -> gmshStructureCorners).
        edit.setObjectName('gmshStructure' + key[:1].upper() + key[1:])
        edit.setAccessibleName(accessible)
        edit.setPlaceholderText(self.tr('1, 2, 3'))
        edit.setText(', '.join(str(t) for t in row.get(key) or []))
        edit.editingFinished.connect(
            lambda r=row, e=edit, k=key: self._set_tags_text(r, k, e.text()))
        pick = QPushButton(self.tr('Pick'), host)
        pick.setObjectName('gmshStructurePick_' + key)
        pick.setAccessibleName(self.tr('Pick %s in the viewport') % accessible)
        pick.clicked.connect(
            lambda _c=False, r=row, k=key, f=filter_name: self._requestPick(
                r, k, f))
        clear = QPushButton(self.tr('Clear'), host)
        clear.setObjectName('gmshStructureClear_' + key)
        clear.clicked.connect(
            lambda _c=False, r=row, k=key, e=edit: (
                e.clear(), self._set_tags_text(r, k, '')))
        box.addWidget(edit, 1)
        box.addWidget(pick)
        box.addWidget(clear)
        return host

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
            combo.setObjectName('gmshStructureDim')
            for word, dim in DIM_WORDS:
                if dim in kind.dims:
                    combo.addItem(self.tr(word + ' tags'), dim)
            combo.setCurrentIndex(max(combo.findData(row.get('dim')), 0))
            combo.currentIndexChanged.connect(
                lambda _i, r=row, c=combo: self._set_dim(r, c.currentData()))
            self._form.addRow(self.tr('Entities are'), combo)
        dim = int(row.get('dim', kind.dims[-1]))
        self._form.addRow(
            self.tr('Surfaces') if dim == 2 else self.tr('Volumes'),
            self._tagRow(row, 'tags', 'Surfaces' if dim == 2 else 'Volumes',
                         self.tr('Entity tags')))
        if any(kind.corners):
            self._form.addRow(
                self.tr('Corner points'),
                self._tagRow(row, 'corners', 'Points',
                             self.tr('Corner points')))
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

    def _set_tags_text(self, row, key, text) -> None:
        try:
            tags = catalogue.parse_tags(text, unique=key != 'corners')
        except catalogue.StructureRefusal as error:
            self._show_problem(str(error))
            return
        if tags != list(row.get(key) or []):
            row[key] = tags
            self._touch()

    # -- picking ------------------------------------------------------------ #

    def _requestPick(self, row, key, filter_name) -> None:
        if self._entityPanel is None:
            self._say(self.tr('There is no viewport to pick in.'))
            return
        self._target = (row, key)
        if not self._entityPanel.pickFor(filter_name):
            self._target = None
            self._say(self._entityPanel.status())
            return
        self._say(self.tr('Pick %s in the viewport; Enter accepts.')
                  % filter_name.lower())

    def _onPicked(self, chosen) -> None:
        target, self._target = self._target, None
        if target is None:
            return
        row, key = target
        row[key] = [int(e[1]) for e in chosen]
        self._changed()
        self._say(self.tr('%d picked.') % len(chosen))

    # -- checking ----------------------------------------------------------- #

    def check(self) -> str:
        """The reason the rows cannot be applied, or ''."""
        try:
            catalogue.validate(self._rows)
        except catalogue.StructureRefusal as error:
            return str(error)
        return ''

    def _check(self) -> None:
        self._show_problem(self.check())

    def _show_problem(self, text: str) -> None:
        self._problem.setText(text)
        self._problem.setVisible(bool(text))

    def _say(self, text: str) -> None:
        self._status.setText(text)

    def status(self) -> str:
        return self._status.text()

    # -- the session work ----------------------------------------------------- #

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _ops(self) -> list | None:
        reason = self.check()
        if reason:
            self._say(reason)
            return None
        return catalogue.validate(self._rows).to_list()

    def _run(self, what: str, work) -> None:
        if self.busy():
            return
        if self._provider is None:
            self._say(self.tr('There is no Gmsh session to work on.'))
            return
        try:
            controller = self._provider()
        except Exception as error:                      # noqa: BLE001
            self._say(str(error))
            return
        self._say(self.tr('Working…'))

        def run():
            try:
                with controller._lock:                  # noqa: SLF001
                    session = controller._build_session()   # noqa: SLF001
                    result = work(session.call)
                self._bridge.done.emit(what, result)
            except Exception as error:                  # noqa: BLE001
                self._bridge.failed.emit(str(error))

        self._worker = threading.Thread(target=run, name='gmsh-structure',
                                        daemon=True)
        self._worker.start()

    def apply(self) -> None:
        ops = self._ops()
        if ops is not None:
            self._run('apply', lambda call: catalogue.apply_to_session(
                call, ops))

    def preview(self) -> None:
        ops = self._ops()
        if ops is not None:
            self._run('preview', lambda call: catalogue.preview_on_session(
                call, ops))

    def measure(self) -> None:
        ops = self._ops()
        if ops is not None:
            self._run('measure', lambda call: catalogue.runtime_module()
                      .measure(catalogue.RemoteGmsh(call), ops))

    def _onFailed(self, message: str) -> None:
        self._worker = None
        self._say(message)

    def _onDone(self, what, result) -> None:
        self._worker = None
        if what == 'apply':
            self._say(self.tr('Applied %1 to the model.').replace(
                '%1', count_text(int(result.get('count', 0)),
                                 self.tr('row'))))
        elif what == 'preview':
            lines = [line for s in result.get('surfaces', [])
                     for line in s['lines']]
            self._preview.setLines(lines)
            self._preview.setVisible(bool(lines))
            skipped = '; '.join(f"surface {s['tag']}: {s['reason']}"
                                for s in result.get('skipped', []))
            self._say(count_text(len(result.get('surfaces', [])),
                                 self.tr('surface')) + self.tr(' drawn.')
                      + (' ' + skipped if skipped else ''))
        elif what == 'measure':
            self._say('\n'.join(
                f"{m['name']}: {m['note']}" for m in result)
                or self.tr('Nothing to measure.'))

    # -- the Structured size grid -------------------------------------------- #

    def write_grid(self) -> dict | None:
        """Write the grid file next to the case and add the Structured field
        (a pending change of the size-field rows) that reads it."""
        try:
            bounds = self._bounds.modelValues()
            if len(bounds) != 6:
                raise grids.GridRefusal(
                    'the bounds are six numbers: xmin ymin zmin xmax ymax '
                    'zmax')
            spec = grids.spec_covering(
                bounds, float(self._step.modelValue()),
                self._expression.text().strip(), self._text.isChecked())
            path = Path(self._client.case_path) / (
                'structured_size_grid.dat' if spec.text
                else 'structured_size_grid.bin')
            written = grids.write(path, spec)
        except (grids.GridRefusal, ValueError) as error:
            self._say(str(error))
            return None
        name = str(path)
        try:
            if self._provider is not None:
                name = self._provider().profile().translate_host_path(path)
        except Exception:                                # noqa: BLE001
            pass
        self._add_structured_field(name, spec.text)
        self._say(self.tr('Wrote %1 nodes to %2; the Structured field reads '
                          'it (save to keep it).')
                  .replace('%1', str(written.get('nodes', '')))
                  .replace('%2', name))
        return {'path': str(path), 'file_name': name, **written}

    def _add_structured_field(self, file_name: str, text: bool) -> None:
        from foammesh.core.gmsh import field_catalogue as fields
        try:
            raw = self._client.field_values((fields.FIELD_ID,)).get(
                fields.FIELD_ID)
            rows = fields.rows_from_text(getattr(raw, 'value', raw))
        except Exception:                                # noqa: BLE001
            rows = []
        pending = self._extra_patch.get(fields.FIELD_ID)
        if pending:
            rows = fields.rows_from_text(pending)
        rows = [r for r in rows if r.get('name') != 'structured_grid']
        row = fields.new_row('Structured', 'structured_grid')
        row['params'] = {'FileName': file_name,
                         'TextFormat': 1 if text else 0}
        rows.append(row)
        self._extra_patch[fields.FIELD_ID] = fields.rows_to_text(rows)
        self._dirty = True
        self.dirtyChanged.emit(True)
