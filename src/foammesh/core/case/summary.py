"""Presentation-neutral summary data for externally managed meshes."""
from __future__ import annotations

from dataclasses import dataclass

from .model import CaseClassification, WorkflowMode, WorkflowResolution


@dataclass(frozen=True)
class ExternalMeshSummary:
    """The small, safe-to-display description of an external mesh case."""

    case_path: str
    poly_mesh_path: str | None
    origin: str
    artifact_state: str
    reason: str
    has_mesh: bool

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            'case_path': self.case_path,
            'poly_mesh_path': self.poly_mesh_path,
            'origin': self.origin,
            'artifact_state': self.artifact_state,
            'reason': self.reason,
            'has_mesh': self.has_mesh,
        }


def external_mesh_summary(classification: CaseClassification,
                          resolution: WorkflowResolution) -> ExternalMeshSummary:
    """Build display data only when authored navigation is intentionally suspended."""
    if resolution.workflow is not WorkflowMode.MESH_EXTERNAL:
        raise ValueError('an external mesh summary requires mesh_external workflow mode')

    return ExternalMeshSummary(
        case_path=str(classification.path),
        poly_mesh_path=(str(classification.poly_mesh_path)
                        if classification.poly_mesh_path is not None else None),
        origin=resolution.mesh_origin.value.replace('_', ' '),
        artifact_state=resolution.artifact_state.value,
        reason=resolution.reason,
        has_mesh=classification.has_mesh,
    )
