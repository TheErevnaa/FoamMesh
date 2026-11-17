"""A small executor of its own for WSL calls, and child runs whose reads are
bounded after a kill (Plan 50 IP22).

DP-6403. The WSL health probes, ``wsl --terminate`` / ``--shutdown`` and the
meshing-host probe used to run in ``asyncio.to_thread``: the event loop's
default executor (``support/loop_executor.py``, 10 workers), which every other
background job of the window shares. With the VM wedged each of them sat
there until its timeout, so a wedged VM could hold the whole default
executor. They run here instead, on :data:`WSL_MAX_WORKERS` workers: a wedged
VM can hold at most these, and the window's own background work goes on.

DP-6404. ``subprocess.run(timeout=...)`` on Windows kills the child when the
timeout passes and then reads the child's pipes with *no* timeout. A
grandchild that inherited the pipes -- the WSL relay behind ``wsl.exe`` --
keeps them open, so that read, and the worker it ran on, waited for as long
as the grandchild lived. :func:`run_bounded` reads for at most
``read_grace`` seconds after the kill and then lets the pipes go (the reader
threads ``subprocess`` started are daemon threads; they end when the pipe
does).

Every call made through this module has a timeout: :func:`run_bounded`
requires one.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import subprocess
import threading

from foammesh.support.loop_executor import PromptExitExecutor

logger = logging.getLogger(__name__)

#: Workers for WSL calls. Each WSL monitor shares one probe among its askers,
#: so three cover the health probe, a restart and a host probe at once.
WSL_MAX_WORKERS = 3
#: How long the pipes of a killed child are still read, in seconds.
READ_GRACE_SECONDS = 2.0

_executor: PromptExitExecutor | None = None
_lock = threading.Lock()


def wsl_executor() -> PromptExitExecutor:
    """The process's executor for WSL calls (created on first use)."""
    global _executor
    with _lock:
        if _executor is None or _executor._shutdown:
            _executor = PromptExitExecutor(
                max_workers=WSL_MAX_WORKERS, grace=0.0,
                thread_name_prefix='foammesh-wsl')
        return _executor


async def run_wsl(fn, /, *args, **kwargs):
    """``fn(*args, **kwargs)`` on :func:`wsl_executor`, awaited.

    The WSL counterpart of ``asyncio.to_thread``. ``fn`` must bound its own
    wait (a ``timeout``); this only keeps it off the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        wsl_executor(), functools.partial(fn, *args, **kwargs))


def shutdown() -> None:
    """Stop the WSL executor without waiting (exit)."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False)


def run_bounded(argv, *, timeout: float,
                read_grace: float = READ_GRACE_SECONDS,
                stdin=subprocess.DEVNULL, **popen_kwargs
                ) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, capture_output=True, timeout=timeout)`` whose
    reads after the kill are bounded by ``read_grace`` (DP-6404).

    Raises :class:`subprocess.TimeoutExpired` (with whatever output was read)
    when the child did not finish in ``timeout``; the child is killed first.
    """
    process = subprocess.Popen(
        list(argv), stdin=stdin, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        stdout = stderr = None
        try:
            stdout, stderr = process.communicate(timeout=max(0.0, read_grace))
        except subprocess.TimeoutExpired:
            # A grandchild holds the pipes. Let them go: this worker is free,
            # and subprocess's reader threads (daemon threads) end with the
            # grandchild. The pipes are not closed here: closing a buffered
            # reader another thread is blocked in waits for that read.
            logger.warning('%s: killed after %.1f s; its pipes are still held '
                           'open (a child process of it lives on); not '
                           'waiting for them', argv[0] if argv else '?',
                           timeout)
        except (OSError, ValueError):
            pass
        try:
            process.poll()
        except OSError:
            pass
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout,
                                        stderr=stderr) from None
    except BaseException:
        process.kill()
        raise
    return subprocess.CompletedProcess(list(argv), process.returncode,
                                       stdout, stderr)
