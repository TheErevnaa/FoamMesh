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
    """Append-only text log; never fatal (a run is not failed by its log)."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._file = None

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
        try:
            self._file.write(str(text).rstrip('\r\n') + '\n')
        except (OSError, ValueError):
            logger.debug('the run log could not be written', exc_info=True)

    def tee(self, on_line=None):
        """An ``on_line`` callback that writes the line here, then hands it
        to ``on_line`` (whose awaitable, if any, the job manager awaits)."""
        def line(text):
            self.write(text)
            return None if on_line is None else on_line(text)
        return line

    def close(self) -> None:
        handle, self._file = self._file, None
        if self.path is not None:
            _ACTIVE.discard(_key(self.path))
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
