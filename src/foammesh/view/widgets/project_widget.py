#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Signal

from .project_widget_ui import Ui_ProjectWidget


class ProjectWidget(QWidget):
    removeClicked = Signal()

    def __init__(self, path):
        super().__init__()
        self._ui = Ui_ProjectWidget()
        self._ui.setupUi(self)

        self._ui.name.setText(path.name)

        self._ui.path.setText(str(path))

        if not path.is_dir():
            self._ui.path.setDisabled(True)
            self._ui.name.setDisabled(True)

        # DP-210. The button paints a cross and no words. It drops the
        # project from the recent list rather than from disk, which is
        # the distinction the confirmation goes on to draw, so the
        # tooltip draws it here too.
        self._ui.remove.setToolTip(
            self.tr('Remove {0} from the list').format(path.name))
        self._ui.remove.setAccessibleName(self._ui.remove.toolTip())

        self._ui.remove.clicked.connect(self._remove)

    def getProjectPath(self):
        return self._ui.path.text()

    def _remove(self):
        self.removeClicked.emit()

