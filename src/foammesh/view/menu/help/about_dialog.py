#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtWidgets import QApplication, QDialog

from app_properties import meshAppProperties
from foammesh.app import app
from foammesh.core.branding import RuntimeDiagnostics

from .about_dialog_ui import Ui_AboutDialog
from .license_dialog import LicenseDialog


class AboutDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_AboutDialog()
        self._ui.setupUi(self)

        self._ui.logo.setPixmap(meshAppProperties.logo())
        self._ui.description.setText(self.tr(
            '<h2>{name} {version}</h2>'
            '<p>Standalone OpenFOAM meshing workbench</p>'
            '<p>Product identity: <b>Erevnaa / FOAM</b>.</p>'
            '<p>Free and open-source software licensed under GPL-3.0-or-later.</p>'
            '<p>Derived from NEXTfoam BaramMesh 26.2.1. Upstream copyright and '
            'licence notices are retained.</p>'
            '<p>Not approved or endorsed by OpenCFD Limited, the OpenFOAM Foundation, '
            'CFD Direct, or NEXTfoam Co., Ltd.</p>').format(
                name=meshAppProperties.fullName,
                version=meshAppProperties.version))
        self._diagnostics = RuntimeDiagnostics.collect(
            meshAppProperties.fullName, meshAppProperties.version, app.capabilities)
        self._ui.diagnostics.setPlainText(self._diagnostics.as_text())

        self._dialog = None

        self._connectSignalsSlots()

    def _connectSignalsSlots(self):
        self._ui.close.clicked.connect(self.close)
        self._ui.copyDiagnostics.clicked.connect(self._copyDiagnostics)
        self._ui.thirdPartySoftwares.clicked.connect(self._showLicenses)

    def _copyDiagnostics(self):
        QApplication.clipboard().setText(self._diagnostics.as_text())
        self._ui.copyDiagnostics.setText(self.tr('Copied'))

    def _showLicenses(self):
        self._dialog = LicenseDialog(self)
        self._dialog.show()
