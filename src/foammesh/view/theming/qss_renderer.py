"""Render the small global QSS template without silent missing placeholders."""
from __future__ import annotations

import re

from . import metrics
from .tokens import ALPHA_TOKENS, ThemeTokens, icon_ink_prefix, split_alpha

_PLACEHOLDER = re.compile(r'\{\{([a-z0-9.]+)\}\}')

_NUMERIC = {
    'radius.control': metrics.RADIUS_CONTROL, 'radius.group': metrics.RADIUS_GROUP,
    'radius.menu': metrics.RADIUS_MENU, 'radius.float': metrics.RADIUS_FLOAT,
    'radius.sheet': metrics.RADIUS_SHEET,
    'type.body': metrics.TYPE_BODY, 'type.small': metrics.TYPE_SMALL,
    'type.caption': metrics.TYPE_CAPTION, 'type.caps': metrics.TYPE_CAPS,
    'type.h3': metrics.TYPE_H3, 'type.h2': metrics.TYPE_H2,
    'space.xs': metrics.SPACE_XS, 'space.sm': metrics.SPACE_SM, 'space.md': metrics.SPACE_MD,
    'space.lg': metrics.SPACE_LG, 'space.xl': metrics.SPACE_XL,
}
"""Plan 44 MF0. Scale placeholders, read from :mod:`metrics`.

They render as a bare number (``3``, ``12.5``); the template spells the unit,
``border-radius: {{radius.control}}px``.
"""


def _number(value) -> str:
    return f'{value:g}'


def _colour(name: str, value: str) -> str:
    """An alpha-capable token with an alpha channel becomes ``rgba()``."""
    if name in ALPHA_TOKENS and len(value) == 9:
        hexed, alpha = split_alpha(value)
        red, green, blue = (int(hexed[i:i + 2], 16) for i in (1, 3, 5))
        return f'rgba({red}, {green}, {blue}, {int(value[7:9], 16)})'
    return value

_DERIVED = {'icon.ink': icon_ink_prefix}
"""Names the template may spell that a theme file does not carry.

DP-203. A theme file is a set of colours and is validated as one, so the name
of an icon directory has no place among them; it is worked out from the ink the
theme writes its words in. Each of these is read only when the template
actually spells it, so a sheet that asks for nothing derived never asks the
tokens for a colour it does not use.
"""


_TWO_VALUE_PADDING = re.compile(r'(?<![-\w])padding:\s*(\d+)px\s+(\d+)px\s*;')
_ONE_VALUE_PADDING = re.compile(r'(?<![-\w])padding:\s*\d+px\s*;')
_RULE = re.compile(r'([^{}]+)\{([^{}]*)\}')
_COMMENT = re.compile(r'/\*.*?\*/', re.S)

#: Rules whose padding is not a control's height: tooltips, menus, the menu bar,
#: dock titles, labels and group boxes keep their own arithmetic.
_DENSITY_EXEMPT = ('QToolTip', 'QMenuBar', 'QMenu', 'QDockWidget', 'QLabel',
                   'QGroupBox')


def apply_density(sheet: str, density: str) -> str:
    """Compact density: the sheet as written, trimmed from 28 px to 24 px.

    Plan 44 MF19 (decision Q1, DP-189 restated: one control height per
    density). The comfortable sheet is written so a control adds up to 28 px;
    compact takes ``COMPACT_TRIM_PX`` (4) off every control by taking half of
    it from the vertical padding above and half below the text. The trim is
    applied once to the comfortable value, so it never stacks on a rule that
    is already compact. Only the two-value ``padding: V H`` rules of controls
    are touched (buttons, tool buttons, tabs, inputs, header sections, item
    rows, and each one's focus ring); a padding of 1 px is already as tight
    as a small chip can be and stays, and :data:`_DENSITY_EXEMPT` rules keep
    their own arithmetic.
    """
    if density != 'compact':
        return sheet
    from . import appearance

    each = appearance.COMPACT_TRIM_PX // 2

    def narrow(match):
        vertical = int(match.group(1))
        if vertical < 2:
            return match.group(0)
        return f'padding: {max(0, vertical - each)}px {match.group(2)}px;'

    def rule(match):
        selector = _COMMENT.sub('', match.group(1))
        if any(name in selector for name in _DENSITY_EXEMPT):
            return match.group(0)
        body = _TWO_VALUE_PADDING.sub(narrow, match.group(2))
        if selector.strip() == 'QAbstractItemView::item':
            # A cell widget is inset by the item padding, and a 24 px row
            # cannot spare 3 px each side for a 22 px editor.
            body = _ONE_VALUE_PADDING.sub('padding: 1px;', body)
        return match.group(1) + '{' + body + '}'

    return _RULE.sub(rule, sheet)


#: Plan 44 MF19. The table skin the mockup draws, spelled once for both
#: densities: a ``background.raised`` header, quiet row lines, radius 3.
_TABLE_SKIN = """
/* Tables (Plan 44 MF19) */
QTableView {
    gridline-color: {{border.quiet}};
    border-radius: {{radius.control}}px;
}
QTableView QHeaderView::section {
    background: {{background.raised}};
}

/* Single-line editors (Plan 44 MF19) */
/* The editors used to take their height from the font alone (25 px for a
 * line edit, 27 for a spin box) while a button was 28. 20 px of content +
 * 2 px of border + two 3 px paddings is 28 (comfortable); the compact trim
 * takes 2 px off each padding and gives 24. The focus ring keeps both. */
QLineEdit, QComboBox {
    min-height: 20px;
}
/* The windows11 style adds 2 px of its own to a spin box. */
QSpinBox, QDoubleSpinBox, QAbstractSpinBox, QDateEdit, QTimeEdit {
    min-height: 18px;
}
/* An editor set into a table cell sits inside the row less its grid line, so
 * it is two pixels shorter than the free-standing one in both densities. */
QTableView QLineEdit, QTableView QComboBox {
    min-height: 18px;
}
QTableView QSpinBox, QTableView QDoubleSpinBox, QTableView QAbstractSpinBox {
    min-height: 16px;
}
"""


def render_qss(template: str, tokens: ThemeTokens, density: str = 'comfortable') -> str:
    skin_names = set(_PLACEHOLDER.findall(_TABLE_SKIN))
    if skin_names <= set(tokens.values) | set(_NUMERIC):
        template = template + _TABLE_SKIN
    names = set(_PLACEHOLDER.findall(template))
    missing = names - set(tokens.values) - set(_DERIVED) - set(_NUMERIC)
    if missing:
        raise ValueError(f'QSS refers to unknown tokens: {", ".join(sorted(missing))}')

    def value(name: str) -> str:
        if name in _NUMERIC:
            return _number(_NUMERIC[name])
        derive = _DERIVED.get(name)
        return derive(tokens) if derive else _colour(name, tokens.values[name])

    rendered = _PLACEHOLDER.sub(lambda match: value(match.group(1)), template)
    if '{{' in rendered or '}}' in rendered:
        raise ValueError('QSS contains an invalid placeholder')
    return apply_density(rendered, density)
