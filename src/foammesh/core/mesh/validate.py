"""Canonical mesh topology and geometry validation."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from .connectivity import boundary_keys, build_face_topology, signed_cell_volumes
from .model import CanonicalMesh


@dataclass(frozen=True)
class MeshIssue:
    code: str
    severity: str
    count: int
    message: str
    entity_ids: tuple[int, ...] = ()

    def to_dict(self):
        return {
            'code': self.code, 'severity': self.severity, 'count': self.count,
            'message': self.message, 'entity_ids': list(self.entity_ids),
        }


@dataclass(frozen=True)
class MeshValidationReport:
    valid: bool
    point_count: int
    cell_count: int
    boundary_face_count: int
    minimum_signed_volume: float | None
    issues: tuple[MeshIssue, ...]

    def to_dict(self):
        return {
            'schema_version': 1, 'valid': self.valid,
            'point_count': self.point_count, 'cell_count': self.cell_count,
            'boundary_face_count': self.boundary_face_count,
            'minimum_signed_volume': self.minimum_signed_volume,
            'issues': [issue.to_dict() for issue in self.issues],
        }


def validate_mesh(mesh: CanonicalMesh, *, volume_tolerance: float = 1.0e-18,
                  require_complete_boundary: bool = True) -> MeshValidationReport:
    issues = []
    all_connectivity = [block.connectivity for block in mesh.cell_blocks]
    all_connectivity.extend(block.connectivity for block in mesh.boundary_blocks)
    for index, connectivity in enumerate(all_connectivity):
        invalid = np.logical_or(connectivity < 0, connectivity >= mesh.point_count)
        if invalid.any():
            issues.append(MeshIssue(
                'node_index_out_of_range', 'error', int(invalid.sum()),
                f'connectivity block {index} references invalid point indices'))
    if any(issue.code == 'node_index_out_of_range' for issue in issues):
        return _report(mesh, issues, None)

    referenced = np.unique(np.concatenate([value.ravel() for value in all_connectivity]))
    unreferenced = np.setdiff1d(np.arange(mesh.point_count), referenced, assume_unique=True)
    if unreferenced.size:
        issues.append(MeshIssue(
            'unreferenced_nodes', 'error', int(unreferenced.size),
            'points are not referenced by any cell or boundary face',
            tuple(map(int, unreferenced[:100]))))
    duplicate_count, intentional_duplicates = _duplicate_point_counts(mesh)
    if duplicate_count:
        issues.append(MeshIssue(
            'duplicate_nodes', 'error', duplicate_count,
            'exact duplicate point coordinates are present'))
    if intentional_duplicates:
        issues.append(MeshIssue(
            'intentional_non_conformal_nodes', 'info',
            intentional_duplicates,
            'coincident coordinates retain separate point identities on '
            'declared non-conformal interface patches'))

    volumes = [signed_cell_volumes(mesh.points, block) for block in mesh.cell_blocks]
    all_volumes = np.concatenate(volumes)
    zero = np.flatnonzero(np.abs(all_volumes) <= volume_tolerance)
    negative = np.flatnonzero(all_volumes < -volume_tolerance)
    if zero.size:
        issues.append(MeshIssue('zero_volume_cells', 'error', int(zero.size),
                                'cells have zero or near-zero signed volume',
                                tuple(map(int, zero[:100]))))
    if negative.size:
        issues.append(MeshIssue('negative_volume_cells', 'error', int(negative.size),
                                'cells have negative canonical orientation',
                                tuple(map(int, negative[:100]))))

    topology = build_face_topology(mesh)
    if topology.non_manifold_keys.size:
        issues.append(MeshIssue(
            'non_manifold_faces', 'error', topology.non_manifold_keys.shape[0],
            'volume faces are owned by more than two cells'))
    supplied = boundary_keys(mesh)
    if supplied.size:
        supplied_unique, supplied_counts = np.unique(supplied, axis=0, return_counts=True)
    else:
        supplied_unique = np.empty((0, 4), np.int64)
        supplied_counts = np.empty(0, np.int64)
    duplicates = int(np.count_nonzero(supplied_counts > 1))
    if duplicates:
        issues.append(MeshIssue('duplicate_boundary_faces', 'error', duplicates,
                                'boundary faces have more than one patch owner'))
    expected = topology.boundary_keys
    missing = _row_difference(expected, supplied_unique)
    extra = _row_difference(supplied_unique, expected)
    if require_complete_boundary and missing:
        issues.append(MeshIssue('missing_boundary_faces', 'error', missing,
                                'external volume faces have no boundary patch'))
    if extra:
        issues.append(MeshIssue('interior_or_unknown_boundary_faces', 'error', extra,
                                'supplied boundary faces are not external volume faces'))
    return _report(mesh, issues, float(all_volumes.min()) if all_volumes.size else None)


def _duplicate_point_counts(mesh):
    """Return invalid and intentional duplicate-coordinate counts.

    Duplicate coordinates are normally an error. They are valid only when
    every point in that coordinate group belongs to a boundary patch whose
    immutable metadata declares the pair ``non_conformal``. This narrow
    exception preserves separate topology without weakening validation for
    accidental MED duplicates elsewhere.
    """
    points = mesh.points
    if not len(points):
        return 0, 0
    _unique, inverse, counts = np.unique(
        points, axis=0, return_inverse=True, return_counts=True)
    duplicate_groups = np.flatnonzero(counts > 1)
    if not len(duplicate_groups):
        return 0, 0
    protected = set()
    for block in mesh.boundary_blocks:
        patch_ids = getattr(block, 'patch_ids', None)
        if patch_ids is None:
            continue
        for patch_id in np.unique(patch_ids):
            metadata = mesh.patches.get(int(patch_id), {})
            if metadata.get('interface', {}).get('coupling') != 'non_conformal':
                continue
            rows = block.connectivity[patch_ids == patch_id]
            protected.update(int(value) for value in rows.ravel())
    invalid = intentional = 0
    for group in duplicate_groups:
        members = np.flatnonzero(inverse == group)
        count = len(members) - 1
        if all(int(value) in protected for value in members):
            intentional += count
        else:
            invalid += count
    return invalid, intentional


def _row_difference(left, right):
    if not len(left):
        return 0
    if not len(right):
        return len(left)
    joined = np.concatenate((left, right))
    _, inverse, counts = np.unique(joined, axis=0, return_inverse=True, return_counts=True)
    return int(np.count_nonzero(counts[inverse[:len(left)]] == 1))


def _report(mesh, issues, minimum):
    return MeshValidationReport(
        not any(issue.severity == 'error' for issue in issues),
        mesh.point_count, mesh.cell_count, mesh.boundary_face_count,
        minimum, tuple(issues))
