#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QHBoxLayout,
    QHeaderView, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from foammesh.support.simple_db.simple_schema import ValidationError
from widgets.async_message_box import AsyncMessageBox
from widgets.multi_selector_dialog import MultiSelectorDialog, SelectorItem

from foammesh.app import app
from foammesh.db.configurations_schema import (
    GeometryType, GapRefinementMode, RefinementRegionMode)
from .volume_refinement_dialog_ui import Ui_VolumeeRefinementDialog


baseName = 'Group_'


class VolumeRefinementDialog(QDialog):
    def __init__(self, parent, db, groupId=None):
        super().__init__(parent)
        self._ui = Ui_VolumeeRefinementDialog()
        self._ui.setupUi(self)
        self._ui.gapRefinement.hide()
        self._ui.levelIncrement.hide()

        # C31-08. All five modes ``refinementRegions.C`` names. Its
        # ``refineModeNames_`` is exactly
        # ``(inside outside distance insideSpan outsideSpan)``; the last two
        # were unreachable from this dialog, so a user who wanted refinement
        # sized from a body's own local thickness -- the usual request for a
        # thin duct or a narrow seal -- had no way to ask for it.
        self._regionMode = QComboBox(self._ui.widget)
        for label, mode in (
                (self.tr('Inside'), RefinementRegionMode.INSIDE),
                (self.tr('Outside'), RefinementRegionMode.OUTSIDE),
                (self.tr('Distance'), RefinementRegionMode.DISTANCE),
                (self.tr('Inside Span'), RefinementRegionMode.INSIDE_SPAN),
                (self.tr('Outside Span'), RefinementRegionMode.OUTSIDE_SPAN)):
            self._regionMode.addItem(label, mode)
        self._regionDistance = QDoubleSpinBox(self._ui.widget)
        self._regionDistance.setRange(1e-12, 1e12)
        self._regionDistance.setDecimals(8)
        # C31-08. A span is measured across a triangulated surface, so v13
        # needs to know how many cells to lay across it. ``refinementRegions.C``
        # reads ``cellsAcrossSpan`` with ``lookup``, not ``lookupOrDefault``:
        # leaving it out is a FatalIOError while the dictionary is still being
        # parsed, which is why this control is mandatory rather than optional.
        self._cellsAcrossSpan = QSpinBox(self._ui.widget)
        self._cellsAcrossSpan.setRange(1, 1000)
        self._cellsAcrossSpan.setToolTip(self.tr(
            'How many cells snappyHexMesh lays across the local thickness of '
            'the surface.'))
        self._ui.formLayout_3.addRow(self.tr('Refinement mode'), self._regionMode)
        self._ui.formLayout_3.addRow(self.tr('Distance'), self._regionDistance)
        self._ui.formLayout_3.addRow(self.tr('Cells Across Span'),
                                     self._cellsAcrossSpan)
        self._buildBandTable()
        self._regionMode.currentIndexChanged.connect(self._modeChanged)

        self._db = db
        self._dbElement = None
        self._creationMode = groupId is None
        self._accepted = False
        self._dialog = None
        self._volumes = None
        self._oldVolumes = None
        self._groupId = groupId
        self._availableVolumes = None

        self._ui.direction.addItem(self.tr('Mixed'),    GapRefinementMode.MIXED)
        self._ui.direction.addItem(self.tr('Inside'),   GapRefinementMode.INSIDE)
        self._ui.direction.addItem(self.tr('Outside'),  GapRefinementMode.OUTSIDE)

        self._xCellSize = None
        self._yCellSize = None
        self._zCellSize = None

        self._ui.volumeRefinementLevel.setValidator(QIntValidator(0, 100))

        self._xCellSize, self._yCellSize, self._zCellSize = app.window.geometryManager.getCellSize()

        self._connectSignalsSlots()

        self._load()

    # ---------------------------------------------------------------- #
    # C31-08: the distance ramp
    # ---------------------------------------------------------------- #
    def _buildBandTable(self):
        """The several ``(distance level)`` bands a distance region may hold.

        ``refinementRegions.C:145-190`` reads ``mode distance`` as a *list* of
        ``(distance level)`` pairs and refines each in turn -- the wake at
        level 3 within 5 mm, level 1 out to 50 mm -- and this dialog could
        only ever write one, so the ramp OpenFOAM's own tutorials use had no
        control. Built here rather than in the ``.ui`` so the file Designer
        produced stays the file Designer produced.
        """
        self._bands = QTableWidget(0, 2, self._ui.widget)
        self._bands.setHorizontalHeaderLabels(
            [self.tr('Distance'), self.tr('Level')])
        self._bands.verticalHeader().hide()
        self._bands.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._bands.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._bands.setToolTip(self.tr(
            'Bands are applied in order. OpenFOAM requires each band to reach '
            'further than the one before it, at the same level or a lower one.'))

        self._addBand = QPushButton(self.tr('Add'), self._ui.widget)
        self._removeBand = QPushButton(self.tr('Remove'), self._ui.widget)
        self._addBand.clicked.connect(self._appendBand)
        self._removeBand.clicked.connect(self._deleteBand)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addStretch(1)
        buttons.addWidget(self._addBand)
        buttons.addWidget(self._removeBand)

        self._bandsBox = QWidget(self._ui.widget)
        layout = QVBoxLayout(self._bandsBox)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._bands)
        layout.addLayout(buttons)
        self._ui.formLayout_3.addRow(self.tr('Refinement Bands'),
                                     self._bandsBox)

    def _appendBand(self, distance=None, level=None):
        row = self._bands.rowCount()
        if distance is None:
            # A new band has to reach further than the last one and may not
            # refine harder, so offer something that already obeys the rule.
            if row:
                distance = self._bandValue(row - 1, 0, 1.0) * 2
                level = max(0, int(self._bandValue(row - 1, 1, 1)) - 1)
            else:
                distance = self._regionDistance.value()
                level = int(self._ui.volumeRefinementLevel.text() or 1)
        self._bands.insertRow(row)
        self._bands.setItem(row, 0, QTableWidgetItem(f'{float(distance):g}'))
        self._bands.setItem(row, 1, QTableWidgetItem(str(int(level))))

    def _deleteBand(self):
        row = self._bands.currentRow()
        if row < 0:
            row = self._bands.rowCount() - 1
        # One band is the floor: a distance region with no bands would write
        # an empty ``levels ()``, which v13 reads and then refines nothing.
        if row >= 0 and self._bands.rowCount() > 1:
            self._bands.removeRow(row)

    def _bandValue(self, row, column, fallback):
        item = self._bands.item(row, column)
        text = '' if item is None else item.text().strip()
        if not text:
            return fallback
        try:
            return float(text)
        except ValueError:
            return fallback

    def _bandRows(self):
        rows = []
        for row in range(self._bands.rowCount()):
            rows.append((self._bandValue(row, 0, 0.0),
                         int(self._bandValue(row, 1, 0))))
        return rows

    def _bandError(self, rows):
        """Foundation 13's own two rules, checked while they can still be fixed.

        ``setAndCheckLevels`` raises ``FatalError`` -- "Refinement should be
        specified in order of increasing distance (and decreasing refinement
        level)" -- and a FatalError inside snappyHexMesh is a failed stage
        reported in a log. Saying it here costs the user a dialog instead.
        """
        for index, (distance, level) in enumerate(rows):
            if distance <= 0:
                return self.tr(
                    'Band {0} has a distance of {1}. Every band has to reach '
                    'a positive distance.').format(index + 1, distance)
            if level < 0:
                return self.tr('Band {0} has a negative level.').format(
                    index + 1)
            if index and (distance <= rows[index - 1][0]
                          or level > rows[index - 1][1]):
                return self.tr(
                    'Band {0} reaches {1} at level {2}, which does not follow '
                    'band {3} at {4}/level {5}. OpenFOAM requires increasing '
                    'distance and non-increasing level.').format(
                        index + 1, distance, level, index,
                        rows[index - 1][0], rows[index - 1][1])
        return None

    def _modeChanged(self):
        mode = self._regionMode.currentData()
        isDistance = mode is RefinementRegionMode.DISTANCE
        isSpan = mode in (RefinementRegionMode.INSIDE_SPAN,
                          RefinementRegionMode.OUTSIDE_SPAN)
        # A span carries a single ``level (distance level)`` pair, so the ramp
        # is meaningless there; inside/outside carry a bare level and use
        # neither. Showing a control that reaches nothing is the fault this
        # package exists to close, so they are hidden, not merely disabled.
        self._bandsBox.setVisible(isDistance)
        self._ui.formLayout_3.labelForField(self._bandsBox).setVisible(
            isDistance)
        self._cellsAcrossSpan.setVisible(isSpan)
        self._ui.formLayout_3.labelForField(self._cellsAcrossSpan).setVisible(
            isSpan)
        self._regionDistance.setVisible(isSpan)
        self._ui.formLayout_3.labelForField(self._regionDistance).setVisible(
            isSpan)
        self._regionDistance.setEnabled(isSpan)
        if isDistance and not self._bands.rowCount():
            self._appendBand()

    def dbElement(self):
        return self._dbElement

    def groupId(self):
        return self._groupId

    def isCreationMode(self):
        return self._creationMode

    def disableEdit(self):
        self._ui.parameters.setEnabled(False)
        self._ui.select.setEnabled(False)
        self._ui.ok.hide()
        self._ui.cancel.setText(self.tr('Close'))

    @qasync.asyncSlot()
    async def _accept(self):
        try:
            groupName = self._ui.groupName.text().strip()
            if self._db.getKeys('castellation/refinementVolumes',
                                lambda i, e: e['groupName'] == groupName and i != self._groupId):
                await AsyncMessageBox().information(self, self.tr('Input Error'),
                                                    self.tr('Group name "{0}" already exists.').format(groupName))
                return

            if not self._volumes:
                await AsyncMessageBox().information(self, self.tr('Input Error'), self.tr('Select volumes'))
                return

            mode = self._regionMode.currentData()
            rows = self._bandRows() if mode is RefinementRegionMode.DISTANCE \
                else []
            if rows:
                message = self._bandError(rows)
                if message:
                    await AsyncMessageBox().information(
                        self, self.tr('Input Error'), message)
                    return

            self._dbElement.setValue('groupName', groupName, self.tr('Group Name'))
            self._dbElement.setValue('mode', mode)
            # C31-08. ``bands`` is the ramp; ``distance`` and
            # ``volumeRefinementLevel`` stay the first band so a project saved
            # by an older build, which has no ``bands`` at all, still means
            # exactly what it meant before -- the writer falls back to them.
            self._dbElement.removeAllElements('bands')
            if rows:
                for distance, level in rows:
                    band = self._dbElement.newElement('bands')
                    band.setValue('distance', str(distance))
                    band.setValue('level', str(level))
                    self._dbElement.addElement('bands', band)
                self._dbElement.setValue('distance', rows[0][0])
                self._dbElement.setValue(
                    'volumeRefinementLevel', str(rows[0][1]),
                    self.tr('Volume Refinement Level'))
            else:
                self._dbElement.setValue(
                    'volumeRefinementLevel',
                    self._ui.volumeRefinementLevel.text(),
                    self.tr('Volume Refinement Level'))
                self._dbElement.setValue('distance', self._regionDistance.value())
            self._dbElement.setValue('cellsAcrossSpan',
                                     str(self._cellsAcrossSpan.value()),
                                     self.tr('Cells Across Span'))

            if self._groupId:
                self._db.commit(self._dbElement)
            else:
                self._groupId = self._db.addElement('castellation/refinementVolumes', self._dbElement)

            volumes = {gId: None for gId in self._oldVolumes}
            for gId in self._volumes:
                if gId in volumes:
                    volumes.pop(gId)
                else:
                    volumes[gId] = self._groupId

            for gId, group in volumes.items():
                self._db.setValue(f'geometry/{gId}/castellationGroup', group)

            super().accept()
        except ValidationError as error:
            await AsyncMessageBox().information(self, self.tr('Input Error'), error.toMessage())

    def _connectSignalsSlots(self):
        self._ui.volumeRefinementLevel.editingFinished.connect(self._updateCellSize)
        self._ui.select.clicked.connect(self._selectVolumes)
        self._ui.ok.clicked.connect(self._accept)
        self._ui.cancel.clicked.connect(self.close)

    def _load(self):
        if self._groupId:
            self._dbElement = self._db.checkout(f'castellation/refinementVolumes/{self._groupId}')
            name = self._dbElement.getValue('groupName')
        else:
            self._dbElement = self._db.newElement('castellation/refinementVolumes')
            name = f"{baseName}{self._db.getUniqueSeq('castellation/refinementVolumes', 'groupName', baseName, 1)}"

        self._ui.groupName.setText(name)
        self._ui.volumeRefinementLevel.setText(self._dbElement.getValue('volumeRefinementLevel'))
        mode = self._dbElement.getEnum('mode')
        self._regionMode.setCurrentIndex(max(0, self._regionMode.findData(mode)))
        self._regionDistance.setValue(float(self._dbElement.getValue('distance')))
        self._cellsAcrossSpan.setValue(
            int(self._dbElement.getValue('cellsAcrossSpan') or 5))
        self._bands.setRowCount(0)
        for _key, band in sorted(
                self._dbElement.getElements('bands').items(),
                key=lambda item: int(item[0])):
            self._appendBand(float(band.value('distance')),
                             int(band.value('level')))
        if not self._bands.rowCount():
            # A project saved before the ramp existed: its one pair is band 1.
            self._appendBand(float(self._dbElement.getValue('distance')),
                             int(self._dbElement.getValue(
                                 'volumeRefinementLevel')))
        self._modeChanged()

        self._volumes = []
        self._availableVolumes = []
        for gId, geometry in self._db.getElements('geometry').items():
            if geometry.value('gType') != GeometryType.VOLUME.value:
                continue

            if app.window.geometryManager.isBoundingHex6(gId):
                continue

            name = geometry.value('name')
            groupId = geometry.value('castellationGroup')
            if groupId is None:
                self._availableVolumes.append(SelectorItem(name, name, gId))
            elif groupId == self._groupId:
                self._availableVolumes.append(SelectorItem(name, name, gId))
                self._ui.volumes.addItem(name)
                self._volumes.append(gId)

        self._oldVolumes = self._volumes

        self._updateCellSize()

    def _updateCellSize(self):
        d = 2 ** int(self._ui.volumeRefinementLevel.text())
        self._ui.cellSize.setText(
            'cell size <b>({:g} x {:g} x {:g})</b>'.format(
                self._xCellSize / d, self._yCellSize / d, self._zCellSize / d))

    def _selectVolumes(self):
        self._dialog = MultiSelectorDialog(
            self, self.tr('Select Volumes'), self._availableVolumes,
            self._volumes, app.selectionService)
        self._dialog.itemsSelected.connect(self._setVolumes)
        self._dialog.open()

    def _setVolumes(self, items):
        self._volumes = []
        self._ui.volumes.clear()
        for gId, name in items:
            self._volumes.append(gId)
            self._ui.volumes.addItem(name)
