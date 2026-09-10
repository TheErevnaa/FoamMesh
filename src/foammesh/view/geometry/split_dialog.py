#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import QDialog, QTreeWidgetItem, QLabel, QTreeWidget, QWidget, QHBoxLayout, QHeaderView
from PySide6.QtGui import QColor, QDoubleValidator, QIntValidator

from vtkmodules.vtkCommonCore import vtkLookupTable
from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkActor

from foammesh.view.geometry.split_dialog_ui import Ui_SplitDialog
from foammesh.view.geometry.stl_utility import StlImporter
from foammesh.app import app
from foammesh.view.theming.status_colors import apply_color_swatch
from foammesh.view.theming.vtk_theme import rgb

from foammesh.support.colormap import getLookupTable


def regionPolyData(regionedData, regionId: int):
    """Just the cells of one region of the split preview, as polydata.

    R48. Selecting a segment row highlighted the row and changed nothing in
    the 3D preview, and two of the five segments the dialog reported had
    IDENTICAL areas (3.14 percent each) -- the preview was the only thing that
    could tell them apart and it did not answer. Returns None for anything
    that cannot be thresholded, so the caller simply hides the highlight.
    """
    try:
        from vtkmodules.vtkCommonDataModel import vtkDataObject
        from vtkmodules.vtkFiltersCore import vtkThreshold
        from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

        threshold = vtkThreshold()
        threshold.SetInputData(regionedData)
        threshold.SetInputArrayToProcess(
            0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, 'RegionId')
        threshold.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
        threshold.SetLowerThreshold(regionId - 0.5)
        threshold.SetUpperThreshold(regionId + 0.5)
        threshold.Update()

        # vtkThreshold yields an unstructured grid; the mapper wants polydata.
        geometry = vtkGeometryFilter()
        geometry.SetInputData(threshold.GetOutput())
        geometry.Update()
        output = geometry.GetOutput()
        return output if output.GetNumberOfCells() else None
    except Exception:  # noqa: BLE001 - a highlight is never worth an exception
        return None


class SegmentItem(QTreeWidgetItem):
    def __init__(self, parent: QTreeWidget, sid: int, color: QColor, area: float):
        super().__init__(parent, [str(sid), None, f'{area:.3g}'])

        self._colorWidget = QLabel()

        apply_color_swatch(self._colorWidget, color)

        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(9, 1, 9, 1)
        layout.addWidget(self._colorWidget)

        self._colorWidget.setMinimumSize(16, 16)
        parent.setItemWidget(self, 1, widget)


class SplitDialog(QDialog):
    def __init__(self, parent, files: [Path], angle):
        super().__init__(parent)

        self._ui = Ui_SplitDialog()
        self._ui.setupUi(self)

        self._view = self._ui.renderingView

        self._future: Optional[asyncio.Future] = None

        self._stlImporter = StlImporter()

        self._ui.featureAngleSlider.setValue(angle)
        self._ui.featureAngleText.setValidator(QIntValidator(0, 180))
        self._ui.featureAngleText.setText(str(angle))

        self._ui.minAreaSlider.setRange(0, 100)
        self._ui.minAreaText.setValidator(QDoubleValidator(0, 100, -1))

        self._ui.segments.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._ui.segments.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._ui.segments.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        self._edgeMapper = vtkPolyDataMapper()
        self._edgeMapper.ScalarVisibilityOff()

        self._edgeActor = vtkActor()
        self._edgeActor.SetMapper(self._edgeMapper)
        edge = '#f5f7fa'
        if app.themeManager is not None and app.themeManager.tokens is not None:
            edge = app.themeManager.tokens.value('foreground.primary')
        self._edgeActor.GetProperty().SetColor(*rgb(edge))
        self._edgeActor.GetProperty().SetLineWidth(2.0)

        self._view.addActor(self._edgeActor)

        self._regionMapper = vtkPolyDataMapper()
        self._regionMapper.ScalarVisibilityOn()
        self._regionMapper.SelectColorArray('RegionId')
        self._regionMapper.SetScalarModeToUseCellData()
        self._regionMapper.SetColorModeToMapScalars()
        lut = getLookupTable('rainbow')
        self._regionMapper.SetLookupTable(lut)

        self._regionActor = vtkActor()
        self._regionActor.SetMapper(self._regionMapper)
        self._regionActor.GetProperty().SetOpacity(1)
        self._regionActor.GetProperty().SetRepresentationToSurface()
        self._regionActor.GetProperty().EdgeVisibilityOff()

        self._view.addActor(self._regionActor)

        # R48. The segment the user picked in the table, drawn opaque and
        # outlined on top of the rest, so a row selection answers the question
        # the table cannot: which of two same-sized segments is this one.
        self._regionedData = None
        self._highlightMapper = vtkPolyDataMapper()
        self._highlightMapper.ScalarVisibilityOff()
        self._highlightMapper.SetResolveCoincidentTopologyToPolygonOffset()

        self._highlightActor = vtkActor()
        self._highlightActor.SetMapper(self._highlightMapper)
        highlight = '#ffffff'
        if app.themeManager is not None and app.themeManager.tokens is not None:
            highlight = app.themeManager.tokens.value('accent.default')
        self._highlightActor.GetProperty().SetColor(*rgb(highlight))
        self._highlightActor.GetProperty().SetRepresentationToSurface()
        self._highlightActor.GetProperty().EdgeVisibilityOn()
        self._highlightActor.GetProperty().SetEdgeColor(*rgb(highlight))
        self._highlightActor.GetProperty().SetLineWidth(3.0)
        self._highlightActor.GetProperty().SetAmbient(0.4)
        self._highlightActor.SetVisibility(False)

        self._view.addActor(self._highlightActor)

        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        self._stlImporter.load(files)

        self._apply()

        # R109. The preview used to open looking straight down the model axis,
        # so a venturi rendered as one flat red disk and the four coloured
        # segments the dialog exists to show were all hidden behind it; neither
        # the 2D toggle nor fit-to-view changed that, only dragging by hand
        # did. An isometric view shows the split from the start (setViewPreset
        # fits as part of the move).
        self._view.setViewPreset('isometric')

        self._connectSignalsSlots()
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(self._applyTheme)

    def _applyTheme(self, _name):
        if app.themeManager is None or app.themeManager.tokens is None:
            return
        self._edgeActor.GetProperty().SetColor(
            *rgb(app.themeManager.tokens.value('foreground.primary')))
        self._view.refresh()

    def _connectSignalsSlots(self):
        self._ui.featureAngleSlider.valueChanged.connect(self._featureAngleSliderChanged)
        self._ui.featureAngleText.editingFinished.connect(self._featureAngleTextEditingFinished)

        self._ui.minAreaSlider.valueChanged.connect(self._minAreaSliderChanged)
        self._ui.minAreaText.editingFinished.connect(self._minAreaTextEditingFinished)

        self._ui.apply.clicked.connect(self._apply)
        # R48. Apply offered to redo a split that had not been
        # reparameterised. It comes back only when the angle or the smallest
        # piece worth keeping actually changes; both sliders write their text
        # field, so watching the text covers every way in.
        self._ui.featureAngleText.textChanged.connect(self._parametersChanged)
        self._ui.minAreaText.textChanged.connect(self._parametersChanged)

        self._ui.segments.itemSelectionChanged.connect(self._segmentSelected)

        self._ui.alignAxis.clicked.connect(self._view.alignCamera)
        self._ui.axis.toggled.connect(self._view.setAxisVisible)
        self._ui.cubeAxis.toggled.connect(self._view.setCubeAxisVisible)
        self._ui.fit.clicked.connect(self._view.fitCamera)
        self._ui.perspective.toggled.connect(self._view.setParallelProjection)
        self._ui.rotate.clicked.connect(self._view.rollCamera)

        self._ui.okButton.clicked.connect(self._okClicked)
        self._ui.cancelButton.clicked.connect(self._cancelClicked)

    def _featureAngleSliderChanged(self, value):
        self._ui.featureAngleText.setText(str(value))

    def _featureAngleTextEditingFinished(self):
        angle = int(self._ui.featureAngleText.text())
        self._ui.featureAngleSlider.setValue(angle)

    def _minAreaSliderChanged(self, value):
        self._ui.minAreaText.setText(f'{value:.3g}')

    def _minAreaTextEditingFinished(self):
        minArea = float(self._ui.minAreaText.text())

        with QSignalBlocker(self._ui.minAreaSlider):
            self._ui.minAreaSlider.setValue(minArea)

    def _parametersChanged(self, _text=None):
        self._ui.apply.setEnabled(True)

    def _segmentSelected(self):
        """Show the picked segment in the preview, not only in the table."""
        items = self._ui.segments.selectedItems()
        piece = None
        if items and self._regionedData is not None:
            piece = regionPolyData(self._regionedData, int(items[0].text(0)))

        if piece is None:
            self._highlightActor.SetVisibility(False)
        else:
            self._highlightMapper.RemoveAllInputs()
            self._highlightMapper.SetInputData(piece)
            self._highlightMapper.Update()
            self._highlightActor.SetVisibility(True)

        self._view.refresh()

    def _apply(self):
        angle = float(self._ui.featureAngleText.text())
        minArea = float(self._ui.minAreaText.text()) / 100

        segments, regionedData, edges = self._stlImporter.split(angle, minArea)
        self._regionedData = regionedData

        self._edgeMapper.RemoveAllInputs()
        self._edgeMapper.SetInputData(edges)
        self._edgeMapper.Update()

        self._regionMapper.RemoveAllInputs()
        self._regionMapper.SetInputData(regionedData)
        self._regionMapper.SetScalarRange(0, len(segments)-1)
        self._regionMapper.Update()

        self._ui.numSegments.setText(f'{len(segments) :,}')

        self._ui.segments.clear()
        self._view.refresh()

        for i in range(0, len(segments)):
            SegmentItem(self._ui.segments, i, self._getColor(i), segments[i][1])

        # This segmentation is now on screen; there is nothing to redo until
        # the angle or the minimum area changes.
        self._ui.apply.setEnabled(False)

        self._view.refresh()

    def _getColor(self, value):
        minValue, maxValue = self._regionMapper.GetScalarRange()
        lut: vtkLookupTable = self._regionMapper.GetLookupTable()
        lut.SetRange(minValue, maxValue)

        rgb = [0, 0, 0]
        if value < minValue:
            value = minValue
        elif value > maxValue:
            value = maxValue

        lut.GetColor(value, rgb)

        return QColor.fromRgbF(rgb[0], rgb[1], rgb[2])

    def featureAngle(self) -> float:
        """The angle the user settled on, in degrees."""
        return float(self._ui.featureAngleText.text())

    def minAreaFraction(self) -> float:
        """The smallest piece worth keeping, as a fraction of the area.

        The field shows a percentage; the geometry artifact store, which
        makes the cut the meshers see, takes a fraction.
        """
        return float(self._ui.minAreaText.text()) / 100

    def show(self) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()

        super().show()

        return self._future

    def _okClicked(self):
        if not self._future.done():
            volumes, surfaces = self._stlImporter.identifyVolumes()
            self._future.set_result((volumes, surfaces))

        self.close()

    def _cancelClicked(self):
        if not self._future.cancelled():
            self._future.cancel()

        self.close()

    def closeEvent(self, event):
        if not self._future.done():
            self._future.cancel()

        self._view.close()

        event.accept()
