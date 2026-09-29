#!/usr/bin/env python
# -*- coding: utf-8 -*-

from typing import Optional

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QHeaderView, QMenu, QTreeWidgetItem, QVBoxLayout, QWidget)

from foammesh.app import app
from foammesh.rendering.actor_info import (
    DisplayMode, Properties, RegionMarkerActor)
from widgets.rendering.rendering_widget import RenderingWidget
from foammesh.view.main_window.mesh_lines_control import MeshLinesPanel
from foammesh.view.widgets.folder_header import FolderHeader
from .mesh_quality_info import MeshQualityInfo

from .opacity_dialog import OpacityDialog
from foammesh.view.theming.metrics import GAP
from .display_item import DisplayItem, Column
from .cut_tool import CutTool, sceneBounds
from .section_panel import CutType


def countedPartIds(items) -> list:
    """The row ids the parts chip counts: the parts of the model on screen.

    DP-679. A snappy case with one imported STL read "2 of 2 parts shown"
    once a region seed existed, because the seed's marker is a row too. A
    seed is a landmark the user placed, not a part of what they imported, and
    a row ``DisplayControl.hide`` took out of the scene is not on screen at
    all; neither is counted. Both keep their rows.
    """
    return [key for key, item in items.items()
            if not item.isHidden()
            and not isinstance(item.actorInfo(), RegionMarkerActor)]


class ContextMenu(QMenu):
    showActionTriggered = Signal()
    hideActionTriggered = Signal()
    opacitySelected = Signal(float)
    colorPicked = Signal(QColor)
    noCutActionTriggered = Signal(bool)
    isolateActionTriggered = Signal()
    showAllActionTriggered = Signal()

    wireframeDisplayModeSelected = Signal()
    surfaceDisplayModeSelected = Signal()
    surfaceEdgeDisplayModeSelected = Signal()

    def __init__(self, parent):
        super().__init__(parent)

        self._opacityDialog = OpacityDialog(app.window)
        self._colorDialog = QColorDialog(app.window)
        self._colorDialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._properties = None

        self._showAction = self.addAction(self.tr('Show'), lambda: self.showActionTriggered.emit())
        self._hideAction = self.addAction(self.tr('Hide'), lambda: self.hideActionTriggered.emit())
        self._opacityAction = self.addAction(self.tr('Opacity'), self._openOpacityDialog)
        self._colorAction = self.addAction(self.tr('Colour'), self._openColorDialog)

        displayMenu = self.addMenu(self.tr('Display mode'))
        self._wireFrameDisplayAction = displayMenu.addAction(
            self.tr('Wireframe'), lambda: self.wireframeDisplayModeSelected.emit())
        self._surfaceDisplayAction = displayMenu.addAction(
            self.tr('Surface'), lambda: self.surfaceDisplayModeSelected.emit())
        self._surfaceEdgeDisplayAction = displayMenu.addAction(
            self.tr('Surface with edges'), lambda: self.surfaceEdgeDisplayModeSelected.emit())

        self._noCutAction = self.addAction(self.tr('No cut'), self._noCutActionTriggered)

        self.addSeparator()
        # Right-clicking the thing itself is the natural verb. This menu has
        # always existed and was offered only on the tree, where you have to
        # know a part's name before you can act on it.
        self._isolateAction = self.addAction(
            self.tr('Isolate'), lambda: self.isolateActionTriggered.emit())
        self._showAllAction = self.addAction(
            self.tr('Show all'), lambda: self.showAllActionTriggered.emit())

        self._wireFrameDisplayAction.setCheckable(True)
        self._surfaceDisplayAction.setCheckable(True)
        self._surfaceEdgeDisplayAction.setCheckable(True)
        self._noCutAction.setCheckable(True)

        self._connectSignalsSlots()

    def execute(self, pos, properties: Properties):
        self._properties = properties

        self._showAction.setVisible(not properties.visibility)
        self._hideAction.setVisible(properties.visibility is None or properties.visibility)
        self._wireFrameDisplayAction.setChecked(properties.displayMode == DisplayMode.WIREFRAME)
        self._surfaceDisplayAction.setChecked(properties.displayMode == DisplayMode.SURFACE)
        self._surfaceEdgeDisplayAction.setChecked(properties.displayMode == DisplayMode.SURFACE_EDGE)
        self._noCutAction.setChecked(properties.cutEnabled is False)

        self.exec(pos)

    def _connectSignalsSlots(self):
        self._opacityDialog.accepted.connect(lambda: self.opacitySelected.emit(self._opacityDialog.opacity()))
        self._colorDialog.accepted.connect(lambda: self.colorPicked.emit(self._colorDialog.selectedColor()))

    def _openOpacityDialog(self):
        self._opacityDialog.setOpacity(self._properties.opacity)
        self._opacityDialog.show()

    def _openColorDialog(self):
        default = (
            QColor(app.themeManager.tokens.value('foreground.primary'))
            if app.themeManager is not None and app.themeManager.tokens is not None
            else self.parentWidget().palette().color(
                self.parentWidget().foregroundRole())
            if self.parentWidget() is not None else QColor()
        )
        self._colorDialog.setCurrentColor(default if self._properties.color is None else self._properties.color)
        self._colorDialog.show()

    def _noCutActionTriggered(self):
        self.noCutActionTriggered.emit(not self._properties.cutEnabled)


class DisplayControl(QObject):
    selectedActorsChanged = Signal(list)
    #: (shown, total) for the parts chip, so isolation is never invisible.
    visibilityChanged = Signal(int, int)
    #: The set of parts changed. The viewport overlay used to be filled only
    #: from the mesh-load path, so a geometry import produced an empty parts
    #: list and closing a case left the previous one on screen. Anything that
    #: adds, drops or replaces an actor says so here and the overlay rebuilds.
    partsChanged = Signal()

    def __init__(self, ui):
        super().__init__()

        self._ui = ui
        self._list = ui.actors
        self._view: RenderingWidget = ui.renderingView

        self._cutTool = CutTool(ui)
        self._meshQualityInfo = MeshQualityInfo(ui)
        self._meshLines = self._addMeshLinesFold(ui)
        self._menu = ContextMenu(self._list)

        self._items: dict[str, DisplayItem] = {}
        self._groups: dict[tuple[str, ...], QTreeWidgetItem] = {}
        self._selectedItems: list[DisplayItem] = []
        self._workingStep = 0
        self._displayStep = 0
        #: Isolation has to be visibly temporary. A user who forgets they
        #: isolated something reads a partial mesh as a broken one.
        self._isolated = False

        self._list.setColumnWidth(Column.COLOR_COLUMN, 20)

        self._list.header().setSectionResizeMode(Column.NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        self._list.header().setSectionResizeMode(Column.TYPE_COLUMN, QHeaderView.ResizeMode.ResizeToContents)

        self._connectSignalsSlots()

    def _addMeshLinesFold(self, ui) -> Optional[MeshLinesPanel]:
        """DP-738. The mesh-line control, in the panel with the other folds.

        DP-710 put it only behind a toolbar icon. It is the same control on
        the same global style, so the two cannot disagree; it sits just above
        the parts list, after Cut and Mesh Quality.
        """
        layout = ui.displayControl.layout()
        if layout is None or layout.indexOf(ui.actors) < 0:
            return None
        fold = QWidget(ui.displayControl)
        fold.setObjectName('meshLinesTool')
        foldLayout = QVBoxLayout(fold)
        foldLayout.setContentsMargins(0, 0, 0, 0)
        foldLayout.setSpacing(GAP)
        header = FolderHeader(fold)
        header.setObjectName('meshLinesHeader')
        header.setText(self.tr('Mesh lines'))
        panel = MeshLinesPanel(fold)
        panel.setToolTip(self.tr(
            'Opacity, colour and width of the grid drawn on every part; '
            'the same setting as the Mesh lines button on the viewport '
            'toolbar'))
        foldLayout.addWidget(header)
        foldLayout.addWidget(panel)
        header.setContents(panel)
        layout.insertWidget(layout.indexOf(ui.actors), fold)
        panel.styleChanged.connect(self._view.refresh)
        return panel

    def setEnabled(self, enabled):
        self._ui.displayControl.setEnabled(enabled)

    def isEnabled(self):
        return self._ui.displayControl.isEnabled()

    def add(self, actorInfo):
        if actorInfo.id() in self._items:
            item = self._items[actorInfo.id()]
            # Plan 35 CR3 step 10: the new actor's precomputed surface and
            # outline come across with its data.
            item.actorInfo().setDataSet(actorInfo.dataSet(), like=actorInfo)
            item.setHidden(False)

            actorInfo = item.actorInfo()
        else:
            actorInfo.sourceChanged.connect(self._actorSourceUpdated)
            silhouetteChanged = getattr(actorInfo, 'silhouetteChanged', None)
            if silhouetteChanged is not None:
                silhouetteChanged.connect(self.refreshView)

            item = DisplayItem(actorInfo)
            self._items[actorInfo.id()] = item
            parent = self._groupItem(actorInfo)
            if parent is None:
                self._list.addTopLevelItem(item)
            else:
                parent.addChild(item)
            item.setupColorWidget(self._list)

        for prop in actorInfo.renderProps():
            self._view.addActor(prop)

        self.partsChanged.emit()
        return actorInfo

    #: Zones used to sit in the same flat list as the boundary patches, so a
    #: two-zone case meant reading every row to find the two that mattered.
    #: The grouping is already in the actor ids -- ``region:category:name``.
    #: DP-711. `regions` holds the named pieces of a mesh whose regions carry
    #: no cell zones -- the fluid and solid of a snappy multi-region mesh.
    ZONE_GROUPS = {'cellZones': 'Cell Zones', 'faceZones': 'Face Zones',
                   'regions': 'Regions'}

    def _groupPath(self, actorInfo) -> tuple[str, ...]:
        # A region seed marker is a landmark in the scene, not a patch of a
        # mesh region. Its id is `region:<id>`, which the rule below read as
        # region `region`, category `boundary`, so every seed in the case was
        # filed under an invented `region` heading with `Boundaries` under it.
        if isinstance(actorInfo, RegionMarkerActor):
            return ()
        parts = str(actorInfo.id()).split(':')
        if len(parts) == 3:
            region, category, _name = parts
        elif len(parts) == 2 and parts[0] in self.ZONE_GROUPS:
            region, category = '', parts[0]
        elif len(parts) == 2:
            region, category = parts[0], (
                'internalMesh' if parts[1] == 'internalMesh' else 'boundary')
        else:
            # A single-region patch, or an imported geometry actor. Nesting
            # those under an invented heading would be noise, not structure.
            return ()

        path = (region,) if region else ()
        if category in self.ZONE_GROUPS:
            return path + (self.ZONE_GROUPS[category],)
        if category == 'boundary':
            return path + (self.tr('Boundaries'),)
        return path

    def _groupItem(self, actorInfo):
        path = self._groupPath(actorInfo)
        if not path:
            return None

        parent = None
        for depth in range(len(path)):
            key = path[:depth + 1]
            node = self._groups.get(key)
            if node is None:
                node = QTreeWidgetItem([path[depth]])
                # R26. This used to clear `ItemIsSelectable`, so the heading
                # rows -- the region, `Boundaries`, `Cell Zones` -- could not
                # be selected and therefore could not be acted on. MEASURED
                # after the base grid: the opaque block hid the STL entirely,
                # switching all six of its faces (xMin..zMax) off changed the
                # chip to "7 of 13 parts shown" and not one pixel of the
                # render, because `internalMesh` -- the block's own volume --
                # is a separate part sitting behind them; and hiding the
                # parent `fluid` row, the one row that covers both, did
                # nothing at all because it was not selectable. A heading
                # stands for everything under it, so it is selected and hidden
                # like anything else.
                if parent is None:
                    self._list.addTopLevelItem(node)
                else:
                    parent.addChild(node)
                node.setExpanded(True)
                self._groups[key] = node
            parent = node
        return parent

    def remove(self, actorInfo):
        item = self._items.pop(str(actorInfo.id()), None)
        if item is None:
            return

        parent = item.parent()
        if parent is None:
            index = self._list.indexOfTopLevelItem(item)
            if index > -1:
                self._list.takeTopLevelItem(index)
        else:
            parent.removeChild(item)

        for prop in item.actorInfo().renderProps():
            self._view.removeActor(prop)
        del item
        self.partsChanged.emit()

    def hide(self, actorInfo):
        item = self._items[actorInfo.id()]
        for prop in item.actorInfo().renderProps():
            self._view.removeActor(prop)
        item.setHidden(True)

    def isShown(self, actorInfo) -> bool:
        """Whether ``actorInfo`` is in the scene: added and not `hide`-den."""
        item = self._items.get(actorInfo.id())
        if item is None:
            item = self._items.get(str(actorInfo.id()))
        return item is not None and not item.isHidden()

    def refreshView(self):
        self._view.refresh()

    def view(self) -> RenderingWidget:
        """The viewport, for a handle that takes its own mouse events."""
        return self._view

    def addOverlay(self, actor):
        """Add a transient viewport actor without exposing it in Display Control."""
        self._view.addActor(actor)
        self._view.refresh()

    def removeOverlay(self, actor):
        """Remove a transient viewport actor previously added with addOverlay."""
        self._view.removeActor(actor)
        self._view.refresh()

    def fitView(self):
        # DP-736. A load's fit: the view decides whether the model is the one
        # its Back/Forward history was recorded on (record the refit) or not
        # (clear the history). A bare fitCamera recorded nothing.
        frameScene = getattr(self._view, 'frameScene', None)
        if frameScene is None:
            self._view.fitCamera()
        else:
            frameScene()

    def orientIsometric(self):
        """Frame the scene from the isometric preset (G4).

        The preset fits after it turns, so this both aims and frames. It goes
        through the scene fit (DP-736): the preset alone pushed the camera it
        replaced, which after a load of another model aimed at the old one.
        """
        frameScene = getattr(self._view, 'frameScene', None)
        if frameScene is None:
            self._view.setViewPreset('isometric')
        else:
            frameScene('isometric')

    def openedStepChanged(self, step):
        self._workingStep = int(step)
        self.sceneChanged()

    def currentStepChanged(self, step):
        self._displayStep = int(step)
        self.sceneChanged()

    def sceneChanged(self):
        """Re-decide which inspection tools the scene on screen can support.

        Both tools used to be gated on workflow step alone -- the section plane
        at ``BASE_GRID``, the quality controls at ``CASTELLATION``. An *opened*
        mesh reaches neither step, so a user who loaded a polyMesh got no
        section plane and no quality controls at all, on precisely the meshes
        people open in order to inspect them.

        Content is the honest gate: a section needs something with bounds, a
        cell-quality threshold needs a volume mesh. Both are facts about the
        scene rather than about how the mesh got there.
        """
        hasScene = sceneBounds() is not None
        hasMesh = self._sceneHasMesh()

        if hasScene:
            if self._cutTool.isVisible():
                self._cutTool.updateBounds()
            else:
                self._cutTool.show()
        elif self._cutTool.isVisible():
            self._cutTool.hide()

        if hasMesh:
            if not self._meshQualityInfo.isVisible():
                self._meshQualityInfo.show()
        elif self._meshQualityInfo.isVisible():
            self._meshQualityInfo.hide()

        # DP-136. Peeling was re-decided only when somebody moved the opacity
        # slider, so a scene that arrived already carrying a translucent part
        # -- which is now how an enclosure arrives -- was drawn in whatever
        # order its props happened to sit in. A content change is exactly when
        # the question needs asking again.
        self._updateTransparency()

    def _sceneHasMesh(self):
        manager = getattr(app.window, 'meshManager', None)
        if manager is None:
            return False
        isEmpty = getattr(manager, 'isEmpty', None)
        return not isEmpty() if isEmpty is not None else False

    def clear(self):
        self._ui.rendering.setChecked(True)
        self._cutTool.hide()
        self._meshQualityInfo.hide()
        self._list.clear()
        self._view.clear()
        self._items = {}
        self._groups = {}
        self._selectedItems = []
        self._isolated = False
        self.partsChanged.emit()

    def applyTheme(self, tokens):
        self._meshQualityInfo.applyTheme(tokens)
        self._cutTool.applyTheme(tokens)
        for item in self._items.values():
            item.actorInfo().applyTheme(tokens)
        self._view.refresh()

    def cutTool(self):
        return self._cutTool

    def meshQualityInfo(self):
        return self._meshQualityInfo

    def setSelectedActors(self, ids: list[str]):
        """Show the selection service's answer, for the parts it answers for.

        This used to clear the whole row list first, so a part the service
        holds no entity for - a mesh boundary - lost its selection the moment
        a part it does hold joined it, and the control and the service then
        disagreed about what was selected. A row outside what the service
        governs is nobody's business but the user's.
        """
        wanted = {str(value) for value in ids}
        governed = app.selectionService.governed_actor_ids()
        for key, item in self._items.items():
            if key in wanted:
                item.setSelected(True)
            elif key in governed:
                item.setSelected(False)
        # A heading has no actor, so it is not in the loop above; leaving it
        # lit while every part under it has just been deselected is the same
        # control keeping its own idea of the selection that this package
        # exists to end. Headings over parts the service does not govern are
        # left alone, like the parts themselves.
        for node in self._groups.values():
            if not node.isSelected():
                continue
            parts = {item.actorInfo().id()
                     for item in self._displayItems([node])}
            if parts and parts <= governed and not parts & wanted:
                node.setSelected(False)

    def selectedItemsChanged(self):
        """Announce every part the selection covers, headings expanded.

        A heading row - `Boundaries`, or a region - is not a part and has no
        actor of its own, so walking the parts answered a heading selection
        with an empty list: measured live as "selecting a row in the
        Boundaries group of the display control gives no viewport highlight".
        A heading stands for what is under it here, exactly as it does for
        Show, Hide and Isolate (R26).
        """
        selected = {item.actorInfo().id()
                    for item in self._displayItems(self._list.selectedItems())}
        ids = []
        for key, item in self._items.items():
            item.actorInfo().setHighlighted(key in selected)
            if key in selected:
                ids.append(key)

        self.selectedActorsChanged.emit(ids)
        self._view.refresh()

    def _connectSignalsSlots(self):
        self._ui.rendering.toggled.connect(app.renderingToggled)
        self._list.customContextMenuRequested.connect(self._showContextMenu)
        self._list.itemSelectionChanged.connect(self.selectedItemsChanged)
        self._view.customContextMenuRequested.connect(self._showContextMenuOnRenderingView)
        self._view.actorPicked.connect(self._actorPicked)
        self._menu.showActionTriggered.connect(self._showActors)
        self._menu.hideActionTriggered.connect(self._hideActors)
        self._menu.opacitySelected.connect(self._applyOpacity)
        self._menu.colorPicked.connect(self._applyColor)
        self._menu.wireframeDisplayModeSelected.connect(self._displayWireframe)
        self._menu.surfaceDisplayModeSelected.connect(self._displaySurface)
        self._menu.surfaceEdgeDisplayModeSelected.connect(self._displayWireSurfaceWithEdges)
        self._menu.noCutActionTriggered.connect(self._applyCutOption)
        self._menu.isolateActionTriggered.connect(lambda: self.isolate())
        self._menu.showAllActionTriggered.connect(self.showAll)

    def _executeContextMenu(self, pos):
        properties = self._selectedItemsInfo()
        if properties is None:
            return

        self._menu.execute(pos, properties)

    def _showContextMenu(self, pos):
        self._executeContextMenu(self._list.mapToGlobal(pos))

    def _showContextMenuOnRenderingView(self, pos):
        #  VTK ignores device pixel ratio and uses real pixel values only
        ratio = app.qApplication.primaryScreen().devicePixelRatio()
        x = pos.x() * ratio
        y = (self._view.height() - pos.y() - 1) * ratio
        actor = self._view.pickActor(x, y)
        if actor:
            self._actorPicked(actor, False, False, True)
            self._executeContextMenu(self._view.mapToGlobal(pos))

    def _displayItems(self, items) -> list:
        """Every part under a selection, group headings expanded (R26).

        A heading row is not a part and has no `ActorInfo`; it stands for the
        parts beneath it. Selecting `fluid` and choosing Hide has to reach
        `fluid:internalMesh` and every boundary under it, which is the only
        way to uncover the geometry the block is drawn on top of.
        """
        resolved = {}
        pending = list(items)
        while pending:
            item = pending.pop(0)
            if isinstance(item, DisplayItem):
                resolved.setdefault(item.actorInfo().id(), item)
                continue
            pending.extend(item.child(index)
                           for index in range(item.childCount()))
        return list(resolved.values())

    def _selectedItemsInfo(self) -> Optional[Properties]:
        items = self._displayItems(self._list.selectedItems())
        if not items:
            return None

        self._selectedItems = []
        baseProp: Properties = items[0].actorInfo().properties()
        properties = Properties(baseProp.visibility,
                                baseProp.opacity,
                                baseProp.color,
                                baseProp.displayMode,
                                baseProp.cutEnabled,
                                baseProp.highlighted)

        for item in items:
            self._selectedItems.append(item)
            properties.merge(item.actorInfo().properties())

        return properties

    def _showActors(self):
        for item in self._selectedItems:
            item.setActorVisible(True)

        self._visibilityChanged()

    def _hideActors(self):
        for item in self._selectedItems:
            item.setActorVisible(False)

        self._visibilityChanged()

    def isolate(self, ids=None):
        """Show only these parts. Everything else is hidden, not removed."""
        keep = set(ids) if ids is not None else set(self.selectedActorIds())
        if not keep:
            return False

        for key, item in self._items.items():
            item.setActorVisible(key in keep)
        self._isolated = True
        self._visibilityChanged()
        return True

    def hiddenActorCount(self) -> int:
        """How many parts are in the scene but not on screen.

        DP-353. `Show all` had no way to say what it had just done, because
        the only thing that knew was this dictionary. The count is the
        difference the press makes, and a press that makes none is worth
        saying out loud too.
        """
        return sum(1 for item in self._items.values()
                   if not item.isActorVisible())

    def showAll(self):
        for item in self._items.values():
            item.setActorVisible(True)
        self._isolated = False
        self._visibilityChanged()

    def selectParts(self, keys, additive: bool = False) -> list[str]:
        """Select parts named by id, as a viewport click on them would.

        DP-712 (viewport audit 0925 F2/F3). The overlay's rows are the parts
        a user is looking at, so a click on one selects it -- Ctrl adds to or
        removes from the selection -- and Fit and Isolate then act on it
        through ``selectedActorIds`` like any other selection. It selects
        the part's row exactly as a pick in the viewport does
        (`_actorPicked`), so a part the selection service holds an entity for
        reaches the service by the same road -- the geometry manager forwards
        `selectedActorsChanged` to it -- and every view of it agrees.
        """
        keys = [str(key) for key in keys if str(key) in self._items]
        if not additive:
            self._list.clearSelection()
        for key in keys:
            item = self._items[key]
            item.setSelected(not item.isSelected() if additive else True)
        return self.selectedActorIds()

    def setVisibilities(self, mapping: dict) -> int:
        """Show and hide many parts in one step; ids not held are skipped.

        DP-711. The Region picker turns one tick into a visibility for every
        part of a region. Setting them row by row would repaint and recount
        once per row; this repaints once. Returns how many ids it held.
        """
        held = 0
        for key, visible in mapping.items():
            item = self._items.get(key)
            if item is None:
                continue
            item.setActorVisible(bool(visible))
            held += 1
        # Show all reads this flag: parts a picker hid are parts to bring back.
        self._isolated = any(not item.isActorVisible()
                             for item in self._items.values())
        self._visibilityChanged()
        return held

    _PLAN_DISPLAY_MODES = {
        'wireframe': DisplayMode.WIREFRAME,
        'surface': DisplayMode.SURFACE,
        'surface_edge': DisplayMode.SURFACE_EDGE,
    }

    def applyViewPlan(self, plan) -> int:
        """Put the whole scene into the state a view mode asked for.

        CP-09 item 5. Everything above this line works on *the selection*:
        ``_displayWireframe`` and friends change the rows you have clicked,
        which is right for "make this patch wireframe" and useless for "show
        me the boundary mesh", because that means acting on parts the user has
        not selected and must not have to. A plan names the ids, so the mode
        does not disturb -- or depend on -- what happens to be selected.

        Returns how many of the plan's actors this control actually holds; a
        plan naming nothing present changes nothing, which is what lets the
        window report an empty mode rather than blanking the viewport.
        """
        mode = self._PLAN_DISPLAY_MODES.get(getattr(plan, 'display_mode', ''))
        visible = [key for key in plan.visible if key in self._items]
        for key in plan.visible:
            item = self._items.get(key)
            if item is None:
                continue
            item.setActorVisible(True)
            if mode is not None:
                item.actorInfo().setDisplayMode(mode)
        for key in plan.hidden:
            item = self._items.get(key)
            if item is not None:
                item.setActorVisible(False)
        # Isolation is a *user* state ("show only what I picked") and the Show
        # All button reads it. A mode hides things too, so the flag has to
        # follow, or Show all would look spent while parts are still hidden.
        self._isolated = bool(plan.hidden)
        self._visibilityChanged()
        return len(visible)

    def selectedActors(self):
        """The rows currently selected, as DisplayItems."""
        return self._displayItems(self._list.selectedItems())

    def selectedActorIds(self) -> list[str]:
        """Which actors are selected, according to the one selection model.

        Isolate and zoom used to read the rows directly, so a selection made
        in the tree or by a viewport pick reached them only if the rows had
        already been synced. Asking the service instead means every source
        of a selection drives them identically. Parts with no selection
        entity - mesh boundaries, for one - still fall back to the rows.
        """
        ids = [value for value in app.selectionService.selected_actor_ids()
               if value in self._items]
        # R26. Group headings are selectable now, and a heading has no actor
        # of its own -- it stands for the parts under it.
        rows = [item.actorInfo().id()
                for item in self._displayItems(self._list.selectedItems())]
        governed = app.selectionService.governed_actor_ids()
        return list(dict.fromkeys(
            ids + [value for value in rows if value not in governed]))

    def actorInfosFor(self, ids) -> list:
        """The ActorInfo for each id this control actually holds."""
        return [self._items[value].actorInfo()
                for value in ids if value in self._items]

    def visibilitySummary(self) -> tuple[int, int]:
        """(shown, total) -- what the "3 of 11 parts shown" chip reports."""
        keys = countedPartIds(self._items)
        shown = sum(1 for key in keys
                    if self._items[key].actorInfo().isVisible())
        return shown, len(keys)

    def isIsolated(self) -> bool:
        return self._isolated

    def _visibilityChanged(self):
        self.visibilityChanged.emit(*self.visibilitySummary())
        self._view.refresh()

    def refreshTransparency(self):
        """Re-decide depth peeling after a caller changed an actor's opacity.

        DP-818. Fading the geometry while a region seed is placed sets the
        same per-actor opacity this control does, and needs the same answer.
        """
        self._updateTransparency()

    def _updateTransparency(self):
        """Turn depth peeling on exactly while something translucent is drawn.

        Per-actor opacity has always been offered with no order-independent
        transparency behind it, so two overlapping translucent patches rendered
        in whatever order their props happened to sit in and the control was
        lying about what it showed. Peeling and multisampling are mutually
        exclusive on this backend, so the trade is made only when it is needed.
        """
        translucent = any(
            (item.actorInfo().properties().opacity or 1.0) < 1.0
            and item.actorInfo().isVisible()
            for item in self._items.values())
        setter = getattr(self._view, 'setOrderIndependentTransparency', None)
        if setter is not None:
            setter(translucent)

    def _applyCutOption(self, enabled):
        option = self._cutTool.option()
        if option is None:
            return
        cutType, planes = option
        if cutType == CutType.CLIP:
            for item in self._selectedItems:
                item.setCutEnabled(enabled)
                item.actorInfo().clip(planes)
        else:
            for item in self._selectedItems:
                item.setCutEnabled(enabled)
                item.actorInfo().slice(planes)

        self._view.refresh()

    def _displayWireframe(self):
        for item in self._selectedItems:
            item.actorInfo().setDisplayMode(DisplayMode.WIREFRAME)

        self._view.refresh()

    def _displaySurface(self):
        for item in self._selectedItems:
            item.actorInfo().setDisplayMode(DisplayMode.SURFACE)

        self._view.refresh()

    def _displayWireSurfaceWithEdges(self):
        for item in self._selectedItems:
            item.actorInfo().setDisplayMode(DisplayMode.SURFACE_EDGE)

        self._view.refresh()

    def _applyOpacity(self, opacity):
        for item in self._selectedItems:
            item.actorInfo().setOpacity(opacity)

        self._updateTransparency()
        self._view.refresh()

    def _applyColor(self, color):
        for item in self._selectedItems:
            item.setActorColor(color)

        # The colour was written into the actor and nothing asked the window
        # to draw again, so the part kept its old colour until some unrelated
        # interaction happened to trigger a render.
        self.refreshView()
        self.partsChanged.emit()

    def _actorPicked(self, actor, ctrlKeyPressed=False, shiftKeyPressed=False,
                     forContextMenu=False):
        if not ctrlKeyPressed and not forContextMenu:
            self._list.clearSelection()

        if not actor:
            return

        actorInfoId: str = actor.GetObjectName()
        if actorInfoId not in self._items:
            return

        if shiftKeyPressed and not forContextMenu:
            # Shift asks for the volume this face belongs to. The service
            # owns that decision, and its renderer callback selects the rows
            # back here, so the answer is the same from every source.
            app.selectionService.viewport_picked(
                [actorInfoId], expand='volume')
            return

        item = self._items[actorInfoId]
        if not item.isSelected() and forContextMenu:
            self._list.clearSelection()

        if ctrlKeyPressed:
            item.setSelected(not item.isSelected())
        else:
            item.setSelected(True)

    def _actorSourceUpdated(self, id_):
        option = self._cutTool.option()
        if option is None:
            return
        cutType, planes = option
        if cutType == CutType.CLIP:
            self._items[id_].actorInfo().clip(planes)
        else:
            self._items[id_].actorInfo().slice(planes)
