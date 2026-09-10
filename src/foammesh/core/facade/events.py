"""Ordered, serializable facade event envelopes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .results import RevisionSnapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


_SECRET_KEY_MARKERS = ('token', 'secret', 'password', 'credential', 'authorization',
                       'api_key', 'apikey', 'bearer')
REDACTED = '***redacted***'


def redact(value: Any) -> Any:
    """Mask secret-bearing values in serializable payloads (§11 redaction)."""
    if isinstance(value, dict):
        return {key: (REDACTED if isinstance(key, str)
                      and any(marker in key.lower() for marker in _SECRET_KEY_MARKERS)
                      and item not in (None, '')
                      else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def serializable(value: Any) -> Any:
    if hasattr(value, 'to_dict'):
        return serializable(value.to_dict())
    if hasattr(value, 'value'):
        return serializable(value.value)
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serializable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class FacadeEvent:
    sequence: int
    event: str
    session_id: str
    case_id: str
    revisions: RevisionSnapshot
    payload: dict
    timestamp: str
    event_id: str
    correlation_id: str | None = None
    actor: dict | None = None
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            'sequence': self.sequence, 'event': self.event,
            'session_id': self.session_id, 'case_id': self.case_id,
            'revisions': self.revisions.to_dict(), 'payload': self.payload,
            'timestamp': self.timestamp, 'event_id': self.event_id,
            'correlation_id': self.correlation_id,
            'actor': self.actor, 'source': self.source,
        }


def build_event(*, sequence: int, event: str, session_id: str, case_id: str,
                revisions: RevisionSnapshot, payload: dict, correlation_id: str | None = None,
                actor: dict | None = None, source: str | None = None) -> FacadeEvent:
    return FacadeEvent(sequence, event, session_id, case_id, revisions,
                       redact(serializable(payload)), utc_now(), str(uuid4()), correlation_id,
                       serializable(actor), source)
