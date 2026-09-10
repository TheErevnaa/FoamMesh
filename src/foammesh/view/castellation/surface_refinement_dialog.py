#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtGui import QIntValidator, QDoubleValidator
from PySide6.QtWidgets import (
    QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QLineEdit,
    QSizePolicy)

from foammesh.support.simple_db.simple_schema import ValidationError
from widgets.async_message_box import AsyncMessageBox
from widgets.multi_selector_dialog import MultiSelectorDialog, SelectorItem
from widgets.validation.validation import FormValidator, NotGreaterValidator

from foammesh.app import app
from foammesh.db.configurations_schema import GeometryType
from foammesh.view.geometry.display_name import readable_geometry_name
from foammesh.view.geometry.merged_boundaries import MergedBoundaries
from .surface_refinement_dialog_ui import Ui_SurfaceRefinementDialog


baseName = 'Group_'


class SurfaceRefinementDialog(QDialog):
    def __init__(self, parent, db, groupId=None):
        super().__init__(parent)
        self._ui = Ui_SurfaceRefinementDialog()
        self._ui.setupUi(self)
        self._ui.curvatureRefinement.hide()
        self._includedAngle = QDoubleSpinBox(self._ui.widget_4)
        self._includedAngle.setObjectName('includedAngle')
        self._includedAngle.setRange(0.001, 179.999)
        self._includedAngle.setDecimals(3)
        self._ui.formLayout_3.addRow(self.tr('Feature included angle'), self._includedAngle)
        # C31-08. Gap refinement, per surface. Foundation 13 reads
        # ``gapLevelIncrement`` on each ``refinementSurfaces`` entry
        # (``refinementSurfaces.C:100-110``) and adds it to that surface's own
        # maximum level, so a narrow seal can be refined harder than the
        # farfield. Only the case-wide increment had a control, which meant
        # the whole mesh paid for one narrow gap. The ESI ``gapLevel`` triple
        # and ``gapMode`` are deliberately absent: they are not in Foundation
        # 13's library at all.
        self._gapLevelIncrement = QLineEdit(self._ui.widget_4)
        self._gapLevelIncrement.setObjectName('gapLevelIncrement')
        self._gapLevelIncrement.setValidator(QIntValidator(0, 10))
        self._gapLevelIncrement.setPlaceholderText(self.tr('Case default'))
        self._gapLevelIncrement.setToolTip(self.tr(
            'Extra refinement levels for narrow gaps on these surfaces, on '
            'top of their maximum level. Leave empty to use the case-wide '
            'value from the Castellation page.'))
        self._ui.formLayout_3.addRow(self.tr('Gap level increment'),
                                     self._gapLevelIncrement)
        self._letTheFormFitANarrowPanel()

        self._db = db
        self._groupId = groupId
        self._dbElement = None
        self._creationMode = groupId is None
        self._dialog = None
        self._surfaces = None
        self._oldSurfaces = None
        self._availableSurfaces = None
        self._merged = None                                      # R137

        self._xCellSize = None
        self._yCellSize = None
        self._zCellSize = None

        self._ui.minimumLevel.setValidator(QIntValidator(0, 100))
        self._ui.maximumLevel.setValidator(QIntValidator(1, 100))
        self._ui.featureEdgeRefinementLevel.setValidator(QIntValidator(0, 100))
        self._ui.curvatureNumberOfCells.setValidator(QIntValidator())
        self._ui.curvatureMaximumCellLevel.setValidator(QIntValidator(1, 100))
        self._ui.curvatureMinimumRadius.setValidator(QDoubleValidator())

        self._xCellSize, self._yCellSize, self._zCellSize = app.window.geometryManager.getCellSize()

        self._connectSignalsSlots()

        self._load()

    #: The width of the Castellation panel this editor is embedded in.
    PANEL_WIDTH = 290

    #: The narrowest an entry field may be squeezed to (R23). Wide
    #: enough to show a refinement level or a cell size in full.
    FIELD_MIN_WIDTH = 64

    def _letTheFormFitANarrowPanel(self):
        """Let this form shrink to the width of the page panel (R23, R138).

        MEASURED at 1920x1080, editor embedded in the ~290px Castellation
        panel: the form was laid out at the width of the standalone dialog it
        was built as -- 934px of minimum width -- so `Select`, the only
        control that puts surfaces into a group, and the `OK` / `Cancel` row
        were drawn past the panel's right edge, and the `cell size (...)`
        annotations were cut mid-parenthesis. The group therefore read as
        having no surfaces and OK refused with "Select surfaces".

        Nothing here is decoration: the width came from single-line labels
        that refuse to wrap plus label-beside-field rows that refuse to stack.
        Every label in these forms is walked rather than a list of the ones
        that were widest on the day, so a row added later cannot quietly put
        the editor back over the edge.
        """
        roles = (QFormLayout.ItemRole.LabelRole,
                 QFormLayout.ItemRole.FieldRole,
                 QFormLayout.ItemRole.SpanningRole)
        for name in ('formLayout', 'formLayout_2', 'formLayout_3'):
            form = getattr(self._ui, name, None)
            if form is None:
                continue
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            for row in range(form.rowCount()):
                for role in roles:
                    item = form.itemAt(row, role)
                    label = item.widget() if item is not None else None
                    if not isinstance(label, QLabel):
                        continue
                    label.setWordWrap(True)
                    if role is not QFormLayout.ItemRole.LabelRole:
                        # A label in the field column is a read-out, not a
                        # control: it is the one that may give way.
                        label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                            QSizePolicy.Policy.Preferred)
            # Every entry field also keeps the minimum width it was given as
            # a standalone dialog, which no amount of label wrapping reaches.
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                field = item.widget() if item is not None else None
                if field is None or isinstance(field, QLabel):
                    continue
                field.setMinimumWidth(min(field.minimumSizeHint().width(),
                                          self.FIELD_MIN_WIDTH))

        # MEASURED: with the labels wrapped the form still asked for 295px
        # against the 290px panel, and all 295 came from one place. A
        # QGroupBox reserves the full drawn width of its title in its own
        # minimum -- Qt will not wrap or elide a frame title -- and
        # `Surface Refinement` is 234px of that. It is also the dialog's
        # window title verbatim, so inside the panel it repeated a heading
        # the user could already read. Dropping the duplicate keeps the
        # frame doing the grouping and gives the 164px back.
        for group in self.findChildren(QGroupBox):
            if group.title() == self.windowTitle():
                group.setTitle('')

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

    def _connectSignalsSlots(self):
        self._ui.minimumLevel.editingFinished.connect(self._updateMinimumLevelCellSize)
        self._ui.maximumLevel.editingFinished.connect(self._updateMaximumLevelCellSize)
        self._ui.featureEdgeRefinementLevel.editingFinished.connect(self._updateFeatureEdgeLevelCellSize)
        self._ui.curvatureMaximumCellLevel.editingFinished.connect(self._updateCurvatureMaximumLevelCellSize)
        self._ui.select.clicked.connect(self._selectSurfaces)
        self._ui.ok.clicked.connect(self._accept)

    @qasync.asyncSlot()
    async def _accept(self):
        groupName = self._ui.groupName.text().strip()
        if self._db.getKeys('castellation/refinementSurfaces',
                            lambda i, e: e['groupName'] == groupName and i != self._groupId):
            await AsyncMessageBox().information(self, self.tr('Input Error'),
                                                self.tr('Group name "{0}" already exists.').format(groupName))
            return

        validator = FormValidator()
        validator.addCustomValidation(NotGreaterValidator(self._ui.minimumLevel, self._ui.maximumLevel,
                                                          self.tr('Minimum Level'), self.tr('Maximum Level')))

        valid, msg = validator.validate()
        if not valid:
            await AsyncMessageBox().information(self, self.tr('Input Error'), msg)
            return

        if not self._surfaces:
            await AsyncMessageBox().information(self, self.tr('Input Error'), self.tr('Select surfaces'))
            return

        try:
            self._dbElement.setValue('groupName', groupName, self.tr('Group Name'))
            self._dbElement.setValue('surfaceRefinement/minimumLevel', self._ui.minimumLevel.text(),
                                     self.tr('Surface Refinement Minimum Level'))
            self._dbElement.setValue('surfaceRefinement/maximumLevel', self._ui.maximumLevel.text(),
                                     self.tr('Surface Refinement Maximum Level'))
            self._dbElement.setValue('featureEdgeRefinementLevel', self._ui.featureEdgeRefinementLevel.text(),
                                     self.tr('Feature Edge Refinement Level'))
            self._dbElement.setValue('includedAngle', self._includedAngle.value())
            # An empty box means "no opinion": the key is left out of this
            # surface's entry and OpenFOAM falls back to the case-wide value.
            self._dbElement.setValue(
                'gapLevelIncrement',
                self._gapLevelIncrement.text().strip() or None,
                self.tr('Gap Level Increment'))

            if self._groupId:
                self._db.commit(self._dbElement)
            else:
                self._groupId = self._db.addElement('castellation/refinementSurfaces', self._dbElement)

            surfaces = {gId: None for gId in self._oldSurfaces}
            for gId in self._surfaces:
                if gId in surfaces:
                    surfaces.pop(gId)
                else:
                    surfaces[gId] = self._groupId

            # R137. One selector entry can stand for several rows: a merged
            # boundary is offered once and every solid it covers has to carry
            # the group, or the refinement reaches one side of the wall only.
            for gId, group in self._merged.expand(surfaces).items():
                self._db.setValue(f'geometry/{gId}/castellationGroup', group)

            super().accept()
        except ValidationError as error:
            await AsyncMessageBox().information(self, self.tr('Input Error'), error.toMessage())

    def _load(self):
        if self._groupId:
            self._dbElement = self._db.checkout(f'castellation/refinementSurfaces/{self._groupId}')
            name = self._dbElement.getValue('groupName')
        else:
            self._dbElement = self._db.newElement('castellation/refinementSurfaces')
            name = f"{baseName}{self._db.getUniqueSeq('castellation/refinementSurfaces', 'groupName', baseName, 1)}"

        self._ui.groupName.setText(name)
        self._ui.minimumLevel.setText(self._dbElement.getValue('surfaceRefinement/minimumLevel'))
        self._ui.maximumLevel.setText(self._dbElement.getValue('surfaceRefinement/maximumLevel'))
        self._ui.featureEdgeRefinementLevel.setText(self._dbElement.getValue('featureEdgeRefinementLevel'))
        self._includedAngle.setValue(float(self._dbElement.getValue('includedAngle')))
        self._gapLevelIncrement.setText(
            str(self._dbElement.getValue('gapLevelIncrement') or ''))

        self._surfaces = []
        self._availableSurfaces = []
        # R137. A Repair merge is recorded in the geometry manifest, not in
        # this collection, so the list below still holds one row per imported
        # solid: the selector offered `wall_bore` and `wall_shell` for a
        # boundary the user had already merged into `wall`.
        self._merged = MergedBoundaries(self._db)
        for gId, geometry in self._db.getElements('geometry').items():
            if geometry.value('gType') == GeometryType.SURFACE.value:
                if app.window.geometryManager.isBoundingHex6(gId):
                    continue
                if self._merged.isFollower(gId):
                    # Half of a boundary the user has named; it is offered
                    # under that name on the row that stands for it.
                    continue

                name = geometry.value('name')
                # E12. A surface whose name is its own content hash tells the
                # user nothing about which part they are refining; the file it
                # was imported from and the volume it sits in do. The raw name
                # stays as the filter text, so typing the hash still finds it.
                label = readable_geometry_name(
                    name, path=geometry.value('path'),
                    parent=self._volumeName(geometry))
                merged = self._merged.nameFor(gId)
                if merged:
                    name = label = merged
                groupId = geometry.value('castellationGroup')
                if groupId is None:
                    self._availableSurfaces.append(SelectorItem(label, name, gId))
                elif groupId == self._groupId:
                    self._availableSurfaces.append(SelectorItem(label, name, gId))
                    self._ui.surfaces.addItem(label)
                    self._surfaces.append(gId)

        # Membership updates need a snapshot, not an alias to the live list.
        self._oldSurfaces = list(self._surfaces)

        self._updateMinimumLevelCellSize()
        self._updateMaximumLevelCellSize()
        self._updateFeatureEdgeLevelCellSize()

    def _volumeName(self, geometry):
        """The name of the volume this surface belongs to, when it has one."""
        volume = geometry.value('volume')
        if not volume:
            return ''
        try:
            return self._db.getElement('geometry', str(volume)).value('name')
        except Exception:                                    # noqa: BLE001
            return ''

    def _updateMinimumLevelCellSize(self):
        self._updateCellSize(self._ui.minimumLevel, self._ui.minimumLevelCellSize)

    def _updateMaximumLevelCellSize(self):
        self._updateCellSize(self._ui.maximumLevel, self._ui.maximumLevelCellSize)

    def _updateFeatureEdgeLevelCellSize(self):
        self._updateCellSize(self._ui.featureEdgeRefinementLevel, self._ui.featureEdgeLevelCellSize)

    def _updateCurvatureMaximumLevelCellSize(self):
        self._updateCellSize(self._ui.curvatureMaximumCellLevel, self._ui.curvatureCellSize)

    def _selectSurfaces(self):
        self._dialog = MultiSelectorDialog(
            self, self.tr('Select Surfaces'), self._availableSurfaces,
            self._surfaces, app.selectionService)
        self._dialog.itemsSelected.connect(self._setSurfaces)
        self._dialog.open()

    def _setSurfaces(self, items):
        self._surfaces = []
        self._ui.surfaces.clear()
        for gId, name in items:
            self._surfaces.append(gId)
            self._ui.surfaces.addItem(name)

    def _updateCellSize(self, level, cellSize):
        d = 2 ** int(level.text())
        # R23. Six significant digits per axis is what pushed this line past
        # the panel edge; four still names the cell.
        cellSize.setText(
            'cell size <b>({:.4g} x {:.4g} x {:.4g})</b>'.format(
                self._xCellSize / d, self._yCellSize / d, self._zCellSize / d))
