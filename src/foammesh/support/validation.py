#!/usr/bin/env python
# -*- coding: utf-8 -*-

from typing import Optional

from foammesh.support import field_complaint


FLOAT_PATTERN = r'[-+]?\d*\.?\d+([eE][-+]?\d+)?'

FLOAT_EXPRESSION = f'^{FLOAT_PATTERN}$'


class ValidationResult:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class FloatValidationResult(ValidationResult):
    def __init__(self, text):
        super().__init__(text)

    def float(self):
        return float(self._text)


def validateFloat(input: str, name: str,
                  low: Optional[float] = None, high: Optional[float] = None, lowInclusive=True, highInclusive=True):
    if not input.strip():
        raise ValueError(field_complaint.sentence(
            name, field_complaint.required_clause()))

    try:
        v = float(input)
    except ValueError as error:
        raise ValueError(field_complaint.sentence(
            name, field_complaint.number_clause())) from error

    # One clause, spelling out the whole permitted span, so that this reads
    # the same as every other refused entry in the product.
    if ((low is not None and (v < low or (v == low and not lowInclusive)))
            or (high is not None and (v > high or (v == high and not highInclusive)))):
        raise ValueError(field_complaint.sentence(name, field_complaint.range_clause(
            low=low, high=high,
            lowInclusive=lowInclusive, highInclusive=highInclusive)))

    return FloatValidationResult(input)
