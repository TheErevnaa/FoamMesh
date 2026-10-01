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
from foammesh.core.facade.fields import REGISTRY
from foammesh.core.quality.policy import (
    NOT_INHERITED_BY_RELAXED,
    OPTIONAL_LIMITS,
    RELAXABLE_LIMITS,
    RELAXED_WITH_DEFAULT,
    STORAGE_ROOT,
    storage_paths,
)
from foammesh.view.theming.metrics import align_unit_column, place_unit
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
        # DP-213 took two rows out of the form as "not consumed by
        # Foundation 13 snappy mesh quality". That was right for
        # `minTriangleTwist`, which v13 has no key for, so its row stays
        # out. It was wrong for `minFaceFlatness`: v13's
        # `meshCheck::checkMesh` (src/meshCheck/checkMesh.C:78-83) runs the
        # flatness check whenever the key is present. The Plan 29 key oracle
        # scanned libsnappyHexMesh and not libmeshCheck, which is where the
        # lookup lives. Plan 37 UF15 measured it (evidence in
        # plans/evidence/plan37/uf15-v13-controls.md) and the row is back,
        # bound through the quality policy like every other limit.
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
        the strict row they loosen.

        DP-170. The label is the registry's too, not a second wording typed
        into Designer beside the same field. Plan 30 WP-04 made this dialog
        and the QA page read one field list so they could not hold different
        fields; they went on holding different *names* for them, thirteen of
        the fifteen, because each had written its own. There is one name now,
        and this is where the dialog reads it.
        """
        prefix = f'{STORAGE_ROOT}/'
        units = []
        for full in storage_paths():
            path = full[len(prefix):]
            name = path.rsplit('/', 1)[-1]
            relaxed = path.startswith('relaxed/')
            attribute = f'{name}Relaxed' if relaxed else name
            title = self._registryTitle(full) or self._rowTitle(
                getattr(self._ui, attribute, None)) or name
            edit = getattr(self._ui, attribute, None)
            if edit is None and relaxed:
                edit = self._insertRelaxedRow(name, title)
            if edit is None:
                continue
            self._setRowTitle(edit, title)
            self._editors[path] = edit
            self._titles[path] = title
            descriptor = REGISTRY.by_storage_path(full)
            units.append((edit, descriptor.unit if descriptor else ''))
        # DP-198. After the loop, not inside it: `place_unit` puts the box in
        # a cell of its own, and from that moment the form knows the cell
        # rather than the box -- which is what `_insertRelaxedRow` asks it
        # about when it looks up the strict row to insert beneath.
        for edit, unit in units:
            place_unit(edit, unit)
        align_unit_column((self._ui.formLayout,))

    def _registryTitle(self, storage_path):
        """The one name this field has, with its unit if it has one.

        The QA page puts the unit in a column of its own; this form is two
        columns of label and box with nowhere to put one, so it goes in
        brackets at the end of the name -- which is where the app's other
        in-label units sit, and it is the same string either way because it
        is the same descriptor.

        DP-198. It used to end `Max face non-orthogonality (deg)`, because
        this form is two columns of label and box and there was said to be
        nowhere else to put a unit. There is: `place_unit` opens the column
        beside the box that `unit_cell` opens for a row a page builds itself,
        and the name is the name again.
        """
        descriptor = REGISTRY.by_storage_path(storage_path)
        return '' if descriptor is None else descriptor.title

    def _insertRelaxedRow(self, name, title):
        form = self._ui.formLayout
        strict = getattr(self._ui, name, None)
        if strict is None:
            return None
        row, _role = form.getWidgetPosition(strict)
        if row < 0:
            return None
        edit = QLineEdit(self)
        edit.setObjectName(f'{name}Relaxed')
        if name in NOT_INHERITED_BY_RELAXED:
            # Plan 37 UF15. OpenFOAM 13 finds this key in the relaxed block
            # without looking at the strict one, so "same as strict" would
            # be a promise the mesher does not keep.
            edit.setPlaceholderText(self.tr('off'))
            edit.setToolTip(self.tr(
                'Limit used only in the phases snappyHexMesh is allowed to '
                'relax, layer addition above all. This one does not inherit '
                'the strict value: leave it empty and there is no such '
                'check in those phases.'))
        else:
            edit.setPlaceholderText(self.tr('same as strict'))
            edit.setToolTip(self.tr(
                'Limit used only in the phases snappyHexMesh is allowed to '
                'relax, layer addition above all. Leave empty and the strict '
                'limit governs there too.'))
        form.insertRow(row + 1, title, edit)
        return edit

    def _labelFor(self, widget):
        form = self._ui.formLayout
        row, _role = form.getWidgetPosition(widget)
        if row < 0 and widget.parentWidget() is not None:
            # DP-198. The box sits in a cell with its unit, so the row the
            # form holds is the cell's.
            row, _role = form.getWidgetPosition(widget.parentWidget())
        if row < 0:
            return None
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        return None if item is None else item.widget()

    def _rowTitle(self, widget):
        if widget is None:
            return ''
        label = self._labelFor(widget)
        return label.text() if label is not None else ''

    def _setRowTitle(self, widget, title):
        label = self._labelFor(widget)
        if label is not None:
            label.setText(title)

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
                elif path in OPTIONAL_LIMITS:
                    # Plan 37 UF15. No shipped value: empty stores nothing,
                    # so the key is not written and the check stays off.
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
            QMessageBox.warning(self, self.tr("Input error"), e.toMessage())
