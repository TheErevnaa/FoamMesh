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


def unavailable_utility_text(utility: str, reason: str = '', *,
                             kind: str = '') -> str:
    """The sentence that says which utility is missing and why (Plan 37 F2).

    MEASURED on the Gmsh UF20 walk (§8 check 4): the Quality refusal read
    "required utility is unavailable", naming neither checkMesh nor why, so
    there was nothing to fix. The runtime probe's reason ("checkMesh was not
    found in WSL distro OpenFOAM13Runtime") is the why; a reason that already
    names the utility is used as it stands.

    A reason that is only the utility's name (``FileNotFoundError('blockMesh')``
    stringifies to "blockMesh") says nothing, and used as it stood the whole
    message read "blockMesh." -- so it counts as no reason. ``kind`` ("pipeline
    utility") says what the utility was needed as.
    """
    name = str(utility or '').strip()
    subject = (f'The {kind} {name}' if kind else name) if name else (
        f'A required {kind or "utility"}')
    why = ' '.join(str(reason or '').split()).rstrip('.').strip()
    if name and why.startswith(name) and not any(
            character.isalnum() for character in why[len(name):]):
        why = ''
    if not why:
        return (f'{subject} is not available, and the runtime probe did not '
                'say why.')
    if name and why.startswith(name):
        return f'The {kind} {why}.' if kind else f'{why}.'
    return f'{subject} is not available: {why}.'


class PreconditionFailedError(FacadeError):
    """An operation precondition (mesh present, path valid, etc.) is unmet."""
    code = 'precondition_failed'


class PresentationUnavailableError(FacadeError):
    """A presentation command arrived with no attached desktop rendering session."""
    code = 'presentation_unavailable'


class CheckUnavailableError(FacadeError):
    """A check could not produce a result (Plan 35 CR2).

    Its worker did not start, died, was cancelled or stopped at its deadline.
    Nothing about the mesh changed: the check can be run again. ``details``
    carries ``outcome`` (the worker's), ``reason`` and ``retryable``.
    """
    code = 'check_unavailable'


class CheckOverBudgetError(CheckUnavailableError):
    """A check was refused before its parse, or stopped at its memory cap.

    ``details`` carries the estimate and the budget it was measured against,
    so the refusal says how much was needed and how much was free.
    """
    code = 'over_budget'
