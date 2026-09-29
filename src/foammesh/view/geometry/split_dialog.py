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
from foammesh.view.theming.metrics import FORM_MARGIN, place_unit
from foammesh.view.theming.status_colors import apply_color_swatch
from foammesh.view.theming.vtk_theme import rgb
from foammesh.view.theming.patch_palette import active_palette, slot_colour
from foammesh.rendering.actor_info import applySurfaceMaterial
from foammesh.support import disposal


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
        layout.setContentsMargins(FORM_MARGIN, 0, FORM_MARGIN, 0)
        layout.addWidget(self._colorWidget)

        self._colorWidget.setMinimumSize(16, 16)
        parent.setItemWidget(self, 1, widget)


def rankSegments(segments, regionedData=None) -> dict:
    """Each preview segment's position in the order its patch is written.

    DP-819. The store numbers the pieces of each file largest first, ties by
    first triangle, and the pieces reach the case -- and take their palette
    slots -- in that order, file after file. The preview ranks its segments
    the same way, so segment and patch land on the same slot. Without the
    region data (nothing to read files or first triangles from) the ranking
    falls back to area alone.
    """
    areas = {}
    for position, (region, area) in enumerate(segments):
        try:
            region = int(region)
        except (TypeError, ValueError):
            region = position
        areas[region] = float(area)
    files, firsts = {}, {}
    try:
        import numpy as np
        from vtkmodules.util.numpy_support import vtk_to_numpy

        cells = regionedData.GetCellData()
        regionIds = vtk_to_numpy(cells.GetArray('RegionId')).astype(np.int64)
        fileArray = cells.GetArray('fIndex')
        fileIds = (vtk_to_numpy(fileArray).astype(np.int64)
                   if fileArray is not None else np.zeros_like(regionIds))
        for region in areas:
            where = np.flatnonzero(regionIds == region)
            if where.size:
                firsts[region] = int(where[0])
                files[region] = int(fileIds[where[0]])
    except Exception:  # noqa: BLE001 - a stand-in or empty preview
        files, firsts = {}, {}
    order = sorted(areas, key=lambda region: (
        files.get(region, 0), -areas[region], firsts.get(region, region), region))
    return {region: rank for rank, region in enumerate(order)}


class SplitDialog(QDialog):
    def __init__(self, parent, files: [Path], angle, firstSlot: int = 0):
        super().__init__(parent)

        #: DP-819. The palette slot the first new patch will take in the main
        #: window: one past the surfaces the case already holds.
        self._firstSlot = int(firstSlot or 0)
        self._segments = []
        self._ranks = {}

        self._ui = Ui_SplitDialog()
        self._ui.setupUi(self)

        self._view = self._ui.renderingView

        self._future: Optional[asyncio.Future] = None
        #: Plan 35 CR3 step 9. What the fields held when the dialog closed.
        #: The dialog deletes itself on close, and the page reads the angle
        #: and the smallest piece after it has gone.
        self._settled: Optional[tuple[float, float]] = None
        self._disposed = False
        disposal.track(self, 'SplitDialog')

        self._stlImporter = StlImporter()

        self._ui.featureAngleSlider.setValue(angle)
        self._ui.featureAngleText.setValidator(QIntValidator(0, 180))
        self._ui.featureAngleText.setText(str(angle))

        self._ui.minAreaSlider.setRange(0, 100)
        self._ui.minAreaText.setValidator(QDoubleValidator(0, 100, -1))

        # DP-198. Both groups named their unit in their own title --
        # `Feature angle (deg)`, `Area threshold (%)` -- which put it at the
        # top of a group whose numbers are at the bottom. It sits beside the
        # box it belongs to now. The tree's `Area (%)` stays as it is: a
        # column of numbers is where a heading is the right place for one.
        place_unit(self._ui.featureAngleText, 'deg')
        place_unit(self._ui.minAreaText, '%')

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
        # DP-819. One table entry per segment, filled from the patch palette
        # in `_applyPalette`; the rainbow scale this replaced is not what the
        # patch list or the viewport draw with.
        self._regionMapper.SetLookupTable(vtkLookupTable())

        self._regionActor = vtkActor()
        self._regionActor.SetMapper(self._regionMapper)
        applySurfaceMaterial(self._regionActor.GetProperty())
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
        # The patches follow the theme too, as they do in the main window.
        self._applyPalette()
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
        self._segments = list(segments)
        self._ranks = rankSegments(self._segments, regionedData)

        self._edgeMapper.RemoveAllInputs()
        self._edgeMapper.SetInputData(edges)
        self._edgeMapper.Update()

        self._regionMapper.RemoveAllInputs()
        self._regionMapper.SetInputData(regionedData)
        self._applyPalette()

        self._ui.numSegments.setText(f'{len(segments) :,}')

        self._ui.segments.clear()
        self._view.refresh()

        for i in range(0, len(segments)):
            SegmentItem(self._ui.segments, i, self._getColor(i), segments[i][1])

        # This segmentation is now on screen; there is nothing to redo until
        # the angle or the minimum area changes.
        self._ui.apply.setEnabled(False)

        self._view.refresh()

    def paletteSlot(self, region) -> int:
        """The main-window palette slot the patch from ``region`` takes."""
        return self._firstSlot + self._ranks.get(int(region), int(region))

    def _palette(self):
        tokens = None
        if app.themeManager is not None:
            tokens = app.themeManager.tokens
        return active_palette(tokens)

    def _getColor(self, value):
        """The colour segment ``value`` wears in the table and the preview.

        DP-819. The same patch-palette slot the geometry row's swatch and the
        viewport actor read, not a scale of its own.
        """
        count = max(1, len(self._segments))
        region = min(max(int(round(value)), 0), count - 1)
        return QColor(slot_colour(self._palette(), self.paletteSlot(region)))

    def _applyPalette(self):
        """Fill the preview's lookup table, one entry per segment."""
        count = max(1, len(self._segments))
        lut = vtkLookupTable()
        lut.SetNumberOfTableValues(count)
        for region in range(count):
            colour = self._getColor(region)
            lut.SetTableValue(region, colour.redF(), colour.greenF(),
                              colour.blueF(), 1.0)
        # Each integer region id sits in the middle of its own entry.
        lut.SetTableRange(-0.5, count - 0.5)
        lut.Build()
        self._regionMapper.SetLookupTable(lut)
        self._regionMapper.SetScalarRange(-0.5, count - 0.5)
        self._regionMapper.Update()
        tree = self._ui.segments
        for row in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(row)
            widget = getattr(item, '_colorWidget', None)
            if widget is not None:
                apply_color_swatch(widget, self._getColor(int(item.text(0))))

    def featureAngle(self) -> float:
        """The angle the user settled on, in degrees."""
        if self._settled is not None:
            return self._settled[0]
        return float(self._ui.featureAngleText.text())

    def minAreaFraction(self) -> float:
        """The smallest piece worth keeping, as a fraction of the area.

        The field shows a percentage; the geometry artifact store, which
        makes the cut the meshers see, takes a fraction.
        """
        if self._settled is not None:
            return self._settled[1]
        return float(self._ui.minAreaText.text()) / 100

    def _settle(self):
        """Keep the two numbers the page reads once the dialog is gone."""
        if self._settled is not None:
            return
        try:
            self._settled = (self.featureAngle(), self.minAreaFraction())
        except (ValueError, RuntimeError):
            # A field left unparseable; the page is never asked for it,
            # because only OK leads there and OK needs both.
            self._settled = None

    def show(self) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()

        super().show()

        return self._future

    def _okClicked(self):
        self._settle()
        if self._future is not None and not self._future.done():
            volumes, surfaces = self._stlImporter.identifyVolumes()
            self._future.set_result((volumes, surfaces))

        self.close()

    def _cancelClicked(self):
        if self._future is not None and not self._future.cancelled():
            self._future.cancel()

        self.close()

    def dispose(self):
        """Release the preview's actors on the GUI thread, once.

        Plan 35 CR3 step 6 (F6). The preview is a second OpenGL context. Its
        actors are released against it -- made current first, because the
        main viewport's is the one usually current -- and taken out of its
        renderer, before the view finalises the window.
        """
        if self._disposed:
            return
        self._disposed = True
        disposal.untrack(self, 'SplitDialog')
        if app.themeManager is not None:
            try:
                app.themeManager.themeChanged.disconnect(self._applyTheme)
            except (RuntimeError, TypeError, AttributeError):
                pass
        actors = (self._edgeActor, self._regionActor, self._highlightActor)
        renderWindow = getattr(self._view, 'renderWindow', None)
        disposal.release_graphics_resources(
            actors, renderWindow() if callable(renderWindow) else None)
        removeActor = getattr(self._view, 'removeActor', None)
        if callable(removeActor):
            for actor in actors:
                removeActor(actor)
        self._regionedData = None

    def closeEvent(self, event):
        self._settle()
        if self._future is not None and not self._future.done():
            self._future.cancel()

        self.dispose()
        # The view releases what is left on the GPU and then finalises.
        self._view.close()

        event.accept()
        # F6. The dialog, and the render window inside it, used to live on
        # as a hidden child of the page after every split.
        self.deleteLater()
