"""Small, evidence-oriented AF1V plan and confirmation implementation."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import uuid4

from .errors import AuthorizationRequiredError, PlanStaleError, ValidationFailedError


class PlanState(str, Enum):
    AWAITING_CONFIRMATION = 'awaiting_confirmation'
    AUTHORIZED = 'authorized'
    EXECUTING = 'executing'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    STALE = 'stale'
    REVERTED = 'reverted'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


def _now():
    return datetime.now(timezone.utc)


@dataclass
class SlicePlan:
    case_id: str
    session_epoch: int
    authored_revision: int
    artifact_sequence: int
    commands: list[dict]
    normalized_diff: list[dict]
    impact: tuple[str, ...]
    plan_id: str = field(default_factory=lambda: str(uuid4()))
    state: PlanState = PlanState.AWAITING_CONFIRMATION
    digest: str = ''
    token: str | None = None
    expires_at: datetime | None = None
    applied_revision: int | None = None
    transaction_id: str | None = None
    confirmation_class: str = 'batch'
    required_capabilities: tuple[str, ...] = ()
    capability_digest: str = ''
    binds_artifacts: bool = False

    def public(self) -> dict:
        return {
            'plan_id': self.plan_id, 'case_id': self.case_id, 'state': self.state.value,
            'session_epoch': self.session_epoch,
            'authored_revision': self.authored_revision, 'commands': self.commands,
            'artifact_sequence': self.artifact_sequence,
            'normalized_diff': self.normalized_diff, 'impact': list(self.impact),
            'digest': self.digest,
            'confirmation_required': self.confirmation_class != 'none',
            'confirmation_class': self.confirmation_class,
            'estimate': {'kind': 'command_plan', 'command_count': len(self.commands),
                         'field_changes': len(self.normalized_diff)},
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'applied_revision': self.applied_revision,
            'transaction_id': self.transaction_id,
            'required_capabilities': list(self.required_capabilities),
            'capability_digest': self.capability_digest,
            'binds_artifacts': self.binds_artifacts,
        }


class PlanStore:
    def __init__(self):
        self._plans: dict[str, SlicePlan] = {}

    def validate(self, session, commands: list[dict], *, field_paths: dict[str, str],
                 operation_registry=None, capability_digest: str = '') -> SlicePlan:
        """Normalize a plan of generic typed commands (§6.4, replacing the narrow
        proposal mapper). Every command must be a registered operation; impact
        and the required confirmation class are derived from the registry."""
        from .confirmation import impact_of, required_confirmation
        if not commands:
            raise ValidationFailedError('plan must contain at least one command')
        configuration = session.snapshot()['configuration']
        diff = []
        normalized = []
        impacts = set()
        capabilities = set()
        binds_artifacts = False
        for item in commands:
            operation = item.get('operation')
            parameters = item.get('parameters') or {}
            if operation_registry is not None and operation not in operation_registry:
                raise ValidationFailedError('operation is not registered', details={
                    'operation': operation})
            if operation == 'configuration.patch':
                patch = parameters.get('patch')
                if not isinstance(patch, dict) or not patch:
                    raise ValidationFailedError('plan patch must be non-empty')
                unknown = sorted(set(patch) - set(field_paths))
                if unknown:
                    raise ValidationFailedError('unknown facade field', details={'field_ids': unknown})
                for field_id in sorted(patch):
                    node = configuration
                    for part in field_paths[field_id].split('/'):
                        node = node.get(part) if isinstance(node, dict) else None
                    diff.append({'field_id': field_id, 'before': node, 'after': patch[field_id]})
            impacts.add(impact_of(operation_registry, operation).value)
            descriptor = operation_registry.get(operation) if operation_registry is not None else None
            if descriptor is not None:
                capabilities.update(descriptor.capabilities)
                binds_artifacts = binds_artifacts or bool(
                    descriptor.artifact_contract or descriptor.capabilities
                    or operation.startswith(('mesh.', 'quality.', 'workflow.', 'case.export.')))
            normalized.append({'operation': operation, 'parameters': parameters})
        confirmation_class = required_confirmation(operation_registry, normalized)
        canonical = {'case_id': session.case_id, 'session_epoch': session.session_epoch,
                     'authored_revision': session.authored_revision, 'commands': normalized}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True,
                                           separators=(',', ':'), default=str).encode()).hexdigest()
        plan = SlicePlan(
            session.case_id, session.session_epoch, session.authored_revision,
            session.artifact_sequence, normalized, diff, tuple(sorted(impacts)),
            digest=digest, confirmation_class=confirmation_class.value,
            required_capabilities=tuple(sorted(capabilities)),
            capability_digest=capability_digest, binds_artifacts=binds_artifacts)
        self._plans[plan.plan_id] = plan
        session._emit('plan.awaiting_confirmation', plan=plan.public())
        return plan

    def confirm(self, session, plan_id: str, digest: str, *, confirmed_by: str,
                ttl_seconds: int = 300, capability_digest: str = '') -> dict:
        plan = self.get(plan_id)
        self._assert_fresh(session, plan, capability_digest=capability_digest)
        if not secrets.compare_digest(plan.digest, digest):
            raise ValidationFailedError('plan digest does not match')
        if ttl_seconds <= 0:
            raise ValidationFailedError('authorization ttl must be positive')
        plan.state = PlanState.AUTHORIZED
        plan.token = secrets.token_urlsafe(32)
        plan.expires_at = _now() + timedelta(seconds=min(ttl_seconds, 900))
        session._emit('plan.authorized', plan_id=plan_id, confirmed_by=confirmed_by,
                      expires_at=plan.expires_at.isoformat())
        return {'plan_id': plan_id, 'token': plan.token, 'expires_at': plan.expires_at.isoformat()}

    def authorize_command(self, session, authorization: dict | None, *,
                          capability_digest: str = '') -> SlicePlan:
        if not authorization:
            raise AuthorizationRequiredError('agent mutation requires a confirmed plan')
        plan = self.get(authorization.get('plan_id', ''))
        if plan.state is PlanState.EXECUTING:
            if session.session_epoch != plan.session_epoch:
                plan.state = PlanState.STALE
                raise PlanStaleError('plan session epoch is stale')
            if plan.required_capabilities and capability_digest != plan.capability_digest:
                plan.state = PlanState.STALE
                raise PlanStaleError('plan capability fingerprint is stale')
        else:
            self._assert_fresh(session, plan, capability_digest=capability_digest)
        token = authorization.get('token') or ''
        if plan.token is None or not secrets.compare_digest(plan.token, token):
            raise AuthorizationRequiredError('plan execution token is invalid')
        if plan.expires_at is None or _now() >= plan.expires_at:
            plan.state = PlanState.EXPIRED
            plan.token = None
            session._emit('plan.expired', plan_id=plan.plan_id)
            raise AuthorizationRequiredError('plan execution token expired')
        return plan

    def get(self, plan_id: str) -> SlicePlan:
        try:
            return self._plans[plan_id]
        except KeyError as error:
            raise ValidationFailedError('plan was not found', details={'plan_id': plan_id}) from error

    def cancel(self, session, plan_id: str) -> dict:
        """Cancel a plan that has not yet finished executing."""
        plan = self.get(plan_id)
        if plan.state in (PlanState.SUCCEEDED, PlanState.REVERTED, PlanState.FAILED):
            raise ValidationFailedError('plan already finished; cannot cancel', details={
                'plan_id': plan_id, 'state': plan.state.value})
        plan.state = PlanState.CANCELLED
        plan.token = None
        session._emit('plan.cancelled', plan_id=plan_id)
        return plan.public()

    def mark_succeeded(self, session, plan: SlicePlan, *, transaction_id: str | None) -> None:
        plan.state = PlanState.SUCCEEDED
        plan.applied_revision = session.authored_revision
        plan.transaction_id = transaction_id
        session._emit('plan.succeeded', plan_id=plan.plan_id, applied_revision=plan.applied_revision,
                      transaction_id=transaction_id)

    def mark_reverted(self, session, plan: SlicePlan, *, transaction_id: str | None) -> None:
        plan.state = PlanState.REVERTED
        session._emit('plan.reverted', plan_id=plan.plan_id, transaction_id=transaction_id)

    def supersede_for_human_edit(self, session, *, actor_id: str, command_id: str) -> list[str]:
        stale = []
        for plan in self._plans.values():
            if (plan.case_id == session.case_id
                    and plan.state in (PlanState.AWAITING_CONFIRMATION, PlanState.AUTHORIZED)
                    and plan.authored_revision != session.authored_revision):
                plan.state = PlanState.STALE
                stale.append(plan.plan_id)
                session._emit('plan.superseded_by_user', plan_id=plan.plan_id,
                              actor_id=actor_id, command_id=command_id,
                              plan_revision=plan.authored_revision,
                              current_revision=session.authored_revision)
        return stale

    @staticmethod
    def _assert_fresh(session, plan: SlicePlan, *, capability_digest: str = '') -> None:
        if session.session_epoch != plan.session_epoch:
            plan.state = PlanState.STALE
            raise PlanStaleError('plan session epoch is stale', details={
                'plan_session_epoch': plan.session_epoch,
                'current_session_epoch': session.session_epoch})
        if session.authored_revision != plan.authored_revision:
            plan.state = PlanState.STALE
            raise PlanStaleError('plan authored revision is stale', details={
                'plan_revision': plan.authored_revision,
                'current_revision': session.authored_revision,
            })
        if plan.binds_artifacts and session.artifact_sequence != plan.artifact_sequence:
            plan.state = PlanState.STALE
            raise PlanStaleError('plan artifact fingerprint is stale', details={
                'plan_artifact_sequence': plan.artifact_sequence,
                'current_artifact_sequence': session.artifact_sequence})
        if plan.required_capabilities and capability_digest != plan.capability_digest:
            plan.state = PlanState.STALE
            raise PlanStaleError('plan capability fingerprint is stale', details={
                'required_capabilities': list(plan.required_capabilities)})
