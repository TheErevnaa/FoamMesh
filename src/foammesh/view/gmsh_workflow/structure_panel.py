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
from foammesh.core.facade.plan48 import pa3_routes
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
    #: Plan 48 PA3: a result or a line of text the local API puts in the panel
    #: from its own thread (queued to the GUI thread).
    shown = Signal(str, object)
    said = Signal(str)


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
    #: Plan 48 GM6. ``{tag: [r, g, b]}`` of the last "Make structured"
    #: answer (green: can be structured, red: cannot); the viewport paints it.
    qualificationChanged = Signal(object)

    def __init__(self, client, provider=None, entity_panel=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._provider = provider
        self._entityPanel = entity_panel
        self._embedded = False
        self._target = None
        self._worker = None
        #: DP-5738: the press of Make structured to keep as a row
        self._keepAutomatic = None
        self._rows: list = []
        self._stored: list = []
        self._extra_patch: dict = {}
        self._dirty = False
        self._filling = False
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._onDone)
        self._bridge.failed.connect(self._onFailed)
        self._bridge.shown.connect(self._onShown)
        self._bridge.said.connect(self._say)
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

        # Plan 48 GM6: the corner pre-check, "Make structured where
        # possible" and the hex-dominant action -------------------------- #
        more = QHBoxLayout()
        self._checkCorners = QPushButton(self.tr('Check this row'), self)
        self._checkCorners.setObjectName('gmshStructureCheckCorners')
        self._checkCorners.setToolTip(self.tr(
            'Can the picked surfaces or volumes be transfinite with these '
            'corners? The reason is named when they cannot.'))
        self._checkCorners.clicked.connect(lambda _c=False: self.check_row())
        self._whichStructured = QPushButton(
            self.tr('Which can be structured?'), self)
        self._whichStructured.setObjectName('gmshStructureWhich')
        self._whichStructured.setToolTip(self.tr(
            'Colour the volumes that can be structured green and the others '
            'red, with the reason; changes nothing'))
        self._whichStructured.clicked.connect(
            lambda _c=False: self.make_structured(apply=False))
        self._makeStructured = QPushButton(
            self.tr('Make structured where possible'), self)
        self._makeStructured.setObjectName('gmshStructureMake')
        self._makeStructured.clicked.connect(
            lambda _c=False: self.make_structured(apply=True))
        for button in (self._checkCorners, self._whichStructured,
                       self._makeStructured):
            more.addWidget(button)
        layout.addLayout(more)

        hexes = QHBoxLayout()
        self._hexLevel = QComboBox(self)
        self._hexLevel.setObjectName('gmshStructureHexLevel')
        self._hexLevel.setAccessibleName(self.tr('3D recombination level'))
        for value, text in ((0, self.tr('Level 0 (simple)')),
                            (1, self.tr('Level 1')), (2, self.tr('Level 2'))):
            self._hexLevel.addItem(text, value)
        self._hexLevel.setCurrentIndex(1)
        self._hexConformity = QComboBox(self)
        self._hexConformity.setObjectName('gmshStructureHexConformity')
        self._hexConformity.setAccessibleName(
            self.tr('3D recombination conformity'))
        for value, text in ((0, self.tr('Non-conforming')),
                            (1, self.tr('Trihedra')),
                            (2, self.tr('Pyramids and trihedra')),
                            (3, self.tr('Pyramids, hex split, trihedra')),
                            (4, self.tr('Pyramids, form quad, trihedra'))):
            self._hexConformity.addItem(text, value)
        self._hexButton = QPushButton(self.tr('Hex-dominant (experimental)'),
                                      self)
        self._hexButton.setObjectName('gmshStructureHexDominant')
        self._hexButton.setToolTip(self.tr(
            'Mesh in 3D with Gmsh\'s experimental 3D recombination and count '
            'the tetrahedra, hexahedra, prisms and pyramids it made'))
        self._hexButton.clicked.connect(lambda _c=False: self.hex_dominant())
        for widget in (self._hexLevel, self._hexConformity, self._hexButton):
            hexes.addWidget(widget)
        layout.addLayout(hexes)
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
        # Plan 48 PA3: the local API drives this panel as a person does.
        self._api = StructureApi(self)
        session = pa3_routes.announce(client, 'structure', self._api)
        if session is not None:
            self.destroyed.connect(
                lambda *_a, s=session, a=self._api: pa3_routes.unregister_panel(
                    s, 'structure', a))

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
        row = catalogue.new_row(kind, pa3_routes.next_name(self._rows, kind))
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

    def check_row(self) -> None:
        """Pre-check the current row's entities and corners."""
        index = self._table.currentRow()
        if not 0 <= index < len(self._rows):
            self._say(self.tr('Select a transfinite row to check.'))
            return
        row = dict(self._rows[index])
        try:
            catalogue.precheck_row_on_session(lambda *a, **k: {}, row)
        except catalogue.StructureRefusal as error:
            self._say(str(error))
            return
        self._run('precheck', lambda call: catalogue.precheck_row_on_session(
            call, row))

    def make_structured(self, apply: bool = True) -> None:
        """Which volumes can be structured; with *apply*, set them."""
        dim = 3
        tags = ()
        self._keepAutomatic = None
        index = self._table.currentRow()
        if 0 <= index < len(self._rows):
            row = self._rows[index]
            if row.get('kind') == 'transfiniteAutomatic':
                dim = int(row.get('dim', 3))
                tags = catalogue.parse_tags(row.get('tags'))
                params = row.get('params') or {}
                angle = float(params.get('corner_angle',
                                         catalogue.DEFAULT_CORNER_ANGLE))
                recombine = bool(params.get('recombine', True))
                self._run('structured' if apply else 'qualify',
                          lambda call: catalogue.make_structured_on_session(
                              call, dim, tags, angle, recombine, apply))
                return
        if apply:
            # DP-5738 (DP-4216): kept as a row once it has been set
            self._keepAutomatic = (dim, tags)
        self._run('structured' if apply else 'qualify',
                  lambda call: catalogue.make_structured_on_session(
                      call, dim, tags, apply=apply))

    def keep_structured(self, dim=3, tags=(),
                        corner_angle=None, recombine=True) -> dict:
        """DP-5738 (DP-4216). Keep "Make structured where possible" as the
        Automatic transfinite row it is a one-press form of.

        The press sets the constraints on the session's model only: a
        re-import (any changed stage input, such as switching the layers
        off) starts that model again and the batch run behind Export never
        sees it. The row is added to the table (saved with the page, like
        any row) unless one already asks for the same; the row is returned.
        """
        if corner_angle is None:
            corner_angle = catalogue.DEFAULT_CORNER_ANGLE
        rows, row, added = catalogue.with_automatic_row(
            self._rows, dim, tags, corner_angle, recombine,
            name=pa3_routes.next_name(self._rows, 'transfiniteAutomatic'))
        if added:
            self._rows.append(row)
            self._changed()
            self._table.setCurrentCell(len(self._rows) - 1, COLUMN_NAME)
        return row

    def hex_dominant(self) -> None:
        level = int(self._hexLevel.currentData())
        conformity = int(self._hexConformity.currentData())
        self._run('hex', lambda call: catalogue.hex_dominant_on_session(
            call, level, conformity))

    def _onFailed(self, message: str) -> None:
        self._worker = None
        self._say(message)

    def _onDone(self, what, result) -> None:
        self._worker = None
        keep, self._keepAutomatic = self._keepAutomatic, None
        if what == 'structured' and keep is not None:
            self.keep_structured(*keep)
        self._onShown(what, result)

    def _onShown(self, what, result) -> None:
        """Put a result in the panel: its picture, its colours, its line."""
        if what == 'preview':
            lines = [line for s in result.get('surfaces', [])
                     for line in s['lines']]
            self._preview.setLines(lines)
            self._preview.setVisible(bool(lines))
        elif what in ('qualify', 'structured'):
            self.qualificationChanged.emit(
                {int(k): v for k, v in result.get('colours', {}).items()})
        text = self.result_text(what, result)
        if text is not None:
            self._say(text)

    def result_text(self, what, result):
        """The status line a result gives (``None``: it gives none)."""
        if what == 'apply':
            return self.tr('Applied %1 to the model.').replace(
                '%1', count_text(int(result.get('count', 0)),
                                 self.tr('row')))
        if what == 'preview':
            skipped = '; '.join(f"surface {s['tag']}: {s['reason']}"
                                for s in result.get('skipped', []))
            return (count_text(len(result.get('surfaces', [])),
                               self.tr('surface')) + self.tr(' drawn.')
                    + (' ' + skipped if skipped else ''))
        if what == 'measure':
            return ('\n'.join(f"{m['name']}: {m['note']}" for m in result)
                    or self.tr('Nothing to measure.'))
        if what == 'precheck':
            lines = []
            for item in result.get('entities', []):
                word = self.tr('surface') if item['dim'] == 2 \
                    else self.tr('volume')
                lines.append('%s %d: %s' % (
                    word, item['tag'],
                    self.tr('can be transfinite') if item['qualifies']
                    else item['reason']))
            return '\n'.join(lines)
        if what in ('qualify', 'structured'):
            good = result.get('qualifies', [])
            lines = [self.tr('%1 can be structured (green): %2.')
                     .replace('%1', str(len(good)))
                     .replace('%2', ', '.join(str(t) for t in good) or '-')]
            for item in result.get('rejected', []):
                lines.append(self.tr('Not structured (red) %1: %2')
                             .replace('%1', str(item['tag']))
                             .replace('%2', item['reason']))
            if what == 'structured':
                lines.append(self.tr(
                    'Transfinite constraints set on the green ones; mesh to '
                    'see the hexahedra.') if result.get('applied')
                    else self.tr('Nothing could be structured.'))
            return '\n'.join(lines)
        if what == 'hex':
            counts = result.get('counts', {})
            text = self.tr(
                'Hex-dominant: %1 tetrahedra, %2 hexahedra, %3 prisms, %4 '
                'pyramids.').replace('%1', str(counts.get('tetrahedra', 0))
                                     ).replace('%2', str(counts.get(
                                         'hexahedra', 0))).replace(
                    '%3', str(counts.get('prisms', 0))).replace(
                    '%4', str(counts.get('pyramids', 0)))
            if result.get('note'):
                text += ' ' + result['note']
            return text
        return None

    # -- the Structured size grid -------------------------------------------- #

    def write_grid(self) -> dict | None:
        """Write the grid file next to the case and add the Structured field
        (a pending change of the size-field rows) that reads it."""
        try:
            bounds = self._bounds.modelValues()
        except ValueError as error:
            self._say(str(error))
            return None
        return self.write_grid_values(
            bounds, float(self._step.modelValue()),
            self._expression.text().strip(), self._text.isChecked())

    def write_grid_values(self, bounds, step, expression, text):
        """:meth:`write_grid` with the values given (model units)."""
        try:
            translate = None
            if self._provider is not None:
                try:
                    translate = self._provider().profile().translate_host_path
                except Exception:                        # noqa: BLE001
                    translate = None
            done = pa3_routes.write_grid_files(
                self._client.case_path, translate, list(bounds), step,
                expression, text)
        except (grids.GridRefusal, ValueError) as error:
            self._say(str(error))
            return None
        rows = self._add_structured_field(done['file_name'], done['text'])
        self._say(self.tr('Wrote %1 nodes to %2; the Structured field reads '
                          'it (save to keep it).')
                  .replace('%1', str(done.get('nodes', '')))
                  .replace('%2', done['file_name']))
        return {**done, 'rows': rows}

    def _add_structured_field(self, file_name: str, text: bool) -> list:
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
        rows = pa3_routes.structured_field_rows(rows, file_name, text)
        self._extra_patch[fields.FIELD_ID] = fields.rows_to_text(rows)
        self._dirty = True
        self.dirtyChanged.emit(True)
        return rows


class StructureApi:
    """What the local API does on the open Structure panel (Plan 48 PA3).

    One per panel, registered on the case session; the routes in
    ``core/facade/plan48/pa3_routes.py`` call it. ``rows`` is read from any
    thread; what changes the panel runs on the GUI thread (the signals of
    the panel's bridge queue to it).
    """

    def __init__(self, panel: StructurePanel):
        self._panel = panel

    def rows(self) -> list:
        return self._panel.rows()

    def keep_structured(self, dim=3, tags=(), corner_angle=None,
                        recombine=True) -> dict:
        """The row the panel keeps after Make structured (DP-5738)."""
        return self._panel.keep_structured(dim, tags, corner_angle, recombine)

    def show_result(self, what: str, result) -> str:
        """The panel shows a result as after its own button; the status
        line it gives comes back."""
        self._panel._bridge.shown.emit(what, result)      # noqa: SLF001
        return self._panel.result_text(what, result)

    def show_failure(self, message: str) -> None:
        self._panel._bridge.said.emit(str(message))       # noqa: SLF001

    def clear_qualification(self) -> None:
        """Take the green and red colours down."""
        self._panel.qualificationChanged.emit({})

    def write_grid(self, bounds, step, expression, text) -> dict:
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread
        done = on_gui_thread(self._panel.write_grid_values, bounds, step,
                             expression, text)
        if done is None:
            raise ValueError(self._panel.status() or 'the grid was not written')
        return done
