#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The project's display unit for lengths.

Plan 40 UXB3 (section 4.3.3). Storage stays SI: the case record and the
facade hold metres, always. This module only decides how a length is *shown*
and *typed*: ``m``, ``mm`` or ``in``. It defaults to the import unit of the
first geometry (Q4) and is ``m`` when nothing was imported. No Qt here.
"""
from __future__ import annotations

import weakref
from typing import Callable

#: Unit name -> metres per unit.
UNIT_METRES = {'m': 1.0, 'mm': 1.0e-3, 'in': 0.0254}
#: Import units that are not display units, and the nearest one that is.
_ALIASES = {'cm': 'mm', 'um': 'mm', 'µm': 'mm', 'ft': 'in', 'inch': 'in',
            'inches': 'in', 'meter': 'm', 'metre': 'm', 'meters': 'm',
            'metres': 'm', 'millimeter': 'mm', 'millimetre': 'mm'}
#: Significant figures a number shows with; full precision on focus.
SIGNIFICANT_FIGURES = 4

_state = {'unit': 'm', 'chosen': False}
_listeners: list = []


def _normal(unit) -> str | None:
    name = str(unit or '').strip().lower()
    name = _ALIASES.get(name, name)
    return name if name in UNIT_METRES else None


def current() -> str:
    """The unit lengths are shown in: ``m``, ``mm`` or ``in``."""
    return _state['unit']


def is_chosen() -> bool:
    """True once a unit was imported or chosen; readouts then stop guessing."""
    return bool(_state['chosen'])


def metres_per_unit() -> float:
    return UNIT_METRES[_state['unit']]


def set_unit(unit) -> bool:
    """Choose the display unit. False when the name is not one of ours."""
    name = _normal(unit)
    if name is None:
        return False
    _state['chosen'] = True
    _apply(name)
    return True


def adopt_import_unit(unit) -> None:
    """Default to the first geometry's import unit, unless one was chosen."""
    if _state['chosen']:
        return
    name = _normal(unit)
    if name is not None:
        _state['chosen'] = True
        _apply(name)


def reset() -> None:
    """A new or reopened project starts again from ``m``."""
    _state['chosen'] = False
    _apply('m')


def _apply(name: str) -> None:
    changed = name != _state['unit']
    _state['unit'] = name
    if changed:
        for ref in list(_listeners):
            callback = ref()
            if callback is None:
                _listeners.remove(ref)
            else:
                try:
                    callback()
                except RuntimeError as error:
                    # The owner is alive in Python but Qt already deleted its
                    # widget (a window closed with the unit still subscribed):
                    # forget it rather than fail the next project's reset.
                    if 'already deleted' not in str(error):
                        raise
                    _listeners.remove(ref)


def subscribe(callback: Callable[[], None]) -> None:
    """Call *callback* when the unit changes (held weakly)."""
    try:
        ref = weakref.WeakMethod(callback)
    except TypeError:
        ref = (lambda cb=callback: cb)
    _listeners.append(ref)


def to_display(metres: float) -> float:
    return float(metres) / metres_per_unit()


def from_display(value: float) -> float:
    return float(value) * metres_per_unit()


def significant(value: float, figures: int = SIGNIFICANT_FIGURES) -> str:
    """*value* to *figures* significant figures, no exponent for plain sizes."""
    number = float(value)
    if number == 0:
        return '0'
    text = f'{number:.{figures}g}'
    if 'e' in text:
        return text.replace('e+', 'e')
    return text


def format_length(metres: float, *, unit: bool = True) -> str:
    """A length stored in metres as the project shows it: ``12.5 mm``."""
    text = significant(to_display(metres))
    return f'{text} {current()}' if unit else text


def adopt_from_case(case_path) -> None:
    """A case opened from disk: start from the unit its first geometry came in."""
    reset()
    try:
        from foammesh.core.geometry.store import GeometryArtifactStore
        for entry in GeometryArtifactStore(case_path).entries():
            # FIX-K (WALK-2): a surface import (STL, OBJ) records the unit
            # the file was written in as 'source_unit' and has no
            # 'declared_unit' or 'unit', so such a case reopened in metres
            # however it was left.
            adopt_import_unit(entry.get('declared_unit')
                              or entry.get('source_unit')
                              or entry.get('unit'))
            if is_chosen():
                break
    except Exception:                                       # noqa: BLE001
        # No manifest, or one this build cannot read: the display stays in m.
        pass
