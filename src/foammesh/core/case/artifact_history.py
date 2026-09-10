"""Append-only audit history for non-undoable mesh artifact operations."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARTIFACT_HISTORY_FILE = 'artifact_history.json'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass(frozen=True)
class ArtifactHistoryEntry:
    operation: str
    status: str
    command: tuple[str, ...] = ()
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    recovery_id: str | None = None
    recovery_status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    entry_id: str = field(default_factory=lambda: f'ART-{uuid.uuid4().hex[:12]}')
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value['command'] = list(self.command)
        value['kind'] = 'artifact'
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'ArtifactHistoryEntry':
        data = dict(value)
        data.pop('kind', None)
        data['command'] = tuple(str(item) for item in data.get('command', ()))
        return cls(**data)


class ArtifactHistoryStore:
    """Persist artifact events independently of the ProjectState undo stack."""

    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path)
        self.path = self.case_path / 'foammesh' / ARTIFACT_HISTORY_FILE

    def entries(self) -> tuple[ArtifactHistoryEntry, ...]:
        if not self.path.is_file():
            return ()
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
            if document.get('schema_version') != 1 or not isinstance(document.get('entries'), list):
                raise ValueError('artifact history schema is unsupported')
            return tuple(ArtifactHistoryEntry.from_dict(item) for item in document['entries'])
        except (OSError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError(f'cannot read artifact history: {error}') from error

    def append(self, entry: ArtifactHistoryEntry) -> ArtifactHistoryEntry:
        entries = [item.to_dict() for item in self.entries()]
        entries.append(entry.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as output:
                json.dump({'schema_version': 1, 'entries': entries}, output,
                          indent=2, sort_keys=True)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return entry


def record_artifact_event(case_path: str | Path, *, operation: str, status: str,
                          command=(), before_fingerprint=None, after_fingerprint=None,
                          recovery_id=None, recovery_status=None, details=None):
    return ArtifactHistoryStore(case_path).append(ArtifactHistoryEntry(
        operation=operation,
        status=status,
        command=tuple(str(item) for item in command),
        before_fingerprint=before_fingerprint,
        after_fingerprint=after_fingerprint,
        recovery_id=recovery_id,
        recovery_status=recovery_status,
        details=dict(details or {}),
    ))
