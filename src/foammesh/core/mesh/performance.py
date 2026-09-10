"""Canonical mesh resource measurements and Plan 17 performance gates."""
from __future__ import annotations

from dataclasses import dataclass
import time
import threading
from typing import Callable

import numpy as np
import psutil

from .model import CanonicalMesh, CellBlock, CellType


@dataclass(frozen=True)
class PerformanceGate:
    name: str
    maximum_seconds: float
    maximum_peak_rss_bytes: int
    minimum_cells: int


@dataclass(frozen=True)
class PerformanceResult:
    name: str
    elapsed_seconds: float
    rss_before_bytes: int
    rss_after_bytes: int
    peak_rss_estimate_bytes: int
    cell_count: int
    passed: bool
    failures: tuple[str, ...]

    def to_dict(self):
        return {
            'name': self.name, 'elapsed_seconds': self.elapsed_seconds,
            'rss_before_bytes': self.rss_before_bytes,
            'rss_after_bytes': self.rss_after_bytes,
            'peak_rss_estimate_bytes': self.peak_rss_estimate_bytes,
            'cell_count': self.cell_count, 'passed': self.passed,
            'failures': list(self.failures),
        }


PLAN17_ONE_MILLION = PerformanceGate(
    'plan17-canonical-1m', 180.0, 6 * 1024 ** 3, 1_000_000)
PLAN17_FIVE_MILLION = PerformanceGate(
    'plan17-canonical-5m', 900.0, 16 * 1024 ** 3, 5_000_000)


def measure(gate: PerformanceGate, operation: Callable[[], object], *,
            cell_count: int | Callable[[object], int]) -> tuple[object, PerformanceResult]:
    process = psutil.Process()
    before = process.memory_info().rss
    peak = [before]
    stopped = threading.Event()

    def sample():
        while not stopped.wait(0.02):
            try:
                peak[0] = max(peak[0], process.memory_info().rss)
            except psutil.Error:
                return

    monitor = threading.Thread(target=sample, name='foammesh-rss-sampler', daemon=True)
    monitor.start()
    started = time.perf_counter()
    try:
        value = operation()
    finally:
        elapsed = time.perf_counter() - started
        stopped.set()
        monitor.join(timeout=1)
    after = process.memory_info().rss
    peak_estimate = max(before, after, peak[0])
    count = int(cell_count(value) if callable(cell_count) else cell_count)
    failures = []
    if elapsed > gate.maximum_seconds:
        failures.append(f'elapsed {elapsed:.3f}s exceeds {gate.maximum_seconds:.3f}s')
    if peak_estimate > gate.maximum_peak_rss_bytes:
        failures.append(
            f'RSS {peak_estimate} exceeds {gate.maximum_peak_rss_bytes} bytes')
    if count < gate.minimum_cells:
        failures.append(f'cell count {count} is below gate minimum {gate.minimum_cells}')
    return value, PerformanceResult(
        gate.name, elapsed, before, after, peak_estimate, count,
        not failures, tuple(failures))


def synthetic_tetra_lattice(minimum_cells: int) -> CanonicalMesh:
    """Build a deterministic connected tetra lattice for performance evidence."""
    if minimum_cells < 1:
        raise ValueError('minimum_cells must be positive')
    cubes_per_axis = max(1, int(np.ceil((minimum_cells / 6) ** (1 / 3))))
    n = cubes_per_axis
    coordinates = np.arange(n + 1, dtype=np.float64)
    x, y, z = np.meshgrid(coordinates, coordinates, coordinates, indexing='ij')
    points = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
    i, j, k = np.meshgrid(np.arange(n), np.arange(n), np.arange(n), indexing='ij')
    base = ((i * (n + 1) + j) * (n + 1) + k).ravel().astype(np.int64)
    sx, sy = (n + 1) ** 2, n + 1
    v000 = base
    v100 = base + sx
    v010 = base + sy
    v110 = base + sx + sy
    v001 = base + 1
    v101 = base + sx + 1
    v011 = base + sy + 1
    v111 = base + sx + sy + 1
    connectivity = np.vstack((
        np.column_stack((v000, v100, v110, v111)),
        np.column_stack((v000, v110, v010, v111)),
        np.column_stack((v000, v010, v011, v111)),
        np.column_stack((v000, v011, v001, v111)),
        np.column_stack((v000, v001, v101, v111)),
        np.column_stack((v000, v101, v100, v111)),
    ))
    count = len(connectivity)
    return CanonicalMesh(
        points, (CellBlock(CellType.TETRA, connectivity,
                           np.arange(count, dtype=np.int64),
                           np.zeros(count, dtype=np.int64)),), (), {},
        {0: {'name': 'fluid', 'stable_id': 'synthetic-fluid'}},
        'performance_fixture', f'synthetic-{count}',
        metadata={'fixture': 'connected_tetra_lattice', 'cubes_per_axis': n})
