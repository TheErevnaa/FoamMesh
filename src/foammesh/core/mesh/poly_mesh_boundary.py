"""Qt-free reader for an OpenFOAM ``constant/polyMesh``.

Plan 23 WP1. Everything the geometry-fidelity checker measures is a property of
the mesh the solver will actually receive, so the checker has to read that mesh
in a CLI, API and CI process where Qt is never initialised.

The only polyMesh reader in the tree before this one is
:class:`~foammesh.openfoam.poly_mesh.poly_mesh_loader.PolyMeshLoader`, which is a
``QObject`` built for rendering: it imports ``PySide6.QtCore`` at module scope
and hands back actors. It cannot be used headlessly and it does not expose the
per-patch face identity the checker joins on.

What this module deliberately does **not** do:

* **Region directories.** FoamMesh publishes one ``constant/polyMesh`` and
  encodes regions as ``cellZones`` -- see the writer's ``_write_cell_zones``.
  ``constant/<region>/polyMesh`` only arises for an imported external case,
  which Plan 23 §16.5 places outside v1 fidelity scope.
* **Binary payloads.** Our own writer emits ASCII only, but snappyHexMesh
  honours the case's ``writeFormat``. A binary file is refused by name
  (``binary_format``) so a caller can normalise it through ``foamFormatConvert``
  (§16.4). Guessing at binary would silently mis-parse it, which is worse than
  refusing.
* **Decomposed cases.** ``layout_expectation`` defaults to ``'reconstructed'``.
  A ``processor*/`` layout is refused rather than reading rank 0 and calling it
  the mesh.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
from pathlib import Path
import re

import numpy as np


class PolyMeshReadError(ValueError):
    """A polyMesh could not be read.

    ``reason`` is a stable machine token so a caller can distinguish "this needs
    ``foamFormatConvert``" from "this is not a mesh", rather than matching on
    prose.
    """

    def __init__(self, reason: str, message: str, *, path: Path | None = None):
        super().__init__(message)
        self.reason = reason
        self.path = Path(path) if path is not None else None


#: Files a complete polyMesh must contain. ``neighbour`` is included because a
#: mesh with no internal faces is not a volume mesh, and its absence has meant a
#: half-written directory every time it has come up.
REQUIRED = ('points', 'faces', 'owner', 'neighbour', 'boundary')
#: Read when present, absent without complaint. Our writer emits ``cellZones``
#: only for multi-region meshes and never emits ``faceZones``.
OPTIONAL = ('cellZones', 'faceZones')

_COMMENT_BLOCK = re.compile(rb'/\*.*?\*/', re.DOTALL)
_COMMENT_LINE = re.compile(rb'//[^\n]*')
_FOAMFILE = re.compile(rb'FoamFile\s*\{(.*?)\}', re.DOTALL)
_ENTRY = re.compile(rb'(\w+)\s+([^;]+);')
#: ``name { ... }`` for one boundary patch or zone.
_NAMED_DICT = re.compile(rb'([A-Za-z_][\w.\-]*)\s*\{(.*?)\}', re.DOTALL)


@dataclass(frozen=True)
class BoundaryPatch:
    """One entry of ``constant/polyMesh/boundary``."""

    name: str
    patch_type: str
    start_face: int
    n_faces: int
    #: ``cyclic`` patches only; the solver name of the coupled patch.
    neighbour_patch: str | None = None
    #: Every other key in the patch dict, so nothing is silently dropped.
    entries: dict = field(default_factory=dict)

    @property
    def end_face(self) -> int:
        return self.start_face + self.n_faces

    def to_dict(self) -> dict:
        return {
            'name': self.name, 'type': self.patch_type,
            'start_face': self.start_face, 'n_faces': self.n_faces,
            'neighbour_patch': self.neighbour_patch,
        }


@dataclass(frozen=True)
class Zone:
    """A ``cellZones`` or ``faceZones`` entry."""

    name: str
    zone_type: str
    labels: np.ndarray

    def to_dict(self) -> dict:
        return {'name': self.name, 'type': self.zone_type,
                'count': int(self.labels.size)}


@dataclass(frozen=True)
class PolyMesh:
    """A read ``constant/polyMesh``, in mesh terms rather than render terms."""

    path: Path
    points: np.ndarray            # (n_points, 3) float64
    face_vertices: np.ndarray     # flat int64 vertex ids
    face_offsets: np.ndarray      # (n_faces + 1,) int64 into face_vertices
    owner: np.ndarray             # (n_faces,) int64
    neighbour: np.ndarray         # (n_internal_faces,) int64
    patches: tuple[BoundaryPatch, ...]
    cell_zones: tuple[Zone, ...] = ()
    face_zones: tuple[Zone, ...] = ()

    @property
    def face_count(self) -> int:
        return int(self.face_offsets.size - 1)

    @property
    def internal_face_count(self) -> int:
        return int(self.neighbour.size)

    @property
    def cell_count(self) -> int:
        """Cells are implied by the highest addressed cell, as in OpenFOAM."""
        if self.owner.size == 0:
            return 0
        return int(max(self.owner.max(), self.neighbour.max(initial=-1))) + 1

    def patch(self, name: str) -> BoundaryPatch:
        for item in self.patches:
            if item.name == name:
                return item
        raise KeyError(f'no such boundary patch: {name}')

    def face(self, face_id: int) -> np.ndarray:
        start = self.face_offsets[face_id]
        return self.face_vertices[start:self.face_offsets[face_id + 1]]

    def patch_face_ids(self, patch: BoundaryPatch | str) -> np.ndarray:
        item = self.patch(patch) if isinstance(patch, str) else patch
        return np.arange(item.start_face, item.end_face, dtype=np.int64)


# --------------------------------------------------------------------------- #
# File access
# --------------------------------------------------------------------------- #

def _resolve(path: str | Path, *, layout_expectation: str) -> Path:
    """Return the polyMesh directory, refusing layouts v1 does not qualify."""
    path = Path(path)
    mesh = path if path.name == 'polyMesh' else path / 'constant' / 'polyMesh'

    if layout_expectation == 'reconstructed':
        # A decomposed case has the real mesh only in the union of the ranks.
        # Reading processor0 would silently qualify one sixteenth of a mesh.
        root = path if path.name != 'polyMesh' else path.parent.parent
        if not mesh.is_dir() and root.is_dir():
            ranks = sorted(root.glob('processor[0-9]*'))
            if ranks:
                raise PolyMeshReadError(
                    'decomposed_layout',
                    f'{root} holds a decomposed case ({len(ranks)} ranks) and no '
                    'reconstructed constant/polyMesh; reconstruct it before '
                    'qualification, or pass layout_expectation explicitly',
                    path=root)
    elif layout_expectation != 'any':
        raise PolyMeshReadError(
            'unsupported_layout',
            f'unsupported layout expectation: {layout_expectation!r}')

    if not mesh.is_dir():
        raise PolyMeshReadError(
            'missing_poly_mesh', f'no polyMesh directory at {mesh}', path=mesh)
    missing = [name for name in REQUIRED if not _member(mesh, name)]
    if missing:
        raise PolyMeshReadError(
            'incomplete_poly_mesh',
            f'{mesh} is missing {", ".join(missing)}', path=mesh)
    return mesh


def _member(mesh: Path, name: str) -> Path | None:
    """Locate ``name`` or ``name.gz``; OpenFOAM writes either."""
    for candidate in (mesh / name, mesh / f'{name}.gz'):
        if candidate.is_file():
            return candidate
    return None


def _read_bytes(path: Path) -> bytes:
    if path.suffix == '.gz':
        with gzip.open(path, 'rb') as stream:
            return stream.read()
    return path.read_bytes()


def _parse_header(raw: bytes, path: Path) -> dict:
    match = _FOAMFILE.search(raw)
    if match is None:
        raise PolyMeshReadError(
            'not_a_foam_file', f'{path} has no FoamFile header', path=path)
    return {
        key.decode('ascii'): value.strip().strip(b'"').decode('ascii', 'replace')
        for key, value in _ENTRY.findall(match.group(1))}


def _payload(raw: bytes, header_end: int) -> bytes:
    body = raw[header_end:]
    body = _COMMENT_BLOCK.sub(b' ', body)
    return _COMMENT_LINE.sub(b' ', body)


def _open_member(mesh: Path, name: str) -> tuple[dict, bytes]:
    """Return the FoamFile header and the comment-stripped payload."""
    path = _member(mesh, name)
    if path is None:
        raise PolyMeshReadError(
            'incomplete_poly_mesh', f'{mesh} is missing {name}', path=mesh)
    raw = _read_bytes(path)
    header = _parse_header(raw, path)
    fmt = header.get('format', 'ascii')
    if fmt != 'ascii':
        # §16.4: normalise through foamFormatConvert rather than guessing.
        raise PolyMeshReadError(
            'binary_format',
            f'{path} is written as {fmt}; convert the case to ASCII with '
            'foamFormatConvert before qualification',
            path=path)
    end = _FOAMFILE.search(raw).end()
    return header, _payload(raw, end)


# --------------------------------------------------------------------------- #
# Payload parsing
# --------------------------------------------------------------------------- #

def _leading_count(payload: bytes, path_hint: str) -> tuple[int, int]:
    """Read the ``N`` that precedes every OpenFOAM list, and where ``(`` opens."""
    open_at = payload.find(b'(')
    if open_at < 0:
        raise PolyMeshReadError(
            'malformed_list', f'{path_hint} has no list body')
    head = payload[:open_at].split()
    if not head or not head[-1].isdigit():
        raise PolyMeshReadError(
            'malformed_list', f'{path_hint} has no list length')
    return int(head[-1]), open_at


def _numbers(text: bytes, dtype) -> np.ndarray:
    """Parse a whitespace/paren separated numeric list.

    An empty list is legitimate -- a single-cell mesh has an empty
    ``neighbour`` -- and must not be an error, which is why this does not hand
    a whitespace-only buffer to numpy's text parser.
    """
    cleaned = text.replace(b'(', b' ').replace(b')', b' ')
    if not cleaned.strip():
        return np.empty(0, dtype=dtype)
    try:
        return np.fromstring(cleaned, dtype=dtype, sep=' ')
    except ValueError:
        # Trailing tokens the fast parser will not accept (a stray keyword, a
        # terminating banner). Falling back is slower but never mis-reads.
        return np.array(cleaned.split(), dtype=dtype)


def _read_points(mesh: Path) -> np.ndarray:
    _header, payload = _open_member(mesh, 'points')
    count, open_at = _leading_count(payload, 'points')
    values = _numbers(payload[open_at:], np.float64)
    if values.size != count * 3:
        raise PolyMeshReadError(
            'malformed_list',
            f'points declares {count} entries but holds {values.size / 3:g}')
    return values.reshape(count, 3)


def _read_labels(mesh: Path, name: str) -> np.ndarray:
    _header, payload = _open_member(mesh, name)
    count, open_at = _leading_count(payload, name)
    values = _numbers(payload[open_at:], np.int64)
    if values.size != count:
        raise PolyMeshReadError(
            'malformed_list',
            f'{name} declares {count} entries but holds {values.size}')
    return values


def _read_faces(mesh: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(flat vertices, offsets)`` for a ``faceList``.

    OpenFOAM also has a ``faceCompactList`` encoding. ``foamFormatConvert``
    does not turn one into the other, so it is refused by name rather than
    mis-read as a plain list.
    """
    header, payload = _open_member(mesh, 'faces')
    if header.get('class', 'faceList') == 'faceCompactList':
        raise PolyMeshReadError(
            'compact_face_list',
            f'{mesh / "faces"} uses faceCompactList, which this reader does '
            'not decode; rewrite the mesh with a faceList encoding')
    count, open_at = _leading_count(payload, 'faces')
    flat = _numbers(payload[open_at:], np.int64)

    # Fast path. A hex- or tet-dominant mesh has one face size throughout, and
    # then the whole file is a reshape rather than a per-face walk. This is the
    # difference between milliseconds and minutes on a multi-million-face mesh.
    if count and flat.size == count * (1 + flat[0]):
        width = int(flat[0])
        grid = flat.reshape(count, width + 1)
        if np.all(grid[:, 0] == width):
            offsets = np.arange(count + 1, dtype=np.int64) * width
            return np.ascontiguousarray(grid[:, 1:].reshape(-1)), offsets

    sizes = np.empty(count, dtype=np.int64)
    starts = np.empty(count, dtype=np.int64)
    position = 0
    for index in range(count):
        if position >= flat.size:
            raise PolyMeshReadError(
                'malformed_list',
                f'faces declares {count} entries but ran out at {index}')
        size = int(flat[position])
        sizes[index] = size
        starts[index] = position + 1
        position += 1 + size
    offsets = np.zeros(count + 1, dtype=np.int64)
    np.cumsum(sizes, out=offsets[1:])
    vertices = np.empty(int(offsets[-1]), dtype=np.int64)
    for index in range(count):
        vertices[offsets[index]:offsets[index + 1]] = \
            flat[starts[index]:starts[index] + sizes[index]]
    return vertices, offsets


def _read_boundary(mesh: Path) -> tuple[BoundaryPatch, ...]:
    _header, payload = _open_member(mesh, 'boundary')
    count, open_at = _leading_count(payload, 'boundary')
    patches = []
    for name, body in _NAMED_DICT.findall(payload[open_at:]):
        entries = {
            key.decode('ascii'): value.strip().decode('ascii', 'replace')
            for key, value in _ENTRY.findall(body)}
        try:
            start_face = int(entries['startFace'])
            n_faces = int(entries['nFaces'])
        except (KeyError, ValueError) as error:
            raise PolyMeshReadError(
                'malformed_boundary',
                f'boundary patch {name.decode()} has no usable '
                'startFace/nFaces') from error
        patches.append(BoundaryPatch(
            name=name.decode('ascii'),
            patch_type=entries.get('type', 'patch'),
            start_face=start_face, n_faces=n_faces,
            neighbour_patch=entries.get('neighbourPatch'),
            entries=entries))
    if len(patches) != count:
        raise PolyMeshReadError(
            'malformed_boundary',
            f'boundary declares {count} patches but holds {len(patches)}')
    return tuple(patches)


def _read_zones(mesh: Path, name: str) -> tuple[Zone, ...]:
    """Read a zone list, treating absence as empty rather than as an error.

    Our own writer emits ``cellZones`` only for multi-region meshes and never
    emits ``faceZones``, so a missing file is the normal case for a
    FoamMesh-published mesh and must not be reported as damage.
    """
    if _member(mesh, name) is None:
        return ()
    _header, payload = _open_member(mesh, name)
    try:
        _count, open_at = _leading_count(payload, name)
    except PolyMeshReadError:
        return ()
    zones = []
    for zone_name, body in _NAMED_DICT.findall(payload[open_at:]):
        # A zone body is ``... cellLabels List<label> N ( ... );`` -- the list
        # is terminated by ``);``, so slice to the closing paren rather than
        # reading to the end of the body and swallowing the semicolon.
        labels_at = body.find(b'(')
        close_at = body.rfind(b')')
        labels = (_numbers(body[labels_at + 1:close_at], np.int64)
                  if 0 <= labels_at < close_at else np.empty(0, dtype=np.int64))
        entries = dict(_ENTRY.findall(body))
        zones.append(Zone(
            name=zone_name.decode('ascii'),
            zone_type=entries.get(b'type', b'cellZone').strip().decode(
                'ascii', 'replace'),
            labels=labels))
    return tuple(zones)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def read_poly_mesh(path: str | Path, *,
                   layout_expectation: str = 'reconstructed') -> PolyMesh:
    """Read ``constant/polyMesh`` without Qt.

    ``path`` may be the case directory or the ``polyMesh`` directory itself.
    ``layout_expectation`` mirrors ``EngineExecutionPlan.layout_expectation``;
    ``'any'`` suppresses the decomposed-case refusal for a caller that has
    already established what it is looking at.
    """
    mesh = _resolve(path, layout_expectation=layout_expectation)
    points = _read_points(mesh)
    vertices, offsets = _read_faces(mesh)
    owner = _read_labels(mesh, 'owner')
    neighbour = _read_labels(mesh, 'neighbour')
    patches = _read_boundary(mesh)

    face_count = int(offsets.size - 1)
    if owner.size != face_count:
        raise PolyMeshReadError(
            'inconsistent_mesh',
            f'{mesh} has {face_count} faces but {owner.size} owner entries',
            path=mesh)
    if neighbour.size > face_count:
        raise PolyMeshReadError(
            'inconsistent_mesh',
            f'{mesh} has more neighbour entries than faces', path=mesh)
    if vertices.size and int(vertices.max()) >= len(points):
        raise PolyMeshReadError(
            'inconsistent_mesh',
            f'{mesh} addresses point {int(vertices.max())} of {len(points)}',
            path=mesh)
    for item in patches:
        if item.end_face > face_count:
            raise PolyMeshReadError(
                'inconsistent_mesh',
                f'patch {item.name} ends at face {item.end_face} of '
                f'{face_count}', path=mesh)

    return PolyMesh(
        path=mesh, points=points, face_vertices=vertices, face_offsets=offsets,
        owner=owner, neighbour=neighbour, patches=patches,
        cell_zones=_read_zones(mesh, 'cellZones'),
        face_zones=_read_zones(mesh, 'faceZones'))


# --------------------------------------------------------------------------- #
# Triangulation
# --------------------------------------------------------------------------- #
#
# A fidelity sample and a hotspot must both map back to the *original* polygon,
# so every triangle carries the face id it came from. The decomposition is a
# pure function of the stored point order, which is what lets a re-read produce
# byte-identical samples (Plan 23 §6.1).
#
# Triangles pass through unchanged. Polygons with more than three vertices fan
# from the average of their own vertices -- the same apex OpenFOAM uses in
# ``primitiveMeshFaceCentresAndAreas``.
#
# MEASURED, and worth stating because it is a trap: for a non-planar face there
# are three different numbers people call "the area", and on a unit quad with
# one corner lifted by 0.5 they are 1.08821, 1.06066 and 1.11237.
#
#   * sum of the fan triangles' *scalar* areas  -> 1.08821
#   * |sum of the fan triangles' area *vectors*| -> 1.06066
#   * VTK's two-triangle split of the same quad -> 1.11237
#
# OpenFOAM's ``magSf`` is the second: it sums the triangle normals and takes
# the magnitude at the end, which is the Newell vector area and is independent
# of the apex. That is the area the solver fluxes through and the one a
# comparison against OpenFOAM must use.
#
# The first is the area of the triangle soup this module hands to the sampler
# and the locator, and is therefore the correct weight for area-weighted sample
# coverage. The two differ by the face's own warp and are not interchangeable.
# :func:`face_areas` returns the triangulated area; :func:`face_magsf` returns
# OpenFOAM's. Reporting one under the other's name would show up later as a
# small unexplained discrepancy charged to the mesher.

def triangulate_faces(mesh: PolyMesh, face_ids) -> tuple[np.ndarray, np.ndarray,
                                                         np.ndarray]:
    """Return ``(vertices, triangles, source_face_ids)`` for the given faces.

    ``vertices`` is the original point array extended with one fan apex per
    decomposed polygon, so point ids below ``len(mesh.points)`` still address
    the mesh's own points.
    """
    face_ids = np.ascontiguousarray(face_ids, dtype=np.int64)
    offsets, flat, points = mesh.face_offsets, mesh.face_vertices, mesh.points
    sizes = offsets[face_ids + 1] - offsets[face_ids]

    triangles: list[np.ndarray] = []
    sources: list[np.ndarray] = []
    apexes: list[np.ndarray] = []
    next_apex = len(points)

    simple = np.flatnonzero(sizes == 3)
    if simple.size:
        starts = offsets[face_ids[simple]]
        rows = np.stack([flat[starts], flat[starts + 1], flat[starts + 2]], axis=1)
        triangles.append(rows)
        sources.append(face_ids[simple])

    # Degenerate faces are a mesh defect, not something to silently repair.
    bad = np.flatnonzero(sizes < 3)
    if bad.size:
        raise PolyMeshReadError(
            'degenerate_face',
            f'face {int(face_ids[bad[0]])} has {int(sizes[bad[0]])} vertices')

    for index in np.flatnonzero(sizes > 3):
        face_id = int(face_ids[index])
        loop = flat[offsets[face_id]:offsets[face_id + 1]]
        apexes.append(points[loop].mean(axis=0))
        apex_id = next_apex
        next_apex += 1
        rolled = np.roll(loop, -1)
        rows = np.stack([
            np.full(loop.size, apex_id, dtype=np.int64), loop, rolled], axis=1)
        triangles.append(rows)
        sources.append(np.full(loop.size, face_id, dtype=np.int64))

    if not triangles:
        return (points.copy(), np.empty((0, 3), dtype=np.int64),
                np.empty(0, dtype=np.int64))

    vertices = (np.vstack([points, np.asarray(apexes, dtype=np.float64)])
                if apexes else points.copy())
    return (vertices, np.vstack(triangles).astype(np.int64, copy=False),
            np.concatenate(sources))


def face_area_vectors(mesh: PolyMesh, face_ids) -> np.ndarray:
    """Outward area vector per face, as OpenFOAM computes ``Sf``.

    The vector sum of the fan triangles' area vectors. Independent of the fan
    apex, so this is also the Newell vector area.
    """
    face_ids = np.ascontiguousarray(face_ids, dtype=np.int64)
    vectors = np.zeros((face_ids.size, 3), dtype=np.float64)
    offsets, flat, points = mesh.face_offsets, mesh.face_vertices, mesh.points
    for position, face_id in enumerate(face_ids):
        loop = points[flat[offsets[face_id]:offsets[face_id + 1]]]
        vectors[position] = 0.5 * np.cross(
            loop, np.roll(loop, -1, axis=0)).sum(axis=0)
    return vectors


def face_magsf(mesh: PolyMesh, face_ids) -> np.ndarray:
    """``magSf``: the area OpenFOAM attributes to each face.

    Use this to compare against ``checkMesh`` or anything else the solver
    reports. Use :func:`face_areas` to weight samples taken on the triangulated
    surface -- for a warped face the two differ, deliberately.
    """
    return np.linalg.norm(face_area_vectors(mesh, face_ids), axis=1)


def face_areas(mesh: PolyMesh, face_ids) -> np.ndarray:
    """Triangulated area of each face, decomposed as :func:`triangulate_faces`.

    This is the area of the surface a sampler actually walks, which is why it
    is the right weight for area-weighted coverage. It is **not** OpenFOAM's
    ``magSf`` on a non-planar face -- see :func:`face_magsf`.
    """
    face_ids = np.ascontiguousarray(face_ids, dtype=np.int64)
    vertices, triangles, sources = triangulate_faces(mesh, face_ids)
    if triangles.size == 0:
        return np.zeros(face_ids.size, dtype=np.float64)
    a, b, c = (vertices[triangles[:, 0]], vertices[triangles[:, 1]],
               vertices[triangles[:, 2]])
    areas = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    order = np.searchsorted(face_ids, sources) if _sorted(face_ids) else None
    if order is None:
        lookup = {int(value): position for position, value in enumerate(face_ids)}
        order = np.fromiter((lookup[int(value)] for value in sources),
                            dtype=np.int64, count=sources.size)
    return np.bincount(order, weights=areas, minlength=face_ids.size)


def _sorted(values: np.ndarray) -> bool:
    return bool(values.size < 2 or np.all(values[1:] >= values[:-1]))


def patch_area(mesh: PolyMesh, patch: BoundaryPatch | str) -> float:
    """Triangulated area of a patch — the sampler's weight."""
    return float(face_areas(mesh, mesh.patch_face_ids(patch)).sum())


def patch_magsf(mesh: PolyMesh, patch: BoundaryPatch | str) -> float:
    """Patch area in OpenFOAM's own terms — the solver's number."""
    return float(face_magsf(mesh, mesh.patch_face_ids(patch)).sum())


def patch_polydata(mesh: PolyMesh, patch: BoundaryPatch | str):
    """Triangulated ``vtkPolyData`` for one patch, carrying ``originalFaceId``.

    VTK is imported here rather than at module scope so the reader itself stays
    usable in a process that has no rendering stack at all.
    """
    from vtkmodules.vtkCommonCore import vtkIdTypeArray, vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray

    item = mesh.patch(patch) if isinstance(patch, str) else patch
    vertices, triangles, sources = triangulate_faces(
        mesh, mesh.patch_face_ids(item))

    polydata = vtkPolyData()
    node = vtkPoints()
    node.SetData(numpy_to_vtk(np.ascontiguousarray(vertices), deep=True))
    polydata.SetPoints(node)

    cells = vtkCellArray()
    if triangles.size:
        connectivity = np.hstack([
            np.full((len(triangles), 1), 3, dtype=np.int64), triangles]).ravel()
        array = vtkIdTypeArray()
        array.DeepCopy(numpy_to_vtkIdTypeArray(
            np.ascontiguousarray(connectivity), deep=True))
        cells.SetCells(len(triangles), array)
    polydata.SetPolys(cells)

    identity = numpy_to_vtkIdTypeArray(np.ascontiguousarray(sources), deep=True)
    identity.SetName('originalFaceId')
    polydata.GetCellData().AddArray(identity)
    return polydata
