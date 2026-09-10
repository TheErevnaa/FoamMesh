#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Naming the boundaries the solver will see, on the page that owns them.

R178. These controls were built as a fourth tab on **2. Repair**, beside
Repair, Wrap and Use as-is, because that page already owned the other views
onto the geometry artifact. Nothing made them belong there: `geometry.patches.*`
reads the artifact store directly and needs no repair, no wrap and no prepared
revision. Meanwhile the boundaries themselves are rows in the geometry tree
(R169), so a user who split a surface saw the five new boundaries under
**1. Geometry** and had to go forward a step to name them -- and a user who
never opened Repair, because the geometry was clean, never found the controls
at all.

So the panel lives here, and the Repair page mounts the same widget only for
continuity of the tabs it already had.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (QAbstractItemView, QDoubleSpinBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from foammesh.app import app
from foammesh.core.facade.errors import FacadeError
from foammesh.core.geometry.features.manifest import DEFAULT_FEATURE_ANGLE_DEG
from foammesh.view.facade_client import FailedResult, query, submit
from widgets.fit_to_text import FlowLayout, fit_to_text


class BoundaryPanel(QWidget):
    """The boundary list and the four operations that edit it.

    Emits `geometryChanged` when an edit writes a new geometry revision, so
    whatever page is hosting the panel can re-read the tree, the findings and
    the revision strip it also shows.
    """

    geometryChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('boundaryPanel')
        self._build()

    # -- construction ------------------------------------------------------ #

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(self.tr(
            'Rename a boundary in place. Select several and merge them into '
            'one -- an inlet is one boundary condition, not forty CAD faces. '
            'Splitting a merged boundary restores the faces it was made from.'),
            self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self._patchTable = QTableWidget(0, 3, self)
        self._patchTable.setObjectName('boundaryPatchTable')
        self._patchTable.setAccessibleName(self.tr('Boundary patches'))
        self._patchTable.setHorizontalHeaderLabels([
            self.tr('Name'), self.tr('Geometry'), self.tr('Sub-surfaces')])
        self._patchTable.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._patchTable.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._patchTable.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._patchTable.itemChanged.connect(self._renamePatch)
        self._patchTable.itemSelectionChanged.connect(self._highlightPatches)
        layout.addWidget(self._patchTable, 1)

        self._patchNote = QLabel(self)
        self._patchNote.setObjectName('boundaryPatchNote')
        self._patchNote.setWordWrap(True)
        layout.addWidget(self._patchNote)

        # R51/R128. Four controls that each refuse to shrink below their own
        # label add up to more width than this panel has, and a QHBoxLayout
        # answered by overdrawing them: `Split merge` was painted over by
        # `Feature ang`, and `Split by feature angle...` ran off the right
        # edge with no scrollbar and no way to widen the panel. The row wraps
        # to a second line instead.
        buttons = FlowLayout()
        self._mergePatches = QPushButton(self.tr('Merge selected...'), self)
        self._mergePatches.setObjectName('mergeBoundaryPatches')
        self._mergePatches.clicked.connect(self._mergeSelectedPatches)
        self._splitPatch = QPushButton(self.tr('Split merged'), self)
        self._splitPatch.setObjectName('splitBoundaryPatch')
        self._splitPatch.clicked.connect(self._splitSelectedPatch)
        buttons.addWidget(self._mergePatches)
        buttons.addWidget(self._splitPatch)
        # Plan 28 WP7. A single-solid STL is one boundary and the two buttons
        # above have nothing to work on. This is where its sub-surfaces come
        # from: cut along the edges sharper than the angle, one boundary per
        # smooth piece, on both engines.
        angleLabel = QLabel(self.tr('Feature angle'), self)
        self._featureAngle = QDoubleSpinBox(self)
        self._featureAngle.setObjectName('featureSplitAngle')
        self._featureAngle.setAccessibleName(self.tr('Feature angle in degrees'))
        self._featureAngle.setRange(1.0, 179.0)
        self._featureAngle.setDecimals(1)
        self._featureAngle.setSingleStep(5.0)
        self._featureAngle.setSuffix('°')
        self._featureAngle.setValue(DEFAULT_FEATURE_ANGLE_DEG)
        self._featureAngle.setToolTip(self.tr(
            'Two triangles stay on one boundary while their normals differ '
            'by less than this.'))
        angleLabel.setBuddy(self._featureAngle)
        # R128. `Split by feature angle...` is wider than the whole content
        # panel, so wrapping the row was not enough on its own. The spin box
        # it sits beside already says which angle, and the tooltip the rest.
        self._splitByAngle = QPushButton(self.tr('Split by angle...'), self)
        self._splitByAngle.setObjectName('splitByFeatureAngle')
        self._splitByAngle.setToolTip(self.tr(
            'Cut the surface into one boundary per smooth region. Writes a '
            'new geometry revision; the previous one stays recoverable.'))
        self._splitByAngle.clicked.connect(self._splitByFeatureAngle)
        for button in (self._mergePatches, self._splitPatch,
                       self._splitByAngle):
            fit_to_text(button)
        fit_to_text(angleLabel, padding=4)
        # The label names the spin box beside it, so the two wrap as one item
        # or not at all: split across lines, `Feature angle` would read as a
        # heading for whatever button followed it.
        self._featureAngleBox = QWidget(self)
        self._featureAngleBox.setObjectName('featureSplitAngleBox')
        angleRow = QHBoxLayout(self._featureAngleBox)
        angleRow.setContentsMargins(0, 0, 0, 0)
        angleRow.addWidget(angleLabel)
        angleRow.addWidget(self._featureAngle)
        buttons.addWidget(self._featureAngleBox)
        buttons.addWidget(self._splitByAngle)
        layout.addLayout(buttons)

    # -- the list ---------------------------------------------------------- #

    def refresh(self):
        try:
            payload = query(app.facadeClient, 'geometry.patches.list').payload
        except (FacadeError, OSError, RuntimeError, ValueError) as error:
            self._patchNote.setText(
                self.tr('Boundaries unavailable: {0}').format(error))
            return
        rows = payload.get('patches', [])
        # Blocked while filling: every setItem emits itemChanged, and an
        # unguarded handler would send a rename for each row it just drew.
        with QSignalBlocker(self._patchTable):
            self._patchTable.setRowCount(len(rows))
            for row, patch in enumerate(rows):
                name = QTableWidgetItem(patch.get('name') or '')
                name.setData(Qt.ItemDataRole.UserRole, patch)
                self._patchTable.setItem(row, 0, name)
                for column, value in ((1, patch.get('geometry_name') or ''),
                                      (2, str(patch.get('member_count', 1)))):
                    item = QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._patchTable.setItem(row, column, item)
        if payload.get('single_boundary'):
            self._patchNote.setText(self.tr(
                'The whole surface is one boundary, so no inlet or outlet can '
                'be applied to it. Split the geometry before meshing.'))
        else:
            self._patchNote.setText(self.tr('{0} boundaries.').format(
                payload.get('count', len(rows))))

    def _highlightPatches(self):
        """Say in the viewport which face the selected row is.

        R165. A split leaves five rows called `tee_1` .. `tee_5` and asks the
        user to name them inlet, outlet and wall. Clicking a row highlighted
        the row and nothing else: the model stayed uniformly blue, so which
        face each row stood for was unknowable, and every boundary condition
        downstream rests on getting that right. The boundaries are geometry
        rows (R169), so they are actors in the scene like any other surface,
        and the selection service is what colours them -- the same path the
        geometry tree uses, so a pick in the viewport and a click in this
        table agree with each other.
        """
        if app.window is None:
            return
        service = getattr(app, 'selectionService', None)
        if service is None:
            return
        keys = self._geometryIdsNamed(
            [str(item.get('name') or '') for item in self._selectedPatches()])
        try:
            known = {str(entity.stable_id) for entity in service.entities()}
            service.select([key for key in keys if key in known])
        except (FacadeError, KeyError, RuntimeError, TypeError, ValueError):
            return

    def _selectedPatches(self):
        rows = sorted({index.row()
                       for index in self._patchTable.selectedIndexes()})
        out = []
        for row in rows:
            item = self._patchTable.item(row, 0)
            if item is not None:
                out.append(item.data(Qt.ItemDataRole.UserRole) or {})
        return out

    # -- the operations ---------------------------------------------------- #

    def _runPatchEdit(self, operation, parameters):
        # C31-12. Scheduled instead of run on the GUI thread: merging,
        # splitting and renaming boundaries all rewrite the geometry manifest
        # and the panel froze for the whole of each one.
        outcome = None

        def ran(result):
            nonlocal outcome
            if isinstance(result, FailedResult):
                # The refusal is written *after* the redraw, not before it:
                # `refresh()` ends by setting this same label to the boundary
                # count, so a refusal announced first was overwritten by the
                # redraw that followed it and the user was told nothing.
                self.refresh()
                self._patchNote.setText(result.message)
                outcome = False
                return
            self.refresh()
            self.geometryChanged.emit()
            outcome = True

        submit(app.facadeClient, operation, parameters, then=ran)
        # ``True``/``False`` once the facade has answered -- which is what a
        # caller sees when there is no loop to schedule onto. ``None`` means
        # the write is in flight, and its answer reaches the panel rather
        # than the caller.
        return outcome

    def _splitByFeatureAngle(self):
        """Preview the cut, say what it makes, and apply it when told to."""
        selected = [item.get('geometry_id') for item in self._selectedPatches()
                    if item.get('geometry_id')]
        parameters = {'angle_deg': float(self._featureAngle.value())}
        if selected:
            parameters['geometry_id'] = selected[0]
        outcome = None

        # C31-12. Both trips through the facade are scheduled: the preview
        # cut and the split it confirms. The question dialog still opens only
        # once the preview has answered -- same order, but the window stays
        # alive while a feature-angle preview runs over a large surface.
        def previewed(result):
            nonlocal outcome
            outcome = False
            if isinstance(result, FailedResult):
                self._patchNote.setText(result.message)
                return
            preview = result.payload
            count = int(preview.get('count') or 0)
            if count < 2:
                self._patchNote.setText(self.tr(
                    'No feature edge sharper than {0:g}°: the surface stays one '
                    'boundary. Try a smaller angle.').format(parameters['angle_deg']))
                return
            answer = QMessageBox.question(
                self, self.tr('Split by feature angle'),
                self.tr('Split the surface into {0} boundaries at {1:g}°?\n\n'
                        'This writes a new geometry revision; the current one stays '
                        'recoverable from the Revisions strip.').format(
                            count, parameters['angle_deg']),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
            parameters['geometry_id'] = (preview.get('geometry_id')
                                         or parameters.get('geometry_id'))
            outcome = self._runPatchEdit('geometry.patches.split_by_angle',
                                         parameters)

        submit(app.facadeClient, 'geometry.patches.split_by_angle',
               {**parameters, 'preview': True}, then=previewed)
        return outcome

    def _renamePatch(self, item):
        if item.column() != 0:
            return
        patch = item.data(Qt.ItemDataRole.UserRole) or {}
        name = item.text().strip()
        before = str(patch.get('name') or '')
        if not patch or name == before:
            return
        # A boundary that also has a tree row has its name in two stores, and
        # editing here used to move only one of them: the tree kept showing
        # the old name. geometry.rename moves both. A patch with no row of its
        # own - one produced by a merge or a feature split - has only the
        # manifest to move, so that path stays.
        geometry_id = self._geometryIdNamed(before)
        if geometry_id is not None:
            self._runPatchEdit('geometry.rename',
                               {'geometry_id': geometry_id, 'name': name})
        else:
            self._runPatchEdit('geometry.patches.rename', {
                'patch_uuid': patch.get('patch_uuid'), 'name': name})

    @classmethod
    def _geometryIdNamed(cls, name):
        """The tree row this boundary belongs to, when there is one."""
        found = cls._geometryIdsNamed([name])
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _geometryIdsNamed(names):
        """The tree rows these boundaries belong to, in the order asked.

        One checkout for the whole selection: this runs on every change of
        the table's selection, and re-reading the project per row would put
        a database read behind each arrow-key press.
        """
        wanted = [str(value) for value in names or () if value]
        if (not wanted or app.facadeClient is None
                or app.facadeClient.session() is None):
            return []
        try:
            db = app.facadeClient.checkout()
            keys = db.getKeys(
                'geometry',
                lambda _key, element: str(element.get('name')) in set(wanted))
            by_name = {}
            for key in keys:
                name = str(db.getElement('geometry', key).value('name'))
                # A name carried by two rows identifies neither of them.
                by_name[name] = None if name in by_name else str(key)
        except (FacadeError, KeyError, TypeError, ValueError):
            return []
        return [by_name[name] for name in wanted
                if by_name.get(name) is not None]

    def _mergeSelectedPatches(self):
        chosen = self._selectedPatches()
        if len(chosen) < 2:
            self._patchNote.setText(
                self.tr('Select two or more boundaries to merge.'))
            return
        name, accepted = QInputDialog.getText(
            self, self.tr('Merge boundaries'),
            self.tr('Name for the merged boundary'),
            text=chosen[0].get('name') or '')
        if not accepted or not name.strip():
            return
        self._runPatchEdit('geometry.patches.merge', {
            'patch_uuids': [item.get('patch_uuid') for item in chosen],
            'name': name.strip()})

    def _splitSelectedPatch(self):
        chosen = self._selectedPatches()
        if len(chosen) != 1:
            self._patchNote.setText(
                self.tr('Select one merged boundary to split.'))
            return
        self._runPatchEdit('geometry.patches.split',
                           {'patch_uuid': chosen[0].get('patch_uuid')})
