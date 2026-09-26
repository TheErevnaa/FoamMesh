#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""One grammar for every complaint the product makes about a typed value.

Before this module there were four validator families -- ``PFloat``,
``support.validation``, ``support.simple_db.simple_schema`` and
``widgets.validation`` -- and between them ten sentence shapes for the same
event: a person typed something into a box and the product will not take it.
Six of those shapes ended with a CPython exception's own text, so a letter in
a numeric field answered with ``could not convert string to float: 'abc'``,
and one answered with the repr of a ``ValidationError`` that already held a
perfectly good sentence.

A complaint is built here in two pieces so that every surface says the same
thing. A *clause* says what is wrong and is always a verb phrase that can
follow the name of a field. A *sentence* is the name of the field, that
clause, and a full stop. Nothing in a clause is ever derived from an
exception: the clause is chosen from what the validator knows -- a bound, a
type, a list of permitted values -- and an exception only selects which clause
to use.

Translation calls wrap the pattern and the values are substituted afterwards,
which is the order a translator needs. ``widgets/validation`` used to format
first and translate the result, which made the lookup key change with every
field name.
"""

from PySide6.QtCore import QCoreApplication


_CONTEXT = 'FieldComplaint'


def _tr(text):
    return QCoreApplication.translate(_CONTEXT, text)


def required_clause() -> str:
    """What is wrong when a required box was left empty."""
    return _tr('is required')


def number_clause() -> str:
    """What is wrong when a numeric box does not hold a number."""
    return _tr('must be a number')


def whole_number_clause() -> str:
    """What is wrong when a whole-number box holds a fraction or worse."""
    return _tr('must be a whole number')


def list_clause() -> str:
    return _tr('must be a list')


def length_clause(size) -> str:
    return _tr('must have {} values').format(size)


def one_of_clause(values) -> str:
    """What is wrong when a value is outside a fixed set.

    ``values`` is what the user may type, not the class that holds them: the
    enum validator used to interpolate the class itself, so the dialog read
    ``Only <enum 'GeometryType'> are allowed.``
    """
    listed = ', '.join(str(value) for value in values)
    return _tr('must be one of: {}').format(listed)


def range_clause(low=None, high=None,
                 lowInclusive=True, highInclusive=True) -> str:
    """What is wrong when a number sits outside its bounds.

    The bounds are spelled out rather than drawn as ``(0 ≤ value ≤ 1)``,
    which paired mathematical notation with the rest of a prose sentence.
    """
    parts = []
    if low is not None:
        parts.append(_tr('at least {}').format(low) if lowInclusive
                     else _tr('greater than {}').format(low))
    if high is not None:
        parts.append(_tr('at most {}').format(high) if highInclusive
                     else _tr('less than {}').format(high))
    if not parts:
        return _tr('is out of range')
    return _tr('must be {}').format(_tr(' and ').join(parts))


def no_greater_than_clause(other) -> str:
    """What is wrong when one field has overtaken another one."""
    return _tr('cannot be greater than {}').format(other)


def sentence(name, clause) -> str:
    """The whole thing a person reads: the field, the clause, a full stop."""
    field = '' if name is None else str(name).strip()
    if not field:
        field = _tr('This value')
    return f'{field} {clause}.'
