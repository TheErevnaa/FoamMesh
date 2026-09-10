#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QDialogButtonBox

from .new_project_widget import NewProjectWidget


class NewProjectDialog(QDialog):
    pathSelected = Signal(Path)

    def __init__(self, parent, title, path=None, suffix=None, note=None,
                 name=''):
        super().__init__(parent)

        self.resize(500, self.size().height())

        self._widget = NewProjectWidget(self, path, suffix, name)

        layout = QVBoxLayout(self)
        if note:
            # D7. Saving a case took four dialogs in a row, the first of them
            # a message box whose whole content was the reason for the other
            # three. A sheet can carry its own reason.
            explanation = QLabel(note, self)
            explanation.setWordWrap(True)
            layout.addWidget(explanation)
        layout.addWidget(self._widget)

        self._buttonBox = QDialogButtonBox(self)
        self._buttonBox.setStandardButtons(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(self._buttonBox)

        self.setWindowTitle(title)
        self._buttonBox.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        self._connectSignalsSlots()

    def projectPath(self):
        return Path(self._widget.projectPath())

    def _connectSignalsSlots(self):
        self._widget.pathChanged.connect(self._pathChanged)
        self._buttonBox.button(QDialogButtonBox.StandardButton.Ok).clicked.connect(self._accept)
        self._buttonBox.button(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)

    def _pathChanged(self, path):
        self._buttonBox.button(QDialogButtonBox.StandardButton.Ok).setEnabled(path is not None)

    def _accept(self):
        self.pathSelected.emit(self.projectPath())
        self.accept()
