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


@dataclass(frozen=True)
class RowGroups:
    """Equal rows of an integer key array, found by one sort (Plan 50 IP14).

    ``order`` sorts the rows lexicographically (rows that are equal come in
    no particular order among themselves); ``starts`` holds the sorted
    positions where each group of equal rows begins, plus the total at the
    end. Everything ``np.unique(keys, axis=0, ...)`` returns follows from the
    two, and is equal to it element for element (see :func:`unique_rows`).
    """

    order: np.ndarray
    starts: np.ndarray

    @property
    def counts(self) -> np.ndarray:
        return np.diff(self.starts)

    @property
    def group_count(self) -> int:
        return int(self.starts.size - 1)

    def sorted_group(self) -> np.ndarray:
        """Group number of each sorted position."""
        counts = self.counts
        return np.repeat(np.arange(counts.size, dtype=np.int64), counts)

    def inverse(self) -> np.ndarray:
        """Group number of each row, in the rows' own order."""
        inverse = np.empty(self.order.size, dtype=np.int64)
        inverse[self.order] = self.sorted_group()
        return inverse

    def first(self) -> np.ndarray:
        """The smallest row index of each group (``np.unique``'s index)."""
        if not self.order.size:
            return np.empty(0, dtype=np.int64)
        return np.minimum.reduceat(self.order, self.starts[:-1])


def _bits(value: int) -> int:
    return max(int(value), 0).bit_length()


def row_groups(keys: np.ndarray) -> RowGroups:
    """Group the equal rows of *keys*, an ``(n, w)`` integer array.

    Plan 50 IP14 (GM-1). ``np.unique(axis=0)`` sorts the rows as a
    structured array, a comparison sort of ``n`` records several times
    slower than sorting ``n`` integers. Here the columns are packed into one
    unsigned 64-bit word each row, most significant column first, so the
    word order is the lexicographic row order; when the next column no longer
    fits, the word packed so far is replaced by its dense rank (which keeps
    the order and needs only ``log2(n)`` bits) and packing carries on. A
    column with one value throughout cannot change the order and is skipped
    (the padding column of an all-triangle face list). Values must be at
    least ``-1``.
    """
    keys = np.asarray(keys)
    if keys.ndim != 2:
        raise ValueError('row_groups takes a two-dimensional array')
    count = keys.shape[0]
    if count == 0:
        return RowGroups(np.empty(0, dtype=np.int64),
                         np.zeros(1, dtype=np.int64))
    packed = None
    packed_bits = 0
    for column in range(keys.shape[1]):
        values = keys[:, column]
        low, high = int(values.min()), int(values.max())
        if low == high:
            continue
        if low < -1:
            raise ValueError('row_groups keys must be at least -1')
        shifted = (values.astype(np.int64) + 1).astype(np.uint64)
        width = _bits(high + 1)
        if packed is None:
            packed, packed_bits = shifted, width
            continue
        if packed_bits + width > 64:
            packed, packed_bits = _dense_rank(packed)
        if packed_bits + width > 64:
            return _lexsorted_groups(keys)
        packed = (packed << np.uint64(width)) | shifted
        packed_bits += width
    if packed is None:
        # every row is the same row
        return RowGroups(np.arange(count, dtype=np.int64),
                         np.array([0, count], dtype=np.int64))
    order = np.argsort(packed)
    ordered = packed[order]
    change = np.flatnonzero(ordered[1:] != ordered[:-1]) + 1
    starts = np.concatenate(([0], change, [count])).astype(np.int64)
    return RowGroups(order.astype(np.int64, copy=False), starts)


def _lexsorted_groups(keys: np.ndarray) -> RowGroups:
    """The general case: values too wide to pack beside a rank."""
    order = np.lexsort(keys.T[::-1])
    ordered = keys[order]
    change = np.flatnonzero((ordered[1:] != ordered[:-1]).any(axis=1)) + 1
    starts = np.concatenate(([0], change, [keys.shape[0]])).astype(np.int64)
    return RowGroups(order.astype(np.int64, copy=False), starts)


def _dense_rank(values: np.ndarray) -> tuple[np.ndarray, int]:
    order = np.argsort(values)
    ordered = values[order]
    step = np.empty(values.size, dtype=np.uint64)
    step[0] = 0
    np.not_equal(ordered[1:], ordered[:-1], out=step[1:], casting='unsafe')
    rank_sorted = np.cumsum(step, dtype=np.uint64)
    ranks = np.empty(values.size, dtype=np.uint64)
    ranks[order] = rank_sorted
    return ranks, _bits(int(rank_sorted[-1]))


def unique_rows(keys: np.ndarray, *, return_index=False, return_inverse=False,
                return_counts=False):
    """``np.unique(keys, axis=0, ...)``, through :func:`row_groups`.

    The same unique rows in the same (lexicographic) order, the same first
    indices, inverse and counts; only faster. Keys must be at least ``-1``.
    """
    keys = np.asarray(keys)
    groups = row_groups(keys)
    first = groups.first()
    result = [keys[first] if first.size else keys[:0]]
    if return_index:
        result.append(first)
    if return_inverse:
        result.append(groups.inverse())
    if return_counts:
        result.append(groups.counts)
    return result[0] if len(result) == 1 else tuple(result)


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
    # Plan 50 IP14: np.unique(axis=0) results, from one integer sort.
    unique, first, inverse, counts = unique_rows(
        combined, return_index=True, return_inverse=True, return_counts=True)
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

    # Plan 50 IP14: the faces met twice, from one integer sort rather than
    # np.unique(axis=0) and a second, stable sort of its inverse.
    groups = row_groups(combined)
    del combined
    counts = groups.counts
    wanted = np.flatnonzero(counts == 2)
    if not wanted.size:
        return StraddleReport(0, 0, 0, 0)

    # The two instances of a face, the lower row index first.
    one = groups.order[groups.starts[wanted]]
    two = groups.order[groups.starts[wanted] + 1]
    left, right = np.minimum(one, two), np.maximum(one, two)
    del one, two

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
