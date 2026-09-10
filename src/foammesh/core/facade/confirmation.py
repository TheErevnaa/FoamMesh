"""AF6 confirmation policy: the engine chooses the confirmation class (§6.5).

A plan's required confirmation class is the most severe class implied by its
commands' impact classes, looked up in the operation registry. A client cannot
downgrade it; the command dispatcher enforces that agent mutations carry an
authorization whose plan actually satisfied that class.
"""
from __future__ import annotations

from .policy import ConfirmationClass, ImpactClass, confirmation_for

# Severity order (ascending). ``max`` over this order yields the class a plan
# must be confirmed at.
_SEVERITY = {
    ConfirmationClass.NONE: 0,
    ConfirmationClass.BATCH: 1,
    ConfirmationClass.DESTINATION: 2,
    ConfirmationClass.ESTIMATE: 3,
    ConfirmationClass.RECOVERY_POINT: 4,
    ConfirmationClass.EXPLICIT: 5,
}


def impact_of(operation_registry, operation: str) -> ImpactClass:
    descriptor = operation_registry.get(operation) if operation_registry else None
    if descriptor is not None:
        return descriptor.impact
    # Conservative default for an unregistered operation: treat as a reversible
    # edit so it still requires confirmation from an agent.
    return ImpactClass.REVERSIBLE_EDIT


def required_confirmation(operation_registry, commands) -> ConfirmationClass:
    """The most severe confirmation class across a plan's commands."""
    result = ConfirmationClass.NONE
    for item in commands:
        operation = item.get('operation') if isinstance(item, dict) else item
        candidate = confirmation_for(impact_of(operation_registry, operation))
        if _SEVERITY[candidate] > _SEVERITY[result]:
            result = candidate
    return result


def is_mutation(operation_registry, operation: str) -> bool:
    """True when the operation changes case state (anything but a pure read)."""
    return impact_of(operation_registry, operation) is not ImpactClass.READ


def requires_authorization(operation_registry, operation: str) -> bool:
    """Agent commands need a confirmed plan unless the operation is a read."""
    return is_mutation(operation_registry, operation)
