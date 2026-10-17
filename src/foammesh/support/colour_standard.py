"""The colour standard: one scale for every continuous field (Plan 43 LY12, §4).

Cell quality, surface deviation, section colouring, the size field, layers
grown and the Gmsh post-processing views all take their lookup table from
:func:`lookup_table`. Nothing else in the application builds a table for a
continuous field; the source scan in ``tests/unit/test_ly12_colour_standard``
holds that line. Categorical tables (patch and region colours, the skin
table, the element-type legend, the split dialog) are not scales and are not
built here.

The standard is what the user saved with *Make this my standard*, or the app
standard (Rainbow, Jet) when nothing is saved. A change made without saving
lasts for the session only (:func:`set_current`). The store is
``AppSettings`` (``SettingKey.COLOUR_STANDARD``), the same store the CLI and
the API read, so a headless capture uses the same standard.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from matplotlib import colormaps
from matplotlib.colors import LinearSegmentedColormap
from vtkmodules.vtkCommonCore import vtkLookupTable

#: Not measured is always this grey, never a colour of the scale.
NAN_GREY = (0.62, 0.62, 0.62, 1.0)

SMOOTH_ENTRIES = 256
MIN_BANDS = 2
MAX_BANDS = 64
SLIDER_MIN, SLIDER_MAX, SLIDER_STEP = 5, 50, 5

RANGE_WHOLE = 'whole'
RANGE_LIMIT = 'limit'
RANGES = (RANGE_WHOLE, RANGE_LIMIT)

#: key -> label. Rainbow (Jet) is the app standard.
SCALES = {
    'jet': 'Rainbow (Jet)',
    'turbo': 'Turbo',
    'viridis': 'Viridis',
    'inferno': 'Inferno',
    'plasma': 'Plasma',
    'blackbody': 'Blackbody',
    'white_to_red': 'White to red',
    'coolwarm': 'Cool to warm',
    'gray': 'Greyscale',
}
APP_SCALE = 'jet'

#: Kinds of continuous field, for the record of who asked.
KINDS = ('quality', 'deviation', 'section', 'size_field', 'layers', 'plugin')


@dataclass(frozen=True)
class Standard:
    scale: str = APP_SCALE
    reverse: bool = False
    #: 0 is smooth; 2 to 64 is banded.
    bands: int = 0
    range: str = RANGE_WHOLE
    legend: bool = True

    def to_record(self) -> dict:
        return {'scale': self.scale, 'reverse': self.reverse,
                'bands': self.bands, 'range': self.range,
                'legend': self.legend}

    @property
    def scale_label(self) -> str:
        return SCALES[self.scale]

    def describe(self) -> str:
        """The words the legend adds: ``Rainbow (Jet) · reversed · 20 bands``."""
        parts = [self.scale_label]
        if self.reverse:
            parts.append('reversed')
        if self.bands:
            parts.append('%d bands' % self.bands)
        return ' · '.join(parts)


APP_STANDARD = Standard()


def normalise(record) -> Standard:
    """A :class:`Standard` from anything stored; damaged fields fall back."""
    if isinstance(record, Standard):
        return record
    base = APP_STANDARD
    if not isinstance(record, dict):
        return base
    scale = record.get('scale')
    reverse = record.get('reverse')
    try:
        bands = int(record.get('bands', base.bands))
    except (TypeError, ValueError):
        bands = base.bands
    if bands != 0 and not MIN_BANDS <= bands <= MAX_BANDS:
        bands = base.bands
    rng = record.get('range')
    legend = record.get('legend')
    return Standard(
        scale=scale if scale in SCALES else base.scale,
        reverse=reverse if isinstance(reverse, bool) else base.reverse,
        bands=bands,
        range=rng if rng in RANGES else base.range,
        legend=legend if isinstance(legend, bool) else base.legend)


# --- the store ---------------------------------------------------------------

_session: Standard | None = None
_listeners: list = []


def _store(settings=None):
    if settings is not None:
        return settings
    from foammesh.settings.app_settings import appSettings
    return appSettings


def saved(settings=None) -> Standard:
    """The standard on this machine: the user's saved one, else the app's."""
    try:
        record = _store(settings).getColourStandard()
    except Exception:                                   # noqa: BLE001
        record = None
    return normalise(record) if record else APP_STANDARD


def has_saved(settings=None) -> bool:
    try:
        return bool(_store(settings).getColourStandard())
    except Exception:                                   # noqa: BLE001
        return False


def current(settings=None) -> Standard:
    """What every view paints with now: the session's, else the standard."""
    return _session if _session is not None else saved(settings)


def is_standard(settings=None) -> bool:
    return current(settings) == saved(settings)


def _ref(callback):
    import weakref
    return (weakref.WeakMethod(callback) if hasattr(callback, '__self__')
            else (lambda: callback))


def subscribe(callback) -> None:
    """Call *callback(standard)* on a change. A bound method is held weakly,
    so a closed window or panel is not kept alive by its subscription."""
    if not any(ref() == callback for ref in _listeners):
        _listeners.append(_ref(callback))


def unsubscribe(callback) -> None:
    _listeners[:] = [ref for ref in _listeners
                     if ref() is not None and ref() != callback]


def _announce(settings=None) -> None:
    _refill_issued(settings)
    _listeners[:] = [ref for ref in _listeners if ref() is not None]
    for ref in list(_listeners):
        callback = ref()
        if callback is None:
            continue
        try:
            callback(current(settings))
        except Exception:                               # noqa: BLE001
            pass


def set_current(settings=None, **changes) -> Standard:
    """Change the session's scale; nothing is saved."""
    global _session
    _session = normalise(replace(current(settings), **changes).to_record())
    _announce(settings)
    return _session


def step_scale(settings=None, direction: int = 1) -> Standard:
    """Alt+Shift+C. The next scale; the session changes, the standard not."""
    keys = list(SCALES)
    at = keys.index(current(settings).scale)
    return set_current(settings, scale=keys[(at + direction) % len(keys)])


def make_my_standard(settings=None) -> Standard:
    """*Make this my standard*: save the current values."""
    global _session
    now = current(settings)
    _store(settings).updateColourStandard(now.to_record())
    _session = None
    _announce(settings)
    return now


def go_back_to_standard(settings=None) -> Standard:
    """*Go back to the standard*: drop the session's changes."""
    global _session
    _session = None
    _announce(settings)
    return current(settings)


def use_app_standard(settings=None) -> Standard:
    """*Use the app standard (Rainbow Jet)*: remove the user's entry."""
    global _session
    _store(settings).clearColourStandard()
    _session = None
    _announce(settings)
    return current(settings)


def reset_session() -> None:
    """Tests and restarts: forget the session without announcing."""
    global _session
    _session = None


# --- the table ---------------------------------------------------------------

_BLACKBODY = LinearSegmentedColormap.from_list(
    'foammesh_blackbody',
    [(0.0, 0.0, 0.0), (0.55, 0.0, 0.0), (0.95, 0.35, 0.0),
     (1.0, 0.8, 0.1), (1.0, 1.0, 0.85)])


def _white_to_red(x: float):
    o = 1.0 - math.pow(x, 2)
    return (1.0, o, o, 1.0)


def _colour_map(key: str):
    if key == 'blackbody':
        return _BLACKBODY
    if key == 'white_to_red':
        return _white_to_red
    return colormaps[key]


def colours(standard: Standard, worst_is_high: bool = True) -> list:
    """The table's colours, low value first, as ``(r, g, b)``.

    Blue is good and red is worst for Rainbow; the other scales put their
    strong end at the worst value. ``worst_is_high=False`` turns the table
    round, and *Reverse* turns it again.
    """
    cmap = _colour_map(standard.scale)
    flip = (not worst_is_high) != bool(standard.reverse)
    count = standard.bands or SMOOTH_ENTRIES
    out = []
    for i in range(count):
        x = (i + 0.5) / count if standard.bands else i / (count - 1)
        if flip:
            x = 1.0 - x
        out.append(tuple(cmap(x)[:3]))
    return out


def narrowed_range(kind: str, low: float, high: float, limit, standard=None):
    """The range the colours span: the whole one, or around the limit."""
    standard = standard or current()
    low, high = float(low), float(high)
    if (standard.range != RANGE_LIMIT or limit is None
            or not math.isfinite(float(limit))):
        return low, high
    limit = float(limit)
    if kind == 'deviation':
        return (low, min(high, 2.0 * limit)) if limit > 0.0 else (low, high)
    half = 0.5 * max(limit - low, high - limit)
    if half <= 0.0:
        return low, high
    return max(low, limit - half), min(high, limit + half)


def lookup_table(kind: str, low: float, high: float,
                 worst_is_high: bool = True, *, limit=None,
                 log: bool = False, standard=None) -> vtkLookupTable:
    """The one table for a continuous field on ``[low, high]``.

    ``worst_is_high`` says which end of the values is the bad one (the red
    end). ``limit`` is where failing starts (the tolerance for deviation),
    used when the standard's range is *Around the limit*. ``log`` makes the
    table logarithmic (the size field). Not measured is grey.
    """
    explicit = standard
    standard = standard or current()
    low, high = narrowed_range(kind, low, high, limit, standard)
    if not high > low:
        high = low + (abs(low) * 1e-6 or 1e-12)
    table = vtkLookupTable()
    entries = colours(standard, worst_is_high)
    table.SetNumberOfTableValues(len(entries))
    if log:
        table.SetScaleToLog10()
    table.SetTableRange(low, high)
    for i, (r, g, b) in enumerate(entries):
        table.SetTableValue(i, r, g, b, 1.0)
    table.SetNanColor(*NAN_GREY)
    if explicit is None:
        _issue(table, (kind, low, high, worst_is_high, limit, log))
    return table


#: Tables handed out on the live standard, newest last, per kind. A change of
#: the standard refills them in place, so every view and legend that holds one
#: follows at once. The cap keeps a long drag of a band from piling them up.
_ISSUED_PER_KIND = 48
_issued: dict = {}


def _issue(table, params) -> None:
    kept = _issued.setdefault(params[0], [])
    kept.append((table, params))
    del kept[:-_ISSUED_PER_KIND]


def _refill_issued(settings=None) -> None:
    standard = current(settings)
    for kept in _issued.values():
        for table, (kind, low, high, worst, limit, log) in kept:
            _fill(table, lookup_table(kind, low, high, worst, limit=limit,
                                      log=log, standard=standard))


def _fill(table, fresh) -> None:
    table.SetNumberOfTableValues(fresh.GetNumberOfTableValues())
    for i in range(fresh.GetNumberOfTableValues()):
        table.SetTableValue(i, *fresh.GetTableValue(i))
    table.SetTableRange(*fresh.GetTableRange())
    table.SetNanColor(*NAN_GREY)
    table.Modified()


def issued_count(kind: str | None = None) -> int:
    if kind is None:
        return sum(len(kept) for kept in _issued.values())
    return len(_issued.get(kind, ()))


def refill(table: vtkLookupTable, kind: str, low: float, high: float,
           worst_is_high: bool = True, *, limit=None, standard=None):
    """Fill *table* in place, so a legend that holds it follows."""
    fresh = lookup_table(kind, low, high, worst_is_high, limit=limit,
                         standard=standard or current())
    _fill(table, fresh)
    # The caller's table now stands for these values; a change of the
    # standard refills it from them.
    for kept in _issued.values():
        kept[:] = [item for item in kept if item[0] is not table]
    if standard is None:
        _issue(table, (kind, low, high, worst_is_high, limit, False))
    return table
