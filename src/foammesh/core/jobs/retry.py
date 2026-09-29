"""Plan 35 CR6. Which failed runs are offered a retry, and how often.

One policy for every mesher run the window starts:

* A transport failure (the WSL relay broke, or the runtime was unreachable
  when the run was about to start) is offered [Retry].
* An out-of-memory kill (SIGKILL) is offered [Retry] and, when the run had
  more than one rank, [Retry with fewer cores] at half the ranks.
* A ``FOAM FATAL ERROR`` is never retried: the case says what is wrong and the
  same case would say it again. Neither is any other exit -- a segfault, an
  abort, a plain non-zero exit.
* A retry runs the stage again from the snapshot the failed attempt restored.
  It is offered once: a run that is itself a retry offers nothing more.

The Gmsh thread retry (DP-56) is the one *automatic* retry, and it follows the
same rule: once, and never on a run that is already a retry.
"""
from __future__ import annotations

from typing import Mapping

RETRY = 'retry'
RETRY_FEWER_CORES = 'retry_fewer_cores'
#: A run is retried at most this many times, automatically or by the user.
MAX_RETRIES = 1
#: The command parameter that counts the retries behind a run.
ATTEMPT_PARAMETER = 'retry_attempt'

_SIGKILL = 9
_RETRIED_KINDS = frozenset({'transport', 'oom'})


def fewer_cores(ranks: int) -> int:
    """Half the ranks, never fewer than one."""
    try:
        ranks = int(ranks)
    except (TypeError, ValueError):
        return 1
    return max(1, ranks // 2)


def attempt_of(parameters: Mapping | None) -> int:
    try:
        return max(0, int((parameters or {}).get(ATTEMPT_PARAMETER) or 0))
    except (TypeError, ValueError):
        return 0


def retry_offer(exit: Mapping | None, *, ranks: int = 1, attempt: int = 0,
                fatal: bool = False) -> dict | None:
    """What a failed run may offer, or ``None``.

    *exit* is the decoded exit (``decode_exit``) the job carries; *ranks* the
    MPI ranks the attempt ran with; *attempt* how many retries are behind it;
    *fatal* whether its log holds a ``FOAM FATAL ERROR``.
    """
    exit = exit if isinstance(exit, Mapping) else {}
    kind = str(exit.get('kind') or '')
    if fatal or attempt >= MAX_RETRIES or kind not in _RETRIED_KINDS:
        return None
    try:
        ranks = max(1, int(ranks or 1))
    except (TypeError, ValueError):
        ranks = 1
    actions = [RETRY]
    offer = {'kind': kind, 'actions': actions, 'attempt': attempt,
             'ranks': ranks}
    if kind == 'oom' and ranks > 1:
        actions.append(RETRY_FEWER_CORES)
        offer['fewer_cores'] = fewer_cores(ranks)
    return offer


def retry_parameters(parameters: Mapping | None, action: str,
                     offer: Mapping) -> dict:
    """The command parameters for the one retry *action* asks for."""
    if action not in (offer.get('actions') or ()):
        raise ValueError(f'{action!r} was not offered for this failure')
    retried = dict(parameters or {})
    retried[ATTEMPT_PARAMETER] = attempt_of(parameters) + 1
    if action == RETRY_FEWER_CORES:
        retried['cores'] = int(offer.get('fewer_cores') or fewer_cores(
            offer.get('ranks') or 1))
    return retried


def signal_of(returncode) -> int:
    """The signal a return code reports (-N or 128+N), or 0."""
    try:
        code = int(returncode)
    except (TypeError, ValueError):
        return 0
    number = -code if code < 0 else (code - 128 if code > 128 else 0)
    return number if 0 < number <= 64 else 0


def automatic_thread_retry(*, status: str, returncode, threads: int,
                           attempt: int = 0) -> bool:
    """DP-56: may a crashed Gmsh run be retried, once, at one thread?

    Yes for a signal death other than SIGKILL (that is memory, whose remedy is
    a smaller mesh), at more than one thread, on a run that is not already a
    retry. Never for a cancellation.
    """
    if str(status or '') == 'cancelled' or attempt >= MAX_RETRIES:
        return False
    number = signal_of(returncode)
    if number == 0 or number == _SIGKILL:
        return False
    try:
        return int(threads or 1) > 1
    except (TypeError, ValueError):
        return False
