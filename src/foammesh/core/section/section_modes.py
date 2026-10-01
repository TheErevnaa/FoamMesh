#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The four section modes over :class:`SectionGeometry` (Plan 37 UF10).

See ``section_geometry`` for the contact rules. Every function returns a
:class:`ModeResult`: display polygons (each carrying its source cell), the
selected source cells where the mode has whole-cell semantics, and what, if
anything, was approximate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from foammesh.core.section.section_geometry import (
    ABOVE, BELOW, MEETS, ON_OR_ABOVE, HalfSpace, Loops, Polygons,
    SectionGeometry, _ranges, clip_polygon, lookup, polygon_area_vectors)

__all__ = ['ModeResult', 'Assembly', 'OutputCap', 'slice_section',
           'cut_cells_section', 'clip_section', 'whole_cells_section',
           'contact_cells', 'cell_scale_step', 'triangulate_contours']


class OutputCap(Exception):
    """A preflight count is over its limit: nothing is built.

    ``estimated``: the count is the prefilter's, before the polygons exist.
    """

    def __init__(self, quantity: str, limit: int, measured: int,
                 estimated: bool = False):
        super().__init__(f'{quantity} {measured:,} exceeds {limit:,}')
        self.quantity = quantity
        self.limit = int(limit)
        self.measured = int(measured)
        self.estimated = bool(estimated)


@dataclass
class ModeResult:
    polygons: Polygons
    #: whole-cell modes: the selected source cells; otherwise the cells the
    #: polygons came from
    cells: np.ndarray
    approximate: dict = field(default_factory=dict)
    triangulated: int = 0
    counts: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Polygon assembly
# --------------------------------------------------------------------------- #

def _area(points) -> np.ndarray:
    return polygon_area_vectors(points, [0, len(points)],
                                np.arange(len(points)))[0]


def _concave(points: np.ndarray, normal: np.ndarray, tol: float) -> bool:
    if len(points) <= 3:
        return False
    edges = np.roll(points, -1, axis=0) - points
    turns = np.cross(edges, np.roll(edges, -1, axis=0)) @ normal
    scale = float((edges * edges).sum(axis=1).max())
    return bool((turns < -tol * scale).any() and (turns > tol * scale).any())


def _concave_rows(block: Polygons, facing, tol) -> np.ndarray:
    """Per polygon: does it turn both ways about ``facing``?"""
    if not block.count:
        return np.zeros(0, bool)
    conn = block.connectivity
    offsets = block.offsets
    slots = np.arange(len(conn), dtype=np.int64)
    nxt = slots + 1
    nxt[offsets[1:] - 1] = offsets[:-1]
    edges = block.points[conn[nxt]] - block.points[conn]
    turns = np.cross(edges, edges[nxt]) @ facing
    scale = np.einsum('ij,ij->i', edges, edges)
    big = np.maximum.reduceat(scale, offsets[:-1])
    thresh = np.repeat(tol * big, np.diff(offsets))
    neg = np.logical_or.reduceat(turns < -thresh, offsets[:-1])
    pos = np.logical_or.reduceat(turns > thresh, offsets[:-1])
    return neg & pos


def triangulate_contours(contours, normal) -> list:
    """Triangles filling closed contours; loops turning against ``normal``
    are holes. Returns ``[(3, 3) array, ...]``, ``[]`` on failure."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkFiltersGeneral import vtkContourTriangulator

    contours = [np.asarray(c, float) for c in contours if len(c) >= 3]
    if not contours:
        return []
    xyz = np.concatenate(contours)
    data = vtkPolyData()
    points = vtkPoints()
    points.SetDataTypeToDouble()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(xyz), deep=True))
    data.SetPoints(points)
    lines = vtkCellArray()
    base = 0
    for contour in contours:
        n = len(contour)
        for i in range(n):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(base + i)
            lines.InsertCellPoint(base + (i + 1) % n)
        base += n
    data.SetLines(lines)
    triangles = vtkCellArray()
    ok = vtkContourTriangulator.TriangulateContours(
        data, 0, data.GetNumberOfLines(), triangles,
        tuple(float(v) for v in normal))
    if not ok or not triangles.GetNumberOfCells():
        return []
    conn = vtk_to_numpy(triangles.GetConnectivityArray()).astype(np.int64)
    out = []
    for tri in conn.reshape(-1, 3):
        corners = xyz[tri]
        if np.cross(corners[1] - corners[0],
                    corners[2] - corners[0]) @ normal < 0:
            corners = corners[::-1]
        out.append(corners)
    return out


@dataclass
class Assembly:
    """Polygons being gathered for one output, plus what was approximate."""

    polygons: list = field(default_factory=list)
    cells: list = field(default_factory=list)
    parts: list = field(default_factory=list)       # ready Polygons blocks
    triangulated: int = 0
    approximate: dict = field(default_factory=dict)

    def add(self, points, cell) -> None:
        self.polygons.append(np.asarray(points, float))
        self.cells.append(int(cell))

    def note(self, reason: str, count: int = 1) -> None:
        self.approximate[reason] = self.approximate.get(reason, 0) + count

    def result(self) -> Polygons:
        return Polygons.concatenate(
            self.parts + [Polygons.from_lists(self.polygons, self.cells)])


def _mask_state(values_per_mask, starts, tol) -> np.ndarray:
    """Per polygon: 1 wholly kept, 0 wholly outside some mask, 2 straddles."""
    state = np.ones(len(starts), dtype=np.int8)
    for value in values_per_mask:
        lo = np.minimum.reduceat(value, starts)
        hi = np.maximum.reduceat(value, starts)
        out = hi < -tol
        straddle = lo < -tol
        state = np.where(out, 0, np.where(straddle & (state != 0), 2, state))
    return state


def emit_loops(assembly: Assembly, loops: Loops, facing, masks, tol: float,
               area_tol: float, *, reverse: bool) -> None:
    """Turn section loops into polygons facing ``facing``, masked.

    A loop's own sense (``Loops``) turns *against* the kept side's inward
    normal for an outer boundary and with it for a hole. ``reverse`` flips
    every loop first. Zero-area loops are dropped; a cell whose loops hold a
    hole, or a polygon that ends up concave, is triangulated.
    """
    if not loops.count:
        return
    facing = np.asarray(facing, float)
    sign = -1.0 if reverse else 1.0
    signed = sign * (loops.areas @ facing)
    live = np.linalg.norm(loops.areas, axis=1) > area_tol
    hole_cells = np.unique(loops.cells[live & (signed < 0)])
    starts = loops.offsets[:-1]
    state = _mask_state([m.kept(loops.points) for m in masks], starts, tol) \
        if masks else np.ones(loops.count, np.int8)
    plain = live & (state == 1) & ~np.isin(loops.cells, hole_cells)
    sizes = np.diff(loops.offsets)
    if plain.any():
        index = np.flatnonzero(plain)
        slots = _ranges(loops.offsets[index], sizes[index])
        if reverse:
            local = np.arange(len(slots)) - np.repeat(
                np.concatenate([[0], np.cumsum(sizes[index])[:-1]]),
                sizes[index])
            slots = np.repeat(loops.offsets[index] + sizes[index] - 1,
                              sizes[index]) - local
        block = Polygons(loops.points,
                         np.concatenate([[0], np.cumsum(sizes[index])])
                         .astype(np.int64), slots, loops.cells[index])
        concave = _concave_rows(block, facing, 1e-9)
        if concave.any():
            for row in np.flatnonzero(concave):
                corners = block.points[block.connectivity[
                    block.offsets[row]:block.offsets[row + 1]]]
                _fill(assembly, [corners], facing, int(block.cells[row]))
            block = block.select(~concave)
        if block.count:
            assembly.parts.append(block.compacted())
    by_cell: dict = {}
    for index in np.flatnonzero(live & ~plain & (state != 0)):
        corners = loops.loop(index)
        by_cell.setdefault(int(loops.cells[index]), []).append(
            corners[::-1] if reverse else corners)
    for cell, contours in by_cell.items():
        if masks:
            contours = [clip_polygon(c, masks, tol) for c in contours]
            contours = [c for c in contours if len(c) >= 3
                        and np.linalg.norm(_area(c)) > area_tol]
        if not contours:
            continue
        hole = any(_area(c) @ facing < 0 for c in contours)
        if hole or any(_concave(c, facing, 1e-9) for c in contours):
            _fill(assembly, contours, facing, cell)
        else:
            for c in contours:
                assembly.add(c, cell)


def _fill(assembly: Assembly, contours, facing, cell: int) -> None:
    triangles = triangulate_contours(contours, facing)
    if triangles:
        assembly.triangulated += 1
        for tri in triangles:
            assembly.add(tri, cell)
    else:
        assembly.note('section_not_triangulated')
        for c in contours:
            assembly.add(c, cell)


# --------------------------------------------------------------------------- #
# Selection helpers
# --------------------------------------------------------------------------- #

def _table(test) -> np.ndarray:
    return np.array([bool(test(code)) for code in range(16)])


#: Per face code: one lookup over the faces instead of a chain of bit ops.
_LOOKUP = {
    'meets': _table(lambda c: c & MEETS == MEETS),
    ABOVE: _table(lambda c: c & ABOVE), BELOW: _table(lambda c: c & BELOW),
    ON_OR_ABOVE: _table(lambda c: c & ON_OR_ABOVE),
    'flat': _table(lambda c: not c & (ABOVE | BELOW)),
}


def _has(codes, bits) -> np.ndarray:
    return lookup(_LOOKUP[bits], codes)


def meeting_faces(codes) -> np.ndarray:
    """Faces whose closed polygon meets the plane (see ``face_codes``)."""
    return lookup(_LOOKUP['meets'], codes)


def contact_cells(geometry: SectionGeometry, value, tol: float,
                  codes=None) -> np.ndarray:
    """Cells whose closed volume meets the plane ``value == 0``.

    Exactly the cells beside a face that meets it: a cell's vertex range
    spans the closed plane iff one of its faces' does (its vertices are
    joined by its faces' edges).
    """
    if codes is None:
        codes = geometry.face_codes(value, tol)
    return geometry.remember('contact', codes, lambda: (
        geometry.cells_of_faces(meeting_faces(codes))))


def cell_scale_step(geometry: SectionGeometry, value, tol: float,
                    codes=None):
    """Median positive span along the normal of the cells the plane meets."""
    cells = np.flatnonzero(contact_cells(geometry, value, tol, codes))
    cmin, cmax = geometry.cell_range_of(cells, value)
    span = cmax - cmin
    span = span[span > tol]
    return float(np.median(span)) if span.size else None


def segment_count(geometry: SectionGeometry, codes, bias: int) -> int:
    """Section loop corners of a plane: one per side of each face it
    crosses -- the exact point count of its loops before any mask."""
    crossed = geometry.crossed_faces(codes, bias)
    return int(crossed.size
               + np.searchsorted(crossed, geometry.n_internal))


def output_estimate(polygons: int, points: int, connectivity: int,
                    limits: dict) -> int:
    """Bytes the written section takes, counted as the worker counts them
    (``cell_data_bytes``: the colour arrays' bytes per polygon)."""
    per_polygon = 8 + int(limits.get('cell_data_bytes', 0))
    return 12 * points + 8 * (connectivity + polygons) + per_polygon * polygons


def early_caps(limits: dict, *, polygons: int, points: int,
               connectivity: int) -> None:
    """The output caps, checked on the prefilter's counts before any
    polygon is built. The built output is checked again afterwards."""
    _preflight('polygons', polygons, limits, estimated=True)
    _preflight('points', points, limits, estimated=True)
    _preflight('output_bytes', output_estimate(
        polygons, points, connectivity, limits), limits, estimated=True)


def _preflight(quantity: str, measured: int, limits: dict,
               estimated: bool = False) -> None:
    limit = limits.get(quantity)
    if limit is not None and measured > limit:
        raise OutputCap(quantity, limit, measured, estimated)


def _clipped_volumes(geometry: SectionGeometry, cells: np.ndarray, spaces,
                     tol: float) -> np.ndarray:
    """Volume of each given cell inside every kept half-space (exact)."""
    wanted = np.zeros(geometry.n_cells, dtype=bool)
    wanted[cells] = True
    volume = np.zeros(geometry.n_cells)
    cell, face, flip = geometry.cell_faces(cells)
    for c, f, r in zip(cell, face, flip):
        piece = clip_polygon(geometry.face_points(int(f), bool(r)), spaces, tol)
        if len(piece) >= 3:
            volume[c] += piece[0] @ _area(piece) / 3.0
    for i, space in enumerate(spaces):
        others = [s for j, s in enumerate(spaces) if j != i]
        value, codes = geometry.plane(space, tol)
        loops = geometry.loops(space, tol, bias=-1, cells=wanted,
                               value=value, codes=codes)
        for index in range(loops.count):
            piece = clip_polygon(loops.loop(index), others, tol)
            if len(piece) >= 3:
                volume[loops.cells[index]] += piece[0] @ _area(piece) / 3.0
    return volume[cells]


# --------------------------------------------------------------------------- #
# The modes
# --------------------------------------------------------------------------- #

def slice_section(geometry: SectionGeometry, active: HalfSpace, masks,
                  tol: float, limits: dict) -> ModeResult:
    """Section polygons of the active plane, masked, facing its ``+n``."""
    plane = HalfSpace(active.normal, active.offset, 1)
    value, codes = geometry.plane(plane, tol)
    contact = contact_cells(geometry, value, tol, codes)
    n_contact = int(np.count_nonzero(contact))
    corners = segment_count(geometry, codes, 1)
    early_caps(limits, polygons=n_contact, points=corners,
               connectivity=corners)
    area_tol = tol * tol
    assembly = Assembly()
    # a vertex on the plane counts as on the +n side: a coincident internal
    # face is sliced once, by the cell below it
    loops = geometry.loops(plane, tol, bias=1, value=value, codes=codes)
    if loops.broken_cells.size:
        assembly.note('open_section_loops', int(loops.broken_cells.size))
    emit_loops(assembly, loops, active.normal, masks, tol, area_tol,
               reverse=True)
    # a boundary face lying on the plane with its cell above: nobody sliced it
    flat = geometry.n_internal + np.flatnonzero(
        lookup(_LOOKUP['flat'], codes[geometry.n_internal:]))
    if flat.size:
        owners = np.unique(geometry.owner[flat])
        cmin, _cmax = geometry.cell_range_of(owners, value)
        flat = flat[np.isin(geometry.owner[flat], owners[cmin >= -tol])]
        if flat.size:
            for f in flat:
                corners = geometry.face_points(int(f))
                if _area(corners) @ active.normal < 0:
                    corners = corners[::-1]
                if masks:
                    corners = clip_polygon(corners, masks, tol)
                    if len(corners) < 3:
                        continue
                assembly.add(corners, geometry.owner[f])
    polygons = assembly.result()
    return ModeResult(polygons, np.unique(polygons.cells),
                      assembly.approximate, assembly.triangulated,
                      {'contact_cells': n_contact})


def _masked_contacts(geometry, active, masks, tol, candidates) -> np.ndarray:
    """Of ``candidates`` (bool), the cells whose closed section by the active
    plane meets every mask's closed kept half-space -- decided on the
    section polygons themselves, so a concave cell is not taken on the
    strength of its vertices alone."""
    chosen = np.zeros(geometry.n_cells, dtype=bool)
    if not candidates.any():
        return chosen
    value, codes = geometry.plane(active, tol)
    for bias in (1, -1):
        loops = geometry.loops(active, tol, bias=bias, cells=candidates,
                               value=value, codes=codes)
        for index in range(loops.count):
            c = loops.cells[index]
            if chosen[c]:
                continue
            if len(clip_polygon(loops.loop(index), masks, tol)):
                chosen[c] = True
    return chosen


def cut_cells_section(geometry: SectionGeometry, active: HalfSpace, masks,
                      tol: float, limits: dict) -> ModeResult:
    """Every cell whose closed volume meets the active plane (masked), whole."""
    _value, codes = geometry.plane(active, tol)
    selected = geometry.cells_of_faces(meeting_faces(codes))
    if not masks:
        # the masks only remove cells: unmasked, this is the selection
        _preflight('selected_cells', int(selected.sum()), limits,
                   estimated=True)
    exact_checked = 0
    if masks and selected.any():
        sure = selected.copy()
        for mask in masks:
            _kept, mask_codes = geometry.plane(mask, tol)
            # some vertex on the kept side; no vertex outside it
            selected &= geometry.cells_of_faces(_has(mask_codes, ON_OR_ABOVE))
            sure &= ~geometry.cells_of_faces(_has(mask_codes, BELOW))
        ambiguous = selected & ~sure
        exact_checked = int(ambiguous.sum())
        if exact_checked:
            _preflight('exact_cells', exact_checked, limits)
            selected = sure | _masked_contacts(geometry, active, masks, tol,
                                               ambiguous)
    return _whole(geometry, selected, limits,
                  {'exact_checked_cells': exact_checked})


def _whole(geometry, selected, limits, counts) -> ModeResult:
    n_selected = int(selected.sum())
    _preflight('selected_cells', n_selected, limits)
    own = selected[geometry.owner]
    nei = np.zeros(geometry.n_faces, dtype=bool)
    if geometry.n_internal:
        nei[:geometry.n_internal] = selected[geometry.neighbour]
    faces = own != nei
    n_faces = int(faces.sum())
    _preflight('polygons', n_faces, limits)
    # the outlines alone, before the shared points are known
    _preflight('output_bytes', output_estimate(
        n_faces, 0, int(geometry.sizes[faces].sum()), limits), limits,
        estimated=True)
    polygons = geometry.exterior(selected)
    counts = dict(counts, selected_cells=n_selected)
    return ModeResult(polygons, np.flatnonzero(selected), {}, 0, counts)


def frozen_cells_section(geometry: SectionGeometry, selected, limits: dict
                         ) -> ModeResult:
    """UF11. Exactly the frozen cells, whole -- the planes choose nothing."""
    return _whole(geometry, np.asarray(selected, dtype=bool), limits,
                  {'frozen_cells': int(np.count_nonzero(selected))})


def whole_cells_section(geometry: SectionGeometry, spaces, tol: float,
                        limits: dict) -> ModeResult:
    """Every cell with positive volume in the kept region, whole (stepped).

    A plane lying on a face therefore keeps the cell on its kept side only.
    """
    selected = np.ones(geometry.n_cells, dtype=bool)
    touching = np.zeros(geometry.n_cells, dtype=bool)
    score = None
    for space in spaces:
        kept, codes = geometry.plane(space, tol)
        # some vertex strictly on the kept side
        selected &= geometry.cells_of_faces(_has(codes, ABOVE))
        if len(spaces) > 1:
            touching |= geometry.cells_of_faces(meeting_faces(codes))
        score = kept if score is None else np.minimum(score, kept)
    if len(spaces) == 1:
        _preflight('selected_cells', int(selected.sum()), limits,
                   estimated=True)
    exact_checked = 0
    if len(spaces) > 1 and selected.any():
        # A selected cell with no vertex strictly inside every half-space
        # has, for some plane, vertices on both sides of it, so it touches
        # that plane: only the cells touching a plane are looked at.
        candidates = np.flatnonzero(selected & touching)
        _smin, smax = geometry.cell_range_of(candidates, score)
        ambiguous = np.zeros(geometry.n_cells, dtype=bool)
        ambiguous[candidates[smax <= tol]] = True
        exact_checked = int(ambiguous.sum())
        if exact_checked:
            _preflight('exact_cells', exact_checked, limits)
            cells = np.flatnonzero(ambiguous)
            volumes = _clipped_volumes(geometry, cells, spaces, tol)
            # a positive volume beyond rounding of the cell's own size
            lo = np.full((len(cells), 3), np.inf)
            hi = np.full((len(cells), 3), -np.inf)
            cell, face, _flip = geometry.cell_faces(cells)
            where = np.searchsorted(cells, cell)
            for f, w in zip(face, where):
                pts = geometry.face_points(int(f))
                lo[w] = np.minimum(lo[w], pts.min(axis=0))
                hi[w] = np.maximum(hi[w], pts.max(axis=0))
            size = np.linalg.norm(hi - lo, axis=1)
            keep = volumes > 1e-9 * size ** 3
            selected[cells[~keep]] = False
    return _whole(geometry, selected, limits,
                  {'exact_checked_cells': exact_checked})


def clip_section(geometry: SectionGeometry, spaces, tol: float,
                 limits: dict) -> ModeResult:
    """The surface of the mesh inside every kept half-space (smooth).

    The kept part of the boundary, plus on each plane the section of the
    mesh masked by the other half-spaces, all facing out of the kept region.
    A boundary face lying on a plane is left to that plane's cap, which the
    kept cell behind it supplies.
    """
    area_tol = tol * tol
    assembly = Assembly()
    boundary = np.arange(geometry.n_internal, geometry.n_faces)
    status = np.ones(boundary.size, dtype=np.int8)       # 1 in, 0 out, 2 cut
    planes = [geometry.plane(space, tol) for space in spaces]
    kept_values = [value for value, _codes in planes]
    for _value, codes in planes:
        face = codes[geometry.n_internal:]
        on_plane = lookup(_LOOKUP['flat'], face)
        out = ~_has(face, ON_OR_ABOVE) | on_plane
        cut = _has(face, BELOW)
        status = np.where(out, 0, np.where(cut & (status != 0), 2, status))
    kept_faces = boundary[status != 0]
    estimate = int(kept_faces.size)
    corners = int(geometry.sizes[kept_faces].sum())
    for value, codes in planes:
        estimate += int(contact_cells(geometry, value, tol, codes).sum())
        corners += segment_count(geometry, codes, -1)
    early_caps(limits, polygons=estimate, points=corners,
               connectivity=corners)
    whole = boundary[status == 1]
    if whole.size:
        sizes = geometry.sizes[whole]
        slots = _ranges(geometry.face_offsets[whole], sizes)
        vertices = geometry.face_vertices[slots]
        used, inverse = np.unique(vertices, return_inverse=True)
        assembly.parts.append(Polygons(
            geometry.points[used],
            np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64),
            inverse.astype(np.int64), geometry.owner[whole]))
    for f in boundary[status == 2]:
        piece = clip_polygon(geometry.face_points(int(f)), spaces, tol)
        if len(piece) >= 3 and np.linalg.norm(_area(piece)) > area_tol:
            assembly.add(piece, geometry.owner[f])
    for i, space in enumerate(spaces):
        others = [s for j, s in enumerate(spaces) if j != i]
        loops = geometry.loops(space, tol, bias=-1, value=kept_values[i],
                               codes=planes[i][1])
        if loops.broken_cells.size:
            assembly.note('open_section_loops', int(loops.broken_cells.size))
        # a cap faces out of the kept region: its loops' own sense already
        emit_loops(assembly, loops, -space.inward, others, tol, area_tol,
                   reverse=False)
    polygons = assembly.result()
    return ModeResult(polygons, np.unique(polygons.cells),
                      assembly.approximate, assembly.triangulated, {})
