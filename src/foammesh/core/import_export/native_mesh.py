"""Safe native OpenFOAM polyMesh replacement/import."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from foammesh.core.case import (
    CaseMetadata, CaseMetadataError, MeshOrigin, classify_case,
    copy_case_directory, fingerprint_poly_mesh, load_case_metadata,
    record_artifact_event, save_case_metadata,
)
from foammesh.core.mesh import MeshRecoveryPoint, MeshRecoveryService


@dataclass(frozen=True)
class NativeMeshImportResult:
    source_case: Path
    target_case: Path
    fingerprint: str
    recovery: MeshRecoveryPoint | None

    def to_dict(self) -> dict:
        return {'source_case': str(self.source_case), 'target_case': str(self.target_case),
                'fingerprint': self.fingerprint,
                'recovery_id': self.recovery.recovery_id if self.recovery else None}


class NativeMeshImportService:
    """Copy a source polyMesh into a target case without corrupting either case."""

    def __init__(self, recovery: MeshRecoveryService | None = None):
        self._recovery = recovery or MeshRecoveryService()

    def import_into_copy(self, source_case: str | Path, current_case: str | Path,
                         destination: str | Path) -> NativeMeshImportResult:
        """§13.1 copy choice: import into a copy of the open case, leaving it untouched."""
        copied = copy_case_directory(current_case, destination)
        try:
            return self.import_from_case(source_case, copied.destination)
        except Exception:
            shutil.rmtree(copied.destination, ignore_errors=True)
            raise

    def import_from_case(self, source_case: str | Path, target_case: str | Path) -> NativeMeshImportResult:
        source = Path(source_case).resolve()
        target = Path(target_case).resolve()
        classification = classify_case(source)
        if classification.poly_mesh_path is None:
            raise ValueError('source case has no complete constant/polyMesh')
        if source == target:
            raise ValueError('source and target cases must be different')

        target_mesh = target / 'constant' / 'polyMesh'
        target_mesh.parent.mkdir(parents=True, exist_ok=True)
        before = fingerprint_poly_mesh(target_mesh).digest if target_mesh.is_dir() else None
        recovery = self._recovery.snapshot(target, operation='native_mesh_import') if target_mesh.is_dir() else None
        staging = target_mesh.with_name(f'.polyMesh.import-{uuid4().hex}')
        displaced = target_mesh.with_name(f'.polyMesh.displaced-{uuid4().hex}')
        try:
            shutil.copytree(classification.poly_mesh_path, staging, copy_function=shutil.copy2)
            fingerprint = fingerprint_poly_mesh(staging)
            if target_mesh.exists():
                os.replace(target_mesh, displaced)
            os.replace(staging, target_mesh)
            provenance = {'source_case': str(source), 'importer': 'native_polyMesh'}
            try:
                previous = load_case_metadata(target)
            except CaseMetadataError:
                metadata = CaseMetadata.external_mesh(
                    fingerprint, origin=MeshOrigin.IMPORTED, provenance=provenance)
            else:
                metadata = previous.with_external_mesh(
                    fingerprint, origin=MeshOrigin.IMPORTED, provenance=provenance)
            save_case_metadata(target, metadata)
            record_artifact_event(
                target, operation='import:native_polyMesh', status='applied',
                command=('native_polyMesh_import', str(source)),
                before_fingerprint=before, after_fingerprint=fingerprint.digest,
                recovery_id=recovery.recovery_id if recovery else None,
                recovery_status='available' if recovery else 'not_required',
                details={'source_case': str(source)})
            shutil.rmtree(displaced, ignore_errors=True)
            return NativeMeshImportResult(source, target, fingerprint.digest, recovery)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if displaced.exists():
                shutil.rmtree(target_mesh, ignore_errors=True)
                os.replace(displaced, target_mesh)
            elif recovery is not None:
                self._recovery.restore(target, recovery)
            raise
