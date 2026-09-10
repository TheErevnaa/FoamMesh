"""Bounded SQLite WAL event journal; serialization can be flushed off-loop.

One ordered writer owns the connection (guarded by ``_flush_lock``).  Events
are enqueued on the owner loop without any synchronous fsync; flushes run in a
worker thread in bounded batches.  The default reconnect retention follows the
plan: 50,000 summary events or 90 days, whichever is reached first.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import threading
import time
from pathlib import Path

from .events import FacadeEvent
from .instrumentation import OwnerLoopMonitor

JOURNAL_FILE_NAME = 'event_journal.sqlite3'
DEFAULT_RETAIN_EVENTS = 50_000
DEFAULT_RETAIN_DAYS = 90


class EventJournal:
    def __init__(self, path: str | Path, *, retain: int = DEFAULT_RETAIN_EVENTS,
                 retain_days: float = DEFAULT_RETAIN_DAYS,
                 monitor: OwnerLoopMonitor | None = None):
        self.path = Path(path)
        self.retain = retain
        self.retain_days = retain_days
        self.monitor = monitor
        self._pending: list[FacadeEvent] = []
        self._pending_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA synchronous=NORMAL')
                connection.execute(
                    'CREATE TABLE IF NOT EXISTS facade_events ('
                    'sequence INTEGER PRIMARY KEY, recorded_at REAL NOT NULL, '
                    'event_json TEXT NOT NULL)')

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute('PRAGMA synchronous=NORMAL')
        return connection

    def append(self, event: FacadeEvent) -> None:
        """Accept an event on the owner loop without synchronously fsyncing it."""
        with self._pending_lock:
            self._pending.append(event)
            depth = len(self._pending)
        if self.monitor is not None:
            self.monitor.record_journal_queue_depth(depth)

    @property
    def queue_depth(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def flush(self) -> int:
        started = time.perf_counter()
        with self._flush_lock:
            with self._pending_lock:
                pending, self._pending = self._pending, []
            if not pending:
                return 0
            try:
                now = time.time()
                with closing(self._connect()) as connection:
                    with connection:
                        connection.executemany(
                            'INSERT OR REPLACE INTO facade_events'
                            '(sequence, recorded_at, event_json) '
                            'VALUES (?, ?, ?)',
                            [(event.sequence, now, json.dumps(
                                event.to_dict(), sort_keys=True))
                             for event in pending])
                        connection.execute(
                            'DELETE FROM facade_events WHERE sequence <= '
                            '(SELECT COALESCE(MAX(sequence), 0) - ? '
                            'FROM facade_events)', (self.retain,))
                        connection.execute(
                            'DELETE FROM facade_events WHERE recorded_at < ?',
                            (now - self.retain_days * 86_400.0,))
            except Exception:
                with self._pending_lock:
                    self._pending = pending + self._pending
                raise
        if self.monitor is not None:
            self.monitor.record('journal_flush', (time.perf_counter() - started) * 1000.0)
            self.monitor.record_journal_queue_depth(self.queue_depth)
        return len(pending)

    async def flush_off_loop(self) -> int:
        return await asyncio.to_thread(self.flush)

    def after(self, sequence: int, *, limit: int = 1_000) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT event_json FROM facade_events WHERE sequence > ? '
                'ORDER BY sequence LIMIT ?', (sequence, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def latest_sequence(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT COALESCE(MAX(sequence), 0) FROM facade_events').fetchone()
        return int(row[0])

    def earliest_sequence(self) -> int | None:
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT MIN(sequence) FROM facade_events').fetchone()
        return None if row[0] is None else int(row[0])

    def checkpoint(self) -> bool:
        """Fold the write-ahead log back into the database file.

        Copying a WAL-mode database means copying three files that only agree
        with each other between transactions. Pending events are flushed and
        the log truncated first, so the ``.sqlite3`` file alone is the whole
        history and a copy of it cannot land mid-transaction.

        Returns whether the checkpoint completed; a busy database is reported
        rather than raised, because failing to compact a journal must never
        fail the operation that asked -- the copy is still made, it just also
        carries the log files beside it.
        """
        self.flush()
        try:
            with closing(self._connect()) as connection:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        self.flush()
