#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtWidgets import QDialog, QLabel

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.core.facade.errors import ValidationFailedError
from foammesh.view.widgets.commit_guard import (CONFLICT_ERRORS, commit_guard,
                                                conflict_message)
from widgets.async_message_box import AsyncMessageBox
from widgets.enum_button_group import EnumButtonGroup

from foammesh.app import app
from foammesh.core.geometry.measure import (format_measurements,
                                             surface_measurements)
from foammesh.db.configurations_schema import CFDType
from foammesh.rendering.vtk_loader import polyDataToActor
from foammesh.view.theming.vtk_theme import rgb
from .surface_dialog_ui import Ui_SurfaceDialog
from .transform_widget import TransformWidget


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
        self._typeRadios = EnumButtonGroup()

        self._transformWidget = TransformWidget(self)

        self._gIds = None
        self._dbElement = None

        self._sources = None
        self._actors = []

        self._editable = True
        self._transformed = False

        self._typeRadios.addEnumButton(self._ui.none,       CFDType.NONE)
        self._typeRadios.addEnumButton(self._ui.boundary,   CFDType.BOUNDARY)
        self._typeRadios.addEnumButton(self._ui.interface_, CFDType.INTERFACE)

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
            try:
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
                        await AsyncMessageBox().information(
                            self, self.tr('Input Error'), str(error))
                        return

                db = app.facadeClient.checkout()

                for gId in self._gIds:
                    element = db.checkout(f'geometry/{gId}')

                    cfdType = self._typeRadios.checkedData()
                    if element.setValue('cfdType', cfdType):
                        if cfdType != CFDType.INTERFACE:
                            element.setValue('slaveLayerGroup', None)
                            if cfdType != CFDType.BOUNDARY:
                                element.setValue('layerGroup', None)

                    element.setValue('nonConformal', self._ui.nonConformal.isChecked())
                    element.setValue('interRegion', self._ui.interRegion.isChecked())

                    db.commit(element)

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
                await AsyncMessageBox().information(
                    self, self.tr('Case Changed'), self.tr(conflict_message(error)))
            except ValidationError as e:
                await AsyncMessageBox().information(self, self.tr("Input Error"), e.toMessage())

    def _connectSignalsSlots(self):
        self._typeRadios.selectionChanged.connect(self._onTypeChanged)
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

        cfdType = CFDType(first.value('cfdType'))
        nonConformal = None
        interRegion = None
        self._typeRadios.setCheckedData(cfdType)
        if cfdType == CFDType.INTERFACE:
            nonConformal = first.value('nonConformal')
            interRegion = first.value('interRegion')
            self._ui.nonConformal.setChecked(nonConformal)
            self._ui.interRegion.setChecked(interRegion)

        for gId, s in surfaces.items():
            if cfdType.value != s.value('cfdType'):
                self._typeRadios.setCheckedData(CFDType.BOUNDARY)
                break

            if (cfdType == CFDType.INTERFACE
                    and (nonConformal != s.value('nonConformal') or interRegion != s.value('interRegion'))):
                self._typeRadios.setCheckedData(CFDType.BOUNDARY)
                break

        self._onTypeChanged(self._typeRadios.checkedData())

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
                await AsyncMessageBox().information(
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

    def _onTypeChanged(self, value):
        self._ui.interfaceType.setEnabled(value == CFDType.INTERFACE)

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
