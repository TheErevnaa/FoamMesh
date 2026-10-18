"""Plan 40 UXC4 (F6c): the taskbar tells the user what the run is doing.

While a job runs the taskbar button shows progress; when a job ends while the
window is not the active one the button flashes (``QApplication.alert``) and,
only if the user turned it on, a desktop notification names the result.

The decisions live in :class:`JobAttention`, which has no Qt in it and talks to
a backend object, so the rule is testable without a taskbar.  The Windows
backend drives ``ITaskbarList3`` through ``ctypes`` (Qt 6 dropped
``QWinTaskbarButton``); on any other platform, or when the call fails, the
progress half is silently absent and the flash still works.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

NO_PROGRESS = 0
INDETERMINATE = 1
ERROR = 4


class JobAttention:
    """Project job events onto taskbar progress, a flash and a notification."""

    def __init__(self, backend, *, is_active, notify_enabled):
        self._backend = backend
        self._is_active = is_active
        self._notify_enabled = notify_enabled
        self._running: dict[str, str] = {}
        self.flashes = 0
        #: Plan 45 W2. What the taskbar button was last told. A window that
        #: has never run anything shows no progress, so that is the start.
        self._shown = NO_PROGRESS

    @property
    def running(self) -> int:
        return len(self._running)

    def _show(self, state):
        """Tell the taskbar ``state``, unless that is what it shows (W2).

        A focus change asked for "no progress" every time, and asking went
        through ``winId()`` and a call into the shell's taskbar on the GUI
        thread -- 2.1 s in one live pass. Nothing is asked when nothing
        would change.
        """
        if state == self._shown:
            return
        self._shown = state
        self._backend.set_progress(state)

    def on_started(self, *, job_id, name='', **_kwargs):
        self._running[str(job_id)] = str(name or '')
        self._show(INDETERMINATE)

    def on_finished(self, *, job_id, **_kwargs):
        self._end(job_id, 'finished', failed=False)

    def on_cancelled(self, *, job_id, **_kwargs):
        self._end(job_id, 'cancelled', failed=False, attention=False)

    def on_failed(self, *, job_id, **_kwargs):
        self._end(job_id, 'failed', failed=True)

    def _end(self, job_id, verb, *, failed, attention=True):
        name = self._running.pop(str(job_id), '')
        if self._running:
            self._show(INDETERMINATE)
        else:
            self._show(ERROR if failed else NO_PROGRESS)
        if not attention or self._is_active():
            return
        self.flashes += 1
        self._backend.flash()
        if self._notify_enabled():
            label = name or 'The run'
            self._backend.notify('FoamMesh', f'{label} {verb}.')

    def on_activated(self):
        """The window came to the front: clear a failure mark on the button."""
        if not self._running:
            self._show(NO_PROGRESS)


class QtBackend:
    """Flash through Qt, progress through the Windows taskbar, notify by tray."""

    def __init__(self, window):
        self._window = window
        self._taskbar = None
        self._tray = None
        self._hwnd = None

    def flash(self):
        from PySide6.QtWidgets import QApplication
        QApplication.alert(self._window, 0)

    def set_progress(self, state):
        taskbar = self._windows_taskbar()
        if taskbar is None:
            return
        try:
            taskbar.set_state(self._hwnd, state)
        except Exception:                       # never break a run over a bar
            logger.debug('taskbar progress failed', exc_info=True)

    def notify(self, title, text):
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            if self._tray is None:
                self._tray = QSystemTrayIcon(self._window.windowIcon(),
                                             self._window)
            self._tray.show()
            self._tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information)
        except Exception:
            logger.debug('desktop notification failed', exc_info=True)

    def _windows_taskbar(self):
        if sys.platform != 'win32':
            return None
        if self._taskbar is None:
            # Made once, on the first state change, never at start-up.
            try:
                self._taskbar = _WindowsTaskbar()
            except Exception:
                logger.debug('ITaskbarList3 unavailable', exc_info=True)
                self._taskbar = False
        if not self._taskbar:
            return None
        # Plan 45 W2. The handle is kept: ``winId()`` dispatched events (and
        # every Python event filter with them) inside the call. It is asked
        # again only when Windows says the kept one is no window any more.
        if not self._hwnd or not _is_window(self._hwnd):
            self._hwnd = int(self._window.winId())
        return self._taskbar


def _is_window(hwnd):
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


class _WindowsTaskbar:
    """Minimal ``ITaskbarList3`` caller: HrInit (3) and SetProgressState (10)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [('a', ctypes.c_ulong), ('b', ctypes.c_ushort),
                        ('c', ctypes.c_ushort), ('d', ctypes.c_ubyte * 8)]

        def guid(a, b, c, *d):
            return GUID(a, b, c, (ctypes.c_ubyte * 8)(*d))

        clsid = guid(0x56FDF344, 0xFD6D, 0x11D0,
                     0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90)
        iid = guid(0xEA1AFB91, 0x9E28, 0x4B86,
                   0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x5E, 0xEF, 0xAF)
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        pointer = ctypes.c_void_p()
        result = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, 1, ctypes.byref(iid),
            ctypes.byref(pointer))
        if result != 0 or not pointer.value:
            raise OSError(f'CoCreateInstance failed: {result}')
        self._pointer = pointer
        table = ctypes.cast(
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p))
        proto = ctypes.WINFUNCTYPE
        self._hr_init = proto(ctypes.c_long, ctypes.c_void_p)(table[3])
        self._set_state = proto(
            ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_int)(
                table[10])
        self._hr_init(pointer)

    def set_state(self, hwnd, state):
        self._set_state(self._pointer, hwnd, int(state))


def install_job_attention(window, events, menu, *, settings, backend=None):
    """Wire the taskbar attention onto ``window``; returns the object and an
    unsubscribe callable.  ``menu`` gets the opt-in notification action."""
    from PySide6.QtCore import QObject
    from PySide6.QtGui import QAction, QGuiApplication

    from foammesh.core.project import Event

    action = QAction(window.tr('Notify when a run ends'), window)
    action.setObjectName('actionNotifyWhenRunEnds')
    action.setCheckable(True)
    action.setChecked(bool(settings.getDesktopNotifications()))
    action.setToolTip(window.tr(
        'Show a desktop notification when a run ends while FoamMesh is not '
        'the window in front. The taskbar button flashes either way.'))
    action.toggled.connect(settings.updateDesktopNotifications)
    menu.addAction(action)

    attention = JobAttention(
        backend or QtBackend(window),
        is_active=window.isActiveWindow,
        notify_enabled=action.isChecked)

    class _Activation(QObject):
        """DP-1447. Told by `focusWindowChanged`, not by an event filter on
        the window: a Python filter on a window was called while the window
        was being torn down (rc 139 at the end of the Gmsh walks)."""

        def focused(self, focused):
            if focused is not None and focused is window.windowHandle():
                attention.on_activated()

    activation = _Activation(window)
    app = QGuiApplication.instance()
    if app is not None:
        app.focusWindowChanged.connect(activation.focused)
    unsubscribes = [
        events.subscribe(Event.JOB_STARTED, attention.on_started),
        events.subscribe(Event.JOB_FINISHED, attention.on_finished),
        events.subscribe(Event.JOB_FAILED, attention.on_failed),
        events.subscribe(Event.JOB_CANCELLED, attention.on_cancelled),
    ]

    def unsubscribe():
        for undo in unsubscribes:
            undo()
        unsubscribes.clear()

    attention.action = action
    attention.unsubscribe = unsubscribe
    return attention
