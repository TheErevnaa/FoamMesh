"""The solids of a Gmsh case, drawn as its regions: Plan 36 RP11.

On Gmsh a region is a solid (`core.mesh.cad_solids`), so the picture RP6
draws for a seed's space -- a translucent volume in the region's colour --
is drawn here for every solid while the Volume controls step is shown. There
is no seed and no gizmo: nothing is placed, the solid is the region.

Each solid is one `RegionVolumeActor`, coloured by `regionColours` in the
order the solids are listed. A fluid (or not yet typed) solid is drawn as
RP6 draws the edited region; a solid-typed one as it draws a stored region,
fainter; an excluded one is not drawn -- it will not be meshed.

The shells are measured on the VTK worker thread (`_pool`), as RP6's
labelling is, and delivered to the GUI thread by a queued signal.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal

from foammesh.rendering.region_volume_actor import (
    EDITED, STORED, RegionVolumeActor, regionColours,
)


def roleFor(kind) -> str | None:
    """How a solid of this type is drawn: RP6's role, or hidden."""
    from foammesh.core.mesh.cad_solids import EXCLUDED, SOLID

    if kind == EXCLUDED:
        return None
    return STORED if kind == SOLID else EDITED


def describe(solid, kind) -> str:
    """``name -- Fluid -- 8.04e-03 m3``: one solid, as the step lists it."""
    word = {'fluid': 'Fluid', 'solid': 'Solid',
            'excluded': 'Excluded'}.get(kind or '', 'Untyped')
    return f'{solid.name} — {word} — {solid.volume:.3g} m³'


class SolidVolumes(QObject):
    """Every solid of the case, one translucent volume each."""

    #: The solids were measured (or failed); `solids()` has the answer.
    solidsReady = Signal()
    _delivered = Signal(int, object)

    def __init__(self, display=None, parent=None):
        super().__init__(parent)
        self._display = display
        self._found = None
        self._error = None
        self._future = None
        self._token = 0
        self._key = None
        self._typing = {}
        self._volumes = {}
        self._assembly = None
        self._shown = False
        self._active = False
        self.jobSeconds = None
        self._started = None
        self._delivered.connect(self._onDelivered)

    # -- the job ---------------------------------------------------------- #

    def request(self, casePath, key=None) -> bool:
        """Measure the case's solids off the GUI thread; False if answered."""
        key = (str(casePath), key)
        if key == self._key and (self._found is not None
                                 or self._future is not None):
            return False
        from foammesh.core.mesh import cad_solids
        from foammesh.support.vtk_threads import _pool

        self._dropActors()
        self._key, self._found, self._error = key, None, None
        self._token += 1
        token = self._token
        self._started = time.perf_counter()
        future = _pool.submit(cad_solids.case_solids, casePath,
                              with_surfaces=True)
        self._future = future
        delivered = self._delivered

        def done(finished):
            try:
                delivered.emit(token, finished)
            except RuntimeError:                  # the receiver has gone
                pass

        future.add_done_callback(done)
        return True

    def _onDelivered(self, token, future) -> None:
        if token != self._token:
            return
        self._future = None
        self.jobSeconds = time.perf_counter() - (self._started or 0.0)
        try:
            self._found = future.result()
        except Exception as error:                            # noqa: BLE001
            self._found, self._error = None, error
        if self._found is not None:
            self._buildActors()
        self.solidsReady.emit()

    def wait(self, timeout: float = 30.0) -> bool:
        """Process events until the job is in (tests and tools)."""
        from PySide6.QtCore import QCoreApplication

        deadline = time.perf_counter() + timeout
        while self._future is not None and time.perf_counter() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return self._future is None

    def reset(self) -> None:
        """The geometry changed: forget the solids and every actor."""
        self._token += 1
        self._future = None
        self._dropActors()
        self._key = self._found = self._error = None

    def error(self):
        return self._error

    def solids(self) -> list:
        return list(self._found.solids) if self._found is not None else []

    def typing(self) -> dict:
        return dict(self._typing)

    def kindOf(self, regionUuid) -> str | None:
        from foammesh.core.mesh.cad_solids import shown_type

        return shown_type(self._typing.get(regionUuid))

    def lines(self) -> list[str]:
        """One line per solid: its name, type and volume."""
        return [describe(solid, self.kindOf(solid.region_uuid))
                for solid in self.solids()]

    # -- the actors ------------------------------------------------------- #

    def _camera(self):
        view = getattr(self._display, 'view', None)
        view = view() if callable(view) else None
        renderer = getattr(view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        return renderer.GetActiveCamera() if renderer is not None else None

    def _buildActors(self) -> None:
        from vtkmodules.vtkRenderingCore import vtkAssembly

        self._dropActors()
        camera = self._camera()
        assembly = vtkAssembly()
        assembly.SetObjectName('solidVolumes')
        assembly.PickableOff()
        assembly.UseBoundsOff()
        for label, solid in enumerate(self._found.solids, start=1):
            polyData = self._found.surfaces.get(solid.region_uuid)
            if polyData is None or not polyData.GetNumberOfCells():
                continue
            volume = RegionVolumeActor(label, polyData, camera)
            for part in volume.actors():
                assembly.AddPart(part)
            self._volumes[solid.region_uuid] = volume
        self._assembly = assembly
        self._apply()
        if self._active:
            self._showAssembly(True)

    def _dropActors(self) -> None:
        self._showAssembly(False)
        self._volumes, self._assembly = {}, None

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

    def _apply(self) -> bool:
        """Colour every solid by its place in the list and its type."""
        uuids = [solid.region_uuid for solid in self.solids()]
        colours = regionColours(range(1, len(uuids) + 1))
        changed = False
        for index, uuid in enumerate(uuids, start=1):
            volume = self._volumes.get(uuid)
            if volume is None:
                continue
            role = roleFor(self.kindOf(uuid)) if self._active else None
            changed |= volume.setRole(role, colours.get(str(index)))
        return changed

    def volumes(self) -> dict:
        """Every solid's actor, by ``region_uuid``."""
        return dict(self._volumes)

    def isActive(self) -> bool:
        return self._active

    def isShown(self) -> bool:
        return self._shown

    # -- the page's hooks ------------------------------------------------- #

    def setTyping(self, typing) -> None:
        """The volume controls' typing (``cad_solids.typing_of``) changed."""
        self._typing = dict(typing or {})
        if self._apply():
            self._render()

    def show(self) -> None:
        self._active = True
        self._apply()
        self._showAssembly(True)
        self._render()

    def hide(self) -> None:
        self._active = False
        self._apply()
        self._showAssembly(False)
        self._render()

    def _render(self) -> None:
        refresh = getattr(self._display, 'refreshView', None)
        if callable(refresh):
            try:
                refresh()
            except RuntimeError:                  # the view has gone
                pass
