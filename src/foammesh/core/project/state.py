#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""ProjectState: the single mutation entry point for a FoamMesh project.

It wraps the existing ``SimpleDB``-based configuration (``checkout`` / ``commit``
/ ``toYaml`` / ``validateData``) and layers on the engine guarantees:

* every commit is recorded as a :class:`Transaction` and published on the bus,
* a pre-change snapshot is captured so the change can be undone/redone,
* agent/preview changes arrive as :class:`Proposal` objects and only touch the
  state when accepted.

Usage mirrors the current GUI pattern, just routed through the state object::

    data = state.checkout()
    data.setValue('.../level', 4)
    state.commit(data, action='set surface refinement', target='body_wall',
                 source=Source.GUI, reason='curvature')

The wrapped db is duck-typed; it must provide ``checkout``, ``commit``,
``toYaml``, ``validateData`` and expose ``_content``/``_modified`` (the
``Configurations`` class does). OpenFOAM dictionaries are generated from this
state by the openfoam writers — they are outputs, never edited directly.
"""
from __future__ import annotations

import yaml

from .events import EventBus, Event
from .history import History
from .proposals import Proposal, ProposalStatus
from .transactions import Transaction, TransactionLog, Source, TxStatus


class ProjectState:
    def __init__(self, db, *, bus: EventBus | None = None,
                 history: History | None = None,
                 log: TransactionLog | None = None):
        self._db = db
        self.bus = bus or EventBus()
        self.history = history or History()
        self.log = log or TransactionLog()
        self._proposals: dict[str, Proposal] = {}

    # ----- reads / checkout ------------------------------------------------
    @property
    def db(self):
        return self._db

    def checkout(self, path: str = ''):
        """Return an editable copy to mutate before committing."""
        return self._db.checkout(path)

    # ----- the single write path ------------------------------------------
    def commit(self, data, *, action: str, source: Source = Source.GUI,
               target: str | None = None, reason: str = '',
               proposal_id: str | None = None) -> Transaction:
        snapshot = self._snapshot()
        self._db.commit(data)
        self.history.record(snapshot, action)
        tx = self.log.append(Transaction(
            action=action, source=source, target=target, reason=reason,
            proposal_id=proposal_id, status=TxStatus.APPLIED))
        self.bus.publish(Event.TRANSACTION_APPLIED, transaction=tx)
        return tx

    # ----- undo / redo -----------------------------------------------------
    def can_undo(self) -> bool:
        return self.history.can_undo()

    def can_redo(self) -> bool:
        return self.history.can_redo()

    def undo_label(self) -> str:
        return self.history.undo_label()

    def redo_label(self) -> str:
        return self.history.redo_label()

    def undo(self) -> Transaction | None:
        label = self.history.undo_label()
        snap = self.history.undo(self._snapshot())
        if snap is None:
            return None
        self._restore(snap)
        tx = self.log.append(Transaction(
            action=f'undo {label}'.strip(), source=Source.SYSTEM, status=TxStatus.REVERTED))
        self.bus.publish(Event.UNDONE, transaction=tx)
        return tx

    def redo(self) -> Transaction | None:
        label = self.history.redo_label()
        snap = self.history.redo(self._snapshot())
        if snap is None:
            return None
        self._restore(snap)
        tx = self.log.append(Transaction(
            action=f'redo {label}'.strip(), source=Source.SYSTEM, status=TxStatus.APPLIED))
        self.bus.publish(Event.REDONE, transaction=tx)
        return tx

    # ----- proposals (agent / preview) ------------------------------------
    def propose(self, proposal: Proposal) -> Proposal:
        self._proposals[proposal.proposal_id] = proposal
        self.bus.publish(Event.PROPOSAL_CREATED, proposal=proposal)
        return proposal

    def proposals(self) -> list[Proposal]:
        return list(self._proposals.values())

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._proposals.get(proposal_id)

    def accept(self, proposal_id: str, apply_fn) -> list[Transaction]:
        """Apply a proposal. *apply_fn(state, proposal)* performs the real
        commits (each tagged source=AGENT, proposal_id set) and returns them.
        """
        proposal = self._proposals[proposal_id]
        txs = apply_fn(self, proposal) or []
        proposal.status = ProposalStatus.ACCEPTED
        self.bus.publish(Event.PROPOSAL_ACCEPTED, proposal=proposal, transactions=txs)
        return txs

    def reject(self, proposal_id: str) -> Proposal:
        proposal = self._proposals[proposal_id]
        proposal.status = ProposalStatus.REJECTED
        self.bus.publish(Event.PROPOSAL_REJECTED, proposal=proposal)
        return proposal

    # ----- persistence hooks (wired into file_db by the project layer) -----
    def dump_history(self) -> list[dict]:
        return self.log.to_list()

    def load_history(self, items: list[dict]) -> None:
        self.log = TransactionLog.from_list(items)

    def dump_proposals(self) -> list[dict]:
        return [p.to_dict() for p in self._proposals.values()]

    def load_proposals(self, items: list[dict]) -> None:
        from .proposals import Proposal
        self._proposals = {}
        for d in (items or []):
            p = Proposal.from_dict(d)
            self._proposals[p.proposal_id] = p

    # ----- snapshot helpers ------------------------------------------------
    def _snapshot(self) -> str:
        return self._db.toYaml()

    def _restore(self, snapshot: str) -> None:
        # Re-validate the snapshot into the live db content. In-session
        # snapshots always use the current exact schema.
        self._db._content = self._db.validateData(yaml.full_load(snapshot))
        self._db._modified = True
