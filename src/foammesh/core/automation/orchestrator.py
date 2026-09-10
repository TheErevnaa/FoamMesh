"""AF6 deterministic workflow orchestrator (§10).

Runs a :class:`Recipe` as a confirmed plan, executing one step at a time and
enforcing :class:`Guardrails` at every step boundary. It stops *safely* — never
mid-atomic-commit — when a guardrail trips, a capability is missing, a step
fails, or the run is cancelled. Checkpoints record a quality/recovery gate. A
run cannot execute anything outside its confirmed plan's authorization scope.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field

from foammesh.core.facade import Actor, ActorKind, Command, CommandSource
from foammesh.core.facade.errors import FacadeError

from .guardrails import Guardrails, GuardrailViolation


@dataclass
class StepOutcome:
    index: int
    operation: str
    status: str                 # 'succeeded' | 'stopped'
    payload: dict = field(default_factory=dict)
    checkpoint: dict | None = None
    error: dict | None = None


@dataclass
class OrchestrationReport:
    recipe: str
    plan_id: str
    status: str                 # 'succeeded' | 'stopped' | 'cancelled'
    steps: list = field(default_factory=list)
    stopped_reason: dict | None = None
    completed: int = 0

    def to_dict(self) -> dict:
        return {
            'recipe': self.recipe, 'plan_id': self.plan_id, 'status': self.status,
            'completed': self.completed, 'stopped_reason': self.stopped_reason,
            'steps': [{'index': s.index, 'operation': s.operation, 'status': s.status,
                       'checkpoint': s.checkpoint, 'error': s.error} for s in self.steps],
        }


class RunHandle:
    """A cancel token for an in-flight orchestration."""

    def __init__(self):
        self._cancelled = False
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        await self._event.wait()


class Orchestrator:
    def __init__(self, facade, guardrails: Guardrails | None = None, *,
                 clock=time.monotonic, actor_id: str = 'orchestrator'):
        self.facade = facade
        self.guardrails = guardrails or Guardrails()
        self._clock = clock
        self.actor_id = actor_id

    async def run(self, case_id: str, recipe, *, confirmed_by: str = 'external-user',
                  attempt: int = 1, start_index: int = 0, handle: RunHandle | None = None,
                  estimated_cells: int | None = None) -> OrchestrationReport:
        """Validate → confirm → execute a recipe with guardrails and checkpoints."""
        handle = handle or RunHandle()
        report = OrchestrationReport(recipe.name, plan_id='', status='stopped')
        if handle.cancelled:
            report.status = 'cancelled'
            report.stopped_reason = {'code': 'cancelled', 'message': 'run cancelled'}
            return report

        # Pre-flight guardrails (before any mutation).
        try:
            self.guardrails.check_attempt(attempt)
            self.guardrails.check_cells(estimated_cells)
            case_path = self.facade.case(case_id).case_path
            self.guardrails.check_root(case_path)
            self.guardrails.check_disk(shutil.disk_usage(case_path).free)
            required = sorted({capability for step in recipe.steps
                               for capability in (self.facade.operations.get(step.operation).capabilities
                                                  if self.facade.operations.get(step.operation) else ())})
            unavailable = [item for item in self.facade.describe_capabilities(required)['capabilities']
                           if not item['available']]
            if unavailable:
                raise GuardrailViolation(
                    'capability_unavailable', 'required runtime capability is unavailable',
                    details={'capabilities': unavailable})
        except GuardrailViolation as violation:
            report.stopped_reason = violation.to_dict()
            return report

        planned_commands = []
        for step in recipe.steps:
            parameters = dict(step.parameters)
            descriptor = self.facade.operations.get(step.operation)
            if (descriptor and descriptor.capabilities
                    and self.guardrails.utility_timeout_seconds is not None
                    and 'timeout_seconds' not in parameters):
                parameters['timeout_seconds'] = self.guardrails.utility_timeout_seconds
            planned_commands.append({'operation': step.operation, 'parameters': parameters})
        draft = self.facade.validate_plan(case_id, planned_commands)
        report.plan_id = draft['plan_id']
        authorization = self.facade.confirm_plan(
            case_id, draft['plan_id'], draft['digest'], confirmed_by=confirmed_by)
        token = authorization['token']

        # A multi-step plan applies several transactions; mark it executing so
        # later steps are not rejected as stale by their own earlier edits
        # (freshness is asserted at confirm, not between a plan's own steps).
        from foammesh.core.facade.plans import PlanState
        plan = self.facade.plans.get(draft['plan_id'])
        plan.state = PlanState.EXECUTING
        self.facade.case(case_id)._emit('plan.executing', plan_id=draft['plan_id'])

        started = self._clock()
        for index, step in enumerate(recipe.steps):
            if index < start_index:
                continue
            if handle.cancelled:
                report.status = 'cancelled'
                report.stopped_reason = {'code': 'cancelled', 'message': 'run cancelled'}
                return report
            try:
                self.guardrails.check_wall_clock(self._clock() - started)
                self.guardrails.check_disk(
                    shutil.disk_usage(self.facade.case(case_id).case_path).free)
            except GuardrailViolation as violation:
                report.stopped_reason = violation.to_dict()
                return report

            parameters = dict(planned_commands[index]['parameters'])
            command = Command(
                step.operation, case_id, parameters,
                Actor(self.actor_id, ActorKind.AGENT), CommandSource.AUTOMATION,
                expected_revision=self.facade.case(case_id).authored_revision,
                authorization={'plan_id': draft['plan_id'], 'token': token},
                correlation_id=draft['plan_id'])
            try:
                execution = asyncio.create_task(self.facade.execute(command))
                cancellation = asyncio.create_task(handle.wait())
                done, _pending = await asyncio.wait(
                    {execution, cancellation}, return_when=asyncio.FIRST_COMPLETED)
                if cancellation in done and handle.cancelled and not execution.done():
                    await self.facade.case(case_id).jobs.cancel_all()
                    result = await execution
                    report.status = 'cancelled'
                    report.stopped_reason = {'code': 'cancelled', 'message': 'run cancelled'}
                    self.facade.plans.get(draft['plan_id']).state = _failed_state()
                    return report
                cancellation.cancel()
                result = await execution
                if result.status not in {'accepted', 'ok'}:
                    raise GuardrailViolation(
                        'operation_failed', f'{step.operation} returned {result.status}',
                        details={'operation': step.operation, 'payload': result.payload})
                encoded_size = len(json.dumps(
                    result.to_dict(), sort_keys=True, default=str).encode('utf-8'))
                self.guardrails.check_output(encoded_size)
                quality = _quality_metric(result.payload)
                if step.checkpoint and quality is not None:
                    if not self.guardrails.check_quality(quality):
                        raise GuardrailViolation(
                            'quality_target_missed',
                            f'non-orthogonality {quality} exceeds target',
                            details={'max_non_orthogonality': quality})
                    previous_quality = next((
                        outcome.checkpoint.get('max_non_orthogonality')
                        for outcome in reversed(report.steps)
                        if outcome.checkpoint and outcome.checkpoint.get('max_non_orthogonality') is not None
                    ), None)
                    self.guardrails.check_regression(previous_quality, quality)
            except (FacadeError, GuardrailViolation) as error:
                # Stop safely — no partial mesh mutation, plan marked failed.
                self.facade.plans.get(draft['plan_id']).state = _failed_state()
                report.steps.append(StepOutcome(
                    index, step.operation, 'stopped',
                    error=error.to_dict() if hasattr(error, 'to_dict') else {'message': str(error)}))
                report.stopped_reason = (error.to_dict() if hasattr(error, 'to_dict')
                                         else {'code': 'error', 'message': str(error)})
                return report

            checkpoint = self._checkpoint(case_id) if step.checkpoint else None
            if checkpoint is not None:
                checkpoint['max_non_orthogonality'] = _quality_metric(result.payload)
            report.steps.append(StepOutcome(
                index, step.operation, 'succeeded', payload=result.payload, checkpoint=checkpoint))
            report.completed = index + 1

        report.status = 'succeeded'
        return report

    def _checkpoint(self, case_id: str) -> dict:
        """A recovery/quality gate after a checkpoint step."""
        session = self.facade.case(case_id)
        return {'authored_revision': session.authored_revision,
                'artifact_sequence': session.artifact_sequence,
                'kind': 'checkpoint'}


def _failed_state():
    from foammesh.core.facade.plans import PlanState
    return PlanState.FAILED


def _quality_metric(payload) -> float | None:
    """Find a canonical max-non-orthogonality value in nested result DTOs."""
    if isinstance(payload, dict):
        for key in ('max_non_ortho', 'max_non_orthogonality'):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        for value in payload.values():
            found = _quality_metric(value)
            if found is not None:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _quality_metric(value)
            if found is not None:
                return found
    return None
