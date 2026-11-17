"""Whether a mesh is on screen, for the Pick tool's Cells filter.

Plan 48 PK (DP-4503). The Cells filter exists only while a mesh is drawn, on
either route. The mesh viewport (``GmshMeshView``) registers itself here while
it shows a mesh; the Pick tool asks ``mesh_on_screen()`` before it offers
Cells, and the panels that show a Cells button listen for the change.

No Qt, no VTK.
"""
from __future__ import annotations

__all__ = ['register', 'unregister', 'current', 'mesh_on_screen',
           'subscribe', 'unsubscribe']

import weakref

#: Weak references: a view that is gone (a closed window, a finished test)
#: shows no mesh.
_VIEWS: list = []
_LISTENERS: list = []


def _alive() -> list:
    live = []
    for ref in list(_VIEWS):
        view = ref()
        if view is None or getattr(view, 'arrays', True) is None:
            _VIEWS.remove(ref)
        else:
            live.append(view)
    return live


def _listener(entry):
    return entry() if isinstance(entry, weakref.WeakMethod) else entry


def _notify() -> None:
    for entry in list(_LISTENERS):
        listener = _listener(entry)
        if listener is None:
            if entry in _LISTENERS:
                _LISTENERS.remove(entry)
            continue
        try:
            listener()
        except RuntimeError:               # a Qt object that is already gone
            unsubscribe(listener)
        except Exception:                  # noqa: BLE001
            pass


def register(view) -> None:
    """*view* shows a mesh (``pickElement`` / ``element_ids`` /
    ``attach_picker``)."""
    _drop(view)
    _VIEWS.append(weakref.ref(view))
    _notify()


def _drop(view) -> bool:
    for ref in list(_VIEWS):
        if ref() is view:
            _VIEWS.remove(ref)
            return True
    return False


def unregister(view) -> None:
    if _drop(view):
        _notify()


def current(host=None):
    """The mesh view most recently shown, or None.  With *host* (a
    viewport), the one drawing in that viewport."""
    for view in reversed(_alive()):
        if host is None or getattr(view, '_view', None) is host:
            return view
    return None


def mesh_on_screen(host=None) -> bool:
    return current(host) is not None


def subscribe(listener) -> None:
    """Call *listener* when the mesh on screen changes.

    DP-6407. A bound method is held weakly: this list lives as long as the
    app, so a strong one kept every closed case's Gmsh entity panel -- and its
    whole geometry page -- alive after File > Close."""
    # Dead entries go here too, not only when the mesh changes (two dead
    # entity-panel entries every ten rounds in the live soak, trace-20a).
    _LISTENERS[:] = [entry for entry in _LISTENERS if _listener(entry) is not None]
    if any(_listener(entry) == listener for entry in _LISTENERS):
        return
    if getattr(listener, '__self__', None) is not None and hasattr(listener, '__func__'):
        _LISTENERS.append(weakref.WeakMethod(listener))
    else:
        _LISTENERS.append(listener)


def unsubscribe(listener) -> None:
    for entry in list(_LISTENERS):
        found = _listener(entry)
        if found is None or found == listener:
            _LISTENERS.remove(entry)
