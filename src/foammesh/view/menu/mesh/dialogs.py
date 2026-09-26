"""Compact structured dialogs for U3 mesh operations."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QPlainTextEdit, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from foammesh.core.mesh import MeshInfo, MeshInfoService, MeshTransformRequest, parse_vector
from foammesh.core.mesh.transform import parse_scale
from foammesh.core.quality import QualityReport
from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell


def _readonly_text(text: str) -> QPlainTextEdit:
    widget = QPlainTextEdit(text)
    widget.setReadOnly(True)
    return widget


def _put(table, row, column, value, *, numeric: bool = False) -> None:
    """One cell. DP-175: a count is grouped and right-aligned, always.

    The patch table drew 1284 and the status bar behind it drew 1,284
    for the same set, and every number in these tables started at the
    left edge of its column, so a column of face counts could not be
    compared by eye at all.
    """
    if value is None:
        text = ''
    elif numeric and isinstance(value, int) and not isinstance(value, bool):
        text = f'{value:,}'
    else:
        text = str(value)
    item = QTableWidgetItem(text)
    if numeric:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
    table.setItem(row, column, item)


class MeshInfoDialog(QDialog):
    def __init__(self, info: MeshInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self.setWindowTitle(self.tr('Mesh info'))
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(_readonly_text(info.to_text()), self.tr('Summary'))
        tabs.addTab(self._patch_table(), self.tr('Patches'))
        tabs.addTab(self._zone_table(), self.tr('Zones'))
        tabs.addTab(self._file_table(), self.tr('Files'))
        tabs.addTab(_readonly_text('\n'.join(info.warnings) or self.tr('No parser warnings.')),
                    self.tr('Warnings'))
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_button = buttons.addButton(self.tr('Copy summary'), QDialogButtonBox.ButtonRole.ActionRole)
        save_button = buttons.addButton(self.tr('Save report…'), QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(info.to_text()))
        save_button.clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _patch_table(self):
        table = QTableWidget(len(self._info.patches), 4)
        table.setHorizontalHeaderLabels((self.tr('Name'), self.tr('Type'),
                                         self.tr('Faces'), self.tr('Start face')))
        for row, patch in enumerate(self._info.patches):
            for column, value in enumerate((patch.name, patch.patch_type,
                                            patch.face_count, patch.start_face)):
                _put(table, row, column, value, numeric=column > 1)
        table.resizeColumnsToContents()
        return table

    def _zone_table(self):
        table = QTableWidget(len(self._info.zones), 3)
        table.setHorizontalHeaderLabels((self.tr('Name'), self.tr('Kind'), self.tr('Size')))
        for row, zone in enumerate(self._info.zones):
            for column, value in enumerate((zone.name, zone.kind, zone.size)):
                _put(table, row, column, value, numeric=column == 2)
        table.resizeColumnsToContents()
        return table

    def _file_table(self):
        table = QTableWidget(len(self._info.file_metadata), 6)
        table.setHorizontalHeaderLabels((self.tr('Object'), self.tr('Class'), self.tr('Format'),
                                         self.tr('Compression'), self.tr('Location'), self.tr('File')))
        for row, item in enumerate(self._info.file_metadata):
            values = (item.object_name, item.file_class, item.format,
                      item.compression, item.location, item.name)
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value or ''))
        table.resizeColumnsToContents()
        return table

    def _save(self):
        path, selected_filter = QFileDialog.getSaveFileName(
            self, self.tr('Save mesh report'), str(self._info.case_path / 'mesh-info.json'),
            self.tr('JSON report (*.json);;CSV report (*.csv)'))
        if not path:
            return
        if not Path(path).suffix:
            path += '.csv' if 'CSV' in selected_filter else '.json'
        try:
            MeshInfoService.save_report(self._info, path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, self.tr('Save mesh report'), str(error))


class QualityDashboardDialog(QDialog):
    setSelected = Signal(str)

    def __init__(self, report: QualityReport, raw_log: str, parent=None):
        super().__init__(parent)
        self._report = report
        self.setWindowTitle(self.tr('Mesh check dashboard'))
        self.resize(820, 620)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        verdict = [
            self.tr('Verdict: {0}').format(report.result.severity.upper()),
            self.tr('Checked: {0}').format(report.checked_at),
            self.tr('Mesh fingerprint: {0}').format(report.mesh_fingerprint),
            self.tr('State: STALE') if report.stale else self.tr('State: current'),
        ]
        tabs.addTab(_readonly_text('\n'.join(verdict)), self.tr('Verdict'))
        metrics = report.result.to_dict()
        tabs.addTab(_readonly_text(json.dumps(metrics, indent=2)), self.tr('Key metrics'))
        tabs.addTab(_readonly_text('\n'.join(report.result.failed_check_details) or
                                    self.tr('No failed checks were reported.')),
                    self.tr('Failed checks'))
        tabs.addTab(self._sets_tab(), self.tr('Patches / sets'))
        tabs.addTab(_readonly_text(self._recommendations_text(report)),
                    self.tr('Recommendations'))
        tabs.addTab(_readonly_text(raw_log), self.tr('Raw log'))
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _recommendations_text(report: QualityReport) -> str:
        """Render the deterministic checkMesh-failure → remedy mapping.

        Uses the shared ``repair_recommendations`` table (the authoritative
        advice source) rather than only the parser's free-text lines.
        """
        from foammesh.core.quality.repair_recommendations import recommendations
        lines = []
        for item in recommendations(report.result):
            actions = ', '.join(item['actions']) if item['actions'] else 'no automated remedy'
            lines.append(f"• {item['failure']}")
            lines.append(f"    advice:  {item['advice']}")
            lines.append(f"    actions: {actions}")
        general = list(report.result.recommendations)
        if general:
            lines.append('')
            lines.append('General guidance:')
            lines.extend(f'  - {line}' for line in general)
        return '\n'.join(lines) or 'No recommendations.'

    def _sets_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        table = QTableWidget(len(self._report.sets), 3)
        table.setHorizontalHeaderLabels((self.tr('Name'), self.tr('Kind'), self.tr('Count')))
        for row, item in enumerate(self._report.sets):
            for column, key in enumerate(('name', 'kind', 'count')):
                _put(table, row, column, item.get(key, ''),
                     numeric=column == 2)
        table.resizeColumnsToContents()
        layout.addWidget(table)
        highlight = QPushButton(self.tr('Highlight selected cell set'))
        highlight.clicked.connect(
            lambda: self.setSelected.emit(table.item(table.currentRow(), 0).text())
            if table.currentRow() >= 0 else None)
        layout.addWidget(highlight)
        return widget


class TransformDialog(QDialog):
    def __init__(self, operation: str, info: MeshInfo, parent=None, *,
                 fields_present: bool = False, field_transform_supported: bool = False):
        super().__init__(parent)
        self._operation = operation
        self._info = info
        # DP-171. `.title()` was the only reason three of the product's
        # window titles were capitalised mid-sentence.
        self.setWindowTitle(self.tr('Mesh {0}').format(operation))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._vector = QLineEdit('1 1 1' if operation == 'scale' else '0 0 0')
        self._angle = CompactDoubleSpinBox()
        self._angle.setRange(-360000.0, 360000.0)
        self._angle.setDecimals(6)
        self._angle.setValue(90.0)
        self._pivot = QLineEdit()
        self._transform_fields = QCheckBox(self.tr('Rotate vector/tensor fields'))
        self._source_unit = QComboBox(); self._target_unit = QComboBox()
        for combo in (self._source_unit, self._target_unit):
            combo.addItems(('m', 'cm', 'mm', 'um'))
        self._target_unit.setCurrentText('m')
        if operation == 'rotate':
            self._vector.setText('Z')
            form.addRow(self.tr('Axis (X/Y/Z or vector)'), self._vector)
            # DP-164. `(degrees)` was the app's fifth spelling of `deg`.
            form.addRow(self.tr('Angle'), unit_cell(self._angle, 'deg'))
            form.addRow(self.tr('Pivot (optional)'), self._pivot)
            if fields_present:
                self._transform_fields.setEnabled(field_transform_supported)
                form.addRow(self.tr('Existing fields'), self._transform_fields)
        else:
            form.addRow(
                self.tr('Factor (one value, or X Y Z)') if operation == 'scale'
                else self.tr('Components (X Y Z)'), self._vector)
            if operation == 'scale':
                units = QHBoxLayout(); units.addWidget(self._source_unit); units.addWidget(QLabel('→'))
                units.addWidget(self._target_unit)
                apply_units = QPushButton(self.tr('Use conversion'))
                apply_units.clicked.connect(self._use_units)
                units.addWidget(apply_units)
                form.addRow(self.tr('Unit shortcut'), units)
            else:
                form.addRow(self.tr('Vector unit'), QLabel(info.display_unit))
        layout.addLayout(form)
        self._warning = QLabel()
        self._warning.setWordWrap(True)
        layout.addWidget(self._warning)
        self._preview = QLabel()
        self._preview.setWordWrap(True)
        layout.addWidget(self._preview)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr('Run'))
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._vector.textChanged.connect(self._update_preview)
        self._pivot.textChanged.connect(self._update_preview)
        self._angle.valueChanged.connect(self._update_preview)
        self._update_preview()

    def request(self) -> MeshTransformRequest:
        if self._operation == 'rotate':
            axis_text = self._vector.text().strip()
            axis = axis_text if axis_text.lower() in {'x', 'y', 'z'} else parse_vector(axis_text)
            pivot = parse_vector(self._pivot.text()) if self._pivot.text().strip() else None
            request = MeshTransformRequest.rotation(axis, self._angle.value(), pivot)
            return MeshTransformRequest(
                request.operation, request.vector, request.angle_degrees,
                request.pivot, self._transform_fields.isChecked())
        if self._operation == 'scale':
            return MeshTransformRequest('scale', parse_scale(self._vector.text()))
        return MeshTransformRequest(self._operation, parse_vector(self._vector.text()))

    def _use_units(self):
        request = MeshTransformRequest.scale_units(
            self._source_unit.currentText(), self._target_unit.currentText())
        self._vector.setText(' '.join(format(value, '.12g') for value in request.vector))

    def _update_preview(self, *_args):
        try:
            request = self.request()
            preview = request.preview_bounds(self._info.bounds) if self._info.bounds else None
            self._warning.setText(
                self.tr('Warning: negative scale mirrors the mesh and may reverse orientation.')
                if request.has_negative_scale else '')
            self._preview.setText(
                self.tr('Resulting bounds ({0}): min {1}, max {2}, span {3}').format(
                    preview.unit, preview.minimum, preview.maximum, preview.span)
                if preview else self.tr('Bounding-box preview is unavailable for this mesh.'))
        except ValueError as error:
            self._preview.setText(self.tr('Enter a valid transform to preview it: {0}').format(error))

    def _accept_validated(self):
        try:
            self.request().argv()
        except ValueError as error:
            QMessageBox.warning(self, self.windowTitle(), str(error))
            return
        if self.request().has_negative_scale and QMessageBox.warning(
                self, self.windowTitle(),
                self.tr('Negative scale mirrors the mesh. Continue?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self.accept()
