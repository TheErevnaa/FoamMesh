#!/usr/bin/env python
# -*- coding: utf-8 -*-

import math
from enum import Enum

from PySide6.QtCore import QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush, QFont, QFontMetrics, QFontMetricsF, QPalette, QRegion,
    QStandardItem, QStandardItemModel)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView, QPushButton,
    QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTreeView)

from foammesh.db.configurations_schema import Step
from foammesh.view.theming.metrics import CONTROL_HEIGHT


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


#: Item role carrying a non-Step navigation token (engine branch nodes).
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
#:
#: R166. Every mark below is also a mark `Pretendard Variable` actually
#: contains. Six of them were not: DONE, LOCKED, FAILED, EVIDENCED, WAIVED
#: and CONFIGURED asked for characters the application font has no glyph
#: for, so Qt fell back per character and the outline painted six of its
#: thirteen states as the same undifferentiated box -- the exact opposite
#: of the promise two paragraphs up. LOCKED was worse again: U+1F512
#: resolved to Segoe UI Emoji, putting one full-colour pictogram in a
#: column of monochrome marks. The replacements are checked with
#: ``QRawFont.fromFont(app.font()).supportsCharacter``, not by eye,
#: because a missing glyph is invisible in source and shows only on screen.
_STATUS_PREFIX = {
    WorkflowRowState.COMPLETED: '✓',
    #: Kept for callers that still name the open row. It is rendered with the
    #: row's own status now, so this reaches the tree only for a row that has
    #: no recorded state at all.
    WorkflowRowState.CURRENT: '○',
    WorkflowRowState.AVAILABLE: '○',
    #: A solid block, not a padlock: the font carries no padlock.
    WorkflowRowState.LOCKED: '■',
    WorkflowRowState.OPTIONAL: '◇',
    WorkflowRowState.SKIPPED: '–',
    WorkflowRowState.WARNING: '⚠',
    WorkflowRowState.FAILED: '✗',
    WorkflowRowState.STALE: '↻',
    WorkflowRowState.RUNNING: '▶',
    WorkflowRowState.EVIDENCED: '▢',
    WorkflowRowState.WAIVED: '※',
    #: Half-filled: the settings are in place, the run is not.
    WorkflowRowState.CONFIGURED: '⬒',
}

#: What each mark means, spelled out for the tooltip and the screen reader.
_STATUS_MEANING = {
    WorkflowRowState.COMPLETED: 'done',
    WorkflowRowState.CURRENT: 'ready',
    WorkflowRowState.AVAILABLE: 'ready',
    WorkflowRowState.LOCKED: 'locked until an earlier task is finished',
    WorkflowRowState.OPTIONAL: 'optional',
    WorkflowRowState.SKIPPED: 'skipped',
    WorkflowRowState.WARNING: 'finished with warnings',
    WorkflowRowState.FAILED: 'failed',
    WorkflowRowState.STALE: 'out of date because inputs changed since it ran',
    WorkflowRowState.RUNNING: 'running',
    WorkflowRowState.EVIDENCED: 'measured, not yet approved',
    WorkflowRowState.WAIVED: 'waived by an engineer',
    WorkflowRowState.CONFIGURED: 'set up but not run yet',
}


#: Width the outline stylesheet takes out of a row on top of its text: 6 px of
#: ``::item`` padding and 4 px of margin on each side, plus the 3 px accent
#: border ``::item:selected`` adds and two pixels of slack for the focus rect.
#: Read straight off ``base.qss.tmpl`` (R9/R76/R111/R163).
_ROW_CHROME_PX = 2 * (6 + 4) + 3 + 2

#: Height the same rule takes out of a row, on top of its text: 4 px of
#: ``::item`` padding and 1 px of margin at the top and at the bottom. A
#: row that asks for the bare line box gets that much of it taken away
#: again and paints the bottom of its letters.
_ROW_CHROME_V_PX = 2 * (4 + 1)

#: Everything Region A spends on a row besides the row itself: the group box
#: frame and its layout margins, plus the tree's own frame. Used only when the
#: widgets have not been laid out yet and cannot be asked; once they have,
#: `_updateTreeWidth` measures the real thing.
_PANE_CHROME_PX = 27

#: The outline never asks for more than this, however long its longest task
#: name is. A pane wide enough for any conceivable name is a pane that has
#: eaten the page it navigates.
OUTLINE_PANE_MAXIMUM = 320


#: The gap between a row's status mark and its label, written once so the
#: delegate that lays the row out and the code that builds the string cannot
#: disagree about where one ends and the other begins.
STATUS_SEPARATOR = '  '


def status_cell_width(font) -> int:
    """The width reserved for a row's status mark, in a given font.

    DP-192. The marks are chosen for what they mean, so they are not all the
    same width: in the shipped font they run from 6 px for the skipped dash
    to 13 px for the warning triangle. Reserving the widest means the label
    beside them starts at one x whichever mark the row is carrying, and it
    keeps doing so if a mark is changed or the font is.
    """
    metrics = QFontMetricsF(font)
    return int(math.ceil(max(metrics.horizontalAdvance(mark)
                             for mark in set(_STATUS_PREFIX.values()))))


class StatusMarkDelegate(QStyledItemDelegate):
    """Draws an outline row as a fixed mark column and a label column.

    DP-192. The mark and the label are one string, so the label began
    wherever the mark happened to end. MEASURED in the shipped font at 10 pt:
    five different label positions across the thirteen row states, a 7 px
    spread, down the column the eye uses to scan the workflow. The row text
    is left as one string because the tooltip, the accessible text and every
    caller read it, and only the drawing is split.
    """

    def sizeHint(self, option, index):
        """The width a row needs once its mark has a column of its own.

        Qt measures the row's string, and the string carries whichever mark
        the row is currently wearing, so rows would go on asking for widths
        that differ by their marks rather than by their names -- and the
        ResizeToContents column would twitch as states changed. The mark's
        own width is swapped for the column's, and nothing else about Qt's
        measurement is second-guessed.
        """
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        mark, separator, _label = text.partition(STATUS_SEPARATOR)
        hint = super().sizeHint(option, index)
        if not separator:
            return hint
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        metrics = QFontMetrics(item.font)
        hint.setWidth(hint.width() - metrics.horizontalAdvance(mark)
                      + status_cell_width(item.font))
        return hint

    def paint(self, painter, option, index):
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        mark, separator, label = text.partition(STATUS_SEPARATOR)
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        if not separator:
            # A row with no status mark -- a branch header, say. Nothing to
            # align it against, so Qt draws it as it always did.
            super().paint(painter, option, index)
            return

        style = item.widget.style() if item.widget else QApplication.style()
        # The row itself first: background, selection band, focus ring. Its
        # text is cleared so the two columns can be placed underneath rather
        # than drawn over.
        blank = QStyleOptionViewItem(item)
        blank.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, blank,
                          painter, item.widget)

        area = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, item, item.widget)
        cell = status_cell_width(item.font)
        metrics = QFontMetrics(item.font)
        selected = bool(item.state & QStyle.StateFlag.State_Selected)
        group = (QPalette.ColorGroup.Normal
                 if item.state & QStyle.StateFlag.State_Enabled
                 else QPalette.ColorGroup.Disabled)
        role = (QPalette.ColorRole.HighlightedText if selected
                else QPalette.ColorRole.Text)

        painter.save()
        painter.setFont(item.font)
        painter.setPen(item.palette.color(group, role))
        flags = (Qt.AlignmentFlag.AlignLeft
                 | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(QRect(area.left(), area.top(), cell, area.height()),
                         flags, mark)
        gap = metrics.horizontalAdvance(STATUS_SEPARATOR)
        left = area.left() + cell + gap
        room = area.right() - left + 1
        if room > 0:
            # DP-205. The mode Qt already handed us, not one named here. The
            # tree asks for ElideMiddle and says why: these names differ at
            # the end, and the pane has no grip to widen. Naming a mode here
            # answered a question the view had already answered, and since
            # this delegate paints every row that carries a mark -- which is
            # every step -- the view's answer reached nothing.
            painter.drawText(
                QRect(left, area.top(), room, area.height()), flags,
                metrics.elidedText(label, item.textElideMode, room))
        painter.restore()


class WorkflowStepTree(QTreeView):
    """The outline tree, with the selection band drawn once instead of twice.

    Plan 31 DP-145. Qt paints a selected row in two goes: first it fills the
    indentation strip in front of the row -- the column the expand chevrons
    live in -- and then it paints the item itself. Both fills come from the
    same ``QTreeView#workflowStepTree::item`` rule, and that rule carries a
    ``margin: 1px 4px`` and a ``border-radius: 6px`` so the row reads as a
    pill. Applied twice to two adjacent rectangles, those turn one band into
    two: a square-cornered slab floating in the gutter, a dark gap, and then
    the pill. MEASURED on the `dp144-requires` gmsh leg -- the selected
    `Compute Mesh` row carried an orphan 15 px slab of `#2f4a78` at x 26..40
    with a 12 px hole before the row's own highlight at x 53, which reads on
    screen as a stray toggle rather than as part of the selection.

    Every selected row in the outline had one, at every depth: a top-level
    row with no children carried a 14 px slab, a child carried 28 px.

    The strip is clipped out of the row paint and the branches are then drawn
    over the untouched background, so nothing has to guess what colour that
    background is -- it is whatever the pane behind the tree already painted,
    in either theme.
    """

    def drawRow(self, painter, option, index):
        item = self.visualRect(index)
        width = item.left() - option.rect.left()
        if width <= 0:
            # No indentation in front of this row, or a right-to-left layout
            # where the strip sits on the other side. Nothing to clip.
            super().drawRow(painter, option, index)
            return
        strip = QRect(option.rect.left(), option.rect.top(),
                      width, option.rect.height())
        painter.save()
        painter.setClipRegion(QRegion(option.rect) - QRegion(strip),
                              Qt.ClipOperation.IntersectClip)
        super().drawRow(painter, option, index)
        painter.restore()
        # Qt would have drawn these at the end of `drawRow`; they were clipped
        # away with the slab, so they are drawn here instead.
        self.drawBranches(painter, strip, index)


class NavigationView(QObject):
    currentStepChanged = Signal(int, int)
    currentStepReactivated = Signal(int)
    #: A non-Step branch route was chosen; carries its token.
    branchRequested = Signal(str)
    #: The outline now needs a different Region A width; carries that width.
    paneWidthChanged = Signal(int)

    def __init__(self, ui):
        super().__init__()

        self._ui = ui
        self._steps = ui.stepButtons
        self._installRepairStep()
        self._currentStep = Step.NONE
        self._workingStep = Step.GEOMETRY
        self._wantedPaneWidth = 0

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
        self._tree = WorkflowStepTree(self._ui.navigation)
        self._tree.setObjectName('workflowStepTree')
        # DP-192. The status marks are not all the same width, so the
        # labels beside them started at five different x. The delegate
        # gives the mark a column of its own.
        self._markDelegate = StatusMarkDelegate(self._tree)
        self._tree.setItemDelegate(self._markDelegate)
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
        # DP-104. A4 kept the elide mode and then took away the thing that
        # triggers it: `ResizeToContents` grows the column past the pane, so
        # the view clips the overflow with no ellipsis and offers a
        # horizontal scrollbar instead. MEASURED in the twenty-leg sweep --
        # `Qualification Summ`, `Resolution Adequac`, `Native Mesh Fidelity`,
        # each cut mid-word, with a scrollbar under the outline that has to
        # be dragged to finish reading a row and dragged back to see the
        # numbers. Fitting the column to the pane is what makes ElideMiddle
        # work: both ends of the name survive, the ellipsis says a name was
        # shortened, and the tooltip below carries it in full.
        self._tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._tree.header().setStretchLastSection(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        self._items = {}
        # A2/A3. The outline numbered three of six rows and skipped Region
        # entirely, so while the Region page was on screen the highlight sat on
        # "2. Repair" and the numbering described no order at all. Every stage
        # is numbered now, in the order the wizard walks them, and since
        # GEO-07 every row in the outline carries one.
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
        # R209 numbered the stages from 3, because two rows sat above them:
        # Mesh Intent, which since Plan 28 is what chooses the engine, and
        # Execution, which says what a run may use. Plan 32 W1 folded both
        # into the Mesh setup page, so the outline opens on the first thing a
        # user actually does. Geometry is 1 again.
        #
        # Plan 32 section 4.1. `Repair` is `3. Preparation`: by the time a
        # user reaches it the method is chosen, and the row holds everything
        # that has to be true of the geometry before that method can mesh it
        # -- the healing and far-field questions Gmsh asks at import as well
        # as the repair and wrap decisions. `2. Mesh setup` is installed
        # between these two by `MeshingMethodBranch`, anchored on Geometry.
        ordered = (
            ('geometryStep', self.tr('1. Geometry')),
            ('geometryRepairStep', self.tr('3. Preparation')),
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
        # GEO-07 (Plan 33 section 1.1) retires the `Scene / display` row R209
        # nested here. This column lists the steps of the job; looking at the
        # scene is not one of them, which is why that row was the only one
        # with no number and why its own tooltip had to say so. Every reader
        # of the outline -- the numbering, the walk order, the Proceed route,
        # the wizard -- carried a special case for it.
        #
        # The controls are not retired with it: cutting, sectioning and
        # hiding move onto the viewport toolbar, beside the picture they act
        # on, and an externally opened mesh still reaches the panel through
        # the external-mesh page, which is the one route that never had a
        # workflow behind it.
        self._tree.setModel(self._model)
        self._tree.expandAll()
        self._updateTreeExtent()
        self._ui.horizontalLayout_2.insertWidget(0, self._tree)
        for button in self._steps.buttons():
            button.hide()
        self._ui.workflowStepTree = self._tree
        self._tree.clicked.connect(self._treeClicked)

    def _updateTreeExtent(self):
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
        self._updateTreeWidth()

    def wantedPaneWidth(self) -> int:
        """Region A width at which no outline row has to be elided.

        Zero until the tree has rows to measure.
        """
        return self._wantedPaneWidth

    def _updateTreeWidth(self):
        """The same question as the height, asked sideways.

        DP-134. `_applyLabelSizeHint` already measures every row at its widest
        -- bold, with the status glyph and the stylesheet's chrome -- and
        DP-104 then set the column to `Stretch` so that the delegate elides
        into the pane instead of clipping. Both halves are right, and nothing
        asked Region A to be as wide as the rows it holds. Region A is
        `REGION_A_PREFERRED`, a constant 220 px at every window size, which
        leaves about 193 px of tree viewport.

        MEASURED on the Gmsh elbow at a 2560 px window: `Describe Geometry`
        (199 px), `Reference Readiness` (210), `Native Mesh Fidelity` (205),
        `Resolution Adequacy` (210) and `Qualification Summary` (222) all ran
        past it, and `ElideMiddle` cut the *head* off each one -- the outline
        read `Descri... Geometry`, `Refere...Readiness`, `Resol...Adequacy`,
        five of its twenty-two rows unidentifiable without hovering for the
        tooltip. Twenty-nine pixels of pane would have shown every one.

        So ask, the way the height does: the widest row plus its indentation,
        plus the chrome the pane spends around it. Capped, and advisory --
        `apply_sizes` starts the pane here, the handle still moves, and a
        pane the user drags narrower elides and tooltips exactly as before.
        """
        wanted = 0
        indent = self._tree.indentation()
        stack = [(self._model.invisibleRootItem(), 0)]
        while stack:
            parent, depth = stack.pop()
            for row in range(parent.rowCount()):
                child = parent.child(row)
                wanted = max(
                    wanted,
                    (depth + 1) * indent + child.sizeHint().width())
                if child.rowCount():
                    stack.append((child, depth + 1))
        if wanted <= 0:
            return
        # Ask the widgets what the pane really spends, and fall back to the
        # measured constant only while they have no geometry yet.
        chrome = self._ui.navigation.width() - self._tree.width()
        if not 0 < chrome < 80:
            chrome = _PANE_CHROME_PX
        wanted = min(wanted + chrome + 2 * self._tree.frameWidth(),
                     OUTLINE_PANE_MAXIMUM)
        if wanted == self._wantedPaneWidth:
            return
        self._wantedPaneWidth = wanted
        self.paneWidthChanged.emit(wanted)

    def _treeClicked(self, index):
        token = index.data(TOKEN_ROLE)
        if token:
            # Plan 32 §7.2 (DP-236). A row this tree has drawn grey does not
            # route. `setBranchChildren` has disabled every LOCKED row since
            # R10 and the numbered steps below already check
            # `button.isEnabled()`, but the branch rows checked nothing --
            # so the two halves of one answer lived in two places and only
            # one of them was ever asked. Qt does not suppress the click
            # itself here: this slot is wired to the view's `clicked`, which
            # fires for a disabled item as readily as for an enabled one.
            if not index.flags() & Qt.ItemFlag.ItemIsEnabled:
                return
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
        # said `3. Preparation` while the button underneath it said `&2.
        # Repair`,
        # and the engine tasks below it were numbered 3..8 although they are
        # children of Meshing Method now. A number is a promise about order,
        # so only the outline -- the thing a user reads -- makes it.
        labels = (
            (self._ui.geometryStep, '&Geometry'), (button, '&Repair'),
            (self._ui.regionStep, '&Region'), (self._ui.baseGridStep, '&Base grid'),
            (self._ui.castellationStep, '&Castellation'), (self._ui.snapStep, '&Snap'),
            (self._ui.boundaryLayerStep, '&Boundary layer'),
            (self._ui.exportStep, '&Export'))
        for item, text in labels:
            item.setText(self.tr(text))

    def installBranchNode(self, label, token, afterStep, *, atTop=False):
        """Insert a token-routed node directly beneath ``afterStep``.

        Used for Mesh setup, which Plan 32 section 4.1 places immediately
        after Geometry -- you choose a method for a geometry you have loaded
        -- but which has no numeric Step of its own (SH8).

        ``atTop`` puts the node above every numbered stage instead (R209).
        Nodes asking for the top queue behind each other exactly as anchored
        nodes do, so a caller's declaration order is the order the outline
        reads. Nothing asks for the top since Plan 32 W1 retired the two rows
        that did; the argument stays because the outline is assembled by
        callers this class does not own.
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
        self._updateTreeExtent()
        return item

    def nestStepInBranch(self, step, token) -> bool:
        """Move a numbered stage into a branch band, ahead of its task rows.

        DP-271. Plan 32 section 4.1 walks `1. Geometry`, `2. Mesh setup`,
        `3. Preparation` and then the engine rows, and the engine rows are
        children of the Mesh setup node -- so a `3. Preparation` left at the
        root was always painted *below* every one of them. MEASURED on the
        15 September guided campaign, in every frame of all three journeys:
        the outline read 1, 2, 4..11 on snappy and 1, 2, 4..12 on Gmsh, and
        then 3. The walk was never wrong; the painting was.

        A child is drawn under its parent and nowhere else, so the only way
        a row walked between the band header and the band can be painted
        there is to put it in the band. The stage keeps its `Step`, its
        compatibility button, its number and its entry in ``self._items``:
        only its parent moves. ``setBranchChildren`` rebuilds the task rows
        *under* the stages nested here rather than over them.

        Returns False for a token with no node or a step this view does not
        own, because assembling the outline is done by callers this class
        does not own and a missing row is not worth raising inside.
        """
        node = self.branchNode(token)
        item = self._items.get(step)
        if node is None or item is None:
            return False
        self._bandSteps = getattr(self, '_bandSteps', {})
        band = self._bandSteps.setdefault(str(token), [])
        if step not in band:
            band.append(step)
        if item.parent() is not node:
            parent = item.parent() or self._model.invisibleRootItem()
            node.insertRow(band.index(step), parent.takeRow(item.row()))
        self._tree.expandAll()
        self._updateTreeExtent()
        return True

    def _bandStepItems(self, token):
        """The stages nested into ``token``'s band, in the order they lead it."""
        rows = []
        node = self.branchNode(token)
        if node is None:
            return rows
        for step in getattr(self, '_bandSteps', {}).get(str(token), ()):
            item = self._items.get(step)
            if item is not None and item.parent() is node:
                rows.append((step, item))
        return rows

    def _itemWithBaseLabel(self, label: str):
        """The first row in the tree whose base label is ``label``.

        The base label is what survives a rebuild. The item does not -- the
        rows are destroyed and made again -- and the painted text carries the
        status mark in front of the label, so neither is a handle a selection
        can be remembered by.
        """
        stack = [self._model.invisibleRootItem()]
        while stack:
            parent = stack.pop()
            for row in range(parent.rowCount()):
                child = parent.child(row)
                if str(child.data(BASE_LABEL_ROLE) or '') == label:
                    return child
                if child.rowCount():
                    stack.append(child)
        return None

    def setBranchChildren(self, token, entries):
        """Replace the child routes of a branch node.

        ``entries`` is ``(label, child_token, enabled[, state])``. The
        engine publishes these, so switching engines replaces the whole set
        rather than editing a fixed list.
        """
        item = getattr(self, '_branchNodes', {}).get(token)
        if item is None:
            return ()
        # Plan 33 W-C item 1 (elbow P2). MEASURED on both engines, and
        # recorded in `selection-reproduction.json`: the outline read
        # `3. Preparation` before the refresh the branch runs after Proceed
        # and `2. Mesh setup` after it. Nothing here selects a row -- the
        # `takeRow` and `removeRows` below destroy the rows the selection
        # model is holding, and the tree then lands on whatever occupies that
        # position afterwards, which is the band header. So the selected row
        # is remembered by name here and put back at the end.
        selected = str(self._tree.currentIndex().data(BASE_LABEL_ROLE) or '')
        # DP-271. A stage nested into this band by `nestStepInBranch` leads it
        # and is not one of the rows the engine publishes, so it is lifted
        # out before the rebuild and put back at the head of it. Destroying
        # it here would take `3. Preparation` off the outline the first time
        # an engine published its tasks.
        leading = [item.takeRow(member.row())
                   for _step, member in reversed(self._bandStepItems(token))]
        item.removeRows(0, item.rowCount())
        for row in reversed(leading):
            item.appendRow(row)
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
        self._updateTreeExtent()
        # The current index is restored directly rather than through
        # `setCurrentStep`, which announces the step: the user is somewhere
        # already, and a refresh that re-announced a step would send Region B
        # back to the Preparation page from wherever they had got to. A row
        # that the rebuild did not republish -- switching engines replaces the
        # whole set -- has nothing to restore, and the tree is left as Qt
        # leaves it.
        if selected:
            restored = self._itemWithBaseLabel(selected)
            if restored is not None:
                self._tree.setCurrentIndex(restored.index())
                self._boldOnly(restored)
        return tuple(children)

    def branchNode(self, token):
        return getattr(self, '_branchNodes', {}).get(token)

    def nextOutlineRoute(self, token: str):
        """Say what Proceed opens from the top-level row carrying ``token``.

        DP-227. The order of this outline is declared in this class and
        nowhere else, so the question "what is the next visible step below
        this row" is answered here rather than copied into the wizard --
        which is what lets W1 delete a row without re-teaching the wizard.

        Answers ``('token', <branch token>)`` for a token-routed row,
        ``('step', <int Step>)`` for a numbered stage, and ``None`` when
        nothing follows. ``None`` is an answer, not a stall: the caller
        says so out loud instead of returning silently.

        GEO-07: there is no row to step over any more. Every row below this
        one is a stage of the job, so the next visible row is the answer.
        """
        node = self.branchNode(token)
        if node is None or node.parent() is not None:
            return None
        # DP-271. A stage nested into this row's band is painted directly
        # beneath it, so it is the next row down and the answer has to say
        # so. Only the nested stages are read here, not the whole band: a
        # band with no stage nested into it answers exactly as it did
        # before, with the root row below it.
        for step, item in self._bandStepItems(token):
            if self._tree.isRowHidden(item.row(), node.index()):
                continue
            return ('step', int(step))
        # Read the top-level rows off the model rather than binding its root
        # item to a name: the AF0 inventory reads any `name = ...Item()` in a
        # view module as a new widget that nobody has reviewed.
        for row in range(node.row() + 1, self._model.rowCount()):
            item = self._model.item(row)
            if item is None:
                continue
            if self._tree.isRowHidden(row, self._tree.rootIndex()):
                continue
            next_token = item.data(TOKEN_ROLE)
            if next_token:
                return ('token', str(next_token))
            step_data = item.data(Qt.ItemDataRole.UserRole)
            if step_data is not None:
                return ('step', int(step_data))
        return None

    def setStepRowCurrent(self, step) -> bool:
        """Highlight a numbered step's own row without announcing the step.

        DP-561. `setCurrentStep` also emits `currentStepChanged`, which is a
        navigation; the page is already moving when this is asked, so only
        the highlight is put where the page is. Answers whether the step has
        a row to stand on.
        """
        item = self._items.get(step)
        if item is None:
            return False
        self._tree.setCurrentIndex(item.index())
        self._boldOnly(item)
        return True

    def setBranchCurrent(self, token: str) -> bool:
        """Select and mark a token route without changing another shell region."""
        node = self.branchNode(token)
        if node is not None:
            self._tree.setCurrentIndex(node.index())
            self._setItemState(node, WorkflowRowState.CURRENT)
            self._boldOnly(node)
            return True
        for parent in getattr(self, '_branchNodes', {}).values():
            for row in range(parent.rowCount()):
                child = parent.child(row)
                if child.data(TOKEN_ROLE) == token:
                    self._tree.setCurrentIndex(child.index())
                    self._setItemState(child, WorkflowRowState.CURRENT)
                    self._boldOnly(child)
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
        item.setText(f'{prefix}{STATUS_SEPARATOR}{label}')
        meaning = _STATUS_MEANING.get(state, state.value)
        item.setToolTip(f'{label} — {meaning}')
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
        # DP-192. The mark is drawn in a reserved column rather than
        # at its own width, so the row asks for that column and not
        # for the width of the mark it happens to be carrying -- or a
        # row wearing the narrow skipped dash would ask for 7 px less
        # than it is given and the column would twitch as states
        # changed.
        _mark, separator, rest = text.partition(STATUS_SEPARATOR)
        width = (metrics.horizontalAdvance(text) if not separator
                 else status_cell_width(font)
                 + metrics.horizontalAdvance(STATUS_SEPARATOR + rest))
        # DP-215. The stylesheet takes room out of a row in both
        # directions, and an item carrying its own size hint is handed
        # back unchanged, so the ``min-height`` in the rule never gets a
        # say. Asking for the bare line box therefore left five pixels
        # of a twelve pixel word on screen. The floor is the one control
        # height, which is the sum the rule itself is commented with.
        item.setSizeHint(QSize(
            width + _ROW_CHROME_PX,
            max(metrics.height() + _ROW_CHROME_V_PX, CONTROL_HEIGHT)))

    def currentStep(self):
        return self._currentStep

    def setCurrentStep(self, step):
        self._steps.button(step).setChecked(True)
        item = self._items.get(step)
        if item is not None:
            self._tree.setCurrentIndex(item.index())
            self._setItemState(item, WorkflowRowState.CURRENT)
            self._boldOnly(item)
        self._stepChanged(step)

    def enableStep(self, step):
        self._steps.button(step).setEnabled(True)
        if step in self._items:
            self._items[step].setEnabled(True)
            self._setItemState(self._items[step], self._openRowState(step))
        self._updateBatchStepsEnabled()

    def _openRowState(self, step) -> WorkflowRowState:
        return (WorkflowRowState.COMPLETED
                if step in getattr(self, '_settledSteps', set())
                else WorkflowRowState.AVAILABLE)

    def setStepSettled(self, step, settled: bool) -> None:
        """Say whether a setup stage's record exists.

        DP-762. `1. Geometry` and `3. Preparation` are not engine tasks, so
        nothing painted them anything but ready: after a finished export the
        outline still read ``○ 1. Geometry`` and ``○ 3. Preparation`` above a
        column of ticked engine rows. The step manager now says when the
        record exists -- a geometry, a current preparation decision -- and
        the tick survives the ``enableStep`` every refresh makes. A locked
        row stays locked: a record does not open a row the workflow has not
        reached.
        """
        self._settledSteps = getattr(self, '_settledSteps', set())
        if settled:
            self._settledSteps.add(step)
        else:
            self._settledSteps.discard(step)
        item = self._items.get(step)
        if item is None or item.data(STATUS_ROLE) == WorkflowRowState.LOCKED.value:
            return
        self._setItemState(item, self._openRowState(step))

    def setBranchNodeSettled(self, token, settled: bool) -> None:
        """DP-762. The same for a token-routed setup row (`2. Mesh setup`)."""
        node = self.branchNode(token)
        if node is None:
            return
        self._setItemState(node, WorkflowRowState.COMPLETED if settled
                           else WorkflowRowState.AVAILABLE)

    def _boldOnly(self, target) -> None:
        """Put the outline's bold on ``target`` and take it off every other row.

        DP-762. Bold went to the legacy *working step*, which in the guided
        workflow stops at `3. Preparation`, so Preparation stayed bold while
        the user stood on Export. Bold now marks the row whose page is on
        screen, and nothing else.
        """
        stack = [self._model.invisibleRootItem()]
        while stack:
            parent = stack.pop()
            for row in range(parent.rowCount()):
                child = parent.child(row)
                if child.font().bold() != (child is target):
                    font = child.font()
                    font.setBold(child is target)
                    child.setFont(font)
                if child.rowCount():
                    stack.append(child)

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
        self._workingStep = step
        setBold(self._steps.button(step), True)
        # DP-762. The row is marked, not emboldened: bold follows the page
        # on screen (`_boldOnly`), which is not always the working step.
        if step in self._items:
            self._setItemState(self._items[step], WorkflowRowState.CURRENT)

    def setExternalMeshMode(self, enabled: bool):
        """Collapse authored generation navigation for an externally sourced mesh.

        The generation steps go, and since GEO-07 nothing is held back from
        the hiding: an opened mesh is inspected from the viewport toolbar and
        from the display panel the external-mesh page opens, neither of which
        is a row in this tree. The tree itself stays visible because the rows
        come back when the mode ends.
        """
        # `navigation` is the group box the tree lives in, so hiding it hid
        # the tree too. The step *buttons* inside it are already hidden; the
        # container stays.
        self._ui.navigation.setVisible(True)
        self._tree.setVisible(True)
        root = self._model.invisibleRootItem()
        for row in range(root.rowCount()):
            self._tree.setRowHidden(row, self._tree.rootIndex(), enabled)
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
