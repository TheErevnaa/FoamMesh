#!/usr/bin/env python
# -*- coding: utf-8 -*-

from foammesh.support import field_complaint

from .validation import Validator


class FloatRangeValidator(Validator):
    def __init__(self, edit, name, bottom=None, top=None, bottomExclusive=False, topExclusive=False):
        super().__init__()

        self._edit = edit
        self._name = name

        self._top = top
        self._bottom = bottom
        self._bottomExclusive = bottomExclusive
        self._topExclusive = topExclusive

    def validate(self):
        text = self._edit.text().strip()
        if not text:
            return False, field_complaint.sentence(
                self._name, field_complaint.required_clause())

        # ``float`` used to be called bare here, so a letter typed into the box
        # left the dialog through an uncaught ValueError instead of a sentence.
        try:
            value = float(text)
        except ValueError:
            return False, field_complaint.sentence(
                self._name, field_complaint.number_clause())

        low = self._bottom
        high = self._top
        if ((low is not None
                and (value < low or (value == low and self._bottomExclusive)))
                or (high is not None
                    and (value > high or (value == high and self._topExclusive)))):
            return False, field_complaint.sentence(self._name, field_complaint.range_clause(
                low=low, high=high,
                lowInclusive=not self._bottomExclusive,
                highInclusive=not self._topExclusive))

        return True, None
