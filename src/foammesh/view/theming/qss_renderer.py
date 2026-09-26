"""Render the small global QSS template without silent missing placeholders."""
from __future__ import annotations

import re

from .tokens import ThemeTokens, icon_ink_prefix

_PLACEHOLDER = re.compile(r'\{\{([a-z.]+)\}\}')

_DERIVED = {'icon.ink': icon_ink_prefix}
"""Names the template may spell that a theme file does not carry.

DP-203. A theme file is a set of colours and is validated as one, so the name
of an icon directory has no place among them; it is worked out from the ink the
theme writes its words in. Each of these is read only when the template
actually spells it, so a sheet that asks for nothing derived never asks the
tokens for a colour it does not use.
"""


def render_qss(template: str, tokens: ThemeTokens) -> str:
    names = set(_PLACEHOLDER.findall(template))
    missing = names - set(tokens.values) - set(_DERIVED)
    if missing:
        raise ValueError(f'QSS refers to unknown tokens: {", ".join(sorted(missing))}')

    def value(name: str) -> str:
        derive = _DERIVED.get(name)
        return derive(tokens) if derive else tokens.values[name]

    rendered = _PLACEHOLDER.sub(lambda match: value(match.group(1)), template)
    if '{{' in rendered or '}}' in rendered:
        raise ValueError('QSS contains an invalid placeholder')
    return rendered
