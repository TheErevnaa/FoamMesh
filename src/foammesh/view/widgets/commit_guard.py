"""One place to say "this dialog is waiting on the write queue".

A commit is serialized behind every other mutation, so between the click on
OK and the moment the facade answers there is a window - short on a warm
machine, seconds on a cold one - in which the dialog still looks idle. Live
that window produced two faults: a second click on OK submitted the same
element twice, and the user had no signal that anything was happening.

``commit_guard`` closes both. It disables the accept button and shows the
wait cursor for the duration of the block, and restores exactly the state it
found on the way out, including when the body raises. Restoring rather than
force-enabling matters: a dialog that disabled OK for its own reasons (an
invalid field) must not come back enabled because a commit failed.
"""
from __future__ import annotations

from contextlib import contextmanager

from foammesh.core.facade.errors import RevisionConflictError
from foammesh.support.simple_db.simple_db import ConcurrentEditError

#: What a view must catch around a commit for the case to have moved under it.
#:
#: WP-13 / F-33. ``ConcurrentEditError`` is the database's own word for a lost
#: update, and no view caught it: the facade converts it to
#: :class:`RevisionConflictError` on the ``configuration.commit_working_copy``
#: route, so every dialog was written against that one shape. Any other route
#: to :meth:`ProjectState.commit` -- a sync commit, a service holding its own
#: working copy -- raises the database error unconverted, and an uncaught one
#: reaches ``sys.excepthook``: the dialog stays open with OK re-enabled and the
#: user is told nothing, which is exactly the silent lost update the merge was
#: added to prevent. Catching both here means a view names the collision
#: whichever layer reported it.
CONFLICT_ERRORS = (RevisionConflictError, ConcurrentEditError)


@contextmanager
def commit_guard(*buttons):
    """Disable ``buttons`` and show the busy cursor for the block.

    Every argument is optional and may be ``None``: dialogs that own no
    accept button still get the cursor, and test doubles that pass nothing
    still get a working context manager.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    live = [button for button in buttons if button is not None]
    previous = []
    for button in live:
        try:
            previous.append(bool(button.isEnabled()))
            button.setEnabled(False)
        except RuntimeError:  # pragma: no cover - widget already destroyed
            previous.append(True)
    QGuiApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QGuiApplication.restoreOverrideCursor()
        for button, was_enabled in zip(live, previous):
            try:
                button.setEnabled(was_enabled)
            except RuntimeError:  # pragma: no cover - dialog closed under us
                pass


def conflict_message(error) -> str:
    """What to tell the user when their working copy went stale.

    Names the leaves that collided rather than saying "conflict": the user
    knows which dialog they left open, and the path tells them which field
    to look at when they reopen it.

    Reads the leaves from either shape: the facade's
    :class:`RevisionConflictError` carries them in ``details['paths']`` and the
    database's :class:`ConcurrentEditError` carries them on ``.paths``.
    """
    details = getattr(error, 'details', None) or {}
    paths = [str(path) for path in (details.get('paths')
                                    or getattr(error, 'paths', None) or ())]
    if paths:
        return ('Another change landed while this dialog was open: '
                '{0}. Reopen to continue.'.format(', '.join(sorted(paths))))
    return ('Another change landed while this dialog was open. '
            'Reopen to continue.')
