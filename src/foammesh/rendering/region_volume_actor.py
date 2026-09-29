"""The space a region's seed will mesh, drawn in the viewport.

Plan 36 RP6. A snappy region is a seed, and snappy keeps the connected space
that contains it. RP5 labels every such space once per geometry and domain
(`foammesh.core.mesh.fluid_spaces`); this module draws them:

* one translucent actor per space shown, proposed or selected, its surface
  built on the worker when first wanted and kept while it fits (RP13 #7:
  together they hold at most 300 k triangles; a space past that is drawn as
  its bounding box and `statusNote` says so);
* the space holding the seed being edited at 0.28, every other region's
  space at 0.14, each in its region's colour, with a silhouette so the shape
  reads through the faded walls;
* while a seed is dragged only visibilities and colours change -- the
  surfaces are never rebuilt (their ``GetMTime`` does not move);
* while the job runs, a small ring stands round the handle, so the page is
  never blank while the answer is worked out;
* while the seed is placed on a section plane (RP9's `SeedSection`), each
  shown space is also drawn as its filled cross-section on that plane
  (`rendering.section_fill.SectionFill`), so the seed can be dropped into the
  space by eye.

The labelling runs on the single VTK worker thread of
``support/vtk_threads.py`` (D6), never on the GUI thread
(`fluid_regions.run_detection` refuses it), and is cached beside the case, so
opening the editor again answers from memory or from the ``.npz``.

None of these actors is pickable, and none widens a fit: they all sit inside
the domain box that is already drawn.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal

#: The space of the seed being edited, and of every other stored region.
EDITED_OPACITY = 0.28
STORED_OPACITY = 0.14

#: What the geometry fades to while a volume is shown (RP6). The walls are
#: already at 0.25 while a seed is placed (DP-818); the volume sits behind
#: them and needs a little more of the view.
VOLUME_WALL_OPACITY = 0.15

#: Roles a space can be drawn in.
EDITED = 'edited'
STORED = 'stored'

#: Used when no theme is loaded (tests, tools).
_FALLBACK_ZONE = ('#4c9be8', '#e39b3b', '#5cb85c', '#b07cd8')

#: The busy ring's radius and arc, in arrow lengths (the handle's own unit).
_BUSY_RADIUS = 0.3
_BUSY_ARC_DEGREES = 270.0


# -- colours ---------------------------------------------------------------- #

def _tokens():
    from foammesh.app import app

    try:
        return app.themeManager.tokens if app.themeManager else None
    except Exception:                                         # noqa: BLE001
        return None


def zonePalette(tokens=None) -> tuple:
    """The region palette: the theme's ``viewport.zone.*`` entries in order.

    Plan 36 RP8 extends the family; this is the one place it is read, so the
    swap is here and nowhere else.
    """
    from foammesh.view.theming.patch_palette import active_palette

    try:
        colours = active_palette(tokens if tokens is not None else _tokens(),
                                 'zone')
    except Exception:                                         # noqa: BLE001
        colours = ()
    colours = tuple(colour for colour in colours if colour)
    return colours or _FALLBACK_ZONE


def regionColours(regionIds, tokens=None) -> dict:
    """Each region's colour, by ``str`` id, slots given in numeric id order.

    The lookup is RP8's `patch_palette.region_zone_colours`, the one the
    regions table and the detection panel use, so a region is the same colour
    in all three. DP-819's lesson: ids are integers held as text, and as text
    ``10`` sorts before ``2``; that lookup sorts them as numbers.
    """
    from foammesh.view.theming.patch_palette import region_zone_colours

    ids = [str(key) for key in regionIds]
    try:
        return region_zone_colours(ids, tokens)
    except Exception:                                         # noqa: BLE001
        from foammesh.view.theming.patch_palette import slot_colour, slot_order

        palette = zonePalette(tokens)
        return {key: slot_colour(palette, slot)
                for slot, key in enumerate(slot_order(ids))}


def nextRegionColour(regionIds, tokens=None) -> str:
    """The colour a region added now will take: the slot after every other."""
    from foammesh.view.theming.patch_palette import slot_colour

    return slot_colour(zonePalette(tokens), len(tuple(regionIds)))


# -- one space -------------------------------------------------------------- #

class RegionVolumeActor:
    """One space's translucent surface, its silhouette and its section fill.

    Built once from the space's polydata. `setRole` shows it as the edited or
    a stored region's space, in a colour, or hides it (``None``); nothing it
    does touches the polydata. `setSectionPlane` cuts the fill on RP9's
    section plane; the fill is drawn only while the space is.
    """

    def __init__(self, label: int, polyData, camera=None, fill=None):
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        from foammesh.rendering.actor_info import applySurfaceMaterial

        self.label = int(label)
        self.polyData = polyData
        self._role = None
        self._colour = None
        self._plane = None
        if fill is None:
            from foammesh.rendering.section_fill import SectionFill

            fill = SectionFill(polyData)
        #: RP9's filled cross-section of this space, one per space.
        self.fill = fill
        fill.actor().VisibilityOff()

        mapper = vtkPolyDataMapper()
        mapper.SetInputData(polyData)
        mapper.ScalarVisibilityOff()
        surface = vtkActor()
        surface.SetMapper(mapper)
        surface.SetObjectName(f'regionVolume:{self.label}')
        prop = surface.GetProperty()
        applySurfaceMaterial(prop)
        prop.SetOpacity(EDITED_OPACITY)
        surface.PickableOff()
        surface.UseBoundsOff()
        surface.VisibilityOff()
        self.surface = surface

        self.silhouette = None
        if camera is not None:
            from vtkmodules.vtkFiltersHybrid import vtkPolyDataSilhouette

            from foammesh.rendering.actor_info import SILHOUETTE_WIDTH

            outline = vtkPolyDataSilhouette()
            outline.SetInputData(polyData)
            outline.SetCamera(camera)
            outline.BorderEdgesOn()
            outline.SetEnableFeatureAngle(False)
            outlineMapper = vtkPolyDataMapper()
            outlineMapper.SetInputConnection(outline.GetOutputPort())
            outlineMapper.ScalarVisibilityOff()
            outlineMapper.SetResolveCoincidentTopologyToPolygonOffset()
            silhouette = vtkActor()
            silhouette.SetMapper(outlineMapper)
            silhouette.SetObjectName(f'regionVolume:{self.label}:silhouette')
            line = silhouette.GetProperty()
            line.SetLighting(False)
            line.SetLineWidth(SILHOUETTE_WIDTH)
            silhouette.PickableOff()
            silhouette.UseBoundsOff()
            silhouette.VisibilityOff()
            self.silhouette = silhouette
            self._outline = outline

    def parts(self) -> list:
        """The actors drawn with the space (the fill is shown separately)."""
        return [part for part in (self.surface, self.silhouette)
                if part is not None]

    def actors(self) -> list:
        """Every actor this space owns, the section fill included."""
        return self.parts() + [self.fill.actor()]

    def setSectionPlane(self, plane) -> None:
        """RP9's section plane is ``(origin, normal)``, or gone (``None``)."""
        self._plane = None if plane is None else (
            tuple(float(v) for v in plane[0]),
            tuple(float(v) for v in plane[1]))
        self._syncFill()

    def _syncFill(self) -> None:
        fill, plane = self.fill, self._plane
        if plane is None:
            if fill.plane() is not None:
                fill.clear()
            return
        if self._role is None:
            # Cut lazily: a hidden space keeps whatever it was cut on.
            fill.actor().VisibilityOff()
            return
        if fill.plane() != plane:
            fill.setPlane(*plane)
        else:
            fill.actor().SetVisibility(fill.polyData().GetNumberOfCells() > 0)

    def role(self):
        return self._role

    def colour(self):
        return self._colour

    def opacity(self) -> float:
        return float(self.surface.GetProperty().GetOpacity())

    def isVisible(self) -> bool:
        return bool(self.surface.GetVisibility())

    def setRole(self, role, colour=None) -> bool:
        """Show as *role* in *colour*, or hide for ``None``; True if it changed."""
        if role == self._role and (role is None or colour == self._colour):
            return False
        self._role = role
        if role is None:
            for part in self.parts():
                part.VisibilityOff()
            self._syncFill()
            return True
        from foammesh.view.theming.vtk_theme import rgb

        self._colour = colour
        tint = rgb(colour)
        prop = self.surface.GetProperty()
        prop.SetColor(*tint)
        prop.SetOpacity(EDITED_OPACITY if role == EDITED else STORED_OPACITY)
        if self.silhouette is not None:
            line = self.silhouette.GetProperty()
            line.SetColor(*tint)
            line.SetOpacity(0.9 if role == EDITED else 0.45)
        self.fill.setColour(colour)
        for part in self.parts():
            part.VisibilityOn()
        self._syncFill()
        return True


# -- the busy ring ---------------------------------------------------------- #

class BusyRing:
    """An open ring round the seed handle while its space is worked out.

    It is drawn in the handle's own overlay renderer, sized from the handle's
    arrow length, and follows the handle before each render. It does not
    spin: the viewport is only drawn when something changes, and a ring that
    moved only then would read as a glitch rather than as work in progress.
    """

    def __init__(self):
        self._gizmo = None
        self._actor = None
        self._observer = None

    def actor(self):
        return self._actor

    def isShown(self) -> bool:
        return self._actor is not None and bool(self._actor.GetVisibility())

    def attach(self, gizmo) -> None:
        if gizmo is self._gizmo:
            return
        self.detach()
        renderer = gizmo.renderer() if gizmo is not None else None
        if renderer is None:
            return
        from vtkmodules.vtkFiltersSources import vtkArcSource
        from vtkmodules.vtkRenderingCore import vtkFollower, vtkPolyDataMapper

        from foammesh.view.geometry.geometry_manager import seedColour
        from foammesh.view.theming.vtk_theme import rgb

        arc = vtkArcSource()
        arc.UseNormalAndAngleOn()
        arc.SetCenter(0.0, 0.0, 0.0)
        arc.SetPolarVector(_BUSY_RADIUS, 0.0, 0.0)
        arc.SetNormal(0.0, 0.0, 1.0)
        arc.SetAngle(_BUSY_ARC_DEGREES)
        arc.SetResolution(36)
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(arc.GetOutputPort())
        ring = vtkFollower()
        ring.SetMapper(mapper)
        ring.SetCamera(renderer.GetActiveCamera())
        ring.SetObjectName('regionVolume:busy')
        prop = ring.GetProperty()
        prop.SetColor(*rgb(seedColour('unknown')))
        prop.SetLineWidth(3.0)
        prop.SetLighting(False)
        ring.PickableOff()
        ring.UseBoundsOff()
        renderer.AddActor(ring)
        self._gizmo, self._actor = gizmo, ring
        self._observer = (renderer, renderer.AddObserver(
            'StartEvent', self._follow))
        self._follow()

    def detach(self) -> None:
        gizmo, self._gizmo = self._gizmo, None
        actor, self._actor = self._actor, None
        observer, self._observer = self._observer, None
        if observer is not None:
            try:
                observer[0].RemoveObserver(observer[1])
            except Exception:                                 # noqa: BLE001
                pass
        if actor is not None and gizmo is not None:
            renderer = gizmo.renderer()
            if renderer is not None:
                renderer.RemoveActor(actor)

    def _follow(self, *_args) -> None:
        gizmo, ring = self._gizmo, self._actor
        if gizmo is None or ring is None:
            return
        scale = gizmo.handleScale()
        ring.SetPosition(*gizmo.position())
        ring.SetScale(scale, scale, scale)


# -- every space, and the job that finds them ------------------------------- #

class RegionVolumes(QObject):
    """The spaces of the current geometry and domain, drawn per region.

    `request` starts the labelling on the VTK worker thread unless the same
    inputs are already answered or in flight; `spacesReady` fires on the GUI
    thread when the answer (or a failure) is in. While a region is edited
    (`begin` .. `end`), `place` shows the space of the seed being edited and
    `setStored` the spaces of the others.
    """

    #: The labelling job finished (or failed); `field()` has the answer.
    spacesReady = Signal()
    #: RP13 #7: the surfaces asked for are built; `volumes()` has them.
    surfacesReady = Signal()
    #: Worker thread -> GUI thread: (job token, its case, the future).
    _delivered = Signal(int, object, object)
    _surfacesDelivered = Signal(int, object, object)

    def __init__(self, display=None, parent=None):
        super().__init__(parent)
        self._display = display
        self._inputs = None
        self._field = None
        self._error = None
        self._future = None
        self._token = 0
        self._stop = None
        #: RP13 #7: the case the field was asked for; an answer for any
        #: other is dropped (`dropped` counts them).
        self._case = None
        self.dropped = 0
        #: RP13 #7: surfaces are built on demand. ``_built`` holds the last
        #: answer ``{label: polyData}``; ``_asked`` the labels in flight or
        #: answered; ``_answered`` the labels the last answer was asked for.
        self._built = {}
        self._asked = set()
        self._answered = set()
        self._surfaceFuture = None
        self._surfaceStop = None
        self._surfaceToken = 0
        self._volumes = {}
        #: RP9: one section fill per space, kept across rebuilds by label.
        self._fills = {}
        self._plane = None
        self._section = None
        self._assembly = None
        self._shown = False
        self._active = False
        self._stored = []
        self._editedColour = None
        self._editedPoint = None
        self._editedSpace = None
        self._external = False
        #: RP7: ``[(colour, seed), ...]`` of the detection panel's ticked
        #: spaces, drawn while it is open (`showCandidates`).
        self._candidates = []
        self._busy = BusyRing()
        self._gizmo = None
        #: MEASURED, per job: seconds on the worker (labelling and surfaces)
        #: and on the GUI thread (building the actors).
        self.jobSeconds = None
        self.buildSeconds = None
        self.surfaceSeconds = None
        self._started = None
        self._delivered.connect(self._onDelivered)
        self._surfacesDelivered.connect(self._onSurfaces)

    # -- the job ---------------------------------------------------------- #

    def request(self, components, box, *, base_cell=None, cache_dir=None,
                key=None, case=None) -> bool:
        """Label the domain for these inputs, off the GUI thread.

        Answers True when a job was started, False when the inputs are
        already answered or being answered. A *box* that is a
        `fluid_spaces.DomainExtent` keeps its domain (RP13 #5), so an L is
        labelled as an L. *case* tags the job (RP13 #7): an answer for
        another case than the one asked last is dropped.
        """
        from foammesh.core.mesh import fluid_spaces

        domain = getattr(box, 'domain', None)
        box = tuple(float(value) for value in box)
        if domain is not None:
            box = fluid_spaces.DomainExtent(box, domain)
        case = None if case is None else str(case)
        inputs = (key, tuple(box), fluid_spaces.domain_key(domain), base_cell,
                  None if cache_dir is None else str(cache_dir), case)
        if inputs == self._inputs and (
                self._field is not None or self._future is not None):
            return False
        self._cancelJob()
        self._dropActors()
        self._inputs, self._field, self._error = inputs, None, None
        self._case = case
        self._editedSpace = None
        from vtkmodules.vtkCommonDataModel import vtkPolyData

        from foammesh.core.mesh import fluid_regions
        from foammesh.support.vtk_threads import _pool

        # The viewport renders these on this thread while the worker reads
        # them: the worker gets copies of its own.
        copies = []
        for component in components:
            copy = vtkPolyData()
            copy.DeepCopy(component)
            copies.append(copy)
        self._token += 1
        token = self._token
        stop = threading.Event()
        self._stop = stop
        # RP13 #7: no surface is built with the field; the ones shown,
        # proposed or selected are built when they are wanted (`_surface`).
        options = {'build_surfaces': False, 'cancelled': stop.is_set}
        if base_cell is not None:
            options['base_cell'] = base_cell
        if cache_dir is not None:
            options['cache_dir'] = cache_dir
        self._started = time.perf_counter()
        future = _pool.submit(fluid_regions.run_detection, copies, box,
                              **options)
        self._future = future
        delivered = self._delivered

        def done(finished):
            try:
                delivered.emit(token, case, finished)
            except RuntimeError:                  # the receiver has gone
                pass

        future.add_done_callback(done)
        self._updateBusy()
        return True

    def _cancelJob(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._stop = None
        self._future = None
        self._token += 1
        self._cancelSurfaces()

    def _late(self, token, case) -> bool:
        """RP13 #7: an answer to a request since replaced, closed or reset,
        or for another case, is dropped."""
        return token != self._token or case != self._case

    def _onDelivered(self, token, case, future) -> None:
        if self._late(token, case):
            self.dropped += 1
            return
        self._future, self._stop = None, None
        self.jobSeconds = time.perf_counter() - (self._started or 0.0)
        try:
            self._field = future.result()
        except Exception as error:                            # noqa: BLE001
            self._field, self._error = None, error
        if self._field is not None:
            self._makeAssembly()
        self._updateBusy()
        if self._active or self._candidates:
            self._apply()
        if self._candidates:
            self._render()
        self.spacesReady.emit()

    def isWorking(self) -> bool:
        return self._future is not None

    def isBuilding(self) -> bool:
        """A surface job is out (RP13 #7)."""
        return self._surfaceFuture is not None

    def field(self):
        return self._field

    def error(self):
        return self._error

    def wait(self, timeout: float = 30.0) -> bool:
        """Process events until the labelling and the surfaces it was asked
        for are in (tests and tools)."""
        from PySide6.QtCore import QCoreApplication

        deadline = time.perf_counter() + timeout
        while ((self._future is not None or self._surfaceFuture is not None)
               and time.perf_counter() < deadline):
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return self._future is None and self._surfaceFuture is None

    def reset(self) -> None:
        """The geometry or the case changed: forget the field and every
        actor; whatever the running jobs answer is dropped."""
        self._cancelJob()
        self._dropActors()
        self._fills = {}
        self._inputs = self._field = self._error = None
        self._case = None
        self._editedSpace = None
        self._updateBusy()

    # -- RP13 #7: surfaces on demand -------------------------------------- #

    def _surface(self, wanted) -> None:
        """Have the surfaces of the *wanted* spaces built, on the worker.

        Only spaces shown, proposed or selected are built. Those already
        built are kept while they fit (so a seed moving back finds its
        space unrebuilt), and every surface asked for at once shares the
        `TOTAL_SURFACE_TRIANGLES` budget -- the actors held never pass it.
        """
        from foammesh.core.mesh import fluid_spaces

        if self._field is None:
            return
        wanted = {int(label) for label in wanted}
        if wanted <= self._asked:
            return
        keep = max(1, fluid_spaces.TOTAL_SURFACE_TRIANGLES
                   // fluid_spaces.MAX_SURFACE_TRIANGLES)
        kept = set(self._built) - wanted
        if len(wanted) + len(kept) > keep:
            kept = set()
        labels = sorted(wanted | kept)
        self._cancelSurfaces()
        from foammesh.support.vtk_threads import _pool

        self._asked = set(labels)
        token = self._surfaceToken
        case = self._case
        stop = threading.Event()
        self._surfaceStop = stop
        field = self._field
        began = time.perf_counter()

        def build():
            built = field.surfaces(labels, cancelled=stop.is_set)
            return built, time.perf_counter() - began

        future = _pool.submit(build)
        self._surfaceFuture = future
        delivered = self._surfacesDelivered

        def done(finished):
            try:
                delivered.emit(token, case, finished)
            except RuntimeError:                  # the receiver has gone
                pass

        future.add_done_callback(done)

    def _cancelSurfaces(self) -> None:
        if self._surfaceStop is not None:
            self._surfaceStop.set()
        self._surfaceStop = None
        self._surfaceFuture = None
        self._surfaceToken += 1
        # Nothing is on its way: what is wanted next is asked again.
        self._asked = set(self._built)

    def _onSurfaces(self, token, case, future) -> None:
        if token != self._surfaceToken or case != self._case:
            self.dropped += 1
            return
        self._surfaceFuture, self._surfaceStop = None, None
        try:
            built, self.surfaceSeconds = future.result()
        except Exception as error:                            # noqa: BLE001
            # Cancelled or failed: nothing new is drawn, and the next want
            # asks again.
            self._asked = set(self._built)
            self._error = self._error or error
            return
        self._answered = set(self._asked)
        self._built = dict(built)
        began = time.perf_counter()
        self._buildActors()
        self.buildSeconds = time.perf_counter() - began
        self._apply()
        self._render()
        self.surfacesReady.emit()

    def triangleCount(self) -> int:
        """Triangles in every region-volume surface held now."""
        return sum(int(volume.polyData.GetNumberOfCells())
                   for volume in self._volumes.values())

    def statusNote(self) -> str | None:
        """What the viewport does not draw as it is, or ``None``.

        RP13 #7: a space past the triangle budget is drawn as its bounding
        box, and past the room even boxes have, not at all -- the status
        line says so rather than drawing it silently.
        """
        from foammesh.core.mesh import fluid_spaces

        if self._field is None:
            return None
        wanted = set(self._roles())
        boxed = sorted(label for label in wanted
                       if label in self._built and self._field.boxed(label))
        missing = sorted(label for label in wanted & self._answered
                         if label not in self._built)
        budget = f'{fluid_spaces.TOTAL_SURFACE_TRIANGLES // 1000} k'
        notes = []
        if boxed:
            notes.append(
                f'{len(boxed)} space{"s are" if len(boxed) > 1 else " is"} '
                f'drawn as {"their" if len(boxed) > 1 else "its"} bounding '
                f'box: the surface would pass the {budget} triangle budget.')
        if missing:
            notes.append(
                f'{len(missing)} space{"s are" if len(missing) > 1 else " is"}'
                f' not drawn: more spaces than the {budget} triangle budget '
                f'can show.')
        return ' '.join(notes) or None

    # -- the actors ------------------------------------------------------- #

    def _camera(self):
        view = getattr(self._display, 'view', None)
        view = view() if callable(view) else None
        renderer = getattr(view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        return renderer.GetActiveCamera() if renderer is not None else None

    def _makeAssembly(self) -> None:
        from vtkmodules.vtkRenderingCore import vtkAssembly

        self._dropActors()
        assembly = vtkAssembly()
        assembly.SetObjectName('regionVolumes')
        assembly.PickableOff()
        assembly.UseBoundsOff()
        self._assembly = assembly
        if self._active or self._candidates:
            self._showAssembly(True)

    def _buildActors(self) -> None:
        """An actor per built surface; one whose surface is unchanged is
        kept as it is, and the others' are dropped."""
        from foammesh.rendering.section_fill import SectionFill

        if self._assembly is None:
            self._makeAssembly()
        assembly = self._assembly
        camera = self._camera()
        volumes, fills = {}, {}
        for label, polyData in self._built.items():
            if polyData is None or not polyData.GetNumberOfCells():
                continue
            old = self._volumes.get(label)
            if old is not None and old.polyData is polyData:
                volumes[label] = old
                fills[label] = self._fills.get(label)
                continue
            fill = self._fills.get(label)
            if fill is None:
                fill = SectionFill(polyData)
            else:
                fill.setSurface(polyData)          # the volume was rebuilt
            fills[label] = fill
            volume = RegionVolumeActor(label, polyData, camera, fill)
            volume.setSectionPlane(self._plane)
            for part in volume.actors():
                assembly.AddPart(part)
            volumes[label] = volume
        for label, volume in self._volumes.items():
            if volumes.get(label) is not volume:
                for part in volume.actors():
                    assembly.RemovePart(part)
        self._volumes = volumes
        self._fills = {**self._fills, **fills}

    def _dropActors(self) -> None:
        self._showAssembly(False)
        self._volumes, self._assembly = {}, None
        self._built, self._asked, self._answered = {}, set(), set()

    def _showAssembly(self, shown: bool) -> None:
        if self._assembly is None or self._display is None:
            self._shown = False
            return
        if shown and not self._shown:
            self._display.addOverlay(self._assembly)
            self._shown = True
        elif not shown and self._shown:
            try:
                self._display.removeOverlay(self._assembly)
            except (ValueError, RuntimeError):
                pass
            self._shown = False

    def volumes(self) -> dict:
        """Every space's actor, by label."""
        return dict(self._volumes)

    # -- RP9: the section plane ------------------------------------------- #

    def setSectionPlane(self, origin, normal) -> None:
        """Draw each shown space's filled section on this plane."""
        self._plane = (tuple(origin), tuple(normal))
        for volume in self._volumes.values():
            volume.setSectionPlane(self._plane)
        self._render()

    def clearSection(self) -> None:
        """The section plane is gone: no fills."""
        self._plane = None
        for volume in self._volumes.values():
            volume.setSectionPlane(None)
        self._render()

    def sectionPlane(self):
        return self._plane

    def followSection(self, section) -> None:
        """Listen to RP9's `SeedSection`: its plane is where the fills cut."""
        self.unfollowSection()
        if section is None:
            return
        self._section = section
        section.planeChanged.connect(self.setSectionPlane)
        section.ended.connect(self.clearSection)
        plane = section.plane() if section.isActive() else None
        if plane is not None:
            self.setSectionPlane(*plane)

    def unfollowSection(self) -> None:
        section, self._section = self._section, None
        if section is not None:
            for signal, slot in ((section.planeChanged, self.setSectionPlane),
                                 (section.ended, self.clearSection)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        if self._plane is not None:
            self.clearSection()

    def _render(self) -> None:
        refresh = getattr(self._display, 'refreshView', None)
        if callable(refresh) and self._shown:
            refresh()

    def volume(self, label):
        return self._volumes.get(int(label)) if label is not None else None

    def shownLabels(self) -> dict:
        """``{label: role}`` for every space drawn now."""
        return {label: volume.role() for label, volume in self._volumes.items()
                if volume.role() is not None}

    def anyShown(self) -> bool:
        return any(volume.role() is not None
                   for volume in self._volumes.values())

    # -- a region being edited -------------------------------------------- #

    def setExternalFlow(self, external: bool) -> None:
        """With external flow on, the space round the body is drawn too."""
        external = bool(external)
        if external != self._external:
            self._external = external
            if self._active:
                self._apply()

    def externalFlow(self) -> bool:
        return self._external

    def isActive(self) -> bool:
        return self._active

    def begin(self, stored, editedColour) -> None:
        """A region's form opened. *stored* is ``[(colour, point), ...]``."""
        self._active = True
        self._stored = [(colour, tuple(float(value) for value in point))
                        for colour, point in stored]
        self._editedColour = editedColour
        self._editedPoint = None
        self._editedSpace = None
        self._showAssembly(True)
        self._apply()
        self._updateBusy()

    def end(self) -> None:
        """The form closed: every space is hidden and the ring goes.

        RP13 #7: with no candidates shown either, nothing is waiting for a
        running job -- it is stopped, and what it answers is dropped.
        """
        self.unfollowSection()
        self._active = False
        self._stored, self._editedPoint, self._editedSpace = [], None, None
        for volume in self._volumes.values():
            volume.setRole(None)
        self._showAssembly(False)
        self.setHandle(None)
        self._closeJobs()

    def _closeJobs(self) -> None:
        """Nobody shows the spaces: stop what is still being worked out."""
        if self._active or self._candidates:
            return
        if self._future is not None:
            self._cancelJob()
            self._inputs = None                   # asked again next time
        elif self._surfaceFuture is not None:
            self._cancelSurfaces()

    def setHandle(self, gizmo) -> None:
        """The seed handle the busy ring is drawn round."""
        self._gizmo = gizmo
        self._updateBusy()

    def busyRing(self) -> BusyRing:
        return self._busy

    def _updateBusy(self) -> None:
        if self._active and self.isWorking() and self._gizmo is not None:
            self._busy.attach(self._gizmo)
        else:
            self._busy.detach()

    def spaceAt(self, point):
        """RP5's ``SpaceAt`` for *point*, or ``None`` (no field, off the box)."""
        if self._field is None or point is None:
            return None
        return self._field.space_at(point)

    def drawable(self, space) -> bool:
        """Whether *space* is one this page draws: a space, and not outside
        unless the flow is external."""
        return (space is not None and space.label != 0
                and (not space.outside or self._external))

    def place(self, point) -> bool:
        """The seed being edited is at *point*: show its space. True if the
        picture changed. One array read and a few visibility switches."""
        self._editedPoint = (None if point is None
                             else tuple(float(value) for value in point))
        self._editedSpace = self.spaceAt(self._editedPoint)
        return self._apply()

    def editedSpace(self):
        """The ``SpaceAt`` of the seed being edited, or ``None``."""
        return self._editedSpace

    def spaceName(self, label) -> str | None:
        """'Fluid space N' by rank among the enclosed spaces, largest first."""
        if self._field is None or not label:
            return None
        for rank, space in enumerate(self._field.enclosed, start=1):
            if space.id == label:
                return f'Fluid space {rank}'
        return 'the space outside'

    # -- RP7: the spaces "how many fluid regions?" proposes ---------------- #

    def showCandidates(self, candidates) -> bool:
        """Draw the detection panel's ticked spaces; ``[]`` hides them.

        *candidates* are the panel's rows (``seed``, ``colour``, ``ticked``).
        Each is found here by its seed, not its number, so this labelling
        and the facade's need not number the spaces alike. A ticked space is
        drawn in its colour at the edited opacity; an unticked one is not
        drawn. True if the picture changed.
        """
        listed = []
        for row in candidates or ():
            seed, colour = row.get('seed'), row.get('colour')
            if not row.get('ticked') or not colour or not seed:
                continue
            listed.append((colour, tuple(float(value) for value in seed)))
        self._candidates = listed
        if listed:
            self._showAssembly(True)
        changed = self._apply()
        if listed or changed:
            self._render()
        if not listed and not self._active:
            self._showAssembly(False)
            self._closeJobs()
        return changed

    def candidateLabels(self) -> dict:
        """``{label: colour}`` of the candidate spaces drawn now."""
        drawn = {}
        for colour, point in self._candidates:
            space = self.spaceAt(point)
            if space is not None and space.label:
                drawn.setdefault(int(space.label), colour)
        return drawn

    def _roles(self) -> dict:
        """``{label: (role, colour)}`` of every space to draw now."""
        roles = {}
        for colour, point in self._stored:
            space = self.spaceAt(point)
            if self.drawable(space):
                roles.setdefault(int(space.label), (STORED, colour))
        for label, colour in self.candidateLabels().items():
            roles[int(label)] = (EDITED, colour)
        if self.drawable(self._editedSpace):
            roles[int(self._editedSpace.label)] = (EDITED, self._editedColour)
        return roles

    def _apply(self) -> bool:
        roles = self._roles()
        changed = False
        for label, volume in self._volumes.items():
            role, colour = roles.get(label, (None, None))
            changed = volume.setRole(role, colour) or changed
        # RP13 #7: a space to draw that has no surface yet gets one.
        self._surface(roles)
        return changed
