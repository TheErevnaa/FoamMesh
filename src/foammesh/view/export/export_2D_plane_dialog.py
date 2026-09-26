#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtWidgets import QDialog, QWidget, QVBoxLayout

from widgets.async_message_box import AsyncMessageBox
from widgets.new_project_widget import NewProjectWidget
from widgets.selector_dialog import SelectorDialog, SelectorItem

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType
from foammesh.core.mesh.extrusion_options import ExtrudeOptions, ExtrudeModel
from foammesh.view.theming.metrics import FORM_MARGIN
from .export_2D_plane_dialog_ui import Ui_Export2DPlaneDialog
from .export_2D_region_widgets import Export2DPlaneRegionWidget


class Export2DPlaneDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_Export2DPlaneDialog()
        self._ui.setupUi(self)

        self._pathWidget = NewProjectWidget(self._ui.path, suffix=None)

        self.setWindowTitle(self.tr('Export 2D plane mesh'))

        # DP-202. The form used to declare a `Run solver after export`
        # checkbox that this line hid on every open, and an
        # `isRunAfterExportChecked()` that no caller in the product ever
        # asked. FoamMesh meshes; it does not run a solver. A control the
        # reader can never see is not a setting, and an accessor whose only
        # readers are tests asserting the value permanent invisibility
        # guarantees is not an answer. Both are gone from the form.

        self._regionWidgets = []

        self._boundaries = []
        self._dialog = None

        layout = QVBoxLayout(self._ui.path)
        layout.addWidget(self._pathWidget)
        # DP-201. This used to call `hideValidationMessage()`. R42 took
        # the same call out of the Export mesh dialog because the label
        # that reads "<location> is not a folder." was being written and
        # never drawn: the reason the destination was refused sat one
        # widget away from the reader and was delivered instead by a
        # modal after a press, which is a round trip for a sentence that
        # was already on the form.

        regionsWidget = QWidget()
        self._ui.parameters.layout().insertWidget(0, regionsWidget)

        layout = QVBoxLayout(regionsWidget)
        layout.setContentsMargins(0, FORM_MARGIN, 0, 0)

        db = app.facadeClient.checkout()
        for region in db.getElements('region').values():
            widget = Export2DPlaneRegionWidget(region.value('name'))
            widget.boundarySelectClicked.connect(self._openBoundarySelectorDialog)
            layout.addWidget(widget)
            self._regionWidgets.append(widget)

        for gId, geometry in db.getElements(
                'geometry', lambda i, e: e['cfdType'] == CFDType.BOUNDARY.value).items():
            name = geometry.value('name')
            self._boundaries.append(SelectorItem(name, name, gId))

        self._connectSignalsSlots()

    def projectPath(self):
        return self._pathWidget.projectPath()

    def extrudeOptions(self):
        return ([(b.rname(), b.boundary(), b.boundary()) for b in self._regionWidgets],
                ExtrudeOptions(ExtrudeModel.PLANE, thickness=self._ui.thickness.text()))

    def _connectSignalsSlots(self):
        self._ui.ok.clicked.connect(self._accept)

    def _openBoundarySelectorDialog(self, widget):
        self._dialog = self._createBoundarySelector()
        self._dialog.accepted.connect(lambda: widget.setText(self._dialog.selectedText()))
        self._dialog.open()

    def _createBoundarySelector(self):
        return SelectorDialog(self, self.tr('Select boundary'), self.tr('Select boundary'), self._boundaries)

    @qasync.asyncSlot()
    async def _accept(self):
        path = self._pathWidget.projectPath()
        if path is None:
            if self._pathWidget.validationMessage():
                await AsyncMessageBox().warning(self, self.tr('Input error'), self._pathWidget.validationMessage())
                return
            else:
                await AsyncMessageBox().warning(self, self.tr('Input error'), self.tr('Enter a project name.'))
                return

        for widget in self._regionWidgets:
            if not widget.boundary():
                await AsyncMessageBox().warning(
                    self, self.tr('Input error'), self.tr('Select boundary — {0}').format(widget.rname()))
                return

        try:
            self._ui.thickness.validate(self.tr('Thickness'))
        except ValueError as e:
            await AsyncMessageBox().warning(self, self.tr('Input error'), str(e))
            return

        super().accept()
