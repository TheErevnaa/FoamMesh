"""Plan 44 MF20. A secondary action drawn as a one-line text link.

A button that is not the step's main action does not get a row of its own the
width of the sheet. It is a flat, 20 px high text link the width of its words;
a few of them share one row. The widget stays the same ``QPushButton`` with the
same object name and signal, so nothing that clicks it or finds it changes.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QPushButton, QSizePolicy,
                               QWidget)

from foammesh.view.theming.metrics import GAP_SECTION

#: The height of one link row (Plan 44 MF20).
LINK_ROW_HEIGHT = 20
#: The role the style sheet can key on.
LINK_ROLE = 'link'


def as_link(button: QPushButton) -> QPushButton:
    """Make *button* a left-aligned text link; return it."""
    button.setFlat(True)
    button.setProperty('foammeshRole', LINK_ROLE)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedHeight(LINK_ROW_HEIGHT)
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return button


def link_row(parent: QWidget, name: str, *buttons: QPushButton) -> QWidget:
    """One row holding *buttons* as links, packed to the left."""
    row = QWidget(parent)
    row.setObjectName(name)
    box = QHBoxLayout(row)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(GAP_SECTION)
    for button in buttons:
        button.setParent(row)
        box.addWidget(as_link(button), 0)
    box.addStretch(1)
    return row
