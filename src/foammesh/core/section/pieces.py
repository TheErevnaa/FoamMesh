#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""How several planes combine, which whole cells survive, how the cut is
coloured (Plan 39 VI10b, F2f, F2g, F2h).

Three small choices that a section carries beside its planes:

* :class:`Combine` -- what several enabled planes keep. *Keep the common
  part* is the intersection of every plane's kept half-space (the only rule
  there was); *Remove the common part* keeps everything outside that
  intersection (the union of the removed sides: a sphere with two planes is
  the sphere minus a slice of cake); *Keep each plane's piece* lets every
  plane cut on its own and shows the results together.
* :class:`CellRule` -- which whole cells a whole-cell mode keeps when a plane
  passes through them: *Any part inside* (a vertex strictly on the kept side,
  the rule there was), *Centroid inside* (the cell's centre on the kept side:
  more than half of it) or *Entirely inside* (every vertex on the kept side).
* :class:`ScalePolicy` -- the range of the colour scale on a cut.

:func:`choose_sides` implements *Keep larger* / *Keep smaller*: it evaluates
the volume the planes would keep for every combination of sides (2^n, n <= 6)
and picks the larger or the smaller. No Qt and no VTK: the section worker, the
local path and the panel all read it.
"""
from __future__ import annotations

import itertools
from enum import Enum

import numpy as np

__all__ = ['Combine', 'CellRule', 'ScalePolicy', 'Prefer', 'COMBINE_LABELS',
           'COMBINE_TOOLTIPS', 'RULE_LABELS', 'RULE_TOOLTIPS',
           'SCALE_LABELS', 'region_value', 'choose_sides', 'MAX_SIDE_PLANES',
           'parse']

#: 2^n combinations of sides are evaluated; the panel holds at most this many.
MAX_SIDE_PLANES = 6


class Combine(Enum):
    COMMON = 'common'
    REMOVE_COMMON = 'remove_common'
    EACH = 'each'


class CellRule(Enum):
    ANY = 'any'
    CENTROID = 'centroid'
    ENTIRE = 'entire'


class ScalePolicy(Enum):
    WHOLE = 'whole'
    CUT = 'cut'
    FIXED = 'fixed'


class Prefer(Enum):
    LARGER = 'larger'
    SMALLER = 'smaller'


COMBINE_LABELS = {
    Combine.COMMON: 'Keep the common part',
    Combine.REMOVE_COMMON: 'Remove the common part',
    Combine.EACH: "Keep each plane's piece",
}

COMBINE_TOOLTIPS = {
    Combine.COMMON:
        'Keep what every plane keeps: the part where all the kept sides '
        'overlap (two planes keep a wedge).',
    Combine.REMOVE_COMMON:
        'Keep everything except that common part: the model with the wedge '
        'taken out.',
    Combine.EACH:
        'Every plane cuts on its own and the pieces are shown together.',
}

RULE_LABELS = {
    CellRule.ANY: 'Any part inside',
    CellRule.CENTROID: 'Centroid inside',
    CellRule.ENTIRE: 'Entirely inside',
}

RULE_TOOLTIPS = {
    CellRule.ANY:
        'Keep a cell if any part of it is on the kept side: the face is a '
        'fringe of whole cells that protrudes past the plane.',
    CellRule.CENTROID:
        'Keep a cell if its centre is on the kept side (more than half of '
        'it is kept).',
    CellRule.ENTIRE:
        'Keep a cell only if all of it is on the kept side.',
}

SCALE_LABELS = {
    ScalePolicy.WHOLE: 'Whole mesh',
    ScalePolicy.CUT: 'This cut',
    ScalePolicy.FIXED: 'Fixed',
}


def parse(enum, value, default):
    """*value* as a member of *enum* (a member or its value); *default* for
    ``None``. ValueError for anything else."""
    if value is None:
        return default
    if isinstance(value, enum):
        return value
    try:
        return enum(value)
    except ValueError:
        raise ValueError(f'{value!r} is not a {enum.__name__}') from None


def region_value(kept, combine) -> np.ndarray:
    """The sign of the region the planes keep, per point.

    *kept*: ``(n_planes, n)`` array, each row a plane's kept-side distance
    (``>= 0`` is on its kept side). The result is ``> 0`` inside the region:
    the smallest row for the common part, the largest negated row for
    everything outside it, the largest row for the union of each plane's own
    kept side.
    """
    kept = np.asarray(kept, dtype=float)
    combine = Combine(combine)
    if combine is Combine.COMMON:
        return kept.min(axis=0)
    if combine is Combine.REMOVE_COMMON:
        return (-kept).max(axis=0)
    return kept.max(axis=0)


def choose_sides(signed, volumes, combine, prefer, current=None):
    """The keep side of each plane that keeps the larger / smaller region.

    *signed*: ``(n_planes, n_cells)``, ``normal . centre - offset`` of every
    cell centre for each plane (the plane's own sign, independent of its
    ``keep``). *volumes*: per cell. For every combination of sides the volume
    of the cells whose centre is in the region is summed (a combination
    keeping nothing is not a candidate); the largest or smallest wins, ties
    going to the combination closest to *current* (a tuple of +1/-1).

    Returns ``(keeps, volume, table)``: the chosen sides, the volume kept and
    ``{keeps: volume}`` for all of them.
    """
    signed = np.asarray(signed, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    prefer = parse(Prefer, prefer, Prefer.SMALLER)
    n = signed.shape[0]
    if not 1 <= n <= MAX_SIDE_PLANES:
        raise ValueError(f'sides are chosen for 1 to {MAX_SIDE_PLANES} '
                         f'planes, not {n}')
    table = {}
    for keeps in itertools.product((1, -1), repeat=n):
        kept = signed * np.asarray(keeps, dtype=float)[:, None]
        table[keeps] = float(volumes[region_value(kept, combine) > 0].sum())
    candidates = {k: v for k, v in table.items() if v > 0.0} or table
    current = tuple(current) if current is not None else (1,) * n
    sign = 1.0 if prefer is Prefer.LARGER else -1.0
    best = min(candidates, key=lambda k: (
        -sign * candidates[k], sum(a != b for a, b in zip(k, current)), k))
    return best, table[best], table
