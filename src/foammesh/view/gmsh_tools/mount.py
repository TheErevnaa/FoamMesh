"""Hang the Gmsh tool dock, its menus and its keys on the main window.

Plan 48 GM11b.  This is the only code that mounts
:mod:`foammesh.view.gmsh_tools.registry` in the window; ``main_window.py``
calls :func:`mount`, :func:`project_opened` and :func:`project_closed` and
nothing else from here.

What it does, once per window:

* the rail group "Gmsh tools" (one button, a popover with the dock's form),
  taken off the rail whenever the engine is not Gmsh;
* every registered menu item as a QAction in its menu, gated the same way;
* the keys of :mod:`tool_keys` (Alt+B, Alt+Shift+1, Ctrl+Shift+R/M/J);
* Save and Clear on the message console;
* for the local API: the window's picture, movie and watch actions on the
  case session (``register_window_actions``).
"""
from __future__ import annotations

import logging
import weakref

from PySide6.QtCore import QObject, QTimer

from foammesh.view.gmsh_tools import (
    build_setup, console_actions, dialogs, engine_switch, recent_files,
    panel_events, panels_api, registry, tool_keys, tools_api,
)
from foammesh.view.gmsh_tools.dock import GmshToolDock
from foammesh.view.gmsh_tools.menus import MenuEntries
from foammesh.view.gmsh_tools.tools_panel import GmshToolsPanel
from foammesh.view.gmsh_tools.window_actions import WindowGmshActions

logger = logging.getLogger(__name__)

RAIL_KEY = 'gmsh_tools'
RAIL_ICON = 'features.svg'
RAIL_GROUP = 3

#: Registry ids of the entries this module registers itself.
SAVE_PICTURE_ID = 'gmsh.file.save_picture'
RECORD_ID = 'gmsh.file.record'
BOXES_ID = 'gmsh.tools.bounding_boxes'
FIT_ID = 'gmsh.tools.fit_visible'


def register_builtin_entries() -> tuple:
    """Register the dock's own actions and menu items (once per process)."""
    from foammesh.core.gmsh import generate_actions, import_formats

    import_formats.register_window_entries()
    generate_actions.register_window_entries()
    known = {a.id for a in registry.actions(None)}
    made = []

    def action(entry_id, title, tooltip, menu, order):
        if entry_id in known:
            return
        made.append(registry.register_action(
            entry_id, title, _unbound(entry_id), tooltip=tooltip))
        made.append(registry.register_menu_item(
            entry_id + '.menu', menu, entry_id, order=order))

    action(SAVE_PICTURE_ID, 'Save as picture...',
           'Write the viewport as a picture: PNG, JPEG, SVG, PDF, EPS, PS, '
           'GIF, PPM, BMP, YUV, XPM, TeX, PGF or TikZ', 'File', 460)
    action(RECORD_ID, 'Record movie...',
           'Record a turn of the model as an MP4 (needs ffmpeg)', 'File', 470)
    action(BOXES_ID, 'Bounding boxes',
           'Hide or show the bounding boxes of the model entities (Alt+B)',
           'View', 900)
    action(FIT_ID, 'Fit to visible entities',
           'Reset the view to the box around the visible entities '
           '(Alt+Shift+1)', 'View', 910)
    if build_setup.ACTION_ID not in known:
        made.append(registry.register_action(
            build_setup.ACTION_ID, "Set up FoamMesh's Gmsh...",
            _unbound(build_setup.ACTION_ID),
            tooltip="Copy FoamMesh's own Gmsh 4.15.2 build into the OpenFOAM "
                    '13 runtime and use it (Revoropt, CVTRemesh)'))
        made.append(registry.register_menu_item(
            build_setup.MENU_ID, 'Settings', build_setup.ACTION_ID,
            order=900))
    return tuple(made)


def register_cvt_entries() -> bool:
    """GM16b's CVTRemesh panel entries; skipped where that module is absent."""
    try:
        from foammesh.core.gmsh import cvt_remesh
    except ImportError:
        return False
    cvt_remesh.register_window_entries()
    return True


#: Registry panel areas the dock shows itself (the Plugins area is the
#: Plugins panel's own).
DOCK_AREAS = ('dock', 'geometry.import_healing')


def _unbound(entry_id):
    def refuse():
        raise RuntimeError(f'{entry_id} runs in the window; no handler')
    return refuse


def _client():
    from foammesh.app import app
    return getattr(app, 'facadeClient', None)


def _case_session(client):
    try:
        return client.session()
    except Exception:                                 # noqa: BLE001
        return None


class GmshToolsMount(QObject):
    """Everything the dock puts in one window."""

    #: Plan 50 IP4: how long to wait, attempt by attempt, for an announced
    #: panel to be placed in the window before it is given up (a panel that
    #: is never placed in this window is not this window's).  Bounded: no
    #: timer runs once these are spent.
    PLACE_RETRY_MS = (0, 250, 1000)

    def __init__(self, window, engine=None):
        super().__init__(window)
        self.window = window
        self.engine = engine or engine_switch.context()
        self.recent = recent_files.RecentModelFiles()
        self.on_gmsh = False
        register_builtin_entries()
        register_cvt_entries()
        view = window._ui.renderingView
        self.dock = GmshToolDock(
            view=lambda: view, controller=self.controller,
            source=self._pick_source, display=self._display_panel,
            client=_client, elements=lambda: getattr(
                window._ui, 'elementDisplayPanel', None))
        self.dock.after_build = self._after_build
        self.actions = WindowGmshActions(lambda: view, self.controller, self)
        self.panel = GmshToolsPanel(self.dock, self._links(), None)
        self._addRailTool()
        self._mountRegistryPanels()
        self.menus = MenuEntries(window, self._handlers())
        self.menus.build()
        self.shortcuts = tool_keys.install(window, self._keyHandlers())
        self._recentMenu = self._addRecentMenu()
        console_actions.install(window.consoleView)
        self._connected: list = []
        # Plan 50 IP4 (DP-6270): no standing poll.  A panel announces itself
        # when it is built (panel_events) and is connected then; the whole
        # window is searched only when the route turns to Gmsh or a case
        # opens, for panels built before that.
        self._pending: list = []
        self._pendingScheduled = False
        panel_events.events().created.connect(self._panelCreated)
        engine_switch.follow(self.engine, self.gate)

    # -- the providers ----------------------------------------------------- #

    def controller(self):
        client = _client()
        if client is None:
            return None
        try:
            from foammesh.view.gmsh_workflow.compute_page import (
                sharedGenerateController)
            controller = sharedGenerateController(client)
        except Exception:                             # noqa: BLE001
            return None
        recent_files.track(controller, self.recent)
        return controller

    def _pick_source(self):
        from foammesh.view.gmsh_workflow.shared_pick import selection_service
        return selection_service().pick_source

    def _display_panel(self):
        # Plan 48 DK: the panel is in the dock (the toolbar's answer is the
        # same object).
        return self.panel.hosted('visibility')

    def reveal_section(self, key: str) -> bool:
        """Open the rail's Gmsh tools and the dock section *key* in it."""
        if self.panel.hosted(key) is None:
            return False
        rail = self.window._viewportRail
        try:
            if rail.tool(RAIL_KEY).enabled:
                rail.open(RAIL_KEY)
        except Exception:                             # noqa: BLE001
            logger.debug('the Gmsh tools did not open', exc_info=True)
        return self.panel.showSection(key)

    def _after_build(self) -> None:
        """A point was made: the Entities panel shows it next."""
        source = self._pick_source()
        reload = getattr(source, 'reload', None) or getattr(
            source, 'refresh', None)
        if callable(reload):
            try:
                reload()
            except Exception:                         # noqa: BLE001
                logger.debug('entity panel did not reload', exc_info=True)

    # -- the rail ----------------------------------------------------------- #

    def _addRailTool(self) -> None:
        rail = self.window._viewportRail
        rail.addTool(
            RAIL_KEY, self.window.tr('Gmsh tools'), RAIL_ICON, self.panel,
            tip=self.window.tr(
                'Gmsh tools: bounding boxes, point placement, previews, '
                'partitions'),
            group=RAIL_GROUP, fold=2)
        rail.setToolVisible(RAIL_KEY, False)

    def _mountRegistryPanels(self) -> int:
        """Build the registered panels that belong in the dock, once each."""
        made = 0
        held = getattr(self, '_registryPanels', None)
        if held is None:
            held = self._registryPanels = {}
        for entry in sorted(registry.panels('gmsh'), key=lambda e: e.order):
            if entry.area not in DOCK_AREAS or entry.id in held:
                continue
            try:
                widget = entry.factory(self.panel)
            except Exception:                         # noqa: BLE001
                logger.warning('panel %s did not build', entry.id,
                               exc_info=True)
                continue
            held[entry.id] = widget
            self.panel.addPanel(entry.title, widget)
            made += 1
        return made

    def give_cvt_context(self) -> int:
        """Give each CVTRemesh panel the session, viewport and Pick tool."""
        try:
            from foammesh.view.gmsh_workflow.cvt_panel import CvtRemeshPanel
        except ImportError:
            return 0
        return sum(self._giveCvt(panel)
                   for panel in self.window.findChildren(CvtRemeshPanel))

    def _giveCvt(self, panel) -> int:
        from foammesh.view.gmsh_workflow.shared_pick import SharedPick

        if getattr(panel, '_gm11bContext', False):
            return 0
        view = self.window._ui.renderingView
        panel.set_context(
            controller=self.controller, viewport=lambda: view,
            picker=SharedPick('gmsh.cvt_remesh', parent=panel))
        panel.meshEdited.connect(self._after_build)
        panel._gm11bContext = True
        return 1

    def _links(self) -> dict:
        from foammesh.view.gmsh_workflow import command_entries as entries
        from foammesh.view.gmsh_workflow import gmsh_keys as keys

        window = self.window
        # Plan 48 DK: each opens its section of the dock (the row's page is
        # shown first, which is what builds the panel).
        return {
            'entities': lambda: entries.activate_key(
                window, (keys.TIER, 'gmsh.geometry', 'gmshEntityHeader')),
            'visibility': lambda: entries.activate_key(
                window, (keys.TIER, 'gmsh.compute', keys.DISPLAY)),
            'plugin_views': lambda: entries.activate_key(
                window, (keys.TIER, 'gmsh.compute', 'gmshPluginsHeader')),
        }

    # -- the entries ------------------------------------------------------- #

    def _handlers(self) -> dict:
        from foammesh.core.gmsh import generate_actions
        from foammesh.core.gmsh import import_formats

        handlers = {
            import_formats.WATCH_ACTION_ID: self.ask_watch,
            SAVE_PICTURE_ID: self.ask_save_picture,
            RECORD_ID: self.ask_record,
            BOXES_ID: self.dock.toggle_bounding_boxes,
            FIT_ID: self.dock.reset_to_visible,
            build_setup.ACTION_ID: lambda: build_setup.run_dialog(self.window),
        }
        for action_id, _menu, _order in generate_actions.MENU_ENTRIES:
            handlers[generate_actions.MENU_PREFIX + action_id] = (
                lambda a=action_id: self.open_generate_action(a))
        return handlers

    def _keyHandlers(self) -> dict:
        return {
            'bounding_boxes': self.dock.toggle_bounding_boxes,
            'fit_visible': self.dock.reset_to_visible,
            'next_to_last': self.open_next_to_last,
            'manipulator': self.show_manipulator,
            'save_defaults': self.save_display_defaults,
        }

    # Plan 50 IP19. Each asks without a nested event loop and acts when the
    # person answers; an answer given at once still comes back to the caller.

    def _act(self, ask, act) -> dict | None:
        result = []

        def answered(answer):
            if answer is None:
                return
            try:
                result.append(act(answer))
            except ValueError as error:
                self.dock._say(str(error))

        ask(self.window, then=answered)
        return result[0] if result else None

    def ask_watch(self) -> dict | None:
        return self._act(dialogs.ask_watch,
                         lambda answer: self.actions.watch_start(*answer))

    def ask_save_picture(self) -> dict | None:
        return self._act(dialogs.ask_picture,
                         lambda answer: self.actions.save_image(*answer, {}))

    def ask_record(self) -> dict | None:
        return self._act(dialogs.ask_movie,
                         lambda path: self.actions.record_movie(path, {}))

    def open_generate_action(self, action_id: str) -> bool:
        """A Mesh menu entry: the Generate panel, on that action."""
        from foammesh.view.gmsh_workflow import command_entries as entries
        from foammesh.view.gmsh_workflow.generate_panel import (
            GmshGeneratePanel)

        page = entries._show_row(self.window, entries.GENERATE_TASK)
        if page is None:
            return False
        panel = page.findChild(GmshGeneratePanel)
        return bool(panel is not None and panel.chooseAction(action_id))

    # -- keys -------------------------------------------------------------- #

    def open_next_to_last(self) -> str:
        """Shift+Ctrl+R: open the file opened before the last one."""
        name = self.recent.next_to_last()
        controller = self.controller()
        if not name or controller is None:
            self.dock._say('There is no earlier file to open.')
            return ''
        try:
            controller.open_model(name, 'open')
        except Exception as error:                    # noqa: BLE001
            self.dock._say(str(error))
            return ''
        self.dock._say('Opened %s.' % name)
        return name

    def show_manipulator(self) -> bool:
        """Shift+Ctrl+M: the Camera tab of the display panel."""
        from foammesh.view.gmsh_workflow import command_entries as entries
        from foammesh.view.gmsh_workflow import gmsh_keys as keys

        if not entries.activate_key(
                self.window, (keys.TIER, 'gmsh.compute', keys.DISPLAY)):
            return False
        panel = self._display_panel()
        tabs = getattr(panel, '_tabs', None)
        if tabs is None:
            return False
        for index in range(tabs.count()):
            if tabs.tabText(index).replace('&', '').lower() in (
                    'camera', 'manipulator'):
                tabs.setCurrentIndex(index)
                return True
        return True

    def save_display_defaults(self) -> bool:
        """Shift+Ctrl+J: keep the display options as the defaults."""
        panel = self._display_panel()
        if panel is None:
            self.dock._say('The display panel is not open on this case.')
            return False
        panel._saveDrawing()
        self.dock._say('The display options are kept as the defaults.')
        return True

    def _addRecentMenu(self):
        from PySide6.QtWidgets import QMenu

        file_menu = self.window._ui.menuFile
        menu = QMenu(self.window.tr('Open recent Gmsh file'), file_menu)
        menu.setObjectName('gmshRecentFilesMenu')
        menu.aboutToShow.connect(lambda: self._fillRecent(menu))
        before = self.window._ui.menuOpen_Recent.menuAction()
        action = file_menu.insertMenu(before, menu)
        action.setObjectName('gmshRecentFilesAction')
        self._recentAction = action
        return menu

    def _fillRecent(self, menu) -> None:
        from pathlib import Path

        menu.clear()
        rows = self.recent.files()
        if not rows:
            menu.addAction(self.window.tr('No recent Gmsh files')).setEnabled(
                False)
            return
        for name in rows:
            action = menu.addAction(Path(name).name)
            action.setToolTip(name)
            action.setEnabled(Path(name).exists())
            action.triggered.connect(lambda _c=False, n=name: self._open(n))

    def _open(self, name: str) -> None:
        controller = self.controller()
        if controller is None:
            return
        try:
            controller.open_model(name, 'open')
        except Exception as error:                    # noqa: BLE001
            self.dock._say(str(error))

    # -- the engine -------------------------------------------------------- #

    def gate(self, on_gmsh: bool) -> None:
        """The Gmsh tools exist on the Gmsh route and nowhere else."""
        self.on_gmsh = bool(on_gmsh)
        self.window._viewportRail.setToolVisible(RAIL_KEY, self.on_gmsh)
        self.menus.gate(self.on_gmsh)
        self._recentAction.setVisible(self.on_gmsh)
        for shortcut in self.shortcuts:
            shortcut.setEnabled(self.on_gmsh)
        if self.on_gmsh:
            self.connect_panels()
        else:
            self._pending.clear()
            self.dock.stop_placement()

    # -- the panels with something to draw --------------------------------- #

    def connect_panels(self) -> int:
        """Connect every previewing panel the window holds; the new count.

        A whole-window search, run once when the route turns to Gmsh and
        when a case opens.  Panels built after that announce themselves
        (:meth:`_panelCreated`)."""
        from foammesh.view.gmsh_workflow.generate_panel import (
            GmshGeneratePanel)
        from foammesh.view.gmsh_workflow.geometry_build_panel import (
            GmshGeometryBuildPanel)
        from foammesh.view.gmsh_workflow.structure_panel import StructurePanel

        found = self._mountRegistryPanels() + self.give_cvt_context()
        window = self.window
        previewing = (window.findChildren(GmshGeometryBuildPanel)
                      + window.findChildren(StructurePanel))
        found += self.dock.connect_previews(*previewing)
        for panel in window.findChildren(GmshGeneratePanel):
            found += self._connectGenerate(panel)
        return found

    def connect_panel(self, panel) -> int:
        """Connect the one *panel*, by its kind; the new count."""
        from foammesh.view.gmsh_workflow.generate_panel import (
            GmshGeneratePanel)
        from foammesh.view.gmsh_workflow.geometry_build_panel import (
            GmshGeometryBuildPanel)
        from foammesh.view.gmsh_workflow.structure_panel import StructurePanel

        if isinstance(panel, (GmshGeometryBuildPanel, StructurePanel)):
            return self.dock.connect_previews(panel)
        if isinstance(panel, GmshGeneratePanel):
            return self._connectGenerate(panel)
        try:
            from foammesh.view.gmsh_workflow.cvt_panel import CvtRemeshPanel
        except ImportError:
            return 0
        if isinstance(panel, CvtRemeshPanel):
            return self._giveCvt(panel)
        return 0

    def _connectGenerate(self, panel) -> int:
        # DP-6407. Held weakly: the mount lives with the window, so a strong
        # list kept every closed case's generate panel, and through it the
        # whole Gmsh compute page, alive after File > Close.
        held = [ref for ref in self._connected if ref() is not None]
        self._connected = held
        if any(ref() is panel for ref in held):
            return 0
        panel.stageFinished.connect(self._stage_finished)
        self._connected.append(weakref.ref(panel))
        return 1

    def _inWindow(self, panel) -> bool:
        node = panel
        while node is not None:
            if node is self.window:
                return True
            node = node.parent()
        return False

    def _panelCreated(self, panel) -> None:
        """A panel was built: connect it once it sits in this window.

        The constructor announces before its page is placed, so the panel
        is looked at on the next turn of the loop, and again at most
        ``len(PLACE_RETRY_MS)`` times in all; then it is dropped (it belongs
        to another window, or to none).  Off the Gmsh route nothing is
        kept: turning to Gmsh searches the window."""
        if not self.on_gmsh:
            return
        self._pending.append([panel, 0])
        self._schedulePending(self.PLACE_RETRY_MS[0])

    def _schedulePending(self, delay_ms: int) -> None:
        if self._pendingScheduled:
            return
        self._pendingScheduled = True
        QTimer.singleShot(delay_ms, self, self._connectPending)

    def _connectPending(self) -> int:
        from shiboken6 import isValid

        self._pendingScheduled = False
        if not self.on_gmsh:
            self._pending = []
            return 0
        found = 0
        waiting = []
        for row in self._pending:
            panel = row[0]
            if not isValid(panel):
                continue
            if self._inWindow(panel):
                found += self.connect_panel(panel)
                continue
            row[1] += 1
            if row[1] < len(self.PLACE_RETRY_MS):
                waiting.append(row)
        self._pending = waiting
        if waiting:
            self._schedulePending(
                min(self.PLACE_RETRY_MS[r[1]] for r in waiting))
        return found

    def _stage_finished(self, stage) -> None:
        action = getattr(stage, 'action', '')
        if not getattr(stage, 'ok', False):
            return
        if action == 'reclassify_preview':
            for result in getattr(stage, 'results', ()) or ():
                if isinstance(result, dict) and 'edges' in result:
                    self.dock.show_reclassify_edges(result)
                    return
        elif action in ('reclassify', 'reclassify_subset'):
            self.dock.reclassify.clear()

    # -- the case ---------------------------------------------------------- #

    def register_with_api(self) -> bool:
        """Tell the local API this window can save pictures, record and watch."""
        from foammesh.core.facade.plan48 import gm13_routes

        session = _case_session(_client())
        if session is None:
            return False
        gm13_routes.register_window_actions(session, self.actions)
        setattr(session, tools_api.ATTRIBUTE, tools_api.ToolsApi(self))
        setattr(session, panels_api.ATTRIBUTE,
                panels_api.PanelsApi(self.window))
        self._api_session = session
        return True

    def unregister_from_api(self) -> None:
        from foammesh.core.facade.plan48 import gm13_routes

        session = getattr(self, '_api_session', None)
        if session is not None:
            gm13_routes.unregister_window_actions(session)
            setattr(session, tools_api.ATTRIBUTE, None)
            setattr(session, panels_api.ATTRIBUTE, None)
            self._api_session = None
        self.actions.watch.stop()

    def case_opened(self) -> None:
        self.register_with_api()
        self.dock.bounding.setShown(False)
        QTimer.singleShot(0, self.connect_panels)

    def case_closed(self) -> None:
        self.unregister_from_api()
        self.dock.stop_placement()
        self.dock.bounding.setShown(False)
        self.dock.clear_previews()


def mount(window):
    """Build the dock on *window*; returns the mount (``window._gmshTools``)."""
    mounted = GmshToolsMount(window)
    window._gmshTools = mounted
    return mounted


def project_opened(window) -> None:
    mounted = getattr(window, '_gmshTools', None)
    if mounted is not None:
        mounted.case_opened()


def project_closed(window) -> None:
    mounted = getattr(window, '_gmshTools', None)
    if mounted is not None:
        mounted.case_closed()
