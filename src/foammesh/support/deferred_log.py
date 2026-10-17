"""A log handler whose file is written by a thread of its own (Plan 39 VI14d).

``RotatingFileHandler.emit`` stats and appends to its file on whichever thread
logged, which on the GUI thread makes every ``logger.info`` wait for the file
system. MEASURED on the host: one record held the GUI thread for 1.03 s while
the virus scanner had the log file (VI14 load, ``shouldRollover`` ->
``os.path.exists``). The record is now formatted and queued (a few
microseconds) and a daemon thread hands it to the real handler.

``flush`` and ``close`` wait for what is queued, so a test, a crash bundle or
an orderly exit reads everything logged before it.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import threading


class DeferredHandler(logging.Handler):
    """Wraps *inner*; its formatter, name and level are *inner*'s."""

    def __init__(self, inner: logging.Handler):
        super().__init__(inner.level)
        self.inner = inner
        name = inner.get_name()
        if name:
            self.set_name(name)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run, name='foammesh-log-writer', daemon=True)
        self._closed = False
        self._thread.start()

    # -- the writer thread -------------------------------------------------
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if isinstance(item, threading.Event):
                try:
                    self.inner.flush()
                finally:
                    item.set()
                continue
            try:
                self.inner.handle(item)
            except Exception:                              # noqa: BLE001
                pass

    # -- the logging side --------------------------------------------------
    def setFormatter(self, formatter) -> None:
        self.inner.setFormatter(formatter)

    def setLevel(self, level) -> None:
        super().setLevel(level)
        self.inner.setLevel(level)

    def emit(self, record: logging.LogRecord) -> None:
        if self._closed:
            return
        try:
            # QueueHandler's preparation: the message and traceback are text
            # from here on, so the record can cross the thread.
            prepared = logging.handlers.QueueHandler(self._queue).prepare(
                record)
        except Exception:                                  # noqa: BLE001
            self.handleError(record)
            return
        self._queue.put_nowait(prepared)

    def flush(self) -> None:
        if self._closed or not self._thread.is_alive():
            return
        done = threading.Event()
        self._queue.put_nowait(done)
        done.wait(10.0)

    def close(self) -> None:
        if not self._closed:
            self.flush()
            self._closed = True
            self._queue.put_nowait(None)
            self._thread.join(5.0)
            try:
                self.inner.close()
            except Exception:                              # noqa: BLE001
                pass
        super().close()

    def __getattr__(self, name):
        # baseFilename, stream, ...: what callers ask of a file handler.
        if name == 'inner':
            raise AttributeError(name)
        return getattr(self.inner, name)
