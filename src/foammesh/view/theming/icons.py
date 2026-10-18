"""DPI-aware SVG recolouring for every Qt icon mode and state."""
from __future__ import annotations

import re

from PySide6.QtCore import QFile, QIODevice, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

#: Keyed by what the picture is drawn from. The mode and the state an icon
#: puts a pixmap under do not change the picture: Plan 45 start-up, MEASURED
#: live 18 October 2026, the window drew 274 SVG pictures at start where
#: 108 differ (each themed icon's Normal and Active cells, and its Off and
#: On cells, were drawn twice).
_PIXMAP_CACHE: dict[tuple[str, int, int, float, str], QPixmap] = {}
_ICON_CACHE: dict[tuple[str, int, int, float, tuple[str, ...]], QIcon] = {}


def clear_icon_cache() -> None:
    _PIXMAP_CACHE.clear()
    _ICON_CACHE.clear()


def load_themed_icon(path: str, size: QSize = QSize(256, 256)) -> QIcon:
    data = _read_all(path)
    if data is None or b'<svg' not in data:
        return QIcon(path)
    palette = QGuiApplication.palette()
    colors = (
        palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText).name(),
        palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText).name(),
        palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight).name(),
        palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText).name(),
    )
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    key = (path, size.width(), size.height(), dpr, colors)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    icon = QIcon()
    # Plan 45 V1. Active is the mode a raised tool button draws under the
    # pointer, and it used to take the Highlight colour -- the theme's
    # selection.background, the very ground a checked or pressed button is
    # filled with. A button just clicked (the pointer still on it) showed an
    # empty highlighted square, and an unchecked one under the pointer drew
    # its glyph at 1.1:1 on light, 2:1 on dark. Hover keeps the ink.
    for mode, color in ((QIcon.Mode.Normal, colors[0]),
                        (QIcon.Mode.Disabled, colors[1]),
                        (QIcon.Mode.Active, colors[0]),
                        (QIcon.Mode.Selected, colors[3])):
        for state in (QIcon.State.Off, QIcon.State.On):
            icon.addPixmap(_render(data, path, size, dpr, mode, state, color), mode, state)
    _ICON_CACHE[key] = icon
    return icon


def _render(data: bytes, path: str, size: QSize, dpr: float,
            mode: QIcon.Mode, state: QIcon.State, color: str) -> QPixmap:
    key = (path, size.width(), size.height(), dpr, color)
    if key in _PIXMAP_CACHE:
        return _PIXMAP_CACHE[key]
    text = data.decode('utf-8').replace('#000000', color).replace('#000', color)
    text = text.replace('currentColor', color)
    # DP-699. An <svg> that already names its fill keeps it: a second fill
    # attribute is a duplicate, the document is invalid, and the icon drew
    # as nothing (the blank show/hide eye on the viewport overlay).
    start = re.search(r'<svg\b[^>]*>', text)
    if start is not None and not re.search(r'\sfill\s*=', start.group(0)):
        text = text[:start.start() + 4] + f' fill="{color}"' + text[start.start() + 4:]
    renderer = QSvgRenderer(text.encode('utf-8'))
    pixmap = QPixmap(round(size.width() * dpr), round(size.height() * dpr))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    _PIXMAP_CACHE[key] = pixmap
    return pixmap


def _read_all(path: str) -> bytes | None:
    file = QFile(path)
    if not file.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        return bytes(file.readAll().data())
    finally:
        file.close()


# --------------------------------------------------------------------------- #
# Plan 44 MF13 / U2: icons tinted from the running theme's tokens.
#
# ``load_themed_icon`` reads the application palette once, when the button is
# built, so an icon made before the theme was applied (or kept across a theme
# switch) stayed black on a dark ground. A token icon is drawn from
# ``foreground.*`` and re-drawn on every theme change.
# --------------------------------------------------------------------------- #

import weakref

#: (state, mode) -> token used for the glyph
_NORMAL_TOKEN = 'foreground.strong'
_CHECKED_TOKEN = 'foreground.strong'
_DISABLED_TOKEN = 'disabled.foreground'
_FALLBACK = {'foreground.strong': '#e6e8eb', 'foreground.primary': '#d4d8de',
             'disabled.foreground': '#6b727c', 'overlay.edge': '#202328',
             'selection.background': '#2f5ca8', 'accent.soft': '#22406f',
             'accent.default': '#5b93f0', 'background.raised': '#262a31'}
_TINTED: 'list[tuple]' = []
_hooked = False


def theme_colour(name: str) -> str:
    """A token of the running theme as ``#rrggbb`` (a fallback without one)."""
    from .elevation import token
    value = token(name, None) or _FALLBACK.get(name, '#808080')
    return value[:7]


def _thicken(text: str, size: QSize, min_stroke_px: float) -> str:
    """DP-2723. Every stroke at least *min_stroke_px* on screen at *size*.

    The outline glyphs are drawn in a 512 viewBox with 20 to 32 unit strokes:
    0.6 to 1 px at 16 px, a hairline the rail's cut, section, display and
    inspector tools all but vanished in (measured live, 2026-10-16)."""
    box = re.search(r'viewBox\s*=\s*"\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)', text)
    if box is None or size.width() <= 0:
        return text
    least = min_stroke_px * float(box.group(1)) / size.width()

    def wider(match):
        width = float(match.group(2))
        return match.group(1) + ('%g' % max(width, least)) + match.group(3)

    text = re.sub(r'(stroke-width\s*:\s*)([\d.]+)(\s*(?:px)?)', wider, text)
    return re.sub(r'(stroke-width\s*=\s*")([\d.]+)(")', wider, text)


def tinted_icon(path: str, size: QSize = QSize(16, 16),
                min_stroke_px: float = 0.0) -> QIcon:
    """The SVG at *path* drawn in the theme's glyph colours.

    Black parts take ``foreground.strong``; the few white strokes take the
    ground of the button (``overlay.edge``, or the checked tile), so no glyph
    is black on a dark ground. Off, On, hover and disabled are all supplied.
    *min_stroke_px* widens any stroke thinner than that on screen.
    """
    data = _read_all(path)
    if data is None or b'<svg' not in data:
        return QIcon(path)
    tag = ''
    if min_stroke_px > 0:
        data = _thicken(data.decode('utf-8'), size, min_stroke_px).encode(
            'utf-8')
        tag = '|s%g' % min_stroke_px
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    icon = QIcon()
    off_ground = theme_colour('overlay.edge')
    on_ground = theme_colour('accent.soft')
    cells = (
        (QIcon.Mode.Normal, QIcon.State.Off, _NORMAL_TOKEN, off_ground),
        (QIcon.Mode.Active, QIcon.State.Off, _NORMAL_TOKEN, off_ground),
        (QIcon.Mode.Selected, QIcon.State.Off, _NORMAL_TOKEN, off_ground),
        (QIcon.Mode.Disabled, QIcon.State.Off, _DISABLED_TOKEN, off_ground),
        (QIcon.Mode.Normal, QIcon.State.On, _CHECKED_TOKEN, on_ground),
        (QIcon.Mode.Active, QIcon.State.On, _CHECKED_TOKEN, on_ground),
        (QIcon.Mode.Selected, QIcon.State.On, _CHECKED_TOKEN, on_ground),
        (QIcon.Mode.Disabled, QIcon.State.On, _DISABLED_TOKEN, on_ground),
    )
    for mode, state, name, ground in cells:
        colour = theme_colour(name)
        text = data.decode('utf-8')
        text = re.sub(r'#fff(?:fff)?\b', ground, text, flags=re.I)
        icon.addPixmap(_render(text.encode('utf-8'), path + '|' + ground + tag,
                               size, dpr, mode, state, colour), mode, state)
    return icon


def apply_tinted_icon(button, path: str, size: QSize = QSize(16, 16),
                      min_stroke_px: float = 0.0) -> None:
    """Give *button* the token icon at *path* and keep it in step with the
    theme (the icon is drawn again whenever the theme changes)."""
    button.setIconSize(size)
    button.setIcon(tinted_icon(path, size, min_stroke_px))
    _TINTED[:] = [r for r in _TINTED if r[0]() is not None
                  and r[0]() is not button]
    _TINTED.append((weakref.ref(button), path, QSize(size), min_stroke_px))
    _hook_theme()


def retint_icons() -> int:
    """Draw every token icon again from the current tokens."""
    clear_icon_cache()
    live = []
    for ref, path, size, stroke in _TINTED:
        button = ref()
        if button is None:
            continue
        try:
            button.setIcon(tinted_icon(path, size, stroke))
        except RuntimeError:                 # the C++ button is already gone
            continue
        live.append((ref, path, size, stroke))
    _TINTED[:] = live
    return len(live)


def _hook_theme() -> None:
    global _hooked
    if _hooked:
        return
    try:
        from foammesh.app import app
        manager = getattr(app, 'themeManager', None)
        if manager is not None:
            manager.themeChanged.connect(lambda *_: retint_icons())
            _hooked = True
    except Exception:  # noqa: BLE001 - tools and tests without the app
        pass
