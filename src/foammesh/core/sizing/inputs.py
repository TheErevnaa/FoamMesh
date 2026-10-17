"""Inputs of one ``foammesh-sizefield`` run, with their defaults (Plan 41 4.8.1).

All lengths are metres here; the card converts to the model's display unit.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math

#: Plan 41 section 12.2 S2 defaults, as divisors of the model length L.
HBULK_DIVISOR = 20.0
HMIN_DIVISOR = 1000.0
DEFAULT_GRADATION = 1.1
DEFAULT_NODES_PER_TWO_PI = 20
DEFAULT_NODES_PER_GAP = 4
DEFAULT_FEATURE_ANGLE = 30.0


class SizeFieldInputError(ValueError):
    pass


def model_length(bounds=None, diagonal=None) -> float:
    """L, the model's bounding diagonal (``derive.py`` uses the same)."""
    if bounds is not None:
        values = [float(item) for item in bounds]
        spans = (values[1] - values[0], values[3] - values[2],
                 values[5] - values[4])
        diagonal = math.sqrt(sum(span * span for span in spans))
    diagonal = float(diagonal or 0.0)
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise SizeFieldInputError('the model has no usable extent')
    return diagonal


@dataclass(frozen=True)
class SizeFieldInputs:
    hmin: float
    hmax: float
    hbulk: float
    gradation: float = DEFAULT_GRADATION
    nodes_per_two_pi: int = DEFAULT_NODES_PER_TWO_PI
    nodes_per_gap: int = DEFAULT_NODES_PER_GAP
    feature_angle: float = DEFAULT_FEATURE_ANGLE
    smoothing: bool = True
    threads: int = 4
    aniso: bool = False

    def validate(self) -> 'SizeFieldInputs':
        if not (self.hmin > 0 and self.hbulk > 0 and self.hmax > 0):
            raise SizeFieldInputError('sizes must be positive')
        if self.hmin > self.hmax:
            raise SizeFieldInputError('minimum size is larger than maximum')
        if not self.hmin <= self.hbulk <= self.hmax * (1 + 1e-9):
            raise SizeFieldInputError(
                'bulk size must lie between minimum and maximum')
        if self.gradation < 1.0:
            raise SizeFieldInputError('gradation must be at least 1')
        if self.nodes_per_two_pi < 3:
            raise SizeFieldInputError('at least 3 nodes per full circle')
        if self.nodes_per_gap < 0:
            raise SizeFieldInputError('nodes per gap cannot be negative')
        if not 1.0 <= self.feature_angle <= 179.0:
            raise SizeFieldInputError('feature angle lies in 1..179 degrees')
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, document: dict) -> 'SizeFieldInputs':
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in dict(document).items() if k in names})


def default_inputs(bounds=None, *, diagonal=None, typed=None,
                   **overrides) -> SizeFieldInputs:
    """Defaults from the extent: hbulk = L/20, hmin = L/1000, hmax = hbulk.

    ``typed`` (name -> value) wins over a default and is never replaced.
    """
    length = model_length(bounds, diagonal)
    values = {'hbulk': length / HBULK_DIVISOR, 'hmin': length / HMIN_DIVISOR}
    values['hmax'] = values['hbulk']
    typed = dict(typed or {})
    for source in (overrides, typed):
        for name, value in source.items():
            if value not in (None, ''):
                values[name] = value
    if 'hbulk' in typed and 'hmax' not in typed:
        values['hmax'] = max(values['hmax'], values['hbulk'])
    return SizeFieldInputs(**values).validate()
