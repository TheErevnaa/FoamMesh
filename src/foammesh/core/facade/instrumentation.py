"""Owner-loop latency instrumentation for the AF1/AF1V budget gates.

The plan gives facade/API work a hard owner-loop budget: no scheduled slice
may block the Qt loop for more than 20 ms on the AF1V reference fixture, with
an 8 ms p95 target.  This module records the evidence: command-slice
durations, snapshot-build durations, journal queue depth, and journal flush
latency, so exceeding the budget is a measurable failure instead of an
accepted desktop pause.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

UNATTRIBUTED = 'an unnamed slice'

OWNER_LOOP_MAX_MS = 20.0
OWNER_LOOP_P95_MS = 8.0
P95_MIN_SAMPLES = 20


@dataclass(frozen=True)
class MetricSummary:
    name: str
    count: int
    maximum_ms: float
    p95_ms: float
    latest_ms: float

    def to_dict(self) -> dict:
        return {
            'name': self.name, 'count': self.count,
            'maximum_ms': round(self.maximum_ms, 3),
            'p95_ms': round(self.p95_ms, 3),
            'latest_ms': round(self.latest_ms, 3),
        }


@dataclass
class _Series:
    samples: deque = field(default_factory=lambda: deque(maxlen=2048))

    def record(self, duration_ms: float) -> None:
        self.samples.append(duration_ms)

    def summary(self, name: str) -> MetricSummary:
        if not self.samples:
            return MetricSummary(name, 0, 0.0, 0.0, 0.0)
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
        return MetricSummary(name, len(self.samples), ordered[-1], ordered[index],
                             self.samples[-1])


class OwnerLoopMonitor:
    """Record owner-loop slice, snapshot-build, and journal timings."""

    def __init__(self, *, budget_max_ms: float = OWNER_LOOP_MAX_MS,
                 budget_p95_ms: float = OWNER_LOOP_P95_MS, clock=time.perf_counter):
        self.budget_max_ms = budget_max_ms
        self.budget_p95_ms = budget_p95_ms
        self._clock = clock
        self._series: dict[str, _Series] = {}
        self._journal_queue_depth = 0
        self._budget_violations: list[dict] = []

    def measure(self, name: str, *, context: str | None = None):
        """Context manager timing one owner-loop slice.

        ``context`` names what was running. DP-409: without it a violation
        says only how long the loop was held, which is the one thing that is
        already obvious to whoever is watching the frozen window.
        """
        return _Measurement(self, name, context)

    def record(self, name: str, duration_ms: float, *,
               context: str | None = None) -> None:
        self._series.setdefault(name, _Series()).record(duration_ms)
        if name == 'owner_loop_slice' and duration_ms > self.budget_max_ms:
            violation = {'metric': name, 'duration_ms': round(duration_ms, 3),
                         'budget_ms': self.budget_max_ms,
                         'context': context or UNATTRIBUTED}
            self._budget_violations.append(violation)
            logger.warning('owner-loop slice exceeded budget: %.1f ms > %.1f ms '
                           'in %s', duration_ms, self.budget_max_ms,
                           context or UNATTRIBUTED)

    def record_journal_queue_depth(self, depth: int) -> None:
        self._journal_queue_depth = depth

    def summary(self, name: str) -> MetricSummary:
        return self._series.get(name, _Series()).summary(name)

    @property
    def budget_violations(self) -> list[dict]:
        return list(self._budget_violations)

    def within_budget(self) -> bool:
        slices = self.summary('owner_loop_slice')
        p95_ready = slices.count >= P95_MIN_SAMPLES
        return (not self._budget_violations
                and (not p95_ready or slices.p95_ms <= self.budget_p95_ms))

    def snapshot(self) -> dict:
        return {
            'budget': {'max_ms': self.budget_max_ms, 'p95_ms': self.budget_p95_ms},
            'p95_min_samples': P95_MIN_SAMPLES,
            'within_budget': self.within_budget(),
            'budget_violations': self.budget_violations,
            'journal_queue_depth': self._journal_queue_depth,
            'metrics': {name: series.summary(name).to_dict()
                        for name, series in sorted(self._series.items())},
        }


class _Measurement:
    def __init__(self, monitor: OwnerLoopMonitor, name: str,
                 context: str | None = None):
        self._monitor = monitor
        self._name = name
        self._context = context
        self._started = 0.0

    def __enter__(self):
        self._started = self._monitor._clock()
        return self

    def __exit__(self, *_exc_info):
        duration = (self._monitor._clock() - self._started) * 1000.0
        self._monitor.record(self._name, duration, context=self._context)
        return False
