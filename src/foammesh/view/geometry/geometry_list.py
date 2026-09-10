#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import IntEnum, auto

from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QHeaderView
from PySide6.QtCore import Signal, QObject, QCoreApplication

from foammesh.app import app
from foammesh.core.geometry.patches.ops import (
    BOUNDARY_CATEGORIES, boundary_category_for_name)
from foammesh.db.configurations_schema import CFDType, GeometryType
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
        self.setText(Column.NAME_COLUMN, geometry.value('name'))
        self.setText(Column.TYPE_COLUMN, typeColumnText(geometry))

        self._geometry = geometry

    def retranslate(self):
        self.setGeometry(self._geometry)


class GeometryList(QObject):
    selectedItemsChanged = Signal()

    def __init__(self, tree: QTreeWidget):
        super().__init__()

        self._tree = tree
        self._items = None
        self._applyTheme()
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(lambda _name: self._applyTheme())

        self._tree.header().setSectionResizeMode(Column.NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
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

    def _applyTheme(self):
        self.volumeIcon = load_themed_icon(VOLUME_ICON_FILE)
        self.surfaceIcon = load_themed_icon(SURFACE_ICON_FILE)
        if self._items:
            for item in self._items.values():
                item.setIcon(Column.NAME_COLUMN,
                             self.volumeIcon if item.isVolume() else self.surfaceIcon)

    def load(self):
        self._tree.clear()
        self._items = {}

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
                del citem

            del self._items[str(gId)]
            del item

    def clear(self):
        self._tree.clear()
        self._items = {}
        
    def selectedIDs(self):
        return [str(item.gId()) for item in self._tree.selectedItems()]

    def selectedItems(self):
        return self._tree.selectedItems()

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
