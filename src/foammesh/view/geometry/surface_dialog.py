#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QLabel

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.core.facade.errors import FacadeError, ValidationFailedError
from foammesh.view import inline_error
from foammesh.view.widgets.commit_guard import (CONFLICT_ERRORS, commit_guard,
                                                conflict_message)
from widgets.async_message_box import AsyncMessageBox

from foammesh.app import app
from foammesh.core.geometry.measure import (format_measurements,
                                             surface_measurements)
from foammesh.db import surface_authoring as authoring
from foammesh.db import surface_intent as intent
from foammesh.rendering.vtk_loader import polyDataToActor
from foammesh.view.theming.vtk_theme import rgb
from .surface_dialog_ui import Ui_SurfaceDialog
from .transform_widget import TransformWidget


#: Plan 49 SR2. The five roles, in plain words (plan section 3).
ROLE_TEXT = (
    (intent.EXTERNAL, 'External boundary'),
    (intent.INTERNAL, 'Internal face'),
    (intent.CONFORMAL, 'Region interface, conformal'),
    (intent.NON_CONFORMAL, 'Region interface, non-conformal'),
    (intent.BAFFLE, 'Blocking baffle'),
)

#: DP-6087. How an interface finds its other side.
AUTOMATIC, MANUAL = 'automatic', 'manual'
PAIRING_TEXT = (
    (AUTOMATIC, 'Automatic: find the walls this surface touches'),
    (MANUAL, 'Manual: choose the surfaces'),
)

PURPOSE_TEXT = {
    'wall': 'Wall', 'inlet': 'Inlet', 'outlet': 'Outlet',
    'far_field': 'Far field', 'symmetry': 'Symmetry', 'wedge': 'Wedge',
    'unclassified': 'Unclassified', 'interface': 'Interface',
}


def _patchTypeText(patchType) -> str:
    """The OpenFOAM type name; ``empty``/``wedge`` say they are not checked."""
    if patchType in authoring.UNCHECKED_PATCH_TYPES:
        return f'{patchType} (not checked)'
    return patchType


def _plainName(name) -> str:
    """A geometry name as the database and the artifact store would both spell it."""
    import re
    return re.sub(r'\W+', '_', str(name or ''), flags=re.ASCII).strip('_').lower()


class SurfaceDialog(QDialog):
    def __init__(self, parent, renderingView):
        super().__init__(parent)
        self._ui = Ui_SurfaceDialog()
        self._ui.setupUi(self)

        self._renderingView = renderingView

        self._transformWidget = TransformWidget(self)

        self._gIds = None
        self._dbElement = None

        self._sources = None
        self._actors = []

        self._editable = True
        self._transformed = False

        # Plan 49 SR2. One role, a purpose and a mesh type for an external
        # boundary, a partner and the region on each side for an interface.
        # These edit the SR1 model only; the legacy type radios and the two
        # interface switches are gone (``surface_authoring`` derives the
        # legacy fields the unmoved readers still need).
        for role, text in ROLE_TEXT:
            self._ui.role.addItem(self.tr(text), role)
        for patchType in intent.FREE_PATCH_TYPES:
            self._ui.patchType.addItem(_patchTypeText(patchType), patchType)
            if patchType in authoring.UNCHECKED_PATCH_TYPES:
                index = self._ui.patchType.count() - 1
                self._ui.patchType.setItemData(
                    index, self.tr('Needs a one-cell-thick extruded (empty) or '
                                   'revolved (wedge) mesh; nothing checks that '
                                   'until the mesh is published.'),
                    Qt.ItemDataRole.ToolTipRole)
        self._ui.intentIssue.hide()
        self._mountPairing()
        #: The document the dialog opened on, for the live preview.
        self._document = None
        self._refusal = None
        #: Controls the person changed; an untouched one keeps its value.
        self._touched = set()
        self._loading = False
        self._purpose = None

        self._ui.dialogContent.layout().addWidget(self._transformWidget)

        # R45. The dialog offered a name and a type and no way to tell which
        # surface it was editing: after a split the five parts arrive as
        # `tee_1`..`tee_5`, and the bounds that identify them could only be
        # read out of rev2.stl from outside the application. Sits outside
        # dialogContent so it stays readable when the page is locked and the
        # editable content is greyed out.
        self._measurements = QLabel(self)
        self._measurements.setObjectName('measurements')
        self._measurements.setWordWrap(True)
        self._measurements.hide()
        self.layout().insertWidget(1, self._measurements)

        self._connectSignalsSlots()
        if app.themeManager is not None:
            app.themeManager.themeChanged.connect(self._applyTheme)

    def _applyTheme(self, _name):
        if app.themeManager is None or app.themeManager.tokens is None:
            return
        colour = rgb(app.themeManager.tokens.value('accent.default'))
        for actor in self._actors:
            actor.GetProperty().SetEdgeColor(*colour)
        self._renderingView.refresh()

    def gIds(self):
        return self._gIds

    def setData(self, gIds, sources):
        self._gIds = gIds
        self._sources = sources
        self._load()

    def disableEdit(self):
        self._ui.dialogContent.setEnabled(False)
        self._ui.ok.hide()
        self._ui.cancel.setText(self.tr('Close'))
        self._editable = False

    def done(self, result):
        for actor in self._actors:
            self._renderingView.removeActor(actor)

        self._renderingView.refresh()

        super().done(result)

    @qasync.asyncSlot()
    async def _accept(self):
        # One click, one commit: the write queue may hold this for
        # seconds on a cold machine, and a second OK in that window
        # submitted the same element twice.
        with commit_guard(self._ui.ok):
            if self._automatic():
                await self._acceptAutomatic()
                return
            try:
                # Plan 49 SR2. An edit the model cannot hold is refused
                # before anything is written, the rename included.
                try:
                    authoring.plan_edit(app.facadeClient.checkout().data(),
                                        self._gIds, self._edit())
                except authoring.EditRefused as error:
                    inline_error.show(self, str(error))
                    return
                if len(self._gIds) == 1:
                    # The name lives in two stores - the tree row and the
                    # artifact's patch manifest - and geometry.rename is the
                    # only thing that moves both. Running it before the
                    # working copy is taken keeps a refused name free: nothing
                    # else has been written yet.
                    try:
                        await app.facadeClient.run('geometry.rename', {
                            'geometry_id': self._gIds[0],
                            'name': self._ui.name.text()})
                    except ValidationFailedError as error:
                        inline_error.show(self, str(error))
                        return

                db = app.facadeClient.checkout()

                # Planned against the copy that is committed, so a rename
                # that just landed is what the relation labels read.
                try:
                    plan = authoring.plan_edit(
                        db.data(), self._gIds, self._edit())
                except authoring.EditRefused as error:
                    inline_error.show(self, str(error))
                    return
                authoring.write_plan(db, plan)

                if self._transformed:
                    for gId, polyData in self._sources.items():
                        surface = db.getElement('geometry', gId)
                        db.updateGeometryPolyData(surface.value('path'), polyData)

                await app.facadeClient.commit_working_copy(db, action='edit surface')
                if self._transformed:
                    await self._transformArtifacts(db)

                super().accept()
            except CONFLICT_ERRORS as error:
                # Stay open: the user's entries are still here, and the only
                # thing that changed is what the case looked like underneath.
                await AsyncMessageBox().warning(
                    self, self.tr('Case changed'), self.tr(conflict_message(error)))
            except ValidationError as e:
                inline_error.show(self, e.toMessage())
            except FacadeError as error:
                # DP-6078. A locked step (TaskLockedError) escaped to the event
                # loop's error handler: OK did nothing and nothing said why.
                # MEASURED on the packaged build 2026-10-26 on a meshed snappy
                # case. The dialog stays open, the entries kept, the reason shown.
                inline_error.show(self, self.tr('Not saved: {0}').format(error))

    async def _acceptAutomatic(self):
        """Rename if asked, then pair the touching walls (DP-6087)."""
        try:
            if len(self._gIds) == 1:
                current = app.facadeClient.checkout().getElement(
                    'geometry', self._gIds[0])
                if current is not None and \
                        current.value('name') != self._ui.name.text():
                    await app.facadeClient.run('geometry.rename', {
                        'geometry_id': self._gIds[0],
                        'name': self._ui.name.text()})
            await app.facadeClient.run('geometry.surface_intent.auto_pair', {
                'surface_role': self._role(),
                'geometry_ids': self._keys()})
        except CONFLICT_ERRORS as error:
            await AsyncMessageBox().warning(
                self, self.tr('Case changed'), self.tr(conflict_message(error)))
            return
        except FacadeError as error:
            inline_error.show(self, self.tr('Not paired: {0}').format(error))
            return
        super().accept()

    def _connectSignalsSlots(self):
        self._ui.role.currentIndexChanged.connect(self._onRoleChanged)
        self._pairing.currentIndexChanged.connect(self._onPairingChanged)
        self._ui.purpose.currentIndexChanged.connect(self._onPurposeChanged)
        self._ui.patchType.currentIndexChanged.connect(self._onIntentEdited)
        self._ui.partner.currentIndexChanged.connect(self._onPartnerChanged)
        self._ui.ownRegion.currentIndexChanged.connect(self._onIntentEdited)
        self._ui.partnerRegion.currentIndexChanged.connect(
            self._onIntentEdited)
        self._transformWidget.transformed.connect(self._onTransformed)
        self._ui.ok.clicked.connect(self._accept)
        self._ui.cancel.clicked.connect(self.close)

    def _load(self):
        surfaces = app.facadeClient.checkout().getElements(
            'geometry', lambda i, e: i in self._gIds)

        first = surfaces[self._gIds[0]]
        if len(surfaces) > 1:
            self._ui.nameSetting.hide()
            self._transformWidget.hide()
        else:
            self._ui.name.setText(first.value('name'))
            # R45/R146. The preview is what tells the user which anonymous
            # split part they are naming, so it is drawn whenever a source
            # mesh was handed over -- not only for a top-level surface, which
            # is the one case that also gets the transform widget.
            if self._sources:
                self._displayPreview()
                self._showMeasurements()
            if first.value('volume') is None and self._editable:
                self._transformWidget.setMeshes(self._sources)
            else:
                self._transformWidget.hide()

        self._loadIntent()

    async def _transformArtifacts(self, db):
        """Apply the transform to the artifacts the mesher reads, too.

        The dialog moved the surfaces on screen and in the geometry database;
        the artifact store, which is what Gmsh meshes and what the snappy
        surfaces are written from, kept the untransformed copy, so the mesh
        came out where the geometry used to be. Artifacts are matched to the
        edited surfaces by the name they were imported under.
        """
        from foammesh.core.facade.errors import FacadeError

        operations = self._transformWidget.operations()
        project = getattr(app, 'project', None)
        if not operations or project is None or getattr(project, 'path', None) is None:
            return
        try:
            from foammesh.core.geometry import GeometryArtifactStore
            entries = GeometryArtifactStore(project.path).entries()
        except Exception:  # noqa: BLE001 - no artifact store, nothing to keep in step
            return

        names = set()
        for gId in self._sources:
            element = db.getElement('geometry', gId)
            names.add(_plainName(element.value('name')))
            volumeId = element.value('volume')
            if volumeId is not None:
                names.add(_plainName(db.getElement('geometry', volumeId).value('name')))

        for entry in entries:
            artifact = _plainName(entry.get('name'))
            if not any(name == artifact or (name.startswith(artifact)
                                            and name[len(artifact):].isdigit())
                       for name in names):
                continue
            try:
                await app.facadeClient.run('geometry.transform', {
                    'geometry_id': entry['geometry_id'], 'operations': operations})
            except FacadeError as error:
                await AsyncMessageBox().warning(
                    self, self.tr('Transform'),
                    self.tr('The mesher\'s copy of {0} was not transformed: {1}')
                    .format(entry.get('name', ''), error))
                return

    def _showMeasurements(self):
        """Say how big the surface being edited is and where it sits.

        Silently stays hidden when the source is not measurable: a readout is
        a courtesy and must never stop the dialog from opening.
        """
        polyData = next(iter(self._sources.values()), None)
        text = format_measurements(surface_measurements(polyData))
        if not text:
            self._measurements.hide()
            return

        self._measurements.setText(text)
        self._measurements.show()

    def _onTransformed(self):
        self._transformed = True
        self._sources = self._transformWidget.meshes()
        self._displayPreview()

    # -- Plan 49 SR2: role, purpose, mesh type and partner ---------------- #

    def _keys(self):
        return [str(gId) for gId in self._gIds]

    # -- DP-6087: Automatic or Manual pairing ----------------------------- #

    def _mountPairing(self):
        """Put the Automatic/Manual choice right under the role.

        DP-6087 (user design, 2026-10-29). Split interfaces was a Geometry
        tab button a person had to know to press first, and choosing the
        other side by hand offered only the bodies already seeded. The choice
        now sits where the interface type is chosen: Automatic measures which
        walls of other bodies touch this one and pairs them (conformal: cut
        into one shared surface; non-conformal: the two walls kept), Manual
        is the partner and regions as before.
        """
        layout = self._ui.typeGroupLayout
        self._pairingLabel = QLabel(self.tr('Pairing'), self._ui.typeGroup)
        self._pairing = QComboBox(self._ui.typeGroup)
        self._pairing.setObjectName('pairing')
        for mode, text in PAIRING_TEXT:
            self._pairing.addItem(self.tr(text), mode)
        layout.insertRow(1, self._pairingLabel, self._pairing)
        self._autoNote = QLabel(self._ui.typeGroup)
        self._autoNote.setObjectName('autoPairNote')
        self._autoNote.setWordWrap(True)
        layout.insertRow(2, self._autoNote)
        #: The last preview the facade answered, for OK and the tests.
        self._autoPreview = None
        self._previewToken = 0

    def _automatic(self):
        return (self._role() in intent.INTERFACE_ROLES
                and self._pairing.currentData() == AUTOMATIC)

    def _setRowVisible(self, widget, visible):
        layout = self._ui.typeGroupLayout
        row, _role = layout.getWidgetPosition(widget)
        if row >= 0:
            layout.setRowVisible(row, visible)

    def _onPairingChanged(self, _index):
        if self._loading:
            return
        self._touch('pairing')
        self._updateRows()
        self._onIntentEdited()

    @qasync.asyncSlot()
    async def _refreshAutoPreview(self):
        """Say which walls Automatic would pair, before anything is written."""
        self._previewToken += 1
        token = self._previewToken
        self._autoNote.setText(self.tr('Looking for the walls this surface '
                                       'touches...'))
        try:
            result = await app.facadeClient.run(
                'geometry.surface_intent.auto_pair', {
                    'surface_role': self._role(),
                    'geometry_ids': self._keys(), 'preview': True})
        except FacadeError as error:
            if token == self._previewToken:
                self._autoPreview = None
                self._autoNote.setText(str(error))
                self._autoOk()
            return
        if token != self._previewToken:
            return
        self._autoPreview = dict(result.payload or {})
        self._autoNote.setText(self._autoText(self._autoPreview))
        self._autoOk()

    def _autoText(self, payload):
        lines = []
        for item in payload.get('contacts') or ():
            first, second = item.get('names') or ('', '')
            if payload.get('surface_role') == intent.CONFORMAL:
                lines.append(self.tr('{0} touches {1}: the shared triangles '
                                     'become one interface surface.')
                             .format(first, second))
            else:
                lines.append(self.tr('{0} touches {1}: the two are paired, '
                                     'each body its own side.')
                             .format(first, second))
        for item in payload.get('skipped') or ():
            lines.append(self.tr('Not paired: {0}. {1}').format(
                item.get('reason'), item.get('remedy')))
        if not lines:
            lines.append(self.tr('No surface of another body touches this '
                                 'one. Choose Manual to pair it by hand.'))
        return '\n'.join(lines)

    def _autoOk(self):
        if self._editable:
            self._ui.ok.setEnabled(
                bool((self._autoPreview or {}).get('contacts')))

    def _loadIntent(self):
        """Fill the controls from the stored model, then show its issues.

        A value the selected surfaces do not share is shown blank and left
        as it is unless the person picks one.
        """
        document = app.facadeClient.checkout().data()
        self._document = copy.deepcopy(document) if document is not None \
            else {}
        rows = [authoring._row(self._document, key) or {}
                for key in self._keys()]

        def shared(field):
            values = {intent._text(row.get(field)) for row in rows}
            return values.pop() if len(values) == 1 else None

        self._loading = True
        try:
            self._setData(self._ui.role, shared('surfaceRole'))
            category = shared('boundaryCategory')
            self._fillPurposes(category)
            self._setData(self._ui.purpose, category)
            self._purpose = category
            self._setData(self._ui.patchType, shared('meshPatchType'))
            self._fillPartners()
            self._fillRegions()
            if len(self._gIds) == 1:
                key = self._keys()[0]
                _relationKey, relation = authoring.interface_relation(
                    self._document, key)
                self._setData(self._pairing,
                              MANUAL if relation is not None else AUTOMATIC)
                if relation is not None:
                    self._setData(self._ui.partner,
                                  authoring.partner_of(relation, key))
                    own, other = authoring.regions_of(relation, key)
                    self._setData(self._ui.ownRegion, own)
                    self._setData(self._ui.partnerRegion, other)
                else:
                    self._setData(self._ui.partner, '')
                    self._setData(self._ui.ownRegion,
                                  authoring.zone_region(self._document, key))
            # Nothing the person has touched yet: every control keeps.
            self._touched = set()
        finally:
            self._loading = False
        self._updateRows()
        self._showIssues(authoring.all_issues(self._document, self._keys()))

    @staticmethod
    def _setData(combo, value):
        index = combo.findData(value) if value not in (None, '') else -1
        if index < 0 and value == '':
            index = combo.findData('')
        combo.setCurrentIndex(index)

    def _fillPurposes(self, stored):
        self._ui.purpose.clear()
        purposes = list(authoring.PURPOSES)
        if stored and stored not in purposes:
            purposes.append(stored)  # a legacy value migration stored
        for purpose in purposes:
            self._ui.purpose.addItem(
                self.tr(PURPOSE_TEXT.get(purpose, purpose)), purpose)

    def _fillPartners(self):
        combo = self._ui.partner
        combo.clear()
        combo.addItem('', '')
        combo.addItem(self.tr('This surface (both sides)'), authoring.SHARED)
        if len(self._gIds) != 1:
            return
        mine = self._keys()[0]
        for key, row in intent._sorted_items(
                (self._document or {}).get('geometry')):
            if str(key) == mine or not authoring._is_surface(row):
                continue
            combo.addItem(intent._text(row.get('name')) or str(key), str(key))

    def _fillRegions(self):
        for combo in (self._ui.ownRegion, self._ui.partnerRegion):
            combo.clear()
            combo.addItem('', '')
            for reference, label in authoring.region_choices(self._document):
                text = (self.tr('Surrounding')
                        if reference == intent.SURROUNDING_REGION else label)
                combo.addItem(text, reference)

    def _role(self):
        role = self._ui.role.currentData()
        if role is None and self._document is not None:
            values = {intent._text((authoring._row(self._document, key) or {})
                                   .get('surfaceRole'))
                      for key in self._keys()}
            role = values.pop() if len(values) == 1 else None
        return role or None

    def _updateRows(self):
        """Show the controls the role uses, and only those."""
        role = self._role()
        external = role == intent.EXTERNAL
        isInterface = role in intent.INTERFACE_ROLES
        # Several surfaces at once are paired only automatically.
        if isInterface and len(self._gIds) != 1:
            loading, self._loading = self._loading, True
            try:
                self._setData(self._pairing, AUTOMATIC)
            finally:
                self._loading = loading
        self._pairing.setEnabled(len(self._gIds) == 1)
        automatic = self._automatic()
        interface = isInterface and len(self._gIds) == 1 and not automatic
        ui = self._ui
        for widget, visible in ((ui.purpose, external),
                                (ui.patchType, external),
                                (self._pairing, isInterface),
                                (self._autoNote, automatic),
                                (ui.partner, interface),
                                (ui.ownRegion, interface),
                                (ui.partnerRegion, interface)):
            self._setRowVisible(widget, visible)
        # One surface is both sides only of a conformal interface.
        shared = self._ui.partner.findData(authoring.SHARED)
        if shared >= 0:
            item = self._ui.partner.model().item(shared)
            if item is not None:
                item.setEnabled(role == intent.CONFORMAL)

    def _edit(self):
        """The edit the controls ask for; an untouched mixed control keeps."""
        def value(combo, name):
            if name not in self._touched \
                    and combo.currentIndex() < 0:
                return authoring.KEEP
            if name not in self._touched:
                return authoring.KEEP
            data = combo.currentData()
            return None if data in (None, '') else data

        edit = authoring.SurfaceEdit()
        if not self._touched:
            # OK on an untouched dialog renames at most: a surface's
            # inferred purpose is not turned into an authored one unseen.
            return edit
        role = self._ui.role.currentData()
        if role:
            edit.role = role
        if role == intent.EXTERNAL:
            edit.category = value(self._ui.purpose, 'purpose')
            edit.patch_type = value(self._ui.patchType, 'patchType')
        if role in intent.INTERFACE_ROLES and len(self._gIds) == 1:
            edit.partner = value(self._ui.partner, 'partner')
            edit.own_region = value(self._ui.ownRegion, 'ownRegion')
            edit.partner_region = value(self._ui.partnerRegion,
                                        'partnerRegion')
        return edit

    def _touch(self, name):
        if not self._loading:
            self._touched = self._touched | {name}

    def _onRoleChanged(self, _index):
        self._touch('role')
        if not self._loading:
            role = self._ui.role.currentData()
            if role == intent.EXTERNAL and self._ui.purpose.currentIndex() < 0 \
                    and len(self._gIds) == 1:
                # The purpose and type this surface would take, shown so the
                # person sees what OK writes.
                row = authoring._row(self._document, self._keys()[0]) or {}
                category = intent._text(row.get('boundaryCategory')) or \
                    intent.category_for_name(
                        intent._text(row.get('name')),
                        intent.default_category(self._document))
                self._loading = True
                try:
                    self._setData(self._ui.purpose, category)
                    self._setData(self._ui.patchType,
                                  intent._text(row.get('meshPatchType'))
                                  or authoring.default_patch_type(category))
                finally:
                    self._loading = False
        self._updateRows()
        self._onIntentEdited()

    def _onPurposeChanged(self, _index):
        if self._loading:
            return
        previous = self._purpose
        self._touch('purpose')
        category = self._ui.purpose.currentData()
        current = self._ui.patchType.currentData()
        # The type follows the purpose unless it was chosen apart from it.
        if current in (None, authoring.default_patch_type(previous)):
            self._loading = True
            try:
                self._setData(self._ui.patchType,
                              authoring.default_patch_type(category))
            finally:
                self._loading = False
            self._touched.add('patchType')
        self._purpose = category
        self._onIntentEdited()

    def _onPartnerChanged(self, _index):
        if self._loading:
            return
        self._touch('partner')
        partner = self._ui.partner.currentData()
        if partner and self._pairing.currentData() != MANUAL:
            # A partner chosen is a pairing made by hand.
            self._loading = True
            try:
                self._setData(self._pairing, MANUAL)
            finally:
                self._loading = False
            self._touch('pairing')
            self._updateRows()
        if partner and 'partnerRegion' not in self._touched:
            region = (intent.SURROUNDING_REGION if partner == authoring.SHARED
                      else authoring.zone_region(self._document, partner))
            self._loading = True
            try:
                self._setData(self._ui.partnerRegion, region)
            finally:
                self._loading = False
            self._touched.add('partnerRegion')
        self._onIntentEdited()

    def _onIntentEdited(self, *_args):
        """Validate as the person edits: refusals block OK, drafts are shown."""
        if self._loading or self._document is None:
            return
        sender = self.sender()
        for name in ('patchType', 'ownRegion', 'partnerRegion'):
            if sender is getattr(self._ui, name):
                self._touch(name)
        if self._automatic():
            # Nothing is planned by hand: the facade measures the contacts.
            # The role itself may still be one the surface cannot take (a
            # periodic side); that refusal stands in either mode.
            try:
                authoring.preview(self._document, self._keys(),
                                  authoring.SurfaceEdit(role=self._role()))
            except authoring.EditRefused as error:
                self._refusal = str(error)
                self._ui.intentIssue.setText(self._refusal)
                self._ui.intentIssue.show()
                self._fitToIssue()
                self._autoNote.clear()
                if self._editable:
                    self._ui.ok.setEnabled(False)
                return
            self._refusal = None
            self._showIssues(())
            self._autoPreview = None
            if self._editable:
                self._ui.ok.setEnabled(False)
            self._refreshAutoPreview()
            return
        try:
            found = authoring.preview(self._document, self._keys(),
                                      self._edit())
        except authoring.EditRefused as error:
            self._refusal = str(error)
            self._ui.intentIssue.setText(self._refusal)
            self._ui.intentIssue.show()
            self._fitToIssue()
            if self._editable:
                self._ui.ok.setEnabled(False)
            return
        self._refusal = None
        if self._editable:
            self._ui.ok.setEnabled(True)
        self._showIssues(found)

    def _showIssues(self, found):
        """One plain line per unresolved surface: which, why, what to do."""
        text = '\n'.join(issue.text() for issue in found)
        self._ui.intentIssue.setText(text)
        self._ui.intentIssue.setVisible(bool(text))
        self._fitToIssue()

    def _fitToIssue(self):
        """Grow the dialog until the whole note under the controls shows.

        DP-6079. The note is a word-wrapped label, and a dialog that was
        already open kept its height when a long refusal arrived: the
        symmetryPlane line was cut off after "and OpenFOAM" (MEASURED on the
        packaged build 2026-10-26).
        """
        label = self._ui.intentIssue
        # The label sits in a group box whose layout does not pass the wrap
        # height up, so it is asked for outright: the dialog then grows to
        # the label's minimum.
        label.setMinimumHeight(0)
        if label.isVisibleTo(self) and label.width() > 0:
            label.setMinimumHeight(label.heightForWidth(label.width()))

    def _displayPreview(self):
        for actor in self._actors:
            self._renderingView.removeActor(actor)

        self._actors.clear()

        for polyData in self._sources.values():
            actor = polyDataToActor(polyData)
            actor.GetProperty().SetRepresentationToSurface()
            actor.GetProperty().EdgeVisibilityOn()
            actor.GetProperty().SetLineWidth(1.0)
            actor.GetProperty().SetDiffuse(0.6)
            edge = (app.themeManager.tokens.value('accent.default')
                    if app.themeManager is not None and app.themeManager.tokens is not None
                    else '#00a6d6')
            actor.GetProperty().SetEdgeColor(*rgb(edge))
            actor.GetProperty().SetLineWidth(2)
            self._actors.append(actor)
            self._renderingView.addActor(actor)

        self._renderingView.refresh()
