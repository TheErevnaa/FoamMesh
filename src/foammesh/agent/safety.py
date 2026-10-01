#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Safety validation for agent proposals.

Hard constraints checked before a proposal is ever shown/applied: refinement
level caps, the estimated mesh against the free RAM (no fixed cell cap since
2026-10-01; a caller may still name one), sane layer counts. Invalid proposals are
rejected (or clamped by the caller) with explicit reasons — the agent cannot push
a workstation-melting mesh through.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from foammesh.core.project.proposals import Proposal


@dataclass
class SafetyLimits:
    #: None: no fixed cell cap -- the estimate is checked against the free
    #: RAM (``resource_budget.mesher_memory_refusal``).
    max_cells: int | None = None
    max_refinement_level: int = 6
    max_layers: int = 20


@dataclass
class SafetyResult:
    ok: bool
    violations: list[str] = field(default_factory=list)


def validate_proposal(proposal: Proposal,
                      estimated_cells: int | None = None,
                      limits: SafetyLimits | None = None) -> SafetyResult:
    limits = limits or SafetyLimits()
    violations: list[str] = []

    for item in proposal.items:
        after = item.after or {}
        level = after.get('level')
        if isinstance(level, int) and level > limits.max_refinement_level:
            violations.append(
                f'{item.action}: refinement level {level} exceeds max '
                f'{limits.max_refinement_level}.')
        n_layers = after.get('n_layers')
        if isinstance(n_layers, int) and n_layers > limits.max_layers:
            violations.append(
                f'{item.action}: {n_layers} layers exceeds max {limits.max_layers}.')

    if estimated_cells is not None:
        if limits.max_cells is not None and estimated_cells > limits.max_cells:
            violations.append(
                f'estimated {estimated_cells:,} cells exceeds max '
                f'{limits.max_cells:,}.')
        from foammesh.support.resource_budget import mesher_memory_refusal

        refusal = mesher_memory_refusal('snappy', estimated_cells)
        if refusal is not None:
            violations.append(f'{refusal}.')

    return SafetyResult(ok=not violations, violations=violations)
