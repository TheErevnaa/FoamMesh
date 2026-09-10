"""Structured errors returned by the facade, independent of HTTP or Qt."""
from __future__ import annotations


class FacadeError(RuntimeError):
    code = 'facade_error'

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': str(self), 'details': self.details}


class CaseNotFoundError(FacadeError):
    code = 'case_not_found'


class CaseLockedError(FacadeError):
    code = 'case_locked'


class ReadOnlySessionError(FacadeError):
    code = 'read_only_session'


class RevisionConflictError(FacadeError):
    code = 'revision_conflict'


class OperationNotFoundError(FacadeError):
    code = 'operation_not_found'


class ValidationFailedError(FacadeError):
    code = 'validation_failed'


class UndoNotAllowedError(FacadeError):
    code = 'undo_not_allowed'


class InteractionBusyError(FacadeError):
    code = 'interaction_busy'


class IdempotencyConflictError(FacadeError):
    code = 'idempotency_conflict'


class EventReplayGapError(FacadeError):
    code = 'event_replay_gap'


class SecurityPolicyError(FacadeError):
    code = 'security_policy_violation'


class AuthorizationRequiredError(FacadeError):
    code = 'authorization_required'


class PlanStaleError(FacadeError):
    code = 'plan_stale'


class CapabilityUnavailableError(FacadeError):
    """A required OpenFOAM/VTK utility is not resolvable in this environment."""
    code = 'capability_unavailable'


class PreconditionFailedError(FacadeError):
    """An operation precondition (mesh present, path valid, etc.) is unmet."""
    code = 'precondition_failed'


class PresentationUnavailableError(FacadeError):
    """A presentation command arrived with no attached desktop rendering session."""
    code = 'presentation_unavailable'
