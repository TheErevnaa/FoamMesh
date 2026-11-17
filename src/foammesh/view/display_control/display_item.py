#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import IntEnum, auto

from PySide6.QtCore import QCoreApplication, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QTreeWidgetItem

from foammesh.rendering.actor_info import ActorInfo, ActorType

#: Plan 50 (DP-6341). One icon per colour, shared by every row that shows it.
_SWATCH_ICONS: dict = {}
_SWATCH_SIZE = 14


def swatch_icon(color) -> QIcon:
    """A small rounded square in ``color``, or an empty icon for None."""
    if color is None:
        return QIcon()
    key = QColor(color).name(QColor.NameFormat.HexArgb)
    icon = _SWATCH_ICONS.get(key)
    if icon is None:
        pixmap = QPixmap(_SWATCH_SIZE, _SWATCH_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(0, 0, 0, 60))
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(0.5, 0.5, _SWATCH_SIZE - 1, _SWATCH_SIZE - 1),
                                3, 3)
        painter.end()
        icon = _SWATCH_ICONS[key] = QIcon(pixmap)
    return icon


class Column(IntEnum):
    NAME_COLUMN = 0
    TYPE_COLUMN = auto()
    COLOR_COLUMN = auto()
    # CUT_ICON_COLUMN = auto()
    # VISIBLE_ICON_COLUMN = auto()


class DisplayItem(QTreeWidgetItem):
    # _emptyIcon = QIcon()
    # _notCutIcon = QIcon(':graphicsIcons/no-cutter.svg')
    # _bulbOnIcon = QIcon(':graphicsIcons/bulb-on.svg')
    # _bulbOffIcon = QIcon(':graphicsIcons/bulb-off.svg')

    _types = {
        ActorType.GEOMETRY: QCoreApplication.translate('DisplayControl', 'Geometry'),
        ActorType.BOUNDARY: QCoreApplication.translate('DisplayControl', 'Boundary'),
        ActorType.MESH: QCoreApplication.translate('DisplayControl', 'Mesh')
    }

    def __init__(self, actorInfo: ActorInfo):
        super().__init__()
        # Plan 50 (DP-6341). The swatch is the row's icon in the colour
        # column. It was a styled QLabel in a QWidget per row, set with
        # ``setItemWidget``: MEASURED live, 500 surfaces spent 0.9 s of the
        # load building, styling and re-polishing those widgets.
        self._swatchColor = None
        self._swatchKey = False

        self._actorInfo = actorInfo
        self.setText(Column.NAME_COLUMN, actorInfo.name())
        self.setText(Column.TYPE_COLUMN, self._types[actorInfo.type()])
        self._updateColorColumn()
        # self._updateCutIcon()
        # self._updateVisibleIcon()

        self._connectSignalsSlots()

    def setActorVisible(self, visible):
        # Hidden parts show an empty swatch, so the tree says what is drawn.
        self._actorInfo.setVisible(visible)
        self._updateColorColumn()

    def isActorVisible(self):
        return self._actorInfo.isVisible()

    def setActorColor(self, color):
        self._actorInfo.setColor(color)
        self._updateColorColumn()

    def setCutEnabled(self, enabled):
        self._actorInfo.setCutEnabled(enabled)
        # self._updateCutIcon()

    def actorInfo(self):
        return self._actorInfo

    def swatchColor(self):
        """The colour the row's swatch shows, or None when it is empty."""
        return self._swatchColor

    def _updateColorColumn(self):
        color = self._actorInfo.color() if self._actorInfo.isVisible() else None
        color = QColor(color) if color is not None else None
        key = None if color is None else color.name(QColor.NameFormat.HexArgb)
        if key == self._swatchKey:
            return
        self._swatchKey = key
        self._swatchColor = color
        self.setIcon(Column.COLOR_COLUMN, swatch_icon(color))

    def _updateName(self, name):
        self.setText(Column.NAME_COLUMN, name)
    #
    # def _updateCutIcon(self):
    #     if self._actorInfo.isCutEnabled():
    #         self._colorWidget.clear()
    #     else:
    #         icon = QPixmap(':/icons/lock-closed.svg')
    #         self._colorWidget.setPixmap(icon.scaled(18, 18))
    #
    # def _updateVisibleIcon(self):
    #     if self._actorInfo.isVisible():
    #         self.setIcon(Column.VISIBLE_ICON_COLUMN, self._bulbOnIcon)
    #     else:
    #         self.setIcon(Column.VISIBLE_ICON_COLUMN, self._bulbOffIcon)

    def _connectSignalsSlots(self):
        self._actorInfo.nameChanged.connect(self._updateName)
        # The row is built before the palette is assigned, so a swatch painted
        # only at construction never shows the colour the actor ends up with.
        self._actorInfo.colorChanged.connect(self._updateColorColumn)
