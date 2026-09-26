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

        before = await self._mesh_fingerprint(session.case_path) if spec.mutation else None
        recovery_point = await self._prepare_recovery(session, spec)
        session.state.bus.publish(
            Event.OPERATION_STARTED, operation=spec.operation,
            mutation=spec.mutation, recovery_id=(
                recovery_point.recovery_id if recovery_point else None))
        request = JobRequest(
            name=spec.operation,
            argv=tuple(str(item) for item in spec.argv),
            cwd=cwd,
            mutation=spec.mutation,
            environment=spec.environment,
            log_path=log_path,
            timeout=spec.timeout,
            max_output_bytes=spec.max_output_bytes,
            expected_outputs=tuple(
                item.path for item in spec.expected_artifacts
                if not item.produced_by_parser),
            cleanup_argv=spec.cleanup_argv,
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
        if recovery_point is not None:
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
                    await self._to_thread(self.recovery.restore, session.case_path, recovery_point)
                    recovery_status = 'restored'
                    session.state.bus.publish(
                        Event.ARTIFACT_RESTORED, operation=spec.operation,
                        recovery_id=recovery_point.recovery_id, job_id=job.job_id)
                    session.state.bus.publish(
                        Event.OPERATION_RECOVERED, operation=spec.operation,
                        recovery_id=recovery_point.recovery_id, job_id=job.job_id)
                except Exception as error:
                    recovery_status = 'failed'
                    job = replace(job, status=JobStatus.FAILED,
                                  error=f'{job.error or "operation failed"}; recovery failed: {error}',
                                  error_category=JobErrorCategory.RECOVERY)

        self.jobs.record_result(job)

        after = await self._mesh_fingerprint(session.case_path) if spec.mutation else None
        if (job.status is JobStatus.DONE and spec.mutation
                and before is not None and before == after):
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
    async def _to_thread(function, /, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(function, *args, **kwargs)
