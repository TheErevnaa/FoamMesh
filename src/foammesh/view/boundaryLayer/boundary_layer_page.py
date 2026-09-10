#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtWidgets import (
    QComboBox, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton)

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.view.widgets.commit_guard import CONFLICT_ERRORS, conflict_message

from widgets.async_message_box import AsyncMessageBox
from widgets.enum_combo_box import EnumComboBox
from widgets.list_table import ListItemWithButtons

from foammesh.app import app
from foammesh.db.configurations import defaultsDB
from foammesh.db.configurations_schema import (
    LayerPolicy, MeshShrinker, OptionalToggle)
from foammesh.view.step_page import StepPage
from foammesh.view.main_window.inpage_editor import open_in_page
from foammesh.core.mesh.sizing import first_layer_height
from .boundary_setting_dialog import BoundarySettingDialog


def layerCountLabel(policy, count) -> str:
    """What the group list shows in its Layers column.

    A frozen group and an inherited one both add no layers, and showing the
    bare count for either says the same thing about two different requests.
    """
    policy = getattr(policy, 'value', policy) or LayerPolicy.GROW.value
    if policy == LayerPolicy.FREEZE.value:
        return '0 (frozen)'
    if policy == LayerPolicy.INHERIT.value:
        return 'inherited'
    return str(count)


#: Running castellation, snapping and layers as separate snappyHexMesh
#: invocations is useful for iterating on settings, but each invocation
#: restarts from the mesh on disk and cannot apply the quality control a single
#: combined run does. Measured on the reference curved pipe, the staged result
#: reported concave cells where the batch pipeline reported "Mesh OK".
STAGED_MESH_ADVISORY = (
    'This mesh was built stage by stage, which is intended for tuning '
    'settings. For the mesh you hand to a solver, finish with the full '
    'pipeline on the Base Grid step so every phase runs in one qualified '
    'snappyHexMesh pass.')


class BoundaryLayerPage(StepPage):
    OUTPUT_TIME = 3

    def __init__(self, ui):
        super().__init__(ui, ui.boundaryLayerPage)

        self._ui = ui
        self._dialog = None
        self._db = None
        self._cm = None
        self._layerWarnings = []

        ui.boundaryLayerConfigurationsHeader.setContents(ui.boundaryLayerConfigurations)
        ui.boundaryLayerConfigurations.setBackgroundColor()
        ui.boundaryLayerConfigurations.setHeaderWithWidth([0, 0, 16, 16])

        ui.boundaryLayerAdvancedConfigurationHeader.setContents(ui.boundaryLayerAdvancedConfiguration)

        shrinker_parent = getattr(ui, 'groupBox_7', ui.boundaryLayerPage)
        self._meshShrinker = QComboBox(shrinker_parent)
        self._meshShrinker.setObjectName('meshShrinker')
        self._meshShrinker.setAccessibleName(self.tr('Layer mesh shrinker'))
        self._meshShrinker.addItem(
            self.tr('Medial-axis shrinker'), MeshShrinker.MEDIAL_AXIS)
        shrinker_form = getattr(ui, 'formLayout_11', None)
        if shrinker_form is not None:
            shrinker_form.addRow(
                QLabel(self.tr('Mesh shrinker'), shrinker_parent),
                self._meshShrinker)
        else:
            ui.verticalLayout_18.insertWidget(
                max(0, ui.verticalLayout_18.count() - 1),
                self._meshShrinker)

        # v13's medial-axis mover decides where layers may slip along a wall
        # from ``slipFeatureAngle``; every Foundation tutorial sets it, so it is
        # a first-class control rather than an implicit featureAngle/2 default.
        self._slipFeatureAngle = QLineEdit(shrinker_parent)
        self._slipFeatureAngle.setObjectName('slipFeatureAngle')
        self._slipFeatureAngle.setAccessibleName(
            self.tr('Layer slip feature angle'))
        if shrinker_form is not None:
            shrinker_form.addRow(
                QLabel(self.tr('Slip feature angle'), shrinker_parent),
                self._slipFeatureAngle)
        else:
            ui.verticalLayout_18.insertWidget(
                max(0, ui.verticalLayout_18.count() - 1),
                self._slipFeatureAngle)

        self._buildMedialAxisControls(shrinker_form, shrinker_parent)

        ui.boundaryLayerCancel.hide()

        self._yPlusHelper = QPushButton(self.tr('Calculate first-layer height from y+…'),
                                       ui.boundaryLayerPage)
        self._yPlusHelper.setAccessibleName(self.tr('Y plus first-layer height calculator'))
        ui.verticalLayout_18.insertWidget(
            max(0, ui.verticalLayout_18.count() - 1), self._yPlusHelper)
        self._yPlusHelper.clicked.connect(self._showYPlusHelper)

        self._connectSignalsSlots()

    # Plan 29 WP7.3. Four addLayersControls keys Foundation 13 reads that the
    # page never offered. The two iteration counts bound work the medial-axis
    # mover would otherwise do to convergence; the two switches ask it to drop
    # islands it cannot extrude and to say what it did. All four are optional:
    # left alone they stay out of the dictionary, so a case tuned before they
    # existed still meshes the way it did.
    def _buildMedialAxisControls(self, form, parent):
        self._nMedialAxisIter = QLineEdit(parent)
        self._nMedialAxisIter.setObjectName('nMedialAxisIter')
        self._nMedialAxisIter.setPlaceholderText(self.tr('OpenFOAM default'))
        self._nMedialAxisIter.setToolTip(self.tr(
            'Cap on medial-axis smoothing iterations. Empty means run to '
            'convergence, which is what snappyHexMesh does on its own.'))

        self._nSmoothDisplacement = QLineEdit(parent)
        self._nSmoothDisplacement.setObjectName('nSmoothDisplacement')
        self._nSmoothDisplacement.setPlaceholderText(self.tr('OpenFOAM default'))
        self._nSmoothDisplacement.setToolTip(self.tr(
            'Smoothing sweeps applied to the computed layer displacement '
            'before the mesh is moved.'))

        self._detectExtrusionIsland = self._layerToggle(parent, self.tr(
            'Drop patches of layer that are extruded on their own, cut off '
            'from the surrounding layer.'))
        self._additionalReporting = self._layerToggle(parent, self.tr(
            'Write extra per-patch layer diagnostics to the log.'))

        rows = ((self.tr('Max. medial axis iter.'), self._nMedialAxisIter),
                (self.tr('Smooth displacement'), self._nSmoothDisplacement),
                (self.tr('Detect extrusion islands'), self._detectExtrusionIsland),
                (self.tr('Additional reporting'), self._additionalReporting))
        for title, widget in rows:
            if form is not None:
                form.addRow(QLabel(title, parent), widget)
            else:
                self._ui.verticalLayout_18.insertWidget(
                    max(0, self._ui.verticalLayout_18.count() - 1), widget)

    def _layerToggle(self, parent, tip):
        combo = EnumComboBox(parent)
        combo.addEnumItems({
            OptionalToggle.DEFAULT: self.tr('OpenFOAM default'),
            OptionalToggle.ON: self.tr('On'),
            OptionalToggle.OFF: self.tr('Off'),
        })
        combo.setToolTip(tip)
        return combo

    def _showYPlusHelper(self):
        prompts = (
            (self.tr('Target y+'), 1.0), (self.tr('Velocity (m/s)'), 10.0),
            (self.tr('Density (kg/mÂ³)'), 1.225),
            (self.tr('Dynamic viscosity (PaÂ·s)'), 1.81e-5),
            (self.tr('Reference length (m)'), 1.0))
        values = []
        for label, default in prompts:
            value, accepted = QInputDialog.getDouble(
                self._widget, self.tr('y+ helper'), label, default,
                1e-12, 1e12, 8)
            if not accepted:
                return
            values.append(value)
        height = first_layer_height(*values)
        QMessageBox.information(
            self._widget, self.tr('Estimated first-layer height'),
            self.tr('First-layer height: {0:.6g} m\n'
                    'This is an engineering estimate; validate it after meshing.').format(height))

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        self.load()
        self.updateWorkingStatus()

        self._ui.boundaryLayerApply.setEnabled(isWorkingStep and not batchRunning)

    async def save(self):
        try:
            # WP-13 / F-33. Scoped to `addLayers`: `self._db` is a whole-database
            # copy taken at load() and kept for the geometry reads the list needs,
            # and committing *that* copy made this page's save collide with every
            # unrelated edit made since the page was shown. The write is one
            # subtree, so it is taken and committed as one subtree.
            addLayer = app.facadeClient.checkout('addLayers')

            addLayer.setValue('nGrow', self._ui.nGrow.text(), self.tr('Number of Grow'))
            addLayer.setValue('featureAngle', self._ui.featureAngleThreshold.text(), self.tr('Feature Angle Threshold'))
            addLayer.setValue('slipFeatureAngle', self._slipFeatureAngle.text(),
                              self.tr('Slip Feature Angle'))
            addLayer.setValue('maxFaceThicknessRatio', self._ui.maxFaceThicknessRatio.text(),
                              self.tr('Max. Thickness Ratio'))
            addLayer.setValue('nSmoothSurfaceNormals', self._ui.nSmoothSurfaceNormals.text(),
                              self.tr('Number of Iterations'))
            addLayer.setValue('nSmoothThickness', self._ui.nSmoothThickness.text(), self.tr('Smooth Layer Thickness'))
            addLayer.setValue('minMedialAxisAngle', self._ui.minMedialAxisAngle.text(), self.tr('Min. Axis Angle'))
            addLayer.setValue('maxThicknessToMedialRatio', self._ui.maxThicknessToMedialRatio.text(),
                              self.tr('Max. Thickness Ratio'))
            addLayer.setValue('nSmoothNormals', self._ui.nSmoothNormals.text(), self.tr('Number of Smoothing Iter.'))
            addLayer.setValue('nRelaxIter', self._ui.nRelaxIter.text(), self.tr('Max. Snapping Relaxation Iter.'))
            addLayer.setValue('nBufferCellsNoExtrude', self._ui.nBufferCellsNoExtrude.text(),
                              self.tr('Num. of Buffer Cells'))
            addLayer.setValue('nLayerIter', self._ui.nLayerIter.text(), self.tr('Max. Layer Addition Iter.'))
            addLayer.setValue('nRelaxedIter', self._ui.nRelaxedIter.text(), self.tr('Max. Iter. Before Relax'))
            addLayer.setValue(
                'meshShrinker', self._meshShrinker.currentData(),
                self.tr('Layer Mesh Shrinker'))
            # An empty box is "no opinion": the key is left out entirely so
            # snappyHexMesh's own default still governs.
            addLayer.setValue(
                'nMedialAxisIter', self._nMedialAxisIter.text().strip() or None,
                self.tr('Max. Medial Axis Iter.'))
            addLayer.setValue(
                'nSmoothDisplacement',
                self._nSmoothDisplacement.text().strip() or None,
                self.tr('Smooth Displacement'))
            addLayer.setValue('detectExtrusionIsland',
                              self._detectExtrusionIsland.currentData())
            addLayer.setValue('additionalReporting',
                              self._additionalReporting.currentData())

            await app.facadeClient.commit_working_copy(addLayer, action='update boundary layers')
            self._db = app.facadeClient.checkout()

            return True
        except CONFLICT_ERRORS as error:
            await AsyncMessageBox().information(
                self._widget, self.tr('Case Changed'), self.tr(conflict_message(error)))

            return False
        except ValidationError as e:
            await AsyncMessageBox().information(self._widget, self.tr("Input Error"), e.toMessage())

            return False

    def load(self):
        self._db = app.facadeClient.checkout()

        if self._loaded:
            return

        self._ui.boundaryLayerConfigurations.clear()

        groups = set()
        for gId, geometry in self._db.getElements('geometry').items():
            groups.add(geometry.value('layerGroup'))
            groups.add(geometry.value('slaveLayerGroup'))
        if None in groups:
            groups.remove(None)

        for groupId, element in self._db.getElements('addLayers/layers').items():
            if groupId in groups:
                self._addConfigurationItem(
                    groupId, element.value('groupName'),
                    layerCountLabel(element.value('layerPolicy'),
                                    element.value('nSurfaceLayers')))
            else:
                self._db.removeElement('addLayers/layers', groupId)

        self._setConfigurastions(self._db.getElement('addLayers'))

        self._loaded = True

    async def runInBatchMode(self):
        if not await self.save():
            return False

        self._ui.boundaryLayerApply.setEnabled(False)

        return await self._run()

    def _connectSignalsSlots(self):
        self._ui.loadBoundaryLayerDefaults.clicked.connect(self._loadDefaults)
        self._ui.boundaryLayerConfigurationsAdd.clicked.connect(lambda: self._openLayerEditDialog())
        self._ui.boundaryLayerApply.clicked.connect(self._apply)
        self._ui.boundaryLayerCancel.clicked.connect(self._cancel)
        self._ui.boundaryLayerReset.clicked.connect(self._reset)

    @qasync.asyncSlot()
    async def _loadDefaults(self):
        if await AsyncMessageBox().confirm(
                self._widget, self.tr('Reset Settings'),
                self.tr('Would you like to reset all Boundary Layer settings to default,excluding the Layer Groups?')):
            self._setConfigurastions(defaultsDB.getElement('addLayers'))

    def _setConfigurastions(self, addLayer):
        self._ui.nGrow.setText(addLayer.value('nGrow'))
        self._ui.featureAngleThreshold.setText(addLayer.value('featureAngle'))
        self._slipFeatureAngle.setText(addLayer.value('slipFeatureAngle'))
        self._ui.maxFaceThicknessRatio.setText(addLayer.value('maxFaceThicknessRatio'))
        self._ui.nSmoothSurfaceNormals.setText(addLayer.value('nSmoothSurfaceNormals'))
        self._ui.nSmoothThickness.setText(addLayer.value('nSmoothThickness'))
        self._ui.minMedialAxisAngle.setText(
            addLayer.value('minMedialAxisAngle'))
        self._ui.maxThicknessToMedialRatio.setText(addLayer.value('maxThicknessToMedialRatio'))
        self._ui.nSmoothNormals.setText(addLayer.value('nSmoothNormals'))
        self._ui.nRelaxIter.setText(addLayer.value('nRelaxIter'))
        self._ui.nBufferCellsNoExtrude.setText(addLayer.value('nBufferCellsNoExtrude'))
        self._ui.nLayerIter.setText(addLayer.value('nLayerIter'))
        self._ui.nRelaxedIter.setText(addLayer.value('nRelaxedIter'))
        try:
            shrinker = MeshShrinker(addLayer.value('meshShrinker'))
        except KeyError:
            shrinker = MeshShrinker.MEDIAL_AXIS
        index = self._meshShrinker.findData(shrinker)
        self._meshShrinker.setCurrentIndex(max(0, index))
        self._nMedialAxisIter.setText(addLayer.value('nMedialAxisIter') or '')
        self._nSmoothDisplacement.setText(
            addLayer.value('nSmoothDisplacement') or '')
        self._detectExtrusionIsland.setCurrentData(
            addLayer.enum('detectExtrusionIsland'))
        self._additionalReporting.setCurrentData(
            addLayer.enum('additionalReporting'))

    def _openLayerEditDialog(self, groupId=None):
        self._dialog = BoundarySettingDialog(self._widget, self._db, groupId)
        if self._locked:
            self._dialog.disableEdit()
        else:
            self._dialog.accepted.connect(self._updateLayerConfiguration)
        if not open_in_page(self._dialog, self._widget,
                            self._ui.boundaryLayerButtons):
            self._dialog.open()

    @qasync.asyncSlot()
    async def _apply(self):
        if not await self.save():
            return

        self._ui.boundaryLayerApply.hide()
        self._ui.boundaryLayerCancel.show()
        app.consoleView.clear()

        if await self._run():
            self.stepCompleted.emit()

            # snappyHexMesh exits successfully even when it rejected every
            # layer, so report what the mesh actually received.
            warnings = getattr(self, '_layerWarnings', [])
            message = self.tr('Boundary layers are applied.')
            if warnings:
                message = (
                    self.tr('Boundary layers were applied, but not every '
                            'requested layer could be added:')
                    + '\n\n- ' + '\n- '.join(warnings))
            await AsyncMessageBox().information(
                self._widget, self.tr('Complete'),
                message + '\n\n' + self.tr(STAGED_MESH_ADVISORY))

        self._enableEdit()
        self._ui.boundaryLayerCancel.hide()

        self.updateWorkingStatus()

    def _reset(self):
        self._showPreviousMesh()
        self.clearResult()
        self._updateControlButtons()
        self._ui.boundaryLayerApply.setEnabled(True)
        self.stepReset.emit()

    def _updateLayerConfiguration(self):
        element = self._dialog.dbElement()
        label = layerCountLabel(element.getValue('layerPolicy'),
                                element.getValue('nSurfaceLayers'))
        if self._dialog.isCreationMode():
            self._addConfigurationItem(self._dialog.groupId(),
                                       element.getValue('groupName'), label)
        else:
            self._ui.boundaryLayerConfigurations.item(self._dialog.groupId()).update(
                [element.getValue('groupName'), label])

    def _addConfigurationItem(self, groupId, name, layers):
        item = ListItemWithButtons(groupId, [name, layers])
        item.editClicked.connect(lambda: self._openLayerEditDialog(groupId))
        item.removeClicked.connect(lambda: self._removeLayerConfiguration(groupId))
        self._ui.boundaryLayerConfigurations.addItem(item)

    def _removeLayerConfiguration(self, groupId):
        self._db.removeElement('addLayers/layers', groupId)

        self._db.updateElements('geometry', 'layerGroup', None, lambda i, e: e['layerGroup'] == groupId)
        self._db.updateElements('geometry', 'slaveLayerGroup', None, lambda i, e: e['slaveLayerGroup'] == groupId)

        self._ui.boundaryLayerConfigurations.removeItem(groupId)

    def _updateControlButtons(self):
        if self.isNextStepAvailable():
            self._ui.boundaryLayerApply.hide()
            self._ui.boundaryLayerReset.show()
        else:
            self._ui.boundaryLayerApply.show()
            self._ui.boundaryLayerApply.setEnabled(True)
            self._ui.boundaryLayerReset.hide()

    def _enableStep(self):
        self._enableEdit()
        self._ui.boundaryLayerButtons.setEnabled(True)

    def _disableStep(self):
        self._disableEdit()
        self._ui.boundaryLayerButtons.setEnabled(False)

    def _enableEdit(self):
        self._ui.loadBoundaryLayerDefaults.setEnabled(True)
        self._ui.boundaryLayerConfigurationsAdd.setEnabled(True)
        self._ui.boundaryLayerConfigurations.enableEdit()
        self._ui.boundaryLayerAdvancedConfiguration.setEnabled(True)

    def _disableEdit(self):
        self._ui.loadBoundaryLayerDefaults.setEnabled(False)
        self._ui.boundaryLayerConfigurationsAdd.setEnabled(False)
        self._ui.boundaryLayerConfigurations.disableEdit()
        self._ui.boundaryLayerAdvancedConfiguration.setEnabled(False)

    @qasync.asyncSlot()
    async def _run(self):
        # H11. An untitled case chooses its home before a stage runs, not
        # after the mesh exists in a folder that gets swept.
        if not await self.requireSavedCase():
            return False

        self._disableEdit()

        result = False
        try:
            execution = await app.facadeClient.run(
                'workflow.run_stage', {'stage': 'layers',
                                       'on_line': app.consoleView.append})
            result = execution.status == 'accepted'
            if not result:
                await AsyncMessageBox().warning(
                    self._widget, self.tr('Boundary layers not applied'),
                    self.stageFailureDetail(execution))
            else:
                self._layerWarnings = list(
                    (execution.payload or {}).get('layer_warnings') or ())
        except Exception as e:
            await AsyncMessageBox().information(self._widget, self.tr('Error'),
                                                self.tr('Failed to apply boundary layers:') + str(e))

        if not result:
            self.clearResult()
        else:
            await self._reloadResultMesh()

        return result

    @qasync.asyncSlot()
    async def _cancel(self):
        await app.facadeClient.cancel_active_job()
