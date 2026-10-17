"""'field: 4' beside every control the size field has proposed a value for.

Plan 41 GN25c (section 4.8.2). The proposals live in
``foammesh/sizing/proposals.json`` and are read through the facade
(``sizing.proposals.get``). A scalar control gets its badge on the page's own
editor; a control inside a collection row's editor gets a note under it that
follows the row being edited.
"""
from __future__ import annotations

import re

from PySide6.QtWidgets import QLabel

from foammesh.view.facade_client import query

_SAFE = re.compile(r'[^A-Za-z0-9_.-]+')
ROW_BADGE_NAME = 'fieldRowBadge'


def safe_key(name) -> str:
    return _SAFE.sub('_', str(name).strip()).strip('_') or 'surface'


def read_badges(client) -> dict:
    """``{target: badge text}`` of the proposals still owned by the field."""
    if client is None:
        return {}
    try:
        result = query(client, 'sizing.proposals.get', {'badges_only': True})
    except Exception:                                        # noqa: BLE001
        return {}
    payload = getattr(result, 'payload', None)
    if not isinstance(payload, dict):
        return {}
    found = payload.get('badges')
    return dict(found) if isinstance(found, dict) else {}


def apply_to_editors(editors: dict, badges: dict) -> int:
    """Set the badge on each scalar editor; returns how many show one."""
    shown = 0
    for field_id, editor in editors.items():
        setter = getattr(editor, 'setFieldBadge', None)
        if setter is None:
            continue
        text = badges.get(field_id, '')
        setter(text)
        shown += 1 if text else 0
    return shown


def apply_to_page(page) -> int:
    """Badge the scalar controls of a task page and its collection panels."""
    editors = getattr(page, '_editors', None)
    client = getattr(page, '_client', None)
    if client is None or (not editors and not getattr(
            page, 'sizingBadgePanels', None)):
        return 0
    badges = read_badges(client)
    shown = apply_to_editors(editors, badges)
    panels = list(getattr(page, '_panels', ()) or ())
    extra = getattr(page, 'sizingBadgePanels', None)
    if callable(extra):
        panels += list(extra() or ())
    for panel in panels:
        attach_row_badges(panel, badges)
    return shown


# -- collection rows ----------------------------------------------------------- #

def element_of(editor, collection_id: str) -> str:
    """``surfaceRefinement.minimumLevel`` for the editor of that column."""
    path = str(getattr(editor.descriptor, 'storage_path', '') or '')
    marker = '/{id}/'
    if marker not in path:
        return ''
    return path.split(marker, 1)[1].replace('/', '.')


class RowBadgeLabel(QLabel):
    """The badge for one column of the row being edited."""

    def __init__(self, panel, key: str, parent=None):
        super().__init__(parent)
        self._panel = panel
        self._key = key
        self.setObjectName(ROW_BADGE_NAME)
        self.setProperty('foammeshStatus', 'info')
        self.setWordWrap(True)
        self._badges: dict = {}
        self.refresh()

    def setBadges(self, badges: dict) -> None:
        self._badges = dict(badges)
        self.refresh()

    def target(self) -> str:
        editor = self._panel.editor(self._key)
        name_editor = self._panel.editor('group_name')
        if editor is None or name_editor is None:
            return ''
        element = element_of(editor, self._panel.collection_id)
        name = name_editor.value()
        if not element or not str(name or '').strip():
            return ''
        return f'{self._panel.collection_id}[{safe_key(name)}].{element}'

    def refresh(self) -> str:
        text = self._badges.get(self.target(), '')
        self.setText(text)
        self.setVisible(bool(text))
        return text

    def showEvent(self, event):                              # noqa: N802
        self.refresh()
        super().showEvent(event)


def attach_row_badges(panel, badges: dict) -> int:
    """A badge note under every column of *panel*'s row editor that the
    field has proposed for some row. Returns the notes attached."""
    if not all(hasattr(panel, name) for name in (
            'collection_id', 'editor', 'editorHost', 'addAnnotation')):
        return 0
    labels = getattr(panel, '_fieldRowBadges', None)
    if labels is None:
        labels = {}
        panel._fieldRowBadges = labels
    prefix = f'{panel.collection_id}['
    wanted = {target for target in badges if target.startswith(prefix)}
    for key in tuple(getattr(panel, '_editors', {})):
        if key in labels or key == 'group_name':
            continue
        editor = panel.editor(key)
        element = element_of(editor, panel.collection_id) if editor else ''
        if not element or not any(
                target.endswith('].' + element) for target in wanted):
            continue
        label = RowBadgeLabel(panel, key, panel.editorHost())
        panel.addAnnotation(key, label)
        labels[key] = label
    for label in labels.values():
        label.setBadges(badges)
    return len(labels)
