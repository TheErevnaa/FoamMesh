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
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, CompactSpinBox
from foammesh.core.gmsh import entity_controls as catalogue
from foammesh.core.naming import humanise_option
from foammesh.core.quantities import count_text
from widgets.fit_to_text import FlowLayout

from .shared_pick import SharedPick

COLUMN_NAME, COLUMN_WHAT, COLUMN_ON = range(3)
DIM_WORDS = (('Point', 0), ('Curve', 1), ('Surface', 2), ('Volume', 3))
#: The pick filter that selects entities of each dimension.
DIM_FILTERS = {0: 'Points', 1: 'Curves', 2: 'Surfaces', 3: 'Volumes'}


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
        #: GM7. The Pick tool and the row it is filling.
        self._picker = None
        self._pick_row = None
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
        # Plan 48 PA2: the local API reaches this panel through the case
        # session, as it reaches the Gmsh tool dock.
        self._api = EntityControlsApi(self)
        self.registerApi()

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
        row = catalogue.new_row(kind, catalogue.next_row_name(kind,
                                                              self._rows))
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

    # -- the viewport picker (GM7) -------------------------------------------- #

    def set_picker(self, picker) -> None:
        """Take picks from *picker* (``pickFor(filter) -> bool``, the signal
        ``pickAccepted`` and ``status()``); by default the application's one
        Pick tool."""
        if self._picker is not None:
            try:
                self._picker.pickAccepted.disconnect(self._on_picked)
            except (RuntimeError, TypeError):
                pass
        self._picker = picker
        picker.pickAccepted.connect(self._on_picked)

    def _pick_tool(self):
        if self._picker is None:
            self.set_picker(SharedPick('gmsh.entity_controls', parent=self))
        return self._picker

    def request_pick(self, row_index: int | None = None) -> bool:
        """Pick the entities of a row in the viewport (the Pick button)."""
        index = self._table.currentRow() if row_index is None else row_index
        if not 0 <= index < len(self._rows):
            return False
        row = self._rows[index]
        kind = catalogue.KINDS[row['kind']]
        dim = row.get('dim')
        if dim not in kind.dims:
            dim = kind.dims[0]
        self._pick_row = (index, dim)
        tool = self._pick_tool()
        if not tool.pickFor(DIM_FILTERS[dim]):
            self._pick_row = None
            status = getattr(tool, 'status', lambda: '')()
            self._show_problem(status or self.tr('Nothing can be picked yet.'))
            return False
        self._show_problem('')
        return True

    def _on_picked(self, chosen) -> None:
        target, self._pick_row = self._pick_row, None
        if target is None:
            return
        index, dim = target
        tags = [int(entity[1]) for entity in chosen if int(entity[0]) == dim]
        if tags:
            self.set_tags(tags, index)

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
        host = QWidget(self)
        box = QHBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(tags, 1)
        pick = QPushButton(self.tr('Pick'), host)
        pick.setObjectName('gmshEntityControlPick')
        pick.setAccessibleName(self.tr('Pick the entities in the viewport'))
        pick.clicked.connect(
            lambda _c=False, r=row: self.request_pick(self._rows.index(r)))
        box.addWidget(pick)
        self._form.addRow(self.tr('Entity tags'), host)
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

    # -- the local API (Plan 48 PA2) ------------------------------------------ #

    def problemText(self) -> str:
        """The red line under the rows: why they cannot be applied ('' = none)."""
        return self._problem.text() if not self._problem.isHidden() else ''

    def helpText(self) -> str:
        return self._help.text()

    def summaryText(self) -> str:
        return self._summary.text()

    def currentRow(self) -> int:
        return self._table.currentRow()

    def api(self):
        return self._api

    def registerApi(self) -> bool:
        """Tell the case session this panel is in a window (idempotent)."""
        from foammesh.core.facade.plan48 import pa2_routes

        session = pa2_routes.case_session_of(self._client)
        if session is None:
            return False
        pa2_routes.register(session, pa2_routes.ENTITY_CONTROLS_ATTRIBUTE,
                            self._api)
        self._api_session = session
        self.destroyed.connect(
            lambda *_a, s=session, a=self._api: pa2_routes.unregister(
                s, pa2_routes.ENTITY_CONTROLS_ATTRIBUTE, a))
        return True


class EntityControlsApi:
    """What the Entity controls panel does for the local API.

    Methods run on the GUI thread and refuse with ``ValueError(reason)``;
    ``core/facade/plan48/pa2_routes.py`` calls them.
    """

    def __init__(self, panel):
        self._panel = panel

    def _gui(self, function, *args):
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread
        return on_gui_thread(function, *args)

    def state(self) -> dict:
        def read():
            panel = self._panel
            return {'rows': panel.rows(), 'dirty': panel.is_dirty,
                    'summary': panel.summaryText(),
                    'problem': panel.problemText(), 'help': panel.helpText(),
                    'current_row': panel.currentRow(),
                    'pending_text': catalogue.rows_to_text(panel.rows())}
        return self._gui(read)

    def check(self) -> dict:
        def read():
            reason = self._panel.check()
            return {'ok': not reason, 'reason': reason}
        return self._gui(read)

    def pick(self, row_index=None) -> dict:
        """The Pick button of a row: the entities arrive when the one Pick
        tool accepts (``gmsh.entities.pick``)."""
        def run():
            panel = self._panel
            started = panel.request_pick(row_index)
            if not started:
                raise ValueError(panel.problemText() or
                                 'there is no such control row to pick for')
            return {'started': True, 'row': (panel.currentRow()
                                             if row_index is None
                                             else int(row_index))}
        return self._gui(run)

