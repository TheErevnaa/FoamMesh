#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.view.widgets.commit_guard import CONFLICT_ERRORS, conflict_message
from widgets.async_message_box import AsyncMessageBox

from foammesh.app import app
from foammesh.db.configurations import defaultsDB
from foammesh.view.step_page import StepPage


class SnapPage(StepPage):
    OUTPUT_TIME = 2

    def __init__(self, ui):
        super().__init__(ui, ui.snapPage)

        self._cm = None

        # These widgets represented unsupported non-v13 snap controls. They remain
        # in the generated .ui for binary/layout compatibility but are removed
        # from the effective Foundation-13 editor.
        form = getattr(self._ui, 'formLayout_6', None)
        if form is not None:
            form.removeRow(self._ui.concaveAngle)
            form.removeRow(self._ui.minAreaRatio)
        else:
            self._ui.concaveAngle.hide()
            self._ui.minAreaRatio.hide()
        self._ui.bufferLayer.hide()
        # ``nSmoothInternal`` is not a Foundation-13 snapControls key.
        internal_form = getattr(self._ui, 'formLayout_4', None)
        if internal_form is not None:
            internal_form.removeRow(self._ui.smoothingForInternal)
        else:
            self._ui.smoothingForInternal.hide()

        self._ui.snapCancel.hide()

        self._connectSignalsSlots()

    def open(self):
        return

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        self.updateWorkingStatus()

        self._ui.snap.setEnabled(isWorkingStep and not batchRunning)

    async def save(self):
        try:
            # WP-13 / F-33. Scoped to `snap`: this page edits nothing else, and
            # a whole-database working copy held across the save is what lets
            # one page's commit collide with another's.
            snap = app.facadeClient.checkout('snap')

            snap.setValue('nSmoothPatch', self._ui.smoothingForSurface.text(), self.tr('Smoothing for Surface'))
            snap.setValue('nSolveIter', self._ui.meshDisplacementRelaxation.text(),
                        self.tr('Mesh Displacement Relaxation'))
            snap.setValue('nRelaxIter', self._ui.globalSnappingRelaxation.text(), self.tr('Global Snapping Relaxation'))
            # F-41. Two switches, not two halves of one. OpenFOAM 13 reads
            # implicitFeatureSnap and explicitFeatureSnap independently and
            # accepts both on; the enum this replaced could only ever write
            # one of them true and the other false.
            snap.setValue('implicitFeatureSnap',
                          self._ui.implicitFeatureSnap.isChecked())
            snap.setValue('explicitFeatureSnap',
                          self._ui.explicitFeatureSnap.isChecked())
            snap.setValue('nFeatureSnapIter', self._ui.featureSnappingRelaxation.text(),
                        self.tr('Feature Snapping Relaxation'))
            snap.setValue('multiRegionFeatureSnap', self._ui.multiSurfaceFeatureSnap.isChecked())
            snap.setValue('tolerance', self._ui.tolerance.text(), self.tr('Tolerance'))
            await app.facadeClient.commit_working_copy(snap, action='update snap')

            return True
        except CONFLICT_ERRORS as error:
            await AsyncMessageBox().information(
                self._widget, self.tr('Case Changed'), self.tr(conflict_message(error)))

            return False
        except ValidationError as e:
            await AsyncMessageBox().information(self._widget, self.tr('Input Error'), e.toMessage())

            return False

    def load(self):
        if self._loaded:
            return

        self._setConfigurations(app.facadeClient.checkout().getElement('snap'))
        self._loaded = True

    async def runInBatchMode(self):
        if not await self.save():
            return False

        self._ui.snap.setEnabled(False)

        return await self._run()

    def _connectSignalsSlots(self):
        self._ui.loadSnapDefaults.clicked.connect(self._loadDefaults)
        self._ui.snap.clicked.connect(self._snap)
        self._ui.snapCancel.clicked.connect(self._cancel)
        self._ui.snapReset.clicked.connect(self._reset)
        self._ui.explicitFeatureSnap.toggled.connect(self._featureSnapSwitchesChanged)

    @qasync.asyncSlot()
    async def _loadDefaults(self):
        if await AsyncMessageBox().confirm(
                self._widget, self.tr('Reset Settings'),
                self.tr('Would you like to reset all Snap settings to default, excluding the Buffer Layer Surfaces?')):
            self._setConfigurations(defaultsDB.getElement('snap'))

    def _setConfigurations(self, snap):
        self._ui.smoothingForSurface.setText(snap.value('nSmoothPatch'))
        self._ui.meshDisplacementRelaxation.setText(snap.value('nSolveIter'))
        self._ui.globalSnappingRelaxation.setText(snap.value('nRelaxIter'))
        self._ui.featureSnappingRelaxation.setText(snap.value('nFeatureSnapIter'))
        self._ui.implicitFeatureSnap.setChecked(snap.value('implicitFeatureSnap'))
        self._ui.explicitFeatureSnap.setChecked(snap.value('explicitFeatureSnap'))
        self._featureSnapSwitchesChanged()
        self._ui.multiSurfaceFeatureSnap.setChecked(snap.value('multiRegionFeatureSnap'))
        self._ui.tolerance.setText(snap.value('tolerance'))

    @qasync.asyncSlot()
    async def _snap(self):
        if not await self.save():
            return

        self._ui.snap.hide()
        self._ui.snapCancel.show()
        app.consoleView.clear()

        if await self._run():
            self.stepCompleted.emit()

            await AsyncMessageBox().information(self._widget, self.tr('Complete'), self.tr('Snapping is completed.'))

        self._enableEdit()
        self._ui.snapCancel.hide()

        self.updateWorkingStatus()

    def _reset(self):
        self._showPreviousMesh()
        self.clearResult()
        self._updateControlButtons()
        self._ui.snap.setEnabled(True)
        self.stepReset.emit()

    def _featureSnapSwitchesChanged(self, *_args):
        # multiRegionFeatureSnap is read by the explicit feature snapper only.
        self._ui.multiSurfaceFeatureSnap.setEnabled(
            self._ui.explicitFeatureSnap.isChecked())

    def _updateControlButtons(self):
        if self.isNextStepAvailable():
            self._ui.snap.hide()
            self._ui.snapReset.show()
        else:
            self._ui.snap.show()
            self._ui.snap.setEnabled(True)
            self._ui.snapReset.hide()

    def _enableStep(self):
        super()._enableStep()
        self._enableEdit()

    def _disableStep(self):
        super()._disableStep()
        self._disableEdit()

    def _enableEdit(self):
        self._ui.loadSnapDefaults.setEnabled(True)
        self._ui.snapContents.setEnabled(True)

    def _disableEdit(self):
        self._ui.loadSnapDefaults.setEnabled(False)
        self._ui.snapContents.setEnabled(False)

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
                'workflow.run_stage', {'stage': 'snap',
                                       'on_line': app.consoleView.append})
            result = execution.status == 'accepted'
            if not result:
                await AsyncMessageBox().warning(
                    self._widget, self.tr('Snapping failed'),
                    self.stageFailureDetail(execution))
        except Exception as e:
            await AsyncMessageBox().information(self._widget, self.tr('Error'),
                                                self.tr('Snapping Failed:') + str(e))

        if not result:
            self.clearResult()
        else:
            await self._reloadResultMesh()

        return result

    @qasync.asyncSlot()
    async def _cancel(self):
        await app.facadeClient.cancel_active_job()
