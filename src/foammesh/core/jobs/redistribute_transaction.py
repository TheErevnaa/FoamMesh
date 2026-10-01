"""Change the core count of a decomposed mesh (Plan 37 UF17).

A snappy case meshed in parallel keeps its result in ``processor*/`` until
something gathers it. Changing the number of cores those processor meshes
are split over is OpenFOAM v13 ``redistributePar -parallel``. Measured on
v13 (``plans/evidence/plan37/uf17-v13-redistribute.md``):

* it must run on ``max(source, target)`` ranks -- below that every rank
  segfaults;
* a refused or crashed run writes **in place** into the processor cases it
  was given (empty ``processor4..7`` on a refusal, empty ``processor1..3``
  on an undecomposed case), so it never runs on the live ones;
* shrinking leaves the surplus ranks as zero-cell meshes to delete;
* ``polyMesh/sets``, the refinement files and the processor addressing are
  not mapped, and the stale sets break a later gather.

So a redistribution is an explicit, durable transaction under
``foammesh/redistribute/<id>/``::

    manifest.json   PREPARED -> LAUNCHED -> VALIDATED -> PUBLISHING -> COMMITTED
    stage/          system/controlDict, system/decomposeParDict, processor*
    retired/        the live processor cases and dictionary, once publishing

The live processor cases are only ever renamed, never written: until
PUBLISHING they are untouched, and during it every step is a rename that
:func:`recover` can finish or undo. The retired cases (every surplus rank
among them) are deleted only after COMMITTED. The core-count setting is the
last thing published; a crash before it lands is finished at case open.
"""
from __future__ import annotations

import contextlib
import errno
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import case_lease
from .run_records import _write_durably

logger = logging.getLogger(__name__)

ROOT = ('foammesh', 'redistribute')
PREPARED = 'prepared'
LAUNCHED = 'launched'
VALIDATED = 'validated'
PUBLISHING = 'publishing'
COMMITTED = 'committed'
STATES = (PREPARED, LAUNCHED, VALIDATED, PUBLISHING, COMMITTED)
SCHEMA_VERSION = 1
OPERATION = 'mesh.redistribute'
PREVIEW_OPERATION = 'mesh.redistribute.preview'
RECOVER_OPERATION = 'mesh.redistribute.recover'
PENDING_GATHER = ('foammesh', 'pending-gather.json')
#: Free space kept beyond the copies themselves.
DISK_RESERVE_BYTES = 64 * 1024 * 1024

#: What ``redistributePar`` maps and a processor mesh keeps. Anything else in
#: ``constant/polyMesh`` is indexed by the old decomposition's local labels.
KEPT_MESH_FILES = frozenset((
    'boundary', 'faces', 'owner', 'neighbour', 'points',
    'cellZones', 'faceZones', 'pointZones'))
#: Named in the result when they were dropped, because each is something a
#: user may have relied on.
UNMAPPED = {
    'sets': 'cell, face and point sets',
    'cellLevel': 'refinement levels (cellLevel)',
    'pointLevel': 'refinement levels (pointLevel)',
    'level0Edge': 'refinement base edge length',
    'refinementHistory': 'refinement history',
    'surfaceIndex': 'snapped surface index',
    'cellProcAddressing': 'processor addressing',
    'faceProcAddressing': 'processor addressing',
    'pointProcAddressing': 'processor addressing',
    'boundaryProcAddressing': 'processor addressing',
}
PROCESSOR_PATCH_TYPES = ('processor', 'processorCyclic')

#: Transaction ids running in this process. The executor's gate and a
#: second open of the case must not "recover" a transaction that is live.
_ACTIVE: set[str] = set()

REASONS = {
    'undecomposed': 'the mesh is not decomposed: there are no processor '
                    'cases to redistribute',
    'single_rank': 'the mesh is decomposed into one processor case; '
                   'redistributePar needs at least two ranks, so gather it '
                   'and mesh in parallel instead',
    'single_rank_target': 'one core is a serial mesh: gather the processor '
                          'cases (Reconstruct) and set the mode to serial '
                          'instead of redistributing',
    'target_invalid': 'the new core count must be a whole number of at '
                      'least 2',
    'unchanged': 'the mesh is already split over that many cores',
    'too_many_ranks': 'redistributePar runs on as many ranks as the larger '
                      'of the two counts, and this machine does not have '
                      'that many cores',
    'collated': 'the processor cases are written collated (processors<N>/), '
                'which this transaction does not handle',
    'regions': 'the processor cases hold region meshes; redistributePar '
               'moves one region per run, and that is not qualified',
    'incomplete': 'the processor cases are incomplete or numbered with gaps',
    'inconsistent_patches': 'the processor cases disagree about the patch '
                            'list',
    'results_present': 'the processor cases hold more than one time, or a '
                       'mesh written at a later time; redistribute only a '
                       'mesh in constant/ with at most its start time',
    'fields_need_internal_patch': 'the processor cases carry fields, and '
                                  'redistributePar then needs a patch of '
                                  'type internal to hold exposed faces; '
                                  'this mesh has none',
    'unreadable_mesh': 'a processor mesh could not be read',
    'pending_transaction': 'an earlier core-count change of this case is '
                           'not settled yet',
    'insufficient_disk': 'there is not enough free disk space for the '
                         'staging copy and its result',
    'stale_revision': 'the processor cases changed since the preview',
    'case_busy': 'another run holds this case',
    'not_leased': 'the case lease is not held',
    'method_needs_cells': 'the decomposition method needs a cell split that '
                          'multiplies to the new core count',
    'settings_locked': 'the core-count setting cannot be changed now',
    'staging_failed': 'the staging copy of the processor cases could not be '
                      'made',
    'validation_failed': 'the redistributed mesh does not match the source',
    'launch_failed': 'redistributePar did not finish',
    'check_failed': 'checkMesh -parallel did not confirm the redistributed '
                    'mesh',
}


class RedistributeError(RuntimeError):
    """A redistribution that changed nothing live, and why."""

    def __init__(self, code: str, message: str | None = None, *,
                 details: dict | None = None):
        super().__init__(message or REASONS.get(code, code))
        self.code = code
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# Paths and small helpers
# --------------------------------------------------------------------------- #

def transactions_root(case_path) -> Path:
    return Path(case_path).joinpath(*ROOT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


#: Pauses between attempts to remove a folder Windows still holds open.
#: MEASURED 2026-09-30 (UF20 check 11): right after the ranks of a failed
#: launch were killed, their stage was still in use (WinError 32).
REMOVE_RETRY_SECONDS = (0.1, 0.25, 0.5, 1.0)


def _remove(path: Path, *, retry_seconds=None, sleep=None) -> bool:
    """Remove *path*; ``False`` (logged) when it could not be.

    On Windows a file a just-killed process held open stays locked for a
    moment, so the removal is retried with a short backoff before giving up.
    """
    if retry_seconds is None:
        retry_seconds = REMOVE_RETRY_SECONDS if os.name == 'nt' else ()
    sleep = sleep or time.sleep
    pauses = list(retry_seconds)
    while True:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return True
        except OSError:
            if not pauses:
                logger.exception('could not remove %s', path)
                return False
            sleep(pauses.pop(0))


def _rank_number(path: Path) -> int | None:
    found = re.fullmatch(r'processor(\d+)', path.name)
    return int(found.group(1)) if found else None


def processor_dirs(root) -> list[Path]:
    """``processorN`` directories of *root*, in rank order."""
    root = Path(root)
    if not root.is_dir():
        return []
    found = [(number, path) for path in root.iterdir()
             if path.is_dir() and (number := _rank_number(path)) is not None]
    return [path for _number, path in sorted(found)]


def _is_time(name: str) -> bool:
    try:
        float(name)
    except ValueError:
        return False
    return True


def _time_dirs(processor: Path) -> list[str]:
    return sorted(path.name for path in processor.iterdir()
                  if path.is_dir() and _is_time(path.name))


def _tree_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(path.stat().st_size for path in root.rglob('*') if path.is_file())


def _reserve_bytes() -> int:
    from . import stage_snapshots
    try:
        return max(DISK_RESERVE_BYTES, stage_snapshots.load_policy().reserve_bytes)
    except Exception:  # noqa: BLE001 - a broken preference keeps the floor
        return DISK_RESERVE_BYTES


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


# --------------------------------------------------------------------------- #
# Census: what the processor cases hold, as global invariants
# --------------------------------------------------------------------------- #

_FOAMFILE = re.compile(rb'FoamFile\s*\{(.*?)\}', re.DOTALL)
_ZONE_TOKEN = re.compile(
    rb'(?P<name>[A-Za-z_][\w.:\-]*)\s*\{'
    rb'|(?P<kind>cell|face|point)Labels\s+List<label>\s*(?P<count>\d+)\s*(?P<open>[({])'
    rb'|flipMap\s+List<bool>\s*(?P<flips>\d+)\s*(?P<fopen>[({])')


def _member(mesh: Path, name: str) -> Path | None:
    for candidate in (mesh / name, mesh / f'{name}.gz'):
        if candidate.is_file():
            return candidate
    return None


def _read_member(path: Path) -> bytes:
    if path.suffix == '.gz':
        with gzip.open(path, 'rb') as stream:
            return stream.read()
    return path.read_bytes()


def _header(raw: bytes) -> tuple[dict, int]:
    found = _FOAMFILE.search(raw)
    if found is None:
        return {}, 0
    entries = {}
    for key, value in re.findall(rb'(\w+)\s+([^;]+);', found.group(1)):
        entries[key.decode('ascii', 'replace')] = value.strip().strip(b'"').decode(
            'ascii', 'replace')
    return entries, found.end()


def zone_counts(mesh: Path, name: str) -> dict[str, int]:
    """``{zone: label count}`` of one zone file, ASCII or binary.

    Labels are skipped by their declared count, so a binary list cannot be
    mistaken for a zone name.
    """
    path = _member(mesh, name)
    if path is None:
        return {}
    raw = _read_member(path)
    header, start = _header(raw)
    binary = header.get('format', 'ascii') == 'binary'
    width = 4
    found = re.search(r'label\s*=\s*(\d+)', header.get('arch', ''))
    if found:
        width = int(found.group(1)) // 8
    counts: dict[str, int] = {}
    current = None
    position = start
    while True:
        token = _ZONE_TOKEN.search(raw, position)
        if token is None:
            break
        if token.group('name') is not None:
            current = token.group('name').decode('ascii', 'replace')
            counts.setdefault(current, 0)
            position = token.end()
            continue
        if token.group('kind') is not None:
            count, opener, size = int(token.group('count')), token.group('open'), width
            if current is not None:
                counts[current] = count
        else:
            count, opener, size = int(token.group('flips')), token.group('fopen'), 1
        if opener == b'{':
            position = raw.find(b'}', token.end()) + 1
        elif binary:
            position = token.end() + count * size + 1
        else:
            position = raw.find(b')', token.end()) + 1
        if position <= 0:
            break
    return counts


@dataclass
class Census:
    """A decomposition in the terms redistribution must preserve."""

    ranks: int = 0
    cells: int = 0
    internal_faces: int = 0
    per_rank_cells: list = field(default_factory=list)
    patches: dict = field(default_factory=dict)         # named patch -> faces
    patch_types: dict = field(default_factory=dict)
    cell_zones: dict = field(default_factory=dict)      # zone -> cells
    face_zones: list = field(default_factory=list)      # names
    point_zones: list = field(default_factory=list)     # names
    time: str | None = None
    fields: list = field(default_factory=list)
    unmapped: list = field(default_factory=list)        # file names present
    mesh_format: str = 'ascii'
    bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def invariants(self) -> dict:
        """What must be equal before and after."""
        return {'cells': self.cells, 'internal_faces': self.internal_faces,
                'patches': dict(sorted(self.patches.items())),
                'cell_zones': dict(sorted(self.cell_zones.items())),
                'face_zones': sorted(self.face_zones),
                'point_zones': sorted(self.point_zones),
                'fields': sorted(self.fields)}


def _rank_mesh(processor: Path) -> dict:
    from foammesh.core.mesh.poly_mesh_boundary import (
        PolyMeshReadError, _head_count, _head_member, _read_boundary)
    mesh = processor / 'constant' / 'polyMesh'
    try:
        _path, header, _payload = _head_member(mesh, 'owner')
        note = dict(re.findall(r'(nCells|nInternalFaces)\s*:\s*(\d+)',
                               header.get('note', '')))
        if 'nCells' not in note:
            raise RedistributeError('unreadable_mesh', details={
                'rank': processor.name, 'file': 'owner'})
        _path, _header_n, internal = _head_count(mesh, 'neighbour')
        patches = _read_boundary(mesh, any_format=True)
    except PolyMeshReadError as error:
        raise RedistributeError('unreadable_mesh', details={
            'rank': processor.name, 'error': str(error)}) from None
    return {'cells': int(note['nCells']), 'internal_faces': int(internal),
            'format': header.get('format', 'ascii'),
            'patches': [(item.name, item.patch_type, item.n_faces)
                        for item in patches]}


def census(root, *, ranks: list[Path] | None = None) -> Census:
    """Read the processor cases of *root*; refuse layouts not qualified."""
    root = Path(root)
    if any(path.is_dir() and re.fullmatch(r'processors\d+.*', path.name)
           for path in root.iterdir()) if root.is_dir() else False:
        raise RedistributeError('collated')
    processors = ranks if ranks is not None else processor_dirs(root)
    result = Census(ranks=len(processors))
    if not processors:
        return result
    numbers = [_rank_number(path) for path in processors]
    if ranks is None and numbers != list(range(len(processors))):
        raise RedistributeError('incomplete', details={'ranks': numbers})
    named_lists = None
    times = None
    processor_faces = 0
    for processor in processors:
        constant = processor / 'constant'
        if not (constant / 'polyMesh').is_dir():
            raise RedistributeError('incomplete', details={
                'rank': processor.name, 'missing': 'constant/polyMesh'})
        if any((path / 'polyMesh').is_dir() for path in constant.iterdir()
               if path.is_dir() and path.name != 'polyMesh'):
            raise RedistributeError('regions', details={'rank': processor.name})
        rank_times = _time_dirs(processor)
        if len(rank_times) > 1 or any(
                (processor / name / 'polyMesh').is_dir() for name in rank_times):
            raise RedistributeError('results_present', details={
                'rank': processor.name, 'times': rank_times})
        if times is None:
            times = rank_times
        elif rank_times != times:
            raise RedistributeError('incomplete', details={
                'rank': processor.name, 'times': rank_times, 'expected': times})
        mesh = _rank_mesh(processor)
        result.mesh_format = mesh['format']
        result.cells += mesh['cells']
        result.per_rank_cells.append(mesh['cells'])
        result.internal_faces += mesh['internal_faces']
        named = []
        for name, patch_type, faces in mesh['patches']:
            if patch_type in PROCESSOR_PATCH_TYPES:
                processor_faces += faces
                continue
            named.append(name)
            result.patches[name] = result.patches.get(name, 0) + faces
            result.patch_types[name] = patch_type
        if named_lists is None:
            named_lists = named
        elif named != named_lists:
            raise RedistributeError('inconsistent_patches', details={
                'rank': processor.name, 'patches': named,
                'expected': named_lists})
        poly = constant / 'polyMesh'
        for zone, count in zone_counts(poly, 'cellZones').items():
            result.cell_zones[zone] = result.cell_zones.get(zone, 0) + count
        for zone in zone_counts(poly, 'faceZones'):
            if zone not in result.face_zones:
                result.face_zones.append(zone)
        for zone in zone_counts(poly, 'pointZones'):
            if zone not in result.point_zones:
                result.point_zones.append(zone)
        for item in poly.iterdir():
            key = item.name[:-3] if item.name.endswith('.gz') else item.name
            if key not in KEPT_MESH_FILES and key not in result.unmapped:
                result.unmapped.append(key)
        fields = []
        if rank_times:
            fields = sorted(path.name for path in (processor / rank_times[0]).iterdir()
                            if path.is_file())
        if processor is processors[0]:
            result.fields = fields
        elif fields != result.fields:
            raise RedistributeError('incomplete', details={
                'rank': processor.name, 'fields': fields,
                'expected': result.fields})
        result.bytes += _tree_bytes(processor)
    result.time = times[0] if times else None
    # A face on a processor boundary is an internal face of the whole mesh,
    # listed once on each side.
    result.internal_faces += processor_faces // 2
    result.unmapped.sort()
    return result


def revision(case_path) -> str:
    """A cheap identity of the live processor cases (names, sizes, mtimes)."""
    digest = hashlib.sha256()
    for processor in processor_dirs(case_path):
        for path in sorted(processor.rglob('*')):
            if path.is_file():
                stat = path.stat()
                digest.update(
                    f'{path.relative_to(case_path).as_posix()}|{stat.st_size}|'
                    f'{stat.st_mtime_ns}\n'.encode('utf-8'))
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Assessment and preview
# --------------------------------------------------------------------------- #

def _internal_patch_present(result: Census) -> bool:
    return any(kind == 'internal' for kind in result.patch_types.values())


def pending(case_path) -> list[dict]:
    """Manifests of transactions still on disk (any state)."""
    root = transactions_root(case_path)
    if not root.is_dir():
        return []
    found = []
    for folder in sorted(root.iterdir()):
        manifest = _read_json(folder / 'manifest.json')
        if isinstance(manifest, dict):
            found.append(manifest)
        elif folder.is_dir():
            found.append({'id': folder.name, 'state': None})
    return found


def assess(case_path, target, *, cpu_limit: int | None = None,
           expected_revision: str | None = None) -> dict:
    """What redistributing *case_path* to *target* ranks would do.

    Never raises for a refusal: ``refusal`` is ``None`` or ``{code, reason,
    details}``, so the preview can show it beside the numbers.
    """
    case_path = Path(case_path)
    report = {'source_ranks': 0, 'target_ranks': None, 'np': None,
              'refusal': None, 'census': None, 'revision': None,
              'unmapped': [], 'disk': None, 'route': None}

    def refuse(code, **details):
        report['refusal'] = {'code': code, 'reason': REASONS.get(code, code),
                             'details': details}
        return report

    try:
        target = int(target)
    except (TypeError, ValueError):
        return refuse('target_invalid', target=target)
    report['target_ranks'] = target
    try:
        source = census(case_path)
    except RedistributeError as error:
        return refuse(error.code, **error.details)
    report['source_ranks'] = source.ranks
    report['census'] = source.to_dict()
    report['unmapped'] = [
        {'file': name, 'what': UNMAPPED.get(name, name)} for name in source.unmapped]
    if source.ranks == 0:
        report['route'] = 'mesh_in_parallel'
        return refuse('undecomposed')
    if source.ranks == 1:
        report['route'] = 'reconstruct_then_parallel'
        return refuse('single_rank')
    if target == 1:
        report['route'] = 'reconstruct_then_serial'
        return refuse('single_rank_target')
    if target < 1:
        return refuse('target_invalid', target=target)
    if target == source.ranks:
        return refuse('unchanged', ranks=target)
    np = max(source.ranks, target)
    report['np'] = np
    if cpu_limit and np > int(cpu_limit):
        return refuse('too_many_ranks', np=np, cpu_limit=int(cpu_limit))
    if source.fields and not _internal_patch_present(source):
        return refuse('fields_need_internal_patch', fields=list(source.fields),
                      time=source.time)
    waiting = [item.get('id') for item in pending(case_path)
               if item.get('id') not in _ACTIVE
               and not _is_husk(case_path, item)]
    if waiting:
        return refuse('pending_transaction', transactions=waiting)
    current = revision(case_path)
    report['revision'] = current
    if expected_revision and str(expected_revision) != current:
        return refuse('stale_revision', expected=str(expected_revision),
                      current=current)
    needed = 2 * source.bytes + _reserve_bytes()
    try:
        free = _free_bytes(case_path)
    except OSError:
        free = None
    report['disk'] = {'needed_bytes': needed, 'free_bytes': free,
                      'staging_bytes': source.bytes}
    if free is not None and free < needed:
        return refuse('insufficient_disk', needed_bytes=needed, free_bytes=free)
    return report


# --------------------------------------------------------------------------- #
# The transaction
# --------------------------------------------------------------------------- #

def folder_of(case_path, transaction_id: str) -> Path:
    return transactions_root(case_path) / str(transaction_id)


def load(case_path, transaction_id: str) -> dict | None:
    manifest = _read_json(folder_of(case_path, transaction_id) / 'manifest.json')
    return manifest if isinstance(manifest, dict) else None


def _save(case_path, manifest: dict) -> dict:
    manifest['updated_at'] = _now()
    _write_durably(folder_of(case_path, manifest['id']) / 'manifest.json', manifest)
    return manifest


def mark(case_path, transaction_id: str, state: str, **fields) -> dict:
    if state not in STATES:
        raise ValueError(f'unknown redistribution state: {state}')
    manifest = load(case_path, transaction_id)
    if manifest is None:
        raise RedistributeError('pending_transaction', 'the transaction record is gone',
                                details={'id': transaction_id})
    manifest.update(fields)
    manifest['state'] = state
    manifest.setdefault('history', []).append({'state': state, 'at': _now()})
    return _save(case_path, manifest)


def _control_dict(time: str | None, write_format: str) -> str:
    from foammesh.openfoam.dict_format import format_dictionary_file
    start = time if time is not None else '0'
    return format_dictionary_file('controlDict', {
        'application': 'redistributePar',
        'startFrom': 'startTime', 'startTime': start,
        'stopAt': 'endTime', 'endTime': start, 'deltaT': 1,
        'writeControl': 'timeStep', 'writeInterval': 1,
        'writeFormat': write_format if write_format in ('ascii', 'binary') else 'ascii',
        'writePrecision': 12, 'writeCompression': 'off',
        'timeFormat': 'general', 'timePrecision': 6,
        'runTimeModifiable': 'false'})


class _CopyFailed(Exception):
    """Carries a copy's OSError past ``copytree``, which would fold it into
    one ``shutil.Error`` and keep copying onto a full disk."""

    def __init__(self, error: OSError):
        super().__init__(str(error))
        self.error = error


def _copy_file(source, destination):
    try:
        return shutil.copy2(source, destination)
    except OSError as error:
        raise _CopyFailed(error) from None


def _ignore_unmapped(directory, names):
    if Path(directory).name != 'polyMesh':
        return ()
    return [name for name in names
            if (name[:-3] if name.endswith('.gz') else name) not in KEPT_MESH_FILES]


def prepare(case_path, target: int, *, write_decompose_dict,
            cpu_limit: int | None = None, expected_revision: str | None = None,
            operation: str = OPERATION) -> dict:
    """Admit the change and build the staging copy; the live cases are untouched.

    The caller holds the case EXCLUSIVE. ``write_decompose_dict(stage, ranks)``
    writes ``stage/system/decomposeParDict`` for the new count. Returns the
    PREPARED manifest. A copy that fails (disk full) leaves nothing behind.
    """
    case_path = Path(case_path)
    if not case_lease.held_by_current_flow(case_path, case_lease.EXCLUSIVE):
        raise RedistributeError('not_leased')
    report = assess(case_path, target, cpu_limit=cpu_limit,
                    expected_revision=expected_revision)
    if report['refusal'] is not None:
        refusal = report['refusal']
        raise RedistributeError(refusal['code'], refusal['reason'],
                                details=refusal['details'])
    source = Census(**report['census'])
    transaction_id = uuid4().hex[:12]
    folder = folder_of(case_path, transaction_id)
    stage = folder / 'stage'
    manifest = {
        'schema_version': SCHEMA_VERSION, 'id': transaction_id,
        'operation': operation, 'state': PREPARED, 'created_at': _now(),
        'source_ranks': source.ranks, 'target_ranks': int(target),
        'np': report['np'], 'revision': report['revision'],
        'time': source.time, 'fields': list(source.fields),
        'zones': {'cell': dict(source.cell_zones), 'face': list(source.face_zones),
                  'point': list(source.point_zones)},
        'region': 'region0', 'source': source.invariants(),
        'dropped': list(source.unmapped),
        'live': [path.name for path in processor_dirs(case_path)],
        'staged': [], 'settings_pending': False,
        'history': [{'state': PREPARED, 'at': _now()}],
    }
    _ACTIVE.add(transaction_id)
    try:
        # The record first: whatever the copy leaves behind is named by it.
        _save(case_path, manifest)
        stage.mkdir(parents=True, exist_ok=True)
        _write_text(stage / 'system' / 'controlDict',
                    _control_dict(source.time, source.mesh_format))
        write_decompose_dict(stage, int(target))
        for processor in processor_dirs(case_path):
            shutil.copytree(processor, stage / processor.name,
                            copy_function=_copy_file, ignore=_ignore_unmapped)
    except BaseException as error:
        _remove(folder)
        _ACTIVE.discard(transaction_id)
        if isinstance(error, _CopyFailed):
            error = error.error
        if isinstance(error, OSError):
            raise RedistributeError('insufficient_disk' if getattr(
                error, 'errno', None) == errno.ENOSPC else 'staging_failed',
                f'the staging copy could not be made: {error}',
                details={'error': str(error)}) from None
        raise
    return manifest


def stage_path(case_path, transaction_id: str) -> Path:
    return folder_of(case_path, transaction_id) / 'stage'


def validate(case_path, transaction_id: str) -> dict:
    """Check the staged result against the source and trim it to *target*.

    Surplus ranks must be zero-cell meshes; they are removed from the stage.
    Files the tool did not map are stripped from every rank. Raises
    ``RedistributeError('validation_failed')`` naming every difference.
    """
    manifest = load(case_path, transaction_id)
    stage = stage_path(case_path, transaction_id)
    target = int(manifest['target_ranks'])
    ranks = processor_dirs(stage)
    problems = []
    names = [path.name for path in ranks]
    wanted = [f'processor{number}' for number in range(target)]
    if names[:target] != wanted:
        problems.append({'check': 'ranks', 'expected': wanted, 'found': names})
    surplus = ranks[target:]
    for processor in surplus:
        try:
            cells = _rank_mesh(processor)['cells']
        except RedistributeError:
            cells = None
        if cells not in (0, None) or (
                cells is None and (processor / 'constant' / 'polyMesh').is_dir()):
            problems.append({'check': 'surplus_rank', 'rank': processor.name,
                             'cells': cells})
    if problems:
        raise RedistributeError('validation_failed',
                                'the redistributed mesh does not match the source',
                                details={'problems': problems})
    for processor in surplus:
        _remove(processor)
    stripped = strip_unmapped(ranks[:target])
    try:
        result = census(stage, ranks=ranks[:target])
    except RedistributeError as error:
        raise RedistributeError('validation_failed', str(error), details=dict(
            error.details, problems=[{'check': error.code}])) from None
    before, after = manifest['source'], result.invariants()
    for key in before:
        if before[key] != after.get(key):
            problems.append({'check': key, 'expected': before[key],
                             'found': after.get(key)})
    if problems:
        raise RedistributeError('validation_failed',
                                'the redistributed mesh does not match the source',
                                details={'problems': problems})
    dropped = sorted(set(manifest.get('dropped') or ()) | set(stripped))
    mark(case_path, transaction_id, VALIDATED, result=result.to_dict(),
         staged=[path.name for path in ranks[:target]], dropped=dropped,
         removed_surplus=[path.name for path in surplus])
    return {'census': result.to_dict(), 'dropped': dropped,
            'removed_surplus': [path.name for path in surplus]}


def strip_unmapped(ranks) -> list[str]:
    stripped = set()
    for processor in ranks:
        poly = Path(processor) / 'constant' / 'polyMesh'
        if not poly.is_dir():
            continue
        for item in list(poly.iterdir()):
            key = item.name[:-3] if item.name.endswith('.gz') else item.name
            if key not in KEPT_MESH_FILES:
                _remove(item)
                stripped.add(key)
    return sorted(stripped)


def _pending_gather(case_path: Path) -> Path:
    return case_path.joinpath(*PENDING_GATHER)


def publish(case_path, transaction_id: str) -> dict:
    """Swap the validated ranks in by rename; COMMITTED once they are live."""
    case_path = Path(case_path)
    manifest = load(case_path, transaction_id)
    if manifest is None or manifest.get('state') != VALIDATED:
        raise RedistributeError('pending_transaction',
                                'only a validated redistribution can be published',
                                details={'state': (manifest or {}).get('state')})
    folder = folder_of(case_path, transaction_id)
    retired = folder / 'retired'
    (retired / 'system').mkdir(parents=True, exist_ok=True)
    dictionary = case_path / 'system' / 'decomposeParDict'
    if dictionary.is_file():
        shutil.copy2(dictionary, retired / 'system' / 'decomposeParDict')
    marker = _pending_gather(case_path)
    if marker.is_file():
        shutil.copy2(marker, retired / 'pending-gather.json')
    manifest = mark(case_path, transaction_id, PUBLISHING,
                    live=[path.name for path in processor_dirs(case_path)])
    _forward(case_path, manifest)
    manifest = load(case_path, transaction_id)
    _clean(case_path, transaction_id)
    return manifest


def _forward(case_path: Path, manifest: dict) -> None:
    """Finish a PUBLISHING transaction: every step is idempotent."""
    folder = folder_of(case_path, manifest['id'])
    stage, retired = folder / 'stage', folder / 'retired'
    for name in manifest.get('live') or ():
        live, parked = case_path / name, retired / name
        if live.is_dir() and not parked.exists():
            live.rename(parked)
    for name in manifest.get('staged') or ():
        staged, live = stage / name, case_path / name
        if staged.is_dir():
            if live.exists():
                # Only a rank this transaction installed can be here now:
                # every live rank was retired above.
                _remove(live)
            staged.rename(live)
    staged_dict = stage / 'system' / 'decomposeParDict'
    if staged_dict.is_file():
        (case_path / 'system').mkdir(parents=True, exist_ok=True)
        temporary = case_path / 'system' / 'decomposeParDict.redistribute'
        shutil.copy2(staged_dict, temporary)
        os.replace(temporary, case_path / 'system' / 'decomposeParDict')
    marker = _pending_gather(case_path)
    if marker.is_file():
        document = _read_json(marker)
        if isinstance(document, dict):
            document['ranks'] = int(manifest['target_ranks'])
            _write_durably(marker, document)
    mark(case_path, manifest['id'], COMMITTED, settings_pending=True,
         committed_at=_now())


def _clean(case_path: Path, transaction_id: str) -> None:
    folder = folder_of(case_path, transaction_id)
    _remove(folder / 'retired')
    _remove(folder / 'stage')


def _rollback(case_path: Path, manifest: dict) -> None:
    """Undo a PUBLISHING transaction whose staged ranks cannot all be installed."""
    folder = folder_of(case_path, manifest['id'])
    retired = folder / 'retired'
    live_names = set(manifest.get('live') or ())
    for name in manifest.get('staged') or ():
        installed = case_path / name
        if installed.is_dir() and (name not in live_names or (retired / name).is_dir()):
            _remove(installed)
    for name in live_names:
        parked = retired / name
        if parked.is_dir() and not (case_path / name).exists():
            parked.rename(case_path / name)
    saved = retired / 'system' / 'decomposeParDict'
    if saved.is_file():
        shutil.copy2(saved, case_path / 'system' / 'decomposeParDict')
    marker = retired / 'pending-gather.json'
    if marker.is_file():
        shutil.copy2(marker, _pending_gather(case_path))
    _remove(folder)


def _can_forward(case_path: Path, manifest: dict) -> bool:
    folder = folder_of(case_path, manifest['id'])
    stage, retired = folder / 'stage', folder / 'retired'
    live_names = set(manifest.get('live') or ())
    staged = manifest.get('staged') or ()
    if not staged:
        return False
    for name in staged:
        if (stage / name).is_dir():
            continue
        installed = (case_path / name).is_dir() and (
            name not in live_names or (retired / name).is_dir())
        if not installed:
            return False
    return True


def finish(case_path, transaction_id: str) -> None:
    """The setting is published too: the transaction is over."""
    _remove(folder_of(case_path, transaction_id))
    _ACTIVE.discard(transaction_id)


def abandon(case_path, transaction_id: str) -> None:
    """Drop a transaction that never reached PUBLISHING; the source is live."""
    manifest = load(case_path, transaction_id)
    if manifest is not None and manifest.get('state') in (PUBLISHING, COMMITTED):
        raise RedistributeError('pending_transaction',
                                'a publishing redistribution is recovered, not abandoned')
    _remove(folder_of(case_path, transaction_id))
    _ACTIVE.discard(transaction_id)


def release(transaction_id: str) -> None:
    _ACTIVE.discard(transaction_id)


@contextlib.contextmanager
def active(transaction_id: str):
    """Mark a transaction live in this process for the duration."""
    _ACTIVE.add(transaction_id)
    try:
        yield
    finally:
        _ACTIVE.discard(transaction_id)


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #

def _is_husk(case_path, item: dict) -> bool:
    """A folder with no readable manifest and no retired ranks, not live here.

    What a stage removal that failed (a file still held open) leaves behind.
    Nothing live was touched -- PUBLISHING writes its record before the first
    rename, and ``retired/`` is its first rename -- so :func:`recover` discards
    it, and it must not refuse the next change meanwhile.
    """
    transaction_id = str(item.get('id') or '')
    if item.get('state') is not None or not transaction_id or transaction_id in _ACTIVE:
        return False
    folder = folder_of(case_path, transaction_id)
    if isinstance(_read_json(folder / 'manifest.json'), dict):
        return False  # a record without a state is damaged, not a husk
    return not (folder / 'retired').is_dir()


def _open_run_for(case_path: Path, transaction_id: str) -> list[str]:
    """Run records of this transaction's launch that are not closed yet.

    A LAUNCHED stage may still have a writer in WSL; the run-record gate
    decides that. Its stage is left until the gate has closed the record.
    """
    from .run_records import RunRecordStore, run_directory
    if not run_directory(case_path).is_dir():
        return []
    try:
        records = RunRecordStore(case_path).open_records()
    except OSError:
        return []
    return [str(record.get('run_id')) for record in records
            if str(record.get('staging') or '').endswith(transaction_id + '/stage')]


def _probe_nothing(_record: dict):
    from .run_records import Liveness
    return Liveness('unreachable', detail='the runtime was not asked')


def _close_runs_proven_dead(case_path: Path, transaction_id: str) -> list[str]:
    """Close this transaction's open runs whose writer is proven gone.

    Only the wrapper's Linux mirror is read (``exited`` with an rc, or the
    watcher's ``confirmed_dead: true``); the runtime is never asked, so case
    open does not wait on WSL. A run the mirror does not prove dead stays
    open -- its writer may still be alive (Plan 35) -- and is returned.
    """
    from .run_records import RecoveryGate, RunRecordStore, mirror_confirms_dead
    runs = _open_run_for(case_path, transaction_id)
    if not runs:
        return []
    store = RunRecordStore(case_path)
    dead = [run_id for run_id in runs
            if mirror_confirms_dead(store.linux_record(run_id))]
    if dead:
        try:
            RecoveryGate(case_path, probe=_probe_nothing).resolve(run_ids=dead)
        except Exception:  # noqa: BLE001 - the executor's gate retries
            logger.warning('runs %s of %s could not be closed', dead,
                           transaction_id, exc_info=True)
    return _open_run_for(case_path, transaction_id)


def recover(case_path) -> dict:
    """Settle every transaction a crash, kill or lost WSL left behind.

    * PREPARED, LAUNCHED, VALIDATED: the stage is discarded -- the live
      processor cases were never touched (``discarded``). A LAUNCHED stage
      whose run is still open in the run records waits for that record.
    * PUBLISHING: finished forward when every validated rank is still there
      (``completed``), otherwise rolled back to the retired ranks
      (``rolled_back``).
    * COMMITTED: the retired ranks are removed (``cleaned``), and the
      setting that was never published is returned in ``settings``.

    Transactions live in this process are left alone.
    """
    case_path = Path(case_path)
    report = {'discarded': [], 'completed': [], 'rolled_back': [],
              'cleaned': [], 'settings': [], 'waiting': [], 'damaged': []}
    root = transactions_root(case_path)
    if not root.is_dir():
        return report
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name in _ACTIVE:
            continue
        manifest = _read_json(folder / 'manifest.json')
        if not isinstance(manifest, dict):
            # No readable record: the copy never started, or the record was
            # being written. Nothing live was touched before PUBLISHING,
            # whose record is written before the first rename.
            if (folder / 'retired').is_dir():
                report['damaged'].append(folder.name)
                continue
            _remove(folder)
            report['discarded'].append(folder.name)
            continue
        state = manifest.get('state')
        transaction_id = str(manifest.get('id') or folder.name)
        if state in (PREPARED, LAUNCHED, VALIDATED):
            if _close_runs_proven_dead(case_path, transaction_id):
                report['waiting'].append(transaction_id)
                continue
            _remove(folder)
            report['discarded'].append(transaction_id)
        elif state == PUBLISHING:
            if _can_forward(case_path, manifest):
                _forward(case_path, manifest)
                _clean(case_path, transaction_id)
                report['completed'].append(transaction_id)
                report['settings'].append({
                    'id': transaction_id,
                    'target_ranks': int(manifest['target_ranks'])})
            else:
                _rollback(case_path, manifest)
                report['rolled_back'].append(transaction_id)
        elif state == COMMITTED:
            _clean(case_path, transaction_id)
            report['cleaned'].append(transaction_id)
            if manifest.get('settings_pending'):
                report['settings'].append({
                    'id': transaction_id,
                    'target_ranks': int(manifest['target_ranks'])})
            else:
                _remove(folder)
        else:
            report['damaged'].append(transaction_id)
    return report


def recover_at_open(case_path) -> dict:
    """Case-open recovery, under the exclusive lease (``busy`` if held)."""
    report = {'discarded': [], 'completed': [], 'rolled_back': [],
              'cleaned': [], 'settings': [], 'waiting': [], 'damaged': [],
              'busy': False}
    if not transactions_root(case_path).is_dir():
        return report
    try:
        with case_lease.hold(case_path, case_lease.EXCLUSIVE, RECOVER_OPERATION):
            report.update(recover(case_path))
    except case_lease.CaseBusyError:
        report['busy'] = True
    return report


def settle(case_path, *, active_run_ids=()) -> dict:
    """Settle what an interruption left behind before a new change.

    The caller holds the case EXCLUSIVE. A waiting transaction's open runs
    go through the run-record gate, which asks the runtime and closes only a
    run whose writer is confirmed dead; then :func:`recover` discards,
    finishes or rolls back as at case open. A run that may still be alive
    keeps its transaction waiting (Plan 35: it is never rolled back).
    """
    from .run_records import RecoveryGate
    case_path = Path(case_path)
    runs = [run_id for transaction_id in blocking(case_path)
            for run_id in _open_run_for(case_path, transaction_id)]
    if runs:
        try:
            RecoveryGate(case_path).resolve(active_run_ids=active_run_ids,
                                            run_ids=runs)
        except Exception:  # noqa: BLE001 - the transaction stays waiting
            logger.warning('runs %s could not be resolved', runs, exc_info=True)
    return recover(case_path)


def blocking(case_path) -> list[str]:
    """Transactions a mesher run must not start over (not live here)."""
    return [str(item.get('id')) for item in pending(case_path)
            if item.get('id') not in _ACTIVE]


# --------------------------------------------------------------------------- #
# The core-count setting
# --------------------------------------------------------------------------- #

def apply_setting(session, target: int, *, source=None,
                  reason: str = 'redistribute') -> dict:
    """Publish the core count the processor cases now hold.

    ``maxCpuCores`` is what `_stage_ranks` asks, so the next parallel stage
    reuses these ranks instead of re-decomposing; a serial mode would ignore
    them, so it becomes parallel. Written through the project state as one
    recorded change; the mesh-stage staleness a hand edit of the count
    carries is not published, because the mesh did not change.
    """
    from foammesh.core.project import Source
    changed = {}
    data = session.state.checkout()
    current = _setting(session, 'mesh/execution/maxCpuCores')
    if int(current or 0) != int(target):
        data.setValue('mesh/execution/maxCpuCores', int(target),
                      'mesh.execution.max_cpu_cores')
        changed['max_cpu_cores'] = {'before': int(current or 0), 'after': int(target)}
    mode = str(_setting(session, 'mesh/execution/mode') or 'auto').split('.')[-1].lower()
    if mode == 'serial':
        data.setValue('mesh/execution/mode', 'parallel', 'mesh.execution.mode')
        changed['mode'] = {'before': mode, 'after': 'parallel'}
    if changed:
        session.state.commit(
            data, action='change core count', source=source or Source.SYSTEM,
            target='mesh.execution.max_cpu_cores', reason=reason)
    return changed


def _setting(session, path: str):
    try:
        value = session.state.db.getValue(path)
    except Exception:  # noqa: BLE001
        return None
    return getattr(value, 'value', value)
