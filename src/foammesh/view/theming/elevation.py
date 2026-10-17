"""Plan 44 MF1: elevation (the mockup's box-shadow) without a graphics effect.

``QGraphicsDropShadowEffect`` renders its whole subtree offscreen, blurs the
text, blanks over a GL window and cannot sit on a top-level popup. So nothing
here ever installs a ``QGraphicsEffect`` on a content tree. Instead:

* **Top-level popups** (menus, popovers, the snapshot, the scale popover) are
  made frameless and translucent, and a pre-blurred 9-slice shadow ring is
  painted into a transparent margin that the window manager composites.
* **Child surfaces** (sheet, console card, minimised pill) get a sibling
  overlay behind them that paints the same ring into the host's margin.
* When a runtime probe says translucency cannot be trusted (software GL,
  remote desktop, a GL host that cannot blend, ``FOAMMESH_FLAT_ELEVATION``),
  the surface gets the Plan 43 section 11 fallback: a 1 px outer line in the
  theme's ``border.default`` and a 1 px light inner highlight.

Token names (MF0): ``shadow.menu|sheet|low|card|popover|snap``,
``border.default`` and the ``overlay.*`` grounds. They are read with fallbacks
so this module works before MF0 lands.
"""
from __future__ import annotations

import math
import os
import sys
import weakref

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

#: name -> (dx, dy, blur, shadow token, fallback shadow colour, radius metric, fallback radius)
LEVELS = {
    'menu': (0, 10, 30, 'shadow.menu', '#000000cc', 'RADIUS_MENU', 5),
    'sheet': (0, 12, 40, 'shadow.sheet', '#000000bb', 'RADIUS_SHEET', 7),
    'card': (0, -8, 24, 'shadow.low', '#00000066', 'RADIUS_FLOAT', 6),
    'chip': (0, 6, 20, 'shadow.popover', '#00000088', 'RADIUS_FLOAT', 6),
    'popover': (0, 8, 24, 'shadow.popover', '#00000088', 'RADIUS_MENU', 5),
    'snap': (0, 12, 32, 'shadow.snap', '#000000dd', 'RADIUS_FLOAT', 6),
}

#: The line-pair stand-in's colours when no theme is running. With a theme,
#: the outer line is the theme's ``border.default`` (opaque, so it is already
#: the section 3 pre-blend over the view): a fixed ``#00000099`` drew every
#: light frame black (Plan 44 MF24 item 6).
FALLBACK_OUTER = '#00000099'
FALLBACK_INNER = '#ffffff0d'
#: The theme token the outer line of the stand-in is drawn in.
OUTER_LINE_TOKEN = 'border.default'

_PROP = 'foammeshElevation'


# -- tokens, with fallbacks ------------------------------------------------

def parse_colour(text: str | None) -> QColor | None:
    """``#rrggbb`` or ``#rrggbbaa`` (CSS order; Qt's own parser reads the
    8-digit form as ``#aarrggbb``, which is not what the themes say)."""
    if not text or not text.startswith('#'):
        return None
    digits = text[1:]
    try:
        if len(digits) == 6:
            return QColor(int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        if len(digits) == 8:
            return QColor(int(digits[0:2], 16), int(digits[2:4], 16),
                          int(digits[4:6], 16), int(digits[6:8], 16))
    except ValueError:
        return None
    return None


def token(name: str, fallback: str | None = None) -> str | None:
    """The running theme's token ``name``, else ``fallback``."""
    try:
        from .patch_palette import live_tokens
        tokens = live_tokens()
        if tokens is not None:
            return tokens.value(name)
    except Exception:  # noqa: BLE001 - no theme, or a token MF0 has not added yet
        pass
    return fallback


def fallback_lines(tokens=None) -> tuple[str, str]:
    """(outer, inner) colours of the line-pair stand-in.

    The outer line is the theme's ``border.default``; ``tokens`` (a mapping of
    token values) is read instead of the running theme when given. The inner
    highlight stays the faint white lift, which vanishes on a white plate.
    """
    if tokens is not None:
        outer = tokens.get(OUTER_LINE_TOKEN) or FALLBACK_OUTER
    else:
        outer = token(OUTER_LINE_TOKEN, FALLBACK_OUTER) or FALLBACK_OUTER
    return outer, FALLBACK_INNER


def _metric(name: str, default: int) -> int:
    from . import metrics
    return int(getattr(metrics, name, default))


def level_spec(level: str):
    try:
        return LEVELS[level]
    except KeyError:
        raise ValueError(f'unknown elevation level: {level}') from None


def level_margins(level: str) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) the ring needs around the panel, in px."""
    dx, dy, blur = level_spec(level)[:3]
    return (max(0, blur - dx), max(0, blur - dy),
            max(0, blur + dx), max(0, blur + dy))


def level_radius(level: str) -> int:
    spec = level_spec(level)
    return _metric(spec[5], spec[6])


def shadow_colour(level: str) -> QColor:
    spec = level_spec(level)
    return parse_colour(token(spec[3], spec[4])) or parse_colour(spec[4])


# -- the runtime probe -----------------------------------------------------

def _remote_session() -> bool:
    if not sys.platform.startswith('win'):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))   # SM_REMOTESESSION
    except Exception:  # noqa: BLE001
        return False


def _software_gl() -> bool:
    return (os.environ.get('QT_OPENGL', '').lower() == 'software'
            or os.environ.get('QT_QUICK_BACKEND', '').lower() == 'software'
            or os.environ.get('LIBGL_ALWAYS_SOFTWARE', '') not in ('', '0'))


def probe_translucency(host: QWidget | None = None) -> bool:
    """Can a translucent surface be trusted here?

    False for: ``FOAMMESH_FLAT_ELEVATION``, a software GL or remote session, a
    platform without alpha windows, and a ``host`` that is (or sits inside) a
    GL or native-window surface that cannot blend a child with its pixels.
    """
    if os.environ.get('FOAMMESH_FLAT_ELEVATION', '') not in ('', '0'):
        return False
    if _software_gl() or _remote_session():
        return False
    if QGuiApplication.platformName() in ('minimal', 'vnc', 'eglfs', 'linuxfb'):
        return False
    widget = host
    while widget is not None:
        if widget.testAttribute(Qt.WidgetAttribute.WA_NativeWindow) and not widget.isWindow():
            return False
        if 'OpenGL' in type(widget).__name__ or 'VTK' in type(widget).__name__:
            return False
        widget = widget.parentWidget()
    return True


# -- the painted ring ------------------------------------------------------

_RING_CACHE: dict = {}


def _smoothstep(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def ring_pixmap(level: str, dpr: float = 1.0, colour: QColor | None = None,
                radius: int | None = None) -> QPixmap:
    """The cached, pre-blurred 9-slice source for ``level`` at ``dpr``.

    A rounded core fades outward over the level's blur width. Corner size is
    ``blur + radius`` px; one centre pixel is stretched.
    """
    blur = level_spec(level)[2]
    colour = colour or shadow_colour(level)
    radius = level_radius(level) if radius is None else radius
    key = (level, round(dpr, 2), colour.rgba(), radius)
    cached = _RING_CACHE.get(key)
    if cached is not None:
        return cached
    m = int(math.ceil(blur * dpr))
    r = int(math.ceil(radius * dpr))
    side = 2 * (m + r) + 2
    image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    painter.setPen(Qt.PenStyle.NoPen)
    core = QRectF(m, m, side - 2 * m, side - 2 * m)
    base = colour.alphaF()
    for i in range(m, -1, -1):             # outermost first, inner layers overwrite
        step = QColor(colour)
        step.setAlphaF(base * (1.0 - _smoothstep(i / m if m else 0.0)))
        painter.setBrush(step)
        painter.drawRoundedRect(core.adjusted(-i, -i, i, i), r + i, r + i)
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(1.0)
    if len(_RING_CACHE) > 64:
        _RING_CACHE.clear()
    _RING_CACHE[key] = pixmap
    return pixmap


def paint_ring(painter: QPainter, level: str, panel: QRectF, dpr: float = 1.0) -> None:
    """Paint ``level``'s shadow ring for a panel occupying ``panel``."""
    dx, dy, blur = level_spec(level)[:3]
    pixmap = ring_pixmap(level, dpr)
    corner = blur + level_radius(level)                   # logical px
    shadow = panel.translated(dx, dy)
    outer = shadow.adjusted(-blur, -blur, blur, blur)
    side = pixmap.width()                                  # device px
    cs = min(corner * dpr, side / 2 - 1)
    xs = (0.0, cs, side - cs, float(side))
    ox = (outer.left(), outer.left() + corner, outer.right() - corner, outer.right())
    oy = (outer.top(), outer.top() + corner, outer.bottom() - corner, outer.bottom())
    if ox[2] < ox[1] or oy[2] < oy[1]:                     # panel smaller than two corners
        return
    painter.save()
    for row in range(3):
        for col in range(3):
            target = QRectF(ox[col], oy[row], ox[col + 1] - ox[col], oy[row + 1] - oy[row])
            source = QRectF(xs[col], xs[row], xs[col + 1] - xs[col], xs[row + 1] - xs[row])
            if target.width() > 0 and target.height() > 0:
                painter.drawPixmap(target, pixmap, source)
    painter.restore()


# -- the handle ------------------------------------------------------------

class Elevation:
    """What ``elevate`` did to a widget. ``mode`` is ``'window'`` (translucent
    top-level), ``'ring'`` (sibling ring behind a child) or ``'lines'`` (the
    fallback line pair)."""

    def __init__(self, widget, level, mode, margins=(0, 0, 0, 0)):
        self._widget = weakref.ref(widget)
        self.level = level
        self.mode = mode
        self.margins = margins
        self._parts: list = []
        self._restore: list = []

    @property
    def widget(self):
        return self._widget()

    def remove(self) -> None:
        for part in self._parts:
            try:
                part.deleteLater()
            except RuntimeError:
                pass
        self._parts.clear()
        for undo in self._restore:
            try:
                undo()
            except RuntimeError:
                pass
        self._restore.clear()
        widget = self.widget
        if widget is not None:
            try:
                widget.setProperty(_PROP, None)
            except RuntimeError:
                pass
            _HANDLES.pop(widget, None)


_HANDLES: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()


def current(widget) -> Elevation | None:
    return _HANDLES.get(widget)


class _WindowRing(QObject):
    """Event filter: paints the ring, then the rounded panel, under a
    translucent top-level before its own paint runs."""

    def __init__(self, widget, level, paint_panel):
        super().__init__(widget)
        self._level = level
        self._panel = paint_panel
        self._margins = level_margins(level)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint and watched.isWidgetType():
            left, top, right, bottom = self._margins
            panel = QRectF(watched.rect()).adjusted(left, top, -right, -bottom)
            painter = QPainter(watched)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            paint_ring(painter, self._level, panel, watched.devicePixelRatioF())
            if self._panel:
                fill = parse_colour(token('overlay.popover', None)) \
                    or watched.palette().window().color()
                edge = parse_colour(token('border.default', None)) \
                    or watched.palette().mid().color()
                painter.setBrush(fill)
                painter.setPen(edge)
                radius = level_radius(self._level)
                painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            painter.end()
        return False


class _RingBehind(QWidget):
    """A transparent sibling stacked under ``target`` that paints its ring."""

    def __init__(self, target: QWidget, level: str):
        super().__init__(target.parentWidget())
        self._target = target
        self._level = level
        self._m = level_margins(level)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setObjectName('foammeshElevationRing')
        target.installEventFilter(self)
        self.sync()

    def sync(self):
        target = self._target
        left, top, right, bottom = self._m
        self.setGeometry(target.geometry().adjusted(-left, -top, right, bottom))
        self.stackUnder(target)
        self.setVisible(target.isVisible())

    def eventFilter(self, watched, event):
        if watched is self._target and event.type() in (
                QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
                QEvent.Type.Hide, QEvent.Type.ZOrderChange, QEvent.Type.ParentChange):
            self.sync()
        return False

    def paintEvent(self, _event):
        left, top, right, bottom = self._m
        panel = QRectF(self.rect()).adjusted(left, top, -right, -bottom)
        painter = QPainter(self)
        paint_ring(painter, self._level, panel, self.devicePixelRatioF())
        painter.end()


class _EdgeLines(QWidget):
    """The fallback: a 1 px ``border.default`` outer line and a 1 px light inner highlight,
    drawn at the target's own edge by a click-through child."""

    def __init__(self, target: QWidget, level: str):
        super().__init__(target)
        self._level = level
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setObjectName('foammeshElevationLines')
        target.installEventFilter(self)
        self.sync()

    def sync(self):
        self.setGeometry(self.parentWidget().rect())
        self.raise_()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (
                QEvent.Type.Resize, QEvent.Type.ChildAdded, QEvent.Type.Show):
            self.sync()
        return False

    def paintEvent(self, _event):
        radius = level_radius(self._level)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        outer, inner = fallback_lines()
        painter.setPen(parse_colour(outer) or parse_colour(FALLBACK_OUTER))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(parse_colour(inner))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1),
                                max(0, radius - 1), max(0, radius - 1))
        painter.end()


def elevate(widget: QWidget, level: str, *, paint_panel: bool = True) -> Elevation:
    """Give ``widget`` the shadow of ``level`` and return the handle.

    Idempotent: elevating again replaces the first elevation. Never installs a
    graphics effect.

    * A top-level ``widget`` becomes frameless and translucent, its contents
      margins grow by the ring width, and the ring (plus, with
      ``paint_panel``, the rounded panel) is painted under its own paint.
      Call it before ``show()`` (a visible window is re-shown).
    * A child ``widget`` gets a ring sibling behind it, so the *host* must
      leave the margin free; or, when ``probe_translucency(widget.parentWidget())``
      fails, the line-pair fallback inside the widget's own edge.
    """
    level_spec(level)
    previous = _HANDLES.get(widget)
    if previous is not None:
        previous.remove()
    if widget.isWindow():
        return _elevate_window(widget, level, paint_panel)
    if probe_translucency(widget.parentWidget()):
        handle = Elevation(widget, level, 'ring', level_margins(level))
        handle._parts.append(_RingBehind(widget, level))
    else:
        handle = Elevation(widget, level, 'lines')
        handle._parts.append(_EdgeLines(widget, level))
    _HANDLES[widget] = handle
    widget.setProperty(_PROP, level)
    return handle


def _elevate_window(widget, level, paint_panel):
    if not probe_translucency():
        handle = Elevation(widget, level, 'lines')
        handle._parts.append(_EdgeLines(widget, level))
        _HANDLES[widget] = handle
        widget.setProperty(_PROP, level)
        return handle
    margins = level_margins(level)
    handle = Elevation(widget, level, 'window', margins)
    was_visible = widget.isVisible()
    old_margins = widget.contentsMargins()
    old_flags = widget.windowFlags()
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    widget.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.setContentsMargins(
        old_margins.left() + margins[0], old_margins.top() + margins[1],
        old_margins.right() + margins[2], old_margins.bottom() + margins[3])
    filt = _WindowRing(widget, level, paint_panel)
    widget.installEventFilter(filt)
    handle._parts.append(filt)

    def undo():
        widget.removeEventFilter(filt)
        widget.setContentsMargins(old_margins)
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        widget.setWindowFlags(old_flags)

    handle._restore.append(undo)
    if was_visible:
        widget.show()
    _HANDLES[widget] = handle
    widget.setProperty(_PROP, level)
    return handle
