"""Where a Gmsh page puts a panel that lives in the tool dock (Plan 48 DK).

The Entities, Visibility and display and Plugin views panels are built by the
Gmsh pages (they need the page's session, viewport and pick providers) but
are *shown* in the rail's Gmsh tools popover.  A page calls :func:`mount`
with its panel; it answers ``False`` when no window with a dock is around
(a page built alone), and the page then keeps the closed tier it always had.
"""
from __future__ import annotations

ENTITIES = 'entities'
VISIBILITY = 'visibility'
PLUGIN_VIEWS = 'plugin_views'


def _window():
    from foammesh.app import app

    return getattr(app, 'window', None)


def dock_panel():
    """The window's Gmsh tools form, or ``None``."""
    mount = getattr(_window(), '_gmshTools', None)
    return getattr(mount, 'panel', None)


def mount(key: str, widget, owner=None) -> bool:
    """Put *widget* in the dock section *key*; *owner* is the page that built it.

    When *owner* is destroyed the panel is released with it (it is no longer
    the owner's child once it is in the dock).
    """
    panel = dock_panel()
    if panel is None or not panel.mountHosted(key, widget):
        return False
    if owner is not None:
        owner.destroyed.connect(lambda *_a: unmount(key, widget, panel))
    return True


def unmount(key: str, widget, panel=None) -> None:
    panel = panel if panel is not None else dock_panel()
    if panel is None:
        return
    try:
        panel.unmountHosted(key, widget)
    except RuntimeError:
        pass                                       # the dock is gone too
