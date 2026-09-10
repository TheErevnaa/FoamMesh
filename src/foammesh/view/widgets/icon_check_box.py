#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import Signal, QSize
from PySide6.QtWidgets import QPushButton, QSizePolicy
from PySide6.QtGui import QIcon
from foammesh.app import app
from widgets.themed_icon import load_themed_icon


class IconCheckBox(QPushButton):
    checkStateChanged = Signal(bool)

    def __init__(self, onIconFileName, offIconFileName):
        super().__init__()
        self._onIconFileName = onIconFileName
        self._offIconFileName = offIconFileName

        self.setCheckable(True)
        self._applyIcons()
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setProperty('foammeshFlat', True)
        self.toggled.connect(self._toggled)
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(lambda _name: self._applyIcons())

    def _applyIcons(self):
        icon = QIcon()
        size = self.iconSize() if self.iconSize().isValid() else QSize(24, 24)
        on_icon = load_themed_icon(self._onIconFileName, size)
        off_icon = load_themed_icon(self._offIconFileName, size)
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled, QIcon.Mode.Active, QIcon.Mode.Selected):
            icon.addPixmap(on_icon.pixmap(size, mode, QIcon.State.On), mode, QIcon.State.On)
            icon.addPixmap(off_icon.pixmap(size, mode, QIcon.State.Off), mode, QIcon.State.Off)
        self.setIcon(icon)

    def _toggled(self, checked):
        self.checkStateChanged.emit(checked)
