#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Which cells a plane passes through, and how thick they are across it.

Plan 37 UF7 (DP-1046). *Cut cells* is its own mode now, not *Clip* with a
tick box: the cells the plane meets, whole, on both sides. The rule is the
plan's contact rule (§4.2, UF10 step 2) with a scale-aware tolerance:

* a cell is in when its closed volume meets the plane -- its lowest vertex is
  at or below the plane and its highest at or above, each within
  ``tolerance``. A plane lying on a face therefore takes both cells that
  share the face; one that only touches an edge or a vertex takes the cells
  there as well (they add no area to a slice, UF10's concern, not this);
* the other enabled planes mask: a cell stays only while some vertex of it is
  on each of their kept sides.

For a convex cell the vertex range is exact. A non-convex polyhedron whose
vertices straddle the plane can miss it in between; UF10's worker owns the
exact test, this is the viewport's.

The step the −/+ buttons take by default is the *cell-scale step*: the median
of the positive spans, projected on n, of the cells the plane meets. It is an
approximate step, not a promise to visit every cell of a graded mesh.

Plain numpy on a cell/point layout (``offsets``, ``connectivity`` as VTK
holds them), so the worker can share it.
"""
from __future__ import annotations

import time

import numpy as np

__all__ = ['cell_ranges', 'cell_means', 'contact_mask', 'cut_cells_mask', 'cell_scale_step',
           'tolerance_for', 'signed_distances', 'projected_ranges',
           'scale_step_from_ranges']

#: Plan 39 VI14d. numpy holds the GIL through a fancy index or a ``reduceat``
#: for tens of milliseconds on a multi-million-cell mesh, and a worker doing
#: that starves the GUI thread of every Python callback meanwhile (measured
#: 31 ms for one ``cell_ranges`` of 6.6 M cells). The work is done in slices
#: and the GIL is handed over between them.
SLICE_CELLS = 200_000
SLICE_POINTS = 1_000_000


def _yield_gil() -> None:
    time.sleep(0)


#: A contact within this fraction of the model's diagonal is a contact.
RELATIVE_TOLERANCE = 1e-9


def tolerance_for(points) -> float:
    points = np.asarray(points)
    if not len(points):
        return 0.0
    low = high = None
    for start in range(0, len(points), SLICE_POINTS):
        part = points[start:start + SLICE_POINTS].astype(float, copy=False)
        part_low, part_high = part.min(axis=0), part.max(axis=0)
        low = part_low if low is None else np.minimum(low, part_low)
        high = part_high if high is None else np.maximum(high, part_high)
        _yield_gil()
    diagonal = float(np.linalg.norm(high - low))
    return RELATIVE_TOLERANCE * (diagonal or 1.0)


def signed_distances(points, origin, normal):
    """``normal . (x - origin)`` for every point; *normal* need not be unit."""
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    points = np.asarray(points)
    origin = np.asarray(origin, dtype=float)
    if len(points) <= SLICE_POINTS:
        return (points.astype(float, copy=False) - origin) @ normal
    out = np.empty(len(points))
    for start in range(0, len(points), SLICE_POINTS):
        part = points[start:start + SLICE_POINTS].astype(float, copy=False)
        out[start:start + SLICE_POINTS] = (part - origin) @ normal
        _yield_gil()
    return out


def cell_ranges(values, offsets, connectivity):
    """``(low, high)`` of *values* over each cell's points.

    A cell with no points gets ``(inf, -inf)`` and so meets nothing.
    """
    values = np.asarray(values, dtype=float)
    offsets = np.asarray(offsets, dtype=np.int64)
    connectivity = np.asarray(connectivity, dtype=np.int64)
    count = len(offsets) - 1
    low = np.full(count, np.inf)
    high = np.full(count, -np.inf)
    if count <= 0 or not len(connectivity):
        return low, high
    for first in range(0, count, SLICE_CELLS):
        last = min(count, first + SLICE_CELLS)
        base = offsets[first]
        local = offsets[first:last + 1] - base
        filled = np.diff(local) > 0
        if not filled.any():
            continue
        per = values[connectivity[base:offsets[last]]]
        starts = local[:-1][filled]
        low[first:last][filled] = np.minimum.reduceat(per, starts)
        high[first:last][filled] = np.maximum.reduceat(per, starts)
        _yield_gil()
    return low, high


def contact_mask(distances, offsets, connectivity, tolerance):
    """The cells whose closed volume meets the plane the distances are to."""
    low, high = cell_ranges(distances, offsets, connectivity)
    return (low <= tolerance) & (high >= -tolerance)


def cut_cells_mask(points, offsets, connectivity, planes, tolerance=None,
                   rule='any'):
    """Cut cells of the first of *planes*, masked by the rest.

    *planes* are ``(origin, normal)`` with the normal pointing into the kept
    half, as every clip in the viewport keeps. *rule* (Plan 39 VI10b, F2g) is
    how a mask keeps a cell: ``'any'`` part of it on the kept side,
    ``'centroid'`` (the mean of its points) on it, or ``'entire'`` cell on
    it.
    """
    if not planes:
        return np.zeros(len(offsets) - 1, dtype=bool)
    if tolerance is None:
        tolerance = tolerance_for(points)
    origin, normal = planes[0]
    mask = contact_mask(signed_distances(points, origin, normal),
                        offsets, connectivity, tolerance)
    for origin, normal in planes[1:]:
        distances = signed_distances(points, origin, normal)
        low, high = cell_ranges(distances, offsets, connectivity)
        if rule == 'entire':
            mask &= low >= -tolerance
        elif rule == 'centroid':
            mask &= cell_means(distances, offsets, connectivity) > tolerance
        else:
            mask &= high >= -tolerance
    return mask


def cell_means(values, offsets, connectivity):
    """The mean of *values* over each cell's points (``-inf`` for a cell with
    none, which is never inside)."""
    values = np.asarray(values, dtype=float)
    offsets = np.asarray(offsets, dtype=np.int64)
    count = len(offsets) - 1
    mean = np.full(max(count, 0), -np.inf)
    sizes = np.diff(offsets)
    filled = sizes > 0
    if count > 0 and filled.any():
        sums = np.add.reduceat(
            values[np.asarray(connectivity, dtype=np.int64)[:offsets[-1]]],
            offsets[:-1][filled])
        mean[filled] = sums / sizes[filled]
    return mean


def projected_ranges(points, offsets, connectivity, normal):
    """``(low, high)`` of each cell's points projected on the unit *normal*.

    Independent of where the plane is along n, so a caller holding them can
    find the step at any offset with `scale_step_from_ranges` without
    reading the cells again.
    """
    return cell_ranges(signed_distances(points, (0.0, 0.0, 0.0), normal),
                       offsets, connectivity)


def scale_step_from_ranges(low, high, offset, tolerance):
    """`cell_scale_step` from `projected_ranges` and the plane's offset
    ``n . origin`` along the same unit normal."""
    found = []
    for first in range(0, len(low), SLICE_CELLS):
        part_low = low[first:first + SLICE_CELLS]
        part_high = high[first:first + SLICE_CELLS]
        met = ((part_low <= offset + tolerance)
               & (part_high >= offset - tolerance))
        spans = (part_high - part_low)[met]
        found.append(spans[spans > tolerance])
        _yield_gil()
    spans = np.concatenate(found) if found else np.empty(0)
    if not len(spans):
        return None
    return float(np.median(spans))


def cell_scale_step(points, offsets, connectivity, origin, normal,
                    tolerance=None):
    """The median positive span along n of the cells the plane meets.

    ``None`` when the plane meets no cell with any thickness across it; the
    caller then keeps its last step or falls back to the labelled domain
    step.
    """
    if tolerance is None:
        tolerance = tolerance_for(points)
    distances = signed_distances(points, origin, normal)
    low, high = cell_ranges(distances, offsets, connectivity)
    met = (low <= tolerance) & (high >= -tolerance)
    spans = (high - low)[met]
    spans = spans[spans > tolerance]
    if not len(spans):
        return None
    return float(np.median(spans))
