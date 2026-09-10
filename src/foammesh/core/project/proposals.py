#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Proposals: changes suggested by the agent (or a preview) that the user
accepts, modifies, or rejects before they touch the project state.

A proposal bundles the changes it *would* make as descriptive items, each with a
rationale and (optionally) the estimated numeric effect. Accepting a proposal is
what actually applies the change through ``ProjectState`` (producing real
transactions tagged ``source=agent``). This keeps every agent action visible and
reversible — the agent never mutates state directly.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum


class ProposalStatus(str, Enum):
    PROPOSED = 'proposed'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    SUPERSEDED = 'superseded'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProposalItem:
    """One described change within a proposal (shown in the Agent Changes panel)."""
    action: str
    target: str | None = None
    reason: str = ''
    # free-form before/after for display, e.g. {"level": 2} -> {"level": 4}
    before: dict | None = None
    after: dict | None = None
    effect: str = ''            # e.g. "cell count 1.2M -> 2.8M"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ProposalItem':
        return cls(**d)


@dataclass
class Proposal:
    title: str
    source: str = 'agent'
    strategy: str = ''          # name of the strategy that produced it (phase 10)
    status: ProposalStatus = ProposalStatus.PROPOSED
    items: list[ProposalItem] = field(default_factory=list)
    proposal_id: str = field(default_factory=lambda: f'P-{uuid.uuid4().hex[:10]}')
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            'proposal_id': self.proposal_id,
            'title': self.title,
            'source': self.source,
            'strategy': self.strategy,
            'status': self.status.value,
            'timestamp': self.timestamp,
            'items': [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'Proposal':
        return cls(
            title=d['title'],
            source=d.get('source', 'agent'),
            strategy=d.get('strategy', ''),
            status=ProposalStatus(d.get('status', 'proposed')),
            items=[ProposalItem.from_dict(i) for i in d.get('items', [])],
            proposal_id=d.get('proposal_id', f'P-{uuid.uuid4().hex[:10]}'),
            timestamp=d.get('timestamp', _now()),
        )
