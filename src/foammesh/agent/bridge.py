#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""LLM bridge: pick/parameterize a catalog strategy from a description.

Uses Anthropic Claude when available (``[agent]`` extra + ANTHROPIC_API_KEY),
otherwise falls back to a keyword heuristic so the agent layer is fully usable
(and testable) offline. The LLM only chooses among approved strategies — it never
emits raw dictionary edits.
"""
from __future__ import annotations

import importlib.util
import os

from .catalog import list_strategies

# Latest capable Claude model by default; configurable.
DEFAULT_MODEL = 'claude-opus-4-8'


def is_llm_available() -> bool:
    return (importlib.util.find_spec('anthropic') is not None
            and bool(os.environ.get('ANTHROPIC_API_KEY')))


def _heuristic(description: str) -> str:
    d = (description or '').lower()
    external = ('car', 'drone', 'aircraft', 'wing', 'external', 'aero',
                'building', 'wind', 'vehicle', 'bluff')
    internal = ('duct', 'pipe', 'manifold', 'internal', 'hvac', 'channel',
                'valve', 'pump', 'nozzle')
    if any(k in d for k in external):
        return 'external_aero'
    if any(k in d for k in internal):
        return 'internal_duct'
    return 'simple_block'


def suggest_strategy(description: str, *, use_llm: bool | None = None,
                     model: str = DEFAULT_MODEL) -> str:
    """Return an approved strategy name for *description*."""
    if use_llm is None:
        use_llm = is_llm_available()

    if use_llm:
        try:
            return _llm_select(description, model)
        except Exception:
            pass  # any failure -> safe heuristic fallback
    return _heuristic(description)


def _llm_select(description: str, model: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    strategies = list_strategies()
    prompt = (
        'You are selecting a CFD meshing strategy. Choose exactly one name from '
        f'this list and reply with only that name: {", ".join(strategies)}.\n\n'
        f'Case description: {description}'
    )
    msg = client.messages.create(
        model=model, max_tokens=16,
        messages=[{'role': 'user', 'content': prompt}])
    text = ''.join(getattr(b, 'text', '') for b in msg.content).strip().lower()
    for name in strategies:
        if name in text:
            return name
    return _heuristic(description)
