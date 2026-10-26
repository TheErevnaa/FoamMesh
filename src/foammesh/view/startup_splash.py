"""Plan 45 splash: something of FoamMesh on screen within about a second.

MEASURED live (plans/evidence/plan45/splash/before.json): nothing at all was on
screen for the first 3.8-4.0 s of a launch -- ~1.4 s of imports, then the
window built in stages, hidden. The user's decision (17 October 2026): show a
FoamMesh splash with the logo within about a second of launch while the window
builds; the main window looks the same once it appears.

The splash is a picture, not a control: a frameless window with the branded
artwork (resources/branding/foammesh_splash.png, made by scripts/make_splash.py),
the application's name and version and a "Starting" line painted over it. It never takes part in "last window closed" (closing
it must not end the application), and :func:`show` gives back ``None`` -- no
splash, the start goes on -- for anything but a real ``QApplication``.

This module imports only Qt and the resource path helper: it is loaded before
the application's heavy modules, which is the whole point.
"""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QRect, Qt, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QPainter,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QWidget

from resources import resource

logger = logging.getLogger(__name__)

#: The splash's size in device-independent pixels.
WIDTH = 800
HEIGHT = 400
#: The Erevnaa mark beside "Developed by", and where it leads.
MARK = 40
#: The centre line of the credit block.
CREDIT_X = 690
LINK = 'https://www.erevnaa.com'
LINK_TEXT = 'www.erevnaa.com'
NAVY, BLUE, MUTED = '#013684', '#018BB6', '#5a6878'
#: Where the name and the version sit on the artwork
#: (scripts/make_splash.py draws the rest).
NAME_X = 250

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
        self._art = QPixmap(str(resource.file('branding/foammesh_splash.png')))
        self._mark = QPixmap(str(resource.file('branding/erevnaa_mark.png')))
        self._link = QRect()
        self.setMouseTracking(True)
        self._name = properties.name
        self._version = f'Version {properties.version}'
        self.setFixedSize(WIDTH, HEIGHT)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.x() + (area.width() - WIDTH) // 2,
                      area.y() + (area.height() - HEIGHT) // 2)

    def paintEvent(self, _event):
        with QPainter(self) as painter:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing
                                   | QPainter.RenderHint.SmoothPixmapTransform
                                   | QPainter.RenderHint.TextAntialiasing)
            if self._art.isNull():
                painter.fillRect(self.rect(), QColor('#f4f6f9'))
            else:
                painter.drawPixmap(self.rect(), self._art)
            self._link = paint_text(painter, self._name, self._version,
                                    self._mark, None)

    def mouseMoveEvent(self, event):
        over = self._link.contains(event.position().toPoint())
        self.setCursor(Qt.CursorShape.PointingHandCursor if over
                       else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if self._link.contains(event.position().toPoint()):
            QDesktopServices.openUrl(QUrl(LINK))


def paint_text(painter, name, version, mark, family) -> QRect:
    """The name, version, "Starting..." and the credit; the link's rectangle.

    Shared with ``scripts/make_splash.py --preview`` so the review copy is the
    splash as it is drawn.
    """
    font = painter.font()
    if family:
        font.setFamily(family)
    font.setPointSizeF(34.0)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(NAVY))
    painter.drawText(QRect(NAME_X, 108, 520, 60),
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                     name)
    font.setPointSizeF(12.0)
    font.setBold(False)
    painter.setFont(font)
    painter.setPen(QColor(BLUE))
    painter.drawText(QRect(NAME_X, 170, 520, 28),
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                     version)
    font.setPointSizeF(9.5)
    painter.setFont(font)
    painter.setPen(QColor(MUTED))
    painter.drawText(QRect(40, 348, 300, 28),
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                     'Starting…')
    # Credit: one centred block in the lower right, on three lines --
    #   Developed by / <Erevnaa mark> / www.erevnaa.com
    cx = CREDIT_X
    lead = 'Developed by'
    font.setPointSizeF(9.0)
    painter.setFont(font)
    lead_w = painter.fontMetrics().horizontalAdvance(lead)
    painter.setPen(QColor(MUTED))
    painter.drawText(QRect(cx - lead_w // 2 - 2, 300, lead_w + 4, 20),
                     Qt.AlignmentFlag.AlignCenter, lead)
    mark_rect = QRect(cx - MARK // 2, 322, MARK, MARK)
    if mark is not None and not mark.isNull():
        painter.drawPixmap(mark_rect, mark)
    font.setPointSizeF(9.5)
    font.setUnderline(True)
    painter.setFont(font)
    url_w = painter.fontMetrics().horizontalAdvance(LINK_TEXT)
    link = QRect(cx - url_w // 2, 322 + MARK + 3, url_w, 22)
    painter.setPen(QColor(BLUE))
    painter.drawText(link, Qt.AlignmentFlag.AlignCenter, LINK_TEXT)
    font.setUnderline(False)
    painter.setFont(font)
    # The mark is part of the link too.
    return link.united(mark_rect)


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
