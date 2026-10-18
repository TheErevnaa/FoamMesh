"""Apply configured System, Light, or Dark themes to a live QApplication."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, Signal

from foammesh.support import gc_policy, heartbeat

from .palette import build_palette
from . import appearance
from .fonts import install_application_fonts
from .metrics import install_control_metrics, install_house_style
from .icons import clear_icon_cache
from .qss_renderer import render_qss
from .status_colors import dock_application_rules
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


def _key(widget) -> int:
    import shiboken6
    return shiboken6.getCppPointer(widget)[0]


def _key_safe(widget) -> int | None:
    try:
        return _key(widget)
    except (RuntimeError, TypeError, ValueError):
        return None


def _ancestors(widget) -> list:
    """*widget*'s parents, nearest first (a widget's parent is a widget)."""
    chain = []
    while widget.parent() is not None:
        chain.append(widget.parent())
        widget = chain[-1]
    return chain


def _depth(widget) -> int:
    return len(_ancestors(widget))


def _depth_safe(widget) -> int:
    try:
        return _depth(widget)
    except RuntimeError:
        return 0


def _repolish(widget) -> None:
    """What Qt's own re-polish does to one widget: its style polishes it
    again and it is told its style changed."""
    import shiboken6
    from PySide6.QtWidgets import QApplication, QWidget
    if not shiboken6.isValid(widget):
        return
    try:
        widget.style().polish(widget)
        QApplication.sendEvent(widget, QEvent(QEvent.Type.StyleChange))
        QWidget.update(widget)
    except RuntimeError:                                # deleted meanwhile
        pass


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
        #: Plan 45 W1: hidden widgets the switch left for their first show,
        #: see _install. C++ addresses only: a wrapper kept across the event
        #: loop can outlive its widget, and touching it then is an access
        #: violation.
        self._pending: set = set()
        self._order = None
        self._drain_armed = False
        self._drain_paused = False          # tests: only the show hook
        self._watching = False
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

    def _sheet_holders(self) -> list:
        """The outermost widgets that carry a style sheet of their own."""
        from PySide6.QtWidgets import QApplication
        sheeted = []
        for widget in QApplication.allWidgets():
            try:
                if widget.styleSheet():
                    sheeted.append(widget)
            except RuntimeError:                # C++ side already gone
                continue
        return [widget for widget in sheeted
                if not any(other is not widget and other.isAncestorOf(widget)
                           for other in sheeted)]

    def _install(self, tokens, sheet: str) -> None:
        """Plan 45 W1. Put the palette and the application sheet on.

        Replacing one application sheet with another makes Qt 6 re-polish
        every styled widget once per ancestor, and every widget under a widget
        holding a sheet of its own (the dock manager: 5,500 of 5,800 widgets
        live) once per ancestor again: 15-18 s live per switch. Yet only about
        150 of those widgets are on screen.

        So Qt's change-of-style path (the sheet taken off, the new one put
        on: one polish per widget) is shown only the widgets on screen that
        follow the application style. The others are kept out of it for the
        length of the two calls -- the hidden ones and those under a sheet
        holder -- and are polished here by the two steps Qt's own re-polish
        takes (``style().polish`` and a StyleChange event): those on screen
        at once, top down, and the hidden ones when they are first shown, or
        in short slices on the event loop, whichever comes first.
        """
        from PySide6.QtWidgets import QApplication, QWidget
        application = self._application
        application.setPalette(build_palette(tokens))
        previous = application.styleSheet()
        if previous == sheet:
            return
        if not previous:
            application.setStyleSheet(sheet)
            return
        polished_flag = Qt.WidgetAttribute.WA_WState_Polished
        set_style_flag = Qt.WidgetAttribute.WA_SetStyle
        held = set()
        for holder in self._sheet_holders():
            held.add(_key(holder))
            held.update(_key(child) for child in holder.findChildren(QWidget))
        now, later, unpolished, unstyled = [], [], [], []
        for widget in QApplication.allWidgets():
            try:
                if widget.windowType() == Qt.WindowType.Desktop:
                    continue
                key = _key(widget)
                shown = widget.isVisible()
                if shown and key not in held:
                    continue                            # Qt's own pass
                if widget.testAttribute(polished_flag):
                    widget.setAttribute(polished_flag, False)
                    unpolished.append(widget)
                elif not shown:
                    continue        # never polished: Qt polishes it on show
                if not widget.testAttribute(set_style_flag):
                    widget.setAttribute(set_style_flag, True)
                    unstyled.append(widget)
                (now if shown else later).append(widget)
            except RuntimeError:                        # C++ side already gone
                continue
        try:
            application.setStyleSheet('')
            application.setStyleSheet(sheet)
        finally:
            for widget in unpolished:
                widget.setAttribute(polished_flag, True)
            for widget in unstyled:
                widget.setAttribute(set_style_flag, False)
        for widget in sorted(now, key=_depth):
            _repolish(widget)
        self._defer(later)

    # -- the hidden widgets, polished when first shown ---------------------

    def _defer(self, widgets) -> None:
        for widget in widgets:
            self._pending.add(_key(widget))
        if not self._pending:
            return
        if not self._watching:
            self._application.installEventFilter(self)
            self._watching = True
        self._order = None
        self._arm_drain()

    def _arm_drain(self) -> None:
        """One slice on the event loop, once the events queued now are done."""
        from PySide6.QtCore import QTimer
        if self._drain_armed:
            return
        self._drain_armed = True
        QTimer.singleShot(0, self, self._drain_tick)

    def _drain_tick(self) -> None:
        self._drain_armed = False
        if self._pending and not self._drain_paused:
            self._drain_slice()

    def pending_polish(self) -> int:
        """How many hidden widgets still wait for the theme in force."""
        return len(self._pending)

    def finish_pending_polish(self) -> None:
        """Polish every widget still waiting."""
        while self._pending:
            self._drain_slice(budget=None)

    def _take(self, widget) -> None:
        """Polish *widget*'s waiting ancestors and its waiting subtree."""
        from PySide6.QtWidgets import QWidget
        top = widget
        for above in _ancestors(widget):
            if _key(above) in self._pending:
                top = above
        group = [top] + top.findChildren(QWidget)
        waiting = [each for each in group if _key(each) in self._pending]
        for each in sorted(waiting, key=_depth):
            self._pending.discard(_key(each))
            _repolish(each)
        self._settle()

    def _drain_slice(self, budget: float | None = 0.012) -> None:
        """Polish waiting widgets, top down, for about *budget* seconds.

        The widgets are looked up afresh among the live ones each slice:
        those deleted since the switch are dropped, never touched."""
        import time
        from PySide6.QtWidgets import QApplication
        live = {}
        for widget in QApplication.allWidgets():
            key = _key_safe(widget)
            if key in self._pending:
                live[key] = widget
        self._pending.intersection_update(live)
        if self._order is None:
            self._order = sorted(self._pending,
                                 key=lambda key: _depth_safe(live[key]))
            self._order.reverse()                       # pop() from the top
        started = time.perf_counter()
        while self._order:
            key = self._order.pop()
            if key not in self._pending:
                continue
            self._pending.discard(key)
            _repolish(live[key])
            if budget is not None and time.perf_counter() - started > budget:
                break
        if not self._order:
            self._pending.clear()
        self._settle()
        if self._pending:
            self._arm_drain()

    def _settle(self) -> None:
        if not self._pending and self._watching:
            self._application.removeEventFilter(self)
            self._watching = False
            self._order = None

    def eventFilter(self, watched, event):              # noqa: N802 - Qt API
        if (self._pending and event.type() == QEvent.Type.Show
                and _key_safe(watched) in self._pending):
            self._take(watched)
        return False

    def set_mode(self, mode: ThemeMode | str):
        requested = ThemeMode(mode)
        if requested is self._mode and self._resolved is not None:
            # Plan 45 W1. The mode in force is asked for again (the settings
            # combo re-selected, a probe, a restore): the theme on screen is
            # already that one, and re-applying it re-polished every widget
            # for nothing (light -> light measured 15.2 s live).
            return
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
        # Plan 45 W1/V12: a stall during the switch is named for what it is.
        operation = f'theme:{resolved}'
        previous_op = heartbeat.last_op()
        heartbeat.set_last_op(operation)
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
            sheet = (render_qss(self.template_text(), tokens, appearance.current().density)
                     + dock_application_rules(tokens))
            install_application_fonts(self._application)
            install_house_style(self._application)
            install_control_metrics(self._application)
            if self._mode is not ThemeMode.SYSTEM:
                self._follow_scheme(resolved)
            self._install(tokens, sheet)
            clear_icon_cache()
            self._tokens = tokens
            self._resolved = resolved
            # After the palette: the caption takes the chrome token last.
            window_frame.apply_to_all(self._application, tokens, resolved)
        finally:
            self._applying = False
        try:
            # Consumers are guaranteed to observe the fully installed palette/QSS.
            self.themeChanged.emit(resolved)
        finally:
            heartbeat.restore_last_op(operation, previous_op)

    def _resolve_mode(self) -> str:
        if self._mode is ThemeMode.LIGHT:
            return ThemeMode.LIGHT.value
        if self._mode is ThemeMode.DARK:
            return ThemeMode.DARK.value
        return ThemeMode.DARK.value if self._application.styleHints().colorScheme() is Qt.ColorScheme.Dark else ThemeMode.LIGHT.value

    def _onSystemSchemeChanged(self, _scheme):
        if self._mode is ThemeMode.SYSTEM:
            self.apply()
