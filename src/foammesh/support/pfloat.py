#!/usr/bin/env python
# -*- coding: utf-8 -*-


import sys

from PySide6.QtWidgets import QApplication

from lxml.builder import E


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
        try:
            value = float(self._text)
        except ValueError as e:
            raise ValueError(f'{name} - {str(e)}')

        self._isParam = False

        if value < low:
            raise ValueError(f"{name} {QApplication.translate('PFloat', 'is less than ')} {low}")
        elif value == low and not lowInclusive:
            raise ValueError(f"{name} {QApplication.translate('PFloat', 'should be greater than ')} {low}")

        if value > high:
            raise ValueError(f"{name} {QApplication.translate('PFloat', 'is greater than ')} {high}")
        elif value == high and not highInclusive:
            raise ValueError(f"{name} {QApplication.translate('PFloat', 'should be less than ')} {high}")

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
