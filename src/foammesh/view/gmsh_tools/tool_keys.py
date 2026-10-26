"""The keys of the Gmsh tool dock (Plan 48 GM11b, Q7).

Gmsh's own keys for the tools the dock holds.  ``gmsh_keys.py`` records each
as "taken: ... (Gmsh tool dock)" with the key, so no key is missing by
omission; this module binds them.  A key is a window shortcut that works only
while the case is on the Gmsh route (a disabled QShortcut gives its key back
to the window), except ``x``, ``y``, ``z`` and their Shift forms, which are
letters a text box may want: they are read from the viewport only, and only
while the point placement tool is running.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut

#: ``(key as Gmsh writes it, id, what it does)``.
KEYS = (
    ('Alt+B', 'bounding_boxes', 'Hide or show the bounding boxes'),
    ('Alt+Shift+1', 'fit_visible',
     'Reset the view to the box around the visible entities'),
    ('Shift+Ctrl+R', 'next_to_last', 'Open the next-to-last opened file'),
    ('Shift+Ctrl+M', 'manipulator', 'Show the manipulator'),
    ('Shift+Ctrl+J', 'save_defaults', 'Keep the display options as defaults'),
)
PREFIX = 'toolKey_'
#: The shortcut registry lists each key under this prefix and its id.
COMMAND_PREFIX = 'gmsh.tool.'


def install(window, handlers: dict) -> list:
    """One QShortcut per key; *handlers* maps the id to a callable.

    The shortcuts are named ``toolKey_<id>`` (not ``gmsh...``: they are keys
    the window owns, and the key-table test reads them as such).  Each is
    adopted by the shortcut registry, so the user can rebind it.
    """
    from foammesh.view.main_window.shortcut_registry import REGISTRY

    made = []
    for key, key_id, _what in KEYS:
        handler = handlers.get(key_id)
        if handler is None:
            continue
        shortcut = QShortcut(QKeySequence(key), window)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setObjectName(PREFIX + key_id)
        shortcut.activated.connect(lambda h=handler: h())
        REGISTRY.adopt(COMMAND_PREFIX + key_id, shortcut)
        made.append(shortcut)
    return made
