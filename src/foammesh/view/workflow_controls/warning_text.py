"""One rendering for a generation warning, shared by every surface that shows one.

Plan 26 WP2.2. The warning entries carry ``requested`` and ``applied`` beside
the message precisely so a user can see the substitution rather than read a
description of it -- "the first group's value was written" is not actionable,
"0.3 → 0.25" is. Both display sites rendered only ``message``, so a shared
formatter is the difference between the extra fields being carried and the
extra fields being used.

Kept out of the view classes themselves because the CLI and the report (WP8)
render the same entries, and three formatters would drift.
"""
from __future__ import annotations


def format_warning(item) -> str:
    """One warning as a single line, with its substitution if it has one."""
    if not isinstance(item, dict):
        return f'- {item}'
    message = str(item.get('message') or '').strip() or str(item)
    line = f'- {message}'
    field_id = str(item.get('field_id') or '').strip()
    if field_id:
        line += f' [{field_id}]'
    requested, applied = item.get('requested'), item.get('applied')
    if requested is not None or applied is not None:
        line += f'\n    requested {_value(requested)} → applied {_value(applied)}'
    return line


def format_warnings(items) -> list[str]:
    return [format_warning(item) for item in items or ()]


def _value(value) -> str:
    if value is None:
        return '(none)'
    if isinstance(value, (list, tuple)):
        return ', '.join(str(item) for item in value)
    return str(value)
