"""Gmsh session actions reach the Console and the run strip (Plan 42 SG16, D9).

The Live run panel already follows every job event.  A session action
(:mod:`foammesh.core.jobs.session_actions`) publishes the same events with
``session_action=True``; this follows them for the two surfaces that were fed
by job output only: the Console and the docked run strip.  Cancel needs no
code here -- the strip's Cancel is ``job.cancel_active``, which reaches the
action's cancel callback.
"""
from __future__ import annotations

from foammesh.core.mesh.run_progress import PROGRESS_PREFIX


def _console_append(window, text: str) -> None:
    console = getattr(window, '_consoleView', None)
    if console is None:
        return
    target = console
    if not hasattr(target, 'append') and hasattr(target, 'widget'):
        target = target.widget()
    append = getattr(target, 'append', None)
    if append is not None:
        append(text)


def install_session_activity(events, window_getter):
    """Follow session-action events; returns one unsubscribe callable."""
    from foammesh.core.project import Event

    names: dict[str, str] = {}

    def window():
        try:
            return window_getter()
        except RuntimeError:
            return None

    def started(**payload):
        if not payload.get('session_action'):
            return
        names[str(payload.get('job_id'))] = str(payload.get('name') or '')
        target = window()
        if target is not None:
            target.showRunStarted(f'Gmsh: {payload.get("name")}')

    def output(**payload):
        if not payload.get('session_action'):
            return
        target = window()
        if target is None:
            return
        for line in payload.get('lines') or ():
            if not str(line).startswith(PROGRESS_PREFIX):
                _console_append(target, str(line))

    def ended(outcome):
        def callback(**payload):
            if not payload.get('session_action'):
                return
            name = names.pop(str(payload.get('job_id')),
                             str(payload.get('name') or ''))
            target = window()
            if target is None:
                return
            text = {'done': f'Gmsh: {name} finished.',
                    'failed': f'Gmsh: {name} failed.',
                    'cancelled': f'Gmsh: {name} was cancelled. The mesh '
                                 'in the session was rebuilt from its log.'
                    }[outcome]
            if outcome == 'failed' and payload.get('message'):
                text += f' {payload["message"]}'
            target.showRunStatus(text, failed=outcome == 'failed')
        return callback

    unsubscribes = [
        events.subscribe(Event.JOB_STARTED, started),
        events.subscribe(Event.JOB_OUTPUT, output),
        events.subscribe(Event.JOB_FINISHED, ended('done')),
        events.subscribe(Event.JOB_FAILED, ended('failed')),
        events.subscribe(Event.JOB_CANCELLED, ended('cancelled')),
    ]

    def release():
        for unsubscribe in unsubscribes:
            unsubscribe()
        unsubscribes.clear()

    return release
