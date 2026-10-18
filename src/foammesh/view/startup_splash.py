"""Plan 45 splash: something of FoamMesh on screen within about a second.

MEASURED live (plans/evidence/plan45/splash/before.json): nothing at all was on
screen for the first 3.8-4.0 s of a launch -- ~1.4 s of imports, then the
window built in stages, hidden. The user's decision (17 October 2026): show a
FoamMesh splash with the logo within about a second of launch while the window
builds; the main window looks the same once it appears.

The splash is a picture, not a control: a frameless window with the logo, the
application's name and version, and a "Starting" line, drawn in the light or
dark theme's own colours. It never takes part in "last window closed" (closing
it must not end the application), and :func:`show` gives back ``None`` -- no
splash, the start goes on -- for anything but a real ``QApplication``.

This module imports only Qt and the resource path helper: it is loaded before
the application's heavy modules, which is the whole point.
"""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from resources import resource

logger = logging.getLogger(__name__)

#: The splash's size in device-independent pixels.
WIDTH = 440
HEIGHT = 260
LOGO = 96

#: The theme tokens the splash draws with, and the values used should the
#: theme file be unreadable (they are the shipped light and dark values).
TOKENS = ('background.surface', 'border.default', 'foreground.primary',
          'foreground.secondary', 'foreground.muted', 'accent.default')
FALLBACK = {
    'light': {'background.surface': '#ffffff', 'border.default': '#d5d9e0',
              'foreground.primary': '#1b1f27', 'foreground.secondary': '#4a4f57',
              'foreground.muted': '#5d6573', 'accent.default': '#3b7dff'},
    'dark': {'background.surface': '#22252b', 'border.default': '#3a3f48',
             'foreground.primary': '#d4d8de', 'foreground.secondary': '#aab0b8',
             'foreground.muted': '#7d838c', 'accent.default': '#5b93f0'},
}


def theme_name(configured: str | None, application=None) -> str:
    """'light' or 'dark': the stored choice, else the system's scheme."""
    if configured in ('light', 'dark'):
        return configured
    try:
        hints = (application or QGuiApplication).styleHints()
        if hints.colorScheme() == Qt.ColorScheme.Dark:
            return 'dark'
    except Exception:                                       # noqa: BLE001
        pass
    return 'light'


def colours(theme: str) -> dict[str, str]:
    """The splash's colours from the shipped theme file, else the fallback."""
    chosen = dict(FALLBACK.get(theme, FALLBACK['light']))
    try:
        with open(resource.file(f'theme/{theme}.json'), encoding='utf-8') as f:
            tokens = json.load(f).get('tokens', {})
        chosen.update({k: tokens[k] for k in TOKENS if isinstance(tokens.get(k), str)})
    except Exception:                                       # noqa: BLE001
        logger.debug('splash: theme %s unreadable; using the fallback', theme,
                     exc_info=True)
    return chosen


def _read_logo(properties) -> QPixmap:
    return QPixmap(str(resource.file(properties.logoResource)))


class StartupSplash(QWidget):
    """The window shown while the main window is built. Draws, nothing else."""

    def __init__(self, properties, theme: str):
        super().__init__(None, Qt.WindowType.SplashScreen
                         | Qt.WindowType.FramelessWindowHint)
        # Closing the splash is never "the last window closed".
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setObjectName('startupSplash')
        self.setWindowTitle(properties.name)
        self.setAccessibleName(f'{properties.name} is starting')
        self.theme = theme
        self._colours = colours(theme)
        self._logo = _read_logo(properties)
        self._name = properties.name
        self._version = f'Version {properties.version}'
        self.setFixedSize(WIDTH, HEIGHT)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.x() + (area.width() - WIDTH) // 2,
                      area.y() + (area.height() - HEIGHT) // 2)

    def paintEvent(self, _event):
        c = self._colours
        with QPainter(self) as painter:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing
                                   | QPainter.RenderHint.SmoothPixmapTransform
                                   | QPainter.RenderHint.TextAntialiasing)
            painter.fillRect(self.rect(), QColor(c['background.surface']))
            # A thin accent band along the top, the border round the rest.
            painter.fillRect(0, 0, WIDTH, 3, QColor(c['accent.default']))
            painter.setPen(QColor(c['border.default']))
            painter.drawRect(0, 0, WIDTH - 1, HEIGHT - 1)
            if not self._logo.isNull():
                painter.drawPixmap(QRect((WIDTH - LOGO) // 2, 36, LOGO, LOGO),
                                   self._logo)
            font = painter.font()
            font.setPointSizeF(20.0)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(c['foreground.primary']))
            painter.drawText(QRect(0, 142, WIDTH, 36),
                             Qt.AlignmentFlag.AlignCenter, self._name)
            font.setPointSizeF(9.5)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(c['foreground.secondary']))
            painter.drawText(QRect(0, 178, WIDTH, 20),
                             Qt.AlignmentFlag.AlignCenter, self._version)
            painter.setPen(QColor(c['foreground.muted']))
            painter.drawText(QRect(16, HEIGHT - 32, WIDTH - 32, 20),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             'Starting\N{HORIZONTAL ELLIPSIS}')


def show(application, properties, configured_theme: str | None = None):
    """Show and paint the splash now; ``None`` when there is none to show.

    Never raises: a splash that cannot be made costs the splash, not the start.
    """
    if not isinstance(application, QApplication):
        return None
    try:
        return _present(StartupSplash(
            properties, theme_name(configured_theme, application)), application)
    except Exception:                                       # noqa: BLE001
        logger.warning('The start-up splash could not be shown', exc_info=True)
        return None


def _present(splash, application):
    splash.show()
    splash.raise_()
    # Painted now, before the caller goes on to the heavy imports.
    splash.repaint()
    application.processEvents()
    return splash


def close(splash) -> None:
    """Close the splash, if there is one; never raises."""
    if splash is None:
        return
    try:
        splash.close()
        splash.deleteLater()
    except Exception:                                       # noqa: BLE001
        logger.debug('closing the start-up splash failed', exc_info=True)
