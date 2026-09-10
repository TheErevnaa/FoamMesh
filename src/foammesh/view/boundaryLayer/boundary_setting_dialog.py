#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtWidgets import QDialog

from foammesh.support.simple_db.simple_schema import ValidationError
from widgets.async_message_box import AsyncMessageBox
from widgets.multi_selector_dialog import SelectorItem, MultiSelectorDialog

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType, LayerPolicy
from foammesh.view.geometry.merged_boundaries import MergedBoundaries
from .thickness_form import ThicknessForm
from .boundary_setting_dialog_ui import Ui_BoundarySettingDialog


baseName = 'Group_'

#: The smallest count that actually grows a layer. The schema's low limit is
#: now 0, because ``nSurfaceLayers 0`` is v13's own spelling for a frozen
#: patch (F-39) -- but a *new* group asking to grow nothing is still nobody's
#: intent, so creation offers this instead.
minimumLayerCount = 1


def initialLayerCount(stored) -> str:
    """What Number of Layers should read when a layer group is opened (R33).

    MEASURED: adding a boundary-layer group opened the dialog with Number of
    Layers = 0 -- a boundary-layer group that adds no boundary layer. The zero
    is not a decision anyone made: a new element takes ``IntType``'s blank
    default of '0'. Zero is now saveable and means "freeze this patch", which
    makes offering it by accident worse rather than better, so a new group
    still starts at one. An existing group keeps whatever it was saved with.
    """
    text = str(stored if stored is not None else '').strip()
    try:
        if int(text) >= minimumLayerCount:
            return text
    except ValueError:
        pass
    return str(minimumLayerCount)


def storedLayerPolicy(stored) -> LayerPolicy:
    """The policy a saved group carries, defaulting to the one it used to have.

    A group saved before F-39 has no ``layerPolicy`` at all, and the loader's
    migration gives it one; a group whose stored text is not a policy is a
    corrupt document, and the dialog opening on "grow" is a better answer
    than the dialog refusing to open.
    """
    if isinstance(stored, LayerPolicy):
        return stored
    try:
        return LayerPolicy(str(stored))
    except ValueError:
        return LayerPolicy.GROW


class BoundarySettingDialog(QDialog):
    def __init__(self, parent, db, groupId=None):
        super().__init__(parent)
        self._ui = Ui_BoundarySettingDialog()
        self._ui.setupUi(self)

        self._thicknessForm = ThicknessForm(self._ui)

        # F-39. Three states, because OpenFOAM 13 reads three: a patch left
        # out of `layers {}` slides during layer addition, a patch written as
        # `nSurfaceLayers 0` is frozen, and a patch with a count grows.
        self._ui.layerPolicy.addEnumItems({
            LayerPolicy.GROW: self.tr('Grow layers'),
            LayerPolicy.FREEZE: self.tr('Freeze (no layers, no sliding)'),
            LayerPolicy.INHERIT: self.tr('Inherit (slide with neighbours)'),
        })

        self._groupId = groupId
        self._db = db
        self._dbElement = None
        self._creationMode = groupId is None
        self._dialog = None
        self._boundaries = None
        self._oldBoundaries = None
        self._availableBoundaries = None
        self._merged = None                                      # R137

        self._connectSignalsSlots()

        self._load()

    def dbElement(self):
        return self._dbElement

    def groupId(self):
        return self._groupId

    def isCreationMode(self):
        return self._creationMode

    def disableEdit(self):
        self._ui.settings.setEnabled(False)
        self._ui.select.setEnabled(False)
        self._ui.ok.hide()
        self._ui.cancel.setText(self.tr('Close'))

    @qasync.asyncSlot()
    async def _accept(self):
        try:
            groupName = self._ui.groupName.text().strip()
            if self._db.getKeys('addLayers/layers', lambda i, e: e['groupName'] == groupName and i != self._groupId):
                await AsyncMessageBox().information(self, self.tr('Input Error'),
                                                    self.tr('Group name "{0}" already exists.').format(groupName))
                return

            if not self._boundaries:
                await AsyncMessageBox().information(self, self.tr('Input Error'), self.tr('Select boundaries'))
                return

            self._dbElement.setValue('groupName', groupName, self.tr('Group Name'))
            policy = self._ui.layerPolicy.currentData()
            self._dbElement.setValue('layerPolicy', policy, None)
            # A frozen group *is* `nSurfaceLayers 0`; storing the count the
            # spin box happens to hold would leave the two disagreeing.
            self._dbElement.setValue(
                'nSurfaceLayers',
                '0' if policy == LayerPolicy.FREEZE
                else self._ui.numberOfLayers.text(),
                self.tr('Number of Layers'))
            self._thicknessForm.save(self._dbElement)

            if self._groupId:
                self._db.commit(self._dbElement)
            else:
                self._groupId = self._db.addElement('addLayers/layers', self._dbElement)

            boundaries = {gId: None for gId in self._oldBoundaries}
            for gId in self._boundaries:
                if gId in boundaries:
                    boundaries.pop(gId)
                else:
                    boundaries[gId] = self._groupId

            for key, group in boundaries.items():
                gId, isSlave = self._extractSelectorKey(key)
                field = 'slaveLayerGroup' if isSlave else 'layerGroup'
                # R137. A merged boundary is one entry standing for several
                # rows; every solid it covers has to carry the group, or the
                # layers reach one side of the wall only.
                for member in self._merged.cover(gId):
                    self._db.setValue(f'geometry/{member}/{field}', group)

            super().accept()
        except ValidationError as error:
            await AsyncMessageBox().information(self, self.tr("Input Error"), error.toMessage())

    def _layerPolicyChanged(self, *_args):
        """A count and a thickness only mean something when layers grow."""
        growing = self._ui.layerPolicy.currentData() == LayerPolicy.GROW
        self._ui.numberOfLayers.setEnabled(growing)
        self._ui.groupBox.setEnabled(growing)
        self._ui.thickness.setEnabled(growing)

    def _connectSignalsSlots(self):
        self._ui.layerPolicy.currentDataChanged.connect(self._layerPolicyChanged)
        self._thicknessForm.modelChanged.connect(self.adjustSize)
        self._ui.select.clicked.connect(self._selectBoundaries)
        self._ui.ok.clicked.connect(self._accept)
        self._ui.cancel.clicked.connect(self.close)

    def _load(self):
        def addAvailableBoundary(name, key):
            self._availableBoundaries.append(SelectorItem(name, name, key))

        def addSelectedBoundary(name, key):
            self._ui.boundaries.addItem(name)
            self._boundaries.append(key)

        if self._groupId:
            self._dbElement = self._db.checkout(f'addLayers/layers/{self._groupId}')
            name = self._dbElement.getValue('groupName')
        else:
            self._dbElement = self._db.newElement('addLayers/layers')
            name = f"{baseName}{self._db.getUniqueSeq('addLayers/layers', 'groupName', baseName, 1)}"

        self._ui.groupName.setText(name)
        self._ui.layerPolicy.setCurrentData(storedLayerPolicy(
            self._dbElement.getValue('layerPolicy')))
        # R33. A new group arrived offering zero layers; see initialLayerCount.
        self._ui.numberOfLayers.setText(
            initialLayerCount(self._dbElement.getValue('nSurfaceLayers'))
            if self._creationMode
            else self._dbElement.getValue('nSurfaceLayers'))
        self._layerPolicyChanged()
        self._thicknessForm.setData(self._dbElement)

        self._boundaries = []
        self._availableBoundaries = []
        # R137. Same defect as the Castellation surface selector: a Repair
        # merge is recorded in the geometry manifest, and this collection
        # still holds one row per imported solid, so a `wall` merged from
        # `wall_shell` + `wall_bore` was offered here as its two halves.
        self._merged = MergedBoundaries(self._db)

        for gId, geometry in self._db.getElements('geometry').items():
            cfdType = geometry.value('cfdType')
            if cfdType == CFDType.BOUNDARY.value or cfdType == CFDType.INTERFACE.value:
                if app.window.geometryManager.isBoundingHex6(gId):
                    continue
                if self._merged.isFollower(gId):
                    continue

                name = self._merged.nameFor(gId) or geometry.value('name')
                groupId = geometry.value('layerGroup')
                if groupId is None:
                    addAvailableBoundary(name, gId)
                elif groupId == self._groupId:
                    addAvailableBoundary(name, gId)
                    addSelectedBoundary(name, gId)

                if cfdType == CFDType.INTERFACE.value:
                    name = f'{name}_slave'
                    sId = f'{gId}s'
                    groupId = geometry.value('slaveLayerGroup')
                    if groupId is None:
                        addAvailableBoundary(name, sId)
                    elif groupId == self._groupId:
                        addAvailableBoundary(name, sId)
                        addSelectedBoundary(name, sId)

        self._oldBoundaries = self._boundaries

    def _selectBoundaries(self):
        if self._dialog is None:
            self._dialog = MultiSelectorDialog(self, self.tr('Select Boundaries'),
                                               self._availableBoundaries, self._boundaries,
                                               app.selectionService)
            self._dialog.itemsSelected.connect(self._setBoundaries)
        self._dialog.open()

    def _setBoundaries(self, items):
        self._boundaries = []
        self._ui.boundaries.clear()
        for key, _ in items:
            self._boundaries.append(key)
            gId, isSlave = self._extractSelectorKey(key)
            # R137. The list under the button has to read the merged name too,
            # or accepting the dialog renames the entry the user just picked.
            name = (self._merged.nameFor(gId)
                    or self._db.getElement('geometry', gId).value('name'))
            self._ui.boundaries.addItem(f'{name}_slave' if isSlave else name)

    def _extractSelectorKey(self, key):
        if key[-1:] == 's':
            return key[:-1], True

        return key, False
