#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPalette


class FlatPushButton(QPushButton):
    doubleClicked = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setProperty('foammeshFlat', True)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        if option.state & QStyle.StateFlag.State_On:
            rect = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
            color = self.palette().color(QPalette.ColorRole.Highlight)
            color.setAlpha(100)  # just a good-looking value
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 4, 4)
            painter.end()
            sp = QStylePainter(self)
            sp.drawControl(QStyle.ControlElement.CE_PushButtonLabel, option)
        elif option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken):
            super().paintEvent(event)
        else:  # transparent background
            painter = QStylePainter(self)
            painter.drawControl(QStyle.ControlElement.CE_PushButtonLabel, option)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        super().mouseDoubleClickEvent(event)
        self.doubleClicked.emit()
