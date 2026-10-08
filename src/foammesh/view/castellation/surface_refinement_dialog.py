#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtGui import QIntValidator, QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QSizePolicy, QWidget)

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.view.widgets.commit_guard import (
    CONFLICT_ERRORS, commit_guard, conflict_message)
from widgets.async_message_box import AsyncMessageBox
from widgets.multi_selector_dialog import MultiSelectorDialog, SelectorItem
from widgets.validation.validation import FormValidator, NotGreaterValidator

from foammesh.app import app
from foammesh.db.configurations_schema import GeometryType, ZoneMode
from foammesh.view.geometry.display_name import readable_geometry_name
from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell
from foammesh.view.geometry.merged_boundaries import MergedBoundaries
from .surface_refinement_dialog_ui import Ui_SurfaceRefinementDialog


baseName = 'Group_'


class SurfaceRefinementDialog(QDialog):
    def __init__(self, parent, db, groupId=None):
        super().__init__(parent)
        self._ui = Ui_SurfaceRefinementDialog()
        self._ui.setupUi(self)
        # DP-202. Hidden, and now said so here rather than only in the
        # accountability generator: `curvatureLevel` is not read by
        # Foundation 13, so the group stays in the form to keep the
        # generated module its shape and never reaches a reader.
        self._ui.curvatureRefinement.hide()
        self._includedAngle = CompactDoubleSpinBox(self._ui.widget_4)
        self._includedAngle.setObjectName('includedAngle')
        self._includedAngle.setRange(0.001, 179.999)
        self._includedAngle.setDecimals(3)
        # DP-164. The same quantity on `snappy.surface_features` wrote its
        # unit inside the box as ` deg`; here it had no unit at all.
        self._ui.formLayout_3.addRow(self.tr('Feature included angle'),
                                     unit_cell(self._includedAngle, 'deg'))
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
        self._buildTheKeysNoClickCouldReach()
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

    # ---------------------------------------------------------------- #
    # DP-388: the four keys the schema declared and no click could set
    # ---------------------------------------------------------------- #

    def _buildTheKeysNoClickCouldReach(self):
        """The four `surfaceRefinement` keys with no control anywhere.

        DP-388. `configurations_schema.surfaceRefinement` declares eight
        top-level keys for a castellation group. Four of them --
        `perpendicularAngle`, `patchGroups`, `zoneMode` and
        `zoneInsidePoint` -- appeared nowhere in `src/foammesh/view/`, in
        this form or any other, so no sequence of clicks could set them and
        every case took the schema default. `CaseBuilder` reads all four
        (`_add_perpendicular_angle`, `_add_patch_groups`, `_zone_selection`),
        which is what made them worth declaring in the first place.

        Each row is shown only where OpenFOAM 13 reads the key, which is
        decided by what the selected surfaces *are* -- see
        `_showTheRowsThatAreRead`. A control that reaches nothing is the
        fault this closes, so it is hidden rather than merely disabled,
        exactly as the sibling volume dialog hides its span controls.
        """
        # `refinementSurfaces.C:141` reads this per surface with a plain
        # `readIfPresent`, so leaving the box empty leaves the key out and
        # v13 keeps its own `-great` sentinel: the baffle-removal pass does
        # nothing, which is what every case written before this control did.
        self._perpendicularAngle = QLineEdit(self._ui.widget_4)
        self._perpendicularAngle.setObjectName('perpendicularAngle')
        self._perpendicularAngle.setValidator(QDoubleValidator(0.0, 180.0, 3))
        self._perpendicularAngle.setPlaceholderText(self.tr('Off'))
        self._perpendicularAngle.setToolTip(self.tr(
            'Refine cells where these surfaces meet the base grid at less '
            'than this angle. Leave empty to leave the key out, which is '
            'what OpenFOAM does by default.'))
        # DP-164/DP-21. Degrees in the box, as the label says; the writer
        # converts, because this is the one angle v13 reads raw.
        self._perpendicularAngleCell = unit_cell(self._perpendicularAngle,
                                                 'deg')
        self._ui.formLayout_3.addRow(self.tr('Perpendicular angle'),
                                     self._perpendicularAngleCell)

        # `patchInfo` is handed to `polyPatch::New` verbatim
        # (`meshRefinement.C:1947`), so `inGroups` in it is the patch group
        # the meshed patch joins -- the mechanism that makes `walls`
        # addressable as one name in every later dictionary.
        self._patchGroups = QLineEdit(self._ui.widget_4)
        self._patchGroups.setObjectName('patchGroups')
        self._patchGroups.setPlaceholderText(self.tr('None'))
        self._patchGroups.setToolTip(self.tr(
            'Patch groups the meshed patches join, separated by spaces. A '
            'group name is an OpenFOAM word: letters, digits, _ and . only.'))
        self._ui.formLayout_3.addRow(self.tr('Patch groups'),
                                     self._patchGroups)

        # `surfaceZonesInfo.C:34-40` registers exactly these four and reads
        # the choice at `:70-82`, but only on an entry that also writes a
        # `faceZone`. The writer hard-coded `inside`, so a jacket, an
        # annulus, or a zone seeded by a point was unreachable.
        self._zoneMode = QComboBox(self._ui.widget_4)
        self._zoneMode.setObjectName('zoneMode')
        for label, mode in (
                (self.tr('Inside the surface'), ZoneMode.INSIDE),
                (self.tr('Outside the surface'), ZoneMode.OUTSIDE),
                (self.tr('Region holding a point'), ZoneMode.INSIDE_POINT),
                (self.tr('No cell selection'), ZoneMode.NONE)):
            self._zoneMode.addItem(label, mode)
        self._zoneMode.setToolTip(self.tr(
            'Which side of these surfaces the cell zone is taken from. '
            'Inside and Outside need a closed surface; a seed point works '
            'on an open one.'))
        self._ui.formLayout_3.addRow(self.tr('Cell zone side'), self._zoneMode)

        # `surfaceZonesInfo.C:79-82`: with `mode insidePoint` the point is
        # mandatory -- `lookup<point>("insidePoint", dimLength)` -- so the
        # row appears with that mode and with no other.
        self._zoneInsidePoint = QWidget(self._ui.widget_4)
        self._zoneInsidePoint.setObjectName('zoneInsidePoint')
        point = QHBoxLayout(self._zoneInsidePoint)
        point.setContentsMargins(0, 0, 0, 0)
        self._zoneInsidePointBoxes = []
        for axis in ('x', 'y', 'z'):
            box = QLineEdit(self._zoneInsidePoint)
            box.setObjectName(f'zoneInsidePoint{axis.upper()}')
            box.setValidator(QDoubleValidator())
            box.setPlaceholderText(axis)
            point.addWidget(box)
            self._zoneInsidePointBoxes.append(box)
        self._zoneInsidePointCell = unit_cell(self._zoneInsidePoint, 'm')
        self._ui.formLayout_3.addRow(self.tr('Inside point'),
                                     self._zoneInsidePointCell)
        # Whether the zone rows are read at all is a property of the selected
        # surfaces, not of whether the dialog happens to be on screen yet:
        # `isVisible` is False for every widget before the first `show`, and
        # the inside-point row is decided while the form is still being
        # loaded.
        self._zoneIsRead = False

    def _rowVisible(self, field, visible):
        """Show or hide one form row, label and all (the sibling idiom)."""
        field.setVisible(visible)
        label = self._ui.formLayout_3.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _effectiveCfdType(self, gId):
        """What a selected surface is for, as `CaseBuilder` decides it.

        DP-387 put the CellZone choice on the *volume*, because "the cells
        inside this shape" is a question about a volume; the surfaces that
        bound it stay `boundary`. `CaseBuilder._effective_cfd_type` reads it
        the same way, and these rows have to agree with the writer or they
        would appear where the key is not read after all.
        """
        try:
            geometry = self._db.getElement('geometry', str(gId))
        except Exception:                                    # noqa: BLE001
            return 'boundary'
        cfdType = str(geometry.value('cfdType') or 'boundary')
        if cfdType != 'boundary':
            return cfdType
        volume = geometry.value('volume')
        if not volume:
            return cfdType
        try:
            owner = self._db.getElement('geometry', str(volume))
        except Exception:                                    # noqa: BLE001
            return cfdType
        if str(owner.value('cfdType') or 'none') == 'cellZone':
            return 'cellZone'
        return cfdType

    def _showTheRowsThatAreRead(self):
        """Show each of the four rows exactly where its key is read.

        The branch in `CaseBuilder` that a surface takes decides which keys
        its `refinementSurfaces` entry can carry:

        - `perpendicularAngle` is written before the branch, so it is read
          for every surface and its row is always there.
        - `patchGroups` lands in `patchInfo`, and only the interface branch
          and the plain-boundary branch create one. A `faceZone`-only entry
          makes no patch, so there is nothing for a group to name -- the
          writer says so in a warning, and this says it by not asking.
        - `zoneMode` is read only on the cellZone branch, which is the only
          branch that writes a `cellZone`.
        - `zoneInsidePoint` is mandatory under `insidePoint` and unread
          under the other three.
        """
        kinds = {self._effectiveCfdType(gId) for gId in (self._surfaces or ())}
        makesAPatch = bool(kinds - {'none', 'cellZone'})
        isZone = 'cellZone' in kinds
        self._zoneIsRead = isZone
        self._rowVisible(self._patchGroups, makesAPatch)
        self._rowVisible(self._zoneMode, isZone)
        self._zoneModeChanged()

    def _zoneModeChanged(self):
        wanted = (self._zoneIsRead
                  and self._zoneMode.currentData() is ZoneMode.INSIDE_POINT)
        self._rowVisible(self._zoneInsidePointCell, wanted)

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
        self._zoneMode.currentIndexChanged.connect(self._zoneModeChanged)

    @qasync.asyncSlot()
    async def _accept(self):
        # DP-427. This dialog used to write its group into the working
        # copy the page handed it at construction and never commit it, so
        # the group survived only while that copy was still the one the case
        # later took. MEASURED over the 148-leg W-G campaign: of the 32
        # snappy legs whose dictionary was written down the plain
        # `patchInfo` path, exactly 1 kept its refinement level and 31 came
        # out `level (0 0)` on every surface -- `SimpleDB.commit` returns
        # `{}` and writes nothing when the copy is not editable, silently,
        # so 31 meshes were castellated against a dictionary that asked for
        # nothing and every one of them was graded runnable. The write is
        # this dialog's, so this dialog commits it, against a copy taken at
        # OK rather than one as old as the dialog. DP-119 made this repair
        # in `boundary_setting_dialog.py` for the same reason.
        with commit_guard(self._ui.ok):
            groupName = self._ui.groupName.text().strip()
            if self._db.getKeys('castellation/refinementSurfaces',
                                lambda i, e: e['groupName'] == groupName and i != self._groupId):
                await AsyncMessageBox().warning(self, self.tr('Input error'),
                                                    self.tr('Group name "{0}" already exists.').format(groupName))
                return

            validator = FormValidator()
            validator.addCustomValidation(NotGreaterValidator(self._ui.minimumLevel, self._ui.maximumLevel,
                                                              self.tr('Minimum level'), self.tr('Maximum level')))

            valid, msg = validator.validate()
            if not valid:
                await AsyncMessageBox().warning(self, self.tr('Input error'), msg)
                return

            if not self._surfaces:
                await AsyncMessageBox().warning(self, self.tr('Input error'), self.tr('Select at least one surface.'))
                return

            # DP-388. A patch group is an OpenFOAM `word`, so it may not carry a
            # space or punctuation other than _ and . -- `CaseBuilder` refuses
            # the same names, and refusing here says so while the field that
            # holds them is still on screen.
            names = self._patchGroups.text().replace(',', ' ').split()
            wrong = [name for name in names
                     if not name.replace('_', '').replace('.', '').isalnum()]
            if wrong:
                await AsyncMessageBox().warning(
                    self, self.tr('Input error'),
                    self.tr('"{0}" is not a patch group name. A group name is an '
                            'OpenFOAM word: letters, digits, _ and . only.'
                            ).format(wrong[0]))
                return

            # `surfaceZonesInfo.C:79-82` makes the point mandatory under this
            # mode, and a seed the user did not place is not a seed.
            if (self._zoneIsRead
                    and self._zoneMode.currentData() is ZoneMode.INSIDE_POINT
                    and not any(box.text().strip()
                                for box in self._zoneInsidePointBoxes)):
                await AsyncMessageBox().warning(
                    self, self.tr('Input error'),
                    self.tr('A cell zone selected by a point needs the point. '
                            'Give a coordinate inside the region you want.'))
                return

            try:
                self._dbElement.setValue('groupName', groupName, self.tr('Group name'))
                self._dbElement.setValue('surfaceRefinement/minimumLevel', self._ui.minimumLevel.text(),
                                         self.tr('Surface refinement minimum level'))
                self._dbElement.setValue('surfaceRefinement/maximumLevel', self._ui.maximumLevel.text(),
                                         self.tr('Surface refinement maximum level'))
                self._dbElement.setValue('featureEdgeRefinementLevel', self._ui.featureEdgeRefinementLevel.text(),
                                         self.tr('Feature edge refinement level'))
                self._dbElement.setValue('includedAngle', self._includedAngle.value())
                # An empty box means "no opinion": the key is left out of this
                # surface's entry and OpenFOAM falls back to the case-wide value.
                self._dbElement.setValue(
                    'gapLevelIncrement',
                    self._gapLevelIncrement.text().strip() or None,
                    self.tr('Gap level increment'))
                # DP-388. An empty angle box means the same as an empty gap box:
                # the key is left out and OpenFOAM keeps its own sentinel.
                self._dbElement.setValue(
                    'perpendicularAngle',
                    self._perpendicularAngle.text().strip() or None,
                    self.tr('Perpendicular angle'))
                self._dbElement.setValue('patchGroups',
                                         self._patchGroups.text().strip(),
                                         self.tr('Patch groups'))
                self._dbElement.setValue('zoneMode',
                                         self._zoneMode.currentData().value,
                                         self.tr('Cell zone side'))
                for axis, box in zip(('x', 'y', 'z'),
                                     self._zoneInsidePointBoxes):
                    self._dbElement.setValue(f'zoneInsidePoint/{axis}',
                                             box.text().strip() or '0',
                                             self.tr('Inside point'))

                # Taken here rather than at load(): the window between OK and
                # the commit is microseconds, where the window between opening
                # the dialog and OK is however long the user spends in it.
                db = app.facadeClient.checkout()
                if self._groupId:
                    db.commit(self._dbElement)
                    groupId = self._groupId
                else:
                    groupId = db.addElement(
                        'castellation/refinementSurfaces', self._dbElement)

                surfaces = {gId: None for gId in self._oldSurfaces}
                for gId in self._surfaces:
                    if gId in surfaces:
                        surfaces.pop(gId)
                    else:
                        surfaces[gId] = groupId

                # R137. One selector entry can stand for several rows: a merged
                # boundary is offered once and every solid it covers has to carry
                # the group, or the refinement reaches one side of the wall only.
                for gId, group in self._merged.expand(surfaces).items():
                    db.setValue(f'geometry/{gId}/castellationGroup', group)

                await app.facadeClient.commit_working_copy(
                    db, action='update surface refinement')

                # Only now: a refused commit added no group, and a dialog that
                # had already taken the id would reopen editing an element the
                # case has never heard of.
                self._groupId = groupId
                super().accept()
            except CONFLICT_ERRORS as error:
                # Stay open. Every value below is read back off the
                # widgets on the next OK, so the user loses nothing.
                self._reopenElement()
                await AsyncMessageBox().warning(
                    self, self.tr('Case changed'),
                    self.tr(conflict_message(error)))
            except ValidationError as error:
                await AsyncMessageBox().warning(self, self.tr('Input error'), error.toMessage())

    def _reopenElement(self):
        """Take the element again after a commit the case refused.

        Committing an element marks it read-only, so the OK pressed after
        "the case changed" would raise ``LookupError`` out of a slot rather
        than retry. Every value this dialog writes is read back off its
        widgets, so a fresh element and a fresh copy are all it needs.
        """
        self._db = app.facadeClient.checkout()
        if self._groupId:
            self._dbElement = self._db.checkout(
                f'castellation/refinementSurfaces/{self._groupId}')
        else:
            self._dbElement = self._db.newElement(
                'castellation/refinementSurfaces')

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
        # DP-388. An unset perpendicular angle is an empty box, the same way
        # an unset gap increment is: both mean "leave the key out".
        angle = self._dbElement.getValue('perpendicularAngle')
        self._perpendicularAngle.setText('' if angle is None else str(angle))
        self._patchGroups.setText(
            str(self._dbElement.getValue('patchGroups') or ''))
        # A stored mode outside the four `surfaceZonesInfo.C` registers is a
        # project written by something other than this app. `inside` is what
        # every case said before this control existed, so that is where an
        # unreadable value lands rather than the dialog refusing to open.
        try:
            mode = ZoneMode(str(self._dbElement.getValue('zoneMode')))
        except ValueError:
            mode = ZoneMode.INSIDE
        self._zoneMode.setCurrentIndex(max(0, self._zoneMode.findData(mode)))
        for axis, box in zip(('x', 'y', 'z'), self._zoneInsidePointBoxes):
            box.setText(str(self._dbElement.getValue(
                f'zoneInsidePoint/{axis}')))

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
        self._showTheRowsThatAreRead()

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
            self, self.tr('Select surfaces'), self._availableSurfaces,
            self._surfaces, app.selectionService)
        self._dialog.itemsSelected.connect(self._setSurfaces)
        self._dialog.open()

    def _setSurfaces(self, items):
        self._surfaces = []
        self._ui.surfaces.clear()
        for gId, name in items:
            self._surfaces.append(gId)
            self._ui.surfaces.addItem(name)
        # DP-388. Which of the four rows are read follows from what was just
        # selected, so it is recomputed here and not only at load.
        self._showTheRowsThatAreRead()

    def _updateCellSize(self, level, cellSize):
        from foammesh.view.snappy_workflow.level_cell_size import cell_text

        # R23. Six significant digits per axis is what pushed this line past
        # the panel edge; four still names the cell. DP-179. The house
        # helper does the rest: one decimal count for all three, so the
        # three components of one cell can be compared with each other,
        # and the unit they are in, which this line never carried.
        # DP-1251. The arithmetic and the rendering are the shared helper's,
        # which the guided row editor's readout reads too, so the two editors
        # of one level cannot print two cells. It is still `format_group`.
        text = cell_text((self._xCellSize, self._yCellSize, self._zCellSize),
                         level.text())
        if not text:
            cellSize.setText(self.tr(
                'cell size available once the base grid is set'))
            return
        cellSize.setText(self.tr('cell size <b>({0})</b>').format(text))
