#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor

from foammesh.support.simple_db.simple_db import Element

from foammesh.app import app
from foammesh.db.configurations_schema import CFDType, GeometryType, Shape
from foammesh.core.mesh.sizing import stand_off_bounds
from foammesh.core.selection import (
    SelectionEntity, SelectionKind, SelectionStatus)
from foammesh.rendering.actor_info import GeometryActor, RegionMarkerActor
from foammesh.rendering.vtk_loader import (hexPolyData, cylinderPolyData, spherePolyData, polygonPolyData,
                                           planePolyData, diskPolyData, openPlatePolyData)
from foammesh.view.main_window.actor_manager import ActorManager, _union


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


#: DP-818. Colours a seed wears when the theme cannot be read, by verdict.
_SEED_FALLBACK_COLOURS = {
    'inside': '#2e9d60',
    'outside': '#d64545',
    'on_surface': '#d64545',
    'unknown': '#d9a21b',
    'open_to_outside': '#d64545',
}
_SEED_STATUS_TOKENS = {
    'inside': 'status.success',
    'outside': 'status.error',
    'on_surface': 'status.error',
    'unknown': 'status.warning',
    # Plan 36 RP6: the classifier says inside, but the space the seed is in
    # reaches the domain boundary -- the surface leaks.
    'open_to_outside': 'status.error',
}

#: DP-818. What the geometry fades to while a seed is being placed: enough to
#: see a point sitting in a hole through the wall around it, enough left to
#: see where the wall is.
SEED_PLACING_OPACITY = 0.25


def seedColour(verdict) -> str:
    """The themed colour for a seed verdict: green in, red out."""
    token = _SEED_STATUS_TOKENS.get(verdict, 'status.warning')
    try:
        tokens = app.themeManager.tokens if app.themeManager else None
        if tokens is not None:
            value = tokens.value(token)
            if value:
                return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return _SEED_FALLBACK_COLOURS.get(verdict, '#d9a21b')


def _caseId():
    """The open case's id, or ``None`` (Plan 36 RP13 #7: the region volume
    jobs are tagged with it, and an answer for a closed case is dropped)."""
    try:
        case = app.facadeClient.case_id
    except Exception:                                         # noqa: BLE001
        return None
    return None if case is None else str(case)


class GeometryManager(ActorManager):
    #: The region whose form is open, if any. Its glyph is not drawn.
    _suppressedRegion = None
    #: DP-818. Seed glyphs are a Domain & Regions landmark, drawn while that
    #: page is shown and nowhere else.
    _regionMarkersShown = False

    selectedActorsChanged = Signal(list)
    #: Plan 36 RP6. The space of the seed being placed is known now (the
    #: labelling job came back), so what the form says about it can change.
    seedSpaceChanged = Signal()

    def __init__(self):
        super().__init__()
        self._findingHighlight = None
        self._selectionHiddenActors = set()
        #: DP-818. The inside/outside test, built once per geometry state.
        self._seedClassifier = None
        self._seedClassifierKey = None
        #: DP-818. The live seed being placed, and the opacities the geometry
        #: had before it was faded for placing it.
        self._seedPreview = None
        #: Plan 37 UF16. The exclude point being placed: a handle of its own.
        self._excludePreview = None
        self._fadedOpacities = None
        #: Plan 36 RP6. The labelled spaces and their volumes, built once
        #: per geometry and domain; the fade the walls are at now.
        self._regionVolumes = None
        self._seedFade = SEED_PLACING_OPACITY

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
        self._clearSeedPreview()
        self._clearExcludePreview()
        # The actors whose opacity was saved are about to go.
        self._fadedOpacities = None
        if self._regionVolumes is not None:
            self._regionVolumes.end()
            self._regionVolumes.reset()
        app.selectionService.remove_owner('geometry-db')
        super().clear()

    def dispose(self):
        """Release the actors and the one global that still holds this manager.

        Plan 35 CR3 disposes the closing case's managers so that nothing is
        left for the cyclic collector. The selection service is app-wide and
        outlives every case, and `__init__` handed it a bound method of this
        manager as its renderer -- so a disposed manager stayed reachable
        from it until the next case bound a new one, or, after the last case,
        until interpreter finalization collected a QObject whose module and
        type objects had already been torn down: an access violation at exit
        (MEASURED: every test file that builds a manager and never replaces it
        exited 3221225477 after all its tests passed). The binding is taken
        back only if it is still this manager's, so disposing an old manager
        after a new one has bound cannot blank the new one's highlights.
        """
        super().dispose()
        service = app.selectionService
        if getattr(service, '_renderer', None) == self._applySelectionState:
            service.bind_renderer(None)
        try:
            self._displayControl.selectedActorsChanged.disconnect(
                self.selectedActorsChanged)
        except (RuntimeError, TypeError, AttributeError):
            pass                    # never connected, or a stub without it

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

    def getSurfaceBounds(self):
        """The extent of the model itself, without the region seed glyphs.

        DP-821. `getBounds` is every actor this manager holds, and the seed
        glyphs are held here too, so a seed placed off the geometry -- an
        external-flow seed, or a mistyped one -- grew the box the Base Grid
        page derived by the seed's distance plus the glyph's radius. The
        block that is written is derived from the surfaces alone.
        """
        return _union(info.bounds() for info in self._actorInfos.values()
                      if not isinstance(info, RegionMarkerActor))

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
        # One checkout serves the seeds and the exclude points: a reload is
        # a read of the region side of the store, not two whole copies of it
        # (DP-397 counts every checkout a refresh asks for).
        db = app.facadeClient.checkout()
        for region_id, region in db.getElements('region').items():
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
        self._reloadExcludeMarkers(bounds if measured else None, db)
        self.applyToDisplay()

    def _reloadExcludeMarkers(self, bounds=None, db=None):
        """Plan 37 UF16. Draw every stored exclude point with the seeds.

        A cube in the warning colour, shown and hidden with the seed glyphs
        (it is a `RegionMarkerActor`, so it never grows the base-grid box
        either, DP-821).
        """
        from foammesh.rendering.actor_info import ExcludeMarkerActor
        from foammesh.rendering.seed_gizmo import excludeColour

        for key in [key for key in tuple(self._actorInfos)
                    if str(key).startswith('exclude:')]:
            self.remove(key)
        try:
            if db is None:
                db = app.facadeClient.checkout()
            rows = db.getElements('castellation/excludePoints')
        except Exception:                                     # noqa: BLE001
            return
        rows = dict(rows or {})
        if not rows:
            return
        if bounds is None:
            try:
                bounds = self.getBounds()
            except (AttributeError, TypeError, ValueError):
                return          # a scene that cannot be measured draws none
        colour = QColor(excludeColour())
        for point_id, row in rows.items():
            try:
                point = row.vector('point')
                name = row.value('name')
            except (LookupError, TypeError, ValueError, AttributeError):
                continue
            marker = ExcludeMarkerActor.build(point_id, name, point, bounds)
            if marker is None:
                continue
            self.add(marker)
            marker.setColor(colour)
            marker.setVisible(self._regionMarkersShown)

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
        # DP-818. Green in the fluid, red in a hole or beyond the wall, so a
        # seed in the wrong space is visible without opening its form.
        marker.setColor(QColor(seedColour(self.classifySeed(point))))
        marker.setVisible(self._regionMarkersShown)
        return marker.id()

    # -- DP-818: where a seed is, and whether it is in the fluid -------------

    def _seedComponents(self):
        """The closed parts a seed is judged against, with a cache key.

        Plan 36 RP3. The choice itself is core's ``seed_components``, the one
        the headless fluid-space field makes too, so the viewport and a
        launch never judge a seed against different parts.
        """
        from foammesh.core.mesh.seed_classifier import seed_components

        surfaces = {gId: info.dataSet() for gId, info in self._actorInfos.items()
                    if isinstance(info, GeometryActor)}
        if not surfaces:
            return [], ()
        try:
            db = app.facadeClient.checkout()
            geometries = db.getElements('geometry') or {}
        except Exception:                                     # noqa: BLE001
            db, geometries = None, {}
        try:
            boundingHex6 = db.getValue('baseGrid/boundingHex6') if db else None
        except Exception:                                     # noqa: BLE001
            boundingHex6 = None
        return seed_components(surfaces, geometries, bounding_hex6=boundingHex6)

    def seedClassifier(self):
        """The inside/outside test for the loaded geometry, built once.

        Built again only when a surface is added, removed or changed, so a
        form can ask it on every keystroke without re-reading the model.
        """
        from foammesh.core.mesh.seed_classifier import SeedClassifier

        components, key = self._seedComponents()
        if self._seedClassifier is None or key != self._seedClassifierKey:
            try:
                self._seedClassifier = SeedClassifier(components)
            except Exception:                                 # noqa: BLE001
                self._seedClassifier = SeedClassifier([])
            self._seedClassifierKey = key
        return self._seedClassifier

    def classifySeed(self, point) -> str:
        """'inside', 'outside', 'on_surface' or 'unknown' for *point*."""
        return self.seedClassifier().classify(point)

    def setRegionMarkersShown(self, shown: bool):
        """Show the seed glyphs while Domain & Regions is up, hide them after."""
        shown = bool(shown)
        if shown == self._regionMarkersShown:
            return
        self._regionMarkersShown = shown
        for key, info in self._actorInfos.items():
            if isinstance(info, RegionMarkerActor):
                info.setVisible(shown and key not in self._selectionHiddenActors)
        self._displayControl.refreshView()

    def regionMarkersShown(self) -> bool:
        return self._regionMarkersShown

    def previewSeed(self, point):
        """Draw the seed being placed, coloured by where it is.

        Plan 36 RP3: the seed is a handle the user drags (``SeedGizmo``), one
        per placement, moved rather than rebuilt as the point changes.
        *point* ``None`` takes it away. Answers the verdict, so the form's
        status line and the handle cannot disagree.
        """
        if point is None:
            self._clearSeedPreview()
            return None
        try:
            centre = tuple(float(component) for component in point)
        except (TypeError, ValueError):
            self._clearSeedPreview()
            return None
        if len(centre) != 3:
            self._clearSeedPreview()
            return None
        verdict = self.classifySeed(centre)
        # Plan 36 RP6. Only visibilities change here: the space's actor was
        # built when the labelling job came back.
        volumes = self._regionVolumes
        spaceChanged = False
        if volumes is not None and volumes.isActive():
            spaceChanged = volumes.place(centre)
            space = volumes.editedSpace()
            if (verdict == 'inside' and space is not None and space.label
                    and space.outside and not volumes.externalFlow()):
                verdict = 'open_to_outside'
            faded = self._fadeForVolumes(volumes.anyShown())
            spaceChanged = spaceChanged or faded
        colour = seedColour(verdict)
        gizmo = self._seedPreview
        if gizmo is None:
            from foammesh.rendering.seed_gizmo import SeedGizmo

            view = getattr(self._displayControl, 'view', None)
            gizmo = SeedGizmo(view() if callable(view) else None, centre)
            self._seedPreview = gizmo
            gizmo.setColour(colour)
            # Plan 36 RP10: the shape says it too, not the colour alone.
            gizmo.setVerdict(verdict)
            if volumes is not None and volumes.isActive():
                volumes.setHandle(gizmo)
            self._displayControl.refreshView()
            return verdict
        if gizmo.position() != centre:
            # The gizmo's own move draws the frame; the volumes' visibilities
            # were switched above, so they land in that same frame.
            gizmo.setPosition(centre)                 # draws itself
        elif spaceChanged:
            self._displayControl.refreshView()
        if gizmo.colour() != colour:
            # A drag frame has already been drawn; only a verdict that
            # changed on it costs a second one.
            gizmo.setColour(colour)
            gizmo.setVerdict(verdict)
            self._displayControl.refreshView()
        elif gizmo.verdict() != verdict:
            gizmo.setVerdict(verdict)
            self._displayControl.refreshView()
        return verdict

    def seedPreview(self):
        """The seed handle being placed, or ``None``."""
        return self._seedPreview

    # -- Plan 37 UF16: the exclude point being placed ------------------------

    def previewExclude(self, point):
        """Draw the exclude point being placed; ``None`` takes it away.

        The same draggable handle as a seed's, as a cube in the warning
        colour (`SeedGizmo` role ``exclude``), moved rather than rebuilt as
        the point changes. Its shape is crossed only on a wall, where v13
        ignores it. Answers the inside/outside verdict of the point.
        """
        if point is None:
            self._clearExcludePreview()
            return None
        try:
            centre = tuple(float(component) for component in point)
        except (TypeError, ValueError):
            self._clearExcludePreview()
            return None
        if len(centre) != 3:
            self._clearExcludePreview()
            return None
        verdict = self.classifySeed(centre)
        gizmo = self._excludePreview
        if gizmo is None:
            from foammesh.rendering.seed_gizmo import EXCLUDE, SeedGizmo

            view = getattr(self._displayControl, 'view', None)
            gizmo = SeedGizmo(view() if callable(view) else None, centre,
                              role=EXCLUDE)
            self._excludePreview = gizmo
            gizmo.setVerdict(verdict)
            self._displayControl.refreshView()
            return verdict
        if gizmo.position() != centre:
            gizmo.setPosition(centre)                 # draws itself
        if gizmo.verdict() != verdict:
            gizmo.setVerdict(verdict)
            self._displayControl.refreshView()
        return verdict

    def excludePreview(self):
        """The exclude-point handle being placed, or ``None``."""
        return self._excludePreview

    def _clearExcludePreview(self):
        if self._excludePreview is not None:
            gizmo, self._excludePreview = self._excludePreview, None
            gizmo.close()
            gizmo.deleteLater()

    # -- Plan 36 RP6: the space a seed will mesh -----------------------------

    def regionVolumes(self):
        """The labelled spaces and their volume actors (built on first use)."""
        if self._regionVolumes is None:
            from foammesh.rendering.region_volume_actor import RegionVolumes

            self._regionVolumes = RegionVolumes(self._displayControl)
            self._regionVolumes.spacesReady.connect(self._regionSpacesReady)
            # RP13 #7: the surfaces come after the spaces, on demand.
            self._regionVolumes.surfacesReady.connect(
                self._regionSpacesReady)
        return self._regionVolumes

    def beginRegionVolumes(self, region_id, box) -> bool:
        """A region's form opened on the domain *box*: label it, show spaces.

        The labelling runs on the VTK worker thread and is cached beside the
        case, so this returns at once; the spaces appear when it comes back.
        Every other region's space is drawn at the stored opacity, the one
        being edited at the edited opacity. Answers False when there is
        nothing to label.
        """
        from foammesh.core.mesh import fluid_regions
        from foammesh.rendering.region_volume_actor import (
            nextRegionColour, regionColours)

        volumes = self.regionVolumes()
        components, key = self._seedComponents()
        if not components or box is None:
            volumes.end()
            return False
        try:
            baseCell = min(abs(float(size)) for size in self.getCellSize())
        except Exception:                                     # noqa: BLE001
            baseCell = None
        try:
            cacheDir = fluid_regions.cache_dir(app.facadeClient.case_root)
        except Exception:                                     # noqa: BLE001
            cacheDir = None
        edited = None if region_id is None else str(region_id)
        try:
            regions = app.facadeClient.checkout().getElements('region') or {}
        except Exception:                                     # noqa: BLE001
            regions = {}
        ids, points = [], {}
        for rid, region in regions.items():
            try:
                if region.value('gType') is not None:
                    continue
            except (LookupError, TypeError):
                pass
            ids.append(str(rid))
            try:
                points[str(rid)] = tuple(region.vector('point'))
            except (LookupError, TypeError, ValueError):
                pass
        colours = regionColours(ids)
        stored = [(colours[rid], point) for rid, point in points.items()
                  if rid != edited]
        editedColour = (colours[edited] if edited in colours
                        else nextRegionColour(ids))
        volumes.request(components, box, base_cell=baseCell,
                        cache_dir=cacheDir,
                        key=(key, None if cacheDir is None else str(cacheDir)),
                        case=_caseId())
        volumes.begin(stored, editedColour)
        if self._seedPreview is not None:
            volumes.setHandle(self._seedPreview)
            volumes.place(self._seedPreview.position())
        self._fadeForVolumes(volumes.anyShown())
        self._displayControl.refreshView()
        return True

    def endRegionVolumes(self) -> None:
        """The form closed: every space is hidden; the field is kept."""
        if self._regionVolumes is not None and self._regionVolumes.isActive():
            self._regionVolumes.end()
            self._fadeForVolumes(False)
            self._displayControl.refreshView()

    def setExternalFlow(self, external: bool) -> None:
        """With external flow, the space round the body is a region too."""
        self.regionVolumes().setExternalFlow(external)

    def showRegionCandidates(self, candidates, box=None) -> bool:
        """Plan 36 RP7. Draw the spaces "how many fluid regions?" lists.

        *candidates* are the detection panel's rows; each ticked one is
        drawn in its colour, found by its seed in this viewport's own
        labelling of *box* -- requested here, on the worker thread, when
        there is none for these inputs yet. ``[]`` hides them again.
        """
        from foammesh.core.mesh import fluid_regions

        volumes = self.regionVolumes()
        candidates = list(candidates or ())
        if candidates and box is not None:
            components, key = self._seedComponents()
            if components:
                try:
                    baseCell = min(abs(float(size))
                                   for size in self.getCellSize())
                except Exception:                             # noqa: BLE001
                    baseCell = None
                try:
                    cacheDir = fluid_regions.cache_dir(
                        app.facadeClient.case_root)
                except Exception:                             # noqa: BLE001
                    cacheDir = None
                volumes.request(
                    components, box, base_cell=baseCell, cache_dir=cacheDir,
                    key=(key, None if cacheDir is None else str(cacheDir)),
                    case=_caseId())
        return volumes.showCandidates(candidates)

    def seedSpace(self) -> dict:
        """What is known about the space of the seed being placed.

        ``working``: the labelling job is still running. ``space``: RP5's
        ``SpaceAt`` for the seed (``None`` unknown or off the domain).
        ``name``: 'Fluid space N', by rank among the enclosed spaces.
        ``inExtent``: the seed is within the model's own extent -- a core
        or a hole rather than beyond the surface. ``inCore``: DP-923, the
        seed is in outside space the surface wraps -- an open core, a bore,
        a hollow -- which the extent alone cannot tell from a point beside
        an elbow in its bounding box. ``external``: external
        flow is on. ``note``: RP13 #7, a space drawn as its box or not at
        all (past the triangle budget), else ``None``.
        """
        volumes = self._regionVolumes
        if volumes is None or not volumes.isActive():
            return {'active': False}
        space = volumes.editedSpace()
        inExtent = False
        gizmo = self._seedPreview
        if gizmo is not None:
            try:
                x1, x2, y1, y2, z1, z2 = self.getSurfaceBounds().toTuple()
                x, y, z = gizmo.position()
                inExtent = (x1 <= x <= x2 and y1 <= y <= y2
                            and z1 <= z <= z2)
            except Exception:                                 # noqa: BLE001
                inExtent = False
        inCore = False
        if gizmo is not None and space is not None and space.outside:
            from foammesh.core.mesh.fluid_regions import wrapped_at
            try:
                inCore = wrapped_at(volumes.field(), gizmo.position())
            except Exception:                                 # noqa: BLE001
                inCore = False
        return {'active': True, 'working': volumes.isWorking(),
                'space': space,
                'name': volumes.spaceName(space.label) if space else None,
                'inExtent': inExtent, 'inCore': inCore,
                'external': volumes.externalFlow(),
                'error': volumes.error(), 'note': volumes.statusNote()}

    def _regionSpacesReady(self):
        volumes = self._regionVolumes
        if volumes is None or not volumes.isActive():
            return
        if self._seedPreview is not None:
            volumes.place(self._seedPreview.position())
        self._fadeForVolumes(volumes.anyShown())
        self._displayControl.refreshView()
        self.seedSpaceChanged.emit()

    def _fadeForVolumes(self, shown: bool) -> bool:
        """Walls to 0.15 while a volume is shown, back to 0.25 after."""
        from foammesh.rendering.region_volume_actor import VOLUME_WALL_OPACITY

        target = VOLUME_WALL_OPACITY if shown else SEED_PLACING_OPACITY
        if self._fadedOpacities is None or target == self._seedFade:
            return False
        self._seedFade = target
        for gId, opacity in self._fadedOpacities.items():
            info = self._actorInfos.get(gId)
            if info is not None:
                info.setOpacity(min(float(opacity if opacity is not None
                                          else 1.0), target))
        return True

    def _clearSeedPreview(self):
        if self._seedPreview is not None:
            gizmo, self._seedPreview = self._seedPreview, None
            if self._regionVolumes is not None:
                # The busy ring lives in the handle's renderer.
                self._regionVolumes.setHandle(None)
            gizmo.close()
            gizmo.deleteLater()

    def fadeGeometryForSeed(self, on: bool):
        """See through the walls while a seed is placed, then put them back.

        A seed in the core of a pipe is hidden by the pipe. The surfaces are
        faded through the same per-part opacity the Display Control sets, and
        each part gets back exactly the opacity it had -- including one the
        user chose -- when the form closes.
        """
        if on:
            if self._fadedOpacities is not None:
                return
            self._seedFade = SEED_PLACING_OPACITY
            self._fadedOpacities = {}
            for gId, info in self._actorInfos.items():
                if not isinstance(info, GeometryActor):
                    continue
                opacity = info.properties().opacity
                self._fadedOpacities[gId] = opacity
                info.setOpacity(min(float(opacity if opacity is not None
                                          else 1.0), SEED_PLACING_OPACITY))
        else:
            if self._fadedOpacities is None:
                return
            for gId, opacity in self._fadedOpacities.items():
                info = self._actorInfos.get(gId)
                if info is not None:
                    info.setOpacity(opacity)
            self._fadedOpacities = None
            self._seedFade = SEED_PLACING_OPACITY
        refreshTransparency = getattr(
            self._displayControl, 'refreshTransparency', None)
        if refreshTransparency is not None:
            refreshTransparency()
        self._displayControl.refreshView()

    def geometryFaded(self) -> bool:
        return self._fadedOpacities is not None

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

    def nextPaletteSlot(self) -> int:
        """The palette slot the next surface written to the case will take.

        DP-819. Slots go out in id order and a new row always gets a larger
        id, so the next surface takes the slot after every surface on screen.
        The split preview starts its colours here to match.
        """
        return sum(1 for info in self._actorInfos.values()
                   if isinstance(info, GeometryActor))

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
            actor_info.setVisible(
                actor_id not in hidden
                and (self._regionMarkersShown
                     or not isinstance(actor_info, RegionMarkerActor)))
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
