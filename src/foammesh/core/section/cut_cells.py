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

import numpy as np

__all__ = ['cell_ranges', 'contact_mask', 'cut_cells_mask', 'cell_scale_step',
           'tolerance_for', 'signed_distances', 'projected_ranges',
           'scale_step_from_ranges']

#: A contact within this fraction of the model's diagonal is a contact.
RELATIVE_TOLERANCE = 1e-9


def tolerance_for(points) -> float:
    points = np.asarray(points, dtype=float)
    if not len(points):
        return 0.0
    diagonal = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
    return RELATIVE_TOLERANCE * (diagonal or 1.0)


def signed_distances(points, origin, normal):
    """``normal . (x - origin)`` for every point; *normal* need not be unit."""
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    return (np.asarray(points, dtype=float)
            - np.asarray(origin, dtype=float)) @ normal


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
    sizes = np.diff(offsets)
    filled = sizes > 0
    starts = offsets[:-1][filled]
    per = values[connectivity[:offsets[-1]]]
    low[filled] = np.minimum.reduceat(per, starts)
    high[filled] = np.maximum.reduceat(per, starts)
    return low, high


def contact_mask(distances, offsets, connectivity, tolerance):
    """The cells whose closed volume meets the plane the distances are to."""
    low, high = cell_ranges(distances, offsets, connectivity)
    return (low <= tolerance) & (high >= -tolerance)


def cut_cells_mask(points, offsets, connectivity, planes, tolerance=None):
    """Cut cells of the first of *planes*, masked by the rest.

    *planes* are ``(origin, normal)`` with the normal pointing into the kept
    half, as every clip in the viewport keeps.
    """
    if not planes:
        return np.zeros(len(offsets) - 1, dtype=bool)
    if tolerance is None:
        tolerance = tolerance_for(points)
    origin, normal = planes[0]
    mask = contact_mask(signed_distances(points, origin, normal),
                        offsets, connectivity, tolerance)
    for origin, normal in planes[1:]:
        _low, high = cell_ranges(signed_distances(points, origin, normal),
                                 offsets, connectivity)
        mask &= high >= -tolerance
    return mask


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
    met = (low <= offset + tolerance) & (high >= offset - tolerance)
    spans = (high - low)[met]
    spans = spans[spans > tolerance]
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
