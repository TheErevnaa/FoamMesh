#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
from pathlib import Path

import qasync
from PySide6.QtCore import Signal

from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.core.mesh.msh_scene import MshSceneError, read_msh_scene
from foammesh.core.mesh.poly_mesh_reader import PolyMeshLoader
from foammesh.core.run_result import GMSH_MSH, POLY_MESH
from foammesh.rendering.actor_info import ActorInfo, BoundaryActor, DisplayMode, MeshActor, MeshQualityIndex
from foammesh.view.main_window.actor_manager import ActorManager
from foammesh.core.quality import extract_selected_cells
from PySide6.QtGui import QColor
from foammesh.view.facade_client import query


logger = logging.getLogger(__name__)


class MeshManager(ActorManager):
    #: (visible, total). F-44: the readout changed under a section with no
    #: way to tell a clipped mesh from a smaller one, so both numbers travel
    #: together and the label says which is which.
    cellCountChanged = Signal(int, int)

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
        self._regionIds: dict[str, list[str]] = {}
        self._patchIds: list[str] = []
        self._zoneIds: list[str] = []

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
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: query(
                    app.facadeClient, 'quality.cell_fields',
                    {'include_values': True}))
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
            (MeshActor, BoundaryActor), 'zone', keys=set(self._zoneIds))

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
        if not fmt:
            return 'this run left no artifact to read'
        return f'no reader for {fmt}'

    async def loadNative(self, path, artifact_id: str = '') -> str:
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
        self.clear()
        self._visibility = True
        self._time = None
        self._root = None
        self._loader = None
        self._nativePath = Path(path)
        self._artifactId = str(artifact_id or '')

        progressDialog = ProgressDialog(app.window, self.tr('Loading Mesh'))
        progressDialog.setLabelText(self.tr('Loading Mesh'))
        progressDialog.open()
        try:
            # Off the event loop: the parse plus the VTK conversion is
            # seconds of pure Python on the meshes this product produces
            # (measured 0.98 s for the 141,486-cell finned_tube candidate),
            # and freezing the window for it is the R95 complaint again.
            scene = await asyncio.get_running_loop().run_in_executor(
                None, read_msh_scene, self._nativePath)
        except MshSceneError as error:
            self._nativePath = None
            self._artifactId = ''
            self.cellCountChanged.emit(0, 0)
            return str(error)
        except Exception as error:                                 # noqa: BLE001
            logger.debug('native mesh read failed', exc_info=True)
            self._nativePath = None
            self._artifactId = ''
            self.cellCountChanged.emit(0, 0)
            return f'{Path(path).name}: {error}'
        finally:
            progressDialog.close()

        if generation != self._load_generation:
            return ''
        self._buildScene(scene.vtk_mesh, hidden)
        return ''

    async def load(self, time: int, root=None, artifact_id: str = ''):
        """Draw time ``time`` of the case at ``root`` (the project by default).

        ``root`` is a case directory -- the thing that holds ``constant/
        polyMesh`` -- not the polyMesh itself, because that is what the reader
        opens. It exists so a run's own result can be drawn without publishing
        it to the project first (F-37).
        """
        assert time >= 0

        if not self._displayControl.isEnabled():
            # Rendering is switched off. Nothing to draw into -- but record
            # it, because this return is otherwise indistinguishable from a
            # mesh that failed to read (R95/R156).
            logger.info('mesh load skipped: rendering is disabled')
            return

        # Deliberately no gather here. The reader selects DECOMPOSED_CASE when
        # `processor0` exists, so a decomposed mesh draws directly from the
        # processor cases. Reconstructing to display was costing a full gather
        # after every stage for a picture the viewer could already read.
        self._load_generation += 1
        generation = self._load_generation
        # R27. Carry the user's per-part visibility across the reload. Only
        # actors that come back are restored, so a mesh with different patches
        # starts from its own defaults rather than inheriting a hidden row
        # that no longer means anything.
        hidden = {key for key, info in self._actorInfos.items()
                  if not info.isVisible()}
        self.clear()
        self._visibility = True

        self._time = time
        self._root = Path(root) if root else Path(app.facadeClient.case_root)
        self._nativePath = None
        self._artifactId = str(artifact_id or '')

        progressDialog = ProgressDialog(app.window, self.tr('Loading Mesh'))
        progressDialog.setLabelText(self.tr('Loading Mesh'))
        progressDialog.open()

        self._loader = PolyMeshLoader(self._root / 'case.foam')
        self._loader.progress.connect(progressDialog.setLabelText)

        try:
            vtkMesh = await self._loader.loadMesh(self._time)
            if generation != self._load_generation:
                return
            self._buildScene(vtkMesh, hidden)
        finally:
            progressDialog.close()

    def _buildScene(self, vtkMesh, hidden=frozenset()):
        """Turn one loaded mesh into actors, whichever reader produced it.

        The polyMesh reader and the native ``.msh`` reader hand over the same
        shape -- region -> boundary/internalMesh/zones -- so both meshes reach
        the same actors, the same tree, the same palettes and the same
        selection. A native Gmsh mesh's physical groups arrive as the boundary
        entries here, which is what makes its patches pickable and hideable
        independently of the volume.
        """
        patch_ids: list[str] = []
        zone_ids: list[str] = []
        region_ids: dict[str, list[str]] = {}
        if vtkMesh:
            multi_region = len(vtkMesh) > 1
            for rname, region in vtkMesh.items():
                region_ids.setdefault(rname, [])
                prefix = f'{rname}:' if rname else ''
                for bname, polyData in region['boundary'].items():
                    actor_id = f'{prefix}{bname}'
                    display = f'{rname}/{bname}' if rname else bname
                    self.add(BoundaryActor(polyData, actor_id, display))
                    patch_ids.append(actor_id)
                    region_ids[rname].append(actor_id)
                internal_id = (
                    f'{prefix}internalMesh' if multi_region
                    else 'internalMesh')
                display = (
                    f'{rname}/internalMesh' if rname else 'internalMesh')
                self.add(MeshActor(
                    region['internalMesh'], internal_id, display))
                region_ids[rname].append(internal_id)
                zones = region.get('zones', region)
                for category in ('cellZones', 'faceZones'):
                    collection = zones.get(category, {})
                    if not isinstance(collection, dict):
                        continue
                    for zone_name, data_set in collection.items():
                        actor_id = f'{prefix}{category}:{zone_name}'
                        display = (
                            f'{rname}/{category}/{zone_name}' if rname
                            else f'{category}/{zone_name}')
                        actor_type = (
                            MeshActor if category == 'cellZones'
                            else BoundaryActor)
                        self.add(actor_type(data_set, actor_id, display))
                        zone_ids.append(actor_id)
                        region_ids[rname].append(actor_id)
        self._regionIds = region_ids
        self._patchIds = patch_ids
        self._zoneIds = zone_ids
        self._assignPalettes()
        for key in set(hidden) & set(self._actorInfos):
            self._actorInfos[key].setVisible(False)
        self.applyToDisplay()
        self.fitDisplay()
        self._notifyCellCountChange()
        rebuild = getattr(app.window, 'rebuildOverlayParts', None)
        if rebuild is not None:
            rebuild()

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
                                  artifact_id=self._artifactId)
            return
        if self._time is None:
            return

        await self.load(self._time, root=self._root,
                        artifact_id=self._artifactId)

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

        return 0

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

        return 0

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

        self._notifyCellCountChange()

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
        coloured = 0
        for name, field in (fields or {}).items():
            actorInfo = self._actorInfos.get(name)
            if actorInfo is None:
                continue
            try:
                face_ids, deviations = field
            except (TypeError, ValueError):
                continue
            if actorInfo.setFaceScalars('deviation', face_ids, deviations):
                coloured += 1

        self.applyToDisplay()
        return coloured

    def clearFidelityColouring(self):
        """Put every patch back on its palette colour and drop the field."""
        for actorInfo in self._actorInfos.values():
            actorInfo.clearFaceScalars()
            actorInfo.resetColor()
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
