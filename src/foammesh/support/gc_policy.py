"""When the cyclic garbage collector runs, and on which thread.

Plan 35, CR3 step 8 (F5, D5). The cyclic collector releases whatever is
unreachable on whichever thread happened to trip the gen-0 threshold. For
most objects that does not matter; for a VTK render object or a Qt object
with events posted for it, running its destructor on a worker thread is the
access violation behind the 28 Sep crash. So the collector is steered, never
switched off (disabling it is interpreter-wide and would let cyclic
garbage grow without bound):

* the start-up heap is frozen once, after the main window exists and before
  the first project opens, so every later collection skips it;
* the gen-0 threshold is raised once, so an automatic collection is rare;
* a GUI-thread timer runs a young collection once the event loop has been
  idle for two seconds, so garbage is reaped where the render objects live;
* a full collection runs on the GUI thread after every project close, the
  moment large cyclic structures become garbage; after a scene swap it runs
  once the window has been quiet for a moment (`collect_full_when_quiet`),
  because a full pass over a big session costs 100 ms or more and a load is
  the one moment the user is watching the window answer (Plan 39 VI14b);
* a full collection runs on the GUI thread before a theme is applied,
  because applying one re-polishes every live widget in one Qt loop
  that calls back into Python, and a collection tripped inside that
  loop destroys widgets Qt is still walking.

This is the one module in ``src`` allowed to call ``gc.freeze``,
``gc.disable``, ``gc.enable``, ``gc.set_threshold`` or ``gc.collect``; a
grep gate in the unit tests holds that line.
"""

import gc
import logging
import threading

logger = logging.getLogger(__name__)

#: Gen-0 threshold after start-up. Python's default is 700; the viewer
#: allocates many small container objects per frame, so 700 trips an
#: automatic collection on whichever thread allocates next several times a
#: second. Twenty thousand keeps automatic collections rare while the idle
#: timer does the routine work on the GUI thread.
GEN0_THRESHOLD = 20_000

#: Milliseconds of idle before the GUI-thread young collection runs.
IDLE_COLLECT_MS = 2000

_state = {
    'frozen': False,
    'frozen_at_startup': None,
    'threshold_raised': False,
    'timer': None,
    'after_paint': None,
}


def _on_main_thread():
    return threading.current_thread() is threading.main_thread()


def raise_threshold():
    """Raise the gen-0 threshold once; later calls change nothing."""
    if _state['threshold_raised']:
        return
    _, gen1, gen2 = gc.get_threshold()
    gc.set_threshold(GEN0_THRESHOLD, gen1, gen2)
    _state['threshold_raised'] = True


def freeze_startup():
    """Freeze the start-up heap into the permanent generation, once.

    Collects first, so start-up garbage is released rather than frozen.
    Never called again: freezing per project open would pin every project's
    objects for the life of the process.
    """
    if _state['frozen']:
        return frozen_count()
    gc.collect()
    gc.freeze()
    _state['frozen'] = True
    _state['frozen_at_startup'] = gc.get_freeze_count()
    return _state['frozen_at_startup']


#: Milliseconds after start-up by which the heap is frozen even if the window
#: was never painted (started minimised, say).
FREEZE_FALLBACK_MS = 1500


def startup_complete(after_paint=None):
    """Apply the whole policy. Called once, after the main window exists.

    ``after_paint`` -- the main window's ``QWindow``, when no project can be
    opened before the window is painted: the start-up heap is then frozen
    once that first paint is done instead of before the window is shown.
    The freeze's collection is ~0.05 s (MEASURED live 18 October 2026) that
    the window used to wait for.
    """
    raise_threshold()
    if after_paint is None or not freeze_after_first_paint(after_paint):
        freeze_startup()
    start_idle_collector()


class _FreezeAfterPaint:
    """Freeze the start-up heap once the window's first expose is painted.

    A widget window paints its first frame while it handles the expose, so a
    zero-delay call queued from the expose runs once that frame is drawn.
    """

    def __init__(self, window, fallback_ms):
        from PySide6.QtCore import QEvent, QObject, QTimer

        class Filter(QObject):
            def eventFilter(filter_self, watched, event):     # noqa: N805
                if event.type() == QEvent.Type.Expose and watched.isExposed():
                    watched.removeEventFilter(filter_self)
                    QTimer.singleShot(0, self.freeze)
                return False

        self._filter = Filter(window)
        window.installEventFilter(self._filter)
        QTimer.singleShot(fallback_ms, self.freeze)

    def freeze(self):
        if _state['after_paint'] is not self:
            return
        _state['after_paint'] = None
        freeze_startup()


def freeze_after_first_paint(window, fallback_ms=FREEZE_FALLBACK_MS):
    """Freeze the start-up heap after *window*'s first paint; True if armed.

    Falls back to a timer so the heap is frozen even with no paint. A
    project opened meanwhile calls :func:`freeze_pending` first.
    """
    if _state['frozen'] or _state['after_paint'] is not None:
        return _state['after_paint'] is not None
    if window is None or not _on_main_thread():
        return False
    try:
        _state['after_paint'] = _FreezeAfterPaint(window, fallback_ms)
    except Exception:                                       # noqa: BLE001
        logger.debug('freeze after paint not armed', exc_info=True)
        _state['after_paint'] = None
        return False
    return True


def freeze_pending():
    """Freeze now if a freeze is waiting for the first paint (a project opens)."""
    pending = _state['after_paint']
    if pending is not None:
        pending.freeze()


def start_idle_collector():
    """Start the GUI-thread idle collector; a no-op without a Qt app."""
    if _state['timer'] is not None:
        return _state['timer']
    try:
        from PySide6.QtCore import QCoreApplication, QTimer
    except Exception:                                       # noqa: BLE001
        return None
    application = QCoreApplication.instance()
    if application is None or not _on_main_thread():
        return None
    timer = QTimer(application)
    timer.setObjectName('gc_policy.idle_collector')
    timer.setInterval(IDLE_COLLECT_MS)
    timer.timeout.connect(collect_young)
    timer.start()
    _state['timer'] = timer
    return timer


def stop_idle_collector():
    timer = _state['timer']
    _state['timer'] = None
    if timer is not None:
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:
            pass


def collect_young():
    """Collect generations 0 and 1 on the GUI thread; elsewhere, skip."""
    if not _on_main_thread():
        return 0
    return gc.collect(1)


def collect_full(reason=''):
    """Full collection on the GUI thread, after a scene swap or close."""
    if not _on_main_thread():
        logger.debug('gc_policy: full collect for %r skipped off the GUI thread', reason)
        return 0
    if _state['after_paint'] is not None:
        # Plan 45 start-up. The start-up freeze, waiting for the first paint,
        # is a full collection of its own; the window's start clears an empty
        # case (MEASURED live 18 October 2026: 0.05 s inside show()).
        return 0
    return gc.collect()


#: Milliseconds the window stays quiet before the deferred full pass runs.
QUIET_COLLECT_MS = 1500


def collect_full_when_quiet(reason=''):
    """Reap what a scene swap left, but not inside the swap.

    A young pass runs now (cheap, and it takes the swap's own temporaries);
    the full pass waits ``QUIET_COLLECT_MS`` and then runs on the GUI thread,
    put off while a mouse button is down. Called again before the first one
    has run, it restarts the wait rather than stacking another.
    """
    if not _on_main_thread():
        return 0
    freed = gc.collect(1)
    try:
        from PySide6.QtCore import QCoreApplication, QTimer
    except Exception:                                       # noqa: BLE001
        return freed
    application = QCoreApplication.instance()
    if application is None:
        gc.collect()
        return freed
    timer = _state.get('quiet_timer')
    if timer is None:
        timer = QTimer(application)
        timer.setObjectName('gc_policy.quiet_collector')
        timer.setSingleShot(True)
        timer.timeout.connect(_quiet_collect)
        _state['quiet_timer'] = timer
    _state['quiet_reason'] = reason
    timer.start(QUIET_COLLECT_MS)
    return freed


def _quiet_collect():
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton:
            _state['quiet_timer'].start(500)
            return
    except Exception:                                       # noqa: BLE001
        pass
    gc.collect()


def full_collect_pending() -> bool:
    timer = _state.get('quiet_timer')
    try:
        return bool(timer is not None and timer.isActive())
    except RuntimeError:
        return False


def frozen_count():
    return gc.get_freeze_count()


def frozen_at_startup():
    """The frozen-object count recorded at start-up, or None before it."""
    return _state['frozen_at_startup']


def is_frozen():
    return _state['frozen']
