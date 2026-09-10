"""Explicit authored/external workflow-mode transitions."""
from __future__ import annotations

from pathlib import Path

from foammesh.core.mesh.recovery import MeshRecoveryService

from .model import (
    ArtifactState, MeshOrigin, WorkflowMode, classify_case,
    fingerprint_poly_mesh, load_case_metadata, save_case_metadata,
)
from .artifact_history import record_artifact_event


class WorkflowTransitionService:
    def __init__(self, recovery: MeshRecoveryService | None = None):
        self._recovery = recovery or MeshRecoveryService()

    def start_authored(self, case_path: str | Path):
        case = Path(case_path)
        metadata = load_case_metadata(case)
        if metadata.workflow is not WorkflowMode.MESH_EXTERNAL:
            raise ValueError('Start Meshing Workflow requires mesh_external mode')
        classification = classify_case(case)
        if classification.poly_mesh_path is None:
            raise ValueError('no external polyMesh is available to retain')
        recovery = self._recovery.snapshot(case, operation='start-authored-workflow')
        provenance = dict(metadata.provenance)
        provenance.update({
            'external_recovery_id': recovery.recovery_id,
            'external_origin': metadata.mesh_origin.value,
            'workflow_transition': 'mesh_external_to_authored',
        })
        updated = metadata.evolve(
            workflow=WorkflowMode.AUTHORED,
            authored_workflow_suspended=False,
            artifact_state=ArtifactState.CURRENT,
            provenance=provenance,
        )
        save_case_metadata(case, updated)
        record_artifact_event(
            case, operation='workflow:start_authored', status='applied',
            before_fingerprint=metadata.mesh_fingerprint.digest if metadata.mesh_fingerprint else None,
            after_fingerprint=metadata.mesh_fingerprint.digest if metadata.mesh_fingerprint else None,
            recovery_id=recovery.recovery_id, recovery_status='available')
        return updated

    def can_return_to_external(self, case_path: str | Path) -> bool:
        metadata = load_case_metadata(case_path)
        recovery_id = metadata.provenance.get('external_recovery_id')
        return bool(recovery_id and any(
            point.recovery_id == recovery_id for point in self._recovery.list_points(case_path)))

    def return_to_external(self, case_path: str | Path):
        case = Path(case_path)
        metadata = load_case_metadata(case)
        recovery_id = metadata.provenance.get('external_recovery_id')
        point = next((item for item in self._recovery.list_points(case)
                      if item.recovery_id == recovery_id), None)
        if point is None:
            raise ValueError('the retained external mesh recovery copy is unavailable')
        self._recovery.restore(case, point)
        origin_value = metadata.provenance.get('external_origin', MeshOrigin.OPENED_NATIVE.value)
        try:
            origin = MeshOrigin(origin_value)
        except ValueError:
            origin = MeshOrigin.OPENED_NATIVE
        updated = metadata.evolve(
            workflow=WorkflowMode.MESH_EXTERNAL,
            mesh_origin=origin,
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint_poly_mesh(case / 'constant' / 'polyMesh'),
            authored_workflow_suspended=True,
            provenance={**metadata.provenance, 'workflow_transition': 'returned_to_external'},
        )
        save_case_metadata(case, updated)
        record_artifact_event(
            case, operation='artifact:restore_external_mesh', status='restored',
            before_fingerprint=metadata.mesh_fingerprint.digest if metadata.mesh_fingerprint else None,
            after_fingerprint=updated.mesh_fingerprint.digest if updated.mesh_fingerprint else None,
            recovery_id=point.recovery_id, recovery_status='restored')
        return updated
