"""Apply configured System, Light, or Dark themes to a live QApplication."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal

from .palette import build_palette
from .fonts import install_application_fonts
from .icons import clear_icon_cache
from .qss_renderer import render_qss
from .tokens import ThemeTokens, load_theme_tokens


class ThemeMode(str, Enum):
    SYSTEM = 'system'
    LIGHT = 'light'
    DARK = 'dark'


class ThemeManager(QObject):
    themeChanged = Signal(str)

    def __init__(self, application, *, configured_mode: ThemeMode = ThemeMode.SYSTEM,
                 persist=None, theme_directory: Path | None = None):
        super().__init__()
        self._application = application
        self._persist = persist
        self._directory = theme_directory or Path(__file__).parents[3] / 'resources' / 'theme'
        self._mode = ThemeMode(configured_mode)
        self._resolved = None
        self._tokens = None
        self._applying = False
        hints = application.styleHints()
        if hasattr(hints, 'colorSchemeChanged'):
            hints.colorSchemeChanged.connect(self._onSystemSchemeChanged)

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def resolved_name(self) -> str | None:
        return self._resolved

    @property
    def tokens(self) -> ThemeTokens | None:
        return self._tokens

    def set_mode(self, mode: ThemeMode | str):
        requested = ThemeMode(mode)
        self._mode = requested
        if self._persist is not None:
            self._persist(self._mode.value)
        self.apply()

    def apply(self):
        if self._applying:
            return
        self._applying = True
        resolved = self._resolve_mode()
        try:
            tokens = load_theme_tokens(self._directory / f'{resolved}.json')
            template = (self._directory / 'base.qss.tmpl').read_text(encoding='utf-8')
            install_application_fonts(self._application)
            self._application.setPalette(build_palette(tokens))
            self._application.setStyleSheet(render_qss(template, tokens))
            clear_icon_cache()
            self._tokens = tokens
            self._resolved = resolved
        finally:
            self._applying = False
        # Consumers are guaranteed to observe the fully installed palette/QSS.
        self.themeChanged.emit(resolved)

    def _resolve_mode(self) -> str:
        if self._mode is ThemeMode.LIGHT:
            return ThemeMode.LIGHT.value
        if self._mode is ThemeMode.DARK:
            return ThemeMode.DARK.value
        return ThemeMode.DARK.value if self._application.styleHints().colorScheme() is Qt.ColorScheme.Dark else ThemeMode.LIGHT.value

    def _onSystemSchemeChanged(self, _scheme):
        if self._mode is ThemeMode.SYSTEM:
            self.apply()
