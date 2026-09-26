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


# --------------------------------------------------------------------------- #
# DP-400: a face between two cells that are both on the same side of it
# --------------------------------------------------------------------------- #
#
# A face separates the two cells that share it only if their centroids lie on
# opposite sides of it. Nothing upstream asks that question. `signed_cell_volumes`
# asks whether a single cell is wound inside out, and the face-count guard asks
# whether more than two cells claim a face; a boundary layer extruded into volume
# the tetrahedra already occupied passes both, because each cell is individually
# well formed and each face is shared by exactly two of them. On `heated_duct`
# the result published cleanly and `checkMesh` then reported 342 open cells --
# the same 342 this returns, three stages earlier.


@dataclass(frozen=True)
class StraddleReport:
    """Internal faces whose two cells fall on the same side of them."""

    count: int
    triangles: int
    quadrilaterals: int
    cells: int
    #: ``(face centre, owner, neighbour)`` for the first few, for the message.
    samples: tuple = ()

    def __bool__(self) -> bool:
        return self.count > 0


def cell_centroids(points: np.ndarray, block: CellBlock) -> np.ndarray:
    """Volume-weighted centroid of every cell in ``block``.

    Built from the same tetrahedral decomposition as :func:`signed_cell_volumes`
    so the two answers cannot disagree about what a cell is. A cell of zero
    volume has no centroid to weight, and falls back to the mean of its nodes.
    """
    cell_points = points[block.connectivity]
    total = np.zeros(block.count, dtype=np.float64)
    moment = np.zeros((block.count, 3), dtype=np.float64)
    for tet in _TETS[block.cell_type]:
        selected = cell_points[:, tet, :]
        volume = np.einsum(
            'ij,ij->i', selected[:, 1] - selected[:, 0],
            np.cross(selected[:, 2] - selected[:, 0],
                     selected[:, 3] - selected[:, 0])) / 6.0
        total += volume
        moment += volume[:, None] * selected.mean(axis=1)
    degenerate = total == 0.0
    safe = np.where(degenerate, 1.0, total)
    centroid = moment / safe[:, None]
    if degenerate.any():
        centroid[degenerate] = cell_points[degenerate].mean(axis=1)
    return centroid


def _face_geometry(nodes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Centre and area vector of each ordered polygon in ``nodes``.

    ``nodes`` is ``(m, n, 3)``. The area vector is the fan sum about the node
    mean, which is exact for a triangle and for a planar quadrilateral, and is
    the usual reading of a warped one. Its sign follows the node order, and the
    test below compares two signs, so an arbitrary overall sign is harmless.
    """
    centre = nodes.mean(axis=1)
    spokes = nodes - centre[:, None, :]
    area = 0.5 * np.cross(spokes, np.roll(spokes, -1, axis=1)).sum(axis=1)
    return centre, area


def straddling_faces(points: np.ndarray, cell_blocks,
                     limit: int = 4) -> StraddleReport:
    """Internal faces that do not lie between the cells sharing them.

    For every face shared by exactly two cells, the sign of
    ``Sf . (owner centroid - face centre)`` and of
    ``Sf . (neighbour centroid - face centre)``. Equal signs mean both cells
    sit on the same side: they occupy the same space, and the face does not
    separate them. A zero dot is a centroid on the face plane, which is a
    flat cell rather than a misplaced one, and is left to the volume guards.
    """
    blocks = [block for block in cell_blocks if block.count]
    if not blocks:
        return StraddleReport(0, 0, 0, 0)

    keys, cell_ids, block_ids, local_ids, centroids = [], [], [], [], []
    offsets, offset = [], 0
    for block_id, block in enumerate(blocks):
        block_keys, block_cells, block_faces = padded_face_keys(
            block.connectivity, _FACES[block.cell_type])
        keys.append(block_keys)
        cell_ids.append(block_cells + offset)
        block_ids.append(np.full(block_keys.shape[0], block_id, dtype=np.int16))
        local_ids.append(block_faces)
        centroids.append(cell_centroids(points, block))
        offsets.append(offset)
        offset += block.count

    combined = np.ascontiguousarray(np.concatenate(keys))
    del keys
    cell_ids = np.concatenate(cell_ids)
    block_ids = np.concatenate(block_ids)
    local_ids = np.concatenate(local_ids)
    centroid = np.concatenate(centroids)

    _unique, inverse, counts = np.unique(
        combined, axis=0, return_inverse=True, return_counts=True)
    del combined
    inverse = np.asarray(inverse).ravel()
    wanted = np.flatnonzero(counts == 2)
    if not wanted.size:
        return StraddleReport(0, 0, 0, 0)

    # The two instances of a face are adjacent once sorted by unique index.
    order = np.argsort(inverse, kind='stable')
    first = np.searchsorted(inverse[order], np.arange(counts.size))
    left, right = order[first[wanted]], order[first[wanted] + 1]

    same_side = np.zeros(wanted.size, dtype=bool)
    sides = np.zeros(wanted.size, dtype=np.int8)
    centres = np.zeros((wanted.size, 3), dtype=np.float64)
    for block_id, block in enumerate(blocks):
        for local_id, face in enumerate(_FACES[block.cell_type]):
            chosen = ((block_ids[left] == block_id)
                      & (local_ids[left] == local_id))
            if not chosen.any():
                continue
            owner = cell_ids[left[chosen]]
            rows = block.connectivity[owner - offsets[block_id]][:, face]
            centre, area = _face_geometry(points[rows])
            toward_owner = np.einsum(
                'ij,ij->i', area, centroid[owner] - centre)
            toward_neighbour = np.einsum(
                'ij,ij->i', area, centroid[cell_ids[right[chosen]]] - centre)
            same_side[chosen] = (toward_owner * toward_neighbour) > 0.0
            sides[chosen] = len(face)
            centres[chosen] = centre

    found = np.flatnonzero(same_side)
    if not found.size:
        return StraddleReport(0, 0, 0, 0)
    touched = np.union1d(cell_ids[left[found]], cell_ids[right[found]])
    samples = tuple(
        (tuple(float(value) for value in centres[index]),
         int(cell_ids[left[index]]), int(cell_ids[right[index]]))
        for index in found[:limit])
    return StraddleReport(
        count=int(found.size),
        triangles=int((sides[found] == 3).sum()),
        quadrilaterals=int((sides[found] == 4).sum()),
        cells=int(touched.size), samples=samples)
