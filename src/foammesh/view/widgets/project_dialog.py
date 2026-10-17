#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import datetime
from pathlib import Path
from functools import partial

import qasync
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QListWidgetItem

from app_properties import meshAppProperties
from foammesh.support.utils import getFit
from widgets.async_message_box import AsyncMessageBox

from foammesh.app import app
from foammesh.view.theming import fonts
from .project_dialog_ui import Ui_ProjectSelector
from .project_widget import ProjectWidget

RECENT_PROJECTS_NUMBER = 100


class ProjectDialog(QDialog):
    actionNewSelected = Signal()
    actionOpenSelected = Signal()
    actionProjectSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ui = Ui_ProjectSelector()
        self._ui.setupUi(self)

        self.setWindowIcon(meshAppProperties.icon())
        # DP-171. The launcher is the first title bar a user reads, and
        # "FoamMesh Start" is not a phrase in any language. Say what the
        # window does, with the product name where a name belongs.
        self.setWindowTitle(
            self.tr('Start {0}').format(meshAppProperties.fullName))

        # Plan 44 MF25. The section title is a small-caps caption (the
        # `.snap .st` language); QSS has no letter-spacing, so it is a font.
        self._ui.recentCaps.setFont(fonts.caps_font(10.5))  # a caps label, not a page heading (DP-219)
        self._refreshEmptyState()

        self._dialog = None
        self._projectDirectory = None

        self._connectSignalsSlots()

        geometry = app.settings.getLastStartWindowGeometry()
        display = app.qApplication.primaryScreen().availableVirtualGeometry()
        fit = getFit(geometry, display)
        self.setGeometry(fit)

    def setRecents(self, paths):
        self._ui.recentCases.clear()

        for i in range(min(RECENT_PROJECTS_NUMBER, len(paths))):
            item = QListWidgetItem()
            widget = ProjectWidget(Path(paths[i]))
            self._styleCard(widget, Path(paths[i]))
            item.setSizeHint(widget.sizeHint())
            self._ui.recentCases.addItem(item)
            self._ui.recentCases.setItemWidget(item, widget)
            widget.removeClicked.connect(partial(self._remove, widget))

        self._refreshEmptyState()

    @staticmethod
    def _styleCard(widget, path):
        """Plan 44 MF25. A recent case is a card: name, path and the date the
        folder was last changed, in a 1 px `border.group` outline (QSS)."""
        widget.setProperty('foammeshStartCard', True)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime)
            widget._ui.status.setText(stamp.strftime('%Y-%m-%d'))
        except OSError:
            widget._ui.status.setText('')

    def _refreshEmptyState(self):
        """First run: no recents means the list is replaced by a sentence
        that says what the two buttons do."""
        empty = self._ui.recentCases.count() == 0
        self._ui.emptyState.setVisible(empty)
        self._ui.recentCases.setVisible(not empty)

    def getProjectDirectory(self):
        return self._projectDirectory

    def _connectSignalsSlots(self):
        self._ui.newCase.clicked.connect(self._new)
        self._ui.open.clicked.connect(self._open)
        self._ui.recentCases.itemClicked.connect(self._openRecentProject)
        self.finished.connect(self._finished)

    @qasync.asyncSlot()
    async def _remove(self, widget):
        path = widget.getProjectPath()
        total = self._ui.recentCases.count()
        selectedPos = -1

        for i in range(total):
            item = self._ui.recentCases.item(i)
            if widget == self._ui.recentCases.itemWidget(item):
                selectedPos = i

        if await AsyncMessageBox().confirm(self, self.tr('Remove from list'),
                                           self.tr('Do you want to remove project "{0}" from the list?').format(path)):
            self._ui.recentCases.takeItem(selectedPos)
            app.settings.removeProject(selectedPos)
            self._refreshEmptyState()

    def _new(self):
        self.actionNewSelected.emit()

    def _open(self):
        self.actionOpenSelected.emit()

    def _openRecentProject(self, item):
        self.actionProjectSelected.emit(self._ui.recentCases.itemWidget(item).getProjectPath())

    def _finished(self, result):
        app.settings.updateLastStartWindowGeometry(self.geometry())
