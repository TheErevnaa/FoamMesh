#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pick the shape of a geometry the user is about to add.

Plan 31. Four shapes were offered -- hex, cylinder, sphere, hex6 -- and all
four are closed volumes. OpenFOAM 13 also offers three *open* searchable
surfaces, ``plane``, ``disk`` and ``plate``, and none of them could be reached
from anywhere in this program, so the only way to refine along a shear layer,
around a fan disc or across a splitter plate was to leave the app and author
an STL. The three rows are built here rather than in the Designer file because
the ``.ui`` is shared and its generated module is not in the repository; a
row added in code is a row that cannot go missing between checkouts.
"""

from PySide6.QtCore import Signal

from PySide6.QtWidgets import QDialog, QRadioButton

from widgets.enum_button_group import EnumButtonGroup

from foammesh.db.configurations_schema import Shape
from .geometry_add_dialog_ui import Ui_GeometryAddDialog


class GeometryAddDialog(QDialog):
    shapeSelected = Signal(Shape)

    #: The open surfaces, and what each one is for. Object name, label,
    #: shape -- the object name is what a test addresses the row by.
    OPEN_SURFACES = (
        ('plane', 'Plane (infinite)', Shape.PLANE),
        ('disk', 'Disk', Shape.DISK),
        ('plate', 'Plate (axis-aligned)', Shape.PLATE),
    )

    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_GeometryAddDialog()
        self._ui.setupUi(self)

        self._radios = EnumButtonGroup()
        self._radios.addEnumButton(self._ui.hex,        Shape.HEX)
        self._radios.addEnumButton(self._ui.cylinder,   Shape.CYLINDER)
        self._radios.addEnumButton(self._ui.sphere,     Shape.SPHERE)
        self._radios.addEnumButton(self._ui.hex6,       Shape.HEX6)

        for objectName, label, shape in self.OPEN_SURFACES:
            button = QRadioButton(self.tr(label), self._ui.groupBox)
            button.setObjectName(objectName)
            setattr(self._ui, objectName, button)
            self._ui.verticalLayout.addWidget(button)
            self._ui.shapeRadios.addButton(button)
            self._radios.addEnumButton(button, shape)

        self._connectSignalsSlots()

    def _connectSignalsSlots(self):
        self._ui.next.clicked.connect(self._onAccept)

    def _onAccept(self):
        self.shapeSelected.emit(self._radios.checkedData())

        self.accept()
