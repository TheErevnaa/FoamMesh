"""Operation-aware execution above :mod:`foammesh.core.jobs.manager`.

This is the single bridge from a registered facade operation to an external
process. It adds parser validation, command metadata, mesh recovery, artifact
history, and project events without teaching ``JobManager`` about domain data.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from foammesh.core.case import ArtifactHistoryStore, fingerprint_poly_mesh, record_artifact_event
from foammesh.core.mesh.recovery import MeshRecoveryPoint, MeshRecoveryService
from foammesh.core.project import Event

from .job import JobStatus
from .manager import JobErrorCategory, JobManager, JobRequest, JobResult


Parser = Callable[[JobResult], Any | Awaitable[Any]]
ArtifactValidator = Callable[[Path], bool]

#: Plan 37 UF5: how long a run waits for the case lease (a running check)
#: before it is refused as busy.
LEASE_WAIT_SECONDS = 120.0


@dataclass(frozen=True)
class ExpectedArtifact:
    """An output that must exist and may receive additional validation."""

    path: Path
    kind: str = 'file'
    validator: ArtifactValidator | None = None
    produced_by_parser: bool = False


@dataclass(frozen=True)
class OperationSpec:
    operation: str
    argv: tuple[str, ...]
    cwd: Path
    mutation: bool = False
    environment: dict[str, str] | None = None
    timeout: float | None = None
    max_output_bytes: int = 1024 * 1024
    log_path: Path | None = None
    expected_artifacts: tuple[ExpectedArtifact, ...] = ()
    parser: Parser | None = None
    recover_mesh: bool = False
    artifact_event: str = Event.ARTIFACT_MESH_CHANGED
    invalidated_outputs: tuple[str, ...] = ()
    cleanup_argv: tuple[str, ...] = ()
    #: DP-817. Whether a successful run is expected to change a mesh, and
    #: which one. DP-113 compares ``constant/polyMesh`` before and after, which
    #: is only the mesh a serial meshing stage writes: ``surfaceFeatures``
    #: writes edge files and no mesh at all, and a parallel stage writes the
    #: processor meshes and leaves the case root for ``reconstructPar``. Both
    #: were warned about on every run, and the warning set the stage's task to
    #: WARNING. ``mesh_change_probes`` names the mesh directories the run
    #: writes (empty means ``constant/polyMesh``); ``expects_mesh_change``
    #: turns the comparison off for a run that writes no mesh.
    expects_mesh_change: bool = True
    mesh_change_probes: tuple[Path, ...] = ()
    #: Plan 35 CR5. How many ranks the run uses (a failed parallel run's
    #: processor cases are deleted by the recovery that follows it), and the
    #: silence before the [Keep waiting] / [Stop] prompt (``None``: the job
    #: kind's default).
    ranks: int = 1
    idle_timeout: float | None = None
    #: Plan 37 UF17. Extra fields for the run record, as ``((key, value),)``
    #: -- ``staging`` names a run that writes into a staging copy, whose
    #: failure must not discard the case's live processor cases.
    record_extra: tuple = ()


@dataclass(frozen=True)
class OperationContext:
    """Dependencies supplied to a production facade operation handler."""

    session: Any
    jobs: JobManager
    executor: 'OperationExecutor'
    artifact_history: ArtifactHistoryStore
    capabilities: Any = None

    @classmethod
    def from_session(cls, session, *, capabilities=None) -> 'OperationContext':
        executor = OperationExecutor(session.jobs)
        return cls(session, session.jobs, executor,
                   ArtifactHistoryStore(session.case_path), capabilities)


@dataclass(frozen=True)
class OperationExecution:
    operation: str
    job: JobResult
    parsed: Any = None
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    recovery_id: str | None = None
    recovery_status: str | None = None
    history_entry_id: str | None = None
    artifacts: tuple[dict[str, str], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.job.status is JobStatus.DONE

    def to_payload(self) -> dict:
        return {
            'job_id': self.job.job_id,
            'job': self.job.to_dict(),
            'parsed': self.parsed,
            'artifacts': list(self.artifacts),
            'before_fingerprint': self.before_fingerprint,
            'after_fingerprint': self.after_fingerprint,
            'recovery': {'recovery_id': self.recovery_id, 'status': self.recovery_status},
            'history_entry_id': self.history_entry_id,
        }


class OperationExecutor:
    """Execute one registered operation through the case's ``JobManager``."""

    def __init__(self, jobs: JobManager, *, recovery: MeshRecoveryService | None = None):
        self.jobs = jobs
        self.recovery = recovery or MeshRecoveryService()

    async def execute(self, session, spec: OperationSpec, *, on_line=None) -> OperationExecution:
        # Plan 37 UF5 DP-1039. The case mutation lease: a run that writes the
        # case holds it exclusively, a reader shares it, so a check never
        # reads a mesh a mesher is rewriting and an unlock never moves files
        # under a running job. A mutation that cannot get the case in time is
        # refused before anything is spawned or snapshotted.
        from . import case_lease
        mode = case_lease.EXCLUSIVE if spec.mutation else case_lease.SHARED
        try:
            lease = case_lease.acquire(session.case_path, mode, spec.operation,
                                       timeout=LEASE_WAIT_SECONDS)
            await lease.__aenter__()
        except case_lease.CaseBusyError as busy:
            return self._refuse_busy(session, spec, busy)
        try:
            return await self._execute_held(session, spec, on_line=on_line)
        finally:
            await lease.__aexit__(None, None, None)

    def _refuse_busy(self, session, spec: OperationSpec, busy) -> OperationExecution:
        request = JobRequest(name=spec.operation, argv=tuple(str(item) for item in spec.argv),
                             cwd=Path(spec.cwd), mutation=spec.mutation)
        refusal = getattr(self.jobs, 'record_refusal', None)
        if refusal is None:
            raise RuntimeError(str(busy))
        job = refusal(request, str(busy), exit={
            'kind': 'case_busy', 'reason': str(busy), 'signal': '',
            'actions': ['retry'], 'hint': '', 'holders': list(busy.holders)})
        session.state.bus.publish(
            Event.OPERATION_FAILED, operation=spec.operation, job_id=job.job_id,
            result=job.to_dict(), recovery_status=None, history_entry_id=None)
        return OperationExecution(spec.operation, job)

    async def _execute_held(self, session, spec: OperationSpec, *, on_line=None) -> OperationExecution:
        if not spec.operation or not spec.argv:
            raise ValueError('operation and argv are required')
        cwd = Path(spec.cwd).resolve()
        case_path = Path(session.case_path).resolve()
        if cwd != case_path and case_path not in cwd.parents:
            raise ValueError('operation working directory must stay inside the case')
        # Every execution writes a bounded log and artifact-history record, even
        # when the external utility itself is observational (for example
        # checkMesh). Read-only sessions therefore cannot start operations.
        session.require_writable()

        log_path = spec.log_path
        if log_path is None:
            safe_name = spec.operation.replace('.', '-')
            log_path = session.storage_path / 'logs' / f'{safe_name}-{uuid4().hex[:12]}.log'

        if spec.mutation:
            # Plan 35 CR5 step 6. No new writer while an earlier run of this
            # case is unresolved -- a crash, a lost WSL connection, a GUI
            # that died mid-run. Resolving restores what it half wrote.
            refused = await self._pass_recovery_gate(session, spec, cwd)
            if refused is not None:
                return refused
            # Plan 37 UF5 DP-1041. An unlock or undo a crash interrupted is
            # finished or rolled back first; one that cannot be (an undo
            # waiting for the project state) refuses the run.
            refused = self._pass_unlock_recovery(session, spec)
            if refused is not None:
                return refused
            # Plan 37 UF17. A core-count change a crash interrupted is
            # finished or discarded before anything else writes the case.
            refused = self._pass_redistribute_recovery(session, spec)
            if refused is not None:
                return refused
        before = await self._mesh_fingerprint(session.case_path) if spec.mutation else None
        probed_before = (await self._probe_fingerprint(spec.mesh_change_probes)
                         if spec.mutation and spec.mesh_change_probes else None)
        recovery_point = await self._prepare_recovery(session, spec)
        if spec.mutation and spec.recover_mesh:
            # Plan 37 UF5 DP-1042. A mesher run is admitted and its own
            # rollback point is durable: "Restore previous mesh and settings"
            # no longer describes a state to go back to. A refused launch
            # returned above and kept it.
            from . import unlock_transaction
            unlock_transaction.consume_undo(session.case_path, reason=spec.operation)
        session.state.bus.publish(
            Event.OPERATION_STARTED, operation=spec.operation,
            mutation=spec.mutation, recovery_id=(
                recovery_point.recovery_id if recovery_point else None))
        record_fields = {
            'operation': spec.operation, 'ranks': int(spec.ranks or 1),
            'recovery_id': recovery_point.recovery_id if recovery_point else None,
            **dict(spec.record_extra or ()),
        } if spec.mutation else None
        request = JobRequest(
            name=spec.operation,
            argv=tuple(str(item) for item in spec.argv),
            cwd=cwd,
            mutation=spec.mutation,
            environment=spec.environment,
            log_path=log_path,
            # Plan 37 #7. 0 is the Preferences "No limit": the job manager
            # waits without a deadline rather than refusing it.
            timeout=(None if spec.timeout is not None and spec.timeout <= 0
                     else spec.timeout),
            max_output_bytes=spec.max_output_bytes,
            expected_outputs=tuple(
                item.path for item in spec.expected_artifacts
                if not item.produced_by_parser),
            cleanup_argv=spec.cleanup_argv,
            idle_timeout=spec.idle_timeout,
            record_case=Path(session.case_path) if spec.mutation else None,
            record_fields=record_fields,
        )
        job = await self.jobs.run(request, on_line=on_line)
        parsed = None
        warnings = list(job.warnings)

        if job.status is JobStatus.DONE and spec.parser is not None:
            try:
                parsed = spec.parser(job)
                if inspect.isawaitable(parsed):
                    parsed = await parsed
            except Exception as error:
                job = replace(job, status=JobStatus.FAILED,
                              error=f'operation output could not be parsed: {error}',
                              error_category=JobErrorCategory.PARSE)

        if job.status is JobStatus.DONE:
            invalid = []
            for item in spec.expected_artifacts:
                path = item.path if item.path.is_absolute() else cwd / item.path
                if item.validator is not None and not item.validator(path):
                    invalid.append(str(path))
            if invalid:
                job = replace(job, status=JobStatus.FAILED,
                              error='artifact validation failed: ' + ', '.join(invalid),
                              error_category=JobErrorCategory.VALIDATION)

        recovery_status = None
        run_records = None
        if job.run_state is not None:
            from .run_records import RunRecordStore
            run_records = RunRecordStore(session.case_path)
        if job.run_state == 'recovery_pending':
            # The transport ended without the wrapper saying the run was
            # over: the writer may still be alive, so nothing is restored
            # until it is confirmed dead (plan 35 CR5 step 6).
            job, recovery_status = await self._recover_lost_run(
                session, spec, job, recovery_point)
        elif recovery_point is not None:
            if job.status is JobStatus.DONE:
                try:
                    await self._to_thread(self.recovery.mark_available, recovery_point)
                    recovery_status = 'available'
                except Exception as error:
                    # DP-111. The stage ran and its mesh is on disk. This
                    # journal entry is bookkeeping for a rollback the user may
                    # never ask for, so a failure to write it costs exactly
                    # that rollback -- it must not turn a finished stage into a
                    # refusal. Record what was lost and keep the result.
                    recovery_status = 'unavailable'
                    warnings.append(
                        'the mesh was produced, but the recovery point for it could not be '
                        f'recorded, so the previous mesh cannot be restored: {error}')
            else:
                try:
                    self._move_record(run_records, job, 'restoring')
                    await self._to_thread(self.recovery.restore, session.case_path, recovery_point)
                    recovery_status = 'restored'
                    self._move_record(run_records, job, 'closed', resolution='restored')
                    session.state.bus.publish(
                        Event.ARTIFACT_RESTORED, operation=spec.operation,
                        recovery_id=recovery_point.recovery_id, job_id=job.job_id)
                    session.state.bus.publish(
                        Event.OPERATION_RECOVERED, operation=spec.operation,
                        recovery_id=recovery_point.recovery_id, job_id=job.job_id)
                except Exception as error:
                    # The record stays at `restoring`: the next run's gate
                    # tries again before anything else writes the mesh.
                    recovery_status = 'failed'
                    job = replace(job, status=JobStatus.FAILED,
                                  error=f'{job.error or "operation failed"}; recovery failed: {error}',
                                  error_category=JobErrorCategory.RECOVERY)
        if job.run_state == 'exited' and recovery_status != 'failed':
            self._move_record(
                run_records, job, 'closed',
                resolution='finished' if job.status is JobStatus.DONE else 'failed')
        if job.run_state is not None and run_records is not None:
            record = await self._to_thread(run_records.load, job.run_id or job.job_id)
            if record is not None and record.get('state') != job.run_state:
                job = replace(job, run_state=record.get('state'))

        self.jobs.record_result(job)

        after = await self._mesh_fingerprint(session.case_path) if spec.mutation else None
        changed_before, changed_after = before, after
        if spec.mesh_change_probes:
            changed_before = probed_before
            changed_after = await self._probe_fingerprint(spec.mesh_change_probes)
        if (job.status is JobStatus.DONE and spec.mutation
                and spec.expects_mesh_change
                and changed_before is not None
                and changed_before == changed_after):
            # DP-113. MEASURED on `two_solid_block`: castellation, snap and
            # layers each reported 91,562 cells, 31,920 boundary faces and
            # 129,402 points, and the outline ticked all three. The only mesh
            # any of them produced was the one the first of them made. A tick
            # is a claim that something happened, and both fingerprints were
            # already computed -- what was missing was comparing them. This
            # does not fail the stage: running a stage that turns out to be a
            # no-op is not an error, it is a result worth stating.
            warnings.append(
                'this stage completed without changing the mesh: the mesh it '
                'wrote is identical to the one it started from')
        artifacts = []
        for item in spec.expected_artifacts:
            path = item.path if item.path.is_absolute() else cwd / item.path
            if path.exists():
                artifacts.append({'path': str(path), 'kind': item.kind})
        artifact_payload = tuple(artifacts)

        entry = await self._to_thread(
            record_artifact_event, session.case_path,
            operation=spec.operation, status=job.status.value, command=job.argv,
            before_fingerprint=before, after_fingerprint=after,
            recovery_id=recovery_point.recovery_id if recovery_point else None,
            recovery_status=recovery_status,
            details={'job': job.to_dict(), 'artifacts': list(artifact_payload)},
        )

        if job.status is JobStatus.DONE and (spec.mutation or artifact_payload or parsed is not None):
            session.state.bus.publish(
                spec.artifact_event, operation=spec.operation, job_id=job.job_id,
                artifacts=list(artifact_payload), before_fingerprint=before,
                after_fingerprint=after, history_entry_id=entry.entry_id)

        terminal_event = (Event.OPERATION_SUCCEEDED if job.status is JobStatus.DONE
                          else Event.OPERATION_FAILED)
        session.state.bus.publish(
            terminal_event, operation=spec.operation, job_id=job.job_id,
            result=job.to_dict(), recovery_status=recovery_status,
            history_entry_id=entry.entry_id)

        return OperationExecution(
            spec.operation, job, parsed, before, after,
            recovery_point.recovery_id if recovery_point else None,
            recovery_status, entry.entry_id, artifact_payload, tuple(warnings))

    # -- plan 35 CR5: run records and the recovery gate ----------------------

    def _gate(self, session):
        from .run_records import RecoveryGate
        return RecoveryGate(session.case_path, recovery=self.recovery)

    def _active_run_ids(self) -> tuple[str, ...]:
        return tuple(getattr(self.jobs, 'active_job_ids', ()) or ())

    async def _pass_recovery_gate(self, session, spec: OperationSpec,
                                  cwd: Path) -> OperationExecution | None:
        """Resolve earlier runs of this case, or refuse this one."""
        from .run_records import run_directory
        if not run_directory(session.case_path).is_dir():
            return None
        gate = self._gate(session)
        verdict = await self._to_thread(gate.resolve, active_run_ids=self._active_run_ids())
        if verdict.closed:
            session.state.bus.publish(
                Event.OPERATION_RECOVERED, operation=spec.operation,
                recovery_id=None, job_id=None, runs=list(verdict.closed))
        if not verdict.blocked:
            return None
        request = JobRequest(name=spec.operation, argv=tuple(str(item) for item in spec.argv),
                             cwd=cwd, mutation=spec.mutation)
        refusal = getattr(self.jobs, 'record_refusal', None)
        if refusal is None:
            raise RuntimeError(verdict.message)
        job = refusal(request, verdict.message, exit={
            'kind': 'recovery_pending', 'reason': verdict.message, 'signal': '',
            'actions': ['stop_old_run', 'retry'], 'hint': '',
            'read_only': verdict.read_only,
            'runs': [record.get('run_id') for record in verdict.records]})
        session.state.bus.publish(
            Event.OPERATION_FAILED, operation=spec.operation, job_id=job.job_id,
            result=job.to_dict(), recovery_status='pending', history_entry_id=None)
        return OperationExecution(spec.operation, job, recovery_status='pending')

    async def _recover_lost_run(self, session, spec: OperationSpec, job: JobResult,
                                recovery_point) -> tuple[JobResult, str]:
        gate = self._gate(session)
        verdict = await self._to_thread(
            gate.resolve, active_run_ids=self._active_run_ids(), wait_for_death=True)
        run_id = job.run_id or job.job_id
        record = gate.store.load(run_id) or {}
        if record.get('state') == 'closed':
            restored = recovery_point is not None and any(
                str(note) == 'the previous mesh was restored'
                for note in record.get('notes') or ())
            if restored:
                session.state.bus.publish(
                    Event.ARTIFACT_RESTORED, operation=spec.operation,
                    recovery_id=recovery_point.recovery_id, job_id=job.job_id)
                session.state.bus.publish(
                    Event.OPERATION_RECOVERED, operation=spec.operation,
                    recovery_id=recovery_point.recovery_id, job_id=job.job_id)
            notes = '; '.join(str(note) for note in record.get('notes') or ())
            if notes and job.status is not JobStatus.DONE:
                job = replace(job, error=f'{job.error or "operation failed"} ({notes})')
            return replace(job, run_state='closed'), ('restored' if restored else 'closed')
        message = verdict.message or 'the run could not be confirmed stopped'
        return (replace(job, status=JobStatus.FAILED,
                        error=f'{job.error or "operation failed"}; {message}',
                        error_category=JobErrorCategory.RECOVERY,
                        run_state=record.get('state') or job.run_state),
                'pending')

    @staticmethod
    def _move_record(store, job: JobResult, state: str, **fields) -> None:
        if store is None:
            return
        run_id = job.run_id or job.job_id
        try:
            record = store.load(run_id)
            if record is None or record.get('state') in (state, 'closed'):
                return
            store.transition(run_id, state, **fields)
        except Exception:  # noqa: BLE001 - the next gate resolves a stale record
            import logging
            logging.getLogger(__name__).exception(
                'run record %s could not move to %s', run_id, state)

    def _pass_unlock_recovery(self, session, spec: OperationSpec):
        from . import unlock_transaction
        case_path = Path(session.case_path)
        if not unlock_transaction.pending_root(case_path).is_dir():
            return None
        store = None
        try:
            from foammesh.core.engine.registry import (
                ENGINE_REGISTRY, configured_engine_id)
            from foammesh.core.workflow.task_state_store import EngineTaskStateStore
            engine_id = configured_engine_id(session.state.db)
            store = EngineTaskStateStore(
                case_path, ENGINE_REGISTRY.get(engine_id).workflow_descriptor())
        except Exception:  # noqa: BLE001 - no engine: nothing to roll back into
            store = None
        unlock_transaction.recover(case_path, store)
        waiting = unlock_transaction.blocking_recovery(case_path)
        if not waiting:
            return None
        message = ('an interrupted unlock or undo of this case has to be '
                   'finished first: choose "Restore previous mesh and '
                   'settings" to complete it')
        request = JobRequest(name=spec.operation, argv=tuple(str(item) for item in spec.argv),
                             cwd=Path(spec.cwd), mutation=spec.mutation)
        refusal = getattr(self.jobs, 'record_refusal', None)
        if refusal is None:
            raise RuntimeError(message)
        job = refusal(request, message, exit={
            'kind': 'unlock_pending', 'reason': message, 'signal': '',
            'actions': ['undo_unlock'], 'hint': '', 'operations': waiting})
        session.state.bus.publish(
            Event.OPERATION_FAILED, operation=spec.operation, job_id=job.job_id,
            result=job.to_dict(), recovery_status=None, history_entry_id=None)
        return OperationExecution(spec.operation, job)

    def _pass_redistribute_recovery(self, session, spec: OperationSpec):
        from . import redistribute_transaction as transaction
        case_path = Path(session.case_path)
        if not transaction.blocking(case_path):
            return None
        report = transaction.recover(case_path)
        for item in report['settings']:
            try:
                transaction.apply_setting(session, item['target_ranks'],
                                          reason='redistribute recovery')
            except Exception:  # noqa: BLE001 - stays pending for the next pass
                import logging
                logging.getLogger(__name__).exception(
                    'core-count setting of %s could not be published', item['id'])
                continue
            transaction.finish(case_path, item['id'])
        waiting = transaction.blocking(case_path)
        if not waiting:
            return None
        message = ('an interrupted core-count change of this case has to be '
                   'settled first: reopen the case once its run has stopped')
        request = JobRequest(name=spec.operation, argv=tuple(str(item) for item in spec.argv),
                             cwd=Path(spec.cwd), mutation=spec.mutation)
        refusal = getattr(self.jobs, 'record_refusal', None)
        if refusal is None:
            raise RuntimeError(message)
        job = refusal(request, message, exit={
            'kind': 'redistribute_pending', 'reason': message, 'signal': '',
            'actions': [], 'hint': '', 'operations': waiting})
        session.state.bus.publish(
            Event.OPERATION_FAILED, operation=spec.operation, job_id=job.job_id,
            result=job.to_dict(), recovery_status=None, history_entry_id=None)
        return OperationExecution(spec.operation, job)

    async def _prepare_recovery(self, session, spec: OperationSpec) -> MeshRecoveryPoint | None:
        mesh = session.case_path / 'constant' / 'polyMesh'
        if not (spec.mutation and spec.recover_mesh and mesh.is_dir()):
            return None
        return await self._to_thread(
            self.recovery.snapshot, session.case_path, operation=spec.operation)

    @staticmethod
    async def _mesh_fingerprint(case_path: Path) -> str | None:
        mesh = Path(case_path) / 'constant' / 'polyMesh'
        if not mesh.is_dir():
            return None
        try:
            value = await OperationExecutor._to_thread(fingerprint_poly_mesh, mesh)
            return value.digest
        except (OSError, ValueError):
            return None

    @staticmethod
    async def _probe_fingerprint(meshes) -> str | None:
        """One digest over several mesh directories, or ``None`` if any is
        missing or unreadable -- an unknown fingerprint claims nothing."""
        digests = []
        for mesh in meshes:
            mesh = Path(mesh)
            if not mesh.is_dir():
                return None
            try:
                value = await OperationExecutor._to_thread(
                    fingerprint_poly_mesh, mesh)
            except (OSError, ValueError):
                return None
            digests.append(f'{mesh.as_posix()}={value.digest}')
        return '|'.join(digests) if digests else None

    @staticmethod
    async def _to_thread(function, /, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(function, *args, **kwargs)
