#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Signal

from foammesh.app import app
from foammesh.db.configurations_schema import RegionType
from .region_card_ui import Ui_RegionCard


class RegionCard(QWidget):
    editClicked = Signal(str)
    removeClicked = Signal(str)

    def __init__(self, id_):
        super().__init__()
        self._ui = Ui_RegionCard()
        self._ui.setupUi(self)

        self._id = id_
        self._type = None
        self._point = None

        self._types = {
            RegionType.FLUID.value: self.tr('(Fluid)'),
            RegionType.SOLID.value: self.tr('(Solid)')
        }

        self._connectSignalsSlots()
        self.load()

    def name(self):
        return self._ui.name.text()

    def type(self):
        return self._type

    def point(self):
        return self._point

    def load(self,):
        path = f'region/{self._id}/'
        db = app.facadeClient.checkout()

        self._type = db.getValue(path + 'type')
        x, y, z = db.getVector(path + 'point')
        self._point = float(x), float(y), float(z)

        name = db.getValue(path + 'name')
        self._ui.name.setText(name)
        # DP-210. Both buttons in the card header paint an icon and no
        # words, so the tooltip is the only place they say what they do,
        # and it is their accessible name as well. That is the idiom the
        # viewport overlay already uses for its icon-only controls.
        # Naming them here rather than in the form means a renamed region
        # renames them too, because load() runs again.
        label = name or self.tr('this region')
        self._ui.edit.setToolTip(self.tr('Edit {0}').format(label))
        self._ui.edit.setAccessibleName(self._ui.edit.toolTip())
        self._ui.remove.setToolTip(self.tr('Remove {0}').format(label))
        self._ui.remove.setAccessibleName(self._ui.remove.toolTip())
        self._ui.type.setText(self._types[self._type])
        self._ui.point.setText(f'({x}, {y}, {z})')

    def addForm(self, form):
        self._ui.header.setEnabled(False)
        self._ui.card.layout().addWidget(form)

    def removeForm(self, form):
        self._ui.header.setEnabled(True)
        self._ui.card.layout().removeWidget(form)

    def showWarning(self, message: str):
        # R164. The label said one thing -- "outside bounding box" -- and the
        # page now has a second reason to refuse a seed: a point inside the
        # box but outside the geometry, which is what happens on an annulus
        # and which used to surface several tasks later as a meshing error.
        # A warning has to name the reason it is complaining about.
        # DP-183. The message is required now. The label used to ship a
        # Designer placeholder carrying this same sentence indented by
        # fifteen spaces, so calling this with nothing painted a duplicate of
        # one live string, misaligned against every other line on the card.
        self._ui.warning.setText(message)
        self._ui.warning.show()

    def hideWarning(self):
        self._ui.warning.hide()

    def _connectSignalsSlots(self):
        self._ui.edit.clicked.connect(self._editClicked)
        self._ui.remove.clicked.connect(self._removeClicked)

    def _editClicked(self):
        self.editClicked.emit(self._id)

    def _removeClicked(self):
        self.removeClicked.emit(self._id)
