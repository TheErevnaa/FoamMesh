"""Plan 44 MF11: the themed menu popup of the mockup (``.menu``, ``.mi``, ``.mh``).

A Qt menu is a square, system-framed popup painted by the style. The mockup's
menu is a rounded panel with a shadow, full-bleed rows (a 30 px gutter on the
left for the check or radio), small caps section headings and, on the right,
either a dimmer shortcut or a summary of what the group is set to.

QSS cannot do most of that (no shadow, no rounded translucent popup, no second
colour or size for the shortcut text), so this module paints the menu itself:

* ``ThemedMenu(QMenu)`` / ``themed(menu)`` make the popup frameless and
  translucent with ``elevation.elevate(menu, 'menu')`` and install a paint
  filter. The filter draws the shadow ring (elevation does that), the panel
  (``background.raised``, 1 px ``border.default``, a 5 px body radius: square
  at the top for a menu-bar menu, square at the top left for a flyout) and
  then every row from ``QMenu.actionGeometry``. Hover, keyboard focus,
  disabling, shortcuts and the submenu arrow still come from Qt; only the
  drawing is ours, so a menu keeps every behaviour of a ``QMenu``.
* Where translucency cannot be trusted (``elevation.probe_translucency``) the
  menu stays an ordinary opaque popup with square corners, and the same rows.
* ``heading(menu, text)`` adds a section heading: a disabled ``QWidgetAction``
  holding a ``QLabel[foammeshMenuHeading]``. It is not a command.
* ``set_summary(menu, title, summary)`` writes the group's right-hand summary
  into its parent row (``title + TAB + summary``, elided to 96 px).
* A menu that scrolls inside itself (``menu-scrollable``) gets the scroll
  arrows Qt would have drawn: a ``background.raised`` band at the top and/or
  bottom of the panel with a small ``foreground.muted`` chevron, shown only
  while there is more to scroll that way (``scroll_state``).

Every colour is read from the running theme on each paint, so a theme switch
needs no rebuild. Rows never carry a ``QKeySequence`` made from a summary.
"""
from __future__ import annotations

import re
import time
import weakref

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt,
                            QTimer)
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QMouseEvent,
                           QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QMenu, QScrollArea,
                               QStyle, QWidget, QWidgetAction)

from . import elevation, fonts

KIND_BAR = 'bar'          # a menu-bar menu: flush with the bar, square top
KIND_FLYOUT = 'flyout'    # a submenu: overlaps its parent by 2 px, square top left
KIND_POPUP = 'popup'      # a free-standing popup: rounded all round

#: (top left, top right, bottom right, bottom left) radius in px
_RADII = {
    KIND_BAR: (0, 0, 1, 1),
    KIND_FLYOUT: (0, 1, 1, 1),
    KIND_POPUP: (1, 1, 1, 1),
}
RADIUS = 5
FLYOUT_OVERLAP = 2
SUMMARY_MAX_PX = 96
SCROLL_RENUDGE_S = 0.13   # Qt's own scroll timer is 50 ms; give it two turns
SCROLL_POLL_MS = 40       # how often a visible menu looks for a pointer on its arrows
SCROLLER_MIN_PX = 12      # the scroll-arrow band; the QSS ``QMenu::scroller`` height

# Mockup type scale, in CSS px.
ITEM_PX = 12.5
SHORTCUT_PX = 11.5
SUMMARY_PX = 11

_HEADING_PROP = 'foammeshMenuHeading'
_FIRST_PROP = 'foammeshMenuFirst'
_RADIO_PROP = 'foammeshMenuRadio'
_FILTERS: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()


# -- tokens ---------------------------------------------------------------

def _colour(name: str, fallback: str) -> QColor:
    return (elevation.parse_colour(elevation.token(name, fallback))
            or elevation.parse_colour(fallback))


def _px_font(base: QFont, px: float) -> QFont:
    font = QFont(base)
    font.setPointSizeF(px * 0.75)          # 96 dpi: 1 CSS px = 0.75 pt
    return font


def strip_mnemonic(text: str) -> str:
    """``&Show`` -> ``Show``; ``&&`` is a literal ampersand."""
    return text.replace('&&', '\0').replace('&', '').replace('\0', '&')


def split_text(action) -> tuple:
    """(label, tail) of an action: the tail is the text after the TAB, or the
    action's own shortcut when it has none."""
    label, _tab, tail = action.text().partition('\t')
    if not tail:
        shortcut = action.shortcut()
        if not shortcut.isEmpty():
            tail = shortcut.toString(shortcut.SequenceFormat.NativeText)
    return strip_mnemonic(label), tail


def is_heading(action) -> bool:
    return bool(action.property(_HEADING_PROP))


# -- the paint filter -------------------------------------------------------

class _MenuPainter(QObject):
    """Event filter: paints the panel and the rows, positions the popup."""

    def __init__(self, menu: QMenu, kind: str):
        super().__init__(menu)
        self._menu = weakref.ref(menu)
        self.kind = kind
        self.handle = None
        self._placing = False
        self._placed = False
        self._poll = None
        self._scrolling = False
        self._nudged = 0.0

    # the margins elevation grew around the panel (0 when it stayed opaque)
    def margins(self) -> tuple:
        if self.handle is not None and self.handle.mode == 'window':
            return self.handle.margins
        return (0, 0, 0, 0)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.Paint and watched.isWidgetType():
            self._paint(watched)
            return True
        # A Move that arrives while the menu is still hidden is not where Qt
        # means the popup to go: a flyout gets one at (0, 0) before Qt has
        # worked out its place, and placing on it used up ``_placed`` so the
        # real position was never corrected (the panel sat 29 px off its
        # parent). The place is final by the Show, or by a Move once shown.
        if ((kind == QEvent.Type.Show
             or (kind == QEvent.Type.Move and watched.isVisible()))
                and not self._placed and not self._placing):
            self._place(watched)
        elif kind == QEvent.Type.Hide:
            self._placed = False
        if watched is self._menu():
            if kind == QEvent.Type.Show:
                self._start_poll()
            elif kind == QEvent.Type.Hide:
                self._stop_poll()
        return False

    # -- the scroll arrows under a pointer ---------------------------------

    def _start_poll(self) -> None:
        if self._poll is None:
            self._poll = QTimer(self)
            self._poll.setInterval(SCROLL_POLL_MS)
            self._poll.timeout.connect(self._follow_pointer)
        if not self._poll.isActive():
            self._poll.start()

    def _stop_poll(self) -> None:
        if self._poll is not None:
            self._poll.stop()
        self._scrolling = False

    def _follow_pointer(self) -> None:
        """Scroll while the pointer stands on a painted arrow.

        CHL (DP-2661, M4). Qt scrolls a menu in ``mouseEventTaken``, from a
        move *the menu itself receives* in the bottom (top) ``scrollerHeight``
        px of its WINDOW, and only for a menu it made scrollable at popup (it
        looks at the screen, not at the cap the view menu puts on a flyout). So
        the painted arrow, which sits above the shadow margin, never scrolled
        anything live, and a menu holding a ``QWidgetAction`` (View > Colour)
        never receives the move at all: the panel under the pointer does. The
        pointer is therefore read from the cursor. A menu body that is a
        :class:`MenuScrollHost` is scrolled by it; any other menu is sent a
        move inside Qt's own zone, and a move out of it to stop.
        """
        menu = self._menu()
        if menu is None or not menu.isVisible():
            self._stop_poll()
            return
        direction = self._arrow_under(menu, menu.mapFromGlobal(QCursor.pos()))
        host = menu.findChild(MenuScrollHost)
        if host is not None:
            host.scroll_by(direction * MenuScrollHost.STEP_PX)
            return
        if direction:
            # Qt restarts its 50 ms scroll timer on every move it takes, so a
            # move sent faster than that would keep it from ever firing: send
            # one on arrival, then again only after the timer has had its turns.
            now = time.monotonic()
            if not self._scrolling or now - self._nudged >= SCROLL_RENUDGE_S:
                edge = 1 if direction < 0 else max(1, menu.height() - 1)
                self._nudge(menu, QPoint(menu.width() // 2, edge))
                self._nudged = now
            self._scrolling = True
        elif self._scrolling:
            self._scrolling = False
            self._nudge(menu, QPoint(menu.width() // 2, menu.height() // 2))

    def _arrow_under(self, menu: QMenu, local: QPoint) -> int:
        """-1 over the up arrow, +1 over the down arrow, 0 elsewhere."""
        up, down = scroll_state(menu)
        if not (up or down):
            return 0
        left, top, right, bottom = self.margins()
        if not (left <= local.x() < menu.width() - right):
            return 0
        band = scroller_height(menu)
        if up and top <= local.y() <= top + 1 + band:
            return -1
        if down and menu.height() - bottom - 1 - band <= local.y() < menu.height() - bottom:
            return 1
        return 0

    @staticmethod
    def _nudge(menu: QMenu, local: QPoint) -> None:
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(local),
                            QPointF(menu.mapToGlobal(local)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(menu, event)

    # -- placement ---------------------------------------------------------

    def _place(self, menu: QMenu) -> None:
        """Slide the window so the *panel* lands where Qt meant the popup.

        A menu-bar menu: the panel's corner goes where Qt put the window's
        (flush under the bar item). A flyout: Qt already lines the first row
        up with the parent's row (it counts our top margin), so only the
        horizontal margin is taken out, leaving the 2 px overlap.
        """
        left, top, right, _bottom = self.margins()
        if not (left or top or right):
            return
        self._placed = True
        pos = menu.pos()
        x, y = pos.x() - left, pos.y() - top
        if self.kind == KIND_FLYOUT:
            y = pos.y()
            parent = _parent_menu(menu)
            if parent is not None and pos.x() < parent.x():   # opens to the left
                x = pos.x() + right - 1
            else:
                x = pos.x() - left + 1
        self._placing = True
        try:
            menu.move(QPoint(x, y))
        finally:
            self._placing = False

    # -- painting ----------------------------------------------------------

    def _panel_path(self, panel: QRectF) -> QPainterPath:
        tl, tr, br, bl = (RADIUS * f for f in _RADII[self.kind])
        if not any(self.margins()):                  # opaque popup: square
            tl = tr = br = bl = 0
        path = QPainterPath()
        path.moveTo(panel.left() + tl, panel.top())
        path.lineTo(panel.right() - tr, panel.top())
        if tr:
            path.arcTo(panel.right() - 2 * tr, panel.top(), 2 * tr, 2 * tr, 90, -90)
        path.lineTo(panel.right(), panel.bottom() - br)
        if br:
            path.arcTo(panel.right() - 2 * br, panel.bottom() - 2 * br, 2 * br, 2 * br, 0, -90)
        path.lineTo(panel.left() + bl, panel.bottom())
        if bl:
            path.arcTo(panel.left(), panel.bottom() - 2 * bl, 2 * bl, 2 * bl, 270, -90)
        path.lineTo(panel.left(), panel.top() + tl)
        if tl:
            path.arcTo(panel.left(), panel.top(), 2 * tl, 2 * tl, 180, -90)
        path.closeSubpath()
        return path

    def _paint(self, menu: QMenu) -> None:
        left, top, right, bottom = self.margins()
        panel = QRectF(menu.rect()).adjusted(left, top, -right, -bottom)
        painter = QPainter(menu)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = self._panel_path(panel)
        painter.fillPath(path, _colour('background.raised', '#262a31'))
        painter.save()
        painter.setClipPath(path)
        base = menu.font()
        active = menu.activeAction()
        for action in menu.actions():
            if not action.isVisible() or isinstance(action, QWidgetAction):
                continue
            geometry = menu.actionGeometry(action)
            if geometry.isEmpty():
                continue
            row = QRectF(panel.left() + 1, geometry.top(),
                         panel.width() - 2, geometry.height())
            if action.isSeparator():
                line = QRectF(row.left(), row.center().y() - 0.5, row.width(), 1)
                painter.fillRect(line, _colour('border.default', '#3a3f48'))
                continue
            self._paint_row(painter, action, row, base, action is active)
        self._paint_scrollers(painter, menu, panel)
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_colour('border.default', '#3a3f48'), 1))
        painter.drawPath(self._panel_path(panel.adjusted(0.5, 0.5, -0.5, -0.5)))
        painter.end()

    def _paint_row(self, painter, action, row, base, is_active) -> None:
        sub = action.menu()
        enabled = action.isEnabled()
        open_group = sub is not None and sub.isVisible()
        hot = enabled and (is_active or open_group)
        if open_group:
            painter.fillRect(row, _colour('selection.open', '#33445f'))
        elif hot:
            painter.fillRect(row, _colour('selection.background', '#2f5ca8'))
        if not enabled:
            ink = _colour('disabled.foreground', '#6a707a')
        elif hot:
            # Plan 44 F1 (MF24): the theme's strong ink, not white: white on
            # the light selection (#d6e4ff) was 1.28:1.
            ink = _colour('foreground.strong', '#e6e8eb')
        else:
            ink = _colour('foreground.primary', '#d4d8de')
        dim = _colour('menu.shortcut.hover', '#cfe0ff') if hot \
            else _colour('foreground.muted', '#7d838c')

        # check or radio, in the 30 px gutter
        if action.isCheckable() and action.isChecked():
            centre = QPointF(row.left() + 12, row.center().y())
            painter.setPen(QPen(ink, 1.6, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            if self._is_radio(action):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(ink)
                painter.drawEllipse(QPointF(row.left() + 16, centre.y()), 3.2, 3.2)
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolyline([QPointF(centre.x() + 0.5, centre.y() + 0.5),
                                      QPointF(centre.x() + 3.5, centre.y() + 3.5),
                                      QPointF(centre.x() + 9, centre.y() - 3.5)])

        label, tail = split_text(action)
        right_edge = row.right() - (10 if sub is not None else 12)
        if sub is not None:                     # the ▸ arrow, then the summary
            arrow_x = row.right() - 10
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink if hot else _colour('foreground.muted', '#7d838c'))
            painter.drawPolygon([QPointF(arrow_x - 5, row.center().y() - 3.5),
                                 QPointF(arrow_x, row.center().y()),
                                 QPointF(arrow_x - 5, row.center().y() + 3.5)])
            right_edge = arrow_x - 5 - 10
        if tail:
            font = _px_font(base, SUMMARY_PX if sub is not None else SHORTCUT_PX)
            painter.setFont(font)
            painter.setPen(dim)
            width = QFontMetrics(font).horizontalAdvance(tail)
            painter.drawText(QRectF(right_edge - width - 2, row.top(), width + 4,
                                    row.height()),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             tail)
            right_edge -= width + 12

        font = _px_font(base, ITEM_PX)
        user_font = action.font()
        if user_font.resolveMask():                       # e.g. a font picker
            font = QFont(user_font)
            font.setPointSizeF(ITEM_PX * 0.75 + 0.5)
        painter.setFont(font)
        painter.setPen(ink)
        text_rect = QRectF(row.left() + 30, row.top(),
                           max(0.0, right_edge - row.left() - 30), row.height())
        elided = QFontMetrics(font).elidedText(
            label, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect,
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         elided)

    def _paint_scrollers(self, painter, menu, panel) -> None:
        """The up / down scroll arrows of a menu that scrolls inside itself."""
        up, down = scroll_state(menu)
        if not (up or down):
            return
        band = scroller_height(menu)
        fill = _colour('background.raised', '#262a31')
        line = _colour('border.default', '#3a3f48')
        ink = _colour('foreground.muted', '#7d838c')
        for show, top in ((up, panel.top() + 1),
                          (down, panel.bottom() - 1 - band)):
            if not show:
                continue
            rect = QRectF(panel.left() + 1, top, panel.width() - 2, band)
            painter.fillRect(rect, fill)
            edge_y = rect.bottom() - 1 if top < panel.center().y() else rect.top()
            painter.fillRect(QRectF(rect.left(), edge_y, rect.width(), 1), line)
            cx, cy = rect.center().x(), rect.center().y()
            sign = -1 if top < panel.center().y() else 1      # tip up / tip down
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            painter.drawPolygon([QPointF(cx - 4, cy - 2 * sign),
                                 QPointF(cx + 4, cy - 2 * sign),
                                 QPointF(cx, cy + 2 * sign)])

    @staticmethod
    def _is_radio(action) -> bool:
        if action.property(_RADIO_PROP):
            return True
        group = action.actionGroup()
        return group is not None and group.isExclusive()


class MenuScrollHost(QScrollArea):
    """A menu body that scrolls inside the room its menu has.

    CHL (DP-2661, M4). Qt scrolls a menu by whole actions and only when the
    menu is taller than the *screen*; a flyout capped to its window (View >
    Colour: one 508 px panel action) showed an arrow and could not move. The
    body now lives in this scroll area, which has no bars: a band of
    ``scroller_height`` px stays free above and below it while it can scroll
    (the menu paints its arrows there, ``scroll_state`` reads this host) and the
    menu follows the pointer over those bands (``_MenuPainter._follow_pointer``).
    The wheel scrolls it too.
    """

    #: px scrolled per poll (40 ms) while the pointer stands on an arrow.
    STEP_PX = 10

    def __init__(self, body: QWidget, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName('menuScrollHost')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet('QScrollArea#menuScrollHost, '
                           'QScrollArea#menuScrollHost > QWidget > QWidget '
                           '{ background: transparent; border: none; }')
        self.viewport().setAutoFillBackground(False)
        self.setWidget(body)
        self._room = 0
        self._band = SCROLLER_MIN_PX

    def setRoom(self, room: int, band: int = SCROLLER_MIN_PX) -> None:
        """Cap the host at ``room`` px (0: no cap)."""
        self._room = max(0, int(room))
        self._band = max(1, int(band))
        self._reserve()
        self.updateGeometry()

    def _body_height(self) -> int:
        body = self.widget()
        return body.sizeHint().height() if body is not None else 0

    def _scrolls(self) -> bool:
        return bool(self._room) and self._body_height() > self._room

    def _reserve(self) -> None:
        edge = self._band if self._scrolls() else 0
        self.setViewportMargins(0, edge, 0, edge)

    def sizeHint(self) -> QSize:
        body = self.widget()
        if body is None:
            return QSize()
        hint = body.sizeHint()
        height = min(hint.height(), self._room) if self._room else hint.height()
        return QSize(hint.width(), height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def state(self) -> tuple:
        """(can scroll up, can scroll down)."""
        if not self._scrolls():
            return (False, False)
        bar = self.verticalScrollBar()
        return (bar.value() > bar.minimum(), bar.value() < bar.maximum())

    def scroll_by(self, delta: int) -> bool:
        """Scroll ``delta`` px (negative: up); True when it moved."""
        if not delta:
            return False
        bar = self.verticalScrollBar()
        before = bar.value()
        bar.setValue(before + int(delta))
        return bar.value() != before

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reserve()


def fit_to_room(menu: QMenu, room: int) -> None:
    """Cap ``menu`` to ``room`` px tall: a body that is a :class:`MenuScrollHost`
    scrolls inside it, any other menu is only capped."""
    host = menu.findChild(MenuScrollHost)
    if host is None:
        menu.setMaximumHeight(room)
        return
    style = menu.style()
    margins = menu.contentsMargins()
    inset = (style.pixelMetric(QStyle.PixelMetric.PM_MenuPanelWidth, None, menu)
             + style.pixelMetric(QStyle.PixelMetric.PM_MenuVMargin, None, menu))
    chrome = margins.top() + margins.bottom() + 2 * inset
    menu.setMaximumHeight(16777215)
    host.setRoom(room - chrome, scroller_height(menu))
    menu.updateGeometry()
    menu.adjustSize()


def make_scrollable(menu: QMenu) -> QMenu:
    """Let ``menu`` scroll inside itself (``menu-scrollable``) when it is
    taller than the screen, with the themed scroll arrows.

    Qt creates a menu's scroller only on a style or font change that sees
    ``SH_Menu_Scrollable``. On a ThemedMenu in a themed app the menu's own
    sheet resolves that hint lazily, at polish time, after the change that
    would have armed it, so a long menu fell back to extra columns. Polishing
    first and then sending one style change arms the scroller.
    """
    from .status_colors import style_scrolling_menu
    style_scrolling_menu(menu)
    menu.ensurePolished()
    if menu.style().styleHint(QStyle.StyleHint.SH_Menu_Scrollable, None, menu):
        QApplication.sendEvent(menu, QEvent(QEvent.Type.StyleChange))
    return menu


def scroller_height(menu: QMenu) -> int:
    """Height of a scroll-arrow band (Qt's scroller metric, at least 12 px)."""
    metric = menu.style().pixelMetric(QStyle.PixelMetric.PM_MenuScrollerHeight,
                                      None, menu)
    return max(SCROLLER_MIN_PX, int(metric))


def scroll_state(menu: QMenu) -> tuple:
    """(can scroll up, can scroll down) of a menu that scrolls inside itself.

    Qt moves every ``actionGeometry`` by the scroll offset, so the menu can
    scroll up while its first row sits above its resting place, and down
    while its last row ends below the panel's inner bottom.
    """
    host = menu.findChild(MenuScrollHost)
    if host is not None:
        return host.state()
    if not menu.style().styleHint(QStyle.StyleHint.SH_Menu_Scrollable, None, menu):
        return (False, False)
    rows = [menu.actionGeometry(a) for a in menu.actions() if a.isVisible()]
    rows = [r for r in rows if not r.isEmpty()]
    if not rows:
        return (False, False)
    style = menu.style()
    inset = (style.pixelMetric(QStyle.PixelMetric.PM_MenuPanelWidth, None, menu)
             + style.pixelMetric(QStyle.PixelMetric.PM_MenuVMargin, None, menu))
    margins = menu.contentsMargins()
    rest_top = margins.top() + inset
    rest_bottom = menu.height() - margins.bottom() - inset
    first_top = min(r.top() for r in rows)
    last_bottom = max(r.top() + r.height() for r in rows)
    return (first_top < rest_top, last_bottom > rest_bottom)


def scroll_hot_points(menu: QMenu) -> dict:
    """Where the pointer must stand, in the menu's own coordinates, to be over
    the painted scroll arrows: ``{'up': QPoint, 'down': QPoint}``.

    The arrows are drawn inside the panel, which is inset by the shadow
    margins; a pointer at the window's bottom edge is over the shadow, not the
    arrow, and Qt does not scroll there (Plan 44 CHR1, M4: the live pass aimed
    at the window edge and read "hovering the arrow does not scroll").
    """
    flt = _FILTERS.get(menu)
    left, top, right, bottom = flt.margins() if flt is not None else (0, 0, 0, 0)
    band = scroller_height(menu)
    x = left + (menu.width() - left - right) // 2
    return {'up': QPoint(x, top + 1 + band // 2),
            'down': QPoint(x, menu.height() - bottom - 1 - band + band // 2)}


def _parent_menu(menu: QMenu):
    for owner in menu.menuAction().associatedObjects():
        if isinstance(owner, QMenu) and owner is not menu:
            return owner
    return None


# -- public API ---------------------------------------------------------

def themed(menu: QMenu, kind: str = KIND_FLYOUT) -> QMenu:
    """Dress an existing ``QMenu`` (menu-bar menu, flyout or popup) in the
    mockup's popup. Idempotent."""
    if menu in _FILTERS:
        _FILTERS[menu].kind = kind
        return menu
    painter = _MenuPainter(menu, kind)
    menu.installEventFilter(painter)          # first, so elevation's ring paints before it
    _FILTERS[menu] = painter
    menu.setFont(_px_font(menu.font(), ITEM_PX))
    menu.setMinimumWidth(250 if kind != KIND_FLYOUT else 270)
    menu.setProperty('foammeshThemedMenu', kind)
    if elevation.probe_translucency():
        painter.handle = elevation.elevate(menu, 'menu', paint_panel=False)
    return menu


class ThemedMenu(QMenu):
    """A ``QMenu`` that is already dressed. ``kind`` is ``'flyout'`` (the
    default), ``'bar'`` or ``'popup'``."""

    def __init__(self, title: str = '', parent=None, kind: str = KIND_FLYOUT):
        super().__init__(title, parent)
        themed(self, kind)


def filter_of(menu: QMenu):
    """The paint filter of a themed menu, or None (for tests)."""
    return _FILTERS.get(menu)


def heading(menu: QMenu, text: str) -> QWidgetAction:
    """Add a section heading to ``menu``: a disabled action holding a caps label.

    It is not a command: ``reachable_actions`` skips it, and the label has an
    accessible name and the static-text role.
    """
    action = QWidgetAction(menu)
    label = QLabel(text)
    label.setProperty(_HEADING_PROP, True)
    label.setProperty(_FIRST_PROP, not menu.actions())
    label.setFont(fonts.caps_font(10.5))
    label.setAccessibleName(text)
    label.setObjectName('menuCaps')  # a caps label, not a page heading (DP-219)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    action.setDefaultWidget(label)
    action.setEnabled(False)
    action.setProperty(_HEADING_PROP, True)
    action.setObjectName('menuHeading_' + re.sub(r'\W+', '_', text).strip('_'))
    menu.addAction(action)
    return action


def elide_summary(text: str, font: QFont | None = None) -> str:
    """``text`` cut to ``SUMMARY_MAX_PX`` with an ellipsis."""
    font = _px_font(font or QFont(), SUMMARY_PX)
    return QFontMetrics(font).elidedText(
        text, Qt.TextElideMode.ElideRight, SUMMARY_MAX_PX)


def set_summary(menu: QMenu, title: str, summary: str) -> None:
    """Show ``summary`` at the right of the group's row in its parent menu.

    The text is ``title TAB summary``, which Qt draws as the shortcut column;
    the painter shows it dim and small. The summary is never a key sequence.
    """
    action = menu.menuAction()
    wanted = title + ('\t' + elide_summary(summary, menu.font()) if summary else '')
    if action.text() != wanted:
        action.setText(wanted)
