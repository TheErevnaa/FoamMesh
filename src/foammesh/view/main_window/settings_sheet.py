#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 43 LY3: the settings sheet.

The settings column used to be a dock beside the viewport, always taking its
width from the model. The same column -- today's task page, its header, the
in-page editor, the Help / Back / Proceed footer and the footer's menu, all of
region B -- is now a *sheet*:

* **floating** (the default): a panel over the viewport at its left or right
  edge. It does not move the model, so the clear area is the whole viewport;
* **pinned**: the sheet is docked again, in Qt-ADS (still the docking engine,
  Plan 40 UXC3), beside the viewport on the chosen side, so it narrows the
  viewport.

Either way it is 300, 360 or 440 px wide, or any width from 280 to 560 px set
by dragging its grip; the width is remembered per page, and a table page
(Sizing, Periodic) opens at 440 px by itself. *Minimised* (Ctrl+\\) leaves a
32 px tab with the step's name at the edge.

Nothing inside the page changes: the column is the same ``regionBHost``
that was in the dock.

Plan 45 PINQ. The column never changes parent on a pin or an unpin. It lives
in the sheet's frame, a native child of the dock manager (the one widget
above both the viewport and the settings dock); the dock holds an empty slot
that takes the sheet's width beside the viewport. Floating, the frame floats
over the viewport with its rounded corners, edge lines and grip; pinned, it
lies flat over the slot, square, without the lines or the grip, in the dock's
own background. A change of parent re-polishes every widget under the one that
moved (Qt's ``inheritStyle`` runs whenever an application style sheet is set;
it has no "same style" shortcut): MEASURED 150-170 ms of the 0.40-0.46 s a pin
took live, with the 4,372 widgets of the column.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from functools import reduce

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QFontMetrics, QPainter, QPainterPath,
    QPen, QPolygonF, QRegion)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QMenu, QSizePolicy, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget)

from foammesh.settings.app_settings import SHEET_MAXIMUM, SHEET_MINIMUM
from foammesh.view.theming import motion
from foammesh.view.theming.metrics import RADIUS_FLOAT, RADIUS_SHEET
from .dock_layout import OUTLINE_DOCK, SETTINGS_DOCK, VIEWPORT_DOCK
from .sheet_header import SheetHeader

#: The three widths on offer in View > Settings sheet.
PRESETS = (300, 360, 440)
#: Where a table page (Sizing, Periodic) opens, whatever the preset.
TABLE_WIDTH = 440
#: At this window width or below the sheet opens at 300 px (Plan 43 section 1).
COMPACT_WINDOW = 1280
COMPACT_WIDTH = 300
STANDARD_WIDTH = 360
#: Plan 44 MF5. The minimised pill: 26 px wide, 10 px below the top edge, as
#: tall as its text plus 28 px.
TAB_WIDTH = 26
TAB_TOP = 10
TAB_PADDING = 28
GRIP_WIDTH = 7
#: Plan 44 MF5. The floating sheet's distance from the viewport's edges.
INSET = 10

MODES = ('floating', 'pinned')
SIDES = ('left', 'right')

#: A page whose class (or any base class) or object name contains one of these
#: is a table page.
TABLE_MARKERS = ('sizing', 'periodic', 'sizefield')

#: The events of the slot, the viewport host and the widgets between them
#: and the dock manager that move the frame (Plan 45 PINQ).
_FOLLOW = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
           QEvent.Type.Hide, QEvent.Type.ParentChange)

NOTE = ('The layout has changed. View → Standard layout puts it back; '
        'Pin docks the settings.')


def clamp_width(width: int) -> int:
    return min(max(int(width), SHEET_MINIMUM), SHEET_MAXIMUM)


def page_key(page: QWidget | None) -> str:
    """The name a page's width is remembered under."""
    if page is None:
        return ''
    return page.objectName() or type(page).__name__


def is_table_page(page: QWidget | None) -> bool:
    """Whether ``page`` is a table page (opens at 440 px unless told otherwise)."""
    if page is None:
        return False
    names = [page.objectName()] + [cls.__name__ for cls in type(page).__mro__]
    return any(marker in name.lower() for name in names for marker in TABLE_MARKERS)


@dataclass
class SheetState:
    """What the user chose, with no widgets in it."""

    mode: str = 'floating'
    side: str = 'left'
    #: The width chosen from the menu (a preset or a drag); ``None`` = standard.
    width: int | None = None
    #: Page key to the width the user gave that page.
    memory: dict = field(default_factory=dict)
    minimised: bool = False

    @classmethod
    def from_record(cls, record: dict | None) -> 'SheetState':
        record = dict(record or {})
        return cls(
            mode=record.get('mode') if record.get('mode') in MODES else 'floating',
            side=record.get('side') if record.get('side') in SIDES else 'left',
            width=record.get('width'),
            memory=(dict(record['memory'])
                    if isinstance(record.get('memory'), dict) else {}),
            minimised=bool(record.get('minimised', False)))

    def to_record(self) -> dict:
        return {'mode': self.mode, 'side': self.side, 'width': self.width,
                'memory': dict(self.memory), 'minimised': self.minimised}

    def base_width(self, window_width: int) -> int:
        if self.width is not None:
            return clamp_width(self.width)
        return COMPACT_WIDTH if window_width <= COMPACT_WINDOW else STANDARD_WIDTH

    def width_for(self, key: str, table: bool, window_width: int) -> int:
        """The width the page ``key`` opens at: its memory, else its kind's."""
        if key in self.memory:
            return clamp_width(self.memory[key])
        if table:
            return TABLE_WIDTH
        return self.base_width(window_width)

    def remember(self, key: str, width: int) -> int:
        width = clamp_width(width)
        if key:
            self.memory[key] = width
        return width

    def choose_width(self, key: str, width: int) -> int:
        """A width picked from the menu: it is the sheet's width from now on."""
        width = self.remember(key, width)
        self.width = width
        return width


def _token(name: str, fallback: str) -> str:
    """A colour of the theme in use (``fallback`` before one is installed)."""
    from foammesh.app import app
    manager = getattr(app, 'themeManager', None)
    tokens = manager.tokens if manager is not None else None
    if tokens is None or name not in tokens.values:
        return fallback
    return tokens.values[name]


def _elevate(widget: QWidget, level: str) -> str:
    """Give ``widget`` its elevation (Plan 44 MF1) and say which kind it got.

    ``'ring'`` is the painted shadow ring, ``'lines'`` the section 3 stand-in
    (a 1 px dark outer line and a 1 px light inner highlight). The sheet and
    the pill are native windows over the native GL viewport: a ring is a plain
    sibling that the GL window covers and a native sibling cannot blend, so
    ``probe_translucency`` answers False for their host and the stand-in is
    what shows. That is the decided outcome (section 3, Q10), not a gap.
    """
    from foammesh.view.theming.elevation import elevate
    return elevate(widget, level).mode


def _rounded_region(width: int, height: int, radii: tuple[float, float, float, float]
                    ) -> QRegion:
    """A region of ``width`` x ``height`` with the given corner radii
    (top-left, top-right, bottom-right, bottom-left): the mask of a native
    window, which cannot be translucent."""
    tl, tr, br, bl = (int(round(r)) for r in radii)

    def inset(radius: int, row: int) -> int:
        # Columns a quarter circle of ``radius`` leaves clear on the row
        # ``row`` rows from the edge (pixel centre row + .5). The polygon this
        # replaces rounded its arc points and measured 9, 8, 7 and 6 px for a
        # radius of 7 on its four corners (Plan 44 CHR1, S1).
        if radius <= 0 or row >= radius:
            return 0
        dy = radius - row - 0.5
        return int(round(radius - max(0.0, radius * radius - dy * dy) ** 0.5))

    path = QPainterPath()
    edge = max(tl, tr, br, bl)
    y = 0
    while y < height:
        if edge <= y < height - edge:
            # the straight middle is one rectangle; rows must not overlap
            # (overlapping subpaths cancel in a fill polygon)
            path.addRect(QRectF(0, y, width, height - edge - y))
            y = height - edge
            continue
        left = max(inset(tl, y), inset(bl, height - 1 - y))
        right = max(inset(tr, y), inset(br, height - 1 - y))
        path.addRect(QRectF(left, y, width - left - right, 1))
        y += 1
    # one polygon per row rectangle, joined (whole pixels, so exact)
    return reduce(QRegion.united,
                  (QRegion(polygon.toPolygon()) for polygon in path.toFillPolygons()))


class SheetFrame(QFrame):
    """The floating sheet: a native window with radius-7 corners.

    The viewport is a native GL window and a native sibling is always drawn
    over a plain one, so the frame has to be native too. A native window
    cannot be translucent, so the corners are a mask and the shadow is the
    ``elevation.elevate`` stand-in (a dark outer line and a light inner
    highlight; see ``_elevate``).
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._radius = RADIUS_SHEET
        # Native first (this makes the host native too), so the elevation
        # probe sees the surface it is dressing.
        self.winId()
        self._elevation = _elevate(self, 'sheet')

    def radius(self) -> int:
        return self._radius

    def set_radius(self, radius: int) -> None:
        self._radius = int(radius)
        self._remask()

    def elevation_mode(self) -> str:
        """``'ring'`` or ``'lines'`` (the section 3 stand-in, see ``_elevate``)."""
        return self._elevation

    def _remask(self) -> None:
        r = self._radius
        if r <= 0 or self.width() <= 2 * r or self.height() <= 2 * r:
            self.clearMask()
        else:
            self.setMask(_rounded_region(self.width(), self.height(), (r, r, r, r)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._remask()


class SheetGrip(QWidget):
    """The strip on the sheet's inner edge; drag to resize, right-click for more."""

    dragged = Signal(int)          # the width the pointer asks for
    menuRequested = Signal(QPoint)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName('settingsSheetGrip')
        self.setAccessibleName('Resize the settings sheet')
        self.setToolTip('Drag to resize the settings sheet (280 to 560 px). '
                        'Right-click for pin, side and width.')
        self.setFixedWidth(GRIP_WIDTH)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Expanding)
        self._side = 'left'
        self._down = False
        self._hover = False

    def set_side(self, side: str) -> None:
        self._side = side

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def wash_colour(self) -> QColor:
        """The accent at ``#5b93f055``, laid over the sheet's own surface (a
        child of a native window cannot blend with what is beneath it)."""
        from foammesh.view.theming.tokens import composite
        accent = _token('accent.default', '#5b93f0')[:7] + '55'
        return QColor(composite(accent, _token('background.surface', '#22252b')))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._hover or self._down:
            painter.fillRect(self.rect(), self.wash_colour())
        color = QColor(self.palette().color(self.foregroundRole()))
        color.setAlpha(70)
        x = 0 if self._side == 'right' else self.width() - 1
        painter.fillRect(x, 0, 1, self.height(), color)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.menuRequested.emit(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._down:
            return
        host = self.parentWidget().parentWidget()
        if host is None:
            return
        x = host.mapFromGlobal(event.globalPosition().toPoint()).x()
        edge = INSET if self.parentWidget().property('floating') else 0
        width = x - edge if self._side == 'left' else host.width() - x - edge
        self.dragged.emit(int(width))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._down = False
        self.update()


class SheetTab(QWidget):
    """The minimised sheet: a short blue pill at the edge, ``> N. Title``
    upright (reading downwards), 26 px wide.

    A native window like the sheet (it sits over the GL view); its free-side
    corners are rounded by a mask and the border is not drawn on the side
    that meets the edge.
    """

    clicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName('settingsSheetTab')
        self.setAccessibleName('Open the settings sheet')
        self.setToolTip('Open the settings sheet (Ctrl+\\)')
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setFixedWidth(TAB_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._text = ''
        self._side = 'left'
        self.winId()
        _elevate(self, 'chip')

    def text(self) -> str:
        return self._text

    def side(self) -> str:
        return self._side

    def set_side(self, side: str) -> None:
        if side != self._side:
            self._side = side
            self._remask()
            self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.setAccessibleDescription(text)
        self.setFixedHeight(self.wanted_height())
        self.update()

    def _font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(13)                          # 12.5 px in the mockup
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def label(self) -> str:
        return '▸ ' + (self._text or 'Settings')

    def wanted_height(self) -> int:
        """The text's length plus 28 px of padding (the mockup's ``14px 6px``)."""
        return QFontMetrics(self._font()).horizontalAdvance(self.label()) + TAB_PADDING

    def _remask(self) -> None:
        r = RADIUS_FLOAT
        if self.height() <= 2 * r:
            self.clearMask()
            return
        radii = (0, r, r, 0) if self._side == 'left' else (r, 0, 0, r)
        self.setMask(_rounded_region(self.width(), self.height(), radii))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._remask()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fill = QColor(_token('tab.pill.bg', '#1e3152'))
        border = QColor(_token('tab.pill.border', '#3f6fb8'))
        painter.fillRect(self.rect(), fill)
        painter.setPen(QPen(border, 1))
        w, h = self.width(), self.height()
        r = RADIUS_FLOAT
        # The border runs along the top, the bottom and the free side only.
        if self._side == 'left':
            path = QPainterPath()
            path.moveTo(0, 0.5)
            path.lineTo(w - r - 0.5, 0.5)
            path.arcTo(QRectF(w - 2 * r - 0.5, 0.5, 2 * r, 2 * r), 90, -90)
            path.lineTo(w - 0.5, h - r - 0.5)
            path.arcTo(QRectF(w - 2 * r - 0.5, h - 2 * r - 0.5, 2 * r, 2 * r), 0, -90)
            path.lineTo(0, h - 0.5)
        else:
            path = QPainterPath()
            path.moveTo(w, 0.5)
            path.lineTo(r + 0.5, 0.5)
            path.arcTo(QRectF(0.5, 0.5, 2 * r, 2 * r), 90, 90)
            path.lineTo(0.5, h - r - 0.5)
            path.arcTo(QRectF(0.5, h - 2 * r - 0.5, 2 * r, 2 * r), 180, 90)
            path.lineTo(w, h - 0.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(QColor(_token('accent.text', '#ffffff')))
        painter.setFont(self._font())
        painter.save()
        metrics = QFontMetrics(painter.font())
        # Plan 45 (user screenshot 2026-10-17: "not readable"). Turned a
        # quarter clockwise, the glyphs stand on the baseline towards +x, so
        # a baseline at w/2 + 4 put the letters from 17 to 29 px in a 26 px
        # tab and cut them off. The baseline sits so the drawn letters (not
        # the font's taller ascent) are centred across the tab.
        ink = metrics.tightBoundingRect(self.label())
        baseline = w / 2 + (ink.top() + ink.bottom()) / 2
        painter.translate(baseline, 14)
        painter.rotate(90)
        text = metrics.elidedText(self.label(), Qt.TextElideMode.ElideRight,
                                  max(h - TAB_PADDING, 20))
        painter.drawText(0, 0, text)
        painter.restore()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                           Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)


def _ancestors(start, stop):
    """``start`` and each parent above it, up to (not including) ``stop``."""
    if start is None or start is stop:
        return
    yield start
    yield from _ancestors(start.parentWidget(), stop)


class SettingsSheet(QObject):
    """Shows region B as the floating sheet or in the Qt-ADS settings dock."""

    changed = Signal()

    #: Plan 45 PINQ: a pin or an unpin leaves the column where it is (in the
    #: frame); only a dock the user floated out of the window takes it.
    COLUMN_STAYS = True

    def __init__(self, *, layout, region_b: QWidget, host: QWidget,
                 stack: QStackedWidget, store=None, step_name=None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._layout = layout
        self._b = region_b
        self._host = host
        self._stack = stack
        self._store = store
        self._step_name = step_name
        self._focus = False
        self._busy = False
        #: Plan 45 PINQ. Context factories held for the length of `_apply`.
        self._arrangement_holds: list = []
        #: What the last `_position` decided; ``None`` before the first.
        self._was_shown = None
        self._render_pending = False
        record = (getattr(store, 'getSettingsSheet', lambda: None)()
                  if store is not None else None)
        self.state = SheetState.from_record(record)

        # The sheet's page column may be as narrow as the grip allows.
        self._b.setMinimumWidth(SHEET_MINIMUM)
        self._layout.pin_area_minimums()

        # Plan 44 MF5. A native window: the viewport is one, and a plain child
        # of the same host is drawn under it (the sheet that could not be seen).
        # Plan 45 PINQ. A child of the dock manager, not of the viewport's
        # host: from there it can lie over the viewport (floating) and over
        # the settings dock's slot (pinned) without the column ever moving.
        self._frame = SheetFrame(layout.manager)
        self._frame.setObjectName('settingsSheet')
        self._frame.setAccessibleName('Settings sheet')
        self._frame.setProperty('floating', True)
        self._frame.setFrameShape(QFrame.Shape.NoFrame)
        self._box = QHBoxLayout(self._frame)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)
        self._holder = QWidget(self._frame)
        self._holder.setObjectName('settingsSheetHolder')
        self._holder_box = QVBoxLayout(self._holder)
        # 7 px under the footer, so no child reaches a rounded corner.
        self._holder_box.setContentsMargins(0, 0, 0, RADIUS_SHEET)
        # Plan 45 I. The dock holds a plain slot for the whole life of the
        # sheet, and the page column moves between the slot and the floating
        # holder. Taking the column out of the dock and into it again went
        # through a parentless step each way (Qt-ADS ``takeWidget`` and
        # ``setWidget``), and every parentless step re-polished all of the
        # column's widgets: MEASURED at 2.3 s to pin and 1.9 s to unpin.
        self._slot = QWidget()
        self._slot.setObjectName('settingsSheetSlot')
        self._slot_box = QVBoxLayout(self._slot)
        self._slot_box.setContentsMargins(0, 0, 0, 0)
        self._slot_box.setSpacing(0)
        self._slot.setMinimumWidth(SHEET_MINIMUM)
        self._grip = SheetGrip(self._frame)
        self._grip.dragged.connect(self._grip_dragged)
        self._grip.menuRequested.connect(self._popup_menu)
        self._tab = SheetTab(host)
        self._tab.clicked.connect(lambda: self.set_minimised(False))
        self._frame.hide()
        self._tab.hide()
        #: True while the frame wears the pinned look (flat over the slot).
        self._flat = False
        #: The widgets whose moves move the frame (see ``_watch``).
        self._chain: list = []
        self._follow_due = False
        self._slide_in = False

        # Plan 44 MF5. One header for every step. It sits at the head of
        # region B's own column, so it moves with the page between the frame
        # and the dock.
        self._header = SheetHeader(self._b)
        column = self._b.layout()
        if column is not None:
            column.insertWidget(0, self._header)
        self._header.pinRequested.connect(
            lambda pinned: self.set_mode('pinned' if pinned else 'floating'))
        self._header.sideRequested.connect(self._swap_side)
        self._header.minimiseRequested.connect(lambda: self.set_minimised(True))

        from foammesh.app import app as _app
        if getattr(_app, 'themeManager', None) is not None:
            _app.themeManager.themeChanged.connect(
                lambda _name: self._header.refresh_icons())

        # The one time the column leaves the dock the layout built for it.
        import PySide6QtAds as QtAds
        dock = self._layout.dock(SETTINGS_DOCK)
        if dock.widget() is self._b:
            dock.takeWidget()
        dock.setWidget(self._slot, QtAds.CDockWidget.ForceNoScrollArea)
        self._layout.pin_area_minimums()
        # Plan 45 PINQ. The column's one home from here on.
        self._holder_box.addWidget(self._b)
        top_level = getattr(dock, 'topLevelChanged', None)
        if top_level is not None:
            # a dock floated out of the window, or docked again
            top_level.connect(lambda *_a: self._apply())

        self._watch()
        stack.currentChanged.connect(self._page_changed)
        regions = getattr(layout, 'regions', None)
        if regions is not None:
            regions.moved.connect(self._pinned_drag)
        self._apply()
        self._sync_header_menu()

    # -- reading ------------------------------------------------------------ #

    @property
    def frame(self) -> QFrame:
        return self._frame

    @property
    def tab(self) -> SheetTab:
        return self._tab

    @property
    def grip(self) -> SheetGrip:
        return self._grip

    @property
    def header(self) -> SheetHeader:
        return self._header

    @property
    def mode(self) -> str:
        return self.state.mode

    @property
    def side(self) -> str:
        return self.state.side

    @property
    def is_floating(self) -> bool:
        return self.state.mode == 'floating'

    @property
    def is_minimised(self) -> bool:
        return self.state.minimised

    def shows_floating_sheet(self) -> bool:
        return (self.is_floating and not self.state.minimised
                and not self._focus)

    def _window_width(self) -> int:
        window = self._host.window()
        return window.width() if window is not None else self._host.width()

    def _page(self):
        return self._stack.currentWidget()

    def current_key(self) -> str:
        return page_key(self._page())

    def current_width(self) -> int:
        page = self._page()
        return self.state.width_for(
            page_key(page), is_table_page(page), self._window_width())

    def frame_width(self) -> int:
        """The width the floating sheet takes: the page's, and at least its own floor."""
        floor = max(self._b.minimumSizeHint().width(), SHEET_MINIMUM)
        return min(max(self.current_width(), floor), max(self._host.width() - 40, SHEET_MINIMUM))

    # -- choices ------------------------------------------------------------ #

    def _save(self) -> None:
        if self._store is not None:
            getattr(self._store, 'updateSettingsSheet', lambda _r: None)(
                self.state.to_record())

    def _commit(self) -> None:
        # Plan 45 PINQ. The sheet's record and the View menu's (written from
        # ``changed``) go to the settings file in one write.
        batched = getattr(self._store, 'batched', None)
        with batched() if callable(batched) else contextlib.nullcontext():
            self._save()
            self._apply()
            self.changed.emit()

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self.state.mode:
            self.state.mode = mode
            if mode == 'pinned':
                self.state.minimised = False
            self._commit()

    def _swap_side(self) -> None:
        self.set_side('right' if self.state.side == 'left' else 'left')

    def set_side(self, side: str) -> None:
        if side in SIDES and side != self.state.side:
            self.state.side = side
            self._commit()

    def set_width(self, width: int) -> None:
        """A preset from the menu: this page and the sheet's standard."""
        self.state.choose_width(self.current_key(), width)
        self._commit()

    def set_minimised(self, minimised: bool) -> None:
        minimised = bool(minimised)
        if minimised != self.state.minimised:
            self.state.minimised = minimised
            self._commit()

    def toggle_minimised(self) -> bool:
        self.set_minimised(not self.state.minimised)
        return self.state.minimised

    def reset(self) -> None:
        """View > Standard layout: floating, left, 360 px, nothing remembered."""
        self.state = SheetState()
        if self._store is not None:
            getattr(self._store, 'updateSettingsSheet', lambda _r: None)(None)
        self._apply()
        self.changed.emit()

    def set_focus_mode(self, on: bool) -> None:
        """Focus mode hides the sheet with the other panels; Esc brings it back."""
        self._focus = bool(on)
        if not on:
            self._apply()
        else:
            self._frame.hide()
            self._tab.hide()

    # -- the widgets -------------------------------------------------------- #

    def _in_dock(self) -> bool:
        """The column stands in the dock's place: pinned (the frame flat over
        the slot), or inside a dock the user floated out of the window."""
        return self._flat or self._b.parent() is self._slot

    def _over_slot(self) -> bool:
        """The frame lies over the dock's slot now (pinned, open, docked)."""
        return (self._flat and not self.state.minimised and not self._focus
                and self._b.parent() is self._holder
                and self._slot.isVisible()
                and self._layout.manager.isAncestorOf(self._slot))

    def _slot_rect(self) -> QRect:
        manager = self._layout.manager
        return QRect(self._slot.mapTo(manager, QPoint(0, 0)), self._slot.size())

    def _host_offset(self) -> QPoint:
        return self._host.mapTo(self._layout.manager, QPoint(0, 0))

    def _set_flat(self, flat: bool) -> None:
        """Plan 45 PINQ. The pinned look (square, no edge lines, no grip, the
        dock's background) or the floating one. Only the frame itself is
        re-polished; nothing under it changes parent."""
        flat = bool(flat)
        if flat == self._flat:
            return
        self._flat = flat
        frame = self._frame
        # Unpinned, the sheet slides in from its edge as it always has: from
        # beyond the edge, without a hide and a show of all of its widgets.
        self._slide_in = (not flat and frame.isVisible()
                          and motion.motion_enabled())
        self._restyle_frame(flat)
        frame.set_radius(0 if flat else RADIUS_SHEET)
        self._grip.setVisible(not flat)
        self._holder_box.setContentsMargins(0, 0, 0, 0 if flat else RADIUS_SHEET)
        for lines in frame.findChildren(QWidget, 'foammeshElevationLines'):
            lines.setVisible(not flat)

    def _restyle_frame(self, flat: bool) -> None:
        """The frame's own rule (``foammeshPinned`` in sheet.qss.tmpl); the
        frame alone is re-polished, not the column in it."""
        frame = self._frame
        frame.setProperty('foammeshPinned', flat)
        style = frame.style()
        style.unpolish(frame)
        style.polish(frame)
        frame.update()

    def _watch(self) -> None:
        """Follow every widget from the slot and from the viewport host up to
        the dock manager: a splitter drag, a re-dock or a closed dock moves
        or hides the place the frame has to cover."""
        manager = self._layout.manager
        chain = []
        for start in (self._slot, self._host):
            for widget in _ancestors(start, manager):
                if widget not in chain:
                    chain.append(widget)
        for widget in chain:
            if widget not in self._chain:
                widget.installEventFilter(self)
        for widget in self._chain:
            if widget not in chain:
                try:
                    widget.removeEventFilter(self)
                except RuntimeError:
                    pass
        self._chain = chain

    def _take_from_dock(self) -> None:
        if self._b.parent() is self._slot:
            self._slot_box.removeWidget(self._b)

    def _adopt_into_dock(self) -> None:
        if self._b.parent() is not self._slot:
            self._release_from_frame()
            self._slot_box.addWidget(self._b)
            self._b.show()

    def _adopt_into_frame(self) -> None:
        if self._b.parent() is not self._holder:
            self._take_from_dock()
            self._holder_box.addWidget(self._b)
            self._b.show()

    def _release_from_frame(self) -> None:
        # No ``setParent(None)``: a column with no parent is re-polished on
        # the way out and again on the way in.
        if self._b.parent() is self._holder:
            self._holder_box.removeWidget(self._b)

    def add_arrangement_hold(self, factory) -> None:
        """``factory()`` -- a context manager -- is entered for the whole of
        each arrangement: what it holds catches up once, at the end."""
        self._arrangement_holds.append(factory)

    def _apply(self) -> None:
        """Reconcile the widgets with ``self.state`` (idempotent)."""
        if self._busy:
            return
        with contextlib.ExitStack() as holds:
            for factory in tuple(self._arrangement_holds):
                holds.enter_context(factory())
            self._apply_held()

    def _apply_held(self) -> None:
        self._busy = True
        self._follow_due = False
        # Plan 45 PINQ. The frame's layout waits until the frame has its new
        # look and place: showing the grip, the holder's new margin and the
        # new geometry would each lay the whole column out again (a shown
        # child activates its parent's layout at once). MEASURED live: the
        # grip's show alone, 22-27 ms of an unpin.
        box = self._box
        box.setEnabled(False)
        try:
            try:
                self._reconcile()
            finally:
                self._busy = False
            if self._follow_due:
                # the slot or the host moved under the arrangement just made
                self._follow_due = False
                self._position()
        finally:
            box.setEnabled(True)
            box.invalidate()
            box.activate()

    def _reconcile(self) -> None:
        if self._focus:
            return
        # Plan 45 I. Opening or closing the dock announced a new arrangement
        # on its own, and the end of this method announced it again: every
        # region size, and the whole column under it, was laid out twice.
        # One announcement, once the widgets are where they belong.
        held = self._layout.blockSignals(True)
        try:
            self._reconcile_widgets()
        finally:
            self._layout.blockSignals(held)
        self._layout.arrangementChanged.emit()

    def _reconcile_widgets(self) -> None:
        state = self.state
        dock = self._layout.dock(SETTINGS_DOCK)
        pinned = state.mode == 'pinned'
        want_dock = pinned and not state.minimised
        # Plan 45 PINQ. Pinned or floating, the column stays in the frame;
        # pinned, the frame lies flat over the dock's empty slot. Only a dock
        # the user floated out of the window (a window of its own) takes the
        # column, the one move that still re-polishes it.
        if want_dock:
            if dock.isClosed():
                dock.toggleView(True)
            if dock.isFloating():
                self._adopt_into_dock()
            else:
                self._adopt_into_frame()
            self._place_dock()
            # the title bar off first, so the slot has its final height
            # when the frame is laid over it
            self._hide_dock_title_bar()
            self._slot.setMinimumHeight(self._b.minimumSizeHint().height())
        elif pinned:
            # Plan 45 I. Pinned and minimised: only the dock closes.
            if not dock.isClosed():
                dock.toggleView(False)
        else:
            if not dock.isClosed():
                dock.toggleView(False)
            self._adopt_into_frame()
        self._set_flat(pinned)
        self._watch()
        self._grip.set_side(state.side)
        self._tab.set_side(state.side)
        self._header.set_pinned(state.mode == 'pinned')
        self._header.set_side(state.side)
        self._order_grip()
        self._position()
        self._set_tab_text()
        self._hide_dock_title_bar()

    def _hide_dock_title_bar(self) -> None:
        """Plan 44 Q12: the pinned sheet has the sheet header and no ADS title
        bar, not even under View > Arrange panels."""
        dock = self._layout.dock(SETTINGS_DOCK)
        area = dock.dockAreaWidget() if dock is not None else None
        bar = area.titleBar() if area is not None else None
        if bar is None or not self._in_dock():
            return
        if getattr(self, '_barWatched', None) is not bar:
            self._barWatched = bar
            bar.installEventFilter(self)
        if area.dockWidgetsCount() <= 1 and not area.dockContainer().isFloating():
            bar.setVisible(False)

    def _order_grip(self) -> None:
        self._box.removeWidget(self._holder)
        self._box.removeWidget(self._grip)
        if self.state.side == 'left':
            self._box.addWidget(self._holder, 1)
            self._box.addWidget(self._grip, 0)
        else:
            self._box.addWidget(self._grip, 0)
            self._box.addWidget(self._holder, 1)

    def _side_of_dock(self) -> str | None:
        regions = self._layout.regions
        splitter = regions.main()
        if splitter is None:
            return None
        settings = regions._index(splitter, SETTINGS_DOCK)
        viewport = regions._index(splitter, VIEWPORT_DOCK)
        if settings < 0 or viewport < 0:
            return None
        return 'left' if settings < viewport else 'right'

    def _place_dock(self) -> None:
        import PySide6QtAds as QtAds

        dock = self._layout.dock(SETTINGS_DOCK)
        if dock.isFloating():
            return                       # the user floated the dock itself
        if self._side_of_dock() == self.state.side:
            return
        manager = self._layout.manager
        central = self._layout.dock(VIEWPORT_DOCK).dockAreaWidget()
        if central is None:
            return
        if self.state.side == 'right':
            manager.addDockWidget(QtAds.RightDockWidgetArea, dock, central)
        else:
            outline = self._layout.dock(OUTLINE_DOCK)
            area = None if outline.isClosed() else outline.dockAreaWidget()
            if area is not None:
                manager.addDockWidget(QtAds.RightDockWidgetArea, dock, area)
            else:
                manager.addDockWidget(QtAds.LeftDockWidgetArea, dock, central)

    def _position(self) -> None:
        """Place the floating sheet and the pill on their edge of the viewport.

        Floating, the sheet is inset 10 px from the top, the bottom and its
        edge; the pill hangs 10 px below the top edge, flush with its side.
        """
        host = self._host
        height = host.height()
        shown = self.shows_floating_sheet()
        was = self._was_shown
        self._was_shown = shown
        if self._over_slot():
            # Plan 45 PINQ. Pinned: flat over the dock's slot, laid over it
            # once the arrangement is made (``_apply``), not at the slot's
            # place before the splitter has sized it: one layout of the
            # column, not two.
            motion._stop(self._frame)
            if self._busy:
                self._follow_due = True
            else:
                self._frame.setGeometry(self._slot_rect())
                self._frame.show()
                self._frame.raise_()
        elif shown:
            width = self.frame_width()
            x = INSET if self.state.side == 'left' else host.width() - width - INSET
            rect = QRect(x, INSET, width, max(height - 2 * INSET, 0)).translated(
                self._host_offset())
            motion._stop(self._frame)             # a hide still running
            slide_in, self._slide_in = self._slide_in, False
            if slide_in or (was is False and not self._frame.isVisible()):
                # Plan 44 MF5: it slides in from beyond its edge (200 ms,
                # OutCubic; instant with motion off or Reduce motion on).
                self._frame.setGeometry(self._off_rect(rect))
                self._frame.show()
                self._frame.raise_()
                animation = motion.slide(self._frame, rect)
                if animation is not None:
                    animation.finished.connect(self._request_render)
            else:
                self._frame.setGeometry(rect)
                self._frame.show()
                self._frame.raise_()
        elif self._frame.isVisible():
            if was:
                self._slide_out()
            else:
                self._frame.hide()
        else:
            self._frame.hide()
        if self.state.minimised and not self._focus:
            x = 0 if self.state.side == 'left' else host.width() - TAB_WIDTH
            self._tab.set_side(self.state.side)
            self._tab.setGeometry(x, TAB_TOP, TAB_WIDTH, self._tab.wanted_height())
            self._tab.show()
            self._tab.raise_()
        else:
            self._tab.hide()
        self._request_render()

    def _request_render(self) -> None:
        """Ask the viewport to render once the sheet's placement has settled.

        The sheet and the pill are native windows over a native GL surface;
        after one moves, hides or finishes sliding, the GL surface keeps the
        old placement's pixels until it renders again. Requests coalesce on a
        0 ms single shot so a drag does not render on every step.
        """
        if self._render_pending:
            return
        self._render_pending = True
        # The sheet as the timer's context: a window closed with a render
        # still queued drops it, rather than the render reading a host that
        # is gone.
        QTimer.singleShot(0, self, self._render_viewport)

    def _render_viewport(self) -> None:
        self._render_pending = False
        try:
            window = self._host.window() if self._host is not None else None
        except RuntimeError:                     # the host was deleted first
            return
        view = getattr(window, 'renderingView', None)
        refresh = getattr(view, 'refresh', None)
        if callable(refresh):
            try:
                refresh()
            except Exception:                                    # noqa: BLE001
                pass

    def _off_rect(self, rect: QRect) -> QRect:
        """``rect`` moved past its own edge: translateX(-w - 20) in the mockup."""
        shift = rect.width() + 20 + INSET
        return rect.translated(-shift if self.state.side == 'left' else shift, 0)

    def _slide_out(self) -> None:
        frame = self._frame
        animation = motion.slide(frame, self._off_rect(frame.geometry()))
        if animation is None:
            frame.hide()
            self._request_render()
            return

        def done() -> None:
            if not self.shows_floating_sheet():
                frame.hide()
            self._request_render()
        animation.finished.connect(done)

    def apply_to_sizes(self, sizes: tuple[int, int, int]) -> None:
        """Make the splitter agree with the sheet after the shell set ``sizes``.

        The shell sizes three regions; the sheet is the middle one. Floating,
        it is not in the splitter, so what it would have taken goes to the
        viewport. Pinned, it takes its own width and the viewport the rest.
        """
        a, b, c = (int(value) for value in sizes)
        regions = self._layout.regions
        if self._in_dock() and not self._layout.dock(SETTINGS_DOCK).isClosed():
            width = max(self.current_width(), SHEET_MINIMUM)
            rest = a + b + c - a - width
            if rest < 420:           # the viewport keeps its own floor
                width = max(SHEET_MINIMUM, width - (420 - rest))
                rest = a + b + c - a - width
            regions.setSizes([a, width, rest])
        else:
            regions.setSizes([a, 0, b + c])

    # -- events -------------------------------------------------------------- #

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind in _FOLLOW and watched in self._chain:
            if kind == QEvent.Type.ParentChange:
                self._watch()            # a re-docked dock: a new chain
            if self._busy:
                self._follow_due = True
            else:
                self._position()
        elif (watched is getattr(self, '_barWatched', None)
              and event.type() == QEvent.Type.Show and self._in_dock()):
            # Plan 45 final pass (LIVEZ). Qt sends Show *before* it shows the
            # native window (show_helper: the event, then show_sys()). A hide
            # from in here left Qt saying "hidden" over a shown native bar
            # that took the header's presses (pin, minimise -> detach). Hide
            # it once the show has finished.
            QTimer.singleShot(0, self._hide_dock_title_bar)
        return False

    def _page_changed(self, *_args) -> None:
        self._set_tab_text()
        self._sync_header_menu()
        if self.shows_floating_sheet():
            self._position()
        elif self._in_dock():
            if self._over_slot():
                self._slot.setMinimumHeight(self._b.minimumSizeHint().height())
            self._layout.arrangementChanged.emit()

    def _set_tab_text(self) -> None:
        name = ''
        if self._step_name is not None:
            try:
                name = str(self._step_name() or '')
            except Exception:                      # a closing window
                name = ''
        if not name:
            page = self._page()
            name = (page.windowTitle() if page is not None else '') \
                or self.current_key()
        self._tab.set_text(name)
        self._header.set_step(name)
        if self.state.minimised and not self._focus:
            self._tab.setFixedHeight(self._tab.wanted_height())

    def _sync_header_menu(self) -> None:
        """Take the engine branch's ``...`` button into the header while the
        branch is the page on screen; hide it on the pages that have none."""
        page = self._page()
        release = getattr(page, 'releaseMoreMenuButton', None)
        button = release() if callable(release) else None
        if isinstance(button, QToolButton):
            self._header.adopt_more(button)
        else:
            self._header.adopt_more(None)

    def refresh_step_name(self) -> None:
        self._set_tab_text()

    def _grip_dragged(self, width: int) -> None:
        self.state.remember(self.current_key(), width)
        self._position()
        self._save()
        self.changed.emit()

    def _pinned_drag(self) -> None:
        """A drag of the splitter handle while pinned is a width the page keeps."""
        if not (self.state.mode == 'pinned' and self._in_dock()):
            return
        width = self._layout.regions.sizes()[1]
        if width > 0:
            self.state.remember(self.current_key(), width)
            self._save()

    # -- the View menu (LY8 owns the entries) ------------------------------- #

    def bind_view_state(self, view_state, view_menu=None) -> None:
        """Make View > Settings sheet (LY8, ``ViewMenuState`` keys ``sheet.*``)
        read by the sheet, and the sheet's own changes (a grip drag, the grip's
        menu, the tab) shown in the menu. The sheet keeps the per-page memory;
        the state holds mode, side and the chosen width."""
        self._view_state = view_state
        self._syncing = False
        self._push_to_state()      # the sheet's saved record is the truth
        view_state.subscribe(self._state_changed)
        self.changed.connect(self._push_to_state)
        if view_menu is not None:
            for value in PRESETS:
                entry = view_menu.entries.get(f'actionSheetWidth_{value}')
                action = entry[0] if entry is not None else None
                if action is not None:
                    # Choosing the width the state already holds sends no
                    # change, but it still means "this width, from now on".
                    action.triggered.connect(
                        lambda _c=False, v=value: self.set_width(v))

    def _state_changed(self, key: str) -> None:
        if self._syncing or not key.startswith('sheet.'):
            return
        if key == 'sheet.mode':
            mode = self._view_state.get('sheet.mode')
            self._syncing = True
            try:
                if mode == 'minimised':
                    self.set_minimised(True)
                else:
                    self.state.mode = mode
                    self.state.minimised = False
                    self._commit()
            finally:
                self._syncing = False
        elif key == 'sheet.side':
            self._syncing = True
            try:
                self.set_side(self._view_state.get('sheet.side'))
            finally:
                self._syncing = False
        elif key == 'sheet.width':
            self._syncing = True
            try:
                self.set_width(self._view_state.get('sheet.width'))
            finally:
                self._syncing = False

    def _push_to_state(self) -> None:
        view_state = getattr(self, '_view_state', None)
        if view_state is None or self._syncing:
            return
        self._syncing = True
        try:
            view_state.set('sheet.mode',
                           'minimised' if self.state.minimised
                           else self.state.mode)
            view_state.set('sheet.side', self.state.side)
            if self.state.width is not None:
                view_state.set('sheet.width', clamp_width(self.state.width))
            else:
                view_state.set('sheet.width', view_state.default('sheet.width'))
        finally:
            self._syncing = False

    @property
    def minimise_action(self):
        """LY8's Minimise sheet entry (Ctrl+backslash), the one that owns the key."""
        return getattr(self._host.window(), '_viewMenu').entries[
            'actionMinimiseSheet'][0]

    def _popup_menu(self, where: QPoint) -> None:
        menu = QMenu(self._frame)
        self._fill_popup(menu)
        menu.exec(where)

    def _fill_popup(self, menu: QMenu) -> None:
        menu.addAction('Pin (dock the settings)',
                       lambda: self.set_mode('pinned'))
        menu.addAction('Move to the other side',
                       lambda: self.set_side(
                           'right' if self.state.side == 'left' else 'left'))
        for value in PRESETS:
            menu.addAction(f'{value} px', lambda v=value: self.set_width(v))
        menu.addAction('Minimise', lambda: self.set_minimised(True))
