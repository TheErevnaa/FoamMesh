"""Plan 35 CR6: is the WSL runtime there, and what to do when it is not.

One :class:`WslHealthMonitor` per distribution holds a small state machine::

    unknown -> ready <-> degraded -> unreachable -> recovering -> ready

Two signals feed it, so the time to notice a lost runtime is bounded:

* **While a job runs**, the CR5 wrapper's ``FOAMMESH_HB`` line. The job
  manager calls :meth:`WslHealthMonitor.heartbeat` for each one; after
  ``SILENCE_SECONDS`` of neither heartbeat nor output it calls
  :meth:`WslHealthMonitor.transport_silent`, which runs one probe with a
  ``PROBE_TIMEOUT_SECONDS`` timeout. The runtime answering means the transport
  is merely suspect (``degraded``); no answer means ``unreachable``. The worst
  case is 10 + 5 = 15 s after the last heartbeat. A *mesher* that is silent
  while the heartbeat keeps arriving never reaches this path: that is the
  CR5 idle prompt.
* **At other times**, the probe ``wsl.exe -d <distro> --exec true``: at start,
  before a run when nothing has answered recently, after a job that lost its
  transport, every ``IDLE_PROBE_SECONDS`` while no job runs, and on the next
  user action once the last probe is that old.

Every ``wsl.exe`` call here runs on the WSL executor
(``support.wsl_executor.run_wsl``, Plan 50 IP22 DP-6403: its own few workers,
never the loop's default executor) with a timeout, and the reads after a
timeout's kill are bounded (DP-6404); the GUI reads
:attr:`WslHealthMonitor.state` and never waits on the runtime.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from foammesh.support.wsl_executor import run_bounded, run_wsl

logger = logging.getLogger(__name__)

UNKNOWN = 'unknown'
READY = 'ready'
DEGRADED = 'degraded'
UNREACHABLE = 'unreachable'
RECOVERING = 'recovering'
STATES = (UNKNOWN, READY, DEGRADED, UNREACHABLE, RECOVERING)

#: How long one ``--exec true`` probe may take. A probe that takes longer is
#: the plan's "probe over 5 s": ``degraded`` the first time when idle.
PROBE_TIMEOUT_SECONDS = 5.0
#: No heartbeat and no output for this long while a wrapped job runs makes
#: the transport suspect, and one probe decides.
SILENCE_SECONDS = 10.0
#: Before a wrapped job's first control line the wrapper may still be booting
#: the distribution; silence is only suspicious after this.
LAUNCH_GRACE_SECONDS = 60.0
#: The idle cadence, and how old the last probe must be for a user action to
#: trigger another one.
IDLE_PROBE_SECONDS = 60.0
#: Plan 50 IP19 (DP-6362). A cadence task that ended while the hub is enabled
#: is started again after this pause (a pause, so a probe that fails at once
#: cannot spin). One still pending but with no probe for STALL_FACTOR idle
#: periods while no job runs has lost a step and is replaced.
IDLE_RESTART_SECONDS = 5.0
STALL_FACTOR = 3
#: A run skips its pre-run probe when the runtime answered this recently.
FRESH_SECONDS = 10.0
#: The silent-transport verdict is due this much before the 15 s bound, so
#: loop scheduling never carries it past (measured live: 15.02 s without).
DEADLINE_MARGIN_SECONDS = 0.25
#: ``wsl --terminate`` / ``wsl --shutdown`` may take a while; never forever.
RESTART_TIMEOUT_SECONDS = 60.0
#: DP-6463. WSL stops a distribution nothing is using, and starting it again
#: on a busy machine takes longer than one probe: MEASURED live, two 5 s
#: probes 49 s apart got no answer and the run was refused, and the same
#: command answered in 0.2 s moments later. When someone is waiting for the
#: answer (a run, Retry connection), "not reachable" is said only after one
#: probe that waits this long: what the job wrapper already allows a start.
START_WAIT_SECONDS = LAUNCH_GRACE_SECONDS
#: The causes someone is waiting behind.
WAITED_FOR = ('run', 'reconnect')


@dataclass(frozen=True)
class ProbeResult:
    """One probe's answer."""

    ok: bool
    elapsed: float = 0.0
    timed_out: bool = False
    detail: str = ''


def _decode(raw) -> str:
    if not raw:
        return ''
    if isinstance(raw, str):
        return raw.replace('\x00', '').strip()
    # wsl.exe writes its own messages in UTF-16LE.
    text = raw.decode('utf-16-le', errors='replace') if b'\x00' in raw[:64] else \
        raw.decode('utf-8', errors='replace')
    return text.replace('\x00', '').strip()


def _hidden():
    return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}


def wsl_call(argv, timeout: float) -> ProbeResult:
    """Run one ``wsl.exe`` command to completion or *timeout*. Blocking.

    Only ever called on the WSL executor (the tests assert it). DP-6404:
    after a timeout the child is killed and its pipes are read for at most
    ``wsl_executor.READ_GRACE_SECONDS``, so a relay that keeps them open
    cannot hold the worker.
    """
    started = time.monotonic()
    try:
        completed = run_bounded(argv, timeout=timeout, **_hidden())
    except subprocess.TimeoutExpired:
        return ProbeResult(False, time.monotonic() - started, True,
                           f'no answer within {timeout:g} s')
    except (OSError, subprocess.SubprocessError) as error:
        return ProbeResult(False, time.monotonic() - started, False, str(error))
    elapsed = time.monotonic() - started
    if completed.returncode == 0:
        return ProbeResult(True, elapsed)
    said = _decode(completed.stderr) or _decode(completed.stdout)
    return ProbeResult(False, elapsed, False,
                       said or f'wsl.exe exited with code {completed.returncode}')


def probe_argv(executable: str, distribution: str) -> list[str]:
    return [executable, '-d', distribution, '--exec', 'true']


class WslHealthMonitor:
    """The health of one WSL distribution, as the UI should read it."""

    def __init__(self, distribution: str, executable: str = 'wsl.exe', *,
                 call: Callable | None = None, clock: Callable | None = None,
                 probe_timeout: float = PROBE_TIMEOUT_SECONDS):
        self.distribution = distribution
        self.executable = executable
        self._call = call or wsl_call
        self._clock = clock or time.monotonic
        self.probe_timeout = probe_timeout
        #: Scaled down by the tests; the plan's numbers otherwise.
        self.silence_seconds = SILENCE_SECONDS
        self.launch_grace_seconds = LAUNCH_GRACE_SECONDS
        self.start_wait_seconds = START_WAIT_SECONDS
        #: DP-6463. True while the long probe is out: the banner says so.
        self.waiting_for_start = False
        self._waiting: asyncio.Future | None = None
        self.state = UNKNOWN
        self.reason = ''
        self.changed_at = self._clock()
        self.last_probe_at: float | None = None
        self.last_alive_at: float | None = None
        self.last_probe: ProbeResult | None = None
        #: DP-6059. The shared probe whose answer is already in the state.
        self._applied = None
        self.jobs_running = 0
        self._timeouts = 0
        self._down = False
        self._probing: asyncio.Future | None = None
        self._listeners: list[Callable] = []
        self._recovered: list[Callable] = []
        self.history: list[tuple[float, str, str]] = []

    # -- observers -------------------------------------------------------- #

    def subscribe(self, callback: Callable) -> Callable:
        """``callback(snapshot)`` on every state change; returns unsubscribe."""
        self._listeners.append(callback)

        def unsubscribe():
            if callback in self._listeners:
                self._listeners.remove(callback)
        return unsubscribe

    def on_recovered(self, callback: Callable) -> Callable:
        """``callback(monitor)`` when an ``unreachable`` runtime answers again.

        It may be a coroutine function; it is then scheduled, not awaited.
        """
        self._recovered.append(callback)

        def unsubscribe():
            if callback in self._recovered:
                self._recovered.remove(callback)
        return unsubscribe

    def snapshot(self) -> dict:
        return {'distribution': self.distribution, 'state': self.state,
                'reason': self.reason, 'changed_at': self.changed_at,
                'last_probe_at': self.last_probe_at,
                'last_alive_at': self.last_alive_at,
                'jobs_running': self.jobs_running,
                'waiting_for_start': self.waiting_for_start,
                'start_wait_seconds': self.start_wait_seconds}

    @property
    def reachable(self) -> bool:
        return self.state in (READY, DEGRADED, UNKNOWN)

    def _set(self, state: str, reason: str = '') -> None:
        if state == UNREACHABLE:
            self._down = True
        if state == self.state and reason == self.reason:
            return
        previous = self.state
        self.state, self.reason = state, reason
        self.changed_at = self._clock()
        self.history.append((self.changed_at, state, reason))
        del self.history[:-50]
        logger.info('WSL %s: %s -> %s %s', self.distribution, previous, state, reason)
        snapshot = self.snapshot()
        for callback in tuple(self._listeners):
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001 - a bad listener must not stop the machine
                logger.exception('WSL health listener failed')
        if state == READY and self._down:
            self._down = False
            for callback in tuple(self._recovered):
                try:
                    outcome = callback(self)
                    if inspect.isawaitable(outcome):
                        from foammesh.support.tasks import spawn
                        spawn(outcome, what='WSL recovery')
                except Exception:  # noqa: BLE001
                    logger.exception('WSL recovery callback failed')

    # -- the probe -------------------------------------------------------- #

    async def _probe_once(self) -> ProbeResult:
        argv = probe_argv(self.executable, self.distribution)
        return await run_wsl(self._call, argv, self.probe_timeout)

    def _shared_task(self):
        # Concurrent askers share one wsl.exe process.
        if self._probing is None or self._probing.done():
            self._probing = asyncio.ensure_future(self._probe_once())
        return self._probing

    async def _shared_probe(self) -> ProbeResult:
        return await asyncio.shield(self._shared_task())

    async def probe(self, cause: str = 'idle', *, within: float | None = None) -> str:
        """Ask the runtime ``--exec true`` and fold the answer into the state.

        *cause* is ``idle`` / ``start`` / ``run`` / ``action`` (a timeout is
        ``degraded`` the first time), ``transport`` (a silent job: any failure
        is ``unreachable``, an answer only ``degraded``) or ``reconnect``
        (the user asked; any failure is ``unreachable``).

        *within* bounds the wait: past it the probe counts as timed out even
        if ``wsl.exe`` has not been reaped yet, so a deadline is a deadline.

        DP-6463. For a cause someone waits behind (``run``, ``reconnect``), a
        probe that got no answer and would make the runtime ``unreachable``
        is followed by one that waits ``start_wait_seconds``; that answer
        decides.
        """
        task = self._shared_task()
        try:
            if within is None:
                result = await asyncio.shield(task)
            else:
                result = await asyncio.wait_for(asyncio.shield(task),
                                                max(0.0, within))
            # DP-6059. Askers share one probe; its answer is one answer.
            # Folded in once per asker, one slow reply counted as two
            # timeouts and took the runtime straight to unreachable.
            if task is self._applied:
                return self.state
            self._applied = task
        except asyncio.TimeoutError:
            result = ProbeResult(False, float(within or 0.0), True,
                                 f'no answer within {self.probe_timeout:g} s')
        except Exception as error:  # noqa: BLE001 - a probe never raises out
            result = ProbeResult(False, 0.0, False, f'the probe failed: {error}')
        if self._worth_waiting(result, cause):
            result = await self._wait_for_start()
        elif (self.waiting_for_start and not result.ok
              and cause not in WAITED_FOR and cause != 'transport'):
            # The long probe is out and will decide; a background probe
            # that got no answer meanwhile says nothing new.
            return self.state
        self.apply(result, cause)
        return self.state

    def _worth_waiting(self, result: ProbeResult, cause: str) -> bool:
        """No answer, someone waiting, and the next word would be unreachable."""
        if result.ok or not result.timed_out or cause not in WAITED_FOR:
            return False
        return (cause == 'reconnect' or self._timeouts >= 1
                or self.state in (UNREACHABLE, RECOVERING))

    async def _wait_for_start(self) -> ProbeResult:
        """One probe with the start allowance; askers share it."""
        if self._waiting is None or self._waiting.done():
            self._waiting = asyncio.ensure_future(run_wsl(
                self._call, probe_argv(self.executable, self.distribution),
                self.start_wait_seconds))
        self.waiting_for_start = True
        self._set(RECOVERING, 'the runtime is slow to answer and may be '
                              f'starting; waiting up to '
                              f'{self.start_wait_seconds:g} s for it')
        try:
            return await asyncio.shield(self._waiting)
        except Exception as error:  # noqa: BLE001 - a probe never raises out
            return ProbeResult(False, 0.0, False, f'the probe failed: {error}')
        finally:
            if self._waiting is None or self._waiting.done():
                self.waiting_for_start = False

    def apply(self, result: ProbeResult, cause: str = 'idle') -> str:
        """Fold one probe answer into the state (pure: tested with fakes)."""
        now = self._clock()
        self.last_probe_at = now
        self.last_probe = result
        if result.ok:
            self._timeouts = 0
            self.last_alive_at = now
            if cause == 'transport':
                self._set(DEGRADED, 'the runtime answers, but the run has '
                                    'stopped reporting')
            else:
                self._set(READY, '')
            return self.state
        detail = result.detail or 'the runtime did not answer'
        if cause in ('transport', 'reconnect') or not result.timed_out:
            self._set(UNREACHABLE, detail)
            return self.state
        self._timeouts += 1
        if self._timeouts == 1 and self.state not in (UNREACHABLE, RECOVERING):
            self._set(DEGRADED, f'the runtime is slow to answer ({detail})')
        else:
            self._set(UNREACHABLE, detail)
        return self.state

    # -- the transport heartbeat ------------------------------------------ #

    def heartbeat(self) -> None:
        """A running job's wrapper spoke: the transport is alive."""
        self.last_alive_at = self._clock()
        self._timeouts = 0
        if self.state in (UNKNOWN, DEGRADED, UNREACHABLE, RECOVERING):
            self._set(READY, '')

    async def transport_silent(self, silent_seconds: float) -> str:
        """A wrapped job has sent nothing for *silent_seconds*: one probe decides.

        The answer is due ``probe_timeout`` after the silence deadline, so the
        state is settled no later than ``silence_seconds + probe_timeout``
        after the last heartbeat (15 s), whatever the probe process does.
        """
        logger.warning('WSL %s: no heartbeat for %.1f s; probing',
                       self.distribution, silent_seconds)
        overdue = max(0.0, silent_seconds - self.silence_seconds)
        margin = min(DEADLINE_MARGIN_SECONDS, self.probe_timeout / 2)
        return await self.probe('transport', within=max(
            0.0, self.probe_timeout - overdue - margin))

    def transport_lost(self, reason: str = 'the connection to WSL was lost') -> None:
        """A job's relay ended without the wrapper's exit line."""
        self._set(UNREACHABLE, reason)

    def job_started(self) -> None:
        self.jobs_running += 1

    def job_ended(self) -> None:
        self.jobs_running = max(0, self.jobs_running - 1)

    # -- when to probe ---------------------------------------------------- #

    def _age(self, stamp) -> float:
        return float('inf') if stamp is None else self._clock() - stamp

    async def before_run(self) -> str:
        """The pre-run probe, skipped when the runtime answered just now."""
        if self.state == READY and self._age(self.last_alive_at) < FRESH_SECONDS:
            return self.state
        return await self.probe('run')

    def probe_due(self) -> bool:
        return self.jobs_running == 0 and self._age(self.last_probe_at) >= IDLE_PROBE_SECONDS

    async def idle_tick(self) -> str | None:
        """One step of the idle cadence: probe when due, otherwise nothing."""
        if not self.probe_due():
            return None
        return await self.probe('idle')

    async def run_idle(self, interval: float = IDLE_PROBE_SECONDS) -> None:
        """Probe every *interval* while no job runs, until cancelled."""
        while True:
            try:
                await self.idle_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the cadence must survive a bad probe
                logger.exception('WSL idle probe failed')
            await asyncio.sleep(max(1.0, interval / 6))

    # -- recovery actions ------------------------------------------------- #

    async def reconnect(self) -> str:
        """[Retry connection]."""
        self._set(RECOVERING, 'asking the runtime again')
        return await self.probe('reconnect')

    async def restart_runtime(self) -> str:
        """[Restart WSL runtime]: ``wsl --terminate <distro>``, then the probe.

        Every process in that distribution stops, including running jobs; the
        banner says so before the user chooses it.
        """
        self._set(RECOVERING, f'restarting {self.distribution}')
        result = await run_wsl(
            self._call, [self.executable, '--terminate', self.distribution],
            RESTART_TIMEOUT_SECONDS)
        if not result.ok:
            logger.warning('wsl --terminate %s: %s', self.distribution, result.detail)
        return await self.probe('reconnect')

    async def shutdown_all(self) -> str:
        """``wsl --shutdown``: every distribution stops. Diagnostics dialog only."""
        self._set(RECOVERING, 'shutting WSL down')
        result = await run_wsl(
            self._call, [self.executable, '--shutdown'], RESTART_TIMEOUT_SECONDS)
        if not result.ok:
            logger.warning('wsl --shutdown: %s', result.detail)
        return await self.probe('reconnect')


class WslHealth:
    """Every distribution's monitor, and which one the app is using.

    Disabled until :meth:`start`: a unit test that builds a ``JobManager``
    or an ``App`` must never find itself probing the real ``wsl.exe``.
    """

    def __init__(self, *, call: Callable | None = None, clock: Callable | None = None):
        self._call = call
        self._clock = clock
        self._monitors: dict[tuple[str, str], WslHealthMonitor] = {}
        self._listeners: list[Callable] = []
        self._recovered: list[Callable] = []
        self.distribution: str | None = None
        self.executable = 'wsl.exe'
        self.enabled = False
        self._idle_task: asyncio.Task | None = None
        self._action_task: asyncio.Task | None = None

    def configure(self, distribution: str | None, executable: str = 'wsl.exe') -> None:
        """The distribution the app's runtime lives in (Preferences may change it)."""
        changed = (distribution, executable) != (self.distribution, self.executable)
        self.distribution, self.executable = distribution, executable
        if changed and self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
            self.start()

    def monitor(self, distribution: str | None = None,
                executable: str | None = None) -> WslHealthMonitor | None:
        distribution = distribution or self.distribution
        if not distribution:
            return None
        executable = executable or self.executable
        key = (distribution.lower(), executable)
        monitor = self._monitors.get(key)
        if monitor is None:
            kwargs = {}
            if self._call is not None:
                kwargs['call'] = self._call
            if self._clock is not None:
                kwargs['clock'] = self._clock
            monitor = WslHealthMonitor(distribution, executable, **kwargs)
            for callback in self._listeners:
                monitor.subscribe(callback)
            for callback in self._recovered:
                monitor.on_recovered(callback)
            self._monitors[key] = monitor
        return monitor

    def for_argv(self, argv) -> WslHealthMonitor | None:
        """The monitor for a job's command line, or ``None`` (native, disabled)."""
        if not self.enabled:
            return None
        from .run_records import wsl_target
        target = wsl_target(argv)
        if not target.get('distribution'):
            return None
        return self.monitor(target['distribution'], target.get('executable') or None)

    def subscribe(self, callback: Callable) -> Callable:
        self._listeners.append(callback)
        for monitor in self._monitors.values():
            monitor.subscribe(callback)

        def unsubscribe():
            if callback in self._listeners:
                self._listeners.remove(callback)
            for monitor in self._monitors.values():
                if callback in monitor._listeners:
                    monitor._listeners.remove(callback)
        return unsubscribe

    def on_recovered(self, callback: Callable) -> None:
        self._recovered.append(callback)
        for monitor in self._monitors.values():
            monitor.on_recovered(callback)

    @property
    def current(self) -> WslHealthMonitor | None:
        return self.monitor() if self.distribution else None

    def start(self) -> asyncio.Task | None:
        """Probe now, then keep the idle cadence. Needs a running loop."""
        self.enabled = True
        monitor = self.current
        if monitor is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._idle_task is None or self._idle_task.done():
            async def cadence():
                await monitor.probe('start')
                await monitor.run_idle()
            self._idle_task = loop.create_task(cadence())
            self._idle_task.add_done_callback(self._cadenceEnded)
        return self._idle_task

    def _cadenceEnded(self, task: asyncio.Task) -> None:
        """Plan 50 IP19 (DP-6362): a cadence lost while enabled comes back."""
        if task is not self._idle_task or not self.enabled:
            return              # stopped, reconfigured or already replaced
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error('The WSL health poll stopped: %s', error,
                         exc_info=(type(error), error, error.__traceback__))
        else:
            logger.warning('The WSL health poll ended unexpectedly; '
                           'starting it again')
        self._idle_task = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(IDLE_RESTART_SECONDS, self._restartCadence)

    def _restartCadence(self) -> None:
        if self.enabled and (self._idle_task is None or self._idle_task.done()):
            self.start()

    def ensure_alive(self) -> bool:
        """Replace a cadence that is pending but no longer probing.

        True when it was replaced. A step asyncio dropped leaves the task
        waiting forever with no exception to see; the sign is that no probe
        landed for ``STALL_FACTOR`` idle periods while no job was running.
        """
        task = self._idle_task
        monitor = self.current
        if not self.enabled or task is None or task.done() or monitor is None:
            return False
        probing = monitor._probing
        if monitor.jobs_running or (probing is not None and not probing.done()):
            return False
        if monitor._age(monitor.last_probe_at) < IDLE_PROBE_SECONDS * (1 + STALL_FACTOR):
            return False
        logger.warning('The WSL health poll stopped probing; starting it again')
        self._idle_task = None
        task.cancel()
        self.start()
        return True

    def stop(self) -> None:
        self.enabled = False
        for task in (self._idle_task, self._action_task):
            if task is not None:
                task.cancel()
        self._idle_task = self._action_task = None

    def note_user_action(self) -> asyncio.Task | None:
        """The user did something: probe if the last probe is a minute old.

        Never awaited by the caller; returns the scheduled task, if any.
        """
        self.ensure_alive()     # Plan 50 IP19: the poll may have been lost
        monitor = self.current
        if not self.enabled or monitor is None or not monitor.probe_due():
            return None
        if self._action_task is not None and not self._action_task.done():
            return self._action_task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._action_task = loop.create_task(monitor.probe('action'))
        return self._action_task
