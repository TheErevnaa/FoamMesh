#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""FoamMesh agent strategy layer.

The agent never mutates state directly: it emits :class:`Proposal` objects from an
approved, parameterized **strategy catalog**, validated by **safety** rules, with
human-readable **explanations**. The user accepts/rejects in the GUI; accepting
applies them through ProjectState as source=agent transactions (reversible).

The LLM bridge (optional ``[agent]`` extra / anthropic) only *selects and
parameterizes* a catalog strategy — it cannot bypass the catalog or the State API.
"""

from .catalog import (
    StrategyContext, STRATEGIES, list_strategies, build_proposal,
)
from .safety import SafetyLimits, SafetyResult, validate_proposal
from .explain import explain

# AF7 external agent contract: the vendor-neutral reference client + schemas
# that drive the facade's /api/v1 surface. This supersedes the pre-facade
# proposal/strategy layer above for external automation.
from .client import AgentClient, ConfirmationCallback, PlanRun
from .schemas import openapi_document, publish_schemas, tool_schemas
from .transports import FacadeTransport, InProcessTransport, RestTransport

__all__ = [
    'StrategyContext', 'STRATEGIES', 'list_strategies', 'build_proposal',
    'SafetyLimits', 'SafetyResult', 'validate_proposal', 'explain',
    'AgentClient', 'ConfirmationCallback', 'PlanRun', 'FacadeTransport',
    'InProcessTransport', 'RestTransport', 'openapi_document', 'publish_schemas',
    'tool_schemas',
]
