#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Human-readable explanation of an agent proposal (for the Agent Changes panel)."""
from __future__ import annotations

from foammesh.core.project.proposals import Proposal


def explain(proposal: Proposal) -> str:
    lines = [f'{proposal.title}  [strategy: {proposal.strategy or "n/a"}]']
    for i, item in enumerate(proposal.items, 1):
        detail = item.reason or ''
        if item.effect:
            detail = f'{detail} ({item.effect})' if detail else item.effect
        target = f' on {item.target}' if item.target else ''
        lines.append(f'  {i}. {item.action}{target}: {detail}')
    return '\n'.join(lines)
