"""AF1V-only file and deterministic asynchronous operations."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from .errors import ValidationFailedError
from .results import OperationResult


class VerticalSliceOperations:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._results: dict[str, dict] = {}

    async def generate_report(self, session, command) -> OperationResult:
        session.require_writable()
        name = command.parameters.get('name', 'af1v-report.json')
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.json'):
            raise ValidationFailedError('report name must be a plain .json filename')
        destination = session.storage_path / 'artifacts' / name
        payload = {
            'case_id': session.case_id, 'revisions': session.revisions.to_dict(),
            'configuration': session.snapshot()['configuration'],
        }

        def write():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + '.tmp')
            try:
                temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write)
        session.state.bus.publish('artifact.quality_changed', path=str(destination), kind='af1v_report')
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'artifacts': [str(destination)]})

    def start_job(self, session, command) -> OperationResult:
        steps = command.parameters.get('steps', 20)
        interval = command.parameters.get('interval', 0.02)
        if not isinstance(steps, int) or not 1 <= steps <= 1000:
            raise ValidationFailedError('steps must be an integer from 1 to 1000')
        if not isinstance(interval, (int, float)) or not 0 <= interval <= 1:
            raise ValidationFailedError('interval must be from 0 to 1 second')
        job_id = uuid4().hex
        actor = {'id': command.actor.id, 'kind': command.actor.kind.value}
        source = command.source.value
        correlation = command.correlation_id or command.command_id

        async def run():
            session.emit_as('job.started', actor=actor, source=source,
                            correlation_id=correlation, job_id=job_id,
                            name='af1v deterministic job', mutation=False)
            try:
                for index in range(steps):
                    await asyncio.sleep(interval)
                    session.emit_as('job.progress', actor=actor, source=source,
                                    correlation_id=correlation, job_id=job_id,
                                    current=index + 1, total=steps)
                result = {'job_id': job_id, 'status': 'done'}
                self._results[job_id] = result
                session.emit_as('job.finished', actor=actor, source=source,
                                correlation_id=correlation, job_id=job_id, result=result)
            except asyncio.CancelledError:
                result = {'job_id': job_id, 'status': 'cancelled', 'recovery': 'not_required'}
                self._results[job_id] = result
                session.emit_as('job.cancelled', actor=actor, source=source,
                                correlation_id=correlation, job_id=job_id, result=result)
            finally:
                self._tasks.pop(job_id, None)
                await session.flush_events()

        self._tasks[job_id] = asyncio.create_task(run(), name=f'foammesh-af1v-{job_id}')
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'job_id': job_id, 'status': 'running'})

    async def cancel_job(self, session, command) -> OperationResult:
        job_id = command.parameters.get('job_id')
        # In-process diagnostics have no process to signal, so they register a
        # budget instead. Checked first: a surface check is the one thing that
        # has been observed to run away, and the Cancel button has to reach it.
        from foammesh.core.geometry.diagnostics import budget as budget_module

        if budget_module.cancel(job_id):
            return OperationResult('accepted', command.operation, session.revisions,
                                   payload={'job_id': job_id, 'cancelled': True,
                                            'recovery': 'not_required',
                                            'kind': 'in_process_diagnostic'})
        if await session.jobs.cancel(job_id):
            return OperationResult('accepted', command.operation, session.revisions,
                                   payload={'job_id': job_id, 'cancelled': True,
                                            'recovery': 'managed_by_operation'})
        task = self._tasks.get(job_id)
        if task is None:
            return OperationResult('accepted', command.operation, session.revisions,
                                   payload={'job_id': job_id, 'cancelled': False})
        task.cancel()
        await task
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'job_id': job_id, 'cancelled': True,
                                        'recovery': 'not_required'})

    async def cancel_active_jobs(self, session, command) -> OperationResult:
        """Stop everything this case is running, without being told an id.

        Plan 30 WP-08 (F-09). Every progress surface now offers Cancel, and
        none of them knows a job id: the run they are showing was started as
        one facade command, and the job ids live inside it. Both engines are
        reachable from here for the same reason -- a snappy stage and a Gmsh
        run each reach the machine through the one :class:`JobManager` that
        owns their process groups, so killing the group kills the WSL child
        tree and the Gmsh runner alike. In-process surface diagnostics have
        no process at all; they register a budget instead, and that is the
        one thing observed to run away, so it is cancelled first.
        """
        from foammesh.core.geometry.diagnostics import budget as budget_module

        budgets = tuple(budget_module.active_ids())
        cancelled_budgets = [job_id for job_id in budgets
                             if budget_module.cancel(job_id)]
        job_ids = tuple(session.jobs.active_job_ids)
        cancelled_jobs = await session.jobs.cancel_all()
        cancelled = len(cancelled_budgets) + cancelled_jobs
        return OperationResult(
            'accepted', command.operation, session.revisions,
            payload={'cancelled': cancelled,
                     'job_ids': list(job_ids),
                     'diagnostic_ids': cancelled_budgets,
                     'recovery': ('managed_by_operation' if cancelled_jobs
                                  else 'not_required')})

    def job(self, job_id: str) -> dict:
        if job_id in self._tasks:
            return {'job_id': job_id, 'status': 'running'}
        return self._results.get(job_id, {'job_id': job_id, 'status': 'not_found'})

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
