"""Authoritative three-region shell for the FoamMesh desktop.

The Designer file still owns the mature engineering pages and rendering
widgets.  This module changes only their visual host: navigation is placed in
Region A, the one canonical page stack in Region B, and the existing viewport
in Region C.  No page or rendering widget is cloned.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSplitter,
    QTabWidget, QVBoxLayout, QWidget,
)

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

    R114. `Details` beside the verdict line raised the Mesh Quality tab into a
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


def preferred_region_sizes(total_width: int) -> tuple[int, int, int]:
    """Return the FOAMFlow-aligned A/B/C allocation for ``total_width``."""
    total_width = max(
        int(total_width),
        REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM,
    )
    a_width = REGION_A_PREFERRED
    b_width = (
        REGION_B_COMPACT_PREFERRED
        if total_width < 1600 else REGION_B_PREFERRED
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


def clamp_region_sizes(
        total_width: int, sizes) -> tuple[int, int, int]:
    """Validate a saved allocation and return a safe current-schema layout."""
    try:
        values = tuple(int(value) for value in sizes)
    except (TypeError, ValueError):
        return preferred_region_sizes(total_width)
    if len(values) != 3 or any(value < 0 for value in values):
        return preferred_region_sizes(total_width)

    total_width = max(
        int(total_width),
        REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM,
    )
    a_width = max(REGION_A_MINIMUM, values[0])
    b_width = max(REGION_B_MINIMUM, values[1])
    # A and B own their requested widths; every remaining pixel belongs to C.
    if a_width + b_width + REGION_C_MINIMUM > total_width:
        return preferred_region_sizes(total_width)
    return a_width, b_width, total_width - a_width - b_width


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

    @property
    def minimum_window_width(self) -> int:
        return (
            REGION_A_MINIMUM + REGION_B_MINIMUM + REGION_C_MINIMUM
            + 2 * self.splitter.handleWidth()
        )

    def apply_sizes(self, total_width: int, saved=None) -> tuple[int, int, int]:
        sizes = (
            preferred_region_sizes(total_width)
            if saved is None else clamp_region_sizes(total_width, saved)
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
    region_a.setAccessibleName('Region A - guided meshing workflow')
    region_a.setMinimumWidth(REGION_A_MINIMUM)
    region_a.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
    a_layout = QVBoxLayout(region_a)
    a_layout.setContentsMargins(0, 0, 0, 0)
    ui.navigation.setParent(region_a)
    ui.navigation.setTitle('Outline')
    ui.horizontalLayout_2.setDirection(QBoxLayout.Direction.TopToBottom)
    ui.horizontalLayout_2.setSpacing(2)
    for label in ui.navigation.findChildren(QLabel):
        pixmap = label.pixmap()
        if pixmap is not None and not pixmap.isNull():
            label.hide()
    a_layout.addWidget(ui.navigation)

    region_b = QWidget(splitter)
    region_b.setObjectName('regionBHost')
    region_b.setAccessibleName('Region B - current meshing page and settings')
    region_b.setMinimumWidth(REGION_B_MINIMUM)
    region_b.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
    b_layout = QVBoxLayout(region_b)
    b_layout.setContentsMargins(0, 0, 0, 0)
    workflow.setParent(region_b)
    workflow.setTitle('')
    workflow.setMinimumWidth(0)
    b_layout.addWidget(workflow)

    # Scene / Display is a Region B page.  Moving the existing widget leaves
    # renderingSplitter with the one and only viewport widget.
    scene = QWidget(ui.content)
    scene.setObjectName('sceneRegionBPage')
    scene.setAccessibleName('Scene and display settings')
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
    action_bar.setAccessibleName('Wizard page actions')
    action_layout = QHBoxLayout(action_bar)
    action_layout.setContentsMargins(8, 4, 8, 8)
    # G3. The two buttons were drawn edge to edge and read as one control
    # with a seam down it.
    action_layout.setSpacing(8)
    action_layout.addStretch(1)
    back = QPushButton('Back', action_bar)
    back.setObjectName('wizardBackButton')
    back.setAccessibleName('Go to previous available meshing page')
    proceed = QPushButton('Proceed', action_bar)
    proceed.setObjectName('wizardProceedButton')
    proceed.setAccessibleName(
        'Validate and apply the current page, then proceed')
    # A6. Once a branch expands this is the only control that moves the
    # workflow forward, while each task page carries its own Update/Revert
    # buttons of the same weight. Drawing this one as the primary says which
    # idiom is the forward one.
    proceed.setProperty('foammeshRole', 'primary')
    action_layout.addWidget(back)
    action_layout.addWidget(proceed)
    b_layout.addWidget(action_bar)

    # Region C is split vertically rather than tabbed. Before Plan 26 WP3 the
    # viewport and the console were peer tabs in one QTabWidget, so only one
    # was ever visible: watching the log during a run hid the mesh entirely,
    # and inspecting the mesh hid the log. The thing being judged has to stay
    # on screen while it is judged, so the viewport is now a sibling of the
    # output band and no tab selection can displace it.
    region_c_host = QWidget(splitter)
    region_c_host.setObjectName('regionCHost')
    region_c_host.setAccessibleName('Region C - mesh viewport and outputs')
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
    region_c.setAccessibleName('Region C - mesh outputs')
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
    ui.wizardActionBar = action_bar
    ui.wizardBackButton = back
    ui.wizardProceedButton = proceed

    shell = ThreeRegionShell(
        splitter, region_a, region_b, region_c, viewport,
        region_c_host=region_c_host, region_c_splitter=region_c_splitter,
        verdict_strip=verdict_strip, output_band=output_band)
    shell.apply_sizes(1280)
    shell.assert_invariants()
    return shell
