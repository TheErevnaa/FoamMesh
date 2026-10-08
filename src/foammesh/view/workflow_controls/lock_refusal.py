"""What the user is told when a locked step refuses a group edit.

DP-1190. A step whose result is the mesh on disk is locked (Plan 37 UF5):
the facade refuses any edit to its inputs with ``TaskLockedError``, whose
details name the step. A group edit is more than one write -- the row, then
one ``geometry.items.patch`` per surface it gains or loses -- and the binding
writes were sent with nobody reading the answer, so a surface un-ticked after
the step locked stayed in the group without a word. This is the one sentence
those refusals are reported in, wherever the group was edited.
"""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication

_CONTEXT = 'LockRefusal'


def _tr(text: str) -> str:
    return QCoreApplication.translate(_CONTEXT, text)


def refused(result) -> bool:
    """Whether a write's answer (or :class:`FailedResult`) is a refusal."""
    if result is None:
        return False
    return str(getattr(result, 'status', 'accepted') or 'accepted') != 'accepted'


def locked_titles(result) -> list | None:
    """The titles of the locked steps a refusal names, or None if it was
    refused for some other reason."""
    if result is None:
        return None
    error = getattr(result, 'error', None)
    details = (getattr(result, 'payload', None)
               or getattr(error, 'details', None)
               or getattr(result, 'details', None) or {})
    code = getattr(error, 'code', None) or getattr(result, 'code', None)
    if code != 'task_locked' and not (
            isinstance(details, dict) and details.get('unlock_operation')):
        return None
    titles = details.get('titles') if isinstance(details, dict) else None
    return [str(title) for title in (titles or ()) if str(title).strip()]


def locked_sentence(titles=()) -> str:
    """``<step> is locked because it has run; unlock it to change its groups.``"""
    titles = [str(title) for title in (titles or ()) if str(title).strip()]
    if len(titles) > 1:
        return _tr('{0} are locked because they have run; unlock them to '
                   'change their groups.').format(', '.join(titles))
    name = titles[0] if titles else _tr('This step')
    return _tr('{0} is locked because it has run; unlock it to change its '
               'groups.').format(name)


def refusal_message(result, fallback: str = '') -> str:
    """The sentence for a refused write: the lock one when a lock refused it,
    otherwise the facade's own message (or *fallback*)."""
    titles = locked_titles(result)
    if titles is not None:
        return locked_sentence(titles)
    return str(getattr(result, 'message', '') or fallback)
