"""Vectorized canonical face construction and signed-volume calculations."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from .model import CanonicalMesh, CellBlock, CellType


_FACES = {
    CellType.TETRA: ((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)),
    CellType.PYRAMID: ((0, 3, 2, 1), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)),
    CellType.PRISM: ((0, 2, 1), (3, 4, 5), (0, 1, 4, 3),
                     (1, 2, 5, 4), (2, 0, 3, 5)),
    CellType.HEXAHEDRON: ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
                         (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)),
}

_TETS = {
    CellType.TETRA: ((0, 1, 2, 3),),
    CellType.PYRAMID: ((0, 1, 2, 4), (0, 2, 3, 4)),
    CellType.PRISM: ((0, 1, 2, 3), (1, 4, 2, 3), (2, 4, 5, 3)),
    CellType.HEXAHEDRON: ((0, 1, 3, 4), (1, 2, 3, 6), (1, 3, 4, 6),
                         (1, 4, 5, 6), (3, 4, 6, 7)),
}


@dataclass(frozen=True)
class FaceTopology:
    keys: np.ndarray
    counts: np.ndarray
    first_indices: np.ndarray
    inverse: np.ndarray
    block_ids: np.ndarray
    cell_indices: np.ndarray
    local_face_indices: np.ndarray

    @property
    def boundary_keys(self) -> np.ndarray:
        return self.keys[self.counts == 1]

    @property
    def non_manifold_keys(self) -> np.ndarray:
        return self.keys[self.counts > 2]


def padded_face_keys(connectivity: np.ndarray, local_faces) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = []
    cell_ids = []
    face_ids = []
    count = connectivity.shape[0]
    for local_id, face in enumerate(local_faces):
        selected = connectivity[:, face]
        padded = np.full((count, 4), -1, dtype=np.int64)
        padded[:, :len(face)] = np.sort(selected, axis=1)
        rows.append(padded)
        cell_ids.append(np.arange(count, dtype=np.int64))
        face_ids.append(np.full(count, local_id, dtype=np.int16))
    return (np.ascontiguousarray(np.concatenate(rows)),
            np.ascontiguousarray(np.concatenate(cell_ids)),
            np.ascontiguousarray(np.concatenate(face_ids)))


def build_face_topology(mesh: CanonicalMesh) -> FaceTopology:
    keys = []
    block_ids = []
    cell_ids = []
    face_ids = []
    for block_id, block in enumerate(mesh.cell_blocks):
        block_keys, block_cells, block_faces = padded_face_keys(
            block.connectivity, _FACES[block.cell_type])
        keys.append(block_keys)
        block_ids.append(np.full(block_keys.shape[0], block_id, dtype=np.int16))
        cell_ids.append(block_cells)
        face_ids.append(block_faces)
    combined = np.ascontiguousarray(np.concatenate(keys))
    unique, first, inverse, counts = np.unique(
        combined, axis=0, return_index=True, return_inverse=True, return_counts=True)
    return FaceTopology(
        unique, counts, first, inverse,
        np.concatenate(block_ids), np.concatenate(cell_ids), np.concatenate(face_ids))


def boundary_keys(mesh: CanonicalMesh) -> np.ndarray:
    blocks = []
    for block in mesh.boundary_blocks:
        padded = np.full((block.count, 4), -1, dtype=np.int64)
        padded[:, :block.cell_type.node_count] = np.sort(block.connectivity, axis=1)
        blocks.append(padded)
    return np.ascontiguousarray(np.concatenate(blocks)) if blocks else np.empty((0, 4), np.int64)


def signed_cell_volumes(points: np.ndarray, block: CellBlock) -> np.ndarray:
    result = np.zeros(block.count, dtype=np.float64)
    cell_points = points[block.connectivity]
    for tet in _TETS[block.cell_type]:
        selected = cell_points[:, tet, :]
        result += np.einsum(
            'ij,ij->i', selected[:, 1] - selected[:, 0],
            np.cross(selected[:, 2] - selected[:, 0], selected[:, 3] - selected[:, 0])) / 6.0
    return result
