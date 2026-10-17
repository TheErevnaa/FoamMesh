"""Application settings state kept separate from any case revision domain."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import RevisionConflictError


def _reach_app_settings(changed: dict) -> None:
    """SG17 (defect 9): a patched setting the app knows is stored in the
    app's own settings file, so the GUI, the CLI and a restart all read it.
    Keys the app has no setting for stay in the session only."""
    from foammesh.settings.app_settings import SettingKey, appSettings
    known = {key.value: key for key in SettingKey}
    for name, value in changed.items():
        key = known.get(name)
        if key is not None:
            appSettings._set(key, value)


@dataclass
class ApplicationSession:
    values: dict = field(default_factory=dict)
    settings_revision: int = 0
    persistence_path: Path | None = None

    def __post_init__(self):
        if self.persistence_path is not None:
            self.persistence_path = Path(self.persistence_path)
            self._load()

    def snapshot(self) -> dict:
        return {'settings_revision': self.settings_revision, 'values': dict(self.values)}

    def patch(self, values: dict, *, expected_revision: int | None = None) -> dict:
        if expected_revision is not None and expected_revision != self.settings_revision:
            raise RevisionConflictError('application settings changed', details={
                'expected_settings_revision': expected_revision,
                'current_settings_revision': self.settings_revision,
            })
        changed = {key: value for key, value in values.items() if self.values.get(key) != value}
        if changed:
            self.values.update(changed)
            _reach_app_settings(changed)
            self.settings_revision += 1
            self._save()
        return {'changed_settings': sorted(changed), **self.snapshot()}

    def _load(self) -> None:
        try:
            stored = json.loads(self.persistence_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if isinstance(stored, dict) and isinstance(stored.get('values'), dict):
            self.values = stored['values']
            self.settings_revision = int(stored.get('settings_revision', 0) or 0)

    def _save(self) -> None:
        if self.persistence_path is None:
            return
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.persistence_path.with_suffix('.tmp')
        try:
            temporary.write_text(
                json.dumps(self.snapshot(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(temporary, self.persistence_path)
        finally:
            temporary.unlink(missing_ok=True)
