"""DP-2856. One log for a run of many jobs (a snappy pipeline).

A Gmsh run is one job: its runner writes ``runner.log`` in the run directory,
and the run's job id reads it. A snappy run is a chain of stage jobs
(blockMesh, surfaceFeatures, snappyHexMesh, ... checkMesh), each with a log
of its own under ``foammesh/logs``, and nothing tied the run to them: the run
had no job id and ``GET jobs/{run id}/log`` answered ``job_not_found``.

The pipeline now writes every stage's output, as it arrives, into
``run.log`` in its run directory, between a line naming the stage and a line
saying how it ended (with the stage's own job id and log). The run's id is
the job id of that log, so it can be followed with ``offset`` while the run
goes, as a Gmsh run's can.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: The name of a run's own log in its run directory.
RUN_LOG_NAME = 'run.log'

#: The logs of the runs going now, so a listing read mid-run says running.
_ACTIVE: set[str] = set()


def _key(path) -> str:
    return str(Path(path).resolve()).lower()


def active(path) -> bool:
    """Whether a run is still writing the log at ``path``."""
    try:
        return _key(path) in _ACTIVE
    except OSError:
        return False


class RunLog:
    """Append-only text log; never fatal (a run is not failed by its log).

    DP-6481. ``write`` is called for every line a stage prints, on the
    event loop, and a write waited up to 13.1 s for a busy disk there
    (MEASURED live). Lines are queued and written, in order, by a writer
    thread of the log's own; ``flush`` waits for them, and ``close`` flushes
    first, so a log that has been closed is whole.
    """

    #: How long the writer waits for another line before it lets go.
    IDLE_SECONDS = 2.0

    def __init__(self) -> None:
        self.path: Path | None = None
        self._file = None
        self._queue: list[tuple[object, str]] = []
        self._ready = threading.Condition()
        self._writer: threading.Thread | None = None

    def open(self, path) -> Path | None:
        self.close()
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Line-buffered, so a reader following the file sees each line.
            self._file = path.open('a', encoding='utf-8', buffering=1,
                                   newline='\n')
            self.path = path
            _ACTIVE.add(_key(path))
        except OSError:
            logger.warning('the run log could not be opened', exc_info=True)
            self._file, self.path = None, None
        return self.path

    def write(self, text: str) -> None:
        if self._file is None:
            return
        line = str(text).rstrip('\r\n') + '\n'
        with self._ready:
            self._queue.append((self._file, line))
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._drain, daemon=False, name='foammesh-run-log')
                self._writer.start()
            else:
                self._ready.notify_all()

    def _drain(self) -> None:
        while True:
            with self._ready:
                if not self._queue:
                    self._ready.wait(self.IDLE_SECONDS)
                if not self._queue:
                    self._writer = None
                    self._ready.notify_all()
                    return
                batch = list(self._queue)
            for handle, line in batch:
                try:
                    handle.write(line)
                except (OSError, ValueError):
                    logger.debug('the run log could not be written',
                                 exc_info=True)
            with self._ready:
                del self._queue[:len(batch)]
                self._ready.notify_all()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until every line written so far is in the file."""
        with self._ready:
            return self._ready.wait_for(lambda: not self._queue, timeout)

    def tee(self, on_line=None):
        """An ``on_line`` callback that writes the line here, then hands it
        to ``on_line`` (whose awaitable, if any, the job manager awaits)."""
        def line(text):
            self.write(text)
            return None if on_line is None else on_line(text)
        return line

    def close(self) -> None:
        self.flush()
        handle, self._file = self._file, None
        if self.path is not None:
            _ACTIVE.discard(_key(self.path))
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
