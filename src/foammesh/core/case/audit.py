"""Read-only case health summary for support and validation workflows."""
from dataclasses import dataclass
from pathlib import Path

from foammesh.core.mesh import MeshRecoveryService
from foammesh.core.quality import MeshCheckService

from .model import classify_case, fingerprint_poly_mesh, load_case_metadata, resolve_workflow


@dataclass(frozen=True)
class CaseAudit:
    kind: str
    workflow: str
    mesh_present: bool
    recovery_points: int
    latest_mesh_ok: bool | None

    def to_dict(self) -> dict[str, str | bool | int | None]:
        return {
            'kind': self.kind, 'workflow': self.workflow, 'mesh_present': self.mesh_present,
            'recovery_points': self.recovery_points, 'latest_mesh_ok': self.latest_mesh_ok,
        }

    def to_text(self) -> str:
        return (f'Case kind: {self.kind}\nWorkflow: {self.workflow}\nMesh present: {self.mesh_present}\n'
                f'Recovery points: {self.recovery_points}\nLatest mesh check: {self.latest_mesh_ok}')


def audit_case(case_path: str | Path) -> CaseAudit:
    classification = classify_case(case_path)
    fingerprint = fingerprint_poly_mesh(classification.poly_mesh_path) if classification.poly_mesh_path else None
    metadata = load_case_metadata(classification.path) if classification.metadata_path else None
    workflow = resolve_workflow(metadata, fingerprint)
    latest = MeshCheckService.load_latest(classification.path)
    return CaseAudit(classification.kind.value, workflow.workflow.value, classification.has_mesh,
                     len(MeshRecoveryService().list_points(classification.path)),
                     latest.mesh_ok if latest else None)
