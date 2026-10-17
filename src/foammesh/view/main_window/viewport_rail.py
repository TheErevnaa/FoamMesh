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
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget)

from foammesh.view.main_window.tool_edges import (
    RIGHT, ToolEdge, centred_span, make_pin_button, set_pin_look,
    style_tool_tile)
from foammesh.view.theming.fonts import caps_font
from foammesh.view.theming.icons import apply_tinted_icon, tinted_icon
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP, GAP_TIGHT, MARGIN_TIGHT)
from foammesh.view.theming.status_colors import (
    style_floating_panel, style_overlay_panel)

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


class Popover(QFrame):
    """A panel drawn over the viewport, anchored to a rail button."""

    closed = Signal()
    pinnedChanged = Signal(bool)

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
        self.setVisible(False)

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
        width = min(want_w, max(160, room.width() - 2 * POPOVER_MARGIN))
        height = min(want_h, max(120, room.height() - 2 * POPOVER_MARGIN))
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
        self._preferences = dict(DEFAULT_PREFERENCES)
        self._transientKey: Optional[str] = None
        self._appFilter = False
        self._railOn = True
        self._clickWatched: list[QWidget] = []
        # DP-2718: widgets outside the rail that open a popover (the Parts
        # chip); a press on one is that popover's own business.
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
        self._column.setContentsMargins(
            RAIL_SIDE, RAIL_PADDING, RAIL_SIDE, RAIL_PADDING)
        self._column.setSpacing(GAP_TIGHT)
        self._bar.setAccessibleName(self.tr('Viewport tools'))
        self._bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._bar.customContextMenuRequested.connect(self._customiseAt)
        self._bar.setFixedWidth(RAIL_WIDTH)
        style_floating_panel(self._bar, radius=6)
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
        """A popover hangs from the rail: it stays out while one is open."""
        return bool(self.openKeys())

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
                group: int = 0) -> RailTool:
        tool = RailTool(key=key, title=title, icon=icon, content=content,
                        tip=tip or title, group=group)
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
            at, button, 0, Qt.AlignmentFlag.AlignHCenter)
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
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon if labels
            else Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setFixedSize(self._buttonSize())

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
                if not self._tools[k].button.isHidden() or k in self._hidden]

    def setToolEnabled(self, key: str, enabled: bool, reason: str = ''):
        tool = self._tools[key]
        tool.enabled = bool(enabled)
        tool.reason = reason
        tool.button.setEnabled(bool(enabled))
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
        tool.button.blockSignals(True)
        tool.button.setChecked(tool.active or self.isOpen(tool.key))
        tool.button.blockSignals(False)

    # -- open / close ------------------------------------------------------- #

    def toggle(self, key: str):
        tool = self._tools[key]
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

    def _chipRect(self) -> Optional[QRect]:
        """The Parts chip's rectangle in the view's pixels (V16 / DP-2649):
        the popovers hang under it and never over it."""
        area = self._clearAreaController()
        finder = getattr(area, 'float_rects_of_rank', None)
        if not callable(finder):
            return None
        try:
            from foammesh.view.main_window.clear_area import FLOAT_RANK_CHIP
            rects = finder(FLOAT_RANK_CHIP)
        except (RuntimeError, AttributeError, TypeError):
            return None
        return rects[0] if rects else None

    def _lowestTop(self, left: int, right: int, top: int) -> int:
        """*top*, moved below the Parts chip when the column ``left..right``
        stands under it."""
        chip = self._chipRect()
        if chip is not None and chip.left() <= right and chip.right() >= left:
            return max(top, chip.bottom() + 1 + POPOVER_MARGIN)
        return top

    def arrange(self):
        """Pinned popovers stack down the right edge; the transient one sits
        beside them, level with its button. Every popover stays inside the
        clear rectangle and under the Parts chip (V16 / DP-2649)."""
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
            left = right - size.width()
            top = self._lowestTop(left, right, top)
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
                left = right - shift - size.width()
                low = self._lowestTop(left, right - shift, low)
                size = popover.wantedSize(
                    QSize(room.width() - shift, floor - low + POPOVER_MARGIN))
                anchor = self._anchorOf(tool)
                # Level with its button, clamped inside the clear rectangle.
                high = floor - size.height() - POPOVER_MARGIN
                y = max(low, min(anchor.y() - POPOVER_MARGIN, high))
                popover.setGeometry(right - shift - size.width(), y,
                                    size.width(), size.height())
                popover.raise_()

    def _anchorOf(self, tool: RailTool) -> QPoint:
        button = tool.button if not tool.button.isHidden() else self._more
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
            keys = list(self._order)
            for k in keys:
                self._tools[k].button.setVisible(True)
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
                    hidden = keys[keep:] if len(keys) > keep else []
                for k in hidden:
                    self._tools[k].button.setVisible(False)
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
