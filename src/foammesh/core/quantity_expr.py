#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A small safe evaluator for typed lengths.

Plan 40 UXD6 (section 4.3.7, F4a). A length field accepts ``10 mm``,
``0.5*12`` and ``=diag/40``. This is a hand-written tokenizer and recursive
descent parser: numbers, units, ``+ - * /`` (and ``x`` / the multiplication
and division signs), parentheses, and the names ``diag``, ``bbox_x``,
``bbox_y``, ``bbox_z`` and ``cell``. There is no Python ``eval`` and no
attribute or call syntax at all. No Qt here.

A value is a number of metres (``dim`` 1) or a bare number (``dim`` 0). A
bare number typed alone, or left bare by the whole expression, counts in the
project display unit. Storage stays metres: the result is metres.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from foammesh.core import display_unit

#: Unit name -> metres per unit.
UNITS = {
    'm': 1.0, 'mm': 1.0e-3, 'cm': 1.0e-2, 'um': 1.0e-6, 'µm': 1.0e-6,
    'km': 1.0e3, 'in': 0.0254, 'inch': 0.0254, 'ft': 0.3048,
}
#: The names an expression may use, each a length in metres.
NAMES = ('diag', 'bbox_x', 'bbox_y', 'bbox_z', 'cell')

_scene: dict = {}


class QuantityError(ValueError):
    """The text is not a quantity; ``str()`` is one line the reader can act on."""


def set_scene(*, diag=None, bbox=None, cell=None) -> None:
    """Say what the names mean (metres). ``bbox`` is the (x, y, z) extent."""
    if bbox is not None:
        bx, by, bz = (float(v) for v in bbox)
        _scene['bbox_x'], _scene['bbox_y'], _scene['bbox_z'] = bx, by, bz
        if diag is None:
            diag = math.sqrt(bx * bx + by * by + bz * bz)
    if diag is not None:
        _scene['diag'] = float(diag)
    if cell is not None:
        _scene['cell'] = float(cell)


def clear_scene() -> None:
    _scene.clear()


def scene_value(name: str):
    return _scene.get(name)


@dataclass(frozen=True)
class _Value:
    number: float        # metres when dim == 1, the bare number when dim == 0
    dim: int


_TOKEN = re.compile(
    r'\s*(?:(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)'
    r'|(?P<word>[A-Za-zµ_][A-Za-z0-9_µ]*)'
    r'|(?P<op>[-+*/()×÷−·]))')

_OPS = {'×': '*', '·': '*', '÷': '/', '−': '-'}


def _tokens(text: str):
    position = 0
    out = []
    while position < len(text):
        if text[position:].strip() == '':
            break
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            bad = text[position:].strip()[:1]
            raise QuantityError(f'"{bad}" is not understood here')
        position = match.end()
        if match.group('num') is not None:
            out.append(('num', float(match.group('num'))))
        elif match.group('word') is not None:
            out.append(('word', match.group('word')))
        else:
            op = match.group('op')
            out.append(('op', _OPS.get(op, op)))
    return out


class _Parser:
    def __init__(self, tokens, per: float):
        self.t = tokens
        self.i = 0
        self.per = per          # metres per bare-number unit

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        token = self.peek()
        self.i += 1
        return token

    def parse(self) -> _Value:
        if not self.t:
            raise QuantityError('Enter a number')
        value = self.expr()
        if self.i < len(self.t):
            raise QuantityError(f'"{self.t[self.i][1]}" is not understood here')
        return value

    def _metres(self, value: _Value) -> float:
        return value.number if value.dim else value.number * self.per

    def expr(self) -> _Value:
        left = self.term()
        while self.peek() in (('op', '+'), ('op', '-')):
            op = self.take()[1]
            right = self.term()
            if left.dim == right.dim:
                number = (left.number + right.number if op == '+'
                          else left.number - right.number)
                left = _Value(number, left.dim)
            else:
                a, b = self._metres(left), self._metres(right)
                left = _Value(a + b if op == '+' else a - b, 1)
        return left

    def term(self) -> _Value:
        left = self.unary()
        while True:
            kind, text = self.peek()
            if (kind, text) in (('op', '*'), ('op', '/')):
                self.take()
                right = self.unary()
                left = self._mul(left, right, text)
            elif kind == 'word' and text.lower() == 'x' and self._x_is_times():
                self.take()
                right = self.unary()
                left = self._mul(left, right, '*')
            else:
                return left

    def _x_is_times(self) -> bool:
        return self.i + 1 < len(self.t)

    @staticmethod
    def _mul(left: _Value, right: _Value, op: str) -> _Value:
        if op == '*':
            if left.dim and right.dim:
                raise QuantityError('A length times a length is an area, not a length')
            return _Value(left.number * right.number, left.dim + right.dim)
        if right.number == 0:
            raise QuantityError('Cannot divide by zero')
        if right.dim > left.dim:
            raise QuantityError('Cannot divide a number by a length')
        return _Value(left.number / right.number, left.dim - right.dim)

    def unary(self) -> _Value:
        if self.peek() in (('op', '-'), ('op', '+')):
            sign = self.take()[1]
            value = self.unary()
            return _Value(-value.number if sign == '-' else value.number, value.dim)
        return self.atom()

    def atom(self) -> _Value:
        kind, text = self.take()
        if kind == 'num':
            return self.unit_suffix(_Value(text, 0))
        if kind == 'op' and text == '(':
            value = self.expr()
            if self.take() != ('op', ')'):
                raise QuantityError('A closing bracket is missing')
            return self.unit_suffix(value)
        if kind == 'word':
            name = text.lower()
            if name in NAMES:
                known = _scene.get(name)
                if known is None:
                    raise QuantityError(
                        f'"{text}" is not known until a geometry is loaded')
                return _Value(float(known), 1)
            if name in UNITS or text in UNITS:
                raise QuantityError(f'"{text}" needs a number before it')
            raise QuantityError(f'"{text}" is not a unit or a known name')
        if kind is None:
            raise QuantityError('The expression ends too early')
        raise QuantityError(f'"{text}" is not understood here')

    def unit_suffix(self, value: _Value) -> _Value:
        kind, text = self.peek()
        if kind != 'word' or value.dim:
            return value
        key = text if text in UNITS else text.lower()
        if key not in UNITS:
            if key == 'x' or key in NAMES:
                return value
            raise QuantityError(f'"{text}" is not a unit I know (m, mm, cm, in, ft)')
        self.take()
        return _Value(value.number * UNITS[key], 1)


def evaluate(text: str, *, per: float | None = None) -> float:
    """The metres *text* stands for.

    A leading ``=`` is allowed and means nothing more. A bare number counts
    in the display unit (``per`` metres), so ``0.5*12`` is six display units.
    Raises :class:`QuantityError` with one readable line.
    """
    body = str(text).strip()
    if body.startswith('='):
        body = body[1:]
    scale = display_unit.metres_per_unit() if per is None else float(per)
    value = _Parser(_tokens(body), scale).parse()
    metres = value.number if value.dim else value.number * scale
    if not math.isfinite(metres):
        raise QuantityError('That is not a finite number')
    return metres


def is_plain_number(text: str) -> bool:
    """True for a bare number, which needs no 'equals' hint beside it."""
    try:
        float(str(text).strip())
        return True
    except ValueError:
        return False
