"""Authoritative three-region shell for the FoamMesh desktop.

The Designer file still owns the mature engineering pages and rendering
widgets.  This module changes only their visual host: navigation is placed in
Region A, the one canonical page stack in Region B, and the existing viewport
in Region C.  No page or rendering widget is cloned.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QTabWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from foammesh.view.theming.metrics import GAP, apply_bar_metrics

from foammesh.view.theming.elevation import level_margins, paint_ring

from .output_tabs import CONSOLE_HEADER_HEIGHT, ConsoleHeader
from .verdict_strip import VerdictStrip


class ElidingPushButton(QPushButton):
    """MF27. The footer's primary button. At 1280x800 the 300 px sheet left
    "Apply layers & Proceed" (and every "<verb> & Proceed") about 30 px less
    than its label, and Qt clipped the centred text at both ends ("ly layers
    & Proc"). The label is now elided at its end when the row is short; the
    full words stay in ``text()``, the accessible name and the tooltip."""

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), self.fontMetrics().horizontalAdvance(
            'Proceed') + 24))
        return hint

    def displayedText(self) -> str:
        """The words as painted: elided to the contents rect when short."""
        from PySide6.QtWidgets import QStyle, QStyleOptionButton
        option = QStyleOptionButton()
        self.initStyleOption(option)
        room = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, self).width()
        # '&&' is a literal ampersand, a lone '&' marks a mnemonic
        plain = '&'.join(part.replace('&', '')
                         for part in self.text().split('&&'))
        metrics = self.fontMetrics()
        if metrics.horizontalAdvance(plain) <= room:
            return plain
        return metrics.elidedText(plain, Qt.TextElideMode.ElideRight,
                                  max(room, 0))

    def paintEvent(self, event):
        from PySide6.QtWidgets import QStyle, QStyleOptionButton, QStylePainter
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.displayedText().replace('&', '&&')
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


REGION_A_MINIMUM = 190
REGION_B_MINIMUM = 340
REGION_C_MINIMUM = 420
#: The 3D view never shrinks below this, whatever the output band wants: the
#: model staying on screen is the point of the WP3 split.
REGION_C_VIEWPORT_MINIMUM = 220
#: Enough of the tab band to read a line of console or one metric row.
REGION_C_BAND_MINIMUM = 90
#: R62/R114/R157. `setStretchFactor` only shares out *extra* space, so with no
#: explicit split the band opened at its own minimum -- MEASURED at about 40 px
#: of tab content, one clipped line of the quality summary, with the Metrics
#: and Offending-elements tables squeezed to their headers and no visible rows.
#: This is the height at which a headline, a metric row and a couple of
#: offending elements are all readable without touching the handle.
REGION_C_BAND_PREFERRED = 260
REGION_A_PREFERRED = 220
#: Plan 40 UXC1 (section 4.4 item 1). Collapsed, the outline is a rail of step
#: numbers and glyphs this wide; the freed room goes to the viewport.
REGION_A_RAIL = 48
#: Plan 40 UXC1 (section 4.4 item 5). From this window width a wide table page
#: may take the room it needs, up to this share of the window.
REGION_B_WIDE_FROM = 1920
REGION_B_WIDE_SHARE = 0.40
REGION_B_PREFERRED = 420
REGION_B_COMPACT_PREFERRED = 360
#: DP-569. A window narrower than this gets the compact settings column. It
#: is named so the table-width gates measure at the width the shell decides
#: rather than at a number copied out of it.
REGION_B_COMPACT_BELOW = 1600
#: DP-A1. How little of the settings column has to be on screen for the
#: column to be worth showing at all -- about four rows of a form. It is a
#: fact about the shell, not about any page, which is the whole point: the
#: page stack is inside a scroller now, so the window's minimum height stops
#: following whichever page in the stack happens to be tallest.
REGION_B_PAGE_MINIMUM = 160
LAYOUT_VERSION = 1


def preferred_band_sizes(total_height: int) -> tuple[int, int]:
    """``(viewport, band)`` heights for Region C's vertical splitter.

    The band gets a quarter of the height, never less than
    ``REGION_C_BAND_PREFERRED`` and never so much that the 3D view drops under
    its own minimum.
    """
    total_height = max(
        int(total_height), REGION_C_VIEWPORT_MINIMUM + REGION_C_BAND_PREFERRED)
    band = max(REGION_C_BAND_PREFERRED, total_height // 4)
    band = min(band, total_height - REGION_C_VIEWPORT_MINIMUM)
    return total_height - band, band


def readable_band_height(band_widget) -> int:
    """R196: the height at which what the band holds is legible -- the
    preferred band, or what the widget says it needs, whichever is larger."""
    minimum = REGION_C_BAND_PREFERRED
    if band_widget is not None:
        minimum = max(minimum, band_widget.minimumSizeHint().height())
    return minimum


def reveal_output_band(splitter, *, minimum: int | None = None) -> bool:
    """Grow Region C's output band to fit what it holds. True when it moved.

    R114. `Details` beside the verdict line raised the Mesh quality tab into a
    band about 40 px tall, so the button that promises the report delivered a
    cropped sentence and the only remedy was to find a splitter handle and drag
    it up 270 px. The viewport keeps its own minimum, so revealing the report
    never hides the mesh it describes.

    R196. That fix moved the handle to a fixed 260 px, and MEASURED on the
    tee that is not enough: the band also carries the verdict strip and the
    tab bar, so a failing gate with one metric row and 13 offending elements
    showed two tables cropped to their column headers -- the report was
    there and none of it was legible. A constant cannot know that; the band
    can. Ask the widget what it needs and use whichever is larger, still
    capped by the viewport's own minimum.

    Plan 45 H. Region C's splitter belongs to its band controller: setting
    its sizes from here, behind the controller's back, left the controller
    sure of a height and a state the splitter no longer had. When the
    splitter has a controller (``_foammeshBandController``), the controller
    does the revealing; this function only measures for a bare splitter.
    """
    if splitter is None or splitter.count() < 2:
        return False
    controller = getattr(splitter, '_foammeshBandController', None)
    if controller is not None:
        return controller.reveal(minimum)
    if minimum is None:
        minimum = readable_band_height(splitter.widget(1))
    sizes = list(splitter.sizes())
    total = sizes[0] + sizes[1]
    if total <= 0 or sizes[1] >= minimum:
        return False
    band = min(int(minimum), total - REGION_C_VIEWPORT_MINIMUM)
    if band <= sizes[1]:
        return False
    splitter.setSizes([total - band, band] + sizes[2:])
    return True


class OutputBandController:
    """The output band on demand, and focus mode (Plan 40 UXC2).

    F2b/F8d/N11. The band under the viewport took about 260 px for a console
    that said nothing and a quality tab that said the same thing three times,
    and reading the quality tables meant shrinking the view of the mesh they
    describe. Closed, the band is the verdict strip alone -- the one-line chip
    -- and the viewport keeps the rest. It opens on a run, on a failure, on
    ``Details`` and on a click on the chip; ``Ctrl+J`` toggles it.

    Focus mode (``F11``) hides the outline, the page column and the band, so
    the model owns the window; ``Esc`` puts back exactly what was there.
    """

    def __init__(self, region_splitter, output_band, tab_host, verdict_strip,
                 region_splitter_h=None, header=None) -> None:
        #: Plan 44 MF6. The console's one header row (it moves the verdict
        #: between the row and the folded chip); ``None`` for a bare band.
        self._header = header
        self._splitter = region_splitter
        self._band = output_band
        self._tabs = tab_host
        self._strip = verdict_strip
        self._outer = region_splitter_h
        self._open = False
        self._focus = False
        self._band_open_before_focus = False
        self._hidden_before_focus: list = []
        self._listeners: list = []
        self._docks = None
        # UXR: a band a run opened folds again when the runs end well; a band
        # the reader opened or took over (a click, Ctrl+J, a tab asked for)
        # stays.
        self._automatic = False
        self._running = 0
        self._failed = False

    def use_docks(self, docks) -> None:
        """Plan 40 UXC3. Focus mode closes the side docks instead of hiding
        splitter children, and the sizes it left come back with them."""
        self._docks = docks
        self._sizes_before_focus = None

    # -- band -------------------------------------------------------------- #

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def in_focus_mode(self) -> bool:
        return self._focus

    def subscribe(self, callback) -> None:
        """``callback(is_open, in_focus_mode)`` after either state changes."""
        self._listeners.append(callback)

    def _changed(self) -> None:
        for callback in tuple(self._listeners):
            callback(self._open, self._focus)

    def _viewport_total(self) -> int:
        sizes = self._splitter.sizes()
        total = sum(sizes[:2])
        return total if total > 0 else 800

    def open(self, *, automatic: bool = False) -> bool:
        """Show the tab band at a readable height. True when it changed.

        ``automatic`` marks a run opening it: that band folds when the runs
        end well. Any other open is the reader's and keeps it."""
        if self._focus:
            # Focus mode owns the screen; the band opens when it ends.
            self._band_open_before_focus = True
            return False
        if self._open:
            if not automatic:
                self._automatic = False
            return False
        self._automatic = automatic
        self._open = True
        self._show_band_widgets(True)
        total = self._viewport_total()
        self._splitter.setSizes(list(preferred_band_sizes(total)))
        self._changed()
        return True

    def close(self) -> bool:
        """Fold the band to the verdict chip. True when it changed."""
        if self._focus:
            self._band_open_before_focus = False
            return False
        self._automatic = False
        if not self._open and self._folded():
            return False
        self._open = False
        self._show_band_widgets(False)
        total = self._viewport_total()
        chip = self._chip_height()
        self._splitter.setSizes([max(total - chip, 0), chip])
        self._changed()
        return True

    def reveal(self, minimum: int | None = None) -> bool:
        """Details: open the band and make it tall enough to read (R114/R196).

        The one way to grow the band for a report, so the controller's state
        and the splitter never disagree (Plan 45 H). True when anything
        changed. In focus mode the band opens when focus mode ends."""
        changed = self.open()
        if not self._open or self._focus:
            return changed
        wanted = (readable_band_height(self._band) if minimum is None
                  else int(minimum))
        sizes = list(self._splitter.sizes())
        total = sizes[0] + sizes[1]
        if total <= 0 or sizes[1] >= wanted:
            return changed
        band = min(wanted, total - REGION_C_VIEWPORT_MINIMUM)
        if band <= sizes[1]:
            return changed
        self._set_band_height(band)
        return True

    def _set_band_height(self, height: int) -> None:
        sizes = list(self._splitter.sizes())
        total = sizes[0] + sizes[1]
        self._splitter.setSizes([total - height, height] + sizes[2:])

    def restore_from_chip(self) -> bool:
        """The folded chip was pressed: open the console. In focus mode the
        chip is the one control on screen, so it ends focus mode first."""
        if self._focus:
            self.leave_focus()
        self.open()
        return self._open

    def toggle(self) -> bool:
        """Ctrl+J. Returns whether the band is open afterwards."""
        if self._focus:
            self.leave_focus()
        if self._open:
            self.close()
        else:
            self.open()
        return self._open

    def run_started(self) -> bool:
        """A job started: bring the band up as the run's, not the reader's."""
        self._running += 1
        self._failed = False
        return self.open(automatic=True)

    def run_failed(self) -> bool:
        """A job failed: the band stays, whoever opened it."""
        self._running = max(self._running - 1, 0)
        self._failed = True
        self._automatic = False
        return self.open()

    def run_ended(self) -> None:
        self._running = max(self._running - 1, 0)

    def settle_after_runs(self) -> bool:
        """Fold the band a run opened once no run is left and none failed."""
        if (self._automatic and self._open and self._running == 0
                and not self._failed and not self._focus):
            return self.close()
        return False

    def _folded(self) -> bool:
        return self._tabs.isHidden()

    def _chip_height(self) -> int:
        # Plan 44 MF6. Folded, the console is its 28 px header row and nothing
        # else; the verdict strip no longer decides the height.
        return CONSOLE_HEADER_HEIGHT

    def _show_band_widgets(self, opened: bool) -> None:
        self._tabs.setVisible(opened)
        if self._header is not None:
            self._header.set_folded(not opened)
        if opened:
            self._band.setMinimumHeight(
                CONSOLE_HEADER_HEIGHT + REGION_C_BAND_MINIMUM)
            self._band.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            self._band.setMaximumHeight(16777215)
        else:
            # One line, and no handle to drag it taller than its content.
            chip = self._chip_height()
            self._band.setMinimumHeight(chip)
            self._band.setMaximumHeight(chip)

    # -- focus mode -------------------------------------------------------- #

    def enter_focus(self) -> bool:
        if self._focus:
            return False
        self._band_open_before_focus = self._open
        self._hidden_before_focus = []
        if self._docks is not None:
            self._sizes_before_focus = self._docks.regions.sizes()
            self._docks.hide_side_docks()
        elif self._outer is not None:
            for index in (0, 1):
                widget = self._outer.widget(index)
                if widget is not None and not widget.isHidden():
                    widget.hide()
                    self._hidden_before_focus.append(widget)
        # Plan 45 H. Not hidden: folded to its 28 px chip, so a way back to
        # the console stays on screen (the chip ends focus mode and opens it).
        self._show_band_widgets(False)
        chip = self._chip_height()
        total = self._viewport_total()
        self._splitter.setSizes([max(total - chip, 0), chip])
        self._focus = True
        self._changed()
        return True

    def leave_focus(self) -> bool:
        if not self._focus:
            return False
        self._focus = False
        if self._docks is not None:
            self._docks.show_side_docks()
            if self._sizes_before_focus and all(self._sizes_before_focus):
                self._docks.regions.setSizes(self._sizes_before_focus)
        for widget in self._hidden_before_focus:
            widget.show()
        self._hidden_before_focus = []
        self._band.show()
        wanted = self._band_open_before_focus
        if wanted:
            self._open = False
            self.open()
        else:
            self._open = True
            self.close()
        self._changed()
        return True

    def toggle_focus(self) -> bool:
        """F11. Returns whether focus mode is on afterwards."""
        if self._focus:
            self.leave_focus()
        else:
            self.enter_focus()
        return self._focus


def preferred_region_sizes(
        total_width: int, a_preferred: int | None = None,
        b_wanted: int | None = None, rail: bool = False
) -> tuple[int, int, int]:
    """Return the FOAMFlow-aligned A/B/C allocation for ``total_width``.

    DP-134. ``a_preferred`` is what the outline says it needs to show its rows
    without eliding them; ``REGION_A_PREFERRED`` is the floor, and B and C keep
    their own minimums ahead of it, so a long task name can widen the pane but
    never starve the page or the viewport.

    Plan 40 UXC1. ``rail`` collapses A to ``REGION_A_RAIL`` and gives what it
    frees to C. ``b_wanted`` is the width the page on screen asks for; in a
    window of ``REGION_B_WIDE_FROM`` or more a wide table page gets it, up to
    ``REGION_B_WIDE_SHARE`` of the window, and a narrower window is unchanged.
    """
    total_width = max(
        int(total_width),
        REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM,
    )
    a_width = (REGION_A_RAIL if rail
               else max(REGION_A_PREFERRED, int(a_preferred or 0)))
    b_width = (
        REGION_B_COMPACT_PREFERRED
        if total_width < REGION_B_COMPACT_BELOW else REGION_B_PREFERRED
    )
    if b_wanted and total_width >= REGION_B_WIDE_FROM:
        b_width = max(b_width, min(
            int(b_wanted), int(total_width * REGION_B_WIDE_SHARE)))
    c_width = total_width - a_width - b_width
    if c_width < REGION_C_MINIMUM:
        deficit = REGION_C_MINIMUM - c_width
        shrink_b = min(deficit, b_width - REGION_B_MINIMUM)
        b_width -= shrink_b
        deficit -= shrink_b
        if not rail:
            a_width -= min(deficit, a_width - REGION_A_MINIMUM)
        c_width = total_width - a_width - b_width
    return a_width, b_width, c_width


def layout_worth_recording(user_moved: bool, restored_sizes) -> bool:
    """Whether the splitter's current sizes are a preference or an accident.

    DP-134.  They are a preference in exactly two cases: somebody dragged the
    handle this session, or somebody dragged it in an earlier one and we are
    looking at what we restored.  Otherwise the sizes are whatever
    :func:`clamp_region_sizes` computed for the last window width, and writing
    them down turns one narrow session into a permanent setting -- which is how
    Region A came to sit at its 190 px minimum, eliding half the outline, with
    no way back short of deleting the settings file.
    """
    return bool(user_moved) or restored_sizes is not None


def clamp_region_sizes(
        total_width: int, sizes, a_preferred: int | None = None,
        b_wanted: int | None = None, rail: bool = False
) -> tuple[int, int, int]:
    """Validate a saved allocation and return a safe current-schema layout."""
    try:
        values = tuple(int(value) for value in sizes)
    except (TypeError, ValueError):
        return preferred_region_sizes(total_width, a_preferred, b_wanted, rail)
    if len(values) != 3 or any(value < 0 for value in values):
        return preferred_region_sizes(total_width, a_preferred, b_wanted, rail)

    total_width = max(
        int(total_width),
        REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM,
    )
    a_width = (REGION_A_RAIL if rail
               else max(REGION_A_MINIMUM, values[0]))
    b_width = max(REGION_B_MINIMUM, values[1])
    # A and B own their requested widths; every remaining pixel belongs to C.
    if a_width + b_width + REGION_C_MINIMUM > total_width:
        return preferred_region_sizes(total_width, a_preferred, b_wanted, rail)
    return a_width, b_width, total_width - a_width - b_width


class PageColumn(QScrollArea):
    """The settings column: one page at a time, scrolled, footer outside it.

    DP-A1. Region B used to hold the page stack directly, so the stack's
    minimum size hint -- which `QStackedLayout` computes as the *largest* of
    every page it holds, shown or not -- was a term in the window's own
    minimum height. MEASURED on the real window built offscreen at
    `4ed5fcb8`: 892 px of minimum height, of which Region B asked for 823 and
    the page stack for 736, and the 736 belonged to `baseGridPage` (Plan 43 D1 has since emptied it: the
    five snappy stack pages are bare shells that keep the stack indices), a page
    the user may never open. The elbow campaign of 16 September measured the
    same thing on a real display: 1,428 px of minimum height against 1,032 px
    of screen, with the one forward button 359 px below the bottom of the
    screen and no resize able to recover it.

    So the column scrolls and the footer does not. Two things make that true
    and both are needed:

    * The stack's *vertical* size policy is `Ignored`, which is what makes
      `qSmartMinSize` -- the function every Qt layout and this scroll area
      ask for a child's floor -- stop reading the stack's own hint. Nothing
      about the width changes: a page too wide for the column is as wide as
      it was.
    * The floor is then written back, per page, as an explicit minimum
      height taken from *the page being shown*. An explicit minimum survives
      `Ignored`, so the scroller still grows a bar for a page that needs one
      -- a Designer page with no scroller of its own, Preparation with its
      findings table -- and a hidden page contributes nothing.

    The height is re-read on every layout request as well as on every page
    change, because a page grows after it is opened: a fold opens, a table
    fills, a validation line appears.

    The same per-page height is also the stack's *maximum*, which is what
    keeps the two scrollers from both drawing a bar. Several pages carry a
    `QScrollArea` of their own -- every engine task page does, and so do the
    Snappy Designer pages -- and Qt, left alone, sizes the widget inside a
    resizable scroll area to its height-for-width, which MEASURED at 916 px
    for every one of those pages in a 700 px window whose column viewport was
    544 px. That put a 372 px bar on the column *and* left the page's own bar
    where it was: two bars, a few pixels apart, one of which moved the page
    heading away from the row the other had just revealed. Capping the stack
    at the viewport height unless the page itself asks for more means the
    outer bar appears only for a page with no scroller of its own, and a page
    that scrolls itself is given exactly the room it is shown in.
    """

    #: Plan 40 UXC1. The width the page on screen asks for, in the window's
    #: own pixels (page floor plus the column's chrome), when it changes.
    wantedWidthChanged = Signal(int)

    def __init__(self, stack, parent=None) -> None:
        super().__init__(parent)
        self._stack = stack
        self._wantedWidth = 0
        self.setObjectName('regionBPageColumn')
        self.setAccessibleName('Settings for the current step')
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # One column, one direction. A settings form that scrolled sideways
        # as well would put the value of a row off screen from its label.
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(REGION_B_PAGE_MINIMUM)
        policy = stack.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Policy.Ignored)
        # DP-534 (audit 0924, S5/S6). The width had the same fault as the
        # height: the stack was as wide as its widest page, shown or not.
        # The Preparation page's revision row outgrew the column on a case
        # with a long geometry name, and every snappy stage page was then
        # laid out that wide inside a column with no sideways bar -- and
        # when focus reached a control on the right, the scroller moved the
        # page left to show it, so the heading read `main & regions` and
        # `stellation`. Width now follows the page being shown too.
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        stack.setSizePolicy(policy)
        self.setWidget(stack)
        # With no sideways bar there is no way back from a sideways scroll,
        # so the column never takes one: a page that is still too wide is
        # cut on its right, where its own scroller or elision can answer,
        # and never loses its heading off the left.
        self.horizontalScrollBar().valueChanged.connect(
            self._keepLeftEdge)
        stack.currentChanged.connect(self._followPage)
        stack.installEventFilter(self)
        self._followPage()

    @property
    def stack(self):
        return self._stack

    def wantedWidth(self) -> int:
        """Width the shown page needs, with the column's own chrome."""
        return self._wantedWidth

    def eventFilter(self, watched, event):
        if watched is self._stack and event.type() in (
                QEvent.Type.LayoutRequest, QEvent.Type.ChildAdded,
                QEvent.Type.ChildRemoved):
            self._followPage()
        return False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The cap below is measured against the viewport, so it is re-read
        # whenever the viewport changes size.
        self._followPage()

    def _followPage(self, *_args) -> None:
        page = self._stack.currentWidget()
        wanted = 0 if page is None else page.minimumSizeHint().height()
        capped = max(wanted, self.viewport().height())
        # DP-534. The shown page's own floor, not the widest page's.
        width = 0 if page is None else max(0, page.minimumSizeHint().width())
        # Guarded, because writing either bound posts a layout request back to
        # the stack and this is the handler for those.
        if self._stack.minimumHeight() != wanted:
            self._stack.setMinimumHeight(wanted)
        if self._stack.maximumHeight() != capped:
            self._stack.setMaximumHeight(capped)
        if self._stack.minimumWidth() != width:
            self._stack.setMinimumWidth(width)
        # What the page asks for, plus the bar the column may draw beside it.
        chrome = self.verticalScrollBar().sizeHint().width() + 2 * GAP
        wanted_width = width + chrome if width else 0
        if wanted_width != self._wantedWidth:
            self._wantedWidth = wanted_width
            self.wantedWidthChanged.emit(wanted_width)

    def _keepLeftEdge(self, value: int) -> None:
        if value:
            self.horizontalScrollBar().setValue(0)


@dataclass(frozen=True)
class ThreeRegionShell:
    splitter: object
    region_a: QWidget
    region_b: QWidget
    #: The keyed output tab band. Plan 26 WP3 removed the mesh viewport from
    #: it: the viewport and the tabs used to be peers, so watching the console
    #: during a run hid the mesh entirely and inspecting the mesh hid the log.
    region_c: QTabWidget
    mesh_page: QWidget
    #: Splitter child 2 -- the whole of Region C, viewport and band together.
    region_c_host: QWidget = None                            # type: ignore[assignment]
    #: Vertical split between the viewport and the output band.
    region_c_splitter: object = None
    verdict_strip: QWidget = None                            # type: ignore[assignment]
    output_band: QWidget = None                              # type: ignore[assignment]
    #: DP-A1. The bounded scroller the page stack lives in.
    page_column: QWidget = None                              # type: ignore[assignment]
    #: DP-A1. The footer, outside that scroller and never inside it.
    action_bar: QWidget = None                               # type: ignore[assignment]
    #: Plan 40 UXC2. Opens and folds the output band; owns focus mode.
    band: object = None

    @property
    def minimum_window_width(self) -> int:
        return (
            REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM
            + 2 * self.splitter.handleWidth()
        )

    @property
    def rail(self) -> bool:
        """Plan 40 UXC1. True while the outline is the narrow rail."""
        return bool(self.region_a.property('railMode'))

    def set_rail(self, collapsed: bool) -> bool:
        """Collapse the outline to the rail, or open it. True when changed."""
        collapsed = bool(collapsed)
        if collapsed == self.rail:
            return False
        self.region_a.setProperty('railMode', collapsed)
        if collapsed:
            self.region_a.setMinimumWidth(REGION_A_RAIL)
            self.region_a.setMaximumWidth(REGION_A_RAIL)
        else:
            self.region_a.setMaximumWidth(16777215)
            self.region_a.setMinimumWidth(REGION_A_MINIMUM)
        return True

    def apply_sizes(self, total_width: int, saved=None,
                    a_preferred: int | None = None,
                    b_wanted: int | None = None, *,
                    commit: bool = True) -> tuple[int, int, int]:
        """Work out the three widths and, with ``commit``, set them.

        Plan 45 I. ``commit=False`` only measures: the settings sheet owns
        the middle width and sets all three itself, and setting them here
        first laid the window out at the shell's idea of B, then again at the
        sheet's (two 45 ms passes, one of them thrown away).
        """
        rail = self.rail
        sizes = (
            preferred_region_sizes(total_width, a_preferred, b_wanted, rail)
            if saved is None
            else clamp_region_sizes(
                total_width, saved, a_preferred, b_wanted, rail)
        )
        if commit and list(self.splitter.sizes()) != list(sizes):
            self.splitter.setSizes(list(sizes))
        return sizes

    def assert_invariants(self) -> None:
        assert self.splitter.count() == 3
        assert self.splitter.orientation() == Qt.Orientation.Horizontal
        assert not self.splitter.childrenCollapsible()
        assert tuple(
            self.splitter.widget(index).objectName() for index in range(3)
        ) == ('regionAHost', 'regionBHost', 'regionCHost')
        assert self.region_a.minimumWidth() == (
            REGION_A_RAIL if self.rail else REGION_A_MINIMUM)
        assert self.region_b.minimumWidth() == REGION_B_MINIMUM
        assert self.region_c_host.minimumWidth() == REGION_C_MINIMUM
        # WP3's whole point: the model never leaves the screen. The viewport is
        # a sibling of the output band, not a tab inside it, so no tab
        # selection can hide it.
        assert self.region_c.indexOf(self.mesh_page) < 0
        assert self.mesh_page.parent() is not self.region_c
        assert self.verdict_strip is not None
        # DP-A1. The settings column scrolls and the footer does not, so the
        # footer is never a descendant of the scroller: if it were, a tall
        # page would carry the one forward action off the bottom of the view.
        assert self.page_column is not None and self.action_bar is not None
        assert not self.page_column.isAncestorOf(self.action_bar)


#: Plan 44 MF17 (M:199, `.page-f{padding:8px 10px;border-top:1px solid}`). The
#: footer row's own padding and the one-pixel rule above it. With a 24 px
#: control the row is 1 + 8 + 24 + 8 = 41 px, the mockup's 40 +- 2.
FOOTER_PAD_H = 10
FOOTER_PAD_V = 8
FOOTER_RULE = 1


def apply_footer_metrics(bar: QWidget, layout) -> None:
    """Widen the footer from the house bar inset to the mockup's padding.

    The rule is drawn by the style sheet (``footer.qss.tmpl``) as the bar's
    top border, inside the bar, so the top padding counts it.
    """
    bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout.setContentsMargins(FOOTER_PAD_H, FOOTER_PAD_V + FOOTER_RULE,
                              FOOTER_PAD_H, FOOTER_PAD_V)


class _HideWhenDisabled(QObject):
    """Plan 44 MF17. Back is hidden where it cannot act, i.e. on step 1.

    The step manager disables Back exactly when there is no previous page
    (no case open, or the route history is one page long). A disabled Back
    on the first page was a dead button in the mockup's place for nothing,
    so the button follows its own enabled state: hidden while disabled,
    shown again when enabled. Proceed does not move, because the row is
    right-aligned behind its spacer.
    """

    def __init__(self, button: QWidget) -> None:
        super().__init__(button)
        self._button = button
        button.installEventFilter(self)
        self.sync()

    def sync(self) -> None:
        self._button.setVisible(self._button.isEnabled())

    def eventFilter(self, watched, event):
        if watched is self._button and event.type() == QEvent.Type.EnabledChange:
            self.sync()
        return False


class _CollapseWhenEmpty(QObject):
    """Plan 44 MF17. No dead band between the page column and the footer.

    The Designer row that holds the legacy Unlock and Cancel buttons kept
    its height (MEASURED 32 px at 1920x1080) with both buttons hidden, an
    empty stripe between the page and the footer. The row now shows only
    while one of its buttons does.
    """

    def __init__(self, row: QWidget) -> None:
        super().__init__(row)
        self._row = row
        self._buttons = [child for child in row.findChildren(QPushButton)
                         if child.parentWidget() is row]
        for button in self._buttons:
            button.installEventFilter(self)
        self.sync()

    def sync(self) -> None:
        if not self._buttons:
            return
        wanted = any(not button.isHidden() for button in self._buttons)
        if self._row.isHidden() == wanted:
            self._row.setVisible(wanted)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.ShowToParent,
                            QEvent.Type.HideToParent):
            self.sync()
        return False


#: Plan 45 P13. How much of the console splitter's handle stays over the view:
#: a thin grip against the card. The rest of the 8 px handle is masked away,
#: where a solid colour read as a white bar across the light theme.
CONSOLE_GRIP_PX = 3


class _ClearOverView(QObject):
    """DP-2707. Cut the console splitter's window down to what it draws.

    MF6 lays the splitter over the whole viewport and calls it transparent,
    but the 3D view is a native GL window and the card's ring is native too,
    which makes the splitter a native window stacked above the view. Windows
    does not see through a sibling window: the view drew its frame into a
    window nobody could see, so the viewport stayed black, and the chips and
    the rail drawn over it left trails. A mask on the splitter removes the
    clear part from its window region, so only the handle and the card band
    stay over the view.
    """

    def __init__(self, splitter: QWidget, spacer: QWidget,
                 card: QWidget | None = None) -> None:
        super().__init__(splitter)
        self._splitter, self._spacer, self._card = splitter, spacer, card
        for watched in (splitter, spacer) + ((card,) if card else ()):
            watched.installEventFilter(self)
        splitter.splitterMoved.connect(lambda *_: (self.sync(), self.sync_later()))
        self._pending = False
        self.sync()

    def sync_later(self) -> None:
        """Resync once the event loop has laid the splitter out.

        The filter below sees a Resize/LayoutRequest *before* the splitter
        has placed its handle and children, so a sync run from it masks the
        previous layout: after the console folded, or the window shrank, the
        mask kept a strip above the chip -- the splitter's unpainted window,
        black over the GL view. One deferred pass reads the settled layout.
        """
        if self._pending:
            return
        self._pending = True

        def run() -> None:
            self._pending = False
            try:
                self.sync()
            except RuntimeError:                  # the splitter was deleted
                pass
        QTimer.singleShot(0, self, run)

    def sync(self) -> None:
        from PySide6.QtGui import QRegion
        splitter, spacer = self._splitter, self._spacer
        region = QRegion(splitter.rect())
        if spacer.isVisibleTo(splitter):
            region = region.subtracted(QRegion(spacer.geometry()))
        card = self._card
        if card is not None and card.isVisibleTo(splitter):
            # The band and the handle are transparent outside the card, and
            # transparent over a GL window is black: keep the card's columns.
            left = card.mapTo(splitter, QPoint(0, 0)).x()
            region = region.intersected(QRegion(QRect(
                left, 0, card.width(), splitter.height())))
        # Plan 45 P13. Of the handle keep only a thin grip (its edge against
        # the card); the rest of its band is cut away, since a solid handle
        # read as a white bar and a transparent one is black over the GL view.
        handle = splitter.handle(1) if splitter.count() > 1 else None
        if handle is not None and handle.isVisibleTo(splitter):
            band = handle.geometry()
            grip = QRect(band.left(), band.bottom() + 1 - CONSOLE_GRIP_PX,
                         band.width(), CONSOLE_GRIP_PX)
            if card is not None and card.isVisibleTo(splitter):
                grip = grip.intersected(QRect(left, 0, card.width(),
                                              splitter.height()))
            region = region.subtracted(QRegion(band)).united(QRegion(grip))
        if card is not None and not card.isVisibleTo(splitter):
            # Plan 45 H. No card, nothing to take a press: the splitter is
            # no longer transparent for the mouse, so it must not cover the
            # view with an empty band either.
            region = region.subtracted(region)
        if region.isEmpty():
            # An empty mask means "no mask" to Qt; keep one pixel instead.
            region = QRegion(QRect(0, 0, 1, 1))
        if splitter.mask() != region:
            splitter.setMask(region)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize,
                            QEvent.Type.Show, QEvent.Type.Hide,
                            QEvent.Type.ShowToParent,
                            QEvent.Type.HideToParent,
                            QEvent.Type.LayoutRequest):
            self.sync()
            self.sync_later()
        return False


class _CardRing(QWidget):
    """The console card's shadow ring (``elevation.paint_ring``, level 'card').

    ``elevation.elevate`` puts its ring in the card's parent, and the card's
    parent is the output band, which is exactly the card's size, so that ring
    would be clipped to nothing. This overlay is a child of the host instead,
    stacked between the viewport and the splitter that carries the card, and
    follows the card as the splitter moves it.
    """

    def __init__(self, host: QWidget, card: QWidget, band: QWidget,
                 splitter: QWidget) -> None:
        super().__init__(host)
        self._host, self._card, self._band = host, card, band
        self._splitter = splitter
        self.setObjectName('consoleCardRing')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._margins = level_margins('card')
        for watched in (host, card, band):
            watched.installEventFilter(self)
        self.sync()

    def sync(self) -> None:
        card = self._card
        left, top, right, bottom = self._margins
        origin = card.mapTo(self._host, QPoint(0, 0))
        rect = QRect(origin, card.size()).adjusted(-left, -top, right, bottom)
        visible = card.isVisible()
        mask = (left, top, card.width(), card.height())
        self.stackUnder(self._splitter)
        # Plan 45 PINQ. Every move, resize and layout request of the host,
        # the card and the band came here, and each re-set the ring's native
        # window -- its mask and place -- whether or not either had changed
        # (MEASURED: eight window-region calls in one pin).
        key = (rect, visible, mask)
        if (key == self._synced and self.geometry() == rect
                and self.isVisible() == visible):
            return
        self._synced = key
        self.setGeometry(rect)
        self.setVisible(visible)
        # DP-2707. The ring is a native window over the GL view, where its
        # translucent shadow paints black; only the card's own rect is kept.
        from PySide6.QtGui import QRegion
        self.setMask(QRegion(QRect(*mask)))

    _synced = None

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize,
                            QEvent.Type.Show, QEvent.Type.Hide,
                            QEvent.Type.LayoutRequest):
            self.sync()
        return False

    def nativeEvent(self, event_type, message):
        # Plan 45 H. ``stackUnder`` does not restack native windows, so the
        # ring's window sat over the splitter's and took every press on the
        # card (MEASURED: Details, "--" and the folded chip all landed on
        # consoleCardRingWindow). Windows now passes the press underneath.
        from foammesh.view.theming.elevation import pass_hit_test
        # (QWidget's own nativeEvent answers "not handled" to everything.)
        return pass_hit_test(event_type, message) or (False, 0)

    def paintEvent(self, _event) -> None:
        left, top, right, bottom = self._margins
        panel = QRectF(self.rect()).adjusted(left, top, -right, -bottom)
        painter = QPainter(self)
        paint_ring(painter, 'card', panel, self.devicePixelRatioF())
        painter.end()


def _elevate_card(card: QWidget, band: QWidget, splitter: QWidget) -> _CardRing:
    """Raise the console card off the viewport (Plan 44 section 3)."""
    ring = _CardRing(splitter.parentWidget(), card, band, splitter)
    card.setProperty('foammeshElevation', 'card')
    return ring


def install_three_region_shell(ui) -> ThreeRegionShell:
    """Rehost the existing Designer objects in three permanent siblings."""
    splitter = ui.centralSplitter
    splitter.setObjectName('regionSplitter')
    splitter.setOrientation(Qt.Orientation.Horizontal)
    splitter.setChildrenCollapsible(False)

    # Detach the two old splitter children before inserting the three explicit
    # hosts.  The widgets remain alive and keep their existing controllers.
    workflow = ui.groupBox_9
    viewport = getattr(ui, 'groupBox_24', ui.renderingSplitter.parentWidget())
    ui.groupBox_24 = viewport
    workflow.setParent(None)
    viewport.setParent(None)

    region_a = QWidget(splitter)
    region_a.setObjectName('regionAHost')
    region_a.setAccessibleName('Outline of the meshing workflow')
    region_a.setMinimumWidth(REGION_A_MINIMUM)
    region_a.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
    a_layout = QVBoxLayout(region_a)
    a_layout.setContentsMargins(0, 0, 0, 0)
    ui.navigation.setParent(region_a)
    ui.navigation.setTitle('Outline')
    ui.horizontalLayout_2.setDirection(QBoxLayout.Direction.TopToBottom)
    # DP-194. The form says GAP for this layout too; the two agree by
    # arithmetic rather than by two authors typing the same number.
    ui.horizontalLayout_2.setSpacing(GAP)
    for label in ui.navigation.findChildren(QLabel):
        pixmap = label.pixmap()
        if pixmap is not None and not pixmap.isNull():
            label.hide()
    a_layout.addWidget(ui.navigation)

    region_b = QWidget(splitter)
    region_b.setObjectName('regionBHost')
    region_b.setAccessibleName('Current meshing page and its settings')
    region_b.setMinimumWidth(REGION_B_MINIMUM)
    region_b.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
    b_layout = QVBoxLayout(region_b)
    b_layout.setContentsMargins(0, 0, 0, 0)
    # Plan 44 MF17. The workflow box's own bottom margin is the one gap
    # between the page and the footer rule (8 px); the column adds none.
    b_layout.setSpacing(0)
    workflow.setParent(region_b)
    workflow.setTitle('')
    workflow.setMinimumWidth(0)
    # DP-A1. The page stack goes into a bounded scroller, in the place in the
    # workflow box's own layout that it already occupied -- so the validation
    # line, the run strip and the legacy button row that follow it stay where
    # they are, outside the scroller, and so does the footer below them.
    workflow_layout = workflow.layout()
    # Read before the scroller adopts the stack: adopting it reparents it,
    # and a reparented widget is dropped from the layout it was in.
    page_slot = max(workflow_layout.indexOf(ui.content), 0)
    page_column = PageColumn(ui.content, workflow)
    workflow_layout.insertWidget(page_slot, page_column, 1)
    # Plan 44 MF17. MEASURED at 9d64d61c: the box's frame below the layout
    # (8 px) and the layout's own bottom margin (8 px) stood 16 px between
    # the last row of the page and the footer rule. The frame's 8 px is the
    # one gap the mockup draws (`.page-body` gap above `.page-f`).
    footer_gap = workflow_layout.contentsMargins()
    footer_gap.setBottom(0)
    workflow_layout.setContentsMargins(footer_gap)
    b_layout.addWidget(workflow, 1)

    # Scene / Display is a Region B page.  Moving the existing widget leaves
    # renderingSplitter with the one and only viewport widget.
    scene = QWidget(ui.content)
    scene.setObjectName('sceneRegionBPage')
    scene.setAccessibleName('Display settings')
    scene_layout = QVBoxLayout(scene)
    scene_layout.setContentsMargins(0, 0, 0, 0)
    ui.widget_37.setParent(scene)
    scene_layout.addWidget(ui.widget_37)
    ui.content.addWidget(scene)

    # The persistent wizard action row is independent from page-specific
    # controls.  Legacy step buttons remain connected and are re-used by their
    # controllers, but the human always has one Back/Proceed location.
    action_bar = QWidget(region_b)
    action_bar.setObjectName('wizardActionBar')
    action_bar.setAccessibleName('Workflow page actions')
    action_layout = QHBoxLayout(action_bar)
    # G3. The two buttons were drawn edge to edge and read as one control
    # with a seam down it, so the bar's gap separates them.
    # DP-191. The row used to sit its buttons two pixels above its own
    # centre, and its inset now matches the shell's other bars.
    apply_bar_metrics(action_bar, action_layout)
    # Plan 44 MF17. The footer then takes the mockup's own padding and rule.
    apply_footer_metrics(action_bar, action_layout)
    action_layout.setSpacing(GAP)
    action_layout.addStretch(1)
    back = QPushButton('Back', action_bar)
    back.setObjectName('wizardBackButton')
    # DP-186. `Back` and `Proceed` are the two words on screen; a name
    # that replaces them leaves voice control with nothing to match.
    # The explanation belongs in the description slot.
    back.setAccessibleDescription(
        'Go to the previous available meshing page.')
    back._foammeshHideWhenDisabled = _HideWhenDisabled(back)
    proceed = ElidingPushButton('Proceed', action_bar)
    proceed.setObjectName('wizardProceedButton')
    proceed.setAccessibleDescription(
        'Validate and apply the current page, then move on.')
    # A6. Once a branch expands this is the only control that moves the
    # workflow forward, while each task page carries its own Update/Revert
    # buttons of the same weight. Drawing this one as the primary says which
    # idiom is the forward one.
    proceed.setProperty('foammeshRole', 'primary')
    # F6 (DP-2659). The legacy Unlock / Cancel buttons stand in the footer
    # row, left of Back, so a locked page has no 46 px band of its own
    # between the page column and the footer. They are the same buttons:
    # every connection and ButtonID mapping still reaches them.
    for legacy_name in ('unlock', 'finishCancel'):
        legacy_button = getattr(ui, legacy_name, None)
        if isinstance(legacy_button, QPushButton):
            legacy_button.setParent(action_bar)
            action_layout.addWidget(legacy_button)
            # Each shows only when the step manager asks (showButton); they
            # used to ride in a row that was hidden until one of them was.
            legacy_button.hide()
    action_layout.addWidget(back)
    # Plan 40 UXA4. The primary is a split button: the label presses, the
    # arrow beside it opens what else this page offers (Run to end, Preview
    # only, Update preview), and only the ones that apply. Drawn with no gap
    # so the two read as one control; hidden when the menu would be empty.
    split = QWidget(action_bar)
    split.setObjectName('wizardProceedSplit')
    split_layout = QHBoxLayout(split)
    split_layout.setContentsMargins(0, 0, 0, 0)
    split_layout.setSpacing(0)
    split_layout.addWidget(proceed)
    more = QToolButton(split)
    more.setObjectName('wizardProceedMore')
    more.setText('▾')
    more.setAccessibleName('More ways forward')
    more.setAccessibleDescription(
        'Other things this page can do: run to the end, preview, update.')
    more.setToolTip('More ways forward')
    more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    more.setProperty('foammeshRole', 'primary')
    more.setVisible(False)
    split_layout.addWidget(more)
    action_layout.addWidget(split)
    # DP-A2. The footer keeps its own height whatever else region B is
    # asked to fit. MEASURED at 4ed5fcb8: a run completion posted a
    # status strip into this column and the window minimum rose from
    # 892 px to 926 px, carrying the one forward button 34 px further
    # down -- off a 1,032 px screen entirely on the real display. With
    # the page column bounded above, the only way the footer can still
    # be pushed out is by being asked to shrink, so it is not askable.
    action_bar.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    b_layout.addWidget(action_bar, 0)
    # Plan 44 MF17. The legacy Unlock / Cancel row collapses while empty.
    legacy_row = getattr(ui, 'widget_4', None)
    if isinstance(legacy_row, QWidget):
        legacy_row._foammeshCollapse = _CollapseWhenEmpty(legacy_row)
        legacy_row.hide()                 # F6: it holds nothing now
    # Plan 40 UXD5. The help pane follows focus: a strip in the footer row's
    # empty left side, its body floating over the bottom of region B.
    from .help_pane import install_help_pane
    install_help_pane(ui, action_bar, region_b)

    # Region C is split vertically rather than tabbed. Before Plan 26 WP3 the
    # viewport and the console were peer tabs in one QTabWidget, so only one
    # was ever visible: watching the log during a run hid the mesh entirely,
    # and inspecting the mesh hid the log. The thing being judged has to stay
    # on screen while it is judged, so the viewport is now a sibling of the
    # output band and no tab selection can displace it.
    region_c_host = QWidget(splitter)
    region_c_host.setObjectName('regionCHost')
    region_c_host.setAccessibleName('Mesh viewport and outputs')
    region_c_host.setMinimumWidth(REGION_C_MINIMUM)
    region_c_host.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    c_layout = QGridLayout(region_c_host)
    c_layout.setContentsMargins(0, 0, 0, 0)
    c_layout.setSpacing(0)

    # Plan 44 MF6. The console is a card that floats over the bottom of the
    # viewport, not a band the viewport gives way to: the viewport fills region
    # C, and the splitter that sizes the card is laid over it. Its mask
    # (``_ClearOverView``) cuts it down to the card and a thin grip, so the
    # mouse reaches the viewport everywhere else and the card and the grip
    # take it. Its first child is a spacer standing for the part of the
    # viewport the card does not cover, so ``sizes()`` is still
    # ``[clear viewport, console]`` for everything that read it before.
    #
    # Plan 45 H. The splitter and the band were also
    # ``WA_TransparentForMouseEvents``, which Qt applies to a widget *and its
    # children*: the card inside them could never be pressed (its "--", the
    # folded chip, Details). The mask is what lets the mouse through now.
    region_c_splitter = QSplitter(Qt.Orientation.Vertical, region_c_host)
    region_c_splitter.setObjectName('regionCSplitter')
    region_c_splitter.setChildrenCollapsible(False)
    viewport.setParent(region_c_host)
    if hasattr(viewport, 'setTitle'):
        viewport.setTitle('')
    viewport.setMinimumHeight(REGION_C_VIEWPORT_MINIMUM)
    viewport.setToolTip('Persistent mesh viewport')
    c_layout.addWidget(viewport, 0, 0)
    c_layout.addWidget(region_c_splitter, 0, 0)
    viewport.lower()
    region_c_splitter.raise_()

    clear_spacer = QWidget(region_c_splitter)
    clear_spacer.setObjectName('regionCClearSpacer')
    clear_spacer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # The card and the band that places it travel together. The band is
    # unpainted (the console's width is set by margins on it; the splitter's
    # mask keeps only the card's columns) and the card is the visible panel:
    # one 28 px header row over the pages.
    output_band = QWidget(region_c_splitter)
    output_band.setObjectName('regionCOutputBand')
    output_band.setAccessibleName('Mesh verdict and output tabs')
    band_layout = QVBoxLayout(output_band)
    band_layout.setContentsMargins(0, 0, 0, 0)
    band_layout.setSpacing(0)

    console_card = QFrame(output_band)
    console_card.setObjectName('consoleCard')
    console_card.setAccessibleName('Console')
    card_layout = QVBoxLayout(console_card)
    card_layout.setContentsMargins(0, 0, 0, 0)
    card_layout.setSpacing(0)

    verdict_strip = VerdictStrip(console_card)

    region_c = QTabWidget(console_card)
    region_c.setObjectName('regionCTabHost')
    region_c.setAccessibleName('Mesh outputs')
    region_c.setTabsClosable(False)
    region_c.setMovable(False)
    region_c.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    # The header row: tabs, filter chips, verdict, size read-out, minimise.
    console_header = ConsoleHeader(console_card, region_c, verdict_strip)
    card_layout.addWidget(region_c, 1)
    card_layout.addWidget(console_header.chip)
    band_layout.addWidget(console_card)
    output_band.setMinimumHeight(
        CONSOLE_HEADER_HEIGHT + REGION_C_BAND_MINIMUM)

    region_c_splitter.addWidget(clear_spacer)
    region_c_splitter.addWidget(output_band)
    region_c_splitter._foammeshClearMask = _ClearOverView(
        region_c_splitter, clear_spacer, console_card)
    ui.consoleCardRing = _elevate_card(
        console_card, output_band, region_c_splitter)
    # The 3D view takes roughly three quarters of the height; the band is a
    # reading surface, not the subject.
    region_c_splitter.setStretchFactor(0, 3)
    region_c_splitter.setStretchFactor(1, 1)
    # R62/R114. The stretch factors above only divide *growth*; the opening
    # split comes from the size hints, and the viewport's dwarfs the band's, so
    # the report opened collapsed on to its minimum every time.
    region_c_splitter.setSizes(list(preferred_band_sizes(800)))
    # R157. The handle that resizes it was a few pixels tall and unnamed:
    # drags at y=657 and y=654 -- both over what reads as the same divider --
    # did nothing, and only y=683 worked. A wider grab target with a name and
    # a tooltip is one a user and a screen reader can both find. Plan 44 MF6:
    # it stays 8 px and named, but is painted only on hover.
    region_c_splitter.setHandleWidth(8)
    c_handle = region_c_splitter.handle(1)
    if c_handle is not None:
        c_handle.setAccessibleName('Resize mesh viewport and mesh outputs')
        c_handle.setToolTip('Drag to resize the mesh viewport and the '
                            'output band below it')
        c_handle.setCursor(Qt.CursorShape.SplitVCursor)

    splitter.insertWidget(0, region_a)
    splitter.insertWidget(1, region_b)
    splitter.insertWidget(2, region_c_host)
    for index, stretch in enumerate((0, 0, 1)):
        splitter.setStretchFactor(index, stretch)
        splitter.setCollapsible(index, False)
    for index, name in (
            (1, 'Resize guided outline and settings'),
            (2, 'Resize settings and mesh outputs')):
        handle = splitter.handle(index)
        handle.setAccessibleName(name)
        handle.setToolTip(name)

    # The old vertical ADS container has no visual owner in the current shell.
    # Detach it instead of retaining a zero-height compatibility band.
    dock_container = getattr(ui, 'dockContainer', None)
    if dock_container is not None:
        dock_container.hide()
        dock_container.setParent(None)

    ui.regionSplitter = splitter
    ui.regionAHost = region_a
    ui.regionBHost = region_b
    ui.regionCHost = region_c_host
    ui.regionCSplitter = region_c_splitter
    ui.regionCOutputBand = output_band
    ui.regionCConsoleCard = console_card
    ui.consoleHeader = console_header
    ui.regionCTabHost = region_c
    ui.meshVerdictStrip = verdict_strip
    ui.meshRegionPage = viewport
    ui.sceneSidebarPage = scene  # controller compatibility; host is Region B
    ui.sceneRegionBPage = scene
    # DP-A1. The bounded scroller the page stack lives in, published so
    # that a page controller can ask what is on screen and so that a
    # gate can measure the one widget the fix is about.
    ui.regionBPageColumn = page_column
    ui.wizardActionBar = action_bar
    ui.wizardBackButton = back
    ui.wizardProceedButton = proceed
    ui.wizardProceedMore = more

    # Plan 43 LY4: the console controller itself, not the base class swapped
    # afterwards (DP-1660). Imported here because console_band imports this
    # module.
    from foammesh.view.main_window.console_band import ConsoleBandController
    band = ConsoleBandController(
        region_c_splitter, output_band, region_c, verdict_strip,
        region_splitter_h=splitter, header=console_header)
    ui.regionCBandController = band
    # Plan 45 H: one owner for the band's height (see reveal_output_band).
    region_c_splitter._foammeshBandController = band
    shell = ThreeRegionShell(
        splitter, region_a, region_b, region_c, viewport,
        region_c_host=region_c_host, region_c_splitter=region_c_splitter,
        verdict_strip=verdict_strip, output_band=output_band,
        page_column=page_column, action_bar=action_bar, band=band)
    # UXC2: nothing to show means a one-line chip, not a 260 px empty band.
    band.close()
    shell.apply_sizes(1280)
    shell.assert_invariants()
    return shell
