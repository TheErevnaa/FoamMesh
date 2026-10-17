"""Apply configured System, Light, or Dark themes to a live QApplication."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal

from foammesh.support import gc_policy

from .palette import build_palette
from . import appearance
from .fonts import install_application_fonts
from .metrics import install_control_metrics, install_house_style
from .icons import clear_icon_cache
from .qss_renderer import render_qss
from . import window_frame
from .tokens import ThemeTokens, load_theme_tokens


#: Plan 44 MF0. The partials of the global style sheet, in cascade order. The
#: order is part of the contract: a later partial may override an earlier one.
QSS_PARTIALS = ('core', 'forms', 'views', 'chrome', 'ribbon', 'menus', 'sheet',
                'console', 'footer', 'dialogs', 'viewport')


def template_text(theme_directory: Path | None = None) -> str:
    """The whole style-sheet template: every partial, concatenated in order."""
    directory = Path(theme_directory) if theme_directory else (
        Path(__file__).parents[3] / 'resources' / 'theme')
    return ''.join((directory / 'qss' / f'{name}.qss.tmpl').read_text(encoding='utf-8')
                   for name in QSS_PARTIALS)


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

    def template_text(self) -> str:
        return template_text(self._directory)

    def set_mode(self, mode: ThemeMode | str):
        requested = ThemeMode(mode)
        self._mode = requested
        if self._persist is not None:
            self._persist(self._mode.value)
        self.apply()

    def _follow_scheme(self, resolved: str | None) -> None:
        """Plan 44 MF3. Tell Qt which scheme is in force, so the native parts
        (the caption's frame, a system menu, a file dialog) agree with the
        palette. System mode hands the choice back to the system."""
        hints = self._application.styleHints()
        try:
            if resolved is None:
                if hasattr(hints, 'unsetColorScheme'):
                    hints.unsetColorScheme()
            elif hasattr(hints, 'setColorScheme'):
                hints.setColorScheme(Qt.ColorScheme.Dark if resolved == 'dark'
                                     else Qt.ColorScheme.Light)
        except (AttributeError, RuntimeError):
            pass

    def apply(self):
        if self._applying:
            return
        self._applying = True
        if self._mode is ThemeMode.SYSTEM:
            # Read the system's own answer, not one this manager forced.
            self._follow_scheme(None)
        resolved = self._resolve_mode()
        # Installing a palette, a style or a style sheet re-polishes every live
        # widget inside one Qt loop, and the house style and the control
        # metrics are Python callbacks that loop calls.  Each callback may
        # allocate, and an allocation may start the cyclic collector, which
        # then destroys an unreachable widget -- and its C++ object -- while
        # Qt is still walking the widget list (heap corruption 0xc0000374 or
        # an access violation in the polish).  The unreachable widgets are
        # reaped here first, on the GUI thread and outside the walk, so the
        # walk has none left to lose (Plan 35 CR3: steered, never off).
        gc_policy.collect_full('theme')
        try:
            tokens = load_theme_tokens(self._directory / f'{resolved}.json')
            template = self.template_text()
            install_application_fonts(self._application)
            install_house_style(self._application)
            install_control_metrics(self._application)
            if self._mode is not ThemeMode.SYSTEM:
                self._follow_scheme(resolved)
            self._application.setPalette(build_palette(tokens))
            self._application.setStyleSheet(render_qss(template, tokens, appearance.current().density))
            clear_icon_cache()
            self._tokens = tokens
            self._resolved = resolved
            # After the palette: the caption takes the chrome token last.
            window_frame.apply_to_all(self._application, tokens, resolved)
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
