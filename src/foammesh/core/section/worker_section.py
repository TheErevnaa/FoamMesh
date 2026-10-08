#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mesh.section``: an exact section computed in the mesh worker (Plan 37 UF10).

The request (``args``, JSON)::

    case          the case directory (or its constant/polyMesh)
    case_id       the window's id for the case (echoed in the key)
    out_dir       job scratch: the result is published at out_dir/<job>/
    job           the job's name (default: a fresh uuid)
    generation    the window's plane generation (echoed in the key)
    planes        the ordered plane set: SectionPlaneState dicts
                  {'enabled', 'pivot', 'normal', 'reference', 'distance',
                  'keep'} -- keep is the keep-side sign
    active        index of the active plane in ``planes``
    mode          'slice' | 'cut_cells' | 'clip' | 'clip_whole_cells'
    arrays        source arrays wanted: 'cell_zone', 'cell_level',
                  'cell_type' (UF9, `section_colour` codes) and
                  'quality.<field>' (UF9, `core.quality.cell_fields`, computed
                  here from the same read, so of the same revision)
    mesh_revision the revision the window expects (optional): a different
                  mesh on disk is refused ``stale_input``
    tolerance     relative contact tolerance (default 1e-9 of the diagonal)
    budgets       read limits (max_points, max_faces, max_cells,
                  max_input_bytes, max_array_bytes) and output limits
                  (max_polygons, max_points_out, max_selected_cells,
                  max_exact_cells, max_output_bytes). None are applied by
                  default except max_exact_cells (a time bound): the read
                  and the output are refused only by the free RAM
    cancel_path   a file whose existence cancels the job between stages
    snapshot      true: copy the mesh into job scratch first (a read lease
                  otherwise; either way a rewrite during the read refuses)
    cells         UF11 frozen layer: sorted inclusive runs ``[[first, last],
                  ...]`` of source cell ids. The answer is exactly those
                  cells, whole, wherever the planes are (mode cut_cells);
                  an id the mesh does not have refuses ``stale_input`` --
                  cells are never matched to the nearest index

The response is the manifest (also written as ``manifest.json``)::

    schema        'foammesh.section/1'
    status        'ok' | 'empty'
    key           {case_id, mesh_revision, generation, mode, arrays} -- the
                  window matches all five before it touches the view
    mesh          {revision, content_digest, cells, faces, points, formats}
    mode, active, planes (the enabled ones, with their index), policy
    tolerance     {relative, absolute}
    exact         true, or false with ``approximate`` {reason: count}
    empty         null or {reason: contradictory_planes | thin_region |
                  misses_mesh, message}
    counts        polygons, points, source_cells, selected_cells, ...
    cell_scale_step  median span of the cells the active plane meets
    arrays        {name: dtype} written as cell data; unavailable_arrays
                  {name: reason} -- a missing array is never zeros
    directory, files {surface, cells}: {path, bytes, sha256}
    timing        seconds per stage; budgets as applied
    peak_rss_bytes  the worker's peak working set when it finished

``section.vtp`` holds the polygons with cell data ``sourceCellId`` (int64)
and any arrays, field data ``meshRevision``; ``cells.npy`` holds the source
cells (the selected set for the whole-cell modes, else the cells the
polygons came from). Nothing is published unless everything was written:
the job writes into ``<job>.part`` and renames it.

``mesh.section_batch`` (:func:`run_batch`, UF11 Compare) takes ``{'jobs':
[args, ...]}`` -- one request as above per stage snapshot -- and cuts them
one after another in the one process, answering ``{'schema':
'foammesh.section-batch/1', 'stages': [...]}`` with, per job in order,
``{job, ok: true, manifest, seconds}`` or ``{job, ok: false, status, reason,
message, details, seconds}``: a refusal of one stage is that stage's answer
and the next stage is still cut.

Refusals raise :class:`SectionRefused` (reason + details), which the worker
reports as a failed result: ``over_budget`` (stage ``input`` or ``output``,
with the quantity, limit and measured value -- the window keeps its previous
preview), ``cancelled``, ``stale_input``, ``no_planes``, ``bad_request``,
and the reader's layout refusals (``decomposed_layout``, ...).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from foammesh.core.section.modes import SectionMode
from foammesh.core.section.plane_state import PlaneState
from foammesh.core.section.section_geometry import (
    HalfSpace, SectionGeometry, region_state)
from foammesh.core.section import section_modes

__all__ = ['SCHEMA', 'BATCH_SCHEMA', 'SectionRefused', 'run', 'run_batch',
           'DEFAULT_OUTPUT_LIMITS', 'KNOWN_ARRAYS']

SCHEMA = 'foammesh.section/1'
BATCH_SCHEMA = 'foammesh.section-batch/1'
OPERATION = 'mesh.section'
RELATIVE_TOLERANCE = 1e-9

#: Output limits when the request names none. There is no fixed count or
#: byte cap on what a section draws (2026-10-01: a mesh may go beyond 150 M
#: cells when the RAM holds it): the worker's own memory was admitted
#: before it started, and the output is refused only when the free RAM
#: cannot hold the window loading it (:data:`WINDOW_LOAD_FACTOR`). The
#: exact-check limit is a time bound on the per-cell Python fallback.
DEFAULT_OUTPUT_LIMITS = {
    'max_polygons': None,
    'max_points_out': None,
    'max_selected_cells': None,
    'max_exact_cells': 200_000,
    'max_output_bytes': None,
}
#: The window holds about this many times the written section's bytes
#: while it reads and draws it (the reader's buffer, the polydata, the
#: mapper's copy); the output is checked against the free RAM with it.
WINDOW_LOAD_FACTOR = 4
READ_LIMITS = ('max_points', 'max_faces', 'max_cells', 'max_input_bytes',
               'max_array_bytes')
#: Plan 37 UF9: every array the section can be coloured by. The vtp names
#: are the ones the local cut carries, so one colour mapping serves both.
QUALITY_ARRAYS = ('cellAspectRatio', 'nonOrthoAngle', 'skewness',
                  'cellVolume')
KNOWN_ARRAYS = ('cell_zone', 'cell_level', 'cell_type',
                *(f'quality.{name}' for name in QUALITY_ARRAYS))
VTK_ARRAY_NAMES = {'cell_zone': 'cellZone', 'cell_level': 'cellLevel',
                   'cell_type': 'cellType',
                   **{f'quality.{name}': name for name in QUALITY_ARRAYS}}

POLICY = {
    'clip': 'the intersection of every enabled plane\'s kept half-space',
    'section': 'the active plane, masked by the other enabled planes\' '
               'kept half-spaces',
    'contact': 'Cut cells takes every cell whose closed volume meets the '
               'plane (both cells beside a face it lies on); Slice draws a '
               'face the plane lies on once, from the cell on its negative '
               'side; an edge or vertex contact adds no slice area',
}


class SectionRefused(Exception):
    """A typed refusal: ``reason`` is a stable token, ``details`` data."""

    def __init__(self, reason: str, message: str, **details):
        super().__init__(message)
        self.reason = reason
        self.details = details


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #

def _planes(args: dict):
    raw = args.get('planes')
    if not isinstance(raw, list) or not raw:
        raise SectionRefused('no_planes', 'the request names no plane')
    planes = []
    for index, item in enumerate(raw):
        try:
            state = PlaneState.from_dict(item)
        except (KeyError, TypeError, ValueError) as error:
            raise SectionRefused('bad_request',
                                 f'plane {index} is not valid: {error}',
                                 plane=index) from None
        planes.append((index, bool(item.get('enabled', True)), state))
    return planes


def _frozen_runs(args: dict):
    """UF11. The frozen cells' runs, validated (``None``: not frozen)."""
    raw = args.get('cells')
    if raw is None:
        return None
    try:
        runs = np.asarray(raw, dtype=np.int64).reshape(-1, 2)
    except (TypeError, ValueError):
        raise SectionRefused('bad_request', 'the frozen cells are not runs '
                             'of [first, last]') from None
    if len(runs) and ((runs[:, 0] > runs[:, 1]).any() or runs.min() < 0
                      or (runs[1:, 0] <= runs[:-1, 1]).any()):
        raise SectionRefused('bad_request', 'the frozen cells are not '
                             'sorted, disjoint runs')
    return runs


def _frozen_mask(runs, n_cells: int) -> np.ndarray:
    if len(runs) and int(runs[-1, 1]) >= n_cells:
        raise SectionRefused(
            'stale_input', 'the frozen cells are not cells of this mesh: '
            f'cell {int(runs[-1, 1])} of {n_cells}', stage='input',
            cells=n_cells, largest=int(runs[-1, 1]))
    delta = np.zeros(n_cells + 1, dtype=np.int64)
    np.add.at(delta, runs[:, 0], 1)
    np.add.at(delta, runs[:, 1] + 1, -1)
    return np.cumsum(delta[:-1]) > 0


def _mode(args: dict) -> SectionMode:
    try:
        return SectionMode(args.get('mode'))
    except ValueError:
        raise SectionRefused('bad_request',
                             f'unknown section mode {args.get("mode")!r}',
                             mode=args.get('mode')) from None


def request_key(args: dict, revision: str | None) -> dict:
    return {'case_id': args.get('case_id'), 'mesh_revision': revision,
            'generation': args.get('generation'), 'mode': args.get('mode'),
            'arrays': sorted(args.get('arrays') or ())}


class _Clock:
    def __init__(self, cancel_path):
        self.started = time.perf_counter()
        self.last = self.started
        self.stages = {}
        self.cancel_path = Path(cancel_path) if cancel_path else None

    def stage(self, name: str) -> None:
        now = time.perf_counter()
        self.stages[name] = round(now - self.last, 4)
        self.last = now
        self.check()

    def check(self) -> None:
        if self.cancel_path is not None and self.cancel_path.exists():
            raise SectionRefused('cancelled', 'the section was cancelled')

    def total(self) -> dict:
        return dict(self.stages,
                    total=round(time.perf_counter() - self.started, 4))


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _poly_data(polygons, arrays: dict, revision: str, zone_names):
    from vtkmodules.util.numpy_support import (
        numpy_to_vtk, numpy_to_vtkIdTypeArray)
    from vtkmodules.vtkCommonCore import vtkPoints, vtkStringArray
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    data = vtkPolyData()
    points = vtkPoints()
    points.SetDataTypeToFloat()
    points.SetData(numpy_to_vtk(
        np.ascontiguousarray(polygons.points, dtype=np.float32), deep=True))
    data.SetPoints(points)
    cells = vtkCellArray()
    cells.SetData(
        numpy_to_vtkIdTypeArray(
            np.ascontiguousarray(polygons.offsets, dtype=np.int64), deep=True),
        numpy_to_vtkIdTypeArray(
            np.ascontiguousarray(polygons.connectivity, dtype=np.int64),
            deep=True))
    data.SetPolys(cells)
    source = numpy_to_vtk(np.ascontiguousarray(polygons.cells, np.int64),
                          deep=True)
    source.SetName('sourceCellId')
    data.GetCellData().AddArray(source)
    for name, per_cell in arrays.items():
        dtype = np.float64 if per_cell.dtype.kind == 'f' else np.int32
        values = numpy_to_vtk(np.ascontiguousarray(
            per_cell[polygons.cells], dtype=dtype), deep=True)
        values.SetName(VTK_ARRAY_NAMES[name])
        data.GetCellData().AddArray(values)
    stamp = vtkStringArray()
    stamp.SetName('meshRevision')
    stamp.InsertNextValue(revision)
    data.GetFieldData().AddArray(stamp)
    if zone_names is not None:
        names = vtkStringArray()
        names.SetName('cellZoneNames')
        for name in zone_names:
            names.InsertNextValue(name)
        data.GetFieldData().AddArray(names)
    return data


def _write_vtp(data, path: Path) -> int:
    from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter

    writer = vtkXMLPolyDataWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(data)
    writer.SetDataModeToAppended()
    writer.EncodeAppendedDataOff()
    writer.SetCompressorTypeToNone()
    writer.SetHeaderTypeToUInt64()
    if not writer.Write():
        raise OSError(f'could not write {path}')
    return path.stat().st_size


class _QualityMesh:
    """`SectionGeometry` as `core.quality.cell_fields` reads a mesh."""

    def __init__(self, geometry):
        self.points = geometry.points
        self.face_vertices = geometry.face_vertices
        self.face_offsets = geometry.face_offsets
        self.owner = geometry.owner
        self.neighbour = geometry.neighbour
        self.cell_count = geometry.n_cells
        self.face_count = geometry.n_faces


#: What the quality fields cost a cell they are computed for, with its face
#: neighbours (the whole-mesh computation MEASURED 2,770-2,780 bytes a cell
#: more than the section; see resource_budget's ``mesh.section.quality``).
QUALITY_BYTES_PER_CELL = 2800
#: What they cost every cell of the mesh whatever is kept: the four
#: full-length float64 arrays.
QUALITY_BYTES_PER_MESH_CELL = 32


def _quality(geometry, kept):
    """The quality arrays for ``kept`` (every cell when None), or why not.

    The job was admitted with ``mesh.section.quality`` over
    ``mesh.section``; a kept set whose quality costs more than that margin
    leaves the quality colour unavailable, naming the RAM, rather than
    outgrowing the worker's memory cap."""
    from foammesh.core.quality import cell_fields
    from foammesh.support import resource_budget

    mesh = _QualityMesh(geometry)
    if kept is None:
        return cell_fields.compute_cell_fields(mesh)
    factors = resource_budget.PEAK_FACTORS
    quality, plain = (factors['mesh.section.quality'],
                      factors['mesh.section'])
    margin = ((quality['cells'] - plain['cells']) * mesh.cell_count
              + quality.get('fixed', 0) - plain.get('fixed', 0))
    grown = cell_fields.subset_cells(mesh, kept).size if kept.size else 0
    needed = (QUALITY_BYTES_PER_CELL * grown
              + QUALITY_BYTES_PER_MESH_CELL * mesh.cell_count)
    if needed > margin:
        return ('colouring the {0:,} drawn cells by quality needs about {1} '
                'of RAM, more than the {2} this section was given for it'
                ).format(int(kept.size), resource_budget.format_bytes(needed),
                         resource_budget.format_bytes(int(margin)))
    return cell_fields.compute_cell_fields_for(mesh, kept)


def _source_arrays(topology, wanted, geometry=None, kept=None):
    """``({name: per-cell array}, {name: reason}, zone names)``.

    ``kept``: the cells the section draws. Quality is computed for those
    cells only (``compute_cell_fields_for``: their faces and their face
    neighbours'), so its memory follows the section, not the whole mesh;
    None computes it for every cell."""
    arrays, unavailable, zone_names = {}, {}, None
    quality = None
    for name in wanted:
        if name == 'cell_type' and geometry is not None:
            from foammesh.core.section.section_colour import (
                cellTypesFromFaces)
            arrays[name] = cellTypesFromFaces(
                geometry.owner, geometry.neighbour, geometry.sizes,
                geometry.n_cells)
            continue
        field = (name[len('quality.'):] if name.startswith('quality.')
                 else None)
        if field in QUALITY_ARRAYS and geometry is not None:
            if quality is None:
                quality = _quality(geometry, kept)
            if isinstance(quality, str):
                unavailable[name] = quality
                continue
            arrays[name] = np.asarray(quality[field], dtype=np.float64)
            continue
        if name == 'cell_zone':
            if topology.cell_zones_status != 'read':
                unavailable[name] = 'the mesh has no cellZones'
                continue
            values = np.full(topology.n_cells, -1, dtype=np.int32)
            zone_names = []
            for index, zone in enumerate(topology.cell_zones):
                values[np.asarray(zone.labels, dtype=np.int64)] = index
                zone_names.append(zone.name)
            arrays[name] = values
        elif name == 'cell_level':
            if topology.cell_level_status == 'read' \
                    and topology.cell_level is not None:
                arrays[name] = np.asarray(topology.cell_level, np.int32)
            elif topology.cell_level_status == 'size_mismatch':
                unavailable[name] = ('cellLevel does not match the mesh\'s '
                                     'cell count')
            else:
                unavailable[name] = ('the mesh has no cellLevel (it was not '
                                     'refined by snappyHexMesh)')
        else:
            unavailable[name] = 'the section worker does not provide it'
    return arrays, unavailable, zone_names


# --------------------------------------------------------------------------- #
# The operation
# --------------------------------------------------------------------------- #

def _import_solver_early() -> None:
    """Import the LP solver ``region_state`` needs on a thread, meanwhile.

    Importing ``scipy.optimize`` takes most of a second; started before the
    mesh is read it overlaps the read (which spends its time in file reads
    and NumPy copies that release the GIL). ``region_state`` imports it as
    before and simply finds it imported, or waits for the import to finish.
    """
    def load() -> None:
        try:
            import scipy.optimize  # noqa: F401
        except Exception:          # region_state reports it when it imports
            pass

    threading.Thread(target=load, name='section-import-solver',
                     daemon=True).start()


def run(args: dict) -> dict:
    """The worker body; returns the manifest (see the module docstring)."""
    from foammesh.core.mesh.poly_mesh_topology import (
        TopologyBudget, TopologyRefusal, read_mesh_topology, refusal_dict)
    from foammesh.core.mesh.poly_mesh_boundary import PolyMeshReadError

    clock = _Clock(args.get('cancel_path'))
    mode = _mode(args)
    planes = _planes(args)
    enabled = [(index, state) for index, on, state in planes if on]
    if not enabled:
        raise SectionRefused('no_planes', 'no plane is enabled')
    active_index = int(args.get('active', enabled[0][0]))
    active_state = next((s for i, s in enabled if i == active_index), None)
    if active_state is None:
        raise SectionRefused('bad_request',
                             f'the active plane {active_index} is not '
                             'enabled', active=active_index)
    wanted = list(dict.fromkeys(args.get('arrays') or ()))
    frozen = _frozen_runs(args)
    budgets = dict(args.get('budgets') or {})
    limits = {key[len('max_'):]: _limit(budgets.get(key, default))
              for key, default in DEFAULT_OUTPUT_LIMITS.items()}
    limits['points'] = limits.pop('points_out')
    # no output byte limit named: the free RAM decides (see _output_room)
    memory_room = None
    if limits['output_bytes'] is None:
        memory_room = _output_room()
        limits['output_bytes'] = memory_room // WINDOW_LOAD_FACTOR
    read_budget = TopologyBudget(**{key: int(budgets[key])
                                    for key in READ_LIMITS if key in budgets})

    out_dir = Path(args.get('out_dir') or '.')
    job = str(args.get('job') or f'section-{uuid.uuid4().hex}')
    scratch = out_dir / f'{job}.part'
    final = out_dir / job
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    if frozen is None and len(enabled) > 1:
        _import_solver_early()
    try:
        clock.check()
        try:
            topology = read_mesh_topology(
                args['case'], budget=read_budget,
                include_cell_zones='cell_zone' in wanted,
                include_cell_level='cell_level' in wanted,
                snapshot_dir=(scratch / 'snapshot') if args.get('snapshot')
                else None)
        except TopologyRefusal as error:
            detail = refusal_dict(error)
            extra = {('layout_reason' if key == 'reason' else
                      'read_stage' if key == 'stage' else key): value
                     for key, value in (detail.get('detail') or {}).items()
                     if key != 'message'}
            extra['stage'] = 'input'
            if detail.get('path'):
                extra['path'] = str(detail['path'])
            raise SectionRefused(error.reason, str(error), **extra) from None
        except PolyMeshReadError as error:
            raise SectionRefused(error.reason, str(error)) from None
        finally:
            shutil.rmtree(scratch / 'snapshot', ignore_errors=True)
        revision = topology.identity.revision
        expected = args.get('mesh_revision')
        if expected and expected != revision:
            raise SectionRefused(
                'stale_input', 'the mesh on disk is not the revision the '
                'window asked about', expected=expected, found=revision)
        clock.stage('read')

        geometry = SectionGeometry.from_topology(topology)
        points = geometry.points
        diagonal = _diagonal(points)
        relative = float(args.get('tolerance') or RELATIVE_TOLERANCE)
        tol = relative * diagonal
        active = HalfSpace.from_state(active_state)
        spaces = [HalfSpace.from_state(s) for _i, s in enabled]
        masks = [HalfSpace.from_state(s) for i, s in enabled
                 if i != active_index]

        # an empty or contradictory region is an answer, not a failure
        # (one half-space always has an interior: no LP, no scipy import)
        if frozen is not None:
            region = 'ok'          # the cells are the answer, not the planes
        elif mode.clips:
            region = (region_state(spaces, None, tol) if len(spaces) > 1
                      else 'ok')
        else:
            region = region_state(masks, active, tol) if masks else 'ok'
        clock.stage('region')
        empty = None
        if region != 'ok':
            empty = {'reason': 'contradictory_planes' if region ==
                     'contradictory' else 'thin_region',
                     'message': ('The enabled planes keep no common region.'
                                 if region == 'contradictory' else
                                 'The enabled planes keep only a sheet with '
                                 'no volume.')}
            result = None
            step = None
        else:
            facing = HalfSpace(active.normal, active.offset, 1)
            value, codes = geometry.plane(facing, tol)
            step = section_modes.cell_scale_step(geometry, value, tol, codes)
            caps = dict(limits, cell_data_bytes=_cell_data_bytes(wanted))
            try:
                if frozen is not None:
                    result = section_modes.frozen_cells_section(
                        geometry, _frozen_mask(frozen, topology.n_cells),
                        caps)
                elif mode is SectionMode.SLICE:
                    result = section_modes.slice_section(
                        geometry, active, masks, tol, caps)
                elif mode is SectionMode.CUT_CELLS:
                    result = section_modes.cut_cells_section(
                        geometry, active, masks, tol, caps)
                elif mode is SectionMode.CLIP:
                    result = section_modes.clip_section(
                        geometry, spaces, tol, caps)
                else:
                    result = section_modes.whole_cells_section(
                        geometry, spaces, tol, caps)
            except section_modes.OutputCap as cap:
                raise _over(cap.quantity, cap.limit, cap.measured,
                            estimated=cap.estimated,
                            room=memory_room) from None
            clock.stage('build')
            if not result.polygons.count:
                empty = {'reason': 'misses_mesh',
                         'message': ('The plane does not meet the mesh.'
                                     if not mode.clips else
                                     'The kept region holds no cells.')}
        kept = (np.unique(np.asarray(result.polygons.cells, dtype=np.int64))
                if result is not None and empty is None
                else np.zeros(0, dtype=np.int64))
        arrays, unavailable, zone_names = _source_arrays(topology, wanted,
                                                         geometry, kept)

        files = {}
        counts = {'mesh_cells': topology.n_cells,
                  'mesh_faces': topology.n_faces,
                  'mesh_points': topology.n_points,
                  'polygons': 0, 'points': 0, 'source_cells': 0}
        if result is not None:
            counts.update(result.counts)
        if empty is None:
            polygons = result.polygons
            if (limits['polygons'] is not None
                    and polygons.count > limits['polygons']):
                raise _over('polygons', limits['polygons'], polygons.count)
            if (limits['points'] is not None
                    and len(polygons.points) > limits['points']):
                raise _over('points', limits['points'], len(polygons.points))
            estimate = (12 * len(polygons.points)
                        + 8 * (len(polygons.connectivity) + polygons.count)
                        + (8 + sum(8 if values.dtype.kind == 'f' else 4
                                   for values in arrays.values()))
                        * polygons.count)
            if estimate > limits['output_bytes']:
                raise _over('output_bytes', limits['output_bytes'], estimate,
                            room=memory_room)
            data = _poly_data(polygons, arrays, revision, zone_names)
            surface = scratch / 'section.vtp'
            size = _write_vtp(data, surface)
            if size > limits['output_bytes']:
                raise _over('output_bytes', limits['output_bytes'], size,
                            room=memory_room)
            cells_path = scratch / 'cells.npy'
            np.save(cells_path, np.asarray(result.cells, dtype=np.int64))
            files = {
                'surface': {'path': str(final / 'section.vtp'),
                            'bytes': size, 'sha256': _sha256(surface)},
                'cells': {'path': str(final / 'cells.npy'),
                          'bytes': cells_path.stat().st_size,
                          'sha256': _sha256(cells_path)}}
            counts.update(polygons=polygons.count,
                          points=int(len(polygons.points)),
                          source_cells=int(np.unique(polygons.cells).size),
                          triangulated_cells=int(result.triangulated))
            clock.stage('write')

        approximate = dict(result.approximate) if result is not None else {}
        manifest = {
            'schema': SCHEMA,
            'operation': OPERATION,
            'status': 'empty' if empty else 'ok',
            'key': request_key(args, revision),
            'mesh': {'revision': revision,
                     'content_digest': topology.identity.content_digest,
                     'cells': topology.n_cells, 'faces': topology.n_faces,
                     'points': topology.n_points,
                     'formats': dict(topology.encodings)},
            'mode': mode.value,
            'active': active_index,
            'planes': [dict(state.to_dict(), index=i) for i, state in enabled],
            'policy': POLICY,
            'tolerance': {'relative': relative, 'absolute': tol},
            'exact': not approximate,
            'approximate': approximate,
            'empty': empty,
            'counts': counts,
            'cell_scale_step': step,
            'arrays': dict({'sourceCellId': 'int64'},
                           **{VTK_ARRAY_NAMES[n]: 'float64'
                              if values.dtype.kind == 'f' else 'int32'
                              for n, values in arrays.items()}),
            'unavailable_arrays': unavailable,
            'cell_zone_names': zone_names,
            'directory': str(final),
            'files': files,
            'budgets': dict(read_budget.to_dict(),
                            **{f'max_{k}': v for k, v in limits.items()}),
        }
        clock.check()
        manifest['timing'] = clock.total()
        manifest['peak_rss_bytes'] = _peak_rss()
        (scratch / 'manifest.json').write_text(
            json.dumps(manifest, indent=1), encoding='utf-8')
        shutil.rmtree(final, ignore_errors=True)
        os.replace(scratch, final)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return manifest


def run_batch(args: dict) -> dict:
    """Cut each of ``args['jobs']`` in turn, in this process (UF11).

    Each job is a whole :func:`run` request with its own case (a stage
    snapshot), job name and cancel file; its answer or its refusal is
    recorded and the next job is cut. Only the jobs named are cut: a stage
    with no snapshot is never in the batch, so nothing stands in for it.
    """
    from foammesh.support import gc_policy

    jobs = args.get('jobs')
    if not isinstance(jobs, list) or not all(isinstance(job, dict)
                                             for job in jobs):
        raise SectionRefused('bad_request', 'the batch names no list of jobs')
    stages = []
    for job in jobs:
        started = time.perf_counter()
        entry = {'job': str(job.get('job') or '')}
        try:
            entry.update(ok=True, manifest=run(job))
        except SectionRefused as refusal:
            entry.update(ok=False, status=refusal.reason,
                         reason=refusal.reason, message=str(refusal),
                         details=dict(refusal.details))
        except MemoryError:
            entry.update(ok=False, status='out_of_memory',
                         reason='out_of_memory',
                         message='the worker ran out of the memory it was '
                                 'granted for this stage', details={})
        except Exception as error:                   # noqa: BLE001
            entry.update(ok=False, status='failed', reason='worker_error',
                         message=f'{type(error).__name__}: {error}',
                         details={})
        entry['seconds'] = round(time.perf_counter() - started, 4)
        stages.append(entry)
        # The next stage starts from the floor (Plan 35 CR3: the policy
        # module is the collector's one caller).
        gc_policy.collect_full('section batch stage')
    return {'schema': BATCH_SCHEMA, 'stages': stages,
            'peak_rss_bytes': _peak_rss()}


def _peak_rss() -> int | None:
    """This process's peak resident set so far, or None if unknown."""
    try:
        import psutil

        info = psutil.Process().memory_info()
        return int(getattr(info, 'peak_wset', 0) or info.rss)
    except Exception:                                   # noqa: BLE001
        return None


def _diagonal(points) -> float:
    """The bounding box diagonal (a column at a time: an ``axis=0``
    reduction of an (n, 3) array is several times slower)."""
    if not len(points):
        return 1.0
    span = [float(points[:, i].max() - points[:, i].min()) for i in range(3)]
    return float(np.linalg.norm(span)) or 1.0


def _cell_data_bytes(wanted) -> int:
    """Bytes per polygon of the colour arrays asked for (as written)."""
    return sum(8 if name.startswith('quality.') else 4
               for name in wanted if name in KNOWN_ARRAYS)


def _limit(value) -> int | None:
    return None if value is None else int(value)


def _output_room() -> int:
    """Bytes of RAM free for the window to load the section (the machine's
    budget now, this worker already holding what it holds)."""
    from foammesh.support import resource_budget

    try:
        measured = resource_budget.quick_snapshot()
        if not measured.forced and measured.total <= 0:
            return 1 << 62      # the memory could not be read: no refusal
        return max(0, int(measured.budget))
    except Exception:                                   # noqa: BLE001
        return 1 << 62          # unknown: never invent a refusal


def _over(quantity: str, limit: int, measured: int, *,
          estimated: bool = False,
          room: int | None = None) -> SectionRefused:
    """``over_budget``, naming the cap. ``estimated``: counted by the
    prefilter before any polygon was built, so nothing was clipped.
    ``room``: the output byte limit came from the free RAM (no limit was
    named), so the refusal states the RAM needed and the RAM free."""
    if quantity == 'output_bytes' and room is not None:
        from foammesh.support.resource_budget import format_bytes

        needed = WINDOW_LOAD_FACTOR * int(measured)
        return SectionRefused(
            'over_budget',
            f'drawing the section needs about {format_bytes(needed)} of RAM '
            f'and {format_bytes(room)} is free. The previous section is '
            f'kept: narrow the planes, choose a simpler mode, or free some '
            f'memory and retry.',
            quantity='memory_bytes', limit=int(room), measured=needed,
            needed_bytes=needed, free_bytes=int(room),
            estimated=bool(estimated), stage='output',
            retained_previous=True,
            offers=['narrower_scope', 'simpler_mode', 'retry'])
    names = {'polygons': 'polygons', 'points': 'points',
             'selected_cells': 'selected cells',
             'exact_cells': 'cells needing the exact test',
             'output_bytes': 'bytes of output'}
    about = 'about ' if estimated else ''
    return SectionRefused(
        'over_budget',
        f'the section would draw {about}{measured:,} '
        f'{names.get(quantity, quantity)}; the limit is {limit:,} '
        f'(max_{"points_out" if quantity == "points" else quantity}). The '
        f'previous section is kept: narrow the planes, choose a simpler '
        f'mode, or retry with a higher limit.',
        quantity=quantity, limit=int(limit), measured=int(measured),
        estimated=bool(estimated), stage='output', retained_previous=True,
        offers=['narrower_scope', 'simpler_mode', 'retry'])
