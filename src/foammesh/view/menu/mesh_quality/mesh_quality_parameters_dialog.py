#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""The mesh-quality record, edited from the Mesh menu.

Plan 30 WP-04 (F-29). This dialog and the snappy QA page
(``ui_location = workflow.quality``) edit the same ``meshQuality`` record, but
each carried its own hand-written list of which leaves that record has: this
one sixteen ``getValue``/``setValue`` pairs plus an eleven-name ``RELAXED``
tuple, the page a twenty-seven-entry tuple of semantic ids. Nothing checked
that the two lists agreed, so "the QA page shows the numbers the summary
shows" was a coincidence maintained by hand (R191).

Both editors now take their field list from :mod:`foammesh.core.quality.policy`
-- one tuple, one order -- so agreeing is what the code does rather than what
someone remembered to do.
"""

from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QMessageBox

from foammesh.support.simple_db.simple_schema import ValidationError

from foammesh.app import app
from foammesh.core.quality.policy import (
    RELAXABLE_LIMITS,
    RELAXED_WITH_DEFAULT,
    STORAGE_ROOT,
    storage_paths,
)
from .mesh_quality_parameters_dialog_ui import Ui_MeshQualityParametersDialog


class MeshQualityParametersDialog(QDialog):
    # Plan 29 WP7.4. The thresholds that had a strict box and no relaxed one.
    # snappyLayerDriver merges the relaxed sub-dictionary over the strict one,
    # so a key left out of relaxed keeps its strict value -- which is what an
    # empty box here means. The list is the policy's now, not a second copy.
    RELAXED = RELAXABLE_LIMITS

    def __init__(self, parent):
        super().__init__(parent)
        self._ui = Ui_MeshQualityParametersDialog()
        self._ui.setupUi(self)
        self._ui.formLayout.removeRow(self._ui.minFaceFlatness)
        self._ui.formLayout.removeRow(self._ui.minTriangleTwist)

        #: Storage path (relative to ``meshQuality``) -> the box editing it.
        self._editors: dict[str, QLineEdit] = {}
        #: The same keys -> the label the user sees, for validation messages.
        self._titles: dict[str, str] = {}
        self._buildEditors()
        #: The relaxed boxes under their leaf name, for the tests and the
        #: harness that walk them.
        self._relaxed = {path.split('/', 1)[1]: edit
                         for path, edit in self._editors.items()
                         if path.startswith('relaxed/')}

        self._dbElement = app.facadeClient.checkout(STORAGE_ROOT)
        for path, edit in self._editors.items():
            edit.setText(self._dbElement.getValue(path) or '')

    def _buildEditors(self):
        """Bind one box to every path the policy says this record has.

        A strict limit already has a box from Designer; a relaxed one has a
        box only for ``maxNonOrtho``, so the rest are inserted here -- beside
        the strict row they loosen, borrowing the label it already carries
        instead of inventing a second wording.
        """
        prefix = f'{STORAGE_ROOT}/'
        for full in storage_paths():
            path = full[len(prefix):]
            name = path.rsplit('/', 1)[-1]
            relaxed = path.startswith('relaxed/')
            attribute = f'{name}Relaxed' if relaxed else name
            edit = getattr(self._ui, attribute, None)
            if edit is None and relaxed:
                edit = self._insertRelaxedRow(name)
            if edit is None:
                continue
            self._editors[path] = edit
            self._titles[path] = self._rowTitle(edit) or path

    def _insertRelaxedRow(self, name):
        form = self._ui.formLayout
        strict = getattr(self._ui, name, None)
        if strict is None:
            return None
        row, _role = form.getWidgetPosition(strict)
        if row < 0:
            return None
        edit = QLineEdit(self)
        edit.setObjectName(f'{name}Relaxed')
        edit.setPlaceholderText(self.tr('same as strict'))
        edit.setToolTip(self.tr(
            'Limit used only in the phases snappyHexMesh is allowed to '
            'relax, layer addition above all. Leave empty and the strict '
            'limit governs there too.'))
        title = self._rowTitle(strict) or name
        form.insertRow(row + 1, self.tr('{0} (Relaxed)').format(title), edit)
        return edit

    def _rowTitle(self, widget):
        form = self._ui.formLayout
        row, _role = form.getWidgetPosition(widget)
        if row < 0:
            return ''
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        return item.widget().text() if item is not None else ''

    def accept(self):
        try:
            for path, edit in self._editors.items():
                value = edit.text()
                if (path.startswith('relaxed/')
                        and path.split('/', 1)[1] not in RELAXED_WITH_DEFAULT):
                    # Unset means "the strict limit governs here too", which
                    # is snappyHexMesh's own rule, so an empty box clears the
                    # key rather than writing a value.
                    value = value.strip() or None
                self._dbElement.setValue(path, value, self._titles[path])

            # C31-12. One of the two synchronous facade calls left in the
            # view, and it stays synchronous on purpose: this dialog closes by
            # returning from `accept()`, and it must stay open when the commit
            # is refused. Scheduling the write would close the dialog first
            # and discover the refusal afterwards, with the values gone.
            app.facadeClient.commit_working_copy_sync(
                self._dbElement, action='update mesh quality parameters')

            super().accept()
        except ValidationError as e:
            QMessageBox.information(self, self.tr("Input Error"), e.toMessage())
