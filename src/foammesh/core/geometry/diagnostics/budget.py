#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Machine-independent budgets for surface diagnostics.

MEASURED: importing a 37,240-triangle propeller ran for 7 h 24 min at a pegged
core and 24.5 GB before it was killed, having written nothing. It was never
going to return. The check responsible, :func:`checks.self_intersections`, was
guarded only by a triangle count -- and that guard is inverted in effect: a
400,020-triangle model is *skipped* as "too big", while a 37,240-triangle one
with four mutually intersecting shells runs unbounded.

The guard was wrong because triangle count is not what the check costs. This
module replaces it with three ideas:

**Budgets are specified in work, not seconds.** A deadline in seconds encodes
the machine it was measured on; ship it and it means one thing on a
workstation and another on a laptop. Each check declares a *workload* in units
that describe its own cost -- for pairwise shell intersection, the sum of
``n_i * n_j`` over the pairs actually tested.

**Seconds are derived locally, from a measured rate.** :func:`calibrated_rate`
measures this machine once and caches the result against a hardware
fingerprint. Work units divided by that rate give a local deadline. The same
model therefore gets a different deadline on every machine and the *same*
verdict.

**The clock is CPU time, not wall clock.** The propeller consumed 26,355 s of
CPU in 26,548 s of wall clock -- it was compute-bound, not blocked. Measuring
CPU time means a machine that is merely busy with a solve does not trip the
budget.

Thresholds are ratios against the measured baseline, not absolutes:
:data:`WARN_RATIO` reports and keeps going, :data:`ABORT_RATIO` stops. Both are
deliberately generous -- a healthy but awkward model runs a few times the norm
and still finishes, where the propeller was four orders of magnitude out.
"""
from __future__ import annotations

import json
import math
import os
import platform
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: Report that a check is overrunning, but let it finish.
WARN_RATIO = 2.0
#: Stop a check whose cost is this many times the measured expectation.
ABORT_RATIO = 3.0

#: Never abort below this, however small the workload: a floor stops rounding
#: and start-up noise from cutting a legitimate check off at the knees.
MINIMUM_BUDGET_SECONDS = 5.0

#: How long the user is prepared to wait for one check, regardless of how large
#: the input is.
#:
#: This is a *preference*, not a machine constant, and it is the second half of
#: the gate. The ratio thresholds catch work that is slow **for its size** --
#: the propeller ran 192x its expected cost. They cannot catch work that is
#: honestly, enormously large: 26 shells over 400,000 triangles is 7.3e10 work
#: units, so a ratio-only budget would grant it fifteen hours and never fire.
#: Ratio finds pathology; this finds "too big to be worth checking". Both are
#: needed, and both are visible to the user rather than hidden in a constant.
DEFAULT_MAXIMUM_SECONDS = 60.0

#: Work units per second on the machine the baseline was measured on. Replaced
#: at runtime by a local measurement; kept only so a machine that cannot run
#: the probe still gets a usable number rather than an exception.
#: Provenance: plans/evidence/diagnostic-baseline/.
FALLBACK_UNITS_PER_SECOND = 2.0e7

#: Locator work units per second, used only when the probe cannot run. Locator
#: queries are far cheaper per unit than the intersection filter, so this is
#: deliberately a separate constant rather than a shared fallback.
FALLBACK_LOCATOR_UNITS_PER_SECOND = 5.0e6

_CALIBRATION_CACHE: dict[str, float] = {}


class BudgetPolicy(str, Enum):
    """What to do when a check exceeds :data:`ABORT_RATIO`."""

    #: Stop the check and report it as not evaluated. The default.
    ABORT = 'abort'
    #: Report the overrun and let the check run to completion.
    WARN_ONLY = 'warn_only'
    #: Impose no limit at all.
    NEVER_LIMIT = 'never_limit'


class BudgetExceeded(RuntimeError):
    """Raised inside a check when its budget is spent and policy says stop."""

    def __init__(self, message: str, *, progress: str = ''):
        super().__init__(message)
        self.progress = progress


class Cancelled(RuntimeError):
    """Raised inside a check when the caller asked it to stop."""


# --------------------------------------------------------------------------- #
# Machine calibration
# --------------------------------------------------------------------------- #

def machine_fingerprint() -> str:
    """Identify the machine closely enough that a stale rate is not reused."""
    return '|'.join((
        platform.machine(), platform.processor() or '?',
        str(os.cpu_count() or 0), platform.system(),
    ))


def _calibration_path() -> Path:
    from foammesh.settings.app_settings import AppSettings

    try:
        root = Path(AppSettings.casesDirectory())
    except Exception:
        root = Path.home() / '.foammesh'
    return root / 'diagnostic-calibration.json'


def _measure_units_per_second() -> float:
    """Time a reference workload of known size on this machine.

    The probe mirrors the shape of the real work -- a pairwise surface
    intersection -- rather than a synthetic loop, so the rate it produces is
    the rate the check will actually see.
    """
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersGeneral import vtkIntersectionPolyDataFilter
    from vtkmodules.vtkFiltersSources import vtkSphereSource

    def sphere(centre, resolution):
        source = vtkSphereSource()
        source.SetCenter(*centre)
        source.SetRadius(1.0)
        source.SetThetaResolution(resolution)
        source.SetPhiResolution(resolution)
        source.Update()
        copy = vtkPolyData()
        copy.DeepCopy(source.GetOutput())
        return copy

    # MEASURED (plans/evidence/diagnostic-baseline): across a corpus of
    # disjoint, touching and interpenetrating shells the healthy work-rate
    # spans 1.28e6 to 1.42e9 units/s -- a thousandfold range, because shells
    # that do not actually intersect are dispatched almost for free while
    # interpenetrating ones do real work. Calibrating on a mid-range shape put
    # the slowest *healthy* sample at 2.92x the probe, all but touching the 3x
    # abort line.
    #
    # So the probe deliberately reproduces the slowest healthy shape: two small
    # interpenetrating spheres, where fixed per-pair overhead dominates. The
    # resulting rate is conservative by construction, and the ratio gate then
    # fires only on geometry that is genuinely pathological rather than merely
    # awkward.
    left, right = sphere((0.0, 0.0, 0.0), 16), sphere((1.2, 0.0, 0.0), 16)
    units = float(left.GetNumberOfCells()) * float(right.GetNumberOfCells())

    started = time.process_time()
    test = vtkIntersectionPolyDataFilter()
    test.SetInputData(0, left)
    test.SetInputData(1, right)
    test.Update()
    elapsed = time.process_time() - started
    if elapsed <= 0:
        return FALLBACK_UNITS_PER_SECOND
    return units / elapsed


def calibrated_rate(*, refresh: bool = False) -> float:
    """Work units per second on this machine, measured once and cached.

    Cached against :func:`machine_fingerprint`, so moving the settings between
    machines re-measures rather than inheriting someone else's hardware.
    """
    fingerprint = machine_fingerprint()
    if not refresh and fingerprint in _CALIBRATION_CACHE:
        return _CALIBRATION_CACHE[fingerprint]

    path = _calibration_path()
    if not refresh and path.is_file():
        try:
            stored = json.loads(path.read_text(encoding='utf-8'))
            if stored.get('fingerprint') == fingerprint:
                rate = float(stored['units_per_second'])
                if rate > 0:
                    _CALIBRATION_CACHE[fingerprint] = rate
                    return rate
        except (OSError, ValueError, KeyError, TypeError):
            pass

    try:
        rate = _measure_units_per_second()
    except Exception:
        rate = FALLBACK_UNITS_PER_SECOND
    _CALIBRATION_CACHE[fingerprint] = rate
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'fingerprint': fingerprint, 'units_per_second': rate,
            'measured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }, indent=2), encoding='utf-8')
    except OSError:
        pass
    return rate


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #

@dataclass
class DiagnosticBudget:
    """A machine-independent allowance for one check.

    ``workload`` is in the check's own units. The seconds this becomes are
    derived from the local calibrated rate, so the same workload is the same
    *verdict* everywhere even though it is a different number of seconds.
    """

    workload: float
    check: str = ''
    policy: BudgetPolicy = BudgetPolicy.ABORT
    rate: float | None = None
    #: The user's patience, in seconds. Not a machine property -- see
    #: :data:`DEFAULT_MAXIMUM_SECONDS`.
    maximum_seconds: float = DEFAULT_MAXIMUM_SECONDS
    #: Floor below which nothing is aborted, so start-up noise cannot cut a
    #: legitimate check off. Exposed per budget so a caller that wants a hard
    #: bound -- a test, or a batch run with its own limits -- can lower it.
    minimum_seconds: float = MINIMUM_BUDGET_SECONDS
    on_progress: object = None
    #: Set by the caller (a cancel button, a job manager) to stop the check.
    cancelled: bool = False

    _started: float = field(default=0.0, init=False)
    #: Seconds of work done in child processes, which this process's CPU
    #: clock cannot see.
    _external: float = field(default=0.0, init=False)
    _units_done: float = field(default=0.0, init=False)
    _warned: bool = field(default=False, init=False)
    warnings: list = field(default_factory=list, init=False)

    def __post_init__(self):
        if self.rate is None:
            self.rate = calibrated_rate()
        self._started = time.process_time()

    # -- allowances -------------------------------------------------------- #

    @property
    def expected_seconds(self) -> float:
        """What a healthy check of this size should cost, on this machine."""
        return max(self.workload, 0.0) / max(self.rate or 1.0, 1e-9)

    @property
    def abort_seconds(self) -> float:
        """The earlier of "slow for its size" and "longer than I will wait"."""
        by_ratio = max(self.expected_seconds * ABORT_RATIO, self.minimum_seconds)
        if self.maximum_seconds and self.maximum_seconds > 0:
            return min(by_ratio, max(self.maximum_seconds, self.minimum_seconds))
        return by_ratio

    @property
    def limited_by(self) -> str:
        """Which of the two gates is binding, so the message can say so."""
        by_ratio = max(self.expected_seconds * ABORT_RATIO, self.minimum_seconds)
        if self.maximum_seconds and 0 < self.maximum_seconds < by_ratio:
            return 'maximum_wait'
        return 'cost_ratio'

    @property
    def warn_seconds(self) -> float:
        return max(self.expected_seconds * WARN_RATIO, self.minimum_seconds / 2)

    def add_external_seconds(self, seconds: float) -> None:
        """Charge work done in a child process to this budget.

        MEASURED: moving the opaque intersection into a subprocess -- needed so
        it can be abandoned safely -- silently disabled the budget, because
        ``process_time`` counts only *this* process. The parent sat idle
        waiting, its CPU clock barely advanced, nothing ever expired, and every
        pair was granted a fresh full allowance. A 60-second import became a
        25-minute one.
        """
        self._external += max(0.0, float(seconds))

    @property
    def elapsed(self) -> float:
        """Work seconds spent: this process's CPU, plus any child's.

        CPU time for the local part, so a machine merely busy with a solve does
        not burn the allowance; wall clock for children, since that is the only
        thing the parent can observe about them.
        """
        return (time.process_time() - self._started) + self._external

    @property
    def ratio(self) -> float:
        """How many times the expected cost this check has taken so far."""
        expected = self.expected_seconds
        return self.elapsed / expected if expected > 0 else 0.0

    # -- progress ---------------------------------------------------------- #

    def report(self, message: str, fraction: float | None = None) -> None:
        if callable(self.on_progress):
            self.on_progress(self.check, fraction, message)

    def observe(self, units_done: float) -> None:
        """Record completed work, so the remaining cost can be re-projected.

        In-run self-calibration: the machine tells us its own speed while it
        works, which absorbs load, thermal throttling and a stale probe without
        anyone having to model them.
        """
        self._units_done = max(self._units_done, float(units_done))
        if self._units_done > 0 and self.elapsed > 0:
            observed = self._units_done / self.elapsed
            # Trust the live measurement once there is enough of it to mean
            # something; early samples are dominated by filter set-up.
            if self._units_done >= self.workload * 0.1:
                self.rate = observed

    # -- the gate ---------------------------------------------------------- #

    def check_in(self, *, progress: str = '') -> None:
        """Poll the budget between units of work.

        Raises :class:`Cancelled` if the caller asked to stop, and
        :class:`BudgetExceeded` once the cost passes :data:`ABORT_RATIO` --
        unless policy says otherwise.
        """
        if self.cancelled:
            raise Cancelled(f'{self.check} cancelled by request')
        if self.policy is BudgetPolicy.NEVER_LIMIT:
            return
        elapsed = self.elapsed
        if not self._warned and elapsed > self.warn_seconds:
            self._warned = True
            note = self.overrun_message(progress)
            self.warnings.append(note)
            self.report(note)
        if self.policy is BudgetPolicy.WARN_ONLY:
            return
        if elapsed > self.abort_seconds:
            raise BudgetExceeded(self.overrun_message(progress), progress=progress)

    def overrun_message(self, progress: str = '') -> str:
        """Why this is slow, in terms that mean the same on every machine."""
        where = f' after {progress}' if progress else ''
        name = self.check or 'check'
        if self.limited_by == 'maximum_wait':
            # Not pathological, just large. Saying so matters: the remedy is a
            # longer allowance, not a repair to the geometry.
            return (
                f'{name} needs about {self.expected_seconds:,.0f}s for this '
                f'input ({self.workload:,.0f} work units), which is beyond the '
                f'{self.maximum_seconds:,.0f}s allowed for one check{where}')
        return (
            f'{name} is running at {self.ratio:.1f}x the '
            f'expected cost for this input{where} '
            f'({self.workload:,.0f} work units)')

    def not_evaluated_reason(self, progress: str = '') -> str:
        return (
            f'Not evaluated: {self.overrun_message(progress)}. '
            'Raise or remove the limit in the geometry-diagnostics settings to '
            'run this check to completion.')


#: In-flight budgets, so a Cancel press can reach work already running.
#:
#: Subprocess jobs are cancellable because there is a process to signal. This
#: work is in-process, so the handle has to be the budget itself.
_ACTIVE: dict[str, DiagnosticBudget] = {}


def register(job_id: str, budget: DiagnosticBudget) -> None:
    _ACTIVE[str(job_id)] = budget


def unregister(job_id: str) -> None:
    _ACTIVE.pop(str(job_id), None)


def cancel(job_id: str) -> bool:
    """Ask an in-flight diagnostic to stop. True if one was listening."""
    budget = _ACTIVE.get(str(job_id))
    if budget is None:
        return False
    budget.cancelled = True
    return True


def active_ids() -> tuple[str, ...]:
    return tuple(_ACTIVE)


def budget_from_settings(check: str = '', *, on_progress=None) -> DiagnosticBudget:
    """A budget honouring the user's diagnostics preference.

    Falls back to the shipped defaults when settings are unavailable, so
    headless and test callers behave like a fresh install rather than
    unbounded.
    """
    policy, maximum = BudgetPolicy.ABORT, DEFAULT_MAXIMUM_SECONDS
    try:
        from foammesh.settings.app_settings import AppSettings

        policy, maximum = AppSettings().getDiagnosticBudget()
    except Exception:
        pass
    return DiagnosticBudget(
        workload=0.0, check=check, policy=policy,
        maximum_seconds=maximum, on_progress=on_progress)


def locator_workload(sample_count: float, target_cell_count: float) -> float:
    """Work units for a closest-point sweep (Plan 23 §10.1).

    Both terms are needed, and the build term is the one that is easy to drop::

        M log M   building the tree over M target cells
        N log M   N closest-point queries against it

    Construction does not scale with the query count, and §4 mandates
    *per-section* locators -- so this plan builds many trees over few queries
    each, which is exactly the regime where a query-only model budgets a real
    check at near-zero and aborts it immediately.
    """
    cells = max(float(target_cell_count), 1.0)
    depth = math.log2(max(cells, 2.0))
    return cells * depth + max(float(sample_count), 0.0) * depth


def _measure_locator_units_per_second() -> float:
    """Time a closest-point sweep, which is not what the other probe measures.

    :func:`_measure_units_per_second` deliberately times
    ``vtkIntersectionPolyDataFilter`` on interpenetrating spheres, because it
    was calibrated for the self-intersection check. Locator queries are orders
    of magnitude cheaper per unit, so reusing that rate would give every
    fidelity check a budget it exceeds on the first chunk.
    """
    import numpy as np
    from vtkmodules.vtkCommonCore import reference as vtk_reference
    from vtkmodules.vtkCommonDataModel import vtkGenericCell, vtkStaticCellLocator
    from vtkmodules.vtkFiltersSources import vtkSphereSource

    source = vtkSphereSource()
    source.SetThetaResolution(32)
    source.SetPhiResolution(32)
    source.Update()
    surface = source.GetOutput()

    locator = vtkStaticCellLocator()
    locator.SetDataSet(surface)
    started = time.process_time()
    locator.BuildLocator()

    probes = np.random.default_rng(0).normal(size=(2000, 3)) * 1.5
    cell, closest = vtkGenericCell(), [0.0, 0.0, 0.0]
    cell_id, sub_id, squared = (vtk_reference(0), vtk_reference(0),
                                vtk_reference(0.0))
    for point in probes:
        locator.FindClosestPoint(point.tolist(), closest, cell, cell_id,
                                 sub_id, squared)
    elapsed = time.process_time() - started
    if elapsed <= 0:
        return FALLBACK_LOCATOR_UNITS_PER_SECOND
    return locator_workload(len(probes), surface.GetNumberOfCells()) / elapsed


def locator_rate(*, refresh: bool = False) -> float:
    """Locator work units per second here, cached against the machine.

    Kept under its own cache key so it cannot be confused with the
    intersection rate: the two differ by orders of magnitude and sharing a key
    would silently give one check the other's budget.
    """
    key = 'locator|' + machine_fingerprint()
    if not refresh and key in _CALIBRATION_CACHE:
        return _CALIBRATION_CACHE[key]
    try:
        rate = _measure_locator_units_per_second()
    except Exception:
        rate = FALLBACK_LOCATOR_UNITS_PER_SECOND
    _CALIBRATION_CACHE[key] = rate
    return rate


def locator_budget(sample_count: float, target_cell_count: float, *,
                   check: str = 'geometry fidelity', on_progress=None
                   ) -> DiagnosticBudget:
    """A budget sized for a closest-point sweep, at the locator rate."""
    budget = budget_from_settings(check, on_progress=on_progress)
    budget.workload = locator_workload(sample_count, target_cell_count)
    budget.rate = locator_rate()
    return budget


def pairwise_workload(sizes) -> float:
    """Work units for a pairwise intersection over shells of these sizes.

    The cost driver is the product of the two shells' triangle counts, summed
    over the pairs -- not the total triangle count, which is what the guard
    this replaces used and why it protected the wrong models.
    """
    sizes = [float(value) for value in sizes]
    total = 0.0
    for left in range(len(sizes)):
        for right in range(left + 1, len(sizes)):
            total += sizes[left] * sizes[right]
    return total
