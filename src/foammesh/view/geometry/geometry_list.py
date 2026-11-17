#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import IntEnum, auto

from PySide6.QtWidgets import (QColorDialog, QHBoxLayout, QHeaderView, QLabel,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QWidget)
from PySide6.QtCore import Qt, Signal, QObject, QCoreApplication, QSize

from foammesh.app import app
from foammesh.core.geometry.patches.ops import (
    BOUNDARY_CATEGORIES, boundary_category_for_name)
from foammesh.db.configurations_schema import CFDType, GeometryType
from foammesh.view.theming.metrics import MARGIN_TIGHT
from foammesh.view.theming.icons import apply_tinted_icon
from foammesh.view.theming.status_colors import apply_color_swatch
from widgets.themed_icon import load_themed_icon
from widgets import async_dialog as dialogs

VOLUME_ICON_FILE = ':/graphicsIcons/volume.svg'
#: DP-6090. The show/hide glyphs of the viewport's Parts list, so a body's
#: row says "drawn / not drawn" the way every other part row does.
SHOWN_ICON_FILE = ':/graphicsIcons/bulb-on.svg'
HIDDEN_ICON_FILE = ':/graphicsIcons/bulb-off.svg'
EYE_ICON = QSize(14, 14)
EYE_STROKE_PX = 1.25
SURFACE_ICON_FILE = ':/graphicsIcons/face.svg'

def cfdTypeToText(cfdType):
    return {
        # Plan 40 UXB1 (#30). A row with no CFD role has no type to show;
        # the word "None" read as a setting that had been turned off.
        CFDType.NONE.value: '',
        CFDType.CELL_ZONE.value: QCoreApplication.translate('GeometryPage', 'CellZone'),
        CFDType.BOUNDARY.value: QCoreApplication.translate('GeometryPage', 'Boundary'),
        CFDType.INTERFACE.value: QCoreApplication.translate('GeometryPage', 'Interface'),
    }.get(cfdType)


#: Plan 49 SR2. The type column's word for each stored surface role.
ROLE_COLUMN_TEXT = {
    'externalBoundary': 'Boundary',
    'internalFace': 'Internal face',
    'regionInterfaceConformal': 'Interface',
    'regionInterfaceNonConformal': 'Interface (non-conformal)',
    'blockingBaffle': 'Baffle',
}


def _surfaceRole(geometry):
    if geometry.value('gType') != GeometryType.SURFACE.value:
        return None
    try:
        role = geometry.value('surfaceRole')
    except (LookupError, TypeError):
        return None
    role = getattr(role, 'value', role)
    return str(role) if role else None


#: DP-6497. Set while `GeometryList.load` builds every row from one read of
#: the case, so a row does not copy the whole case again to learn it.
_loadingDefaultCategory = None


def defaultBoundaryCategory(db=None):
    """The category a patch adopts when its name announces none.

    Read from the project rather than assumed so the list agrees with what the
    publication step will actually write. Any failure to read it falls back to
    the schema default, which is ``wall``.
    """
    if db is None and _loadingDefaultCategory is not None:
        return _loadingDefaultCategory
    try:
        value = (db if db is not None else app.facadeClient.checkout()).getValue(
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

    Plan 49 SR2 (DP-5814). A surface with a stored role is shown by that
    role and, for an external boundary, its stored purpose -- not by the
    legacy type and not by a purpose read again from the name, which a
    rename would change while the stored purpose stays.
    """
    role = _surfaceRole(geometry)
    if role:
        text = QCoreApplication.translate('GeometryPage', ROLE_COLUMN_TEXT.get(role, role))
        category = geometry.value('boundaryCategory')
        if role == 'externalBoundary' and category:
            return f'{text} ({str(getattr(category, "value", category))})'
        return text

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


class FarfieldItem(QTreeWidgetItem):
    """The farfield, as one row of the geometry tree.

    Plan 37 (Geometry > Farfield...). Not a geometry element: the farfield is
    the one ``gmsh/farfield`` record both engines read, and this row is how
    the page says it is on and what shape it is. It carries no geometry id,
    so nothing that acts on geometry rows -- the boundary operations, the
    selection service, Remove of a surface -- ever receives it; the page's
    own menu edits it or switches it off.
    """

    def __init__(self, spec):
        super().__init__()
        self._spec = spec
        self.setSpec(spec)

    def gId(self):
        return ''

    def geometry(self):
        return None

    def spec(self):
        return self._spec

    def isVolume(self):
        return False

    def isSurface(self):
        return False

    def setSpec(self, spec):
        from foammesh.core.mesh import farfield_spec

        self._spec = spec
        name = QCoreApplication.translate('GeometryPage', 'Farfield ({0})').format(
            QCoreApplication.translate('GeometryPage', spec.shape))
        kind = QCoreApplication.translate('GeometryPage', 'Outer boundary')
        self.setText(Column.NAME_COLUMN, name)
        self.setText(Column.TYPE_COLUMN, kind)
        tip = QCoreApplication.translate(
            'GeometryPage', '{0}: a {1} around the geometry, with the bodies '
            'cut out of it. Double-click to edit it.').format(
                farfield_spec.ROLE_TITLE, spec.shape)
        self.setToolTip(Column.NAME_COLUMN, tip)
        self.setToolTip(Column.TYPE_COLUMN, tip)

    def retranslate(self):
        self.setSpec(self._spec)


def isFarfield(item) -> bool:
    return isinstance(item, FarfieldItem)


def readFarfield(db):
    """The case's farfield spec when it is on, else ``None``.

    Read-only and forgiving: a double of the database without the farfield
    record (older unit fakes) has no farfield rather than an error.
    """
    from foammesh.core.mesh import farfield_spec

    try:
        spec = farfield_spec.read(db)
    except Exception:                                        # noqa: BLE001
        return None
    return spec if spec.enabled else None


class GeometryList(QObject):
    selectedItemsChanged = Signal()

    def __init__(self, tree: QTreeWidget):
        super().__init__()

        self._tree = tree
        self._items = None
        self._swatches = {}
        self._eyes = {}
        #: DP-6497. Each body's surface ids, read from the case once and
        #: kept until the rows change; ``None`` means read it again.
        self._bodies = None
        #: DP-6506. Rows made during a load and not yet in the tree.
        self._pending = None
        self._watchedControl = None
        self._farfield = None
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
        for gId in list(self._eyes):
            self._paintEye(gId)

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
        cell = QWidget(self._tree)
        row = QHBoxLayout(cell)
        row.setContentsMargins(MARGIN_TIGHT, 0, MARGIN_TIGHT, 0)
        self._tree.setItemWidget(item, int(Column.COLOUR_COLUMN), cell)
        if item.isVolume():
            # DP-6099. A body is drawn as its surfaces (the scene has no
            # actor for a volume), so a swatch on its row did nothing; the
            # row carries its show / hide button only.
            row.addWidget(self._mountEye(gId, cell, item))
            row.addStretch(1)
            return
        swatch = ColorSwatch(self._tree)
        swatch.setObjectName(f'geometrySwatch_{gId}')
        swatch.setAccessibleName(
            QCoreApplication.translate('GeometryPage', 'Boundary colour'))
        swatch.setToolTip(QCoreApplication.translate(
            'GeometryPage', 'The colour this surface is drawn in. '
                            'Click to change it.'))
        swatch.clicked.connect(lambda key=gId: self._pickColour(key))
        row.addWidget(swatch)
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
        def chosen(colour):
            if colour is None or not colour.isValid():
                return
            actorInfo.setColor(colour)
            self._repaintSwatch(gId)

        # Plan 50 IP19: no nested event loop.
        dialogs.getColor(
            actorInfo.color(), self._tree,
            QCoreApplication.translate('GeometryPage', 'Boundary colour'),
            then=chosen)

    # -- a body's show / hide (DP-6090) ------------------------------------ #

    def _displayControl(self):
        window = getattr(app, 'window', None)
        try:
            return getattr(window, 'displayControl', None)
        except (AttributeError, RuntimeError):
            return None

    def _mountEye(self, gId, parent, item=None):
        """Hide or show every surface of one body with one click.

        DP-6090 (user, 2026-10-31, the CHT tubes). Three coaxial bodies hide
        each other; the only way to look inside was a right-click Hide on
        each face in the viewport. The button sits on the body's row and
        goes through Display control, so the Parts list, the region picker
        and Show all agree with it.
        """
        displayToggle = QToolButton(parent)
        displayToggle.setObjectName(f'geometryEye_{gId}')
        displayToggle.setCheckable(True)
        displayToggle.setAutoRaise(True)
        displayToggle.setIconSize(EYE_ICON)
        name = self._items_name(gId, item)
        displayToggle.setToolTip(QCoreApplication.translate(
            'GeometryPage', 'Show or hide {0}').format(name))
        displayToggle.setAccessibleName(displayToggle.toolTip())
        self._eyes[gId] = displayToggle
        displayToggle.setChecked(self._bodyShown(gId))
        displayToggle.toggled.connect(
            lambda checked, key=gId: self._showBody(key, checked))
        self._paintEye(gId)
        self._watchVisibility()
        return displayToggle

    def _items_name(self, gId, item=None):
        # DP-6497. The row's own record first: a checkout copies the whole
        # case, and one per row made a 500-body import hold the GUI thread
        # for minutes.
        if item is None:
            item = (self._items or {}).get(str(gId))
        geometry = getattr(item, '_geometry', None) if item is not None else None
        try:
            if geometry is not None and geometry.value('name'):
                return geometry.value('name')
            return app.facadeClient.checkout().getElement(
                'geometry', gId).value('name')
        except (AttributeError, KeyError, RuntimeError, TypeError):
            return str(gId)

    def _bodyIndex(self, rows=None):
        """Every body's surface ids, from the case (the tree may be
        half-built while it loads), read once until the rows change."""
        if rows is None and self._bodies is not None:
            return self._bodies
        if rows is None:
            try:
                rows = app.facadeClient.checkout().getElements('geometry')
            except (AttributeError, RuntimeError):
                return {}
        bodies = {}
        for key, row in rows.items():
            volume = str(row.value('volume') or '')
            if volume:
                bodies.setdefault(volume, []).append(str(key))
        self._bodies = bodies
        return bodies

    def _bodySurfaces(self, gId):
        """The surface ids of one body."""
        return list(self._bodyIndex().get(str(gId), ()))

    def _bodyShown(self, gId):
        """Shown while any of its surfaces is drawn."""
        shown = None
        for key in self._bodySurfaces(gId):
            actorInfo = self._actorInfo(key)
            if actorInfo is None:
                continue
            try:
                if actorInfo.isVisible():
                    return True
            except (AttributeError, RuntimeError):
                continue
            shown = False
        return shown is None

    def _showBody(self, gId, visible):
        control = self._displayControl()
        if control is not None:
            control.setVisibilities(
                {key: visible for key in self._bodySurfaces(gId)})
        self._paintEye(gId)

    def _paintEye(self, gId):
        eye = self._eyes.get(str(gId))
        if eye is None:
            return
        try:
            apply_tinted_icon(eye, SHOWN_ICON_FILE if eye.isChecked()
                              else HIDDEN_ICON_FILE, EYE_ICON, EYE_STROKE_PX)
        except RuntimeError:
            self._eyes.pop(str(gId), None)

    def _watchVisibility(self):
        """Follow a Hide, Isolate or Show all made anywhere else."""
        control = self._displayControl()
        if control is None or control is self._watchedControl:
            return
        self._watchedControl = control
        control.visibilityChanged.connect(lambda *_args: self.syncEyes())

    def syncEyes(self):
        for gId, eye in list(self._eyes.items()):
            try:
                eye.blockSignals(True)
                eye.setChecked(self._bodyShown(gId))
                eye.blockSignals(False)
            except RuntimeError:
                self._eyes.pop(gId, None)
                continue
            self._paintEye(gId)

    def eyeFor(self, gId):
        """The show / hide button of one body's row, or None."""
        return self._eyes.get(str(gId))

    def swatchFor(self, gId):
        """The colour swatch of one row, keyed by geometry id and not by row."""
        return self._swatches.get(str(gId))

    def load(self):
        self._tree.clear()
        self._items = {}
        self._swatches = {}
        self._eyes = {}

        # R171. In the order the case made them, not alphabetical. Sorting by
        # name was applied on every rebuild, and a rename rebuilds -- so
        # naming the four parts of a split tee from the top row down moved the
        # row that had just been named and the next double-click opened a
        # different face: `wall` dropped to the bottom, and the second row,
        # which had been the bottom disc, was the top disc by the time it was
        # opened. An id never changes under an edit, so this order does not
        # either; it is also the order the split produced, which is the order
        # the user watched appear.
        self._farfield = None
        db = app.facadeClient.checkout()
        geometries = db.getElements('geometry')
        self._bodyIndex(geometries)
        global _loadingDefaultCategory
        _loadingDefaultCategory = defaultBoundaryCategory(db)
        # DP-6504. One scroll and one repaint for the whole load: each row's
        # own scrollToBottom laid the whole tree out again, and a 500-body
        # import held the GUI thread 34.4 s (watchdog, `surfaces500`).
        # DP-6506. The rows are made first and put in the tree in one
        # insert; each addTopLevelItem re-placed every row widget already in
        # the tree and ran the tree's row slots, 80 s for a 500-body import
        # (watchdog, `surfaces500`, rerun on 645499f2).
        added = False
        self._bulk = True
        self._pending = []
        self._tree.setUpdatesEnabled(False)
        try:
            for gId, geometry in sorted(geometries.items(),
                                        key=lambda item: int(item[0])):
                if gId not in self._items:
                    volume = geometry.value('volume')
                    if volume and volume not in self._items:
                        self.add(volume, geometries[volume])
                    self.add(gId, geometry)
                    added = True
            pending, self._pending = self._pending, None
            tops = [item for item, _gId in pending if item.parent() is None]
            if tops:
                self._tree.addTopLevelItems(tops)
            for item, gId in pending:
                if item.parent() is None:
                    item.setExpanded(True)
                self._mountSwatch(gId, item)
        finally:
            _loadingDefaultCategory = None
            self._bulk = False
            self._pending = None
            self._tree.setUpdatesEnabled(True)
        if added:
            self._tree.scrollToBottom()
        self._showFarfield(readFarfield(db))

    # -- the farfield row -------------------------------------------------- #

    def _showFarfield(self, spec):
        """One Farfield row, first in the tree, while the farfield is on."""
        if spec is None:
            if self._farfield is not None:
                index = self._tree.indexOfTopLevelItem(self._farfield)
                if index > -1:
                    self._tree.takeTopLevelItem(index)
                self._farfield = None
            return
        if self._farfield is None:
            self._farfield = FarfieldItem(spec)
            self._farfield.setIcon(Column.NAME_COLUMN, self.volumeIcon)
            self._tree.insertTopLevelItem(0, self._farfield)
        else:
            self._farfield.setSpec(spec)

    def refreshFarfield(self):
        """Re-read the farfield record (after the dialog or Remove wrote it)."""
        self._showFarfield(readFarfield(app.facadeClient.checkout()))

    def farfieldItem(self):
        return self._farfield

    def farfieldSelected(self) -> bool:
        return self._farfield is not None and self._farfield.isSelected()

    def add(self, gId, geometry):
        item = GeometryItem(gId, geometry)
        volume = str(geometry.value('volume') or '')
        if volume and self._bodies is not None:
            members = self._bodies.setdefault(volume, [])
            if str(gId) not in members:
                members.append(str(gId))

        pending = getattr(self, '_pending', None)
        if geometry.value('volume'):
            self._items[geometry.value('volume')].addChild(item)
        elif pending is None:
            self._tree.addTopLevelItem(item)
            item.setExpanded(True)

        item.setIcon(Column.NAME_COLUMN,
                     self.volumeIcon if geometry.value('gType') == GeometryType.VOLUME.value else self.surfaceIcon)
        if pending is not None:
            # DP-6506. A load puts its rows in the tree at once, then mounts
            # their swatches (a row widget needs its row in the tree).
            pending.append((item, gId))
        else:
            self._mountSwatch(gId, item)
        if not getattr(self, '_bulk', False):
            self._tree.scrollToBottom()

        self._items[gId] = item

    def update(self, gId, geometry):
        self._bodies = None
        self._items[gId].setGeometry(geometry)

    def remove(self, gId):
        index = -1
        for i in range(self._tree.topLevelItemCount()):
            if self._tree.topLevelItem(i).gId() == gId:
                index = i
                break

        self._bodies = None
        if index > -1:
            item = self._tree.takeTopLevelItem(index)
            while item.childCount():
                citem = item.takeChild(0)
                del self._items[str(citem.gId())]
                self._swatches.pop(str(citem.gId()), None)
                del citem

            del self._items[str(gId)]
            self._swatches.pop(str(gId), None)
            self._eyes.pop(str(gId), None)
            del item

    def clear(self):
        self._tree.clear()
        self._bodies = None
        self._items = {}
        self._swatches = {}
        self._eyes = {}
        self._farfield = None

    def selectedIDs(self):
        return [str(item.gId()) for item in self.selectedItems()]

    def selectedItems(self):
        """The selected geometry rows; the farfield row is not one."""
        return [item for item in self._tree.selectedItems()
                if not isFarfield(item)]

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
        if self._farfield is not None:
            self._farfield.retranslate()

    def _connectSignalsSlots(self):
        self._tree.itemSelectionChanged.connect(self._correctSelection)

    def _correctSelection(self):
        if len(self._tree.selectedItems()) > 1:
            for item in self._tree.selectedItems():
                # The farfield is edited on its own: selected with geometry
                # rows it would be half of an operation it takes no part in.
                if item.isVolume() or isFarfield(item):
                    item.setSelected(False)

        self.selectedItemsChanged.emit()
