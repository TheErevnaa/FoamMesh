"""Headless service for the authored OpenFOAM export pipeline.

The desktop supplies only a destination and optional 2-D extrusion options;
case configuration, filesystem layout, parallel settings, utility execution,
and history are owned here rather than by the view.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from foammesh.support.openfoam.constants import CASE_DIRECTORY_NAME, Directory
from foammesh.support.openfoam.polymesh import isPolyMesh, removeVoidBoundaries
from foammesh.support.utils import rmtree
from resources import resource

from foammesh.core.import_export import path_bytes, record_export_event
from foammesh.db.configurations_schema import CFDType
from foammesh.openfoam.file_system import FileSystem
from foammesh.openfoam.constant.region_properties import RegionProperties
from foammesh.core.jobs import JobRequest, JobStatus
from foammesh.openfoam.system.collapse_dict import CollapseDict
from foammesh.openfoam.system.create_patch_dict import CreatePatchDict
from foammesh.openfoam.system.extrude_mesh_dict import ExtrudeMeshDict
from foammesh.openfoam.system.topo_set_dict import TopoSetDict
from foammesh.openfoam.utility.restore_cyclic_patch_names import RestoreCyclicPatchNames
from foammesh.settings.local_settings import LocalSettings


class AuthoredExportService:
    OUTPUT_TIME = 4

    def __init__(self, *, capabilities=None):
        self._capabilities = capabilities

    async def _run(self, session, utility, arguments=(), *, cwd,
                   parallel=None, on_line=None):
        if self._capabilities is None:
            raise RuntimeError(
                'authored export requires the qualified OpenFOAM runtime')
        if parallel is not None and parallel.isParallelOn():
            launch = self._capabilities.command(
                'mpirun',
                ('-np', str(parallel.np()), utility, '-parallel', *arguments),
                cwd=cwd)
        else:
            launch = self._capabilities.command(
                utility, arguments, cwd=cwd)
        job = await session.jobs.run(JobRequest(
            name=f'authored export: {utility}', argv=launch.argv, cwd=cwd,
            mutation=True,
            log_path=session.storage_path / 'logs' /
            f'authored-export-{utility}.log',
            timeout=900, cleanup_argv=launch.cleanup_argv),
            on_line=on_line)
        if job.status is not JobStatus.DONE:
            raise RuntimeError(
                f'{utility} failed during authored export: '
                f'{job.error or job.status.value}')

    def _meshSourcePath(self, file_system, processorNo=None):
        """The directory that actually holds the finished mesh.

        snappyHexMesh used to leave the mesh behind as numbered time
        directories, and the export copied the last of those into
        ``OUTPUT_TIME``. Both engines now publish to ``constant/polyMesh``
        instead. Prefer a numbered directory when one still holds a mesh so
        that a case meshed by an older build keeps exporting, and fall back to
        ``constant``.
        """
        for time in range(self.OUTPUT_TIME - 1, 0, -1):
            candidate = file_system.timePath(time, processorNo)
            if isPolyMesh(candidate / Directory.POLY_MESH_DIRECTORY_NAME):
                return candidate

        root = (file_system.caseRoot() if processorNo is None
                else file_system.processorPath(processorNo, False))
        constant = root / Directory.CONSTANT_DIRECTORY_NAME
        if isPolyMesh(constant / Directory.POLY_MESH_DIRECTORY_NAME):
            return constant

        return None

    async def _stageMesh(self, file_system, parallel):
        """Copy the finished mesh into ``OUTPUT_TIME`` for the export steps.

        Everything downstream moves ``<case>/<OUTPUT_TIME>/polyMesh`` into the
        destination, so the mesh has to be there whichever of the two layouts
        it was written in.
        """
        indices = range(parallel.np()) if parallel.isParallelOn() else (None,)
        for index in indices:
            source = self._meshSourcePath(file_system, index)
            if source is None:
                where = '' if index is None else f' for processor{index}'
                raise RuntimeError(
                    f'nothing to export{where}: no mesh was found in a time '
                    f'directory or in constant/polyMesh')

            destination = file_system.timePath(self.OUTPUT_TIME, index)
            if destination.exists():
                rmtree(destination)
            await asyncio.to_thread(
                shutil.copytree,
                source / Directory.POLY_MESH_DIRECTORY_NAME,
                destination / Directory.POLY_MESH_DIRECTORY_NAME)

    def _regionMeshRoot(self, file_system, regions):
        """Where splitMeshRegions left the per-region meshes.

        It writes to ``constant`` when the mesh it read was there and to the
        latest time directory otherwise, so neither location can be assumed.
        """
        probe = next(iter(regions.values())).value('name')
        candidates = [file_system.constantPath()]
        candidates += [file_system.timePath(time)
                       for time in range(self.OUTPUT_TIME, -1, -1)]
        for candidate in candidates:
            if isPolyMesh(candidate / probe / Directory.POLY_MESH_DIRECTORY_NAME):
                return candidate

        raise RuntimeError(
            'nothing to export: splitMeshRegions produced no region meshes')

    async def run(self, session, destination: str | Path, *, boundaries=(),
                  options=None, on_line=None, on_progress=None) -> dict:
        destination = Path(destination)
        db = session.state.db
        case_root = (session.case_path if (session.case_path / 'constant').is_dir()
                     else session.case_path / CASE_DIRECTORY_NAME)
        file_system = FileSystem(session.case_path, case_root=case_root)
        parallel = LocalSettings(session.storage_path).parallelEnvironment()
        progress = on_progress or (lambda _message: None)

        regions = db.getElements('region')
        if len(regions) > 1:
            progress('Splitting Mesh Regions')
            await self._run(
                session, 'splitMeshRegions', ('-cellZonesOnly',),
                cwd=case_root, parallel=parallel, on_line=on_line)
        else:
            progress('Copying Files')
            await self._stageMesh(file_system, parallel)

        topo = TopoSetDict(file_system, db).build(TopoSetDict.Mode.CREATE_CELL_ZONES)
        if topo.isBuilt():
            progress('Processing Cell Zones')
            if len(regions) == 1:
                topo.write()
                await self._run(
                    session, 'topoSet', cwd=case_root,
                    parallel=parallel, on_line=on_line)
            else:
                for region in regions.values():
                    name = region.value('name')
                    topo.setRegion(name).write()
                    await self._run(
                        session, 'topoSet', ('-region', name),
                        cwd=case_root, parallel=parallel, on_line=on_line)

        destination.mkdir(parents=True, exist_ok=True)
        exported = FileSystem(destination)
        exported.createCase(resource.file('openfoam/case'))
        if len(regions) > 1:
            RegionProperties(exported.caseRoot()).build().write()

        progress('Exporting Files')
        output_path = (self._regionMeshRoot(file_system, regions)
                       if len(regions) > 1
                       else file_system.timePath(self.OUTPUT_TIME))
        if parallel.isParallelOn():
            for index in range(parallel.np()):
                processor = exported.processorPath(index, False)
                processor.mkdir()
                shutil.move(file_system.timePath(self.OUTPUT_TIME, index),
                            processor / Directory.CONSTANT_DIRECTORY_NAME)
            progress('Reconstructing exported mesh')
            await self._run(
                session, 'reconstructPar',
                ('-constant', '-noFields', '-case', str(exported.caseRoot())),
                cwd=exported.caseRoot(), on_line=on_line)
        elif len(regions) > 1:
            for region in regions.values():
                shutil.move(output_path / region.value('name'), exported.constantPath())
        else:
            shutil.move(output_path / Directory.POLY_MESH_DIRECTORY_NAME,
                        exported.polyMeshPath())

        interfaces = db.elementCount(
            'geometry', lambda _i, element:
            element['cfdType'] == CFDType.INTERFACE.value
            and not element['interRegion'] and not element['nonConformal'])
        if interfaces:
            prefix = 'NFBRM_'
            CreatePatchDict(prefix, exported, db).build().write()
            # MEASURED on OpenFOAM 13 (`createPatch -help`, build
            # 13-58ed5c2046ef): `-overwrite  Deprecated option, this is now
            # default behaviour`. Overwriting in place is what v13 does unless
            # `-noOverwrite` asks otherwise, so the flag is dropped rather than
            # carried as a no-op that reads like a requirement.
            await self._run(
                session, 'createPatch',
                ('-allRegions', '-case', str(exported.caseRoot())),
                cwd=exported.caseRoot(), on_line=on_line)
            RestoreCyclicPatchNames(prefix, exported, db).restore()

        if options is not None:
            progress('Extruding Mesh')
            if len(regions) > 1:
                for region_name, first, second in boundaries:
                    await exported.createRegionSystemDirectory(region_name)
                    ExtrudeMeshDict(exported).build(first, second, options).write()
                    await self._run(
                        session, 'extrudeMesh',
                        ('-region', region_name, '-dict',
                         'system/extrudeMeshDict'),
                        cwd=exported.caseRoot(), on_line=on_line)
            else:
                ExtrudeMeshDict(exported).build(
                    boundaries[0][1], boundaries[0][2], options).write()
                await self._run(
                    session, 'extrudeMesh', cwd=exported.caseRoot(),
                    on_line=on_line)
                CollapseDict(exported).create()
                # Same v13 contract as createPatch above: `collapseEdges
                # -help` reports `-overwrite` as deprecated and in-place
                # overwriting as the default.
                await self._run(
                    session, 'collapseEdges', (),
                    cwd=exported.caseRoot(), on_line=on_line)

        # Only the staged time directory is ours to delete; when
        # splitMeshRegions wrote into `constant` that is the source case's own
        # directory, and the region meshes have already been moved out of it.
        if output_path == file_system.timePath(self.OUTPUT_TIME):
            rmtree(output_path)
        rmtree(exported.polyMeshPath() / 'sets')
        removeVoidBoundaries(exported.caseRoot())
        entry = record_export_event(
            session.case_path, entry_id='openfoam', destination=destination,
            total_bytes=path_bytes(destination),
            details={'entry_point': 'authored_export_step',
                     'two_dimensional': options is not None})
        return {'destination': str(destination), 'history_entry_id': entry.entry_id,
                'total_bytes': path_bytes(destination),
                'two_dimensional': options is not None}
