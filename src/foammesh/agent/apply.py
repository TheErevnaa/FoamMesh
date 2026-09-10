#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Apply an accepted proposal to the project state as agent transactions.

Each proposal item becomes exactly one transaction tagged ``source=agent`` (and
``proposal_id``), so the whole proposal is auditable and undoable. Items that map
to a concrete configuration field (e.g. base grid) mutate state; geometry-
dependent items (surface refinement, layers — which need imported geometry) are
*recorded* as agent transactions documenting the intent, to be realised once the
geometry/refinement objects exist.

This is the generic proposal->state mapper used by ProjectState.accept and the API.
"""
from __future__ import annotations

from foammesh.core.project import Source
from foammesh.core.project.proposals import Proposal, ProposalStatus
from foammesh.core.project.transactions import Transaction, TxStatus


def apply_proposal(state, proposal: Proposal) -> dict:
    applied: list[Transaction] = []

    for item in proposal.items:
        after = item.after or {}
        action = f'agent: {item.action}'

        if item.action == 'base_grid' and 'cells' in after:
            data = state.checkout()
            for axis in ('numCellsX', 'numCellsY', 'numCellsZ'):
                data.setValue(f'baseGrid/{axis}', str(after['cells']))
            tx = state.commit(data, action=action, source=Source.AGENT,
                              target=item.target, reason=item.reason,
                              proposal_id=proposal.proposal_id)
            applied.append(tx)
        else:
            # record intent (geometry-dependent items realised when geometry exists)
            tx = state.log.append(Transaction(
                action=action, source=Source.AGENT, target=item.target,
                reason=item.reason, status=TxStatus.APPLIED,
                proposal_id=proposal.proposal_id))
            applied.append(tx)

    proposal.status = ProposalStatus.ACCEPTED
    return {'applied': applied, 'count': len(applied)}


def accept_proposal(state, proposal_id: str) -> dict:
    """Convenience: accept a registered proposal via ProjectState.accept()."""
    result_holder = {}

    def _apply(st, prop):
        result = apply_proposal(st, prop)
        result_holder.update(result)
        return result['applied']

    state.accept(proposal_id, _apply)
    return result_holder
