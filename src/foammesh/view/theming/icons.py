"""DPI-aware SVG recolouring for every Qt icon mode and state."""
from __future__ import annotations

import re

from PySide6.QtCore import QFile, QIODevice, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PIXMAP_CACHE: dict[tuple[str, int, int, float, int, int, str], QPixmap] = {}
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
    for mode, color in ((QIcon.Mode.Normal, colors[0]),
                        (QIcon.Mode.Disabled, colors[1]),
                        (QIcon.Mode.Active, colors[2]),
                        (QIcon.Mode.Selected, colors[3])):
        for state in (QIcon.State.Off, QIcon.State.On):
            icon.addPixmap(_render(data, path, size, dpr, mode, state, color), mode, state)
    _ICON_CACHE[key] = icon
    return icon


def _render(data: bytes, path: str, size: QSize, dpr: float,
            mode: QIcon.Mode, state: QIcon.State, color: str) -> QPixmap:
    key = (path, size.width(), size.height(), dpr, int(mode.value), int(state.value), color)
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
