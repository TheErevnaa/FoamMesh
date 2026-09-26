#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio

import qasync
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QLineEdit

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.view.widgets.commit_guard import CONFLICT_ERRORS, conflict_message
from widgets.async_message_box import AsyncMessageBox
from widgets.enum_combo_box import EnumComboBox
from widgets.list_table import ListItemWithButtons

from foammesh.app import app
from foammesh.db.configurations_schema import (
    GeometryType, OptionalToggle, schema)
from foammesh.db.configurations import defaultsDB
from foammesh.view.main_window.main_window_ui import Ui_MainWindow
from foammesh.view.step_page import StepPage
from foammesh.view.main_window.inpage_editor import open_in_page
from foammesh.view.theming.metrics import unit_cell
from .surface_refinement_dialog import SurfaceRefinementDialog
from .volume_refinement_dialog import VolumeRefinementDialog


class CastellationPage(StepPage):
    OUTPUT_TIME = 1

    def __init__(self, ui: Ui_MainWindow):
        super().__init__(ui, ui.castellationPage)

        self._ui = ui
        self._db = None
        self._dialog = None
        self._cm = None
        # DP-376. `save` is entered from two places that can overlap: the
        # button, and `StepPage.hide` when a navigation route leaves this
        # page. Both spend `self._db`, which lives as long as the page does.
        self._saving = asyncio.Lock()

        ui.castellationConfigurationHeader.setContents(ui.castellationConfiguration)
        ui.castellationAdvancedHeader.setContents(ui.castellationAdvanced)
        ui.surfaceRefinementHeader.setContents(ui.surfaceRefinement)
        ui.volumeRefinementHeader.setContents(ui.volumeRefinement)

        ui.surfaceRefinement.setBackgroundColor()
        ui.volumeRefinement.setBackgroundColor()
        self._addSurfacesColumnHeader(ui.surfaceRefinement)
        ui.surfaceRefinement.setHeaderWithWidth([0, 0, 0, 0, 16, 16])
        ui.volumeRefinement.setHeaderWithWidth([0, 0, 16, 16])

        ui.nCellsBetweenLevels.setValidator(QIntValidator(1, 1000000))
        edge_options = ui.keepNonManifoldEdges.parentWidget()
        if edge_options is not None:
            edge_options.hide()
        self._cellRisk = QLabel(ui.castellationConfiguration)
        self._cellRisk.setObjectName('castellationCellEstimate')
        self._cellRisk.setWordWrap(True)
        # DP-186. The label's text is the estimate and is rewritten on
        # every edit; a fixed accessible name did not shorten that for
        # an assistive reader, it replaced it. With no name of its own
        # the label is read out as what it says.
        ui.castellationConfiguration.layout().addWidget(self._cellRisk)

        ui.castellationCancel.hide()

        self._buildAdvancedControls()
        self._buildDiagnosticControls()

        self._connectSignalsSlots()

    def _addSurfacesColumnHeader(self, table):
        """Head the column that says which surfaces a group covers (R91).

        MEASURED: the refinement-group table listed the group name and its
        min/max levels only, so `Group_1` read identically whether it covered
        two surfaces or none and the only way to find out was to reopen the
        editor. The header is added here rather than in main_window.ui because
        that file is the one Designer produced.
        """
        # The header is drawn into the ListTable's own grid, so a stand-in
        # that is not a widget simply has no header to add.
        if not hasattr(table, 'layout'):
            return
        layout = table.layout()
        if layout is None:
            layout = QGridLayout(table)
            table.setLayout(layout)
        if layout.itemAtPosition(0, 3) is not None:
            return
        header = QLabel(self.tr('Surfaces'), table)
        header.setObjectName('surfaceRefinementSurfacesHeader')
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        layout.addWidget(header, 0, 3)

    def _surfaceRefinementMembers(self, groupId):
        """The surfaces a refinement group covers, for the table's fourth
        column (R91). The set of surfaces is what a group *is*, and it was the
        one thing the table did not report."""
        # R137. A merged boundary is one surface here too: reporting the
        # solids it was made of would contradict the selector that offered it
        # under a single name.
        from foammesh.view.geometry.merged_boundaries import MergedBoundaries

        merged = MergedBoundaries(self._db)
        names = [merged.nameFor(gId) or geometry.value('name')
                 for gId, geometry in self._db.getElements('geometry').items()
                 if geometry.value('gType') == GeometryType.SURFACE.value
                 and geometry.value('castellationGroup') == groupId
                 and not merged.isFollower(gId)]
        # A group with no surfaces refines nothing; saying so is the point.
        return ', '.join(sorted(names)) if names else self.tr('(none)')

    # Plan 29 WP7.1/WP7.5. Four castellatedMeshControls keys OpenFOAM
    # Foundation 13 reads that had no control, plus snappy's own diagnostic
    # output. They are added to the existing form rather than to the generated
    # .ui so the .ui stays the one Designer produced.
    def _buildAdvancedControls(self):
        form = self._ui.formLayout_8

        self._gapLevelIncrement = QLineEdit()
        self._gapLevelIncrement.setValidator(QIntValidator(0, 10))
        self._gapLevelIncrement.setPlaceholderText(self.tr('OpenFOAM default'))
        self._gapLevelIncrement.setToolTip(self.tr(
            'Extra refinement levels inside narrow gaps, on top of the surface '
            'level. Leave empty to use snappyHexMesh’s own default.'))
        form.addRow(self.tr('Gap level increment'), self._gapLevelIncrement)

        self._planarAngle = QLineEdit()
        self._planarAngle.setPlaceholderText(self.tr('OpenFOAM default'))
        self._planarAngle.setToolTip(self.tr(
            'Angle below which two faces count as planar when detecting cells '
            'that cannot be snapped. Leave empty to use the default.'))
        # DP-198. DP-164 left this one in the label on the grounds that a
        # line edit cannot carry a unit. It can: `unit_cell` takes any
        # editor, and this row now ends the way the registry rows beside it
        # do.
        form.addRow(self.tr('Planar angle'),
                    unit_cell(self._planarAngle, 'deg'))

        self._useTopologicalSnapDetection = self._toggle(self.tr(
            'Detect unsnappable cells from mesh topology rather than geometry.'))
        form.addRow(self.tr('Topological snap detection'),
                    self._useTopologicalSnapDetection)

        self._handleSnapProblems = self._toggle(self.tr(
            'Run the extra castellation pass that removes cells snapping '
            'cannot resolve.'))
        form.addRow(self.tr('Handle snap problems'), self._handleSnapProblems)

        # C31-08. The companion switch to the span refinement modes on the
        # volume dialog. ``meshRefinement.C:1142`` reads it out of
        # castellatedMeshControls with a default of true; turning it off keeps
        # a span's refinement inside the span instead of letting it extend.
        self._extendedRefinementSpan = self._toggle(self.tr(
            'Let refinement asked for by an Inside span or Outside span region '
            'reach beyond the span itself. OpenFOAM leaves this on.'))
        form.addRow(self.tr('Extended refinement span'),
                    self._extendedRefinementSpan)

    def _toggle(self, tip):
        """A switch that can also be left at whatever OpenFOAM decides.

        A plain checkbox would force a value into every case, changing meshes
        that were tuned before the control existed.
        """
        combo = EnumComboBox()
        combo.addEnumItems({
            OptionalToggle.DEFAULT: self.tr('OpenFOAM default'),
            OptionalToggle.ON: self.tr('On'),
            OptionalToggle.OFF: self.tr('Off'),
        })
        combo.setToolTip(tip)
        return combo

    def _buildDiagnosticControls(self):
        # The flag names come from the schema this page writes to. Reading them
        # off the OpenFOAM writer instead would put the dictionary format in
        # the view, which is the boundary the PC0 gate keeps.
        advanced = schema['snappyAdvanced']
        form = self._ui.formLayout_8
        self._writeFlags = {}
        self._debugFlags = {}
        labels = {
            'scalarLevels': self.tr('cellLevel field'),
            'layerSets': self.tr('layer cell/face sets'),
            'layerFields': self.tr('layer coverage field'),
            'mesh': self.tr('intermediate meshes'),
            'intersections': self.tr('mesh intersections'),
            'featureSeeds': self.tr('feature edge seeds'),
            'attraction': self.tr('feature attraction'),
            'layerInfo': self.tr('layer information'),
        }
        for target, names, title in (
                (self._writeFlags, advanced['writeFlags'], self.tr('Write')),
                (self._debugFlags, advanced['debugFlags'], self.tr('Debug'))):
            for index, name in enumerate(names):
                box = QCheckBox(labels[name])
                target[name] = box
                form.addRow(title if index == 0 else '', box)

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        self.load()
        self.updateWorkingStatus()

    async def save(self):
        # DP-376. One at a time, and each against a copy of its own.
        #
        # MEASURED on `two_cubes_one_file`, the topology fixture of core rows
        # V03 and V04: a route out of this page (step 4 -> step 3) was started
        # and then overtaken, but `_moveToStep` awaits `hide()` -- and so the
        # autosave -- before it asks whether it has been superseded. That
        # autosave was still inside `commit_working_copy` when the explicit
        # save began, so both held the same working copy, `id` for `id`. The
        # first commit spent it (`_editable = False`); the second was refused
        # with `commit_working_copy requires an editable working copy`, which
        # is neither a conflict nor a `ValidationError` and so escaped both
        # handlers below and ended the leg.
        #
        # The copy could be taken per save now that DP-427 has made the
        # refinement dialogs commit their own -- which is what used to make
        # a fresh checkout here drop their groups. Serialising is kept
        # anyway, because it is what stops two saves of *this* page's own
        # two subtrees from spending one copy twice, and it loses nothing -- the waiting save re-reads the
        # widgets and runs against the copy the finished one re-checked out,
        # so it writes what the page says now rather than what it said when
        # it was first asked.
        async with self._saving:
            return await self._save()

    async def _save(self):
        try:
            # WP-13 / F-33. This one stays a whole-database copy: the page writes
            # two subtrees, `castellation` and `snappyAdvanced`, and a scoped copy
            # can only carry one. Committing both together is also what the user
            # means by pressing Apply once, so the merge - and its conflict, now
            # caught below - is the right behaviour rather than a cost.
            castellation = self._db.checkout('castellation')

            castellation.setValue('nCellsBetweenLevels', self._ui.nCellsBetweenLevels.text(),
                              self.tr('Number of cells between levels'))
            castellation.setValue('resolveFeatureAngle', self._ui.resolveFeatureAngle.text(),
                              self.tr('Feature angle threshold'))
            castellation.setValue('maxGlobalCells', self._ui.maxGlobalCells.text(), self.tr('Max. global cell count'))
            castellation.setValue('maxLocalCells', self._ui.maxLocalCells.text(), self.tr('Max. local cell count'))
            castellation.setValue('minRefinementCells', self._ui.minRefinementCells.text(),
                              self.tr('Min. refinement cell count'))
            castellation.setValue('maxLoadUnbalance', self._ui.maxLoadUnbalance.text(), self.tr('Max. load unbalance'))
            castellation.setValue('allowFreeStandingZoneFaces', self._ui.allowFreeStandingZoneFaces.isChecked())
            # An empty box means "no opinion": the key is left out of the
            # dictionary entirely so OpenFOAM's own default still governs.
            castellation.setValue(
                'gapLevelIncrement', self._gapLevelIncrement.text().strip() or None,
                self.tr('Gap level increment'))
            castellation.setValue(
                'planarAngle', self._planarAngle.text().strip() or None,
                self.tr('Planar angle'))
            castellation.setValue('useTopologicalSnapDetection',
                                  self._useTopologicalSnapDetection.currentData())
            castellation.setValue('handleSnapProblems',
                                  self._handleSnapProblems.currentData())
            castellation.setValue('extendedRefinementSpan',
                                  self._extendedRefinementSpan.currentData())

            advanced = self._db.checkout('snappyAdvanced')
            for group, boxes in (('writeFlags', self._writeFlags),
                                 ('debugFlags', self._debugFlags)):
                for name, box in boxes.items():
                    advanced.setValue(f'{group}/{name}', box.isChecked())
            self._db.commit(advanced)

            self._db.commit(castellation)
            await app.facadeClient.commit_working_copy(self._db, action='update castellation')

            # DP-16. A refinement group added here can be renumbered by the
            # merge, when another copy had already taken the key it allocated.
            # The rows on this page are keyed by the number they were given,
            # so they are rebuilt from what actually landed rather than left
            # pointing at whatever now holds the old number.
            remapped = bool(self._db.remappedKeys())
            self._db = app.facadeClient.checkout()
            if remapped:
                self._loaded = False
                self.load()

            return True
        except CONFLICT_ERRORS as error:
            await AsyncMessageBox().warning(
                self._widget, self.tr('Case changed'), self.tr(conflict_message(error)))
            return False
        except ValidationError as e:
            await AsyncMessageBox().warning(self._widget, self.tr('Input error'), e.toMessage())
            return False

    def load(self):
        self._db = app.facadeClient.checkout()

        if self._loaded:
            return

        castellation = self._db.getElement('castellation')
        self._setConfigurastions(castellation)
        self._setDiagnostics(self._db.getElement('snappyAdvanced'))

        self._ui.surfaceRefinement.clear()
        self._ui.volumeRefinement.clear()

        groups = {GeometryType.SURFACE.value: set(), GeometryType.VOLUME.value: set()}
        for gId, geometry in self._db.getElements('geometry').items():
            if group := geometry.value('castellationGroup'):
                groups[geometry.value('gType')].add(group)

        for groupId, element in castellation.elements('refinementSurfaces').items():
            if groupId in groups[GeometryType.SURFACE.value]:
                surfaceRefinement = element.element('surfaceRefinement')
                self._addSurfaceRefinementItem(
                    groupId, element.value('groupName'),
                    surfaceRefinement.value('minimumLevel'), surfaceRefinement.value('maximumLevel'))
            else:
                self._db.removeElement('castellation/refinementSurfaces', groupId)

        for groupId, element in castellation.elements('refinementVolumes').items():
            if groupId in groups[GeometryType.VOLUME.value]:
                self._addVolumeRefinementItem(groupId,
                                              element.value('groupName'), element.value('volumeRefinementLevel'))
            else:
                self._db.removeElement('castellation/refinementVolumes', groupId)

        self._loaded = True

    async def runInBatchMode(self):
        if not await self.save():
            return False

        self._ui.refine.setEnabled(False)

        return await self._run()

    def _connectSignalsSlots(self):
        self._ui.loadCastellationDefaults.clicked.connect(self._loadDefaults)
        self._ui.surfaceRefinementAdd.clicked.connect(lambda: self._openSurfaceRefinementDialog())
        self._ui.volumeRefinementAdd.clicked.connect(lambda: self._openVolumeRefinementDialog())
        self._ui.refine.clicked.connect(self._refine)
        self._ui.castellationCancel.clicked.connect(self._cancel)
        self._ui.castellationReset.clicked.connect(self._reset)
        self._ui.maxGlobalCells.textChanged.connect(self._updateCellRisk)

    @qasync.asyncSlot()
    async def _loadDefaults(self):
        if await AsyncMessageBox().confirm(
                self._widget, self.tr('Reset settings'),
                self.tr(
                    'Would you like to reset all Castellation settings to default, excluding the refinement groups?')):
            self._setConfigurastions(defaultsDB.getElement('castellation'))

    def _setConfigurastions(self, castellation):
        self._ui.nCellsBetweenLevels.setText(castellation.value('nCellsBetweenLevels'))
        self._ui.resolveFeatureAngle.setText(castellation.value('resolveFeatureAngle'))
        self._ui.maxGlobalCells.setText(castellation.value('maxGlobalCells'))
        self._ui.maxLocalCells.setText(castellation.value('maxLocalCells'))
        self._ui.minRefinementCells.setText(castellation.value('minRefinementCells'))
        self._ui.maxLoadUnbalance.setText(castellation.value('maxLoadUnbalance'))
        self._ui.allowFreeStandingZoneFaces.setChecked(castellation.value('allowFreeStandingZoneFaces'))
        self._gapLevelIncrement.setText(
            castellation.value('gapLevelIncrement') or '')
        self._planarAngle.setText(castellation.value('planarAngle') or '')
        self._useTopologicalSnapDetection.setCurrentData(
            castellation.enum('useTopologicalSnapDetection'))
        self._handleSnapProblems.setCurrentData(
            castellation.enum('handleSnapProblems'))
        self._extendedRefinementSpan.setCurrentData(
            castellation.enum('extendedRefinementSpan'))
        self._updateCellRisk()

    def _setDiagnostics(self, advanced):
        for group, boxes in (('writeFlags', self._writeFlags),
                             ('debugFlags', self._debugFlags)):
            element = advanced.element(group)
            for name, box in boxes.items():
                box.setChecked(element.value(name))

    def _updateCellRisk(self):
        try:
            db = self._db or app.facadeClient.checkout()
            counts = tuple(int(db.getValue(f'baseGrid/numCells{axis}'))
                           for axis in 'XYZ')
            background = counts[0] * counts[1] * counts[2]
            maximum = int(float(self._ui.maxGlobalCells.text()))
        except (AttributeError, KeyError, TypeError, ValueError):
            self._cellRisk.setText(self.tr('Cell-count estimate unavailable.'))
            return
        message = self.tr(
            'Estimated background cells: {0:,}; maxGlobalCells: {1:,}.').format(
                background, maximum)
        if background / max(1, maximum) >= .5:
            message += self.tr(
                ' Warning: refinement has little remaining global-cell budget.')
        self._cellRisk.setText(message)

    def _openSurfaceRefinementDialog(self, groupId=None):
        self._dialog = SurfaceRefinementDialog(self._widget, self._db, groupId)
        if self._locked:
            self._dialog.disableEdit()
        else:
            self._dialog.accepted.connect(self._surfaceRefinementDialogAccepted)
        if not open_in_page(self._dialog, self._widget,
                            self._ui.castellationButtons):
            self._dialog.open()

    def _openVolumeRefinementDialog(self, groupId=None):
        self._dialog = VolumeRefinementDialog(self._widget, self._db, groupId)
        if self._locked:
            self._dialog.disableEdit()
        else:
            self._dialog.accepted.connect(self._volumeRefinementDialogAccepted)
        if not open_in_page(self._dialog, self._widget,
                            self._ui.castellationButtons):
            self._dialog.open()

    @qasync.asyncSlot()
    async def _refine(self):
        if not await self.save():
            return

        self._ui.refine.hide()
        self._ui.castellationCancel.show()
        app.consoleView.clear()

        if await self._run():
            self.stepCompleted.emit()

            await AsyncMessageBox().information(self._widget, self.tr('Complete'),
                                                self.tr('Castellation refinement is completed.'))

        self._enableEdit()
        self._ui.castellationCancel.hide()

        self.updateWorkingStatus()

    def _reset(self):
        self._showPreviousMesh()
        self.clearResult()
        self._updateControlButtons()
        self.stepReset.emit()

    def _surfaceRefinementDialogAccepted(self):
        element = self._dialog.dbElement()
        # DP-427. The dialog committed its own copy, so this page's is a
        # revision behind: the group and the `geometry/<id>/castellationGroup`
        # it set are in the case and not in `self._db`. Left stale, the next
        # Apply would commit over them and the table below would read the
        # members off a copy that has never heard of the group.
        self._db = app.facadeClient.checkout()
        if self._dialog.isCreationMode():
            self._addSurfaceRefinementItem(self._dialog.groupId(), element.getValue('groupName'),
                                           element.getValue('surfaceRefinement/minimumLevel'),
                                           element.getValue('surfaceRefinement/maximumLevel'))
        else:
            # R91. The edited group may have gained or lost surfaces, so the
            # fourth column is rewritten with the other three.
            item = self._ui.surfaceRefinement.item(self._dialog.groupId())
            surfaces = self._surfaceRefinementMembers(self._dialog.groupId())
            item.update([
                element.getValue('groupName'),
                element.getValue('surfaceRefinement/minimumLevel'),
                element.getValue('surfaceRefinement/maximumLevel'), surfaces])
            item.widget(3).setToolTip(surfaces)

    def _addSurfaceRefinementItem(self, groupId, name, minLevel, maxLevel):
        surfaces = self._surfaceRefinementMembers(groupId)
        item = ListItemWithButtons(
            groupId, [name, minLevel, maxLevel, surfaces])
        # Several names in a 290px panel have to wrap; the panel cannot widen.
        item.widget(3).setWordWrap(True)
        item.widget(3).setToolTip(surfaces)
        item.editClicked.connect(lambda: self._openSurfaceRefinementDialog(groupId))
        item.removeClicked.connect(lambda: self._removeSurfaceRefinement(groupId))
        self._ui.surfaceRefinement.addItem(item)

    def _removeSurfaceRefinement(self, groupId):
        self._db.removeElement('castellation/refinementSurfaces', groupId)
        self._db.updateElements(
            'geometry', 'castellationGroup', None,
            lambda i, e: e['castellationGroup'] == groupId and e['gType'] == GeometryType.SURFACE.value)

        self._ui.surfaceRefinement.removeItem(groupId)

    def _volumeRefinementDialogAccepted(self):
        element = self._dialog.dbElement()
        # DP-427. The dialog committed its own copy, so this page's is a
        # revision behind: the group and the `geometry/<id>/castellationGroup`
        # it set are in the case and not in `self._db`. Left stale, the next
        # Apply would commit over them and the table below would read the
        # members off a copy that has never heard of the group.
        self._db = app.facadeClient.checkout()
        if self._dialog.isCreationMode():
            self._addVolumeRefinementItem(self._dialog.groupId(), element.getValue('groupName'),
                                          element.getValue('volumeRefinementLevel'))
        else:
            self._ui.volumeRefinement.item(self._dialog.groupId()).update(
                [element.getValue('groupName'), element.getValue('volumeRefinementLevel')])

    def _addVolumeRefinementItem(self, groupId, name, level):
        item = ListItemWithButtons(groupId, [name, level])
        item.editClicked.connect(lambda: self._openVolumeRefinementDialog(groupId))
        item.removeClicked.connect(lambda: self._removeVolumeRefinement(groupId))
        self._ui.volumeRefinement.addItem(item)

    def _removeVolumeRefinement(self, groupId):
        self._db.removeElement('castellation/refinementVolumes', groupId)
        self._db.updateElements(
            'geometry', 'castellationGroup', None,
            lambda i, e: e['castellationGroup'] == groupId and e['gType'] == GeometryType.VOLUME.value)

        self._ui.volumeRefinement.removeItem(groupId)

    def _updateControlButtons(self):
        if self.isNextStepAvailable():
            self._ui.refine.hide()
            self._ui.castellationReset.show()
        else:
            self._ui.refine.show()
            self._ui.refine.setEnabled(True)
            self._ui.castellationReset.hide()

    def _enableStep(self):
        self._enableEdit()
        self._ui.castellationButtons.setEnabled(True)

    def _disableStep(self):
        self._disableEdit()
        self._ui.castellationButtons.setEnabled(False)

    def _enableEdit(self):
        self._ui.loadCastellationDefaults.setEnabled(True)
        self._ui.castellationConfiguration.setEnabled(True)
        self._ui.castellationAdvanced.setEnabled(True)
        self._ui.surfaceRefinementAdd.setEnabled(True)
        self._ui.surfaceRefinement.enableEdit()
        self._ui.volumeRefinementAdd.setEnabled(True)
        self._ui.volumeRefinement.enableEdit()

    def _disableEdit(self):
        self._ui.loadCastellationDefaults.setEnabled(False)
        self._ui.castellationConfiguration.setEnabled(False)
        self._ui.castellationAdvanced.setEnabled(False)
        self._ui.surfaceRefinementAdd.setEnabled(False)
        self._ui.surfaceRefinement.disableEdit()
        self._ui.volumeRefinementAdd.setEnabled(False)
        self._ui.volumeRefinement.disableEdit()

    async def _run(self):
        # H11. An untitled case chooses its home before a stage runs, not
        # after the mesh exists in a folder that gets swept.
        if not await self.requireSavedCase():
            return False

        self._disableEdit()

        result = False
        try:
            execution = await app.facadeClient.run(
                'workflow.run_stage', {'stage': 'castellation',
                                       'on_line': app.consoleView.append})
            result = execution.status == 'accepted'
            if not result:
                await AsyncMessageBox().warning(
                    self._widget, self.tr('Castellation refinement failed'),
                    self.stageFailureDetail(execution))
        except Exception as e:
            await AsyncMessageBox().warning(
                self._widget,
                self.tr('Castellation refinement failed'), str(e))

        if not result:
            self.clearResult()
        else:
            await self._reloadResultMesh(self.tr('Castellated mesh'))

        return result

    @qasync.asyncSlot()
    async def _cancel(self):
        await app.facadeClient.cancel_active_job()
