#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import functools
import logging
from pathlib import Path

import numpy as np
import qasync
from PySide6.QtCore import Signal

from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.core.mesh.msh_scene import MshSceneError, read_msh_scene
from foammesh.core.mesh.presentation import (
    ENCLOSURE_OPACITY, VOLUME_DEPTH_BIAS, artifact_kind, display_mode_name,
    enclosing_part_ids, hides_geometry, summary_text)
from foammesh.core.mesh.mesh_preview import estimated_cells
from foammesh.core.mesh.poly_mesh_reader import MeshPreviewLoader
from foammesh.core.run_result import GMSH_MSH, GMSH_SURFACE_MSH, POLY_MESH
from foammesh.rendering.actor_info import ActorInfo, BoundaryActor, DisplayMode, MeshActor, MeshQualityIndex
from foammesh.rendering.actor_info import ActorType
from foammesh.view.main_window.actor_manager import ActorManager
from foammesh.core.quality import extract_selected_cells
from PySide6.QtGui import QColor
from foammesh.view.facade_client import query_async
from foammesh.core.quality.geometry_fidelity import live_distance
from foammesh.support.colormap import deviationLut
from foammesh.support import gc_policy


logger = logging.getLogger(__name__)


#: DP-714. How opaque the surfaces are while the poor cells are coloured:
#: enough to keep the model's shape, little enough to see the cells inside.
QUALITY_SEE_THROUGH_OPACITY = 0.3


def _closeProgress(dialog):
    """Close a load's progress dialog and let Qt delete it (Plan 35 F16).

    Every load made a parented dialog and only closed it, so each one lived
    on as a hidden child of the main window: one leaked per mesh load.
    """
    dialog.close()
    deleteLater = getattr(dialog, 'deleteLater', None)
    if callable(deleteLater):
        deleteLater()


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
    gc_policy.collect_full('scene swap')


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
        return all(self.hasScalar(index) for index in MeshQualityIndex)

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

        progressDialog = ProgressDialog(app.window, self.tr('Loading mesh'))
        progressDialog.setLabelText(self.tr('Loading mesh…'))
        progressDialog.open()
        try:
            # Off the event loop: the parse plus the VTK conversion is
            # seconds of pure Python on the meshes this product produces
            # (measured 0.98 s for the 141,486-cell finned_tube candidate),
            # and freezing the window for it is the R95 complaint again.
            scene = await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(read_msh_scene, self._nativePath,
                                        surface_only=self._surfaceOnly))
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
        self.clear()
        _swapScene()
        self._visibility = True
        self._buildScene(scene.vtk_mesh, hidden)
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
        self._time = time
        self._root = Path(root) if root else Path(app.facadeClient.case_root)
        self._nativePath = None
        self._surfaceOnly = False
        self._artifactId = str(artifact_id or '')
        self._stage = str(stage or '')

        progressDialog = ProgressDialog(app.window, self.tr('Loading mesh'))
        progressDialog.setLabelText(self.tr('Loading mesh…'))
        progressDialog.open()

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
        try:
            vtkMesh = await loader.loadMesh(self._time)
            preview = _previewOf(loader)
            finished = True
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
        self.clear()
        _swapScene()
        self._visibility = True
        self._setPreview(preview)
        self._buildScene(vtkMesh, hidden, precomputed=preview['precomputed'],
                         outline=preview['outline'])
        self.previewNoticeChanged.emit(self._previewNotice)

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
        """Read the volume too (up to 5 M cells / 1 GB), in the worker."""
        if self._time is None or self._nativePath is not None:
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
        """Turn one loaded mesh into actors, whichever reader produced it.

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
        # DP-714. Nor any surface faded for a colouring it no longer has.
        self._qualityFaded = {}
        patch_ids: list[str] = []
        zone_ids: list[str] = []
        region_part_ids: list[str] = []
        region_ids: dict[str, list[str]] = {}
        patch_bounds: dict[str, tuple] = {}
        volume_of: dict[str, str] = {}
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
                    # The volume's own exterior surface is the same box its
                    # boundary patches describe, so a patch that fades without
                    # it fades behind an opaque copy of itself.
                    for member in region_ids[rname]:
                        volume_of[member] = internal_id
                    region_ids[rname].append(internal_id)
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
        self._fadeEnclosures(patch_bounds, volume_of)
        for key in set(hidden) & set(self._actorInfos):
            self._actorInfos[key].setVisible(False)
        self._presentArtifact()
        self.applyToDisplay()
        self.fitDisplay()
        self._notifyCellCountChange()
        rebuild = getattr(app.window, 'rebuildOverlayParts', None)
        if rebuild is not None:
            rebuild()

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
        self.meshSummaryChanged.emit(self._summary)
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
            logger.info('failed-cell overlay refused: it belongs to %s, '
                        'the viewport is showing %s',
                        artifact_id, self._artifactId or '(nothing)')
            self.applyToDisplay()
            return False
        if not ids:
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
        self.applyToDisplay()
        return True

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

    def showFidelityHotspots(self, fields, artifact_id: str = '') -> int:
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
                np.asarray(deviations, dtype=np.float64).ravel()
                * live_distance.MODEL_TO_MM)
        return self._paintDeviation(measured, self.patchStartFaces(), 'run')

    def showLiveDeviation(self, reference) -> int:
        """DP-713. Colour each patch by its signed distance to ``reference``.

        Viewport audit 0925 F8. With no stored fidelity run the colouring had
        nothing to show, on a case whose reference geometry was loaded in
        the same window. This measures every patch face centre against that
        geometry now -- blue short of it, red past it -- on one shared range.
        """
        if reference is None:
            return 0
        surfaces = {}
        for actor_id in self._patchIds:
            actorInfo = self._actorInfos.get(actor_id)
            if (isinstance(actorInfo, BoundaryActor)
                    and actorInfo.dataSet() is not None):
                surfaces[actor_id] = actorInfo.dataSet()
        distances = live_distance.face_distances_mm(reference, surfaces)
        fields = {name: (np.arange(values.size, dtype=np.int64), values)
                  for name, values in distances.items()}
        return self._paintDeviation(
            fields, {name: 0 for name in fields}, 'live')

    def _paintDeviation(self, fields, starts, source) -> int:
        """Paint ``name -> (face labels, mm)`` on one symmetric range."""
        valueRange = live_distance.symmetric_range(
            values for _ids, values in fields.values())
        self._deviation = None
        if valueRange is None:
            self.applyToDisplay()
            return 0
        lut = deviationLut(valueRange)
        painted = {}
        for name, (face_ids, values) in fields.items():
            actorInfo = self._actorInfos[name]
            if actorInfo.setFaceScalars(
                    'deviation', face_ids, values,
                    start_face=starts.get(name), value_range=valueRange,
                    lookup_table=lut):
                painted[name] = actorInfo.faceScalars('deviation')
        if painted:
            self._deviation = live_distance.DeviationReadout(
                fields=painted, value_range=valueRange, lookup_table=lut,
                source=source)
        self.applyToDisplay()
        return len(painted)

    def deviationReadout(self):
        """DP-713. What the deviation colouring on screen shows, or None."""
        return getattr(self, '_deviation', None)

    def patchStartFaces(self) -> dict[str, int]:
        """DP-713. ``actor id -> startFace`` of every patch on screen.

        A face label is only a cell of its patch's actor once the patch's
        first label is known. Read from the boundary file of the mesh the
        viewport drew -- the time's own polyMesh when it has one -- and empty
        for a native mesh or one that cannot be read, which leaves only a
        field with one value per face to paint.
        """
        from foammesh.core.mesh.poly_mesh_boundary import _read_boundary

        if self._root is None:
            return {}
        starts: dict[str, int] = {}
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
                    starts[f'{prefix}{patch.name}'] = int(patch.start_face)
                break
        return starts

    def clearFidelityColouring(self):
        """Put every patch back on its palette colour and drop the field."""
        for actorInfo in self._actorInfos.values():
            actorInfo.clearFaceScalars()
            actorInfo.resetColor()
        self._deviation = None
        self.applyToDisplay()

    def clearFailedCells(self):
        self.remove('failedCells')
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
        self._seeThroughSurfaces()

        self._notifyCellCountChange()

    def clip(self, planes):
        super().clip(planes)
        self._notifyCellCountChange()

    def slice(self, plane):
        super().slice(plane)
        self._notifyCellCountChange()

    def _notifyCellCountChange(self):
        shown = self.getNumberOfDisplayedCells()
        # A total below the shown count means the volume could not be asked;
        # the readout then reads as an uncut mesh, which is what it looks like
        # from here.
        self.cellCountChanged.emit(shown, max(self.getNumberOfCells(), shown))

    def _show(self):
        super()._show()
        self._notifyCellCountChange()
