"""Plan 43 LY11: the Mesh quality tab's surface rows.

While the model is coloured by deviation, the tab carries the figures of the
surfaces (max, mean, RMS and the share within the tolerance) and the
tolerance control, the second home of the control the legend's range row
carries. Both write the one tolerance.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QVBoxLayout)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell


class SurfaceRowsBox(QGroupBox):
    """Deviation figures and the tolerance, inside the Mesh quality tab."""

    #: Tolerance in mm.
    toleranceChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('meshQualitySurfaceRows')
        self.setTitle(self.tr('Surface rows'))
        layout = QVBoxLayout(self)
        self._stats = QLabel(self)
        self._stats.setObjectName('surfaceRowsStats')
        self._stats.setWordWrap(True)
        layout.addWidget(self._stats)
        form = QFormLayout()
        self._tolerance = CompactDoubleSpinBox()
        self._tolerance.setObjectName('surfaceTolerance')
        self._tolerance.setDecimals(4)
        self._tolerance.setRange(0.0, 1.0e6)
        self._tolerance.setSingleStep(0.01)
        self._tolerance.setAccessibleName(self.tr('Tolerance'))
        self._tolerance.setToolTip(self.tr(
            'Faces within this distance of the reference count as on it'))
        form.addRow(self.tr('Tolerance ±'),
                    unit_cell(self._tolerance, 'mm'))
        layout.addLayout(form)
        self._tolerance.valueChanged.connect(
            lambda value: self.toleranceChanged.emit(float(value)))
        self.setVisible(False)

    def tolerance(self) -> float:
        return float(self._tolerance.value())

    def toleranceBox(self) -> CompactDoubleSpinBox:
        return self._tolerance

    def statsText(self) -> str:
        return self._stats.text()

    def adoptTolerance(self, value: float) -> None:
        """Show a tolerance edited elsewhere, without a signal."""
        self._tolerance.blockSignals(True)
        self._tolerance.setValue(abs(float(value)))
        self._tolerance.blockSignals(False)

    def showDeviation(self, spec, tolerance: float) -> None:
        """Show the deviation figures of a legend spec."""
        self.adoptTolerance(tolerance)
        self._stats.setText('\n'.join(spec.statistics))
        self.setVisible(True)

    def clearDeviation(self) -> None:
        self._stats.setText('')
        self.setVisible(False)
