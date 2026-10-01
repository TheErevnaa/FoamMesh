#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Exact sections of a polyMesh, for the section worker (Plan 37 UF10).

Everything here works on a polyMesh's own topology -- points, faces in
compact form, owner and neighbour -- so every cell is built from *all* its
faces, each turned outward (a face's stored order points from its owner to
its neighbour, so the neighbour sees it reversed). Plain numpy, vectorised
over the faces a plane crosses; the rare awkward cases (a face the plane
crosses more than twice, a polygon a mask plane cuts, a concave section)
fall back to short Python loops over just those.

The four modes (``foammesh.core.section.modes``):

* **Slice** -- the section polygons of the active plane, masked by the
  other enabled planes' kept half-spaces. A vertex within the tolerance of
  the plane counts as on its positive side, so a plane lying on a face is
  sliced once, by the cell on the negative side (a deterministic owner), and
  a plane that only touches an edge or a vertex gives a zero-area loop that
  is dropped.
* **Cut cells** -- every cell whose *closed* volume meets the active plane
  (inside the other planes' closed kept half-spaces), drawn whole. Both
  cells beside a face the plane lies on are in.
* **Clip** -- the surface of the mesh intersected with every kept
  half-space: the kept part of the boundary plus, on each plane, the section
  of the mesh masked by the others. Cells wholly inside are retained; the
  cut is smooth.
* **Clip by whole cells** -- every cell with positive volume inside the kept
  region, drawn whole (a stepped surface).

The exactness facts the code leans on:

* the extreme values of a linear function over a polyhedron -- convex or
  not -- are at vertices, so for one plane "the closed cell meets it" is
  exactly "its vertex range spans zero", and "the cell has a point strictly
  inside one half-space" is exactly "some vertex is";
* neither holds for an *intersection* of half-spaces with a non-convex
  cell, or with a cell straddling the corner of two planes, so those cells
  are decided on their actual section polygons or clipped volume.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = ['HalfSpace', 'Polygons', 'SectionGeometry', 'clip_polygon',
           'polygon_area_vectors', 'region_state']

#: Bits of :meth:`SectionGeometry.face_codes`, per vertex value ``v`` and
#: tolerance ``tol``. A face (or cell) meets the closed plane exactly when
#: its vertices carry both ON_OR_BELOW and ON_OR_ABOVE: either one vertex is
#: within ``tol`` or two are on opposite sides, and a face's -- or a cell's
#: -- vertices are joined by its edges, so one edge then crosses.
ON_OR_BELOW = 1         # v <= tol
ON_OR_ABOVE = 2         # v >= -tol
ABOVE = 4               # v > tol
BELOW = 8               # v < -tol
MEETS = ON_OR_BELOW | ON_OR_ABOVE


# --------------------------------------------------------------------------- #
# Planes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class HalfSpace:
    """``normal . x - offset`` is the signed distance; ``keep`` its kept sign.

    Built from a canonical plane state (``n . (x - p_ref) = d``, keep ``s``):
    ``offset = n . p_ref + d``.
    """

    normal: np.ndarray
    offset: float
    keep: int = 1

    @classmethod
    def from_state(cls, state) -> 'HalfSpace':
        normal = np.asarray(state.normal, dtype=float)
        reference = np.asarray(state.reference, dtype=float)
        return cls(normal, float(normal @ reference + state.distance),
                   1 if state.keep >= 0 else -1)

    def signed(self, points) -> np.ndarray:
        return np.asarray(points, dtype=float) @ self.normal - self.offset

    def kept(self, points) -> np.ndarray:
        return self.keep * self.signed(points)

    @property
    def inward(self) -> np.ndarray:
        """The unit normal pointing into the kept half."""
        return self.keep * self.normal


def region_state(spaces, active: HalfSpace | None, tol: float) -> str:
    """``'ok'``, ``'contradictory'`` (no common point) or ``'thin'``.

    With ``active`` the region is that plane inside the closed half-spaces;
    without, the half-spaces' intersection must have an interior (``thin``
    when it is only a sheet, e.g. two opposed planes on top of each other).
    """
    if not spaces:
        return 'ok'
    from scipy.optimize import linprog

    normals = np.array([s.inward for s in spaces])
    offsets = np.array([s.keep * s.offset for s in spaces])
    # maximise t subject to inward . x - keep * offset >= t, t <= 1
    a_ub = np.hstack([-normals, np.ones((len(spaces), 1))])
    b_ub = -offsets
    a_eq = b_eq = None
    if active is not None:
        a_eq = np.array([[*active.normal, 0.0]])
        b_eq = np.array([active.offset])
    result = linprog(np.array([0, 0, 0, -1.0]), A_ub=a_ub, b_ub=b_ub,
                     A_eq=a_eq, b_eq=b_eq,
                     bounds=[(None, None)] * 3 + [(None, 1.0)],
                     method='highs')
    if result.status == 2:
        return 'contradictory'
    if result.status != 0:
        return 'ok'                         # unbounded / numerical: do not refuse
    best = -result.fun
    if best < -tol:
        return 'contradictory'
    if active is None and best <= tol:
        return 'thin'
    return 'ok'


# --------------------------------------------------------------------------- #
# Polygons
# --------------------------------------------------------------------------- #

@dataclass
class Polygons:
    """Polygons over a point table, each with the source cell it came from."""

    points: np.ndarray = field(
        default_factory=lambda: np.zeros((0, 3), dtype=float))
    offsets: np.ndarray = field(
        default_factory=lambda: np.zeros(1, dtype=np.int64))
    connectivity: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64))
    cells: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64))

    @property
    def count(self) -> int:
        return int(self.cells.size)

    @classmethod
    def from_lists(cls, polygons, cells) -> 'Polygons':
        """From ``[(k, 3) array, ...]`` and their cell ids (points not shared)."""
        if not polygons:
            return cls()
        sizes = np.array([len(p) for p in polygons], dtype=np.int64)
        points = np.concatenate([np.asarray(p, float) for p in polygons])
        return cls(points, np.concatenate([[0], np.cumsum(sizes)]),
                   np.arange(len(points), dtype=np.int64),
                   np.asarray(cells, dtype=np.int64))

    @classmethod
    def concatenate(cls, parts) -> 'Polygons':
        parts = [p for p in parts if p.count]
        if not parts:
            return cls()
        points, offsets, conn, cells = [], [np.zeros(1, np.int64)], [], []
        base_point = base_conn = 0
        for part in parts:
            points.append(part.points)
            conn.append(part.connectivity + base_point)
            offsets.append(part.offsets[1:] + base_conn)
            cells.append(part.cells)
            base_point += len(part.points)
            base_conn += len(part.connectivity)
        return cls(np.concatenate(points), np.concatenate(offsets),
                   np.concatenate(conn), np.concatenate(cells))

    def area_vectors(self) -> np.ndarray:
        return polygon_area_vectors(self.points, self.offsets,
                                    self.connectivity)

    def area(self) -> float:
        return float(np.linalg.norm(self.area_vectors(), axis=1).sum())

    def enclosed_volume(self) -> float:
        """Divergence theorem: exact for a closed, outward, planar set."""
        if not self.count:
            return 0.0
        first = self.points[self.connectivity[self.offsets[:-1]]]
        return float(np.einsum('ij,ij->', first, self.area_vectors()) / 3.0)

    def select(self, keep: np.ndarray) -> 'Polygons':
        keep = np.asarray(keep, dtype=bool)
        sizes = np.diff(self.offsets)[keep]
        slots = _ranges(self.offsets[:-1][keep], sizes)
        return Polygons(self.points,
                        np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64),
                        self.connectivity[slots], self.cells[keep])

    def compacted(self) -> 'Polygons':
        used, inverse = np.unique(self.connectivity, return_inverse=True)
        return Polygons(self.points[used], self.offsets,
                        inverse.astype(np.int64), self.cells)


def _ranges(starts, sizes) -> np.ndarray:
    """``concatenate([arange(s, s + n) for s, n in zip(starts, sizes)])``."""
    sizes = np.asarray(sizes, dtype=np.int64)
    total = int(sizes.sum())
    if not total:
        return np.zeros(0, dtype=np.int64)
    bases = np.repeat(np.asarray(starts, np.int64) - np.concatenate(
        [[0], np.cumsum(sizes)[:-1]]), sizes)
    return bases + np.arange(total, dtype=np.int64)


def polygon_area_vectors(points, offsets, connectivity) -> np.ndarray:
    """Newell's area vector (half the summed cross products) per polygon."""
    offsets = np.asarray(offsets, dtype=np.int64)
    n = len(offsets) - 1
    if n <= 0:
        return np.zeros((0, 3))
    slots = np.arange(len(connectivity), dtype=np.int64)
    nxt = slots + 1
    nxt[offsets[1:] - 1] = offsets[:-1]
    a = points[connectivity]
    b = points[connectivity[nxt]]
    cross = np.cross(a, b)
    sizes = np.diff(offsets)
    out = np.zeros((n, 3))
    nonempty = sizes > 0
    if nonempty.any():
        out[nonempty] = np.add.reduceat(cross, offsets[:-1][nonempty], axis=0)
    return 0.5 * out


def clip_polygon(points, spaces, tol: float) -> np.ndarray:
    """Sutherland-Hodgman: the part of a polygon in every closed half-space.

    A point within ``tol`` of a plane counts as kept. Returns a ``(k, 3)``
    array, ``k == 0`` when nothing is left.
    """
    points = np.asarray(points, dtype=float)
    for space in spaces:
        if not len(points):
            break
        value = space.kept(points)
        inside = value >= -tol
        if inside.all():
            continue
        if not inside.any():
            return np.zeros((0, 3))
        out = []
        n = len(points)
        for i in range(n):
            j = (i + 1) % n
            if inside[i]:
                out.append(points[i])
            if inside[i] != inside[j]:
                t = value[i] / (value[i] - value[j])
                out.append(points[i] + t * (points[j] - points[i]))
        points = np.array(out) if out else np.zeros((0, 3))
    return points


# --------------------------------------------------------------------------- #
# The mesh
# --------------------------------------------------------------------------- #

@dataclass
class Loops:
    """Closed section loops: point sequences with their cell and sense."""

    cells: np.ndarray
    offsets: np.ndarray
    points: np.ndarray            # (m, 3), one row per loop corner
    areas: np.ndarray             # (L, 3) area vectors
    broken_cells: np.ndarray      # cells whose segments did not close

    @property
    def count(self) -> int:
        return int(self.cells.size)

    def loop(self, index: int) -> np.ndarray:
        return self.points[self.offsets[index]:self.offsets[index + 1]]


#: Per face code: does the plane cross the face, with a vertex within the
#: tolerance counted on the positive (+1) or the negative (-1) side.
_CROSSED = {
    1: np.array([bool(c & ON_OR_ABOVE and c & BELOW) for c in range(16)]),
    -1: np.array([bool(c & ABOVE and c & ON_OR_BELOW) for c in range(16)]),
}

#: Faces per thread before the per-face passes are split: numpy releases
#: the GIL inside a gather or a ``reduceat``, so the parts run at once.
PART_FACES = 2_000_000


def _in_parts(n: int, work, part: int = PART_FACES) -> None:
    """``work(first, last)`` over ``[0, n)`` in parts, on threads when big."""
    import os

    threads = min(8, os.cpu_count() or 1, max(1, -(-n // part)))
    if threads <= 1:
        work(0, n)
        return
    from concurrent.futures import ThreadPoolExecutor

    bounds = np.linspace(0, n, threads + 1).astype(np.int64)
    with ThreadPoolExecutor(threads) as pool:
        for future in [pool.submit(work, int(a), int(b))
                       for a, b in zip(bounds[:-1], bounds[1:])]:
            future.result()


def lookup(table: np.ndarray, codes: np.ndarray) -> np.ndarray:
    """``table[codes]`` for a 16-entry table over the face codes, in parts."""
    out = np.empty(codes.shape, dtype=table.dtype)

    def work(first: int, last: int) -> None:
        np.take(table, codes[first:last], out=out[first:last])

    _in_parts(codes.size, work)
    return out


class _Clamped:
    """``where(|k| <= tol, 0, k)`` read at the indices asked for only."""

    def __init__(self, values, tol: float):
        self.values = values
        self.tol = tol

    def __getitem__(self, index):
        v = self.values[index]
        return np.where(np.abs(v) <= self.tol, 0.0, v)


class SectionGeometry:
    """A polyMesh's topology with the per-face helpers sections need."""

    def __init__(self, points, face_vertices, face_offsets, owner, neighbour,
                 n_cells: int):
        self.points = np.asarray(points, dtype=float)
        self.face_vertices = np.asarray(face_vertices, dtype=np.int64)
        self.face_offsets = np.asarray(face_offsets, dtype=np.int64)
        self.owner = np.asarray(owner, dtype=np.int64)
        self.neighbour = np.asarray(neighbour, dtype=np.int64)
        self.n_cells = int(n_cells)
        self.n_faces = len(self.face_offsets) - 1
        self.n_internal = len(self.neighbour)
        self.sizes = np.diff(self.face_offsets)
        self._planes = {}
        self._derived = {}

    @classmethod
    def from_topology(cls, topology) -> 'SectionGeometry':
        return cls(topology.points, topology.face_vertices,
                   topology.face_offsets, topology.owner, topology.neighbour,
                   topology.n_cells)

    # -- the per-plane prefilter -------------------------------------------- #

    def face_codes(self, values, tol: float) -> np.ndarray:
        """Per face, the OR of its vertices' :data:`ON_OR_BELOW` ...
        :data:`BELOW` bits for the plane ``values == 0``.

        One byte per point and one ``bitwise_or.reduceat`` over the faces:
        every closed contact question below is a bit test on the result,
        so the whole mesh is read once per plane and the float work is
        left to the faces the plane actually meets.
        """
        values = np.asarray(values)
        code = np.full(len(values), ON_OR_BELOW | ON_OR_ABOVE, dtype=np.uint8)
        code[values > tol] = ON_OR_ABOVE | ABOVE
        code[values < -tol] = ON_OR_BELOW | BELOW
        out = np.zeros(self.n_faces, dtype=np.uint8)
        offsets = self.face_offsets

        def part(first: int, last: int) -> None:
            if last > first:
                start = int(offsets[first])
                out[first:last] = np.bitwise_or.reduceat(
                    code[self.face_vertices[start:int(offsets[last])]],
                    offsets[first:last] - start)

        _in_parts(self.n_faces, part)
        return out

    def plane(self, space: 'HalfSpace', tol: float):
        """``(space.kept(points), face_codes)``, computed once per plane."""
        key = (tuple(float(v) for v in space.normal), float(space.offset),
               int(space.keep), float(tol))
        found = self._planes.get(key)
        if found is None:
            value = space.kept(self.points)
            found = (value, self.face_codes(value, tol))
            self._planes[key] = found
        return found

    def remember(self, name, source, make):
        """``make()``, derived from the array ``source``, made once per
        ``source`` object; the entry keeps ``source`` so its id is never
        reused for another array while remembered."""
        key = (name, id(source))
        held = self._derived.get(key)
        if held is None or held[0] is not source:
            held = self._derived[key] = (source, make())
        return held[1]

    def crossed_faces(self, codes: np.ndarray, bias: int) -> np.ndarray:
        """Sorted ids of the faces the plane crosses (see ``loops``),
        remembered per ``codes`` array."""
        side = 1 if bias > 0 else -1
        return self.remember(('crossed', side), codes, lambda: np.flatnonzero(
            lookup(_CROSSED[side], codes)))

    def cells_of_faces(self, faces) -> np.ndarray:
        """Cells (bool) on either side of any of ``faces`` (bool or ids)."""
        faces = np.asarray(faces)
        if faces.dtype == bool:
            faces = np.flatnonzero(faces)
        chosen = np.zeros(self.n_cells, dtype=bool)
        chosen[self.owner[faces]] = True
        if self.n_internal:
            chosen[self.neighbour[faces[faces < self.n_internal]]] = True
        return chosen

    def faces_of_cells(self, cells) -> np.ndarray:
        """Ids of the faces with a side in ``cells`` (bool)."""
        cells = np.asarray(cells, dtype=bool)
        touch = cells[self.owner]
        if self.n_internal:
            touch[:self.n_internal] |= cells[self.neighbour]
        return np.flatnonzero(touch)

    def cell_range_of(self, cells, values) -> tuple[np.ndarray, np.ndarray]:
        """Min and max of ``values`` over the vertices of each of ``cells``
        (sorted ids), reading only those cells' faces."""
        cells = np.asarray(cells, dtype=np.int64)
        lo = np.full(len(cells), np.inf)
        hi = np.full(len(cells), -np.inf)
        if not len(cells):
            return lo, hi
        mask = np.zeros(self.n_cells, dtype=bool)
        mask[cells] = True
        faces = self.faces_of_cells(mask)
        sizes = self.sizes[faces]
        gathered = np.asarray(values)[self.face_vertices[
            _ranges(self.face_offsets[faces], sizes)]]
        starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
        fmin = np.minimum.reduceat(gathered, starts)
        fmax = np.maximum.reduceat(gathered, starts)
        own = self.owner[faces]
        side = mask[own]
        where = np.searchsorted(cells, own[side])
        np.minimum.at(lo, where, fmin[side])
        np.maximum.at(hi, where, fmax[side])
        internal = faces < self.n_internal
        nei = self.neighbour[faces[internal]]
        side = mask[nei]
        where = np.searchsorted(cells, nei[side])
        np.minimum.at(lo, where, fmin[internal][side])
        np.maximum.at(hi, where, fmax[internal][side])
        return lo, hi

    # -- helpers ------------------------------------------------------------ #

    def face_reduce(self, values, ufunc) -> np.ndarray:
        """``ufunc.reduceat`` of a per-point array over each face's vertices."""
        return ufunc.reduceat(np.asarray(values)[self.face_vertices],
                              self.face_offsets[:-1])

    def cell_range(self, values) -> tuple[np.ndarray, np.ndarray]:
        """Per-cell min and max of a per-point array over the cell's vertices.

        A full-mesh pass: the modes ask :meth:`face_codes` and
        :meth:`cell_range_of` instead."""
        fmin = self.face_reduce(values, np.minimum)
        fmax = self.face_reduce(values, np.maximum)
        cmin = np.full(self.n_cells, np.inf)
        cmax = np.full(self.n_cells, -np.inf)
        np.minimum.at(cmin, self.owner, fmin)
        np.maximum.at(cmax, self.owner, fmax)
        if self.n_internal:
            np.minimum.at(cmin, self.neighbour, fmin[:self.n_internal])
            np.maximum.at(cmax, self.neighbour, fmax[:self.n_internal])
        return cmin, cmax

    def cell_faces(self, cells) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``(cell, face, reversed)`` for every face of the given cells."""
        chosen = np.zeros(self.n_cells, dtype=bool)
        chosen[np.asarray(cells, dtype=np.int64)] = True
        own = np.flatnonzero(chosen[self.owner])
        nei = np.flatnonzero(chosen[self.neighbour]) if self.n_internal else \
            np.zeros(0, np.int64)
        cell = np.concatenate([self.owner[own], self.neighbour[nei]])
        face = np.concatenate([own, nei])
        flip = np.concatenate([np.zeros(len(own), bool), np.ones(len(nei), bool)])
        order = np.lexsort((face, cell))
        return cell[order], face[order], flip[order]

    def face_points(self, face: int, reverse: bool = False) -> np.ndarray:
        verts = self.face_vertices[self.face_offsets[face]:
                                   self.face_offsets[face + 1]]
        if reverse:
            verts = verts[::-1]
        return self.points[verts]

    # -- exterior of a cell set -------------------------------------------- #

    def exterior(self, selected: np.ndarray) -> Polygons:
        """The faces of a cell set with exactly one side in it, turned
        outward and carrying that side's cell."""
        selected = np.asarray(selected, dtype=bool)
        own = selected[self.owner]
        nei = np.zeros(self.n_faces, dtype=bool)
        if self.n_internal:
            nei[:self.n_internal] = selected[self.neighbour]
        faces = np.flatnonzero(own != nei)
        flip = ~own[faces]
        cells = np.where(flip, np.concatenate(
            [self.neighbour, np.zeros(self.n_faces - self.n_internal,
                                      np.int64)])[faces], self.owner[faces])
        sizes = self.sizes[faces]
        slots = _ranges(self.face_offsets[faces], sizes)
        offsets = np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64)
        if flip.any():
            # reverse the slots of flipped faces within their own range
            local = np.arange(len(slots), dtype=np.int64) - np.repeat(
                offsets[:-1], sizes)
            rev = np.repeat(flip, sizes)
            size_rep = np.repeat(sizes, sizes)
            start_rep = np.repeat(self.face_offsets[faces], sizes)
            slots = np.where(rev, start_rep + size_rep - 1 - local, slots)
        vertices = self.face_vertices[slots]
        used, inverse = np.unique(vertices, return_inverse=True)
        return Polygons(self.points[used], offsets, inverse.astype(np.int64),
                        cells.astype(np.int64))

    # -- section loops ------------------------------------------------------ #

    def loops(self, space: HalfSpace, tol: float, *, bias: int = 1,
              cells=None, value=None, codes=None) -> Loops:
        """The closed section loops of ``space``'s plane through the cells.

        ``bias`` +1 counts a vertex within ``tol`` of the plane as on its
        positive (``space.kept`` >= 0) side, -1 as on the negative side.
        ``cells`` restricts the loops to those cells (a boolean mask).
        Loops of zero area are kept -- the caller decides about contacts.
        ``value`` and ``codes`` are the plane's :meth:`plane` pair when the
        caller has them; only the faces the plane crosses are read further.
        """
        if value is None or codes is None:
            if value is None:
                value, codes = self.plane(space, tol)
            else:
                codes = self.face_codes(value, tol)
        k = value
        faces = self.crossed_faces(codes, bias)
        if cells is not None:
            wanted = np.asarray(cells, dtype=bool)
            touch = wanted[self.owner[faces]]
            internal = faces < self.n_internal
            touch[internal] |= wanted[self.neighbour[faces[internal]]]
            faces = faces[touch]
        empty = Loops(np.zeros(0, np.int64), np.zeros(1, np.int64),
                      np.zeros((0, 3)), np.zeros((0, 3)),
                      np.zeros(0, np.int64))
        if not len(faces):
            return empty
        fv = self.face_vertices
        sizes = self.sizes[faces]
        slots = _ranges(self.face_offsets[faces], sizes)
        local_face = np.repeat(np.arange(len(faces), dtype=np.int64), sizes)
        ends = np.cumsum(sizes) - 1
        nxt_slot = slots + 1
        nxt_slot[ends] = self.face_offsets[faces]
        a = fv[slots]
        b = fv[nxt_slot]
        if bias > 0:
            pos_a, pos_b = k[a] >= -tol, k[b] >= -tol
        else:
            pos_a, pos_b = k[a] > tol, k[b] > tol
        change = pos_a != pos_b
        ca, cb = a[change], b[change]
        entry = pos_b[change]                      # - -> + along the face
        clamped = _Clamped(k, tol)
        n_points = len(self.points)
        lo = np.minimum(ca, cb)
        hi = np.maximum(ca, cb)
        ckey = lo * n_points + hi
        counts = np.bincount(local_face[change], minlength=len(faces))
        first = np.concatenate([[0], np.cumsum(counts)[:-1]])

        seg_face, seg_start, seg_end = [], [], []
        simple = counts == 2
        if simple.any():
            i0 = first[simple]
            i1 = i0 + 1
            e0 = entry[i0]
            seg_face.append(faces[simple])
            seg_start.append(np.where(e0, ckey[i0], ckey[i1]))
            seg_end.append(np.where(e0, ckey[i1], ckey[i0]))
        many = np.flatnonzero(counts > 2)
        extra_face, extra_start, extra_end = [], [], []
        for index in many:
            f = faces[index]
            rows = np.arange(first[index], first[index] + counts[index])
            ea, eb = ca[rows], cb[rows]
            ta = clamped[ea] / (clamped[ea] - clamped[eb])
            xyz = self.points[ea] + ta[:, None] * (self.points[eb]
                                                   - self.points[ea])
            m = polygon_area_vectors(
                self.points, np.array([0, self.sizes[f]]),
                fv[self.face_offsets[f]:self.face_offsets[f + 1]])[0]
            direction = np.cross(m, space.normal * space.keep)
            order = np.argsort(xyz @ direction, kind='stable')
            for p, q in zip(order[0::2], order[1::2]):
                p_row, q_row = rows[p], rows[q]
                if entry[p_row] == entry[q_row]:
                    # not an entry/exit pair: a warped face; take it as it is
                    pass
                if entry[p_row] or not entry[q_row]:
                    s_key, e_key = ckey[p_row], ckey[q_row]
                else:
                    s_key, e_key = ckey[q_row], ckey[p_row]
                extra_face.append(f)
                extra_start.append(s_key)
                extra_end.append(e_key)
        if extra_face:
            seg_face.append(np.array(extra_face, np.int64))
            seg_start.append(np.array(extra_start, np.int64))
            seg_end.append(np.array(extra_end, np.int64))
        seg_face = np.concatenate(seg_face)
        seg_start = np.concatenate(seg_start)
        seg_end = np.concatenate(seg_end)

        # owner side as stored, neighbour side reversed
        internal = seg_face < self.n_internal
        cell = np.concatenate([self.owner[seg_face],
                               self.neighbour[seg_face[internal]]])
        start = np.concatenate([seg_start, seg_end[internal]])
        end = np.concatenate([seg_end, seg_start[internal]])
        if cells is not None:
            keep = wanted[cell]
            cell, start, end = cell[keep], start[keep], end[keep]
        if not len(cell):
            return empty

        keys, inverse = np.unique(np.concatenate([start, end]),
                                  return_inverse=True)
        si = inverse[:len(start)]
        ei = inverse[len(start):]
        ka = keys // n_points
        kb = keys % n_points
        t = clamped[ka] / (clamped[ka] - clamped[kb])
        xyz = self.points[ka] + t[:, None] * (self.points[kb]
                                              - self.points[ka])

        # chain: the segment whose start is this one's end, in the same cell
        width = np.int64(len(keys))
        start_key = cell * width + si
        order = np.argsort(start_key, kind='stable')
        sorted_keys = start_key[order]
        target = cell * width + ei
        where = np.searchsorted(sorted_keys, target)
        where = np.minimum(where, len(sorted_keys) - 1)
        found = sorted_keys[where] == target
        duplicate = np.zeros(len(cell), bool)
        if len(sorted_keys) > 1:
            dup = sorted_keys[1:] == sorted_keys[:-1]
            duplicate[order[1:][dup]] = True
            duplicate[order[:-1][dup]] = True
        nxt = order[where]
        bad = ~found | duplicate
        broken = np.zeros(0, np.int64)
        if bad.any():
            broken = np.unique(cell[bad])
            ok = ~np.isin(cell, broken)
            # rebuild on the good cells only
            remap = np.cumsum(ok) - 1
            cell, si, ei, nxt = cell[ok], si[ok], ei[ok], remap[nxt[ok]]
            if not len(cell):
                empty.broken_cells = broken
                return empty

        # label each cycle by its smallest member (pointer doubling)
        n = len(cell)
        leader = np.arange(n, dtype=np.int64)
        jump = nxt.copy()
        longest = int(np.unique(cell, return_counts=True)[1].max())
        for _ in range(max(1, int(np.ceil(np.log2(longest + 1))) + 1)):
            leader = np.minimum(leader, leader[jump])
            jump = jump[jump]
        heads = np.flatnonzero(leader == np.arange(n))
        rank = np.zeros(n, dtype=np.int64)
        current = heads.copy()
        step = 0
        active = np.ones(len(heads), dtype=bool)
        while active.any():
            rank[current[active]] = step
            current = nxt[current]
            active &= current != heads
            step += 1
            if step > n:
                break
        head_of = leader
        order = np.lexsort((rank, head_of))
        loop_sizes = np.bincount(np.searchsorted(heads, head_of[order]),
                                 minlength=len(heads))
        offsets = np.concatenate([[0], np.cumsum(loop_sizes)]).astype(np.int64)
        loop_points = xyz[si[order]]
        areas = polygon_area_vectors(loop_points, offsets,
                                     np.arange(len(loop_points)))
        return Loops(cell[heads], offsets, loop_points, areas, broken)
