#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 45 L (P4, P5): the one home of slicing, under the top bar's Section.

The top bar's Section button used to raise a plane and open nothing; the
plane's controls lived in two rail popovers (*Cut plane*, *Section*) and, a
third time, on the Display Control page. This popover holds the one
`SectionPanel` the cut tool drives, with its two tabs:

* a press on Section raises a plane along the view (as before) and opens the
  popover docked under the button, inside the viewport's clear area;
* a second press, Esc or the close button folds it away; the planes stay
  (the panel's *Clear* takes them down);
* the pin floats it: dragged by its header it goes anywhere in the viewport
  and stays where it is dropped, across closing and opening; unpinned it docks
  back under the button.
"""
from __future__ import annotations

from typing import Optional

from shiboken6 import isValid
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout,
    QWidget)

from foammesh.view.main_window.tool_edges import (
    round_native_corners, set_pin_look)
from foammesh.view.theming.fonts import caps_font
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP_TIGHT, MARGIN_TIGHT)
from foammesh.view.theming.status_colors import (
    OVERLAY_KINDS, style_overlay_panel)

#: The popover's width: the panel's widest row reads whole in it at
#: 1280 x 800 (and it never grows past the clear area).
SECTION_POPOVER_WIDTH = 400
#: Pixels between the popover and the button it hangs from, and between the
#: popover and the clear area's edges.
ANCHOR_GAP = 4
EDGE_INSET = 6
#: The least height the popover is given when the clear area is short.
MIN_HEIGHT = 160


class SectionPopover(QFrame):
    """The section panel in a popover hung from the top bar's Section."""

    #: Folded away (by the button, Esc or the close button).
    closed = Signal()
    #: Pinned (floating) or unpinned (docked under the button).
    pinnedChanged = Signal(bool)

    def __init__(self, panel: QWidget, view: QWidget,
                 anchor: Optional[QWidget] = None, clearRect=None):
        """*panel*: the `SectionPanel`; *view*: the viewport the popover
        floats over; *anchor*: the button it hangs from; *clearRect*: a
        callable giving the view's clear area (the whole view without)."""
        super().__init__(view)
        self._view = view
        self._panel = panel
        self._anchor = anchor
        self._clearRect = clearRect
        #: Where a floating popover was dropped, in the view's pixels.
        self._floatPos: Optional[QPoint] = None
        self._dragFrom: Optional[QPoint] = None

        self.setObjectName('sectionPopover')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(True)
        title = self.tr('Section')
        self.setAccessibleName(title)
        style_overlay_panel(self, 'popover')
        # A native window over the GL view: cut to the sheet's radius
        # (Plan 45 P16).
        round_native_corners(self, OVERLAY_KINDS['popover'][2])

        column = QVBoxLayout(self)
        column.setContentsMargins(FORM_MARGIN, MARGIN_TIGHT, FORM_MARGIN,
                                  FORM_MARGIN)
        column.setSpacing(GAP_TIGHT)

        self._head = QWidget()
        self._head.setObjectName('sectionPopoverHead')
        self._head.setCursor(Qt.CursorShape.OpenHandCursor)
        self._head.setToolTip(self.tr(
            'Drag to float the section panel anywhere over the view'))
        head = QHBoxLayout(self._head)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(GAP_TIGHT)
        self._title = QLabel(title)
        self._title.setObjectName('popoverTitle')
        self._title.setFont(caps_font(10.5))
        self._pin = QToolButton()
        self._pin.setObjectName('sectionPopoverPin')
        self._pin.setCheckable(True)
        self._pin.setAutoRaise(True)
        self._pin.setAccessibleName(self.tr('Float the section panel'))
        set_pin_look(self._pin, self.tr('section panel'))
        self._pin.toggled.connect(
            lambda _on: set_pin_look(self._pin, self.tr('section panel')))
        self._pin.toggled.connect(self._pinToggled)
        self._close = QToolButton()
        self._close.setObjectName('sectionPopoverClose')
        self._close.setText('✕')
        self._close.setAutoRaise(True)
        self._close.setToolTip(self.tr(
            'Close with {0}; the planes stay').format('Esc'))
        self._close.setAccessibleName(self.tr('Close the section panel'))
        self._close.clicked.connect(self.dismiss)
        head.addWidget(self._title, 1)
        head.addWidget(self._pin)
        head.addWidget(self._close)
        self._head.installEventFilter(self)
        self._title.installEventFilter(self)
        column.addWidget(self._head)

        self._scroll = QScrollArea()
        self._scroll.setObjectName('sectionPopoverScroll')
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(panel)
        panel.setVisible(True)
        column.addWidget(self._scroll, 1)

        view.installEventFilter(self)
        panel.installEventFilter(self)
        self.setVisible(False)

    # -- state ------------------------------------------------------------- #

    def panel(self) -> QWidget:
        return self._panel

    def anchor(self) -> Optional[QWidget]:
        return self._anchor

    def setAnchor(self, anchor: Optional[QWidget]) -> None:
        self._anchor = anchor
        self.place()

    def isOpen(self) -> bool:
        return self.isVisible()

    def isPinned(self) -> bool:
        return self._pin.isChecked()

    def setPinned(self, pinned: bool) -> None:
        self._pin.setChecked(bool(pinned))

    def isFloating(self) -> bool:
        return self.isPinned()

    def floatPosition(self) -> Optional[QPoint]:
        return QPoint(self._floatPos) if self._floatPos is not None else None

    # -- open / close ------------------------------------------------------- #

    def open(self, tab: str = 'cut') -> None:
        showTab = getattr(self._panel, 'showTab', None)
        if callable(showTab):
            showTab(tab)
        self.setVisible(True)
        self.place()
        self.raise_()
        self.setFocus()

    def dismiss(self) -> None:
        if not self.isVisible():
            return
        self.setVisible(False)
        self.closed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- placing ------------------------------------------------------------ #

    def clearRect(self) -> QRect:
        """The viewport's clear area, in the view's pixels."""
        rect = None
        if callable(self._clearRect):
            try:
                rect = self._clearRect()
            except (RuntimeError, AttributeError):
                rect = None
        if rect is None or not rect.isValid() or rect.isEmpty():
            rect = QRect(0, 0, self._view.width(), self._view.height())
        return rect

    def wantedSize(self) -> QSize:
        clear = self.clearRect()
        margins = self.layout().contentsMargins()
        chrome = (margins.left() + margins.right() + 2 * self.frameWidth()
                  + self._scroll.verticalScrollBar().sizeHint().width())
        width = max(SECTION_POPOVER_WIDTH,
                    self._panel.sizeHint().width() + chrome)
        width = min(width, max(160, clear.width() - 2 * EDGE_INSET))
        height = (self._panel.sizeHint().height() + margins.top()
                  + margins.bottom() + self._head.sizeHint().height()
                  + self.layout().spacing() + 2 * self.frameWidth())
        return QSize(width, height)

    def dockedPosition(self, size: QSize) -> QPoint:
        """Under the anchor, kept inside the clear area."""
        clear = self.clearRect()
        anchor = self._anchor
        # A folded top bar hides the button with it but keeps its place;
        # only the overflow hides the button itself.
        if (anchor is not None and isValid(anchor) and not anchor.isHidden()
                and anchor.width() > 0):
            corner = self._view.mapFromGlobal(
                anchor.mapToGlobal(QPoint(0, anchor.height())))
            x = corner.x()
            y = corner.y() + ANCHOR_GAP
        else:
            # The button is folded into the overflow menu: the top right
            # of the clear area, clear of the rail.
            x = clear.right() - size.width() - 6 * EDGE_INSET
            y = clear.top() + EDGE_INSET
        return self._clamp(QPoint(x, y), size)

    def _clamp(self, point: QPoint, size: QSize) -> QPoint:
        clear = self.clearRect()
        low_x = clear.left() + EDGE_INSET
        high_x = max(low_x, clear.right() - size.width() - EDGE_INSET + 1)
        low_y = clear.top() + EDGE_INSET
        high_y = max(low_y, clear.bottom() - min(size.height(), MIN_HEIGHT)
                     - EDGE_INSET + 1)
        return QPoint(min(max(point.x(), low_x), high_x),
                      min(max(point.y(), low_y), high_y))

    def place(self) -> None:
        if not self.isVisible():
            return
        size = self.wantedSize()
        if self.isPinned() and self._floatPos is not None:
            point = self._clamp(self._floatPos, size)
        else:
            point = self.dockedPosition(size)
        clear = self.clearRect()
        room = clear.bottom() - point.y() - EDGE_INSET + 1
        height = max(min(size.height(), room), min(MIN_HEIGHT, size.height()))
        self.setGeometry(QRect(point, QSize(size.width(), height)))

    # -- floating ----------------------------------------------------------- #

    def _pinToggled(self, on: bool) -> None:
        if on:
            if self._floatPos is None:
                self._floatPos = QPoint(self.pos())
        else:
            # Unpinned, it docks back under the button.
            self._floatPos = None
        self.place()
        self.pinnedChanged.emit(bool(on))

    def floatTo(self, point: QPoint) -> None:
        """Float the popover with its top left at *point* (view pixels)."""
        self._floatPos = QPoint(point)
        if not self.isPinned():
            self._pin.setChecked(True)      # places it
        else:
            self.place()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self._view:
            if kind in (QEvent.Type.Resize, QEvent.Type.Show):
                self.place()
        elif watched is self._panel:
            if kind == QEvent.Type.LayoutRequest:
                self.place()
        elif watched in (self._head, self._title):
            if (kind == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton):
                self._dragFrom = (event.globalPosition().toPoint()
                                  - self.mapToGlobal(QPoint(0, 0)))
                return True
            if kind == QEvent.Type.MouseMove and self._dragFrom is not None:
                top_left = self._view.mapFromGlobal(
                    event.globalPosition().toPoint() - self._dragFrom)
                self.floatTo(top_left)
                return True
            if kind == QEvent.Type.MouseButtonRelease:
                self._dragFrom = None
                return True
        return super().eventFilter(watched, event)
