#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import numpy as np
from vtkmodules.vtkIOParallel import vtkPOpenFOAMReader
from vtkmodules.vtkFiltersCore import vtkFeatureEdges
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import (
    vtkCellArray, vtkCompositeDataSet, vtkPolyData)
from vtkmodules.util.numpy_support import (
    numpy_to_vtk, numpy_to_vtkIdTypeArray)
from foammesh.core.mesh.poly_mesh_boundary import (
    PolyMeshReadError, read_face_zone_surfaces)
from foammesh.core.mesh.mesh_preview import case_layout
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
from vtkmodules.vtkRenderingLOD import vtkQuadricLODActor
from vtkmodules.vtkCommonCore import vtkCommand
from foammesh.support import disposal
from foammesh.support.vtk_threads import vtk_run_in_thread
from vtkmodules.util.vtkConstants import VTK_MULTIBLOCK_DATA_SET, VTK_UNSTRUCTURED_GRID, VTK_POLY_DATA
from pathlib import Path

from foammesh.core.mesh.seeded_regions import region_grids, seeded_region_cells

from PySide6.QtCore import QObject, Signal


logger = logging.getLogger(__name__)


def getActor(dataset):
    gFilter = vtkGeometryFilter()
    gFilter.SetInputData(dataset)
    gFilter.Update()

    mapper = vtkPolyDataMapper()
    mapper.SetInputData(gFilter.GetOutput())

    actor = vtkQuadricLODActor()    # vtkActor()
    actor.SetMapper(mapper)

    return actor


def getFeatureActor(dataset):
    edges = vtkFeatureEdges()
    edges.SetInputData(dataset)
    edges.Update()

    mapper = vtkPolyDataMapper()
    mapper.SetInputData(edges.GetOutput())
    mapper.ScalarVisibilityOff()

    actor = vtkActor()
    actor.SetMapper(mapper)

    return actor


def build(mBlock):
    vtkMesh = {}
    n = mBlock.GetNumberOfBlocks()
    for i in range(0, n):
        if mBlock.HasMetaData(i):
            name = mBlock.GetMetaData(i).Get(vtkCompositeDataSet.NAME())
        else:
            name = ''
        ds = mBlock.GetBlock(i)
        dsType = ds.GetDataObjectType()
        if dsType == VTK_MULTIBLOCK_DATA_SET:
            vtkMesh[name] = build(ds)
        elif dsType == VTK_UNSTRUCTURED_GRID:
            # if ds.GetNumberOfCells() > 0:
            #     # vtkMesh[name] = ActorInfo(getActor(ds))
            #     gFilter = vtkGeometryFilter()
            #     gFilter.SetInputData(ds)
            #     gFilter.Update()
            #
            #     vtkMesh[name] = gFilter.GetOutput()
            vtkMesh[name] = ds
        elif dsType == VTK_POLY_DATA:
            # vtkMesh[name] = ActorInfo(getActor(ds), getFeatureActor(ds))
            vtkMesh[name] = ds
        else:
            vtkMesh[name] = f'Type {dsType}'  # ds

    return vtkMesh


def _faceZoneActorData(surface) -> vtkPolyData:
    """One :class:`FaceZoneSurface` as the polydata a boundary actor draws.

    DP-414. The zone arrives already compacted to its own points, so this is
    a copy of two arrays and a legacy cell array: ``[n, v0..vn-1]`` repeated,
    which is the one cell encoding every VTK build accepts.
    """
    sizes = np.diff(surface.face_offsets)
    cells = np.empty(int(sizes.sum()) + sizes.size, dtype=np.int64)
    heads = surface.face_offsets[:-1] + np.arange(sizes.size, dtype=np.int64)
    body = np.ones(cells.size, dtype=bool)
    body[heads] = False
    cells[heads] = sizes
    cells[body] = surface.face_vertices

    points = vtkPoints()
    points.SetData(numpy_to_vtk(
        np.ascontiguousarray(surface.points, dtype=np.float64), deep=True))
    array = vtkCellArray()
    array.SetCells(int(sizes.size), numpy_to_vtkIdTypeArray(
        np.ascontiguousarray(cells), deep=True))

    data = vtkPolyData()
    data.SetPoints(points)
    data.SetPolys(array)
    return data


def _joinSurfaces(parts: list):
    """One surface out of the same zone read from several ranks.

    Each rank numbers its own points, so the parts are concatenated and each
    one's vertex ids are shifted past the points already filed. Points shared
    across a processor boundary arrive once per rank that holds them, which a
    renderer does not care about and a solver never sees.
    """
    if len(parts) == 1:
        return parts[0]
    points, vertices, offsets = [], [], [np.zeros(1, dtype=np.int64)]
    shift = 0
    grown = 0
    for part in parts:
        points.append(part.points)
        vertices.append(part.face_vertices + shift)
        offsets.append(part.face_offsets[1:] + grown)
        shift += part.points.shape[0]
        grown += int(part.face_offsets[-1])
    first = parts[0]
    return type(first)(
        name=first.name, zone_type=first.zone_type,
        points=np.concatenate(points),
        face_vertices=np.concatenate(vertices),
        face_offsets=np.concatenate(offsets))


def _mergeFaceZones(zones: dict, roots) -> dict:
    """Add the face zones `vtkPOpenFOAMReader` could not read, in place.

    DP-414. ``ReadZonesOn()`` returns the cell zones correctly and no face
    zones at all, because OpenFOAM 13 writes a faceZone's ``flipMap`` as
    ``List<bool>`` with the literals ``true`` and ``false``; the reader errors
    at ``vtkOpenFOAMReader.cxx:10114`` once per processor directory per load
    and carries on, so the mesh and its cell zones draw normally and nothing on
    screen says the interface is missing. On the campaign's six zone-bearing
    models -- the multiregion meshes the conformal-interface work exists to
    test -- that is the whole of what the interface was.

    A zone the reader did manage to produce is left alone, so a case written
    in a spelling VTK understands keeps the reader's own geometry. A decomposed
    case is the union of its ranks: each rank numbers its own faces, so the
    zone is read per rank and appended, which is the same thing the reader does
    with the mesh beside it.
    """
    gathered = {}
    for root in roots:
        try:
            surfaces = read_face_zone_surfaces(root)
        except (PolyMeshReadError, OSError, ValueError) as error:
            # Drawing is best-effort: a mesh that cannot be read here is still
            # a mesh the reader drew, and refusing to show it would be worse
            # than showing it without its interfaces. Say so in the log, which
            # is the one thing this fault did not do.
            logger.warning('face zones could not be read from %s: %s',
                           root, error)
            continue
        for surface in surfaces:
            if surface.face_count == 0 or surface.name in zones:
                continue
            gathered.setdefault(surface.name, []).append(surface)
    for name, parts in gathered.items():
        zones[name] = _faceZoneActorData(_joinSurfaces(parts))
    return zones


class PolyMeshLoader(QObject):
    progress = Signal(str)

    def __init__(self, foamFile, regionSeeds=()):
        super().__init__()

        # DP-711. ``[(name, point), ...]`` from the Regions page, so a mesh
        # that holds several regions without naming them (snappy's multi-
        # region output) still reaches the viewport as named parts.
        self._regionSeeds = [(name, point) for name, point in regionSeeds]
        self._reader = vtkPOpenFOAMReader()
        self._caseDir = Path(foamFile).parent
        self._processorPath = self._caseDir / 'processor0'

        self._reader.SetFileName(foamFile)
        self._reader.EnableAllCellArrays()
        self._reader.EnableAllPointArrays()
        self._reader.EnableAllPatchArrays()
        self._reader.EnableAllLagrangianArrays()
        self._reader.CreateCellToPointOn()
        self._reader.CacheMeshOn()
        self._reader.ReadZonesOn()
        self._reader.SkipZeroTimeOff()

        self._progress_range = [0, 100]

        self._reader.AddObserver(vtkCommand.ProgressEvent, self._readerProgressed)
        disposal.track(self, 'PolyMeshLoader')

    def dispose(self):
        """Drop the reader, and with it the copy of the mesh it caches.

        Plan 35 CR3. The reader keeps the whole mesh (`CacheMeshOn`) for as
        long as it lives, and its progress observer is a bound method of this
        object, so the pair formed a cycle only the collector could free --
        a second full copy of the mesh held until it ran, on whatever thread
        it ran on. Called once the read is over; never while `loadMesh` may
        still be running on the VTK thread.
        """
        reader = self._reader
        if reader is None:
            return
        self._reader = None
        disposal.untrack(self, 'PolyMeshLoader')
        reader.RemoveAllObservers()

    def _polyMeshDirectories(self, root: Path) -> list:
        """Every polyMesh directory under one case root, in no order.

        `constant/polyMesh` holds the mesh a serial run or a `reconstructPar`
        wrote; a `<time>/polyMesh` holds one a moving-mesh run wrote later.
        """
        found = [root / 'constant' / 'polyMesh']
        found.extend(sorted(root.glob('[0-9]*/polyMesh')))
        return [path for path in found if path.is_dir()]

    def _meshWrittenAt(self, root: Path):
        """When the newest mesh under one case root was written, or None.

        A polyMesh directory is dated by its `faces` file, which is the one
        file every mesh has and which every writer rewrites.
        """
        newest = None
        for directory in self._polyMeshDirectories(root):
            for name in ('faces', 'faces.gz'):
                faces = directory / name
                if not faces.is_file():
                    continue
                written = faces.stat().st_mtime
                if newest is None or written > newest:
                    newest = written
        return newest

    def _caseType(self):
        """Which of the two meshes on disk the reader should be pointed at.

        `reconstructPar` leaves the `processor*` directories exactly where the
        parallel run left them, so `processor0` existing does not mean the
        decomposition is the thing to look at. Reading it back draws the
        decomposition rather than the mesh: the subdomain meshes are appended,
        so every face on a subdomain boundary arrives as an exterior face and
        every point on one arrives once per subdomain that touches it. On the
        nozzle of the 45db9031 sweep that is 20021 exterior polygons against
        the mesh's own 10773, 84457 points against 79317, and 5051 silhouette
        edges against 296 -- brick-work drawn across faces that are flat.

        So the reconstructed mesh wins whenever the case root holds one that is
        not older than the processor mesh. The decomposition is read only when
        it is the only mesh there is, or when it is the newer of the two and so
        is what the last run actually produced.
        """
        # Plan 35 CR3: the rule lives in ``mesh_preview.case_layout``, which
        # the window also asks before it starts a preview worker.
        if case_layout(self._caseDir) == 'decomposed':
            return vtkPOpenFOAMReader.DECOMPOSED_CASE
        return vtkPOpenFOAMReader.RECONSTRUCTED_CASE

    def hasMesh(self) -> bool:
        """Whether there is a polyMesh for the reader to open at all."""
        for root in (self._caseDir, self._processorPath):
            if self._polyMeshDirectories(root):
                return True
        return False

    async def loadMesh(self, time):
        if not self.hasMesh():
            # The reader raises a VTK error window for a case without a mesh.
            # A case that has not been meshed yet is not an error.
            return None
        self._reader.SetCaseType(self._caseType())

        self._reader.UpdateInformation()
        self._reader.SetTimeValue(time)

        if self._reader.GetTimeValue() != time:
            return None

        self._reader.Modified()

        self._progress_range = [0, 50]
        # Be careful!
        # This should  be protected by modal dialog to prohibit users from interacting with rendering window
        # Only one VTK can be allowed to keep integrity
        await vtk_run_in_thread(self._reader.Update)
        self._progress_range = [50, 100]
        return await vtk_run_in_thread(
            self._getVtkMesh, self._buildPatchArrayStatus())

    def readNow(self, time, *, boundaryOnly=False):
        """The same scene as :meth:`loadMesh`, read on the calling thread.

        Plan 35 CR3. Only the preview worker calls this, in its own process:
        the window no longer runs the reader. ``boundaryOnly`` leaves the
        volume and the cell zones unread -- what the default surface preview
        needs of a mesh the bounded reader cannot read.
        """
        if not self.hasMesh():
            return None
        self._reader.SetCaseType(self._caseType())
        self._reader.UpdateInformation()
        self._reader.SetTimeValue(time)
        if self._reader.GetTimeValue() != time:
            return None
        if boundaryOnly:
            self._reader.ReadZonesOff()
        self._reader.Modified()
        self._reader.Update()
        status = self._buildPatchArrayStatus()
        if boundaryOnly:
            status = {name: 0 if name.endswith('internalMesh') else value
                      for name, value in status.items()}
        return self._getVtkMesh(status)

    def _buildPatchArrayStatus(self):
        #
        # for i in range(self._reader.GetNumberOfCellArrays()):
        #     name = self._reader.GetCellArrayName(i)
        #     status = self._reader.GetCellArrayStatus(name)
        #     print(f'CellArray {name} : {status}')

        statusConfig = {}
        for i in range(self._reader.GetNumberOfPatchArrays()):
            name = self._reader.GetPatchArrayName(i)
            statusConfig[name] = 1

        statusConfig['internalMesh'] = 1

        return statusConfig

    def _getVtkMesh(self, statusConfig: dict):
        """
        VtkMesh dict
        {
            <region> : {
                "boundary" : {
                    <boundary> : <PolyData>
                    ...
                },
                "internalMesh" : <ActorInfo>,
                "zones" : {
                    "cellZones" : {
                        <cellZone> : <PolyData>,
                        ...
                    }
                }
            },
            ...
        }
        """

        if statusConfig:
            for patchName, status in statusConfig.items():
                self._reader.SetPatchArrayStatus(patchName, status)

        for i in range(self._reader.GetNumberOfCellArrays()):
            name = self._reader.GetCellArrayName(i)
            self._reader.SetCellArrayStatus(name, 1)

        for i in range(self._reader.GetNumberOfPointArrays()):
            name = self._reader.GetPointArrayName(i)
            self._reader.SetPointArrayStatus(name, 1)

        self._reader.Update()

        vtkMesh = build(self._reader.GetOutput())

        if 'boundary' in vtkMesh:  # single region mesh
            vtkMesh = {'': vtkMesh}

        self._addUnreadFaceZones(vtkMesh)
        self._addSeededRegions(vtkMesh)

        return vtkMesh

    def _addSeededRegions(self, vtkMesh: dict) -> None:
        """DP-711. Name the regions of a mesh that holds them unnamed.

        Viewport audit 0925 F1, case S5: a fluid-and-solid snappy mesh is one
        ``internalMesh`` with no cell zones, so "will I be able to select
        which region to visualize" had nothing to select. The export already
        splits such a mesh into the connected piece around each region point
        (``region_zones``); the same split, read from the same files, becomes
        a ``regions`` group of volume parts here, so the picture and the
        exported case agree about which cells are the fluid.

        Only for a single-region reconstructed read: a decomposed read
        numbers its cells rank by rank, not in polyMesh order.
        """
        if (len(self._regionSeeds) < 2 or list(vtkMesh) != ['']
                or self._caseType() != vtkPOpenFOAMReader.RECONSTRUCTED_CASE):
            return
        region = vtkMesh['']
        internal = region.get('internalMesh')
        if internal is None:
            return
        zones = region.setdefault('zones', {})
        if not isinstance(zones, dict) or zones.get('cellZones'):
            return
        try:
            cells = seeded_region_cells(
                self._caseDir, self._regionSeeds,
                expected_cells=internal.GetNumberOfCells())
            if cells:
                zones['regions'] = region_grids(internal, cells)
        except Exception:                                    # noqa: BLE001
            # Best-effort, like the face zones above: the whole mesh still
            # draws, it just is not split.
            logger.warning('regions could not be split for the viewport',
                           exc_info=True)

    def _faceZoneRoots(self) -> list:
        """The case roots whose faceZones make up the mesh now on screen.

        The same choice `_caseType` made, read the same way: a decomposed case
        is the union of every rank, a reconstructed one is the case root.
        """
        if self._caseType() != vtkPOpenFOAMReader.DECOMPOSED_CASE:
            return [self._caseDir]
        return sorted(
            path for path in self._caseDir.glob('processor[0-9]*')
            if path.is_dir())

    def _addUnreadFaceZones(self, vtkMesh: dict) -> None:
        """DP-414. Fill in the face zones the VTK reader silently dropped."""
        roots = self._faceZoneRoots()
        if not roots:
            return
        for region in vtkMesh.values():
            if not isinstance(region, dict):
                continue
            zones = region.setdefault('zones', {})
            if not isinstance(zones, dict):
                continue
            _mergeFaceZones(zones.setdefault('faceZones', {}), roots)

    def _readerProgressed(self, caller: vtkPOpenFOAMReader, ev):
        percent = int(self._progress_range[0] + (float(caller.GetProgress())
                      * (self._progress_range[1] - self._progress_range[0])))
        self.progress.emit(self.tr('Loading mesh… {0}%').format(percent))
