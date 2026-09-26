"""Authoritative three-region shell for the FoamMesh desktop.

The Designer file still owns the mature engineering pages and rendering
widgets.  This module changes only their visual host: navigation is placed in
Region A, the one canonical page stack in Region B, and the existing viewport
in Region C.  No page or rendering widget is cloned.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QBoxLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from foammesh.view.theming.metrics import GAP, apply_bar_metrics

from .verdict_strip import VerdictStrip


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
    """
    if splitter is None or splitter.count() < 2:
        return False
    if minimum is None:
        minimum = REGION_C_BAND_PREFERRED
        band_widget = splitter.widget(1)
        if band_widget is not None:
            minimum = max(minimum,
                          band_widget.minimumSizeHint().height())
    sizes = list(splitter.sizes())
    total = sizes[0] + sizes[1]
    if total <= 0 or sizes[1] >= minimum:
        return False
    band = min(int(minimum), total - REGION_C_VIEWPORT_MINIMUM)
    if band <= sizes[1]:
        return False
    splitter.setSizes([total - band, band] + sizes[2:])
    return True


def preferred_region_sizes(
        total_width: int, a_preferred: int | None = None
) -> tuple[int, int, int]:
    """Return the FOAMFlow-aligned A/B/C allocation for ``total_width``.

    DP-134. ``a_preferred`` is what the outline says it needs to show its rows
    without eliding them; ``REGION_A_PREFERRED`` is the floor, and B and C keep
    their own minimums ahead of it, so a long task name can widen the pane but
    never starve the page or the viewport.
    """
    total_width = max(
        int(total_width),
        REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM,
    )
    a_width = max(REGION_A_PREFERRED, int(a_preferred or 0))
    b_width = (
        REGION_B_COMPACT_PREFERRED
        if total_width < REGION_B_COMPACT_BELOW else REGION_B_PREFERRED
    )
    c_width = total_width - a_width - b_width
    if c_width < REGION_C_MINIMUM:
        deficit = REGION_C_MINIMUM - c_width
        shrink_b = min(deficit, b_width - REGION_B_MINIMUM)
        b_width -= shrink_b
        deficit -= shrink_b
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
        total_width: int, sizes, a_preferred: int | None = None
) -> tuple[int, int, int]:
    """Validate a saved allocation and return a safe current-schema layout."""
    try:
        values = tuple(int(value) for value in sizes)
    except (TypeError, ValueError):
        return preferred_region_sizes(total_width, a_preferred)
    if len(values) != 3 or any(value < 0 for value in values):
        return preferred_region_sizes(total_width, a_preferred)

    total_width = max(
        int(total_width),
        REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM,
    )
    a_width = max(REGION_A_MINIMUM, values[0])
    b_width = max(REGION_B_MINIMUM, values[1])
    # A and B own their requested widths; every remaining pixel belongs to C.
    if a_width + b_width + REGION_C_MINIMUM > total_width:
        return preferred_region_sizes(total_width, a_preferred)
    return a_width, b_width, total_width - a_width - b_width


class PageColumn(QScrollArea):
    """The settings column: one page at a time, scrolled, footer outside it.

    DP-A1. Region B used to hold the page stack directly, so the stack's
    minimum size hint -- which `QStackedLayout` computes as the *largest* of
    every page it holds, shown or not -- was a term in the window's own
    minimum height. MEASURED on the real window built offscreen at
    `4ed5fcb8`: 892 px of minimum height, of which Region B asked for 823 and
    the page stack for 736, and the 736 belonged to `baseGridPage`, a page
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

    def __init__(self, stack, parent=None) -> None:
        super().__init__(parent)
        self._stack = stack
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

    @property
    def minimum_window_width(self) -> int:
        return (
            REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM
            + 2 * self.splitter.handleWidth()
        )

    def apply_sizes(self, total_width: int, saved=None,
                    a_preferred: int | None = None) -> tuple[int, int, int]:
        sizes = (
            preferred_region_sizes(total_width, a_preferred)
            if saved is None
            else clamp_region_sizes(total_width, saved, a_preferred)
        )
        self.splitter.setSizes(list(sizes))
        return sizes

    def assert_invariants(self) -> None:
        assert self.splitter.count() == 3
        assert self.splitter.orientation() == Qt.Orientation.Horizontal
        assert not self.splitter.childrenCollapsible()
        assert tuple(
            self.splitter.widget(index).objectName() for index in range(3)
        ) == ('regionAHost', 'regionBHost', 'regionCHost')
        assert self.region_a.minimumWidth() == REGION_A_MINIMUM
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
    action_layout.setSpacing(GAP)
    action_layout.addStretch(1)
    back = QPushButton('Back', action_bar)
    back.setObjectName('wizardBackButton')
    # DP-186. `Back` and `Proceed` are the two words on screen; a name
    # that replaces them leaves voice control with nothing to match.
    # The explanation belongs in the description slot.
    back.setAccessibleDescription(
        'Go to the previous available meshing page.')
    proceed = QPushButton('Proceed', action_bar)
    proceed.setObjectName('wizardProceedButton')
    proceed.setAccessibleDescription(
        'Validate and apply the current page, then move on.')
    # A6. Once a branch expands this is the only control that moves the
    # workflow forward, while each task page carries its own Update/Revert
    # buttons of the same weight. Drawing this one as the primary says which
    # idiom is the forward one.
    proceed.setProperty('foammeshRole', 'primary')
    action_layout.addWidget(back)
    action_layout.addWidget(proceed)
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
    c_layout = QVBoxLayout(region_c_host)
    c_layout.setContentsMargins(0, 0, 0, 0)
    c_layout.setSpacing(0)

    region_c_splitter = QSplitter(Qt.Orientation.Vertical, region_c_host)
    region_c_splitter.setObjectName('regionCSplitter')
    region_c_splitter.setChildrenCollapsible(False)
    viewport.setParent(region_c_splitter)
    if hasattr(viewport, 'setTitle'):
        viewport.setTitle('')
    viewport.setMinimumHeight(REGION_C_VIEWPORT_MINIMUM)
    viewport.setToolTip('Persistent mesh viewport')

    # The strip and the tab band travel together: collapsing the band leaves
    # the strip, which is what "always visible" means here.
    output_band = QWidget(region_c_splitter)
    output_band.setObjectName('regionCOutputBand')
    output_band.setAccessibleName('Mesh verdict and output tabs')
    band_layout = QVBoxLayout(output_band)
    band_layout.setContentsMargins(0, 0, 0, 0)
    band_layout.setSpacing(0)

    verdict_strip = VerdictStrip(output_band)
    band_layout.addWidget(verdict_strip)

    region_c = QTabWidget(output_band)
    region_c.setObjectName('regionCTabHost')
    region_c.setAccessibleName('Mesh outputs')
    region_c.setTabsClosable(False)
    region_c.setMovable(False)
    region_c.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    band_layout.addWidget(region_c, 1)
    output_band.setMinimumHeight(
        verdict_strip.sizeHint().height() + REGION_C_BAND_MINIMUM)

    region_c_splitter.addWidget(viewport)
    region_c_splitter.addWidget(output_band)
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
    # a tooltip is one a user and a screen reader can both find.
    region_c_splitter.setHandleWidth(8)
    c_handle = region_c_splitter.handle(1)
    if c_handle is not None:
        c_handle.setAccessibleName('Resize mesh viewport and mesh outputs')
        c_handle.setToolTip('Drag to resize the mesh viewport and the '
                            'output band below it')
        c_handle.setCursor(Qt.CursorShape.SplitVCursor)
    c_layout.addWidget(region_c_splitter, 1)

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

    shell = ThreeRegionShell(
        splitter, region_a, region_b, region_c, viewport,
        region_c_host=region_c_host, region_c_splitter=region_c_splitter,
        verdict_strip=verdict_strip, output_band=output_band,
        page_column=page_column, action_bar=action_bar)
    shell.apply_sizes(1280)
    shell.assert_invariants()
    return shell
