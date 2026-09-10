#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Deterministic OpenFOAM dictionary serialization.

Turns a plain Python mapping into OpenFOAM dictionary syntax with stable,
reproducible formatting (insertion order preserved, fixed indentation, canonical
bool/number rendering). Determinism matters for FoamMesh: dictionaries are
generated outputs of the project state, so identical state must produce
byte-identical dicts — which makes golden-file tests and the audit/transaction
model meaningful.

Pure (no Qt / no app), so it is unit-testable headless. The GUI's existing
``DictionaryFile`` writers remain for now; this is the version-agnostic core that
the headless case builder and tests use.
"""
from __future__ import annotations

from typing import Any

_INDENT = '    '


def _fmt_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float):
        # Avoid locale/precision drift; trim trailing zeros but keep a number.
        return repr(value)
    return str(value)


def _fmt_list(values: list, level: int) -> str:
    # Inline simple scalar lists: (a b c). Nested/dict items go multi-line.
    if all(not isinstance(v, (dict, list)) for v in values):
        return '(' + ' '.join(_fmt_scalar(v) for v in values) + ')'
    pad = _INDENT * (level + 1)
    inner = '\n'.join(pad + _render_value_block(v, level + 1) for v in values)
    return '(\n' + inner + '\n' + _INDENT * level + ')'


def _render_value_block(value: Any, level: int) -> str:
    if isinstance(value, dict):
        return '{\n' + _render_dict(value, level + 1) + '\n' + _INDENT * level + '}'
    if isinstance(value, list):
        return _fmt_list(value, level)
    return _fmt_scalar(value)


def _render_dict(d: dict, level: int) -> str:
    lines = []
    pad = _INDENT * level
    for key, value in d.items():
        if isinstance(value, dict):
            lines.append(f'{pad}{key}')
            lines.append(f'{pad}{{')
            lines.append(_render_dict(value, level + 1))
            lines.append(f'{pad}}}')
        elif isinstance(value, list):
            lines.append(f'{pad}{key} {_fmt_list(value, level)};')
        else:
            lines.append(f'{pad}{key} {_fmt_scalar(value)};')
    return '\n'.join(lines)


def format_dict(d: dict) -> str:
    """Render *d* as an OpenFOAM dictionary body (no FoamFile header)."""
    return _render_dict(d, 0) + '\n'


FOAMFILE_HEADER = """\
/*--------------------------------*- C++ -*----------------------------------*\\
| FoamMesh — generated dictionary (do not edit by hand; edit project state)   |
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      {object};
}}
"""


def format_dictionary_file(object_name: str, d: dict) -> str:
    """Full dictionary file text: FoamFile header + body. Deterministic."""
    return FOAMFILE_HEADER.format(object=object_name) + '\n' + format_dict(d)
