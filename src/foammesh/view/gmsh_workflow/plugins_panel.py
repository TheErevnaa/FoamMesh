"""Gmsh's Plugins window, as a dock on the Generate mesh page.

Plan 41 GN20 (T-02, P-02, M-16), Plan 48 GM9 (section 4.11).  Choose one of
the 31 mesh plugins, set its options (numbers, switches and text, each with
Gmsh's own default), give it an input view where it needs one, and run it on
the mesh in the session.  The input view can be any view of the session: a
plugin result, an Analyse mesh quality view, a size-data view.  Every view the
plugin makes is listed with its extremes and drawn in the viewport coloured by
its value; a result can be kept as the input of the next plugin (``CutPlane``
on a ``Distance`` view), hidden, removed, or written as a ``PostView`` size
field.  Nothing here depends on the target solver.

Extension point (GM16b, CVTRemesh).  A package that builds a plugin of its own
registers a ``plugin_catalogue.Plugin`` with ``plugin_catalogue.register_extra``
and, if it wants its own form instead of the generated one, a panel in
``view/gmsh_tools/registry.py`` with ``area=PLUGIN_PANEL_AREA`` and the id
``extra_panel_id(name)``.  This file is not edited for it: the choice list
follows the catalogue, and a chosen plugin with a registered panel shows that
panel in place of the generated form.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.view.combo_items import set_item_enabled
from foammesh.view.theming.metrics import CompactDoubleSpinBox, CompactSpinBox
from foammesh.view.workflow_controls.length_box import (
    ModelLengthBox, length_cell,
)
from foammesh.core.gmsh import field_catalogue, plugin_catalogue

COLUMNS = ('View', 'Name', 'Cells', 'Min', 'Average', 'Max', 'Shown')

#: The ``area`` a package gives a registry panel to have it shown as the form of
#: a plugin (see the module docstring).
PLUGIN_PANEL_AREA = 'plugins'


def extra_panel_id(name: str) -> str:
    """Registry id of the form for plugin *name* (``gmsh.plugins.cvtremesh``)."""
    return 'gmsh.plugins.' + str(name).lower()


def extra_panel_entry(name: str):
    """The registry entry that supplies the form of plugin *name*, or None."""
    from foammesh.view.gmsh_tools import registry

    wanted = extra_panel_id(name)
    for entry in registry.panels('gmsh'):
        if entry.id == wanted and entry.area == PLUGIN_PANEL_AREA:
            return entry
    return None


def _number(value) -> str:
    return '' if value is None else f'{float(value):.6g}'


class GmshPluginsPanel(QWidget):
    """Run Gmsh plugins on the session mesh and draw what they make."""

    #: A run finished; the result as the controller returned it.
    ran = Signal(dict)
    #: The run changed the mesh in the session (Skin of the mesh, partitions).
    meshEdited = Signal()
    #: A ``PostView`` size field was written for the view with this tag.
    sizeFieldWritten = Signal(int)

    def __init__(self, controller=lambda: None, viewport=lambda: None,
                 parent=None, *, client=None):
        super().__init__(parent)
        self._controller = controller
        self._viewport = viewport
        self._client = client
        self._plugin_view = None
        self._rows: list = []
        self._editors: dict = {}
        self.setObjectName('gmshPluginsPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._choice = QComboBox(self)
        self._choice.setObjectName('gmshPluginChoice')
        self._choice.setAccessibleName(self.tr('Gmsh plugin'))
        self._fillChoice()
        self._choice.currentIndexChanged.connect(self._onChosen)
        layout.addWidget(self._choice)

        self._summary = QLabel(self)
        self._summary.setObjectName('gmshPluginSummary')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._form_host = QWidget(self)
        self._form_host.setObjectName('gmshPluginOptions')
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_host)

        self._input = QComboBox(self)
        self._input.setObjectName('gmshPluginInputView')
        self._input.setAccessibleName(self.tr('Input view'))
        self._input_label = QLabel(self.tr('Input view'), self)
        self._form.addRow(self._input_label, self._input)

        self._extra_host = QWidget(self)
        self._extra_host.setObjectName('gmshPluginExtraForm')
        self._extra_layout = QVBoxLayout(self._extra_host)
        self._extra_layout.setContentsMargins(0, 0, 0, 0)
        self._extra_host.setVisible(False)
        self._extra = None
        layout.addWidget(self._extra_host)

        buttons = QHBoxLayout()
        self._run = QPushButton(self.tr('Run plugin'), self)
        self._run.setObjectName('gmshPluginRun')
        self._run.clicked.connect(lambda _c=False: self.run())
        buttons.addWidget(self._run)
        self._defaults = QPushButton(self.tr('Gmsh defaults'), self)
        self._defaults.setObjectName('gmshPluginDefaults')
        self._defaults.setToolTip(self.tr(
            'Put every option of this plugin back to the value Gmsh itself '
            'declares'))
        self._defaults.clicked.connect(lambda _c=False: self.resetDefaults())
        buttons.addWidget(self._defaults)
        self._showInput = QPushButton(self.tr('Show input view'), self)
        self._showInput.setObjectName('gmshPluginShowInput')
        self._showInput.setToolTip(self.tr(
            'Draw the chosen input view in the viewport'))
        self._showInput.clicked.connect(lambda _c=False: self.showInputView())
        buttons.addWidget(self._showInput)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._note = QLabel(self)
        self._note.setObjectName('gmshPluginNote')
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.addWidget(self._note)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName('gmshPluginResults')
        self._table.setAccessibleName(self.tr('Plugin results'))
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(110)
        layout.addWidget(self._table)

        row = QHBoxLayout()
        self._show = QPushButton(self.tr('Show'), self)
        self._show.setObjectName('gmshPluginShow')
        self._show.clicked.connect(lambda _c=False: self.showSelected())
        self._hide = QPushButton(self.tr('Hide'), self)
        self._hide.setObjectName('gmshPluginHide')
        self._hide.clicked.connect(lambda _c=False: self.hideSelected())
        self._remove = QPushButton(self.tr('Remove'), self)
        self._remove.setObjectName('gmshPluginRemove')
        self._remove.clicked.connect(lambda _c=False: self.removeSelected())
        self._field = QPushButton(self.tr('Use as size field'), self)
        self._field.setObjectName('gmshPluginSizeField')
        self._field.setToolTip(self.tr(
            'Write a PostView size field that reads the selected view, as a '
            'background size'))
        self._field.clicked.connect(lambda _c=False: self.useAsSizeField())
        for button in (self._show, self._hide, self._remove, self._field):
            row.addWidget(button)
        layout.addLayout(row)

        self._log = QLabel(self)
        self._log.setObjectName('gmshPluginLog')
        self._log.setWordWrap(True)
        self._log.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._log.setVisible(False)
        layout.addWidget(self._log)

        self._onChosen()

    # -- the chosen plugin and its options ----------------------------------- #

    def _fillChoice(self) -> None:
        keep = self._choice.currentData()
        self._choice.blockSignals(True)
        self._choice.clear()
        for entry in plugin_catalogue.PLUGINS:
            self._choice.addItem(entry.title, entry.name)
            last = self._choice.count() - 1
            self._choice.setItemData(
                last, entry.summary, Qt.ItemDataRole.ToolTipRole)
            if entry.unavailable:
                set_item_enabled(self._choice, last, False)
                self._choice.setItemData(
                    last, entry.unavailable, Qt.ItemDataRole.ToolTipRole)
        index = self._choice.findData(keep) if keep else 0
        self._choice.setCurrentIndex(max(index, 0))
        self._choice.blockSignals(False)

    def refreshPlugins(self) -> None:
        """Follow the catalogue: a package may have registered a plugin."""
        names = [self._choice.itemData(i) for i in range(self._choice.count())]
        if names != [p.name for p in plugin_catalogue.PLUGINS]:
            self._fillChoice()
            self._onChosen()

    def showEvent(self, event) -> None:
        self.refreshPlugins()
        super().showEvent(event)

    def pluginName(self) -> str:
        return str(self._choice.currentData() or '')

    def pluginNames(self) -> list:
        return [self._choice.itemData(i) for i in range(self._choice.count())]

    def choosePlugin(self, name: str) -> bool:
        index = self._choice.findData(name)
        if index < 0:
            self.refreshPlugins()
            index = self._choice.findData(name)
        if index < 0:
            return False
        self._choice.setCurrentIndex(index)
        return True

    def extraForm(self):
        """The registered form shown for the chosen plugin, or None."""
        return self._extra

    def _onChosen(self, *_args) -> None:
        entry = plugin_catalogue.BY_NAME.get(self.pluginName())
        for editor, label in self._editors.values():
            editor.setParent(None)
            label.setParent(None)
            editor.deleteLater()
            label.deleteLater()
        self._editors = {}
        self._dropExtra()
        if entry is None:
            return
        self._summary.setText(
            entry.summary + (' ' + entry.unavailable if entry.unavailable
                             else ''))
        self._summary.setToolTip(entry.help)
        supplied = extra_panel_entry(entry.name)
        if supplied is not None:
            self._extra = supplied.factory(self._extra_host)
            self._extra_layout.addWidget(self._extra)
        self._extra_host.setVisible(self._extra is not None)
        self._run.setVisible(self._extra is None)
        self._defaults.setVisible(self._extra is None)
        for option in ([] if self._extra is not None else entry.options):
            if option.name in ('View', 'OtherView'):
                continue             # the input view box fills these
            editor = self._makeEditor(option)
            label = QLabel(option.name, self)
            label.setBuddy(editor)
            editor.setToolTip(option.help)
            editor.setObjectName('gmshPluginOption_' + option.name)
            # UXB3c: a length is typed in the project display unit, its unit
            # in a column of its own; values() still hands over metres.
            shown = (length_cell(editor, self)
                     if isinstance(editor, ModelLengthBox) else editor)
            self._form.insertRow(self._form.rowCount() - 1, label, shown)
            self._editors[option.name] = (editor, label)
        takes_view = (entry.needs_view or entry.takes_view) \
            and self._extra is None
        self._input.setVisible(takes_view)
        self._input_label.setVisible(takes_view)
        self._showInput.setVisible(takes_view)
        self._run.setEnabled(not entry.unavailable)
        self._fillInputs()

    def _dropExtra(self) -> None:
        if self._extra is not None:
            self._extra.setParent(None)
            self._extra.deleteLater()
            self._extra = None

    def resetDefaults(self) -> None:
        """Put every option back to the value Gmsh declares for it."""
        entry = plugin_catalogue.BY_NAME.get(self.pluginName())
        if entry is None:
            return
        for option in entry.options:
            if option.name in self._editors:
                self.setValue(option.name, option.default)

    def _makeEditor(self, option):
        if option.kind == 'string':
            editor = QLineEdit(self)
            editor.setText(str(option.default))
            editor.setClearButtonEnabled(True)
        elif option.kind == 'bool':
            editor = QCheckBox(self)
            editor.setChecked(bool(option.default))
        elif option.kind == 'int':
            editor = CompactSpinBox(self)
            editor.setRange(-2_000_000_000, 2_000_000_000)
            editor.setValue(int(option.default))
        elif option.is_length:
            editor = ModelLengthBox(self, minimum=None, maximum=1e12)
            editor.setModelValue(float(option.default))
        else:
            editor = CompactDoubleSpinBox(self)
            editor.setDecimals(6)
            editor.setRange(-1e12, 1e12)
            editor.setValue(float(option.default))
        return editor

    def values(self) -> dict:
        """Every shown option: a float for a number, a ``str`` for a text."""
        out = {}
        for name, (editor, _label) in self._editors.items():
            if isinstance(editor, QLineEdit):
                out[name] = editor.text()
            elif isinstance(editor, QCheckBox):
                out[name] = 1.0 if editor.isChecked() else 0.0
            elif isinstance(editor, ModelLengthBox):
                out[name] = float(editor.modelValue())
            else:
                out[name] = float(editor.value())
        return out

    def setValue(self, name: str, value) -> None:
        editor = self._editors[name][0]
        if isinstance(editor, QLineEdit):
            editor.setText('' if value is None else str(value))
        elif isinstance(editor, QCheckBox):
            editor.setChecked(bool(value))
        elif isinstance(editor, ModelLengthBox):
            editor.setModelValue(float(value))
        else:
            editor.setValue(value)

    def sessionViews(self) -> list:
        """Every view the session holds (not only those this panel made)."""
        controller = self._controller()
        listing = getattr(controller, 'plugin_views', None)
        if controller is None or not callable(listing):
            return []
        try:
            return [dict(v) for v in listing() or ()]
        except Exception:                                # noqa: BLE001
            return []

    def _fillInputs(self) -> None:
        keep = self._input.currentData()
        self._input.blockSignals(True)
        self._input.clear()
        self._input.addItem(self.tr('(none)'), None)
        seen = set()
        for row in self._rows:
            if row.get('tag') is not None:
                seen.add(row['tag'])
                self._input.addItem(f"{row['tag']}: {row['name']}", row['tag'])
        for view in self.sessionViews():
            if view.get('tag') is not None and view['tag'] not in seen:
                seen.add(view['tag'])
                self._input.addItem(
                    f"{view['tag']}: {view.get('name', '')}", view['tag'])
        index = self._input.findData(keep) if keep is not None else -1
        if index < 0 and self._input.count() > 1:
            index = self._input.count() - 1
        self._input.setCurrentIndex(max(index, 0))
        self._input.blockSignals(False)

    def inputViews(self) -> list:
        return [self._input.itemData(i) for i in range(1, self._input.count())]

    def chooseInputView(self, tag) -> bool:
        index = self._input.findData(tag)
        if index < 0:
            return False
        self._input.setCurrentIndex(index)
        return True

    # -- running ------------------------------------------------------------- #

    def _say(self, text: str) -> None:
        self._note.setText(text)
        self._note.setVisible(bool(text))

    def run(self) -> dict:
        """Run the chosen plugin on the session mesh; returns the result."""
        controller = self._controller()
        if controller is None:
            self._say(self.tr('There is no mesh yet; generate one first.'))
            return {}
        name = self.pluginName()
        entry = plugin_catalogue.BY_NAME[name]
        takes_view = entry.needs_view or entry.takes_view
        view = self._input.currentData() if takes_view else None
        from .generate_panel import beginSessionAction

        handle = beginSessionAction(controller, self.tr('Plugin %s') % name)
        try:
            result = self._runOn(controller, name, view)
        except Exception as error:                       # noqa: BLE001
            if handle is not None:
                handle.finish(False, str(error))
            self._say(str(error))
            return {}
        if handle is not None:
            handle.finish(True)
        self._say('')
        self._adopt(result, entry)
        return result

    def _runOn(self, controller, name: str, view) -> dict:
        """Run with every option, text included.

        The window's controller is a ``GenerateController``; one that offers
        only ``run_plugin`` (numbers) is still driven, without text options.
        """
        if hasattr(controller, '_lock') and hasattr(controller, 'session'):
            return plugin_catalogue.run_on_controller(
                controller, name, self.values(), view=view)
        numbers = plugin_catalogue.numbers_for(name, self.values())
        return controller.run_plugin(name, numbers, view=view)

    def showInputView(self) -> dict:
        """Draw the chosen input view, whichever package or plugin made it."""
        tag = self._input.currentData()
        if tag is None:
            self._say(self.tr('Choose an input view first.'))
            return {}
        known = [r for r in self._rows if r.get('tag') == tag]
        if known and known[0].get('path'):
            return self._draw(known[0])
        controller = self._controller()
        with_lock = getattr(controller, '_lock', None)
        live = getattr(controller, 'session', None)
        if controller is None or live is None or with_lock is None:
            self._say(self.tr('There is no mesh yet; generate one first.'))
            return {}
        try:
            with with_lock:
                directory = controller.plugin_directory()
                row = live.call('session.plugin_describe', int(tag),
                                live.runtime_path(directory))
                if row.get('path'):
                    row['host_path'] = str(Path(directory) / Path(
                        row['path']).name)
        except Exception as error:                       # noqa: BLE001
            self._say(str(error))
            return {}
        if not row.get('path'):
            self._say(self.tr('That view has no elements to draw.'))
            return row
        self._rows = [r for r in self._rows if r.get('tag') != tag] + [row]
        self._fillTable()
        return self._draw(row)

    def _adopt(self, result: dict, entry) -> None:
        fresh = [dict(item) for item in result.get('views') or ()]
        alive = {v['tag'] for v in result.get('kept') or ()}
        mine = {self._key(r): r for r in self._rows
                if r.get('tag') is None or r['tag'] in alive}
        for item in fresh:
            mine[self._key(item)] = item
        self._rows = list(mine.values())
        self._fillTable()
        self._fillInputs()
        for item in fresh:
            if item.get('path'):
                self._draw(item)
        self._showLog(result, fresh)
        self.ran.emit(result)
        if result.get('entities') or plugin_catalogue.edits_mesh(
                entry.name, self.values()):
            self.meshEdited.emit()

    def _showLog(self, result: dict, fresh: list) -> None:
        lines = [line.split('Info: ', 1)[-1] for line in result.get('log') or ()
                 if line.startswith('Info') and 'Running Plugin' not in line
                 and 'Done running' not in line]
        lines = [x for x in lines if x][-6:]
        if not fresh and not lines:
            lines = [self.tr('The plugin ran and made nothing to show.')]
        self._log.setText('\n'.join(lines))
        self._log.setVisible(bool(lines))

    # -- the list and the viewport ------------------------------------------- #

    def results(self) -> list:
        return [dict(r) for r in self._rows]

    def _key(self, row):
        return row['tag'] if row.get('tag') is not None else \
            '%d:%d' % tuple(row['entity'])

    def _fillTable(self) -> None:
        self._table.setRowCount(len(self._rows))
        shown = set(self._plugin_view.keys()) if self._plugin_view else set()
        for index, row in enumerate(self._rows):
            label = ('' if row.get('tag') is None else str(row['tag']))
            values = (label, row.get('name', ''),
                      f"{int(row.get('cells') or 0):,}", _number(row.get('min')),
                      _number(row.get('avg')), _number(row.get('max')),
                      self.tr('yes') if self._key(row) in shown else '')
            for column, text in enumerate(values):
                self._table.setItem(index, column, QTableWidgetItem(text))

    def _selected(self):
        index = self._table.currentRow()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def selectResult(self, row_index: int) -> None:
        self._table.setCurrentCell(row_index, 0)

    # -- what the panel shows (Plan 48 PA4: the local API reads the same) ------ #

    def noteText(self) -> str:
        return self._note.text() if not self._note.isHidden() else ''

    def logText(self) -> str:
        return self._log.text() if not self._log.isHidden() else ''

    def tableRows(self) -> list:
        """The results table as text, one list per row (View ... Shown)."""
        return [[(self._table.item(row, column).text()
                  if self._table.item(row, column) is not None else '')
                 for column in range(self._table.columnCount())]
                for row in range(self._table.rowCount())]

    def rowIndexOfTag(self, tag) -> int:
        """The row of the result that is view *tag*, or -1."""
        for index, row in enumerate(self._rows):
            if row.get('tag') is not None and int(row['tag']) == int(tag):
                return index
        return -1

    def selectedIndex(self) -> int:
        return self._table.currentRow()

    def inputTag(self):
        return self._input.currentData()

    def _viewOf(self):
        if self._plugin_view is None:
            widget = self._viewport()
            if widget is None:
                return None
            from foammesh.rendering.gmsh_plugin_view import GmshPluginView
            self._plugin_view = GmshPluginView(widget)
        return self._plugin_view

    def _draw(self, row) -> dict:
        view = self._viewOf()
        if view is None or not row.get('path'):
            return {}
        path = row.get('host_path') or row['path']
        if not Path(path).is_file():
            self._say(self.tr('The result file %s is not there.') % path)
            return {}
        shown = view.show(self._key(row), path, row.get('name', ''))
        self._fillTable()
        return shown

    def showSelected(self) -> dict:
        row = self._selected()
        if row is None:
            self._say(self.tr('Select a result first.'))
            return {}
        if not row.get('path'):
            self._say(self.tr('That result has no elements to draw.'))
            return {}
        return self._draw(row)

    def hideSelected(self) -> bool:
        row = self._selected()
        view = self._plugin_view
        if row is None or view is None:
            return False
        gone = view.hide(self._key(row))
        self._fillTable()
        return gone

    def shownKeys(self) -> list:
        return self._plugin_view.keys() if self._plugin_view else []

    def removeSelected(self) -> dict:
        row = self._selected()
        controller = self._controller()
        if row is None or controller is None:
            return {}
        if row.get('tag') is None:
            self._say(self.tr('A surface a plugin added to the mesh is part '
                              'of the mesh: regenerate the mesh to drop it.'))
            return {}
        if self._plugin_view is not None:
            self._plugin_view.hide(self._key(row))
        try:
            left = controller.remove_plugin_view(row['tag'])
        except Exception as error:                       # noqa: BLE001
            self._say(str(error))
            return {}
        alive = {v['tag'] for v in left.get('left') or ()}
        self._rows = [r for r in self._rows
                      if r.get('tag') is None or r['tag'] in alive]
        self._fillTable()
        self._fillInputs()
        return left

    def clear(self) -> None:
        """The mesh is gone: so are its views."""
        if self._plugin_view is not None:
            self._plugin_view.clear()
        self._rows = []
        self._fillTable()
        self._fillInputs()
        self._say('')
        self._log.setVisible(False)

    # -- M-16: a plugin result as a PostView size field ----------------------- #

    def useAsSizeField(self) -> dict:
        """Write a ``PostView`` size field that reads the selected view."""
        row = self._selected()
        if row is None or row.get('tag') is None:
            self._say(self.tr('Select a result that is a view first.'))
            return {}
        if self._client is None:
            self._say(self.tr('There is no project to write the field to.'))
            return {}
        stored = []
        try:
            raw = self._client.field_values((field_catalogue.FIELD_ID,))
            value = raw.get(field_catalogue.FIELD_ID)
            stored = field_catalogue.rows_from_text(
                getattr(value, 'value', value))
        except Exception as error:                       # noqa: BLE001
            self._say(str(error))
            return {}
        # The row is built by the helper gmsh.fields.use_view builds it with,
        # so the button and the route cannot differ (Plan 48 PA3).
        from foammesh.core.facade.plan48.pa3_routes import use_view_rows
        stored, new = use_view_rows(stored, int(row['tag']))
        from foammesh.view.facade_client import submit
        submit(self._client, 'configuration.patch',
               {'patch': {field_catalogue.FIELD_ID:
                          field_catalogue.rows_to_text(stored)}})
        self._say(self.tr(
            'Wrote the size field %s: it reads view %d while this Gmsh session '
            'is open, and is a background size.') % (new['name'], row['tag']))
        self.sizeFieldWritten.emit(int(row['tag']))
        return new
