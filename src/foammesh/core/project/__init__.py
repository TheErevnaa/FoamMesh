#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""FoamMesh project-state engine.

The single source of truth for a project. Every mutation flows through
``ProjectState`` so it can be validated, recorded as a :class:`Transaction`,
published on the :class:`EventBus`, and undone/redone. Agent and "preview"
changes enter as :class:`Proposal` objects that the user accepts or rejects.
"""

from .events import EventBus, Event
from .transactions import Transaction, TransactionLog, Source, TxStatus
from .proposals import Proposal, ProposalStatus
from .history import History
from .state import ProjectState

__all__ = [
    'EventBus', 'Event',
    'Transaction', 'TransactionLog', 'Source', 'TxStatus',
    'Proposal', 'ProposalStatus',
    'History',
    'ProjectState',
]
