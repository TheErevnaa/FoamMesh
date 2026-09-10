"""Confirmation policy and impact classes (§4.4, §6.5) for the operation registry.

Every operation declares an impact class; the engine derives the confirmation
class from it. A client cannot downgrade the class — this is the internal
enforcement side of the external-chatbot confirmation protocol.
"""
from __future__ import annotations

from enum import Enum


class ImpactClass(str, Enum):
    READ = 'read'
    REVERSIBLE_EDIT = 'reversible_edit'
    FILE_PRODUCING = 'file_producing'
    EXPENSIVE_JOB = 'expensive_job'
    MESH_MUTATION = 'mesh_mutation'
    DESTRUCTIVE = 'destructive'


class ConfirmationClass(str, Enum):
    NONE = 'none'
    BATCH = 'batch'                 # reversible agent-plan edits, batch confirmed
    DESTINATION = 'destination'     # confirm destination/overwrite for file output
    ESTIMATE = 'estimate'           # confirm plan + cost for an expensive job
    RECOVERY_POINT = 'recovery_point'  # confirm with a recovery point for a mutation
    EXPLICIT = 'explicit'           # always explicit for destructive work


_CONFIRMATION_BY_IMPACT = {
    ImpactClass.READ: ConfirmationClass.NONE,
    ImpactClass.REVERSIBLE_EDIT: ConfirmationClass.BATCH,
    ImpactClass.FILE_PRODUCING: ConfirmationClass.DESTINATION,
    ImpactClass.EXPENSIVE_JOB: ConfirmationClass.ESTIMATE,
    ImpactClass.MESH_MUTATION: ConfirmationClass.RECOVERY_POINT,
    ImpactClass.DESTRUCTIVE: ConfirmationClass.EXPLICIT,
}


def confirmation_for(impact: ImpactClass) -> ConfirmationClass:
    return _CONFIRMATION_BY_IMPACT[impact]


def requires_confirmation(impact: ImpactClass) -> bool:
    return confirmation_for(impact) is not ConfirmationClass.NONE
