#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 43 LY7: the Viewport notice strip (top centre).

Case-level notices drawn over the view, not in the Parts card: the preview
notice with its Try again, Build anyway and Load full volume buttons, and the
progress strip with its Cancel. The strip owns no behaviour. Each notice is
the widget it always was, adopted here, so the signals, object names and the
text a test reads are unchanged; the strip only decides where they stand and
how much room they take.

* It is one item of the overlay layout's TOP slot. With nothing to say it
  asks for no height, so it never covers the model.
* The progress strip runs the full width (as it always did); a notice alone is
  centred and only as wide as its text, so the model beside it keeps its mouse.
* It is a sibling of the tool edges, not a part of them: it never folds, and it
  never moves the camera.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from foammesh.view.display_control.overlay_layout import Rect, Slot
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT
from foammesh.view.theming.status_colors import style_floating_panel

OBJECT_NAME = 'noticeStrip'
#: The widest a lone notice grows; longer text wraps.
NOTICE_MAX_WIDTH = 560


class NoticeStrip(QFrame):
    """The container; ``adopt`` puts a notice widget in it."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName(OBJECT_NAME)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAccessibleName(self.tr('Viewport notices'))
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._column = column
        self._full: list[QWidget] = []
        self._cards: list[QWidget] = []
        self._owner = _Watcher(self)
        self._view = None
        self._clearance = lambda: 0

    # -- the occupants ------------------------------------------------------ #

    def adoptStrip(self, widget: QWidget) -> None:
        """A full-width strip (the progress strip): put in, as it was."""
        self._put(widget, self._full)
        self._column.addWidget(widget)

    def adoptNotice(self, widget: QWidget) -> None:
        """A notice card: framed, centred, only as wide as it needs."""
        self._put(widget, self._cards)
        style_floating_panel(widget, radius=6)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = widget.layout()
        if layout is not None:
            layout.setContentsMargins(MARGIN_TIGHT * 2, MARGIN_TIGHT,
                                      MARGIN_TIGHT * 2, MARGIN_TIGHT)
            layout.setSpacing(GAP_TIGHT)
        self._column.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)

    def _put(self, widget: QWidget, bucket: list) -> None:
        if widget in bucket:
            return
        widget.setParent(self)
        bucket.append(widget)
        widget.installEventFilter(self._owner)

    def occupants(self) -> list:
        return list(self._full) + list(self._cards)

    # -- what the strip asks for ------------------------------------------- #

    def _shown(self, widgets: list) -> list:
        return [w for w in widgets if not w.isHidden()]

    def hasContent(self) -> bool:
        return bool(self._shown(self._full) or self._shown(self._cards))

    def wanted(self) -> tuple:
        """(width, height) for the layout; (0, 0) when nothing is shown."""
        full = self._shown(self._full)
        cards = self._shown(self._cards)
        height = sum(w.sizeHint().height() for w in full)
        width = 0
        for card in cards:
            hint = card.sizeHint()
            height += self._cardHeight(card)
            width = max(width, min(hint.width(), NOTICE_MAX_WIDTH))
        return (0 if full else width), height

    def _cardHeight(self, card: QWidget) -> int:
        width = min(card.sizeHint().width(), NOTICE_MAX_WIDTH)
        layout = card.layout()
        if layout is not None and layout.hasHeightForWidth():
            return layout.totalHeightForWidth(width)
        return card.sizeHint().height()

    def setClearance(self, clearance) -> None:
        """*clearance* answers how far down the top bar reaches while it is
        out; the notices stand below it, so a pinned top bar never covers
        the button a notice offers."""
        self._clearance = clearance
        self.refresh()

    def place(self, rect: Rect, view_width: int) -> None:
        """The layout gave the TOP slot *rect* (full width); a lone notice
        is centred in it and only as wide as it asked."""
        width, height = self.wanted()
        top = rect.y + max(0, int(self._clearance()))
        if height <= 0:
            self.setGeometry(rect.x, top, 0, 0)
            return
        if self._shown(self._full):
            self.setGeometry(rect.x, top, rect.w, height)
        else:
            width = max(1, min(width, rect.w))
            self.setGeometry(rect.x + (rect.w - width) // 2, top,
                             width, height)
        for card in self._shown(self._cards):
            card.setMaximumWidth(NOTICE_MAX_WIDTH)
        self.raise_()

    def install(self, view) -> None:
        """Take the TOP slot of *view*'s overlay layout."""
        layout = view.overlayLayout()
        layout.register(
            'notice-strip', Slot.TOP, lambda: (0, self.wanted()[1]),
            lambda rect: self.place(rect, view.width()))
        self._view = view
        view.arrangeOverlays()

    def refresh(self) -> None:
        if self._view is not None:
            self._view.arrangeOverlays()


class _Watcher(QObject):
    """Re-arranges the overlays when an occupant is shown, hidden or resized."""

    def __init__(self, strip: NoticeStrip):
        super().__init__(strip)
        self._strip = strip

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide,
                            QEvent.Type.LayoutRequest,
                            QEvent.Type.ShowToParent,
                            QEvent.Type.HideToParent):
            self._strip.refresh()
        return False
