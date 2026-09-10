#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit

from foammesh.core.documentation import document_text
from .license_dialog_ui import Ui_LicenseDialog


class LicenseDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_LicenseDialog()
        self._ui.setupUi(self)
        self._ui.licenseText.setPlainText(document_text('LICENSE'))
        self._ui.noticeText.setPlainText(document_text('NOTICE'))
        self._ui.thirdPartyText.setPlainText(document_text('THIRD_PARTY.md'))
        self._ui.close.clicked.connect(self.close)
        self._ui.copy.clicked.connect(self._copyCurrentTab)

    def _copyCurrentTab(self):
        editor = self._ui.tabs.currentWidget().findChild(QPlainTextEdit)
        if editor is not None:
            QApplication.clipboard().setText(editor.toPlainText())
            self._ui.copy.setText(self.tr('Copied'))
