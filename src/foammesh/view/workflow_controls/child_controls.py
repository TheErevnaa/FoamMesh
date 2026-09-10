"""Repeatable child controls for the Local Sizing and Boundary Layers tasks.

Plan 17 §5.10 and §5.13 model these as parent tasks owning ordered child items
with stable IDs.  The children are an ``IntKeyList`` collection in the schema,
so they are created, edited, and removed through the collection facade
operations (``<collection>.create`` / ``.patch`` / ``.remove``) rather than a
scalar configuration patch.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.core.facade.field_adapters import EntityAdapter
from foammesh.core.facade.fields import REGISTRY
from foammesh.app import app
from foammesh.core.selection import SelectionKind, SelectionStatus
from foammesh.view.facade_client import submit

from .field_widgets import FieldEditor


def collection_adapter(collection_id: str) -> EntityAdapter:
    """The same element adapter the facade validates patches with.

    Editors must be keyed by the adapter's semantic relative IDs
    (``scope_token``), not the storage schema keys (``scopeToken``):
    ``normalize_patch`` rejects unknown relative IDs, so any other keying
    produces controls whose edits the facade refuses.
    """
    return EntityAdapter(REGISTRY.collections[collection_id])


def element_descriptors(collection_id: str) -> dict:
    """Typed descriptors for one collection element, keyed by relative ID."""
    adapter = collection_adapter(collection_id)
    return {relative_id: field.descriptor
            for relative_id, field in adapter.fields.items()}


class ChildControlPanel(QGroupBox):
    """Table of child items plus an editor form for the selected row."""

    childrenChanged = Signal()

    def __init__(self, facade_client, collection_id: str, title: str,
                 columns=(), parent=None, *, choices=None, annotations=None):
        super().__init__(title, parent)
        self._client = facade_client
        self.collection_id = collection_id
        # R60/R152. Fields whose value is an opaque index the app can name --
        # the per-surface size's `Surface Id` -- get a picker built by the
        # owning page, keyed by relative id. Empty or absent means the plain
        # typed editor, so a case with no prepared geometry still has a
        # usable control.
        self._choices = {key: list(value)
                         for key, value in dict(choices or {}).items() if value}
        # C31-11. Widgets the owning page wants shown *under* a field in the
        # editor, keyed by relative id -- the layer pattern's match preview is
        # the first. A field whose value only means something in relation to
        # the rest of the case needs to say so where it is typed; a note
        # elsewhere on the page is read after the mistake, not before it.
        self._annotations = {key: widget
                             for key, widget in dict(annotations or {}).items()
                             if widget is not None}
        self._adapter = collection_adapter(collection_id)
        self._descriptors = {relative_id: field.descriptor
                             for relative_id, field in self._adapter.fields.items()}
        self._columns = tuple(columns) or tuple(self._descriptors)
        self._editors: dict[str, FieldEditor] = {}
        self._rows: list[dict] = []
        self.setObjectName(collection_id.replace('.', '_') + 'Panel')
        project = getattr(app, 'project', None)
        if project is not None:
            try:
                app.selectionService.synchronize_prepared_case(
                    project.path, app.facadeClient.checkout())
            except (FileNotFoundError, OSError, ValueError):
                pass

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self._columns), self)
        self.table.setHorizontalHeaderLabels(
            [_humanise(name) for name in self._columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAccessibleName(title)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        # R59/R151. Stretch divided the panel width by the column count, so an
        # eight-column table drew every heading as an unreadable stub -- Size
        # fields read `Vame able ld ty etry e ins out ress riorit` -- and with
        # no horizontal scrollbar the text was unrecoverable from the GUI.
        # Sizing to contents and scrolling instead means a column is never
        # narrower than its own heading.
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        for column, name in enumerate(self._columns):
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(_humanise(name))
        self.table.itemSelectionChanged.connect(self._load_selected)
        layout.addWidget(self.table)

        # The editors are built here, not in the dialog, so a headless caller
        # can set values and commit without a dialog ever existing -- and so
        # they keep their state across openings.
        self._form = QWidget(self)
        self._form.setVisible(False)
        for key, descriptor in self._descriptors.items():
            self._editors[key] = self._build_field_editor(key, descriptor)
            # FS-B. A new editor holds whatever its widget starts as, which
            # for a text field is empty -- and the schema's default is not
            # always the empty string. A background block's three grading
            # fields default to '1' and are not optional, so pressing Add on
            # an untouched form sent '' for all three and the facade answered
            # "entity value failed validation": a panel that refuses its own
            # first row until three boxes nobody mentioned are filled in.
            # The form starts at the defaults the schema states instead.
            editor = self._editors[key]
            if descriptor.default is not None:
                editor.set_value(descriptor.default)
        self._dialog = None

        self._empty = QLabel(
            self.tr('No items yet. This task is optional; leaving it empty is '
                    'recorded as an explicit skip.'), self)
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)

        buttons = QHBoxLayout()
        self._add = QPushButton(self.tr('Add…'), self)
        self._edit = QPushButton(self.tr('Edit…'), self)
        self._remove = QPushButton(self.tr('Remove'), self)
        self._add.clicked.connect(self.open_add_dialog)
        self._edit.clicked.connect(self.open_edit_dialog)
        self._remove.clicked.connect(self.remove_selected)
        for button in (self._add, self._edit, self._remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.table.doubleClicked.connect(self.open_edit_dialog)

        self.refresh()

    # -- the editor, which is a dialog rather than a column of rows --------- #

    def _build_field_editor(self, key, descriptor) -> FieldEditor:
        choices = (
            self._scope_choices(key) if self._is_scope_field(key)
            else self._choices.get(key))
        editor = FieldEditor(descriptor, self._form, choices=choices)
        if key == 'scope_token' or key.endswith('_scope_token'):
            editor.label.setText(self.tr(
                key.replace('_token', '').replace('_', ' ').title()))
            editor.editor.setAccessibleName(self.tr('Stable geometry scope'))
        return editor

    def editor(self, key):
        """The live editor for one relative id, so a page can watch it."""
        return self._editors.get(key)

    def set_choices(self, key: str, options) -> None:
        """Re-offer one field as a picker over *options*.

        C31-11. A feature refinement band names the surface refinement group
        it grades, and that list is not fixed at construction: the user adds
        the surface refinement and then the band, in that order, in the same
        sitting. Typing the group name again by hand and getting it subtly
        wrong writes a band that is silently never read, so the field is a
        picker -- and a picker that cannot be re-offered is a picker that is
        empty exactly when it is first needed.

        The editor widget is rebuilt rather than repopulated because a plain
        text box and a combo are different widgets; the dialog is dropped so
        the next open lays out the new one.
        """
        if key not in self._descriptors:
            raise KeyError(key)
        options = [tuple(option) for option in (options or ())]
        previous = self._editors[key].value() if key in self._editors else None
        if options:
            self._choices[key] = options
        else:
            self._choices.pop(key, None)
        old = self._editors.get(key)
        self._editors[key] = self._build_field_editor(
            key, self._descriptors[key])
        if previous not in (None, ''):
            self._editors[key].set_value(previous)
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        if self._dialog is not None:
            self._dialog.setParent(None)
            self._dialog.deleteLater()
            self._dialog = None

    def editor_dialog(self):
        """The shared editor, built on first use.

        One instance per panel: the editors are the panel's, and moving them
        between dialogs on every open would reparent live widgets for nothing.
        """
        if self._dialog is None:
            from .child_editor_dialog import RELEVANCE, ChildEditorDialog

            self._dialog = ChildEditorDialog(
                self.title(), self._editors, self,
                relevance=RELEVANCE.get(self.collection_id),
                annotations=self._annotations)
        return self._dialog

    def open_add_dialog(self) -> None:
        """Edit a fresh item, then create it.

        Reset to schema defaults first: opening on whatever the last selected
        row happened to hold means a user who presses Add and then OK has
        silently duplicated a row they were only looking at.
        """
        for editor in self._editors.values():
            editor.set_value(editor.descriptor.default)
        dialog = self.editor_dialog()
        # `set_value` blocks signals, so the rows do not re-evaluate on their
        # own. The dialog is reused, and one opened on a rotational pair would
        # otherwise still be showing rotation rows over a coincident one.
        dialog.applyRelevance()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_child()

    def open_edit_dialog(self, *_args) -> None:
        if self.selected_key() is None:
            return
        self._load_selected()
        dialog = self.editor_dialog()
        dialog.applyRelevance()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.apply_selected()

    # -- data -------------------------------------------------------------- #

    def rows(self) -> list[dict]:
        return list(self._rows)

    def refresh(self) -> None:
        self._rows = self._read_rows()
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            scope_problem = self._scope_problem(row)
            for column, key in enumerate(self._columns):
                value = row.get(key, '')
                display, detail = self._cell_text(key, value)
                if column == 0 and scope_problem:
                    display = self.tr('Needs scope: %s') % display
                item = QTableWidgetItem(display)
                if detail:
                    item.setToolTip(detail)
                if scope_problem:
                    item.setForeground(QColor('#ff5f56'))
                    item.setToolTip(scope_problem)
                    item.setStatusTip(scope_problem)
                self.table.setItem(index, column, item)
        empty = not self._rows
        self._empty.setVisible(empty)
        self._edit.setEnabled(not empty)
        self._remove.setEnabled(not empty)
        self._fit_table_height()
        if self._rows and not self.table.selectedItems():
            self.table.selectRow(0)

    #: Past this the table scrolls. This panel sits under the thing it
    #: annotates -- the geometry tree -- and a table that keeps growing takes
    #: that space from the tree, which is what the user is actually looking at.
    MAX_VISIBLE_ROWS = 6

    def _fit_table_height(self) -> None:
        """Size the table to its contents, floored at two rows.

        An empty table stretched to fill claims a third of the panel to say
        nothing; the message below it is what carries the empty state.
        """
        rows = max(2, min(len(self._rows), self.MAX_VISIBLE_ROWS))
        row_height = self.table.verticalHeader().defaultSectionSize()
        header = self.table.horizontalHeader().height()
        # R59. The columns are sized to their contents now, so the table can
        # be wider than the panel and needs somewhere to put the horizontal
        # scrollbar; without this it is drawn over the last row.
        scrollbar = self.table.horizontalScrollBar().sizeHint().height()
        self.table.setFixedHeight(header + rows * row_height + scrollbar
                                  + 2 * self.table.frameWidth())

    def _is_scope_field(self, key: str) -> bool:
        return key == 'scope_token' or key.endswith('_scope_token')

    def _cell_text(self, key: str, value) -> tuple[str, str]:
        """What one cell shows, and what its tooltip adds.

        R150. Every dialog picks a scope by the name typed on the Geometry
        page and every table then printed the choice back as a raw UUID
        prefix -- `inlet_fine` showed `927...` where `inlet` was chosen -- so
        three controls on three different surfaces were indistinguishable
        from three on the same one. The name is what the user chose; the
        stable id stays reachable in the tooltip.
        """
        text = _display(value)
        if not text:
            return text, ''
        supplied = self._choices.get(key)
        if supplied is not None:
            for label, option, status_label, _enabled in supplied:
                if str(option) == text:
                    return str(label), str(status_label)
            return text, ''
        if not self._is_scope_field(key):
            return text, ''
        entity = app.selectionService.entity(text)
        label = str(getattr(entity, 'label', '') or '').strip()
        if not label:
            return text, self.tr('Stable ID: %s') % text
        return label, self.tr('Stable ID: %s') % text

    def _read_rows(self) -> list[dict]:
        node = self._client.configuration()
        for segment in self._adapter.storage_path.split('/'):
            if not isinstance(node, dict):
                return []
            node = node.get(segment)
            if node is None:
                return []
        if isinstance(node, dict):
            items = sorted(node.items(), key=_sort_key)
        else:
            items = list(enumerate(node or ()))
        return [dict(self._semantic_row(value), **{'__key__': key})
                for key, value in items]

    def _semantic_row(self, stored: dict) -> dict:
        """Re-key one stored element from storage keys to relative IDs."""
        row = {}
        for relative_id, field in self._adapter.fields.items():
            if field.relative_path in stored:
                row[relative_id] = stored[field.relative_path]
        return row

    def selected_key(self):
        index = self.table.currentRow()
        if index < 0 or index >= len(self._rows):
            return None
        return self._rows[index].get('__key__', index)

    def _load_selected(self) -> None:
        index = self.table.currentRow()
        if index < 0 or index >= len(self._rows):
            return
        row = self._rows[index]
        for key, editor in self._editors.items():
            editor.set_value(row.get(key, editor.descriptor.default))
        scopes = tuple(
            str(row.get(key) or '')
            for key in self._descriptors
            if key == 'scope_token' or key.endswith('_scope_token'))
        scopes = tuple(
            scope for scope in scopes
            if scope and app.selectionService.entity(scope) is not None)
        if scopes:
            try:
                # A geometry-level interface row owns two faces. Selecting the
                # row must therefore highlight both the master and slave, not
                # just scalar-control rows that happen to use ``scope_token``.
                app.selectionService.select(scopes)
            except ValueError:
                pass

    # -- mutation ---------------------------------------------------------- #

    def child_values(self) -> dict:
        return {key: editor.value() for key, editor in self._editors.items()}

    def add_child(self) -> None:
        # The collection handlers read 'fields' and 'entity_id'
        # (facade._make_collection_create/_patch/_remove); anything else is
        # silently dropped or rejected as a missing entity.
        values = self._validated_child_values()
        if values is not None:
            self._run(f'{self.collection_id}.create', {'fields': values})

    def apply_selected(self) -> None:
        key = self.selected_key()
        if key is None:
            return
        values = self._validated_child_values()
        if values is not None:
            self._run(f'{self.collection_id}.patch',
                      {'entity_id': str(key), 'fields': values})

    def remove_selected(self) -> None:
        key = self.selected_key()
        if key is None:
            return
        confirm = QMessageBox.question(
            self, self.tr('Remove item'),
            self.tr('Remove the selected item? Downstream mesh artifacts '
                    'become stale.'))
        if confirm != QMessageBox.Yes:
            return
        self._run(f'{self.collection_id}.remove', {'entity_id': str(key)})

    def _run(self, operation: str, parameters: dict) -> None:
        # C31-12. Create/patch/remove of a child row is scheduled rather than
        # run on the GUI thread. `ran` keeps the original order: the warning,
        # or the table refresh followed by childrenChanged, both after the
        # facade has answered.
        def ran(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                QMessageBox.warning(
                    self, self.tr('Operation failed'),
                    str(getattr(result, 'message', '')
                        or self.tr('The facade rejected the change.')))
                return
            self.refresh()
            self.childrenChanged.emit()

        submit(self._client, operation, parameters, then=ran)

    #: Collections scoped to volume regions rather than surface groups.
    #: Engines register their own volume-scoped collection ids here.
    region_scoped_collections = frozenset({'gmsh.volume_controls.controls'})

    def _scope_choices(self, _field='scope_token'):
        is_region = self.collection_id in self.region_scoped_collections
        kind = SelectionKind.REGION if is_region else SelectionKind.SURFACE_GROUP
        owner = 'prepared-regions' if is_region else 'prepared-groups'
        return [
            (
                entity.label, entity.stable_id, entity.status_label,
                entity.status is SelectionStatus.VALID,
            )
            for entity in app.selectionService.entities(
                kinds=(kind,))
            if entity.owner == owner
        ]

    def _scope_problem(self, row):
        scope_fields = tuple(
            key for key in self._descriptors
            if key == 'scope_token' or key.endswith('_scope_token'))
        for field in scope_fields:
            scope = str(row.get(field) or '').strip()
            entity = app.selectionService.entity(scope) if scope else None
            if not scope or entity is None:
                return self.tr(
                    'This row cannot be planned until every prepared geometry '
                    'scope is selected.')
            if entity.status is not SelectionStatus.VALID:
                return self.tr(
                    'This row references a stale or missing prepared geometry '
                    'scope.')
        return ''

    def _validated_child_values(self):
        values = self.child_values()
        scope_fields = tuple(
            key for key in self._editors
            if key == 'scope_token' or key.endswith('_scope_token'))
        selected = []
        for field in scope_fields:
            scope = values.get(field)
            entity = app.selectionService.entity(str(scope or ''))
            if not scope or entity is None:
                QMessageBox.warning(
                    self, self.tr('Geometry scope required'),
                    self.tr('Choose a prepared geometry scope before applying '
                            'this control.'))
                return None
            if entity.status is not SelectionStatus.VALID:
                QMessageBox.warning(
                    self, self.tr('Geometry scope unavailable'),
                    self.tr('The selected scope is stale or missing. Re-prepare '
                            'the geometry and choose a valid scope.'))
                return None
            selected.append(entity.stable_id)
        if selected:
            app.selectionService.select(tuple(selected))
        return values


def _sort_key(item):
    key = item[0]
    try:
        return (0, int(key))
    except (TypeError, ValueError):
        return (1, str(key))


def _display(value) -> str:
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    return '' if value is None else str(value)


def _humanise(name: str) -> str:
    if name == 'scope_token':
        return 'Geometry scope'
    out = []
    for character in str(name):
        if character.isupper() and out:
            out.append(' ')
        out.append(character)
    return ''.join(out).replace('_', ' ').strip().capitalize()
