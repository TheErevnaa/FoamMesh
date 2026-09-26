#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtCore import QObject, Signal


#: R75. This was 50, and fifty entries is not a shortcut. MEASURED: about
#: fifty rows in two columns, of which `elbow` appeared seven times, `duct`
#: eight, `pipe` three and `sphere` twice -- all identical text, all pointing
#: at different folders. Ten is what a recent list is for; anything older is
#: found with File > Open.
MAX_COUNT = 10


class RecentFilesMenu(QObject):
    projectSelected = Signal(str)
    clearRequested = Signal()

    def __init__(self, root):
        super().__init__()
        self._root = root
        # R75. Every entry already carried the full path in a tooltip and none
        # of them ever showed one: QMenu suppresses action tooltips unless it
        # is told to display them.
        show_tips = getattr(root, 'setToolTipsVisible', None)
        if show_tips is not None:
            show_tips(True)
        self._actions = None
        self._recentest = None

    def setRecents(self, paths):
        self._root.clear()
        self._actions = {}

        if paths:
            for item in paths[:MAX_COUNT]:
                record = item if isinstance(item, dict) else {'path': item}
                path = Path(record['path'])
                action = self._newAction(path, record.get('display_name'))
                self._root.addAction(action)

            first = paths[0] if not isinstance(paths[0], dict) else paths[0]['path']
            self._recentest = self._actions[Path(first)]
            self._addClearAction()
        else:
            empty = self._root.addAction(self.tr('No recent cases'))
            empty.setEnabled(False)

    def addRecentest(self, path):
        path = Path(path)
        if not self._actions:
            self._root.clear()
            self._actions = {}
            action = self._newAction(path)
            self._root.addAction(action)
            self._recentest = action
            self._addClearAction()
            return
        existing = self._actions.get(path)
        if existing:
            self._root.removeAction(existing)
        action = self._newAction(path)
        if self._recentest is None:
            self._root.addAction(action)
        else:
            self._root.insertAction(self._recentest, action)
        self._recentest = action

    def _addClearAction(self):
        self._root.addSeparator()
        clear = self._root.addAction(self.tr('Clear recent'))
        clear.triggered.connect(self.clearRequested)

    def updateRecentest(self, path):
        action = self._actions[path] if path in self._actions else None
        if action == self._recentest:
            return

        if action:
            self._root.removeAction(action)
            self._root.insertAction(self._recentest, action)
            self._recentest = action
        else:
            self.addRecentest(path)

    def _recentTriggered(self):
        self.projectSelected.emit(str(self.sender().data()))

    @staticmethod
    def entryLabel(path, display_name=None) -> str:
        """Name the case *and* the folder it is in (R75).

        The menu used to show `path.name` alone, so eight cases called `duct`
        in eight different folders were eight identical lines and reopening
        yesterday's work meant picking one and seeing what loaded. The
        containing directory is the part that differs, so it is on the line
        rather than only in a tooltip. Ampersands are doubled because Qt reads
        a single one in a menu entry as a mnemonic marker.
        """
        name = display_name or path.name
        parent = path.parent
        folder = parent.name or str(parent)
        label = f'{name}  —  {folder}' if folder else name
        return label.replace('&', '&&')

    def _newAction(self, path, display_name=None):
        action = QAction(self.entryLabel(path, display_name))
        action.setData(path)
        action.setToolTip(str(path))
        action.setStatusTip(str(path))
        action.triggered.connect(self._recentTriggered)
        self._actions[path] = action

        return action
