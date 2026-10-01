"""Open a modal from a coroutine without holding its task across the modal.

Plan 37 UF20 follow-up, MEASURED on the SU2 walk. ``run_mesh_check_async``
raised the "checkMesh is not available" refusal with ``QMessageBox.warning``
from inside the wizard's ``_wizardProceed`` task. A modal box runs a nested Qt
event loop; under qasync that nested loop steps other tasks while the
wizard's task is still current, and asyncio refuses each one -- "Cannot enter
into task <Task-488 MeshManager.reload()> while another task <Task-…
StepManager._wizardProceed()> is being executed" -- and a task whose step was
refused never runs again: the mesh reload that had been scheduled was dropped.

DP-34 found the same shape in ``facade_client.submit`` and fixed it by handing
the outcome back through ``loop.call_soon``: a callback runs from the loop
with no task current, so the same dialog spins the same nested loop
harmlessly. This is that pattern for every other place a coroutine says
something in a modal.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable


def _task_loop():
    """The running loop when a task is current on it, else None."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop if asyncio.current_task(loop) is not None else None


async def modal(function: Callable[..., Any], *args, **kwargs) -> Any:
    """Call ``function`` with no task current, wait for it, return its result.

    Called with no task current (a plain callback), it is called at once.
    """
    loop = _task_loop()
    if loop is None:
        return function(*args, **kwargs)
    future = loop.create_future()

    def deliver() -> None:
        if future.cancelled():
            return
        try:
            result = function(*args, **kwargs)
        except BaseException as error:                      # noqa: BLE001
            future.set_exception(error)
            return
        future.set_result(result)

    loop.call_soon(deliver)
    return await future


def say(function: Callable[..., Any], *args, **kwargs) -> None:
    """Call ``function`` now, or on the loop's next turn if a task is current.

    For a synchronous caller that may be running inside a task and needs no
    answer back (a warning, an offer whose outcome it does not read).
    """
    loop = _task_loop()
    if loop is None:
        function(*args, **kwargs)
        return
    loop.call_soon(lambda: function(*args, **kwargs))
