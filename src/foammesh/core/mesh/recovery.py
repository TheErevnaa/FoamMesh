"""Recoverable polyMesh snapshots for external utility mutations."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from foammesh.core.case import (
    CaseMetadataError, fingerprint_poly_mesh, load_case_metadata,
    record_artifact_event, save_case_metadata,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass(frozen=True)
class MeshRecoveryPoint:
    recovery_id: str
    root: Path
    mesh_backup: Path
    manifest: Path

    def to_dict(self) -> dict[str, str]:
        return {'recovery_id': self.recovery_id, 'root': str(self.root),
                'mesh_backup': str(self.mesh_backup), 'manifest': str(self.manifest)}


@dataclass(frozen=True)
class MeshRestoreOutcome:
    """Result of an explicit user-facing Restore Previous Mesh operation."""
    point: MeshRecoveryPoint
    operation: str
    created_at: str
    before_fingerprint: str
    after_fingerprint: str

    def to_dict(self) -> dict:
        return {'recovery': self.point.to_dict(), 'operation': self.operation,
                'created_at': self.created_at,
                'before_fingerprint': self.before_fingerprint,
                'after_fingerprint': self.after_fingerprint}


class MeshRecoveryService:
    """Snapshot and restore only FoamMesh's explicitly selected mesh artifact."""

    def snapshot(self, case_path: str | Path, *, operation: str) -> MeshRecoveryPoint:
        case = Path(case_path)
        mesh = case / 'constant' / 'polyMesh'
        if not mesh.is_dir():
            raise ValueError('no constant/polyMesh is available for recovery')
        recovery_id = uuid4().hex
        root = case / 'foammesh' / 'recovery' / recovery_id
        backup = root / 'polyMesh'
        root.mkdir(parents=True)
        try:
            shutil.copytree(mesh, backup, copy_function=shutil.copy2)
            manifest = root / 'manifest.json'
            payload = {
                'schema_version': 1, 'operation': operation, 'state': 'prepared',
                'created_at': _utc_now(),
                'mesh_path': 'constant/polyMesh',
                'backup_fingerprint': fingerprint_poly_mesh(backup).digest,
            }
            temporary = manifest.with_suffix('.json.tmp')
            with temporary.open('w', encoding='utf-8', newline='\n') as output:
                json.dump(payload, output, indent=2, sort_keys=True)
                output.write('\n'); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, manifest)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return MeshRecoveryPoint(recovery_id, root, backup, manifest)

    def restore(self, case_path: str | Path, point: MeshRecoveryPoint):
        case = Path(case_path)
        mesh = case / 'constant' / 'polyMesh'
        staging = mesh.with_name(f'.polyMesh.restore-{point.recovery_id}')
        if not point.mesh_backup.is_dir():
            raise FileNotFoundError(f'recovery mesh is missing: {point.mesh_backup}')
        if not self.verify(point):
            raise ValueError(f'recovery mesh failed fingerprint verification: {point.recovery_id}')
        try:
            shutil.copytree(point.mesh_backup, staging, copy_function=shutil.copy2)
            displaced = mesh.with_name(f'.polyMesh.displaced-{point.recovery_id}')
            if mesh.exists():
                os.replace(mesh, displaced)
            os.replace(staging, mesh)
            shutil.rmtree(displaced, ignore_errors=True)
            self._mark(point.manifest, 'restored')
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def list_points(self, case_path: str | Path) -> tuple[MeshRecoveryPoint, ...]:
        root = Path(case_path) / 'foammesh' / 'recovery'
        if not root.is_dir():
            return ()
        points = []
        for item in sorted(root.iterdir()):
            manifest = item / 'manifest.json'
            backup = item / 'polyMesh'
            if manifest.is_file() and backup.is_dir():
                points.append(MeshRecoveryPoint(item.name, item, backup, manifest))
        return tuple(points)

    def manifest_payload(self, point: MeshRecoveryPoint) -> dict:
        """Return the manifest content, or an empty dict when unreadable."""
        try:
            payload = json.loads(point.manifest.read_text(encoding='utf-8'))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def has_available(self, case_path: str | Path) -> bool:
        """Cheap probe for the action policy: any restorable point exists?

        Only reads manifests; full fingerprint verification is deferred to the
        moment the user actually triggers Restore Previous Mesh.
        """
        return any(self.manifest_payload(point).get('state') == 'available'
                   for point in self.list_points(case_path))

    def latest_available(self, case_path: str | Path) -> MeshRecoveryPoint | None:
        """Return the most recent verified point from a successful mutation.

        Points are ordered by their manifest ``created_at`` (falling back to
        manifest mtime for pre-timestamp manifests); recovery ids are random,
        so directory order is meaningless.
        """
        candidates = []
        for point in self.list_points(case_path):
            payload = self.manifest_payload(point)
            if payload.get('state') != 'available':
                continue
            try:
                fallback = datetime.fromtimestamp(
                    point.manifest.stat().st_mtime, timezone.utc).isoformat()
            except OSError:
                fallback = ''
            candidates.append((payload.get('created_at') or fallback, point))
        for _created, point in sorted(candidates, key=lambda item: item[0], reverse=True):
            if self.verify(point):
                return point
        return None

    def restore_previous(self, case_path: str | Path) -> MeshRestoreOutcome:
        """Explicit §5.3.5 Restore Previous Mesh artifact operation.

        Restores the newest verified recovery point left behind by a
        successful transform/repair, refreshes the sidecar mesh fingerprint so
        provenance still agrees with the mesh on disk, and records its own
        artifact-history event.  It is not ProjectState undo.
        """
        case = Path(case_path).resolve()
        point = self.latest_available(case)
        if point is None:
            raise ValueError('no verified previous-mesh recovery point is available')
        payload = self.manifest_payload(point)
        try:
            before = fingerprint_poly_mesh(case / 'constant' / 'polyMesh').digest
        except (OSError, ValueError):
            before = 'unavailable'
        self.restore(case, point)
        after = fingerprint_poly_mesh(case / 'constant' / 'polyMesh')
        try:
            metadata = load_case_metadata(case)
        except CaseMetadataError:
            metadata = None  # raw native cases have no sidecar to refresh
        if metadata is not None:
            provenance = dict(metadata.provenance)
            provenance['last_mesh_mutation'] = f'restore:{payload.get("operation", "unknown")}'
            save_case_metadata(case, metadata.evolve(
                mesh_fingerprint=after, provenance=provenance))
        record_artifact_event(
            case, operation='restore_previous_mesh', status='applied',
            before_fingerprint=before, after_fingerprint=after.digest,
            recovery_id=point.recovery_id, recovery_status='restored',
            details={'restored_operation': payload.get('operation'),
                     'recovery_created_at': payload.get('created_at')})
        return MeshRestoreOutcome(
            point=point, operation=str(payload.get('operation', 'unknown')),
            created_at=str(payload.get('created_at', '')),
            before_fingerprint=before, after_fingerprint=after.digest)

    def verify(self, point: MeshRecoveryPoint) -> bool:
        try:
            payload = json.loads(point.manifest.read_text(encoding='utf-8'))
            expected = payload.get('backup_fingerprint')
            return bool(expected and fingerprint_poly_mesh(point.mesh_backup).digest == expected)
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def mark_available(self, point: MeshRecoveryPoint):
        if not self.verify(point):
            raise ValueError(f'recovery mesh failed fingerprint verification: {point.recovery_id}')
        self._mark(point.manifest, 'available')

    @staticmethod
    def _mark(manifest: Path, state: str):
        payload = json.loads(manifest.read_text(encoding='utf-8'))
        payload['state'] = state
        temporary = manifest.with_suffix('.json.tmp')
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(temporary, manifest)
        finally:
            if temporary.exists():
                temporary.unlink()
