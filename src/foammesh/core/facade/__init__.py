"""AF1 persistent facade foundation (no transport or Qt adapter dependency)."""
from .application_session import ApplicationSession
from .commands import Actor, ActorKind, Command, CommandSource
from .domain_operations import DomainOperations
from .errors import (AuthorizationRequiredError, CapabilityUnavailableError, CaseLockedError,
                     CaseNotFoundError, EventReplayGapError, FacadeError,
                     IdempotencyConflictError, OperationNotFoundError, InteractionBusyError,
                     PlanStaleError, PreconditionFailedError, PresentationUnavailableError,
                     ReadOnlySessionError, RevisionConflictError, SecurityPolicyError,
                     UndoNotAllowedError, ValidationFailedError)
from .presentation import PresentationOperations, PresentationState
from .facade import AF1V_FIELDS, FIELD_STORAGE, FoamMeshFacade
from .field_adapters import EntityAdapter, build_entity_adapters
from .field_claims import FieldClaimLease, FieldClaimRegistry
from .fields import REGISTRY as FIELD_REGISTRY, FieldDescriptor, FieldRegistry, FieldType, build_field_registry
from .instrumentation import OwnerLoopMonitor
from .desktop_host import DesktopHost
from .operations import ExecutionMode, OperationDescriptor, OperationRegistry, build_operation_registry
from .plans import PlanState, PlanStore
from .policy import ConfirmationClass, ImpactClass
from .results import OperationResult, RevisionSnapshot
from .session import CaseSession, probe_locking

__all__ = [
    'AF1V_FIELDS', 'Actor', 'ActorKind', 'ApplicationSession', 'AuthorizationRequiredError',
    'CapabilityUnavailableError', 'CaseLockedError',
    'CaseNotFoundError', 'CaseSession', 'Command', 'CommandSource', 'ConfirmationClass',
    'DomainOperations', 'EntityAdapter', 'EventReplayGapError', 'DesktopHost', 'FIELD_REGISTRY',
    'FIELD_STORAGE', 'FacadeError', 'FieldClaimLease', 'FieldClaimRegistry', 'FieldDescriptor',
    'FieldRegistry', 'FieldType', 'ImpactClass', 'IdempotencyConflictError',
    'InteractionBusyError', 'FoamMeshFacade', 'ExecutionMode', 'OperationDescriptor', 'OperationNotFoundError',
    'OperationRegistry', 'OperationResult', 'OwnerLoopMonitor', 'PlanStaleError', 'PlanState',
    'PlanStore', 'PreconditionFailedError', 'PresentationOperations', 'PresentationState',
    'PresentationUnavailableError', 'ReadOnlySessionError', 'RevisionConflictError',
    'RevisionSnapshot', 'SecurityPolicyError', 'UndoNotAllowedError', 'ValidationFailedError',
    'build_entity_adapters', 'build_field_registry', 'build_operation_registry', 'probe_locking',
]
