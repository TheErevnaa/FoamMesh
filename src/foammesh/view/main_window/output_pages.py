"""Read-only Region C reports backed by existing product data."""
from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QLabel, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget,
)

from foammesh.view.workflow_controls.warning_text import format_warnings


def _text_page(text: str, parent=None) -> QPlainTextEdit:
    page = QPlainTextEdit(parent)
    page.setReadOnly(True)
    page.setPlainText(text)
    return page


class EffectiveSetupPage(QWidget):
    """Immutable effective dictionary/plan view; never an editor."""

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self.setObjectName('effectiveSetupOutput')
        self.setAccessibleName('Effective meshing setup and provenance')
        layout = QVBoxLayout(self)
        heading = QLabel('Effective meshing setup', self)
        layout.addWidget(heading)
        tabs = QTabWidget(self)
        tabs.setObjectName('effectiveSetupTabs')
        layout.addWidget(tabs, 1)

        links = payload.get('source_links') or {}
        warnings = payload.get('warnings') or []
        lines = [
            f"Target: {payload.get('target', '')}",
            f"Configuration: {payload.get('configuration_sha256', '')}",
            f"Last run: {payload.get('last_run_configuration_sha256') or 'none'}",
            f"Stale since last run: {payload.get('stale_since_last_run', False)}",
            'Stale stages: ' + ', '.join(
                name for name, stale in
                (payload.get('stale_stages') or {}).items() if stale),
            '', 'Source fields:',
            *(f"{section}: {', '.join(fields)}"
              for section, fields in sorted(links.items())),
        ]
        if warnings:
            lines.extend(['', 'Validation warnings:',
                          *format_warnings(warnings)])
        tabs.addTab(_text_page('\n'.join(lines), tabs), 'Provenance')
        for name, content in sorted((payload.get('current') or {}).items()):
            tabs.addTab(_text_page(str(content), tabs), name)
        for name, content in sorted((payload.get('diffs') or {}).items()):
            if content:
                tabs.addTab(_text_page(str(content), tabs), f'Diff: {name}')


class JsonOutputPage(QWidget):
    def __init__(self, title: str, payload: dict, parent=None):
        super().__init__(parent)
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(title, self))
        layout.addWidget(_text_page(
            json.dumps(payload, indent=2, sort_keys=True, default=str), self),
            1)
