"""Typed facade command envelope shared by GUI, CLI, and transport adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4
import hashlib
import json


class ActorKind(str, Enum):
    HUMAN = 'human'
    AGENT = 'agent'
    API_CLIENT = 'api_client'
    CLI = 'cli'
    SYSTEM = 'system'


class CommandSource(str, Enum):
    GUI = 'gui'
    REST = 'rest'
    CLI = 'cli'
    AUTOMATION = 'automation'
    SYSTEM = 'system'


@dataclass(frozen=True)
class Actor:
    id: str
    kind: ActorKind


@dataclass(frozen=True)
class Command:
    operation: str
    case_id: str
    parameters: dict = field(default_factory=dict)
    actor: Actor = field(default_factory=lambda: Actor('system', ActorKind.SYSTEM))
    source: CommandSource = CommandSource.SYSTEM
    expected_revision: int | None = None
    idempotency_key: str | None = None
    command_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str | None = None
    authorization: dict | None = None
    scope: str = 'case'  # 'case' | 'application' | 'presentation' (§6.3)

    @property
    def is_human_gui_command(self) -> bool:
        return self.actor.kind is ActorKind.HUMAN and self.source is CommandSource.GUI

    def fingerprint(self) -> str:
        """Stable identity used to prevent cross-command idempotency replay."""
        value = {
            'operation': self.operation, 'case_id': self.case_id,
            'parameters': self.parameters, 'actor': {'id': self.actor.id, 'kind': self.actor.kind.value},
            'source': self.source.value, 'expected_revision': self.expected_revision,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()
