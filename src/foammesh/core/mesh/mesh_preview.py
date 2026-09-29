"""The mesh preview, built out of the window (Plan 35 CR3 steps 1-4).

The viewport used to read a mesh by constructing ``vtkPOpenFOAMReader`` in the
GUI process and asking it for the whole volume. On a mesh a few million cells
large that is gigabytes held by the window -- the thing CR2 moved the checks
away from. The picture now comes from a worker (``foammesh.workers``):

* **By default the preview is the boundary surface**, read by CR2's bounded
  boundary-only reader (:func:`read_poly_mesh_boundary`): the internal faces
  of the ``faces`` file are counted, not parsed, and owner and neighbour are
  not read at all. The worker writes one ``.vtp`` -- every patch and face
  zone, their feature edges, and which range of it is which part -- into the
  case's ``foammesh/cache/``. The window only reads that file back.
* **Below** :data:`VOLUME_AUTO_MAX_CELLS` cells (D7) the worker also reads the
  volume, the way the viewport always did, and writes it as ``.vtu``; above
  it, "Load full volume" asks for that, up to
  ``resource_budget.FULL_VOLUME_MAX_CELLS``.
* A surface above ``PREVIEW_MAX_TRIANGLES`` triangles, or one whose file would
  exceed ``PREVIEW_MAX_BYTES``, is decimated in the worker
  (``vtkQuadricDecimation``) and says so.
* A mesh the fast reader cannot read -- binary, decomposed, several regions,
  a moving mesh's time directories -- is read by the same ``vtkPOpenFOAMReader``
  the viewport always used, in the worker rather than in the window.

The feature edges each actor draws round itself (step 10) are computed here
too, with the settings :data:`FEATURE_ANGLE` names, so the window's pipeline
does not run ``vtkGeometryFilter`` or ``vtkFeatureEdges`` on a whole mesh the
first time it paints.

Nothing at module scope imports Qt or VTK; the helpers the window uses to
decide *whether* to start a worker are plain file reads.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import numpy as np

from foammesh.core.mesh.poly_mesh_boundary import (
    PolyMeshReadError, _raw_member, _close_raw, _read_boundary, _numbers,
    _SCAN_CHUNK, read_counts, read_face_zone_surfaces,
    read_poly_mesh_boundary,
)

OPERATION = 'mesh.preview'
VOLUME_OPERATION = 'mesh.volume'
OPERATIONS = (OPERATION, VOLUME_OPERATION)

#: D7. The volume is read by default only below this many cells.
VOLUME_AUTO_MAX_CELLS = 200_000
#: Must equal ``actor_info.SILHOUETTE_FEATURE_ANGLE``: the precomputed edges
#: stand in for the ones the actor would have computed itself.
FEATURE_ANGLE = 30.0
#: A part this small is left alone when the surface is decimated; it costs
#: nothing and decimating it can erase it.
DECIMATE_MIN_TRIANGLES = 1_000
#: Files a crashed or abandoned preview left in the cache go after this long.
STALE_SECONDS = 3600.0

PROCESSOR_TYPES = ('processor', 'processorCyclic')
FILE_PREFIX = 'preview-'


class PreviewRefused(Exception):
    """The worker would not write the preview; ``reason`` says why."""

    def __init__(self, reason: str, message: str, **details):
        super().__init__(message)
        self.reason = reason
        self.details = details


# --------------------------------------------------------------------------- #
# Where the mesh is, and how big (window side; no VTK)
# --------------------------------------------------------------------------- #

def cache_dir(case) -> Path:
    return Path(case) / 'foammesh' / 'cache'


def poly_mesh_directories(root) -> list:
    """Every polyMesh under one case root: ``constant`` and time directories."""
    root = Path(root)
    found = [root / 'constant' / 'polyMesh']
    found.extend(sorted(root.glob('[0-9]*/polyMesh')))
    return [path for path in found if path.is_dir()]


def mesh_written_at(root):
    """When the newest mesh under ``root`` was written (its ``faces``), or None."""
    newest = None
    for directory in poly_mesh_directories(root):
        for name in ('faces', 'faces.gz'):
            faces = directory / name
            if not faces.is_file():
                continue
            written = faces.stat().st_mtime
            if newest is None or written > newest:
                newest = written
    return newest


def case_layout(case) -> str | None:
    """``'reconstructed'``, ``'decomposed'`` or None when there is no mesh.

    The reconstructed mesh wins unless it is missing or older than the
    processor mesh (DP-140): see ``PolyMeshLoader._caseType``, which asks
    this.
    """
    case = Path(case)
    processor = case / 'processor0'
    if not poly_mesh_directories(processor):
        return 'reconstructed' if poly_mesh_directories(case) else None
    if not poly_mesh_directories(case):
        return 'decomposed'
    reconstructed = mesh_written_at(case)
    decomposed = mesh_written_at(processor)
    if reconstructed is None:
        return 'decomposed'
    if decomposed is not None and decomposed > reconstructed:
        return 'decomposed'
    return 'reconstructed'


def _ranks(case: Path) -> list:
    return sorted(path for path in case.glob('processor[0-9]*')
                  if (path / 'constant' / 'polyMesh').is_dir())


def _face_zone_bytes(mesh: Path) -> int:
    for name in ('faceZones', 'faceZones.gz'):
        path = mesh / name
        if path.is_file():
            return path.stat().st_size
    return 0


def case_counts(case, layout: str | None = None) -> dict | None:
    """Header counts of the mesh the preview would draw, or None.

    A decomposed case is the sum of its ranks, less the processor patches,
    which are not boundary. Only headers and the boundary tables are read.
    """
    case = Path(case)
    layout = layout or case_layout(case)
    try:
        if layout == 'reconstructed':
            counts = read_counts(case).to_dict()
            counts['face_zone_bytes'] = _face_zone_bytes(
                case / 'constant' / 'polyMesh')
            counts['layout'] = layout
            return counts
        if layout != 'decomposed':
            return None
        total = {'points': 0, 'faces': 0, 'internal_faces': 0, 'cells': 0,
                 'boundary_faces': 0, 'file_bytes': {}, 'formats': {},
                 'face_zone_bytes': 0, 'layout': layout}
        patches: dict[str, int] = {}
        ranks = _ranks(case)
        if not ranks:
            return None
        for rank in ranks:
            counts = read_counts(rank)
            total['points'] += counts.points
            total['faces'] += counts.faces
            total['internal_faces'] += counts.internal_faces
            if counts.cells is None or total['cells'] is None:
                total['cells'] = None
            else:
                total['cells'] += counts.cells
            for key, value in counts.file_bytes.items():
                total['file_bytes'][key] = total['file_bytes'].get(key, 0) + value
            for key, value in counts.formats.items():
                total['formats'].setdefault(key, value)
                if value != 'ascii':
                    total['formats'][key] = value
            mesh = rank / 'constant' / 'polyMesh'
            total['face_zone_bytes'] += _face_zone_bytes(mesh)
            for patch in _read_boundary(mesh):
                if patch.patch_type in PROCESSOR_TYPES:
                    continue
                patches[patch.name] = patches.get(patch.name, 0) + patch.n_faces
        total['patches'] = [[name, count] for name, count in patches.items()]
        total['boundary_faces'] = int(sum(patches.values()))
        total['path'] = str(case)
        return total
    except (PolyMeshReadError, OSError, ValueError):
        return None


def estimated_cells(counts: dict | None) -> int | None:
    """Cells from the owner note, or from the face counts when it is absent."""
    if not counts:
        return None
    if counts.get('cells') is not None:
        return int(counts['cells'])
    internal = int(counts.get('internal_faces') or 0)
    boundary = int(counts.get('boundary_faces') or 0)
    if internal + boundary <= 0:
        return None
    # Every internal face bounds two cells, every boundary face one; a
    # hexahedron has six. Only used to choose between two ways of drawing.
    return max(1, (2 * internal + boundary) // 6)


def bounded_readable(case, time_value=0, counts: dict | None = None) -> bool:
    """Whether the bounded boundary reader can draw this case.

    It reads ``constant/polyMesh`` of a reconstructed single-region ASCII
    case. Anything else -- a decomposed case, several regions, a mesh a run
    wrote into a time directory, binary lists -- goes to the VTK reader, in
    the worker, exactly as the viewport used to read it.
    """
    case = Path(case)
    if counts is None or counts.get('layout') != 'reconstructed':
        return False
    if any(value != 'ascii' for value in (counts.get('formats') or {}).values()):
        return False
    if list(case.glob('[0-9]*/polyMesh')):
        return False
    constant = case / 'constant'
    if any(path.is_dir() for path in constant.glob('*/polyMesh')):
        return False
    return True


def read_points_bounds(case) -> tuple | None:
    """``(xmin, xmax, ymin, ymax, zmin, zmax)`` of the mesh, streamed.

    The outline shown when no preview could be built. One chunk of the
    ``points`` file is parsed at a time and reduced, so this costs a chunk
    whatever the mesh's size. None for binary or unreadable points.
    """
    case = Path(case)
    layout = case_layout(case)
    roots = _ranks(case) if layout == 'decomposed' else [case]
    low = np.full(3, np.inf)
    high = np.full(3, -np.inf)
    for root in roots:
        mesh = root / 'constant' / 'polyMesh'
        try:
            buffer, stream, open_at, count = _raw_member(mesh, 'points')
        except (PolyMeshReadError, OSError, ValueError):
            return None
        try:
            close_at = buffer.rfind(b')')
            if close_at < open_at:
                close_at = len(buffer)
            carry = np.empty(0)
            position = open_at + 1
            while position < close_at:
                end = min(close_at, position + _SCAN_CHUNK)
                if end < close_at:
                    newline = buffer.rfind(b'\n', position, end)
                    if newline > position:
                        end = newline
                chunk = bytes(buffer[position:end])
                if b'/*' in chunk or b'//' in chunk:
                    return None
                values = np.concatenate([carry, _numbers(chunk, np.float64)])
                whole = values.size - values.size % 3
                carry = values[whole:]
                if whole:
                    block = values[:whole].reshape(-1, 3)
                    low = np.minimum(low, block.min(axis=0))
                    high = np.maximum(high, block.max(axis=0))
                position = end
        except (PolyMeshReadError, ValueError):
            return None
        finally:
            _close_raw(buffer, stream)
    if not np.all(np.isfinite(low)):
        return None
    return (float(low[0]), float(high[0]), float(low[1]), float(high[1]),
            float(low[2]), float(high[2]))


# --------------------------------------------------------------------------- #
# The worker's side
# --------------------------------------------------------------------------- #

def _poly_data(points, face_vertices, face_offsets):
    """A polygon ``vtkPolyData`` from compacted numpy arrays (VTK 9 cells)."""
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    data = vtkPolyData()
    node = vtkPoints()
    node.SetDataTypeToFloat()
    node.SetData(numpy_to_vtk(
        np.ascontiguousarray(points, dtype=np.float32).reshape(-1, 3), deep=True))
    data.SetPoints(node)
    cells = vtkCellArray()
    cells.SetData(
        numpy_to_vtkIdTypeArray(
            np.ascontiguousarray(face_offsets, dtype=np.int64), deep=True),
        numpy_to_vtkIdTypeArray(
            np.ascontiguousarray(face_vertices, dtype=np.int64), deep=True))
    data.SetPolys(cells)
    return data


def _compact(points, face_vertices, face_offsets, first: int, last: int):
    """Faces ``first:last`` of a face list, with only the points they use."""
    offsets = np.asarray(face_offsets[first:last + 1], dtype=np.int64)
    vertices = face_vertices[offsets[0]:offsets[-1]]
    used = np.unique(vertices)
    return (points[used], np.searchsorted(used, vertices).astype(np.int64),
            offsets - offsets[0])


def _bounded_parts(case: Path) -> list:
    """``[(region, category, name, polydata), ...]`` from the bounded reader."""
    mesh = read_poly_mesh_boundary(case, layout_expectation='any')
    parts = []
    for patch in mesh.patches:
        if patch.patch_type in PROCESSOR_TYPES:
            continue
        first = patch.start_face - mesh.face_base
        if patch.n_faces:
            points, vertices, offsets = _compact(
                mesh.points, mesh.face_vertices, mesh.face_offsets,
                first, first + patch.n_faces)
        else:
            points = np.empty((0, 3))
            vertices = np.empty(0, dtype=np.int64)
            offsets = np.zeros(1, dtype=np.int64)
        parts.append(('', 'boundary', patch.name,
                      _poly_data(points, vertices, offsets)))
    del mesh
    try:
        zones = read_face_zone_surfaces(case)
    except (PolyMeshReadError, OSError, ValueError):
        zones = ()
    for zone in zones:
        if zone.face_count == 0:
            continue
        parts.append(('', 'faceZones', zone.name, _poly_data(
            zone.points, zone.face_vertices, zone.face_offsets)))
    return parts


def _vtk_scene(case: Path, time_value, *, boundary_only: bool,
               region_seeds=()) -> dict | None:
    """The reader's own scene, read in this (worker) process."""
    from foammesh.openfoam.poly_mesh.poly_mesh_loader import PolyMeshLoader

    loader = PolyMeshLoader(case / 'case.foam', region_seeds)
    try:
        return loader.readNow(time_value, boundaryOnly=boundary_only)
    finally:
        loader.dispose()


def _scene_parts(scene: dict) -> tuple[list, list]:
    """Split a reader scene into surface parts and volume parts, in order."""
    surfaces, volumes = [], []
    for region, body in (scene or {}).items():
        if not isinstance(body, dict):
            continue
        for name, data in (body.get('boundary') or {}).items():
            surfaces.append((region, 'boundary', name, data))
        internal = body.get('internalMesh')
        if internal is not None and hasattr(internal, 'GetNumberOfCells'):
            volumes.append((region, 'internalMesh', '', internal))
        zones = body.get('zones') or {}
        if not isinstance(zones, dict):
            continue
        for category in ('cellZones', 'faceZones', 'regions'):
            collection = zones.get(category) or {}
            if not isinstance(collection, dict):
                continue
            for name, data in collection.items():
                if not hasattr(data, 'GetNumberOfCells'):
                    continue
                if category == 'faceZones':
                    surfaces.append((region, category, name, data))
                else:
                    volumes.append((region, category, name, data))
    return surfaces, volumes


def _as_polydata(data):
    """A reader part as polydata: a face zone can arrive as a grid."""
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

    if isinstance(data, vtkPolyData):
        return data
    surface = vtkGeometryFilter()
    surface.SetInputData(data)
    surface.Update()
    return surface.GetOutput()


def feature_edges(polydata):
    """The outline an actor draws round ``polydata`` (``ActorInfo`` settings)."""
    from vtkmodules.vtkFiltersCore import vtkFeatureEdges

    edges = vtkFeatureEdges()
    edges.BoundaryEdgesOn()
    edges.FeatureEdgesOn()
    edges.SetFeatureAngle(FEATURE_ANGLE)
    edges.ManifoldEdgesOff()
    edges.NonManifoldEdgesOff()
    edges.ColoringOff()
    edges.SetInputData(polydata)
    edges.Update()
    return edges.GetOutput()


def exterior_surface(grid):
    """What ``MeshActor``'s ``vtkGeometryFilter`` sends to the renderer."""
    from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

    surface = vtkGeometryFilter()
    surface.SetInputData(grid)
    surface.Update()
    return surface.GetOutput()


def _triangles(polydata) -> int:
    """Triangles a polygon surface becomes: sum of (n - 2) over its faces."""
    polys = polydata.GetPolys()
    if polys is None or polys.GetNumberOfCells() == 0:
        return 0
    cells = polys.GetNumberOfCells()
    return int(polys.GetNumberOfConnectivityIds() - 2 * cells)


def _surface_bytes(parts) -> int:
    """Bytes the packed file takes for these parts, before the edges."""
    total = 0
    for *_key, data in parts:
        polys = data.GetPolys()
        total += 12 * data.GetNumberOfPoints()
        if polys is not None:
            total += 8 * (polys.GetNumberOfConnectivityIds()
                          + polys.GetNumberOfCells() + 1)
    return total


def _decimate(data, reduction: float):
    from vtkmodules.vtkFiltersCore import vtkQuadricDecimation, vtkTriangleFilter

    triangles = vtkTriangleFilter()
    triangles.SetInputData(data)
    decimate = vtkQuadricDecimation()
    decimate.SetInputConnection(triangles.GetOutputPort())
    decimate.SetTargetReduction(float(min(max(reduction, 0.0), 0.99)))
    decimate.VolumePreservationOn()
    decimate.Update()
    out = decimate.GetOutput()
    result = type(out)()
    result.ShallowCopy(out)
    return result


def decimate_parts(parts: list, *, max_triangles: int, max_bytes: int):
    """Decimate the parts to fit the preview caps; ``(parts, decimated)``."""
    total = sum(_triangles(data) for *_key, data in parts)
    size = _surface_bytes(parts)
    if total <= max_triangles and size <= max_bytes:
        return parts, False
    # Each triangle of the output costs about 12 bytes of point and 32 of
    # cell (a decimated surface has half as many points as triangles).
    wanted = min(max_triangles, max(1, int(max_bytes / 38)))
    keep = wanted / max(total, 1)
    decimated = []
    for region, category, name, data in parts:
        if _triangles(data) > DECIMATE_MIN_TRIANGLES:
            data = _decimate(data, 1.0 - keep)
        decimated.append((region, category, name, data))
    return decimated, True


def _write_xml(data, path: Path) -> int:
    from vtkmodules.vtkIOXML import (
        vtkXMLPolyDataWriter, vtkXMLUnstructuredGridWriter)
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    writer = (vtkXMLPolyDataWriter() if isinstance(data, vtkPolyData)
              else vtkXMLUnstructuredGridWriter())
    temporary = path.with_name(path.name + '.part')
    writer.SetFileName(str(temporary))
    writer.SetInputData(data)
    writer.SetDataModeToAppended()
    writer.EncodeAppendedDataOff()
    writer.SetCompressorTypeToNone()
    writer.SetHeaderTypeToUInt64()
    if not writer.Write():
        temporary.unlink(missing_ok=True)
        raise OSError(f'could not write {path}')
    os.replace(temporary, path)
    return path.stat().st_size


def pack_parts(parts: list, edges: list):
    """One polydata holding every part and its edges, and the part table.

    Points are the parts' points then the edges' points; polygons are the
    parts' faces in order; lines are the edges'. Field data
    ``partRanges`` holds, per part, ``(point start, point count, poly start,
    poly count, edge point start, edge point count, line start, line
    count)``; ``partRegion``, ``partCategory`` and ``partName`` name it. Cell
    data ``patchId`` is the part's index on its polygons, -1 on lines.
    """
    from vtkmodules.util.numpy_support import (
        numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy)
    from vtkmodules.vtkCommonCore import vtkPoints, vtkStringArray
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    def arrays(cells):
        if cells is None or cells.GetNumberOfCells() == 0:
            return np.zeros(1, np.int64), np.empty(0, np.int64)
        return (vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64),
                vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64))

    def coords(data):
        if data.GetNumberOfPoints() == 0:
            return np.empty((0, 3), np.float32)
        return vtk_to_numpy(data.GetPoints().GetData()).astype(
            np.float32).reshape(-1, 3)

    point_blocks, poly_off, poly_conn = [], [np.zeros(1, np.int64)], []
    line_off, line_conn = [np.zeros(1, np.int64)], []
    ranges = np.zeros((len(parts), 8), dtype=np.int64)
    point_at = poly_at = conn_at = 0
    for index, (_r, _c, _n, data) in enumerate(parts):
        xyz = coords(data)
        offsets, connectivity = arrays(data.GetPolys())
        ranges[index, 0:4] = (point_at, len(xyz), poly_at, offsets.size - 1)
        point_blocks.append(xyz)
        poly_off.append(offsets[1:] + conn_at)
        poly_conn.append(connectivity + point_at)
        point_at += len(xyz)
        poly_at += offsets.size - 1
        conn_at += connectivity.size
    line_at = line_conn_at = 0
    for index, data in enumerate(edges):
        xyz = coords(data)
        offsets, connectivity = arrays(data.GetLines())
        ranges[index, 4:8] = (point_at, len(xyz), line_at, offsets.size - 1)
        point_blocks.append(xyz)
        line_off.append(offsets[1:] + line_conn_at)
        line_conn.append(connectivity + point_at)
        point_at += len(xyz)
        line_at += offsets.size - 1
        line_conn_at += connectivity.size

    packed = vtkPolyData()
    node = vtkPoints()
    node.SetDataTypeToFloat()
    xyz = (np.concatenate(point_blocks) if point_blocks
           else np.empty((0, 3), np.float32))
    node.SetData(numpy_to_vtk(np.ascontiguousarray(xyz), deep=True))
    packed.SetPoints(node)
    for setter, offsets, connectivity in (
            (packed.SetPolys, poly_off, poly_conn),
            (packed.SetLines, line_off, line_conn)):
        cells = vtkCellArray()
        cells.SetData(
            numpy_to_vtkIdTypeArray(np.concatenate(offsets), deep=True),
            numpy_to_vtkIdTypeArray(
                np.concatenate(connectivity) if connectivity
                else np.empty(0, np.int64), deep=True))
        setter(cells)
    ids = np.concatenate([
        np.full(line_at, -1, np.int32),
        np.repeat(np.arange(len(parts), dtype=np.int32), ranges[:, 3])])
    patch_id = numpy_to_vtk(ids, deep=True)
    patch_id.SetName('patchId')
    packed.GetCellData().AddArray(patch_id)
    field = packed.GetFieldData()
    table = numpy_to_vtk(ranges.reshape(-1), deep=True)
    table.SetName('partRanges')
    field.AddArray(table)
    for label, column in (('partRegion', 0), ('partCategory', 1),
                          ('partName', 2)):
        names = vtkStringArray()
        names.SetName(label)
        for part in parts:
            names.InsertNextValue(str(part[column]))
        field.AddArray(names)
    return packed


def _prune(directory: Path) -> None:
    now = time.time()
    for path in directory.glob(f'{FILE_PREFIX}*'):
        try:
            if now - path.stat().st_mtime > STALE_SECONDS:
                path.unlink()
        except OSError:
            pass


def _bounds(parts) -> list | None:
    low = np.full(3, np.inf)
    high = np.full(3, -np.inf)
    for *_key, data in parts:
        if data.GetNumberOfPoints() == 0:
            continue
        b = data.GetBounds()
        low = np.minimum(low, b[0::2])
        high = np.maximum(high, b[1::2])
    if not np.all(np.isfinite(low)):
        return None
    return [float(low[0]), float(high[0]), float(low[1]), float(high[1]),
            float(low[2]), float(high[2])]


def build_preview(operation: str, args: dict) -> dict:
    """The worker body: read, decimate, pack, write; returns the payload."""
    from foammesh.support import resource_budget as budget

    case = Path(args['case'])
    time_value = args.get('time', 0)
    volume = operation == VOLUME_OPERATION
    out_dir = Path(args.get('out_dir') or cache_dir(case))
    stem = str(args.get('stem') or f'{FILE_PREFIX}{uuid.uuid4().hex}')
    max_triangles = int(args.get('max_triangles')
                        or budget.PREVIEW_MAX_TRIANGLES)
    max_bytes = int(args.get('max_bytes') or (
        budget.FULL_VOLUME_MAX_BYTES if volume else budget.PREVIEW_MAX_BYTES))
    max_cells = int(args.get('max_cells') or budget.FULL_VOLUME_MAX_CELLS)
    seeds = [(name, point) for name, point in (args.get('region_seeds') or ())]

    layout = case_layout(case)
    payload = {'empty': True, 'mode': 'volume' if volume else 'surface',
               'layout': layout, 'source': '', 'surface': None,
               'volumes': [], 'decimated': False}
    if layout is None:
        return payload
    counts = case_counts(case, layout)
    payload['counts'] = counts
    cells = estimated_cells(counts)
    if volume and cells is not None and cells > max_cells:
        raise PreviewRefused(
            'volume_too_large',
            f'the volume has {cells:,} cells; the viewport draws at most '
            f'{max_cells:,}', cells=cells, max_cells=max_cells)

    volumes = []
    bounded_error = None
    if not volume and bounded_readable(case, time_value, counts):
        try:
            surfaces = _bounded_parts(case)
            payload['source'] = 'bounded'
        except PolyMeshReadError as error:
            bounded_error = error
            surfaces = None
    else:
        surfaces = None
    if surfaces is None:
        scene = _vtk_scene(case, time_value, boundary_only=not volume,
                           region_seeds=seeds)
        if scene is None:
            return payload
        surfaces, volumes = _scene_parts(scene)
        surfaces = [(r, c, n, _as_polydata(d)) for r, c, n, d in surfaces]
        payload['source'] = 'vtk'
        if volume:
            cells = sum(data.GetNumberOfCells() for r, c, n, data in volumes
                        if c == 'internalMesh')
            if cells > max_cells:
                raise PreviewRefused(
                    'volume_too_large',
                    f'the volume has {cells:,} cells; the viewport draws at '
                    f'most {max_cells:,}', cells=cells, max_cells=max_cells)
        else:
            volumes = []
    if (counts or {}).get('boundary_faces') and not any(
            d.GetNumberOfCells() for *_k, d in surfaces):
        # K7: the headers promise faces and neither reader produced one -- a
        # truncated or corrupt list. Say so; an empty picture says nothing.
        why = bounded_error or 'the reader produced no faces'
        raise PreviewRefused(
            'unreadable', f'the mesh files could not be read ({why})')
    payload['empty'] = False
    payload['source_faces'] = {f'{r}\0{c}\0{n}': int(d.GetNumberOfCells())
                               for r, c, n, d in surfaces}
    payload['triangles'] = int(sum(_triangles(d) for *_k, d in surfaces))
    if not volume:
        surfaces, payload['decimated'] = decimate_parts(
            surfaces, max_triangles=max_triangles, max_bytes=max_bytes)
    payload['displayed_faces'] = int(sum(d.GetNumberOfCells()
                                         for *_k, d in surfaces))
    payload['bounds'] = _bounds(surfaces)

    # The volumes' exteriors are drawn as surfaces too (step 10): the actor
    # would otherwise run vtkGeometryFilter over the whole grid on first paint.
    parts = list(surfaces)
    for region, category, name, grid in volumes:
        parts.append((region, f'exterior:{category}', name,
                      exterior_surface(grid)))
    edges = [feature_edges(data) for *_key, data in parts]

    out_dir.mkdir(parents=True, exist_ok=True)
    _prune(out_dir)
    written = []
    try:
        surface_path = out_dir / f'{stem}.vtp'
        surface_bytes = _write_xml(pack_parts(parts, edges), surface_path)
        written.append(surface_path)
        total = surface_bytes
        volume_docs = []
        for index, (region, category, name, grid) in enumerate(volumes):
            path = out_dir / f'{stem}-v{index}.vtu'
            size = _write_xml(grid, path)
            written.append(path)
            total += size
            volume_docs.append({'region': region, 'category': category,
                                'name': name, 'path': str(path),
                                'bytes': size,
                                'cells': int(grid.GetNumberOfCells())})
        if total > max_bytes:
            raise PreviewRefused(
                'volume_too_large' if volume else 'preview_too_large',
                f'the preview files take {total:,} bytes; the limit is '
                f'{max_bytes:,}', bytes=total, max_bytes=max_bytes)
    except BaseException:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    payload['surface'] = {
        'path': str(surface_path), 'bytes': surface_bytes,
        'parts': [{'region': r, 'category': c, 'name': n,
                   'faces': int(d.GetNumberOfCells())}
                  for r, c, n, d in parts]}
    payload['volumes'] = volume_docs
    return payload


# --------------------------------------------------------------------------- #
# The window's side: read the worker's files back
# --------------------------------------------------------------------------- #

def unpack_parts(packed) -> list:
    """``[(region, category, name, surface, edges), ...]`` from a packed file."""
    from vtkmodules.util.numpy_support import (
        numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy)
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    field = packed.GetFieldData()
    table = field.GetAbstractArray('partRanges')
    if table is None:
        return []
    ranges = vtk_to_numpy(table).astype(np.int64).reshape(-1, 8)
    labels = [field.GetAbstractArray(name)
              for name in ('partRegion', 'partCategory', 'partName')]
    xyz = (vtk_to_numpy(packed.GetPoints().GetData()).reshape(-1, 3)
           if packed.GetNumberOfPoints() else np.empty((0, 3), np.float32))

    def cell_arrays(cells):
        if cells is None or cells.GetNumberOfCells() == 0:
            return np.zeros(1, np.int64), np.empty(0, np.int64)
        return (vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64),
                vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64))

    poly_off, poly_conn = cell_arrays(packed.GetPolys())
    line_off, line_conn = cell_arrays(packed.GetLines())

    def piece(point_start, point_count, cell_start, cell_count,
              offsets, connectivity, lines):
        data = vtkPolyData()
        node = vtkPoints()
        node.SetDataTypeToFloat()
        node.SetData(numpy_to_vtk(np.ascontiguousarray(
            xyz[point_start:point_start + point_count]), deep=True))
        data.SetPoints(node)
        local = offsets[cell_start:cell_start + cell_count + 1]
        conn = connectivity[local[0]:local[-1]] - point_start
        cells = vtkCellArray()
        cells.SetData(
            numpy_to_vtkIdTypeArray(np.ascontiguousarray(local - local[0]),
                                    deep=True),
            numpy_to_vtkIdTypeArray(np.ascontiguousarray(conn), deep=True))
        (data.SetLines if lines else data.SetPolys)(cells)
        return data

    parts = []
    for index, row in enumerate(ranges):
        surface = piece(row[0], row[1], row[2], row[3], poly_off, poly_conn,
                        False)
        edges = piece(row[4], row[5], row[6], row[7], line_off, line_conn,
                      True)
        parts.append((labels[0].GetValue(index), labels[1].GetValue(index),
                      labels[2].GetValue(index), surface, edges))
    return parts


def read_surface_file(path):
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader

    reader = vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    return unpack_parts(reader.GetOutput())


def read_volume_file(path):
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    result = type(grid)()
    result.ShallowCopy(grid)
    return result


def scene_from_payload(payload: dict) -> tuple[dict, dict]:
    """``(scene, precomputed)`` -- the reader-shaped scene and, per part key
    ``(region, category, name)``, ``{'edges': ..., 'surface': ...}``.

    Blocking VTK reads; call it on the VTK thread.
    """
    scene: dict = {}
    precomputed: dict = {}

    def region(name):
        return scene.setdefault(name, {
            'boundary': {}, 'internalMesh': None,
            'zones': {'cellZones': {}, 'faceZones': {}, 'regions': {}}})

    surface = payload.get('surface') or {}
    exteriors = {}
    if surface.get('path'):
        for rname, category, name, data, edges in read_surface_file(
                surface['path']):
            body = region(rname)
            if category == 'boundary':
                body['boundary'][name] = data
                precomputed[(rname, category, name)] = {'edges': edges}
            elif category == 'faceZones':
                body['zones']['faceZones'][name] = data
                precomputed[(rname, category, name)] = {'edges': edges}
            elif category.startswith('exterior:'):
                exteriors[(rname, category.split(':', 1)[1], name)] = {
                    'surface': data, 'edges': edges}
    for item in payload.get('volumes') or ():
        grid = read_volume_file(item['path'])
        body = region(item['region'])
        key = (item['region'], item['category'], item['name'])
        if item['category'] == 'internalMesh':
            body['internalMesh'] = grid
        else:
            body['zones'].setdefault(item['category'], {})[item['name']] = grid
        if key in exteriors:
            precomputed[key] = exteriors[key]
    return scene, precomputed


def payload_files(payload: dict) -> list:
    """``[(path, bytes the worker said it wrote), ...]`` of one preview."""
    files = []
    surface = (payload or {}).get('surface') or {}
    if surface.get('path'):
        files.append((Path(surface['path']), int(surface.get('bytes') or 0)))
    for item in (payload or {}).get('volumes') or ():
        files.append((Path(item['path']), int(item.get('bytes') or 0)))
    return files


def remove_files(payload: dict) -> None:
    """Delete one preview's files, and the cache folders if left empty.

    Only files a preview can have written -- ``preview-*`` in a ``cache``
    folder -- are touched, whatever path the payload names.
    """
    parents = set()
    for path, _size in payload_files(payload):
        if (not path.name.startswith(FILE_PREFIX)
                or path.parent.name != 'cache'):
            continue
        parents.add(path.parent)
        try:
            path.unlink()
        except OSError:
            pass
    for parent in parents:
        for folder in (parent, parent.parent):
            if folder.name not in ('cache', 'foammesh'):
                break
            try:
                folder.rmdir()
            except OSError:
                break
