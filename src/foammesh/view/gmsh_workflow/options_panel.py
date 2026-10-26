"""The Gmsh option catalogue as a panel: search, edit, save, restore.

Plan 41 GN2 (O-01..O-03, O-10, T-01).  Every option of the installed Gmsh
build is listed with its default and its manual text; a value typed here is
stored in the project as ``gmsh.options.overrides`` and applied by the runner
after every page control, so the value the user chose is the one Gmsh meshes
with.  An option the page has its own control for stays reachable here; the
Options value wins and the derivation says so.

One panel serves two places:

* the complete, searchable list on the Compute page (``page_id=None``), which
  is what the plan calls the Options dock -- the application has no dock
  widget to host it, so it lives where a run is started;
* an Advanced tier on each of the other Gmsh pages (``page_id='gmsh....'``),
  rendered from the same catalogue and restricted to the options that page
  owns.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.core import display_unit
from foammesh.core.facade.plan48.pa4_routes import replacement_set
from foammesh.core.gmsh import option_catalogue as catalogue
from foammesh.view.workflow_controls.length_box import (
    LengthItemDelegate, mark_length_item,
)

#: Plan 48 GM2. There is no row cut-off: every option of the build is a row,
#: and the label says how many matched the search and the group.
_REMOVE = object()


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ', '.join(str(int(item)) for item in value)
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return str(value)


class GmshOptionsPanel(QWidget):
    """Edits ``gmsh.options.overrides``; adoptable by a task page."""

    dirtyChanged = Signal(bool)

    def __init__(self, client, page_id=None, parent=None):
        super().__init__(parent)
        self._client = client
        self._page_id = page_id
        self._embedded = False
        self._stored: dict = {}
        self._pending: dict = {}
        self._filling = False
        self._error = ''
        self.setObjectName('gmshOptionsPanel' if page_id is None
                           else 'gmshOptionsTier')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._summary = QLabel(self)
        self._summary.setObjectName('gmshOptionsSummary')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        row = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setObjectName('gmshOptionsSearch')
        self._search.setPlaceholderText(self.tr('Search names and help'))
        self._search.setAccessibleName('Search the Gmsh options')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._fill)
        row.addWidget(self._search, 1)
        self._group = QComboBox(self)
        self._group.setObjectName('gmshOptionsGroup')
        self._group.setAccessibleName('Option group')
        self._group.addItem(self.tr('All groups'), '')
        for group in catalogue.GROUPS:
            self._group.addItem(group, group)
        self._group.currentIndexChanged.connect(self._fill)
        row.addWidget(self._group)
        self._changed = QCheckBox(self.tr('Set by me'), self)
        self._changed.setObjectName('gmshOptionsChangedOnly')
        self._changed.toggled.connect(self._fill)
        row.addWidget(self._changed)
        layout.addLayout(row)
        if page_id is not None:
            self._group.setVisible(False)

        self._table = QTableWidget(0, 5, self)
        self._table.setObjectName('gmshOptionsTable')
        self._table.setHorizontalHeaderLabels(
            [self.tr('Option'), self.tr('Value'), self.tr('Default'),
             self.tr('Also on a page'), self.tr('Unit')])
        # UXB3c. A length option holds model units and shows the display unit.
        self._lengths = LengthItemDelegate(self._table)
        self._table.setItemDelegateForColumn(1, self._lengths)
        self._table.setItemDelegateForColumn(2, self._lengths)
        display_unit.subscribe(self._on_display_unit)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(220)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(self._on_current)
        layout.addWidget(self._table)

        self._help = QLabel(self)
        self._help.setObjectName('gmshOptionsHelp')
        self._help.setWordWrap(True)
        layout.addWidget(self._help)
        self._errorLabel = QLabel(self)
        self._errorLabel.setObjectName('gmshOptionsError')
        self._errorLabel.setWordWrap(True)
        self._errorLabel.setProperty('foammeshStatus', 'error')
        self._errorLabel.setVisible(False)
        layout.addWidget(self._errorLabel)

        buttons = QHBoxLayout()
        for attribute, text, slot in (
                ('_resetOne', self.tr('Reset option'), self.reset_current),
                ('_resetAll', self.tr('Reset all'), self.reset_all),
                ('_openOpt', self.tr('Open .opt…'), self._choose_open),
                ('_saveOpt', self.tr('Save .opt…'), self._choose_save),
                ('_saveDefault', self.tr('Save as my defaults'),
                 self.save_as_defaults),
                ('_useDefault', self.tr('Use my defaults'),
                 self.use_defaults)):
            button = QPushButton(text, self)
            button.clicked.connect(lambda _checked=False, slot=slot: slot())
            setattr(self, attribute, button)
            buttons.addWidget(button)
        if page_id is not None:
            for attribute in ('_openOpt', '_saveOpt', '_saveDefault',
                              '_useDefault'):
                getattr(self, attribute).setVisible(False)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.reload()

    def _on_display_unit(self) -> None:
        """The display unit changed: same stored values, new words."""
        try:
            self._fill()
        except RuntimeError:
            pass

    # -- the FieldGroupPage-shaped surface a task page adopts --------------- #

    def setEmbedded(self, embedded: bool = True) -> None:
        self._embedded = bool(embedded)

    @property
    def is_dirty(self) -> bool:
        return bool(self._pending)

    def effective(self) -> dict:
        """What the project will hold once the pending edits are committed."""
        mapping = dict(self._stored)
        for name, value in self._pending.items():
            if value is _REMOVE:
                mapping.pop(name, None)
            else:
                mapping[name] = value
        return mapping

    def pending_patch(self) -> dict:
        if not self._pending:
            return {}
        return {catalogue.OVERRIDES_FIELD:
                catalogue.overrides_to_text(self.effective())}

    def reload(self, *, discard_pending: bool = True) -> None:
        if discard_pending:
            self._pending.clear()
        self._stored = self._read_stored()
        self._fill()
        self.dirtyChanged.emit(self.is_dirty)

    def revert(self) -> None:
        self.reload(discard_pending=True)

    def _read_stored(self) -> dict:
        try:
            values = self._client.field_values((catalogue.OVERRIDES_FIELD,))
            raw = values.get(catalogue.OVERRIDES_FIELD)
            raw = getattr(raw, 'value', raw)
            return catalogue.overrides_from_text(raw)
        except Exception as error:                       # noqa: BLE001
            self._show_error(str(error))
            return {}

    # -- the table ----------------------------------------------------------- #

    def _scope(self):
        group = self._group.currentData() or ''
        if self._page_id is not None:
            words = self._search.text().lower().split()
            return [option for option in catalogue.options_for_page(
                        self._page_id)
                    if all(word in (option.name + ' ' + option.help).lower()
                           for word in words)]
        return list(catalogue.search(self._search.text(), group))

    def _fill(self, *_args) -> None:
        mapping = self.effective()
        options = self._scope()
        if self._changed.isChecked():
            options = [o for o in options if o.name in mapping]
        shown = options
        self._filling = True
        self._table.setUpdatesEnabled(False)
        try:
            self._table.clearContents()
            self._table.setRowCount(len(shown))
            for index, option in enumerate(shown):
                name_item = QTableWidgetItem(option.name)
                name_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                   | Qt.ItemFlag.ItemIsSelectable)
                name_item.setToolTip(option.help)
                name_item.setData(Qt.ItemDataRole.UserRole, option.name)
                value = mapping.get(option.name, option.default)
                value_item = QTableWidgetItem(_text(value))
                flags = (Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
                if not option.read_only:
                    flags |= Qt.ItemFlag.ItemIsEditable
                value_item.setFlags(flags)
                value_item.setToolTip(self._describe(option))
                if option.name in mapping:
                    font = value_item.font()
                    font.setBold(True)
                    value_item.setFont(font)
                default_item = QTableWidgetItem(_text(option.default))
                default_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                unit_item = QTableWidgetItem(
                    display_unit.current() if option.is_length else '')
                unit_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                if option.is_length:
                    mark_length_item(value_item)
                    mark_length_item(default_item)
                self._table.setItem(index, 0, name_item)
                self._table.setItem(index, 1, value_item)
                self._table.setItem(index, 2, default_item)
                # Plan 42 SG20. An option a page control also sets: say so,
                # and which one wins.
                note = catalogue.precedence_note(option.name)
                page_item = QTableWidgetItem(
                    self.tr('page control: Options wins') if note else '')
                page_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                page_item.setToolTip(note)
                self._table.setItem(index, 3, page_item)
                self._table.setItem(index, 4, unit_item)
                if not option.read_only and option.kind in ('bool', 'enum'):
                    self._table.setCellWidget(
                        index, 1, self._combo_for(option, value))
        finally:
            self._table.setUpdatesEnabled(True)
            self._filling = False
        self._summary.setText(
            f'{len(shown)} options shown. '
            f'{len(mapping)} set from this list.')

    @staticmethod
    def _choices_of(option):
        if option.kind == 'bool':
            return ((0, 'off'), (1, 'on'))
        return tuple(option.choices)

    def _combo_for(self, option, value):
        """Plan 48 GM2. A bool or an enum is picked from its native range."""
        combo = QComboBox(self._table)
        combo.setObjectName('gmshOptionCombo')
        combo.setAccessibleName(option.name)
        combo.setProperty('optionName', option.name)
        choices = self._choices_of(option)
        try:
            current = int(float(value))
        except (TypeError, ValueError):
            current = None
        for key, label in choices:
            combo.addItem(f'{key}: {label}', key)
        index = combo.findData(current)
        if index < 0 and current is not None:
            combo.addItem(f'{current}', current)
            index = combo.count() - 1
        combo.setCurrentIndex(max(index, 0))
        combo.setToolTip(self._describe(option))
        combo.activated.connect(
            lambda _i, combo=combo, name=option.name:
            self._combo_picked(name, combo.currentData()))
        return combo

    def _combo_picked(self, name, key) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.text() == name:
                self._filling = True
                try:
                    self._table.item(row, 1).setText(_text(key))
                finally:
                    self._filling = False
        self.set_option(name, key, _from_table=True)

    @staticmethod
    def _describe(option) -> str:
        text = option.help
        if option.kind == 'enum':
            text += ' | allowed: ' + ', '.join(
                f'{key}: {label}' for key, label in option.choices)
        elif option.kind == 'bitmask' and option.bits:
            text += ' | bits: ' + ', '.join(
                f'{2 ** bit} = {label}' if option.name.endswith('Clip')
                else f'digit {bit} = {label}' for bit, label in option.bits)
        elif option.minimum is not None or option.maximum is not None:
            low = '' if option.minimum is None else f'{option.minimum:g}'
            high = '' if option.maximum is None else f'{option.maximum:g}'
            text += f' | range: {low} to {high}'
        if option.read_only:
            text += ' | read-only'
        note = catalogue.precedence_note(option.name)
        if note:
            text += ' | ' + note
        return text

    def _row_option(self, row: int):
        item = self._table.item(row, 0)
        return catalogue.get(item.data(Qt.ItemDataRole.UserRole)) \
            if item else None

    def _on_current(self, row, _column, _prow, _pcol) -> None:
        option = self._row_option(row) if row >= 0 else None
        self._help.setText('' if option is None else self._describe(option))

    def _on_item_changed(self, item) -> None:
        if self._filling or item.column() != 1:
            return
        option = self._row_option(item.row())
        if option is None:
            return
        self.set_option(option.name, item.text(), _from_table=True)

    # -- editing ------------------------------------------------------------- #

    def set_option(self, name: str, raw, *, _from_table: bool = False) -> bool:
        """Set one option; ``False`` (with the reason shown) if refused."""
        try:
            value = catalogue.parse_value(name, raw)
        except catalogue.OptionRefusal as error:
            self._show_error(str(error))
            if _from_table:
                self._mark_invalid(name)
            return False
        self._show_error('')
        option = catalogue.get(name)
        value = list(value) if isinstance(value, tuple) else value
        if catalogue.same_value(option, value, option.default):
            self._stage(name, _REMOVE)
        else:
            self._stage(name, value)
        if not _from_table:
            self._fill()
        else:
            self._restyle_row(name)
        return True

    def _stage(self, name, value) -> None:
        stored = self._stored.get(name, _REMOVE)
        if value is _REMOVE and stored is _REMOVE:
            same = True
        elif value is _REMOVE or stored is _REMOVE:
            same = False
        else:
            same = catalogue.same_value(catalogue.get(name), value, stored)
        if same:
            self._pending.pop(name, None)
        else:
            self._pending[name] = value
        self.dirtyChanged.emit(self.is_dirty)

    def _mark_invalid(self, name) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.text() == name:
                self._table.item(row, 1).setForeground(
                    QBrush(QColor('#c62828')))

    def _restyle_row(self, name) -> None:
        mapping = self.effective()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.text() == name:
                self._filling = True
                try:
                    value_item = self._table.item(row, 1)
                    value_item.setForeground(QBrush())
                    font = value_item.font()
                    font.setBold(name in mapping)
                    value_item.setFont(font)
                finally:
                    self._filling = False

    def reset_current(self) -> None:
        row = self._table.currentRow()
        option = self._row_option(row) if row >= 0 else None
        if option is not None:
            self._stage(option.name, _REMOVE)
            self._fill()

    def reset_all(self) -> None:
        for name in list(self.effective()):
            self._stage(name, _REMOVE)
        self._fill()

    def replace_with(self, mapping: dict) -> bool:
        """Make *mapping* the whole set (an .opt file, the user's defaults).

        Validated first: a file with one bad line changes nothing.
        """
        try:
            catalogue.resolve_overrides(mapping)
        except catalogue.OptionRefusal as error:
            self._show_error(str(error))
            return False
        self._show_error('')
        wanted = replacement_set(mapping)
        for name in list(self.effective()):
            if name not in wanted:
                self._stage(name, _REMOVE)
        for name, value in wanted.items():
            self._stage(name, value)
        self._fill()
        return True

    # -- files ---------------------------------------------------------------- #

    def load_opt_file(self, path) -> bool:
        try:
            mapping = catalogue.read_opt_file(path)
        except (OSError, catalogue.OptionRefusal) as error:
            self._show_error(f'{path}: {error}')
            return False
        return self.replace_with(mapping)

    def save_opt_file(self, path) -> bool:
        try:
            catalogue.write_opt_file(path, self.effective())
        except OSError as error:
            self._show_error(f'{path}: {error}')
            return False
        return True

    def _choose_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr('Open a Gmsh options file'), '',
            self.tr('Gmsh options (*.opt)') + ';;' + self.tr('All files (*)'))
        if path:
            self.load_opt_file(path)

    def _choose_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr('Save the Gmsh options'), 'options.opt',
            self.tr('Gmsh options (*.opt)'))
        if path:
            self.save_opt_file(path)

    def save_as_defaults(self) -> bool:
        try:
            catalogue.save_defaults(self.effective())
        except OSError as error:
            self._show_error(str(error))
            return False
        self._show_error('')
        return True

    def use_defaults(self) -> bool:
        try:
            mapping = catalogue.load_defaults()
        except catalogue.OptionRefusal as error:
            self._show_error(str(error))
            return False
        return self.replace_with(mapping)

    def _show_error(self, text: str) -> None:
        self._error = text
        self._errorLabel.setText(text)
        self._errorLabel.setVisible(bool(text))

    def last_error(self) -> str:
        return self._error
