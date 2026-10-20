"""One outline of every refinement the case asks for, bands under their group.

Plan 40 UXD9 (#44). The Castellation page stacked four tables -- surface
groups, volume groups, feature-edge bands and volume bands -- each with its
own *Add*, so the reader chose a table before knowing which of the four
things they meant, and a band sat in a table of its own with a group name to
type or pick. This is one outline instead: a surface group or a volume group
is a top-level row, its bands are rows under it, and one *Add refinement*
chooser offers the four things by name. A band is added under the group that
is selected.

The four ``ChildControlPanel`` tables stay exactly what they were -- the
editors, the dialogs and the facade operations are theirs -- and are no
longer drawn. This outline reads their rows and hands every press to them.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QMenu, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from foammesh.core import display_unit

#: What each of the four collections is called to the reader, and which
#: groups a band belongs under.
SURFACE = 'surface'
VOLUME = 'volume'
FEATURE_BAND = 'feature_band'
VOLUME_BAND = 'volume_band'

KIND_ROLE = Qt.ItemDataRole.UserRole
KEY_ROLE = Qt.ItemDataRole.UserRole + 1

#: The chooser, in the order a case is usually built.
CHOICES = (
    (SURFACE, 'Surface refinement…',
     'Splits the base cells near a surface group.'),
    (VOLUME, 'Volume refinement…',
     'Splits the base cells inside a named region.'),
    (FEATURE_BAND, 'Feature-edge band…',
     'Grades the feature-edge refinement of a surface group with distance. '
     'Added under the surface group that is selected.'),
    (VOLUME_BAND, 'Volume distance band…',
     'Grades a distance-mode volume group with distance. Added under the '
     'volume group that is selected.'),
)

#: Why a band cannot be added yet.
NO_GROUP = {
    FEATURE_BAND: 'Add a surface refinement first: a band grades one.',
    VOLUME_BAND: 'Add a volume refinement first: a band grades one.',
}


def _text(value) -> str:
    return '' if value is None else str(value)


class RefinementOutline(QGroupBox):
    """The tree, the chooser and the two row buttons."""

    def __init__(self, panels: dict, parent=None):
        super().__init__('', parent)
        #: ``{kind: ChildControlPanel}`` for the four collections.
        self._panels = dict(panels)
        self.setObjectName('refinementOutline')
        self.setAccessibleName(self.tr('Refinement'))
        self.setTitle(self.tr('Refinement'))
        layout = QVBoxLayout(self)

        self.tree = QTreeWidget(self)
        self.tree.setObjectName('refinementOutlineTree')
        self.tree.setHeaderLabels((self.tr('Refinement'), self.tr('Detail')))
        self.tree.setAccessibleName(self.tr('Refinement groups and bands'))
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        # DP-2813. In the 1280x800 column the two columns were wider than
        # the tree, so a horizontal scroll bar opened inside the fixed
        # header-plus-rows height and covered the one row. The tree never
        # scrolls sideways: the detail column is as wide as its text, the
        # name column takes what is left and elides (its tooltip keeps the
        # whole name).
        self.tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_args: self.edit())
        layout.addWidget(self.tree)

        buttons = QHBoxLayout()
        self.add_button = QPushButton(self.tr('Add refinement'), self)
        self.add_button.setObjectName('refinementAdd')
        self.menu = QMenu(self.add_button)
        self._actions = {}
        for kind, label, tip in CHOICES:
            action = self.menu.addAction(self.tr(label))
            action.setToolTip(self.tr(tip))
            action.triggered.connect(
                lambda _checked=False, kind=kind: self.add(kind))
            self._actions[kind] = action
        self.add_button.setMenu(self.menu)
        self.edit_button = QPushButton(self.tr('Edit…'), self)
        self.edit_button.setObjectName('refinementEdit')
        self.remove_button = QPushButton(self.tr('Remove'), self)
        self.remove_button.setObjectName('refinementRemove')
        self.edit_button.clicked.connect(self.edit)
        self.remove_button.clicked.connect(self.remove)
        # FIX-H (DP-573's rule). Add on its own row, Edit and Remove on the
        # next: the three on one row asked 300 px, and the Castellation page
        # body 316 px inside the 314 px compact column, cut on its right.
        buttons.addWidget(self.add_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        row_actions = QHBoxLayout()
        for button in (self.edit_button, self.remove_button):
            row_actions.addWidget(button)
        row_actions.addStretch(1)
        layout.addLayout(row_actions)

        for panel in self._panels.values():
            panel.childrenChanged.connect(self._rowsChanged)
        self.refresh()

    # -- what is shown ------------------------------------------------------- #

    def _rowsChanged(self) -> None:
        """A table wrote a row: redraw the tree from the tables' rows.

        Only the tree. The page's own fields are not re-read here, so a
        value typed above the table survives the row (Plan 37 UF20).
        """
        self.refresh()

    def _cell(self, kind: str, key: str, row: dict) -> str:
        panel = self._panels[kind]
        return panel._cell_text(key, row.get(key))[0]

    def detail(self, kind: str, row: dict) -> str:
        """One line saying what the row asks for."""
        if kind == SURFACE:
            low = self._cell(kind, 'surface_refinement.minimum_level', row)
            high = self._cell(kind, 'surface_refinement.maximum_level', row)
            return self.tr('Levels %s to %s') % (low, high)
        if kind == VOLUME:
            mode = self._cell(kind, 'mode', row)
            level = self._cell(kind, 'volume_refinement_level', row)
            return self.tr('%s, level %s') % (mode, level)
        distance = row.get('distance')
        text = (display_unit.format_length(distance)
                if isinstance(distance, (int, float))
                and not isinstance(distance, bool) else _text(distance))
        return self.tr('Within %s: level %s') % (
            text, self._cell(kind, 'level', row))

    def _item(self, kind: str, row: dict, name: str) -> QTreeWidgetItem:
        detail = self.detail(kind, row)
        item = QTreeWidgetItem((name, detail))
        item.setToolTip(0, name)   # DP-2813: the name may be elided
        item.setToolTip(1, detail)
        item.setData(0, KIND_ROLE, kind)
        item.setData(0, KEY_ROLE, row.get('__key__'))
        return item

    def refresh(self) -> None:
        """Rebuild from the four tables' rows, keeping the selection."""
        selected = self.selected()
        self.tree.blockSignals(True)
        self.tree.clear()
        parents = {}
        for kind, band_kind in ((SURFACE, FEATURE_BAND),
                                (VOLUME, VOLUME_BAND)):
            for row in self._panels[kind].rows():
                name = _text(row.get('group_name')) or self.tr('(unnamed)')
                item = self._item(kind, row, name)
                self.tree.addTopLevelItem(item)
                parents.setdefault((band_kind, _text(row.get('group_name'))),
                                   item)
            for row in self._panels[band_kind].rows():
                owner = parents.get((band_kind, _text(row.get('group_name'))))
                band = self._item(band_kind, row, self.detail(band_kind, row))
                band.setText(1, '')
                band.setToolTip(1, '')
                if owner is None:
                    # A band whose group is gone is shown, not dropped.
                    band.setText(0, '%s — %s' % (
                        self.tr('No group "%s"') % _text(
                            row.get('group_name')), self.detail(
                                band_kind, row)))
                    band.setToolTip(0, band.text(0))
                    self.tree.addTopLevelItem(band)
                else:
                    owner.addChild(band)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._fit_tree()
        if selected is not None:
            self.select(*selected)
        self._sync_buttons()
        for band_kind, action in ((FEATURE_BAND, self._actions[FEATURE_BAND]),
                                  (VOLUME_BAND, self._actions[VOLUME_BAND])):
            owner_kind = SURFACE if band_kind == FEATURE_BAND else VOLUME
            has_group = bool(self._panels[owner_kind].rows())
            action.setEnabled(has_group)
            action.setToolTip(
                self.tr([c[2] for c in CHOICES if c[0] == band_kind][0])
                if has_group else self.tr(NO_GROUP[band_kind]))

    #: Plan 44 MF27. The tree is as tall as its rows, up to this many; past
    #: it, it scrolls. Left to its size hint it stood 192 px with no row in
    #: it, which took Castellation past the 300 px sheet.
    FIT_ROWS = 8

    def _fit_tree(self) -> None:
        """Hold the tree at its header plus its shown rows (the autofit
        ceiling every sheet table has, ``theming.metrics.autofit_table``)."""
        tree = self.tree
        header = tree.header()
        head = 0
        if header is not None and not header.isHidden():
            head = max(header.height() if header.isVisible() else 0,
                       header.sizeHint().height())
        rows = []
        item = tree.topLevelItem(0) if tree.topLevelItemCount() else None
        while item is not None and len(rows) < self.FIT_ROWS:
            rows.append(item)
            item = tree.itemBelow(item)
        body = 0
        for row in rows:
            height = tree.visualItemRect(row).height()
            if height <= 0:
                height = max(tree.sizeHintForRow(0),
                             tree.fontMetrics().height() + 6)
            body += height
        tree.setFixedHeight(head + body + 2 * tree.frameWidth())

    def outline(self) -> list:
        """``[(kind, name, detail, [(kind, text), ...])]`` for a test."""
        found = []
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            found.append((
                item.data(0, KIND_ROLE), item.text(0), item.text(1),
                [(item.child(child).data(0, KIND_ROLE),
                  item.child(child).text(0))
                 for child in range(item.childCount())]))
        return found

    # -- selection ------------------------------------------------------------ #

    def selected(self):
        """``(kind, key)`` of the selected row, or ``None``."""
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, KIND_ROLE), items[0].data(0, KEY_ROLE)

    def select(self, kind: str, key) -> bool:
        iterator = [self.tree.topLevelItem(i)
                    for i in range(self.tree.topLevelItemCount())]
        stack = list(iterator)
        while stack:
            item = stack.pop(0)
            if item.data(0, KIND_ROLE) == kind and item.data(0, KEY_ROLE) == key:
                self.tree.setCurrentItem(item)
                return True
            stack.extend(item.child(i) for i in range(item.childCount()))
        return False

    def _sync_buttons(self) -> None:
        has = self.selected() is not None
        self.edit_button.setEnabled(has)
        self.remove_button.setEnabled(has)

    def _group_of_selection(self, owner_kind: str):
        """The name of the group a new band should go under, or ''."""
        items = self.tree.selectedItems()
        if not items:
            return ''
        item = items[0]
        if item.parent() is not None:
            item = item.parent()
        if item.data(0, KIND_ROLE) != owner_kind:
            return ''
        key = item.data(0, KEY_ROLE)
        for row in self._panels[owner_kind].rows():
            if row.get('__key__') == key:
                return _text(row.get('group_name'))
        return ''

    def _select_in_table(self, kind: str, key) -> bool:
        panel = self._panels[kind]
        for index, row in enumerate(panel.rows()):
            if row.get('__key__') == key:
                panel.table.clearSelection()
                panel.table.selectRow(index)
                return True
        return False

    # -- the presses ---------------------------------------------------------- #

    def add(self, kind: str) -> None:
        """Open the new-row editor of one of the four collections."""
        panel = self._panels[kind]
        if kind in (FEATURE_BAND, VOLUME_BAND):
            owner = SURFACE if kind == FEATURE_BAND else VOLUME
            group = self._group_of_selection(owner)
            # Read by `fresh_value` of the band panels, so the dialog opens
            # on the group the reader was looking at.
            panel.preferred_group = group
        try:
            panel.open_add_dialog()
        finally:
            panel.preferred_group = ''

    def edit(self) -> None:
        chosen = self.selected()
        if chosen is None or not self._select_in_table(*chosen):
            return
        self._panels[chosen[0]].open_edit_dialog()

    def remove(self) -> None:
        chosen = self.selected()
        if chosen is None or not self._select_in_table(*chosen):
            return
        self._panels[chosen[0]].remove_selected()
