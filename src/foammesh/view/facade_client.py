"""AF4 desktop facade client — the GUI's single write path.

The GUI is an in-process facade client (§9): view models query field IDs and
submit facade commands instead of touching ``ProjectState`` or ``Configurations``
directly. This client wraps the shared ``FoamMeshFacade`` already attached to
the desktop's live case session (``app._attachFacadeSession``), so GUI edits go
through the very same command dispatcher, revision domains, history, and event
stream as REST/CLI/agent edits — no shadow state (§4.1).

Every GUI edit is submitted as a *local human GUI command*, the highest
authority in the supersession order (§4.6). This module imports no Qt.
"""
from __future__ import annotations

from collections.abc import Callable

from foammesh.core.facade import (
    Actor, ActorKind, Command, CommandSource, FIELD_REGISTRY, OperationResult)
from foammesh.core.facade.field_adapters import read_value
from foammesh.support import lifecycle


class NoOpenCaseError(RuntimeError):
    """A facade command was requested with no case attached to the desktop."""


class DesktopFacadeClient:
    """Thin async client that GUI pages use in place of ``app.db``/``app.state``."""

    def __init__(self, facade, session_provider: Callable, *, actor_id: str = 'local-user'):
        self._facade = facade
        self._session_provider = session_provider
        self._actor = Actor(actor_id, ActorKind.HUMAN)

    # -- identity ---------------------------------------------------------- #

    @property
    def facade(self):
        return self._facade

    def session(self):
        session = self._session_provider()
        if session is None:
            raise NoOpenCaseError('no case is open in the desktop session')
        return session

    @property
    def case_id(self) -> str:
        return self.session().case_id

    def has_case(self) -> bool:
        # DP-361. Asked through `session()` rather than the provider behind it,
        # so that anything substituting a session -- a test double, a wrapper
        # that lends one out -- is answered by the same call the guarded code
        # would have made. The two must never be able to disagree.
        try:
            self.session()
        except NoOpenCaseError:
            return False
        return True

    @property
    def case_path(self):
        return self.session().case_path

    @property
    def storage_path(self):
        return self.session().storage_path

    @property
    def case_root(self):
        path = self.case_path
        return path if (path / 'constant').is_dir() else path / 'case'

    # -- commands ---------------------------------------------------------- #

    def _is_query(self, operation: str) -> bool:
        kind_of = getattr(self._facade, 'operation_kind', None)
        try:
            kind = kind_of(operation) if kind_of is not None else None
        except Exception:                                    # noqa: BLE001
            return False
        return getattr(kind, 'value', str(kind)) == 'query'

    def _command(self, operation: str, parameters: dict, *, scope: str = 'case',
                 expected_revision: int | None = None) -> Command:
        # Only application-scoped settings commands omit the case id; case and
        # presentation commands both target the attached desktop case session.
        case_id = '' if scope == 'application' else self.case_id
        # DP-551. Every GUI command is built here, so this is the one place
        # that knows what the window last asked for when a process dies.
        # Reads are left out: a page repaint asks dozens of them, and what a
        # post-mortem needs is the last thing the user set in motion.
        if not self._is_query(operation):
            lifecycle.note_operation(operation)
        return Command(operation, case_id, parameters, self._actor, CommandSource.GUI,
                       scope=scope, expected_revision=expected_revision)

    async def apply_fields(self, patch: dict, *, expected_revision: int | None = None
                           ) -> OperationResult:
        """Submit a batch configuration edit as one human GUI command."""
        command = self._command('configuration.patch', {'patch': patch},
                                 expected_revision=expected_revision)
        return await self._facade.execute(command)

    async def dry_run(self, patch: dict) -> OperationResult:
        return await self._facade.execute(self._command('configuration.dry_run', {'patch': patch}))

    def checkout(self, path: str = ''):
        """Return an editable working copy of the case configuration.

        This is a detached copy (a read); it becomes a state change only when
        handed back to :meth:`commit_working_copy`, which commits it through the
        facade command dispatcher.
        """
        return self.session().state.checkout(path)

    async def commit_working_copy(self, working_copy, *, action: str,
                                  reason: str | None = None, target: str | None = None
                                  ) -> OperationResult:
        """Commit an edited working copy (from :meth:`checkout`) via the facade."""
        return await self._facade.execute(self._command(
            'configuration.commit_working_copy',
            {'working_copy': working_copy, 'action': action,
             'reason': reason, 'target': target}))

    async def cancel_active_job(self) -> OperationResult | None:
        """Cancel the newest tracked case job through the semantic cancel path."""
        active = self.session().jobs.active_job_ids
        if not active:
            return None
        return await self.run('job.cancel', {'job_id': active[-1]})

    async def cancel_all_jobs(self) -> None:
        for job_id in tuple(self.session().jobs.active_job_ids):
            await self.run('job.cancel', {'job_id': job_id})

    def history_status(self) -> dict:
        state = self.session().state
        return {
            'can_undo': state.can_undo(), 'can_redo': state.can_redo(),
            'undo_label': state.undo_label(), 'redo_label': state.redo_label(),
        }

    async def presentation(self, operation: str, parameters: dict | None = None) -> OperationResult:
        return await self._facade.execute(
            self._command(operation, parameters or {}, scope='presentation'))

    async def undo(self) -> OperationResult:
        return await self._facade.execute(self._command('history.undo', {}))

    async def redo(self) -> OperationResult:
        return await self._facade.execute(self._command('history.redo', {}))

    # -- synchronous path for Qt sync slots (dialog accept) ----------------- #

    def commit_working_copy_sync(self, working_copy, *, action: str,
                                 reason: str | None = None, target: str | None = None
                                 ) -> OperationResult:
        """Commit a working copy from a slot whose *return* is the decision.

        C31-12 left exactly one caller: ``QDialog.accept``. A dialog closes
        itself by returning from ``accept()``, and it must stay open when the
        commit is refused, so the answer has to be in hand before the method
        returns. Everything else that committed a working copy now schedules
        the write through :func:`submit`.

        ``undo_sync``/``redo_sync`` used to live beside this and are gone:
        Undo and Redo are ordinary scheduled writes now, so nothing was left
        that had to skip the queue to record a history move.
        """
        return self._facade.execute_sync(self._command(
            'configuration.commit_working_copy',
            {'working_copy': working_copy, 'action': action,
             'reason': reason, 'target': target}))

    @staticmethod
    def _resync_external_baseline() -> None:
        """Re-baseline the case after a write this application performed.

        ``Project`` fingerprints the FoamMesh-owned files to detect edits made
        outside the app. Every desktop write now travels through the facade, so
        without this the app's own saves and meshing artifacts would be reported
        back to the user as foreign changes by the pre-operation guards.
        """
        from foammesh.app import app
        project = getattr(app, 'project', None)
        if project is not None:
            project.acceptExternalChanges()

    def run_sync(self, operation: str, parameters: dict | None = None) -> OperationResult:
        """Submit a registered *write* from a synchronous Qt slot.

        Reads belong on :meth:`query`. The difference is not cosmetic: this
        path re-baselines the project fingerprint afterwards, which walks the
        case directory, and a read has nothing to re-baseline.
        """
        result = self._facade.execute_sync(self._command(operation, parameters or {}))
        self._resync_external_baseline()
        return result

    def query(self, operation: str, parameters: dict | None = None) -> OperationResult:
        """Read through the facade from a synchronous Qt slot (Plan 30 WP-08).

        Only an operation the registry calls a *query* is allowed here, and
        the registry decides that from what the operation declares rather than
        from a list someone has to remember to update (F-09). A write reaching
        the GUI's synchronous path skips the serialized command queue, so
        refusing one here is the point of the method, not a formality.

        No fingerprint re-baseline, because a query writes nothing.
        """
        kind = self._facade.operation_kind(operation)
        if getattr(kind, 'value', str(kind)) != 'query':
            raise RuntimeError(
                f'{operation} is a {getattr(kind, "value", kind)}, not a query: '
                'use run_sync() or the asynchronous run().')
        return self._facade.execute_sync(self._command(operation, parameters or {}))

    async def cancel_active_jobs(self) -> OperationResult:
        """Stop whatever this case is running, from any progress surface.

        The surfaces that show progress do not know a job id -- the run they
        are showing was started as one facade command -- so Cancel asks the
        facade to stop the case's jobs rather than one named job (F-09).
        """
        return await self._facade.execute(
            self._command('job.cancel_active', {}))

    async def run(self, operation: str, parameters: dict | None = None) -> OperationResult:
        """Submit a registered operation that may have an async handler.

        ``mesh.engine.probe``/``self_test``/``select``
        are async facade handlers; routing them through :meth:`run_sync`
        raises ``RuntimeError`` by design.
        """
        result = await self._facade.execute(self._command(operation, parameters or {}))
        self._resync_external_baseline()
        return result

    # -- queries ----------------------------------------------------------- #

    def snapshot(self) -> dict:
        return self._facade.snapshot(self.case_id)

    def configuration(self) -> dict:
        return self.session().configuration()

    def descriptor(self, field_id: str):
        return FIELD_REGISTRY.get(field_id)

    def field_value(self, field_id: str):
        descriptor = FIELD_REGISTRY.get(field_id)
        return read_value(self.configuration(), descriptor.storage_path)

    def field_values(self, field_ids) -> dict:
        configuration = self.configuration()
        return {field_id: read_value(configuration, FIELD_REGISTRY.get(field_id).storage_path)
                for field_id in field_ids}

    def revisions(self) -> dict:
        return self.session().revisions.to_dict()

    # -- dirty-form claims + events ---------------------------------------- #

    def claim(self, field_ids):
        """Claim fields for the local human. External actors editing them get a
        conflict; the human's own subsequent apply (same actor) is allowed.
        Returns a lease whose ``claim_id`` releases exactly this claim."""
        return self.session().field_claims.claim(field_ids, actor_id=self._actor.id)

    def release(self, claim_id: str | None = None):
        if claim_id is not None:
            self.session().field_claims.release(claim_id)
        else:
            self.session().field_claims.release(actor_id=self._actor.id)

    def subscribe(self, event, callback):
        """Subscribe a view to facade/project events for external-refresh UX."""
        return self.session().state.bus.subscribe(event, callback)


def query(client, operation: str, parameters: dict | None = None) -> OperationResult:
    """Read through whatever facade client a page was handed.

    Pages take their client by injection and the tests hand them doubles, so
    this prefers :meth:`DesktopFacadeClient.query` and falls back to the older
    ``run_sync`` when the object it is given has never heard of it. One place
    knows about the fallback; the pages just read.
    """
    reader = getattr(client, 'query', None)
    if reader is None:
        return client.run_sync(operation, parameters or {})
    return reader(operation, parameters or {})


class FailedResult:
    """A facade refusal shaped like an ``OperationResult`` (C31-12).

    Facade errors arrive as exceptions. A continuation that runs after a
    scheduled write must be handed *something*, and handing it the exception
    would make every caller write the same two branches, so a refusal is
    normalized into the three attributes a result already carries.
    """

    def __init__(self, error):
        self.status = 'failed'
        self.message = str(error)
        self.payload = dict(getattr(error, 'details', None) or {})
        self.error = error


def submit(client, operation: str, parameters: dict | None = None, *,
           then=None, on_error=None):
    """Send a facade *write* from a synchronous Qt slot without freezing it.

    C31-12. The GUI made its writes with :meth:`DesktopFacadeClient.run_sync`,
    which runs the handler on the Qt thread and then walks the case directory
    to re-baseline the external-change fingerprint. Everything the window is
    doing -- a progress dialog, the viewport, the Cancel button of a meshing
    run already in flight -- stops for the whole of it.

    Most of those calls sit in slots that cannot become ``async``: they are
    reached from ``clicked``, from a page's ``refresh()``, or from a navigation
    callback whose return value is used. So the write is *scheduled* on the
    running loop instead, and whatever used to follow it is passed as ``then``
    and runs when the write is done -- the same order, off the GUI thread.

    Returns the ``asyncio.Task`` when the write was scheduled, and ``None``
    when it already ran synchronously. It runs synchronously in exactly two
    cases, both of which have no loop to schedule onto:

    * the client is a test double that implements only ``run_sync``;
    * no event loop is running (bare page construction, early startup).

    ``then(result)`` receives the ``OperationResult``, or a :class:`FailedResult`
    when the facade refused and no ``on_error`` was given. ``on_error(error)``,
    when given, receives the exception instead.
    """
    import asyncio

    def failed(error):
        if on_error is not None:
            on_error(error)
        elif then is not None:
            then(FailedResult(error))

    def run_blocking():
        # The one remaining synchronous write in the view layer. It is reached
        # only with no loop to schedule onto, where "blocking the GUI thread"
        # is not a thing that can happen.
        blocking = getattr(client, 'run_sync', None)
        if blocking is None:
            # Raised, not swallowed: a client that can neither schedule nor
            # run a write has dropped it, and a dropped write that reports
            # "refused" reads to the caller like a facade decision.
            raise AttributeError(
                f'{type(client).__name__} offers neither run() nor run_sync(),'
                f' so {operation} cannot be submitted')
        try:
            result = blocking(operation, parameters or {})
        except Exception as error:                           # noqa: BLE001
            failed(error)
            return None
        if then is not None:
            then(result)
        return None

    runner = getattr(client, 'run', None)
    if runner is None:
        return run_blocking()

    async def call():
        # DP-34, MEASURED. The outcome is handed back through `call_soon`
        # rather than called from here, and the distance matters more than it
        # looks. `then` is view code: on a refusal it opens a modal warning,
        # and a modal dialog runs a nested Qt event loop. Called from inside
        # this coroutine, that nested loop asks the running loop to step other
        # tasks while *this* task is still on the stack, and Python refuses --
        # "Cannot enter into task <foammesh-command-scheduler> ... while
        # another task is being executed". The step it refused was the command
        # scheduler's, which then never ran again: every subsequent write in
        # that session waited forever on a future nothing would set, with the
        # window still answering and no error anywhere. Seen in the `t3`
        # campaign leg, which stalled for 78 minutes after one refused edit.
        #
        # A `call_soon` callback runs from the loop with no task current, so
        # the same dialog can spin the same nested loop harmlessly. The order
        # callers depend on is unchanged -- one loop iteration later, still
        # after the write, still before anything queued behind it.
        loop = asyncio.get_running_loop()
        try:
            result = await runner(operation, parameters or {})
        except Exception as error:                           # noqa: BLE001
            loop.call_soon(failed, error)
            return
        if then is not None:
            loop.call_soon(then, result)

    # DP-79, MEASURED. This asked `ensure_future` to raise `RuntimeError` and
    # took that as "no loop". It does not: on Python 3.11
    # `asyncio.get_event_loop()` still *creates* a loop for the main thread on
    # demand, so `ensure_future` happily returned a pending Task attached to a
    # loop that was never going to run, and the write -- with everything the
    # caller passed as `then` -- was dropped in silence. A page refreshed
    # before the qasync loop starts drew nothing and reported nothing: the
    # readiness table measured 0 rows with an empty banner, which is neither
    # the report nor the refusal. The running loop is what decides, so ask for
    # it by name.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return run_blocking()
    return loop.create_task(call())
