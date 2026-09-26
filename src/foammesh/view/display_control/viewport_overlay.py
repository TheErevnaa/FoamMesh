#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Controls pinned to the picture rather than parked in another region.

Display Control is a docked tree somewhere else on screen. While you are
looking at a mesh and want one zone gone, that is a trip away from the thing
you are looking at -- and it is a trip you make constantly.

This overlay is a *view* of the same actor state Display Control edits, never a
second source of truth: both drive the same ``ActorInfo`` objects, and anything
hidden here shows as hidden there. It also carries the legend, so a screenshot
explains itself, and the section controls, so the plane can be aimed without
opening a panel.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

from foammesh.view.theming.icons import load_themed_icon
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT, apply_bar_metrics

from .region_picker import ALL
from .section_panel import SectionPanel


SWATCH = 11
ROW_ICON = QSize(14, 14)
#: DP-712. The parts list may take this share of the viewport's height, and
#: scrolls past it. A fixed 180 px showed six rows of a 40-part mesh on a
#: tall window and still covered half of a short one.
PARTS_HEIGHT_SHARE = 0.4
PARTS_MIN_HEIGHT = 96


def _swatch(color: QColor) -> QPixmap:
    pixmap = QPixmap(SWATCH, SWATCH)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(QColor(0, 0, 0, 90))
    painter.drawRoundedRect(0, 0, SWATCH - 1, SWATCH - 1, 2, 2)
    painter.end()
    return pixmap


class PartRow(QWidget):
    """One part: its colour, its name, an eye, and a solo button."""

    visibilityToggled = Signal(str, bool)
    soloRequested = Signal(str)
    #: DP-712. A click on the row: ``(key, additive)``, additive with Ctrl.
    selectRequested = Signal(str, bool)

    def __init__(self, key: str, name: str, color: QColor, parent=None):
        super().__init__(parent)
        self._key = key
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self.tr('Click to select {0}. Ctrl+click adds it '
                                'to the selection.').format(name))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(MARGIN_TIGHT, 0, MARGIN_TIGHT, 0)
        layout.setSpacing(GAP_TIGHT)

        self._swatch = QLabel()
        self._swatch.setPixmap(_swatch(color))
        self._name = QLabel(name)
        self._name.setToolTip(name)

        # The bulb pair already in the icon set is this app's existing idiom
        # for "this thing is drawn / this thing is not".
        self._eye = QToolButton()
        self._eye.setCheckable(True)
        self._eye.setChecked(True)
        self._eye.setIconSize(ROW_ICON)
        self._eye.setToolTip(self.tr('Show or hide {0}').format(name))
        self._eye.setAccessibleName(self._eye.toolTip())
        self._eye.setAutoRaise(True)

        self._solo = QToolButton()
        self._solo.setIcon(load_themed_icon(':/graphicsIcons/isolate.svg'))
        self._solo.setIconSize(ROW_ICON)
        self._solo.setAutoRaise(True)
        self._solo.setToolTip(self.tr('Show only {0}').format(name))
        self._solo.setAccessibleName(self._solo.toolTip())

        layout.addWidget(self._swatch)
        layout.addWidget(self._name, 1)
        layout.addWidget(self._eye)
        layout.addWidget(self._solo)

        self._eye.toggled.connect(
            lambda checked: self.visibilityToggled.emit(self._key, checked))
        self._solo.clicked.connect(lambda: self.soloRequested.emit(self._key))

    def key(self):
        return self._key

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            additive = bool(event.modifiers()
                            & Qt.KeyboardModifier.ControlModifier)
            self.selectRequested.emit(self._key, additive)
            event.accept()
            return
        super().mousePressEvent(event)

    def isSelected(self) -> bool:
        return self._selected

    def setSelected(self, selected: bool):
        """Mark the row as selected, the way the tree marks its rows."""
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        font = QFont(self._name.font())
        font.setBold(selected)
        self._name.setFont(font)
        self.setProperty('selected', selected)
        self.update()

    def paintEvent(self, event):
        # Painted here rather than through the palette: a theme's style sheet
        # overrides a palette's background, and the row must read as selected
        # under any theme.
        if self._selected:
            painter = QPainter(self)
            highlight = QColor(self.palette().color(QPalette.ColorRole.Highlight))
            highlight.setAlpha(90)
            painter.fillRect(self.rect(), highlight)
            highlight.setAlpha(255)
            painter.fillRect(0, 0, 3, self.height(), highlight)
            painter.end()
        super().paintEvent(event)

    def setColor(self, color: QColor):
        self._swatch.setPixmap(_swatch(color))

    def setVisibleState(self, visible: bool):
        was = self._eye.blockSignals(True)
        self._eye.setChecked(visible)
        self._eye.setIcon(load_themed_icon(
            ':/graphicsIcons/bulb-on.svg' if visible
            else ':/graphicsIcons/bulb-off.svg'))
        self._eye.blockSignals(was)


class RegionPicker(QWidget):
    """DP-711 (viewport audit 0925 F1). Which regions and volume parts to show.

    A checkable list: All, then each region (for a multi-region case) with
    its cell zones -- or, for a mesh whose regions carry no zones, the parts
    named after each region point -- indented beneath it. Hidden when there
    is nothing to choose between.
    """

    filterChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('regionPicker')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._title = QLabel(self.tr('Regions'))
        self._list = QListWidget()
        self._list.setObjectName('regionPickerList')
        self._list.setAccessibleName(self.tr('Regions to show'))
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self._title)
        layout.addWidget(self._list)
        self._list.itemChanged.connect(self._itemChanged)
        self.setVisible(False)

    def setEntries(self, entries, checked=None):
        """``entries`` is ``[(key, label, depth)]``; ``checked`` the keys on."""
        entries = list(entries)
        checked = ({key for key, _label, _depth in entries}
                   if checked is None else set(checked))
        was = self._list.blockSignals(True)
        self._list.clear()
        if entries:
            self._add(ALL, self.tr('All'), 0,
                      all(key in checked for key, _l, _d in entries))
            for key, label, depth in entries:
                self._add(key, label, depth + 1, key in checked)
        self._list.blockSignals(was)
        rows = self._list.count()
        if rows:
            height = self._list.sizeHintForRow(0) * min(rows, 6) + 4
            self._list.setFixedHeight(height)
        self.setVisible(bool(entries))

    def _add(self, key, label, depth, on):
        item = QListWidgetItem(('    ' * depth) + label)
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if on
                           else Qt.CheckState.Unchecked)
        item.setToolTip(self.tr('Show or hide {0}').format(label))
        self._list.addItem(item)

    def keys(self) -> list:
        return [self._list.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(1, self._list.count())]

    def checkedKeys(self) -> list:
        return [item.data(Qt.ItemDataRole.UserRole)
                for item in (self._list.item(row)
                             for row in range(1, self._list.count()))
                if item.checkState() == Qt.CheckState.Checked]

    def setChecked(self, key, on: bool):
        """Tick or untick one row as a click would."""
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                item.setCheckState(Qt.CheckState.Checked if on
                                   else Qt.CheckState.Unchecked)
                return

    def _itemChanged(self, item):
        was = self._list.blockSignals(True)
        rows = [self._list.item(row) for row in range(self._list.count())]
        state = item.checkState()
        if item.data(Qt.ItemDataRole.UserRole) == ALL:
            for other in rows[1:]:
                other.setCheckState(state)
        else:
            everything = all(other.checkState() == Qt.CheckState.Checked
                             for other in rows[1:])
            rows[0].setCheckState(Qt.CheckState.Checked if everything
                                  else Qt.CheckState.Unchecked)
        self._list.blockSignals(was)
        self.filterChanged.emit(self.checkedKeys())


class ViewportOverlay(QFrame):
    """A collapsible panel floating over the viewport."""

    visibilityToggled = Signal(str, bool)
    soloRequested = Signal(str)
    showAllRequested = Signal()
    explodeChanged = Signal(float)
    #: DP-711. The Region picker's ticked keys (see `region_picker`).
    regionFilterChanged = Signal(list)
    #: DP-712. A part row was clicked: ``(key, additive)``.
    partSelectionRequested = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('viewportOverlay')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._rows: dict[str, PartRow] = {}
        self._groupLabels: list[QLabel] = []
        self._expanded = False

        layout = QVBoxLayout(self)
        # DP-191. The overlay had two more pixels under its content than
        # over it, so what it painted sat above its own centre.
        apply_bar_metrics(self, layout)
        layout.setSpacing(GAP_TIGHT)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._toggle = QToolButton()
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setAutoRaise(True)
        self._toggle.setToolTip(self.tr('Show the parts and section controls'))
        self._toggle.setAccessibleName(self._toggle.toolTip())
        self._chip = QLabel()
        self._chip.setObjectName('overlayChip')
        # Wide enough for "12 of 12 parts shown" without eliding; a chip that
        # reads "12 of" tells the user nothing about what is hidden.
        self._chip.setMinimumWidth(150)
        self._showAll = QPushButton()
        self._showAll.setIcon(load_themed_icon(':/graphicsIcons/showAll.svg'))
        self._showAll.setIconSize(ROW_ICON)
        self._showAll.setFlat(True)
        self._showAll.setToolTip(self.tr('Make every part visible again'))
        self._showAll.setAccessibleName(self._showAll.toolTip())
        header.addWidget(self._toggle)
        header.addWidget(self._chip, 1)
        header.addWidget(self._showAll)
        layout.addLayout(header)

        # DP-95/DP-96. What the viewport is showing, and how big it is.
        # It sits above the run line because it answers the first question a
        # finished stage raises -- "what did that make" -- and because the
        # run line is often empty while this one is not.
        self._mesh = QLabel()
        self._mesh.setObjectName('overlayMesh')
        self._mesh.setWordWrap(True)
        self._mesh.setVisible(False)
        layout.addWidget(self._mesh)

        # F-37. Which run produced what is on screen, and how that run ended.
        # Without it a refused candidate and an accepted mesh are the same
        # picture, and the user is asked to judge cells with no way to tell
        # which run they belong to.
        self._result = QLabel()
        self._result.setObjectName('overlayResult')
        self._result.setVisible(False)
        layout.addWidget(self._result)

        self._body = QWidget()
        bodyLayout = QVBoxLayout(self._body)
        bodyLayout.setContentsMargins(0, 0, 0, 0)
        bodyLayout.setSpacing(GAP_TIGHT)

        self._regionPicker = RegionPicker()
        self._regionPicker.filterChanged.connect(self.regionFilterChanged)
        bodyLayout.addWidget(self._regionPicker)

        self._partsHost = QWidget()
        self._partsLayout = QVBoxLayout(self._partsHost)
        self._partsLayout.setContentsMargins(0, 0, 0, 0)
        self._partsLayout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName('overlayPartsScroll')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._partsHost)
        self._partsScroll = scroll
        bodyLayout.addWidget(scroll)
        # DP-712. The cap follows the viewport the overlay floats over.
        self._capPartsHeight()
        if parent is not None:
            parent.installEventFilter(self)

        # WP7.5. Cheap once zones have their own colours, and the fastest way
        # to see what is actually in a conjugate case.
        explodeRow = QWidget()
        explodeLayout = QHBoxLayout(explodeRow)
        explodeLayout.setContentsMargins(0, 0, 0, 0)
        explodeLayout.setSpacing(GAP_TIGHT)
        explodeLabel = QLabel(self.tr('Explode'))
        self._explode = QSlider(Qt.Orientation.Horizontal)
        self._explode.setRange(0, 100)
        self._explode.setValue(0)
        self._explode.setToolTip(
            self.tr('Push the parts apart along the vector from the centre'))
        self._explode.setAccessibleName(self._explode.toolTip())
        explodeLayout.addWidget(explodeLabel)
        explodeLayout.addWidget(self._explode, 1)
        bodyLayout.addWidget(explodeRow)
        self._explode.valueChanged.connect(
            lambda value: self.explodeChanged.emit(value / 100.0))

        self._sectionPanel = SectionPanel()
        self._sectionToggle = QToolButton()
        self._sectionToggle.setIcon(
            load_themed_icon(':/graphicsIcons/section.svg'))
        self._sectionToggle.setIconSize(ROW_ICON)
        self._sectionToggle.setText(self.tr('Section'))
        self._sectionToggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._sectionToggle.setAutoRaise(True)
        self._sectionToggle.setCheckable(True)
        self._sectionToggle.setToolTip(
            self.tr('Aim the cut plane without opening a panel'))
        bodyLayout.addWidget(self._sectionToggle)
        bodyLayout.addWidget(self._sectionPanel)
        self._sectionPanel.setVisible(False)

        layout.addWidget(self._body)
        self._body.setVisible(False)

        self._toggle.clicked.connect(self.toggleExpanded)
        self._showAll.clicked.connect(self.showAllRequested)
        self._sectionToggle.toggled.connect(self._sectionPanel.setVisible)
        self._sectionToggle.toggled.connect(lambda _checked: self.adjustSize())

        self.setChip(0, 0)

    # -- state ------------------------------------------------------------- #

    def sectionPanel(self) -> SectionPanel:
        return self._sectionPanel

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self._capPartsHeight()
        return super().eventFilter(watched, event)

    def partsHeightCap(self) -> int:
        return self._partsScroll.maximumHeight()

    def _capPartsHeight(self):
        """DP-712. At most 40% of the viewport, and scroll past it."""
        parent = self.parentWidget()
        height = parent.height() if parent is not None else 450
        cap = max(PARTS_MIN_HEIGHT, int(height * PARTS_HEIGHT_SHARE))
        if cap != self._partsScroll.maximumHeight():
            self._partsScroll.setMaximumHeight(cap)
            if self._expanded:
                self.adjustSize()

    def regionPicker(self) -> RegionPicker:
        return self._regionPicker

    def setRegionEntries(self, entries, checked=None):
        """Offer these regions and parts; hidden when there is no choice."""
        self._regionPicker.setEntries(entries, checked)
        self.adjustSize()

    def isExpanded(self) -> bool:
        return self._expanded

    def setExpanded(self, expanded: bool):
        self._expanded = bool(expanded)
        self._body.setVisible(self._expanded)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded
            else Qt.ArrowType.RightArrow)
        self.adjustSize()

    def toggleExpanded(self):
        self.setExpanded(not self._expanded)

    def setMesh(self, text: str):
        """Say what mesh is on screen and how big it is, or hide the line.

        DP-96. The toolbar's cell count is the only other always-visible
        statement of size, and it says how many cells and nothing else: not
        which stage made them, not whether this is a surface or a volume,
        not how big the thing is in metres. A user reading `36,533 cells`
        cannot tell a 0.4 m duct from a 0.4 mm one.
        """
        self._mesh.setText(text or '')
        self._mesh.setToolTip(text or '')
        self._mesh.setVisible(bool(text))
        self.adjustSize()

    def meshText(self) -> str:
        return self._mesh.text()

    def setResult(self, text: str):
        """Name the run whose mesh is on screen, or hide the line (F-37)."""
        self._result.setText(text or '')
        self._result.setToolTip(text or '')
        self._result.setVisible(bool(text))
        self.adjustSize()

    def resultText(self) -> str:
        return self._result.text()

    def setChip(self, shown: int, total: int, noun: str = '',
                detail: str = ''):
        """The "3 of 11 mesh parts shown" chip.

        A user who forgets they isolated something reads a partial mesh as a
        broken one, so isolation is never allowed to be invisible.

        F-44. ``noun`` says *what* is being counted. It used to be every prop
        in the scene, so one duct read "14 parts" -- the surfaces it was
        meshed from, its patches, its internal volume and its zones, added up
        because the renderer holds them all.
        """
        noun = noun or self.tr('parts')
        if not total:
            self._chip.setText(self.tr('Nothing loaded'))
            self._showAll.setEnabled(False)
            self._chip.setProperty('partial', False)
        else:
            self._chip.setText(
                self.tr('{0} of {1} {2} shown').format(shown, total, noun))
            self._showAll.setEnabled(shown < total)
            self._chip.setProperty('partial', shown < total)
        # CP-09 item 6. The count says how many; the detail says which, by
        # name, split into boundary patches and cell zones and grouped by
        # region. A count on its own cannot answer "is my inlet still there".
        self._chipDetail = str(detail or '')
        self._chip.setToolTip(self._chipDetail)
        self._chip.style().unpolish(self._chip)
        self._chip.style().polish(self._chip)
        self.adjustSize()

    def setParts(self, parts, groups=None):
        """Rebuild the rows. ``parts`` is (key, name, QColor, visible).

        DP-712. ``groups`` maps a key to the heading it files under -- its
        region, and within a region whether it is a boundary or a zone. The
        rows are listed heading by heading, in the order the headings first
        appear; with one heading or none, no heading is drawn.
        """
        for row in self._rows.values():
            self._partsLayout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for label in self._groupLabels:
            self._partsLayout.removeWidget(label)
            label.deleteLater()
        self._groupLabels = []
        # And the gaps left above the headings.
        while self._partsLayout.count():
            self._partsLayout.takeAt(0)

        parts = list(parts)
        groups = dict(groups or {})
        order: dict[str, list] = {}
        for part in parts:
            order.setdefault(groups.get(part[0], ''), []).append(part)
        headed = len(order) > 1
        for heading, members in order.items():
            if headed:
                label = QLabel(heading or self.tr('Other'), self._partsHost)
                label.setObjectName('overlayPartGroup')
                font = QFont(label.font())
                font.setBold(True)
                label.setFont(font)
                # The heading's own step in from the rows and down from the
                # group above, without writing padding into the bar.
                label.setIndent(MARGIN_TIGHT)
                self._partsLayout.addSpacing(GAP_TIGHT)
                self._partsLayout.addWidget(label)
                self._groupLabels.append(label)
            for key, name, color, visible in members:
                row = PartRow(key, name, color, self._partsHost)
                row.setVisibleState(visible)
                row.visibilityToggled.connect(self.visibilityToggled)
                row.soloRequested.connect(self.soloRequested)
                row.selectRequested.connect(self.partSelectionRequested)
                self._partsLayout.addWidget(row)
                self._rows[key] = row

        shown = sum(1 for _key, _name, _color, visible in parts if visible)
        # The caller that knows the mesh re-sets the chip with the mesh's own
        # nouns and detail straight after (`MainWindow.rebuildOverlayParts`);
        # this keeps a scene with no mesh in it counted.
        self.setChip(shown, len(parts))

    def chipDetail(self) -> str:
        """What the chip says the scene is made of, by name."""
        return getattr(self, '_chipDetail', '')

    def groupHeadings(self) -> list[str]:
        return [label.text() for label in self._groupLabels]

    def partKeys(self) -> list[str]:
        return list(self._rows)

    def setSelectedParts(self, keys):
        """DP-712. Mark the selected parts' rows, from any selection source."""
        keys = set(keys)
        for key, row in self._rows.items():
            row.setSelected(key in keys)

    def selectedParts(self) -> list[str]:
        return [key for key, row in self._rows.items() if row.isSelected()]

    def updateParts(self, states):
        """Refresh colour and eye state without rebuilding the rows."""
        for key, color, visible in states:
            row = self._rows.get(key)
            if row is None:
                continue
            row.setColor(color)
            row.setVisibleState(visible)
