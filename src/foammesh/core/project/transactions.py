#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Transactions: the audit record of every project mutation.

Each accepted change to the project state produces one ``Transaction`` with its
source (gui/api/cli/agent), a human-readable action and reason, and a status.
The append-only ``TransactionLog`` is persisted with the project so the full
history is explainable and reproducible.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum


class Source(str, Enum):
    GUI = 'gui'
    API = 'api'
    CLI = 'cli'
    AGENT = 'agent'
    SYSTEM = 'system'


class TxStatus(str, Enum):
    APPLIED = 'applied'
    PROPOSED = 'proposed'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    REVERTED = 'reverted'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Transaction:
    action: str
    source: Source = Source.GUI
    target: str | None = None          # id/path of the affected object, if any
    reason: str = ''
    status: TxStatus = TxStatus.APPLIED
    tx_id: str = field(default_factory=lambda: f'TX-{uuid.uuid4().hex[:12]}')
    timestamp: str = field(default_factory=_now)
    proposal_id: str | None = None     # set when the tx came from a proposal

    def to_dict(self) -> dict:
        d = asdict(self)
        d['source'] = self.source.value
        d['status'] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Transaction':
        d = dict(d)
        d['source'] = Source(d.get('source', 'gui'))
        d['status'] = TxStatus(d.get('status', 'applied'))
        return cls(**d)


class TransactionLog:
    """Append-only, ordered log of transactions (newest last)."""

    def __init__(self, transactions: list[Transaction] | None = None):
        self._txs: list[Transaction] = list(transactions or [])

    def append(self, tx: Transaction) -> Transaction:
        self._txs.append(tx)
        return tx

    def all(self) -> list[Transaction]:
        return list(self._txs)

    def last(self) -> Transaction | None:
        return self._txs[-1] if self._txs else None

    def get(self, tx_id: str) -> Transaction | None:
        return next((t for t in self._txs if t.tx_id == tx_id), None)

    def __len__(self) -> int:
        return len(self._txs)

    # persistence ---------------------------------------------------------
    def to_list(self) -> list[dict]:
        return [t.to_dict() for t in self._txs]

    @classmethod
    def from_list(cls, items: list[dict]) -> 'TransactionLog':
        return cls([Transaction.from_dict(i) for i in (items or [])])
