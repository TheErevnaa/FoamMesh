#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio

import qasync

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox, QMenu
from PySide6.QtCore import Signal, QSignalBlocker

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType, Shape, GeometryType
from foammesh.view.step_page import StepPage
from widgets.async_message_box import AsyncMessageBox
from .geometry import RESERVED_NAMES
from .geometry_add_dialog import GeometryAddDialog
from .geometry_import_dialog import ImportDialog
from .geometry_list import GeometryList
from .split_dialog import SplitDialog
from .stl_utility import StlImporter
from .surface_dialog import SurfaceDialog
from .volume_dialog import VolumeDialog


class ContextMenu(QMenu):
    editActionTriggered = Signal()
    removeActionTriggered = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._removeAction = QAction(self.tr('Remove'), self)

        editAction = QAction(self.tr('Edit/View'), self)

        self.addAction(editAction)
        self.addAction(self._removeAction)

        editAction.triggered.connect(self.editActionTriggered)
        self._removeAction.triggered.connect(self.removeActionTriggered)

    def enableEditActions(self):
        self._removeAction.setVisible(True)

    def disableEditActions(self):
        self._removeAction.setVisible(False)


def _volumeName(surfaces):
    """What to call the assembly a set of imported surfaces forms.

    R72/R126. The old rule took the FIRST solid's name. MEASURED importing
    `venturi.stl` (solids `inlet`, `outlet`, `wall_converging`,
    `wall_diverging`): the volume arrived called **inlet**, colliding with its
    own inlet surface, which was then silently renamed `inlet_surface`; the
    name `venturi` was used nowhere. `annulus.stl` did the same. A single-solid
    file still takes its solid's name, which is what made `tee.stl` correct.
    """
    explicit = getattr(surfaces[0], 'volumeName', None)
    if explicit:
        return explicit

    solids = {getattr(surface, 'sName', '') or '' for surface in surfaces}
    solids.discard('')
    if len(solids) == 1:
        # One solid, or several that agree: its name IS the assembly's name.
        return solids.pop()

    # Several differently named solids are parts of the file, not the file.
    return surfaces[0].fName


def _toMetres(volumes, surfaces, unit):
    """Rescale imported surfaces from the declared unit into metres.

    A ``StlSurface`` is a named polydata, so conversion replaces the polydata
    and carries every other field across -- the names are what the boundary
    patches are built from and must survive untouched.
    """
    from foammesh.core.geometry.units import si_factor, to_metres

    from .stl_utility import StlSurface

    try:
        if not unit or si_factor(unit) == 1.0:
            return volumes, surfaces
    except (KeyError, ValueError):
        return volumes, surfaces

    def convert(surface):
        return StlSurface(to_metres(surface.polyData, unit), surface.fName,
                          surface.sName, surface.sIndex)

    return ([[convert(surface) for surface in volume] for volume in volumes],
            [convert(surface) for surface in surfaces])


class GeometryPage(StepPage):
    geometryRemoved = Signal()

    def __init__(self, ui):
        super().__init__(ui, ui.geometryPage)

        self._geometryManager = None
        self._list = GeometryList(self._ui.geometryList)
        self._menu = None

        self._dialog = None
        self._menu = ContextMenu()
        self._actorsBackup = []
        self._selectionManager = None
        #: True while the tree is being written from the selection
        #: snapshot, so that echo is not read back as a user action.
        self._selectionEcho = False
        self._interfacePairs = None
        self._boundaries = None
        self._cad = None

        self._connectSignalsSlots()

    def isNextStepAvailable(self):
        return app.facadeClient.checkout().elementCount('geometry') > 0

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        if not self._loaded:
            self.load()
        else:
            # Another page may have changed what the case holds since this one
            # was last on screen: the Repair page cuts one surface into five
            # boundaries, and those are database rows now (R169). The tree is
            # written from the database rather than kept in step with it, so
            # coming back here has to read it again -- otherwise the page that
            # names the boundaries shows the surface they replaced.
            self._list.load()
            self._ensureBoundaryPanel()
            self._ensureCadPanel()
            self._updateNextStepAvailable()

        app.window.meshManager.unload()

    async def hide(self):
        return True

    def load(self):
        self._geometryManager = app.window.geometryManager
        self._list.load()
        self._ensureBoundaryPanel()
        self._ensureCadPanel()
        self._ensureInterfacePairPanel()

        if self._selectionManager is not self._geometryManager:
            if self._selectionManager is not None:
                try:
                    self._selectionManager.selectedActorsChanged.disconnect(
                        self._applyDisplaySelections)
                except (RuntimeError, TypeError):
                    pass
            self._geometryManager.selectedActorsChanged.connect(
                self._applyDisplaySelections)
            self._selectionManager = self._geometryManager

        # Whatever selected the geometry - a viewport pick, the display
        # control, an operation - the tree shows it.
        service = app.selectionService
        try:
            service.selection_changed.disconnect(self._mirrorSelection)
        except (KeyError, LookupError, RuntimeError, TypeError, ValueError):
            pass
        service.selection_changed.connect(self._mirrorSelection)

        self._loaded = True

    def _ensureBoundaryPanel(self):
        """Mount the boundary list under the tree it describes (R178).

        The controls that rename, merge and split boundaries were a tab on
        2. Repair. Nothing put them there but history: `geometry.patches.*`
        reads the artifact store directly, so it needs no repair, no wrap and
        no prepared revision, and the boundaries are rows in this very tree
        (R169). A user whose geometry imported clean had no reason to open
        Repair and so never found the controls that name the inlet.
        """
        if self._boundaries is not None:
            self._boundaries.refresh()
            return
        layout = self._widget.layout()
        if layout is None:
            # Isolated widget tests do not build the generated page.
            return
        from .boundary_panel import BoundaryPanel

        self._boundaries = BoundaryPanel(self._widget)
        self._boundaries.setObjectName('geometryBoundaryPanel')
        # A split writes new rows into the tree, so the tree has to be read
        # again -- otherwise the page that names the boundaries goes on
        # showing the surface they replaced.
        self._boundaries.geometryChanged.connect(self._reloadFromDatabase)
        layout.addWidget(self._boundaries)
        self._boundaries.refresh()

    @staticmethod
    def _cadEntries():
        """The CAD sources the case holds, read from the artifact store.

        A manifest read, not a CAD read: it says which entries have a solid
        model and what deflection each was faceted at, without opening OCCT.
        """
        project = getattr(app, 'project', None)
        if project is None or getattr(project, 'path', None) is None:
            return []
        try:
            from foammesh.core.geometry import GeometryArtifactStore
            entries = GeometryArtifactStore(project.path).entries()
        except (OSError, ValueError):
            return []
        return [entry for entry in entries if entry.get('cad_artifact')]

    def _ensureCadPanel(self):
        """Mount the CAD tessellation controls, for cases that have CAD (F-10).

        The deflection decides whether the facets the mesher sees are the part
        or a caricature of it, and until now it was a constant inside the
        importer: the panel that sets it was built, tested and never mounted
        anywhere, so a STEP file was always faceted at 0.1 whatever its size.
        The panel shows only when the case holds a solid model -- an STL is
        already faceted and has nothing to re-tessellate.
        """
        entries = self._cadEntries()
        if self._cad is None:
            layout = self._widget.layout()
            if layout is None:
                # Isolated widget tests do not build the generated page.
                return
            from .cad_page import CadPanel

            self._cad = CadPanel(self._widget)
            self._cad.setObjectName('geometryCadPanel')
            self._cad.retessellateRequested.connect(self._retessellateCad)
            layout.addWidget(self._cad)
        self._cad.setStoreEntries(entries)
        self._cad.setVisible(bool(entries))

    def _cadTessellation(self):
        """What the panel says a CAD import should be faceted at, if mounted."""
        if self._cad is None:
            return None
        import dataclasses

        return dataclasses.asdict(self._cad.params())

    @qasync.asyncSlot(object)
    async def _retessellateCad(self, params):
        """Re-facet the CAD in the case at the deflection now on the panel.

        A new revision of the same geometry rather than a re-import: the
        boundaries keep their names and the patch map is carried across, which
        is what makes changing the deflection safe once a case has been named.
        """
        import dataclasses

        from foammesh.core.facade.errors import FacadeError

        ids = [entry['geometry_id'] for entry in self._cadEntries()]
        if not ids:
            return
        settings = dataclasses.asdict(params)
        try:
            for geometryId in ids:
                await app.facadeClient.run('geometry.repair.apply', {
                    'route': 'cad', 'geometry_id': geometryId,
                    'actions': [{'action': 'cad.retessellate',
                                 'params': settings}]})
        except (RuntimeError, OSError, ValueError, FacadeError) as ex:
            QMessageBox.information(
                self._widget, self.tr('Re-tessellate'), str(ex))
            return
        self._ensureCadPanel()
        self._reloadFromDatabase()

    def _ensureInterfacePairPanel(self):
        """Mount engine-neutral conformal/periodic/NCC pairing at geometry level."""
        if self._interfacePairs is not None:
            self._interfacePairs.refresh()
            return
        layout = getattr(self._ui, 'verticalLayout_4', None)
        if layout is None:
            # Small isolated widget tests do not construct the generated main
            # window layout. Production always supplies it.
            return
        from foammesh.view.workflow_controls.child_controls import ChildControlPanel
        # Four short columns, not seven. This panel lives in the navigation
        # column, and seven stretched columns cut every heading to fragments
        # ("er scope t", "tch tolerar") -- a table nobody can read is not an
        # index. The table says which pair this is and whether it is on; the
        # scopes, tolerance and transform live in the editor, which has the
        # width to show them.
        self._interfacePairs = ChildControlPanel(
            app.facadeClient, 'geometry.interface_pairs',
            self.tr(
                'Conformal, cyclic/periodic and non-conformal interface pairs'),
            ('name', 'enabled', 'coupling', 'transform'), self._widget)
        self._interfacePairs.setObjectName('geometryInterfacePairsPanel')
        layout.addWidget(self._interfacePairs)

    def retranslate(self):
        self._list.retranslate()

    def _connectSignalsSlots(self):
        self._ui.geometryList.customContextMenuRequested.connect(self._executeContextMenu)
        # Double-click opens the same editor the context menu does. Everything
        # a surface has -- its CFD type, and the scale/rotate/translate widget
        # for an imported one -- lived behind a right-click nobody had been
        # told about, so a user who double-clicked a row (which is what a tree
        # of things trains you to do) got nothing at all and concluded the
        # options were gone.
        self._ui.geometryList.itemDoubleClicked.connect(self._doubleClicked)
        self._list.selectedItemsChanged.connect(self._selectedItemsChanged)
        self._ui.import_.clicked.connect(self._importClicked)
        self._ui.add.clicked.connect(self._addClicked)
        self._menu.editActionTriggered.connect(self._openEditDialog)
        self._menu.removeActionTriggered.connect(self._removeGeometry)

    def _doubleClicked(self, item, _column):
        """Open the editor for the row that was double-clicked.

        Selecting first matters: the editor reads the *selection*, and a
        double-click on an unselected row would otherwise edit whatever was
        selected before -- the quietest kind of wrong.
        """
        if not item.isSelected():
            self._ui.geometryList.setCurrentItem(item)
        if self._locked:
            # R71. The locked page used to swallow the gesture: the row still
            # highlighted and still looked editable, so the user repeated the
            # double-click and nothing ever said the geometry had been
            # prepared or that Unlock is the way back in.
            QMessageBox.information(
                self._widget, self.tr('Geometry Locked'),
                self.tr('This geometry has been prepared, so its surfaces can '
                        'no longer be edited here. Use Unlock to edit them '
                        'again; unlocking discards the prepared geometry and '
                        'everything meshed from it.'))
            return
        if self._list.selectedItems():
            self._openEditDialog()

    def _executeContextMenu(self, pos):
        if not self._list.selectedItems():
            return

        if self._locked:
            self._menu.disableEditActions()
        else:
            self._menu.enableEditActions()

        self._menu.exec(self._ui.geometryList.mapToGlobal(pos))

    def _selectedItemsChanged(self):
        """Tell the selection service what the tree now shows.

        The old code drove the geometry manager directly behind a signal
        blocker, so the service - which the display control, the overlay and
        the viewport all read - never learned that the tree had changed.
        The echo flag replaces the blocker: it stops the service's own
        callback from bouncing back into here as a fresh user selection.
        """
        if getattr(self, '_selectionEcho', False):
            return
        from foammesh.app import app
        known = {entity.stable_id for entity in app.selectionService.entities()}
        app.selectionService.select(
            [id_ for id_ in self._list.selectedIDs() if str(id_) in known])

    def _mirrorSelection(self, stable_ids):
        """Show in the tree whatever selected the geometry, from any source."""
        self._selectionEcho = True
        try:
            self._list.setSelectedItems([str(value) for value in stable_ids])
        finally:
            self._selectionEcho = False

    @qasync.asyncSlot()
    async def _importClicked(self):
        self._dialog = ImportDialog(self._widget)
        self._dialog.accepted.connect(self._importSTL)
        self._dialog.open()

    async def openImportDialog(self):
        """Shared File-menu entry point for the existing geometry workflow."""
        if not self._loaded:
            self.load()
        await self._importClicked()

    def _addClicked(self):
        self._dialog = GeometryAddDialog(self._widget)
        self._dialog.shapeSelected.connect(self._openAddDialog)
        self._dialog.open()

    def _openAddDialog(self, shape):
        def addVolume():
            self._addVolume(self._dialog.gId())

        self._dialog = self._newVolumeDialog()
        self._dialog.setupForAdding(shape)
        self._dialog.accepted.connect(addVolume)
        self._dialog.open()

    def _openEditDialog(self):
        def updateVolume():
            gId = self._dialog.gId()
            db = app.facadeClient.checkout()
            volume = db.getElement('geometry',  gId)
            self._list.update(gId, volume)
            self._geometryManager.updateCustomSurfaces(volume, self._list.childSurfaces(gId))
            # R127. Opening the editor emits stepReset, which the step manager
            # wires unconditionally to disableNextButton, so accepting an edit
            # left Next grey until the user navigated away and back. Nothing on
            # the accept path re-asserted it; this does.
            self._updateNextStepAvailable()

        def updateSurfaces():
            self._surfacesEdited(self._dialog.gIds())

        items = self._list.selectedItems()

        sources = {}
        if len(items) == 1 and items[0].isVolume():
            gId = str(items[0].gId())
            for sId in self._list.childSurfaces(gId):
                actorInfo = self._geometryManager.actorInfo(sId)
                self._backupActor(actorInfo)
                sources[sId] = actorInfo.dataSet()

            self._dialog = self._newVolumeDialog()
            self._dialog.setupForEdit(gId, sources)
            self._dialog.accepted.connect(updateVolume)
            self._dialog.open()
        else:
            if len(items) == 1 and items[0].isSurface():
                # R45/R146. A child surface -- which is what a split delivers,
                # and the only kind whose name says nothing -- used to reach
                # the dialog with no source mesh at all, so it got neither the
                # highlight nor the measurements that identify it. The
                # transform widget is still confined to top-level surfaces;
                # SurfaceDialog decides that from the element's own `volume`.
                gId = str(items[0].gId())
                actorInfo = self._geometryManager.actorInfo(gId)
                self._backupActor(actorInfo)
                sources[gId] = actorInfo.dataSet()

            self._dialog = SurfaceDialog(self._widget, self._ui.renderingView)
            if not self._ui.geometryButtons.isEnabled():
                self._dialog.disableEdit()

            self._dialog.setData([item.gId() for item in items], sources)
            self._dialog.finished.connect(self._restoreActors)
            self._dialog.accepted.connect(updateSurfaces)
            self._dialog.open()

    @qasync.asyncSlot()
    async def _removeGeometry(self):
        if not await AsyncMessageBox().confirm(self._widget, self.tr("Remove Geometries"),
                                               self.tr('Are you sure you want to remove the selected items?')):
            return

        items = self._list.selectedItems()

        volume = None
        if len(items) == 1 and items[0].isVolume():
            volume = str(items[0].gId())
            surfaces = self._list.childSurfaces(volume)
        elif not any([item.geometry().value('volume') for item in items]):
            surfaces = {item.gId(): item.geometry() for item in items}
        else:
            await AsyncMessageBox().information(self._widget, self.tr('Delete Surfaces'),
                                                self.tr('Surfaces contained in a volume cannot be deleted.'))
            return

        db = app.facadeClient.checkout()

        for gId, surface in surfaces.items():
            db.removeGeometryPolyData(surface.value('path'))
            db.removeElement('geometry', gId)
            self._list.remove(gId)
        self._geometryManager.removeGeometry(surfaces)

        if volume:
            db.removeElement('geometry', volume)
            self._list.remove(volume)
            self._geometryManager.removeGeometry([volume])

        await app.facadeClient.commit_working_copy(db, action='update geometry')

        self._updateNextStepAvailable()

        self.geometryRemoved.emit()

    @qasync.asyncSlot()
    async def _importSTL(self):
        def getUniqueSeq(name, seq):
            if seq == '' and name in RESERVED_NAMES:
                seq = 1

            return db.getUniqueSeq('geometry', 'name', name, seq)

        from foammesh.core.facade.errors import FacadeError
        from foammesh.core.geometry.cad import CAD_SUFFIXES, is_available, CAD_INSTALL_HINT
        files = self._dialog.files()
        cadFiles = [f for f in files if f.suffix.lower() in CAD_SUFFIXES]
        surfaceFiles = [f for f in files if f.suffix.lower() not in CAD_SUFFIXES]

        # STL and OBJ carry no unit, so the dialog asked. STEP and IGES say
        # what they were written in, and the answer only reaches a BREP, which
        # does not. Convert before any of it is stored: the viewport, the
        # geometry database and the artifact the mesher reads must all agree,
        # and a case where they disagree about scale is worse than one that
        # is uniformly wrong.
        unit = self._dialog.unit()
        # F-10. The deflection the CAD panel shows is the one the part is
        # faceted at. Nothing could set it before: the importer's constant
        # decided it, and the tessellation the mesher read was not even the
        # one on screen.
        tessellation = self._cadTessellation()
        volumes, surfaces = [], []
        if cadFiles:
            # CAD (STEP/IGES/BREP) import -> tessellated per-face surfaces.
            # Only the CAD files go this way: a mixed selection used to hand
            # its STL files to the CAD reader as well, which failed the whole
            # import.
            if not is_available():
                await AsyncMessageBox().information(
                    self._widget, self.tr('CAD support'), CAD_INSTALL_HINT)
                return
            try:
                volumes, surfaces = await self._importThroughStore(
                    cadFiles, unit, tessellation=tessellation)
            except (RuntimeError, OSError, ValueError, FacadeError) as ex:
                QMessageBox.information(
                    self._widget, self.tr('Geometry Loading Error'), str(ex))
                return
        if surfaceFiles:
            if self._dialog.featureAngle():
                splitDialog = SplitDialog(
                    self._widget, surfaceFiles, float(self._dialog.featureAngle()))
                try:
                    await splitDialog.show()
                except asyncio.exceptions.CancelledError:
                    return
                # The dialog is where the angle and the smallest piece worth
                # keeping are chosen. The cut itself is made by the geometry
                # artifact store, whose pieces are what both meshers stage;
                # the dialog's own segmentation used to reach only this
                # database, so neither mesher ever saw a split.
                try:
                    meshVolumes, meshSurfaces = await self._splitThroughStore(
                        surfaceFiles, unit, splitDialog.featureAngle(),
                        splitDialog.minAreaFraction())
                except (RuntimeError, OSError, ValueError, FacadeError) as ex:
                    QMessageBox.information(
                        self._widget, self.tr('Geometry Loading Error'), str(ex))
                    return
            else:
                # Plan 30 §5.2. One write per click: the surface shown is read
                # back from the artifact that was just written, so the
                # viewport and the mesher cannot be looking at two different
                # tessellations of the same file -- and the store's copy is
                # already in metres, which is why nothing rescales here.
                try:
                    meshVolumes, meshSurfaces = await self._importThroughStore(
                        surfaceFiles, unit)
                except (RuntimeError, OSError, ValueError, FacadeError) as ex:
                    QMessageBox.information(
                        self._widget, self.tr('Geometry Loading Error'), str(ex))
                    return
            volumes = [*volumes, *meshVolumes]
            surfaces = [*surfaces, *meshSurfaces]

        try:
            addedVolumes = []
            addedSurfaces = []

            db = app.facadeClient.checkout()
            seq = ''
            for volume in volumes:
                name = _volumeName(volume)
                seq = getUniqueSeq(name, seq)
                volumeName = name + seq
                element = db.newElement('geometry')
                element.setValue('gType', GeometryType.VOLUME)
                element.setValue('name', volumeName)
                element.setValue('shape', Shape.TRI_SURFACE_MESH.value)
                element.setValue('cfdType', CFDType.NONE.value)
                volumeId = db.addElement('geometry', element)
                addedVolumes.append(volumeId)

                sName = f'{volumeName}_surface'
                sseq = ''
                for surface in volume:
                    name = surface.sName if surface.sName and surface.sName != volumeName else sName
                    sseq = db.getUniqueSeq('geometry', 'name', name, sseq)
                    surfaceName = name + getUniqueSeq(name, sseq)
                    element = db.newElement('geometry')
                    element.setValue('gType', GeometryType.SURFACE.value)
                    element.setValue('volume', volumeId)
                    element.setValue('name', surfaceName)
                    element.setValue('shape', Shape.TRI_SURFACE_MESH.value)
                    element.setValue('cfdType', CFDType.BOUNDARY.value)
                    element.setValue('path', db.addGeometryPolyData(surface.polyData))
                    db.addElement('geometry', element)

            for surface in surfaces:
                name = surface.sName if surface.sName else surface.fName
                seq = getUniqueSeq(name, seq)
                surfaceName = name + seq
                element = db.newElement('geometry')
                element.setValue('gType', GeometryType.SURFACE.value)
                element.setValue('name', surfaceName)
                element.setValue('shape', Shape.TRI_SURFACE_MESH.value)
                element.setValue('cfdType', CFDType.BOUNDARY.value)
                element.setValue('path', db.addGeometryPolyData(surface.polyData))
                gId = db.addElement('geometry', element)
                addedSurfaces.append(gId)

            await app.facadeClient.commit_working_copy(db, action='update geometry')

            # The sources are already persisted: every branch above imported
            # through `geometry.import` and then read the artifact back, so
            # there is nothing left to write here. This used to be a second
            # pass over the same files -- a second tessellation of every STEP,
            # at the importer's fixed deflection rather than the one shown --
            # which is how the viewport and the mesher came to disagree about
            # the same part.

            # Rebuild once from what was committed, rather than also adding
            # each element incrementally.
            #
            # Committing yields to the event loop, and the project refresh it
            # schedules already rebuilds the tree and the scene from the
            # database. Adding the same elements again afterwards gave the
            # geometry list a second, childless copy of the volume -- the
            # duplicate "duct" row -- and asked the actor manager to register
            # actors it already had, which raises. `_importSTL` catches only
            # RuntimeError, so that KeyError aborted the rest of the import
            # with nothing said.
            #
            # Both loads clear before they read, so this is idempotent and it
            # does not matter whether the scheduled refresh runs before or
            # after it.
            del addedVolumes, addedSurfaces
            self._reloadFromDatabase()
        except (RuntimeError, OSError, ValueError, FacadeError) as ex:
            # RuntimeError alone let a rejected artifact import or an
            # unreadable file abort the import with nothing said.
            QMessageBox.information(
                self._widget, self.tr('Geometry Loading Error'), str(ex))

    async def _importThroughStore(self, files, unit, tessellation=None):
        """Write each source to the artifact store once and show what it wrote.

        Plan 30 §5.2. Every import used to happen twice: the view read the
        file for the viewport and `geometry.import` read it again for the
        artifact. For a STEP that meant two tessellations at two deflections,
        so the faces on screen were not the faces staged for meshing, and the
        CAD panel's setting could not have reached the mesher even if it had
        been mounted. Now the store's artifact is the only tessellation, and
        the surfaces here are read back from it -- under the patch names the
        store recorded, which are the names both meshers stage.

        Returns ``(volumes, surfaces)`` the way ``StlImporter.identifyVolumes``
        does. ``tessellation`` is a mapping of TessellationParams fields and
        reaches CAD sources only; STL and OBJ arrive already faceted.
        """
        from pathlib import Path

        from foammesh.core.geometry.cad import CAD_SUFFIXES

        importer = StlImporter()
        volumes, surfaces = [], []
        for source in files:
            parameters = {'source': str(source), 'unit': unit}
            if tessellation and source.suffix.lower() in CAD_SUFFIXES:
                parameters['tessellation'] = dict(tessellation)
            imported = await app.facadeClient.run('geometry.import', parameters)
            entry = imported.payload
            names = {}
            for row in entry.get('patches') or ():
                reference = row.get('source_ref') or {}
                written = reference.get('original_name')
                if written and row.get('name'):
                    names[written] = row['name']
            importer.loadNamedSolids(
                Path(entry['artifact']), names, fileName=source.stem,
                volumeName=entry.get('name') or source.stem)
            fileVolumes, fileSurfaces = importer.identifyVolumes()
            volumes.extend(fileVolumes)
            surfaces.extend(fileSurfaces)
        return volumes, surfaces

    async def _splitThroughStore(self, files, unit, angle, minAreaFraction):
        """Persist each surface, cut it in the artifact store, load the pieces.

        Returns ``(volumes, surfaces)`` the way ``StlImporter.identifyVolumes``
        does, so the rest of the import treats the pieces like any other
        loaded surface. The pieces come back from the artifact the store
        wrote, under the names its patch records carry: the tree, the
        snappy ``regions`` dictionary and the Gmsh physical groups then all
        name the same faces. A surface with no edge sharper than the angle
        stays one boundary; the store says so and the import goes on.
        """
        from pathlib import Path

        from foammesh.core.facade.errors import ValidationFailedError

        importer = StlImporter()
        volumes, surfaces = [], []
        for source in files:
            imported = await app.facadeClient.run(
                'geometry.import', {'source': str(source), 'unit': unit})
            entry = imported.payload
            geometryId = entry['geometry_id']
            artifact = Path(entry['artifact'])
            names = {}
            try:
                split = await app.facadeClient.run(
                    'geometry.patches.split_by_angle',
                    {'geometry_id': geometryId, 'angle_deg': angle,
                     'min_area_fraction': minAreaFraction})
            except ValidationFailedError as ex:
                QMessageBox.information(
                    self._widget, self.tr('Feature Angle Split'), str(ex))
            else:
                payload = split.payload
                artifact = artifact.with_name(
                    f"rev{payload['revision']}{artifact.suffix}")
                for row in payload.get('patches') or ():
                    if row.get('geometry_id') != geometryId:
                        continue
                    for ref in row.get('source_refs') or ():
                        if ref.get('original_name'):
                            names[ref['original_name']] = row['name']
            importer.loadNamedSolids(
                artifact, names, fileName=source.stem,
                volumeName=entry.get('name') or source.stem)
            pieceVolumes, pieceSurfaces = importer.identifyVolumes()
            volumes.extend(pieceVolumes)
            surfaces.extend(pieceSurfaces)
        return volumes, surfaces

    def _reloadFromDatabase(self):
        """One authoritative rebuild of the tree and the scene.

        The single place an import refreshes what is on screen, so the tree,
        the actors and the database cannot disagree about what was imported.
        """
        self._list.load()
        self._geometryManager.load()
        # An import may have brought the first solid model into the case, and
        # the deflection controls exist only while there is one to re-facet.
        self._ensureCadPanel()
        self._updateNextStepAvailable()

    def _addVolume(self, gId):
        db = app.facadeClient.checkout()
        if not db.hasElement('geometry', gId):
            # The id the dialog reported is not in the case any more: another
            # commit landed, or the merge re-keyed it. Read the case back
            # rather than dereferencing a row that is not there.
            self._reloadGeometry()
            return
        volume = db.getElement('geometry',  gId)
        self._addGeometry(gId, volume)

        surfaces = db.getElements('geometry', lambda i, e: e['volume'] == gId)
        for surfaceId in surfaces:
            self._addGeometry(surfaceId, surfaces[surfaceId], volume)

    def _addSurface(self, gId):
        db = app.facadeClient.checkout()
        if not db.hasElement('geometry', gId):
            self._reloadGeometry()
            return
        surface = db.getElement('geometry',  gId)
        self._addGeometry(gId, surface, surface.value('volume'))

    def _reloadGeometry(self):
        """Rebuild the list and the actors from what the case actually holds."""
        if self._geometryManager is not None:
            self._geometryManager.load()
        self._list.load()
        self._updateNextStepAvailable()

    def _addGeometry(self, gId, geometry, volume=None):
        self._geometryManager.addGeometry(gId, geometry, volume)
        self._list.add(gId, geometry)
        self._updateNextStepAvailable()

    def _surfacesEdited(self, gIds):
        """Read back every surface the edit dialog touched.

        A name lives in five places on screen - the tree row, the actor, the
        display-control row, the parts overlay and the Boundaries row - and
        one store behind each pair. `geometry.rename` moves both stores;
        this moves all five readers. It used to move the tree row alone, and
        the actor only for a surface that belonged to a volume, so renaming
        an imported surface in the edit dialog changed one label out of five.
        """
        db = app.facadeClient.checkout()
        for gId, surface in db.getElements(
                'geometry', lambda i, e: i in gIds).items():
            self._list.update(gId, surface)
            self._geometryManager.renameGeometry(gId, surface.value('name'))
            if surface.value('volume') is not None:
                self._geometryManager.updateIndependentSurface(gId, surface)
        if self._boundaries is not None:
            # The rows here come from the patch manifest the rename rewrote.
            self._boundaries.refresh()
        # R47/R145. Next went grey when the first Edit Surface dialog opened
        # and stayed grey through five renames; only leaving the page and
        # coming back brought it back. Re-assert it here, where the edit is
        # known to have landed.
        self._updateNextStepAvailable()

    def _applyDisplaySelections(self, gIds: list[str]):
        with QSignalBlocker(self._list):
            self._list.setSelectedItems(gIds)

    def _enableStep(self):
        # self._ui.geometryList.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._ui.geometryButtons.setEnabled(True)

    def _disableStep(self):
        # self._ui.geometryList.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._ui.geometryButtons.setEnabled(False)

    def _clear(self):
        self._list.clear()

    def _newVolumeDialog(self):
        dialog = VolumeDialog(self._widget, self._ui.renderingView)
        if not self._ui.geometryButtons.isEnabled():
            dialog.disableEdit()

        dialog.finished.connect(self._restoreActors)

        return dialog

    def _backupActor(self, actorInfo):
        self._actorsBackup.append((actorInfo, actorInfo.properties().opacity))
        actorInfo.setOpacity(0.1)

    def _restoreActors(self):
        for actorInfo, opacity in self._actorsBackup:
            actorInfo.setOpacity(opacity)

        self._actorsBackup.clear()
