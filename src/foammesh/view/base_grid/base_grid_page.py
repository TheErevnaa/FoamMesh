#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtWidgets import (QComboBox, QGridLayout, QLabel,
                               QMessageBox, QWidget)

from pathlib import Path

from foammesh.core.facade import FacadeError, ValidationFailedError as FacadeValidationError
from widgets.async_message_box import AsyncMessageBox
from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.db.configurations_schema import (
    BaseGridSizingMode, BoundaryPatchType, GeometryType, Shape, CFDType,
    schema)
from foammesh.core.mesh.sizing import (background_estimate,
                                       derive_background_counts,
                                       stand_off_bounds)
from foammesh.rendering.vtk_loader import hexPolyData, polyDataToFeatureActor
from foammesh.support import field_complaint
from foammesh.view.length_readout import format_lengths
from foammesh.view.step_page import StepPage
from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell
from foammesh.view.theming.vtk_theme import rgb


def _faceField(name):
    """``xMin`` as the field registry spells it: ``x_min``."""
    return f'{name[0]}_{name[1:].lower()}'


class BaseGridPage(StepPage):
    OUTPUT_TIME = 0

    #: The count the schema ships on every axis, and therefore the cell budget
    #: an untouched default is asking for (R15).
    DEFAULT_COUNT = 10
    DEFAULT_CELL_BUDGET = DEFAULT_COUNT ** 3

    def __init__(self, ui):
        super().__init__(ui, ui.baseGridPage)

        self._xLen = None
        self._yLen = None
        self._zLen = None
        #: Until a count is typed, or read back from a saved case, the shipped
        #: 10/10/10 is a placeholder this page may re-spend (R15).
        self._countsAreDefault = True
        self._boundingHex6 = None
        self._loaded = False
        self._outlineActor = None
        #: How many lines the last run streamed into the console (F3).
        self._streamedLines = 0

        self._sizingMode = QComboBox(ui.groupBox_2)
        self._sizingMode.setObjectName('baseGridSizingMode')
        self._sizingMode.addItem(self.tr('Direct cell counts'), BaseGridSizingMode.COUNTS.value)
        self._sizingMode.addItem(self.tr('Target cell size'), BaseGridSizingMode.TARGET_SIZE.value)
        self._targetCellSize = CompactDoubleSpinBox(ui.groupBox_2)
        self._targetCellSize.setObjectName('targetCellSize')
        self._targetCellSize.setDecimals(8)
        # DP-669. The floor is "Auto" (unset): the block diagonal / 40, as
        # the Gmsh global size. Any typed size is above it.
        self._targetCellSize.setRange(0.0, 1e12)
        self._targetCellSize.setSpecialValueText(self.tr('Auto'))
        self._targetCellSize.setValue(0.0)
        self._sizingWarning = QLabel(ui.groupBox_2)
        self._sizingWarning.setWordWrap(True)
        ui.formLayout.addRow(self.tr('Sizing mode'), self._sizingMode)
        # DP-164. The unit sits after the box in its own column, the way
        # every registry-built row has spelled a unit since DP-156.
        self._targetSizeCell = unit_cell(self._targetCellSize, 'm')
        ui.formLayout.addRow(self.tr('Target cell size'),
                             self._targetSizeCell)
        #: The form row the target size lives on, so the mode can hide it.
        self._targetSizeRow = ui.formLayout
        ui.formLayout.addRow(self._sizingWarning)
        self._standoffWarning = QLabel(ui.groupBox_2)
        self._standoffWarning.setObjectName('baseGridStandoffWarning')
        self._standoffWarning.setWordWrap(True)
        self._standoffWarning.setVisible(False)
        ui.formLayout.addRow(self._standoffWarning)

        self._buildBlockControls(ui)
        self._buildOutcomeLine(ui)

        self._connectSignalsSlots()

    # Plan 29 WP7.5. blockMeshDict has always had a ``convertToMeters`` scale,
    # a per-axis ``simpleGrading`` and a patch type per outer face; the page
    # wrote 1, ``1 1 1`` and six ``patch`` entries with no way to say otherwise.
    # A background block graded towards the geometry, or an outer face that has
    # to be ``symmetry`` or ``empty`` for the case to run at all, meant hand
    # editing the dictionary after every generate.
    def _buildBlockControls(self, ui):
        self._scale = CompactDoubleSpinBox(ui.groupBox_2)
        self._scale.setObjectName('baseGridScale')
        self._scale.setDecimals(8)
        self._scale.setRange(1e-12, 1e12)
        self._scale.setValue(1.0)
        self._scale.setToolTip(self.tr(
            'blockMeshDict convertToMeters. Multiplies every vertex, so the '
            'bounds above are read in these units.'))
        ui.formLayout.addRow(self.tr('Scale to metres'),
                             unit_cell(self._scale))

        # R167. The flush-block warning below said "grow the block" on a page
        # where the span was six read-only labels. This is the control it
        # names: the block the page derives from the geometry, pushed out on
        # all six faces so no surface is coplanar with one of them.
        self._standoff = CompactDoubleSpinBox(ui.groupBox_2)
        self._standoff.setObjectName('baseGridStandoff')
        self._standoff.setDecimals(3)
        self._standoff.setRange(0.0, 100.0)
        self._standoff.setSingleStep(0.05)
        self._standoff.setValue(0.0)
        self._standoff.setToolTip(self.tr(
            'How far the block stands off the geometry on every face, as a '
            'fraction of the geometry\'s largest span. 0 is the geometry\'s '
            'own bounding box. Ignored while a bounding Hex6 is chosen, '
            'because that block is yours.'))
        ui.formLayout.addRow(self.tr('Standoff of largest span'),
                             unit_cell(self._standoff, 'fraction'))

        self._grading = {}
        for axis in 'xyz':
            box = CompactDoubleSpinBox(ui.groupBox_2)
            box.setObjectName(f'baseGridGrading{axis.upper()}')
            box.setDecimals(4)
            box.setRange(1e-4, 1e4)
            box.setValue(1.0)
            box.setToolTip(self.tr(
                'simpleGrading ratio along this axis: the last cell divided by '
                'the first. 1 is a uniform block.'))
            self._grading[axis] = box
            ui.formLayout.addRow(
                self.tr('Grading {0}').format(axis.upper()),
                unit_cell(box, 'ratio'))

        self._boundaryTypes = {}
        # D3. Six near-identical rows -- ``xMin type`` .. ``zMax type``, all
        # reading ``patch`` -- took about a fifth of the panel to say one
        # thing. The same six controls fit in a three-by-two grid whose axis
        # and side are read off the headers instead of repeated in every label.
        faces = QWidget(ui.groupBox_2)
        grid = QGridLayout(faces)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(QLabel(self.tr('Min side'), faces), 0, 1)
        grid.addWidget(QLabel(self.tr('Max side'), faces), 0, 2)
        # The six face names come from the schema the page writes to, not from
        # the blockMesh writer: a page that reached into the OpenFOAM adapter
        # for them would be reading the dictionary format to draw a widget.
        names = list(schema['baseGrid']['boundaryTypes'])
        for name in names:
            combo = QComboBox(faces)
            combo.setObjectName(f'baseGridBoundaryType{name}')
            for member, label in (
                    (BoundaryPatchType.PATCH, self.tr('patch')),
                    (BoundaryPatchType.WALL, self.tr('wall')),
                    (BoundaryPatchType.SYMMETRY, self.tr('symmetry')),
                    (BoundaryPatchType.EMPTY, self.tr('empty'))):
                combo.addItem(label, member.value)
            combo.setToolTip(self.tr(
                'Type of the {0} face of the background block. Faces the '
                'geometry is snapped onto keep whatever snappyHexMesh gives '
                'them; this governs the block face that survives.').format(name))
            # R55. The six combos were drawn at roughly half the height
            # they need -- glyphs cut through the middle and unreadable --
            # because the single form row holding the grid handed it whatever
            # vertical space was left over. A combo that states its own
            # minimum cannot be squeezed below it.
            combo.setMinimumHeight(combo.sizeHint().height())
            self._boundaryTypes[name] = combo
            axis = name[0]
            row = 'xyz'.index(axis) + 1 if axis in 'xyz' else names.index(name) + 1
            grid.addWidget(combo, row, 2 if name.endswith('Max') else 1)
        for axis in 'xyz':
            grid.addWidget(QLabel(axis.upper(), faces), 'xyz'.index(axis) + 1, 0)
        # ... and the row has to ask for the height its grid needs, or the
        # form gives it the height of one line for four rows of controls (R55).
        faces.setMinimumHeight(grid.minimumSize().height())
        ui.formLayout.addRow(self.tr('Outer face types'), faces)

    def _buildOutcomeLine(self, ui):
        """A line under the buttons that says how the last generate went.

        F4. The only report of a successful generate was ``6,000 cells``
        appearing in the far top-right toolbar, three panels away from the
        button that had just been pressed; the panel that ran the stage said
        nothing at all, so there was no way to tell a finished run from one
        that had quietly refused.
        """
        self._outcome = QLabel(self._widget)
        self._outcome.setObjectName('baseGridOutcome')
        self._outcome.setWordWrap(True)
        self._outcome.setVisible(False)
        layout = getattr(ui, 'verticalLayout_5', None)
        if layout is None:
            self._outcome.setParent(None)
            self._outcome = None
            return
        # The page ends with a stretch; the line belongs under the buttons,
        # not pinned to the bottom of the panel.
        layout.insertWidget(max(layout.count() - 1, 0), self._outcome)

    def _reportOutcome(self, text: str, failed: bool = False) -> None:
        """Show, or clear, the one-line result of the last generate (F4)."""
        label = getattr(self, '_outcome', None)
        if label is None:
            return
        label.setText(text)
        label.setProperty('foammeshOutcome', 'failed' if failed else 'ok')
        label.setVisible(bool(text))

    @staticmethod
    def _stageLogPath(result):
        """Where the stage wrote its log, when the result carries one (F3)."""
        payload = getattr(result, 'payload', None) or {}
        path = (payload.get('job') or {}).get('log_path')
        return Path(path) if path else None

    def _echoStageLog(self, console, result) -> None:
        """Make sure the pane holds the run, not just its last word.

        F3. A 45-second blockMesh left the console holding the single word
        ``End``. Whatever the cause of a dropped stream -- a buffered utility,
        a launch profile that swallows the pipe -- the stage log on disk has
        the whole run, so read it back when the pane came up nearly empty, and
        name the file either way so the output is always reachable.
        """
        log_path = self._stageLogPath(result)
        if log_path is None:
            return
        try:
            lines = log_path.read_text(encoding='utf-8',
                                       errors='replace').splitlines()
        except OSError:
            console.append(self.tr('Log: {0}').format(log_path))
            return
        if len(lines) > self._streamedLines + 1:
            console.clear()
            for line in lines:
                console.append(line)
        console.append(self.tr('Log: {0}').format(log_path))

    def _reportGeneratedMesh(self) -> None:
        """Say in the panel what the generate produced (F4)."""
        manager = getattr(app.window, 'meshManager', None)
        count = 0
        if manager is not None:
            try:
                count = int(manager.getNumberOfDisplayedCells())
            except (AttributeError, TypeError, ValueError):
                count = 0
        if count:
            self._reportOutcome(
                self.tr('Base grid generated: {0:,} cells.').format(count))
        else:
            self._reportOutcome(self.tr('Base grid generated.'))

    @qasync.asyncSlot()
    async def _cancelGenerate(self):
        """Stop the running stage from the dialog that is reporting it (D5)."""
        await app.facadeClient.cancel_active_job()

    def isNextStepAvailable(self):
        return (app.facadeClient.case_root / 'constant' / 'polyMesh' / 'boundary').exists()
    #
    # def open(self):
    #     self.load()
    #     self._updatePage()

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        if not self._loaded:
            self.load()

        self._updatePage()
        self.updateWorkingStatus()
        self._showOutline()

    async def hide(self):
        # The outline belongs to this page, not to the case: leaving it drawn
        # over the castellation or layers page would put a box around a mesh
        # that no longer has anything to do with it.
        self._hideOutline()
        return await super().hide()

    def _showOutline(self):
        """Draw the domain this page sizes, and say how big one cell is.

        Every number on the page describes a box the user could not see. The
        commonest base-grid mistake is a domain that does not contain the
        geometry -- a Hex6 chosen by the wrong name, or an extent read before
        an import -- and it used to be invisible until snappy failed to find a
        cell for the location-in-mesh. Drawing the box makes it a look.
        """
        self._hideOutline()
        if app.window is None:
            return
        bounds = self.boundingBox()
        if bounds is None:
            return
        x1, x2, y1, y2, z1, z2 = bounds
        if not (x2 > x1 and y2 > y1 and z2 > z1):
            return

        actor = polyDataToFeatureActor(
            hexPolyData((x1, y1, z1), (x2, y2, z2)))
        actor.SetObjectName('baseGrid:outline')
        prop = actor.GetProperty()
        prop.SetRepresentationToWireframe()
        prop.SetLineWidth(2.0)
        prop.SetLighting(False)
        tokens = app.themeManager.tokens if app.themeManager is not None else None
        prop.SetColor(*rgb(tokens.value('accent.default')
                           if tokens is not None else '#3f8ae0'))
        # There is no Display Control row for the outline, so a click on it
        # must still resolve to whatever surface is behind it.
        actor.PickableOff()

        self._outlineActor = actor
        app.window.displayControl.addOverlay(actor)
        app.window.setScaleNote(self._cellSizeNote())

    def _hideOutline(self):
        if self._outlineActor is not None and app.window is not None:
            app.window.displayControl.removeOverlay(self._outlineActor)
            app.window.setScaleNote('')
        self._outlineActor = None

    def dropOutline(self):
        """Take the domain box and its base-cell note off the viewport.

        DP-139. `hide()` is the page's own way out, and it is deliberately
        not called when the Meshing Method branch moves between tasks --
        a blind save there would un-finish a passed stage. So the box and
        the note it is annotated with stayed drawn over Castellation, Snap,
        Layers and Export, and survived a case close into the next case,
        where the shipped 10/10/10 counts of the *new* case were divided
        into the *old* case's span. Every Gmsh frame of the ten-model sweep
        carried the same `base cell 411.1 x 405.9 x 516.1 mm` -- the last
        snappy case's domain -- on a route that has no base grid at all.

        This is the way out that takes the overlays down without writing
        anything: whoever moves the panel away calls it.
        """
        self._hideOutline()

    def _clear(self):
        """Forget the domain, so the next case is never sized against it.

        DP-139. `unload()` reaches here on a case close. The span lives on
        this page as `_xLen/_yLen/_zLen` and was kept, so `load()` for the
        next case -- which fills the count boxes from the new case before
        the span is re-measured -- could annotate one model's box with
        another model's dimensions.
        """
        self.dropOutline()
        self._bounds = None
        self._xLen = None
        self._yLen = None
        self._zLen = None
        self._boundingHex6 = None
        self._countsAreDefault = True

    def _cellSizeNote(self):
        """The base cell as one line, or nothing when it is not yet known."""
        try:
            counts = (int(self._ui.numCellsX.text()),
                      int(self._ui.numCellsY.text()),
                      int(self._ui.numCellsZ.text()))
        except (TypeError, ValueError):
            return ''
        lengths = (self._xLen, self._yLen, self._zLen)
        if None in lengths or min(counts) <= 0:
            return ''
        # DP-106. With a unit, off the same ladder the extent clause beside
        # it reads from, and one unit for all three numbers -- three numbers
        # in three units cannot be compared, which is the whole point.
        sizes = format_lengths(length / count
                               for length, count in zip(lengths, counts))
        if not sizes:
            return ''
        return self.tr('base cell {0}').format(sizes)

    async def save(self):
        # AF4: the page is a facade client. It submits one batch configuration
        # command through the shared facade instead of writing ProjectState
        # directly, so this edit shares the desktop case's dispatcher, revision
        # domains, history, and event stream with REST/CLI/agent edits.
        boundingHex6 = None
        if self._ui.useHex6.isChecked():
            boundingHex6, _ = self._getHex6ByName(self._ui.boundingHex6.currentText())
        patch = {
            'meshing.base_grid.bounding_hex6': boundingHex6,
            'meshing.base_grid.cells.x': self._ui.numCellsX.text(),
            'meshing.base_grid.cells.y': self._ui.numCellsY.text(),
            'meshing.base_grid.cells.z': self._ui.numCellsZ.text(),
            'meshing.base_grid.sizing_mode': self._sizingMode.currentData(),
            'meshing.base_grid.target_cell_size': self._storedTargetCellSize(),
            'meshing.base_grid.scale': self._scale.value(),
            'meshing.base_grid.standoff': self._standoff.value(),
        }
        for axis, box in self._grading.items():
            patch[f'meshing.base_grid.grading.{axis}'] = box.value()
        for name, combo in self._boundaryTypes.items():
            patch[f'meshing.base_grid.boundary_types.{_faceField(name)}'] = \
                combo.currentData()
        try:
            await app.facadeClient.apply_fields(patch)
            return True
        except FacadeError as error:
            message = error.details.get('error', str(error))
            QMessageBox.warning(self._widget, self.tr("Input error"), message)
            return False

    def _outputPath(self):
        return app.facadeClient.case_root / 'constant' / 'polyMesh'

    def _enableStep(self):
        self._widget.setEnabled(True)
        self._ui.baseGridReset.setEnabled(True)

    def _disableStep(self):
        self._widget.setEnabled(False)
        self._ui.baseGridReset.setEnabled(False)

    def _connectSignalsSlots(self):
        self._ui.useHex6.toggled.connect(self._useHex6Toggled)
        self._ui.boundingHex6.currentTextChanged.connect(self._boundingHex6Changed)

        self._ui.numCellsX.editingFinished.connect(self._updateCellX)
        self._ui.numCellsY.editingFinished.connect(self._updateCellY)
        self._ui.numCellsZ.editingFinished.connect(self._updateCellZ)
        self._ui.numCellsX.editingFinished.connect(self._countsEdited)
        self._ui.numCellsY.editingFinished.connect(self._countsEdited)
        self._ui.numCellsZ.editingFinished.connect(self._countsEdited)
        self._ui.numCellsX.editingFinished.connect(self._refreshSizingWarning)
        self._ui.numCellsY.editingFinished.connect(self._refreshSizingWarning)
        self._ui.numCellsZ.editingFinished.connect(self._refreshSizingWarning)
        self._sizingMode.currentIndexChanged.connect(self._applySizingMode)
        self._targetCellSize.valueChanged.connect(self._deriveTargetCounts)
        self._standoff.valueChanged.connect(self._standoffChanged)
        self._ui.generate.clicked.connect(self._generate)
        self._ui.baseGridReset.clicked.connect(self._reset)

    def _updateBoundingBox(self, x1, x2, y1, y2, z1, z2):
        self._bounds = tuple(float(v) for v in (x1, x2, y1, y2, z1, z2))
        self._xLen = x2 - x1
        self._yLen = y2 - y1
        self._zLen = z2 - z1

        self._ui.xMin.setText('{:.4g}'.format(x1))
        self._ui.xMax.setText('{:.4g}'.format(x2))
        self._ui.xLen.setText('{:.4g}'.format(self._xLen))
        self._ui.yMin.setText('{:.4g}'.format(y1))
        self._ui.yMax.setText('{:.4g}'.format(y2))
        self._ui.yLen.setText('{:.4g}'.format(self._yLen))
        self._ui.zMin.setText('{:.4g}'.format(z1))
        self._ui.zMax.setText('{:.4g}'.format(z2))
        self._ui.zLen.setText('{:.4g}'.format(self._zLen))

        if self._sizingMode.currentData() == BaseGridSizingMode.TARGET_SIZE.value:
            self._seedTargetCellSize()
            self._deriveTargetCounts()
        else:
            self._seedNearCubicCounts()

        self._updateCellX()
        self._updateCellY()
        self._updateCellZ()

        # R16. The estimate and the anisotropy warning were only ever
        # refreshed from an edit, so the untouched 10/10/10 default -- the
        # state most likely to be accepted as it stands, and 6:1 anisotropic
        # on the measured domain -- showed no warning at all until the user
        # changed something or left the page and came back.
        self._refreshSizingWarning()
        self._refreshStandoffWarning()

        # Choosing a different bounding Hex6, or deriving counts from a target
        # size, moves the box and the cell size the box is annotated with.
        if self._outlineActor is not None:
            self._showOutline()

    def boundingBox(self):
        """The domain this page meshes, as ``(x1, x2, y1, y2, z1, z2)`` floats.

        Computed the way the page computes it -- a chosen bounding Hex6, else
        the geometry's own extent -- rather than parsed back out of the
        labels: before the page has been shown those still read "TextLabel",
        which is what a run straight from the tree used to try to turn into
        a number.
        """
        if getattr(self, '_bounds', None) is None:
            self._refreshBounds()
        return getattr(self, '_bounds', None)

    def _refreshBounds(self):
        if getattr(self, '_boundingHex6', None) is None:
            try:
                self._boundingHex6 = app.facadeClient.field_value(
                    'meshing.base_grid.bounding_hex6')
            except Exception:  # noqa: BLE001 - no case yet means no Hex6
                self._boundingHex6 = None
        geometry = (self._getHex6ById(self._boundingHex6)
                    if self._boundingHex6 else None)
        if geometry:
            x1, y1, z1 = geometry.vector('point1')
            x2, y2, z2 = geometry.vector('point2')
        else:
            x1, x2, y1, y2, z1, z2 = self._standOff(
                app.window.geometryManager.getBounds().toTuple())
        self._updateBoundingBox(x1, x2, y1, y2, z1, z2)

    def _standOff(self, bounds):
        """Push the derived block off the geometry (R167).

        A Hex6 the user modelled is left alone -- that block is theirs. This
        is only the box the page derives when there is none, and the same
        margin goes on all six faces so a thin geometry gets a real standoff
        on its thin axis too, rather than a fraction of nothing.

        R174. Every path that derives the block comes through here. Three did
        not: reopening a case, showing the page again, and clearing the Hex6
        checkbox each re-read the geometry extent directly, so the standoff
        survived on screen as a number in a spin box while the block that was
        actually meshed went back to flush -- which is the one thing the
        setting exists to prevent, and the save that Generate asks for on an
        untitled case put the user on that path every first run.
        """
        widget = getattr(self, '_standoff', None)
        return stand_off_bounds(
            bounds, 0.0 if widget is None else widget.value())

    def _standoffChanged(self, _value=None):
        """Move the block, and everything the page says about it."""
        if self._ui.useHex6.isChecked():
            return
        try:
            self._refreshBounds()
        except (AttributeError, TypeError, ValueError):
            # No geometry loaded yet: the block has nothing to stand off.
            return

    async def runInBatchMode(self):
        """Generate the base grid for the wizard and the batch loop."""
        return await self._generate()

    def load(self):
        # AF4: read the page's fields from the facade by semantic ID.
        client = app.facadeClient
        self._boundingHex6 = client.field_value('meshing.base_grid.bounding_hex6')

        cells = tuple(client.field_value(f'meshing.base_grid.cells.{axis}')
                      for axis in 'xyz')
        self._ui.numCellsX.setText(str(cells[0]))
        self._ui.numCellsY.setText(str(cells[1]))
        self._ui.numCellsZ.setText(str(cells[2]))
        # R15. Only the shipped 10/10/10 is a placeholder; anything else was
        # chosen, either by the user or by an earlier near-cubic seed that was
        # saved with the case.
        self._countsAreDefault = all(
            str(value) == str(self.DEFAULT_COUNT) for value in cells)
        mode = client.field_value('meshing.base_grid.sizing_mode')
        self._sizingMode.setCurrentIndex(max(0, self._sizingMode.findData(mode)))
        stored = client.field_value('meshing.base_grid.target_cell_size')
        # DP-669: unset is "Auto", which the box shows on its floor.
        self._targetCellSize.setValue(0.0 if stored is None else float(stored))
        self._applySizingMode()

        self._scale.setValue(float(client.field_value('meshing.base_grid.scale')))
        self._standoff.setValue(
            float(client.field_value('meshing.base_grid.standoff') or 0.0))
        for axis, box in self._grading.items():
            box.setValue(float(
                client.field_value(f'meshing.base_grid.grading.{axis}')))
        for name, combo in self._boundaryTypes.items():
            stored = client.field_value(
                f'meshing.base_grid.boundary_types.{_faceField(name)}')
            combo.setCurrentIndex(max(0, combo.findData(stored)))

        self._loaded = True

    def _setTargetSizeRowVisible(self, visible: bool) -> None:
        """Show the target-size row only in the mode that reads it.

        D1. Three controls described one thing: the X/Y/Z counts, the sizing
        mode, and a target size that -- in the default "Direct cell counts"
        mode -- sat there greyed out showing a meaningless ``1.00000000``. A
        disabled control still asks to be read; a hidden one does not.
        """
        form = getattr(self, '_targetSizeRow', None)
        if form is None:
            return
        # DP-164. The row's field is the cell holding the box and its unit,
        # not the box, so that is what the layout is asked about.
        cell = self._targetSizeCell
        label = form.labelForField(cell)
        if hasattr(form, 'setRowVisible'):
            form.setRowVisible(cell, visible)
        else:                                   # Qt older than 6.4
            cell.setVisible(visible)
            if label is not None:
                label.setVisible(visible)

    def _applySizingMode(self):
        target = self._sizingMode.currentData() == BaseGridSizingMode.TARGET_SIZE.value
        self._targetCellSize.setEnabled(target)
        self._setTargetSizeRowVisible(target)
        for edit in (self._ui.numCellsX, self._ui.numCellsY, self._ui.numCellsZ):
            edit.setReadOnly(target)
        if target:
            self._seedTargetCellSize()
            self._deriveTargetCounts()
        else:
            self._refreshSizingWarning()

    def _deriveTargetCounts(self):
        if self._sizingMode.currentData() != BaseGridSizingMode.TARGET_SIZE.value:
            return
        if None in (self._xLen, self._yLen, self._zLen):
            return
        bounds = (0, self._xLen, 0, self._yLen, 0, self._zLen)
        size = self._effectiveTargetCellSize()
        if size is None:
            return
        counts = derive_background_counts(bounds, size)
        for edit, value in zip(
                (self._ui.numCellsX, self._ui.numCellsY, self._ui.numCellsZ), counts):
            edit.setText(str(value))
        self._refreshSizingWarning()
        self._updateCellX()
        self._updateCellY()
        self._updateCellZ()

    def _seedNearCubicCounts(self):
        """Spend the default cell budget on a near-cubic base cell (R15).

        The shipped default is 10 x 10 x 10 whatever the domain is, so a
        0.1 x 0.2999 x 0.6 m box got a base cell of 0.01 x 0.02999 x 0.06 --
        a 6:1 brick, which the page's own header printed. snappyHexMesh
        carries the base cell shape through every refinement level and
        nothing later in the workflow squares it up. The span is known here,
        so keep the cell count the default asked for and split it between the
        axes in proportion to their span instead.

        Only an untouched default is re-spent: once a count has been typed,
        or read back from a saved case, the user's numbers stand.
        """
        if not self._countsAreDefault:
            return
        lengths = (self._xLen, self._yLen, self._zLen)
        if None in lengths or min(lengths) <= 0:
            return
        size = (lengths[0] * lengths[1] * lengths[2]
                / self.DEFAULT_CELL_BUDGET) ** (1.0 / 3.0)
        counts = derive_background_counts(
            (0, lengths[0], 0, lengths[1], 0, lengths[2]), size)
        for edit, value in zip(
                (self._ui.numCellsX, self._ui.numCellsY, self._ui.numCellsZ),
                counts):
            edit.setText(str(value))

    def _countsEdited(self):
        """A typed count belongs to the user and is never re-seeded (R15)."""
        self._countsAreDefault = False

    def _impliedCellSize(self):
        """The base cell the counts on screen describe, as one number (R133)."""
        lengths = (self._xLen, self._yLen, self._zLen)
        if None in lengths or min(lengths) <= 0:
            return None
        try:
            counts = (int(self._ui.numCellsX.text()),
                      int(self._ui.numCellsY.text()),
                      int(self._ui.numCellsZ.text()))
        except (TypeError, ValueError):
            return None
        if min(counts) <= 0:
            return None
        sizes = [length / count for length, count in zip(lengths, counts)]
        return (sizes[0] * sizes[1] * sizes[2]) ** (1.0 / 3.0)

    def _seedTargetCellSize(self):
        """Arrive in Target cell size mode holding a usable size (R133).

        The schema default is 1 m. On a 0.12 x 0.12 x 0.4 m model, choosing
        the mode replaced a 0.012 m grid with 2 x 2 x 2 = 8 background cells
        and the page's own "strongly anisotropic" warning -- a number wrong by
        two orders of magnitude for the geometry on screen, while the mode
        just abandoned held a sensible one. A size that cannot fit two cells
        along the shortest axis is not a size for this domain, whatever it was
        stored as, so start from the cell the counts already describe.
        """
        lengths = (self._xLen, self._yLen, self._zLen)
        if None in lengths or min(lengths) <= 0:
            return
        current = self._targetCellSize.value()
        if current == 0.0:
            return  # DP-669: "Auto" is derived from this block, so it fits
        if 0 < current <= min(lengths) / 2.0:
            return
        implied = self._impliedCellSize()
        if implied is None:
            implied = (lengths[0] * lengths[1] * lengths[2]
                       / self.DEFAULT_CELL_BUDGET) ** (1.0 / 3.0)
        self._targetCellSize.setValue(implied)

    def _storedTargetCellSize(self):
        """The typed target size, or ``None`` for "Auto" (DP-669)."""
        value = self._targetCellSize.value()
        return None if value <= 0.0 else value

    def _effectiveTargetCellSize(self):
        """The size the counts are derived from: typed, or Auto's number.

        DP-669. "Auto" is the block's diagonal / 40, the same number the
        dictionary writer derives, and the box's tooltip says what it came to.
        """
        typed = self._storedTargetCellSize()
        if typed is not None:
            self._targetCellSize.setToolTip('')
            return typed
        from foammesh.core.mesh.sizing import auto_target_cell_size

        size = auto_target_cell_size(
            (0, self._xLen, 0, self._yLen, 0, self._zLen))
        if size is not None:
            self._targetCellSize.setToolTip(self.tr(
                'Auto: {0:.4g} m (background block diagonal / 40). Type a '
                'size to override it.').format(size))
        return size

    def _flushFaces(self):
        """Which faces of the block coincide with the geometry extent (R28)."""
        bounds = getattr(self, '_bounds', None)
        if bounds is None:
            return []
        try:
            geometry = app.window.geometryManager.getBounds().toTuple()
        except Exception:                                    # noqa: BLE001
            return []
        if geometry is None or len(tuple(geometry)) != 6:
            return []
        spans = (bounds[1] - bounds[0], bounds[3] - bounds[2],
                 bounds[5] - bounds[4])
        if min(spans) <= 0:
            return []
        flush = []
        for index, name in enumerate(
                ('xMin', 'xMax', 'yMin', 'yMax', 'zMin', 'zMax')):
            span = spans[index // 2]
            if abs(float(bounds[index])
                   - float(geometry[index])) <= 1e-6 * span:
                flush.append(name)
        return flush

    def _refreshStandoffWarning(self):
        """Say when the background block is flush with the geometry (R28).

        The default block is the geometry's own bounding box, so the inlet and
        outlet caps end up coplanar with the block faces. Measured on the
        delivered mesh, snappy then split the top outlet between the named
        patch (55 faces) and the leftover background patch ``zMax`` (340) --
        86 per cent of the outlet on a patch nobody named -- and the workflow
        reported the run as a clean pass. Nothing said the block has to stand
        off the geometry, so say it here, on the page that sizes the block.
        """
        label = getattr(self, '_standoffWarning', None)
        if label is None:
            return
        flush = self._flushFaces()
        if not flush:
            label.setText('')
            label.setVisible(False)
            return
        label.setText(self.tr(
            'Warning: the background block is flush with the geometry on '
            '{0}. Any surface touching a block face is split between the '
            'patch you named and the leftover background patch. Raise '
            'Standoff above, or pick a bounding Hex6 that stands off the '
            'geometry.'
        ).format(', '.join(flush)))
        label.setVisible(True)

    def _refreshSizingWarning(self):
        """Cell-count estimate and anisotropy warning for derived *and* entered counts."""
        if None in (self._xLen, self._yLen, self._zLen):
            return
        try:
            counts = (int(self._ui.numCellsX.text()), int(self._ui.numCellsY.text()),
                      int(self._ui.numCellsZ.text()))
        except (TypeError, ValueError):
            return
        if min(counts) <= 0:
            return
        bounds = (0, self._xLen, 0, self._yLen, 0, self._zLen)
        estimate = background_estimate(bounds, counts)
        warning = self.tr('Estimated background cells: {0:,}').format(estimate['cell_count'])
        if estimate['anisotropic']:
            warning += self.tr(' · Warning: background cells are strongly anisotropic.')
        self._sizingWarning.setText(warning)
        # Typing a count, or deriving one from a target size, changes the cell
        # the readout names while the box itself stays where it is.
        if self._outlineActor is not None and app.window is not None:
            app.window.setScaleNote(self._cellSizeNote())

    def _useHex6Toggled(self, checked):
        if checked:
            name = self._ui.boundingHex6.currentText()
            gId, geometry = self._getHex6ByName(name)
            if geometry is None:
                QMessageBox.warning(
                    self._widget, self.tr('Hex6 not found'),
                    self.tr('There is no Hex6 named {0}.').format(name))
                return
            self._boundingHex6 = gId
            x1, y1, z1 = geometry.vector('point1')
            x2, y2, z2 = geometry.vector('point2')
        else:
            self._boundingHex6 = None
            x1, x2, y1, y2, z1, z2 = self._standOff(
                app.window.geometryManager.getBounds().toTuple())

        self._updateBoundingBox(x1, x2, y1, y2, z1, z2)

    def _boundingHex6Changed(self, name):
        if not self._ui.useHex6.isChecked():
            return

        gId, geometry = self._getHex6ByName(name)
        if geometry is None:
            QMessageBox.warning(
                self._widget, self.tr('Hex6 not found'),
                self.tr('There is no Hex6 named {0}.').format(name))
            return

        self._boundingHex6 = gId

        x1, y1, z1 = geometry.vector('point1')
        x2, y2, z2 = geometry.vector('point2')

        self._updateBoundingBox(x1, x2, y1, y2, z1, z2)

    def _updateCellX(self):
        count = int(self._ui.numCellsX.text())
        if count <= 0 or self._xLen is None:
            return
        self._ui.xCell.setText('{:.4g}'.format(self._xLen / count))

    def _updateCellY(self):
        count = int(self._ui.numCellsY.text())
        if count <= 0 or self._yLen is None:
            return
        self._ui.yCell.setText('{:.4g}'.format(self._yLen / count))

    def _updateCellZ(self):
        count = int(self._ui.numCellsZ.text())
        if count <= 0 or self._zLen is None:
            return
        self._ui.zCell.setText('{:.4g}'.format(self._zLen / count))

    def _validate(self) -> (bool, str):
        if int(self._ui.numCellsX.text()) < 2 \
                or int(self._ui.numCellsY.text()) < 2 \
                or int(self._ui.numCellsZ.text()) < 2:
            return False, field_complaint.sentence(
                self.tr('Number of cells per direction'),
                field_complaint.range_clause(low=2))

        return True, ''

    @qasync.asyncSlot()
    async def _generate(self):
        valid, msg = self._validate()
        if not valid:
            await AsyncMessageBox().warning(
                self._widget, self.tr('Input error'), msg)
            return False

        # R18. The page's edits reached the facade *after* the Save-case
        # dialog, and saving an untitled case reloads the page: counts typed
        # as 10/30/60 came back as 10/10/10, nothing said the settings had
        # been dropped, and the run that followed used numbers the user had
        # not chosen. Committing the widgets first means the save writes the
        # edits out instead of over them.
        if not await self.save():
            return False

        # H11. An untitled case chooses its home before a stage runs, not
        # after the mesh exists in a folder that gets swept.
        if not await self.requireSavedCase():
            return False

        # D5. One run had two progress surfaces -- this modal and the status
        # bar -- and the Cancel was on the one that was off screen (C3). The
        # modal is the surface the user is looking at, so the Cancel goes on
        # it; `autoCloseOnCancel` stays off so the dialog survives long enough
        # to report what cancelling did.
        progressDialog = ProgressDialog(
            self._widget, self.tr('Base grid generating'),
            cancelable=True, autoCloseOnCancel=False)
        progressDialog.setLabelText(self.tr('Generating the base grid…'))
        progressDialog.cancelClicked.connect(self._cancelGenerate)
        progressDialog.open()

        console = app.consoleView
        console.clear()
        self._streamedLines = 0
        self._reportOutcome('')

        def onLine(line):
            self._streamedLines += 1
            console.append(line)

        # DP-576. The raw geometry extent: the case builder applies the saved
        # bounding Hex6 and standoff itself, for every frontend alike, so
        # handing over this page's already stood-off box would apply the
        # standoff twice.
        bounds = app.window.geometryManager.getBounds().toTuple()
        try:
            await app.facadeClient.run(
                'workflow.generate_dictionaries', {'bbox': list(bounds)})
            # F3. The other stage pages hand the console straight to the
            # operation as `on_line`; this one subscribed to `JOB_OUTPUT`
            # instead and ended a 45-second blockMesh with the word `End` and
            # nothing before it. Use the route that is known to stream.
            result = await app.facadeClient.run(
                'workflow.run_stage',
                {'stage': 'blockMesh', 'on_line': onLine})
        except (FacadeError, OSError, TypeError, ValueError) as error:
            # D6. This raised two stacked dialogs for one condition: a warning
            # naming the cause, and behind it a progress dialog reading `Mesh
            # Generation Failed.` -- which, once the first was dismissed, was
            # the only one left and no longer said why. Close the progress
            # surface, then say it once.
            #
            # R170. It caught FacadeError alone, and the case builder's own
            # refusal is a ValueError: the modal outlived the coroutine that
            # opened it, animating over an empty console, and Cancel greyed
            # itself out without closing because there was no longer a job to
            # cancel. Whatever ends the run, the dialog it opened closes and
            # the reason is said out loud.
            progressDialog.close()
            self._reportOutcome(
                self.tr('Generation failed: {0}').format(error), failed=True)
            await AsyncMessageBox().warning(
                self._widget, self.tr('Base grid not generated'), str(error))
            self.clearResult()
            return False

        self._echoStageLog(console, result)

        if result.status != 'accepted':
            progressDialog.close()
            reason = (self.tr('The run was cancelled.')
                      if progressDialog.isCanceled()
                      else self.tr('blockMesh did not finish. The console pane '
                                   'holds its output.'))
            self._reportOutcome(
                self.tr('Generation failed. {0}').format(reason), failed=True)
            await AsyncMessageBox().warning(
                self._widget, self.tr('Base grid not generated'), reason)
            self.clearResult()
            return False

        await app.facadeClient.run('case.parallel.redistribute', {
            'on_progress': progressDialog.setLabelText})

        # Plan 28 WP6 (D23). The base grid used to run a full mesh check here,
        # every time, on a mesh three mutating stages away from the one anyone
        # judges. On a large case that is minutes nobody asked for, and its
        # verdict was about to be invalidated by castellation. The QA row owns
        # the check, and runs it on the mesh that ships.
        progressDialog.close()

        await app.window.meshManager.load(self.OUTPUT_TIME,
                                          stage=self.tr('Base grid'))
        self._updatePage()
        self._reportGeneratedMesh()

        if self.isNextStepAvailable():
            self.stepCompleted.emit()
        return True

    @qasync.asyncSlot()
    async def _reset(self):
        """Throw the background grid away so it can be built again (R85).

        What this button used to do was clear the numbered time directory that
        the snappy path never writes, and emit a signal the current shell does
        not listen to. The mesh vanished from the viewport and nothing else
        changed: the task kept its checkmark and the button kept saying Reset,
        so once blockMesh had run there was no way back to Generate -- editing
        the cell counts or the grid span had nowhere to go.

        The grid is what every later stage is built on, so discarding it
        discards them too. Say so before doing it.
        """
        confirm = await AsyncMessageBox().question(
            self._widget, self.tr('Reset base grid'),
            self.tr('Delete the background grid and every mesh stage built on '
                    'it, so the grid can be generated again? '
                    'Castellation, snapping and boundary layers will have to '
                    'be re-run.'))
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            await app.facadeClient.run(
                'workflow.reset_stage', {'stage': 'blockMesh'})
            # The tree keeps its own record of what has passed. Without this
            # the row would still read as a pass over a case that no longer
            # holds a mesh.
            await app.facadeClient.run('mesh.workflow.task_transition', {
                'engine_id': 'snappy', 'task_id': 'snappy.base_grid',
                'transition': 'configure'})
        except FacadeError as error:
            await AsyncMessageBox().warning(
                self._widget, self.tr('Reset failed'), str(error))
            return

        self._showPreviousMesh()
        self.clearResult()
        self._updatePage()
        self.stepReset.emit()
        app.window.meshManager.unload()

    def _updatePage(self):
        self._ui.useHex6.toggled.disconnect(self._useHex6Toggled)
        self._ui.boundingHex6.currentTextChanged.disconnect(self._boundingHex6Changed)

        self._ui.boundingHex6.clear()
        if hex6List := self._getHex6List():
            self._ui.boundingHex6.addItems(hex6List)
            self._ui.boundingHex6.setCurrentIndex(0)
            self._ui.useHex6.setEnabled(True)
        else:
            self._ui.useHex6.setEnabled(False)

        if geometry := self._getHex6ById(self._boundingHex6):
            self._ui.useHex6.setChecked(True)
            self._ui.boundingHex6.setCurrentText(geometry.value('name'))
            x1, y1, z1 = geometry.vector('point1')
            x2, y2, z2 = geometry.vector('point2')
        else:
            self._boundingHex6 = None
            self._ui.useHex6.setChecked(False)
            x1, x2, y1, y2, z1, z2 = self._standOff(
                app.window.geometryManager.getBounds().toTuple())

        self._updateBoundingBox(x1, x2, y1, y2, z1, z2)

        self._ui.useHex6.toggled.connect(self._useHex6Toggled)
        self._ui.boundingHex6.currentTextChanged.connect(self._boundingHex6Changed)

        if self.isNextStepAvailable():
            self._ui.generate.hide()
            self._ui.baseGridReset.show()
            self._ui.baseGridReset.setEnabled(not self._locked)
        else:
            self._ui.generate.show()
            self._ui.baseGridReset.hide()
            # R54. This line is the page's only report of whether the base
            # grid step has been done, and it kept the previous case's
            # sentence -- "Base grid generated: 21,600 cells." -- on a case
            # where Generate had never been pressed, next to its own estimate
            # of 1,000 and a viewport header reading 0 cells.
            self._reportOutcome('')

    def _showPreviousMesh(self):
        app.window.meshManager.unload()

    def _getHex6ByName(self, name):
        gId, geometry = app.facadeClient.checkout().findElement(
            'geometry', lambda i, e: e['name'] == name)
        if self._isHex6(gId, geometry):
            return gId, geometry

        return None, None

    def _getHex6ById(self, gId):
        if gId is None:
            return None

        geometry = app.facadeClient.checkout().getElement('geometry', gId)
        if self._isHex6(gId, geometry):
            return geometry

        return None

    def _getHex6List(self):
        names = []
        for gId, geometry in app.facadeClient.checkout().getElements(
                'geometry',
                lambda i, e: e['gType'] == GeometryType.VOLUME.value and e['shape'] == Shape.HEX6.value).items():
            if self._isHex6(gId, geometry):
                names.append(geometry.value('name'))

        return sorted(names)

    def _isHex6(self, gId, geometry):
        if geometry is None:
            return False

        if (geometry.value('gType') != GeometryType.VOLUME.value
                or geometry.value('shape') != Shape.HEX6.value):
            return False

        if app.facadeClient.checkout().getKeys(
                'geometry', lambda i, e:
                e['volume'] == gId and e['cfdType'] != CFDType.BOUNDARY.value):
            return False

        return True
