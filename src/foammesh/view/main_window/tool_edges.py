#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 43 LY6 and LY7: tool edges that fold away, and the Parts chip.

The top bar (the viewport toolbar), the bottom bar and the side rail are
*edges* of the viewport. Each one folds to a nub at its edge and comes back
when the pointer rests on the nub, when the nub is pressed, or when the
keyboard moves into it. A pin button keeps it out; Ctrl+H pins or unpins all
three.

The rule that shapes every line below (Plan 43 section 1, rule 1): **a tool
edge never moves the model.** An edge is a child widget drawn over the
viewport. It is never put in a layout that shares the viewport's rows or
columns, so revealing, concealing, pinning and unpinning change no size the
render window sees and leave the camera alone. The viewport's clear area is
decided by the settings sheet and the console only (LY3 to LY5), and an edge
is left out of it.

An edge that is folded is *hidden* with its pin (Plan 44 MF10): a widget
parked past the viewport's border was still drawn where the border does not
clip it (a "Pin" in the footer, slivers along the border). Every control stays
one reveal away through the nub, and none of the edge's buttons carries a
shortcut of its own: the menu actions do.

No application-wide event filter is installed (DP-1447, DP-1430). The edge
filters its own nub, its own frame and the viewport it sits on; a focus move
into a folded edge arrives through the application's ``focusChanged`` signal.
"""
from __future__ import annotations

from typing import Callable, Optional

from shiboken6 import isValid
from PySide6.QtCore import (
    QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (
    QBitmap, QColor, QCursor, QFont, QPainter, QPainterPath, QPalette, QPen, QRegion,
    QResizeEvent)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QSlider, QToolButton, QWidget)

from foammesh.view.theming import motion
from foammesh.view.theming.icons import theme_colour
from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT
from foammesh.view.theming import status_colors
from foammesh.view.theming.status_colors import style_floating_panel

TOP = 'top'
BOTTOM = 'bottom'
RIGHT = 'right'

#: The nub's hit area across the edge (MF13: it stays 12 px) and its length
#: along the edge. What is *drawn* is ``NUB_DRAW`` thick and ``NUB_LENGTH`` long:
#: 60 x 5 on the top and bottom, 5 x 60 on the rail, radius 3, no glyph.
NUB_THICKNESS = 12
NUB_DRAW = 5
NUB_LENGTH = 60
#: How far a revealed edge slides in from its border (mockup M:364-384).
SLIDE_PX = {'top': 48, 'bottom': 48, 'right': 52}
#: The accent of a nub, pre-blended over the viewport (Plan 44 section 3).
NUB_ALPHA = 0.6
#: Distance between an edge and the viewport border it hangs from.
EDGE_MARGIN = 6
#: How long the pointer may be away before a revealed edge folds again.
HIDE_DELAY_MS = 150

#: The three edges Ctrl+H pins together, in the order the View menu lists them.
EDGE_KEYS = ('top', 'bottom', 'rail')

_EDGE_TITLES = {'top': 'top bar', 'bottom': 'bottom bar', 'rail': 'side rail'}


def edge_title(key: str) -> str:
    return _EDGE_TITLES.get(key, key)


def pin_button_icon(pinned: bool):
    """The push-pin glyph (MF5's pin.svg / pin-on.svg), tilted when the edge
    floats and upright with a red head when it is pinned."""
    from foammesh.view.main_window.sheet_header import pin_icon
    return pin_icon(pinned)


def set_pin_look(pin: QToolButton, title: str) -> None:
    """Icon, tooltip and size of a push-pin button; the object name, the
    accessible name and the action it fires stay as they were (Plan 44 U6)."""
    pinned = pin.isChecked()
    pin.setText('')
    pin.setIcon(pin_button_icon(pinned))
    pin.setIconSize(QSize(16, 16))
    pin.setToolTip((QApplication.translate('ToolEdge', 'Unpin the {0}')
                    if pinned else QApplication.translate(
                        'ToolEdge', 'Pin the {0}')).format(title))
    pin.setStatusTip(QApplication.translate(
        'ToolEdge', 'Keep the {0} open (Ctrl+H pins or unpins all three '
        'tool edges)').format(title))


def make_pin_button(key: str, title: str) -> QToolButton:
    """The pin button every edge carries: pressed means *keep it out*.

    A push-pin icon, never the word "Pin" (Plan 44 U6)."""
    pin = QToolButton()
    pin.setObjectName('toolEdgePin_' + key)
    pin.setCheckable(True)
    pin.setAutoRaise(True)
    pin.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    pin.setAccessibleName(QApplication.translate(
        'ToolEdge', 'Pin the {0}').format(title))
    set_pin_look(pin, title)
    pin.toggled.connect(lambda _on, b=pin, t=title: set_pin_look(b, t))
    try:
        from foammesh.app import app
        manager = getattr(app, 'themeManager', None)
        if manager is not None:
            manager.themeChanged.connect(
                lambda *_a, b=pin, t=title: _refresh_pin(b, t))
    except Exception:  # noqa: BLE001 - tools and tests without the app
        pass
    return pin


def _refresh_pin(pin: QToolButton, title: str) -> None:
    try:
        set_pin_look(pin, title)
    except RuntimeError:                      # the button is already gone
        pass


def style_tool_tile(button: QToolButton) -> None:
    """Style a tool button of a viewport edge as a tile (hover, checked) and
    keep it in step with the theme. The sheet lives in the theming package
    (``status_colors.style_tool_tile``); needs the object name set first."""
    status_colors.style_tool_tile(button)


def restyle_tiles() -> int:
    return status_colors.restyle_tool_tiles()


def centred_span(length: int, centre: float, low: int, high: int) -> int:
    """Where a bar *length* long starts so that it is centred on *centre*
    and stays inside ``[low, high]`` (Plan 44 U3). Used by every edge."""
    start = int(round(centre - length / 2.0))
    return max(low, min(start, high - length))


class NubButton(QToolButton):
    """The folded edge's nub: an accent tab of ``NUB_DRAW`` px, radius 3, in
    a ``NUB_THICKNESS`` px hit area, flush with the viewport border."""

    def __init__(self, parent: QWidget, side: str):
        super().__init__(parent)
        self._side = side

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # DP-2707. A sibling of the GL view is a native window, and a native
        # window over GL cannot be see-through: the undrawn part of the hit
        # area painted black. The window is cut to the tab it draws.
        self.setMask(QRegion(self.drawnRect()))

    def drawnRect(self) -> QRect:
        rect = self.rect()
        if self._side == TOP:
            return QRect(0, 0, rect.width(), NUB_DRAW)
        if self._side == BOTTOM:
            return QRect(0, rect.height() - NUB_DRAW, rect.width(), NUB_DRAW)
        return QRect(rect.width() - NUB_DRAW, 0, NUB_DRAW, rect.height())

    def colour(self) -> QColor:
        accent = QColor(theme_colour('accent.default'))
        if self.underMouse():
            return accent
        ground = QColor(theme_colour('viewport.mid'))
        return QColor(
            round(accent.red() * NUB_ALPHA + ground.red() * (1 - NUB_ALPHA)),
            round(accent.green() * NUB_ALPHA
                  + ground.green() * (1 - NUB_ALPHA)),
            round(accent.blue() * NUB_ALPHA + ground.blue() * (1 - NUB_ALPHA)))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.colour())
        painter.drawRoundedRect(self.drawnRect(), 3, 3)
        painter.end()


def make_edge_frame(view: QWidget, key: str, content: QWidget,
                    pin: QToolButton) -> QFrame:
    """A floating frame holding *content* and its pin button side by side."""
    frame = QFrame(view)
    frame.setObjectName('toolEdgeFrame_' + key)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    row = QHBoxLayout(frame)
    row.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT,
                           MARGIN_TIGHT)
    row.setSpacing(GAP_TIGHT)
    row.addWidget(content, 1)
    row.addWidget(pin, 0, Qt.AlignmentFlag.AlignTop)
    style_floating_panel(frame, radius=6)
    return frame


class ToolEdge(QObject):
    """One edge of the viewport: a frame, a nub, a pin, and the rules between.

    *placer* answers the frame's rectangle in view coordinates while the edge
    is out. *hold* answers True while something keeps the edge out (an open
    popover of the rail, say). Neither is called on a hidden edge's account:
    folding an edge only moves its frame.
    """

    pinnedChanged = Signal(bool)
    revealedChanged = Signal(bool)

    def __init__(self, view: QWidget, key: str, side: str, frame: QWidget,
                 pin: QToolButton, placer: Callable[[], QRect],
                 hold: Optional[Callable[[], bool]] = None,
                 settled: Optional[Callable[[], None]] = None):
        super().__init__(view)
        self._view = view
        self._key = key
        self._side = side
        self._frame = frame
        self._pin = pin
        self._placer = placer
        self._hold = hold or (lambda: False)
        #: Called after a hidden frame was sized and laid out at its revealed
        #: rectangle, so a row that reflows to its width (the top bar) is
        #: right while it is folded and the first reveal shows it as it is.
        self._settled = settled
        self._pinned = False
        self._revealed = False
        self._byFocus = False
        self._available = True
        self._applying = False
        self._settleKey = None
        self._revealOnHover = True
        self._anim = None
        self._animTarget = QRect()
        self._folding = False

        nub = NubButton(view, side)
        nub.setObjectName('toolEdgeNub_' + key)
        nub.setAutoRaise(True)
        nub.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        nub.setCursor(Qt.CursorShape.PointingHandCursor)
        nub.setToolTip(QApplication.translate(
            'ToolEdge', 'Show the {0}').format(edge_title(key)))
        nub.setAccessibleName(nub.toolTip())
        nub.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # Only the 5 px tab is drawn (NubButton.paintEvent); the application
        # sheet must not fill the 12 px hit area around it.
        status_colors.style_bare_hit_area(nub)
        if side == RIGHT:
            nub.setFixedSize(NUB_THICKNESS, NUB_LENGTH)
        else:
            nub.setFixedSize(NUB_LENGTH, NUB_THICKNESS)
        nub.clicked.connect(self._nubPressed)
        nub.installEventFilter(self)
        self._nub = nub

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(HIDE_DELAY_MS)
        self._timer.timeout.connect(self._maybeConceal)

        frame.installEventFilter(self)
        view.installEventFilter(self)
        pin.toggled.connect(self.setPinned)

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focusMoved)
        nub.show()
        frame.show()
        self.apply()

    # -- state -------------------------------------------------------------- #

    @property
    def key(self) -> str:
        return self._key

    def frame(self) -> QWidget:
        return self._frame

    def nub(self) -> QToolButton:
        return self._nub

    def pinButton(self) -> QToolButton:
        return self._pin

    def isPinned(self) -> bool:
        return self._pinned

    def isRevealed(self) -> bool:
        """True while the frame is drawn in the view (pinned or revealed)."""
        return self._pinned or self._revealed

    def isAvailable(self) -> bool:
        return self._available

    def setAvailable(self, available: bool) -> None:
        """Switch the whole edge on or off (the View menu's Show entries)."""
        available = bool(available)
        if available == self._available:
            return
        self._available = available
        if not available:
            self._timer.stop()
            self._revealed = False
        self.apply()

    def setPinned(self, pinned: bool) -> None:
        pinned = bool(pinned)
        if pinned == self._pinned:
            return
        self._pinned = pinned
        if self._pin.isChecked() != pinned:
            blocked = self._pin.blockSignals(True)
            self._pin.setChecked(pinned)
            self._pin.blockSignals(blocked)
        if pinned:
            self._timer.stop()
        else:
            # Let go: it stays out until the pointer has left, like a reveal.
            if self._revealed:
                self._timer.start()
        self.apply()
        self.pinnedChanged.emit(pinned)

    # -- reveal and fold ---------------------------------------------------- #

    def reveal(self, *, byFocus: bool = False) -> None:
        if not self._available:
            return
        self._byFocus = bool(byFocus) and not self._pinned
        if self._revealed:
            self._timer.start()
            return
        self._revealed = True
        self.apply()
        self._timer.start()
        self.revealedChanged.emit(True)

    def conceal(self) -> None:
        """Fold the edge. A pinned edge stays out."""
        self._timer.stop()
        if self._pinned or not self._revealed:
            return
        self._revealed = False
        self._byFocus = False
        self.apply()
        self.revealedChanged.emit(False)

    def setRevealOnHover(self, hover: bool) -> None:
        """View > Viewport tools > Tools appear on hover / on click: with
        *hover* False only a press on the nub brings the edge back."""
        self._revealOnHover = bool(hover)

    def revealsOnHover(self) -> bool:
        return self._revealOnHover

    def _nubPressed(self) -> None:
        if self._revealed and not self._pinned:
            self.conceal()
        else:
            self.reveal()

    def _cursorOver(self) -> bool:
        point = QCursor.pos()
        for widget in (self._frame, self._nub):
            if widget.isVisible() and QRect(
                    widget.mapToGlobal(QPoint(0, 0)),
                    widget.size()).contains(point):
                return True
        return False

    def _maybeConceal(self) -> None:
        if self._pinned or not self._revealed or not self._alive():
            return
        keep = (self._cursorOver() or bool(self._hold())
                or QApplication.activePopupWidget() is not None)
        if not keep and self._byFocus:
            focus = QApplication.focusWidget()
            keep = (focus is not None
                    and (focus is self._frame
                         or self._frame.isAncestorOf(focus)))
        if keep:
            self._timer.start()
        else:
            self.conceal()

    def _focusMoved(self, _old, new) -> None:
        if (not self._available or not isinstance(new, QWidget)
                or not self._alive()):
            return
        inside = new is self._frame or self._frame.isAncestorOf(new)
        if inside and not self.isRevealed():
            self.reveal(byFocus=True)
        elif not inside and self._byFocus and self._revealed:
            self._timer.start()

    # -- geometry ----------------------------------------------------------- #

    def revealedRect(self) -> QRect:
        return QRect(self._placer())

    def foldedRect(self) -> QRect:
        """Where the frame waits: just past its edge, out of the view."""
        rect = self.revealedRect()
        if self._side == TOP:
            rect.moveTop(-rect.height() - EDGE_MARGIN)
        elif self._side == BOTTOM:
            rect.moveTop(self._view.height() + EDGE_MARGIN)
        else:
            rect.moveLeft(self._view.width() + EDGE_MARGIN)
        return rect

    def _nubRect(self) -> QRect:
        """The nub is centred on the edge's own axis (U3): the middle of the
        tool bar it brings back, which sits on the clear area's axis."""
        width, height = self._view.width(), self._view.height()
        rect = self.revealedRect() if self._available else QRect()
        if self._side == TOP:
            x = centred_span(NUB_LENGTH, rect.center().x() + 0.5
                             if not rect.isNull() else width / 2.0, 0, width)
            return QRect(x, 0, NUB_LENGTH, NUB_THICKNESS)
        if self._side == BOTTOM:
            x = centred_span(NUB_LENGTH, rect.center().x() + 0.5
                             if not rect.isNull() else width / 2.0, 0, width)
            return QRect(x, height - NUB_THICKNESS, NUB_LENGTH, NUB_THICKNESS)
        y = centred_span(NUB_LENGTH, rect.center().y() + 0.5
                         if not rect.isNull() else height / 2.0, 0, height)
        return QRect(width - NUB_THICKNESS, y, NUB_THICKNESS, NUB_LENGTH)

    def slideOffset(self) -> QPoint:
        """Where a revealed rect starts from, and returns to: 48 px (52 on
        the rail) toward its border."""
        px = SLIDE_PX[self._side]
        if self._side == TOP:
            return QPoint(0, -px)
        if self._side == BOTTOM:
            return QPoint(0, px)
        return QPoint(px, 0)

    def _alive(self) -> bool:
        """False once the viewport, the frame or the nub is being destroyed:
        Qt sends a dying widget's last events to its filters."""
        return (isValid(self._view) and isValid(self._frame)
                and isValid(self._nub))

    def apply(self) -> None:
        """Put the frame and the nub where the state says.

        Revealing slides the frame in from its border (48 px, 52 on the rail,
        180 ms, OutCubic) and folding slides it back out and then hides it; the
        slide is skipped while a render hold is active and under reduced motion
        (``motion.slide``), when the frame lands at once.
        """
        if self._applying or not self._alive():
            return
        self._applying = True
        try:
            out = self._available and self.isRevealed()
            if out:
                self._settleKey = None
                self._cancelFold()
                rect = self.revealedRect()
                if not self._frame.isVisible():
                    start = rect if not motion.motion_enabled() else (
                        rect.translated(self.slideOffset()))
                    self._frame.setGeometry(start)
                    self._frame.setVisible(True)
                self._goTo(rect)
            else:
                self._fold()
                self._parkHidden()
            self._nub.setGeometry(self._nubRect())
            self._nub.setVisible(self._available and not self._pinned)
            if out:
                self._frame.raise_()
            else:
                self._nub.raise_()
        finally:
            self._applying = False

    def _goTo(self, rect: QRect) -> None:
        """Move the frame to *rect*: at once, or by the reveal slide."""
        if rect == self._frame.geometry():
            return
        running = self._anim is not None and self._animTarget == rect
        if running:
            return
        self._animTarget = QRect(rect)
        self._anim = motion.slide(self._frame, rect, 180)

    def _cancelFold(self) -> None:
        self._folding = False

    def _fold(self) -> None:
        """Hide the frame: slide it out first when motion is on."""
        frame = self._frame
        if frame.isVisible() and motion.motion_enabled() and self._available:
            if self._folding:
                return
            self._folding = True
            target = self.revealedRect().translated(self.slideOffset())
            anim = motion.slide(frame, target, 150)
            self._anim, self._animTarget = anim, QRect(target)
            if anim is not None:
                anim.finished.connect(self._foldDone)
                return
        self._foldDone()

    def _foldDone(self) -> None:
        # Plan 44 MF10: a folded edge (and a switched-off one) is hidden, with
        # its pin, which is a child of the frame, so no sliver of it shows in
        # the footer or along the viewport border.
        self._anim = None
        self._folding = False
        if not self._alive() or (self._available and self.isRevealed()):
            return                    # it came back out while it was folding
        self._frame.setVisible(False)
        self._parkHidden()

    def _parkHidden(self) -> None:
        """A hidden frame waits at its revealed rect, laid out for the next
        reveal."""
        if self._available and not self._frame.isVisible():
            rect = self.revealedRect()
            if rect != self._frame.geometry():
                self._frame.setGeometry(rect)
            # Plan 44 HNG (DP-2688): settling lays the row out again, which
            # posts a LayoutRequest to the frame, which applies, which parks
            # and settles again: a folded edge never went idle (and a modal
            # prompt spun the main thread). Settle once per distinct
            # (rect, wanted size) only.
            hint = self._frame.sizeHint()
            key = (rect.x(), rect.y(), rect.width(), rect.height(),
                   hint.width(), hint.height())
            if key == self._settleKey:
                return
            self._settleKey = key
            self._settleHidden()

    def _settleHidden(self) -> None:
        """A hidden frame gets no resize or layout events until it is shown,
        so its row is laid out here at the size it will have."""
        frame = self._frame
        # Parents first: each layout hands its children their new sizes.
        for widget in [frame, *frame.findChildren(QWidget)]:
            if widget.layout() is not None:
                QApplication.sendEvent(
                    widget, QResizeEvent(widget.size(), widget.size()))
        if frame.layout() is not None:
            frame.layout().activate()
        if self._settled is not None:
            self._settled()

    def nubExtent(self) -> int:
        """What a folded edge covers of the viewport, across its edge."""
        if not self._available:
            return 0
        return NUB_THICKNESS + EDGE_MARGIN

    # -- events ------------------------------------------------------------- #

    def eventFilter(self, watched, event):
        if not self._alive():
            return False
        kind = event.type()
        if watched is self._view:
            if kind in (QEvent.Type.Resize, QEvent.Type.Show):
                self.apply()
        elif watched is self._nub:
            if kind == QEvent.Type.Enter and self._revealOnHover:
                self.reveal()
        elif watched is self._frame:
            if kind == QEvent.Type.Enter:
                self._timer.stop()
            elif kind == QEvent.Type.Leave:
                if self._revealed and not self._pinned:
                    self._timer.start()
            elif kind in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
                # The row inside learned its width, or wants another (a
                # readout changed its text). The frame follows what it wants,
                # and a folded frame keeps waiting past its edge at the size
                # it now has.
                self.apply()
        return super().eventFilter(watched, event)


def centring_insets(edges) -> tuple:
    """What the tool edges take from the clear area: nothing.

    LY5 centres the model in the clear area the sheet and the console leave.
    A tool edge is an overlay and is left out of that area, whether it is
    folded, revealed or pinned. The answer is ``(left, top, right, bottom)``
    and is always zeros; it is a function so that LY5 and the tests ask the
    same question.
    """
    return (0, 0, 0, 0)


def centre_rect(clear: QRect, size: QSize, axis: str, cross: int, *,
                margin: int = EDGE_MARGIN,
                lo: Optional[int] = None, hi: Optional[int] = None) -> QRect:
    """Plan 44 MF3 (U3). The rectangle of an edge that sizes to its content
    (*size*, its sizeHint) and stands on the clear area's centre line.

    *axis* is ``'x'`` for a horizontal edge (the top and the bottom bar): the
    rectangle is centred on the clear area's centre x and *cross* is its top.
    It is ``'y'`` for a vertical edge (the side rail, MF13): centred on the
    centre y, *cross* is its left. The length along the axis never exceeds
    what fits on both sides of the centre, between *lo* and *hi* (the clear
    area's own ends less *margin* unless given), so the edge stays centred
    instead of sliding to fit; a long edge is shortened, never moved.
    """
    if axis == 'x':
        start, length = clear.x(), clear.width()
        want, across = size.width(), size.height()
    else:
        start, length = clear.y(), clear.height()
        want, across = size.height(), size.width()
    centre2 = 2 * start + length                      # twice the centre
    low = start + margin if lo is None else lo
    high = start + length - margin if hi is None else hi
    half = min(centre2 - 2 * low, 2 * high - centre2)  # twice the half room
    extent = max(0, min(want, half))
    begin = (centre2 - extent) // 2
    if axis == 'x':
        return QRect(begin, cross, extent, across)
    return QRect(cross, begin, across, extent)


PARTS_CHIP_HEIGHT = 26
PARTS_CHIP_RADIUS = 6
PARTS_CHIP_FONT_PX = 12
PARTS_CHIP_TEXT_ALPHA = 0.7
PARTS_PANEL_WIDTH = 260
PARTS_PANEL_RADIUS = 6


class PartsChip(QToolButton):
    """The Parts chip: "‹ Parts · 3 of 3", at the clear area's top right.

    Plan 44 MF21 (F9). A faint outline chip, not a pill: the ``overlay.chip.
    border`` outline, 26 px tall, radius 6, 12 px ``foreground.legend`` text
    at 70 %. The native GL viewport draws over a plain sibling and a native
    window cannot be translucent (section 3), so the chip is a *native* window
    whose mask is only its outline and its glyphs: the interior is the view,
    pixel for pixel. Resting on it peeks the Parts panel.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName('partsChip')
        self.setAutoRaise(True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        font = QFont(self.font())
        font.setPixelSize(PARTS_CHIP_FONT_PX)
        self.setFont(font)
        self.setToolTip(self.tr(
            'Parts: show, hide, isolate and explode. Rest the pointer here, '
            'or press, to open the panel; pin it to keep it open.'))
        self.setProperty('foammeshFlat', False)
        self._placer: Optional[Callable[[QRect], QRect]] = None
        self.setCounts(0, 0)
        self.winId()                    # native now, so the mask is honoured

    # -- the words ---------------------------------------------------------- #

    def setCounts(self, shown: int, total: int) -> None:
        if total:
            self.setText(self.tr('‹ Parts · {0} of {1}').format(
                shown, total))
            # DP-186 (WCAG 2.1 SC 2.5.3): the name carries the words the
            # chip paints, the count with them; only the chevron is dropped.
            self.setAccessibleName(self.tr('Parts · {0} of {1}').format(
                shown, total))
        else:
            self.setText(self.tr('‹ Parts · 0'))
            self.setAccessibleName(self.tr('Parts · 0'))
        self.setFixedHeight(PARTS_CHIP_HEIGHT)
        self.adjustSize()
        self._remask()
        self.update()

    def sizeHint(self) -> QSize:
        width = self.fontMetrics().horizontalAdvance(self.text()) + 20
        return QSize(max(width, 56), PARTS_CHIP_HEIGHT)

    def wantedSize(self) -> tuple:
        hint = self.sizeHint()
        return (hint.width(), hint.height())

    # -- where it stands ---------------------------------------------------- #

    def setPlacer(self, placer: Optional[Callable[[QRect], QRect]]) -> None:
        """*placer* turns the rectangle the overlay layout gives into the one
        that keeps the chip off the sheet header and the top pill."""
        self._placer = placer

    def setGeometry(self, *args) -> None:       # noqa: N802 (Qt name)
        rect = args[0] if len(args) == 1 else QRect(*args)
        if self._placer is not None:
            rect = self._placer(QRect(rect))
        super().setGeometry(rect)

    # -- the look ------------------------------------------------------------ #

    def _textRect(self) -> QRect:
        return QRect(10, 0, max(0, self.width() - 20), self.height())

    def _outline(self) -> QPainterPath:
        path = QPainterPath()
        r = float(PARTS_CHIP_RADIUS)
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0,
                                   self.height() - 1.0), r, r)
        return path

    def _remask(self) -> None:
        """The mask: the 1 px outline plus a 1 px halo round each glyph."""
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2:
            return
        bits = QBitmap(w, h)
        bits.fill(Qt.GlobalColor.color0)
        painter = QPainter(bits)
        painter.setPen(QPen(Qt.GlobalColor.color1, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.drawPath(self._outline())
        painter.setFont(self.font())
        rect = self._textRect()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                painter.drawText(rect.translated(dx, dy),
                                 int(Qt.AlignmentFlag.AlignVCenter
                                     | Qt.AlignmentFlag.AlignLeft),
                                 self.text())
        painter.end()
        self.setMask(bits)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._remask()

    def textColour(self) -> QColor:
        colour = QColor(theme_colour('foreground.legend'))
        colour.setAlphaF(PARTS_CHIP_TEXT_ALPHA if self.isEnabled() else 0.4)
        return colour

    def borderColour(self) -> QColor:
        raw = _token('overlay.chip.border') or '#ffffff1f'
        colour = QColor(raw if len(raw) < 9 else '#' + raw[7:9] + raw[1:7])
        if self.underMouse():
            colour.setAlpha(min(255, colour.alpha() * 2))
        return colour

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # What the mask lets through (the glyph halos) is the view's own
        # ground, so a glyph's soft edge blends with it, not with a plate.
        ground = QColor(theme_colour('viewport.mid'))
        painter.fillRect(self.rect(), ground)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # The outline is composited over the ground (a native window has no
        # alpha), the same as ``#ffffff1f`` over the view.
        painter.setPen(QPen(self.borderColour(), 1))
        painter.drawPath(self._outline())
        painter.setFont(self.font())
        painter.setPen(self.textColour())
        painter.drawText(self._textRect(),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft), self.text())
        painter.end()


def _token(name: str) -> Optional[str]:
    from foammesh.view.theming.elevation import token
    return token(name, None)


class PartsPanelEdge(QObject):
    """The Parts panel's auto-hide: hover on the chip opens it, leaving folds
    it, a pin keeps it. The panel is the rail's Parts popover, unchanged."""

    pinnedChanged = Signal(bool)
    #: The panel was shown or hidden (by the chip, the rail, a pin or Esc).
    openChanged = Signal(bool)

    def __init__(self, rail, chip: PartsChip):
        super().__init__(chip)
        self._rail = rail
        self._chip = chip
        self._revealOnHover = True
        self._open = False
        self._byHover = False
        self._wantPinned = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(HIDE_DELAY_MS)
        self._timer.timeout.connect(self._maybeFold)
        self._closedSeen = False
        chip.installEventFilter(self)
        chip.clicked.connect(self._pressed)
        # DP-2718: a press on the chip is the panel's toggle, not a click
        # away that closes it before the press opens it again.
        if hasattr(rail, 'addOpener'):
            rail.addOpener('parts', chip)
        popover = rail.popover('parts')
        if popover is not None:
            popover.installEventFilter(self)
            popover.pinnedChanged.connect(self._popoverPinned)
            popover.closed.connect(self._popoverClosed)
            self._dress(popover)
            app = QApplication.instance()
            if app is not None:
                app.focusChanged.connect(self._focusMoved)

    # -- the panel's look (MF21, F10) --------------------------------------- #

    def _dress(self, popover) -> None:
        """260 px, ``overlay.parts``, radius 6, ``elevate('card')``, and a
        native window with a rounded mask: the GL view draws over a plain
        sibling (section 3), and a native window cannot be translucent."""
        from foammesh.view.theming.elevation import elevate
        from foammesh.view.theming.status_colors import style_overlay_panel
        popover.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        popover.winId()
        style_overlay_panel(popover, 'parts', PARTS_PANEL_RADIUS)
        self._elevation = elevate(popover, 'card')
        wanted = popover.wantedSize

        def panelSize(room: QSize) -> QSize:
            size = wanted(room)
            return QSize(min(PARTS_PANEL_WIDTH, size.width()), size.height())

        popover.wantedSize = panelSize
        self._remask()

    def _remask(self) -> None:
        popover = self._popover()
        if popover is None:
            return
        w, h = popover.width(), popover.height()
        r = PARTS_PANEL_RADIUS
        if w <= 2 * r or h <= 2 * r:
            popover.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), r, r)
        popover.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def accentExplode(self) -> int:
        """Colour the Explode slider with ``explode.accent``; how many."""
        popover = self._popover()
        if popover is None:
            return 0
        colour = QColor(theme_colour('explode.accent'))
        count = 0
        for slider in popover.findChildren(QSlider):
            palette = slider.palette()
            palette.setColor(QPalette.ColorRole.Highlight, colour)
            slider.setPalette(palette)
            count += 1
        return count

    # -- code and the pointer ------------------------------------------------ #

    def setOpen(self, on: bool) -> None:
        """Show or hide the panel from code. Hiding also lets go of a pin and
        of a hover reveal; showing is a steady open (it does not fold when the
        pointer is elsewhere)."""
        self._timer.stop()
        self._byHover = False
        if on:
            if not self.isOpen():
                self._rail.open('parts')
        elif self.isOpen():
            self._rail.close('parts')
            popover = self._popover()
            if popover is not None and popover.isVisible():
                popover.setVisible(False)     # a rail that refused is overruled

    def hide(self) -> None:
        self.setOpen(False)

    def show(self) -> None:
        self.setOpen(True)

    def _focusMoved(self, _old, new) -> None:
        """Using the panel (a slider, an eye) keeps what a hover opened."""
        popover = self._popover()
        if (self._byHover and popover is not None and new is not None
                and new is not popover and popover.isAncestorOf(new)):
            self._byHover = False
            self._timer.stop()

    def _popover(self):
        return self._rail.popover('parts')

    def setRevealOnHover(self, hover: bool) -> None:
        self._revealOnHover = bool(hover)

    def _visibilityChanged(self) -> None:
        popover = self._popover()
        parent = popover.parentWidget() if popover is not None else None
        now = bool(popover is not None and parent is not None
                   and popover.isVisibleTo(parent))
        if now != self._open:
            self._open = now
            self.openChanged.emit(now)

    def isPinned(self) -> bool:
        popover = self._popover()
        return popover is not None and popover.isPinned()

    def isOpen(self) -> bool:
        return self._rail.isOpen('parts')

    def setPinned(self, pinned: bool) -> None:
        """Pin the panel (opening it) or let it auto-hide again."""
        pinned = bool(pinned)
        popover = self._popover()
        if popover is None:
            return
        self._wantPinned = pinned
        if pinned:
            if not self.isOpen():
                self._rail.open('parts')
            popover.setPinned(True)
        elif popover.isPinned():
            popover.setPinned(False)
        self._byHover = False

    def wantsPinned(self) -> bool:
        """The pin the user asked for, kept while the scene has no parts."""
        return self._wantPinned or self.isPinned()

    def reveal(self, *, sticky: bool = False) -> None:
        """Peek (hover): it folds once the pointer has gone. ``sticky`` is the
        code path that wants it open and left open."""
        if sticky:
            self.setOpen(True)
            return
        if not self.isOpen():
            self._rail.open('parts')
            if self.isOpen():
                self._byHover = True
        self._timer.start()

    def _pressed(self) -> None:
        if self._byHover and self.isOpen():
            # It was opened by resting on the chip; a press keeps it open.
            self._byHover = False
            return
        self._byHover = False
        self._rail.toggle('parts')

    def _popoverPinned(self, on: bool) -> None:
        self._wantPinned = bool(on)
        if on:
            self._byHover = False
        self.pinnedChanged.emit(bool(on))

    def _outsideHide(self) -> None:
        """A ``popover.hide()`` from code never says ``closed``; the rail and
        the pin would still think the panel is up. Say it, once."""
        self._closedSeen = False
        QTimer.singleShot(0, self._settleHide)

    def _settleHide(self) -> None:
        popover = self._popover()
        if (popover is None or popover.isVisible() or self._closedSeen):
            return
        popover.closed.emit()

    def _popoverClosed(self) -> None:
        self._closedSeen = True
        self._byHover = False
        self._timer.stop()
        if self._wantPinned:
            self._wantPinned = False
            self.pinnedChanged.emit(False)

    def _cursorOver(self) -> bool:
        point = QCursor.pos()
        popover = self._popover()
        for widget in (self._chip, popover):
            if widget is not None and widget.isVisible() and QRect(
                    widget.mapToGlobal(QPoint(0, 0)),
                    widget.size()).contains(point):
                return True
        return False

    def _maybeFold(self) -> None:
        if not self._byHover or not self.isOpen() or self.isPinned():
            return
        if self._cursorOver() or QApplication.activePopupWidget() is not None:
            self._timer.start()
            return
        self._byHover = False
        self._rail.close('parts')

    # -- the view under the chip (DP-2718) ----------------------------------- #

    def passThrough(self, widget: Optional[QWidget]) -> None:
        """The chip's mask is its outline and glyphs, so a press or a rest
        between the letters reaches the view under it, not the chip. Watch
        *widget* (the view's interactor) and hand the chip what lands inside
        its rectangle."""
        if not isinstance(widget, QWidget):
            widget = None
        self._under = widget
        self._underPressed = False
        self._underInside = False
        if widget is not None:
            widget.installEventFilter(self)

    def _overChip(self, watched, event) -> bool:
        chip = self._chip
        if not (chip.isVisible() and chip.isEnabled()):
            return False
        pos = event.position().toPoint()
        return chip.rect().contains(
            chip.mapFromGlobal(watched.mapToGlobal(pos)))

    def _underEvent(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            if (event.button() == Qt.MouseButton.LeftButton
                    and self._overChip(watched, event)):
                self._underPressed = True
                return True
        elif kind == QEvent.Type.MouseButtonRelease and self._underPressed:
            self._underPressed = False
            if self._overChip(watched, event):
                self._chip.click()
            return True
        elif kind == QEvent.Type.MouseButtonDblClick:
            return self._overChip(watched, event)
        elif kind == QEvent.Type.MouseMove:
            inside = self._overChip(watched, event)
            if inside and not self._underInside:
                if self._revealOnHover:
                    self.reveal()
                else:
                    self._timer.stop()
            elif not inside and self._underInside and self._byHover:
                self._timer.start()
            self._underInside = inside
            return self._underPressed
        elif kind == QEvent.Type.Leave and self._underInside:
            self._underInside = False
            if self._byHover:
                self._timer.start()
        return False

    def eventFilter(self, watched, event):
        if watched is getattr(self, '_under', None) and watched is not None:
            if self._underEvent(watched, event):
                return True
            return super().eventFilter(watched, event)
        kind = event.type()
        if watched is self._chip and kind == QEvent.Type.Enter:
            if self._chip.isEnabled() and self._revealOnHover:
                self.reveal()
        elif kind == QEvent.Type.Resize and watched is self._popover():
            self._remask()
        elif kind in (QEvent.Type.Show, QEvent.Type.Hide):
            self._visibilityChanged()
            if watched is self._popover():
                if kind == QEvent.Type.Show:
                    self.accentExplode()
                else:
                    self._outsideHide()
        elif (kind == QEvent.Type.MouseButtonPress
              and watched is self._popover() and self._byHover):
            self._byHover = False
            self._timer.stop()
        elif kind == QEvent.Type.Enter:
            self._timer.stop()
        elif kind == QEvent.Type.Leave and self._byHover:
            self._timer.start()
        return super().eventFilter(watched, event)


def bar_height(frame: QWidget) -> int:
    hint = frame.sizeHint()
    return max(hint.height(), 30)

