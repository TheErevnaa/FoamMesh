#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 40 UXC3: Qt-ADS docking with saved layouts and Reset layout.

F2a / F2c. The window was a fixed three-region splitter that could not be
rearranged, and Qt-ADS (``PySide6-QtAds``) was a dependency with no
``CDockManager`` behind it. One manager now hosts the three regions:

* the **outline** and the **settings column** (page column and footer) are
  docks that can be moved, tabbed together or floated;
* the **mesh viewport** (with the output band under it) is the central widget,
  which stays put, so the model never leaves the window.

The arrangement is saved per user on close and restored at start. *View >
Reset layout* returns to the default captured the moment the docks were first
laid out. A floated dock is saved with the name of the screen it was on; it
reopens there, or, when that screen is gone, on the primary one.

Nothing in the regions changes: the hosts (``regionAHost`` and friends) are
the same widgets, only their parents are dock widgets now.
"""
from __future__ import annotations

import base64

from PySide6.QtCore import QByteArray, QObject, QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget
import PySide6QtAds as QtAds
from foammesh.settings.app_settings import LAYOUT_VERSION
from foammesh.view.theming.status_colors import strip_dock_rules

OUTLINE_DOCK = 'dockOutline'
SETTINGS_DOCK = 'dockSettings'
VIEWPORT_DOCK = 'dockViewport'

#: The docks that can leave the main splitter; the viewport cannot.
SIDE_DOCKS = (OUTLINE_DOCK, SETTINGS_DOCK)

#: Plan 43 LY2. The Outline dock is replaced by the step ribbon. Its widget
#: still exists -- it is the tree the ribbon reads and the command palette and
#: the wizard route through -- but the dock is never open, so it takes no room
#: and no arrangement, saved or default, can bring it back.
RETIRED_DOCKS = (OUTLINE_DOCK,)


def fit_to_screen(rect: QRect, screen: QRect) -> QRect:
    """``rect`` moved (never resized past the screen) to lie inside ``screen``."""
    width = min(rect.width(), screen.width())
    height = min(rect.height(), screen.height())
    x = min(max(rect.x(), screen.left()), screen.right() - width + 1)
    y = min(max(rect.y(), screen.top()), screen.bottom() - height + 1)
    return QRect(x, y, width, height)


def screen_for(name: str | None, screens) -> tuple[object | None, bool]:
    """``(screen, found)``: the screen called ``name``, else the primary one."""
    for screen in screens:
        if name is not None and screen.name() == name:
            return screen, True
    primary = QGuiApplication.primaryScreen()
    if primary is None and screens:
        primary = screens[0]
    return primary, False


class RegionSizes(QObject):
    """The widths of the three regions, whichever splitter they are in.

    Before docking, the regions were the three children of one ``QSplitter``
    and the window's sizing code spoke to it. This keeps that vocabulary
    (``sizes``, ``setSizes``, ``handleWidth``, a moved signal) over the dock
    areas that now hold the regions, so a region that has been floated simply
    reads 0 and is left alone.
    """

    moved = Signal()

    def __init__(self, layout: 'DockLayout') -> None:
        super().__init__(layout)
        self._layout = layout
        self._hooked: set[int] = set()

    # -- the splitter the regions share ----------------------------------- #

    def main(self):
        area = self._layout.dock(VIEWPORT_DOCK).dockAreaWidget()
        widget = area.parentWidget() if area is not None else None
        while widget is not None:
            if (hasattr(widget, 'orientation') and hasattr(widget, 'sizes')
                    and widget.orientation() == Qt.Orientation.Horizontal):
                return widget
            widget = widget.parentWidget()
        return None

    def hook(self) -> None:
        """Forward a drag of the handle; called again when splitters change."""
        splitter = self.main()
        if splitter is not None and id(splitter) not in self._hooked:
            self._hooked.add(id(splitter))
            splitter.splitterMoved.connect(lambda *_a: self.moved.emit())

    def _index(self, splitter, name: str) -> int:
        area = self._layout.dock(name).dockAreaWidget()
        if splitter is None or area is None or self._layout.dock(name).isClosed():
            return -1
        return splitter.indexOf(area)

    # -- the splitter vocabulary ------------------------------------------ #

    def handleWidth(self) -> int:
        splitter = self.main()
        return splitter.handleWidth() if splitter is not None else 4

    def sizes(self) -> list[int]:
        splitter = self.main()
        values = splitter.sizes() if splitter is not None else []
        result = []
        for name in (OUTLINE_DOCK, SETTINGS_DOCK, VIEWPORT_DOCK):
            index = self._index(splitter, name)
            result.append(int(values[index]) if 0 <= index < len(values) else 0)
        return result

    def setSizes(self, sizes) -> None:
        splitter = self.main()
        if splitter is None:
            return
        current = list(splitter.sizes())
        sizes = list(sizes)
        names = (OUTLINE_DOCK, SETTINGS_DOCK, VIEWPORT_DOCK)
        # A retired region has no room to give: what the caller offered it
        # goes to the viewport, so the model keeps the pixels the outline had.
        if self._layout.isRetired(OUTLINE_DOCK) and len(sizes) == 3:
            sizes[2] = int(sizes[2]) + int(sizes[0])
            sizes[0] = 0
        for name, size in zip(names, sizes):
            index = self._index(splitter, name)
            if 0 <= index < len(current):
                current[index] = int(size)
        splitter.setSizes(current)

    def count(self) -> int:
        return sum(1 for name in (OUTLINE_DOCK, SETTINGS_DOCK, VIEWPORT_DOCK)
                   if self._index(self.main(), name) >= 0)


class DockLayout(QObject):
    """The dock manager, the three regions in it, and their saved arrangement."""

    #: Emitted after the arrangement changed in a way the sizes must follow
    #: (a restore, a reset, a dock opened or closed).
    arrangementChanged = Signal()

    def __init__(self, parent: QWidget, outline: QWidget, settings: QWidget,
                 viewport: QWidget, *, retire: tuple = RETIRED_DOCKS) -> None:
        super().__init__(parent)
        self._retired = tuple(retire)
        flags = QtAds.CDockManager
        flags.setConfigFlags(
            flags.DefaultNonOpaqueConfig | flags.HideSingleCentralWidgetTitleBar)
        self._manager = QtAds.CDockManager(parent)
        self._manager.setObjectName('dockManager')
        # Qt-ADS draws a 1 px rule above every dock's content. That pixel is
        # the difference between the Mesh setup page fitting the settings
        # column of a 700 px window and scrolling by one row.
        strip_dock_rules(self._manager)
        self._docks: dict[str, QtAds.CDockWidget] = {}

        central = self._make(VIEWPORT_DOCK, 'Mesh viewport', viewport,
                             movable=False)
        central_area = self._manager.setCentralWidget(central)
        outline_dock = self._make(OUTLINE_DOCK, 'Outline', outline)
        settings_dock = self._make(SETTINGS_DOCK, 'Settings', settings)
        outline_area = self._manager.addDockWidget(
            QtAds.LeftDockWidgetArea, outline_dock, central_area)
        self._manager.addDockWidget(
            QtAds.RightDockWidgetArea, settings_dock, outline_area)

        self.pin_area_minimums()
        self.regions = RegionSizes(self)
        self.regions.hook()
        self._manager.dockAreasAdded.connect(self._arranged)
        self._manager.dockAreasRemoved.connect(self._arranged)
        self._manager.stateRestored.connect(self._arranged)
        self._default = self._manager.saveState()
        self._hidden_for_focus: list[str] = []
        self._title_bars = False
        self.set_title_bars(False)
        self._retire()

    # -- construction ------------------------------------------------------ #

    def _make(self, name: str, title: str, widget: QWidget, *,
              movable: bool = True):
        dock = QtAds.CDockWidget(self._manager, title)
        dock.setObjectName(name)
        dock.setWidget(widget, QtAds.CDockWidget.ForceNoScrollArea)
        # A region is never *closed* by its own button (it holds the footer or
        # the outline); View > Reset layout is the way back from any muddle.
        dock.setFeature(QtAds.CDockWidget.DockWidgetClosable, False)
        dock.setFeature(QtAds.CDockWidget.DockWidgetMovable, movable)
        dock.setFeature(QtAds.CDockWidget.DockWidgetFloatable, movable)
        dock.setMinimumSizeHintMode(
            QtAds.CDockWidget.MinimumSizeHintFromContent)
        self._docks[name] = dock
        return dock

    @property
    def manager(self):
        return self._manager

    def isRetired(self, name: str) -> bool:
        return name in self._retired

    def _retire(self) -> None:
        """Keep the retired docks closed whatever arrangement was applied."""
        for name in self._retired:
            dock = self._docks.get(name)
            if dock is None:
                continue
            if not dock.isClosed():
                dock.toggleView(False)

    def dock(self, name: str):
        return self._docks[name]

    @property
    def title_bars(self) -> bool:
        return self._title_bars

    def set_title_bars(self, visible: bool) -> None:
        """Show or hide the title bars of the docked, single-dock areas.

        A title bar is 21 px of the settings column's height, and the footer
        has to fit a 700 px window with it, so the bars are off until the user
        asks to arrange the panels (View > Arrange panels). An area that holds
        several docks keeps its bar (it carries their tabs), and so does every
        floating dock: neither costs the column anything.
        """
        self._title_bars = bool(visible)
        manager = self._manager
        for index in range(manager.dockAreaCount()):
            area = manager.dockArea(index)
            if area is None or area.isCentralWidgetArea():
                continue
            lone = area.dockWidgetsCount() <= 1
            area.titleBar().setVisible(
                self._title_bars or not lone or area.dockContainer().isFloating())

    def pin_area_minimums(self) -> None:
        """Hold a lone side dock's area to its content's own minimum width.

        Plan 40 UXC2. Qt-ADS caches an area's minimum size hint when a dock is
        inserted -- MEASURED at 344 px for the settings column although its
        content asks 280 px once the footer shows its usual buttons -- and the
        splitter will not go below that cache. The shell's own minimum is the
        floor the column is meant to have, so an explicit area minimum states
        it (a widget's explicit minimum beats its hint).
        """
        for name in SIDE_DOCKS:
            dock = self._docks.get(name)
            area = dock.dockAreaWidget() if dock is not None else None
            if area is None:
                continue
            lone = area.dockWidgetsCount() <= 1
            content = dock.widget()
            area.setMinimumWidth(
                int(content.minimumWidth()) if lone and content is not None
                else 0)

    def _arranged(self, *_args) -> None:
        self.pin_area_minimums()
        self.set_title_bars(self._title_bars)
        self.regions.hook()
        self.arrangementChanged.emit()

    # -- focus mode -------------------------------------------------------- #

    def hide_side_docks(self) -> list[str]:
        """Close the outline and the settings column; remember which were open."""
        self._hidden_for_focus = []
        for name in SIDE_DOCKS:
            dock = self._docks[name]
            if not dock.isClosed():
                self._hidden_for_focus.append(name)
                dock.toggleView(False)
        return list(self._hidden_for_focus)

    def show_side_docks(self) -> None:
        for name in self._hidden_for_focus:
            self._docks[name].toggleView(True)
        self._hidden_for_focus = []
        self.regions.hook()

    # -- saved layouts ------------------------------------------------------ #

    def floating(self) -> dict[str, object]:
        """Dock name to its floating container, for every floated dock."""
        result = {}
        for name, dock in self._docks.items():
            if dock.isFloating() and not dock.isClosed():
                container = dock.floatingDockContainer()
                if container is not None:
                    result[name] = container
        return result

    def snapshot(self) -> dict:
        """The arrangement as a settings record (see ``getDockLayout``)."""
        state = bytes(self._manager.saveState().toBase64().data()).decode('ascii')
        screens = {}
        for name, container in self.floating().items():
            screen = container.screen()
            if screen is not None:
                screens[name] = screen.name()
        return {'version': LAYOUT_VERSION, 'state': state, 'screens': screens}

    def restore(self, record: dict | None) -> bool:
        """Apply a saved arrangement. False (and nothing changed) when unusable."""
        if not record or not record.get('state'):
            return False
        try:
            raw = base64.b64decode(record['state'].encode('ascii'))
        except (ValueError, UnicodeEncodeError):
            return False
        if not self._manager.restoreState(QByteArray(raw)):
            return False
        self._put_floating_on_its_screen(dict(record.get('screens') or {}))
        self._retire()
        self._arranged()
        return True

    def dispose(self) -> None:
        """Take the floating windows down with the window that owns them."""
        for container in list(self.floating().values()):
            container.hide()
            container.deleteLater()

    def reset(self) -> None:
        """Back to the default arrangement."""
        self._manager.restoreState(self._default)
        for name, dock in self._docks.items():
            if dock.isClosed() and name not in self._retired:
                dock.toggleView(True)
        self._retire()
        self._arranged()

    def _put_floating_on_its_screen(self, screens: dict[str, str]) -> None:
        all_screens = QGuiApplication.screens()
        for name, container in self.floating().items():
            screen, found = screen_for(screens.get(name), all_screens)
            if screen is None:
                continue
            area = screen.availableGeometry()
            rect = container.frameGeometry()
            on_screen = area.contains(rect.center())
            if found and on_screen:
                continue
            # Its screen is gone (or it was saved somewhere else): bring it
            # fully inside the screen it should be on, size unchanged.
            fitted = fit_to_screen(rect, area)
            container.move(fitted.topLeft())


def dock_the_regions(ui, shell) -> DockLayout:
    """Rehost the shell's three regions in a dock manager (window only).

    ``install_three_region_shell`` still builds the regions; this lifts them
    out of its splitter into docks and points the shell's sizing at them.
    """
    splitter = shell.splitter
    host = splitter.parentWidget()
    layout = DockLayout(host, shell.region_a, shell.region_b,
                        shell.region_c_host)
    box = host.layout()
    box.replaceWidget(splitter, layout.manager)
    layout.host_box = box
    splitter.hide()
    object.__setattr__(shell, 'splitter', layout.regions)
    ui.regionSplitter = layout.regions
    ui.dockLayout = layout
    shell.band.use_docks(layout)
    return layout
