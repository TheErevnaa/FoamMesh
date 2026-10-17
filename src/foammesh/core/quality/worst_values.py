"""The worst values on the model and the statistics caption (Plan 43 LY10).

Pure arithmetic and wording, no Qt and no VTK, so the CLI, the API and the
tests read the same numbers the viewport writes on the model.

* :func:`pick_worst` chooses the three cells (or, for deviation, the three
  faces with the largest |d|) that get a label. The picks are at least
  :data:`MIN_ROW_GAP` rows apart in the ranked list, so two neighbours of one
  bad spot do not take two labels.
* :func:`layout_labels` places the label boxes on the screen so that none
  overlaps another and each keeps a leader to its anchor.
* :func:`quality_caption` and :func:`deviation_caption` word the two-line
  statistics card.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .cell_fields import rank_cells

#: How many values are written on the model (Plan 43 section 7, item 6: not a
#: setting).
LABEL_COUNT = 3
#: The picks are at least this many rows apart in the ranked list.
MIN_ROW_GAP = 3

MINUS = '−'


@dataclass(frozen=True)
class Pick:
    """One value to write on the model: where, and what."""
    index: int                  # cell or face id in its own array
    value: float
    rank: int                   # row in the ranked list, 0 = worst
    source: str = ''            # patch name for a face, '' for a cell


def pick_worst(values, *, worst_is_high: bool = True,
               count: int = LABEL_COUNT, gap: int = MIN_ROW_GAP,
               by_magnitude: bool = False, source: str = '') -> list:
    """The *count* worst entries of *values*, each *gap* rows after the last.

    ``by_magnitude`` ranks by |value| (deviation); the returned value keeps its
    sign. Entries that are not numbers are never picked. Fewer than *count*
    come back when the array is shorter than the rows the picks need.
    """
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return []
    need = (max(int(count), 1) - 1) * max(int(gap), 1) + 1
    keyed = np.abs(array) if by_magnitude else array
    ids = rank_cells(keyed, need,
                     worst_is_high=True if by_magnitude else worst_is_high)
    picks = []
    for row in range(0, int(ids.size), max(int(gap), 1)):
        if len(picks) >= count:
            break
        cell = int(ids[row])
        picks.append(Pick(cell, float(array[cell]), row, source))
    return picks


def format_value(value: float) -> str:
    """Three significant figures, trailing zero kept: ``4.60``, ``0.812``."""
    if value != value:
        return 'n/a'
    text = f'{value:#.3g}'
    if 'e' not in text and text.endswith('.'):
        text = text[:-1]
    return text


def format_signed_mm(value: float) -> str:
    """``+0.429 mm`` or ``−0.442 mm``: the sign says inside or outside."""
    sign = '+' if value >= 0 else MINUS
    return f'{sign}{abs(value):.3f} mm'


def label_for(pick: Pick, *, deviation: bool) -> str:
    return (format_signed_mm(pick.value) if deviation
            else format_value(pick.value))


# -- screen layout ------------------------------------------------------------ #

#: Directions tried for a label, nearest first: up-right, up-left, down-right,
#: down-left, then the four axes.
_DIRECTIONS = ((1, 1), (-1, 1), (1, -1), (-1, -1),
               (0, 1), (0, -1), (1, 0), (-1, 0))


def _overlap(a, b, pad: float) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0]
                or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def layout_labels(anchors, sizes, viewport, *, pad: float = 4.0,
                  reach: float = 24.0) -> list:
    """Place one box per anchor so no two overlap.

    *anchors* are display points ``(x, y)``; *sizes* the ``(width, height)`` of
    each box; *viewport* ``(width, height)``. Returns, per label,
    ``(x0, y0, x1, y1)`` of its box (display, y up). The leader runs from the
    anchor to the nearest point of the box. A box is moved out in steps of
    *reach* along eight directions until it is free and inside the viewport;
    the anchors are handled worst first, so the worst value keeps the nearest
    position.
    """
    width, height = float(viewport[0]), float(viewport[1])
    placed: list = []
    for (ax, ay), (bw, bh) in zip(anchors, sizes):
        chosen = None
        for ring in range(1, 40):
            distance = reach * ring
            for dx, dy in _DIRECTIONS:
                x0 = ax + (dx * distance if dx > 0
                           else (-distance - bw if dx < 0 else -bw / 2))
                y0 = ay + (dy * distance if dy > 0
                           else (-distance - bh if dy < 0 else -bh / 2))
                x0 = min(max(x0, 2.0), max(width - bw - 2.0, 2.0))
                y0 = min(max(y0, 2.0), max(height - bh - 2.0, 2.0))
                box = (x0, y0, x0 + bw, y0 + bh)
                if not any(_overlap(box, other, pad) for other in placed):
                    chosen = box
                    break
            if chosen is not None:
                break
        if chosen is None:
            # A viewport too small for the boxes: stack them from the corner
            # rather than leave one unplaced.
            y0 = 2.0 + sum(b[3] - b[1] + pad for b in placed)
            chosen = (2.0, y0, 2.0 + bw, y0 + bh)
        placed.append(chosen)
    return placed


def leader_end(anchor, box) -> tuple:
    """The point of *box* nearest *anchor*, where its leader ends."""
    x = min(max(anchor[0], box[0]), box[2])
    y = min(max(anchor[1], box[1]), box[3])
    return (x, y)


def label_size(text: str, font_pixels: float) -> tuple:
    """A box for *text* at *font_pixels*, with its frame: the same estimate the
    layout and the test use, wider than any of the four fonts draws it."""
    width = 0.62 * font_pixels * max(len(text), 1) + 10.0
    height = 1.45 * font_pixels + 6.0
    return (width, height)


# -- the statistics caption ---------------------------------------------------- #

def _count(value) -> str:
    return f'{int(value):,}'


def quality_caption(*, case: str, metric_label: str, values,
                    worst_is_high: bool = True, limit=None,
                    source: str = 'checkMesh report',
                    scale_name: str = '', version: str = '',
                    stamp: str = '', kind: str = 'cells') -> tuple:
    """Two lines for a quality colouring.

    ``pipe · Cell quality: Skewness · worst 4.60 · 2 of 13,412 cells over 4.00
    · mean 0.81`` then ``checkMesh report · Rainbow (Jet) · FoamMesh 1.1.0 ·
    12 Oct 2026``.
    """
    array = np.asarray(values, dtype=np.float64)
    finite = array[np.isfinite(array)]
    first = [case or 'mesh', f'Cell quality: {metric_label}']
    if finite.size:
        worst = float(finite.max() if worst_is_high else finite.min())
        first.append(f'worst {format_value(worst)}')
        if limit is not None:
            over = int((finite > limit).sum() if worst_is_high
                       else (finite < limit).sum())
            word = 'over' if worst_is_high else 'under'
            first.append(f'{_count(over)} of {_count(finite.size)} {kind} '
                         f'{word} {format_value(float(limit))}')
        first.append(f'mean {format_value(float(finite.mean()))}')
    else:
        first.append('not measured')
    return (' · '.join(first), _second_line(source, scale_name, version,
                                                 stamp))


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def headline_metric(metrics):
    """The metric row a plainly shown mesh is captioned with (DP-1948).

    The first row that is not a pass; else the row whose worst value is
    nearest its limit; else the first row. ``None`` for no rows.
    """
    rows = [m for m in (metrics or ()) if isinstance(m, dict)]
    if not rows:
        return None
    for row in rows:
        if str(row.get('verdict') or '') not in ('', 'pass', 'unrated'):
            return row
    best, best_ratio = None, None
    for row in rows:
        worst, limit = (_number(row.get('achievedMinimum')),
                        _number(row.get('requestedMinimum')))
        if worst is None or not limit:
            continue
        ratio = worst / limit
        if best_ratio is None or ratio > best_ratio:
            best, best_ratio = row, ratio
    return best or rows[0]


def verdict_caption(*, case: str, metric: dict, source: str = 'checkMesh',
                    scale_name: str = '', version: str = '',
                    stamp: str = '', kind: str = 'cells') -> tuple:
    """Two lines for a mesh shown plainly: what the checkMesh report filed
    for one metric (its worst value, its mean, how many cells are beyond the
    limit), worded like :func:`quality_caption`."""
    first = [case or 'mesh',
             f"Cell quality: {metric.get('measure') or 'metric'}"]
    worst = _number(metric.get('achievedMinimum'))
    limit = _number(metric.get('requestedMinimum'))
    below = _number(metric.get('belowThreshold'))
    total = _number(metric.get('total'))
    mean = _number(metric.get('achievedMean'))
    if worst is not None:
        first.append(f'worst {format_value(worst)}')
    if below is not None and total and limit is not None:
        first.append(f'{_count(below)} of {_count(total)} {kind} over '
                     f'{format_value(limit)}')
    elif limit is not None:
        first.append(f'limit {format_value(limit)}')
    if mean is not None:
        first.append(f'mean {format_value(mean)}')
    if worst is None and mean is None:
        first.append('not reported')
    return (' · '.join(first), _second_line(source, scale_name, version,
                                                  stamp))


def deviation_caption(*, case: str, fields, tolerance: float = 0.0,
                      signed: bool = True, source: str = 'live distance',
                      scale_name: str = '', version: str = '',
                      stamp: str = '') -> tuple:
    """Two lines for a deviation colouring: max, mean, RMS, share in tolerance
    (all of |d|, in millimetres)."""
    chunks = [np.asarray(f, dtype=np.float64).ravel() for f in fields]
    joined = np.concatenate(chunks) if chunks else np.zeros(0)
    finite = np.abs(joined[np.isfinite(joined)])
    first = [case or 'mesh', 'Surface deviation']
    if finite.size:
        rms = math.sqrt(float((finite ** 2).mean()))
        within = 100.0 * float((finite <= abs(tolerance)).sum()) / finite.size
        first += [f'max |d| {finite.max():.3f} mm',
                  f'mean |d| {finite.mean():.3f} mm',
                  f'RMS {rms:.3f} mm',
                  f'{within:.0f}% within {abs(tolerance):.3f} mm']
    else:
        first.append('not measured')
    return (' · '.join(first), _second_line(source, scale_name, version,
                                                 stamp))


def _second_line(source, scale_name, version, stamp) -> str:
    parts = [source, scale_name,
             f'FoamMesh {version}' if version else '', stamp]
    return ' · '.join(part for part in parts if part)
