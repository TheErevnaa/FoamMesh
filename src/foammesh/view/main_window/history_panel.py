"""The History panel: every step Undo and Redo walk, and a jump to any of them.

Plan 40 UXD7 (F7d). Settings edits, geometry operations, finished steps and
mesh stage runs share one history, so this lists them in the order they happened ("Castellation run · 4,374
cells"), shows where the case stands now, and lets the user go to any step in
one move: the panel works out how many Undos or Redos lie between.
"""
from __future__ import annotations

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QFont
from qtpy.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
    QWidget)

#: What each row means, kept on the item so a jump can be worked out.
_ROLE = Qt.ItemDataRole.UserRole


class HistoryPanel(QWidget):
    """Rows, oldest first: the opened case, the steps taken, *Now*, the steps
    that were undone (still reachable with Redo)."""

    #: ``('undo' | 'redo', steps)``
    jumpRequested = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('historyPanel')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._hint = QLabel(self.tr(
            'Settings changes, geometry operations, finished steps and mesh '
            'stage runs, in the order they happened. Choose a step to return '
            'the case, and what it made, to it.'),
            self)
        self._hint.setObjectName('historyHint')
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)
        self._list = QListWidget(self)
        self._list.setObjectName('historyList')
        self._list.setAccessibleName(self.tr('History of steps'))
        self._list.itemDoubleClicked.connect(lambda _item: self._jump())
        self._list.currentItemChanged.connect(lambda *_: self._updateButton())
        outer.addWidget(self._list, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self._goTo = QPushButton(self.tr('Go to this step'), self)
        self._goTo.setObjectName('historyGoTo')
        self._goTo.clicked.connect(self._jump)
        row.addWidget(self._goTo)
        outer.addLayout(row)
        self.setEntries({'done': [], 'undone': []})

    # -- content ------------------------------------------------------------ #

    def setEntries(self, listing: dict) -> None:
        """Show ``{'done': [...], 'undone': [...]}`` from ``history.query``."""
        done = list(listing.get('done') or ())
        undone = list(listing.get('undone') or ())
        self._list.clear()
        self._add(self.tr('Before the first step'), ('undo', len(done)), '')
        for index, entry in enumerate(done):
            self._add(entry.get('label') or self.tr('Change'),
                      ('undo', len(done) - 1 - index), entry.get('at', ''),
                      kind=entry.get('kind') or ('stage' if entry.get('stage') else ''))
        now = self._add(self.tr('Now'), None, '', current=True)
        for index, entry in enumerate(undone):
            self._add(entry.get('label') or self.tr('Change'),
                      ('redo', index + 1), entry.get('at', ''),
                      kind=entry.get('kind') or ('stage' if entry.get('stage') else ''), undone=True)
        self._list.setCurrentItem(now)
        self._updateButton()

    def _add(self, text, move, at, *, kind='', undone=False, current=False):
        stamp = at.replace('T', ' ')[11:19] if at else ''
        item = QListWidgetItem(f'{stamp}  {text}' if stamp else text)
        item.setData(_ROLE, move)
        font = QFont(item.font())
        if current:
            font.setBold(True)
        if undone:
            font.setItalic(True)
        item.setFont(font)
        if kind == 'stage':
            item.setToolTip(self.tr('A mesh stage run: going back restores the '
                                    'mesh it replaced and that mesh’s task state'))
        elif kind == 'geometry':
            item.setToolTip(self.tr('A geometry operation: going back restores '
                                    'the geometry as it was before'))
        elif kind == 'task':
            item.setToolTip(self.tr('A finished step: going back reopens it and '
                                    'marks what came after as out of date'))
        if undone:
            item.setToolTip(self.tr('Undone. Going to it applies it again.'))
        self._list.addItem(item)
        return item

    # -- moves --------------------------------------------------------------- #

    def rowMove(self, row: int):
        """``('undo' | 'redo', steps)`` to reach *row*, or ``None`` for Now."""
        item = self._list.item(row)
        return item.data(_ROLE) if item is not None else None

    def rowCount(self) -> int:
        return self._list.count()

    def rowText(self, row: int) -> str:
        return self._list.item(row).text()

    def selectRow(self, row: int) -> None:
        self._list.setCurrentRow(row)

    def _updateButton(self) -> None:
        item = self._list.currentItem()
        move = item.data(_ROLE) if item is not None else None
        self._goTo.setEnabled(bool(move) and move[1] > 0)

    def _jump(self) -> None:
        item = self._list.currentItem()
        move = item.data(_ROLE) if item is not None else None
        if move and move[1] > 0:
            self.jumpRequested.emit(move[0], int(move[1]))
