"""The one quality scale the volume and a section share.

Plan 39 VI9 (F5c, F5f, §4.4 item 6). The Quality inspector colours cells on a
band of one metric with the colour standard (``colour_standard``). A section coloured by that same
metric used a different blue-to-red ramp and its own range, so the cut and the
volume disagreed about which cell was bad. Both now take their table from
:func:`lookup_table` and, while the inspector holds a band for the metric the
section is coloured by, the section takes the band as its range too.
"""
from __future__ import annotations

from foammesh.core.quality.cell_fields import (
    FIELD_NAMES, GMSH_FIELD_NAMES, WORST_IS_LOW)
from foammesh.support import colour_standard

_band: dict = {}


def is_quality_array(name) -> bool:
    return str(name) in FIELD_NAMES or str(name) in GMSH_FIELD_NAMES


def lookup_table(metric: str, low: float, high: float):
    """The poor-cell table on ``[low, high]``, red at the bad end."""
    return colour_standard.lookup_table(
        'quality', low, high, str(metric) not in WORST_IS_LOW,
        limit=limit(metric))


def set_band(metric: str, low: float, high: float, limit=None) -> None:
    """The inspector's colouring is on screen: *metric* over ``[low, high]``."""
    _band.clear()
    _band.update(metric=str(metric), low=float(low), high=float(high),
                 limit=None if limit is None else float(limit))


def clear_band() -> None:
    _band.clear()


def band(metric: str | None = None):
    """``(low, high)`` the inspector is colouring, or ``None``; with *metric*,
    only when it is that metric."""
    if not _band or (metric is not None and _band['metric'] != str(metric)):
        return None
    return _band['low'], _band['high']


def limit(metric: str | None = None):
    if not _band or (metric is not None and _band['metric'] != str(metric)):
        return None
    return _band['limit']


def share(mapped: dict) -> dict:
    """*mapped* (a section colour mapping) on the inspector's band, if its
    metric is the one the inspector is colouring. Returns a new mapping."""
    if not mapped or mapped.get('kind') != 'scalar':
        return mapped
    shared = band(mapped.get('array'))
    if shared is None:
        return mapped
    return dict(mapped, range=shared, shared='inspector',
                limit=limit(mapped.get('array')))
