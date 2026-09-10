#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Turn a geometry/intent analysis into a concrete, safety-checked Proposal.

Pipeline: description + geometry facts -> choose a catalog strategy (bridge) ->
build the parameterized Proposal -> attach a numeric cell-count effect (estimator)
-> validate against safety limits. Headless and testable without an LLM (the
bridge falls back to a heuristic offline).
"""
from __future__ import annotations

from dataclasses import dataclass

from foammesh.core.project.proposals import Proposal
from foammesh.core.quality.estimator import estimate_cell_count
from .catalog import StrategyContext, build_proposal
from .bridge import suggest_strategy
from .safety import SafetyLimits, validate_proposal


@dataclass
class GeometryFacts:
    bbox_diagonal: float = 1.0
    watertight: bool = True
    base_cells: int = 1000
    wall_patch: str = 'wall'


@dataclass
class BuiltProposal:
    proposal: Proposal
    strategy: str
    estimated_cells: int
    safe: bool
    violations: list


def build(description: str, facts: GeometryFacts | None = None,
          *, use_llm: bool | None = None,
          limits: SafetyLimits | None = None) -> BuiltProposal:
    facts = facts or GeometryFacts()
    strategy = suggest_strategy(description, use_llm=use_llm)

    ctx = StrategyContext(bbox_diagonal=facts.bbox_diagonal,
                          wall_patch=facts.wall_patch)
    proposal = build_proposal(strategy, ctx)

    # numeric effect: estimate cells from the proposed surface refinement levels
    levels = [i.after['level'] for i in proposal.items
              if i.after and 'level' in i.after]
    estimated = estimate_cell_count(facts.base_cells, levels)
    before = estimate_cell_count(facts.base_cells, [])
    for item in proposal.items:
        if item.after and 'level' in item.after:
            item.effect = f'cells ~{before:,} -> ~{estimated:,}'
            break

    result = validate_proposal(proposal, estimated_cells=estimated, limits=limits)
    return BuiltProposal(proposal=proposal, strategy=strategy,
                         estimated_cells=estimated, safe=result.ok,
                         violations=result.violations)
