"""The registry's menu entries, built in the window's menus (Plan 48 GM11b).

Packages register actions and menu items in ``registry`` by id and never touch
a menu.  :func:`build` is the one place that does: each registered menu item
becomes a QAction in the menu its path names (``File``, ``Mesh/Reverse``...),
in ``order``, shown while its engine is the active one.  The registry's
contract is not changed.
"""
from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu

from foammesh.view.gmsh_tools import registry

#: Registry top-level menu word -> the window's attribute name.
TOP_MENUS = {'File': 'menuFile', 'Edit': 'menuEdit', 'Mesh': 'menuMesh',
             'View': 'menuView', 'Settings': 'menuSettings',
             'Help': 'menuHelp'}


def _top_menu(window, word: str) -> QMenu:
    name = TOP_MENUS.get(word)
    menu = getattr(window._ui, name, None) if name else None
    if menu is not None:
        return menu
    bar = window.menuBar()
    for existing in bar.findChildren(QMenu):
        if existing.title().replace('&', '') == word:
            return existing
    return bar.addMenu(window.tr(word))


def menu_for(window, path: str) -> QMenu:
    """The QMenu for ``'Mesh/Reverse'``, creating submenus as needed."""
    parts = [p for p in str(path).split('/') if p]
    menu = _top_menu(window, parts[0])
    for word in parts[1:]:
        found = None
        for action in menu.actions():
            held = action.menu()
            if held is not None and held.title().replace('&', '') == word:
                found = held
                break
        if found is None:
            found = QMenu(window.tr(word), menu)
            menu.addMenu(found)
            found.setObjectName('gmshMenu_' + path.replace('/', '_')
                                .replace(' ', '_'))
        menu = found
    return menu


class MenuEntries:
    """The QActions made from the registry; gated by the engine."""

    def __init__(self, window, handlers: dict | None = None):
        self._window = window
        self._handlers = dict(handlers or {})
        self.actions: dict = {}
        self._entries: dict = {}
        self._placed: dict = {}

    def build(self) -> int:
        """Make an action for every menu item not yet built; the count."""
        made = 0
        action_table = {a.id: a for a in registry.actions(None)}
        for item in registry.menu_items(None):
            if item.id in self.actions:
                continue
            entry = action_table.get(item.action_id)
            if entry is None:
                continue
            handler = self._handlers.get(entry.id) or entry.handler
            action = QAction(entry.title, self._window)
            action.setObjectName(entry.id)
            if entry.tooltip:
                action.setToolTip(entry.tooltip)
                action.setStatusTip(entry.tooltip)
            if entry.shortcut:
                action.setShortcut(QKeySequence(entry.shortcut))
                # A menu key is a row of the shortcut registry, so the user
                # can rebind it and the sheet lists it.
                from foammesh.view.main_window import shortcut_registry
                command_id = 'gmsh.menu.' + entry.id
                if command_id in shortcut_registry.BY_ID:
                    shortcut_registry.REGISTRY.adopt(command_id, action)
            action.triggered.connect(lambda _c=False, h=handler: h())
            menu = menu_for(self._window, item.menu)
            self._insert(menu, action, item.order)
            self.actions[item.id] = action
            self._entries[item.id] = (item, entry)
            self._placed[item.id] = menu
            made += 1
        return made

    @staticmethod
    def _insert(menu: QMenu, action: QAction, order: int) -> None:
        """Keep the registry's own entries in ``order``; ours go after the
        window's (the window's carry no order, so they stay first)."""
        action.setProperty('gmshMenuOrder', int(order))
        before = None
        for existing in menu.actions():
            at = existing.property('gmshMenuOrder')
            if at is not None and int(at) > int(order):
                before = existing
                break
        if before is None:
            menu.addAction(action)
        else:
            menu.insertAction(before, action)

    def gate(self, on_gmsh: bool) -> None:
        """Show Gmsh entries on the Gmsh route; the others always."""
        for item_id, action in self.actions.items():
            item, entry = self._entries[item_id]
            wanted = entry.engine in (registry.ENGINE_ANY,) or (
                entry.engine == registry.ENGINE_GMSH and on_gmsh)
            enabled = True
            if wanted and entry.enabled_when is not None:
                try:
                    enabled = bool(entry.enabled_when())
                except Exception:                     # noqa: BLE001
                    enabled = False
            action.setVisible(wanted)
            action.setEnabled(wanted and enabled)
        for menu in {id(m): m for m in self._placed.values()}.values():
            self._fold(menu)

    def state(self) -> list:
        """One row per menu entry as the window has it (Plan 48 PA4).

        ``visible`` and ``enabled`` are what the QAction holds, so a route
        and the menu agree on which entries show on the Gmsh route and which
        are greyed because their precondition fails.
        """
        rows = []
        for item_id, action in self.actions.items():
            item, entry = self._entries[item_id]
            rows.append({
                'id': item_id, 'action': entry.id, 'title': entry.title,
                'menu': item.menu, 'engine': entry.engine,
                'shortcut': entry.shortcut or '',
                'visible': action.isVisible(), 'enabled': action.isEnabled(),
                'tooltip': entry.tooltip or ''})
        return rows

    @staticmethod
    def _fold(menu: QMenu) -> None:
        """A submenu this module made is hidden while it has nothing shown."""
        if not str(menu.objectName()).startswith('gmshMenu_'):
            return
        shown = any(a.isVisible() for a in menu.actions())
        menu.menuAction().setVisible(shown)
