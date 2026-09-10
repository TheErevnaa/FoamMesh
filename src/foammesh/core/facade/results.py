"""Serializable results for facade commands and queries."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RevisionSnapshot:
    session_epoch: int
    authored_revision: int
    artifact_sequence: int
    presentation_sequence: int

    def to_dict(self) -> dict:
        return {
            'session_epoch': self.session_epoch,
            'authored_revision': self.authored_revision,
            'artifact_sequence': self.artifact_sequence,
            'presentation_sequence': self.presentation_sequence,
        }


@dataclass(frozen=True)
class OperationResult:
    status: str
    operation: str
    revisions: RevisionSnapshot
    changed_fields: tuple[str, ...] = ()
    invalidated_outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    payload: dict = field(default_factory=dict)
    idempotent_replay: bool = False

    def to_dict(self) -> dict:
        return {
            'status': self.status, 'operation': self.operation,
            'revisions': self.revisions.to_dict(),
            'changed_fields': list(self.changed_fields),
            'invalidated_outputs': list(self.invalidated_outputs),
            'warnings': list(self.warnings), 'payload': self.payload,
            'idempotent_replay': self.idempotent_replay,
        }
