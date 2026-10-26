"""Plan 41 GN6: the size fields of Gmsh, as rows, a graph and a preview.

The panel edits ``gmsh.nativeFields.rows`` (``core/gmsh/field_catalogue``).
Every parameter of every type is a control generated from the catalogue, so
the panel knows no field by name.  A row is checked by the catalogue as it is
edited, and the reason a graph cannot be built is shown beside the rows, not
after a run.

Adoptable by a task page (``pending_patch``/``dirtyChanged``/``reload``).

Plan 48 GM8 adds: every field type of the build (the anisotropic ones are
listed and greyed, with the one note the user allowed, until BAMG or MMG3D is
chosen), lists of numbers and file choosers (the file is given to Gmsh by its
name inside WSL), the Delta default of the model (``lc / 1e4``), size data
loaded from a ``.pos`` / ``.msh`` file (``SizeDataDialog``), and a button that
shows ONE field on the model in the viewport on the shared scale.
"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QHeaderView, QWidget)

from foammesh.core.facade.plan48 import pa3_routes
from foammesh.core.gmsh import field_catalogue as catalogue
from foammesh.core.quantities import count_text
from foammesh.view.combo_items import item_enabled, set_item_enabled
from foammesh.view.workflow_controls.length_box import (
    ModelLengthBox, format_model_units, length_cell,
)
from widgets.fit_to_text import FlowLayout

COLUMN_NAME, COLUMN_TYPE, COLUMN_BACKGROUND, COLUMN_ON = range(4)


class FieldGraphView(QWidget):
    """The field graph: one box per field, an arrow from a field to what it
    reads.  The positions come from ``catalogue.graph_layout`` (tested)."""

    BOX_WIDTH = 104
    BOX_HEIGHT = 26
    GAP_X = 34
    GAP_Y = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('gmshFieldGraph')
        self.setAccessibleName(self.tr('Map of the size fields and their links'))
        self._rows: list = []
        self._layout: dict = {'nodes': {}, 'edges': [], 'columns': 0}
        self.setMinimumHeight(60)

    def setRows(self, rows) -> None:
        self._rows = [dict(r) for r in rows]
        self._layout = catalogue.graph_layout(self._rows)
        depth = max([row for _c, row in self._layout['nodes'].values()],
                    default=-1) + 1
        self.setMinimumHeight(max(60, depth * (self.BOX_HEIGHT + self.GAP_Y)
                                  + self.GAP_Y))
        self.setAccessibleDescription(self.description())
        self.update()

    def layoutData(self) -> dict:
        return self._layout

    def description(self) -> str:
        edges = self._layout['edges']
        if not self._rows:
            return self.tr('No size fields.')
        return (count_text(len(self._rows), self.tr('size field')) + ', '
                + count_text(len(edges), self.tr('link')) + '.')

    def _box(self, name) -> QRectF:
        column, row = self._layout['nodes'][name]
        return QRectF(self.GAP_Y + column * (self.BOX_WIDTH + self.GAP_X),
                      self.GAP_Y + row * (self.BOX_HEIGHT + self.GAP_Y),
                      self.BOX_WIDTH, self.BOX_HEIGHT)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        columns = max(1, self._layout['columns'])
        return QSize(columns * (self.BOX_WIDTH + self.GAP_X) + self.GAP_Y,
                     self.minimumHeight())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        text = palette.color(palette.ColorRole.WindowText)
        fill = palette.color(palette.ColorRole.Base)
        painter.setPen(QPen(text, 1))
        by_name = {str(r.get('name')): r for r in self._rows}
        for reader, read in self._layout['edges']:
            a, b = self._box(read), self._box(reader)
            start = QPointF(a.right(), a.center().y())
            end = QPointF(b.left(), b.center().y())
            painter.drawLine(start, end)
            painter.drawLine(end, QPointF(end.x() - 5, end.y() - 3))
            painter.drawLine(end, QPointF(end.x() - 5, end.y() + 3))
        for name in self._layout['nodes']:
            row = by_name.get(name, {})
            box = self._box(name)
            painter.setBrush(fill)
            pen = QPen(text, 2 if row.get('background') else 1)
            if not row.get('enabled', True):
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(QPen(text, 1))
            painter.drawText(box.adjusted(4, 0, -4, 0),
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(
                                 f'{name}', Qt.TextElideMode.ElideRight,
                                 int(box.width()) - 8))
        painter.end()


class PreviewView(QWidget):
    """The model's points where Gmsh asked for a size, coloured by the size.

    Drawn on the two axes the points spread over most; blue is the finest
    size and red the coarsest of the sample.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('gmshFieldPreview')
        self.setAccessibleName(self.tr('Size field preview'))
        self.setMinimumHeight(160)
        self._points: list = []
        self._sizes: list = []

    def setSamples(self, points, sizes) -> None:
        self._points = [tuple(p) for p in points]
        self._sizes = list(sizes)
        self.update()

    def samples(self) -> tuple:
        return self._points, self._sizes

    def axes(self) -> tuple:
        if not self._points:
            return 0, 1
        spans = [(max(p[i] for p in self._points)
                  - min(p[i] for p in self._points), i) for i in range(3)]
        spans.sort(reverse=True)
        return tuple(sorted((spans[0][1], spans[1][1])))

    def colour(self, size: float) -> QColor:
        low, high = min(self._sizes), max(self._sizes)
        t = 0.0 if high <= low else (size - low) / (high - low)
        return QColor.fromHsvF(0.66 * (1.0 - t), 0.85, 0.9)

    def paintEvent(self, _event) -> None:
        if not self._points:
            return
        painter = QPainter(self)
        a, b = self.axes()
        lo = [min(p[i] for p in self._points) for i in (a, b)]
        hi = [max(p[i] for p in self._points) for i in (a, b)]
        span = max(hi[0] - lo[0], hi[1] - lo[1], 1e-12)
        margin = 8
        scale = (min(self.width(), self.height()) - 2 * margin) / span
        for point, size in zip(self._points, self._sizes):
            painter.setPen(self.colour(size))
            painter.drawPoint(int(margin + (point[a] - lo[0]) * scale),
                              int(self.height() - margin
                                  - (point[b] - lo[1]) * scale))
        painter.end()


class NativeFieldsPanel(QWidget):
    """Edits ``gmsh.nativeFields.rows``; adoptable by a task page."""

    dirtyChanged = Signal(bool)
    previewReady = Signal(object, str)
    #: (result, error, field name): one field sampled for the viewport.
    visualizeReady = Signal(object, str, str)

    def __init__(self, client, controller=None, parent=None,
                 overlay_factory=None):
        super().__init__(parent)
        self._client = client
        self._controller = controller
        self._overlay_factory = overlay_factory
        self._overlay = None
        self._shown_field = ''
        self._diagonal = (None, False)
        self._embedded = False
        self._stored: list = []
        self._rows: list = []
        self._dirty = False
        self._filling = False
        self._editors: dict = {}
        self._algorithms = ('', '')
        self.setObjectName('gmshNativeFields')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._summary = QLabel(self)
        self._summary.setObjectName('gmshNativeFieldsSummary')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, 4, self)
        self._table.setObjectName('gmshNativeFieldsTable')
        self._table.setAccessibleName(self.tr('Size fields'))
        self._table.setHorizontalHeaderLabels(
            [self.tr('Name'), self.tr('Type'), self.tr('Background'),
             self.tr('On')])
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_NAME,
                                    QHeaderView.ResizeMode.Stretch)
        for column in (COLUMN_TYPE, COLUMN_BACKGROUND, COLUMN_ON):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(110)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(self._on_current)
        layout.addWidget(self._table)

        # DP-573: a row of controls wraps rather than standing wider than
        # the settings column (the compact column is 314 px).
        add = FlowLayout()
        self._type = QComboBox(self)
        self._type.setObjectName('gmshNativeFieldType')
        self._type.setAccessibleName(self.tr('Type of size field to add'))
        add.addWidget(self._type)
        self._add = QPushButton(self.tr('Add field'), self)
        self._add.setObjectName('gmshNativeFieldAdd')
        self._add.clicked.connect(lambda _c=False: self.add_row())
        add.addWidget(self._add)
        self._remove = QPushButton(self.tr('Remove'), self)
        self._remove.setObjectName('gmshNativeFieldRemove')
        self._remove.clicked.connect(lambda _c=False: self.remove_row())
        add.addWidget(self._remove)
        self._loadData = QPushButton(self.tr('Load size data…'), self)
        self._loadData.setObjectName('gmshNativeFieldLoadData')
        self._loadData.clicked.connect(lambda _c=False: self.load_dialog())
        add.addWidget(self._loadData)
        layout.addLayout(add)

        self._help = QLabel(self)
        self._help.setObjectName('gmshNativeFieldHelp')
        self._help.setWordWrap(True)
        layout.addWidget(self._help)

        self._form_host = QWidget(self)
        self._form_host.setObjectName('gmshNativeFieldForm')
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_host)

        self._problem = QLabel(self)
        self._problem.setObjectName('gmshNativeFieldProblem')
        self._problem.setWordWrap(True)
        self._problem.setProperty('foammeshStatus', 'error')
        self._problem.setVisible(False)
        layout.addWidget(self._problem)

        self._graph = FieldGraphView(self)
        layout.addWidget(self._graph)

        preview = QHBoxLayout()
        self._previewButton = QPushButton(self.tr('Preview field'), self)
        self._previewButton.setObjectName('gmshNativeFieldPreviewButton')
        self._previewButton.clicked.connect(lambda _c=False: self.preview())
        preview.addWidget(self._previewButton)
        self._previewNote = QLabel(self)
        self._previewNote.setObjectName('gmshNativeFieldPreviewNote')
        self._previewNote.setWordWrap(True)
        preview.addWidget(self._previewNote, 1)
        self._visualizeButton = QPushButton(self.tr('Show in viewport'), self)
        self._visualizeButton.setObjectName('gmshNativeFieldVisualizeButton')
        self._visualizeButton.clicked.connect(
            lambda _c=False: self.toggle_visualize())
        preview.addWidget(self._visualizeButton)
        layout.addLayout(preview)
        self._previewView = PreviewView(self)
        self._previewView.setVisible(False)
        layout.addWidget(self._previewView)
        self.previewReady.connect(self._show_preview)
        self.visualizeReady.connect(self._show_visualized)
        self.reload()
        # Plan 48 PA3: the local API drives this page as a person does.
        self._api = NativeFieldsApi(self)
        session = pa3_routes.announce(client, 'fields', self._api)
        if session is not None:
            self.destroyed.connect(
                lambda *_a, s=session, a=self._api: pa3_routes.unregister_panel(
                    s, 'fields', a))

    # -- the FieldGroupPage-shaped surface a task page adopts --------------- #

    def setEmbedded(self, embedded: bool = True) -> None:
        self._embedded = bool(embedded)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def rows(self) -> list:
        return [dict(r, params=dict(r.get('params') or {}))
                for r in self._rows]

    def pending_patch(self) -> dict:
        if not self._dirty:
            return {}
        return {catalogue.FIELD_ID: catalogue.rows_to_text(self._rows)}

    def reload(self, *, discard_pending: bool = True) -> None:
        if discard_pending:
            self._dirty = False
        self._algorithms = self._read_algorithms()
        self._stored = self._read_stored()
        if discard_pending:
            self._rows = [dict(r, params=dict(r.get('params') or {}))
                          for r in self._stored]
        self._fill_types()
        self._fill()
        self.dirtyChanged.emit(self._dirty)

    def revert(self) -> None:
        self.reload(discard_pending=True)

    def _read(self, ids) -> dict:
        try:
            values = self._client.field_values(tuple(ids))
        except Exception:                                # noqa: BLE001
            return {}
        return {k: getattr(v, 'value', v) for k, v in values.items()}

    def _read_algorithms(self) -> tuple:
        values = self._read(('gmsh.global_sizing.surface',
                             'gmsh.global_sizing.volume'))
        return (str(values.get('gmsh.global_sizing.surface') or ''),
                str(values.get('gmsh.global_sizing.volume') or ''))

    def _read_stored(self) -> list:
        try:
            return catalogue.rows_from_text(
                self._read((catalogue.FIELD_ID,)).get(catalogue.FIELD_ID))
        except catalogue.FieldRefusal as error:
            self._show_problem(str(error))
            return []

    # -- editing ----------------------------------------------------------- #

    def _fill_types(self) -> None:
        """Every type, in the build's order (Q3). A type that will not run
        now (anisotropic without BAMG / MMG3D) is greyed with the reason."""
        self._type.blockSignals(True)
        self._type.clear()
        first = -1
        for index, (kind, usable, note) in enumerate(
                catalogue.listed_types(*self._algorithms)):
            self._type.addItem(kind.title, kind.gmsh)
            if usable:
                first = index if first < 0 else first
                continue
            # DP-5603: no QStandardItem wrapper (DP-1447, combo_items)
            set_item_enabled(self._type, index, False)
            self._type.setItemData(index, note[:1].upper() + note[1:] + '.',
                                   Qt.ItemDataRole.ToolTipRole)
        if first >= 0:
            self._type.setCurrentIndex(first)
        self._type.blockSignals(False)

    def visible_type_names(self) -> tuple:
        """The types that can be added now (the others are listed greyed)."""
        return tuple(name for name, usable in self.listed_types() if usable)

    def listed_types(self) -> tuple:
        """``(type, usable)`` for every entry of the type list."""
        found = []
        for i in range(self._type.count()):
            found.append((self._type.itemData(i),
                          item_enabled(self._type, i) is not False))
        return tuple(found)

    def type_note(self, gmsh_type: str) -> str:
        """The note shown beside a greyed type, or ''."""
        return catalogue.inactive_reason(gmsh_type, *self._algorithms)

    def _changed(self) -> None:
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._fill_table()
        self._check()

    def add_row(self, gmsh_type: str | None = None) -> dict:
        gmsh_type = gmsh_type or self._type.currentData()
        # Q3: a greyed type cannot be added; one already in the table stays.
        if gmsh_type in dict(self.listed_types()) and not dict(
                self.listed_types())[gmsh_type]:
            raise catalogue.FieldRefusal(
                catalogue.inactive_reason(gmsh_type, *self._algorithms))
        base = gmsh_type[0].lower() + gmsh_type[1:]
        row = catalogue.new_row(gmsh_type,
                                pa3_routes.next_name(self._rows, base))
        self._rows.append(row)
        self._changed()
        self._table.setCurrentCell(len(self._rows) - 1, COLUMN_NAME)
        return row

    def select_row(self, name: str) -> bool:
        """Show the row called ``name`` (M-20: the Automatic sizing shortcut)."""
        for index, row in enumerate(self._rows):
            if row['name'] == name:
                self._table.setCurrentCell(index, COLUMN_NAME)
                self._build_form(row)
                return True
        return False

    def remove_row(self) -> None:
        index = self._table.currentRow()
        if not 0 <= index < len(self._rows):
            return
        gone = self._rows.pop(index)['name']
        # A field that read the removed one no longer names it.
        pa3_routes.drop_references(self._rows, gone)
        self._changed()

    def _fill(self) -> None:
        self._fill_table()
        self._check()

    def _fill_table(self) -> None:
        self._filling = True
        current = self._table.currentRow()
        self._table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            kind = catalogue.TYPES[row['type']]
            name = QTableWidgetItem(row['name'])
            kindItem = QTableWidgetItem(kind.title)
            unsupported = catalogue.inactive_reason(kind, *self._algorithms)
            kindItem.setFlags(kindItem.flags() & ~Qt.ItemFlag.ItemIsEditable)
            back = QTableWidgetItem('')
            back.setFlags((back.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                          & ~Qt.ItemFlag.ItemIsEditable)
            back.setCheckState(Qt.CheckState.Checked if row.get('background')
                               else Qt.CheckState.Unchecked)
            back.setToolTip(self.tr(
                'Use this field as (part of) the background mesh size'))
            on = QTableWidgetItem('')
            on.setFlags((on.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable)
            on.setCheckState(Qt.CheckState.Checked
                             if row.get('enabled', True)
                             else Qt.CheckState.Unchecked)
            if unsupported:
                # M-20. Listed, its settings editable, but switched off for
                # the run on this Gmsh, with the reason in plain words.
                on.setFlags(on.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                for item in (name, kindItem, on):
                    item.setToolTip(unsupported[:1].upper()
                                    + unsupported[1:] + '.')
                kindItem.setText(kind.title + self.tr(' (not run here)'))
            for column, item in enumerate((name, kindItem, back, on)):
                self._table.setItem(index, column, item)
        self._filling = False
        if self._rows:
            chosen = min(max(current, 0), len(self._rows) - 1)
            self._table.setCurrentCell(chosen, COLUMN_NAME)
            self._build_form(self._rows[chosen])
        else:
            self._build_form(None)
        self._graph.setRows(self._rows)
        hidden = sum(1 for r in self._rows if not r.get('enabled', True))
        unsupported = sum(1 for r in self._rows
                          if catalogue.TYPES[r['type']].refusal)
        self._summary.setText(
            count_text(len(self._rows), self.tr('size field'))
            + (self.tr(', %1 switched off').replace('%1', str(hidden))
               if hidden else '')
            + (self.tr(', %1 not run by this Gmsh').replace(
                '%1', str(unsupported)) if unsupported else ''))

    def _on_item_changed(self, item) -> None:
        if self._filling:
            return
        row = self._rows[item.row()]
        if item.column() == COLUMN_NAME:
            old, new = row['name'], item.text().strip()
            if new and new != old:
                row['name'] = new
                pa3_routes.rename_references(self._rows, old, new)
        elif item.column() == COLUMN_BACKGROUND:
            row['background'] = item.checkState() == Qt.CheckState.Checked
        elif item.column() == COLUMN_ON:
            row['enabled'] = item.checkState() == Qt.CheckState.Checked
        self._changed()

    def _on_current(self, row, *_args) -> None:
        if self._filling:
            return
        self._build_form(self._rows[row] if 0 <= row < len(self._rows)
                         else None)

    # -- the parameter form, from the catalogue ----------------------------- #

    def _build_form(self, row) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._editors = {}
        if row is None:
            self._help.setText('')
            return
        kind = catalogue.TYPES[row['type']]
        self._help.setText(kind.help + (
            ' ' + kind.refusal if kind.refusal else ''))
        others = [r['name'] for r in self._rows if r is not row]
        if row.get('data'):
            self._form.addRow(self.tr('Data'), self._data_cell(row))
        for option in kind.shown_options:
            if row.get('data') and option.name in ('ViewTag', 'ViewIndex'):
                continue
            editor = self._editor(row, option, others)
            length = option.kind == 'float' and option.unit == 'm'
            label = option.name + (
                f' ({option.unit})' if option.unit and not length else '')
            if option.required:
                label += ' *'
            editor.setToolTip(option.help)
            editor.setAccessibleName(f'{row["name"]} {option.name}')
            if isinstance(editor, ModelLengthBox):
                # UXB3c: the unit sits in a column of its own (DP-164).
                self._form.addRow(label, length_cell(editor, self))
            else:
                self._form.addRow(label, editor)
            self._editors[option.name] = editor

    def _editor(self, row, option, others):
        value = row['params'].get(option.name)
        if option.kind == 'bool':
            box = QCheckBox(self)
            box.setChecked(bool(option.default if value is None else value))
            box.toggled.connect(
                lambda state, o=option, r=row: self._set(r, o, bool(state)))
            return box
        if option.kind == 'enum':
            combo = QComboBox(self)
            for choice in option.choices:
                combo.addItem(str(choice[1]), choice[0])
            current = option.default if value is None else value
            index = combo.findData(current)
            combo.setCurrentIndex(max(index, 0))
            combo.currentIndexChanged.connect(
                lambda _i, o=option, r=row, c=combo: self._set(
                    r, o, c.currentData()))
            return combo
        if option.kind == 'field':
            combo = QComboBox(self)
            combo.addItem('', '')
            for name in others:
                combo.addItem(name, name)
            combo.setCurrentIndex(max(combo.findData(value or ''), 0))
            combo.currentIndexChanged.connect(
                lambda _i, o=option, r=row, c=combo: self._set(
                    r, o, c.currentData()))
            return combo
        if option.kind == 'float' and option.unit == 'm' and (
                value is None or isinstance(value, (int, float))
                and not isinstance(value, bool)):
            # UXB3c: a length is typed in the project display unit; the
            # stored row keeps model units (metres) exactly as before.
            box = ModelLengthBox(self, minimum=None)
            default = catalogue.default_for(option, self.model_diagonal())
            if default is None:
                box.setUnsetDefault(self.tr('Not set'))
            else:
                box.setUnsetDefault(self.tr('Default'), default)
            box.setKeyboardTracking(False)
            box.setModelValue(None if value is None else float(value))
            box.valueChanged.connect(
                lambda _v, o=option, r=row, b=box: self._set(
                    r, o, '' if b.modelValue() is None
                    else format_model_units(b.modelValue())))
            return box
        edit = QLineEdit(self)
        if value is not None:
            edit.setText(', '.join(str(v) for v in value)
                         if isinstance(value, list) else str(value))
        elif option.default is not None:
            edit.setPlaceholderText(str(option.default))
        edit.editingFinished.connect(
            lambda o=option, r=row, e=edit: self._set(r, o, e.text()))
        if option.kind in ('path', 'file'):
            return self._path_cell(edit, row, option)
        return edit

    def _path_cell(self, edit, row, option):
        """A path box with a Browse button; the file is chosen on this
        computer and given to Gmsh by its name inside WSL (at the build)."""
        cell = QWidget(self)
        box = QHBoxLayout(cell)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(edit, 1)
        browse = QPushButton(self.tr('Browse…'), cell)
        browse.setObjectName('gmshNativeFieldBrowse_' + option.name)
        browse.clicked.connect(lambda _c=False: self._browse(edit, row, option))
        box.addWidget(browse)
        cell.edit = edit
        return cell

    def _browse(self, edit, row, option) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, self.tr('Choose a file'), edit.text() or '')
        if chosen:
            edit.setText(chosen)
            self._set(row, option, chosen)
            self._build_form(row)

    def _set(self, row, option, raw) -> None:
        """Store what was typed; an empty control means "Gmsh's default"."""
        params = row['params']
        before = params.get(option.name)
        if option.kind in ('entities', 'fields'):
            parts = [p.strip() for p in str(raw).split(',') if p.strip()]
            value = ([int(p) if p.lstrip('-').isdigit() else p
                      for p in parts] if option.kind == 'entities' else parts)
            value = value or None
        elif option.kind in ('reals', 'ints'):
            parts = [p.strip() for p in str(raw).replace(';', ',').replace(
                ' ', ',').split(',') if p.strip()]
            try:
                numbers = [float(p) for p in parts]
                value = ([int(n) if n == int(n) else n for n in numbers]
                         if option.kind == 'ints' else numbers) or None
            except ValueError:
                value = str(raw)          # kept as typed; validate refuses it
        elif option.kind in ('float', 'int'):
            text = str(raw).strip()
            if not text:
                value = None
            else:
                try:
                    value = (int(text) if option.kind == 'int'
                             else float(text))
                except ValueError:
                    value = text          # kept as typed; validate refuses it
        elif option.kind == 'bool':
            value = bool(raw)
        else:
            value = (str(raw) if str(raw).strip() != '' else None)
            if option.kind == 'field' and not raw:
                value = None
        if value is None:
            params.pop(option.name, None)
        else:
            params[option.name] = value
        if params.get(option.name) != before:
            self._dirty = True
            self.dirtyChanged.emit(True)
            self._graph.setRows(self._rows)
            self._check()

    # -- checking and previewing -------------------------------------------- #

    def check(self) -> str:
        """The reason the graph cannot be built, or ''."""
        try:
            catalogue.validate(self._rows,
                               surface_algorithm=self._algorithms[0],
                               volume_algorithm=self._algorithms[1])
        except catalogue.FieldRefusal as error:
            return str(error)
        return ''

    def _check(self) -> None:
        reason = self.check()
        self._show_problem(reason)

    def _show_problem(self, text: str) -> None:
        self._problem.setText(text)
        self._problem.setVisible(bool(text))

    def preview_refusal(self) -> str:
        """Why the preview cannot run now, or ''."""
        reason = self.check()
        if reason:
            return reason
        if self._controller_object() is None:
            return self.tr('Open a project first.')
        if self._dirty:
            return self.tr('Press Update first: the preview reads the saved '
                           'size fields.')
        return ''

    def preview(self, dim: int = 2) -> None:
        """Ask the resident session for the sizes the field gives."""
        reason = self.preview_refusal()
        if reason:
            self._previewNote.setText(reason)
            return
        controller = self._controller_object()
        self._previewButton.setEnabled(False)
        self._previewNote.setText(self.tr('Sampling the field…'))

        def work():
            try:
                self.previewReady.emit(controller.preview_fields(dim=dim), '')
            except Exception as error:                   # noqa: BLE001
                self.previewReady.emit(None, str(error))

        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, result, error: str) -> None:
        self._previewButton.setEnabled(True)
        if error or result is None:
            self._previewNote.setText(error or self.tr('No sizes returned.'))
            return
        if result.get('error'):
            self._previewNote.setText(str(result['error']))
            return
        sizes = result.get('sizes') or []
        self._previewView.setSamples(result.get('points') or [], sizes)
        self._previewView.setVisible(bool(sizes))
        if sizes:
            self._previewNote.setText(self.tr(
                '%1 of %2 asked; sizes %3 to %4.')
                .replace('%1', count_text(len(sizes), self.tr('sample')))
                .replace('%2', str(result.get('asked', len(sizes))))
                .replace('%3', f'{min(sizes):.4g}')
                .replace('%4', f'{max(sizes):.4g}'))
        else:
            self._previewNote.setText(self.tr(
                'The fields give no size anywhere on the model.'))
    # -- size data from a .pos or .msh file (Plan 48 GM8, D6) --------------- #

    def case_path(self):
        return getattr(self._client, 'case_path', None)

    def model_diagonal(self):
        """The model's diagonal in metres (Delta defaults to 1/10000 of it)."""
        value, known = self._diagonal
        if not known:
            case = self.case_path()
            value = catalogue.model_diagonal(case) if case else None
            self._diagonal = (value, True)
        return value

    def _controller_object(self):
        return self._controller() if callable(self._controller) \
            else self._controller

    def scan_data(self, path) -> dict:
        """The datasets of a data file; a binary file is read by Gmsh."""
        scan = catalogue.scan_size_data(path)
        if scan.get('binary'):
            session = getattr(self._controller_object(), '_session', None)
            if session is None:
                scan['error'] = ('a binary file is read by the Gmsh session; '
                                 'open a project first')
                return scan
            try:
                got = session.call('session.size_data_scan',
                                   path=catalogue.runtime_path(path))
                scan['datasets'] = got.get('datasets') or []
                scan['error'] = '' if scan['datasets'] else \
                    'the file holds no data view'
            except Exception as error:                   # noqa: BLE001
                scan['error'] = str(error)
        return scan

    def load_size_data(self, path, dataset=None, step: int = 0,
                       unit: str = 'm', mode: str = 'auto',
                       name: str = '') -> dict:
        """Copy the file into the project and add a PostView field reading
        the chosen dataset, step and unit. Returns the new row."""
        case = self.case_path()
        if not case:
            raise catalogue.FieldRefusal(
                'open a project first; the data is kept inside it')
        scan = self.scan_data(path)
        datasets = scan.get('datasets') or []
        if not datasets:
            raise catalogue.FieldRefusal(scan.get('error') or
                                         'the file holds no data view')
        chosen = None
        for item in datasets:
            if dataset is None and item.get('scalar'):
                chosen = item
            elif dataset is not None and dataset in (item['index'],
                                                     item['name']):
                chosen = item
            if chosen is not None:
                break
        if chosen is None:
            raise catalogue.FieldRefusal(
                f'the file has no dataset {dataset!r}; it has '
                + ', '.join(repr(d['name']) for d in datasets))
        copy = catalogue.copy_size_data(path, case)
        ref = catalogue.make_size_data_ref(copy, chosen, step=step, unit=unit,
                                           mode=mode, name=name)
        self._rows, row = catalogue.add_size_data_row(self._rows, ref, name)
        self._changed()
        self.select_row(row['name'])
        return row

    def set_data_options(self, name: str, **changes) -> dict:
        """Change the unit, mode or time step of loaded data."""
        row = next((r for r in self._rows if r['name'] == name), None)
        if row is None or not row.get('data'):
            raise catalogue.FieldRefusal(f'{name!r} has no loaded size data')
        ref = dict(row['data'])
        for key, value in changes.items():
            if key not in ('unit', 'mode', 'step'):
                raise catalogue.FieldRefusal(
                    f'size data has no setting {key!r}')
            ref[key] = value
        catalogue.size_data_node(ref, name, strict=False)
        row['data'] = ref
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._graph.setRows(self._rows)
        self._check()
        return ref

    def remove_data(self, name: str) -> None:
        """Take loaded data away; refused while another field reads it."""
        self._rows = catalogue.remove_size_data(self._rows, name)
        self._changed()

    def _data_cell(self, row):
        ref = row['data']
        cell = QWidget(self)
        box = QHBoxLayout(cell)
        box.setContentsMargins(0, 0, 0, 0)
        label = QLabel(
            f"{ref.get('source') or os.path.basename(ref.get('file', ''))} "
            f"[{ref.get('dataset', '')}]", cell)
        label.setObjectName('gmshNativeFieldDataLabel')
        label.setToolTip(str(ref.get('file', '')))
        box.addWidget(label, 1)
        unit = QComboBox(cell)
        unit.setObjectName('gmshNativeFieldDataUnit')
        unit.setAccessibleName(self.tr('Unit of the data'))
        for key in catalogue.SIZE_DATA_UNITS:
            unit.addItem(key, key)
        unit.setCurrentIndex(max(unit.findData(ref.get('unit', 'm')), 0))
        unit.currentIndexChanged.connect(
            lambda _i, n=row['name'], c=unit: self.set_data_options(
                n, unit=c.currentData()))
        box.addWidget(unit)
        mode = QComboBox(cell)
        mode.setObjectName('gmshNativeFieldDataMode')
        mode.setAccessibleName(self.tr('How the data is kept'))
        for key, text in (('auto', self.tr('Automatic')),
                          ('list', self.tr('List')),
                          ('hidden', self.tr('Hidden model'))):
            mode.addItem(text, key)
        mode.setCurrentIndex(max(mode.findData(ref.get('mode', 'auto')), 0))
        mode.currentIndexChanged.connect(
            lambda _i, n=row['name'], c=mode: self.set_data_options(
                n, mode=c.currentData()))
        box.addWidget(mode)
        gone = QPushButton(self.tr('Remove data'), cell)
        gone.setObjectName('gmshNativeFieldDataRemove')
        gone.clicked.connect(lambda _c=False, n=row['name']: self._drop(n))
        box.addWidget(gone)
        return cell

    def _drop(self, name: str) -> None:
        try:
            self.remove_data(name)
        except catalogue.FieldRefusal as error:
            self._show_problem(str(error))

    def load_dialog(self, path: str = '') -> bool:
        """Ask for the file, dataset, step and unit; add the field.

        Plan 41 GN18 (Plan 48 UF, DP-5651): the Gmsh pages hold no nested
        event loop, so the dialog is opened and the field is added when it
        is accepted, not when a blocking ``exec`` returns.
        """
        dialog = SizeDataDialog(self, path)
        self._sizeDataDialog = dialog

        def accepted():
            try:
                self.load_size_data(**dialog.choice())
            except catalogue.FieldRefusal as error:
                self._show_problem(str(error))

        dialog.accepted.connect(accepted)
        dialog.open()
        return True

    # -- one field in the viewport (Plan 48 GM8) ---------------------------- #

    def overlay(self):
        if self._overlay is None:
            if self._overlay_factory is not None:
                self._overlay = self._overlay_factory()
            else:
                try:
                    from foammesh.app import app
                    from foammesh.rendering.size_field_overlay import (
                        SizeFieldOverlay)
                    display = app.window._displayControl
                    tokens = (app.themeManager.tokens
                              if app.themeManager is not None else None)
                    self._overlay = SizeFieldOverlay(display, tokens)
                except Exception:                        # noqa: BLE001
                    return None
        return self._overlay

    def overlay_refusal(self, name: str) -> str:
        """Why one field cannot be shown in the viewport now, or ''."""
        return self.visualize_reason(name) or (
            '' if self._controller_object() is not None
            else self.tr('Open a project first.'))

    def visualize_reason(self, name: str) -> str:
        """Why this field cannot be shown alone, or ''."""
        row = next((r for r in self._rows if r['name'] == name), None)
        if row is None:
            return self.tr('Choose a size field first.')
        kind = catalogue.TYPES[row['type']]
        if kind.gmsh == 'BoundaryLayer' or kind.anisotropic:
            return self.tr('A %1 field has no single size to show.').replace(
                '%1', kind.gmsh)
        if not row.get('enabled', True):
            return self.tr('The field is switched off.')
        if self._dirty:
            return self.tr('Press Update first: the viewport shows the saved '
                           'size fields.')
        return catalogue.inactive_reason(kind, *self._algorithms)

    def current_name(self) -> str:
        index = self._table.currentRow()
        return self._rows[index]['name'] if 0 <= index < len(self._rows) \
            else ''

    def toggle_visualize(self) -> None:
        if self._shown_field:
            self.hide_visualized()
        else:
            self.visualize()

    def hide_visualized(self) -> None:
        overlay = self.overlay()
        if overlay is not None:
            overlay.clear_field()
        self._shown_field = ''
        self._visualizeButton.setText(self.tr('Show in viewport'))

    def visualize(self, name: str | None = None, dim: int = 2) -> None:
        """Sample ONE field on the model and draw it in the viewport."""
        name = name or self.current_name()
        reason = self.overlay_refusal(name)
        if reason:
            self._previewNote.setText(reason)
            return
        controller = self._controller_object()
        self._visualizeButton.setEnabled(False)
        self._previewNote.setText(self.tr('Sampling the field…'))

        def work():
            try:
                self.visualizeReady.emit(
                    controller.sample_field(name, dim=dim), '', name)
            except Exception as error:                   # noqa: BLE001
                self.visualizeReady.emit(None, str(error), name)

        threading.Thread(target=work, daemon=True).start()

    def _show_visualized(self, result, error: str, name: str) -> None:
        self._visualizeButton.setEnabled(True)
        if error or result is None or result.get('error'):
            self._previewNote.setText(
                error or str((result or {}).get('error') or
                             self.tr('No sizes returned.')))
            return
        sizes = result.get('sizes') or []
        overlay = self.overlay()
        drawn = 0
        if overlay is not None and sizes:
            drawn = overlay.show_samples(result.get('points') or [], sizes,
                                         title=name)
        if drawn:
            self._shown_field = name
            self._visualizeButton.setText(self.tr('Hide from viewport'))
            self._previewNote.setText(self.tr('%1: sizes %2 to %3.')
                                      .replace('%1', name)
                                      .replace('%2', f'{min(sizes):.4g}')
                                      .replace('%3', f'{max(sizes):.4g}'))
        else:
            self._previewNote.setText(self.tr(
                'The field gives no size anywhere on the model.'))


class SizeDataDialog(QDialog):
    """Pick a data file, one of its datasets, a time step and the unit."""

    def __init__(self, panel, path: str = '', parent=None):
        super().__init__(parent or panel)
        self.setObjectName('gmshSizeDataDialog')
        self.setWindowTitle(self.tr('Load size data'))
        self._panel = panel
        self._scan: dict = {'datasets': []}
        form = QFormLayout(self)
        row = QHBoxLayout()
        self._path = QLineEdit(self)
        self._path.setObjectName('gmshSizeDataPath')
        self._path.setAccessibleName(self.tr('Data file'))
        self._path.editingFinished.connect(self.rescan)
        row.addWidget(self._path, 1)
        browse = QPushButton(self.tr('Browse…'), self)
        browse.setObjectName('gmshSizeDataBrowse')
        browse.clicked.connect(lambda _c=False: self._browse())
        row.addWidget(browse)
        form.addRow(self.tr('File'), row)
        self._dataset = QComboBox(self)
        self._dataset.setObjectName('gmshSizeDataDataset')
        self._dataset.setAccessibleName(self.tr('Dataset'))
        self._dataset.currentIndexChanged.connect(self._fill_steps)
        form.addRow(self.tr('Dataset'), self._dataset)
        self._step = QComboBox(self)
        self._step.setObjectName('gmshSizeDataStep')
        self._step.setAccessibleName(self.tr('Time step'))
        form.addRow(self.tr('Step'), self._step)
        self._unit = QComboBox(self)
        self._unit.setObjectName('gmshSizeDataUnit')
        self._unit.setAccessibleName(self.tr('Unit of the data'))
        for key in catalogue.SIZE_DATA_UNITS:
            self._unit.addItem(key, key)
        form.addRow(self.tr('Unit'), self._unit)
        self._problem = QLabel(self)
        self._problem.setObjectName('gmshSizeDataProblem')
        self._problem.setWordWrap(True)
        self._problem.setProperty('foammeshStatus', 'error')
        form.addRow(self._problem)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        if path:
            self.set_path(path)

    def _browse(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, self.tr('Choose a data file'), self._path.text() or '',
            self.tr('Size data (*.pos *.msh)'))
        if chosen:
            self.set_path(chosen)

    def set_path(self, path: str) -> None:
        self._path.setText(path)
        self.rescan()

    def rescan(self) -> None:
        path = self._path.text().strip()
        self._dataset.clear()
        self._step.clear()
        self._scan = {'datasets': []}
        if path:
            self._scan = self._panel.scan_data(path)
        for item in self._scan.get('datasets') or []:
            if item.get('scalar'):
                self._dataset.addItem(item['name'], item)
        self._problem.setText(self._scan.get('error') or '')
        self._ok.setEnabled(self._dataset.count() > 0)
        self._fill_steps()

    def _fill_steps(self, *_args) -> None:
        self._step.clear()
        item = self._dataset.currentData()
        for step in (item or {}).get('steps') or []:
            self._step.addItem(str(step['index']), step['index'])

    def choice(self) -> dict:
        item = self._dataset.currentData() or {}
        return {'path': self._path.text().strip(),
                'dataset': item.get('index'),
                'step': int(self._step.currentData() or 0),
                'unit': self._unit.currentData()}


class NativeFieldsApi:
    """What the local API does on the open Fields page (Plan 48 PA3).

    One per panel, registered on the case session; the routes in
    ``core/facade/plan48/pa3_routes.py`` call it, so the page and the API run
    the same code. Every method runs on the GUI thread.
    """

    def __init__(self, panel: NativeFieldsPanel):
        self._panel = panel

    @staticmethod
    def _gui(function, *args):
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread
        return on_gui_thread(function, *args)

    def state(self) -> dict:
        def read():
            panel = self._panel
            return {'current': panel.current_name(), 'rows': panel.rows(),
                    'dirty': panel.is_dirty, 'problem': panel.check(),
                    'summary': panel._summary.text(),
                    'shown_field': panel._shown_field,
                    'preview_note': panel._previewNote.text(),
                    'graph': panel._graph.description()}
        return self._gui(read)

    def select_row(self, name: str) -> bool:
        return bool(self._gui(self._panel.select_row, name))

    def preview_refusal(self) -> str:
        return self._gui(self._panel.preview_refusal)

    def show_preview(self, result, error: str) -> str:
        """The page shows an all-fields preview; its read-out comes back."""
        def show():
            self._panel._show_preview(result, error)
            return self._panel._previewNote.text()
        return self._gui(show)

    def overlay_refusal(self, name: str) -> str:
        return self._gui(self._panel.overlay_refusal, name)

    def show_overlay(self, result, error: str, name: str) -> dict:
        def show():
            self._panel._show_visualized(result, error, name)
            return {'shown_field': self._panel._shown_field,
                    'note': self._panel._previewNote.text()}
        return self._gui(show)

    def hide_overlay(self) -> dict:
        def hide():
            self._panel.hide_visualized()
            return {'shown_field': self._panel._shown_field}
        return self._gui(hide)
