"""Explicit disposal of the objects that own VTK and OpenGL resources.

Plan 35, CR3 step 6 (F5, F6). A render object released by the cyclic
collector is destroyed on whichever thread the collection ran on, with
whatever OpenGL context happened to be current there. So every owner of
VTK/GL objects -- an actor, the mesh scene, a rendering widget, the split
dialog -- has a ``dispose()`` that runs on the GUI thread: it takes its props
out of the renderer, releases their graphics resources against the window
they were drawn in, disconnects its Qt signals and drops its references.
The removal paths call it; nothing waits for the collector.

The registry here counts the disposables that are still live, per class, so
a test can assert that closing a scene or a dialog leaves none behind. It
holds them weakly, and counts separately any that died without having been
disposed -- released by reference counting or reaped by the collector --
because that is exactly the path this module exists to take away.
"""

import collections
import warnings
import weakref

# Weak-reference callbacks can run inside a garbage collection, on any
# thread, possibly while this thread is already in one of the functions
# below, so the callbacks take no lock and only use single, atomic dict
# operations; the readers work on snapshots.
_live: dict[str, dict[int, weakref.ref]] = collections.defaultdict(dict)
_undisposed_deaths: collections.Counter = collections.Counter()


def _kind(obj) -> str:
    return type(obj).__name__


def track(obj, kind: str | None = None) -> None:
    """Count `obj` as live until it is disposed."""
    kind = kind or _kind(obj)
    key = id(obj)
    table = _live[kind]

    def died(ref, kind=kind, key=key, table=table):
        if table.get(key) is ref:
            table.pop(key, None)
            _undisposed_deaths[kind] += 1

    table[key] = weakref.ref(obj, died)


def untrack(obj, kind: str | None = None) -> None:
    """`obj` has been disposed."""
    key = id(obj)
    tables = [_live[kind]] if kind is not None else list(_live.values())
    for table in tables:
        ref = table.get(key)
        if ref is not None and ref() is obj:
            table.pop(key, None)


def is_tracked(obj) -> bool:
    key = id(obj)
    for table in list(_live.values()):
        ref = table.get(key)
        if ref is not None and ref() is obj:
            return True
    return False


def live(kind: str) -> int:
    """How many objects of `kind` are tracked and not yet disposed."""
    return sum(1 for ref in list(_live.get(kind, {}).values()) if ref() is not None)


def live_counts() -> dict[str, int]:
    """Every kind with at least one live object, and how many."""
    counts = {kind: live(kind) for kind in list(_live)}
    return {kind: count for kind, count in counts.items() if count}


def live_objects(kind: str) -> list:
    objects = (ref() for ref in list(_live.get(kind, {}).values()))
    return [obj for obj in objects if obj is not None]


def undisposed_deaths() -> dict[str, int]:
    """Per kind, how many tracked objects died without being disposed."""
    return {kind: n for kind, n in _undisposed_deaths.items() if n}


def reset_undisposed_deaths() -> None:
    _undisposed_deaths.clear()


def make_current(render_window) -> bool:
    """Make `render_window`'s context current if it has one; True if it did.

    Releasing a buffer or a texture calls into OpenGL, which acts on the
    context current on this thread. With two render windows open (the main
    viewport and the split preview) that is not necessarily the one the
    resource belongs to, so the owner makes its own window current first.
    A window that was never initialised, or has already been finalised, has
    no context to make current and nothing on the GPU to release.
    """
    if render_window is None:
        return False
    try:
        initialized = render_window.GetInitialized()
    except AttributeError:
        initialized = True
    except Exception:                                       # noqa: BLE001
        return False
    if not initialized:
        return False
    try:
        render_window.MakeCurrent()
    except Exception:                                       # noqa: BLE001
        return False
    return True


def release_graphics_resources(props, render_window) -> None:
    """Release each prop's GPU resources against the window it was drawn in."""
    if render_window is None or not make_current(render_window):
        return
    for prop in props:
        if prop is None:
            continue
        try:
            prop.ReleaseGraphicsResources(render_window)
        except Exception:                                   # noqa: BLE001
            pass


def disconnect_all(*signals) -> None:
    """Disconnect every slot from each signal, quietly if there are none."""
    # PySide6 warns, rather than raises, when a signal has no slots.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        for signal in signals:
            try:
                signal.disconnect()
            except Exception:                               # noqa: BLE001
                pass
