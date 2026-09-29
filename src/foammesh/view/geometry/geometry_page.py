#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio

import qasync

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QMenu, QPushButton, QVBoxLayout,
                               QWidget)
from PySide6.QtCore import Signal

from foammesh.app import app
from foammesh.core.quantities import count_text
from foammesh.db.configurations_schema import CFDType, Shape, GeometryType
from foammesh.view.step_page import StepPage
from widgets.async_message_box import AsyncMessageBox
from .geometry import RESERVED_NAMES
from .geometry_add_dialog import GeometryAddDialog
from .geometry_import_dialog import ImportDialog, parseFeatureAngle
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

        editAction = QAction(self.tr('View/edit'), self)

        self.addAction(editAction)
        self.addAction(self._removeAction)

        editAction.triggered.connect(self.editActionTriggered)
        self._removeAction.triggered.connect(self.removeActionTriggered)

        self._boundaryActions = []

    def setBoundaryActions(self, actions):
        """Put the four boundary operations on the list's own menu.

        DP-B1. They were the buttons of a second table of the same rows. The
        same `QAction` objects are on the row under the list and here, so a
        right-click and a button press are one operation on one selection
        rather than two editors of the same boundaries.
        """
        if self._boundaryActions:
            return
        self.addSeparator()
        for action in actions:
            self.addAction(action)
        self._boundaryActions = list(actions)

    def enableEditActions(self):
        self._removeAction.setVisible(True)

    def disableEditActions(self):
        self._removeAction.setVisible(False)


def _volumesFromBodies(entry, pieces):
    """One volume per CAD body, from the records the import already wrote.

    DP-419. `identifyVolumes` composes a volume out of surfaces that close,
    and for a store-written artifact every patch is its own solid, so a
    multi-body STEP gives it ten open faces and it hands all ten back loose.
    MEASURED on `coaxial_ducts`: ten surface rows, every one `volume: None`,
    against the two volume rows the same model's STL gets. So the Gmsh branch
    has no volume to type a CellZone on and no name to give a cell zone, and
    published `Part_1_solid_1_11ff99c5` where snappy published
    `coaxial_ducts_core` -- two stores, and only one of them reachable from
    the tree.

    Nothing has to be inferred to fix that. The CAD kernel already said which
    faces bound which body and the store wrote it down, one region record per
    body, and this reads it. Only where it reads cleanly: two or more bodies,
    every face claimed by exactly one of them, and every body claiming at
    least one face. Anything else is left to `identifyVolumes`, which is what
    happens for every single-body import and every STL.
    """
    records = [item for item in (entry.get('regions') or ())
               if isinstance(item, dict) and item.get('region_uuid')]
    if len(records) < 2:
        return None
    owner = {}
    for record in records:
        for value in record.get('boundary_patch_uuids') or ():
            token = str(value or '').strip()
            if not token or token in owner:
                # A face two bodies both claim is a shared boundary the tree
                # cannot show under one parent. Leave it alone rather than
                # pick one.
                return None
            owner[token] = record
    grouped = {}
    for piece in pieces:
        record = owner.get(str(getattr(piece, 'patchUuid', '') or ''))
        if record is None:
            return None
        grouped.setdefault(record['region_uuid'], []).append(piece)
    if len(grouped) != len(records):
        return None

    volumes = []
    for record in records:
        members = grouped[record['region_uuid']]
        name = str(record.get('name') or '').strip()
        for piece in members:
            piece.regionUuid = str(record['region_uuid'])
            if name:
                piece.volumeName = name
        volumes.append(members)
    return volumes


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


def _patchIdentities(entry) -> tuple[dict, dict]:
    """``(solid -> patch name, patch name -> patch uuid)`` for one import.

    DP-636. A multi-solid or CAD import lists its boundaries under
    ``patches``; a single-solid STL does not -- its one boundary is the entry
    itself, with the uuid and the written solid name on the entry. Reading
    only ``patches`` left that surface's tree row without a ``patchUuid``, so
    a rename in the edit dialog could not find the manifest row (the tree
    called it ``pipe_surface``, the manifest ``pipe``) and renamed the tree
    alone: MEASURED on the 15 September guided walk, which renamed pipe.stl's
    surface ``pipe_wall1`` and exported the snappy boundary as ``pipe``.
    """
    names, uuids = {}, {}
    rows = list(entry.get('patches') or ())
    for row in rows:
        reference = row.get('source_ref') or {}
        written = reference.get('original_name')
        if written and row.get('name'):
            names[written] = row['name']
        if row.get('name') and row.get('patch_uuid'):
            uuids[row['name']] = row['patch_uuid']
    if not rows and entry.get('patch_uuid'):
        written = (entry.get('source_ref') or {}).get('original_name')
        for name in {written, entry.get('name')} - {None, ''}:
            uuids[str(name)] = entry['patch_uuid']
    return names, uuids


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
        #: True while the tree is being written from the selection
        #: snapshot, so that echo is not read back as a user action.
        self._selectionEcho = False
        self._interfacePairs = None
        self._interfacePairsFolder = None
        self._boundaries = None
        self._cad = None
        self._cadFolder = None
        self._advanced = None
        self._advancedFolder = None
        self._lockBanner = None
        self._splitInterfaces = None

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
            self._ensureBoundaryActions()
            self._ensureCadPanel()
            self._updateNextStepAvailable()

        app.window.meshManager.unload()

    async def hide(self):
        return True

    def load(self):
        self._geometryManager = app.window.geometryManager
        self._list.load()
        self._ensureLockBanner()
        self._ensureBoundaryActions()
        self._ensureCadPanel()
        self._ensureInterfacePairPanel()
        self._ensureSplitInterfacesButton()

        # Whatever selected the geometry - a viewport pick, the display
        # control, an operation - the tree shows it.
        #
        # DP-389. There used to be a second path here: the geometry manager's
        # `selectedActorsChanged` was wired straight into the tree. That
        # signal carries *actor* ids, and a closed volume is one entity over
        # the actors of its children, so applying a volume selection to the
        # renderer made the display control echo the child surfaces and this
        # page wrote them into the tree. MEASURED live: selecting the volume
        # row of `jacketed_pipe_fluid` (id 1) left the tree holding id 2, its
        # one boundary surface -- so `_openEditDialog` never saw a volume and
        # `VolumeDialog`, the only place CellZone is offered, could not be
        # opened at all. The manager already hands that same signal to
        # `SelectionService.viewport_picked`, which ignores an echo raised
        # while the service is applying to the renderer; the tree follows
        # `selection_changed`, which carries stable ids. One path, one answer.
        service = app.selectionService
        try:
            service.selection_changed.disconnect(self._mirrorSelection)
        except (KeyError, LookupError, RuntimeError, TypeError, ValueError):
            pass
        service.selection_changed.connect(self._mirrorSelection)

        self._loaded = True

    def _selectedGeometryIds(self):
        """The geometry rows the tree has selected, in the order they read.

        Visual order, not selection order: a merge names the boundary after
        the first of them, and `selectedItems()` hands them back in whatever
        order the selection model happened to grow.
        """
        tree = self._ui.geometryList
        found = []

        def walk(item):
            if item.isSelected():
                found.append(str(item.gId()))
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(tree.topLevelItemCount()):
            walk(tree.topLevelItem(index))
        return found

    def _ensureBoundaryActions(self):
        """Put the four boundary operations on the list itself (DP-B1).

        R178 moved the controls that rename, merge and split boundaries here
        from 2. Repair, and they arrived as a panel with a table of its own.
        That left the page holding two lists of the same seven boundaries --
        the tree, which showed them, and the table, which edited them -- so
        the row the user was reading was never the row the user could edit.
        The table is gone; the operations act on the tree's selection, from a
        row under it and from its context menu.
        """
        if self._boundaries is not None:
            self._boundaries.refresh()
            return
        layout = self._widget.layout()
        if layout is None:
            # Isolated widget tests do not build the generated page.
            return
        from .boundary_panel import BoundaryActions

        self._boundaries = BoundaryActions(self._selectedGeometryIds,
                                           self._widget)
        # A split writes new rows into the tree, so the tree has to be read
        # again -- otherwise the page that names the boundaries goes on
        # showing the surface they replaced.
        self._boundaries.geometryChanged.connect(self._reloadFromDatabase)
        index = layout.indexOf(self._ui.geometryList)
        if index < 0:
            layout.addWidget(self._boundaries)
        else:
            layout.insertWidget(index + 1, self._boundaries)
        self._menu.setBoundaryActions(self._boundaries.boundaryActions())
        self._boundaries.refresh()

    def _ensureLockBanner(self):
        """Say the step is locked, and offer the way out of it (DP-B3).

        A locked page went flat -- every control disabled -- and said nothing
        about why. The only sentence that mentioned unlocking came from a
        double-click and named `Unlock`, which is a footer button that is not
        on this page at all. The banner is the page saying what state it is
        in, next to the one control that changes it.
        """
        if self._lockBanner is not None:
            return
        layout = self._widget.layout()
        if layout is None:
            return
        banner = QWidget(self._widget)
        banner.setObjectName('geometryLockBanner')
        row = QHBoxLayout(banner)
        row.setContentsMargins(0, 0, 0, 0)
        indicator = QLabel(self.tr('Locked'), banner)
        indicator.setObjectName('geometryLockedIndicator')
        edit = QPushButton(self.tr('Edit geometry\u2026'), banner)
        edit.setObjectName('editLockedGeometry')
        edit.setToolTip(self.tr(
            'Unlock this step so the surfaces can be edited again'))
        edit.clicked.connect(self._editLockedGeometry)
        row.addWidget(indicator)
        row.addWidget(edit)
        row.addStretch(1)
        index = layout.indexOf(self._ui.geometryList)
        layout.insertWidget(index if index > -1 else 0, banner)
        self._lockBanner = banner
        banner.setVisible(bool(self._locked))

    def _discardedResults(self):
        """What unlocking this step throws away, read out of the case.

        Named rather than summarised: `unlocking discards everything meshed
        from it` is as true of a case with no mesh, no quality report and no
        export as of one with all three, and a warning that is true of
        nothing is one the user learns to click through.
        """
        names = [self.tr('the prepared geometry')]
        path = getattr(getattr(app, 'project', None), 'path', None)
        if path is None:
            return names
        if self._hasMesh(path):
            names.append(self.tr('the mesh'))
        if self._hasQualityReport(path):
            names.append(self.tr('the mesh quality report'))
        if self._hasExportRecord(path):
            names.append(self.tr('the export record'))
        return names

    @staticmethod
    def _hasMesh(path):
        from foammesh.core.engine.registry import configured_engine_id
        from foammesh.core.facade.mesh_presence import has_engine_mesh

        try:
            engine = configured_engine_id(app.db)
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            engine = ''
        try:
            return bool(has_engine_mesh(path, engine))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return False

    @staticmethod
    def _hasQualityReport(path):
        from foammesh.core.quality import checkmesh_service

        try:
            return checkmesh_service.report_path(path).exists()
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _hasExportRecord(path):
        from foammesh.core.case import ArtifactHistoryStore
        from foammesh.core.import_export.authored import export_record

        try:
            entries = [entry.to_dict()
                       for entry in ArtifactHistoryStore(path).entries()]
            return bool(export_record(entries))
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError,
                ValueError):
            return False

    def _editLockedGeometry(self):
        """Press, from this page, the footer button that unlocks the step."""
        discarded = self._discardedResults()
        if len(discarded) == 1:
            what = discarded[0]
        else:
            what = self.tr('{0} and {1}').format(
                ', '.join(discarded[:-1]), discarded[-1])
        answer = QMessageBox.question(
            self._widget, self.tr('Edit geometry'),
            self.tr('Editing the geometry discards {0}. Everything after this '
                    'step has to be made again from the edited geometry.\n\n'
                    'Edit it anyway?').format(what),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        # The same button the footer shows, so one route clears the later
        # steps and re-opens this one: `StepManager._unlockCurrentStep`.
        unlock = getattr(self._ui, 'unlock', None)
        if unlock is not None:
            unlock.click()

    def _ensureAdvanced(self):
        """The fold the CAD tessellation form lives under.

        GEO-04. The tessellation form and the interface-pair table were
        mounted open, under the list, so the page asked for more height than
        the navigation column has and the list itself was pushed out of
        sight. The tessellation is a setting a case touches once, so it is
        shut until it is wanted. DP-497: the interface pairs are not under
        here any more -- they are a section of their own.
        """
        if self._advanced is not None:
            return self._advanced.layout()
        layout = self._widget.layout()
        if layout is None:
            # Isolated widget tests do not build the generated page.
            return None
        from foammesh.view.widgets.folder_header import FolderHeader

        self._advancedFolder = FolderHeader(self.tr('Advanced'), self._widget)
        self._advancedFolder.setObjectName('geometryAdvancedFolder')
        # Plan 33 section 1.1 keeps CAD tessellation folded on this page, and
        # this box holds only that, so it opens closed with it. A fold opens open everywhere
        # else in the tree (W-O2, `FolderHeader`), which is why the exception
        # is written here rather than assumed.
        self._advancedFolder.setChecked(False)
        self._advanced = QWidget(self._widget)
        self._advanced.setObjectName('geometryAdvancedSection')
        inner = QVBoxLayout(self._advanced)
        inner.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._advancedFolder)
        layout.addWidget(self._advanced)
        self._advancedFolder.setContents(self._advanced)
        return inner

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
            inner = self._ensureAdvanced()
            if inner is None:
                # Isolated widget tests do not build the generated page.
                return
            from foammesh.view.widgets.folder_header import FolderHeader

            from .cad_page import CadPanel

            self._cadFolder = FolderHeader(self.tr('CAD tessellation'),
                                           self._advanced)
            self._cadFolder.setObjectName('geometryCadFolder')
            # Section 1.1: one of the two folds that survive the rule.
            self._cadFolder.setChecked(False)
            self._cad = CadPanel(self._advanced)
            self._cad.setObjectName('geometryCadPanel')
            self._cad.retessellateRequested.connect(self._retessellateCad)
            inner.addWidget(self._cadFolder)
            inner.addWidget(self._cad)
            self._cadFolder.setContents(self._cad)
        self._cad.setStoreEntries(entries)
        # The case says whether there is anything to re-facet; the fold says
        # whether the user has asked to see it.
        self._cadFolder.setVisible(bool(entries))
        self._cad.setVisible(bool(entries) and self._cadFolder.isChecked())

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
            QMessageBox.warning(
                self._widget, self.tr('Re-tessellate'), str(ex))
            return
        self._ensureCadPanel()
        self._reloadFromDatabase()

    def _ensureInterfacePairPanel(self):
        """Mount engine-neutral conformal/periodic/NCC pairing at geometry level.

        DP-497. The table was folded shut inside `Advanced`, a fold inside a
        fold, so a two-body case -- the one case that needs a pair -- showed
        no sign that pairs exist. It is a section of the page now, open, just
        above `Advanced`.

        DP-496 (audit MA-06). A pair joins two prepared surfaces, so it is
        authored once the geometry is prepared -- and authoring it then keeps
        the prepared geometry: the table stays live while the step is locked,
        and a pair stales the engine plan and the mesh, not the preparation.
        """
        if self._interfacePairs is not None:
            self._interfacePairs.refresh()
            return
        layout = self._widget.layout()
        if layout is None:
            # Small isolated widget tests do not construct the generated main
            # window layout. Production always supplies it.
            return
        from foammesh.view.widgets.folder_header import FolderHeader
        from foammesh.view.geometry.interface_pair_panel import InterfacePairPanel

        self._interfacePairsFolder = FolderHeader(self.tr('Interface pairs'),
                                                  self._widget)
        self._interfacePairsFolder.setObjectName(
            'geometryInterfacePairsFolder')
        # Three short columns. This panel lives in the navigation column, and
        # every column past these cut its heading to fragments ("er scope t",
        # "tch tolerar"); DP-497 took the transform off as well, which showed
        # as a clipped `transl...` at the product width. The table says which
        # pair this is, whether it is on and how it couples; the scopes,
        # tolerance and transform live in the editor, which has the width to
        # show them.
        # DP-556. Add opens on two faces that touch, and OK asks before
        # saving a coincident pair whose faces do not.
        self._interfacePairs = InterfacePairPanel(
            app.facadeClient, 'geometry.interface_pairs',
            self.tr(
                'Conformal, cyclic/periodic and non-conformal interface pairs'),
            ('name', 'enabled', 'coupling'), self._widget,
            unscoped_note=self.tr(
                'A pair joins two prepared surfaces. Prepare the geometry '
                'first, then come back here and add the pair: adding it '
                'keeps the prepared geometry.'))
        self._interfacePairs.setObjectName('geometryInterfacePairsPanel')
        table = getattr(self._interfacePairs, 'table', None)
        if table is not None:
            # The name takes the spare width; the two short columns keep to
            # their contents, so neither is clipped and nothing scrolls.
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in range(1, table.columnCount()):
                header.setSectionResizeMode(
                    column, QHeaderView.ResizeMode.ResizeToContents)
        index = (layout.indexOf(self._advancedFolder)
                 if self._advancedFolder is not None else -1)
        if index < 0:
            layout.addWidget(self._interfacePairsFolder)
            layout.addWidget(self._interfacePairs)
        else:
            layout.insertWidget(index, self._interfacePairs)
            layout.insertWidget(index, self._interfacePairsFolder)
        self._interfacePairsFolder.setContents(self._interfacePairs)

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
            QMessageBox.warning(
                self._widget, self.tr('Geometry locked'),
                self.tr('This geometry has been prepared, so its surfaces '
                        'can no longer be edited here. Edit geometry\u2026 at '
                        'the top of this page unlocks the step; unlocking '
                        'discards the prepared geometry and everything meshed '
                        'from it.'))
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

    def _ensureSplitInterfacesButton(self):
        """Mount the control that cuts an assembly into the pieces v13 names.

        DP-421. A conjugate case is written as one surface per interface plus
        one for the outer skin, each interface carrying its own face zone and
        cell zone. Nothing on this page could produce that: an imported body
        is one closed surface, and one closed surface can be typed one thing,
        so two bodies sharing a wall came out as two cell zones each drawing
        that wall for itself. The cut is the operation; this is the way to
        reach it.

        It sits beside Import because that is where the assembly arrives and
        the moment the question is worth asking. It is offered only when the
        case holds more than one body, the cut having nothing to do
        otherwise.
        """
        button = getattr(self._ui, 'import_', None)
        if button is None:
            # Isolated widget tests do not build the generated page.
            return
        layout = button.parentWidget().layout()
        if layout is None:
            return
        if self._splitInterfaces is None:
            from PySide6.QtWidgets import QPushButton

            self._splitInterfaces = QPushButton(
                self.tr('Split interfaces'), button.parentWidget())
            self._splitInterfaces.setObjectName('geometrySplitInterfaces')
            self._splitInterfaces.setToolTip(self.tr(
                'Cut the walls two bodies share away from the walls they do '
                'not, so each shared wall becomes one interface'))
            self._splitInterfaces.clicked.connect(self._splitInterfacesClicked)
            layout.insertWidget(layout.indexOf(button), self._splitInterfaces)
        self._splitInterfaces.setVisible(self._bodyCount() > 1)

    @staticmethod
    def _bodyCount() -> int:
        """How many imported bodies the case holds."""
        from foammesh.core.geometry import GeometryArtifactStore

        project = getattr(app, 'project', None)
        if project is None or getattr(project, 'path', None) is None:
            return 0
        try:
            return len(GeometryArtifactStore(project.path).entries())
        except (OSError, ValueError):
            return 0

    @qasync.asyncSlot()
    async def _splitInterfacesClicked(self):
        """Run the cut, then show the case the way the database now holds it.

        The preview comes first because the cut writes a revision of every
        body it touches, and a user is entitled to know what is about to
        happen to a model they spent an afternoon assembling. A case whose
        bodies share nothing says so and nothing is written.
        """
        from foammesh.core.facade.errors import FacadeError

        try:
            preview = await app.facadeClient.run(
                'geometry.split_interfaces', {'preview': True})
        except FacadeError as ex:
            QMessageBox.warning(
                self._widget, self.tr('Interface split failed'), str(ex))
            return
        found = preview.payload.get('interfaces') or ()
        if not found:
            await AsyncMessageBox().information(
                self._widget, self.tr('Split interfaces'),
                self.tr('These bodies share no wall, so there is no interface '
                        'to cut. Nothing was changed.'))
            return
        listed = ', '.join(str(item['name']) for item in found)
        confirmed = await AsyncMessageBox().confirm(
            self._widget, self.tr('Split interfaces'),
            self.tr('{0} found: {1}.\n\nEach body keeps a new '
                    'revision holding only the walls it does not share, and '
                    'each shared wall becomes a surface of its own. '
                    'Continue?').format(count_text(len(found), 'interface'),
                                        listed))
        if not confirmed:
            return
        try:
            await app.facadeClient.run('geometry.split_interfaces', {})
        except FacadeError as ex:
            QMessageBox.warning(
                self._widget, self.tr('Interface split failed'), str(ex))
            return
        self._reloadFromDatabase()

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
        if not items:
            # DP-454/DP-437. Both of the product's own ways in guard this --
            # the double click at `:566` and the context menu at `:570` each
            # test the selection first -- so an empty one arrives only from
            # code, and what it used to produce was `IndexError: list index
            # out of range` from `SurfaceDialog._load` indexing `_gIds[0]`.
            # That traceback names neither the editor nor the geometry, which
            # is how eight of them in a row were recorded as eight opaque
            # gaps. This says what was asked for and what the tree held.
            showing = self._list.rowIDs()
            raise LookupError(
                'the geometry editor was asked to open on an empty selection; '
                + ('the geometry list has not been loaded' if showing is None
                   else 'the list holds '
                        + count_text(len(showing), 'row')))

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
        if not await AsyncMessageBox().confirm(self._widget, self.tr('Remove geometries'),
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
            await AsyncMessageBox().warning(self._widget, self.tr('Delete surfaces'),
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
        # DP-637. The angle is read before anything is imported: a bare
        # float() further down raised out of this slot after the CAD half of
        # a mixed selection had already gone in. The dialog refuses an
        # unreadable angle at OK; this answers any other caller the same way.
        splitAngle = None
        angleText = self._dialog.featureAngle()
        if surfaceFiles and angleText not in (None, ''):
            try:
                splitAngle = parseFeatureAngle(angleText)
            except ValueError as ex:
                QMessageBox.warning(
                    self._widget, self.tr('Import geometry'), str(ex))
                return

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
                await AsyncMessageBox().warning(
                    self._widget, self.tr('CAD support'), CAD_INSTALL_HINT)
                return
            try:
                volumes, surfaces = await self._importThroughStore(
                    cadFiles, unit, tessellation=tessellation)
            except (RuntimeError, OSError, ValueError, FacadeError) as ex:
                QMessageBox.warning(
                    self._widget, self.tr('Geometry loading error'), str(ex))
                return
        if surfaceFiles:
            if splitAngle is not None:
                # DP-819. The pieces take palette slots after every surface
                # already in the case (and any CAD faces this import writes
                # first), so the preview starts its colours there too.
                splitDialog = SplitDialog(
                    self._widget, surfaceFiles, splitAngle,
                    firstSlot=self._nextPaletteSlot(volumes, surfaces))
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
                    QMessageBox.warning(
                        self._widget, self.tr('Geometry loading error'), str(ex))
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
                    QMessageBox.warning(
                        self._widget, self.tr('Geometry loading error'), str(ex))
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
                if volume:
                    self._stampIdentity(element, volume[0], patch=False)
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
                    # DP-383. The row is about to be given a name the tree can
                    # show, which is not always the name the manifest holds:
                    # `getUniqueSeq` suffixes a name another import already
                    # took, so a second STEP's face0 becomes face01 here and
                    # stays face0 there. The identity is what survives that.
                    self._stampIdentity(element, surface)
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
                self._stampIdentity(element, surface)
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
            QMessageBox.warning(
                self._widget, self.tr('Geometry loading error'), str(ex))

    @staticmethod
    def _stampIdentity(element, surface, patch: bool = True) -> None:
        """Record which artifact and which patch record a row stands for.

        DP-383. Nothing but the display name tied the two stores together, and
        the tree is obliged to make display names unique while the manifest is
        not, so the pair diverged on the second CAD file of any import and the
        rename that follows wrote only one of them.

        ``patch`` is false for a volume row, which stands for a whole artifact
        and for no single boundary of it.
        """
        fields = [('geometryId', getattr(surface, 'geometryId', None))]
        if patch:
            fields.append(('patchUuid', getattr(surface, 'patchUuid', None)))
        else:
            # DP-419. A volume row stands for one CAD body where the import
            # could say which, and for the whole artifact where it could not.
            fields.append(('regionUuid', getattr(surface, 'regionUuid', None)))
        for field, value in fields:
            if value:
                element.setValue(field, str(value))

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
            names, uuids = _patchIdentities(entry)
            pieces = importer.loadNamedSolids(
                Path(entry['artifact']), names, fileName=source.stem,
                volumeName=entry.get('name') or source.stem,
                geometryId=entry.get('geometry_id'), uuids=uuids)
            # DP-419. The bodies the import recorded, where it recorded more
            # than one; otherwise the composition that has always run.
            bodies = _volumesFromBodies(entry, pieces)
            if bodies is None:
                fileVolumes, fileSurfaces = importer.identifyVolumes()
            else:
                fileVolumes, fileSurfaces = bodies, []
            volumes.extend(fileVolumes)
            surfaces.extend(fileSurfaces)
        return volumes, surfaces

    def _nextPaletteSlot(self, volumes, surfaces) -> int:
        """The slot the first piece of a split will take in the main window."""
        try:
            onScreen = int(self._geometryManager.nextPaletteSlot())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            onScreen = 0
        return onScreen + sum(len(volume) for volume in volumes) + len(surfaces)

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
            uuids = {}
            try:
                split = await app.facadeClient.run(
                    'geometry.patches.split_by_angle',
                    {'geometry_id': geometryId, 'angle_deg': angle,
                     'min_area_fraction': minAreaFraction})
            except ValidationFailedError as ex:
                QMessageBox.warning(
                    self._widget, self.tr('Feature angle split'), str(ex))
            else:
                payload = split.payload
                artifact = artifact.with_name(
                    f"rev{payload['revision']}{artifact.suffix}")
                for row in payload.get('patches') or ():
                    if row.get('geometry_id') != geometryId:
                        continue
                    if row.get('name') and row.get('patch_uuid'):
                        uuids[row['name']] = row['patch_uuid']
                    for ref in row.get('source_refs') or ():
                        if ref.get('original_name'):
                            names[ref['original_name']] = row['name']
            importer.loadNamedSolids(
                artifact, names, fileName=source.stem,
                volumeName=entry.get('name') or source.stem,
                geometryId=geometryId, uuids=uuids)
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
        surfaces = db.getElements('geometry', lambda i, e: e['volume'] == gId)
        if any(self._holdsActor(key) for key in (gId, *surfaces)):
            # DP-662. The dialog's commit refreshed the project before its
            # `accepted` reached us, and that refresh already read these rows
            # back; adding them again raised KeyError in the actor manager
            # and listed the shape twice. Read the case back instead.
            self._reloadGeometry()
            return
        self._addGeometry(gId, volume)

        for surfaceId in surfaces:
            self._addGeometry(surfaceId, surfaces[surfaceId], volume)

    def _addSurface(self, gId):
        db = app.facadeClient.checkout()
        if not db.hasElement('geometry', gId):
            self._reloadGeometry()
            return
        surface = db.getElement('geometry',  gId)
        if self._holdsActor(gId):
            self._reloadGeometry()
            return
        self._addGeometry(gId, surface, surface.value('volume'))

    def _holdsActor(self, gId) -> bool:
        if self._geometryManager is None:
            return False
        try:
            return self._geometryManager.actorInfo(gId) is not None
        except KeyError:
            return False

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

    def _enableStep(self):
        # self._ui.geometryList.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._ui.geometryButtons.setEnabled(True)
        if self._boundaries is not None:
            self._boundaries.setEnabled(True)
        if self._lockBanner is not None:
            self._lockBanner.setVisible(False)

    def _disableStep(self):
        # self._ui.geometryList.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._ui.geometryButtons.setEnabled(False)
        if self._boundaries is not None:
            self._boundaries.setEnabled(False)
        # DP-B3. The banner stays enabled while the step is locked: it is the
        # page saying it is locked, and the one control that unlocks it.
        if self._lockBanner is not None:
            self._lockBanner.setVisible(True)

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
