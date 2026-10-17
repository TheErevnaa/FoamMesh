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
``snapshot`` and ``restore`` (the ``Configurations`` class does). OpenFOAM
dictionaries are generated from this state by the openfoam writers — they are
outputs, never edited directly.
"""
from __future__ import annotations

from .events import EventBus, Event
from .history import History
from .proposals import Proposal, ProposalStatus
from .transactions import Transaction, TransactionLog, Source, TxStatus


class StageStepError(RuntimeError):
    """The next step moves more than the settings, which only the facade can do."""

    def __init__(self, label: str):
        super().__init__(f'{label!r} is a step that also moves the mesh, the '
                         'geometry or the task state; it is undone '
                         'with what it restores')
        self.label = label


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

    def replace_document(self, content, *, action: str,
                         source: Source = Source.SYSTEM) -> Transaction:
        """Replace the whole document as one recorded, undoable change.

        Plan 35 CR9: restoring unsaved changes after a crash is an ordinary
        change set, so it is undoable and itself journalled.
        """
        snapshot = self._snapshot()
        self._db.restore(content)
        self.history.record(snapshot, action)
        tx = self.log.append(Transaction(
            action=action, source=source, status=TxStatus.APPLIED))
        self.bus.publish(Event.TRANSACTION_APPLIED, transaction=tx)
        return tx

    def record_stage(self, label: str, companion: dict, *,
                     source: Source = Source.SYSTEM) -> Transaction:
        """Put a mesh stage run in the one history (UXD7).

        The settings do not change, but the run is a step Undo can take back:
        the entry holds the settings as they stand with *companion*, the mesh
        actions for each direction, so one Undo returns the previous stage and
        the settings it was made from together.
        """
        self.history.record(self._snapshot(), label, companion)
        # Not published as TRANSACTION_APPLIED: nothing authored changed, so
        # the authored revision, the change sets and the autosave journal
        # stay as they were.
        return self.log.append(Transaction(
            action=label, source=source, status=TxStatus.APPLIED))

    def history_mark(self) -> int:
        return self.history.mark()

    def record_step(self, label: str, companion: dict, mark: int, *,
                    source: Source = Source.SYSTEM) -> Transaction:
        """Put an operation that moved more than the settings in the history.

        *mark* is :meth:`history_mark` taken before it ran; any settings the
        operation committed on the way fold into this one step (UXD7).
        """
        self.history.squash_since(mark, self._snapshot(), label, companion)
        return self.log.append(Transaction(
            action=label, source=source, status=TxStatus.APPLIED))

    # ----- undo / redo -----------------------------------------------------
    def can_undo(self) -> bool:
        return self.history.can_undo()

    def can_redo(self) -> bool:
        return self.history.can_redo()

    def undo_label(self) -> str:
        return self.history.undo_label()

    def redo_label(self) -> str:
        return self.history.redo_label()

    def undo(self, *, stage_applied: bool = False) -> Transaction | None:
        """Undo the newest step.

        A mesh stage step also moves the mesh on disk, which only the facade
        can do; it passes ``stage_applied=True`` once it has. Anyone else
        reaching such a step is refused rather than leave the settings and
        the mesh disagreeing.
        """
        if self.history.undo_companion() and not stage_applied:
            raise StageStepError(self.history.undo_label())
        label = self.history.undo_label()
        snap = self.history.undo(self._snapshot())
        if snap is None:
            return None
        self._restore(snap)
        tx = self.log.append(Transaction(
            action=f'undo {label}'.strip(), source=Source.SYSTEM, status=TxStatus.REVERTED))
        self.bus.publish(Event.UNDONE, transaction=tx)
        return tx

    def redo(self, *, stage_applied: bool = False) -> Transaction | None:
        if self.history.redo_companion() and not stage_applied:
            raise StageStepError(self.history.redo_label())
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
    def _snapshot(self):
        # DP-72. This used to be ``self._db.toYaml()``, and undo parsed the
        # text back and re-validated it. Nobody ever read the text: a snapshot
        # goes to `History`, which holds it opaquely, and comes back only to
        # `_restore`. MEASURED on the reference fixture -- 4.832 ms to
        # serialise against 0.112 ms to copy, and that dump was ~80% of the
        # facade's owner-loop budget on every committed change.
        return self._db.snapshot()

    def _restore(self, snapshot) -> None:
        # In-session snapshots always use the current exact schema, so there
        # is nothing to re-validate: this content was valid when it was taken
        # and copying does not change it.
        self._db.restore(snapshot)
