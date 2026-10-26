"""Recently opened Gmsh model files (Plan 48 GM11b; Gmsh's File > Open recent).

Gmsh keeps the last files File > Open and Merge read and binds Shift+Ctrl+R
to "open the next-to-last one".  The list is kept per user, in
``~/.FoamMesh/gmsh-recent-files.json``, next to the discovery file; the
Gmsh route's File menu shows it, and a file that is gone is greyed with its
name rather than dropped, so a moved folder is noticed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_RECENT = 10
FILE_NAME = 'gmsh-recent-files.json'


def default_path() -> Path:
    return Path.home() / '.FoamMesh' / FILE_NAME


class RecentModelFiles:
    """The list, newest first."""

    def __init__(self, path=None, limit: int = MAX_RECENT):
        self._path = Path(path) if path else default_path()
        self._limit = int(limit)

    def files(self) -> list:
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        rows = data.get('files') if isinstance(data, dict) else data
        return [str(item) for item in (rows or []) if isinstance(item, str)
                ][:self._limit]

    def _save(self, rows: list) -> None:
        try:
            from foammesh.core.facade.plan48.gm11b_routes import save_text
            save_text(self._path, json.dumps({'files': rows}, indent=1) + '\n')
        except OSError:
            pass            # a list that cannot be kept is not worth a dialog

    def remember(self, path) -> list:
        """Put *path* first (once); the new list."""
        name = str(Path(path))
        rows = [name] + [item for item in self.files()
                         if os.path.normcase(item) != os.path.normcase(name)]
        rows = rows[:self._limit]
        self._save(rows)
        return rows

    def forget(self, path=None) -> list:
        """Drop *path*, or the whole list when none is given."""
        if path is None:
            rows = []
        else:
            name = os.path.normcase(str(Path(path)))
            rows = [item for item in self.files()
                    if os.path.normcase(item) != name]
        self._save(rows)
        return rows

    def next_to_last(self) -> str:
        """Gmsh's Shift+Ctrl+R: the file opened before the last one."""
        rows = self.files()
        return rows[1] if len(rows) > 1 else ''


def track(controller, store: RecentModelFiles) -> bool:
    """Make *controller* remember every file it opens or merges.

    Wrapped once per controller; False when it was wrapped already or has no
    ``open_model``.
    """
    if getattr(controller, '_recent_model_files', None) is not None:
        return False
    original = getattr(controller, 'open_model', None)
    if not callable(original):
        return False

    def open_model(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        store.remember(path)
        return result

    controller.open_model = open_model
    controller._recent_model_files = store
    return True
