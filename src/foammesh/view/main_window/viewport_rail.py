#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 40 UXE2: the viewport tool rail and its popovers (section 4.4 item 6).

The horizontal toolbar over the viewport keeps the camera, the view mode and
the capture. Every tool that used to open a panel on top of the model -- the
parts list, the cut plane, the section, the display switches, the inspector --
is one button on a narrow rail at the right edge of the viewport. A button
opens a *popover* drawn over the model, anchored to it; the viewport is never
resized for it, so the mesh keeps the area.

The rules, drawn in ``plans/evidence/plan40/viewport-rail-mockup.html``:

* one transient popover at a time; opening another closes it;
* a click anywhere else, Esc, or the same button again closes it;
* the pin keeps a popover open (at most two, stacked down the right edge) and
  nothing the view does -- rotating, rendering, reloading -- closes it;
* a short window folds the lowest tools of the rail into its ``...`` menu, and
  a narrow window sends the horizontal toolbar's overflow there as well, so
  every tool stays reachable at any size;
* *Customise* (right-click on the rail or the toolbar): text under the icons,
  and the rail itself shown or hidden. It is saved and comes back;
* Plan 43 LY6: the rail folds to a nub at the right edge and comes back on
  hover or press (``tool_edges.ToolEdge``). A pin keeps it out. It never moves
  the model: folded or out, it is drawn over the view and the view's size is
  untouched. While a popover is open the rail stays out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from shiboken6 import isValid
from PySide6.QtCore import (
    QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QCursor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget)

from foammesh.view.main_window.tool_edges import (
    RIGHT, ToolEdge, centred_span, make_pin_button, round_native_corners,
    set_pin_look, style_tool_tile)
from foammesh.view.theming.fonts import caps_font
from foammesh.view.theming.icons import apply_tinted_icon, tinted_icon
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_TIGHT, MARGIN_TIGHT)
from foammesh.view.theming.status_colors import (
    OVERLAY_KINDS, style_floating_panel, style_overlay_panel)

#: Plan 44 MF13 (mockup M:139-165): a 34 px rail of 26 px tiles with 16 px
#: glyphs, padding 10 0, radius 6, 18 x 1 separators.
RAIL_WIDTH = 34
RAIL_MARGIN = 6
RAIL_PADDING = 10
RAIL_RULE = 18
GLYPH = 16
#: DP-2723. No stroke of a rail glyph thinner than this on screen.
GLYPH_STROKE_PX = 1.5
RAIL_WIDTH_WITH_TEXT = 64
BUTTON_EDGE = 26
#: The rail is a button wide and what is left over is its side inset.
RAIL_SIDE = (RAIL_WIDTH - BUTTON_EDGE) // 2
POPOVER_WIDTH = 330
POPOVER_MARGIN = 8
#: Pinned popovers allowed at once (mockup, point 6).
MAX_PINNED = 2

#: Rail groups, top to bottom. A short window folds the *last* tool first.
DEFAULT_PREFERENCES = {'rail': True, 'labels': False}


@dataclass
class RailTool:
    key: str
    title: str
    icon: str
    #: The popover's body, or None for a tool that does something instead
    #: (the inspector opens the Mesh quality report).
    content: Optional[QWidget] = None
    tip: str = ''
    button: Optional[QToolButton] = None
    popover: Optional['Popover'] = None
    group: int = 0
    active: bool = False
    enabled: bool = True
    reason: str = ''
    #: Plan 45 N (P9). A control the rail took over rather than made: the
    #: row's own switch, same object and wiring, standing on the rail as a
    #: tool of its own. ``widget`` is what stands in the column (the button,
    #: or the frame that holds it).
    adopted: bool = False
    widget: Optional[QWidget] = None
    #: Which tools a short rail folds first: the highest rank goes first, a
    #: tie folds the lowest tool first.
    fold: int = 0


class _PopoverGrip(QWidget):
    """Plan 45 M. The handle at a popover's bottom-left corner. The popover
    hangs from the rail's edge, so pulling the corner out to the left widens
    it and pulling down makes it taller. (Not a QSizeGrip: that resizes the
    top-level window.)"""

    EDGE = 14

    def __init__(self, popover: 'Popover'):
        super().__init__(popover)
        self.setObjectName('popoverGrip')
        self.setFixedSize(self.EDGE, self.EDGE)
        self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        self.setToolTip(self.tr('Drag to resize'))
        self.setAccessibleName(self.tr('Resize {0}').format(popover.windowTitle()
                                                            or popover.key))
        self._press: Optional[tuple] = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            popover = self.parentWidget()
            self._press = (event.globalPosition().toPoint(),
                           QSize(popover.size()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is None:
            return
        start, size = self._press
        delta = event.globalPosition().toPoint() - start
        self.parentWidget().setUserSize(
            QSize(max(160, size.width() - delta.x()),
                  max(120, size.height() + delta.y())))
        event.accept()

    def mouseReleaseEvent(self, event):
        self._press = None
        event.accept()

    def paintEvent(self, _event):
        painter = QPainter(self)
        pen = QPen(self.palette().color(QPalette.ColorRole.Mid))
        painter.setPen(pen)
        for step in (4, 8, 12):
            painter.drawLine(0, self.height() - step - 1, step, self.height() - 1)


class Popover(QFrame):
    """A panel drawn over the viewport, anchored to a rail button."""

    closed = Signal()
    pinnedChanged = Signal(bool)
    #: Plan 45 P12: the content asked for a new size while the popover is
    #: open (a section of it opened or closed); the rail arranges again.
    contentResized = Signal()

    def __init__(self, key: str, title: str, content: QWidget, parent: QWidget):
        super().__init__(parent)
        self.key = key
        self.setObjectName('partsPopover' if key == 'parts'
                           else 'viewportPopover_' + key)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(True)
        self.setAccessibleName(title)
        style_overlay_panel(self, 'popover')
        # Plan 45 P16: a native window over the GL view; cut to the sheet's
        # radius or the square outside it paints black.
        round_native_corners(self, OVERLAY_KINDS['popover'][2])

        column = QVBoxLayout(self)
        column.setContentsMargins(FORM_MARGIN, MARGIN_TIGHT, FORM_MARGIN, FORM_MARGIN)
        column.setSpacing(GAP)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel(title)
        self._title.setObjectName('popoverTitle')
        # Plan 44 MF13: the popover's header is the small-caps caption.
        self._title.setFont(caps_font(10.5))
        self._pin = QToolButton()
        self._pin.setObjectName('popoverPin')
        self._pin.setCheckable(True)
        self._pin.setAutoRaise(True)
        self._pin.setAccessibleName(self.tr('Pin {0}').format(title))
        set_pin_look(self._pin, title)
        self._pin.toggled.connect(
            lambda _on: set_pin_look(self._pin, title))
        self._pin.toggled.connect(self.pinnedChanged)
        self._close = QToolButton()
        self._close.setObjectName('popoverClose')
        self._close.setText('✕')
        self._close.setAutoRaise(True)
        self._close.setToolTip(self.tr('Close with {0}').format('Esc'))
        self._close.setAccessibleName(self.tr('Close {0}').format(title))
        self._close.clicked.connect(self.dismiss)
        head.addWidget(self._title, 1)
        head.addWidget(self._pin)
        head.addWidget(self._close)
        column.addLayout(head)

        if content.property('popoverOwnsScroll'):
            # Plan 45 M: the content scrolls its own list; a second scroll
            # area around it would put a bar inside a bar.
            self._scroll = None
            # Into the popover first: shown while it had no parent, the
            # content was a window of its own on screen for a moment (the
            # Parts panel, 200 x 100, at every start; seen live 18 October
            # 2026).
            column.addWidget(content, 1)
            content.setVisible(True)
        else:
            self._scroll = QScrollArea()
            self._scroll.setObjectName('popoverScroll')
            self._scroll.setWidgetResizable(True)
            self._scroll.setFrameShape(QFrame.Shape.NoFrame)
            self._scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self._scroll.setWidget(content)
            content.setVisible(True)
            column.addWidget(self._scroll, 1)
        self._content = content
        # Plan 45 P12: one arrange per burst of layout requests, a turn of
        # the event loop later, when the content's size hint is settled.
        self._resizeTimer = QTimer(self)
        self._resizeTimer.setSingleShot(True)
        self._resizeTimer.setInterval(0)
        self._resizeTimer.timeout.connect(self._contentSettled)
        content.installEventFilter(self)
        # Plan 45 M: the user's size, kept for the session.
        self._userSize: Optional[QSize] = None
        self._grip = _PopoverGrip(self)
        self.setVisible(False)

    def userSize(self) -> Optional[QSize]:
        return self._userSize

    def setUserSize(self, size: Optional[QSize]) -> None:
        self._userSize = QSize(size) if size is not None else None
        self.contentResized.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._grip.move(2, self.height() - self._grip.height() - 2)
        self._grip.raise_()

    def eventFilter(self, watched, event):
        if (watched is self._content
                and event.type() == QEvent.Type.LayoutRequest
                and self.isVisible()):
            self._resizeTimer.start()
        return super().eventFilter(watched, event)

    def _contentSettled(self) -> None:
        if self.isVisible():
            self.contentResized.emit()

    def isPinned(self) -> bool:
        return self._pin.isChecked()

    def setPinned(self, pinned: bool):
        self._pin.setChecked(bool(pinned))

    def contentWidget(self) -> QWidget:
        return self._content

    def wantedSize(self, room: QSize) -> QSize:
        """Wide enough for what is inside, never wider than the viewport."""
        hint = self._content.sizeHint()
        want_w = max(POPOVER_WIDTH, hint.width() + 28)
        want_h = hint.height() + self._title.sizeHint().height() + 36
        if self._scroll is None:
            # Plan 45 M: the list inside must get exactly its ten rows, so
            # no slack: header, the column's margins and gap, the frame.
            head = max(self._title.sizeHint().height(),
                       self._pin.sizeHint().height(),
                       self._close.sizeHint().height())
            column = self.layout()
            margins = column.contentsMargins()
            want_h = (hint.height() + head + column.spacing()
                      + margins.top() + margins.bottom()
                      + 2 * self.frameWidth())
        if self._userSize is not None:
            want_w = self._userSize.width()
            want_h = self._userSize.height()
        width = min(max(want_w, 160) if self._userSize is not None else want_w,
                    max(160, room.width() - 2 * POPOVER_MARGIN))
        height = min(max(want_h, 120) if self._userSize is not None
                     else want_h,
                     max(120, room.height() - 2 * POPOVER_MARGIN))
        return QSize(width, height)

    def dismiss(self):
        self.setVisible(False)
        self.closed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)


class ViewportRail(QObject):
    """The rail, its popovers and the rules between them."""

    #: A tool without a popover was pressed (the inspector).
    actionRequested = Signal(str)
    #: ``{'rail': bool, 'labels': bool}`` changed through Customise.
    preferencesChanged = Signal(dict)
    #: The width of the view's right edge the rail covers changed (VI15).
    extentChanged = Signal(int)

    def __init__(self, view: QWidget):
        """*view* is the viewport the rail and the popovers float over."""
        super().__init__(view)
        self._view = view
        self._tools: dict[str, RailTool] = {}
        self._order: list[str] = []
        self._off: set[str] = set()
        self._preferences = dict(DEFAULT_PREFERENCES)
        self._transientKey: Optional[str] = None
        self._appFilter = False
        self._railOn = True
        self._clickWatched: list[QWidget] = []
        # DP-2718: widgets outside the rail that open a popover (the bottom
        # bar's Parts button); a press on one is that popover's own business.
        self._openers: dict[str, list[QWidget]] = {}
        self._pinned: list[str] = []
        self._extraOverflow: Optional[Callable[[QMenu], None]] = None
        self._extraCount: Callable[[], int] = lambda: 0
        self._hidden: list[str] = []
        self._sceneEmpty = False
        self._clearHooked = False

        # The rail floats over the right edge of the view, like the card at
        # the top left: the viewport keeps its area (the U2 share is the
        # view's, with or without the rail).
        self._host = view

        # A frame with a column of buttons, not a QToolBar: the toolbar's own
        # layout hides what does not fit behind an extension arrow of its own,
        # which is a second overflow with a different menu.
        self._bar = QFrame(view)
        self._bar.setObjectName('viewportRail')
        self._bar.setFrameShape(QFrame.Shape.NoFrame)
        self._bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._column = QVBoxLayout(self._bar)
        # Plan 45 N: no side margins. The bar's 1 px border already takes a
        # pixel off each side, so 4 + 26 + 4 overflowed the 32 px inside and
        # every button stood 1 px right of the bar's centre (measured live:
        # 5 px left, 3 px right). Each item is centred in what is left.
        self._column.setContentsMargins(0, RAIL_PADDING, 0, RAIL_PADDING)
        self._column.setSpacing(GAP_TIGHT)
        self._bar.setAccessibleName(self.tr('Viewport tools'))
        self._bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._bar.customContextMenuRequested.connect(self._customiseAt)
        self._bar.setFixedWidth(RAIL_WIDTH)
        style_floating_panel(self._bar, radius=6)
        # Plan 45 P16: the bar is a native window over the GL view.
        round_native_corners(self._bar, 6)
        self._bar.installEventFilter(self)

        self._more = QToolButton()
        self._more.setObjectName('viewportRailMore')
        self._more.setText('⋯')
        self._more.setAutoRaise(True)
        self._more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more.setToolTip(self.tr(
            'The viewport tools that do not fit at this size'))
        self._more.setAccessibleName(self.tr('More viewport tools'))
        menu = QMenu(self._more)
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(self._fillMore)
        self._more.setMenu(menu)
        style_tool_tile(self._more)
        # LY6. The pin is the first button: pressed, the rail stays out.
        self._pin = make_pin_button('rail', self.tr('side rail'))
        self._pin.setFixedSize(self._buttonSize().width(), 22)
        style_tool_tile(self._pin)
        self._column.addWidget(self._pin, 0, Qt.AlignmentFlag.AlignHCenter)
        self._column.addStretch(1)
        self._column.addWidget(
            self._more, 0, Qt.AlignmentFlag.AlignHCenter)
        self._more.setFixedSize(self._buttonSize())
        self._more.setVisible(False)
        self._separators: list[QFrame] = []

        view.installEventFilter(self)
        self._edge = ToolEdge(view, 'rail', RIGHT, self._bar, self._pin,
                              self._revealedRect, hold=self._popoverHeld)
        self._edge.pinnedChanged.connect(
            lambda _on: self.extentChanged.emit(self._extent()))
        self._place()
        self._bar.raise_()
        view.destroyed.connect(self._releaseAppFilter)

    # -- the click-away filter ---------------------------------------------- #

    @property
    def _transient(self) -> Optional[str]:
        return self._transientKey

    @_transient.setter
    def _transient(self, key: Optional[str]):
        self._transientKey = key
        # The application-wide filter exists only while a popover that a
        # click elsewhere would dismiss is open. A filter that stayed on the
        # application for the whole session was still called while the view
        # and this object were being torn down (exit 139 on the Gmsh walk).
        if key:
            self._holdAppFilter()
        else:
            self._releaseAppFilter()

    def _holdAppFilter(self):
        """Start watching for a click away from the open popover.

        DP-1447. No filter goes on the application: a Python filter on the
        application is called for every event of every object, including
        ones Qt is destroying, and on the Gmsh walks it was handed a freed
        object at the end of a leg (rc 139, ``QObject.eventFilter called with
        wrong argument types: (QAbstractEventDispatcher, QStandardItem)``;
        the same family as DP-1430 and DP-1438).  A click in the view lands
        on the view or one of its child widgets, so the filter goes on those
        -- widget filters are only called for a live widget -- and a click
        elsewhere in the window moves the focus, which the application
        reports through a signal.
        """
        if self._appFilter:
            return
        self._appFilter = True
        for widget in [self._view, *self._view.findChildren(QWidget)]:
            if self._isRailWidget(widget):
                continue
            widget.installEventFilter(self)
            self._clickWatched.append(widget)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focusMoved)

    def _isRailWidget(self, widget) -> bool:
        if widget is self._bar or self._bar.isAncestorOf(widget):
            return True
        for tool in self._tools.values():
            popover = tool.popover
            if popover is not None and (
                    widget is popover or popover.isAncestorOf(widget)):
                return True
        return False

    def _releaseAppFilter(self, *_):
        if not self._appFilter:
            return
        self._appFilter = False
        watched, self._clickWatched = self._clickWatched, []
        for widget in watched:
            if widget is self._view:
                continue            # the view keeps its resize filter
            try:
                widget.removeEventFilter(self)
            except RuntimeError:
                pass
        app = QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._focusMoved)
            except (RuntimeError, TypeError):
                pass

    def addOpener(self, key: str, widget: QWidget) -> None:
        """*widget* opens and closes *key*'s popover from outside the rail:
        a press on it (anywhere in its rectangle) is not a click away."""
        self._openers.setdefault(key, []).append(widget)

    def _onOpener(self, target, point) -> bool:
        if point is not None:
            # DP-2720: a press inside an open popover moves the focus (to the
            # rail, then the view) before it is delivered; the pointer, not
            # the focus, says where it was.
            for tool in self._tools.values():
                popover = tool.popover
                if (popover is not None and isValid(popover)
                        and popover.isVisible()
                        and QRect(popover.mapToGlobal(QPoint(0, 0)),
                                  popover.size()).contains(point)):
                    return True
        for widget in self._openers.get(self._transient or '', ()):
            if not isValid(widget) or not widget.isVisible():
                continue
            if widget is target or widget.isAncestorOf(target):
                return True
            if point is not None and QRect(widget.mapToGlobal(QPoint(0, 0)),
                                           widget.size()).contains(point):
                return True
        return False

    def _focusMoved(self, _old, new):
        if self._transient and isinstance(new, QWidget):
            # DP-2718: a press moves the focus before it is delivered; where
            # the pointer is says whether it was on an opener.
            self._maybeClickAway(new, QCursor.pos())

    # -- building ----------------------------------------------------------- #

    def extent(self) -> int:
        """How much of the view's right edge the rail covers (0 when hidden)."""
        return self._extent()

    def _extent(self) -> int:
        """How much of the view's right edge the rail covers.

        LY6. A rail that folds covers its nub and no more, whether it is out
        for the moment or not, so the legends and the readouts do not shuffle
        each time the pointer passes. A pinned rail covers its width.
        """
        if not self._railOn:
            return 0
        edge = getattr(self, '_edge', None)
        if edge is None or edge.isPinned():
            return self._barExtent()
        return edge.nubExtent()

    def _barExtent(self) -> int:
        """The rail's full width plus its margin (0 when it is switched off)."""
        if not self._railOn:
            return 0
        return self._bar.width() + RAIL_MARGIN

    def _revealedRect(self) -> QRect:
        """The rail is as tall as its tools and centred on the clear area's
        horizontal axis (Plan 44 MF13 / U3), never the full height."""
        view = self._view
        room = max(0, view.height() - 2 * RAIL_MARGIN)
        height = min(max(self._bar.sizeHint().height(), 0), room)
        low, high = RAIL_MARGIN, max(RAIL_MARGIN, view.height() - RAIL_MARGIN)
        top = centred_span(height, self._clearCentreY(), low, high)
        return QRect(view.width() - self._bar.width() - RAIL_MARGIN, top,
                     self._bar.width(), height)

    def _clearAreaController(self):
        """The window's clear-area controller, when it has one."""
        try:
            # MF27: the window's ``clearArea`` is a property, so the
            # controller itself comes back; the old ``callable`` test read
            # it as missing and the popovers were clamped to the whole view
            # (the Parts popover hung below the clear area, under the
            # console). A method that returns the controller still works.
            area = getattr(self._view.window(), 'clearArea', None)
            if callable(area) and not hasattr(area, 'clearRect'):
                area = area()
            if area is not None and not hasattr(area, 'clearRect'):
                area = None
        except (RuntimeError, AttributeError):
            return None
        if area is not None and not self._clearHooked:
            changed = getattr(area, 'changed', None)
            if changed is not None:
                changed.connect(lambda *_a: self._recentre())
                self._clearHooked = True
        return area

    def clearRect(self) -> QRect:
        """The clear area the rail centres on (the view when there is none)."""
        area = self._clearAreaController()
        if area is not None:
            try:
                rect = area.clearRect()
                if rect.isValid() and not rect.isEmpty():
                    return rect
            except (RuntimeError, AttributeError):
                pass
        return QRect(0, 0, self._view.width(), self._view.height())

    def _clearCentreY(self) -> float:
        rect = self.clearRect()
        return rect.top() + rect.height() / 2.0

    def _recentre(self) -> None:
        if isValid(self._bar):
            self._place()
            self.arrange()

    def _popoverHeld(self) -> bool:
        """A popover hangs from the rail: it stays out while one is open,
        and while a rail tool's menu is (Plan 45 N)."""
        if self.openKeys():
            return True
        for menu, _anchor in getattr(self, '_sideMenus', {}).values():
            if isValid(menu) and menu.isVisible():
                return True
        return False

    def edge(self) -> ToolEdge:
        return self._edge

    def _place(self):
        edge = getattr(self, '_edge', None)
        if edge is not None:
            edge.apply()
        self._syncColumn()

    def _syncColumn(self) -> bool:
        """DP-2724. Lay the column out again when the bar's size has moved on.

        Measured live 2026-10-16: the column last ran while the rail was
        folded to its 84 px nub and the bar then grew to 204 px with no
        layout pass, so the five 26 px buttons stood 9 to 15 px apart, one
        over the next. True when it had to."""
        bar = getattr(self, '_bar', None)
        if bar is None or not isValid(bar):
            return False
        if self._column.geometry() == bar.contentsRect():
            return False
        self._column.invalidate()
        self._column.activate()
        return True

    def host(self) -> QWidget:
        return self._host

    def bar(self) -> QFrame:
        return self._bar

    def addTool(self, key: str, title: str, icon: str,
                content: Optional[QWidget] = None, *, tip: str = '',
                group: int = 0, fold: int = 0) -> RailTool:
        tool = RailTool(key=key, title=title, icon=icon, content=content,
                        tip=tip or title, group=group, fold=fold)
        button = QToolButton()
        button.setObjectName('railTool_' + key)
        apply_tinted_icon(button, ':/graphicsIcons/' + icon,
                          QSize(GLYPH, GLYPH), GLYPH_STROKE_PX)
        style_tool_tile(button)
        button.setText(title)
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setProperty('foammeshFlat', True)
        button.setToolTip(tip or title)
        button.setAccessibleName(title)
        button.setFixedSize(self._buttonSize())
        button.clicked.connect(lambda _c=False, k=key: self.toggle(k))
        tool.button = button
        if content is not None:
            tool.popover = Popover(key, title, content, self._view)
            tool.popover.closed.connect(lambda k=key: self._popoverClosed(k))
            tool.popover.pinnedChanged.connect(
                lambda on, k=key: self._pinnedToggled(k, on))
            tool.popover.contentResized.connect(self._popoverResized)
        return self._addToColumn(tool, button)

    def adoptTool(self, key: str, title: str, widget: QWidget, *,
                  button=None, tip: str = '', group: int = 0,
                  fold: int = 0) -> RailTool:
        """Plan 45 N (P9, Q3 b). Stand an existing control on the rail.

        The display switches were rows of one Display popover; each is now a
        rail tool of its own. The control is the one the row had -- the same
        object, name, signals and gating -- moved into the column, so there
        is still one copy of it. *button* is what a press works (the
        Background frame's swatch button); a switch keeps its own checked
        state, and a button with a menu opens it beside the rail.
        """
        button = widget if button is None else button
        tool = RailTool(key=key, title=title, icon='', tip=tip or title,
                        group=group, fold=fold, button=button, widget=widget,
                        adopted=True)
        widget.setParent(self._bar)
        if widget is not button and widget.layout() is not None:
            widget.layout().setContentsMargins(0, 0, 0, 0)
        if isinstance(button, QToolButton):
            # A push button (the axes, the ruler) would print the words.
            if not button.text():
                button.setText(title)
            button.setAutoRaise(True)
        if isinstance(button, QAbstractButton):
            button.setIconSize(QSize(GLYPH, GLYPH))
            # DP-2720: a switch that is on looks on (the accent ring).
            if button.objectName():
                button.setProperty('foammeshTile', True)
                style_tool_tile(button)
        menu = self._menuOf(button)
        if menu is not None:
            self._besideTheRail(menu, button)
        key_text = ''
        shortcut = getattr(button, 'shortcut', None)
        if callable(shortcut):
            key_text = shortcut().toString()
        tip = tool.tip + (' ({0})'.format(key_text) if key_text else '')
        tool.tip = tip
        # The row's gate keeps the default tip and shows a reason instead
        # while the switch is off; the default is now the rail's.
        button.setProperty('foammeshDefaultTip', tip)
        if button.isEnabled():
            button.setToolTip(tip)
        if not button.accessibleName():
            button.setAccessibleName(title)
        widget.setVisible(True)
        return self._addToColumn(tool, widget)

    def _addToColumn(self, tool: RailTool, item: QWidget) -> RailTool:
        key, group = tool.key, tool.group
        # Above the stretch, so the menu button stays at the foot.
        at = self._column.count() - 2
        if self._order and self._tools[self._order[-1]].group != group:
            rule = QFrame()
            rule.setObjectName('railGroupRule')
            rule.setFrameShape(QFrame.Shape.HLine)
            rule.setFrameShadow(QFrame.Shadow.Plain)
            rule.setFixedSize(RAIL_RULE, 1)
            self._column.insertWidget(
                at, rule, 0, Qt.AlignmentFlag.AlignHCenter)
            self._separators.append(rule)
            at += 1
        self._column.insertWidget(
            at, item, 0, Qt.AlignmentFlag.AlignHCenter)
        self._tools[key] = tool
        self._order.append(key)
        self._applyStyle(tool)
        self.reflow()
        return tool

    def _buttonSize(self) -> QSize:
        if self._preferences.get('labels'):
            return QSize(RAIL_WIDTH_WITH_TEXT - 4, 46)
        return QSize(BUTTON_EDGE, BUTTON_EDGE)

    def _applyStyle(self, tool: RailTool):
        button = tool.button
        labels = bool(self._preferences.get('labels'))
        if isinstance(button, QToolButton):
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon if labels
                else Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setFixedSize(self._buttonSize())
        item = self._item(tool)
        if item is not button:
            item.setFixedSize(self._buttonSize())

    @staticmethod
    def _item(tool: RailTool) -> QWidget:
        """What stands in the column for *tool*."""
        return tool.widget if tool.widget is not None else tool.button

    # -- state -------------------------------------------------------------- #

    def tools(self) -> list[str]:
        return list(self._order)

    def tool(self, key: str) -> RailTool:
        return self._tools[key]

    def popover(self, key: str) -> Optional[Popover]:
        return self._tools[key].popover

    def isOpen(self, key: str) -> bool:
        popover = self._tools[key].popover
        return popover is not None and popover.isVisible()

    def openKeys(self) -> list[str]:
        return [k for k in self._order if self.isOpen(k)]

    def pinnedKeys(self) -> list[str]:
        return list(self._pinned)

    def hiddenTools(self) -> list[str]:
        """The tools the rail has folded into its ``...`` menu."""
        return list(self._hidden)

    def reachable(self) -> list[str]:
        """Every tool a user can press: on the rail or in the menu."""
        return [k for k in self._order
                if not self._item(self._tools[k]).isHidden()
                or k in self._hidden]

    def setToolVisible(self, key: str, visible: bool) -> None:
        """Plan 48 GM11b. Take a tool off the rail, or put it back.

        The Gmsh tool dock is for the Gmsh route only (R6): on the other
        engine its button is not folded into the ``...`` menu, it is gone.
        A tool that is off the rail closes its popover.
        """
        if key not in self._tools:
            raise KeyError(key)
        if visible:
            self._off.discard(key)
        else:
            if self.isOpen(key):
                self.close(key)
            self._off.add(key)
        self.reflow()

    def isToolVisible(self, key: str) -> bool:
        return key in self._tools and key not in self._off

    def setToolEnabled(self, key: str, enabled: bool, reason: str = ''):
        tool = self._tools[key]
        tool.enabled = bool(enabled)
        tool.reason = reason
        tool.button.setEnabled(bool(enabled))
        if tool.adopted:
            return
        tool.button.setToolTip(
            tool.tip if enabled or not reason
            else '{0}\n{1}'.format(tool.tip, reason))
        if not enabled and self.isOpen(key):
            self.close(key)

    def setSceneEmpty(self, empty: bool, gated=()):
        self._sceneEmpty = bool(empty)
        for key in gated:
            self.setToolEnabled(
                key, not empty,
                self.tr('Load a geometry or a mesh') if empty else '')

    def setToolActive(self, key: str, active: bool):
        """Highlight the button while its tool is in effect, open or not."""
        tool = self._tools[key]
        tool.active = bool(active)
        tool.button.setProperty('toolActive', bool(active))
        self._refreshChecked(tool)
        tool.button.style().unpolish(tool.button)
        tool.button.style().polish(tool.button)

    def isToolActive(self, key: str) -> bool:
        return self._tools[key].active

    def _refreshChecked(self, tool: RailTool):
        if tool.adopted:
            return              # a switch says what it is itself (Plan 45 N)
        tool.button.blockSignals(True)
        tool.button.setChecked(tool.active or self.isOpen(tool.key))
        tool.button.blockSignals(False)

    # -- open / close ------------------------------------------------------- #

    def toggle(self, key: str):
        tool = self._tools[key]
        if tool.adopted:
            # One press: a switch flips, a button with a menu opens it.
            if not tool.button.isEnabled():
                return
            if self._menuOf(tool.button) is not None:
                self.popMenu(key)
            else:
                tool.button.click()
            return
        if tool.popover is None:
            self._refreshChecked(tool)
            self.actionRequested.emit(key)
            return
        if self.isOpen(key):
            self.close(key)
        else:
            self.open(key)

    def open(self, key: str):
        tool = self._tools[key]
        if tool.popover is None or not tool.enabled:
            return
        if self._transient and self._transient != key:
            self.close(self._transient)
        popover = tool.popover
        if not popover.isPinned():
            self._transient = key
        popover.setVisible(True)
        popover.raise_()
        self.arrange()
        popover.setFocus()
        self._refreshChecked(tool)

    def close(self, key: str):
        tool = self._tools[key]
        if tool.popover is not None and tool.popover.isVisible():
            tool.popover.dismiss()
        else:
            self._popoverClosed(key)

    def closeTransient(self):
        if self._transient:
            self.close(self._transient)

    def _popoverClosed(self, key: str):
        tool = self._tools[key]
        if tool.popover is not None:
            tool.popover.blockSignals(True)
            tool.popover.setPinned(False)
            tool.popover.blockSignals(False)
        if key in self._pinned:
            self._pinned.remove(key)
        if self._transient == key:
            self._transient = None
        self._refreshChecked(tool)
        self.arrange()

    def _pinnedToggled(self, key: str, on: bool):
        if on:
            if self._transient == key:
                self._transient = None
            if key not in self._pinned:
                self._pinned.append(key)
            while len(self._pinned) > MAX_PINNED:
                self.close(self._pinned[0])
        else:
            if key in self._pinned:
                self._pinned.remove(key)
            # Unpinned, it is the transient one again (the old one goes).
            if self.isOpen(key):
                if self._transient and self._transient != key:
                    self.close(self._transient)
                self._transient = key
        self.arrange()

    # -- placement ---------------------------------------------------------- #

    def arrange(self):
        """Pinned popovers stack down the right edge; the transient one sits
        beside them, level with its button. Every popover stays inside the
        clear rectangle (V16 / DP-2649; the Parts chip it once hung under is
        retired, Plan 45)."""
        view = self._view
        room = view.size()
        clear = self.clearRect()
        floor = min(room.height(), clear.bottom() + 1)
        right = room.width() - self._barExtent() - POPOVER_MARGIN
        top = max(POPOVER_MARGIN, clear.top() + POPOVER_MARGIN)
        for key in self._pinned:
            popover = self._tools[key].popover
            if popover is None or not popover.isVisible():
                continue
            size = popover.wantedSize(
                QSize(room.width(), floor - top + POPOVER_MARGIN))
            popover.setGeometry(right - size.width(), top,
                                size.width(), size.height())
            popover.raise_()
            top += size.height() + POPOVER_MARGIN
        if self._transient:
            tool = self._tools[self._transient]
            popover = tool.popover
            if popover is not None and popover.isVisible():
                shift = 0
                if self._pinned:
                    widest = max(
                        (self._tools[k].popover.width() for k in self._pinned
                         if self._tools[k].popover.isVisible()), default=0)
                    shift = widest + POPOVER_MARGIN
                low = max(POPOVER_MARGIN, clear.top() + POPOVER_MARGIN)
                size = popover.wantedSize(
                    QSize(room.width() - shift, floor - low + POPOVER_MARGIN))
                anchor = self._anchorOf(tool)
                # Level with its button, clamped inside the clear rectangle.
                high = floor - size.height() - POPOVER_MARGIN
                y = max(low, min(anchor.y() - POPOVER_MARGIN, high))
                popover.setGeometry(right - shift - size.width(), y,
                                    size.width(), size.height())
                popover.raise_()

    def _popoverResized(self) -> None:
        """Plan 45 P12: an open popover's content changed its size hint."""
        if isValid(self._view) and self.openKeys():
            self.arrange()

    def _anchorOf(self, tool: RailTool) -> QPoint:
        item = self._item(tool)
        button = item if not item.isHidden() else self._more
        centre = button.mapToGlobal(QPoint(0, button.height() // 2))
        return self._view.mapFromGlobal(centre)

    # -- the rail's own size ------------------------------------------------ #

    def setExtraOverflow(self, fill: Callable[[QMenu], None],
                         count: Callable[[], int]):
        """The horizontal toolbar's overflow, shown in the rail's menu."""
        self._extraOverflow = fill
        self._extraCount = count
        self.reflow()

    def reflow(self, room: Optional[int] = None):
        """Fold the lowest tools into the ``...`` menu until the rail fits.

        *room* is the height to fit, the rail's own when omitted."""
        if getattr(self, '_reflowing', False):
            return
        # A widget that is being destroyed sends its last Hide to the filters.
        if not (isValid(self._bar) and isValid(self._pin)):
            return
        self._reflowing = True
        try:
            # Plan 48 GM11b: a tool taken off the rail for the engine in use
            # (setToolVisible) stays out of the row, the fold and the menu.
            keys = [k for k in self._order if k not in self._off]
            for k in self._order:
                self._item(self._tools[k]).setVisible(k not in self._off)
            if room is None:
                # The rail is as tall as its tools, so what it has to fit in
                # is the view, not its own (shrunk) height.
                room = max(0, self._view.height() - 2 * RAIL_MARGIN)
            extra = bool(self._extraCount())
            hidden: list[str] = []
            if room > 0 and keys:
                step = self._buttonSize().height() + self._column.spacing()
                spacing = self._column.spacing()
                rules = sum(rule.sizeHint().height() + spacing
                            for rule in self._separators)
                margins = self._column.contentsMargins()
                pin = self._pin.height() + spacing
                usable = (room - margins.top() - margins.bottom() - rules
                          - pin)
                if len(keys) * step > usable or extra:
                    keep = max(0, int((usable - step) // step))
                    # Plan 45 N: the highest fold rank goes first; among
                    # equals, the lowest tool (the old rule).
                    ranked = sorted(range(len(keys)), key=lambda i: (
                        self._tools[keys[i]].fold, i))
                    kept = {keys[i] for i in ranked[:keep]}
                    hidden = [k for k in keys if k not in kept]
                for k in hidden:
                    self._item(self._tools[k]).setVisible(False)
            self._hidden = hidden
            self._more.setFixedSize(self._buttonSize())
            self._more.setVisible(bool(hidden) or extra)
        finally:
            self._reflowing = False
        self._syncColumn()
        self.arrange()

    def _fillMore(self):
        menu = self._more.menu()
        menu.clear()
        for key in self._hidden:
            tool = self._tools[key]
            if tool.adopted:
                # The switch itself is hidden; its entry says its state.
                button = tool.button
                action = menu.addAction(button.icon(), tool.title)
                action.setToolTip(tool.tip)
                action.setEnabled(button.isEnabled())
                if button.isCheckable():
                    action.setCheckable(True)
                    action.setChecked(button.isChecked())
                action.triggered.connect(
                    lambda _c=False, k=key: self.toggle(k))
                continue
            action = menu.addAction(
                tinted_icon(':/graphicsIcons/' + tool.icon,
                            QSize(GLYPH, GLYPH), GLYPH_STROKE_PX), tool.title)
            action.setToolTip(tool.tip)
            action.setEnabled(tool.enabled)
            action.triggered.connect(
                lambda _c=False, k=key: self.toggle(k))
        if self._extraOverflow is not None and self._extraCount():
            if not menu.isEmpty():
                menu.addSeparator()
            self._extraOverflow(menu)
        if menu.isEmpty():
            menu.addAction(self.tr('Everything fits at this size')).setEnabled(
                False)

    # -- a rail tool's own menu (Plan 45 N) -------------------------------- #

    def _besideTheRail(self, menu: QMenu, anchor: QWidget) -> None:
        """*menu* opens to the left of the rail, level with *anchor*, not
        under a 26 px button at the window's right edge."""
        if not hasattr(self, '_sideMenus'):
            self._sideMenus = {}
        self._sideMenus[id(menu)] = (menu, anchor)
        menu.installEventFilter(self)

    @staticmethod
    def _menuOf(button) -> Optional[QMenu]:
        menu = getattr(button, 'menu', None)
        return menu() if callable(menu) else None

    def popMenu(self, key: str) -> Optional[QMenu]:
        """Open *key*'s menu beside the rail (from its button, or from the
        ``...`` menu when the button is folded away)."""
        tool = self._tools[key]
        menu = self._menuOf(tool.button) if tool.adopted else None
        if menu is None:
            return None
        menu.popup(self._besidePoint(menu, tool))
        return menu

    def _besidePoint(self, menu: QMenu, tool: Optional[RailTool]) -> QPoint:
        anchor = self._more
        if tool is not None and not self._item(tool).isHidden():
            anchor = tool.button
        width = max(menu.width(), menu.sizeHint().width())
        left = self._bar.mapToGlobal(QPoint(0, 0)).x()
        top = anchor.mapToGlobal(QPoint(0, 0)).y()
        point = QPoint(left - width - POPOVER_MARGIN, top)
        screen = anchor.screen()
        if screen is not None:
            room = screen.availableGeometry()
            height = max(menu.height(), menu.sizeHint().height())
            point.setY(max(room.top(), min(point.y(),
                                           room.bottom() - height)))
            point.setX(max(room.left(), point.x()))
        return point

    def _placeSideMenu(self, menu: QMenu) -> None:
        tool = next((t for t in self._tools.values() if t.adopted
                     and self._menuOf(t.button) is menu), None)
        point = self._besidePoint(menu, tool)
        if menu.pos() != point:
            menu.move(point)

    # -- the local API (Plan 48 PA5) ---------------------------------------- #

    def overflowEntries(self) -> list:
        """What the ``...`` menu lists now: ``{key, text, enabled, checked}``
        for each folded tool (``key`` is '' for the toolbar's own entries)."""
        menu = self._more.menu()
        self._fillMore()
        keys = list(self._hidden)
        entries = []
        for action in menu.actions():
            if action.isSeparator():
                continue
            entries.append({'key': keys[len(entries)]
                            if len(entries) < len(keys) else '',
                            'text': action.text(), 'enabled': action.isEnabled(),
                            'checked': bool(action.isChecked())
                            if action.isCheckable() else False})
        return entries

    def railState(self) -> dict:
        """Everything the rail shows: its tools, popovers, pins, overflow
        and the Customise choices."""
        tools = []
        for key in self._order:
            tool = self._tools[key]
            popover = tool.popover
            size = popover.size() if popover is not None else None
            user = popover.userSize() if popover is not None else None
            tools.append({
                'key': key, 'title': tool.title, 'tip': tool.tip,
                'visible': self.isToolVisible(key),
                'on_rail': (self.isToolVisible(key) and key not in self._hidden
                            and not self._item(tool).isHidden()),
                'folded': key in self._hidden,
                'enabled': bool(tool.enabled), 'reason': tool.reason,
                'checked': bool(tool.button.isChecked())
                if tool.button is not None and tool.button.isCheckable()
                else False,
                'adopted': bool(tool.adopted),
                'popover': popover is not None,
                'open': self.isOpen(key),
                'pinned': popover is not None and popover.isPinned(),
                'size': ({'width': size.width(), 'height': size.height()}
                         if size is not None else None),
                'user_size': ({'width': user.width(), 'height': user.height()}
                              if user is not None else None)})
        return {
            'tools': tools, 'open': self.openKeys(),
            'pinned': self.pinnedKeys(), 'hidden': self.hiddenTools(),
            'overflow': {'shown': not self._more.isHidden(),
                         'entries': self.overflowEntries()},
            'preferences': self.preferences(),
            'rail': {'pinned': self._edge.isPinned(),
                     'revealed': self._edge.isRevealed(),
                     'available': self._edge.isAvailable(),
                     'extent': self.extent()}}

    def setRailPinned(self, pinned: bool) -> None:
        """Press the rail's own pin (the first button of the column)."""
        if not self._edge.isAvailable():
            raise ValueError('the tool rail is not shown (Customise > '
                             'Show the tool rail)')
        self._pin.setChecked(bool(pinned))

    def setPopoverPinned(self, key: str, pinned: bool) -> None:
        """Press a popover's pin; the popover must be open, as its button is."""
        popover = self.popover(key)
        if popover is None:
            raise ValueError(f'{key} has no popover')
        if not popover.isVisible():
            raise ValueError(f'the {key} popover is not open')
        popover.setPinned(bool(pinned))

    def setPopoverSize(self, key: str, width=None, height=None) -> None:
        """Drag a popover's grip to a size; no size gives it back to its content."""
        popover = self.popover(key)
        if popover is None:
            raise ValueError(f'{key} has no popover')
        if width is None and height is None:
            popover.setUserSize(None)
        elif width is None or height is None:
            raise ValueError('give both width and height, or neither')
        else:
            popover.setUserSize(QSize(int(width), int(height)))
        self.arrange()

    def pressOverflow(self, key: str) -> None:
        """Choose a tool in the ``...`` menu, as its entry does."""
        if key not in self._hidden:
            raise ValueError(f'{key} is not in the overflow menu')
        self.toggle(key)

    # -- Customise ---------------------------------------------------------- #

    def preferences(self) -> dict:
        return dict(self._preferences)

    def setPreferences(self, preferences: dict, *, emit: bool = False):
        merged = dict(DEFAULT_PREFERENCES)
        for name in merged:
            if name in (preferences or {}):
                merged[name] = bool(preferences[name])
        changed = merged != self._preferences
        self._preferences = merged
        labels = merged['labels']
        self._bar.setFixedWidth(
            RAIL_WIDTH_WITH_TEXT if labels else RAIL_WIDTH)
        self._place()
        for tool in self._tools.values():
            self._applyStyle(tool)
        self._pin.setFixedSize(self._buttonSize().width(), 22)
        self._railOn = bool(merged['rail'])
        self._edge.setAvailable(self._railOn)
        self._place()
        if not merged['rail']:
            for key in list(self._order):
                if self.isOpen(key):
                    self.close(key)
        self.reflow()
        if changed and emit:
            self.preferencesChanged.emit(dict(merged))

    def customiseMenu(self, parent=None) -> QMenu:
        menu = QMenu(parent)
        menu.setObjectName('viewportCustomiseMenu')
        labels = menu.addAction(self.tr('Text under the icons'))
        labels.setObjectName('customiseLabels')
        labels.setCheckable(True)
        labels.setChecked(self._preferences['labels'])
        labels.toggled.connect(
            lambda on: self.setPreferences(
                {**self._preferences, 'labels': on}, emit=True))
        shown = menu.addAction(self.tr('Show the tool rail'))
        shown.setObjectName('customiseRail')
        shown.setCheckable(True)
        shown.setChecked(self._preferences['rail'])
        shown.toggled.connect(
            lambda on: self.setPreferences(
                {**self._preferences, 'rail': on}, emit=True))
        return menu

    def _customiseAt(self, point):
        menu = self.customiseMenu(self._bar)
        menu.exec(self._bar.mapToGlobal(point))

    def showCustomiseMenu(self, global_point):
        self.customiseMenu(self._view).exec(global_point)

    # -- events ------------------------------------------------------------- #

    def eventFilter(self, watched, event):
        kind = event.type()
        side = getattr(self, '_sideMenus', {}).get(id(watched))
        if (kind == QEvent.Type.Show and side is not None
                and side[0] is watched and isValid(self._bar)):
            self._placeSideMenu(watched)
            return super().eventFilter(watched, event)
        if watched is self._view and kind == QEvent.Type.Resize:
            self._place()
            self.reflow()
        elif watched is self._bar and kind in (
                QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            # A Hide that is the rail's own (Customise, the View menu) changes
            # what it covers. The Hide a parent sends its children as it is
            # destroyed changes nothing, and answering it reflowed a rail
            # whose buttons were being deleted (access violation, LY6).
            if kind == QEvent.Type.Hide and not self._bar.isHidden():
                return super().eventFilter(watched, event)
            self.reflow()
            self.extentChanged.emit(self._extent())
        elif kind == QEvent.Type.MouseButtonPress and self._transient:
            self._maybeClickAway(watched,
                                 event.globalPosition().toPoint())
        return super().eventFilter(watched, event)

    def _maybeClickAway(self, target, point=None):
        if not isinstance(target, QWidget):
            return
        if self._onOpener(target, point):
            return
        popover = self._tools[self._transient].popover
        if popover is None or not popover.isVisible():
            return
        # A combo box's list or a colour dialog is a window of its own: a
        # press in it is still the popover's business.
        if target.window() is not self._view.window():
            return
        if popover.isAncestorOf(target) or target is popover:
            return
        if self._bar.isAncestorOf(target) or target is self._bar:
            return
        for tool in self._tools.values():
            if tool.popover is not None and (
                    tool.popover.isAncestorOf(target) or target is tool.popover):
                return
        self.closeTransient()
