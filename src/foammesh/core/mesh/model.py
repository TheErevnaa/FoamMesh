"""Compact canonical mixed-element mesh model.

Topology is stored exclusively in contiguous typed NumPy arrays.  The model
contains no engine-specific objects and is the common boundary for validation,
visualization, quality, and solver export.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import MappingProxyType
from typing import Mapping

import numpy as np


class CanonicalMeshError(ValueError):
    pass


class CellType(str, Enum):
    TETRA = 'tetra'
    PYRAMID = 'pyramid'
    PRISM = 'prism'
    HEXAHEDRON = 'hexahedron'
    TRIANGLE = 'triangle'
    QUADRILATERAL = 'quadrilateral'

    @property
    def node_count(self) -> int:
        return {
            CellType.TETRA: 4, CellType.PYRAMID: 5, CellType.PRISM: 6,
            CellType.HEXAHEDRON: 8, CellType.TRIANGLE: 3,
            CellType.QUADRILATERAL: 4,
        }[self]

    @property
    def dimension(self) -> int:
        return 2 if self in {CellType.TRIANGLE, CellType.QUADRILATERAL} else 3


def _array(value, dtype, *, ndim, columns=None, name='array'):
    result = np.ascontiguousarray(value, dtype=dtype)
    if result.ndim != ndim:
        raise CanonicalMeshError(f'{name} must have {ndim} dimensions')
    if columns is not None and result.shape[1] != columns:
        raise CanonicalMeshError(f'{name} requires {columns} columns')
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class CellBlock:
    cell_type: CellType
    connectivity: np.ndarray
    source_ids: np.ndarray
    region_ids: np.ndarray

    def __post_init__(self) -> None:
        cell_type = CellType(self.cell_type)
        if cell_type.dimension != 3:
            raise CanonicalMeshError('CellBlock requires a volume cell type')
        connectivity = _array(self.connectivity, np.int64, ndim=2,
                              columns=cell_type.node_count, name='connectivity')
        count = connectivity.shape[0]
        source = _array(self.source_ids, np.int64, ndim=1, name='source_ids')
        regions = _array(self.region_ids, np.int64, ndim=1, name='region_ids')
        if source.shape != (count,) or regions.shape != (count,):
            raise CanonicalMeshError('cell metadata length differs from connectivity')
        if len(np.unique(source)) != count:
            raise CanonicalMeshError('source cell IDs must be unique within a block')
        object.__setattr__(self, 'cell_type', cell_type)
        object.__setattr__(self, 'connectivity', connectivity)
        object.__setattr__(self, 'source_ids', source)
        object.__setattr__(self, 'region_ids', regions)

    @property
    def count(self) -> int:
        return self.connectivity.shape[0]


@dataclass(frozen=True)
class BoundaryBlock:
    cell_type: CellType
    connectivity: np.ndarray
    source_ids: np.ndarray
    patch_ids: np.ndarray

    def __post_init__(self) -> None:
        cell_type = CellType(self.cell_type)
        if cell_type.dimension != 2:
            raise CanonicalMeshError('BoundaryBlock requires triangle or quadrilateral')
        connectivity = _array(self.connectivity, np.int64, ndim=2,
                              columns=cell_type.node_count, name='boundary connectivity')
        count = connectivity.shape[0]
        source = _array(self.source_ids, np.int64, ndim=1, name='source_ids')
        patches = _array(self.patch_ids, np.int64, ndim=1, name='patch_ids')
        if source.shape != (count,) or patches.shape != (count,):
            raise CanonicalMeshError('boundary metadata length differs from connectivity')
        if len(np.unique(source)) != count:
            raise CanonicalMeshError('source face IDs must be unique within a block')
        object.__setattr__(self, 'cell_type', cell_type)
        object.__setattr__(self, 'connectivity', connectivity)
        object.__setattr__(self, 'source_ids', source)
        object.__setattr__(self, 'patch_ids', patches)

    @property
    def count(self) -> int:
        return self.connectivity.shape[0]


@dataclass(frozen=True)
class CanonicalMesh:
    points: np.ndarray
    cell_blocks: tuple[CellBlock, ...]
    boundary_blocks: tuple[BoundaryBlock, ...]
    patches: Mapping[int, dict]
    regions: Mapping[int, dict]
    source_engine: str
    source_fingerprint: str
    units: str = 'm'
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise CanonicalMeshError('unsupported canonical mesh schema')
        if self.units != 'm':
            raise CanonicalMeshError('canonical mesh coordinates must use metres')
        points = _array(self.points, np.float64, ndim=2, columns=3, name='points')
        if not np.isfinite(points).all():
            raise CanonicalMeshError('points contain non-finite values')
        cells = tuple(self.cell_blocks)
        boundaries = tuple(self.boundary_blocks)
        if not cells:
            raise CanonicalMeshError('canonical mesh requires volume cells')
        if len({block.cell_type for block in cells}) != len(cells):
            raise CanonicalMeshError('duplicate volume cell blocks')
        if len({block.cell_type for block in boundaries}) != len(boundaries):
            raise CanonicalMeshError('duplicate boundary cell blocks')
        patches = {int(key): dict(value) for key, value in self.patches.items()}
        regions = {int(key): dict(value) for key, value in self.regions.items()}
        used_patches = set(np.concatenate([block.patch_ids for block in boundaries]).tolist()) \
            if boundaries else set()
        used_regions = set(np.concatenate([block.region_ids for block in cells]).tolist())
        if used_patches - set(patches):
            raise CanonicalMeshError('boundary references unknown patch IDs')
        if used_regions - set(regions):
            raise CanonicalMeshError('cells reference unknown region IDs')
        object.__setattr__(self, 'points', points)
        object.__setattr__(self, 'cell_blocks', cells)
        object.__setattr__(self, 'boundary_blocks', boundaries)
        object.__setattr__(self, 'patches', MappingProxyType(patches))
        object.__setattr__(self, 'regions', MappingProxyType(regions))
        object.__setattr__(self, 'metadata', MappingProxyType(dict(self.metadata)))

    @property
    def point_count(self) -> int:
        return self.points.shape[0]

    @property
    def cell_count(self) -> int:
        return sum(block.count for block in self.cell_blocks)

    @property
    def boundary_face_count(self) -> int:
        return sum(block.count for block in self.boundary_blocks)

    def to_manifest(self) -> dict:
        return {
            'schema_version': self.schema_version, 'units': self.units,
            'source_engine': self.source_engine,
            'source_fingerprint': self.source_fingerprint,
            'point_count': self.point_count, 'cell_count': self.cell_count,
            'boundary_face_count': self.boundary_face_count,
            'cell_blocks': [
                {'cell_type': block.cell_type.value, 'count': block.count,
                 'connectivity': f'cells.{block.cell_type.value}.connectivity',
                 'source_ids': f'cells.{block.cell_type.value}.source_ids',
                 'region_ids': f'cells.{block.cell_type.value}.region_ids'}
                for block in self.cell_blocks],
            'boundary_blocks': [
                {'cell_type': block.cell_type.value, 'count': block.count,
                 'connectivity': f'boundary.{block.cell_type.value}.connectivity',
                 'source_ids': f'boundary.{block.cell_type.value}.source_ids',
                 'patch_ids': f'boundary.{block.cell_type.value}.patch_ids'}
                for block in self.boundary_blocks],
            'patches': {str(key): value for key, value in self.patches.items()},
            'regions': {str(key): value for key, value in self.regions.items()},
            'metadata': dict(self.metadata),
        }


class CanonicalMeshStore:
    MANIFEST_NAME = 'mesh-manifest.json'
    ARRAYS_NAME = 'mesh-arrays.npz'

    def write(self, destination: str | Path, mesh: CanonicalMesh) -> dict:
        destination = Path(destination).resolve()
        if destination.exists():
            raise CanonicalMeshError('canonical artifact destination already exists')
        temporary = destination.with_name(destination.name + '.tmp')
        temporary.mkdir(parents=True)
        try:
            arrays = {'points': mesh.points}
            for block in mesh.cell_blocks:
                prefix = f'cells.{block.cell_type.value}'
                arrays[f'{prefix}.connectivity'] = block.connectivity
                arrays[f'{prefix}.source_ids'] = block.source_ids
                arrays[f'{prefix}.region_ids'] = block.region_ids
            for block in mesh.boundary_blocks:
                prefix = f'boundary.{block.cell_type.value}'
                arrays[f'{prefix}.connectivity'] = block.connectivity
                arrays[f'{prefix}.source_ids'] = block.source_ids
                arrays[f'{prefix}.patch_ids'] = block.patch_ids
            arrays_path = temporary / self.ARRAYS_NAME
            np.savez_compressed(arrays_path, **arrays)
            manifest = mesh.to_manifest()
            manifest['arrays'] = self.ARRAYS_NAME
            manifest['arrays_sha256'] = _sha256(arrays_path)
            _write_json(temporary / self.MANIFEST_NAME, manifest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
            return manifest
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def read(self, source: str | Path) -> CanonicalMesh:
        source = Path(source).resolve()
        manifest = json.loads((source / self.MANIFEST_NAME).read_text(encoding='utf-8'))
        arrays_path = source / manifest['arrays']
        if _sha256(arrays_path) != manifest['arrays_sha256']:
            raise CanonicalMeshError('canonical array checksum mismatch')
        with np.load(arrays_path, allow_pickle=False) as arrays:
            cells = tuple(CellBlock(
                CellType(item['cell_type']), arrays[item['connectivity']],
                arrays[item['source_ids']], arrays[item['region_ids']])
                for item in manifest['cell_blocks'])
            boundaries = tuple(BoundaryBlock(
                CellType(item['cell_type']), arrays[item['connectivity']],
                arrays[item['source_ids']], arrays[item['patch_ids']])
                for item in manifest['boundary_blocks'])
            return CanonicalMesh(
                arrays['points'], cells, boundaries,
                {int(key): value for key, value in manifest['patches'].items()},
                {int(key): value for key, value in manifest['regions'].items()},
                manifest['source_engine'], manifest['source_fingerprint'],
                manifest['units'], manifest.get('metadata', {}),
                manifest['schema_version'])


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
