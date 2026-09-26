#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import Signal

from foammesh.support.simple_db.simple_db import Element

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType, GeometryType, Shape
from foammesh.core.mesh.sizing import stand_off_bounds
from foammesh.core.selection import (
    SelectionEntity, SelectionKind, SelectionStatus)
from foammesh.rendering.actor_info import GeometryActor, RegionMarkerActor
from foammesh.rendering.vtk_loader import (hexPolyData, cylinderPolyData, spherePolyData, polygonPolyData,
                                           planePolyData, diskPolyData, openPlatePolyData)
from foammesh.view.main_window.actor_manager import ActorManager


#: The three open searchable surfaces, by the value stored in the database.
#: Plan 31. They are volume rows like any other, but what they draw is a
#: surface rather than the boundary of a solid, so they need their own arm in
#: the polydata switch below.
OPEN_SURFACE_SHAPE_VALUES = (Shape.PLANE.value, Shape.DISK.value, Shape.PLATE.value)


def openSurfacePolyData(shape, volume):
    """What the viewport draws for a plane, a disk or a plate.

    The plane is unbounded, so what is drawn is a square marking where it sits
    and which way it faces, sized off how far from the origin the user put it
    -- the same stand-in the Add Volume preview draws, so the shape does not
    change appearance when the dialog closes.

    A plate whose span OpenFOAM would refuse draws nothing rather than drawing
    something misleading.
    """
    point1 = [float(component) for component in volume.vector('point1')]
    point2 = [float(component) for component in volume.vector('point2')]

    if shape == Shape.PLANE.value:
        extent = max(max(abs(component) for component in point1), 1.0)
        return planePolyData(point1, point2, extent)

    if shape == Shape.DISK.value:
        return diskPolyData(point1, point2, volume.float('radius'))

    return openPlatePolyData(point1, point2)


def platePolyData(shape, volume):
    x1, y1, z1 = volume.vector('point1')
    x2, y2, z2 = volume.vector('point2')

    if shape == Shape.X_MIN.value:
        return polygonPolyData([(x1, y1, z1), (x1, y1, z2), (x1, y2, z2), (x1, y2, z1)])
    elif shape == Shape.X_MAX.value:
        return polygonPolyData([(x2, y1, z1), (x2, y1, z2), (x2, y2, z2), (x2, y2, z1)])
    elif shape == Shape.Y_MIN.value:
        return polygonPolyData([(x1, y1, z1), (x2, y1, z1), (x2, y1, z2), (x1, y1, z2)])
    elif shape == Shape.Y_MAX.value:
        return polygonPolyData([(x1, y2, z1), (x2, y2, z1), (x2, y2, z2), (x1, y2, z2)])
    elif shape == Shape.Z_MIN.value:
        return polygonPolyData([(x1, y1, z1), (x1, y2, z1), (x2, y2, z1), (x2, y1, z1)])
    elif shape == Shape.Z_MAX.value:
        return polygonPolyData([(x1, y1, z2), (x1, y2, z2), (x2, y2, z2), (x2, y1, z2)])


class GeometryManager(ActorManager):
    #: The region whose form is open, if any. Its glyph is not drawn.
    _suppressedRegion = None

    selectedActorsChanged = Signal(list)

    def __init__(self):
        super().__init__()
        self._findingHighlight = None
        self._selectionHiddenActors = set()

        self._displayControl.selectedActorsChanged.connect(self.selectedActorsChanged)
        self.selectedActorsChanged.connect(
            app.selectionService.viewport_picked)
        app.selectionService.bind_renderer(self._applySelectionState)

    def highlightLocations(self, locations):
        """Show diagnostic locations as a transient, non-persistent overlay."""
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        self.clearFindingHighlight()
        valid = [tuple(map(float, location)) for location in locations
                 if isinstance(location, (list, tuple)) and len(location) == 3]
        if not valid:
            return
        points, vertices = vtkPoints(), vtkCellArray()
        for location in valid:
            point_id = points.InsertNextPoint(*location)
            vertices.InsertNextCell(1)
            vertices.InsertCellPoint(point_id)
        data = vtkPolyData()
        data.SetPoints(points)
        data.SetVerts(vertices)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('geometryFindingLocations')
        actor.GetProperty().SetColor(1.0, .25, .08)
        actor.GetProperty().SetPointSize(12)
        actor.GetProperty().RenderPointsAsSpheresOn()
        self._findingHighlight = actor
        self._displayControl.addOverlay(actor)

    def clearFindingHighlight(self):
        if self._findingHighlight is not None:
            self._displayControl.removeOverlay(self._findingHighlight)
            self._findingHighlight = None

    def clear(self):
        self.clearFindingHighlight()
        app.selectionService.remove_owner('geometry-db')
        super().clear()

    def subSurfaces(self, gId):
        return app.facadeClient.checkout().getElements(
            'geometry', lambda i, e: e['volume'] == gId)

    def polyData(self, gId):
        return self._actorInfos[gId].dataSet()

    def referenceSurface(self):
        """DP-713. The loaded geometry as one surface to measure a mesh by.

        Viewport audit 0925 F8: deviation could only be coloured from a
        stored fidelity run, although the geometry the mesh was made from is
        loaded right here. A surface marked as refinement only (CFD type
        ``none``) shapes the cells, not the boundary, so it is left out --
        unless it is all there is. ``None`` when there is no geometry.
        """
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData

        surfaces = {
            gId: info.dataSet() for gId, info in self._actorInfos.items()
            if isinstance(info, GeometryActor)
            and info.dataSet() is not None
            and info.dataSet().GetNumberOfCells() > 0}
        if not surfaces:
            return None
        try:
            geometries = app.facadeClient.checkout().getElements('geometry')
        except Exception:                                     # noqa: BLE001
            geometries = {}

        def refinementOnly(gId):
            geometry = geometries.get(gId) if geometries else None
            try:
                return (geometry is not None
                        and geometry.value('cfdType') == CFDType.NONE.value)
            except (KeyError, LookupError, TypeError):
                return False

        kept = [data for gId, data in surfaces.items()
                if not refinementOnly(gId)] or list(surfaces.values())
        append = vtkAppendPolyData()
        for data in kept:
            append.AddInputData(data)
        append.Update()
        return append.GetOutput()

    def load(self):
        self.clear()
        self._visibility = True

        geometries = app.facadeClient.checkout().getElements('geometry')
        entities = []
        for gId, geometry in geometries.items():
            self._add(gId, geometry, geometries.get(geometry.value('volume')))
            geometry_type = geometry.value('gType')
            if geometry_type == GeometryType.VOLUME.value:
                actor_ids = tuple(
                    str(child_id) for child_id, child in geometries.items()
                    if str(child.value('volume')) == str(gId))
                kind = SelectionKind.CLOSED_VOLUME
            else:
                actor_ids = (str(gId),)
                kind = SelectionKind.SURFACE_GROUP
            entities.append(SelectionEntity(
                str(gId), str(geometry.value('name')), kind,
                actor_ids, SelectionStatus.VALID, owner='geometry-db',
                metadata=(('cfd_type', geometry.value('cfdType')),)))
        app.selectionService.synchronize(entities, owner='geometry-db')

        self._assignPalettes()
        # The seed markers come last so the glyph is sized from the model that
        # is actually loaded, not from an empty scene.
        self.reloadRegions()
        self.fitDisplay()

    def suppressRegionMarker(self, region_id):
        """Stand one seed glyph down while its form owns the point.

        Editing a region puts an interactive handle on the same coordinate.
        Two markers on one seed, one of which stays where the point used to
        be while the other is dragged, reads as two seeds - which is exactly
        the confusion the marker was added to remove.
        """
        suppressed = None if region_id is None else str(region_id)
        if suppressed == self._suppressedRegion:
            # Closing a form releases a marker that was never held: every add
            # and every cancel comes through here, and rebuilding the seeds
            # for a state that has not changed is a scene rebuild for nothing.
            return
        self._suppressedRegion = suppressed
        self.reloadRegions()

    def reloadRegions(self):
        """Rebuild the region seed markers from what the database now holds.

        Adding, moving or deleting a region used to change a card and nothing
        else: the viewport had never drawn the seed, so there was nothing to
        keep in step. Now there is, and every one of those three paths comes
        back through here.
        """
        for key in [key for key in tuple(self._actorInfos)
                    if str(key).startswith('region:')]:
            self.remove(key)

        kept = [entity for entity in app.selectionService.entities()
                if entity.owner == 'geometry-db'
                and entity.kind is not SelectionKind.REGION]
        regions = []
        bounds = None
        measured = False
        for region_id, region in app.facadeClient.checkout().getElements(
                'region').items():
            try:
                if region.value('gType') is not None:
                    continue
            except (LookupError, TypeError):
                pass
            if not measured:
                # The glyph is sized from the model, and reading the model
                # bounds walks every actor in the scene. A case with no seeds
                # in it - which is every case until someone adds one - should
                # not pay for that on each reload.
                bounds, measured = self.getBounds(), True
            marker = (None if str(region_id) == self._suppressedRegion
                      else self._addRegionMarker(region_id, region, bounds))
            regions.append(SelectionEntity(
                f'region:{region_id}', str(region.value('name')),
                SelectionKind.REGION,
                (marker,) if marker is not None else (),
                SelectionStatus.VALID, owner='geometry-db'))
        app.selectionService.synchronize(kept + regions, owner='geometry-db')
        self.applyToDisplay()

    def _addRegionMarker(self, region_id, region, bounds):
        """Draw one region seed, and answer with the actor id it took.

        A region with no point yet, or one whose point is unreadable, simply
        has no marker: an entity with no actor still selects and still shows
        its row, which is what an unplaced seed should look like.
        """
        try:
            point = region.vector('point')
        except (LookupError, TypeError, ValueError):
            return None
        marker = RegionMarkerActor.build(
            region_id, region.value('name'), point, bounds)
        if marker is None:
            return None
        if marker.id() in self._actorInfos:
            self.remove(marker.id())
        self.add(marker)
        return marker.id()

    def _assignPalettes(self):
        """Give each geometry surface its own colour.

        The mesh side has done this since Plan 27 WP2 -- "every one of them
        used to render in the same default white, so telling them apart meant
        clicking each in turn" -- and the geometry side never got the same
        treatment. So an imported model arrived as one undifferentiated grey
        solid whatever it was made of, which is the opposite of what someone
        opens a multi-part model to see.

        Assignment is by sorted id, so a surface keeps its colour across
        reloads; one that changed colour every time would be worse than none.
        A surface the user has coloured by hand keeps that choice -- palette
        slots only ever set the *default*.
        """
        self.assignPatchPalette(GeometryActor, 'patch')
        # The palette is assigned only once every actor is in, so the colours
        # the viewport overlay picked up while they were being added one at a
        # time are a step behind. Say so once, at the end.
        self._displayControl.partsChanged.emit()

    def addGeometry(self, gId, geometry, volume):
        self._add(gId, geometry, volume)
        app.selectionService.register(SelectionEntity(
            str(gId), str(geometry.value('name')),
            SelectionKind.SURFACE_GROUP
            if geometry.value('gType') == GeometryType.SURFACE.value
            else SelectionKind.CLOSED_VOLUME,
            (str(gId),), owner='geometry-db'))

        # A newly imported surface takes a slot too, and the re-assignment
        # keeps every surface's colour a function of the set rather than of
        # the order things happened to be added in.
        self._assignPalettes()
        self.applyToDisplay()

    def updateCustomSurfaces(self, volume, surfaces):
        for gId, surface in surfaces.items():
            self.update(gId, self._surfaceToPolyData(surface, volume))

        self.applyToDisplay()

    def renameGeometry(self, gId, name):
        """Move one name to every place the viewport shows it.

        `geometry.rename` moves the two *stores* - the database row and the
        artifact's patch manifest - and the page used to answer it by
        re-reading the tree row alone. So the actor kept its old name (and
        with it the display-control row and the parts overlay), and the
        selection entity, which is the label the tree, the display control
        and the scope pickers all read, was never re-registered at all.
        """
        gId, name = str(gId), str(name)
        if gId in self._actorInfos and self._actorInfos[gId].name() != name:
            self._updateActorName(gId, name)
        entity = app.selectionService.entity(gId)
        if entity is not None and entity.label != name:
            # Replace the label and nothing else: the kind, the actor ids and
            # the metadata are what the scope pickers read.
            app.selectionService.register(
                SelectionEntity(entity.stable_id, name, entity.kind,
                                entity.geometry_ids, entity.status,
                                owner=entity.owner,
                                removable=entity.removable,
                                metadata=entity.metadata))

    def updateIndependentSurface(self, gId, surface: Element):
        self._updateActorName(gId, surface.value('name'))
        if surface.enum('shape') == Shape.TRI_SURFACE_MESH:
            self.update(gId, self._surfaceToPolyData(surface))

        self.applyToDisplay()

    def removeGeometry(self, gIds):
        for gId in gIds:
            self.remove(gId)
        retained = [
            entity for entity in app.selectionService.entities()
            if entity.owner == 'geometry-db'
            and entity.stable_id not in {str(value) for value in gIds}]
        app.selectionService.synchronize(retained, owner='geometry-db')

        self.applyToDisplay()

    def show(self):
        self._show()

    def selectActors(self, ids):
        self._displayControl.setSelectedActors(ids)

    def _applySelectionState(self, snapshot, entities):
        roles = {}
        hidden = set()
        for stable_id, entity in entities.items():
            actor_ids = entity.geometry_ids or (stable_id,)
            if stable_id in snapshot.hidden_ids:
                hidden.update(actor_ids)
            role = None
            if entity.status is SelectionStatus.ORPHAN:
                role = 'invalid'
            elif entity.status is SelectionStatus.STALE:
                role = 'stale'
            elif stable_id in snapshot.preview_ids:
                role = 'preview'
            elif stable_id in snapshot.selected_ids:
                role = 'selected'
            if role:
                for actor_id in actor_ids:
                    roles[actor_id] = role
        selected_actor_ids = [
            actor_id for actor_id, role in roles.items()
            if role in {'selected', 'preview'}]
        self._displayControl.setSelectedActors(selected_actor_ids)
        for actor_id in self._selectionHiddenActors - hidden:
            if actor_id in self._actorInfos:
                self._actorInfos[actor_id].setVisible(True)
        for actor_id, actor_info in self._actorInfos.items():
            actor_info.setVisible(actor_id not in hidden)
            actor_info.setHighlightRole(roles.get(actor_id))
        self._selectionHiddenActors = hidden
        self._displayControl.refreshView()

    def getBoundingHex6(self):
        db = app.facadeClient.checkout()
        boundingHex6 = db.getValue('baseGrid/boundingHex6')  # can be "None"
        if boundingHex6 is None:
            return None, None

        geometry = db.getElement('geometry', boundingHex6)
        if (geometry is None
                or geometry.value('gType') != GeometryType.VOLUME.value
                or geometry.value('shape') != Shape.HEX6.value):
            return None, None

        return boundingHex6, geometry

    def isBoundingHex6(self, gId):
        db = app.facadeClient.checkout()
        if not db.hasElement('geometry', gId):
            return False

        boundingHex6 = db.getValue('baseGrid/boundingHex6')  # can be "None"

        geometry = db.getElement('geometry', gId)
        if geometry.value('gType') == GeometryType.VOLUME.value:
            if gId == boundingHex6:
                return True
        elif geometry.value('gType') == GeometryType.SURFACE.value:
            if geometry.value('shape') in Shape.PLATES.value and geometry.value('volume') == boundingHex6:
                return True

        return False

    def getCellSize(self):
        """The base cell of the block that is actually meshed.

        R175. This divided the raw geometry extent by the cell counts, so
        every "cell size (...)" the Castellation refinement editors print was
        the cell of a block that no longer exists once a standoff is set --
        smaller than the truth by exactly the standoff, and smaller still at
        every level below. Refinement levels are chosen against these
        numbers, so a user sizing a boundary layer or a feature refinement
        was sizing it against the wrong cell. A Hex6 the user modelled is
        left alone, the same as on the Base grid page.
        """
        gId, geometry = self.getBoundingHex6()

        db = app.facadeClient.checkout()
        baseGrid = db.getElement('baseGrid')

        if geometry is None:
            x1, x2, y1, y2, z1, z2 = stand_off_bounds(
                self.getBounds().toTuple(), baseGrid.float('standoff'))
        else:
            x1, y1, z1 = geometry.vector('point1')
            x2, y2, z2 = geometry.vector('point2')

        return ((x2 - x1) / baseGrid.float('numCellsX'),
                (y2 - y1) / baseGrid.float('numCellsY'),
                (z2 - z1) / baseGrid.float('numCellsZ'))


    def _add(self, gId, geometry, volume):
        if geometry.value('gType') == GeometryType.SURFACE.value:
            self.add(GeometryActor(self._surfaceToPolyData(geometry, volume), gId, geometry.value('name')))

    def _surfaceToPolyData(self, surface, volume=None):
        shape = surface.value('shape')

        if shape == Shape.TRI_SURFACE_MESH.value:
            polyData = app.facadeClient.checkout().geometryPolyData(surface.value('path'))
        else:
            if shape == Shape.HEX.value:
                polyData = hexPolyData(volume.vector('point1'), volume.vector('point2'))
            elif shape == Shape.CYLINDER.value:
                polyData = cylinderPolyData(volume.vector('point1'), volume.vector('point2'), volume.float('radius'))
            elif shape == Shape.SPHERE.value:
                polyData = spherePolyData(volume.vector('point1'), volume.float('radius'))
            elif shape in OPEN_SURFACE_SHAPE_VALUES:
                # Plan 31. Without this branch the three open surfaces fell
                # into the hex6 arm below, which answers `None` for any shape
                # that is not one of the six face names -- so a plane, disk or
                # plate the user had just added would be invisible in the
                # viewport while sitting in the tree and in the dictionary.
                polyData = openSurfacePolyData(shape, volume)
            else:  # Shape.HEX6.value
                polyData = platePolyData(shape, volume)

        return polyData
