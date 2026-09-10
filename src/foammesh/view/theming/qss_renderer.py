"""Render the small global QSS template without silent missing placeholders."""
from __future__ import annotations

import re

from .tokens import ThemeTokens

_PLACEHOLDER = re.compile(r'\{\{([a-z.]+)\}\}')


def render_qss(template: str, tokens: ThemeTokens) -> str:
    names = set(_PLACEHOLDER.findall(template))
    missing = names - set(tokens.values)
    if missing:
        raise ValueError(f'QSS refers to unknown tokens: {", ".join(sorted(missing))}')
    rendered = _PLACEHOLDER.sub(lambda match: tokens.value(match.group(1)), template)
    if '{{' in rendered or '}}' in rendered:
        raise ValueError('QSS contains an invalid placeholder')
    return rendered
