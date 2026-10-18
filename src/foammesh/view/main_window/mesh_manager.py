#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import functools
import logging
from pathlib import Path

import numpy as np
import qasync
from PySide6.QtCore import QCoreApplication, Qt, Signal

from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.core.mesh.msh_scene import MshSceneError, read_msh_scene
from foammesh.core.mesh import handover_scene
from foammesh.core.mesh.presentation import (
    ENCLOSURE_OPACITY, VOLUME_DEPTH_BIAS, artifact_kind, display_mode_name,
    enclosing_part_ids, hides_geometry, summary_text)
from foammesh.core.mesh.mesh_preview import estimated_cells
from foammesh.core.mesh.poly_mesh_reader import MeshPreviewLoader
from foammesh.core.run_result import GMSH_MSH, GMSH_SURFACE_MSH, POLY_MESH
from foammesh.rendering.actor_info import ActorInfo, BoundaryActor, DisplayMode, MeshActor, MeshQualityIndex
from foammesh.rendering.actor_info import ActorType
from foammesh.rendering import layers_grown
from foammesh.rendering.patch_skin import PatchSkin
from foammesh.view.main_window.actor_manager import ActorManager
from foammesh.core.quality import extract_selected_cells
from PySide6.QtGui import QColor
from foammesh.view.facade_client import query_async
from foammesh.core.quality.geometry_fidelity import live_distance
from foammesh.support import gc_policy


logger = logging.getLogger(__name__)


#: DP-714. How opaque the surfaces are while the poor cells are coloured:
#: enough to keep the model's shape, little enough to see the cells inside.
QUALITY_SEE_THROUGH_OPACITY = 0.3

#: DP-2725. How opaque the volume is while failed cells are shown. The
#: failed cells sit inside it, so at full strength it buried all but the ones
#: touching the boundary; the faded patches keep the model's shape instead.
FAILED_CELLS_VOLUME_OPACITY = 0.0


def _closeProgress(dialog):
    """Close a load's progress dialog and let Qt delete it (Plan 35 F16).

    Every load made a parented dialog and only closed it, so each one lived
    on as a hidden child of the main window: one leaked per mesh load.
    """
    dialog.close()
    deleteLater = getattr(dialog, 'deleteLater', None)
    if callable(deleteLater):
        deleteLater()


def _connectCancel(dialog, slot):
    """Wire a dialog's Cancel to ``slot``; a stand-in without one is left alone."""
    signal = getattr(dialog, 'cancelClicked', None)
    if signal is not None:
        signal.connect(slot)


class _Cancellation:
    """Whether the user pressed Cancel on a load, and the task it should stop."""

    def __init__(self, dialog):
        self.requested = False
        self.task = None
        self._dialog = dialog
        _connectCancel(dialog, self.request)

    def request(self):
        self.requested = True
        label = getattr(self._dialog, 'setLabelText', None)
        if callable(label):
            label(QCoreApplication.translate('MeshManager', 'Cancelling…'))
        if self.task is not None and not self.task.done():
            self.task.cancel()


_SOURCE_FIELDS = ('_time', '_root', '_nativePath', '_surfaceOnly',
                  '_artifactId', '_stage')


def _sourceState(manager) -> dict:
    """Where the picture on screen came from, to put back on a Cancel."""
    return {name: getattr(manager, name, None) for name in _SOURCE_FIELDS}


def _restoreSource(manager, state: dict):
    for name, value in state.items():
        setattr(manager, name, value)


def _disposeLoader(loader):
    dispose = getattr(loader, 'dispose', None)
    if callable(dispose):
        dispose()


def _previewOf(loader) -> dict:
    """What a preview loader said besides the scene (Plan 35 CR3).

    Read through ``getattr``: a stand-in loader that only returns a scene
    says nothing more, and draws the way the old reader did.
    """
    return {'counts': getattr(loader, 'counts', None),
            'patchFaces': dict(getattr(loader, 'patchFaces', None) or {}),
            'precomputed': getattr(loader, 'precomputed', None) or {},
            'outline': getattr(loader, 'outline', None),
            'notice': getattr(loader, 'notice', None),
            'shown': getattr(loader, 'shown', '')}


def _swapScene():
    """The old scene has been disposed; reap what it left, on this thread."""
    # Plan 39 VI14b. The full pass is put off until the window is quiet: it
    # cost 124 ms of the load's GUI-thread hold on the 6.6 M-cell volume.
    gc_policy.collect_full_when_quiet('scene swap')


#: Plan 39 VI14b. The longest the GUI thread is kept by one slice of a scene
#: swap before the event loop gets it back.
SCENE_SLICE_SECONDS = 0.02

#: Plan 39 VI14d. How long the loop is left between two slices of a swap.
SLICE_REST_SECONDS = 0.006


async def _swapInScene(manager, generation, prepare, build, *args, **kwargs):
    """Take the old scene down and put the new one up, in slices.

    The 6.6 M-cell load held the GUI thread for the old scene's actors coming
    down (58 ms), the full collection (124 ms) and the new scene's build
    (161 ms), in one piece. Here each actor is one step; whenever a slice has
    run ``SCENE_SLICE_SECONDS`` the loop is handed back, and rendering is held
    meanwhile so no frame shows half of each scene.

    ``prepare`` runs once the old scene is down; ``build`` is the manager's
    ``_buildSceneSteps`` generator function, called with ``*args, **kwargs``.
    Returns False when this load was superseded or the manager disposed
    during a pause: a superseded build is finished in one go first, so the
    scene is never left half built for the load that overtook it. A manager
    without step support (a stand-in) draws in one piece, as it always did.
    """
    from time import perf_counter

    from foammesh.support import vtk_threads
    if not hasattr(manager, 'remove') or not hasattr(manager, '_actorInfos'):
        manager.clear()
        _swapScene()
        manager._visibility = True
        prepare()
        manager._buildScene(*args, **kwargs)
        return True
    budget = SCENE_SLICE_SECONDS
    # The progress dialog has just closed (8 to 19 ms of its own): let the
    # timers and paints behind it run before the old scene comes down. Before
    # the hold, not inside it: the old scene is still whole here, so there is
    # nothing to hide, and whatever the loop runs in this turn (the strip, the
    # task list, the window repainting where the dialog was) is not charged
    # to the "Updating the view" cover.
    await asyncio.sleep(SLICE_REST_SECONDS)
    if (getattr(manager, '_disposed', False)
            or generation != getattr(manager, '_load_generation', generation)):
        return False
    parts = _QuietParts(getattr(manager, '_displayControl', None))
    vtk_threads.holdScene()
    try:
        slice_began = perf_counter()

        async def _pause():
            nonlocal slice_began
            if perf_counter() - slice_began < budget:
                return
            # Plan 39 VI14d. Not sleep(0): a zero-delay hand-back is run
            # again before Windows delivers the WM_TIMER messages behind
            # Qt's timers, so the slices ran back to back and the 5 ms
            # heartbeat saw one gap as long as all of them (MEASURED 66 to
            # 119 ms over five runs). A real wait lets the timers and paint
            # events through between slices.
            parts.release()
            await asyncio.sleep(SLICE_REST_SECONDS)
            parts.quiet()
            slice_began = perf_counter()

        parts.quiet()
        slice_began = perf_counter()
        for key in tuple(manager._actorInfos):
            manager.remove(key)
            await _pause()
            if manager._disposed:
                return False
            if generation != manager._load_generation:
                return False       # what is left is the next load's to clear
        manager.applyToDisplay()
        manager._visibility = False
        manager._framed = False
        _swapScene()
        manager._visibility = True
        prepare()
        steps = build(*args, **kwargs)
        for _ in steps:
            await _pause()
            if manager._disposed:
                return False
            if generation != manager._load_generation:
                for _ in steps:        # finish it: never leave it half built
                    pass
                return False
        # The announcement rebuilds the overlay's parts once; it gets a
        # slice of its own when the build's last step used this one up.
        await _pause()
        parts.release()
        parts.announce()
        return True
    finally:
        parts.release()
        vtk_threads.resumeScene()
        parts.announce()
        try:
            manager._displayControl.refreshView()
        except Exception:                                    # noqa: BLE001
            pass


#: The display controls a sliced swap is holding quiet right now.
_quietControls: list = []


def _partsQuiet(control) -> bool:
    """Whether a sliced swap is holding ``control``'s signals right now."""
    return any(control is quiet for quiet in _quietControls)


class _QuietParts:
    """The display control's signals held for the length of each slice.

    MEASURED (real ``MainWindow``, offscreen, a 4,096-cell box with seven
    actors): every actor added or removed made the display control emit
    ``partsChanged``, and each one rebuilt the viewport overlay's parts list,
    chip and region picker -- 106 of the swap's 165 ms of GUI-thread work,
    10 to 18 ms a step, so nearly every step crossed the slice budget and
    paused, and each pause then ran the layout and paint the rebuilt overlay
    left behind (13 to 22 ms against 6.6 ms for an idle wait). The swap held
    the view 280 to 315 ms for seven actors, and grows with the parts squared.

    Blocked only while a slice runs, never across a pause: anything the
    user does to the tree in between still announces itself. When the swap
    ends, the parts, the selection and the visible count are announced once,
    for the scene as it now stands.
    """

    def __init__(self, control):
        from PySide6.QtCore import QObject
        self._control = control if isinstance(control, QObject) else None
        self._was = None
        self._announced = False

    def quiet(self):
        if self._control is None or self._was is not None:
            return
        try:
            self._was = bool(self._control.blockSignals(True))
        except RuntimeError:                       # its C++ object is gone
            self._control = None
            return
        _quietControls.append(self._control)

    def release(self):
        if self._control is None or self._was is None:
            return
        try:
            _quietControls.remove(self._control)
        except ValueError:
            pass
        try:
            self._control.blockSignals(self._was)
        except RuntimeError:
            self._control = None
        self._was = None

    def announce(self):
        """Say once what the swap changed: parts, selection, visible count."""
        control = self._control
        if self._announced or control is None:
            return
        self._announced = True
        try:
            if control.signalsBlocked():
                return
        except RuntimeError:
            return
        for say in (lambda: control.partsChanged.emit(),
                    lambda: control.selectedItemsChanged(),
                    lambda: control.visibilityChanged.emit(
                        *control.visibilitySummary())):
            try:
                say()
            except (AttributeError, RuntimeError, TypeError):
                pass
            except Exception:                                # noqa: BLE001
                logger.debug('announcing the swapped scene', exc_info=True)


#: Plan 42 SG2. The boundary type a native group's role stands for.
_NATIVE_TYPE_OF_CATEGORY = {
    'wall': 'wall', 'far_field': 'patch', 'inlet': 'patch',
    'outlet': 'patch', 'symmetry': 'symmetry'}


class MeshManager(ActorManager):
    #: (visible, total). F-44: the readout changed under a section with no
    #: way to tell a clipped mesh from a smaller one, so both numbers travel
    #: together and the label says which is which.
    cellCountChanged = Signal(int, int)
    #: The one sentence that says what is on screen and how big it is.
    #: DP-96: emitted on every load and on every unload, so the line
    #: cannot outlive the mesh it describes the way the cell count did.
    meshSummaryChanged = Signal(str)
    #: Plan 35 CR3. What to say about the picture: a decimated or surface-only
    #: preview, or the outline drawn in place of one that was not built.
    #: ``dict`` (see ``MeshPreviewLoader.notice``) or None to clear it.
    previewNoticeChanged = Signal(object)
    #: Plan 39 VI1 (F6b). The user pressed Cancel on a load: the picture on
    #: screen is the one that stood before it, unchanged.
    loadCancelled = Signal()
    #: Plan 41 GN13. "Load full volume" on a native mesh that holds no volume.
    nativeVolumeRefused = Signal(str)
    #: Plan 39 VI2 (F1a). A cut or quality filter over the loaded volume is
    #: being worked out on the VTK thread (True), or has come to its end,
    #: swapped in or cancelled (False). The picture on screen is the one
    #: before it until then.
    volumeCutBusy = Signal(bool)
    #: Plan 39 VI16. The volume's exterior colour was taken off (a cut began).
    volumeColourDropped = Signal()

    def __init__(self):
        super().__init__()

        self._loader = None
        self._time = None
        # F-37. Which case root the mesh on screen came from. Not always the
        # project's own: a Gmsh candidate refused on quality lives in its run
        # directory, and drawing the case root instead would show the last
        # accepted mesh under this run's verdict.
        self._root = None
        # CP-02 (C31-03). The native ``.msh`` on screen, when the artifact is
        # one; the polyMesh route uses ``_root`` instead. A reload has to go
        # back to the same file, and a case with no polyMesh at all -- which
        # since CP-01 is every Gmsh case with no solver target -- has no root
        # to go back to.
        self._nativePath = None
        # Plan 41 GN23. The patches of the native scene on screen ({name:
        # cells}, in draw order) and the directory whose ``foammesh/`` store
        # holds what is kept about it (a session's case, else the file's).
        self._nativePatches = {}
        self._nativeStoreRoot = None
        self._nativeCurves = {}
        # Which artifact the picture, the counters and any overlay belong to.
        # ``RunResultHandle.artifact_id``: run, format and content. Overlays
        # are refused when they name a different one, because a quality
        # colouring computed on one run's mesh over another run's cells is
        # F-37 wearing a different coat.
        self._artifactId = ''
        # CP-09 item 8: "preserve run identity in screenshots and restored
        # view state". A capture recorded a mesh fingerprint and nothing else,
        # so a picture of a *refused* candidate and a picture of the accepted
        # mesh were indistinguishable once they were both in the gallery.
        self._runId = ''
        self._resultLabel = ''
        self._load_generation = 0
        # DP-485. polyMesh reads in flight, and the event a writer waits on
        # until there are none. See `readsFinished` and `_waitForWriters`.
        self._reads = 0
        self._readsIdle = asyncio.Event()
        self._readsIdle.set()
        self._regionIds: dict[str, list[str]] = {}
        self._patchIds: list[str] = []
        self._zoneIds: list[str] = []
        self._regionPartIds: list[str] = []
        # Plan 39 VI4 (F1e). One per region whose exterior says which patch
        # each face is: the exterior wears the patches' colours and the
        # patches draw their edges only.
        self._skins: list[PatchSkin] = []

        # DP-96. What the user pressed to make this mesh, when a stage
        # page made it. A result loaded from a run has no stage and
        # names itself by what it is instead.
        self._stage = ''
        # DP-133. Whether the file named by `_nativePath` is to be read as a
        # surface pass. Part of what identifies the mesh on screen, not a
        # property of the file: the same file read the other way is the
        # refusal `read_msh_scene` exists to make.
        self._surfaceOnly = False
        self._summary = ''
        # Plan 35 CR3. The counts of the mesh behind a surface preview: with
        # no volume on screen, the cells and points come from the headers,
        # and a decimated patch is not its mesh's face count.
        self._previewCounts = None
        self._patchFaces: dict[str, int] = {}
        self._previewNotice = None

        self._name = 'Mesh'

    def artifactId(self) -> str:
        """Which artifact is on screen, or '' when nothing is."""
        return self._artifactId

    def runId(self) -> str:
        """Which run produced what is on screen, or '' when nothing did."""
        return self._runId

    def resultLabel(self) -> str:
        """The run's own one-line description, for a caption or a sidecar."""
        return self._resultLabel

    def nativePath(self):
        """The .msh currently drawn, or None when the scene came from a case."""
        return self._nativePath

    def nativeStoreRoot(self):
        """Plan 41 GN23. The directory whose ``foammesh/`` folder keeps the
        saved sections, stored deviations and pass snapshots of the native
        mesh on screen (``None``: no native mesh)."""
        if self._nativePath is None:
            return None
        return Path(self._nativeStoreRoot) if self._nativeStoreRoot             else Path(self._nativePath).parent

    def nativeCurves(self) -> dict:
        """Plan 41 GN23. ``{curve name: [polyline points]}`` of the native
        mesh on screen: the model's curve entities as the mesh lines them."""
        return dict(self._nativeCurves) if self._nativePath is not None else {}

    def nativeDigest(self) -> str:
        """Plan 41 GN23. The sha256 of the native file on screen (cached by
        size and time), which names the mesh a stored result belongs to."""
        if self._nativePath is None:
            return ''
        from foammesh.core.mesh import native_mesh
        try:
            stat = Path(self._nativePath).stat()
        except OSError:
            return ''
        key = (str(self._nativePath), stat.st_size, stat.st_mtime_ns)
        cached = getattr(self, '_nativeDigestCache', None)
        if cached is None or cached[0] != key:
            try:
                cached = (key, native_mesh.digest(self._nativePath))
            except OSError:
                return ''
            self._nativeDigestCache = cached
        return cached[1]

    def meshIdentity(self) -> dict:
        """Plan 42 SG14 (D8). Which kind of mesh is on screen and which one:
        a saved element selection records it and is marked when opened on
        another."""
        if self._nativePath is not None:
            return {'source': 'gmsh', 'digest': self.nativeDigest()}
        return {'source': 'openfoam', 'digest': self._artifactId}

    def cellsForElements(self, elements) -> list:
        """Plan 42 SG14. The positions in the drawn volume of saved element
        numbers: Gmsh element tags on a native mesh, cell positions on an
        OpenFOAM mesh (numbers beyond the mesh are dropped)."""
        if self._nativePath is not None:
            return self.nativeCellsForTags(elements)
        internal = self._actorInfos.get('internalMesh')
        data = internal.dataSet() if internal is not None else None
        count = data.GetNumberOfCells() if data is not None else 0
        return sorted({int(e) for e in elements if 0 <= int(e) < count})

    def nativeCellsForTags(self, tags) -> list:
        """Plan 41 GN23. The positions in the drawn volume of the cells
        whose Gmsh element tags are *tags* (the offending rows of a gate)."""
        from vtkmodules.util.numpy_support import vtk_to_numpy
        internal = self._actorInfos.get('internalMesh')
        data = internal.dataSet() if internal is not None else None
        array = data.GetCellData().GetArray('gmshElementTag') \
            if data is not None else None
        if array is None:
            return []
        present = vtk_to_numpy(array)
        return np.nonzero(np.isin(present, np.asarray(
            list(tags), dtype=present.dtype)))[0].astype(int).tolist()

    def caseRoot(self):
        """The case directory the drawn mesh was read from, or None (Plan 37
        UF10: the section worker cuts that case's polyMesh)."""
        return None if self._nativePath is not None else self._root

    def ownsArtifact(self, artifact_id) -> bool:
        """Whether ``artifact_id`` names what is currently displayed.

        An empty id from the caller means "not tracked" and is allowed
        through: most callers predate the identity and colour whatever is
        there. A *non-empty* id that disagrees is refused.
        """
        wanted = str(artifact_id or '')
        return not wanted or wanted == self._artifactId

    def regions(self) -> dict[str, list[str]]:
        """Actor ids grouped by region, for grouping the tree and the legend."""
        return dict(self._regionIds)

    def zoneIds(self) -> list[str]:
        return list(self._zoneIds)

    def regionPartIds(self) -> list[str]:
        """The volume parts named after a region point (DP-711)."""
        return list(self._regionPartIds)

    def volumePartIds(self) -> list[str]:
        """What the Region picker offers under each region: cell zones and
        the region-point parts, each a piece of volume that can be shown on
        its own."""
        return list(self._zoneIds) + list(self._regionPartIds)

    def patchIds(self) -> list[str]:
        return list(self._patchIds)

    def hasScalar(self, index: MeshQualityIndex) -> bool:
        """Whether any loaded mesh actually carries the named quality metric.

        Thresholding an absent array colours nothing -- which reads as "no bad
        cells" -- so the controls ask first, and :meth:`ensureQualityFields`
        is what makes the answer become yes.
        """
        return any(actorInfo.hasScalar(index)
                   for actorInfo in self._actorInfos.values()
                   if isinstance(actorInfo, MeshActor))

    def hasAllQualityFields(self) -> bool:
        if self.hasGmshQuality():
            # Plan 41 GN13. A session mesh arrives with Gmsh's measures in
            # its cells; there is nothing to compute from a polyMesh.
            return True
        return all(self.hasScalar(index)
                   for index in MeshQualityIndex.openfoam())

    def dataSets(self) -> list:
        """The datasets drawn, the volume first (cells to be inspected)."""
        infos = sorted(self._actorInfos.items(),
                       key=lambda item: item[0] != 'internalMesh')
        return [info.dataSet() for _key, info in infos
                if hasattr(info, 'dataSet') and info.dataSet() is not None]

    def hasGmshQuality(self) -> bool:
        """Whether the volume on screen carries Gmsh's quality arrays."""
        return self.hasScalar(MeshQualityIndex.GMSH_SICN)

    def qualityMetrics(self) -> list:
        """The metrics the mesh on screen can be coloured by, in order."""
        if self.hasGmshQuality():
            return [index for index in MeshQualityIndex.gmsh()
                    if self.hasScalar(index)]
        return MeshQualityIndex.openfoam()

    async def ensureQualityFields(self) -> str:
        """Compute the per-cell quality arrays and attach them to the mesh.

        **The single backend for every entry point that needs them** -- the
        Apply button, the toolbar's poor-cells one-click, and the availability
        check that enables both. They ask this one method rather than each
        deciding for itself whether the arrays are present and how to get them.

        The arrays were never reaching the renderer. `quality.cell_fields`
        computes all four from the polyMesh and has documented
        ``include_values`` as *"the arrays themselves for the renderer"* since
        Plan 26 WP6.5 -- and no view-layer caller ever passed it. So Display
        Control offered four metrics backed by arrays that only existed if some
        other tool had written them into the case, and on OpenFOAM Foundation
        v13 nothing does: its `checkMesh` has no ``-writeAllFields`` at all.
        Every one of those four selector entries coloured by nothing.

        Deliberately on demand rather than on load. The computation is a full
        second pass over the mesh -- measured at 1.9 s for 188k cells, so
        minutes at the sizes this product targets -- and paying that on every
        mesh load, for a control most sessions never open, is not a trade worth
        making silently.

        Returns a reason rather than a bare bool, because "no mesh", "the
        numbers do not describe this mesh" and "the computation failed" call
        for different things to be said to the user.
        """
        if self.isEmpty():
            return 'no-mesh'
        if self.hasAllQualityFields():
            return 'ready'

        try:
            # Plan 35 CR2: the arrays are computed in a worker process, and
            # awaited here on the loop rather than parsed in a thread of
            # this one.
            result = await query_async(
                app.facadeClient, 'quality.cell_fields',
                {'include_values': True})
        except Exception:                                          # noqa: BLE001
            logger.debug('per-cell quality fields unavailable', exc_info=True)
            return 'failed'

        values = (result.payload or {}).get('values') or {}
        if not values:
            return 'failed'

        attached = 0
        for actorInfo in self._actorInfos.values():
            if not isinstance(actorInfo, MeshActor):
                continue
            for name, series in values.items():
                if actorInfo.attachQualityField(name, series):
                    attached += 1

        # A cell-zone actor holds a subset and a second region a different
        # mesh, so both decline on count and only the whole volume takes the
        # arrays. None taking them means the numbers do not describe what is on
        # screen, which must be said rather than shown as an empty colouring.
        return 'ready' if attached else 'mismatch'

    def _assignPalettes(self):
        """Colour patches, zones and regions from their own palettes.

        Zones were built as actors and then left in the neutral surface colour,
        so a multi-zone mesh looked like one undifferentiated solid -- which is
        exactly the thing a user opens a multi-zone mesh to see.

        In a conjugate case the question is "which solid is which", not "which
        patch is which", so when more than one region is loaded every actor of
        a region shares that region's colour.
        """
        if len(self._regionIds) > 1:
            for index, (_name, keys) in enumerate(sorted(self._regionIds.items())):
                for key in keys:
                    if key in self._zoneIds:
                        continue
                    actorInfo = self._actorInfos.get(key)
                    if actorInfo is not None:
                        actorInfo.setPaletteIndex(index, 'patch')
        else:
            # Boundary patches carry the case's meaning -- inlet, outlet, wall
            # -- and every one of them used to render in the same default
            # white, so telling them apart meant clicking each in turn.
            self.assignPatchPalette(
                BoundaryActor, 'patch', keys=set(self._patchIds))

        self.assignPatchPalette(
            (MeshActor, BoundaryActor), 'zone',
            keys=set(self._zoneIds) | set(self._regionPartIds))

    async def loadResult(self, handle) -> str:
        """Draw the artifact ``handle`` names, whichever format it is in.

        CP-02 (C31-03). The two readers this product needs are the OpenFOAM
        ``constant/polyMesh`` and the native Gmsh ``.msh``, and which one runs
        is decided by the result's recorded format -- not by looking for a
        polyMesh and giving up. MEASURED in the tier-1 sweep: four of ten Gmsh
        runs were refused on quality, and every one of those cases holds its
        ``mesh.msh`` and no ``constant/polyMesh`` at all, so the polyMesh-only
        route reported "no mesh" over a mesh that was sitting on disk.

        Nothing here needs a solver target, an export or an SU2 installation:
        the native file is opened as it was written.

        Returns '' on success, or a sentence saying why not.
        """
        if handle is None:
            return 'there is no result to draw'
        artifact_id = getattr(handle, 'artifact_id', '') or ''
        self._runId = str(getattr(handle, 'run_id', '') or '')
        describe = getattr(handle, 'describe', None)
        self._resultLabel = str(describe()) if callable(describe) else ''
        fmt = getattr(handle, 'artifact_format', '') or ''
        if fmt == POLY_MESH:
            root = getattr(handle, 'case_root', '') or ''
            if not root:
                return 'this result names a polyMesh but no case to open it in'
            await self.load(0, root=root, artifact_id=artifact_id)
            return ''
        if fmt == GMSH_MSH:
            return await self.loadNative(
                getattr(handle, 'artifact_path', ''), artifact_id=artifact_id)
        if fmt == GMSH_SURFACE_MSH:
            # DP-133. The same file format, read the other way, and the
            # reader is told so rather than left to work it out: a surface
            # mesh arriving where a volume was expected is the failure it
            # refuses by default. The stage is what the sentence beside the
            # picture calls it, so the user can tell this mesh from the
            # volume one at a glance instead of by counting cells.
            return await self.loadNative(
                getattr(handle, 'artifact_path', ''), artifact_id=artifact_id,
                stage=self.tr('Surface pass'), surface_only=True)
        if not fmt:
            return 'this run left no artifact to read'
        return f'no reader for {fmt}'

    async def loadSessionMesh(self, controller) -> str:
        """Draw the mesh the resident Gmsh session holds, with its quality.

        Plan 41 GN13. The hand-over is written by the session (a file, as
        Plan 39 Q1 decided), read off the GUI thread, and drawn like any
        native mesh -- no ``.msh`` written, no polyMesh made. Returns ''
        on success or the reason nothing was drawn.
        """
        directory = Path(controller.case_path) / 'foammesh' / 'gmsh'             / 'handover'
        loop = asyncio.get_running_loop()
        try:
            written = await loop.run_in_executor(
                None, controller.handover, directory)
        except Exception as error:                                 # noqa: BLE001
            return str(error)
        mesh = (written or {}).get('mesh') or {}
        if not mesh.get('host_path'):
            return 'the session holds no mesh to draw'
        use = mesh.get('dims') or []
        self._keepPassSnapshot(controller, mesh)
        problem = await self.loadNative(
            mesh['host_path'], artifact_id='gmsh-session',
            stage=self.tr('Gmsh session'), surface_only=3 not in use)
        if not problem:
            self._nativeStoreRoot = Path(controller.case_path)
        return problem

    def _keepPassSnapshot(self, controller, mesh) -> None:
        """Plan 41 GN23 (FM-08). The hand-over of a stage that ends a pass
        (1D, 2D, 3D, Optimize, Set order) is kept for stage compare, keyed
        to that pass. A failed keep is logged, never a failure to draw."""
        from foammesh.core.gmsh import pass_snapshots
        stages = list(getattr(getattr(controller, 'timeline', None),
                              'stages', ()) or ())
        last = stages[-1] if stages else None
        key = pass_snapshots.pass_key(getattr(last, 'action', ''))
        if key is None or not getattr(last, 'ok', False):
            return
        source = Path(mesh['host_path'])
        try:
            pass_snapshots.capture_file(
                controller.case_path, key, source, action=last.action,
                after=dict(getattr(last, 'after', {}) or {}))
        except Exception:                                      # noqa: BLE001
            logger.warning('could not keep the %s pass', key, exc_info=True)

    async def loadNative(self, path, artifact_id: str = '', stage: str = '',
                         *, surface_only: bool = False) -> str:
        """Draw a native Gmsh ``.msh`` directly, publishing nothing.

        The scene is built from the mesher's own file, so a refused candidate
        is drawn from its own run directory and never written to the accepted
        case root merely to be rendered.

        Returns '' on success, or the actual read failure -- a viewport that
        shows nothing and says nothing is the defect this replaces.
        """
        if not path:
            return 'this run left no artifact to read'
        if not self._displayControl.isEnabled():
            logger.info('mesh load skipped: rendering is disabled')
            return ''

        self._load_generation += 1
        generation = self._load_generation
        hidden = {key for key, info in self._actorInfos.items()
                  if not info.isVisible()}
        # DP-128. The scene on screen is not cleared here. Reading a mesh
        # takes seconds, and clearing first means the viewport is empty for
        # every one of them -- a reload blanked the mesh a user was looking
        # at and brought it back unchanged. The old scene stands in until the
        # new one is in hand, and the two swap in `_buildScene` below.
        previous = _sourceState(self)
        self._time = None
        self._root = None
        self._loader = None
        self._nativePath = Path(path)
        self._artifactId = str(artifact_id or '')
        self._stage = str(stage or '')
        # DP-133. Kept so `reload` re-reads the file the same way. A surface
        # pass re-read as a volume would refuse, and the mesh a user was
        # looking at would come off the screen on a reload.
        self._surfaceOnly = bool(surface_only)
        self._setPreview({})

        progressDialog = ProgressDialog(
            app.window, self.tr('Loading mesh'), cancelable=True,
            autoCloseOnCancel=False, onViewport=True)
        progressDialog.setLabelText(self.tr('Loading mesh…'))
        progressDialog.open()
        cancellation = _Cancellation(progressDialog)
        try:
            # Off the event loop: the parse plus the VTK conversion is
            # seconds of pure Python on the meshes this product produces
            # (measured 0.98 s for the 141,486-cell finned_tube candidate),
            # and freezing the window for it is the R95 complaint again.
            # Plan 39 VI1. Cancel stops waiting for the read and leaves the
            # picture alone; the read thread is not a process and finishes
            # on its own, its result dropped.
            # Plan 41 GN13. The session's hand-over (``mesh.vtu``) is read
            # by its own reader; a ``.msh`` by the MSH parser.
            reader = (handover_scene.read_handover_scene
                      if handover_scene.is_handover(self._nativePath)
                      else read_msh_scene)
            reading = asyncio.get_running_loop().run_in_executor(
                None, functools.partial(reader, self._nativePath,
                                        surface_only=self._surfaceOnly))
            cancellation.task = reading
            scene = await reading
        except asyncio.CancelledError:
            if not cancellation.requested:
                raise
            _restoreSource(self, previous)
            self.loadCancelled.emit()
            return ''
        except MshSceneError as error:
            # DP-128. The old scene stood in while the read ran; it cannot
            # stand in for a read that failed, so it comes down here instead.
            self.clear()
            self._nativePath = None
            self._artifactId = ''
            self.cellCountChanged.emit(0, 0)
            return str(error)
        except Exception as error:                                 # noqa: BLE001
            logger.debug('native mesh read failed', exc_info=True)
            self.clear()
            self._nativePath = None
            self._artifactId = ''
            self.cellCountChanged.emit(0, 0)
            return f'{Path(path).name}: {error}'
        finally:
            _closeProgress(progressDialog)

        if generation != self._load_generation:
            return ''
        # Plan 35 CR3 step 5. `clear` disposes every actor of the old scene
        # here, on the GUI thread, and the collection that follows reaps
        # what it leaves before the new scene is built.
        def _prepare():
            self._nativePatches = dict(scene.patches)
            self._nativeCurves = dict(getattr(scene, 'curves', None) or {})
            self._nativeStoreRoot = None
        if not await _swapInScene(
                self, generation, _prepare,
                getattr(self, '_buildSceneSteps', None), scene.vtk_mesh, hidden):
            return ''

        return ''

    async def load(self, time: int, root=None, artifact_id: str = '',
                   stage: str = '', *, previewMode: str = 'auto',
                   buildAnyway: bool = False):
        """Draw time ``time`` of the case at ``root`` (the project by default).

        ``root`` is a case directory -- the thing that holds ``constant/
        polyMesh`` -- not the polyMesh itself, because that is what the reader
        opens. It exists so a run's own result can be drawn without publishing
        it to the project first (F-37).

        Plan 35 CR3. The picture is built in a worker (``MeshPreviewLoader``):
        the boundary surface by default, the volume too below 200k cells or
        when ``previewMode`` is ``'volume'`` ("Load full volume").
        ``buildAnyway`` is the user overriding a refusal on memory.
        """
        assert time >= 0

        if not self._displayControl.isEnabled():
            # Rendering is switched off. Nothing to draw into -- but record
            # it, because this return is otherwise indistinguishable from a
            # mesh that failed to read (R95/R156).
            logger.info('mesh load skipped: rendering is disabled')
            return

        # Deliberately no gather here. The reader picks whichever of the two
        # meshes on disk is the newer, so a run that has not been
        # reconstructed yet still draws straight from the processor cases.
        # Reconstructing to display would cost a full gather after every
        # stage for a mesh `reconstructPar` has usually already written.
        # DP-140: what it must not do is read the decomposition *instead* of
        # a reconstruction that is sitting right there -- that draws the
        # subdomain cuts as if they were mesh.
        self._load_generation += 1
        generation = self._load_generation
        # DP-485. A job that is rewriting the polyMesh is not read under.
        # The job's own stage reload follows it, and supersedes this one.
        await self._waitForWriters()
        if generation != self._load_generation:
            return
        # R27. Carry the user's per-part visibility across the reload. Only
        # actors that come back are restored, so a mesh with different patches
        # starts from its own defaults rather than inheriting a hidden row
        # that no longer means anything.
        hidden = {key for key, info in self._actorInfos.items()
                  if not info.isVisible()}
        # DP-128. Not cleared here -- see `loadNative`. Reading a polyMesh is
        # seconds of work, and this method is what a stage reload calls, so
        # clearing first emptied the viewport for the whole of every reload.
        previous = _sourceState(self)
        self._time = time
        self._root = Path(root) if root else Path(app.facadeClient.case_root)
        self._nativePath = None
        self._surfaceOnly = False
        self._artifactId = str(artifact_id or '')
        self._stage = str(stage or '')

        # Plan 39 VI14d. Opening the strip is 25 to 32 ms of its own (the
        # strip, the status bar and the task list all answer it); the call
        # that started the load, and the work after, each get a turn of the
        # event loop around it so the three do not make one 60 ms hold.
        await asyncio.sleep(SLICE_REST_SECONDS)
        if generation != self._load_generation:
            return
        progressDialog = ProgressDialog(
            app.window, self.tr('Loading mesh'), cancelable=True,
            autoCloseOnCancel=False, onViewport=True)
        progressDialog.setLabelText(self.tr('Loading mesh…'))
        progressDialog.open()
        await asyncio.sleep(SLICE_REST_SECONDS)
        cancellation = _Cancellation(progressDialog)

        # DP-711. Region points reach the loader only when there are any.
        # Asked of the class: it needs no state of this manager.
        seeds = MeshManager._regionSeeds()
        options = {'regionSeeds': seeds} if seeds else {}
        if previewMode != 'auto':
            options['mode'] = previewMode
        if buildAnyway:
            options['buildAnyway'] = True
        self._loader = MeshPreviewLoader(self._root / 'case.foam', **options)
        loader = self._loader
        loader.progress.connect(progressDialog.setLabelText)

        self._readStarted()
        finished = False
        cancelled = False
        try:
            # Plan 39 VI1. A task of its own, so Cancel can stop the read:
            # cancelling it kills the preview worker's whole process tree
            # (`local_worker.run_worker`), and the scene on screen stays.
            reading = asyncio.ensure_future(loader.loadMesh(self._time))
            cancellation.task = reading
            vtkMesh = await reading
            preview = _previewOf(loader)
            finished = True
        except asyncio.CancelledError:
            if not cancellation.requested:
                raise
            cancelled = True
        except Exception:
            finished = True
            # DP-128. A read that failed leaves nothing to swap in, so the
            # scene that was standing in for it comes down.
            self.clear()
            raise
        finally:
            self._readFinished()
            _closeProgress(progressDialog)
            # Plan 35 CR3. The reader caches a whole copy of the mesh; it
            # goes as soon as the read is over. Not on a cancellation: the
            # read may still be running on the VTK thread.
            if finished:
                _disposeLoader(loader)
                if self._loader is loader:
                    self._loader = None
            elif cancelled and self._loader is loader:
                self._loader = None
        if cancelled:
            # The old scene was never cleared, so it is still the picture.
            if generation == self._load_generation:
                _restoreSource(self, previous)
                self.loadCancelled.emit()
            return
        if generation != self._load_generation:
            # Superseded mid-read. Leave the scene alone: the load that
            # overtook this one owns what is on screen now.
            return
        if previewMode == 'volume' and preview['shown'] == 'outline' \
                and self._actorInfos:
            # "Load full volume" refused: the surface on screen stays, and
            # the notice says why the volume is not there.
            notice = dict(preview['notice'] or {})
            notice.update(kind='volume_refused', actions=[], patches=[])
            self._previewNotice = notice
            self.previewNoticeChanged.emit(notice)
            return
        # Plan 35 CR3 step 5 -- see `loadNative`.
        # Plan 39 VI14b: swapped in slices, so the window keeps answering.
        if not await _swapInScene(
                self, generation, lambda: self._setPreview(preview),
                getattr(self, '_buildSceneSteps', None), vtkMesh, hidden, precomputed=preview['precomputed'],
                outline=preview['outline']):
            return
        self.previewNoticeChanged.emit(self._previewNotice)
        self._startCaseFields(generation)

    # -- Plan 39 VI14 (check 8b): the hover names the cell's level and zone -- #

    _caseFieldsTask = None

    def _startCaseFields(self, generation: int) -> None:
        """Read the case's cellLevel and cellZones for the volume on screen."""
        if not self._volumeActors() or self.caseRoot() is None:
            return
        try:
            self._caseFieldsTask = asyncio.get_running_loop().create_task(
                self._attachCaseFields(generation))
        except RuntimeError:
            self._caseFieldsTask = None

    async def _attachCaseFields(self, generation: int) -> None:
        """Put the refinement level and the cell zones of the case's polyMesh
        on the loaded volume, so the cell under the cursor says them.

        Read off the GUI thread, by the reader Colour mesh by uses, and put
        on only a volume of the whole mesh's cell count. A case without the
        files, or whose files are of another mesh, leaves the volume as it
        was: the readout then simply has no such row.
        """
        from vtkmodules.vtkCommonCore import vtkStringArray

        from foammesh.core.section import section_colour
        from foammesh.view.display_control import section_case_fields as fields

        try:
            meshDir, _why = fields.mesh_directory(str(self.caseRoot()))
            if meshDir is None:
                return
            for key in (section_colour.CELL_LEVEL, section_colour.CELL_ZONE):
                if not fields.has_member(meshDir, key):
                    continue
                field = await asyncio.to_thread(
                    fields.read_case_field, meshDir, key)
                if (generation != self._load_generation
                        or field.status != fields.READ):
                    continue
                for actor in self._volumeActors():
                    data = actor.dataSet()
                    if not fields.attach(data, field):
                        continue
                    if key == section_colour.CELL_ZONE and field.names:
                        names = vtkStringArray()
                        names.SetName('cellZoneNames')
                        for name in field.names:
                            names.InsertNextValue(str(name))
                        info = data.GetFieldData()
                        if info.GetAbstractArray('cellZoneNames') is not None:
                            info.RemoveArray('cellZoneNames')
                        info.AddArray(names)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            logger.exception('Reading the case fields for the hover')

    def _setPreview(self, preview: dict):
        self._previewCounts = preview.get('counts')
        self._patchFaces = dict(preview.get('patchFaces') or {})
        self._previewNotice = preview.get('notice')
        if not preview:
            self.previewNoticeChanged.emit(None)

    def previewNotice(self):
        """What the viewport says about its picture (Plan 35 CR3), or None."""
        return self._previewNotice

    def _previewCells(self) -> int:
        return int(estimated_cells(self._previewCounts) or 0)

    async def retryPreview(self):
        """Build the preview again, the default way."""
        if self._time is None or self._nativePath is not None:
            return
        await self.load(self._time, root=self._root,
                        artifact_id=self._artifactId, stage=self._stage)

    async def buildPreviewAnyway(self):
        """Build the preview with the budget raised to 90% of the machine.

        The caller has already warned the user: the worker may take most of
        the memory there is, and the rest of the machine will page.
        """
        if self._time is None or self._nativePath is not None:
            return
        await self.load(self._time, root=self._root,
                        artifact_id=self._artifactId, stage=self._stage,
                        buildAnyway=True)

    async def loadFullVolume(self):
        """Read the volume too, in the worker.

        There is no cell cap (user rule 2026-10-01): the only refusal is the
        free RAM the volume's files need (Plan 39 F1d).

        Plan 41 GN13. A native mesh (a ``.msh`` or the session's hand-over)
        drawn as its surface is read again as the volume; a file that holds
        no volume says so and leaves the surface on screen.
        """
        if self._nativePath is not None:
            if not self._surfaceOnly:
                return
            path, artifact, stage = (self._nativePath, self._artifactId,
                                     self._stage)
            problem = await self.loadNative(path, artifact, stage,
                                            surface_only=False)
            if problem:
                self.nativeVolumeRefused.emit(problem)
                await self.loadNative(path, artifact, stage,
                                      surface_only=True)
            return
        if self._time is None:
            return
        await self.load(self._time, root=self._root,
                        artifact_id=self._artifactId, stage=self._stage,
                        previewMode='volume')

    @staticmethod
    def _regionSeeds() -> list:
        """``[(name, point), ...]`` from the Regions page (DP-711).

        The reader uses them to name the regions of a mesh that holds several
        without saying which cells are which. No case, or a region without a
        point, simply gives nothing to split by.
        """
        try:
            regions = app.db.getElements('region')
        except Exception:                           # noqa: BLE001 - no case
            return []
        seeds = []
        for region in regions.values():
            try:
                seeds.append((region.value('name'), region.vector('point')))
            except Exception:                       # noqa: BLE001
                continue
        return seeds

    @staticmethod
    def _writerActive() -> bool:
        try:
            return bool(app.facadeClient.session().jobs.mesh_write_pending)
        except Exception:       # noqa: BLE001 - no case open writes nothing
            return False

    async def _waitForWriters(self):
        """Return once no job is rewriting the case (DP-485).

        MEASURED on `centrifugal_impeller`, 12 processors: pressing layers
        commits the page's settings, the commit schedules a scene refresh,
        and the refresh reloaded the snap mesh out of `processor*/constant/
        polyMesh` while addLayers was writing those same files. VTK read a
        truncated `faces` ("Unexpected EOF") and the GUI died rc=139.
        """
        while self._writerActive():
            await asyncio.sleep(0.25)

    def _readStarted(self):
        self._reads += 1
        self._readsIdle.clear()

    def _readFinished(self):
        self._reads = max(0, self._reads - 1)
        if not self._reads:
            self._readsIdle.set()

    async def readsFinished(self):
        """The read barrier a mutating job awaits before it starts."""
        await self._readsIdle.wait()

    def _buildScene(self, vtkMesh, hidden=frozenset(), precomputed=None,
                    outline=None):
        """Build the whole scene in one go (see :meth:`_buildSceneSteps`)."""
        for _ in self._buildSceneSteps(vtkMesh, hidden, precomputed, outline):
            pass

    def _buildSceneSteps(self, vtkMesh, hidden=frozenset(), precomputed=None,
                         outline=None):
        """Turn one loaded mesh into actors, whichever reader produced it.

        A generator: it yields between its steps (after each actor, and
        between the closing passes) so a caller on the GUI thread can hand the
        event loop back when a slice has used its budget -- Plan 39 VI14b,
        the 6.6 M-cell load held the thread for 161 ms in this one call.

        The polyMesh reader and the native ``.msh`` reader hand over the same
        shape -- region -> boundary/internalMesh/zones -- so both meshes reach
        the same actors, the same tree, the same palettes and the same
        selection. A native Gmsh mesh's physical groups arrive as the boundary
        entries here, which is what makes its patches pickable and hideable
        independently of the volume.

        Plan 35 CR3. ``precomputed`` maps ``(region, category, name)`` to the
        surface and feature edges the preview worker already computed for
        that part, so the actor's own filters do not run on first paint
        (step 10). ``outline`` is the bounding box drawn when no preview could
        be built; it is not a patch.
        """
        precomputed = precomputed or {}

        def _adopt(actor, key):
            parts = precomputed.get(key)
            use = getattr(actor, 'usePrecomputed', None)
            if parts and use is not None:
                use(surface=parts.get('surface'), edges=parts.get('edges'))
            return actor

        # DP-713. A new scene carries no deviation colouring, so neither
        # does what describes it.
        self._deviation = None
        self._deviationState = None
        # Plan 39 VI16. Nor the Layers grown colouring.
        self._layersGrown = None
        # DP-714. Nor any surface faded for a colouring it no longer has.
        self._qualityFaded = {}
        patch_ids: list[str] = []
        zone_ids: list[str] = []
        region_part_ids: list[str] = []
        region_ids: dict[str, list[str]] = {}
        patch_bounds: dict[str, tuple] = {}
        volume_of: dict[str, str] = {}
        skin_regions: dict[str, list[str]] = {}
        if vtkMesh:
            multi_region = len(vtkMesh) > 1
            for rname, region in vtkMesh.items():
                region_ids.setdefault(rname, [])
                prefix = f'{rname}:' if rname else ''
                for bname, polyData in region['boundary'].items():
                    actor_id = f'{prefix}{bname}'
                    display = f'{rname}/{bname}' if rname else bname
                    self.add(_adopt(BoundaryActor(polyData, actor_id, display),
                                    (rname, 'boundary', bname)))
                    yield
                    patch_ids.append(actor_id)
                    # Not every reader hands over a vtkPolyData; one
                    # that cannot say where it is simply does not
                    # take part in the enclosure question.
                    reportBounds = getattr(polyData, 'GetBounds', None)
                    if reportBounds is not None:
                        patch_bounds[actor_id] = reportBounds()
                    region_ids[rname].append(actor_id)
                internal_id = (
                    f'{prefix}internalMesh' if multi_region
                    else 'internalMesh')
                display = (
                    f'{rname}/internalMesh' if rname else 'internalMesh')
                # DP-133. A surface mesh has no volume, and the surface pass
                # of a 3D run is now something a user can ask to see. The
                # reader says so by handing over `internalMesh: None` rather
                # than an empty grid, because an empty grid is a row in the
                # tree that can be picked and hidden and shows nothing when
                # it is. Every use of `internal_id` below is inside this
                # guard: with no volume there is nothing for a patch to fade
                # behind and nothing to bias away from it.
                internal = region['internalMesh']
                if internal is not None:
                    internal_actor = _adopt(
                        MeshActor(internal, internal_id, display),
                        (rname, 'internalMesh', ''))
                    # DP-141. That same coincidence is also a depth fight: the
                    # volume's exterior surface and the patches are the same
                    # faces, reaching the renderer through two different
                    # triangulations, so neither wins cleanly and the model
                    # renders as speckle. The patches carry the case's
                    # meaning, so the volume is the one that steps back.
                    internal_actor.setDepthBias(VOLUME_DEPTH_BIAS)
                    self.add(internal_actor)
                    yield
                    # The volume's own exterior surface is the same box its
                    # boundary patches describe, so a patch that fades without
                    # it fades behind an opaque copy of itself.
                    for member in region_ids[rname]:
                        volume_of[member] = internal_id
                    region_ids[rname].append(internal_id)
                    # The region's patches in boundary order: the order the
                    # worker numbered them in the exterior's `patchId`.
                    skin_regions[internal_id] = [
                        f'{prefix}{bname}' for bname in region['boundary']]
                zones = region.get('zones', region)
                # DP-711. `regions` is the reader naming the pieces of a mesh
                # that holds several regions without cell zones (S5's fluid
                # and solid). Each is a volume part like a cell zone, filed
                # apart from the zones so nothing counts it as one.
                for category in ('cellZones', 'faceZones', 'regions'):
                    collection = zones.get(category, {})
                    if not isinstance(collection, dict):
                        continue
                    for zone_name, data_set in collection.items():
                        actor_id = f'{prefix}{category}:{zone_name}'
                        display = (
                            f'{rname}/{category}/{zone_name}' if rname
                            else f'{category}/{zone_name}')
                        actor_type = (
                            BoundaryActor if category == 'faceZones'
                            else MeshActor)
                        self.add(_adopt(
                            actor_type(data_set, actor_id, display),
                            (rname, category, zone_name)))
                        yield
                        (region_part_ids if category == 'regions'
                         else zone_ids).append(actor_id)
                        region_ids[rname].append(actor_id)
        self._regionIds = region_ids
        self._patchIds = patch_ids
        self._zoneIds = zone_ids
        self._regionPartIds = region_part_ids
        if outline is not None:
            self.add(BoundaryActor(outline, 'meshOutline',
                                   self.tr('Mesh outline')))
        self._assignPalettes()
        yield
        self._fadeEnclosures(patch_bounds, volume_of)
        yield
        for key in set(hidden) & set(self._actorInfos):
            self._actorInfos[key].setVisible(False)
        self._buildSkins(skin_regions)
        yield
        yield from self._presentArtifactSteps()
        yield
        self.applyToDisplay()
        yield
        self.fitDisplay()
        self._notifyCellCountChange()
        rebuild = getattr(app.window, 'rebuildOverlayParts', None)
        if rebuild is not None and not _partsQuiet(self._displayControl):
            # Inside a sliced swap the overlay is rebuilt once, when the
            # swap announces the parts (`_QuietParts.announce`).
            rebuild()

    # -- Plan 39 VI4 (F1e): patches as edges over the volume's exterior ----- #

    def _buildSkins(self, regions: dict):
        """Let each volume's exterior carry its patches' faces.

        ``regions`` maps a volume's actor id to its patches' ids in the order
        the exterior's ``patchId`` numbers them. A volume whose exterior does
        not say (a filter's, a native mesh's) is left as it was.
        """
        for volume_id, patch_ids in regions.items():
            volume = self._actorInfos.get(volume_id)
            patches = [self._actorInfos.get(i) for i in patch_ids]
            if (not isinstance(volume, MeshActor) or not patches
                    or not callable(getattr(volume, 'carriesPatchIds', None))
                    or not volume.carriesPatchIds()
                    or not all(isinstance(p, BoundaryActor) for p in patches)):
                continue
            self._skins.append(PatchSkin(volume, patches))
        view = getattr(self._displayControl, 'view', None)
        view = view() if callable(view) else None
        if view is not None and hasattr(view, 'setPickResolver'):
            view.setPickResolver(
                self._resolveSkinPick if self._skins else None)

    def skins(self) -> list:
        return list(self._skins)

    def remove(self, key):
        # A skin lets go of its actors before any of them goes.
        for skin in tuple(self._skins):
            if key == skin.volume().id() or any(
                    key == patch.id() for patch in skin.patches()):
                skin.dispose()
                self._skins.remove(skin)
        super().remove(key)

    def _resolveSkinPick(self, actor, x, y, renderer):
        """A pick on a volume's exterior is the patch whose face it is."""
        from vtkmodules.vtkRenderingCore import vtkCellPicker

        for skin in self._skins:
            if actor is not skin.volume().actor():
                continue
            picker = vtkCellPicker()
            picker.SetTolerance(0.0005)
            picker.PickFromListOn()
            picker.AddPickList(actor)
            if not picker.Pick(float(x), float(y), 0.0, renderer):
                return actor
            data = picker.GetDataSet()
            ids = data.GetCellData().GetArray('patchId') if data else None
            cell = int(picker.GetCellId())
            if ids is None or not 0 <= cell < ids.GetNumberOfTuples():
                return actor
            patch = skin.patchAt(int(ids.GetValue(cell)))
            return patch.actor() if patch is not None else actor
        return actor

    def _fadeEnclosures(self, patch_bounds: dict, volume_of: dict):
        """Draw an outer boundary that holds a body so the body reads through it.

        DP-136. The geometry view already does this -- every surface is drawn
        at less than full opacity, which is why an imported cyclone in its
        tunnel shows the cyclone. The mesh view draws every patch solid, so the
        moment blockMesh finishes the same case becomes a grey box and stays
        one for every remaining stage of the run: castellation, snap, layers,
        all identical from outside.

        :func:`enclosing_part_ids` decides which patch that is, from geometry
        rather than from a name or a group -- see its docstring for why those
        do not separate the two cases. It answers with nothing at all for an
        ordinary internal-flow mesh, which is most of them, and that is the
        path that leaves the scene exactly as it was.

        MEASURED: fading the patch alone changed not one pixel. The region's
        ``internalMesh`` actor sends the volume's *exterior* surface to the
        renderer, which for an enclosure case is that same outer box, drawn
        solid on top. So the volume behind an enclosure fades with it, while
        the body's own patch keeps full opacity and reads through both.
        """
        for actor_id in enclosing_part_ids(patch_bounds):
            for target in (actor_id, volume_of.get(actor_id)):
                actor_info = (self._actorInfos.get(target)
                              if target is not None else None)
                if actor_info is not None:
                    actor_info.setOpacity(ENCLOSURE_OPACITY)

    def meshSummary(self) -> str:
        """What the viewport is showing and how big it is (DP-96)."""
        return self._summary

    def boundaryFaceCount(self) -> int:
        """Faces on the named patches, which is the mesh's skin.

        DP-529 (MA G1-P2, G1 ``jacketed_pipe``): a faceZone is drawn with a
        ``BoundaryActor`` too, and counting every one of those said 1,838
        boundary faces for a mesh whose patches hold 1,216 -- the other 622
        are the interface between the two volumes, which is interior. Only
        the patches are counted.
        """
        if self._patchFaces and not self._patchIds:
            # No preview was built: the counts come from the boundary file.
            return int(sum(self._patchFaces.values()))
        total = 0
        for actor_id in self._patchIds:
            if actor_id in self._patchFaces:
                # CR3: a decimated patch is not its mesh's face count.
                total += self._patchFaces[actor_id]
                continue
            actorInfo = self._actorInfos.get(actor_id)
            if not isinstance(actorInfo, BoundaryActor):
                continue
            dataSet = getattr(actorInfo, 'dataSet', None)
            count = getattr(dataSet() if dataSet else None,
                            'GetNumberOfCells', None)
            if count is not None:
                total += int(count())
        return total

    def pointCount(self) -> int:
        """Points in the volume, counted once.

        Only the volume is asked. Each patch carries its own copy of the
        points it shares with its neighbours, so adding the patches up
        reports a mesh as larger than it is -- and a number that is wrong in
        the user's favour is worse than no number.
        """
        for actorInfo in self._actorInfos.values():
            if isinstance(actorInfo, MeshActor):
                dataSet = getattr(actorInfo, 'dataSet', None)
                count = getattr(dataSet() if dataSet else None,
                                'GetNumberOfPoints', None)
                return int(count()) if count is not None else 0
        if self._previewCounts:
            return int(self._previewCounts.get('points') or 0)
        return 0

    def _presentArtifact(self):
        """The whole of :meth:`_presentArtifactSteps`, in one go."""
        for _ in self._presentArtifactSteps():
            pass

    def _presentArtifactSteps(self):
        """Put the mesh a load just built in front of the user (DP-95/DP-96).

        Three things, none of which the user should have to ask for: the
        edges are drawn, so the picture is of a mesh and not of a skin; the
        geometry it was built from steps out of the way, because it occupies
        the same space and wins the depth test as often as not; and the
        sentence beside it says which stage made it, how many cells, faces
        and points it has, and how big it is in model units.

        Called before ``applyToDisplay`` so the modes reach the renderer in
        the same repaint as the actors do -- a mesh that appears smooth and
        then re-draws with edges is the same flicker in a slower form.
        """
        cells = self.getNumberOfCells()
        faces = self.boundaryFaceCount()
        kind = artifact_kind(cells, faces)
        member = getattr(DisplayMode, display_mode_name(kind), None)
        if member is not None:
            for actorInfo in self._actorInfos.values():
                setMode = getattr(actorInfo, 'setDisplayMode', None)
                if setMode is not None:
                    setMode(member)
        # Plan 39 VI14d. Each of the three costs 18 to 24 ms on the 6.6 M-cell
        # volume (the bounds of 6 M points, the readouts that follow the
        # summary, the geometry stepping aside); one slice of the swap
        # holding all of them held the GUI thread 42 ms on top of its
        # neighbours.
        yield
        # An actor that cannot say where it is is not a reason to refuse to
        # draw the mesh. The size sentence loses its extent and keeps its
        # counts, which is the part that cannot be got anywhere else.
        try:
            bounds = self.getBounds()
            size = bounds.size() if bounds is not None else None
        except Exception:                                    # noqa: BLE001
            logger.debug('mesh extent unavailable', exc_info=True)
            size = None
        self._summary = summary_text(
            self._stage, kind, cells, faces, self.pointCount(), size)
        yield
        self.meshSummaryChanged.emit(self._summary)
        yield
        if hides_geometry(kind):
            geometry = getattr(app.window, 'geometryManager', None)
            hide = getattr(geometry, 'hide', None)
            if hide is not None:
                hide()

    def dispose(self):
        """Release the whole scene for good -- a project closing (Plan 35 CR3).

        A load still in flight keeps its own loader and disposes it when its
        read ends; bumping the generation makes it build nothing afterwards.
        """
        self._load_generation += 1
        self._loader = None
        super().dispose()

    def unload(self):
        # A failed-cell overlay belongs to a specific mesh/result pair; never
        # let it survive a case/mesh unload and reappear over different data.
        self.remove('failedCells')
        # DP-2725. What it faded goes with the mesh it faded.
        self._failedFaded = {}
        self._qualityFaded = {}
        self._qualityFilterOn = False
        self.hide()
        self._load_generation += 1
        self._time = None
        self._root = None
        self._nativePath = None
        self._artifactId = ''
        self._runId = ''
        self._resultLabel = ''
        self._stage = ''
        self._surfaceOnly = False
        self._summary = ''
        self._setPreview({})
        self.meshSummaryChanged.emit('')
        # R87. The toolbar kept the last number it was handed: `36,533 cells`
        # was still on screen after a Base Grid reset had deleted
        # `constant/polyMesh` outright. It is the only always-visible
        # statement of how big the current mesh is, so it goes when the mesh
        # goes.
        self.cellCountChanged.emit(0, 0)

    async def reload(self):
        if self._nativePath is not None:
            # A native result has no time directory and no case root to go
            # back to; it reloads from the file it was drawn from.
            await self.loadNative(self._nativePath,
                                  artifact_id=self._artifactId,
                                  stage=self._stage,
                                  surface_only=self._surfaceOnly)
            return
        if self._time is None:
            return

        await self.load(self._time, root=self._root,
                        artifact_id=self._artifactId, stage=self._stage)

    @qasync.asyncSlot()
    async def show(self, time: int):
        if time < 0:
            return

        if self._time == time:
            self._show()
        else:
            await self.load(time)

    def boundaries(self):
        return self._actorInfos.keys()

    def getScalarRange(self, index: MeshQualityIndex) -> tuple[float, float]:
        actorInfo: ActorInfo
        for actorInfo in self._actorInfos.values():
            if isinstance(actorInfo, MeshActor):
                return actorInfo.getScalarRange(index)

        return 0.0, 0.0

    def getNumberOfDisplayedCells(self) -> int:
        actorInfo: ActorInfo
        for actorInfo in self._actorInfos.values():
            if isinstance(actorInfo, MeshActor):
                return actorInfo.getNumberOfDisplayedCells()

        # CR3: a surface preview has no volume to cut; all of it is shown.
        return self._previewCells()

    def getNumberOfCells(self) -> int:
        """Every cell in the loaded volume, section plane or not.

        F-44. The toolbar showed the *displayed* count alone, so cutting a
        section silently rewrote the size of the mesh: 39,921 cells became
        12,004 with nothing saying they were the same mesh. The displayed
        number is the useful one while cutting; it is only misleading without
        the number it is a fraction of.
        """
        actorInfo: ActorInfo
        for actorInfo in self._actorInfos.values():
            if isinstance(actorInfo, MeshActor):
                # The volume dataset the reader handed over, before any cut
                # filter. Asked through `getattr` because a dataset that
                # cannot answer is not a reason to fail a render: the caller
                # falls back to the displayed count, which is never wrong,
                # only less informative.
                count = getattr(actorInfo.dataSet(), 'GetNumberOfCells', None)
                return int(count()) if count is not None else 0

        # CR3: the volume of a surface preview is counted from its headers.
        return self._previewCells()

    def meshPartIds(self) -> list[str]:
        """The parts of the mesh: its patches and its zones.

        F-44. Not its render actors. One duct reported "14 parts" -- the STL
        surfaces it was meshed from, the four patches, the internal volume and
        the zones, all counted together because they are all props in the
        scene. A user counting parts is counting the mesh.
        """
        return list(self._patchIds) + list(self._zoneIds)

    def setScalar(self, index: MeshQualityIndex):
        for actorInfo in self._actorInfos.values():
            actorInfo.setScalar(index)

    def setScalarBand(self, low, high):
        for actorInfo in self._actorInfos.values():
            actorInfo.setScalarBand(low, high)

    def clearCellFilter(self):
        for actorInfo in self._actorInfos.values():
            actorInfo.clearCellFilter()
        self._qualityFilterOn = False
        # DP-2725. Failed cells still on screen keep the model see-through,
        # and the whole volume is back, so it is faded round them again.
        if 'failedCells' in self._actorInfos:
            self._fadeAroundFailedCells()
        else:
            self._restoreSurfaces()

        self._notifyCellCountChange()

    def _seeThroughSurfaces(self):
        """DP-714. Fade every surface while the poor cells are coloured.

        Viewport audit 0925 F10. The poor cells are volume cells, and most of
        them are inside: the boundary patches drawn over them are opaque, so
        rotated to the far side the highlight vanished behind the model.
        Each surface keeps its own opacity to come back to.
        """
        faded = getattr(self, '_qualityFaded', None) or {}
        for id_, actorInfo in self._actorInfos.items():
            kind = getattr(actorInfo, 'type', None)
            if kind is None or kind() is ActorType.MESH or id_ in faded:
                continue
            opacity = actorInfo.properties().opacity
            opacity = 1.0 if opacity is None else float(opacity)
            if opacity <= QUALITY_SEE_THROUGH_OPACITY:
                continue
            faded[id_] = opacity
            actorInfo.setOpacity(QUALITY_SEE_THROUGH_OPACITY)
        self._qualityFaded = faded

    def _restoreSurfaces(self):
        """Give the faded surfaces their opacity back -- unless the user set
        a new one while they were faded, which then stands."""
        faded = getattr(self, '_qualityFaded', None) or {}
        self._qualityFaded = {}
        for id_, opacity in faded.items():
            actorInfo = self._actorInfos.get(id_)
            if actorInfo is None:
                continue
            if actorInfo.properties().opacity == QUALITY_SEE_THROUGH_OPACITY:
                actorInfo.setOpacity(opacity)

    def showFailedCells(self, ids: list[int], set_name: str = 'Failed cells',
                        artifact_id: str = '') -> bool:
        """Replace the transient failed-cell actor with a selected cell-set overlay.

        ``artifact_id`` is the result the cell ids were computed on. Cell ids
        are positions in one particular mesh, so painting a run's failed cells
        over a different run's mesh highlights whatever happens to sit at
        those indices -- confidently, and wrongly. When the caller names an
        artifact and it is not the one displayed, nothing is drawn.
        """
        self.remove('failedCells')
        if not self.ownsArtifact(artifact_id):
            self._restoreAfterFailedCells()
            logger.info('failed-cell overlay refused: it belongs to %s, '
                        'the viewport is showing %s',
                        artifact_id, self._artifactId or '(nothing)')
            self.applyToDisplay()
            return False
        if not ids:
            self._restoreAfterFailedCells()
            self.applyToDisplay()
            return False

        internal_mesh = self._actorInfos.get('internalMesh')
        if not isinstance(internal_mesh, MeshActor):
            self.applyToDisplay()
            return False

        overlay = MeshActor(
            extract_selected_cells(internal_mesh.dataSet(), ids),
            'failedCells', self.tr('Failed: {0}').format(set_name))
        error_colour = '#ef5350'
        if app.themeManager is not None and app.themeManager.tokens is not None:
            error_colour = app.themeManager.tokens.value('status.error')
        overlay.setColor(QColor(error_colour))
        overlay.setOpacity(0.85)
        overlay.setDisplayMode(DisplayMode.SURFACE_EDGE)
        self.add(overlay)
        self._fadeAroundFailedCells()
        self.applyToDisplay()
        return True

    def _fadeAroundFailedCells(self):
        """DP-2725. Let the failed cells be seen inside the model.

        The cells are volume cells, most of them inside, and the volume was
        left drawn at full strength over them: on the snappy pipe only the
        cells touching the outlet showed. The volume is faded out and the
        patches see-through, as for the poor cells (DP-714), so the model's
        shape stays on screen around the cells. Each keeps its own opacity to
        come back to.
        """
        self._seeThroughSurfaces()
        if getattr(self, '_qualityFilterOn', False):
            # The volume shows only the poor cells now; it stays as it is.
            return
        faded = getattr(self, '_failedFaded', None) or {}
        for id_, actorInfo in self._actorInfos.items():
            if (id_ == 'failedCells' or id_ in faded
                    or not isinstance(actorInfo, MeshActor)):
                continue
            opacity = actorInfo.properties().opacity
            faded[id_] = 1.0 if opacity is None else float(opacity)
            actorInfo.setOpacity(FAILED_CELLS_VOLUME_OPACITY)
        self._failedFaded = faded

    def _restoreAfterFailedCells(self):
        """Give the volume and, unless the poor cells are still coloured,
        the surfaces their opacity back. An opacity the user set meanwhile
        stands."""
        self._restoreFadedVolume()
        if not getattr(self, '_qualityFilterOn', False):
            self._restoreSurfaces()

    def _restoreFadedVolume(self):
        faded = getattr(self, '_failedFaded', None) or {}
        self._failedFaded = {}
        for id_, opacity in faded.items():
            actorInfo = self._actorInfos.get(id_)
            if (actorInfo is not None and actorInfo.properties().opacity
                    == FAILED_CELLS_VOLUME_OPACITY):
                actorInfo.setOpacity(opacity)

    #: Fidelity verdict -> the status token its colour comes from. Roles, not
    #: hex, so the colouring follows the theme like everything else.
    FIDELITY_ROLES = {
        'pass': 'status.success',
        'blemish': 'status.warning',
        'warning': 'status.warning',
        'fail': 'status.error',
        'invalid': 'status.error',
    }

    def showSectionFidelity(self, sections, artifact_id: str = '') -> int:
        """WP3.3 tier one: colour each patch by its fidelity verdict.

        ``sections`` is ``solver_name -> verdict``. Reads from the stored
        report, so it works on any case that has ever been qualified -- which
        matters, because the fidelity check is a deliberate step and most
        opened meshes will not have a per-face field.

        A patch the report does not mention keeps its own colour rather than
        being greened by omission: not measured is not the same as passed.
        Nor is measured-on-another-run: a report naming an artifact that is
        not the one displayed colours nothing.
        """
        if not self.ownsArtifact(artifact_id):
            logger.info('fidelity colouring refused: it belongs to %s, '
                        'the viewport is showing %s',
                        artifact_id, self._artifactId or '(nothing)')
            return 0
        tokens = (app.themeManager.tokens
                  if app.themeManager is not None else None)
        coloured = 0
        for name, verdict in (sections or {}).items():
            actorInfo = self._actorInfos.get(name)
            if actorInfo is None or not isinstance(actorInfo, BoundaryActor):
                continue
            role = self.FIDELITY_ROLES.get(str(verdict))
            if role is None:
                continue
            colour = tokens.value(role) if tokens is not None else '#c62828'
            actorInfo.setColor(QColor(colour))
            coloured += 1

        self.applyToDisplay()
        return coloured

    def showFidelityHotspots(self, fields, artifact_id: str = '',
                             unit=live_distance.DEFAULT_UNIT,
                             tolerance=None) -> int:
        """WP3.3 tier two: colour each patch by *where* it left the reference.

        ``fields`` is ``solver_name -> (face_ids, deviations)``. A section-level
        verdict says which patch is wrong; this says which part of it, which is
        the question you actually have once a patch has failed.

        Bound to the displayed artifact for the same reason as the failed-cell
        overlay: face ids are positions in one particular mesh.
        """
        if not self.ownsArtifact(artifact_id):
            logger.info('fidelity hotspots refused: they belong to %s, '
                        'the viewport is showing %s',
                        artifact_id, self._artifactId or '(nothing)')
            return 0
        # DP-713. Model units in the file, millimetres on screen, and the
        # faces put where their labels say rather than where they fell.
        # Plan 39 VI5: ``unit`` is the case's length unit, not always metres.
        # Plan 39 VI6 (F4e): a stored run holds closest distances, never
        # negative, so it is painted on a 0 -> scale ramp and with the run's
        # own tolerance (``tolerance``, in mm) rather than a signed scale.
        factor = live_distance.unit_to_mm(unit)
        measured = {}
        for name, field in (fields or {}).items():
            if self._actorInfos.get(name) is None:
                continue
            try:
                face_ids, deviations = field
            except (TypeError, ValueError):
                continue
            measured[name] = (
                np.asarray(face_ids, dtype=np.int64).ravel(),
                np.abs(np.asarray(deviations, dtype=np.float64).ravel())
                * factor)
        universe = [(name, name in measured,
                     '' if name in measured else 'not in the run')
                    for name in self._patchIds]
        return self._paintDeviation(
            measured, self.patchStartFaces(), 'run', signed=False,
            tolerance=tolerance, universe=universe)

    def showLiveDeviation(self, reference,
                          unit=live_distance.DEFAULT_UNIT,
                          tolerance=None, identity=None) -> int:
        """DP-713. Colour each patch by its distance to ``reference``.

        Viewport audit 0925 F8. With no stored fidelity run the colouring had
        nothing to show, on a case whose reference geometry was loaded in
        the same window. This measures the patches that have a reference
        against that geometry now.

        Plan 39 VI5. ``reference`` is in the mesh's frame and ``unit`` is the
        case's length unit (a name or metres per model unit); the figures are
        millimetres whatever the mesh is modelled in.

        Plan 39 VI6. Only the patches that stand for a surface of the geometry
        are ticked and measured (F4b): a wall, or a patch ``identity`` (the
        patch-identity map) traces to prepared geometry -- not the far field,
        inlet, outlet, symmetry, empty, cyclic or wedge. The others stay
        listed, unticked, and a tick measures them anyway
        (`setDeviationSurface`). ``tolerance`` (mm) is the neutral band. The
        sign is kept only against a closed reference (F4f).
        """
        if reference is None:
            return 0
        surfaces = {}
        for actor_id in self._patchIds:
            actorInfo = self._actorInfos.get(actor_id)
            if (isinstance(actorInfo, BoundaryActor)
                    and actorInfo.dataSet() is not None):
                surfaces[actor_id] = actorInfo.dataSet()
        types = getattr(self, 'patchTypes', None)
        types = types() if callable(types) else {}
        universe = []
        for name in surfaces:
            solver = name.split(':', 1)[-1]
            known = identity.get(solver) if identity is not None else None
            has, why = live_distance.patch_reference(
                solver, types.get(name), known)
            universe.append((name, has, why))
        ticked = {name for name, has, _why in universe if has}
        notice = ''
        if not ticked:
            # No patch traces to the geometry (a mesh imported without its
            # sidecar): measure what could be a surface rather than nothing.
            ticked = {name for name, _has, why in universe
                      if why not in live_distance._NO_REFERENCE_TYPES}
            if ticked:
                notice = ('No patch is traced to the geometry; every patch '
                          'that can be a surface is measured.')
        prepared = live_distance.prepare_reference(reference)
        fields = {}
        measure = {name: surfaces[name] for name in ticked}
        distances, _prepared = live_distance.measure_surfaces_mm(
            prepared, measure, unit)
        for name, values in distances.items():
            fields[name] = (np.arange(values.size, dtype=np.int64), values)
        state = {'prepared': prepared, 'unit': unit, 'live': True}
        return self._paintDeviation(
            fields, {name: 0 for name in surfaces}, 'live',
            signed=prepared.closed, tolerance=tolerance, universe=universe,
            ticked=ticked,
            notice=' '.join(part for part in (prepared.notice, notice)
                            if part),
            state=state)

    def _paintDeviation(self, fields, starts, source, *, signed=None,
                        tolerance=None, scale=None, universe=None,
                        ticked=None, notice='', state=None,
                        keep_empty=False) -> int:
        """Paint ``name -> (face labels, mm)`` by |d| on one magnitude scale.

        Plan 43 LY11. The faces carry ``|d|`` (a face at -d and one at +d are
        one colour); the signed values stay in the readout for the numbers.

        ``universe`` is ``[(name, has_reference, why_not)]`` for every patch
        of the Surfaces list and ``ticked`` the names to paint (default: every
        name in ``fields``); the others keep their plain colour. ``scale``
        overrides the default, the 98th percentile of |d| over what is shown.
        """
        if signed is None:
            signed = source != 'run'
        universe = list(universe) if universe is not None else [
            (name, True, '') for name in fields]
        ticked = set(fields) if ticked is None else set(ticked)
        shown = {name: field for name, field in fields.items()
                 if name in ticked}
        self._deviation = None
        self._deviationState = dict(
            fields=dict(fields), starts=dict(starts),
            source=source, signed=bool(signed), universe=universe,
            ticked=set(ticked), notice=notice,
            tolerance=0.0 if tolerance is None else abs(float(tolerance)),
            scale_user=scale, **(state or {}))
        # Whatever is not shown goes back to its plain colour.
        for name, _has, _why in universe:
            if name in shown:
                continue
            info = self._actorInfos.get(name)
            if info is not None and info.faceScalarName() is not None:
                info.clearFaceScalars()
                info.resetColor()
        values = [field[1] for field in shown.values()]
        autoScale = live_distance.absolute_scale(values)
        scale = float(scale) if scale else autoScale
        if not np.isfinite(scale) or scale <= 0.0:
            scale = autoScale
        if not shown or live_distance._joined(values).size == 0:
            if keep_empty and universe:
                self._deviation = live_distance.DeviationReadout(
                    fields={}, value_range=live_distance.scale_range(
                        scale, signed),
                    lookup_table=live_distance.deviation_lut(
                        scale, self._deviationState['tolerance'], signed),
                    source=source, signed=bool(signed), scale=scale,
                    tolerance=self._deviationState['tolerance'],
                    notice=notice, surfaces=MeshManager._deviationSurfaceRows(self))
            self.applyToDisplay()
            return 0
        tolerance = self._deviationState['tolerance']
        valueRange = live_distance.scale_range(scale, signed)
        lut = live_distance.deviation_lut(scale, tolerance, signed)
        painted = {}
        for name, (face_ids, field) in shown.items():
            actorInfo = self._actorInfos[name]
            if actorInfo.setFaceScalars(
                    'deviation', face_ids, np.abs(np.asarray(
                        field, dtype=np.float64)),
                    start_face=starts.get(name), value_range=valueRange,
                    lookup_table=lut, magnitude=True):
                painted[name] = actorInfo.faceScalars('deviation')
        if painted or keep_empty:
            self._deviation = live_distance.DeviationReadout(
                fields=painted, value_range=valueRange, lookup_table=lut,
                source=source, signed=bool(signed), scale=scale,
                tolerance=tolerance, notice=notice,
                surfaces=MeshManager._deviationSurfaceRows(self),
                signed_fields={name: np.asarray(shown[name][1],
                                                dtype=np.float64)
                               for name in painted})
        self.applyToDisplay()
        return len(painted)

    def _deviationSurfaceRows(self) -> tuple:
        """``[(name, ticked, has_reference, why_not)]`` for the Surfaces list."""
        state = getattr(self, '_deviationState', None) or {}
        ticked = state.get('ticked', set())
        return tuple((name, name in ticked, bool(has), why)
                     for name, has, why in state.get('universe', ()))

    def deviationReadout(self):
        """DP-713. What the deviation colouring on screen shows, or None."""
        return getattr(self, '_deviation', None)

    def setDeviationTolerance(self, tolerance) -> bool:
        """Plan 39 VI6 (F4a). Move the neutral band; nothing is measured again.

        The scale's table is refilled in place, so the faces on screen and
        the legend that share it repaint together.
        """
        readout = getattr(self, '_deviation', None)
        state = getattr(self, '_deviationState', None)
        if readout is None or state is None:
            return False
        tolerance = abs(float(tolerance))
        state['tolerance'] = tolerance
        readout.tolerance = tolerance
        live_distance.fill_deviation_lut(
            readout.lookup_table, readout.scale, tolerance, readout.signed)
        self.applyToDisplay()
        return True

    def setDeviationScale(self, scale=None) -> bool:
        """Plan 39 VI6. Set the distance the ramp reaches; ``None`` = automatic."""
        state = getattr(self, '_deviationState', None)
        if state is None or getattr(self, '_deviation', None) is None:
            return False
        self._paintDeviation(
            state['fields'], state['starts'], state['source'],
            signed=state['signed'], tolerance=state['tolerance'],
            scale=scale or None, universe=state['universe'],
            ticked=state['ticked'], notice=state['notice'],
            state={key: state[key] for key in ('prepared', 'unit', 'live')
                   if key in state}, keep_empty=True)
        return True

    def setDeviationSurface(self, name, ticked: bool) -> bool:
        """Plan 39 VI6 (Q2). Tick or untick one patch of the Surfaces list.

        Ticking measures only that patch (when it has not been) and repaints;
        the rest of the picture is not measured again.
        """
        state = getattr(self, '_deviationState', None)
        if state is None or name not in [row[0] for row in state['universe']]:
            return False
        shown = set(state['ticked'])
        if ticked:
            shown.add(name)
            if name not in state['fields']:
                prepared = state.get('prepared')
                info = self._actorInfos.get(name)
                if prepared is None or info is None or info.dataSet() is None:
                    return False
                distances, _p = live_distance.measure_surfaces_mm(
                    prepared, {name: info.dataSet()}, state.get('unit'))
                values = distances[name]
                state['fields'][name] = (
                    np.arange(values.size, dtype=np.int64), values)
        else:
            shown.discard(name)
        self._paintDeviation(
            state['fields'], state['starts'], state['source'],
            signed=state['signed'], tolerance=state['tolerance'],
            scale=state.get('scale_user'), universe=state['universe'],
            ticked=shown, notice=state['notice'],
            state={key: state[key] for key in ('prepared', 'unit', 'live')
                   if key in state}, keep_empty=True)
        return True

    def _boundaryPatches(self) -> dict:
        """``actor id -> BoundaryPatch`` of every patch on screen.

        Read from the boundary file of the mesh the viewport drew -- the
        time's own polyMesh when it has one -- and empty for a native mesh or
        one that cannot be read.
        """
        from foammesh.core.mesh.poly_mesh_boundary import _read_boundary

        if self._root is None:
            return {}
        found: dict = {}
        bases = [self._root / str(self._time)] if self._time else []
        bases.append(self._root / 'constant')
        for rname in (self._regionIds or {'': []}):
            prefix = f'{rname}:' if rname else ''
            for base in bases:
                mesh = (base / rname / 'polyMesh') if rname else (
                    base / 'polyMesh')
                if not mesh.is_dir():
                    continue
                try:
                    patches = _read_boundary(mesh)
                except Exception:                             # noqa: BLE001
                    logger.debug('no readable boundary in %s', mesh,
                                 exc_info=True)
                    continue
                for patch in patches:
                    found[f'{prefix}{patch.name}'] = patch
                break
        return found

    def patchStartFaces(self) -> dict[str, int]:
        """DP-713. ``actor id -> startFace`` of every patch on screen.

        A face label is only a cell of its patch's actor once the patch's
        first label is known. Empty for a native mesh or one that cannot be
        read, which leaves only a field with one value per face to paint.
        """
        if self._nativePath is not None:
            # Plan 41 GN23. A native mesh has no boundary file: a patch's
            # first label is the count of the faces drawn before it.
            starts, running = {}, 0
            for name, count in self._nativePatches.items():
                starts[name] = running
                running += int(count)
            return starts
        return {name: int(patch.start_face)
                for name, patch in self._boundaryPatches().items()}

    def patchTypes(self) -> dict[str, str]:
        """Plan 39 VI6. ``actor id -> boundary type`` of every patch on screen.

        Plan 42 SG2. A native mesh has no boundary file; its patches take the
        type their group's role says (the role the Gmsh route publishes them
        with): a far field, inlet or outlet is a plain ``patch``, a symmetry
        plane ``symmetry``, a wall ``wall``, and a group that announces
        nothing has no type, so the measuring code still asks its name.
        """
        if self.nativePath() is not None:
            from foammesh.core.gmsh.layer_targets import boundary_category

            types = {}
            for name in self._patchIds:
                category = boundary_category(name.split(':', 1)[-1])
                types[name] = _NATIVE_TYPE_OF_CATEGORY.get(category, '')
            return types
        return {name: str(patch.patch_type)
                for name, patch in self._boundaryPatches().items()}

    def nativeScaleToMetres(self) -> float:
        """Plan 42 SG2. Metres in one coordinate unit of the native mesh on
        screen: what the session recorded beside it at hand-over
        (``mesh.names.json``), else 1.0 -- the mesh the product's own Gmsh
        run writes is in metres. 1.0 for a mesh read from a case."""
        if self._nativePath is None:
            return 1.0
        import json
        try:
            text = (Path(self._nativePath).parent
                    / 'mesh.names.json').read_text(encoding='utf-8')
            value = float(json.loads(text).get('metres_per_unit'))
        except (OSError, ValueError, TypeError):
            return 1.0
        return value if value > 0.0 else 1.0

    def clearFidelityColouring(self):
        """Put every patch back on its palette colour and drop the field."""
        for actorInfo in self._actorInfos.values():
            actorInfo.clearFaceScalars()
            actorInfo.resetColor()
        self._deviation = None
        self._deviationState = None
        self._layersGrown = None
        self.applyToDisplay()

    # -- Plan 39 VI16 (I6): Colour mesh by > Layers grown ------------------- #

    def showLayersGrown(self, rows) -> int:
        """Paint each patch with the layers the run achieved on it.

        *rows* are the stored layer report's rows. A patch that asked for
        layers takes the count it got on one table (0 to the largest request);
        a patch that asked for none, or that the report does not name, is
        grey. Returns how many patches were painted by count.
        """
        if (getattr(self, '_layersGrown', None) is not None
                or getattr(self, '_deviation', None) is not None):
            self.clearFidelityColouring()
        painted = layers_grown.plan(rows, self._patchIds)
        high = layers_grown.top(painted)
        table = layers_grown.lookup_table(high)
        grey = QColor.fromRgbF(*layers_grown.GREY)
        counted = 0
        for actorId, paint in painted.items():
            actor = self._actorInfos.get(actorId)
            if actor is None:
                continue
            actor.clearFaceScalars()
            data = actor.dataSet()
            cells = data.GetNumberOfCells() if data is not None else 0
            if paint.achieved is None or cells <= 0:
                actor.setColor(grey)
                continue
            if actor.setFaceScalars(
                    layers_grown.LEGEND_KEY, None,
                    np.full(cells, paint.achieved, dtype=np.float64),
                    value_range=(0.0, high), lookup_table=table):
                counted += 1
        self._layersGrown = {
            'table': table, 'range': (0.0, high),
            'patches': {a: p.achieved for a, p in painted.items()}}
        self.applyToDisplay()
        return counted

    def layersGrownReadout(self):
        """``{'table', 'range', 'patches'}`` of the colouring on screen, or None."""
        return getattr(self, '_layersGrown', None)

    def clearLayersGrown(self):
        if getattr(self, '_layersGrown', None) is not None:
            self.clearFidelityColouring()

    # -- Plan 39 VI16 (I6): Colour mesh by, on the volume's exterior -------- #

    def _volumeActors(self) -> list:
        return [info for info in self._actorInfos.values()
                if isinstance(info, MeshActor)]

    async def colourVolumeBy(self, key: str):
        """Paint the loaded volume's exterior by a *Colour cut by* choice.

        Cell type, refinement level, region / zone and the four quality
        metrics, on the same mapping, table and quality band the section
        uses. Returns ``(mapped, reason)``: *mapped* is the legend's mapping
        (``{'kind': 'none'}`` when nothing was painted) and *reason* says why
        not.
        """
        from foammesh.core.section import section_colour
        from foammesh.rendering.section_colour_map import (
            MapperColouring, cellArray, stampCellIdentity)
        from foammesh.view.display_control import section_case_fields as fields

        none = {'kind': 'none'}
        actors = self._volumeActors()
        if not actors:
            return none, 'No volume mesh is loaded.'
        item = section_colour.choice(key)
        names = None
        if item.key == section_colour.CELL_TYPE:
            for actor in actors:
                stampCellIdentity(actor.dataSet())
        elif fields.is_case_field(item.key):
            root = self.caseRoot()
            if not root:
                return none, ('{0} is read from the polyMesh of an OpenFOAM '
                              'case; this mesh has none.').format(item.label)
            meshDir, why = fields.mesh_directory(str(root))
            if meshDir is None or not fields.has_member(meshDir, item.key):
                return none, (why or fields.absent_reason(item.key))
            revision = fields.mesh_revision(meshDir)
            field = await asyncio.to_thread(
                fields.read_case_field, meshDir, item.key)
            if field.status != fields.READ:
                return none, field.reason
            if not any(fields.attach(actor.dataSet(), field)
                       for actor in actors):
                return none, ('{0} is read for the whole mesh, and none of '
                              'the cells on screen are the whole mesh.'
                              ).format(item.label)
            if item.key == section_colour.CELL_ZONE:
                names = field.names
        elif item.key.startswith(section_colour.QUALITY_PREFIX):
            state = await self.ensureQualityFields()
            if state != 'ready':
                return none, ('The quality fields could not be computed '
                              '({0}).').format(state)
        else:
            return none, 'There is no such colouring.'
        values = [v for v in (cellArray(actor.dataSet(), item.array)
                              for actor in actors) if v is not None]
        if not values:
            return none, 'The mesh has no {0} values.'.format(item.label)
        mapped = section_colour.mapping(
            item.key, np.concatenate(values), names=names)
        colouring = getattr(self, '_exteriorColouring', None)
        if colouring is None:
            colouring = self._exteriorColouring = MapperColouring()
        painted = sum(1 for actor in actors
                      if actor.paintExterior(mapped, colouring))
        if not painted:
            return none, ('A cut or filter is on the volume; colour it with '
                          'Colour cut by in the section.')
        self.applyToDisplay()
        return mapped, ''

    def clearVolumeColour(self) -> bool:
        """Put the volume's exterior back on its plain colour."""
        cleared = False
        for actor in self._volumeActors():
            clear = getattr(actor, 'clearExteriorPaint', None)
            cleared = (clear is not None and clear()) or cleared
        if cleared:
            self.applyToDisplay()
            self.volumeColourDropped.emit()
        return cleared

    def volumeColoured(self) -> bool:
        return any(getattr(actor, 'exteriorPainted', lambda: False)()
                   for actor in self._volumeActors())

    def clearFailedCells(self):
        self.remove('failedCells')
        self._restoreAfterFailedCells()
        self.applyToDisplay()

    def rethemeFailedCells(self):
        overlay = self._actorInfos.get('failedCells')
        if not isinstance(overlay, MeshActor):
            return
        error_colour = '#ef5350'
        if app.themeManager is not None and app.themeManager.tokens is not None:
            error_colour = app.themeManager.tokens.value('status.error')
        overlay.setColor(QColor(error_colour))
        self.applyToDisplay()

    def applyCellFilter(self):
        for actorInfo in self._actorInfos.values():
            actorInfo.applyCellFilter()
        self._qualityFilterOn = True
        # DP-2725. The volume now shows only the poor cells, so a fade put
        # on it for the failed cells would hide them.
        self._restoreFadedVolume()
        self._seeThroughSurfaces()

        self._notifyCellCountChange()

    def add(self, actorInfo):
        super().add(actorInfo)
        shown = self._actorInfos.get(actorInfo.id())
        busy = getattr(shown, 'cutBusy', None)
        if isinstance(shown, MeshActor) and busy is not None:
            busy.connect(self._volumeCutBusy,
                         Qt.ConnectionType.UniqueConnection)

    def _volumeCutBusy(self, busy):
        if not busy:
            # The count comes from the output the filters finished.
            self._notifyCellCountChange()
        self.volumeCutBusy.emit(bool(busy))

    def isVolumeCutting(self) -> bool:
        """Whether a cut or band over the volume is still being worked out."""
        return any(isinstance(info, MeshActor) and info.isCutting()
                   for info in self._actorInfos.values())

    def cancelVolumeCut(self) -> bool:
        """Stop the cut being worked out; the picture on screen stays."""
        stopped = False
        for info in list(self._actorInfos.values()):
            if isinstance(info, MeshActor):
                stopped = info.cancelCut() or stopped
        return stopped

    def clip(self, planes):
        self.clearVolumeColour()
        self._sectioned = bool(planes)
        super().clip(planes)
        self._notifyCellCountChange()

    def slice(self, plane):
        self.clearVolumeColour()
        self._sectioned = bool(plane)
        super().slice(plane)
        self._notifyCellCountChange()

    def cellCountCause(self) -> str:
        """Plan 45 V5. What takes cells off the screen: ``'section'`` (a cut
        plane), ``'filter'`` (the quality filter showing only its band),
        ``'both'``, or ``''`` for neither."""
        sectioned = bool(getattr(self, '_sectioned', False))
        filtered = bool(getattr(self, '_qualityFilterOn', False))
        if sectioned and filtered:
            return 'both'
        return 'section' if sectioned else ('filter' if filtered else '')

    def _notifyCellCountChange(self):
        shown = self.getNumberOfDisplayedCells()
        # A total below the shown count means the volume could not be asked;
        # the readout then reads as an uncut mesh, which is what it looks like
        # from here.
        self.cellCountChanged.emit(shown, max(self.getNumberOfCells(), shown))

    def _show(self):
        super()._show()
        self._notifyCellCountChange()
