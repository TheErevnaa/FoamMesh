"""Merge the cells whose ``checkMesh`` determinant cannot be solved (DP-6131).

A tetrahedron with two boundary faces has two internal faces. OpenFOAM's cell
determinant (the "wellposedness" check) is built from internal and coupled
faces only, so two normals leave it rank-deficient: it reads 0 and ``checkMesh``
prints ``Failed 1 mesh checks``. MEASURED on a Gmsh baffle duct, 2026-11-01:
608 of 125,813 cells, every one of them on a wall-to-wall edge of the duct or on
a rim where a wall meets the inlet, the outlet or the baffle. A tetrahedral
mesh makes them wherever one tet owns both triangles of a boundary edge, so no
sizing prevents it.

The remedy is the one a hex-dominant mesher gets for free: such a cell joins a
neighbour and the pair becomes one polyhedron. The merged cell has the
neighbour's other internal faces, so the determinant is solvable. Two things
the merge itself creates have to be settled in the same step:

* the two triangles that met on the same wall are now coplanar faces of one
  cell, which OpenFOAM calls concave; boundary triangles of one patch are
  combined into one polygon;
* a union that is not convex is refused, and a cell no single neighbour can
  absorb tries a connected cluster of up to :data:`MAX_CLUSTER` cells.

Only the written ``polyMesh`` changes. The canonical mesh, the Gmsh ``.msh``,
the SU2 file and every patch stay as they were, and a cell never merges across
a cell zone, a cyclic face or a face the caller protects.
"""
from __future__ import annotations

from dataclasses import dataclass
import itertools

import numpy as np

#: Two outward normals closer than this (as a cosine) belong to one plane.
COPLANAR = 0.99999
#: A vertex may lie this far (of the cell's span) in front of a face plane.
CONVEX_TOLERANCE = 1e-9
#: The largest group of cells merged into one.
MAX_CLUSTER = 5


@dataclass(frozen=True)
class Agglomeration:
    faces: np.ndarray
    face_sizes: np.ndarray
    owner: np.ndarray
    neighbour: np.ndarray
    patch_ids: np.ndarray
    internal_count: int
    #: ``cell_map[old] -> new``: where every cell of the unmerged mesh went.
    cell_map: np.ndarray
    cell_count: int
    flagged: int
    merged_cells: int
    combined_faces: int
    unresolved: int

    def to_dict(self) -> dict:
        return {
            'flagged_cells': self.flagged, 'merged_cells': self.merged_cells,
            'cells': self.cell_count,
            'combined_boundary_faces': self.combined_faces,
            'unresolved_cells': self.unresolved,
        }


def agglomerate(points, faces, face_sizes, owner, neighbour, patch_ids,
                internal_count, cell_count, cell_regions, *,
                coupled_patches=(), protected_faces=()):
    """The merged topology, or ``None`` when no cell needs one.

    ``faces`` rows are oriented out of their owner; boundary faces are grouped
    by patch after ``internal_count`` internal ones. ``coupled_patches`` are
    the patch ids of cyclic patches (their faces count towards a cell's
    determinant and are never combined); ``protected_faces`` are internal face
    labels a cell merge may not remove.
    """
    internal = int(internal_count)
    owner = np.asarray(owner)
    neighbour = np.asarray(neighbour)
    patch_ids = np.asarray(patch_ids)
    coupled = np.isin(patch_ids, list(coupled_patches)) if len(
        coupled_patches) else np.zeros(len(patch_ids), dtype=bool)
    solvable = (np.bincount(owner[:internal], minlength=cell_count)
                + np.bincount(neighbour, minlength=cell_count)
                + np.bincount(owner[internal:][coupled], minlength=cell_count))
    flagged = np.flatnonzero(solvable <= 2)
    if not len(flagged):
        return None
    return _Merger(
        points, faces, face_sizes, owner, neighbour, patch_ids, internal,
        cell_count, np.asarray(cell_regions), coupled, set(
            int(value) for value in protected_faces), solvable).run(flagged)


class _Merger:
    def __init__(self, points, faces, sizes, owner, neighbour, patch_ids,
                 internal, cell_count, regions, coupled, protected, solvable):
        self.points = np.asarray(points, dtype=float)
        self.faces = faces
        self.sizes = sizes
        self.owner = owner
        self.neighbour = neighbour
        self.patch_ids = patch_ids
        self.internal = internal
        self.cell_count = cell_count
        self.regions = regions
        self.coupled = coupled
        self.protected = protected
        self.solvable = solvable
        total = len(owner)
        cells = np.concatenate((owner, neighbour))
        ids = np.concatenate((np.arange(total), np.arange(internal)))
        order = np.argsort(cells, kind='stable')
        self._by_cell = ids[order]
        self._starts = np.searchsorted(
            cells[order], np.arange(cell_count + 1))

    # -- topology access -------------------------------------------------

    def cell_faces(self, cell):
        return self._by_cell[self._starts[cell]:self._starts[cell + 1]].tolist()

    def verts(self, face):
        return self.faces[face, :int(self.sizes[face])].tolist()

    def outward(self, face, cell):
        row = self.verts(face)
        return row if int(self.owner[face]) == cell else row[::-1]

    def other(self, face, cell):
        return int(self.neighbour[face] if self.owner[face] == cell
                   else self.owner[face])

    def neighbours(self, cell):
        return [self.other(face, cell) for face in self.cell_faces(cell)
                if face < self.internal]

    def patch_of(self, face):
        return None if face < self.internal else int(
            self.patch_ids[face - self.internal])

    # -- geometry --------------------------------------------------------

    def _normal(self, loop):
        p = self.points[loop]
        vector = np.zeros(3)
        for index in range(1, len(loop) - 1):
            vector += np.cross(p[index] - p[0], p[index + 1] - p[0])
        length = np.linalg.norm(vector)
        return vector / length if length else vector

    def _convex_polygon(self, loop):
        normal = self._normal(loop)
        p = self.points[loop]
        scale = np.linalg.norm(p.max(axis=0) - p.min(axis=0)) or 1.0
        for index in range(len(loop)):
            a, b, c = p[index - 1], p[index], p[(index + 1) % len(loop)]
            if np.dot(np.cross(b - a, c - b), normal) < -1e-9 * scale * scale:
                return False
        return True

    def _combined(self, loop_a, loop_b):
        for k in range(len(loop_a)):
            a, b = loop_a[k], loop_a[(k + 1) % len(loop_a)]
            for m in range(len(loop_b)):
                if (loop_b[m] == b
                        and loop_b[(m + 1) % len(loop_b)] == a):
                    first = loop_a[k + 1:] + loop_a[:k + 1]
                    second = loop_b[m + 1:] + loop_b[:m + 1]
                    return first + second[1:-1]
        return None

    def _combinable(self, face_a, face_b, loop_a, loop_b):
        patch = self.patch_of(face_a)
        if patch is None or patch != self.patch_of(face_b):
            return False
        if self.coupled[face_a - self.internal]:
            return False
        loop = self._combined(loop_a, loop_b)
        return loop is not None and self._convex_polygon(loop)

    def _unmergeable_pair(self, loops, ids):
        """True when two coplanar faces of the union cannot be combined."""
        normals = [self._normal(loop) for loop in loops]
        edges: dict = {}
        for index, loop in enumerate(loops):
            for a, b in zip(loop, loop[1:] + loop[:1]):
                edges.setdefault((min(a, b), max(a, b)), []).append(index)
        for members in edges.values():
            if len(members) != 2:
                continue
            i, j = members
            if float(np.dot(normals[i], normals[j])) > COPLANAR:
                if not self._combinable(ids[i], ids[j], loops[i], loops[j]):
                    return True
        return False

    def _convex_union(self, loops, ids):
        if self._unmergeable_pair(loops, ids):
            return False
        vertices = sorted({v for loop in loops for v in loop})
        span = self.points[vertices]
        scale = np.linalg.norm(span.max(axis=0) - span.min(axis=0)) or 1.0
        for loop in loops:
            p = self.points[loop]
            normal = self._normal(loop)
            if not normal.any():
                return False
            centre = p.mean(axis=0)
            if (np.dot(span - centre, normal) > CONVEX_TOLERANCE * scale).any():
                return False
        return True

    def _union_faces(self, cluster):
        inside = set()
        for cell in cluster:
            for face in self.cell_faces(cell):
                if (face < self.internal and int(self.owner[face]) in cluster
                        and int(self.neighbour[face]) in cluster):
                    inside.add(face)
        loops, ids = [], []
        for cell in cluster:
            for face in self.cell_faces(cell):
                if face not in inside:
                    loops.append(self.outward(face, cell))
                    ids.append(face)
        return inside, loops, ids

    def _acceptable(self, cluster):
        if len({int(self.regions[cell]) for cell in cluster}) != 1:
            return None
        inside, loops, ids = self._union_faces(cluster)
        if inside & self.protected:
            return None
        if sum(1 for face in ids if face < self.internal) < 3:
            return None
        return inside if self._convex_union(loops, ids) else None

    # -- the merge -------------------------------------------------------

    def run(self, flagged):
        taken: set = set()
        clusters: list = []
        removed_faces: set = set()
        unresolved = []
        flagged_set = set(int(c) for c in flagged)
        for cell in sorted(flagged_set):
            if cell in taken:
                continue
            best = None
            for neighbour in self.neighbours(cell):
                if neighbour in taken or neighbour == cell:
                    continue
                inside = self._acceptable({cell, neighbour})
                if inside is None:
                    continue
                score = int(self.solvable[neighbour]) - (
                    3 if neighbour in flagged_set else 0)
                if best is None or score > best[0]:
                    best = (score, neighbour, inside)
            if best is None:
                unresolved.append(cell)
                continue
            _score, neighbour, inside = best
            clusters.append({cell, neighbour})
            taken.update((cell, neighbour))
            removed_faces |= inside
        still = []
        for cell in unresolved:
            found = self._search_cluster(cell, taken)
            if found is None:
                still.append(cell)
                continue
            cluster, inside = found
            clusters.append(cluster)
            taken.update(cluster)
            removed_faces |= inside
        return self._rebuild(clusters, removed_faces, flagged_set, still)

    def _search_cluster(self, cell, taken):
        ring = set(self.neighbours(cell))
        for neighbour in list(ring):
            ring |= set(self.neighbours(neighbour))
        ring = sorted(c for c in ring if c != cell and c not in taken)
        for size in range(1, MAX_CLUSTER):
            for members in itertools.combinations(ring, size):
                cluster = {cell, *members}
                seen, stack = {cell}, [cell]
                while stack:
                    for other in self.neighbours(stack.pop()):
                        if other in cluster and other not in seen:
                            seen.add(other)
                            stack.append(other)
                if seen != cluster:
                    continue
                inside = self._acceptable(cluster)
                if inside is not None:
                    return cluster, inside
        return None

    def _rebuild(self, clusters, removed_faces, flagged, unresolved):
        head = np.arange(self.cell_count)
        for cluster in clusters:
            lowest = min(cluster)
            for cell in cluster:
                head[cell] = lowest
        kept = head == np.arange(self.cell_count)
        index = np.cumsum(kept) - 1
        cell_map = index[head]
        merged_cells = {c for cluster in clusters for c in cluster}
        internal = self.internal
        keep_face = np.ones(internal, dtype=bool)
        if removed_faces:
            keep_face[np.fromiter(removed_faces, dtype=np.int64)] = False
        labels = np.flatnonzero(keep_face)
        new_owner = cell_map[self.owner[labels]]
        new_neighbour = cell_map[self.neighbour[labels]]
        rows = self.faces[labels].copy()
        sizes = self.sizes[labels].copy()
        for position in np.flatnonzero(new_owner > new_neighbour):
            size = int(sizes[position])
            rows[position, :size] = rows[position, :size][::-1]
        swap = new_owner > new_neighbour
        new_owner[swap], new_neighbour[swap] = (
            new_neighbour[swap].copy(), new_owner[swap].copy())
        order = np.lexsort((new_neighbour, new_owner))
        rows, sizes = rows[order], sizes[order]
        new_owner, new_neighbour = new_owner[order], new_neighbour[order]

        replaced, dropped = self._combine_boundary(
            set(int(cell_map[c]) for c in merged_cells), cell_map)
        combined = len(dropped)
        count = len(self.patch_ids)
        keep_boundary = np.ones(count, dtype=bool)
        if dropped:
            keep_boundary[np.fromiter(dropped, dtype=np.int64)] = False
        survivors = np.flatnonzero(keep_boundary)
        boundary_sizes = self.sizes[internal + survivors].copy()
        width = max(rows.shape[1],
                    max((len(loop) for loop in replaced.values()), default=0),
                    self.faces.shape[1])
        faces = np.full((len(rows) + len(survivors), width), -1,
                        dtype=rows.dtype)
        faces[:len(rows), :rows.shape[1]] = rows
        faces[len(rows):, :self.faces.shape[1]] = self.faces[internal + survivors]
        position = {int(offset): number for number, offset in enumerate(survivors)}
        for offset, loop in replaced.items():
            number = position[int(offset)]
            faces[len(rows) + number, :] = -1
            faces[len(rows) + number, :len(loop)] = loop
            boundary_sizes[number] = len(loop)
        boundary_owner = cell_map[self.owner[internal + survivors]]
        boundary_patch = self.patch_ids[survivors]
        return Agglomeration(
            faces=faces,
            face_sizes=np.concatenate(
                (sizes, boundary_sizes.astype(sizes.dtype))),
            owner=np.concatenate(
                (new_owner, boundary_owner.astype(new_owner.dtype))),
            neighbour=new_neighbour,
            patch_ids=boundary_patch,
            internal_count=len(rows), cell_map=cell_map,
            cell_count=int(kept.sum()), flagged=len(flagged),
            merged_cells=int((~kept).sum()), combined_faces=combined,
            unresolved=len(unresolved))

    def _combine_boundary(self, merged_cell_set, cell_map):
        """Combine coplanar same-patch boundary faces of the merged cells."""
        replaced: dict = {}
        dropped: set = set()
        by_cell: dict = {}
        mapped = cell_map[self.owner[self.internal:]]
        candidates = np.flatnonzero(
            np.isin(mapped, list(merged_cell_set)) & ~self.coupled)
        for offset in candidates.tolist():
            by_cell.setdefault(
                (int(mapped[offset]), int(self.patch_ids[offset])), []).append(offset)
        for offsets in by_cell.values():
            if len(offsets) < 2:
                continue
            loops = {offset: self.verts(self.internal + offset)
                     for offset in offsets}
            changed = True
            while changed:
                changed = False
                live = [offset for offset in offsets if offset not in dropped]
                for first, second in itertools.combinations(live, 2):
                    a, b = loops[first], loops[second]
                    if float(np.dot(self._normal(a), self._normal(b))) < COPLANAR:
                        continue
                    loop = self._combined(a, b)
                    if loop is None or not self._convex_polygon(loop):
                        continue
                    loops[first] = loop
                    replaced[first] = loop
                    dropped.add(second)
                    changed = True
                    break
        return replaced, dropped
