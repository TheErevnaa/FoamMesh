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
* a full collection runs on the GUI thread after every scene swap and every
  project close, the two moments large cyclic structures become garbage.

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


def startup_complete():
    """Apply the whole policy. Called once, after the main window exists."""
    raise_threshold()
    freeze_startup()
    start_idle_collector()


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
    return gc.collect()


def frozen_count():
    return gc.get_freeze_count()


def frozen_at_startup():
    """The frozen-object count recorded at start-up, or None before it."""
    return _state['frozen_at_startup']


def is_frozen():
    return _state['frozen']
