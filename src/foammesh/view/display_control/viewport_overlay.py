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

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QToolButton, QVBoxLayout, QWidget)

from foammesh.view.theming.icons import load_themed_icon

from .section_panel import SectionPanel


SWATCH = 11
ROW_ICON = QSize(14, 14)


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

    def __init__(self, key: str, name: str, color: QColor, parent=None):
        super().__init__(parent)
        self._key = key

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(4)

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

    def setColor(self, color: QColor):
        self._swatch.setPixmap(_swatch(color))

    def setVisibleState(self, visible: bool):
        was = self._eye.blockSignals(True)
        self._eye.setChecked(visible)
        self._eye.setIcon(load_themed_icon(
            ':/graphicsIcons/bulb-on.svg' if visible
            else ':/graphicsIcons/bulb-off.svg'))
        self._eye.blockSignals(was)


class ViewportOverlay(QFrame):
    """A collapsible panel floating over the viewport."""

    visibilityToggled = Signal(str, bool)
    soloRequested = Signal(str)
    showAllRequested = Signal()
    explodeChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('viewportOverlay')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._rows: dict[str, PartRow] = {}
        self._expanded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)

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
        bodyLayout.setSpacing(4)

        self._partsHost = QWidget()
        self._partsLayout = QVBoxLayout(self._partsHost)
        self._partsLayout.setContentsMargins(0, 0, 0, 0)
        self._partsLayout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(180)
        scroll.setWidget(self._partsHost)
        bodyLayout.addWidget(scroll)

        # WP7.5. Cheap once zones have their own colours, and the fastest way
        # to see what is actually in a conjugate case.
        explodeRow = QWidget()
        explodeLayout = QHBoxLayout(explodeRow)
        explodeLayout.setContentsMargins(0, 0, 0, 0)
        explodeLayout.setSpacing(4)
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

    def setParts(self, parts):
        """Rebuild the rows. ``parts`` is (key, name, QColor, visible)."""
        for row in self._rows.values():
            self._partsLayout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        for key, name, color, visible in parts:
            row = PartRow(key, name, color, self._partsHost)
            row.setVisibleState(visible)
            row.visibilityToggled.connect(self.visibilityToggled)
            row.soloRequested.connect(self.soloRequested)
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

    def updateParts(self, states):
        """Refresh colour and eye state without rebuilding the rows."""
        for key, color, visible in states:
            row = self._rows.get(key)
            if row is None:
                continue
            row.setColor(color)
            row.setVisibleState(visible)
