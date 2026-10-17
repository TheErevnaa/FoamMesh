"""What the Quality inspector asks of a metric: a limit, the cells beyond it.

Plan 39 VI9 (F5c, F5e, F5f). One place decides which side of a limit is the
bad one, which limit a metric is judged by, and what "beyond" means, so the
histogram, the colouring, the section and the ranked list cannot disagree.

``checkMesh`` counts a face as severe when its angle is *greater than* the
threshold, so "beyond the limit" is a strict inequality here too -- a cell
sitting exactly on the limit is within it.
"""
from __future__ import annotations

import numpy as np

from .cell_fields import FIELD_NAMES, WORST_IS_LOW

#: Metric -> (project key of the mesher's limit, project key of the check's
#: limit, the check's shipped default). A metric with no entry has no limit in
#: either place (aspect ratio and volume are judged by no OpenFOAM threshold
#: that a number here could stand for).
LIMIT_KEYS = {
    'nonOrthoAngle': ('meshQuality/maxNonOrtho', 'meshCheck/nonOrthThreshold'),
    'skewness': ('meshQuality/maxInternalSkewness',
                 'meshCheck/skewThreshold'),
}

SOURCE_MESHER = 'mesher'
SOURCE_CHECK = 'check'


def worst_is_high(metric: str) -> bool:
    """Whether a large number is the bad one (volume is the exception)."""
    return str(metric) not in WORST_IS_LOW


def has_limit(metric: str) -> bool:
    return str(metric) in LIMIT_KEYS


def beyond_mask(values, limit: float, metric: str) -> np.ndarray:
    """Cells strictly on the failing side of *limit* (non-finite never are)."""
    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values)
    with np.errstate(invalid='ignore'):
        mask = values > limit if worst_is_high(metric) else values < limit
    return mask & finite


def beyond_cells(values, limit: float, metric: str) -> np.ndarray:
    """The ids of the cells beyond *limit*, ascending."""
    return np.flatnonzero(beyond_mask(values, limit, metric))


def beyond_band(limit: float, low: float, high: float, metric: str):
    """The ``(low, high)`` an inclusive threshold takes to keep exactly the
    cells :func:`beyond_cells` names; ``None`` when none can be beyond."""
    limit, low, high = float(limit), float(low), float(high)
    if worst_is_high(metric):
        edge = float(np.nextafter(limit, np.inf))
        return None if edge > high else (max(edge, low), high)
    edge = float(np.nextafter(limit, -np.inf))
    return None if edge < low else (low, min(edge, high))


def limit_value(read, metric: str, source: str) -> float | None:
    """The mesher's or the check's limit for *metric*, or ``None``.

    *read* is ``read(path) -> value or None`` over the project; the check's
    shipped default stands where the project holds nothing.
    """
    keys = LIMIT_KEYS.get(str(metric))
    if keys is None:
        return None
    path = keys[0] if source == SOURCE_MESHER else keys[1]
    try:
        raw = read(path)
    except Exception:                                          # noqa: BLE001
        raw = None
    if raw is None or raw == '':
        return _DEFAULTS.get(path)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEFAULTS.get(path)


#: The shipped defaults (``db.configurations_schema``): the snappy limits and
#: OpenFOAM 13's own checkMesh thresholds.
_DEFAULTS = {
    'meshQuality/maxNonOrtho': 65.0,
    'meshQuality/maxInternalSkewness': 4.0,
    'meshCheck/nonOrthThreshold': 70.0,
    'meshCheck/skewThreshold': 4.0,
}

assert set(LIMIT_KEYS) <= set(FIELD_NAMES)
