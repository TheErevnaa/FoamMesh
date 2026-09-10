#!/usr/bin/env python
# -*- coding: utf-8 -*-

import qasync
from PySide6.QtWidgets import QDialog, QMessageBox
from PySide6.QtCore import QEvent, QTimer

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.view.widgets.commit_guard import (CONFLICT_ERRORS, commit_guard,
                                                conflict_message)
from widgets.async_message_box import AsyncMessageBox
from widgets.radio_group import RadioGroup

from foammesh.app import app
from foammesh.db.configurations_schema import Shape, GeometryType, CFDType
from foammesh.rendering.vtk_loader import (hexPolyData, cylinderPolyData, spherePolyData, polyDataToActor,
                                           planePolyData, diskPolyData, openPlatePolyData)
from foammesh.view.theming.vtk_theme import rgb
from .geometry import RESERVED_NAMES
from .open_surface_page import OpenSurfacePage
from .transform_widget import TransformWidget
from .volume_dialog_ui import Ui_VolumeDialog


BASE_NAMES = {
    Shape.HEX:      'Hex_',
    Shape.CYLINDER: 'Cylinder_',
    Shape.SPHERE:   'Sphere_',
    Shape.HEX6:     'Hex6_',
    # Plan 31. OpenFOAM 13's three open searchable surfaces. They are volume
    # *rows* like the others -- one row, one geometry entry, one child surface
    # -- but the surface they describe encloses nothing, which is why the
    # writer sends them to refinementSurfaces and not to refinementRegions.
    Shape.PLANE:    'Plane_',
    Shape.DISK:     'Disk_',
    Shape.PLATE:    'Plate_',
}

#: The shapes served by ``OpenSurfacePage`` rather than by a Designer page.
OPEN_SURFACE_SHAPES = (Shape.PLANE, Shape.DISK, Shape.PLATE)


def showStackPage(stack, page):
    for i in range(stack.count()):
        widget = stack.widget(i)
        if widget.objectName() == page:
            stack.setCurrentIndex(i)
        else:
            widget.hide()

    stack.adjustSize()


class VolumeDialog(QDialog):
    _cfdTypes = {
        'none': CFDType.NONE.value,
        'cellZone': CFDType.CELL_ZONE.value
    }

    def __init__(self, parent, renderingView):
        super().__init__(parent)
        self._ui = Ui_VolumeDialog()
        self._ui.setupUi(self)

        self._renderingView = renderingView
        self._typeRadios = None

        self._transformWidget = TransformWidget(self)

        self._gId = None
        self._shape = None
        #: Plan 31. Shape -> the ``OpenSurfacePage`` built for it, created on
        #: first use so a dialog opened for a hex builds nothing extra.
        self._openSurfacePages = {}

        self._dbElement = None
        self._creationMode = True

        self._sources = None
        self._actors = []

        self._editable = True
        self._transformed = False

        self._ui.dialogContent.layout().addWidget(self._transformWidget)

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

    def gId(self):
        return self._gId

    def isForCreation(self):
        return self._creationMode

    def setupForAdding(self, shape):
        self.setWindowTitle(self.tr('Add Volume'))

        self._creationMode = True
        self._gId = None
        self._sources = {}
        self._dbElement = app.facadeClient.checkout().newElement('geometry')
        self._shape = shape

        self._dbElement.setValue('shape', shape)

        self._transformWidget.hide()

        self._load()
        self._previewCustomVolume()

        self.adjustSize()

    def setupForEdit(self, gId, sources):
        self.setWindowTitle(self.tr('Edit Volume'))

        self._creationMode = False
        self._gId = gId
        self._sources = sources
        self._dbElement = app.facadeClient.checkout(f'geometry/{gId}')
        self._shape = Shape(self._dbElement.getValue('shape'))

        self._load()

        self.adjustSize()

    def disableEdit(self):
        self._ui.dialogContent.setEnabled(False)
        self._ui.ok.hide()
        self._ui.cancel.setText(self.tr('Close'))
        self._editable = False

    def event(self, ev):
        if ev.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self.adjustSize)

        return super().event(ev)

    def done(self, result):
        for actor in self._actors:
            self._renderingView.removeActor(actor)

        self._renderingView.refresh()

        super().done(result)

    def _connectSignalsSlots(self):
        self._transformWidget.transformed.connect(self._onTransformed)
        self._ui.preview.clicked.connect(self._previewCustomVolume)
        self._ui.ok.clicked.connect(self._accept)
        self._ui.cancel.clicked.connect(self.close)

    def _load(self):
        name = self._dbElement.getValue('name')
        if not name:
            baseName = BASE_NAMES[self._shape]
            name = f"{baseName}{app.facadeClient.checkout().getUniqueSeq('geometry', 'name', baseName, 1)}"

        self._ui.name.setText(name)

        self._typeRadios = RadioGroup(self._ui.typeRadios)
        self._typeRadios.setObjectMap(self._cfdTypes, self._dbElement.getValue('cfdType'))

        if self._shape == Shape.TRI_SURFACE_MESH:
            self._ui.geometryStack.hide()
            self._ui.preview.hide()
            if self._editable:
                self._displayPreview()
                self._transformWidget.setMeshes(self._sources)
            else:
                self._transformWidget.hide()
        else:
            self._ui.preview.setVisible(self._editable)
            self._transformWidget.hide()

            if self._shape == Shape.HEX or self._shape == Shape.HEX6:
                self._loadHexPage()
            elif self._shape == Shape.SPHERE:
                self._loadSpherePage()
            elif self._shape == Shape.CYLINDER:
                self._loadCylinderpage()
            elif self._shape in OPEN_SURFACE_SHAPES:
                self._loadOpenSurfacePage()
                self._forbidCellZone()

    def _forbidCellZone(self):
        """An open surface cannot become a cell zone, so do not offer it.

        Plan 31. A cell zone is "the cells inside this shape", and OpenFOAM
        answers that question with ``hasVolumeType()`` -- which ``plane``,
        ``disk`` and ``plate`` all answer no to. Leaving the radio enabled
        would let a user ask for a zone that the case can never produce, and
        they would only find out when the meshing run came back without it.
        """
        self._ui.cellZone.setEnabled(False)
        self._ui.cellZone.setToolTip(
            self.tr('A plane, disk or plate encloses nothing, so there are no '
                    'cells inside it to collect into a zone. Use a hex, '
                    'sphere, cylinder or closed surface for a cell zone.'))
        self._ui.none.setChecked(True)

    def _openSurfacePage(self):
        """The stack page for the current open surface, built once.

        Plan 31. The page is added to the same ``geometryStack`` the Designer
        pages live in, so ``showStackPage`` and everything else in this dialog
        goes on working the way it did -- there is one stack, not two.
        """
        page = self._openSurfacePages.get(self._shape)
        if page is None:
            page = OpenSurfacePage(self._shape, self._ui.geometryStack)
            self._ui.geometryStack.addWidget(page)
            self._openSurfacePages[self._shape] = page
        return page

    def _loadOpenSurfacePage(self):
        page = self._openSurfacePage()
        page.setValues(self._dbElement.getVector('point1'),
                       self._dbElement.getVector('point2'),
                       self._dbElement.getValue('radius'))
        showStackPage(self._ui.geometryStack, self._shape.value)

    def _loadHexPage(self):
        showStackPage(self._ui.geometryStack, 'hex')

        x1, y1, z1 = self._dbElement.getVector('point1')
        x2, y2, z2 = self._dbElement.getVector('point2')
        self._ui.minX.setText(x1)
        self._ui.minY.setText(y1)
        self._ui.minZ.setText(z1)
        self._ui.maxX.setText(x2)
        self._ui.maxY.setText(y2)
        self._ui.maxZ.setText(z2)

    def _loadSpherePage(self):
        showStackPage(self._ui.geometryStack, 'sphere')

        x, y, z = self._dbElement.getVector('point1')
        self._ui.centerX.setText(x)
        self._ui.centerY.setText(y)
        self._ui.centerZ.setText(z)

        self._ui.sphereRadius.setText(self._dbElement.getValue('radius'))

    def _loadCylinderpage(self):
        showStackPage(self._ui.geometryStack, 'cylinder')

        x1, y1, z1 = self._dbElement.getVector('point1')
        x2, y2, z2 = self._dbElement.getVector('point2')
        self._ui.axis1X.setText(x1)
        self._ui.axis1Y.setText(y1)
        self._ui.axis1Z.setText(z1)
        self._ui.axis2X.setText(x2)
        self._ui.axis2Y.setText(y2)
        self._ui.axis2Z.setText(z2)

        self._ui.cylinderRadius.setText(self._dbElement.getValue('radius'))

        self._ui.annulusRadius.hide()

    async def _updateElement(self):
        name = self._ui.name.text()

        if name in RESERVED_NAMES:
            await AsyncMessageBox().information(
                self, self.tr('Input Error'), self.tr('"{0}" is an invalid geometry name.').format(name))
            return

        if name.find(' ') > -1:
            await AsyncMessageBox().information(
                self, self.tr('Input Error'), self.tr('Geometry name cannot contain spaces'))
            return

        if app.facadeClient.checkout().getKeys(
                'geometry', lambda i, e: e['name'] == name and i != self._gId):
            await AsyncMessageBox().information(self, self.tr('Input Error'),
                                                self.tr('geometry "{0}" already exists.').format(name))
            return False

        self._dbElement.setValue('gType', GeometryType.VOLUME.value)
        self._dbElement.setValue('name', name)
        self._dbElement.setValue('cfdType', self._typeRadios.value())

        if self._shape == Shape.HEX or self._shape == Shape.HEX6:
            return self._updateHexData()
        elif self._shape == Shape.SPHERE:
            return self._updateSphereData()
        elif self._shape == Shape.CYLINDER:
            return self._updateCylinderData()
        elif self._shape in OPEN_SURFACE_SHAPES:
            return self._updateOpenSurfaceData()
        elif self._transformed:     # triSurfaceMesh transformed
            for gId, polyData in self._sources.items():
                surface = app.facadeClient.checkout().getElement('geometry', gId)
                self._dbElement.updateGeometryPolyData(surface.value('path'), polyData)

        return True

    def _updateHexData(self):
        if not self._validateHex():
            QMessageBox.information(self, self.tr('Add Geometry Failed'), self.tr('Invalid coordinates'))
            return False

        self._dbElement.setValue('point1/x', self._ui.minX.text(), self.tr('Minimum X'))
        self._dbElement.setValue('point1/y', self._ui.minY.text(), self.tr('Minimum Y'))
        self._dbElement.setValue('point1/z', self._ui.minZ.text(), self.tr('Minimum Z'))
        self._dbElement.setValue('point2/x', self._ui.maxX.text(), self.tr('Maximum X'))
        self._dbElement.setValue('point2/y', self._ui.maxY.text(), self.tr('Maximum Y'))
        self._dbElement.setValue('point2/z', self._ui.maxZ.text(), self.tr('Maximum Z'))

        return True

    def _updateCylinderData(self):
        self._dbElement.setValue('point1/x', self._ui.axis1X.text(), self.tr('Axis Point1 X'))
        self._dbElement.setValue('point1/y', self._ui.axis1Y.text(), self.tr('Axis Point1 Y'))
        self._dbElement.setValue('point1/z', self._ui.axis1Z.text(), self.tr('Axis Point1 Z'))
        self._dbElement.setValue('point2/x', self._ui.axis2X.text(), self.tr('Axis Point2 X'))
        self._dbElement.setValue('point2/y', self._ui.axis2Y.text(), self.tr('Axis Point2 Y'))
        self._dbElement.setValue('point2/z', self._ui.axis2Z.text(), self.tr('Axis Point2 Z'))
        self._dbElement.setValue('radius', self._ui.cylinderRadius.text(), self.tr('radius'))

        return True

    def _updateOpenSurfaceData(self):
        """Store the two vectors -- and, for a disk, the radius.

        Plan 31. The same ``point1``/``point2`` pair the box and the cylinder
        already use: for a plane it is point and normal, for a disk origin and
        normal, for a plate origin and span. The writer knows which is which
        from the shape, so nothing has to be stored twice.
        """
        page = self._openSurfacePage()
        first, second, third = ('Point', 'Normal', None)
        if self._shape == Shape.DISK:
            first, second, third = 'Origin', 'Normal', 'Radius'
        elif self._shape == Shape.PLATE:
            first, second = 'Origin', 'Span'

        for axis, value in zip('xyz', page.first()):
            self._dbElement.setValue(f'point1/{axis}', value,
                                     self.tr(f'{first} {axis.upper()}'))
        for axis, value in zip('xyz', page.second()):
            self._dbElement.setValue(f'point2/{axis}', value,
                                     self.tr(f'{second} {axis.upper()}'))
        if third is not None:
            self._dbElement.setValue('radius', page.radius(), self.tr(third))

        return True

    def _updateSphereData(self):
        self._dbElement.setValue('point1/x', self._ui.centerX.text(), self.tr('Center X'))
        self._dbElement.setValue('point1/y', self._ui.centerY.text(), self.tr('Center Y'))
        self._dbElement.setValue('point1/z', self._ui.centerZ.text(), self.tr('Center Z'))
        self._dbElement.setValue('radius', self._ui.sphereRadius.text(), self.tr('radius'))

        return True

    def _onTransformed(self):
        self._transformed = True
        self._sources = self._transformWidget.meshes()
        self._displayPreview()

    @qasync.asyncSlot()
    async def _previewCustomVolume(self):
        polyData = None
        try:
            if self._shape == Shape.HEX or self._shape == Shape.HEX6:
                if points := self._validateHex():
                    polyData = hexPolyData(*points)
            elif self._shape == Shape.CYLINDER:
                polyData = cylinderPolyData(
                    (float(self._ui.axis1X.text()), float(self._ui.axis1Y.text()), float(self._ui.axis1Z.text())),
                    (float(self._ui.axis2X.text()), float(self._ui.axis2Y.text()), float(self._ui.axis2Z.text())),
                    float(self._ui.cylinderRadius.text()))
            elif self._shape == Shape.SPHERE:
                polyData = spherePolyData(
                    (float(self._ui.centerX.text()), float(self._ui.centerY.text()), float(self._ui.centerZ.text())),
                    float(self._ui.sphereRadius.text()))
            elif self._shape in OPEN_SURFACE_SHAPES:
                polyData = self._openSurfacePolyData()
        except ValueError:
            pass

        if polyData:
            self._sources = {self._gId: polyData}
        else:
            self._sources.clear()
            await AsyncMessageBox().information(self, self.tr('Preview Failed'), self.tr('Invalid coordinates'))

        self._displayPreview()

    @qasync.asyncSlot()
    async def _accept(self):
        # One click, one commit: the write queue may hold this for
        # seconds on a cold machine, and a second OK in that window
        # submitted the same element twice.
        with commit_guard(self._ui.ok):
            try:
                if not await self._updateElement():
                    return

                if self._creationMode:
                    db = app.facadeClient.checkout()
                    self._gId = db.addElement('geometry', self._dbElement)

                    name = self._ui.name.text()
                    if self._shape == Shape.HEX6:
                        for plate in Shape.PLATES.value:
                            element = db.newElement('geometry')
                            element.setValue('gType', GeometryType.SURFACE.value)
                            element.setValue('volume', self._gId)
                            element.setValue('name', db.getUniqueValue('geometry', 'name', f'{name}_{plate}'))
                            element.setValue('shape', plate)
                            element.setValue('cfdType', CFDType.BOUNDARY.value)
                            db.addElement('geometry', element)
                    else:
                        element = db.newElement('geometry')
                        element.setValue('gType', GeometryType.SURFACE.value)
                        element.setValue('volume', self._gId)
                        element.setValue('name', db.getUniqueValue('geometry', 'name', f'{name}_surface'))
                        element.setValue('shape', self._shape)
                        element.setValue('cfdType', CFDType.BOUNDARY.value)
                        db.addElement('geometry', element)

                    await app.facadeClient.commit_working_copy(db, action='edit volume')
                    # DP-16. Another copy may have taken this key first, in
                    # which case the commit renumbered the volume and its
                    # plates rather than refusing them. The dialog's own id
                    # has to follow, or it names a different volume.
                    self._gId = db.remappedKey('geometry', self._gId)
                else:
                    await app.facadeClient.commit_working_copy(self._dbElement, action='edit volume')

                super().accept()
            except CONFLICT_ERRORS as error:
                # Stay open: the user's entries are still here, and the only
                # thing that changed is what the case looked like underneath.
                await AsyncMessageBox().information(
                    self, self.tr('Case Changed'), self.tr(conflict_message(error)))
            except ValidationError as e:
                await AsyncMessageBox().information(self, self.tr("Input Error"), e.toMessage())

    def _openSurfacePolyData(self):
        """What the preview draws for a plane, a disk or a plate.

        Plan 31. A plane has no edges, so what is drawn is a square marking
        where it sits and which way it faces; a plate that OpenFOAM would
        refuse -- a span without exactly two non-zero entries -- draws nothing,
        which is the dialog's existing way of saying the numbers are wrong.
        """
        vectors = self._openSurfacePage().vectors()
        if vectors is None:
            return None
        first, second = vectors
        if self._shape == Shape.PLANE:
            return planePolyData(first, second, self._planePreviewExtent(first))
        if self._shape == Shape.PLATE:
            return openPlatePolyData(first, second)
        try:
            radius = float(self._openSurfacePage().radius())
        except (TypeError, ValueError):
            return None
        return diskPolyData(first, second, radius)

    @staticmethod
    def _planePreviewExtent(point):
        """How big to draw the stand-in square for an unbounded plane.

        There is no right answer -- the surface is infinite -- so the square
        is sized off how far from the origin the user put it, which keeps it
        visible at whatever scale the model is in rather than vanishing on a
        millimetre part or filling the view on a kilometre one.
        """
        distance = max(abs(component) for component in point)
        return max(distance, 1.0)

    def _validateHex(self):
        try:
            point1 = (float(self._ui.minX.text()), float(self._ui.minY.text()), float(self._ui.minZ.text()))
            point2 = (float(self._ui.maxX.text()), float(self._ui.maxY.text()), float(self._ui.maxZ.text()))
        except ValueError:
            return None

        minX, minY, minZ = point1
        maxX, maxY, maxZ = point2
        if not (minX < maxX and minY < maxY and minZ < maxZ):
            return None

        return point1, point2

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
