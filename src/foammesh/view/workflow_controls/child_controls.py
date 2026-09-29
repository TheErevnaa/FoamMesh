"""Repeatable child controls for the Local Sizing and Boundary Layers tasks.

Plan 17 §5.10 and §5.13 model these as parent tasks owning ordered child items
with stable IDs.  The children are an ``IntKeyList`` collection in the schema,
so they are created, edited, and removed through the collection facade
operations (``<collection>.create`` / ``.patch`` / ``.remove``) rather than a
scalar configuration patch.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QMessageBox, QPushButton, QStyle, QStyleOptionHeaderV2, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.facade.field_adapters import EntityAdapter
from foammesh.core.facade.fields import REGISTRY, FieldType
from foammesh.app import app
from foammesh.core.selection import SelectionKind, SelectionStatus
from foammesh.view.facade_client import submit
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT

from .field_widgets import FieldEditor
from .labels import humanise_option


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


class _NumericFloorHeader(QHeaderView):
    """A horizontal header whose number columns hold a coordinate.

    DP-535. The floor is added where the header measures a section, so it
    follows the font the theme gives the header and leaves the height --
    padding and all -- to the style.
    """

    #: A sign and three digits each side of the point.
    SAMPLE = '-000.000'

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self._numeric: frozenset = frozenset()

    def set_numeric_columns(self, columns) -> None:
        self._numeric = frozenset(columns)

    def numeric_floor(self) -> int:
        margin = self.style().pixelMetric(
            self.style().PixelMetric.PM_HeaderMargin, None, self)
        return self.fontMetrics().horizontalAdvance(self.SAMPLE) + 4 * margin

    def sectionSizeFromContents(self, logical_index):       # noqa: N802
        return self.measure(logical_index, self._wrap)

    def measure(self, logical_index, wrap: bool):
        """The size one heading asks for, on one line or on two."""
        size = super().sectionSizeFromContents(logical_index)
        lines = self.wrapped_lines(logical_index, wrap)
        if len(lines) > 1:
            metrics = self._bold_metrics()
            text = self._text(logical_index)
            # What the style adds around the text -- margins, a sort arrow --
            # is whatever the one-line size carries beyond the line itself.
            chrome = max(0, size.width() - metrics.horizontalAdvance(text))
            size.setWidth(chrome + 2 + max(
                metrics.horizontalAdvance(line) for line in lines))
            size.setHeight(size.height()
                           + (len(lines) - 1) * metrics.lineSpacing())
        if logical_index in self._numeric:
            size.setWidth(max(size.width(), self.numeric_floor()))
        return size

    # -- DP-559: a heading that does not fit on one line takes two ---------- #

    #: Whether a heading of several words may break onto a second line.
    #: The panel turns it on only when the one-line headings would not fit.
    _wrap = False

    def set_wrapping(self, wrap: bool) -> bool:
        """Let headings break onto two lines; True when that changed."""
        wrap = bool(wrap)
        if wrap == self._wrap:
            return False
        self._wrap = wrap
        count = self.count()
        if count:
            # Drops the cached height and re-measures every section.
            self.headerDataChanged(self.orientation(), 0, count - 1)
        return True

    def wrapping(self) -> bool:
        return self._wrap

    def _text(self, logical_index) -> str:
        model = self.model()
        if model is None:
            return ''
        value = model.headerData(logical_index, self.orientation(),
                                 Qt.ItemDataRole.DisplayRole)
        return '' if value is None else str(value)

    def _bold_metrics(self) -> QFontMetrics:
        # A selected row draws every heading bold, and the column is not
        # re-measured when the selection changes, so it is measured bold.
        font = QFont(self.font())
        font.setBold(True)
        return QFontMetrics(font)

    def wrapped_lines(self, logical_index, wrap=None) -> list:
        """The lines one heading is drawn on: two at most, balanced."""
        wrap = self._wrap if wrap is None else wrap
        text = self._text(logical_index)
        words = text.split()
        if not wrap or len(words) < 2:
            return [text]
        metrics = self._bold_metrics()
        best = None
        for cut in range(1, len(words)):
            lines = [' '.join(words[:cut]), ' '.join(words[cut:])]
            widest = max(metrics.horizontalAdvance(line) for line in lines)
            if best is None or widest < best[0]:
                best = (widest, lines)
        return best[1]

    def paintSection(self, painter, rect, logical_index):   # noqa: N802
        lines = self.wrapped_lines(logical_index)
        if len(lines) < 2:
            super().paintSection(painter, rect, logical_index)
            return
        option = QStyleOptionHeaderV2()
        self.initStyleOption(option)
        self.initStyleOptionForIndex(option, logical_index)
        option.rect = rect
        option.text = '\n'.join(lines)
        # Eliding measures the two lines as one and would cut them back to
        # the single line this is here to avoid.
        option.textElideMode = Qt.TextElideMode.ElideNone
        painter.save()
        self.style().drawControl(QStyle.ControlElement.CE_Header, option,
                                 painter, self)
        painter.restore()


class ChildControlPanel(QGroupBox):
    """Table of child items plus an editor form for the selected row."""

    childrenChanged = Signal()
    #: FIELD-04. The prepared boundary the selected row refines, as the stable
    #: reference the catalogue knows it by -- empty when the row names none.
    #: W-O2, Plan 33 section 6 check 9: this used to say that a page owning a
    #: viewport listens to it to paint the surface, and no page does. The
    #: painting happens three lines below the emit, where this panel hands the
    #: reference to `app.selectionService.select`, and the viewport follows the
    #: selection service. What the signal is for is the other half: a page
    #: stacking several of these panels connects it so the sibling tables let
    #: go of their own row, and one selection stays one selection. The one
    #: listener in the product is `GmshSizeFieldsPage._scopeSelected`, which
    #: does exactly that and does not read the payload.
    selectedSurfaceChanged = Signal(str)

    def __init__(self, facade_client, collection_id: str, title: str,
                 columns=(), parent=None, *, choices=None, annotations=None,
                 headings=None, unscoped_note=None, stretch=None):
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
        # FIELD-07/CURVE-05. What one field is called on this page, where the
        # registry title names the store rather than the thing. `Surface id`
        # is the import order of a surface and `Geometry scope` is any scope
        # at all; the page that mounts the panel knows it is asking for a
        # boundary and for a face group, and says so. DP-162 still holds: the
        # heading over the column and the label over the editor are the same
        # string, because both are read from here.
        self._headings = {key: str(value)
                          for key, value in dict(headings or {}).items()
                          if str(value).strip()}
        self._adapter = collection_adapter(collection_id)
        self._descriptors = {relative_id: field.descriptor
                             for relative_id, field in self._adapter.fields.items()}
        self._columns = tuple(columns) or tuple(self._descriptors)
        # DP-569 (0924 rerun follow-up). The column that takes the width the
        # others leave over. By default the last one, which on most tables is
        # a number: it grew wide where a name would have used the room, and
        # being a number it raised every column's floor to a coordinate's
        # width (DP-535). A page names the column that identifies a row
        # instead, so a long name is the thing that gets read in full.
        self._stretch = (self._columns.index(stretch)
                         if stretch in self._columns
                         else len(self._columns) - 1)
        self._editors: dict[str, FieldEditor] = {}
        self._rows: list[dict] = []
        self.setObjectName(collection_id.replace('.', '_') + 'Panel')
        # The editors below are built from the catalogue, so the catalogue is
        # brought up to date before they are, and again on every refresh.
        self._live = False
        self._scope_fingerprints: dict[str, tuple] = {}
        self._synchronizePreparedCase()
        # CURVE-01/FIELD-02. Whatever changes the catalogue -- preparing the
        # geometry again, opening another case -- changes what this picker
        # may offer, and the panel is not always the thing that caused it.
        app.selectionService.state_changed.connect(self._onCatalogueChanged)

        layout = QVBoxLayout(self)
        # DP-518. The style's 8 px margins and 6 px spacing, on a panel that
        # three stage pages stack three deep, were padding the reader scrolls
        # past; the group box's own frame and title still separate panels.
        # The tight steps of the house scale, not hand-typed numbers.
        layout.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT,
                                  MARGIN_TIGHT, MARGIN_TIGHT)
        layout.setSpacing(GAP_TIGHT)
        self.table = QTableWidget(0, len(self._columns), self)
        self.table.setHorizontalHeader(_NumericFloorHeader(self.table))
        self.table.setHorizontalHeaderLabels(
            [self._heading(name) for name in self._columns])
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
        # Plan 33 OF-07. `setStretchLastSection` grows the last column into
        # spare width but never shrinks it below `defaultSectionSize`, which
        # is 100 px, so a table whose columns already fill the viewport paid
        # 100 px for its last heading however short that heading is and
        # scrolled sideways by the difference. MEASURED on the snappy layer
        # table at a 560 px settings column, in the product font: columns
        # [83, 131, 84, 100, 100] against a 492 px viewport, 6 px of scroll.
        # Stretching the last section by resize mode instead lets it fall to
        # `minimumSectionSize`: the same table measures [83, 131, 84, 100, 94]
        # and no scroll, and at a 480 px column the last falls to 55. It still
        # grows the same way when there is room, so R59/R151 is untouched --
        # every other column is still sized to its own contents.
        if self._columns:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(self._stretch,
                                        QHeaderView.ResizeMode.Stretch)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        for column, name in enumerate(self._columns):
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(self._heading(name))
        self._floor_numeric_columns()
        # DP-559. Re-fit the headings whenever the width they share changes.
        self.table.viewport().installEventFilter(self)
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

        # Plan 33 FORM-03/LAYER-05. Two wrapped lines under an empty table
        # on every child collection in the product -- MEASURED, the sentence
        # was on screen nine times across the twenty-nine task pages, and it
        # said what an empty table already says. The table itself is the
        # empty state; the words a reader cannot see from the table are the
        # table's accessible description, set in `refresh`.
        self._emptyDescription = self.tr(
            'No items yet. Leaving this empty is recorded as a skip.')

        # DP-496 (audit MA-06). A row of a scoped collection names prepared
        # geometry, and the facade refuses one that names none -- so before
        # the geometry was prepared, Add opened a form whose scope pickers
        # were empty and whose OK could only be refused. Add now says what it
        # is waiting for instead of opening that form, and the button carries
        # the same sentence before it is pressed. A page opts in by passing
        # the sentence: panels whose scope comes from elsewhere keep Add.
        self._unscopedNote = str(unscoped_note) if unscoped_note else ''

        buttons = QHBoxLayout()
        buttons.setSpacing(GAP_TIGHT)
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
        # DP-518. The scrollbar is reserved only while there is something to
        # scroll, so the height follows the columns as they are sized.
        self.table.horizontalScrollBar().rangeChanged.connect(
            lambda *_range: self._fit_table_height())

        self.refresh()
        self._live = True

    # -- the editor, which is a dialog rather than a column of rows --------- #

    def _build_field_editor(self, key, descriptor) -> FieldEditor:
        choices = (
            self._scope_choices(key) if self._is_scope_field(key)
            else self._choices.get(key))
        if self._is_scope_field(key):
            # What this editor was built from, so a later catalogue change
            # can be told from a mere selection change without rebuilding a
            # live widget to find out.
            self._scope_fingerprints[key] = tuple(
                tuple(option) for option in (choices or ()))
        editor = FieldEditor(descriptor, self._form, choices=choices)
        # DP-162. The heading over the column and the label over the editor
        # are the same name, so they are spelled in the same place.
        label = self._label_for(key, descriptor)
        if label != editor.label.text():
            editor.label.setText(self.tr(label))
        if self._is_scope_field(key):
            editor.editor.setAccessibleName(self.tr('Stable geometry scope'))
        return editor

    def _floor_numeric_columns(self) -> None:
        """Give every real-number column room for a coordinate.

        DP-535 (audit 0924, S5/S6). Sized to contents, a column headed `X`
        is as wide as `X` and whatever values it holds at the moment, so the
        region table drew X and Y as slivers beside a Z that took the rest
        of the row as the stretched last column -- and that last column
        falls to `minimumSectionSize` once the others fill the row, which
        cut a coordinate down to its sign. A number column now asks for the
        width of a coordinate, and when the stretched column is a number the
        header will not squeeze any column below that.
        """
        header = self.table.horizontalHeader()
        numeric = {column for column, name in enumerate(self._columns)
                   if getattr(self._descriptors.get(name), 'value_type', None)
                   == FieldType.NUMBER}
        header.set_numeric_columns(numeric)
        if self._columns and self._stretch in numeric:
            header.setMinimumSectionSize(
                max(header.minimumSectionSize(), header.numeric_floor()))

    def _heading(self, name: str) -> str:
        """The text over one column of the table."""
        return self.tr(self._label_for(name, self._descriptors.get(name)))

    def _label_for(self, name: str, descriptor=None) -> str:
        """The one name a field is shown under on this panel."""
        return self._headings.get(name) or _field_label(name, descriptor)

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
        if options:
            self._choices[key] = options
        else:
            self._choices.pop(key, None)
        self._rebuild_editor(key)

    def _rebuild_editor(self, key: str) -> None:
        """Replace one editor with a fresh one over the choices as they are.

        The widget is rebuilt rather than repopulated because a plain text box
        and a combo are different widgets, and the dialog is dropped so the
        next open lays out the new one. Whatever was typed is carried over.
        """
        old = self._editors.get(key)
        previous = old.value() if old is not None else None
        self._editors[key] = self._build_field_editor(
            key, self._descriptors[key])
        if previous not in (None, ''):
            self._editors[key].set_value(previous)
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        self._discard_dialog()

    def _discard_dialog(self) -> None:
        """Drop the dialog so the next open lays the current editors out.

        DP-495 (audit MA-07). The dialog borrows the editors' widgets, so it
        gives them back before it goes; deleting it with them inside took the
        untouched editors down too, and the next Add died on a deleted text
        box.
        """
        dialog = self._dialog
        self._dialog = None
        if dialog is None:
            return
        dialog.releaseEditors(self._form)
        dialog.setParent(None)
        dialog.deleteLater()

    def refreshChoices(self) -> None:
        """Re-read the scope catalogue and rebuild every scope picker from it.

        CURVE-01/CURVE-02/FIELD-02. The pickers were filled once, in the
        constructor, from a catalogue that was itself filled once. A panel
        built before the geometry was prepared offered nothing for the rest of
        the sitting, and a panel built before a second case was opened went on
        offering the first case as `Missing geometry`. This is called from
        `refresh` and from both dialog openings, so the list a user reads was
        read after the last thing that could have changed it.
        """
        self._synchronizePreparedCase()
        self._followCatalogue()

    def _onCatalogueChanged(self, _state=None) -> None:
        """The catalogue moved; the pickers move with it."""
        if not self._live:
            return
        try:
            self._followCatalogue()
            self._updateScopeReadiness()
        except RuntimeError:
            # The panel was destroyed and the catalogue outlived it.
            try:
                app.selectionService.state_changed.disconnect(
                    self._onCatalogueChanged)
            except (KeyError, LookupError, TypeError, ValueError):
                pass

    def _followCatalogue(self) -> None:
        """Rebuild the scope pickers whose options are no longer what they were.

        Guarded by what each editor was built from, because the catalogue also
        emits on a plain selection change -- and rebuilding the widget a user
        is typing in, sixty times a drag, is its own defect.
        """
        if self._dialog is not None and self._dialog.isVisible():
            # Not under an open dialog: the openings rebuild first, which is
            # the moment in the lifecycle this is for.
            return
        for key in tuple(self._descriptors):
            if not self._is_scope_field(key):
                continue
            fingerprint = tuple(
                tuple(option) for option in self._scope_choices(key))
            if fingerprint != self._scope_fingerprints.get(key):
                self._rebuild_editor(key)

    def _synchronizePreparedCase(self) -> None:
        """Republish the catalogue for the open case, if there is one.

        Advisory, and belt-and-braces beside the facade hooks: a panel is
        allowed to be built in a process where nothing has prepared anything.
        """
        project = getattr(app, 'project', None)
        if project is None:
            return
        try:
            app.selectionService.synchronize_prepared_case(
                project.path, app.facadeClient.checkout())
        except (AttributeError, FileNotFoundError, OSError, ValueError):
            pass

    def editor_dialog(self):
        """The shared editor, built on first use.

        One instance per panel: the editors are the panel's, and moving them
        between dialogs on every open would reparent live widgets for nothing.
        """
        if self._dialog is None:
            from .child_editor_dialog import ChildEditorDialog, relevance_rules

            self._dialog = ChildEditorDialog(
                self.title(), self._editors, self,
                relevance=relevance_rules(self.collection_id),
                annotations=self._annotations)
        return self._dialog

    def open_add_dialog(self) -> None:
        """Edit a fresh item, then create it.

        Reset to schema defaults first: opening on whatever the last selected
        row happened to hold means a user who presses Add and then OK has
        silently duplicated a row they were only looking at.
        """
        self.refreshChoices()
        if self.awaitsPreparedScope():
            QMessageBox.information(
                self, self.tr('Geometry not prepared yet'),
                self._unscopedNote)
            return
        for key, editor in self._editors.items():
            # DP-595: a panel may start a new row from something other than
            # the schema default (see ``fresh_value``).
            editor.set_value(self.fresh_value(key, editor.descriptor))
        dialog = self.editor_dialog()
        # `set_value` blocks signals, so the rows do not re-evaluate on their
        # own. The dialog is reused, and one opened on a rotational pair would
        # otherwise still be showing rotation rows over a coincident one.
        dialog.applyRelevance()
        self._run_editor(dialog, self.add_child)

    def fresh_value(self, key: str, descriptor):
        """The value a new row's *key* starts from (DP-595 hook)."""
        return descriptor.default

    def open_edit_dialog(self, *_args) -> None:
        if self.selected_key() is None:
            return
        self.refreshChoices()
        self._load_selected()
        dialog = self.editor_dialog()
        dialog.applyRelevance()
        self._run_editor(dialog, self.apply_selected)

    def _run_editor(self, dialog, accepted) -> None:
        """Show the editor, and call *accepted* if it closes on OK.

        Plan 36 RP2. Every panel edits its row in a modal dialog, and that
        stays the rule: the collections these tables hold are edited as forms
        and nothing else on screen means anything while one is open. A panel
        whose row is placed *in the viewport* -- a region's seed -- cannot use
        a modal dialog, because Qt refuses the viewport every mouse event
        while `exec()` runs. Such a panel overrides this to show the same
        dialog without blocking and to call *accepted* when OK is pressed.
        """
        if dialog.exec() == QDialog.DialogCode.Accepted:
            accepted()

    # -- data -------------------------------------------------------------- #

    def rows(self) -> list[dict]:
        return list(self._rows)

    def refresh(self) -> None:
        if self._live:
            self.refreshChoices()
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
        self.table.setAccessibleDescription(
            self._emptyDescription if empty else '')
        self._edit.setEnabled(not empty)
        self._remove.setEnabled(not empty)
        self._updateScopeReadiness()
        self._fit_headings()
        self._fit_table_height()
        if self._rows and not self.table.selectedItems():
            self.table.selectRow(0)

    def _scope_fields(self) -> tuple:
        return tuple(key for key in self._descriptors
                     if self._is_scope_field(key))

    def awaitsPreparedScope(self) -> bool:
        """True while this panel's rows need a scope and none can be chosen.

        DP-496. Only a panel built with an `unscoped_note` waits, and only on
        the scope fields its page does not offer as a picker of its own.
        """
        if not self._unscopedNote:
            return False
        fields = tuple(key for key in self._scope_fields()
                       if key not in self._choices)
        if not fields:
            return False
        return not any(
            enabled for key in fields
            for _label, _value, _status, enabled in self._scope_choices(key))

    def _updateScopeReadiness(self) -> None:
        if not self._unscopedNote:
            return
        waiting = self.awaitsPreparedScope()
        self._add.setToolTip(self._unscopedNote if waiting else '')
        self._add.setAccessibleDescription(
            self._unscopedNote if waiting else '')

    #: Past this the table scrolls. This panel sits under the thing it
    #: annotates -- the geometry tree -- and a table that keeps growing takes
    #: that space from the tree, which is what the user is actually looking at.
    MAX_VISIBLE_ROWS = 6

    def eventFilter(self, watched, event):                   # noqa: N802
        if (event.type() == QEvent.Type.Resize
                and watched is self.table.viewport()):
            self._fit_headings()
        return super().eventFilter(watched, event)

    def _column_need(self, column: int, wrap: bool) -> int:
        """The width one column asks for: its heading or its widest cell."""
        header = self.table.horizontalHeader()
        return max(header.measure(column, wrap).width(),
                   self.table.sizeHintForColumn(column),
                   header.minimumSectionSize())

    def headings_fit(self) -> bool:
        """Whether every column has the width its heading and cells ask."""
        header = self.table.horizontalHeader()
        wrap = header.wrapping()
        return all(header.sectionSize(column) >= self._column_need(column, wrap)
                   for column in range(len(self._columns)))

    def _fit_headings(self) -> None:
        """Break a long heading onto two lines when one line does not fit.

        DP-559 (0924 rerun, S6). Sized to contents, the snappy surface
        refinement table asked 430 px of a 302 px table: the last column is
        the stretched one, so it fell to its 65 px floor and `Feature edge
        refinement level` read `ge refine`. A heading of several words now
        takes a second line when the one-line headings would pass the
        table's width, which is what lets four columns share a settings
        column without a sideways scroll.
        """
        if not self._columns:
            return
        header = self.table.horizontalHeader()
        available = self.table.viewport().width()
        one_line = sum(self._column_need(column, False)
                       for column in range(len(self._columns)))
        if header.set_wrapping(one_line > available):
            header.resizeSections()
            self._fit_table_height()

    def _fit_table_height(self) -> None:
        """Size the table to its contents, floored at two rows.

        An empty table stretched to fill claims a third of the panel to say
        nothing. Plan 33: the two floored rows are what carry the empty
        state, and the sentence that used to sit under them is the table's
        accessible description.
        """
        rows = max(2, min(len(self._rows), self.MAX_VISIBLE_ROWS))
        row_height = self.table.verticalHeader().defaultSectionSize()
        header = self.table.horizontalHeader().height()
        # R59. The columns are sized to their contents now, so the table can
        # be wider than the panel and needs somewhere to put the horizontal
        # scrollbar; without this it is drawn over the last row.
        # DP-518. Only then: MEASURED on the snappy Castellation page, each of
        # three empty tables reserved a scrollbar under two empty rows with
        # nothing to scroll. `rangeChanged` re-fits when one is needed.
        bar = self.table.horizontalScrollBar()
        scrollbar = bar.sizeHint().height() if bar.maximum() > 0 else 0
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
        """Re-key one stored element from storage keys to relative IDs.

        DP-533 (audit MA24-01). A field nested in the element -- a region's
        `point/x`, a surface group's `surfaceRefinement/minimumLevel` -- is
        stored as nested dictionaries, so looking its whole path up as one
        key found nothing: the table showed those cells blank, and the row
        editor opened on the schema default and wrote that back on OK.
        """
        row = {}
        for relative_id, field in self._adapter.fields.items():
            node = stored
            for part in field.relative_path.split('/'):
                if not isinstance(node, dict) or part not in node:
                    break
                node = node[part]
            else:
                row[relative_id] = node
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
        # FIELD-04. A per-surface size names its boundary by reference rather
        # than through `scope_token`, and selecting its row highlighted
        # nothing: the one control whose whole subject is a single surface was
        # the one that never said which. The reference is the same stable id
        # the catalogue is keyed by, so it highlights the same way.
        reference = self._selected_surface_reference(row)
        self.selectedSurfaceChanged.emit(reference)
        if reference and app.selectionService.entity(reference) is not None:
            scopes = scopes + (reference,)
        if scopes:
            try:
                # A geometry-level interface row owns two faces. Selecting the
                # row must therefore highlight both the master and slave, not
                # just scalar-control rows that happen to use ``scope_token``.
                app.selectionService.select(scopes)
            except ValueError:
                pass

    def _selected_surface_reference(self, row: dict) -> str:
        """The prepared geometry one row is about, if it is about one.

        CURVE-05. A per-surface size names its boundary through
        ``surface_ref``; an edge control names a face group through
        ``scope_token``, and the faces of that group are what the viewport has
        to paint for the reader to see which curves the control reaches. Both
        are the same stable id the catalogue is keyed by, so both travel on
        the same signal. A row scoped to a region is left out: a volume is not
        a surface, and painting one as though it were would be a lie about
        what the control touches.
        """
        for key in self._descriptors:
            if key == 'surface_ref' or key.endswith('_surface_ref'):
                value = str(row.get(key) or '').strip()
                if value:
                    return value
        if self.collection_id in self.region_scoped_collections:
            return ''
        value = str(row.get('scope_token') or '').strip()
        return value

    # -- mutation ---------------------------------------------------------- #

    def child_values(self) -> dict:
        values = {key: editor.value() for key, editor in self._editors.items()}
        # DP-531. A scope the item's kind does not have is stored as none.
        # The picker on a Box size field is hidden, but a combo always holds
        # an option, so the row was saved scoped to whichever surface group
        # happened to be listed first -- a choice nobody made.
        for key in self._scope_fields():
            if key in values and not self._applies(key, values):
                values[key] = ''
        return values

    def _applies(self, key: str, values: dict) -> bool:
        """Whether *key* is a field the item described by *values* has.

        DP-531. The same rule the editor dialog hides its rows by
        (`RELEVANCE`), read from the values rather than from which rows are
        on screen, so a headless commit is judged the way a dialog is.
        """
        from .child_editor_dialog import relevance_rules

        # DP-596: every rule that names *key* must keep it.
        for controlling, branches in relevance_rules(self.collection_id):
            conditional = frozenset().union(*branches.values()) \
                if branches else frozenset()
            if key not in conditional:
                continue
            value = values.get(controlling)
            value = getattr(value, 'value', value)
            applicable = branches.get(str(value))
            # An unrecognised kind hides nothing, as in the dialog.
            if applicable is not None and key not in applicable:
                return False
        return True

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
            if not self._applies(field, row):
                # DP-531. A Box size field has no scope to be missing.
                continue
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
            if not self._applies(field, values):
                # DP-531. A hidden scope is not validated: its row cannot be
                # seen, so a refusal over it could not be acted on.
                continue
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
    """The label one control or one stored value is shown under.

    DP-143. The word-splitting and the proper-name table are shared with
    `field_widgets` now -- they disagreed before, and the registry-built
    combos were the copy that got it wrong. The one thing that stays here is
    `scope_token`, which is a field name rather than a value and reads as
    nothing useful whichever way it is split.
    """
    if name == 'scope_token':
        return 'Geometry scope'
    return humanise_option(name)


def _field_label(name: str, descriptor=None) -> str:
    """The one name a stored field is shown under.

    DP-162. There were two of them. The heading over a column was spelled
    from the storage key the column reads, and the editor that edits that
    column was spelled from the field's own title -- so seventeen of the
    ninety-one columns across the two pipelines named one setting twice, in
    two different vocabularies, and five of them named it with the raw path
    it is stored under. `Surface refinement.minimum level` stood over a
    column whose editor is headed `Minimum Level`; `Point.x`, `Point.y` and
    `Point.z` stood over editors headed `X`, `Y` and `Z`; `Points` stood
    over `Through points` and `Zone mode` over `Cell zone side`. A user
    reads the table to find the row worth changing and then changes it in a
    dialog that calls the setting something else.

    So the title wins, for both, wherever the registry has one. The scope
    family is the one thing that stays spelled here: `master_scope_token`
    is a field name rather than a value, its schema title spells the
    storage word `token`, and neither the splitter nor the title reads as
    anything a user would recognise. It is spelled once now, and the
    heading and the editor take that one spelling.
    """
    if name == 'scope_token' or name.endswith('_scope_token'):
        prefix = name[:-len('scope_token')].strip('_')
        if not prefix:
            return 'Geometry scope'
        return '%s geometry scope' % humanise_option(prefix)
    title = str(getattr(descriptor, 'title', '') or '').strip()
    return title or _humanise(name)
