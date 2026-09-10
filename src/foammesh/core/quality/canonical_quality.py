"""Engine-neutral surface and mixed-volume quality calculations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from foammesh.core.mesh.connectivity import signed_cell_volumes
from foammesh.core.mesh.model import CanonicalMesh, CellType


class QualityError(ValueError):
    pass


QUALITY_POLICIES = {
    'coarse': {
        'minimum_surface_angle': 8.0, 'maximum_surface_aspect': 30.0,
        'minimum_scaled_jacobian': 0.02, 'maximum_volume_aspect': 50.0,
        'minimum_volume': 1.0e-20,
    },
    'balanced': {
        'minimum_surface_angle': 15.0, 'maximum_surface_aspect': 15.0,
        'minimum_scaled_jacobian': 0.08, 'maximum_volume_aspect': 25.0,
        'minimum_volume': 1.0e-18,
    },
    'strict': {
        'minimum_surface_angle': 22.0, 'maximum_surface_aspect': 8.0,
        'minimum_scaled_jacobian': 0.18, 'maximum_volume_aspect': 12.0,
        'minimum_volume': 1.0e-16,
    },
}


@dataclass(frozen=True)
class FailedEntitySet:
    metric: str
    entity_kind: str
    entity_ids: np.ndarray
    values: np.ndarray
    threshold: float
    comparison: str

    def __post_init__(self):
        ids = np.ascontiguousarray(self.entity_ids, dtype=np.int64)
        values = np.ascontiguousarray(self.values, dtype=np.float64)
        if ids.ndim != 1 or values.shape != ids.shape:
            raise QualityError('failed-set IDs and values must be aligned arrays')
        ids.setflags(write=False)
        values.setflags(write=False)
        object.__setattr__(self, 'entity_ids', ids)
        object.__setattr__(self, 'values', values)

    def to_dict(self, *, include_values=True):
        result = {
            'metric': self.metric, 'entity_kind': self.entity_kind,
            'entity_ids': self.entity_ids.tolist(), 'count': len(self.entity_ids),
            'threshold': self.threshold, 'comparison': self.comparison,
        }
        if include_values:
            result['values'] = self.values.tolist()
        return result


@dataclass(frozen=True)
class VolumeQualityReport:
    valid: bool
    cell_count: int
    volume_minimum: float
    volume_maximum: float
    scaled_jacobian_minimum: float
    aspect_ratio_maximum: float
    cell_type_counts: Mapping[str, int]
    region_counts: Mapping[int, int]
    failed_sets: tuple[FailedEntitySet, ...]
    policy: str

    def to_dict(self):
        return {
            'schema_version': 1, 'valid': self.valid, 'policy': self.policy,
            'cell_count': self.cell_count, 'volume_minimum': self.volume_minimum,
            'volume_maximum': self.volume_maximum,
            'scaled_jacobian_minimum': self.scaled_jacobian_minimum,
            'aspect_ratio_maximum': self.aspect_ratio_maximum,
            'cell_type_counts': dict(self.cell_type_counts),
            'region_counts': {str(key): value for key, value in self.region_counts.items()},
            'failed_sets': [item.to_dict() for item in self.failed_sets],
        }


def evaluate_volume(mesh: CanonicalMesh, *, policy: str = 'balanced') -> VolumeQualityReport:
    thresholds = _policy(policy)
    all_volumes = []
    all_aspects = []
    all_jacobians = []
    all_ids = []
    type_counts = {}
    region_values = []
    offset = 0
    for block in mesh.cell_blocks:
        volumes = signed_cell_volumes(mesh.points, block)
        vertices = mesh.points[block.connectivity]
        edges = _cell_edge_lengths(vertices, block.cell_type)
        aspects = np.divide(edges.max(axis=1), edges.min(axis=1),
                            out=np.full(block.count, np.inf), where=edges.min(axis=1) > 0)
        jacobians = _scaled_jacobian(vertices, block.cell_type, volumes)
        all_volumes.append(volumes)
        all_aspects.append(aspects)
        all_jacobians.append(jacobians)
        all_ids.append(np.arange(offset, offset + block.count, dtype=np.int64))
        offset += block.count
        type_counts[block.cell_type.value] = block.count
        region_values.append(block.region_ids)
    volumes = np.concatenate(all_volumes)
    aspects = np.concatenate(all_aspects)
    jacobians = np.concatenate(all_jacobians)
    ids = np.concatenate(all_ids)
    failed = []
    _append_failure(failed, 'signed_volume', ids, volumes,
                    thresholds['minimum_volume'], 'less_equal',
                    volumes <= thresholds['minimum_volume'])
    _append_failure(failed, 'scaled_jacobian', ids, jacobians,
                    thresholds['minimum_scaled_jacobian'], 'less_than',
                    jacobians < thresholds['minimum_scaled_jacobian'])
    _append_failure(failed, 'volume_aspect_ratio', ids, aspects,
                    thresholds['maximum_volume_aspect'], 'greater_than',
                    aspects > thresholds['maximum_volume_aspect'])
    regions, counts = np.unique(np.concatenate(region_values), return_counts=True)
    return VolumeQualityReport(
        not failed, mesh.cell_count, float(volumes.min()), float(volumes.max()),
        float(jacobians.min()), float(aspects.max()), type_counts,
        {int(region): int(count) for region, count in zip(regions, counts)},
        tuple(failed), policy)


def _polygon_angles(edges):
    incoming = -np.roll(edges, 1, axis=1)
    outgoing = edges
    denominators = np.linalg.norm(incoming, axis=2) * np.linalg.norm(outgoing, axis=2)
    cosines = np.divide(np.einsum('ijk,ijk->ij', incoming, outgoing), denominators,
                        out=np.ones_like(denominators), where=denominators > 0)
    return np.degrees(np.arccos(np.clip(cosines, -1, 1)))


_EDGES = {
    CellType.TETRA: ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)),
    CellType.PYRAMID: ((0, 1), (1, 2), (2, 3), (3, 0),
                       (0, 4), (1, 4), (2, 4), (3, 4)),
    CellType.PRISM: ((0, 1), (1, 2), (2, 0), (3, 4), (4, 5), (5, 3),
                     (0, 3), (1, 4), (2, 5)),
    CellType.HEXAHEDRON: ((0, 1), (1, 2), (2, 3), (3, 0),
                          (4, 5), (5, 6), (6, 7), (7, 4),
                          (0, 4), (1, 5), (2, 6), (3, 7)),
}


def _cell_edge_lengths(vertices, cell_type):
    return np.column_stack([
        np.linalg.norm(vertices[:, end] - vertices[:, start], axis=1)
        for start, end in _EDGES[cell_type]])


def _scaled_jacobian(vertices, cell_type, volumes):
    edges = _cell_edge_lengths(vertices, cell_type)
    characteristic = np.mean(edges, axis=1) ** 3
    reference = {CellType.TETRA: 6.0, CellType.PYRAMID: 3.0,
                 CellType.PRISM: 2.0, CellType.HEXAHEDRON: 1.0}[cell_type]
    return np.divide(reference * volumes, characteristic,
                     out=np.zeros_like(volumes), where=characteristic > 0)


def _append_failure(collection, metric, ids, values, threshold, comparison, mask):
    selected = np.flatnonzero(mask)
    if selected.size:
        collection.append(FailedEntitySet(
            metric, 'volume_cell', ids[selected], values[selected], threshold, comparison))


def _policy(name):
    try:
        return QUALITY_POLICIES[name]
    except KeyError as error:
        raise QualityError(f'unknown quality policy: {name}') from error
