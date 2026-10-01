"""The bounded topology read a worker section is built from (Plan 37 UF10a).

:func:`read_mesh_topology` returns the points, faces, owner and neighbour of
the reconstructed ``constant/polyMesh`` -- optionally ``cellZones`` and
``cellLevel`` -- together with the identity of exactly the files it read.
It either returns a verified, immutable :class:`MeshTopology` or raises a
:class:`~foammesh.core.mesh.poly_mesh_boundary.PolyMeshReadError` whose
``reason`` is one of :data:`REFUSAL_REASONS`. It never writes to the user's
case; the optional snapshot is a copy in the caller's scratch directory.

Budgets are checked twice: from the headers and list counts before any list
is parsed (``stage='preflight'``), and from the arrays once they exist
(``stage='read'``), so a header that under-states its list cannot slip a
large read past the limit. By default there is no count limit: the read is
refused only when its memory estimate exceeds the free RAM, and says both.

Formats: ASCII, ``.gz``, binary (little-endian, 32/64-bit labels, 32/64-bit
scalars, width inferred when no ``arch`` entry states it), ``N{v}`` uniform
lists and ``faceCompactList`` in either format. See
``plans/evidence/plan37/uf10a-reader-qualification.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from foammesh.core.mesh import poly_mesh_binary as _binary
from foammesh.core.mesh.poly_mesh_boundary import (
    _FOAMFILE, _NOTE_COUNT, REQUIRED, BoundaryPatch, PolyMesh,
    PolyMeshReadError, Zone, _list_count, _member, _open_member, _parse_header,
    _payload, _read_boundary, _read_faces, _read_head, _read_labels,
    _read_points, _read_zones, _uncompressed_size)
from foammesh.core.mesh.poly_mesh_lease import (
    MemberStamp, ReadLease, StaleInputError)

#: Every ``reason`` :func:`read_mesh_topology` can refuse with.
REFUSAL_REASONS = frozenset({
    'missing_poly_mesh', 'incomplete_poly_mesh', 'not_a_foam_file',
    'decomposed_layout', 'decomposed_newer_than_reconstructed',
    'malformed_list', 'malformed_boundary', 'inconsistent_mesh',
    'unsupported_encoding', 'binary_width_ambiguous',
    'over_budget', 'stale_input',
})

#: Optional members a caller may ask for.
CELL_ZONES = 'cellZones'
CELL_LEVEL = 'cellLevel'

#: Called with each member's name once it has been read. Tests use it to
#: overwrite a file mid-read; production code leaves it None.
_after_member: Callable[[str], None] | None = None


class TopologyRefusal(PolyMeshReadError):
    """A typed refusal that carries machine-readable ``detail``."""

    def __init__(self, reason: str, message: str, *, path: Path | None = None,
                 detail: dict | None = None):
        super().__init__(reason, message, path=path)
        self.detail = dict(detail or {})


def refusal_dict(error: PolyMeshReadError) -> dict:
    """``{'reason', 'message', 'path', 'detail'}`` for any reader refusal."""
    detail = dict(getattr(error, 'detail', {}) or {})
    if isinstance(error, StaleInputError):
        detail.setdefault('changed', list(error.changed))
    return {'reason': error.reason, 'message': str(error),
            'path': str(error.path) if error.path is not None else None,
            'detail': detail}


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TopologyBudget:
    """The most a topology read may take on. Every limit is inclusive.

    No count or byte limit is set by default: a mesh of any size is read
    when the RAM holds it (2026-10-01). The read is refused only when its
    memory estimate, from the headers, exceeds ``max_memory_bytes`` -- by
    default the RAM free when the read starts
    (:func:`foammesh.support.resource_budget.snapshot`). The count and byte
    limits remain for a caller that wants one.
    """

    max_points: int | None = None
    max_faces: int | None = None
    max_cells: int | None = None
    #: Bytes of the member files once decompressed.
    max_input_bytes: int | None = None
    #: Bytes of the arrays returned.
    max_array_bytes: int | None = None
    #: RAM the read may take; ``None``: what is free when it starts.
    max_memory_bytes: int | None = None

    def check(self, quantity: str, measured: int, *, stage: str,
              path: Path) -> None:
        limit = getattr(self, f'max_{quantity}')
        if limit is not None and measured > limit:
            raise TopologyRefusal(
                'over_budget',
                f'{path}: {quantity.replace("_", " ")} {measured:,} exceeds '
                f'the budget of {limit:,} ({stage})', path=path,
                detail={'quantity': quantity, 'limit': int(limit),
                        'measured': int(measured), 'stage': stage})

    def check_memory(self, needed: int, *, path: Path) -> None:
        """Refuse a read whose estimated RAM is more than is free."""
        from foammesh.support import resource_budget

        if self.max_memory_bytes is not None:
            room = int(self.max_memory_bytes)
            if needed <= room:
                return
            message = (f'reading the mesh needs about '
                       f'{resource_budget.format_bytes(needed)} of RAM and '
                       f'{resource_budget.format_bytes(room)} is allowed')
        else:
            measured = resource_budget.quick_snapshot()
            room = int(measured.budget)
            message = resource_budget.memory_refusal(
                'reading the mesh', needed, measured)
            if message is None:
                return
        raise TopologyRefusal(
            'over_budget', f'{path}: {message}', path=path,
            detail={'quantity': 'memory_bytes', 'limit': room,
                    'measured': int(needed), 'needed_bytes': int(needed),
                    'free_bytes': room, 'stage': 'preflight'})

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in (
            'max_points', 'max_faces', 'max_cells', 'max_input_bytes',
            'max_array_bytes', 'max_memory_bytes')}


def read_memory_estimate(n_points: int, n_faces: int, n_internal: int,
                         member_bytes) -> int:
    """RAM a topology read takes at its peak, from the header counts.

    The returned arrays (int64 labels, float64 points, at least four
    vertices a face) plus the largest member's raw bytes, which a binary
    read holds while it widens them -- twice that for an ASCII member,
    whose tokens are parsed from a copy.
    """
    arrays = (24 * n_points + 8 * (n_faces + 1) + 8 * n_faces
              + 8 * n_internal + 32 * n_faces)
    largest = max([0, *(size * (1 if binary else 2)
                        for size, binary in member_bytes)])
    return int(arrays + largest)


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LayoutState:
    """What a case directory holds, before anything is read.

    ``kind`` is ``reconstructed`` (constant/polyMesh only),
    ``reconstructed_and_decomposed`` (both, and the ranks are still a
    decomposition of it or older than it), ``decomposed_only``,
    ``decomposed_newer`` (a rank mesh was rewritten after the reconstructed
    one, e.g. by a parallel snappyHexMesh) or ``missing``. Only the
    first two are ``supported``; the rest carry the refusal ``reason``.
    """

    kind: str
    case_dir: Path
    mesh_dir: Path
    ranks: int = 0
    reason: str | None = None
    message: str = ''

    @property
    def supported(self) -> bool:
        return self.reason is None

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'case_dir': str(self.case_dir),
                'mesh_dir': str(self.mesh_dir), 'ranks': self.ranks,
                'supported': self.supported, 'reason': self.reason,
                'message': self.message}


def _diverged_rank_owners(case_dir: Path) -> list[Path]:
    """Owners of rank meshes that are no longer a decomposition.

    Observed on v13: ``decomposePar`` writes each rank's mesh together with
    its ``cellProcAddressing`` (same mtime), so ranks newer than
    ``constant/polyMesh`` are normal right after it. A parallel
    ``snappyHexMesh -overwrite`` rewrites the rank meshes and *removes* the
    ``*ProcAddressing`` files: the ranks now hold a different mesh. A rank
    mesh without addressing, or rewritten after it, has diverged.
    """
    owners = []
    for rank in sorted(case_dir.glob('processor[0-9]*')):
        for mesh in (rank / 'constant' / 'polyMesh', *rank.glob('*/polyMesh')):
            owner = _member(mesh, 'owner') if mesh.is_dir() else None
            if owner is None:
                continue
            addressing = _member(mesh, 'cellProcAddressing')
            if (addressing is None or owner.stat().st_mtime_ns
                    > addressing.stat().st_mtime_ns):
                owners.append(owner)
    return owners


def probe_layout(path: str | Path) -> LayoutState:
    """Classify ``path`` (a case or its polyMesh directory). Reads no list."""
    path = Path(path)
    if path.name == 'polyMesh':
        mesh, case_dir = path, path.parent.parent
    else:
        mesh, case_dir = path / 'constant' / 'polyMesh', path
    ranks = sorted(case_dir.glob('processor[0-9]*')) if case_dir.is_dir() else []
    reconstructed = mesh.is_dir() and _member(mesh, 'owner') is not None
    if not ranks:
        if mesh.is_dir():
            return LayoutState('reconstructed', case_dir, mesh)
        return LayoutState('missing', case_dir, mesh, reason='missing_poly_mesh',
                           message=f'no polyMesh directory at {mesh}')
    if not reconstructed:
        return LayoutState(
            'decomposed_only', case_dir, mesh, ranks=len(ranks),
            reason='decomposed_layout',
            message=(f'{case_dir} holds a decomposed case ({len(ranks)} '
                     'ranks) and no reconstructed constant/polyMesh; run '
                     'reconstructPar -constant first'))
    own = _member(mesh, 'owner').stat().st_mtime_ns
    newer = [owner for owner in _diverged_rank_owners(case_dir)
             if owner.stat().st_mtime_ns > own]
    if newer:
        return LayoutState(
            'decomposed_newer', case_dir, mesh, ranks=len(ranks),
            reason='decomposed_newer_than_reconstructed',
            message=(f'{newer[0].parent} was written after {mesh}; the '
                     'reconstructed mesh is not the one the ranks hold. '
                     'Reconstruct again before reading'))
    return LayoutState('reconstructed_and_decomposed', case_dir, mesh,
                       ranks=len(ranks))


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MeshIdentity:
    """Which files were read: enough to tell whether they are still there."""

    mesh_dir: Path
    #: Digest of every member's size, mtime and file id (stat only).
    revision: str
    members: tuple[MemberStamp, ...]
    #: :func:`fingerprint_poly_mesh`'s digest of the bytes read -- present
    #: only for a snapshot read, where the bytes were hashed as copied.
    content_digest: str | None = None

    def to_dict(self) -> dict:
        return {'mesh_dir': str(self.mesh_dir), 'revision': self.revision,
                'content_digest': self.content_digest,
                'members': [item.to_dict() for item in self.members]}


def _frozen(array: np.ndarray) -> np.ndarray:
    array = np.ascontiguousarray(array)
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class MeshTopology:
    """A verified, read-only polyMesh topology and where it came from."""

    identity: MeshIdentity
    points: np.ndarray           # (n_points, 3) float64
    face_vertices: np.ndarray    # flat int64
    face_offsets: np.ndarray     # (n_faces + 1,) int64
    owner: np.ndarray            # (n_faces,) int64
    neighbour: np.ndarray        # (n_internal_faces,) int64
    patches: tuple[BoundaryPatch, ...]
    n_cells: int
    #: ``member -> 'ascii' | 'ascii.gz' | 'binary' | ...`` plus
    #: ``' faceCompactList'`` / ``' label=4'`` details.
    encodings: dict = field(default_factory=dict)
    cell_zones: tuple[Zone, ...] = ()
    #: ``not_requested`` | ``absent`` | ``read``
    cell_zones_status: str = 'not_requested'
    cell_level: np.ndarray | None = None
    #: ``not_requested`` | ``absent`` | ``read`` | ``size_mismatch``
    cell_level_status: str = 'not_requested'
    budget: TopologyBudget = TopologyBudget()

    @property
    def n_points(self) -> int:
        return int(self.points.shape[0])

    @property
    def n_faces(self) -> int:
        return int(self.face_offsets.size - 1)

    @property
    def n_internal_faces(self) -> int:
        return int(self.neighbour.size)

    @property
    def array_bytes(self) -> int:
        arrays = [self.points, self.face_vertices, self.face_offsets,
                  self.owner, self.neighbour]
        arrays += [zone.labels for zone in self.cell_zones]
        if self.cell_level is not None:
            arrays.append(self.cell_level)
        return int(sum(array.nbytes for array in arrays))

    def as_poly_mesh(self) -> PolyMesh:
        """The same arrays as a :class:`PolyMesh`, for the CR2 helpers."""
        return PolyMesh(
            path=self.identity.mesh_dir, points=self.points,
            face_vertices=self.face_vertices, face_offsets=self.face_offsets,
            owner=self.owner, neighbour=self.neighbour, patches=self.patches,
            cell_zones=self.cell_zones)

    def summary(self) -> dict:
        return {'identity': self.identity.to_dict(),
                'points': self.n_points, 'faces': self.n_faces,
                'internal_faces': self.n_internal_faces,
                'cells': self.n_cells, 'encodings': dict(self.encodings),
                'cell_zones': [zone.to_dict() for zone in self.cell_zones],
                'cell_zones_status': self.cell_zones_status,
                'cell_level_status': self.cell_level_status,
                'array_bytes': self.array_bytes,
                'budget': self.budget.to_dict()}


# --------------------------------------------------------------------------- #
# Preflight: headers and counts only
# --------------------------------------------------------------------------- #

@dataclass
class _Head:
    path: Path
    header: dict
    count: int
    bytes: int

    @property
    def binary(self) -> bool:
        return self.header.get('format', 'ascii') == 'binary'

    @property
    def compact(self) -> bool:
        return self.header.get('class') == 'faceCompactList'


def _head(mesh: Path, name: str) -> _Head:
    path = _member(mesh, name)
    if path is None:
        raise PolyMeshReadError(
            'incomplete_poly_mesh', f'{mesh} is missing {name}', path=mesh)
    raw = _read_head(path)
    header = _parse_header(raw, path)
    fmt = header.get('format', 'ascii')
    start = _FOAMFILE.search(raw).end()
    if fmt == 'binary':
        count, _end = _binary._count(raw, start, name)
    elif fmt == 'ascii':
        count = _list_count(_payload(raw, start), name)
    else:
        raise TopologyRefusal(
            'unsupported_encoding', f'{path} is written as {fmt!r}',
            path=path, detail={'member': name, 'format': fmt})
    return _Head(path, header, count, _uncompressed_size(path))


def _encoding(head: _Head) -> str:
    text = head.header.get('format', 'ascii')
    if head.path.suffix == '.gz':
        text += '.gz'
    if head.compact:
        text += ' faceCompactList'
    return text


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #

class _Widths:
    """The label width the mesh's binary members agree on."""

    def __init__(self):
        self.label: int | None = None

    def agree(self, width: int, name: str, path: Path) -> None:
        if self.label is None:
            self.label = width
        elif width != self.label:
            raise TopologyRefusal(
                'binary_width_ambiguous',
                f'{name} decodes with {width}-byte labels but the rest of the '
                f'mesh with {self.label}', path=path,
                detail={'member': name, 'width': width, 'mesh': self.label})


def _done(name: str) -> None:
    if _after_member is not None:
        _after_member(name)


def _points(mesh: Path, head: _Head, encodings: dict) -> np.ndarray:
    if head.binary:
        points, width = _binary.read_binary_points(head.path)
        encodings['points'] = _encoding(head) + f' scalar={width}'
    else:
        points = _read_points(mesh)
        encodings['points'] = _encoding(head)
    return points


def _faces(mesh: Path, head: _Head, widths: _Widths, encodings: dict):
    if head.binary:
        flat, offsets, width = _binary.read_binary_compact_faces(
            head.path, label_width=widths.label)
        widths.agree(width, 'faces', head.path)
        encodings['faces'] = _encoding(head) + f' label={width}'
    elif head.compact:
        _header, payload = _open_member(mesh, 'faces')
        flat, offsets = _binary.read_ascii_compact_faces(payload)
        encodings['faces'] = _encoding(head)
    else:
        flat, offsets = _read_faces(mesh)
        encodings['faces'] = _encoding(head)
    return flat, offsets


def _labels(mesh: Path, name: str, head: _Head, widths: _Widths,
            encodings: dict) -> np.ndarray:
    if head.binary:
        labels, width = _binary.read_binary_labels(
            head.path, name, label_width=widths.label)
        widths.agree(width, name, head.path)
        encodings[name] = _encoding(head) + f' label={width}'
    else:
        labels = _read_labels(mesh, name)
        encodings[name] = _encoding(head)
    return labels


def _verify(mesh: Path, points, flat, offsets, owner, neighbour, patches,
            note_cells: int | None) -> int:
    faces = int(offsets.size - 1)
    if owner.size != faces:
        raise PolyMeshReadError(
            'inconsistent_mesh',
            f'{mesh} has {faces} faces but {owner.size} owner entries',
            path=mesh)
    if neighbour.size > faces:
        raise PolyMeshReadError(
            'inconsistent_mesh', f'{mesh} has more neighbour entries than '
            'faces', path=mesh)
    if flat.size and (int(flat.min()) < 0 or int(flat.max()) >= len(points)):
        raise PolyMeshReadError(
            'inconsistent_mesh',
            f'{mesh} addresses a point outside 0..{len(points) - 1}',
            path=mesh)
    if owner.size and int(owner.min()) < 0 or (
            neighbour.size and int(neighbour.min()) < 0):
        raise PolyMeshReadError(
            'inconsistent_mesh', f'{mesh} has a negative cell label',
            path=mesh)
    end = 0
    for patch in patches:
        if patch.end_face > faces:
            raise PolyMeshReadError(
                'inconsistent_mesh',
                f'patch {patch.name} ends at face {patch.end_face} of {faces}',
                path=mesh)
        end = max(end, patch.end_face)
    cells = 0 if owner.size == 0 else int(
        max(owner.max(), neighbour.max(initial=-1))) + 1
    if note_cells is not None and note_cells != cells:
        raise PolyMeshReadError(
            'inconsistent_mesh',
            f'{mesh}: the owner header says {note_cells} cells, the lists '
            f'address {cells}', path=mesh)
    return cells


def read_mesh_topology(path: str | Path, *,
                       budget: TopologyBudget | None = None,
                       include_cell_zones: bool = False,
                       include_cell_level: bool = False,
                       snapshot_dir: str | Path | None = None) -> MeshTopology:
    """Read and verify the topology of a reconstructed polyMesh.

    ``path`` is a case directory or its ``constant/polyMesh``. With
    ``snapshot_dir`` the members are first copied there (hashed as copied)
    and the copy is read, so the result is one mesh even if a run rewrites
    the case meanwhile; the caller owns and removes ``snapshot_dir``.
    Without it the files are read in place under a stat lease and any change
    during the read raises ``stale_input``.
    """
    budget = budget or TopologyBudget()
    layout = probe_layout(path)
    if not layout.supported:
        raise TopologyRefusal(
            layout.reason, layout.message, path=layout.case_dir,
            detail=layout.to_dict())
    source = layout.mesh_dir
    missing = [name for name in REQUIRED if _member(source, name) is None]
    if missing:
        raise PolyMeshReadError(
            'incomplete_poly_mesh', f'{source} is missing {", ".join(missing)}',
            path=source)
    names = list(REQUIRED)
    if include_cell_zones:
        names.append(CELL_ZONES)
    if include_cell_level:
        names.append(CELL_LEVEL)
    lease = ReadLease.take(source, names)

    # Budget, before any list is parsed.
    budget.check('input_bytes', lease.input_bytes, stage='preflight',
                 path=source)
    heads = {name: _head(source, name) for name in
             ('points', 'faces', 'owner', 'neighbour')}
    input_bytes = sum(head.bytes for head in heads.values())
    budget.check('input_bytes', input_bytes, stage='preflight', path=source)
    n_points = heads['points'].count
    n_faces = heads['faces'].count - (1 if heads['faces'].compact else 0)
    n_internal = heads['neighbour'].count
    note = dict(_NOTE_COUNT.findall(heads['owner'].header.get('note', '')))
    note_cells = int(note['nCells']) if 'nCells' in note else None
    budget.check('points', n_points, stage='preflight', path=source)
    budget.check('faces', n_faces, stage='preflight', path=source)
    if note_cells is not None:
        budget.check('cells', note_cells, stage='preflight', path=source)
    # 4 vertices a face is the floor of any mesh worth reading.
    budget.check('array_bytes',
                 24 * n_points + 8 * (n_faces + 1) + 8 * n_faces
                 + 8 * n_internal + 32 * n_faces,
                 stage='preflight', path=source)
    budget.check_memory(read_memory_estimate(
        n_points, n_faces, n_internal,
        [(head.bytes, head.binary) for head in heads.values()]), path=source)

    content_digest = None
    mesh = source
    if snapshot_dir is not None:
        mesh, content_digest = lease.snapshot(Path(snapshot_dir))
        heads = {name: _head(mesh, name) for name in heads}

    encodings: dict = {}
    widths = _Widths()
    points = _points(mesh, heads['points'], encodings)
    _done('points')
    flat, offsets = _faces(mesh, heads['faces'], widths, encodings)
    _done('faces')
    owner = _labels(mesh, 'owner', heads['owner'], widths, encodings)
    _done('owner')
    neighbour = _labels(mesh, 'neighbour', heads['neighbour'], widths,
                        encodings)
    _done('neighbour')
    patches = _read_boundary(mesh, any_format=True)
    encodings['boundary'] = 'text'
    _done('boundary')

    budget.check('points', len(points), stage='read', path=source)
    budget.check('faces', int(offsets.size - 1), stage='read', path=source)
    cells = _verify(source, points, flat, offsets, owner, neighbour, patches,
                    note_cells)
    budget.check('cells', cells, stage='read', path=source)

    zones: tuple[Zone, ...] = ()
    zones_status = 'not_requested'
    if include_cell_zones:
        zones_status = 'absent'
        zone_path = _member(mesh, CELL_ZONES)
        if zone_path is not None:
            zone_head = _head(mesh, CELL_ZONES)
            if zone_head.binary:
                zones = _binary.read_binary_zones(
                    zone_path, CELL_ZONES, label_width=widths.label)
            else:
                zones = _read_zones(mesh, CELL_ZONES)
            encodings[CELL_ZONES] = _encoding(zone_head)
            zones_status = 'read'
            for zone in zones:
                if zone.labels.size and (int(zone.labels.min()) < 0
                                         or int(zone.labels.max()) >= cells):
                    raise PolyMeshReadError(
                        'inconsistent_mesh',
                        f'cellZone {zone.name} addresses a cell outside '
                        f'0..{cells - 1}', path=source)
            _done(CELL_ZONES)

    level = None
    level_status = 'not_requested'
    if include_cell_level:
        level_status = 'absent'
        if _member(mesh, CELL_LEVEL) is not None:
            level_head = _head(mesh, CELL_LEVEL)
            level = _labels(mesh, CELL_LEVEL, level_head, widths, encodings)
            level_status = 'read'
            if level.size != cells:
                # A cellLevel from an earlier run, left behind by a tool that
                # rewrote the mesh without it: not this mesh's refinement.
                level, level_status = None, 'size_mismatch'
            elif level.size and int(level.min()) < 0:
                raise PolyMeshReadError(
                    'malformed_list', 'cellLevel holds a negative level',
                    path=source)
            _done(CELL_LEVEL)

    # The files are what the lease saw, or none of this is one mesh.
    if snapshot_dir is None:
        lease.verify()

    topology = MeshTopology(
        identity=MeshIdentity(mesh_dir=source, revision=lease.revision,
                              members=lease.stamps,
                              content_digest=content_digest),
        points=_frozen(points), face_vertices=_frozen(flat),
        face_offsets=_frozen(offsets), owner=_frozen(owner),
        neighbour=_frozen(neighbour), patches=patches, n_cells=cells,
        encodings=encodings,
        cell_zones=tuple(Zone(zone.name, zone.zone_type, _frozen(zone.labels))
                         for zone in zones),
        cell_zones_status=zones_status,
        cell_level=None if level is None else _frozen(level),
        cell_level_status=level_status, budget=budget)
    budget.check('array_bytes', topology.array_bytes, stage='read',
                 path=source)
    return topology
