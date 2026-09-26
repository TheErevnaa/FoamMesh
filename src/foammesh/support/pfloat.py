#!/usr/bin/env python
# -*- coding: utf-8 -*-


import sys

from lxml.builder import E

from foammesh.support import field_complaint


class PFloat():
    def __init__(self,
                 text: str,
                 name: str = '',
                 low:  float = -sys.float_info.max,
                 high: float =  sys.float_info.max,
                 lowInclusive:bool  = True,
                 highInclusive:bool = True):

        self._text = text.strip()

        # Parameter-expression support is intentionally absent. PFloat
        # validates plain numeric values only.
        if not self._text:
            raise ValueError(field_complaint.sentence(
                name, field_complaint.required_clause()))

        try:
            value = float(self._text)
        except ValueError as error:
            raise ValueError(field_complaint.sentence(
                name, field_complaint.number_clause())) from error

        self._isParam = False

        # One clause, spelling out the whole permitted span, so that this
        # reads the same as every other refused entry in the product. The
        # sentinel bounds this class defaults to are not a span a reader
        # needs to be told about, so they are dropped.
        if (value < low or (value == low and not lowInclusive)
                or value > high or (value == high and not highInclusive)):
            raise ValueError(field_complaint.sentence(name, field_complaint.range_clause(
                low=None if low <= -sys.float_info.max else low,
                high=None if high >= sys.float_info.max else high,
                lowInclusive=lowInclusive, highInclusive=highInclusive)))

        self._value = value

    @staticmethod
    def fromElement(e):
        return PFloat(e.text)

    def toElement(self, tag):
        return E(tag, self._text)

    def __str__(self):
        return self._text

    def __float__(self):
        return self._value

    def __gt__(self, other):
        if isinstance(other, PFloat):
            return self._value > other._value

        return self._value > other

    def __ge__(self, other):
        if isinstance(other, PFloat):
            return self._value >= other._value

        return self._value >= other

    def __lt__(self, other):
        if isinstance(other, PFloat):
            return self._value < other._value

        return self._value < other

    def __le__(self, other):
        if isinstance(other, PFloat):
            return self._value <= other._value

        return self._value <= other
