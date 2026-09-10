#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Approved, parameterized meshing strategies.

Each strategy turns a geometry/intent context into a :class:`Proposal` — the same
kind of structured change a human would make through the State API. The agent
picks and parameterizes one of these; it cannot emit arbitrary dictionary edits.
"""
from __future__ import annotations

from dataclasses import dataclass

from foammesh.core.project.proposals import Proposal, ProposalItem


@dataclass
class StrategyContext:
    bbox_diagonal: float = 1.0       # SI metres
    wall_patch: str = 'wall'
    surface_level: int = 4           # desired near-body surface refinement
    n_layers: int = 5
    target_yplus: float = 1.0


def external_aero(ctx: StrategyContext) -> Proposal:
    return Proposal(
        title='External aerodynamics meshing',
        strategy='external_aero',
        items=[
            ProposalItem(action='create_far_field_box', target='domain',
                         after={'size_factor': 10},
                         reason='Far-field box ~10x body for blockage-free flow.'),
            ProposalItem(action='surface_refinement', target=ctx.wall_patch,
                         after={'level': ctx.surface_level},
                         reason='Resolve the body surface and curvature.'),
            ProposalItem(action='feature_refinement', target=ctx.wall_patch,
                         after={'angle': 150},
                         reason='Capture sharp feature edges.'),
            ProposalItem(action='region_refinement', target='wake',
                         after={'level': max(1, ctx.surface_level - 2)},
                         reason='Resolve the downstream wake.'),
            ProposalItem(action='boundary_layers', target=ctx.wall_patch,
                         after={'n_layers': ctx.n_layers, 'target_yplus': ctx.target_yplus},
                         reason='Resolve the near-wall boundary layer.'),
        ],
    )


def internal_duct(ctx: StrategyContext) -> Proposal:
    return Proposal(
        title='Internal duct/flow meshing',
        strategy='internal_duct',
        items=[
            ProposalItem(action='surface_refinement', target=ctx.wall_patch,
                         after={'level': max(2, ctx.surface_level - 1)},
                         reason='Resolve duct walls.'),
            ProposalItem(action='boundary_layers', target=ctx.wall_patch,
                         after={'n_layers': ctx.n_layers, 'target_yplus': ctx.target_yplus},
                         reason='Resolve wall-bounded shear layers.'),
        ],
    )


def simple_block(ctx: StrategyContext) -> Proposal:
    return Proposal(
        title='Simple background mesh (validation)',
        strategy='simple_block',
        items=[ProposalItem(action='base_grid', target='domain',
                            after={'cells': 20},
                            reason='Uniform background grid for validation cases.')],
    )


STRATEGIES = {
    'external_aero': external_aero,
    'internal_duct': internal_duct,
    'simple_block': simple_block,
}


def list_strategies() -> list[str]:
    return list(STRATEGIES.keys())


def build_proposal(name: str, ctx: StrategyContext | None = None) -> Proposal:
    if name not in STRATEGIES:
        raise ValueError(f'unknown strategy: {name!r}; '
                         f'available: {", ".join(STRATEGIES)}')
    return STRATEGIES[name](ctx or StrategyContext())
