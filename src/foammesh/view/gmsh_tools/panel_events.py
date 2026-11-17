"""The Gmsh panels the tool dock connects say when they are built.

Plan 50 IP4 (UI-3, DP-6270).  The mount used to look for these panels with a
1.5 s timer that ran ``findChildren`` over the whole window for as long as
the Gmsh route was on; the watchdog held hundreds of GUI stalls on it.  Now
each panel that previews something, or reports a finished stage, calls
:func:`announce` at the end of its constructor, and the mount connects that
one panel (:meth:`GmshToolsMount._panelCreated`).

Nothing here knows the mount or the window: a panel built in a test, with no
window, announces to nobody.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class _PanelEvents(QObject):
    #: A panel the dock connects was built; the argument is the panel.
    created = Signal(object)


_events: _PanelEvents | None = None


def events() -> _PanelEvents:
    """The process-wide notifier (made on first use, on the GUI thread)."""
    global _events
    if _events is None:
        _events = _PanelEvents()
    return _events


def announce(panel) -> None:
    """*panel* was built; whoever connects such panels is told."""
    events().created.emit(panel)
