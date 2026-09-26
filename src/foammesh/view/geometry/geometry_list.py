#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import IntEnum, auto

from PySide6.QtWidgets import (QColorDialog, QHBoxLayout, QHeaderView, QLabel,
                               QTreeWidget, QTreeWidgetItem, QWidget)
from PySide6.QtCore import Qt, Signal, QObject, QCoreApplication

from foammesh.app import app
from foammesh.core.geometry.patches.ops import (
    BOUNDARY_CATEGORIES, boundary_category_for_name)
from foammesh.db.configurations_schema import CFDType, GeometryType
from foammesh.view.theming.metrics import MARGIN_TIGHT
from foammesh.view.theming.status_colors import apply_color_swatch
from widgets.themed_icon import load_themed_icon

VOLUME_ICON_FILE = ':/graphicsIcons/volume.svg'
SURFACE_ICON_FILE = ':/graphicsIcons/face.svg'

def cfdTypeToText(cfdType):
    return {
        CFDType.NONE.value: QCoreApplication.translate('GeometryPage', 'None'),
        CFDType.CELL_ZONE.value: QCoreApplication.translate('GeometryPage', 'CellZone'),
        CFDType.BOUNDARY.value: QCoreApplication.translate('GeometryPage', 'Boundary'),
        CFDType.INTERFACE.value: QCoreApplication.translate('GeometryPage', 'Interface'),
    }.get(cfdType)


def defaultBoundaryCategory():
    """The category a patch adopts when its name announces none.

    Read from the project rather than assumed so the list agrees with what the
    publication step will actually write. Any failure to read it falls back to
    the schema default, which is ``wall``.
    """
    try:
        value = app.facadeClient.checkout().getValue(
            'geometryPreparation/defaultBoundaryCategory')
    except Exception:
        return 'wall'
    text = str(value).split('.')[-1].strip().lower()
    return text if text in BOUNDARY_CATEGORIES else 'wall'


def typeColumnText(geometry):
    """Type-column text, including the boundary category for a boundary.

    R69. The column used to read ``Boundary`` for every surface, so the
    wall/inlet/outlet category that decides the published patch type was shown
    nowhere on the page. MEASURED on a five-patch tee: the list said
    ``Boundary`` five times while ``constant/polyMesh/boundary`` had written
    ``outlet_top`` and ``outlet_branch`` as walls, and the only way to find
    that out was to read the published file by hand. The category is derived
    from the name by exactly the rule publication uses, so what the list shows
    is what the solver will get.
    """
    if geometry.value('cfdType') == CFDType.INTERFACE.value and geometry.value('interRegion'):
        text = QCoreApplication.translate('GeometryPage', 'Interface(R)')
    else:
        text = cfdTypeToText(geometry.value('cfdType'))

    if (geometry.value('cfdType') == CFDType.BOUNDARY.value
            and geometry.value('gType') == GeometryType.SURFACE.value):
        category = boundary_category_for_name(geometry.value('name'),
                                              defaultBoundaryCategory())
        return f'{text} ({category})'

    return text


class Column(IntEnum):
    NAME_COLUMN = 0
    TYPE_COLUMN = auto()
    #: GEO-03. Last, like the colour column of Display control, so the two
    #: indices every other reader of this tree already uses do not move.
    COLOUR_COLUMN = auto()


class ColorSwatch(QLabel):
    """The colour a boundary is drawn in, and the way to change it.

    GEO-03/GEO-07. A feature split leaves seven rows called
    ``<part>_1`` .. ``<part>_7``, and the colour each is drawn in was the only
    thing on screen that said which row was the inlet -- shown in the viewport
    and nowhere in the list. Clicking a row to find out re-selected it and
    painted it the highlight colour, which is not its colour.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(16, 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()


class GeometryItem(QTreeWidgetItem):
    def __init__(self, gId, geometry):
        super().__init__(int(gId))

        self._geometry = None

        self.setGeometry(geometry)

    def gId(self):
        return str(self.type())

    def geometry(self):
        return self._geometry

    def isVolume(self):
        return self._geometry.value('gType') == GeometryType.VOLUME.value

    def isSurface(self):
        return self._geometry.value('gType') == GeometryType.SURFACE.value

    def setGeometry(self, geometry):
        name = geometry.value('name')
        kind = typeColumnText(geometry)
        self.setText(Column.NAME_COLUMN, name)
        self.setText(Column.TYPE_COLUMN, kind)
        # DP-510. A name longer than the column is elided, so the whole of
        # it is what the row says when the pointer rests on it.
        self.setToolTip(Column.NAME_COLUMN, str(name or ''))
        self.setToolTip(Column.TYPE_COLUMN, str(kind or ''))

        self._geometry = geometry

    def retranslate(self):
        self.setGeometry(self._geometry)


class GeometryList(QObject):
    selectedItemsChanged = Signal()

    def __init__(self, tree: QTreeWidget):
        super().__init__()

        self._tree = tree
        self._items = None
        self._swatches = {}
        self._setupColourColumn()
        self._applyTheme()
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(lambda _name: self._applyTheme())

        self._fitColumns()
        # R171. The header sorts on click, and a sorted tree re-orders itself
        # on the next rename for the same reason the load-time sort did.
        self._tree.header().setSectionsClickable(False)
        self._tree.header().setSortIndicatorShown(False)
        # R1/R46/R112/R147. Live sorting re-ordered the tree on every keystroke
        # of a rename, because setText() on the name column re-sorts the row
        # immediately. MEASURED renaming the five parts of a split tee from the
        # top row down: `inlet`, `outlet_top` and `outlet_branch` all landed on
        # the SAME surface, because each committed name jumped that row out from
        # under the cursor and the next edit opened on whatever had slid into
        # its place. The tree is sorted once per load instead, so a row stays
        # where the user is looking at it.
        self._tree.setSortingEnabled(False)

        self._connectSignalsSlots()

    def _fitColumns(self):
        """Name takes the slack; Type and Colour are as wide as they read.

        DP-510. Only Name was given a resize mode, so Type kept Qt's default
        100 px and `Boundary (wall)` was cut to `Boundary (w...` at the
        width the column normally has -- the one word on the row that says
        which patch type the solver will write. Type is sized to its longest
        value and its header, Colour to its swatch, and Name stretches over
        what is left and elides (its tooltip carries the full name).
        """
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(int(Column.NAME_COLUMN),
                                    QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(int(Column.TYPE_COLUMN),
                                    QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)

    def _setupColourColumn(self):
        """Give the tree its colour column, whatever the .ui asked for."""
        if self._tree.columnCount() <= int(Column.COLOUR_COLUMN):
            self._tree.setColumnCount(int(Column.COLOUR_COLUMN) + 1)
        header = self._tree.headerItem()
        if header is not None:
            header.setText(int(Column.COLOUR_COLUMN),
                           QCoreApplication.translate('GeometryPage', 'Colour'))
        self._tree.header().setSectionResizeMode(
            int(Column.COLOUR_COLUMN),
            QHeaderView.ResizeMode.ResizeToContents)

    def _applyTheme(self):
        self.volumeIcon = load_themed_icon(VOLUME_ICON_FILE)
        self.surfaceIcon = load_themed_icon(SURFACE_ICON_FILE)
        if self._items:
            for item in self._items.values():
                item.setIcon(Column.NAME_COLUMN,
                             self.volumeIcon if item.isVolume() else self.surfaceIcon)
        # DP-B2. A theme swap re-resolves every other colour on the page; the
        # swatch is a stylesheet on a widget the theme does not know about, so
        # it is asked for again here or it keeps the old theme's palette.
        for gId in list(self._swatches):
            self._repaintSwatch(gId)

    # -- the colour column ------------------------------------------------- #

    def _actorInfo(self, gId):
        """The actor this row stands for, or None when there is no scene."""
        window = getattr(app, 'window', None)
        manager = getattr(window, 'geometryManager', None)
        if manager is None:
            return None
        try:
            return manager.actorInfo(str(gId))
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            return None

    def _mountSwatch(self, gId, item):
        gId = str(gId)
        swatch = ColorSwatch(self._tree)
        swatch.setObjectName(f'geometrySwatch_{gId}')
        swatch.setAccessibleName(
            QCoreApplication.translate('GeometryPage', 'Boundary colour'))
        swatch.setToolTip(QCoreApplication.translate(
            'GeometryPage', 'The colour this surface is drawn in. '
                            'Click to change it.'))
        swatch.clicked.connect(lambda key=gId: self._pickColour(key))
        cell = QWidget(self._tree)
        row = QHBoxLayout(cell)
        row.setContentsMargins(MARGIN_TIGHT, 0, MARGIN_TIGHT, 0)
        row.addWidget(swatch)
        self._tree.setItemWidget(item, int(Column.COLOUR_COLUMN), cell)
        self._swatches[gId] = swatch
        actorInfo = self._actorInfo(gId)
        if actorInfo is not None:
            actorInfo.colorChanged.connect(
                lambda key=gId: self._repaintSwatch(key))
        self._repaintSwatch(gId)

    def _repaintSwatch(self, gId):
        swatch = self._swatches.get(str(gId))
        if swatch is None:
            return
        actorInfo = self._actorInfo(gId)
        try:
            colour = None if actorInfo is None else actorInfo.color()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            colour = None
        apply_color_swatch(swatch, colour)

    def _pickColour(self, gId):
        """Change the colour this surface is drawn in, from its own row."""
        actorInfo = self._actorInfo(gId)
        if actorInfo is None:
            return
        colour = QColorDialog.getColor(
            actorInfo.color(), self._tree,
            QCoreApplication.translate('GeometryPage', 'Boundary colour'))
        if colour is None or not colour.isValid():
            return
        actorInfo.setColor(colour)
        self._repaintSwatch(gId)

    def swatchFor(self, gId):
        """The colour swatch of one row, keyed by geometry id and not by row."""
        return self._swatches.get(str(gId))

    def load(self):
        self._tree.clear()
        self._items = {}
        self._swatches = {}

        # R171. In the order the case made them, not alphabetical. Sorting by
        # name was applied on every rebuild, and a rename rebuilds -- so
        # naming the four parts of a split tee from the top row down moved the
        # row that had just been named and the next double-click opened a
        # different face: `wall` dropped to the bottom, and the second row,
        # which had been the bottom disc, was the top disc by the time it was
        # opened. An id never changes under an edit, so this order does not
        # either; it is also the order the split produced, which is the order
        # the user watched appear.
        geometries = app.facadeClient.checkout().getElements('geometry')
        for gId, geometry in sorted(geometries.items(),
                                    key=lambda item: int(item[0])):
            if gId not in self._items:
                volume = geometry.value('volume')
                if volume and volume not in self._items:
                    self.add(volume, geometries[volume])
                self.add(gId, geometry)


    def add(self, gId, geometry):
        item = GeometryItem(gId, geometry)

        if geometry.value('volume'):
            self._items[geometry.value('volume')].addChild(item)
        else:
            self._tree.addTopLevelItem(item)
            item.setExpanded(True)

        item.setIcon(Column.NAME_COLUMN,
                     self.volumeIcon if geometry.value('gType') == GeometryType.VOLUME.value else self.surfaceIcon)
        self._mountSwatch(gId, item)
        self._tree.scrollToBottom()

        self._items[gId] = item

    def update(self, gId, geometry):
        self._items[gId].setGeometry(geometry)

    def remove(self, gId):
        index = -1
        for i in range(self._tree.topLevelItemCount()):
            if self._tree.topLevelItem(i).gId() == gId:
                index = i
                break

        if index > -1:
            item = self._tree.takeTopLevelItem(index)
            while item.childCount():
                citem = item.takeChild(0)
                del self._items[str(citem.gId())]
                self._swatches.pop(str(citem.gId()), None)
                del citem

            del self._items[str(gId)]
            self._swatches.pop(str(gId), None)
            del item

    def clear(self):
        self._tree.clear()
        self._items = {}
        self._swatches = {}
        
    def selectedIDs(self):
        return [str(item.gId()) for item in self._tree.selectedItems()]

    def selectedItems(self):
        return self._tree.selectedItems()

    def rowIDs(self):
        """The geometry ids the tree is showing, or `None` before it loads.

        DP-454. The tree and the geometry store are filled by different paths
        -- the store by a queued facade commit, the tree by the rebuild that
        runs when that commit's await comes back -- so "what does the case
        hold" and "what is on screen" are two questions, and the whole of
        DP-413/417/437 lived in the gap between them. Anything that drives a
        row has to ask this one, which is why it is public.

        `None` and `[]` are kept apart on purpose: `_items` is `None` until
        the first `load()`, and a tree that has never been built is a
        different fault from one that was built from an empty case.
        """
        return None if self._items is None else list(self._items)

    def setSelectedItems(self, ids):
        self.clearSelection()

        for i in ids:
            if i in self._items:
                self._items[i].setSelected(True)

    def childSurfaces(self, gId):
        item = self._items[gId]
        return {str(item.child(i).type()): item.child(i).geometry() for i in range(item.childCount())}

    def clearSelection(self):
        self._tree.clearSelection()

    def retranslate(self):
        for item in self._items.values():
            item.retranslate()

    def _connectSignalsSlots(self):
        self._tree.itemSelectionChanged.connect(self._correctSelection)

    def _correctSelection(self):
        if len(self._tree.selectedItems()) > 1:
            for item in self._tree.selectedItems():
                if item.isVolume():
                    item.setSelected(False)

        self.selectedItemsChanged.emit()
