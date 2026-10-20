"""Plan 43 LY11: what the one viewport legend says, in every mode.

The viewport used to carry three separate legends (the poor-cell scalar bar,
the deviation scalar bar, the patch colours) and a deviation panel with its own
tolerance control. There is one legend now, with a Patches / Quality /
Deviation segment, and this module is its content: pure data, no Qt, so the
numbers in it are tested on their own.

* **Patches** lists the categorical patch colours. It is not a scale.
* **Quality** is the metric, the bar between the band's ends, the limit line,
  grey *not measured (n cells)* and the statistics: worst, mean and the count
  over the limit.
* **Deviation** is the size of the distance, ``|d|``, from zero to the scale,
  with the tolerance line, grey *not measured (n faces)* and: max, mean, RMS
  and the share within the tolerance. A face at ``-d`` and one at ``+d`` are
  one colour; the sign is read from the numbers.

The scale button reads *Rainbow (Jet)*, and says *· not the standard* while the
session's scale differs from the user's standard (section 4). Until the
colour standard of LY12 exists the standard is the app's own, `APP_STANDARD`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

#: The modes of the segment, in order.
PATCHES = 'patches'
QUALITY = 'quality'
DEVIATION = 'deviation'
MODES = (PATCHES, QUALITY, DEVIATION)
MODE_LABELS = {PATCHES: 'Patches', QUALITY: 'Quality', DEVIATION: 'Deviation'}

#: The marker the scale button carries while the view is off the standard.
NOT_THE_STANDARD = 'not the standard'
NOT_MEASURED = 'not measured'
#: Ticks on the bar.
TICK_COUNT = 5


@dataclass(frozen=True)
class ScaleChoice:
    """The colour scale of the view: which one, reversed, how many bands."""

    name: str = 'Rainbow (Jet)'
    reverse: bool = False
    #: 0 is smooth; 2..64 is banded.
    bands: int = 0

    def label(self) -> str:
        parts = [self.name]
        if self.reverse:
            parts.append('reversed')
        if self.bands:
            parts.append(f'{int(self.bands)} bands')
        return ' · '.join(parts)


#: What the app ships; LY12 replaces it with the user's saved standard.
APP_STANDARD = ScaleChoice()


def scale_label(choice: ScaleChoice = APP_STANDARD,
                standard: ScaleChoice = APP_STANDARD) -> str:
    """The scale button's text, with *not the standard* when it differs."""
    text = choice.label()
    if choice != standard:
        text += ' · ' + NOT_THE_STANDARD
    return text


def ticks(low: float, high: float, count: int = TICK_COUNT) -> tuple:
    """``count`` evenly spaced tick values from ``low`` to ``high``."""
    low, high = float(low), float(high)
    count = max(2, int(count))
    return tuple(low + (high - low) * i / (count - 1) for i in range(count))


def _finite(values) -> np.ndarray:
    if values is None:
        return np.zeros(0, dtype=np.float64)
    array = np.asarray(values, dtype=np.float64).ravel()
    return array[np.isfinite(array)]


def _count_unmeasured(values) -> int:
    if values is None:
        return 0
    array = np.asarray(values, dtype=np.float64).ravel()
    return int(array.size - np.count_nonzero(np.isfinite(array)))


@dataclass(frozen=True)
class LegendSpec:
    """Everything the legend draws for one mode."""

    mode: str
    title: str
    #: The bar's ends (empty for Patches).
    low: float = 0.0
    high: float = 0.0
    ticks: tuple = ()
    #: Quality: the cell value where failing starts, else None.
    limit: float | None = None
    limit_label: str = ''
    #: Deviation: the tolerance in mm, else None.
    tolerance: float | None = None
    tolerance_label: str = ''
    #: True when the bar's red end is the high end (quality can be low-bad).
    worst_is_high: bool = True
    #: ``not measured (n faces)``; '' when everything was measured.
    not_measured: str = ''
    unmeasured_count: int = 0
    #: One line each: the statistics under the bar.
    statistics: tuple = ()
    #: The metric offered by the picker and the choices beside it.
    metric: str = ''
    metrics: tuple = ()
    #: Patches: ``((name, (r, g, b)), ...)``.
    patches: tuple = ()
    scale_label: str = ''
    #: Which unit the bar reads in ('mm' for deviation).
    unit: str = ''
    #: The figures behind the statistics, for tests and the caption.
    figures: dict = field(default_factory=dict)
    #: DP-2738. Values run past the bar's high end (the scale is a percentile
    #: or the user's): the top end reads "≥ high" and carries a cap.
    over_range: bool = False

    def tolerance_off_scale(self) -> bool:
        """DP-2739. The deviation tolerance lies past the bar's high end.

        The bar has no line for it then -- clamped to the top it read as if
        the tolerance were the scale's end -- and the statistics say so.
        """
        return (self.mode == DEVIATION and self.tolerance is not None
                and self.high > self.low and self.tolerance > self.high)

    @property
    def has_bar(self) -> bool:
        return self.mode != PATCHES

    def limit_fraction(self) -> float | None:
        """Where the limit (or tolerance) line sits on the bar, 0 to 1."""
        value = self.limit if self.mode == QUALITY else self.tolerance
        if value is None or self.high == self.low:
            return None
        if self.tolerance_off_scale():
            return None
        return min(1.0, max(0.0, (value - self.low) / (self.high - self.low)))


def patches_spec(patches, choice: ScaleChoice = APP_STANDARD,
                 standard: ScaleChoice = APP_STANDARD) -> LegendSpec:
    """The Patches mode: ``patches`` is ``[(name, (r, g, b))]``."""
    rows = tuple((str(name), tuple(colour)) for name, colour in patches)
    return LegendSpec(
        mode=PATCHES, title='Patches', patches=rows,
        scale_label=scale_label(choice, standard),
        statistics=(f'{len(rows)} patches',) if rows else ())


def _format(value: float) -> str:
    return f'{value:.3g}'


def quality_spec(metric: str, low: float, high: float, *, unit: str = '',
                 worst_is_high: bool = True, limit: float | None = None,
                 values=None, metrics=(),
                 choice: ScaleChoice = APP_STANDARD,
                 standard: ScaleChoice = APP_STANDARD) -> LegendSpec:
    """The Quality mode for ``metric`` coloured between ``low`` and ``high``.

    ``values`` are the per-cell values of the metric (``nan`` = not measured);
    with them the legend states the worst, the mean and how many cells are
    over the limit.
    """
    low, high = float(low), float(high)
    if high <= low:
        high = low + (abs(low) * 1e-6 or 1e-12)
    title = f'{metric} ({unit})' if unit else metric
    finite = _finite(values)
    missing = _count_unmeasured(values)
    figures: dict = {}
    stats = []
    if finite.size:
        worst = float(finite.max() if worst_is_high else finite.min())
        mean = float(finite.mean())
        figures.update(worst=worst, mean=mean, count=int(finite.size))
        line = f'worst {_format(worst)} · mean {_format(mean)}'
        if limit is not None:
            over = int(np.count_nonzero(
                finite > limit if worst_is_high else finite < limit))
            figures['over_limit'] = over
            line = (f'worst {_format(worst)} · {over:,} of '
                    f'{finite.size:,} cells over {_format(limit)}'
                    f' · mean {_format(mean)}')
        stats.append(line)
    return LegendSpec(
        mode=QUALITY, title=title, low=low, high=high,
        ticks=ticks(low, high), limit=None if limit is None else float(limit),
        limit_label=('' if limit is None else f'limit {_format(limit)}'),
        worst_is_high=bool(worst_is_high),
        not_measured=(f'{NOT_MEASURED} ({missing:,} cells)' if missing else ''),
        unmeasured_count=missing, statistics=tuple(stats), metric=metric,
        metrics=tuple(metrics), unit=unit,
        scale_label=scale_label(choice, standard), figures=figures)


def deviation_spec(fields, scale: float, tolerance: float, *,
                   signed: bool = True, source: str = 'live',
                   choice: ScaleChoice = APP_STANDARD,
                   standard: ScaleChoice = APP_STANDARD) -> LegendSpec:
    """The Deviation mode: ``fields`` are the per-face distances in mm.

    Signed or magnitudes, both read as ``|d|`` here: the colour is the size.
    The bar runs 0 to ``scale``.
    """
    arrays = [np.asarray(v, dtype=np.float64).ravel() for v in fields]
    joined = np.concatenate(arrays) if arrays else np.zeros(0)
    missing = int(joined.size - np.count_nonzero(np.isfinite(joined)))
    magnitude = np.abs(joined[np.isfinite(joined)])
    tolerance = abs(float(tolerance))
    scale = abs(float(scale)) or 1e-6
    figures: dict = {}
    stats = []
    if magnitude.size:
        maximum = float(magnitude.max())
        mean = float(magnitude.mean())
        rms = float(math.sqrt(float(np.mean(magnitude * magnitude))))
        within = int(np.count_nonzero(magnitude <= tolerance))
        share = within / magnitude.size
        figures.update(max_abs=maximum, mean_abs=mean, rms=rms,
                       within=within, count=int(magnitude.size),
                       within_share=share)
        stats.append(f'max |d| {_format(maximum)} mm · mean |d| '
                     f'{_format(mean)} mm · RMS {_format(rms)} mm')
        stats.append(f'{100.0 * share:.1f}% of {magnitude.size:,} faces '
                     f'within ±{_format(tolerance)} mm')
        above = int(np.count_nonzero(magnitude > scale))
        figures.update(above_scale=above)
        if above:
            stats.append(f'{above:,} faces above the scale of '
                         f'{_format(scale)} mm (max {_format(maximum)} mm)')
    if tolerance > scale:
        stats.append(f'tolerance ±{_format(tolerance)} mm is above the '
                     f'scale (0 to {_format(scale)} mm)')
    title = ('Deviation |d| (mm)' if source != 'run'
             else 'Distance from reference |d| (mm)')
    return LegendSpec(
        mode=DEVIATION, title=title, low=0.0, high=scale,
        ticks=ticks(0.0, scale), tolerance=tolerance,
        tolerance_label=f'tolerance ±{_format(tolerance)} mm',
        not_measured=(f'{NOT_MEASURED} ({missing:,} faces)' if missing else ''),
        unmeasured_count=missing, statistics=tuple(stats),
        metric='Deviation', unit='mm',
        scale_label=scale_label(choice, standard), figures=figures,
        over_range=bool(figures.get('above_scale')))


def with_scale(spec: LegendSpec, choice: ScaleChoice,
               standard: ScaleChoice = APP_STANDARD) -> LegendSpec:
    """``spec`` with the scale button for ``choice``."""
    return replace(spec, scale_label=scale_label(choice, standard))
