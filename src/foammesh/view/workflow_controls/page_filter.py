"""Plan 40 UXD3 (F7a): the filter box at the top of a settings page.

Typing narrows the page to the settings whose label or field name contains
every word typed. The box only chooses which rows are on screen: a row it
hides keeps its editor, its value and its place in the page's patch.
"""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QLineEdit


def make_filter_box(parent=None) -> QLineEdit:
    """The one-line filter, empty, with a clear button."""
    box = QLineEdit(parent)
    box.setObjectName('pageFilter')
    box.setClearButtonEnabled(True)
    box.setPlaceholderText(
        QCoreApplication.translate('pageFilter', 'Filter settings'))
    box.setAccessibleName(
        QCoreApplication.translate('pageFilter', 'Filter settings on this page'))
    box.setToolTip(QCoreApplication.translate(
        'pageFilter', 'Type to show only the settings whose name contains '
        'the words you type. Hidden settings keep their values.'))
    return box


__all__ = ['make_filter_box']
