"""Plan 43 LY4: the console -- the output band's size, width and minimised state.

The console is today's output band (Log, Console, Mesh quality, Live run,
Captures, History ...). This module owns what Plan 43 section 1 says about it:

* height 100, 144 or 420 px, plus a top grip from 64 to 640 px;
* width up to the right edge, 900 or 640 px, plus a side grip;
* minimised to a 28 px header (Ctrl+`);
* opened when a run starts (a choice), and the Mesh quality tab opens at 280 px
  and folds back to the console's own height when the reader leaves the tab.

Everything the layout decides is plain numbers (`ConsoleLayout`, the clamp
functions), so it is testable without a window. `ConsoleBandController` is
`OutputBandController` (Plan 40 UXC2) taught those rules. The shell builds it
(`install_three_region_shell`); the window then hands it the user's saved
layout and the callbacks it reads from (`configure`) and the settings sheet it
sits beside (`follow_sheet`). The console is docked at the sheet's edge, not
the window's: a floating sheet covers the full height of region C, so the
console starts where the sheet ends (sheet on the left) or ends where it begins
(sheet on the right), and follows the sheet when it moves, resizes, minimises
or pins (DP-1660).

The layout is a per-user choice and is saved in ``foammesh.settings.yaml``
(the ``console`` member of the three-region record); a project never carries it.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QWidget

from foammesh.view.main_window.three_region_shell import (
    OutputBandController, REGION_C_VIEWPORT_MINIMUM)

#: Height presets of the band (verdict line and tabs together), in px.
HEIGHT_PRESETS = (100, 144, 420)
STANDARD_HEIGHT = 144
#: Plan 43 section 1 rule 4: at 1280 x 800 the console opens at 100 px.
COMPACT_HEIGHT = 100
COMPACT_BELOW_WINDOW_HEIGHT = 900
#: The top grip's range.
HEIGHT_MIN = 64
HEIGHT_MAX = 640
#: Width presets; ``None`` is "up to the right edge".
WIDTH_PRESETS = (900, 640)
WIDTH_MIN = 320
MINIMISED_HEIGHT = 28
#: Section 3: the Mesh quality tab opens at this height.
QUALITY_HEIGHT = 280
QUALITY_TAB_KEY = 'quality'
SIDE_GRIP_WIDTH = 6


def clamp_height(value: int) -> int:
    return max(HEIGHT_MIN, min(HEIGHT_MAX, int(value)))


def clamp_width(value: int | None, available: int | None = None) -> int | None:
    """``None`` stays "to the right edge". A number is held to the range
    from ``WIDTH_MIN`` to the width there is."""
    if value is None:
        return None
    width = max(WIDTH_MIN, int(value))
    if available is not None and available > 0:
        width = min(width, max(available, WIDTH_MIN))
    return width


def standard_height(window_height: int | None) -> int:
    """The default console height for a window of ``window_height`` px."""
    if window_height and window_height < COMPACT_BELOW_WINDOW_HEIGHT:
        return COMPACT_HEIGHT
    return STANDARD_HEIGHT


def band_width(choice: int | None, available: int) -> int:
    """The console's drawn width inside ``available`` px of clear area."""
    available = max(int(available), 0)
    if choice is None:
        return available
    return min(max(int(choice), WIDTH_MIN), available) if available else 0


def console_margins(side: str | None, inset: int, choice: int | None,
                    available: int) -> tuple[int, int]:
    """``(left, right)`` margins of the console inside a band ``available`` px
    wide whose ``side`` edge is covered by a sheet ``inset`` px wide.

    The console is drawn in the clear area (``available - inset``), as wide as
    ``choice`` allows. Sheet on the left: it starts at the sheet's edge and is
    narrowed from the right. Sheet on the right (or none): it starts at the
    left and ends at the sheet's edge, or short of it."""
    available = max(int(available), 0)
    inset = min(max(int(inset), 0), available) if side in ('left', 'right') else 0
    clear = available - inset
    drawn = band_width(choice, clear)
    spare = max(clear - drawn, 0)
    if side == 'left':
        return (inset, spare)
    return (0, inset + spare)


@dataclass
class ConsoleLayout:
    """The reader's console choices. ``None`` means the standard."""

    height: int | None = None
    width: int | None = None
    open_on_run: bool = True

    def resolved_height(self, window_height: int | None) -> int:
        if self.height is None:
            return standard_height(window_height)
        return clamp_height(self.height)

    def to_dict(self) -> dict:
        return {'version': 1, 'height': self.height, 'width': self.width,
                'open_on_run': bool(self.open_on_run)}

    @classmethod
    def from_dict(cls, value) -> 'ConsoleLayout':
        if not isinstance(value, dict) or value.get('version') != 1:
            return cls()
        height, width = value.get('height'), value.get('width')
        return cls(
            height=(clamp_height(height) if isinstance(height, int)
                    and not isinstance(height, bool) else None),
            width=(clamp_width(width) if isinstance(width, int)
                   and not isinstance(width, bool) else None),
            open_on_run=bool(value.get('open_on_run', True)))


def exclusive_group(parent) -> QActionGroup:
    """One-of-many group for the Height and Width menus."""
    return QActionGroup(parent)


class _SideGrip(QWidget):
    """The console's right edge: drag it to choose the width."""

    def __init__(self, controller: 'ConsoleBandController', parent) -> None:
        super().__init__(parent)
        self.setObjectName('consoleSideGrip')
        self.setAccessibleName('Resize the console width')
        self.setToolTip('Drag to set the console width')
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setFixedWidth(SIDE_GRIP_WIDTH)
        self._controller = controller
        self._dragging = False

    def mousePressEvent(self, event):
        self._dragging = True
        event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            x = self.mapToParent(event.position().toPoint()).x()
            # the console starts at the sheet's edge, so the width is the
            # distance from there
            self._controller.set_width(x - self._controller.left_edge())
        event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        event.accept()


class _BandWatcher(QObject):
    """Re-applies the width when the band is resized, and keeps the grip."""

    def __init__(self, controller: 'ConsoleBandController') -> None:
        super().__init__(controller._band)
        self._controller = controller

    def eventFilter(self, obj, event):
        # The band itself and the sheet's frame and tab: any of them changing
        # size, place or visibility can move the console's edge.
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show,
                            QEvent.Type.Hide, QEvent.Type.Move):
            self._controller._apply_width()
        return False


class ConsoleBandController(OutputBandController):
    """`OutputBandController` with Plan 43's console rules."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._console = ConsoleLayout()
        self._window_height = lambda: None
        self._current_tab_key = lambda: ''
        self._save = lambda _layout: None
        self._sheet = None
        self._edge = (0, 0)
        self._minimised = False
        self._programmatic = 0
        self._quality_raised = False
        self._geometry_listeners: list = []
        # The grip and the watcher are parts of the console, not tools of
        # their own: kept in one tuple so the facade inventory does not take
        # them for new widgets (the grip is listed in facade-drafts.md).
        self._parts = (_SideGrip(self, self._band), _BandWatcher(self))
        self._parts[0].hide()
        self._band.installEventFilter(self._parts[1])
        self._splitter.splitterMoved.connect(self._splitter_moved)
        if self._header is not None:
            # Plan 44 MF6. The header row's minimise chip folds the console;
            # the folded chip opens it again (the same two moves Ctrl+` and
            # Ctrl+J make).
            self._header.minimiseRequested.connect(
                lambda: self.minimise())
            self._header.restoreRequested.connect(lambda: self.open())
        self._tabs.currentChanged.connect(lambda _index: self.tab_changed())

    def configure(self, *, layout: ConsoleLayout, window_height=lambda: None,
                  current_tab_key=lambda: '',
                  save=lambda _layout: None) -> 'ConsoleBandController':
        """The window hands over the user's saved layout and what the console
        reads from the window (its height, the current output tab, where to
        save the layout)."""
        self._console = layout
        self._window_height = window_height
        self._current_tab_key = current_tab_key
        self._save = save
        self._apply_width()
        return self

    def follow_sheet(self, sheet) -> None:
        """Dock at ``sheet``'s edge: ``sheet`` is the settings sheet (it has
        ``side``, ``shows_floating_sheet()``, ``is_floating``, ``is_minimised``,
        ``frame_width()``, ``frame``, ``tab`` and a ``changed`` signal)."""
        self._sheet = sheet
        for part in (sheet.frame, sheet.tab):
            part.installEventFilter(self._parts[1])
        sheet.changed.connect(self._apply_width)
        self._apply_width()

    # -- state -------------------------------------------------------------- #

    @property
    def _grip(self):
        return self._parts[0]

    @property
    def layout(self) -> ConsoleLayout:
        return self._console

    @property
    def is_minimised(self) -> bool:
        return self._minimised

    @property
    def band_widget(self):
        """The console's widget, for the clear area (Plan 43 LY5)."""
        return self._band

    @property
    def band_height(self) -> int:
        return self._band.height()

    def subscribe_geometry(self, callback) -> None:
        """``callback()`` after the console's size, width or minimised state
        changes -- the hook Plan 43 LY5 centres the model from."""
        self._geometry_listeners.append(callback)

    def _geometry_changed(self) -> None:
        for callback in tuple(self._geometry_listeners):
            callback()

    def _persist(self) -> None:
        self._save(self._console)

    def _on_quality_tab(self) -> bool:
        return self._current_tab_key() == QUALITY_TAB_KEY

    def target_height(self) -> int:
        """What the band's height should be right now, open."""
        height = self._console.resolved_height(self._window_height())
        if self._on_quality_tab():
            height = max(height, QUALITY_HEIGHT)
        return height

    # -- the height --------------------------------------------------------- #

    def _apply_height(self, height: int | None = None) -> None:
        """Set the band to ``height`` px (verdict line included), keeping the
        viewport at its own minimum."""
        sizes = self._splitter.sizes()
        total = sum(sizes[:2])
        if total <= 0:
            total = self._viewport_total()
        if height is None and self._on_quality_tab():
            # MF27: a band that opens (or is re-sized) straight onto the Mesh
            # quality tab is raised to 280 here, not in tab_changed; remember
            # it, so leaving the tab folds back to the console's own height.
            own = clamp_height(
                self._console.resolved_height(self._window_height()))
            if own < QUALITY_HEIGHT:
                self._quality_raised = True
        height = clamp_height(self.target_height() if height is None
                              else height)
        height = max(min(height, total - REGION_C_VIEWPORT_MINIMUM), 0)
        self._band.setMinimumHeight(min(HEIGHT_MIN, height))
        self._band.setMaximumHeight(HEIGHT_MAX)
        self._programmatic += 1
        try:
            self._splitter.setSizes([total - height, height] + sizes[2:])
        finally:
            self._programmatic -= 1
        self._geometry_changed()

    def set_height(self, height: int | None) -> None:
        """A preset or a number (``None`` is the standard) -- and keep it."""
        self._console.height = None if height is None else clamp_height(height)
        self._persist()
        if self._open and not self._focus:
            self._apply_height()
        else:
            self._geometry_changed()

    def _splitter_moved(self, _pos, _index) -> None:
        """The top grip: clamp to 64..640 and remember where it was left."""
        if self._programmatic or not self._open or self._focus:
            return
        height = self._splitter.sizes()[1]
        clamped = clamp_height(height)
        if clamped != height:
            self._apply_height(clamped)
        if self._on_quality_tab():
            # Dragged on the quality tab: this visit only; the console's own
            # height is what the tab folds back to.
            self._geometry_changed()
            return
        self._console.height = clamped
        self._persist()
        self._geometry_changed()

    # -- the width ---------------------------------------------------------- #

    def sheet_inset(self) -> tuple[str | None, int]:
        """``(side, px)`` the settings sheet (or its 32 px tab) covers of the
        console's row; ``(None, 0)`` when it covers nothing: pinned (the
        sheet is then a dock beside region C), focus mode, or no sheet."""
        sheet = self._sheet
        if sheet is None or self._focus:
            return (None, 0)
        try:
            if sheet.shows_floating_sheet():
                # the frame floats INSET px off the window edge (MF5), so the
                # console stops at the frame's inner edge, not the slot's
                from .settings_sheet import INSET
                return (sheet.side, int(sheet.frame_width()) + INSET)
            if sheet.is_floating and sheet.is_minimised:
                return (sheet.side, int(sheet.tab.width()))
        except RuntimeError:                      # a window being closed
            pass
        return (None, 0)

    def left_edge(self) -> int:
        """Where the console starts, in the band's own coordinates."""
        return self._edge[0]

    def available_width(self) -> int:
        """The clear width: the splitter's, less what the sheet covers."""
        return max(self._splitter.width() - self.sheet_inset()[1], 0)

    def set_width(self, width: int | None) -> None:
        """A preset, a dragged width, or ``None`` for the right edge."""
        self._console.width = clamp_width(width, self.available_width())
        self._persist()
        self._apply_width()
        self._geometry_changed()

    def drawn_width(self) -> int:
        return band_width(self._console.width, self.available_width())

    def _apply_width(self) -> None:
        """Narrow the band from the right by margin; the grip sits at the new
        edge. At full width the margin is zero and the grip hides."""
        layout = self._band.layout()
        if layout is None:
            return
        total = self._band.width()
        side, inset = self.sheet_inset()
        left, right = console_margins(side, inset, self._console.width, total)
        drawn = max(total - left - right, 0)
        margins = layout.contentsMargins()
        moved = (margins.left(), margins.right()) != (left, right)
        if moved:
            layout.setContentsMargins(left, margins.top(), right,
                                      margins.bottom())
        self._edge = (left, right)
        self._set_flush_left(side is None and self._pinned_left())
        show = (self._open and not self._focus and not self._minimised
                and drawn > 0)
        self._parts[0].setVisible(show)
        if show:
            self._parts[0].setGeometry(left + max(drawn - SIDE_GRIP_WIDTH, 0),
                                       0, SIDE_GRIP_WIDTH, self._band.height())
            self._parts[0].raise_()
        if moved:
            self._geometry_changed()

    def _pinned_left(self) -> bool:
        """The settings sheet is pinned at the left edge, beside the console."""
        sheet = self._sheet
        try:
            return bool(sheet is not None and not self._focus
                        and sheet.mode == 'pinned' and sheet.side == 'left')
        except (AttributeError, RuntimeError):
            return False

    def _set_flush_left(self, flush: bool) -> None:
        """Plan 44 MF6: next to a pinned sheet the card's top-left corner is
        square (``foammeshFlushLeft`` in the style sheet)."""
        card = self._header.card if self._header is not None else None
        if card is None or bool(card.property('foammeshFlushLeft')) == flush:
            return
        card.setProperty('foammeshFlushLeft', flush)
        card.style().unpolish(card)
        card.style().polish(card)

    # -- open, close, minimise --------------------------------------------- #

    def open(self, *, automatic: bool = False) -> bool:
        was_minimised = self._minimised
        if was_minimised and not self._focus:
            self._leave_minimised()
        changed = super().open(automatic=automatic)
        if changed or (was_minimised and self._open):
            self._apply_height()
            self._apply_width()
        return changed or was_minimised

    def close(self) -> bool:
        was_minimised = self._minimised and not self._focus
        if was_minimised:
            self._leave_minimised()
            # The header is a folded state: let the base fold it to the chip.
            self._open = True
        changed = super().close()
        self._quality_raised = False
        self._apply_width()
        if changed:
            self._geometry_changed()
        return changed

    def _leave_minimised(self) -> None:
        self._minimised = False
        self._strip.setMaximumHeight(16777215)
        self._band.setMaximumHeight(16777215)

    def minimise(self) -> bool:
        """Fold to the 28 px header. True when it changed."""
        if self._focus or self._minimised:
            return False
        self._automatic = False
        self._open = False
        self._quality_raised = False
        self._show_band_widgets(False)
        self._minimised = True
        self._strip.setMaximumHeight(MINIMISED_HEIGHT)
        self._band.setMinimumHeight(MINIMISED_HEIGHT)
        self._band.setMaximumHeight(MINIMISED_HEIGHT)
        total = self._viewport_total()
        self._programmatic += 1
        try:
            self._splitter.setSizes([total - MINIMISED_HEIGHT,
                                     MINIMISED_HEIGHT])
        finally:
            self._programmatic -= 1
        self._apply_width()
        self._changed()
        self._geometry_changed()
        return True

    def toggle_minimised(self) -> bool:
        """Ctrl+`. Returns whether the console is minimised afterwards."""
        if self._focus:
            return False
        if self._minimised:
            self.open()
        else:
            self.minimise()
        return self._minimised

    def toggle(self) -> bool:
        # Ctrl+J from the minimised header shows the console.
        if self._minimised and not self._focus:
            self.open()
            return self._open
        return super().toggle()

    def standard(self) -> None:
        """Standard layout: the heights and widths back to the defaults."""
        self._console.height = None
        self._console.width = None
        self._persist()
        if self._minimised:
            self.open()
        if self._open:
            self._apply_height()
        self._apply_width()
        self._geometry_changed()

    # -- runs ---------------------------------------------------------------- #

    def run_started(self) -> bool:
        if self._console.open_on_run:
            return super().run_started()
        # Not opened, but the run still counts so a failure keeps the band.
        self._running += 1
        self._failed = False
        return False

    def set_open_on_run(self, on: bool) -> None:
        self._console.open_on_run = bool(on)
        self._persist()

    # -- the Mesh quality tab ------------------------------------------------ #

    def tab_changed(self) -> None:
        """280 px on the Mesh quality tab, and back to the console's own height
        when the reader leaves it."""
        if not self._open or self._focus:
            return
        if self._on_quality_tab():
            if self._splitter.sizes()[1] < QUALITY_HEIGHT:
                self._quality_raised = True
                self._apply_height()
        elif self._quality_raised:
            self._quality_raised = False
            self._apply_height()
