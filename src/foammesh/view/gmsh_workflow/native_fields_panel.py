"""Plan 41 GN6: the size fields of Gmsh, as rows, a graph and a preview.

The panel edits ``gmsh.nativeFields.rows`` (``core/gmsh/field_catalogue``).
Every parameter of every type is a control generated from the catalogue, so
the panel knows no field by name.  A row is checked by the catalogue as it is
edited, and the reason a graph cannot be built is shown beside the rows, not
after a run.

Adoptable by a task page (``pending_patch``/``dirtyChanged``/``reload``).
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
    QWidget)

from foammesh.core.gmsh import field_catalogue as catalogue
from foammesh.core.quantities import count_text
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

    def __init__(self, client, controller=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._controller = controller
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
        layout.addLayout(preview)
        self._previewView = PreviewView(self)
        self._previewView.setVisible(False)
        layout.addWidget(self._previewView)
        self.previewReady.connect(self._show_preview)
        self.reload()

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
        values = self._read(('gmsh.algorithms.surface',
                             'gmsh.algorithms.volume'))
        return (str(values.get('gmsh.algorithms.surface') or ''),
                str(values.get('gmsh.algorithms.volume') or ''))

    def _read_stored(self) -> list:
        try:
            return catalogue.rows_from_text(
                self._read((catalogue.FIELD_ID,)).get(catalogue.FIELD_ID))
        except catalogue.FieldRefusal as error:
            self._show_problem(str(error))
            return []

    # -- editing ----------------------------------------------------------- #

    def _fill_types(self) -> None:
        shown = catalogue.visible_types(*self._algorithms)
        self._type.blockSignals(True)
        self._type.clear()
        for kind in shown:
            self._type.addItem(kind.title, kind.gmsh)
        self._type.blockSignals(False)

    def visible_type_names(self) -> tuple:
        return tuple(self._type.itemData(i) for i in range(self._type.count()))

    def _changed(self) -> None:
        self._dirty = True
        self.dirtyChanged.emit(True)
        self._fill_table()
        self._check()

    def add_row(self, gmsh_type: str | None = None) -> dict:
        gmsh_type = gmsh_type or self._type.currentData()
        base = gmsh_type[0].lower() + gmsh_type[1:]
        names = {r['name'] for r in self._rows}
        number = 1
        while f'{base}{number}' in names:
            number += 1
        row = catalogue.new_row(gmsh_type, f'{base}{number}')
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
        for row in self._rows:
            for key, value in list(row['params'].items()):
                if value == gone:
                    del row['params'][key]
                elif isinstance(value, list) and gone in value:
                    row['params'][key] = [v for v in value if v != gone]
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
            unsupported = kind.refusal
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
                for other in self._rows:
                    for key, value in other['params'].items():
                        if value == old:
                            other['params'][key] = new
                        elif isinstance(value, list) and old in value:
                            other['params'][key] = [new if v == old else v
                                                    for v in value]
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
        for option in kind.options:
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
            if option.default is None:
                box.setUnsetDefault(self.tr('Not set'))
            else:
                box.setUnsetDefault(self.tr('Default'), option.default)
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
        return edit

    def _set(self, row, option, raw) -> None:
        """Store what was typed; an empty control means "Gmsh's default"."""
        params = row['params']
        before = params.get(option.name)
        if option.kind in ('entities', 'fields'):
            parts = [p.strip() for p in str(raw).split(',') if p.strip()]
            value = ([int(p) if p.lstrip('-').isdigit() else p
                      for p in parts] if option.kind == 'entities' else parts)
            value = value or None
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

    def preview(self, dim: int = 2) -> None:
        """Ask the resident session for the sizes the field gives."""
        reason = self.check()
        if reason:
            self._previewNote.setText(reason)
            return
        controller = self._controller() if callable(self._controller) \
            else self._controller
        if controller is None:
            self._previewNote.setText(self.tr('Open a project first.'))
            return
        if self._dirty:
            self._previewNote.setText(
                self.tr('Press Update first: the preview reads the saved '
                        'size fields.'))
            return
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
