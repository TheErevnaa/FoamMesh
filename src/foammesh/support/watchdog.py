#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR1: the GUI thread says it is alive; a stall leaves its stacks.

A 250 ms ``QTimer`` on the GUI thread (:data:`TICK_MS`), on every tick:

* bumps the shared-memory heartbeat (``support.heartbeat``), which the crash
  helper reads from its own process -- ten seconds without a tick and it
  writes a hang dump, sixty and it offers to close (``support.crash_helper``);
* stamps the time of the tick for the stall monitor (below);
* measures the gap since the previous tick. A gap of :data:`STALL_SECONDS` or
  more is a stall that has just ended: it is logged with the last operation
  and :attr:`Watchdog.stalled` fires, which the status-bar item
  (:func:`attach_status_indicator`) turns into "UI was unresponsive for X s
  (details)", the details link opening ``watchdog.log``.

Every :data:`HELPER_CHECK_TICKS` ticks it also checks the crash helper is
still running, and logs once if it is not.

The stacks taken *while* the GUI thread is stuck come from two places, so
that neither walks a thread that is running Python (DP-988):

* a small monitor thread wakes every :data:`MONITOR_SECONDS`. When the GUI
  thread has not ticked for :data:`STALL_SECONDS` it writes every thread's
  stack from ``sys._current_frames()`` -- holding the GIL, so the frames it
  reads cannot change under it. A GUI thread stuck in Python, or in a native
  call that released the GIL, is covered this way;
* on every wake the monitor also re-arms
  ``faulthandler.dump_traceback_later(STALL_SECONDS)`` into ``watchdog.log``.
  That timer runs in a C thread that needs no GIL, and it can only fire when
  the monitor could not take the GIL for :data:`STALL_SECONDS` -- that is,
  when some thread has held the GIL without running bytecode (a native call
  that holds it, like ``ctypes.PyDLL`` or a hung driver). Every other thread
  is then parked on the GIL, so the GIL-less walk reads frames that are not
  moving.

Before DP-988 the GUI tick armed faulthandler's timer itself, so the timer
also fired while the GUI thread was busy *in Python*, and its walk of a
changing frame chain crashed the process in python311.dll about half the
time (CR8's K8f child).

Deviation from the plan's wording, deliberately: the stall *report* comes from
the tick that ends the stall, not from a separate Python thread. A Python
thread cannot run while the GUI thread holds the GIL -- the case that matters
most -- and the evidence taken *during* the stall already comes from threads
that need no GIL (faulthandler's, and the helper process).
"""
from __future__ import annotations

import datetime
import faulthandler
import logging
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel

from foammesh.support import crash_helper, heartbeat

logger = logging.getLogger(__name__)

TICK_MS = 250
STALL_SECONDS = 2.0
HELPER_CHECK_TICKS = 40
MONITOR_SECONDS = 0.1
MAX_FRAMES = 100


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec='milliseconds')


def _timeout_header(seconds: float) -> str:
    """faulthandler's own header, so one reader parses both kinds of dump."""
    whole = int(seconds)
    micro = int(round((seconds - whole) * 1_000_000))
    minutes, sec = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    text = f'Timeout ({hours}:{minutes:02d}:{sec:02d}'
    if micro:
        text += f'.{micro:06d}'
    return text + ')!'


def format_stacks(seconds: float, gui_ident: int | None,
                  skip_ident: int | None = None) -> str:
    """Every thread's stack, GUI thread first, in faulthandler's layout.

    Runs holding the GIL (all Python code does): that is what makes it safe
    where faulthandler's GIL-less walk is not.
    """
    frames = sys._current_frames()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    order = sorted(frames, key=lambda ident: ident != gui_ident)
    lines = [_timeout_header(seconds)]
    for ident in order:
        if ident == skip_ident:
            continue
        if ident == gui_ident:
            label = ' (GUI thread)'
        else:
            label = f' ({names[ident]})' if names.get(ident) else ''
        lines.append(f'Thread 0x{ident:016x}{label} (most recent call first):')
        stack = []
        frame = frames[ident]
        while frame is not None:
            stack.append(frame)
            frame = frame.f_back
        if len(stack) > MAX_FRAMES:
            # Both ends: the innermost frames say where it is stuck, the
            # outermost what the user was doing.
            half = MAX_FRAMES // 2
            omitted = len(stack) - 2 * half
            stack = stack[:half] + [f'  ... ({omitted} frames omitted)'] + stack[-half:]
        for frame in stack:
            if isinstance(frame, str):
                lines.append(frame)
                continue
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno} '
                         f'in {code.co_name}')
        del stack, frame
        lines.append('')
    return '\n'.join(lines)


class Watchdog(QObject):
    """The GUI-thread heartbeat and stall recorder."""

    #: (seconds unresponsive, last operation)
    stalled = Signal(float, str)

    def __init__(self, log_path, parent=None, *, clock=time.monotonic,
                 stall_seconds: float = STALL_SECONDS):
        super().__init__(parent)
        self.log_path = Path(log_path)
        self._clock = clock
        self._stall_seconds = stall_seconds
        self._file = None
        self._last = None
        self._ticks = 0
        self._lock = threading.Lock()
        self._gui_ident: int | None = None
        self._beat_at: float | None = None     # real monotonic, for the monitor
        self._dumped_for: float | None = None  # the beat whose stall has stacks
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None
        #: stall dumps the monitor wrote from ``sys._current_frames``
        self.python_dumps = 0
        self.stalls: list[tuple[float, str]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if self._file is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Kept open: faulthandler writes to the descriptor from its own
            # thread and cannot open a file.
            self._file = open(self.log_path, 'a', encoding='utf-8')
            self._write(f'--- watchdog for pid={os.getpid()} started {_now()} '
                        f'(stacks after {self._stall_seconds:.1f} s) ---')
        self._last = self._clock()
        self._gui_ident = threading.get_ident()
        self._beat_at = time.monotonic()
        heartbeat.beat()
        self._timer.start()
        if self._monitor is None:
            self._monitor_stop.clear()
            self._monitor = threading.Thread(
                target=self._run_monitor, name='foammesh-stall-monitor',
                daemon=True)
            self._monitor.start()

    def stop(self) -> None:
        self._timer.stop()
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            self._monitor_stop.set()
            monitor.join(timeout=2.0)
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:                                  # noqa: BLE001
            pass
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _write(self, text: str) -> None:
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.write(text + '\n')
                self._file.flush()
            except (OSError, ValueError):
                pass

    def _arm(self) -> None:
        """Re-arm the GIL-less dump; only the monitor thread calls this."""
        with self._lock:
            if self._file is None:
                return
            try:
                faulthandler.dump_traceback_later(
                    self._stall_seconds, repeat=False, file=self._file,
                    exit=False)
            except (OSError, ValueError, RuntimeError):
                pass

    def _run_monitor(self) -> None:
        own = threading.get_ident()
        armed_at = time.monotonic()
        self._arm()
        while not self._monitor_stop.wait(MONITOR_SECONDS):
            now = time.monotonic()
            beat = self._beat_at
            if now - armed_at >= self._stall_seconds:
                # This thread could not take the GIL for the whole timeout,
                # so faulthandler's timer has fired: the stall has stacks.
                self._dumped_for = beat
            self._arm()
            armed_at = now
            if (beat is None or self._dumped_for == beat
                    or now - beat < self._stall_seconds):
                continue
            self._dumped_for = beat
            try:
                text = format_stacks(self._stall_seconds, self._gui_ident,
                                     skip_ident=own)
            except Exception as error:                      # noqa: BLE001
                text = (f'{_timeout_header(self._stall_seconds)}\n'
                        f'(the stacks could not be read: {error!r})')
            self._write(text)
            self.python_dumps += 1

    @staticmethod
    def _last_op() -> str:
        shared = heartbeat.block()
        if shared is None:
            return ''
        try:
            return heartbeat.read(shared)['last_op']
        except (ValueError, TypeError):
            return ''

    def _tick(self) -> None:
        now = self._clock()
        gap = now - self._last if self._last is not None else 0.0
        self._last = now
        self._beat_at = time.monotonic()
        heartbeat.beat()
        self._ticks += 1
        if self._ticks % HELPER_CHECK_TICKS == 0:
            crash_helper.check_helper(logger)
        if gap >= self._stall_seconds:
            self._report(gap)

    def _report(self, seconds: float) -> None:
        operation = self._last_op()
        self.stalls.append((seconds, operation))
        self._write(f'[{_now()}] the GUI thread was unresponsive for '
                    f'{seconds:.1f} s; last operation: {operation or "none"}')
        logger.warning('The GUI thread was unresponsive for %.1f s (last '
                       'operation: %s); stacks in %s', seconds,
                       operation or 'none', self.log_path)
        self.stalled.emit(seconds, operation)


class StallIndicator(QLabel):
    """Status-bar item: "UI was unresponsive for X s (details)"."""

    def __init__(self, log_path, parent=None):
        super().__init__(parent)
        self._log_path = Path(log_path)
        self.setObjectName('uiStallIndicator')
        self.setTextFormat(Qt.TextFormat.RichText)
        self.linkActivated.connect(self._open)
        self.hide()

    def show_stall(self, seconds: float, operation: str = '') -> None:
        text = self.tr('UI was unresponsive for {0:.1f} s').format(seconds)
        self.setText(f'{text} (<a href="details">{self.tr("details")}</a>)')
        self.setToolTip(self.tr('Last operation: {0}\nStacks: {1}').format(
            operation or self.tr('none'), self._log_path))
        self.show()

    def _open(self, _link: str = '') -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_path)))


def attach_status_indicator(window, watchdog: Watchdog) -> StallIndicator | None:
    """Put the stall item in ``window``'s status bar; ``None`` without one."""
    status_bar = getattr(window, 'statusBar', None)
    if status_bar is None:
        return None
    bar = status_bar()
    if not hasattr(bar, 'addPermanentWidget'):
        return None
    indicator = StallIndicator(watchdog.log_path, bar)
    bar.addPermanentWidget(indicator)
    watchdog.stalled.connect(indicator.show_stall)
    return indicator


_state: dict = {'watchdog': None, 'indicator': None}


def start(log_path, window=None) -> Watchdog | None:
    """Start the process's watchdog (idempotent) and its status-bar item.

    ``None`` without a log path (the lifecycle log directory was not set up).
    """
    if log_path is None:
        return None
    watchdog = _state['watchdog']
    if watchdog is None:
        watchdog = Watchdog(log_path)
        _state['watchdog'] = watchdog
        watchdog.start()
    if window is not None and _state['indicator'] is None:
        _state['indicator'] = attach_status_indicator(window, watchdog)
    return watchdog


def stop() -> None:
    watchdog = _state['watchdog']
    _state['watchdog'] = None
    _state['indicator'] = None
    if watchdog is not None:
        watchdog.stop()
