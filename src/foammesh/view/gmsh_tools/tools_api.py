"""What the Gmsh tool dock does for the local API (Plan 48 GM11b).

One object per window, registered on the case session by the mount; the
routes in ``core/facade/plan48/gm11b_routes.py`` call its methods, so every
control the dock has in the window is a route and both run the same code.
Methods run on the GUI thread (``on_gui_thread``) and refuse with
``ValueError(reason)``.
"""
from __future__ import annotations

from foammesh.view.gmsh_tools import console_actions
from foammesh.view.gmsh_tools.window_actions import on_gui_thread

ATTRIBUTE = 'gmsh_tool_dock'


class ToolsApi:
    def __init__(self, mount):
        self._mount = mount

    @property
    def dock(self):
        return self._mount.dock

    def _run(self, function, *args):
        return on_gui_thread(function, *args)

    # -- state ------------------------------------------------------------ #

    def state(self) -> dict:
        def read():
            state = self.dock.state()
            state['on_gmsh'] = self._mount.on_gmsh
            panel = self._mount.panel
            state['sections'] = {
                name: {'hosted': panel.hosted(key) is not None,
                       'open': panel.sectionOpen(key)}
                for name, key in self.SECTIONS.items()}
            from foammesh.view.gmsh_tools.mount import RAIL_KEY
            state['tools_open'] = self._mount.window._viewportRail.isOpen(
                RAIL_KEY)
            return state
        return self._run(read)

    # -- bounding boxes ---------------------------------------------------- #

    def bounding_boxes(self, shown) -> dict:
        def run():
            self.dock.set_bounding_boxes(bool(shown))
            return {'shown': self.dock.bounding.shown(),
                    'count': len(self.dock.bounding.boxes())}
        return self._run(run)

    def fit_visible(self) -> dict:
        def run():
            bounds = self.dock.reset_to_visible()
            if not bounds:
                raise ValueError('there are no visible entities to fit')
            return {'bounds': [float(v) for v in bounds]}
        return self._run(run)

    # -- point placement --------------------------------------------------- #

    def placement(self, active=None, freeze=None, move_only=None, plane=None,
                  offset=None, grid=None, size=None) -> dict:
        from foammesh.view.gmsh_tools.point_placement import PlacementRefusal

        def run():
            tool = self.dock.placement
            rules = tool.placement
            try:
                for axis, on in dict(freeze or {}).items():
                    rules.freeze(axis, bool(on))
                if move_only is not None:
                    wanted = str(move_only).lower() or None
                    if wanted != rules.only:
                        rules.move_only(wanted)
                if plane is not None or offset is not None:
                    rules.set_plane(plane or rules.plane,
                                    rules.offset if offset is None
                                    else float(offset))
                if grid is not None:
                    rules.set_grid(grid)
                if size is not None:
                    rules.set_size(size)
            except PlacementRefusal as error:
                raise ValueError(str(error)) from error
            if active is True and not self.dock.start_placement():
                raise ValueError(tool.status() or 'no viewport')
            if active is False:
                self.dock.stop_placement()
            return {'active': tool.active(), **rules.state()}
        return self._run(run)

    def place_point(self, x, y, z) -> dict:
        def run():
            tool = self.dock.placement
            account = tool.click_world((float(x), float(y), float(z)))
            if account is None:
                raise ValueError(tool.status() or 'the point was not placed')
            return {'placed': tool.placed()[-1], 'result': account}
        return self._run(run)

    # -- partitions -------------------------------------------------------- #

    def partition(self, colour=None, ghost_cells=None) -> dict:
        def run():
            if colour is not None and not self.dock.set_partition_colour(
                    bool(colour)):
                raise ValueError(self.dock.status)
            if ghost_cells is not None and not self.dock.set_ghost_cells(
                    bool(ghost_cells)):
                raise ValueError(self.dock.status)
            return {'colour': self.dock.partition_colour(),
                    'ghost_cells': self.dock.ghost_cells()}
        return self._run(run)

    # -- previews ---------------------------------------------------------- #

    def previews(self, clear=False) -> dict:
        def run():
            if clear:
                self.dock.clear_previews()
            return {'sweep_segments': self.dock.sweep.count,
                    'transfinite_painted': len(self.dock.qualify.painted),
                    'reclassify_edges': self.dock.reclassify.count}
        return self._run(run)

    # -- console ----------------------------------------------------------- #

    def console(self, action, path=None) -> dict:
        def run():
            console = self._mount.window.consoleView
            if action == 'read':
                text = console_actions.log_text(console)
                return {'text': text, 'lines': len(text.splitlines())}
            if action == 'save':
                return console_actions.save_log(console, path or '')
            if action == 'clear':
                return console_actions.clear_log(console)
            raise ValueError('console action must be read, save or clear')
        return self._run(run)

    # -- recent files ------------------------------------------------------ #

    def recent(self, open_index=None) -> dict:
        def run():
            rows = self._mount.recent.files()
            if open_index is not None:
                if not 0 <= int(open_index) < len(rows):
                    raise ValueError('there is no recent file number %s'
                                     % open_index)
                self._mount._open(rows[int(open_index)])
            return {'files': rows}
        return self._run(run)

    # -- opening the panels the dock links to (Plan 48 PA4) -------------------- #

    #: The three links of the dock's Model group, by the key it gives them.
    LINKS = {'entities': 'entities', 'visibility': 'visibility',
             'plugins': 'plugin_views'}
    TARGETS = ('entities', 'visibility', 'plugins', 'manipulator', 'tools',
               'generate_action')

    def open_panel(self, target, action=None) -> dict:
        """What a person does to bring a panel forward.

        ``entities``, ``visibility`` and ``plugins`` open that section of the
        dock (Plan 48 DK: the panels are in the dock now);
        ``manipulator`` is Shift+Ctrl+M (the Camera tab); ``tools`` opens the
        rail's Gmsh tools popover; ``generate_action`` is a Mesh menu entry
        (the Generate panel with *action* chosen).
        """
        def run():
            mount = self._mount
            if target not in self.TARGETS:
                raise ValueError('target must be one of: '
                                 + ', '.join(self.TARGETS))
            if not mount.on_gmsh:
                raise ValueError('the Gmsh tools are on the Gmsh route '
                                 'only; this case is not on it')
            if target in self.LINKS:
                opened = mount.panel._link(self.LINKS[target])
            elif target == 'manipulator':
                opened = mount.show_manipulator()
            elif target == 'tools':
                from foammesh.view.gmsh_tools.mount import RAIL_KEY
                rail = mount.window._viewportRail
                tool = rail.tool(RAIL_KEY)
                if not tool.enabled:
                    raise ValueError(
                        tool.reason or 'the Gmsh tools are not available '
                        'now')
                rail.open(RAIL_KEY)
                opened = rail.isOpen(RAIL_KEY)
            else:
                from foammesh.core.gmsh import generate_actions
                known = [a for a, _menu, _order
                         in generate_actions.MENU_ENTRIES]
                if action not in known:
                    raise ValueError('action must be one of: '
                                     + ', '.join(known))
                opened = mount.open_generate_action(action)
            if not opened:
                raise ValueError(self.dock.status
                                 or 'the panel could not be opened')
            from foammesh.view.gmsh_tools.mount import RAIL_KEY
            # Plan 48 DK: the three links open a section of the dock.
            section = (mount.panel.sectionOpen(self.LINKS[target])
                       if target in self.LINKS else None)
            return {'target': target, 'action': action, 'opened': True,
                    'tools_open': mount.window._viewportRail.isOpen(RAIL_KEY),
                    'section_open': section, 'status': self.dock.status}
        return self._run(run)

    #: Plan 48 DK: the dock sections, by the name the route gives them.
    SECTIONS = {'entities': 'entities', 'visibility': 'visibility',
                'plugins': 'plugin_views'}

    def set_section(self, section, opened=True) -> dict:
        """Open or close a section of the dock, as its title bar does.

        Opening also brings the Gmsh tools forward (``open_panel`` does the
        rest: it builds the panel by showing the row first).
        """
        def run():
            mount = self._mount
            if section not in self.SECTIONS:
                raise ValueError('section must be one of: '
                                 + ', '.join(self.SECTIONS))
            if not mount.on_gmsh:
                raise ValueError('the Gmsh tools are on the Gmsh route '
                                 'only; this case is not on it')
            key = self.SECTIONS[section]
            panel = mount.panel
            if panel.hosted(key) is None:
                raise ValueError(
                    'the %s panel is not in the dock yet: open it with '
                    'gmsh.tools.open first' % section)
            if opened:
                mount.reveal_section(key)
            else:
                panel.hideSection(key)
            return {'section': section, 'open': panel.sectionOpen(key)}
        return self._run(run)

    def sections(self) -> dict:
        """Which dock sections hold a panel and which are open."""
        def run():
            panel = self._mount.panel
            return {name: {'hosted': panel.hosted(key) is not None,
                           'open': panel.sectionOpen(key)}
                    for name, key in self.SECTIONS.items()}
        return self._run(run)

    def menus(self) -> dict:
        """The Gmsh menu entries and keys as the window has them."""
        from foammesh.view.gmsh_tools import tool_keys

        def run():
            mount = self._mount
            enabled = {s.objectName(): s.isEnabled()
                       for s in mount.shortcuts}
            return {
                'on_gmsh': mount.on_gmsh,
                'entries': mount.menus.state(),
                'keys': [{'key': key, 'id': key_id, 'what': what,
                          'enabled': enabled.get(tool_keys.PREFIX + key_id,
                                                 False)}
                         for key, key_id, what in tool_keys.KEYS]}
        return self._run(run)

    # -- statistics -------------------------------------------------------- #

    def geometry_counts(self, visible_only=None) -> dict:
        from foammesh.view.gmsh_workflow.statistics_panel import (
            GmshStatisticsPanel)

        def run():
            panels = self._mount.window.findChildren(GmshStatisticsPanel)
            if not panels:
                raise ValueError('the statistics panel is not open')
            panel = panels[0]
            if visible_only is not None:
                panel.setVisibleOnly(bool(visible_only))
            panel.refreshGeometry()
            return {'counts': panel.geometryCounts(),
                    'visible_only': panel.visibleOnly()}
        return self._run(run)
