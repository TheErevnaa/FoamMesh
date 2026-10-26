"""Registry of Gmsh-route window entries (Plan 48 seam, contract only).

A package that wants a panel, an action or a menu entry on the Gmsh route
registers it here by id, from its own module, and never edits
``main_window.py``, the menus or the rail. Only GM11b mounts the registry in
the window (its mount lines in ``main_window.py``); until then registering
has no visible effect.

Nothing here touches Qt: a ``factory`` is a callable the mounting code calls
with the parent widget, and a ``handler`` is a callable the mounting code
connects to the entry. Ids are dotted lower-case words
(``gmsh.tools.statistics``). An id is registered once; a second registration
raises, so two packages cannot silently replace each other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

#: Engines an entry can be limited to (R6: Gmsh's tools hide on snappy).
ENGINE_GMSH = 'gmsh'
ENGINE_SNAPPY = 'snappy'
ENGINE_ANY = 'any'
_ENGINES = (ENGINE_GMSH, ENGINE_SNAPPY, ENGINE_ANY)


class RegistryError(ValueError):
    """An entry was registered twice, or with an unusable id or field."""


@dataclass(frozen=True)
class PanelEntry:
    id: str
    title: str
    factory: Callable
    engine: str = ENGINE_GMSH
    area: str = 'dock'           # where it is placed: 'dock', 'plugins', ...
    order: int = 1000


@dataclass(frozen=True)
class ActionEntry:
    id: str
    title: str
    handler: Callable
    engine: str = ENGINE_GMSH
    shortcut: str = ''
    tooltip: str = ''
    enabled_when: Callable | None = None


@dataclass(frozen=True)
class MenuEntry:
    id: str
    menu: str                    # menu path, e.g. 'Tools' or 'Tools/Mesh'
    action_id: str               # an id registered as an action
    engine: str = ENGINE_GMSH
    order: int = 1000


@dataclass
class _Tables:
    panels: dict = field(default_factory=dict)
    actions: dict = field(default_factory=dict)
    menu_items: dict = field(default_factory=dict)


_tables = _Tables()


def _check(entry_id, engine):
    if not isinstance(entry_id, str) or not entry_id or entry_id != entry_id.strip():
        raise RegistryError(f'bad entry id {entry_id!r}')
    if engine not in _ENGINES:
        raise RegistryError(f'{entry_id}: engine must be one of {_ENGINES}')


def _put(table, entry):
    if entry.id in table:
        raise RegistryError(f'{entry.id} is already registered')
    table[entry.id] = entry
    return entry


def register_panel(entry_id, title, factory, *, engine=ENGINE_GMSH,
                   area='dock', order=1000) -> PanelEntry:
    _check(entry_id, engine)
    if not callable(factory):
        raise RegistryError(f'{entry_id}: factory must be callable')
    return _put(_tables.panels, PanelEntry(
        entry_id, title, factory, engine, area, order))


def register_action(entry_id, title, handler, *, engine=ENGINE_GMSH,
                    shortcut='', tooltip='', enabled_when=None) -> ActionEntry:
    _check(entry_id, engine)
    if not callable(handler):
        raise RegistryError(f'{entry_id}: handler must be callable')
    return _put(_tables.actions, ActionEntry(
        entry_id, title, handler, engine, shortcut, tooltip, enabled_when))


def register_menu_item(entry_id, menu, action_id, *, engine=ENGINE_GMSH,
                       order=1000) -> MenuEntry:
    _check(entry_id, engine)
    if not menu:
        raise RegistryError(f'{entry_id}: a menu entry needs a menu')
    return _put(_tables.menu_items, MenuEntry(
        entry_id, menu, action_id, engine, order))


def _for_engine(table, engine):
    rows = [e for e in table.values()
            if engine is None or e.engine in (ENGINE_ANY, engine)]
    return tuple(sorted(rows, key=lambda e: (getattr(e, 'order', 1000), e.id)))


def panels(engine=None) -> tuple:
    """Panels for ``engine`` (all when ``None``), by order then id."""
    return _for_engine(_tables.panels, engine)


def actions(engine=None) -> tuple:
    return _for_engine(_tables.actions, engine)


def menu_items(engine=None) -> tuple:
    return _for_engine(_tables.menu_items, engine)


def dangling_menu_items() -> tuple:
    """Menu entries whose action id was never registered (a mount check)."""
    return tuple(m for m in _tables.menu_items.values()
                 if m.action_id not in _tables.actions)


def clear() -> None:
    """Forget everything; for tests only."""
    _tables.panels.clear()
    _tables.actions.clear()
    _tables.menu_items.clear()
