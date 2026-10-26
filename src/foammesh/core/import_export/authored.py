"""Headless service for the authored OpenFOAM export pipeline.

The desktop supplies only a destination and optional 2-D extrusion options;
case configuration, filesystem layout, parallel settings, utility execution,
and history are owned here rather than by the view.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import shutil
from pathlib import Path

from foammesh.support.openfoam.constants import CASE_DIRECTORY_NAME, Directory
from foammesh.support.openfoam.polymesh import isPolyMesh, removeVoidBoundaries
from foammesh.support.utils import rmtree
from resources import resource

from foammesh.core.import_export import path_bytes, record_export_event
from foammesh.core.import_export import couplings as couplings_module
from foammesh.core.import_export.handoff import write_openfoam_handoff
from foammesh.core.import_export.region_zones import (
    CELL_ZONES, ORIGINAL_CELL_ZONES, RegionZoneError, decomposed_region_plan,
    prepare_region_zones, region_zones_dict, require_solid_zones,
    restore_cell_zones)
from foammesh.core.import_export.export_regions import (
    are_solids, export_regions)
from foammesh.core.run_result import read_failure_cause
from foammesh.db.configurations_schema import CFDType, RegionType
from foammesh.openfoam.file_system import FileSystem
from foammesh.openfoam.constant.region_properties import RegionProperties
from foammesh.core.jobs import JobRequest, JobStatus
from foammesh.core.openfoam_runtime import LaunchProfileError
from foammesh.openfoam.system.collapse_dict import CollapseDict
from foammesh.openfoam.system.extrude_mesh_dict import ExtrudeMeshDict
from foammesh.openfoam.system.create_zones_dict import CreateZonesDict, surfaceFileName
from foammesh.settings.local_settings import LocalSettings
from foammesh.core.gmsh.manifest import accepted_run_layout
from foammesh.openfoam import decomposition


@dataclass(frozen=True)
class _ExportLayout:
    """The processor layout one export has to reproduce.

    Shaped like the legacy parallel environment -- ``isParallelOn`` and
    ``np`` -- so the staging step and the launch helper below are unchanged by
    where the answer now comes from.
    """

    cores: int
    run_id: str = ''
    source: str = 'legacy-parallel-dialog'

    def np(self) -> int:
        return max(1, int(self.cores))

    def isParallelOn(self) -> bool:
        return int(self.cores) > 1


#: The time directory every export step moves the mesh through.
OUTPUT_TIME = 4

#: DP-5749. The ``topoSet`` dictionary a decomposed region split is zoned by.
REGION_ZONES_DICT = 'topoSetDict.foammesh-regions'


def _removeRegionZoneFile(path) -> None:
    """Remove one thing written for the region split, file or folder.

    A ``cellZones`` written over the mesher's own (DP-5751) gives the
    mesher's back.
    """
    path = Path(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        restore_cell_zones(path)


def _writeRegionSolvers(case_root, regions) -> None:
    """DP-5754. Name every region and its solver in ``system/controlDict``.

    OpenFOAM 13's ``-allRegions`` utilities (``reconstructPar``,
    ``decomposePar``) and ``foamMultiRun`` read the region list from
    ``regionSolvers``, not from ``constant/regionProperties``; without it they
    exit before reading a mesh.
    """
    control_dict = Path(case_root) / Directory.SYSTEM_DIRECTORY_NAME / 'controlDict'
    entries = ''.join(
        f'    {region.value("name")} '
        f'{"solid" if region.value("type") == RegionType.SOLID.value else "fluid"};\n'
        for region in regions.values())
    text = control_dict.read_text(encoding='utf-8') if control_dict.exists() else ''
    control_dict.parent.mkdir(parents=True, exist_ok=True)
    control_dict.write_text(
        text.rstrip() + f'\n\nregionSolvers\n{{\n{entries}}}\n', encoding='utf-8')


def _boundaryNames(path) -> set[str]:
    """The patch names in one ``polyMesh/boundary`` file."""
    path = Path(path)
    if not path.is_file():
        return set()
    text = path.read_text(encoding='utf-8', errors='replace')
    body = text[text.index('(', text.index('FoamFile') if 'FoamFile' in text else 0):]
    names, depth, word = set(), 0, ''
    for token in re.findall(r'[{}]|[^\s{}();]+', body):
        if token == '{':
            if depth == 0 and word:
                names.add(word)
            depth += 1
        elif token == '}':
            depth -= 1
        elif depth == 0:
            word = token
    return names


def _exportedMeshes(exported, regions) -> dict:
    """``{region: polyMesh/boundary}`` of the exported case (``None`` alone
    for a single-region case)."""
    if len(regions) <= 1:
        return {None: exported.boundaryFilePath()}
    return {region.value('name'): exported.boundaryFilePath(region.value('name'))
            for region in regions.values()}


def _legacyInterfaceNames(db) -> list[str]:
    """Surfaces still typed ``interface`` by hand, with no role.

    Plan 49 SR1 leaves such a surface unresolved: it was written as a cyclic
    pair (DP-5784), which is not a wall, a region interface or a periodic
    connection, and what it was meant to be cannot be read from it.
    """
    names = []
    for element in db.getElements(
            'geometry', lambda _i, e: e['cfdType'] == CFDType.INTERFACE.value
    ).values():
        try:
            role = element.value('surfaceRole')
        except Exception:  # noqa: BLE001 - a row saved before roles existed
            role = None
        role = getattr(role, 'value', role)
        if not role:
            names.append(element.value('name'))
    return names


def _refuseLegacyInterfaces(exported, regions, db) -> None:
    """DP-5760. Refuse a mesh still holding a hand-ticked interface's sides.

    The export used to join ``<name>`` and ``<name>_slave`` into a cyclic
    pair with ``createPatch`` (DP-5784), which carries flow straight through
    the sheet; without it they are two open patches and flow leaks out of
    the domain through them. Neither is what was drawn. The snappy writer
    now refuses an interface with no role; a mesh made before it is refused
    here, with the remedy, rather than exported either way.
    """
    wanted = _legacyInterfaceNames(db)
    if not wanted:
        return
    found = {region: set(couplings_module.boundary_names(path))
             for region, path in _exportedMeshes(exported, regions).items()}
    for name in wanted:
        where = sorted(str(region or 'the mesh') for region, names
                       in found.items()
                       if name in names or f'{name}_slave' in names)
        if where:
            raise RuntimeError(
                f'Surface {name}: it is an interface ticked by hand, and its '
                f'two sides are in {", ".join(where)} as patches with no '
                'coupling -- the export no longer joins them into a cyclic '
                'pair, which let flow through the sheet. Choose what it is '
                '(a blocking baffle, a region interface with its two regions, '
                'or one side of a periodic pair), mesh again, then export.')


def _mesh_at(root, output_time: int = OUTPUT_TIME):
    """The directory under *root* holding a finished mesh, or ``None``.

    *root* is a case directory or one processor case of it. The search order
    is ``AuthoredExportService._meshSourcePath``'s, whose docstring says why
    both places are looked in; it lives at module level so that the layout
    question below can be answered about a case on disk, with no export
    service and no ``FileSystem`` in hand.
    """
    root = Path(root)
    for time in range(output_time - 1, 0, -1):
        candidate = root / str(time)
        if isPolyMesh(candidate / Directory.POLY_MESH_DIRECTORY_NAME):
            return candidate

    constant = root / Directory.CONSTANT_DIRECTORY_NAME
    if isPolyMesh(constant / Directory.POLY_MESH_DIRECTORY_NAME):
        return constant

    return None


def _case_root(case_path) -> Path:
    """Where this case's OpenFOAM directories live.

    A case saved by this product keeps them at its own root; an adopted one
    keeps them under ``case/``. One rule, read by the export and by the count
    of mesh pieces below, so the two cannot answer about different trees.
    """
    case_path = Path(case_path)
    return (case_path if (case_path / 'constant').is_dir()
            else case_path / CASE_DIRECTORY_NAME)


def _requireEmptyDestination(destination) -> None:
    """Refuse a destination that already holds something (DP-562).

    The exported case is written by replacing its root folder, and that root
    is the destination itself, so a folder with anything in it would be
    emptied. The file writers refuse an existing file the same way.
    """
    destination = Path(destination)
    if destination.exists() and (not destination.is_dir()
                                 or any(destination.iterdir())):
        raise RuntimeError(
            f'export destination already exists: {destination}. '
            f'Choose a new folder name; nothing has been exported.')


def _mesh_pieces(case_path) -> int:
    """How many pieces the mesh in this case is written in; 0 for no mesh.

    Counted the way the copy step reads them, one processor case at a time
    from zero, so a case that holds ``processor0`` and ``processor1`` answers
    two and an undecomposed case answers one. A case with no mesh anywhere
    answers zero, which is not a layout and corrects nothing.
    """
    case_root = _case_root(case_path)
    pieces = 0
    while _mesh_at(case_root / f'processor{pieces}') is not None:
        pieces += 1
    if pieces:
        return pieces
    return 1 if _mesh_at(case_root) is not None else 0


def _exportLayout(session) -> _ExportLayout:
    """The layout of the run whose mesh is about to be exported.

    Plan 32 UX-13 / DP-229. This was
    ``LocalSettings(session.storage_path).parallelEnvironment()`` alone: the
    per-project Parallel Environment dialog. That store is not what the
    meshing run resolved its ranks from and nothing writes a finished run back
    into it, so it says whatever it was last left saying. MEASURED: with the
    dialog holding two, a serial native mesh failed its first export looking
    for a ``processor0`` directory the run had never created; it exported only
    after the count was hand-edited to one, without remeshing.

    The accepted run records the layout it really used, so that is read first.
    A case meshed before that record existed has none, and then the legacy
    store is read exactly as it was before -- no stored case changes shape and
    no existing case exports differently than it did.

    DP-257. Both of those are records *about* the mesh, and the guided
    workflow writes neither: a stage started from an engine row opens no run
    directory, so ``accepted_run_layout`` answers ``None`` and the dialog is
    read after all. MEASURED on the guided snappy walk -- the journey set two
    cores in the Parallel Environment dialog, the run was serial, and the
    export refused a finished ``constant/polyMesh`` with ``nothing to export
    for processor0``, writing no file and advancing no task. So the last word
    belongs to the mesh: the pieces it is actually written in are counted, and
    a declared count that disagrees is corrected to them. The declared record
    is still what names the run, and a case with no mesh to read keeps it
    whole -- the refusal such a case needs is the copy step's, one call later.
    """
    recorded = None
    try:
        recorded = accepted_run_layout(session.case_path)
    except OSError:
        recorded = None
    if recorded:
        declared = _ExportLayout(
            int(recorded.get('cores') or 1),
            str(recorded.get('run_id') or ''), 'accepted-run')
    else:
        parallel = LocalSettings(session.storage_path).parallelEnvironment()
        declared = _ExportLayout(
            int(parallel.np()) if parallel.isParallelOn() else 1)
    pieces = _mesh_pieces(session.case_path)
    if pieces and pieces != declared.np():
        return _ExportLayout(pieces, declared.run_id, 'mesh-on-disk')
    return declared


#: Every export event this module and its siblings write is recorded under
#: ``export:<entry_id>``; ``history.query`` hands them back under
#: ``artifacts``.
EXPORT_EVENT_PREFIX = 'export:'


def export_layout_details(source: str, run_id: str, cores, decomposed=None
                          ) -> dict:
    """The four details every export files about the layout it reproduced.

    DP-229 gave the authored OpenFOAM step these four keys; the native SU2
    copy in ``core/import_export/service.py`` filed a run id under a second
    spelling and nothing else, so half the exports a case can make carried
    three fewer facts than the other half and the reader had to know two
    vocabularies. One helper, called by both writers, is what keeps a single
    history speaking one language.
    """
    count = max(1, int(cores or 1))
    return {'mesh_layout_source': str(source or ''),
            'mesh_layout_run_id': str(run_id or ''),
            'decomposed': bool(count > 1 if decomposed is None else decomposed),
            'cores': count}


def export_record(entries) -> dict:
    """What the last export of this case wrote, and which run it came from.

    DP-244. The record has existed since DP-229 -- destination, the accepted
    run the layout was reproduced from, whether it was decomposed and over how
    many cores -- and nothing read it back, so the one surface that exists to
    say an export happened said only what it said before one.

    *entries* is the ``artifacts`` list of a ``history.query`` payload, or any
    iterable of the same mappings. The reader is pure and takes no session: it
    is called from the view, from the CLI and from tests, and a reader that
    needed a case open could not be any of those.

    Two spellings of one thing are accepted on purpose. The authored OpenFOAM
    step records ``mesh_layout_run_id``; ``_copy_native_su2`` records the same
    identifier as ``run_id``. A reader that knew only one spelling would tell
    an SU2 user that no run was recorded.
    """
    latest = None
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        operation = str(entry.get('operation') or '')
        if not operation.startswith(EXPORT_EVENT_PREFIX):
            continue
        # A refused export is not where the mesh went.
        if str(entry.get('status') or 'applied') not in ('applied', 'ok'):
            continue
        latest = entry
    if latest is None:
        return {}
    details = dict(latest.get('details') or {})
    return {
        'entry_id': str(latest.get('operation'))[len(EXPORT_EVENT_PREFIX):],
        'destination': str(details.get('destination') or ''),
        'run_id': str(details.get('mesh_layout_run_id')
                      or details.get('run_id') or ''),
        'layout_source': str(details.get('mesh_layout_source')
                             or details.get('source') or ''),
        'decomposed': bool(details.get('decomposed')),
        'cores': int(details.get('cores') or 1),
        'total_bytes': int(details.get('total_bytes') or 0),
        'timestamp': str(latest.get('timestamp') or ''),
        'history_entry_id': str(latest.get('entry_id') or ''),
    }


def export_destination_missing(record) -> bool:
    """Whether the destination the record names is no longer on disk.

    DP-244 left this standing: the record names the destination that was
    written and not whether it is still there, so a folder moved, renamed or
    deleted after an export still read as exported. This asks the second
    question without touching the first -- the history entry stays the record
    of an act, and the answer here is about the disk today.

    ``False`` when there is nothing to check and when the filesystem refuses
    to answer: a record with no destination makes no claim, and a share that
    is offline is not a destination that was thrown away.
    """
    destination = str((record or {}).get('destination') or '').strip()
    if not destination:
        return False
    try:
        return not Path(destination).exists()
    except OSError:
        return False


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
        log_path = session.storage_path / 'logs' / f'authored-export-{utility}.log'
        job = await session.jobs.run(JobRequest(
            name=f'authored export: {utility}', argv=launch.argv, cwd=cwd,
            mutation=True, log_path=log_path,
            timeout=900, cleanup_argv=launch.cleanup_argv),
            on_line=on_line)
        if job.status is not JobStatus.DONE:
            # DP-540 (MA24-02). MEASURED on S6: the modal said only "process
            # exited with code 1" while the log held OpenFOAM's one sentence
            # saying what was wrong. The cause is read out of the log the way
            # a failed meshing stage's is (DP-506), and the log is named.
            cause, _details = read_failure_cause(log_path)
            message = (f'{utility} failed during authored export: '
                       f'{job.error or job.status.value}')
            if cause:
                message += f'\nOpenFOAM: {cause}'
            if log_path.is_file():
                message += f'\nLog: {log_path}'
            raise RuntimeError(message)

    async def _requireRegionZones(self, session, file_system, regions,
                                  parallel, on_line=None):
        """Cell zones for the region split, or a refusal that says why.

        DP-538/DP-539 (MA24-02). Returns what was written for the split from
        the region points -- the caller removes each with
        ``_removeRegionZoneFile`` afterwards -- or nothing when the mesh's own
        zones already cover it.

        DP-5749. A decomposed mesh was returned unzoned, and the parallel
        split died on "Cell 0 ... is not in a cellZone" (MEASURED: the 15-core
        CHT tube). Its pieces cross processors, so OpenFOAM makes the zones:
        ``topoSet -parallel``, ``regionToCell`` from each region point.

        DP-6058. A Gmsh region is a typed solid whose zone the mesher wrote
        under its name: nothing is made, and a mesh whose zones are not the
        regions is refused, saying which.
        """
        if are_solids(regions):
            names = [region.value('name') for region in regions.values()]
            sources = ([self._meshSourcePath(file_system, index)
                        for index in range(parallel.np())]
                       if parallel.isParallelOn()
                       else [self._meshSourcePath(file_system)])
            try:
                for source in sources:
                    if source is not None:
                        require_solid_zones(
                            source / Directory.POLY_MESH_DIRECTORY_NAME, names)
            except RegionZoneError as error:
                raise RuntimeError(
                    f'the mesh cannot be split into its regions: {error}'
                ) from error
            return []
        if parallel.isParallelOn():
            return await self._requireDecomposedRegionZones(
                session, file_system, regions, parallel, on_line)
        source = self._meshSourcePath(file_system)
        if source is None:
            return []
        # Lazy: the points are read only when the mesh has no zones.
        seeds = ((region.value('name'), region.vector('point'))
                 for region in regions.values())
        try:
            written = prepare_region_zones(
                source / Directory.POLY_MESH_DIRECTORY_NAME, seeds)
        except RegionZoneError as error:
            raise RuntimeError(
                f'the mesh cannot be split into its regions: {error}') from error
        return [] if written is None else [written]

    async def _requireDecomposedRegionZones(self, session, file_system,
                                            regions, parallel, on_line):
        meshes = []
        for index in range(parallel.np()):
            source = self._meshSourcePath(file_system, index)
            if source is None:
                return []
            meshes.append(source / Directory.POLY_MESH_DIRECTORY_NAME)
        seeds = [(region.value('name'), region.vector('point'))
                 for region in regions.values()]
        try:
            # DP-5751. A mesher's zones are completed and named by region,
            # as the serial export does (``complete_region_zones``).
            plan = decomposed_region_plan(meshes, seeds)
        except RegionZoneError as error:
            raise RuntimeError(
                f'the mesh cannot be split into its regions: {error}') from error
        if plan is None:
            return []
        case_root = file_system.caseRoot()
        dictionary = case_root / 'system' / REGION_ZONES_DICT
        dictionary.write_text(region_zones_dict(*plan), encoding='ascii',
                              newline='\n')
        written = [dictionary]
        for mesh in meshes:
            zones = mesh / CELL_ZONES
            if zones.is_file():
                shutil.copyfile(zones, mesh / ORIGINAL_CELL_ZONES)
            written += [zones, mesh / 'sets']
        try:
            await self._run(
                session, 'topoSet',
                ('-dict', f'system/{REGION_ZONES_DICT}'),
                cwd=case_root, parallel=parallel, on_line=on_line)
        except BaseException:
            for path in written:
                _removeRegionZoneFile(path)
            raise
        return written

    def _requireReachableDestination(self, destination, utilities):
        """Refuse a destination no utility can be pointed at, up front.

        Plan 33 W-P. MEASURED on the live campaign, leg `1/3 snappy elbow
        auto for openfoam` at four cores: the export moved all four processor
        meshes into the destination, wrote `decomposeParDict`, and only then
        asked the runtime to run `reconstructPar` there -- which refused,
        because the destination was relative and the WSL profile cannot
        translate such a path. What the reader was left with was a
        destination holding four processor cases and no mesh, a source case
        with its mesh copied out, and a sentence saying the export failed.

        The translation is attempted here, before anything is staged, moved
        or created, for exactly the utilities this export will launch into
        the destination. Building a command launches nothing; it is the same
        call `_run` makes, so a destination that passes here is one the
        runtime can be pointed at later.
        """
        if self._capabilities is None or not utilities:
            return
        case_root = Path(destination)
        for utility in utilities:
            try:
                self._capabilities.command(utility, (), cwd=case_root)
            except LaunchProfileError as error:
                raise RuntimeError(
                    f'the OpenFOAM runtime cannot be pointed at this export '
                    f'destination, so nothing has been exported and the mesh '
                    f'is untouched: {error}') from error

    def _meshSourcePath(self, file_system, processorNo=None):
        """The directory that actually holds the finished mesh.

        snappyHexMesh used to leave the mesh behind as numbered time
        directories, and the export copied the last of those into
        ``OUTPUT_TIME``. Both engines now publish to ``constant/polyMesh``
        instead. Prefer a numbered directory when one still holds a mesh so
        that a case meshed by an older build keeps exporting, and fall back to
        ``constant``.
        """
        root = (file_system.caseRoot() if processorNo is None
                else file_system.processorPath(processorNo, False))
        return _mesh_at(root, self.OUTPUT_TIME)

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

    def _regionMeshRoot(self, file_system, regions, index=None):
        """Where splitMeshRegions left the per-region meshes.

        It writes to ``constant`` when the mesh it read was there and to the
        latest time directory otherwise, so neither location can be assumed.
        DP-5752. A parallel split writes them in each processor directory,
        which ``index`` names.
        """
        probe = next(iter(regions.values())).value('name')
        if index is None:
            candidates = [file_system.constantPath()]
        else:
            candidates = [file_system.processorPath(index, False)
                          / Directory.CONSTANT_DIRECTORY_NAME]
        candidates += [file_system.timePath(time, index)
                       for time in range(self.OUTPUT_TIME, -1, -1)]
        for candidate in candidates:
            if isPolyMesh(candidate / probe / Directory.POLY_MESH_DIRECTORY_NAME):
                return candidate

        where = '' if index is None else f' for processor{index}'
        raise RuntimeError(
            f'nothing to export{where}: splitMeshRegions produced no region '
            f'meshes')

    async def _createCellZones(self, session, file_system, exported, zones,
                               surfaces, regions, progress, on_line):
        """Add the cell-zone volumes to the mesh the export has just placed.

        DP-553/DP-554 (0924 follow-up: OpenFOAM 13 topoSet has no
        cellZoneSet). The export used to write a ``topoSetDict`` into the
        meshing case and run ``topoSet`` there, after staging the mesh into
        ``<case>/4/polyMesh``. That case starts at time 0, so OpenFOAM read
        and zoned ``constant/polyMesh`` -- MEASURED on OpenFOAM 13: a cube
        staged into ``4/`` came out of ``topoSet`` with ``boxZone``,
        ``ballZone`` and ``pipeZone`` in ``constant/polyMesh/cellZones`` and
        no ``cellZones`` in ``4/polyMesh``, the mesh that is exported. And
        ``topoSet`` is deprecated in v13 in favour of ``createZones``.

        So the zones are made here, by ``createZones``, in the exported case
        and after the mesh is in its ``constant`` (reconstructed, when the run
        was decomposed), which is the only mesh that time 0 there can mean.
        A region case runs it once per region with ``-region``, reading
        ``system/<region>/createZonesDict``.
        """
        if not zones.isBuilt():
            return

        for name in surfaces:
            shutil.copyfile(
                file_system.triSurfacePath() / surfaceFileName(name),
                exported.triSurfacePath() / surfaceFileName(name))

        progress('Processing Cell Zones')
        if len(regions) == 1:
            zones.write()
            await self._run(
                session, 'createZones', cwd=exported.caseRoot(),
                on_line=on_line)
            return
        for region in regions.values():
            name = region.value('name')
            if not zones.generatesZones():
                # DP-563 (0924 rerun). ``splitMeshRegions -cellZonesOnly``
                # hands every region every split zone -- its own whole, the
                # others empty -- and those are all the zones a split region
                # holds, since the split is by exactly those zones. With no
                # volume to add, removing them leaves an empty list, and
                # ``createZones`` does not write an empty list, so the file
                # the split left would stand. MEASURED on S6: both
                # ``cube_a_fluid`` and ``cube_b_fluid`` carried both zones.
                # The file is removed instead; a region needs no zone that is
                # the whole of itself.
                for stale in ('cellZones', 'cellZones.gz'):
                    (exported.polyMeshPath(name) / stale).unlink(
                        missing_ok=True)
                continue
            await exported.createRegionSystemDirectory(name)
            zones.setRegion(name).write()
            await self._run(
                session, 'createZones', ('-region', name),
                cwd=exported.caseRoot(), on_line=on_line)

    async def _coupleAfterSplit(self, session, exported, regions,
                                couplings, progress, on_line):
        """Couple the recorded region interfaces and periodic pairs.

        Plan 49 SR4 (DP-5761, DP-5762, DP-5791). After the split, with the
        dictionary form of ``createNonConformalCouples`` that names each
        side's region; see ``couplings.py``. The coupled patches are checked
        on disk, so a coupling that did not happen is said, not exported.
        """
        meshes = _exportedMeshes(exported, regions)
        try:
            renames, entries = couplings_module.plan(couplings, meshes)
        except couplings_module.CouplingRefusal as refusal:
            raise RuntimeError(str(refusal)) from refusal
        if not entries:
            return
        for region, old, new in renames:
            couplings_module.rename_patch(meshes[region], old, new)
        couplings_module.write(exported.caseRoot(), entries)
        progress('Coupling interfaces')
        await self._run(
            session, 'createNonConformalCouples',
            ('-case', str(exported.caseRoot())),
            cwd=exported.caseRoot(), on_line=on_line)
        coupled = couplings_module.coupled_patch_types(meshes)
        made = [name for found in coupled.values() for name in found]
        missing = [str(item.get('name')) for item in couplings
                   if not any(str(item.get('name') or '') and
                              name.startswith(str(item.get('name')) + '_on_')
                              for name in made)]
        if missing:
            raise RuntimeError(
                'createNonConformalCouples ran but the exported mesh holds no '
                f'coupled patch for {", ".join(missing)}; nothing coupled '
                'them, so the case is not exported as if it did.')

    async def run(self, session, destination: str | Path, *, boundaries=(),
                  options=None, on_line=None, on_progress=None) -> dict:
        destination = Path(destination)
        db = session.state.db
        case_root = _case_root(session.case_path)
        file_system = FileSystem(session.case_path, case_root=case_root)
        parallel = _exportLayout(session)
        progress = on_progress or (lambda _message: None)

        # DP-6058. On Gmsh the regions are the typed solids.
        regions = export_regions(db)
        # Plan 49 SR4. The couplings the mesher recorded for after the split
        # -- read here so the preflight below knows every utility this
        # export will launch into the destination before it stages anything.
        try:
            couplings = couplings_module.recorded_couplings(session.case_path)
        except couplings_module.CouplingRefusal as refusal:
            raise RuntimeError(str(refusal)) from refusal
        # DP-562 (0924 rerun). The destination the Export page shows is the
        # case: ``FileSystem(destination)`` put the case one folder deeper, in
        # ``<destination>/case`` -- the project layout of the application this
        # was forked from, whose exported folder was a project holding a case.
        # MEASURED on S6 and G6: ``S6_two_cubes_openfoam/case/constant/...``,
        # and the page, asked about its own destination again, said it
        # already existed. The written case root is the destination now, and
        # since the case is made by replacing that folder, a folder that
        # already holds something is refused before anything is staged.
        _requireEmptyDestination(destination)
        # DP-553. The cell zones are made in the destination, so whether
        # ``createZones`` will run there is known, and checked, up front. A
        # tri-surface volume is regenerated only from its own ``<name>.stl``
        # in the meshing case; without one the zone is the mesher's.
        exported = FileSystem(destination, case_root=destination)
        zones = CreateZonesDict(exported, db)
        surfaces = [
            name for name in zones.triSurfaceVolumes()
            if (file_system.triSurfacePath() / surfaceFileName(name)).is_file()]
        zones.build(surfaces)
        self._requireReachableDestination(destination, (
            *(('reconstructPar',) if parallel.isParallelOn() else ()),
            *(('createZones',) if zones.isBuilt() else ()),
            *(('createNonConformalCouples',) if couplings else ()),
            *(('extrudeMesh', 'collapseEdges') if options is not None else ())))

        if len(regions) > 1:
            # DP-538/DP-539 (MA24-02). ``-cellZonesOnly`` needs every cell in
            # exactly one cell zone, and a snappy mesh seeded from several
            # region points carries none -- Foundation 13 keeps the regions
            # but cannot name them. The zones are made from the region
            # points here, or the export is refused with the reason, before
            # OpenFOAM is started.
            zones_written = await self._requireRegionZones(
                session, file_system, regions, parallel, on_line)
            progress('Splitting Mesh Regions')
            try:
                await self._run(
                    session, 'splitMeshRegions', ('-cellZonesOnly',),
                    cwd=case_root, parallel=parallel, on_line=on_line)
            finally:
                # The native mesh is left as the mesher wrote it.
                for path in zones_written:
                    _removeRegionZoneFile(path)
        else:
            progress('Copying Files')
            await self._stageMesh(file_system, parallel)

        destination.mkdir(parents=True, exist_ok=True)
        exported.createCase(resource.file('openfoam/case'))
        if len(regions) > 1:
            RegionProperties(exported.caseRoot(), db).build().write()
            _writeRegionSolvers(exported.caseRoot(), regions)

        progress('Exporting Files')
        output_path = (self._regionMeshRoot(file_system, regions)
                       if len(regions) > 1 and not parallel.isParallelOn()
                       else file_system.timePath(self.OUTPUT_TIME))
        if parallel.isParallelOn():
            for index in range(parallel.np()):
                processor = exported.processorPath(index, False)
                processor.mkdir()
                constant = processor / Directory.CONSTANT_DIRECTORY_NAME
                if len(regions) > 1:
                    # DP-5752. A parallel split leaves each processor's
                    # region meshes in that processor's directory.
                    root = self._regionMeshRoot(file_system, regions, index)
                    constant.mkdir()
                    for region in regions.values():
                        shutil.move(root / region.value('name'), constant)
                else:
                    shutil.move(file_system.timePath(self.OUTPUT_TIME, index),
                                constant)
            # DP-229. ``reconstructPar`` reads ``system/decomposeParDict``
            # from the case it is pointed at, and the exported case template
            # (``src/resources/openfoam/case``) carries no such file, so the
            # reconstruct step in the destination had nothing to read. It is
            # written here from the layout this export is reproducing, before
            # the utility is launched, and it adds no command of its own.
            decomposition.write(
                exported.caseRoot(), parallel.np(),
                decomposition.DecompositionSettings.read(db))
            progress('Reconstructing exported mesh')
            await self._run(
                session, 'reconstructPar',
                ('-constant', '-noFields',
                 *(('-allRegions',) if len(regions) > 1 else ()),
                 '-case', str(exported.caseRoot())),
                cwd=exported.caseRoot(), on_line=on_line)
        elif len(regions) > 1:
            for region in regions.values():
                shutil.move(output_path / region.value('name'), exported.constantPath())
        else:
            shutil.move(output_path / Directory.POLY_MESH_DIRECTORY_NAME,
                        exported.polyMeshPath())

        await self._createCellZones(
            session, file_system, exported, zones, surfaces, regions,
            progress, on_line)

        # DP-5760/DP-5784. A hand-ticked interface is no longer joined into a
        # cyclic pair; a mesh still holding one is refused.
        _refuseLegacyInterfaces(exported, regions, db)
        if couplings:
            await self._coupleAfterSplit(
                session, exported, regions, couplings, progress, on_line)

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
        # Plan 49 SR6. The hand-off sidecar and the final check, as a separate
        # step after the export has finished: it reads what is on disk and
        # changes none of it, writes constant/foammesh_handoff.json, and
        # refuses (after writing it) an export that does not carry the
        # authored roles and relations.
        handoff = write_openfoam_handoff(
            exported.caseRoot(), source_case=session.case_path, db=db,
            couplings=couplings).summary()
        entry = record_export_event(
            session.case_path, entry_id='openfoam', destination=destination,
            total_bytes=path_bytes(destination),
            details={'entry_point': 'authored_export_step',
                     'two_dimensional': options is not None,
                     # DP-229. Which run's layout this export was built
                     # against, so a written case can be traced to the result
                     # it came from rather than to a setting nobody recorded.
                     # Through the shared helper, because the SU2 copy files
                     # the same four facts and two spellings of one record is
                     # what left the reader guessing.
                     **export_layout_details(
                         parallel.source, parallel.run_id, parallel.np(),
                         parallel.isParallelOn()),
                     'handoff': handoff})
        return {'destination': str(destination), 'history_entry_id': entry.entry_id,
                'total_bytes': path_bytes(destination),
                'two_dimensional': options is not None,
                # Plan 49 SR6: the sidecar beside the mesh, and its verdict.
                'handoff': handoff}
