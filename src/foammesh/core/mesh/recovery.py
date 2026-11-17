"""Recoverable polyMesh snapshots for external utility mutations."""
from __future__ import annotations

import json
import os
import shutil
import time
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


#: DP-111. A snappy castellation on the parallel path lost a stage that had
#: already meshed to `[WinError 5] Access is denied` swapping this journal's
#: `manifest.json.tmp` over `manifest.json`. The call was already `os.replace`,
#: which does overwrite an existing destination on Windows -- so the denial is
#: a sharing violation, not a rename that refused to replace: something held
#: one of the two paths open for the instant the swap needed them. Those holds
#: are short. Give the swap more than one attempt rather than letting the first
#: one decide the fate of a mesh that is already written to disk.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_SECONDS = 0.05


def _replace_with_retry(source: Path, destination: Path) -> None:
    """``os.replace``, with a short bounded retry for a transient denial."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def _write_json_durably(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as output:
        json.dump(payload, output, indent=2, sort_keys=True)
        output.write('\n'); output.flush(); os.fsync(output.fileno())
    _replace_with_retry(temporary, path)


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
    """Result of an explicit user-facing Restore previous mesh operation."""
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
            _replace_with_retry(temporary, manifest)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return MeshRecoveryPoint(recovery_id, root, backup, manifest)

    def snapshot_reference(self, case_path: str | Path, *, operation: str,
                           revision: str, stage: str) -> MeshRecoveryPoint | None:
        """A recovery point that reads a kept stage snapshot (Plan 50 IP16).

        DP-6300. The mesh a stage starts from is, in a staged run, the
        previous stage's kept snapshot: an immutable, hashed copy under
        ``foammesh/stages``. Copying and hashing it again into
        ``foammesh/recovery`` kept the same mesh twice. This point names that
        snapshot instead -- no copy, no hash -- and :meth:`restore` reads it
        from there (verified against the snapshot's own manifest first).
        ``None`` when the snapshot is not a whole, gathered mesh.
        """
        from foammesh.core.jobs import stage_snapshots
        case = Path(case_path)
        kept = stage_snapshots.read_manifest(case, stage, revision)
        mesh = stage_snapshots.stage_mesh_path(case, stage, revision)
        if kept is None or mesh is None or not kept.get('digest'):
            return None
        recovery_id = uuid4().hex
        root = case / 'foammesh' / 'recovery' / recovery_id
        root.mkdir(parents=True)
        manifest = root / 'manifest.json'
        try:
            _write_json_durably(manifest, {
                'schema_version': 1, 'operation': operation, 'state': 'prepared',
                'created_at': _utc_now(), 'mesh_path': 'constant/polyMesh',
                'source': {'kind': 'stage_snapshot', 'revision': str(revision),
                           'stage': str(stage), 'digest': kept['digest']}})
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return MeshRecoveryPoint(recovery_id, root, mesh, manifest)

    @staticmethod
    def _source(manifest: Path) -> dict | None:
        """The kept stage a reference point reads, or ``None`` for a copy."""
        try:
            payload = json.loads(manifest.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        source = payload.get('source') if isinstance(payload, dict) else None
        return source if isinstance(source, dict) and source.get(
            'kind') == 'stage_snapshot' else None

    @classmethod
    def _point_at(cls, case: Path, root: Path) -> MeshRecoveryPoint | None:
        manifest = root / 'manifest.json'
        if not manifest.is_file():
            return None
        source = cls._source(manifest)
        if source is None:
            backup = root / 'polyMesh'
            return (MeshRecoveryPoint(root.name, root, backup, manifest)
                    if backup.is_dir() else None)
        from foammesh.core.jobs import stage_snapshots
        mesh = (stage_snapshots.stages_root(case) / str(source.get('revision'))
                / str(source.get('stage')) / 'constant' / 'polyMesh')
        return MeshRecoveryPoint(root.name, root, mesh, manifest)

    @staticmethod
    def _reference_intact(case: Path, source: dict) -> bool:
        """Cheap: the kept stage is still the snapshot the point names."""
        from foammesh.core.jobs import stage_snapshots
        kept = stage_snapshots.read_manifest(
            case, str(source.get('stage')), str(source.get('revision')))
        return bool(kept and kept.get('digest') == source.get('digest')
                    and stage_snapshots.stage_mesh_path(
                        case, str(source.get('stage')),
                        str(source.get('revision'))) is not None)

    @staticmethod
    def _case_of(point: MeshRecoveryPoint) -> Path:
        # <case>/foammesh/recovery/<id>
        return point.root.parent.parent.parent

    def referenced_stages(self, case_path) -> set[tuple[str, str]]:
        """``(revision, stage)`` of every kept stage a recovery point reads."""
        root = Path(case_path) / 'foammesh' / 'recovery'
        pairs = set()
        if not root.is_dir():
            return pairs
        for item in root.iterdir():
            source = self._source(item / 'manifest.json')
            if source is not None:
                pairs.add((str(source.get('revision')), str(source.get('stage'))))
        return pairs

    def prune(self, case_path: str | Path, *, keep=()) -> list[str]:
        """Remove recovery points no one can use any more; the ids removed.

        Plan 50 IP16 (DP-6301). Points were never removed. Kept: the newest
        ``available`` point (what *Restore previous mesh* restores), every
        point an open run record names (the recovery gate restores it after
        a crash), and *keep*. Everything else -- older available points,
        restored ones, prepared points of runs that are long resolved -- is
        removed. Nothing is removed while a restore is half done or while an
        open run record cannot be read (it may name any point).
        """
        case = Path(case_path)
        root = case / 'foammesh' / 'recovery'
        if not root.is_dir() or self.restore_in_progress(case):
            return []
        from foammesh.core.jobs.run_records import RunRecordStore
        wanted = {str(item) for item in keep if item}
        for record in RunRecordStore(case).open_records():
            if record.get('unreadable'):
                return []
            if record.get('recovery_id'):
                wanted.add(str(record['recovery_id']))
        newest, newest_at = None, ''
        for item in root.iterdir():
            payload = {}
            try:
                payload = json.loads((item / 'manifest.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                payload = {}
            if isinstance(payload, dict) and payload.get('state') == 'available':
                created = str(payload.get('created_at') or '')
                if newest is None or created >= newest_at:
                    newest, newest_at = item.name, created
        if newest:
            wanted.add(newest)
        removed = []
        for item in sorted(root.iterdir()):
            if item.name in wanted or not item.is_dir():
                continue
            shutil.rmtree(item, ignore_errors=True)
            if not item.exists():
                removed.append(item.name)
        return removed

    #: Plan 35 CR5 step 7. The restore stages here and swaps in by rename.
    STAGING_NAME = 'polyMesh.restoring'
    #: Written beside the staging directory once its copy is complete and
    #: verified; never inside it, so the restored mesh's fingerprint is the
    #: snapshot's.
    STAGING_MARKER = '.polyMesh.restoring.json'

    def restore(self, case_path: str | Path, point: MeshRecoveryPoint):
        """Put the snapshot back, quietly and resumably.

        Plan 35 CR5 step 7. The copy goes to ``constant/polyMesh.restoring``;
        a marker beside it says the copy is complete and verified; then the
        half-written mesh is moved aside and the staged one renamed in. A
        crash at any point leaves either the old directory or the restored
        one, plus enough on disk for the next call to finish the swap rather
        than start over: a complete stage is reused, a partial one redone.
        """
        case = Path(case_path)
        mesh = case / 'constant' / 'polyMesh'
        staging = mesh.with_name(self.STAGING_NAME)
        marker = mesh.with_name(self.STAGING_MARKER)
        displaced = mesh.with_name(f'.polyMesh.displaced-{point.recovery_id}')
        if not point.mesh_backup.is_dir():
            raise FileNotFoundError(f'recovery mesh is missing: {point.mesh_backup}')
        if not self.verify(point):
            raise ValueError(f'recovery mesh failed fingerprint verification: {point.recovery_id}')
        expected = self._backup_fingerprint(point)
        staged = self._staged_for(marker, point, expected)
        if staged and not staging.is_dir():
            # The swap happened; only the tidying was interrupted.
            if self._fingerprint_or_none(mesh) == expected:
                shutil.rmtree(displaced, ignore_errors=True)
                marker.unlink(missing_ok=True)
                self._mark(point.manifest, 'restored')
                return
            staged = False
        if not staged:
            shutil.rmtree(staging, ignore_errors=True)
            marker.unlink(missing_ok=True)
            try:
                shutil.copytree(point.mesh_backup, staging, copy_function=shutil.copy2)
                if self._fingerprint_or_none(staging) != expected:
                    raise ValueError(
                        f'the staged recovery mesh does not match its snapshot: {point.recovery_id}')
                _write_json_durably(marker, {
                    'recovery_id': point.recovery_id, 'fingerprint': expected,
                    'staged_at': _utc_now()})
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                marker.unlink(missing_ok=True)
                raise
        if mesh.exists():
            shutil.rmtree(displaced, ignore_errors=True)
            _replace_with_retry(mesh, displaced)
        _replace_with_retry(staging, mesh)
        shutil.rmtree(displaced, ignore_errors=True)
        marker.unlink(missing_ok=True)
        self._mark(point.manifest, 'restored')

    @staticmethod
    def restore_in_progress(case_path: str | Path) -> bool:
        """A restore left something behind that the next one must finish."""
        constant = Path(case_path) / 'constant'
        return ((constant / MeshRecoveryService.STAGING_NAME).exists()
                or (constant / MeshRecoveryService.STAGING_MARKER).exists())

    @classmethod
    def _backup_fingerprint(cls, point: MeshRecoveryPoint) -> str:
        try:
            payload = json.loads(point.manifest.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return ''
        if not isinstance(payload, dict):
            return ''
        if cls._source(point.manifest) is not None:
            # DP-6300. A reference point hashed nothing when it was made;
            # the kept stage it reads is fingerprinted when it is restored.
            return cls._fingerprint_or_none(point.mesh_backup) or ''
        return str(payload.get('backup_fingerprint') or '')

    @staticmethod
    def _fingerprint_or_none(path: Path) -> str | None:
        if not path.is_dir():
            return None
        try:
            return fingerprint_poly_mesh(path).digest
        except (OSError, ValueError):
            return None

    @staticmethod
    def _staged_for(marker: Path, point: MeshRecoveryPoint, expected: str) -> bool:
        try:
            payload = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return (isinstance(payload, dict)
                and payload.get('recovery_id') == point.recovery_id
                and bool(expected) and payload.get('fingerprint') == expected)

    def list_points(self, case_path: str | Path) -> tuple[MeshRecoveryPoint, ...]:
        root = Path(case_path) / 'foammesh' / 'recovery'
        if not root.is_dir():
            return ()
        points = []
        for item in sorted(root.iterdir()):
            point = self._point_at(Path(case_path), item)
            if point is not None and point.mesh_backup.is_dir():
                points.append(point)
        return tuple(points)

    @classmethod
    def point_for(cls, case_path: str | Path, recovery_id: str) -> MeshRecoveryPoint | None:
        """The recovery point a run record names, if it is still on disk."""
        if not recovery_id or '/' in recovery_id or '\\' in recovery_id:
            return None
        root = Path(case_path) / 'foammesh' / 'recovery' / recovery_id
        point = cls._point_at(Path(case_path), root)
        return point if point is not None and point.mesh_backup.is_dir() else None

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
        moment the user actually triggers Restore previous mesh.
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
        """Explicit §5.3.5 Restore previous mesh artifact operation.

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
        source = self._source(point.manifest)
        if source is not None:
            # A reference point is as good as the kept stage it reads: still
            # the same snapshot, and every file of it matching its hash.
            from foammesh.core.jobs import stage_snapshots
            case = self._case_of(point)
            if not self._reference_intact(case, source):
                return False
            try:
                return bool(stage_snapshots.verify(
                    case, str(source['stage']), str(source['revision'])).get('ok'))
            except (OSError, ValueError, KeyError, TypeError):
                return False
        try:
            payload = json.loads(point.manifest.read_text(encoding='utf-8'))
            expected = payload.get('backup_fingerprint')
            return bool(expected and fingerprint_poly_mesh(point.mesh_backup).digest == expected)
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    def mark_available(self, point: MeshRecoveryPoint):
        source = self._source(point.manifest)
        if source is not None:
            # DP-6300. The kept stage was hashed and verified when it was
            # captured, and is verified again before it is restored; reading
            # it whole here once more would be the third hash of one mesh.
            if not self._reference_intact(self._case_of(point), source):
                raise ValueError(
                    f'the kept stage this recovery point reads is gone: {point.recovery_id}')
            self._mark(point.manifest, 'available')
            return
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
            _replace_with_retry(temporary, manifest)
        finally:
            if temporary.exists():
                temporary.unlink()
