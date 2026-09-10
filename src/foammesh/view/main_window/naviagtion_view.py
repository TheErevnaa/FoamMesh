#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import Enum

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush, QFont, QFontMetrics, QStandardItem, QStandardItemModel)
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QPushButton, QSizePolicy, QTreeView)

from foammesh.db.configurations_schema import Step


steps = {
    'geometryStep': Step.GEOMETRY,
    'geometryRepairStep': Step.GEOMETRY_REPAIR,
    'regionStep': Step.REGION,
    'baseGridStep': Step.BASE_GRID,
    'castellationStep': Step.CASTELLATION,
    'snapStep': Step.SNAP,
    'boundaryLayerStep': Step.BOUNDARY_LAYER,
    'exportStep': Step.EXPORT
}


#: Item role carrying a non-Step navigation token (Scene, engine branch nodes).
TOKEN_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_ROLE = TOKEN_ROLE + 1
BASE_LABEL_ROLE = STATUS_ROLE + 1


class WorkflowRowState(str, Enum):
    COMPLETED = 'completed'
    CURRENT = 'current'
    AVAILABLE = 'available'
    LOCKED = 'locked'
    OPTIONAL = 'optional'
    SKIPPED = 'skipped'
    WARNING = 'warning'
    FAILED = 'failed'
    STALE = 'stale'
    #: A stage is executing right now. It used to share CURRENT with "the row
    #: you are standing on", so a running task and a merely open one drew the
    #: same mark and the outline could not say which was which.
    RUNNING = 'running'
    #: A valid report exists; the mesh was measured, not approved. Neutral on
    #: purpose -- Plan 23 §8.5 reserves the green completion mark for an actual
    #: engineering pass, and a GF2 task whose report says ``fail`` is
    #: ``EVIDENCED``.
    EVIDENCED = 'evidenced'
    #: An engineer accepted a non-passing report. Distinct from WARNING so a
    #: recorded human decision is never mistaken for a measurement.
    WAIVED = 'waived'
    #: R94. Settings have been entered and nothing has been run with them.
    #: This used to share COMPLETED with an actual pass, so after the Base
    #: Grid reset of R85 the row read as a finished stage over a case holding
    #: no mesh at all -- MEASURED on venturi.stl, with the toolbar beside it
    #: reading `0 cells` and the strip under the viewport reading "No mesh
    #: yet". It is the state every task sits in between being filled in and
    #: being run, so the outline could not distinguish a workflow that had
    #: been described from one that had been executed.
    CONFIGURED = 'configured'


#: One mark per state, and no state told apart by colour alone.
#:
#: Locked, ready and open rows used to be three greys apart: a shaded box, a
#: hollow circle, and a triangle that *replaced* whichever of them the row had
#: earned. Every mark below is a different shape, and each one still reads in a
#: screenshot printed in black and white.
_STATUS_PREFIX = {
    WorkflowRowState.COMPLETED: '✔',
    #: Kept for callers that still name the open row. It is rendered with the
    #: row's own status now, so this reaches the tree only for a row that has
    #: no recorded state at all.
    WorkflowRowState.CURRENT: '○',
    WorkflowRowState.AVAILABLE: '○',
    WorkflowRowState.LOCKED: '🔒',
    WorkflowRowState.OPTIONAL: '◇',
    WorkflowRowState.SKIPPED: '–',
    WorkflowRowState.WARNING: '⚠',
    WorkflowRowState.FAILED: '✖',
    WorkflowRowState.STALE: '↻',
    WorkflowRowState.RUNNING: '▶',
    WorkflowRowState.EVIDENCED: '▤',
    WorkflowRowState.WAIVED: '⚑',
    #: Half-filled: the settings are in place, the run is not.
    WorkflowRowState.CONFIGURED: '◐',
}

#: What each mark means, spelled out for the tooltip and the screen reader.
_STATUS_MEANING = {
    WorkflowRowState.COMPLETED: 'done',
    WorkflowRowState.CURRENT: 'ready',
    WorkflowRowState.AVAILABLE: 'ready',
    WorkflowRowState.LOCKED: 'locked - an earlier task is unfinished',
    WorkflowRowState.OPTIONAL: 'optional',
    WorkflowRowState.SKIPPED: 'skipped',
    WorkflowRowState.WARNING: 'finished with warnings',
    WorkflowRowState.FAILED: 'failed',
    WorkflowRowState.STALE: 'out of date - inputs changed since it ran',
    WorkflowRowState.RUNNING: 'running',
    WorkflowRowState.EVIDENCED: 'measured, not yet approved',
    WorkflowRowState.WAIVED: 'waived by an engineer',
    WorkflowRowState.CONFIGURED: 'set up - not run yet',
}


#: Width the outline stylesheet takes out of a row on top of its text: 6 px of
#: ``::item`` padding and 4 px of margin on each side, plus the 3 px accent
#: border ``::item:selected`` adds and two pixels of slack for the focus rect.
#: Read straight off ``base.qss.tmpl`` (R9/R76/R111/R163).
_ROW_CHROME_PX = 2 * (6 + 4) + 3 + 2


class NavigationView(QObject):
    currentStepChanged = Signal(int, int)
    currentStepReactivated = Signal(int)
    sceneRequested = Signal()
    #: A non-Step branch route was chosen; carries its token.
    branchRequested = Signal(str)

    def __init__(self, ui):
        super().__init__()

        self._ui = ui
        self._steps = ui.stepButtons
        self._installRepairStep()
        self._currentStep = Step.NONE
        self._workingStep = Step.GEOMETRY

        for b in self._steps.buttons():
            self._steps.setId(b, steps[b.objectName()])

        self._installStepTree()
        self._connectSignalsSlots()

    def _installStepTree(self):
        """Install only engine-neutral workflow nodes.

        Engine-native tasks are token-routed children of Meshing Method.  The
        legacy buttons remain as compatibility controllers while StepManager
        is retired, but they are never mounted into the visible tree.
        """
        self._model = QStandardItemModel(self)
        self._tree = QTreeView(self._ui.navigation)
        self._tree.setObjectName('workflowStepTree')
        # The tree font is owned by the stylesheet, not declared here as well.
        # Two places each asking for 12pt DemiBold made the outline noticeably
        # larger than every other list in the app -- and than the same outline
        # in FoamFlow, where it is plain body text.
        self._tree.setAccessibleName(self.tr('Meshing workflow steps'))
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(14)
        self._tree.setUniformRowHeights(True)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # A4. "Surface Features & Ref..." lost its tail with no way to read it:
        # the pane has no grip, and the end of a name is exactly where these
        # names differ. Eliding from the middle keeps both ends, the column now
        # grows to the longest row and scrolls rather than clipping, and every
        # row carries its full name in a tooltip.
        self._tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setStretchLastSection(False)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        self._items = {}
        # A2/A3. The outline numbered three of six rows and skipped Region
        # entirely, so while the Region page was on screen the highlight sat on
        # "2. Repair" and the numbering described no order at all. Every stage
        # is numbered now, in the order the wizard walks them; Scene / Display
        # stays unnumbered because it is not a stage -- it is reachable at any
        # point, including for a mesh that has no stages.
        #
        # R10/R13. `3. Region` was a fourth entry here, and it was both a
        # duplicate and a dead end: regions are created on `Domain & Regions`
        # nested under Meshing Method, this row stayed lock-greyed for the
        # whole run even after a fluid region had been added there, and the
        # wizard walked 2. Repair straight to 4. Meshing Method past it. A
        # number is a promise about order, so a numbered row the wizard never
        # visits is a numbering that describes nothing. The Region *page* is
        # unchanged -- it is reached through the engine task that owns it.
        #
        # R209. The numbered stages start at 3. The two rows a user answers
        # before there is any geometry to answer them about -- Mesh Intent,
        # which since Plan 28 is what chooses the engine, and Execution, which
        # says what it may run on -- were numbered 4 and 5 and filed below the
        # engine branch's thirteen task rows. They are installed above these
        # by `MeshingMethodBranch`.
        ordered = (
            ('geometryStep', self.tr('3. Geometry')),
            ('geometryRepairStep', self.tr('4. Repair')),
        )
        for object_name, label in ordered:
            step = steps[object_name]
            item = QStandardItem(label)
            item.setData(label, BASE_LABEL_ROLE)
            item.setData(int(step), Qt.ItemDataRole.UserRole)
            item.setAccessibleText(label)
            self._model.appendRow(item)
            self._items[step] = item
            self._setItemState(
                item,
                WorkflowRowState.CURRENT
                if step == Step.GEOMETRY else WorkflowRowState.LOCKED)
        # R209. Scene / Display belongs under Geometry -- it is how you look
        # at what the case holds, not a stage of its own -- and as a top-level
        # row it sat at the very bottom of the outline, past a whole engine
        # branch, away from the thing it displays.
        #
        # It was promoted to top level because external-mesh mode hides every
        # generation row and a child of a hidden row is hidden with it, which
        # left an opened polyMesh with no route to the actor tree, the
        # colours, the section plane or the quality controls -- on precisely
        # the meshes people open in order to inspect them. `_placeSceneItem`
        # is what keeps that fixed: the row is promoted back to the root for
        # as long as that mode lasts, and nested again when it ends.
        self._sceneItem = QStandardItem(self.tr('Scene / Display'))
        self._sceneItem.setData(self.tr('Scene / Display'), BASE_LABEL_ROLE)
        self._sceneItem.setAccessibleText(
            self.tr('Scene and display controls'))
        self._sceneItem.setToolTip(self.tr(
            'Scene and display controls. Always available; not a workflow '
            'stage, which is why it carries no number.'))
        self._sceneItem.setData('scene', int(Qt.ItemDataRole.UserRole) + 1)
        self._items[Step.GEOMETRY].appendRow(self._sceneItem)
        self._setItemState(self._sceneItem, WorkflowRowState.AVAILABLE)
        self._tree.setModel(self._model)
        self._tree.expandAll()
        self._updateTreeHeight()
        self._ui.horizontalLayout_2.insertWidget(0, self._tree)
        for button in self._steps.buttons():
            button.hide()
        self._ui.workflowStepTree = self._tree
        self._tree.clicked.connect(self._treeClicked)

    def _updateTreeHeight(self):
        """Ask for room for the rows that actually exist.

        The height was fixed at four rows when the tree was built and never
        revisited, so the engine branch -- thirteen rows on snappy -- unrolled
        into a pane sized for a quarter of it. Capped, because the outline is a
        band above the content and must not swallow the page.
        """
        rows = 0
        stack = [self._model.invisibleRootItem()]
        while stack:
            parent = stack.pop()
            for row in range(parent.rowCount()):
                child = parent.child(row)
                rows += 1
                if child.rowCount():
                    stack.append(child)
        rows = max(4, min(rows, 16))
        row_height = self._tree.fontMetrics().height() + 16
        self._tree.setMinimumHeight(
            rows * row_height + 2 * self._tree.frameWidth())

    def _treeClicked(self, index):
        token = index.data(TOKEN_ROLE)
        if token == 'scene':
            self.sceneRequested.emit()
            return
        if token:
            # Engine branch routes are published by the selected engine, so
            # they carry a token instead of a numeric Step (SH8).
            self.branchRequested.emit(str(token))
            return
        step_data = index.data(Qt.ItemDataRole.UserRole)
        if step_data is None:
            return
        step = int(step_data)
        button = self._steps.button(step)
        if button is not None and button.isEnabled():
            if step == self._currentStep:
                self.currentStepReactivated.emit(step)
                return
            button.setChecked(True)
            self._stepChanged(step)

    def _installRepairStep(self):
        """Insert a workflow step while the Designer shell is being replaced."""
        if hasattr(self._ui, 'geometryRepairStep'):
            return
        button = QPushButton(self.tr('&Repair'), self._ui.navigation)
        button.setObjectName('geometryRepairStep')
        button.setCheckable(True)
        button.setMinimumHeight(32)
        self._ui.horizontalLayout_2.insertWidget(2, button)
        self._ui.stepButtons.addButton(button)
        self._ui.geometryRepairStep = button
        # F-27. These are compatibility controllers, never mounted in the
        # visible tree, and they carried a numbering of their own: the outline
        # said `4. Repair` while the button underneath it said `&2. Repair`,
        # and the engine tasks below it were numbered 3..8 although they are
        # children of Meshing Method now. A number is a promise about order,
        # so only the outline -- the thing a user reads -- makes it.
        labels = (
            (self._ui.geometryStep, '&Geometry'), (button, '&Repair'),
            (self._ui.regionStep, '&Region'), (self._ui.baseGridStep, '&Base Grid'),
            (self._ui.castellationStep, '&Castellation'), (self._ui.snapStep, '&Snap'),
            (self._ui.boundaryLayerStep, '&Boundary Layer'),
            (self._ui.exportStep, '&Export'))
        for item, text in labels:
            item.setText(self.tr(text))

    def installBranchNode(self, label, token, afterStep, *, atTop=False):
        """Insert a token-routed node directly beneath ``afterStep``.

        Used for Meshing Method, which §5.1 places immediately after the repair
        decision but which has no numeric Step of its own (SH8).

        ``atTop`` puts the node above every numbered stage instead (R209).
        Nodes asking for the top queue behind each other exactly as anchored
        nodes do, so a caller's declaration order is the order the outline
        reads.
        """
        if atTop:
            row = getattr(self, '_topInsertRow', 0)
            self._topInsertRow = row + 1
            return self._insertBranchRow(row, label, token)
        anchor = self._items.get(afterStep)
        base = ((anchor.row() + 1) if anchor is not None
                else self._model.rowCount())
        # Every branch node used to be inserted directly beneath the same
        # anchor, so each new one landed *above* the last: asking for Meshing
        # Method, Mesh Intent and Execution in that order produced Execution,
        # Mesh Intent, Meshing Method -- two unnumbered rows wedged between
        # step 2 and step 3. Nodes sharing an anchor now queue behind each
        # other, and the outline reads in the order it was declared.
        self._branchInsertRows = getattr(self, '_branchInsertRows', {})
        row = max(base, self._branchInsertRows.get(int(afterStep), base))
        self._branchInsertRows[int(afterStep)] = row + 1
        return self._insertBranchRow(row, label, token)

    def _insertBranchRow(self, row, label, token):
        """Put one token-routed row at ``row`` and register it."""
        item = QStandardItem(label)
        item.setData(label, BASE_LABEL_ROLE)
        item.setData(token, TOKEN_ROLE)
        item.setAccessibleText(label)
        self._model.insertRow(row, item)
        self._branchNodes = getattr(self, '_branchNodes', {})
        self._branchNodes[token] = item
        self._setItemState(item, WorkflowRowState.AVAILABLE)
        self._tree.expandAll()
        self._updateTreeHeight()
        return item

    def _placeSceneItem(self, nested: bool) -> None:
        """Nest Scene / Display under Geometry, or promote it to the root.

        Nested is where it belongs. The root is where it has to be while an
        externally opened mesh is being shown: that mode hides every
        generation row, and a child of a hidden row is hidden with it.
        """
        anchor = self._items.get(Step.GEOMETRY)
        at_root = self._sceneItem.parent() is None
        if nested and anchor is not None:
            if not at_root:
                return
            source, target = self._model.invisibleRootItem(), anchor
        else:
            if at_root:
                return
            source, target = (self._sceneItem.parent(),
                              self._model.invisibleRootItem())
        target.appendRow(source.takeRow(self._sceneItem.row()))
        self._tree.expandAll()
        self._updateTreeHeight()

    def setBranchChildren(self, token, entries):
        """Replace the child routes of a branch node.

        ``entries`` is ``(label, child_token, enabled[, state])``. The
        engine publishes these, so switching engines replaces the whole set
        rather than editing a fixed list.
        """
        item = getattr(self, '_branchNodes', {}).get(token)
        if item is None:
            return ()
        item.removeRows(0, item.rowCount())
        children = []
        for entry in entries:
            label, child_token = entry[0], entry[1]
            enabled = entry[2] if len(entry) > 2 else True
            state = (
                WorkflowRowState(entry[3]) if len(entry) > 3
                else (WorkflowRowState.AVAILABLE
                      if enabled else WorkflowRowState.LOCKED)
            )
            child = QStandardItem(label)
            child.setData(label, BASE_LABEL_ROLE)
            child.setData(child_token, TOKEN_ROLE)
            child.setAccessibleText(f'{label}, {state.value}')
            item.appendRow(child)
            self._setItemState(child, state)
            child.setEnabled(bool(enabled) and state is not WorkflowRowState.LOCKED)
            children.append(child)
        self._tree.expandAll()
        self._updateTreeHeight()
        return tuple(children)

    def branchNode(self, token):
        return getattr(self, '_branchNodes', {}).get(token)

    def setBranchCurrent(self, token: str) -> bool:
        """Select and mark a token route without changing another shell region."""
        node = self.branchNode(token)
        if node is not None:
            self._tree.setCurrentIndex(node.index())
            self._setItemState(node, WorkflowRowState.CURRENT)
            return True
        for parent in getattr(self, '_branchNodes', {}).values():
            for row in range(parent.rowCount()):
                child = parent.child(row)
                if child.data(TOKEN_ROLE) == token:
                    self._tree.setCurrentIndex(child.index())
                    self._setItemState(child, WorkflowRowState.CURRENT)
                    return True
        return False

    def routeTokens(self) -> tuple[str, ...]:
        values = []
        for parent in getattr(self, '_branchNodes', {}).values():
            token = parent.data(TOKEN_ROLE)
            if token:
                values.append(str(token))
            for row in range(parent.rowCount()):
                token = parent.child(row).data(TOKEN_ROLE)
                if token:
                    values.append(str(token))
        return tuple(values)

    def requestBranch(self, token: str) -> None:
        """Programmatically follow the same route used by a tree activation."""
        self.branchRequested.emit(str(token))

    def _setItemState(self, item, state: WorkflowRowState):
        """Render a row's own state, or select it without overwriting that.

        A9. ``CURRENT`` used to be a state like any other, so opening a row
        replaced whatever it had earned -- a pass, a warning, a lock -- with a
        triangle, and the one row whose state you could not read was the row
        you were standing on. Selection is drawn by the stylesheet
        (``QTreeView#workflowStepTree::item:selected``); the glyph goes on
        reporting the task.
        """
        state = WorkflowRowState(state)
        if state is WorkflowRowState.CURRENT:
            stored = item.data(STATUS_ROLE)
            state = (WorkflowRowState(stored) if stored
                     else WorkflowRowState.AVAILABLE)
        else:
            item.setData(state.value, STATUS_ROLE)
        label = str(item.data(BASE_LABEL_ROLE) or item.text())
        prefix = _STATUS_PREFIX[state]
        item.setText(f'{prefix}  {label}')
        meaning = _STATUS_MEANING.get(state, state.value)
        item.setToolTip(f'{label} - {meaning}')
        item.setAccessibleText(f'{label}, {meaning}')
        self._applyLabelSizeHint(item)
        if state is WorkflowRowState.LOCKED:
            item.setForeground(QBrush(Qt.GlobalColor.gray))
            item.setBackground(QBrush(
                Qt.GlobalColor.transparent, Qt.BrushStyle.Dense6Pattern))
        else:
            item.setForeground(QBrush())
            item.setBackground(QBrush())

    def _applyLabelSizeHint(self, item):
        """Size a row for how it looks when it is the selected one.

        R9/R76/R111/R163. The column is ``ResizeToContents``, and the width it
        resolves to is measured from the row as it is drawn *unselected*.
        Selecting a row changes how it is drawn: ``setWorkingStep`` puts its
        font in bold, and ``QTreeView#workflowStepTree::item:selected`` adds a
        3 px accent border on the left. Neither is in the measurement, so the
        selected row -- and only the selected row -- no longer fitted the
        column it had asked for and the delegate elided it. MEASURED:
        `4. Meshing Method` drawn in full until it was clicked and
        `4. Me...g Method` afterwards, and `Qualifi...on Summary` on the row
        the user was standing on, in a 175-180 px panel with room to spare.

        Measuring in bold, with the chrome the stylesheet adds, means the
        width a row asks for is the width it needs at its widest.
        """
        label = str(item.data(BASE_LABEL_ROLE) or item.text())
        font = QFont(item.font())
        font.setBold(True)
        metrics = QFontMetrics(font)
        text = item.text() or label
        item.setSizeHint(QSize(
            metrics.horizontalAdvance(text) + _ROW_CHROME_PX,
            metrics.height()))

    def currentStep(self):
        return self._currentStep

    def setCurrentStep(self, step):
        self._steps.button(step).setChecked(True)
        item = self._items.get(step)
        if item is not None:
            self._tree.setCurrentIndex(item.index())
            self._setItemState(item, WorkflowRowState.CURRENT)
        self._stepChanged(step)

    def enableStep(self, step):
        self._steps.button(step).setEnabled(True)
        if step in self._items:
            self._items[step].setEnabled(True)
            self._setItemState(
                self._items[step], WorkflowRowState.AVAILABLE)
        self._updateBatchStepsEnabled()

    def disableStep(self, step):
        self._steps.button(step).setEnabled(False)
        if step in self._items:
            self._items[step].setEnabled(False)
            self._setItemState(self._items[step], WorkflowRowState.LOCKED)

        self._updateBatchStepsEnabled()

    def setWorkingStep(self, step):
        def setBold(button, bold):
            font = button.font()
            font.setBold(bold)
            button.setFont(font)

        self.enableStep(step)
        setBold(self._steps.button(self._workingStep), False)
        if self._workingStep in self._items:
            font = self._items[self._workingStep].font()
            font.setBold(False)
            self._items[self._workingStep].setFont(font)
        self._workingStep = step
        setBold(self._steps.button(step), True)
        if step in self._items:
            font = self._items[step].font()
            font.setBold(True)
            self._items[step].setFont(font)
            self._setItemState(self._items[step], WorkflowRowState.CURRENT)

    def setExternalMeshMode(self, enabled: bool):
        """Collapse authored generation navigation for an externally sourced mesh.

        The generation steps go; Scene / Display stays. Hiding the whole tree
        used to take the only route to Display Control with it, leaving an
        opened mesh with no actor tree, no section plane and no quality
        controls -- on precisely the meshes people open in order to inspect
        them.
        """
        # `navigation` is the group box the tree lives in, so hiding it hid the
        # tree too -- including the Scene row that is meant to survive. The
        # step *buttons* inside it are already hidden; the container stays.
        self._ui.navigation.setVisible(True)
        self._tree.setVisible(True)
        # R209. Scene / Display is a child of Geometry in an authored
        # workflow, and a child of a hidden row is hidden too. It comes back
        # to the root for as long as this mode lasts.
        self._placeSceneItem(nested=not enabled)
        root = self._model.invisibleRootItem()
        for row in range(root.rowCount()):
            item = root.child(row)
            self._tree.setRowHidden(
                row, self._tree.rootIndex(),
                enabled and item is not self._sceneItem)
        if enabled:
            self._currentStep = Step.NONE
        else:
            self._ui.navigation.setEnabled(True)

    def _connectSignalsSlots(self):
        self._steps.idClicked.connect(self._stepChanged)

    def _stepChanged(self, step=None):
        step = self._steps.id(self._steps.checkedButton())
        self.currentStepChanged.emit(step, self._currentStep)
        self._currentStep = step

    def _updateBatchStepsEnabled(self):
        enabled = self._ui.castellationStep.isEnabled()
        for step, button in (
                (Step.SNAP, self._ui.snapStep),
                (Step.BOUNDARY_LAYER, self._ui.boundaryLayerStep)):
            button.setEnabled(enabled)
            if step in self._items:
                self._items[step].setEnabled(enabled)
