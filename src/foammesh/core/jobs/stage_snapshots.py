"""Immutable per-stage snappy mesh snapshots (Plan 37 UF5).

Each snappy phase reads the mesh the previous one left in ``constant/polyMesh``
and overwrites it, so a case holds one mesh and nothing else. This module
keeps what every stage produced: an independent copy, taken when the stage is
published, hashed, verified and never written again.

Layout::

    foammesh/stages/
        r0001/
            revision.json         {revision, inherits: {stage: revision}}
            blockMesh/            one published stage, laid out as a case:
                manifest.json     input, settings fingerprint, engine, runtime,
                                  decomposition, every file with size + sha256
                case.foam         (empty; lets a reader open the folder)
                constant/polyMesh/...   all mesh files: zones, sets,
                                        cellLevel, pointLevel, level0Edge
                0/cellLevel ...   the level fields, when snappy wrote them there
            castellation/ ...
        r0002/ ...                a re-run stage starts a new revision that
                                  inherits the stages before it
        active.json               which snapshot the live mesh is a copy of
        skipped.json              snapshots admission declined, and why
        .<stage>.partial-<id>     an interrupted copy; removed on recovery

Revisions. A stage is written into the newest revision unless that revision
already holds it (or a stage after it). Then a new revision is started that
*inherits* the earlier stages by reference, so an edited Snap sits beside the
Castellation it was replayed from without copying it twice. ``blockMesh``
builds from nothing and so starts a revision of its own.

Copies are real copies (``open``/``write``, never ``os.link``): a later stage
and a patch rename rewrite mesh files in place, and a hard link would carry
that write into the "immutable" copy. Copy and verify run in whatever thread
calls :func:`capture` -- the facade calls it through ``asyncio.to_thread`` --
and check a cancel event every :data:`CHUNK_BYTES`, so a cancel takes effect
within one chunk.

Publication order, and what a crash at each boundary leaves:

1. copy into ``.<stage>.partial-<id>``  -> partial folder; recovery removes it
2. verify the copy against the source   -> same
3. write ``manifest.json`` in the copy  -> same
4. rename the copy to ``<stage>``       -> a complete snapshot; ``active.json``
                                           lags, so the next replay copies it
                                           back in rather than trusting the
                                           live mesh
5. write ``active.json``                -> done

Gmsh has no stages to replay: its run folder already keeps the generated mesh
as the historical result, and nothing here is written for it.

Read API for "Compare stages" (UF11)
------------------------------------
``list_revisions(case)``
    ``[{revision, stages: [own stage names], inherits: {stage: revision}}]``
    oldest first.
``list_stages(case, revision=None)``
    the stages a revision resolves to (own + inherited), newest revision when
    *revision* is ``None``: ``[{stage, revision, path, mesh_path, bytes,
    captured_at, input, digest}]`` in stage order.
``stage_mesh_path(case, stage, revision=None)``
    ``<snapshot>/constant/polyMesh`` (the folder above it opens as a case), or
    ``None`` when that stage has no snapshot.
``read_manifest(case, stage, revision=None)`` / ``verify(case, stage, revision)``
    the manifest, and a re-hash of the copy against it.

Admission (``AdmissionPolicy``). A case quota and a free-space reserve, both
application settings (``stage_snapshot_quota_bytes``,
``disk_free_reserve_bytes`` in the user's settings file) with defaults. The
quota counts retained stages, the live mesh, the undo copy and copies in
flight. A snapshot is nonessential -- replay can regenerate any stage from the
nearest earlier one -- so one that does not fit is skipped and the replay
consequence is recorded in ``skipped.json``; the essential protections (the
unlock undo copy, a stage's input copy) are refused instead, with a typed
reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STAGES_DIR = ('foammesh', 'stages')
#: The mesh stages, in the order each reads the one before it.
STAGE_ORDER = ('blockMesh', 'castellation', 'snap', 'layers')
#: Which stage's output each stage consumes. ``snappyHexMesh`` is the
#: all-in-one run: it starts from the base grid like Castellation does.
PREDECESSOR = {'castellation': 'blockMesh', 'snappyHexMesh': 'blockMesh',
               'snap': 'castellation', 'layers': 'snap'}
STAGE_TASKS = {'blockMesh': 'snappy.base_grid',
               'castellation': 'snappy.castellation',
               'snappyHexMesh': 'snappy.castellation',
               'snap': 'snappy.snap', 'layers': 'snappy.layers'}
#: The level fields snappy may leave beside the mesh instead of in it.
LEVEL_FIELDS = ('cellLevel', 'pointLevel', 'level0Edge')
CHUNK_BYTES = 1024 * 1024

GIB = 1024 ** 3
DEFAULT_QUOTA_BYTES = 20 * GIB
DEFAULT_RESERVE_BYTES = 1 * GIB
QUOTA_SETTING = 'stage_snapshot_quota_bytes'
RESERVE_SETTING = 'disk_free_reserve_bytes'


class SnapshotError(RuntimeError):
    """A snapshot could not be taken, read or restored; ``code`` says why."""

    def __init__(self, message: str, *, code: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class SnapshotCancelled(SnapshotError):
    def __init__(self, message: str = 'the stage snapshot was cancelled'):
        super().__init__(message, code='cancelled')


class DiskAdmissionError(SnapshotError):
    """An essential protection copy does not fit (quota or free space)."""


# -- cancellation --------------------------------------------------------- #

_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, set] = {}


def _case_key(case_path) -> str:
    return os.path.normcase(str(Path(case_path).resolve()))


def cancel_token(case_path) -> threading.Event:
    """A fresh cancel event registered for *case_path* (see :func:`cancel_all`)."""
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.setdefault(_case_key(case_path), set()).add(event)
    return event


def release_token(case_path, event) -> None:
    with _CANCEL_LOCK:
        events = _CANCEL_EVENTS.get(_case_key(case_path))
        if events is not None:
            events.discard(event)
            if not events:
                _CANCEL_EVENTS.pop(_case_key(case_path), None)


def cancel_all(case_path) -> int:
    """Cancel every snapshot copy running for *case_path*; how many."""
    with _CANCEL_LOCK:
        events = list(_CANCEL_EVENTS.get(_case_key(case_path), ()))
    for event in events:
        event.set()
    return len(events)


# -- small helpers -------------------------------------------------------- #

def stages_root(case_path) -> Path:
    return Path(case_path).joinpath(*STAGES_DIR)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, document) -> None:
    """Durable: temp file, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise SnapshotCancelled()


def _hash_file(path: Path, cancel=None) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            _check(cancel)
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_hashing(source: Path, target: Path, cancel=None) -> tuple[int, str]:
    """Copy *source* to a new, independent *target*; ``(bytes, sha256)``."""
    digest = hashlib.sha256()
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, 'rb') as reader, open(target, 'xb') as writer:
        while True:
            _check(cancel)
            chunk = reader.read(CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, target)
    return size, digest.hexdigest()


def _remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        logger.exception('could not remove %s', path)


def _tree_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob('*'):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def files_digest(files: dict) -> str:
    """One hash naming a whole snapshot: its sorted ``path:sha256`` list."""
    text = '|'.join(f'{name}:{files[name][1]}' for name in sorted(files))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _live_identity(case_path):
    from foammesh.core.workflow.task_state_store import mesh_identity
    return mesh_identity(case_path)


# -- what a stage produced ------------------------------------------------ #

def mesh_sources(case_path) -> dict[str, Path]:
    """``{case-relative posix path: file}`` for everything a stage produced."""
    case = Path(case_path)
    sources = {}
    mesh = case / 'constant' / 'polyMesh'
    if mesh.is_dir():
        for path in sorted(mesh.rglob('*')):
            if path.is_file():
                sources[path.relative_to(case).as_posix()] = path
    zero = case / '0'
    for name in LEVEL_FIELDS:
        for candidate in (zero / name, zero / f'{name}.gz'):
            if candidate.is_file():
                sources[candidate.relative_to(case).as_posix()] = candidate
    return sources


def decomposition_state(case_path) -> dict:
    processors = sorted(path.name for path in Path(case_path).glob('processor*')
                        if path.is_dir())
    return {'processors': len(processors), 'decomposed': bool(processors),
            'reconstructed': (Path(case_path) / 'constant' / 'polyMesh'
                              / 'owner').exists()
            or (Path(case_path) / 'constant' / 'polyMesh'
                / 'owner.gz').exists()}


def settings_fingerprint(case_path, stage: str) -> str:
    """The dictionaries the stage ran from, hashed (``''`` when absent)."""
    system = Path(case_path) / 'system'
    names = (('blockMeshDict',) if stage == 'blockMesh'
             else ('snappyHexMeshDict', 'meshQualityDict'))
    digest = hashlib.sha256()
    found = False
    for name in names:
        path = system / name
        if path.is_file():
            found = True
            digest.update(name.encode('utf-8'))
            digest.update(path.read_bytes())
    return digest.hexdigest() if found else ''


# -- revisions ------------------------------------------------------------ #

def _revision_dirs(case_path) -> list[Path]:
    root = stages_root(case_path)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if path.is_dir() and path.name.startswith('r')
                  and path.name[1:].isdigit())


def _own_stages(folder: Path) -> list[str]:
    return [path.name for path in sorted(folder.iterdir())
            if path.is_dir() and not path.name.startswith('.')
            and (path / 'manifest.json').is_file()] if folder.is_dir() else []


def _revision_record(folder: Path) -> dict:
    record = _read_json(folder / 'revision.json')
    if not isinstance(record, dict):
        record = {}
    return {'revision': folder.name,
            'inherits': dict(record.get('inherits') or {}),
            'created_at': record.get('created_at')}


def _stage_rank(stage: str) -> int:
    if stage == 'snappyHexMesh':
        return STAGE_ORDER.index('castellation')
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)


def resolve(case_path, revision: str | None = None) -> dict[str, str]:
    """``{stage: revision holding it}`` for *revision* (newest by default)."""
    folders = [path for path in _revision_dirs(case_path) if _own_stages(path)]
    if not folders:
        return {}
    folder = (stages_root(case_path) / revision) if revision else folders[-1]
    if not folder.is_dir():
        return {}
    record = _revision_record(folder)
    resolved = {stage: holder for stage, holder in record['inherits'].items()
                if (stages_root(case_path) / holder / stage
                    / 'manifest.json').is_file()}
    for stage in _own_stages(folder):
        resolved[stage] = folder.name
    return resolved


def _ancestors(stage: str) -> list[str]:
    chain = []
    current = PREDECESSOR.get(stage)
    while current:
        chain.append(current)
        current = PREDECESSOR.get(current)
    return chain


def _target_revision(case_path, stage: str) -> tuple[Path, bool, dict]:
    """``(folder, is_new, inherits)`` for a capture of *stage*."""
    all_dirs = _revision_dirs(case_path)
    populated = [path for path in all_dirs if _own_stages(path)]
    number = int(all_dirs[-1].name[1:]) + 1 if all_dirs else 1
    new = stages_root(case_path) / f'r{number:04d}'
    if not populated:
        return new, True, {}
    current = populated[-1]
    resolved = resolve(case_path, current.name)
    if stage == 'blockMesh':
        return new, True, {}
    rank = _stage_rank(stage)
    occupied = stage in resolved or any(
        _stage_rank(other) >= rank for other in resolved if other != 'blockMesh')
    if not occupied:
        return current, False, {}
    inherits = {name: resolved[name] for name in _ancestors(stage)
                if name in resolved}
    return new, True, inherits


# -- admission ------------------------------------------------------------ #

@dataclass(frozen=True)
class AdmissionPolicy:
    quota_bytes: int = DEFAULT_QUOTA_BYTES
    reserve_bytes: int = DEFAULT_RESERVE_BYTES

    def to_dict(self) -> dict:
        return {'quota_bytes': self.quota_bytes,
                'reserve_bytes': self.reserve_bytes}


class _SettingKey:
    """Quacks like ``SettingKey``: the application store reads ``.value``."""

    def __init__(self, value: str):
        self.value = value


def load_policy() -> AdmissionPolicy:
    """The user's quota and reserve, or the defaults when unset/unreadable."""
    try:
        from foammesh.settings.app_settings import AppSettings
        settings = AppSettings()
        quota = settings._get(_SettingKey(QUOTA_SETTING), DEFAULT_QUOTA_BYTES)
        reserve = settings._get(_SettingKey(RESERVE_SETTING),
                                DEFAULT_RESERVE_BYTES)
        return AdmissionPolicy(max(0, int(quota)), max(0, int(reserve)))
    except Exception:                                       # noqa: BLE001
        logger.debug('snapshot admission settings unreadable', exc_info=True)
        return AdmissionPolicy()


def save_policy(policy: AdmissionPolicy) -> AdmissionPolicy:
    from foammesh.settings.app_settings import AppSettings
    settings = AppSettings()
    settings._set(_SettingKey(QUOTA_SETTING), int(policy.quota_bytes))
    settings._set(_SettingKey(RESERVE_SETTING), int(policy.reserve_bytes))
    return policy


def case_usage(case_path) -> dict:
    """What the quota counts, in bytes."""
    case = Path(case_path)
    root = stages_root(case)
    retained = pending = 0
    for folder in _revision_dirs(case):
        for child in folder.iterdir():
            size = _tree_bytes(child) if child.is_dir() else 0
            if child.name.startswith('.') and '.partial-' in child.name:
                pending += size
            else:
                retained += size
    active = _tree_bytes(case / 'constant' / 'polyMesh')
    undo = _tree_bytes(case / 'foammesh' / 'workflow' / 'pending')
    inputs = _tree_bytes(case / 'foammesh' / 'mesh-snapshots')
    usage = {'retained_stages': retained, 'active_mesh': active,
             'undo': undo, 'stage_inputs': inputs, 'pending_copies': pending}
    usage['total'] = sum(usage.values())
    usage['root'] = str(root)
    return usage


def admit(case_path, incoming_bytes: int, *, policy: AdmissionPolicy | None = None,
          disk_usage=None, essential: bool = False) -> dict:
    """Whether *incoming_bytes* more may be written for this case.

    ``essential`` copies (undo, a stage's input) are not held to the quota --
    refusing them would refuse the operation they protect -- only to the
    free-space reserve.
    """
    policy = policy or load_policy()
    usage = case_usage(case_path)
    disk_usage = disk_usage or shutil.disk_usage
    try:
        free = disk_usage(Path(case_path)).free
    except OSError:
        free = None
    result = {'admitted': True, 'reason': '', 'incoming_bytes': int(incoming_bytes),
              'free_bytes': free, 'usage': usage, 'policy': policy.to_dict()}
    if free is not None and free - incoming_bytes < policy.reserve_bytes:
        result.update(admitted=False, reason='free_space',
                      required_bytes=int(incoming_bytes) + policy.reserve_bytes)
    elif not essential and usage['total'] + incoming_bytes > policy.quota_bytes:
        result.update(admitted=False, reason='quota',
                      required_bytes=usage['total'] + int(incoming_bytes))
    return result


def require_admission(case_path, incoming_bytes: int, *, what: str,
                      policy: AdmissionPolicy | None = None,
                      disk_usage=None) -> dict:
    """:func:`admit` for an essential copy; raises :class:`DiskAdmissionError`."""
    verdict = admit(case_path, incoming_bytes, policy=policy,
                    disk_usage=disk_usage, essential=True)
    if not verdict['admitted']:
        raise DiskAdmissionError(
            f'not enough free disk space to keep {what}; free some space or '
            f'lower the reserve in the settings',
            code='insufficient_disk',
            details={key: verdict.get(key) for key in (
                'reason', 'required_bytes', 'free_bytes', 'policy')})
    return verdict


def skipped(case_path) -> list[dict]:
    document = _read_json(stages_root(case_path) / 'skipped.json')
    return list(document) if isinstance(document, list) else []


def _record_skip(case_path, entry: dict) -> None:
    entries = skipped(case_path)
    entries.append(entry)
    _write_json(stages_root(case_path) / 'skipped.json', entries[-200:])


def _skip_consequence(case_path, stage: str) -> str:
    after = {'blockMesh': 'castellation', 'castellation': 'snap',
             'snap': 'layers'}.get(stage)
    if after is None:
        return (f'the {stage} result is kept only as the live mesh; it is not '
                'available to compare once a later run replaces it')
    nearest = next((name for name in _ancestors(stage)
                    if name in resolve(case_path)), None)
    source = f'the kept {nearest} snapshot' if nearest else 'the base grid'
    return (f're-running {after} regenerates {stage} from {source} first, '
            'which takes as long as running it did')


# -- capture -------------------------------------------------------------- #

def recover(case_path) -> list[str]:
    """Finish what a crash interrupted; the names removed or put back.

    Interrupted snapshot copies are removed. An interrupted :func:`restore`
    is rolled back: when the live mesh was moved aside and its replacement
    never moved in, the moved-aside mesh goes back (``active.json`` was not
    written, so the next replay restores again).
    """
    removed = []
    mesh = Path(case_path) / 'constant' / 'polyMesh'
    retired = mesh.with_name('polyMesh.replay-retired')
    staging = mesh.with_name('polyMesh.replay-staging')
    if retired.is_dir() and not mesh.exists():
        os.replace(retired, mesh)
        removed.append('constant/polyMesh (put back)')
    for leftover in (retired, staging):
        if leftover.exists():
            _remove(leftover)
            removed.append(f'constant/{leftover.name}')
    for folder in _revision_dirs(case_path):
        for child in list(folder.iterdir()):
            if child.name.startswith('.') and '.partial-' in child.name:
                _remove(child)
                removed.append(f'{folder.name}/{child.name}')
    return removed


def capture(case_path, stage: str, *, engine_id: str = 'snappy',
            runtime: dict | None = None, policy: AdmissionPolicy | None = None,
            disk_usage=None, cancel=None, fault=None,
            clock=time.monotonic) -> dict:
    """Keep what *stage* just produced; the manifest, or why it was skipped.

    ``fault(point)`` is called at each publication boundary (``copied``,
    ``verified``, ``manifest``, ``published``, ``active``) -- a test hook for
    crash injection. Cancelling raises :class:`SnapshotCancelled` and leaves
    nothing behind.
    """
    case = Path(case_path)
    recover(case)
    sources = mesh_sources(case)
    decomposition = decomposition_state(case)
    base = {'stage': stage, 'engine_id': engine_id,
            'task_id': STAGE_TASKS.get(stage), 'at': _now()}
    if not any(name.startswith('constant/polyMesh/') for name in sources):
        return dict(base, captured=False, skipped=True, reason='no_mesh')
    if not decomposition['reconstructed']:
        entry = dict(base, reason='decomposed',
                     consequence='captured when the mesh is reconstructed')
        _record_skip(case, entry)
        return dict(entry, captured=False, skipped=True)
    incoming = sum(path.stat().st_size for path in sources.values())
    verdict = admit(case, incoming, policy=policy, disk_usage=disk_usage)
    if not verdict['admitted']:
        entry = dict(base, reason=verdict['reason'], bytes=incoming,
                     consequence=_skip_consequence(case, stage))
        _record_skip(case, entry)
        return dict(entry, captured=False, skipped=True, admission=verdict)
    folder, is_new, inherits = _target_revision(case, stage)
    resolved_before = resolve(case)
    predecessor = PREDECESSOR.get(stage)
    input_ref = None
    if predecessor and predecessor in (inherits or resolved_before):
        holder = (inherits or resolved_before)[predecessor]
        manifest = read_manifest(case, predecessor, holder)
        input_ref = {'stage': predecessor, 'revision': holder,
                     'digest': (manifest or {}).get('digest')}
    if is_new:
        folder.mkdir(parents=True, exist_ok=True)
        _write_json(folder / 'revision.json', {
            'schema_version': SCHEMA_VERSION, 'revision': folder.name,
            'inherits': inherits, 'created_at': _now()})
    target = folder / stage
    if target.exists():
        raise SnapshotError(f'{folder.name}/{stage} is already kept and is '
                            'never rewritten', code='snapshot_exists')
    staging = folder / f'.{stage}.partial-{uuid.uuid4().hex[:12]}'
    started = clock()
    try:
        files = {}
        for name, path in sources.items():
            size, digest = _copy_hashing(path, staging / name, cancel)
            files[name] = [size, digest]
        if fault:
            fault('copied')
        copied = clock()
        for name, (size, digest) in files.items():
            if _hash_file(staging / name, cancel) != digest:
                raise SnapshotError(f'the copy of {name} does not match the mesh',
                                    code='verify_failed', details={'file': name})
        if fault:
            fault('verified')
        verified = clock()
        manifest = {
            'schema_version': SCHEMA_VERSION, 'revision': folder.name,
            'stage': stage, 'task_id': STAGE_TASKS.get(stage),
            'engine_id': engine_id, 'runtime': dict(runtime or {}),
            'input': input_ref,
            'settings_fingerprint': settings_fingerprint(case, stage),
            'decomposition': decomposition,
            'files': files, 'bytes': incoming, 'digest': files_digest(files),
            'mesh_identity': _live_identity(case),
            'captured_at': _now(),
            'copy_seconds': round(copied - started, 3),
            'verify_seconds': round(verified - copied, 3),
        }
        _write_json(staging / 'manifest.json', manifest)
        (staging / 'case.foam').write_bytes(b'')
        if fault:
            fault('manifest')
        os.replace(staging, target)
    except BaseException:
        _remove(staging)
        raise
    if fault:
        fault('published')
    _write_json(stages_root(case) / 'active.json', {
        'revision': folder.name, 'stage': stage, 'digest': manifest['digest'],
        'mesh_identity': manifest['mesh_identity'], 'at': _now()})
    if fault:
        fault('active')
    return dict(manifest, captured=True, skipped=False, path=str(target))


# -- reading (UF11) ------------------------------------------------------- #

def read_manifest(case_path, stage: str, revision: str | None = None) -> dict | None:
    holder = revision or resolve(case_path).get(stage)
    if not holder:
        return None
    manifest = _read_json(stages_root(case_path) / holder / stage / 'manifest.json')
    return manifest if isinstance(manifest, dict) else None


def list_revisions(case_path) -> list[dict]:
    revisions = []
    for folder in _revision_dirs(case_path):
        own = _own_stages(folder)
        if not own:
            continue
        record = _revision_record(folder)
        revisions.append({'revision': folder.name, 'stages': own,
                          'inherits': record['inherits'],
                          'created_at': record['created_at']})
    return revisions


def list_stages(case_path, revision: str | None = None) -> list[dict]:
    resolved = resolve(case_path, revision)
    listed = []
    for stage in sorted(resolved, key=_stage_rank):
        holder = resolved[stage]
        folder = stages_root(case_path) / holder / stage
        manifest = read_manifest(case_path, stage, holder) or {}
        listed.append({'stage': stage, 'revision': holder, 'path': str(folder),
                       'mesh_path': str(folder / 'constant' / 'polyMesh'),
                       'bytes': manifest.get('bytes'),
                       'captured_at': manifest.get('captured_at'),
                       'input': manifest.get('input'),
                       'digest': manifest.get('digest')})
    return listed


def stage_mesh_path(case_path, stage: str, revision: str | None = None) -> Path | None:
    holder = revision if revision and (
        stages_root(case_path) / revision / stage).is_dir() else (
        resolve(case_path, revision).get(stage))
    if not holder:
        return None
    path = stages_root(case_path) / holder / stage / 'constant' / 'polyMesh'
    return path if path.is_dir() else None


def verify(case_path, stage: str, revision: str | None = None, *,
           cancel=None) -> dict:
    """Re-hash a snapshot against its manifest."""
    holder = revision or resolve(case_path).get(stage)
    manifest = read_manifest(case_path, stage, holder)
    if manifest is None:
        return {'ok': False, 'missing': True, 'stage': stage, 'revision': holder}
    folder = stages_root(case_path) / holder / stage
    mismatched, missing = [], []
    for name, (size, digest) in manifest['files'].items():
        path = folder / name
        if not path.is_file():
            missing.append(name)
        elif path.stat().st_size != size or _hash_file(path, cancel) != digest:
            mismatched.append(name)
    return {'ok': not (mismatched or missing), 'stage': stage,
            'revision': holder, 'mismatched': mismatched, 'missing': missing}


def active(case_path) -> dict | None:
    document = _read_json(stages_root(case_path) / 'active.json')
    return document if isinstance(document, dict) else None


def live_mesh_is(case_path, stage: str, revision: str) -> bool:
    """Whether the live mesh is still the copy of that snapshot (cheap)."""
    record = active(case_path)
    return bool(record and record.get('stage') == stage
                and record.get('revision') == revision
                and record.get('mesh_identity')
                and record.get('mesh_identity') == _live_identity(case_path))


# -- replay --------------------------------------------------------------- #

def plan_replay(case_path, stage: str, *, valid_stages=None) -> dict:
    """Where *stage* must start from, so it never runs on its own output.

    ``valid_stages`` are the stages whose published result still stands (the
    workflow graph knows; an edited stage is not valid). Returns
    ``{stage, input, restore: {stage, revision} | None, regenerate: [...],
    live_is_input: bool}``: restore that snapshot, then run ``regenerate`` in
    order, then *stage*. ``blockMesh`` needs nothing.
    """
    predecessor = PREDECESSOR.get(stage)
    plan = {'stage': stage, 'input': predecessor, 'restore': None,
            'regenerate': [], 'live_is_input': False}
    if predecessor is None:
        return plan
    valid = None if valid_stages is None else set(valid_stages)
    resolved = resolve(case_path)
    chain = [predecessor, *_ancestors(predecessor)]
    regenerate = []
    for name in chain:
        holder = resolved.get(name)
        if holder and (valid is None or name in valid):
            plan['restore'] = {'stage': name, 'revision': holder}
            plan['live_is_input'] = (name == predecessor
                                     and live_mesh_is(case_path, name, holder))
            break
        regenerate.insert(0, name)
    plan['regenerate'] = regenerate
    return plan


def restore(case_path, stage: str, revision: str, *, cancel=None,
            policy: AdmissionPolicy | None = None,
            disk_usage=None, fault=None) -> dict:
    """Make the live mesh an exact copy of a kept snapshot (verified first).

    ``fault('retired')`` fires after the live mesh is moved aside and before
    the copy moves in -- the one boundary :func:`recover` must roll back.
    """
    case = Path(case_path)
    checked = verify(case, stage, revision, cancel=cancel)
    if not checked['ok']:
        raise SnapshotError(f'the kept {stage} snapshot is damaged',
                            code='snapshot_damaged', details=checked)
    manifest = read_manifest(case, stage, revision)
    require_admission(case, int(manifest.get('bytes') or 0),
                      what=f'the {stage} mesh this stage starts from',
                      policy=policy, disk_usage=disk_usage)
    folder = stages_root(case) / revision / stage
    mesh = case / 'constant' / 'polyMesh'
    staging = mesh.with_name('polyMesh.replay-staging')
    _remove(staging)
    try:
        for name in manifest['files']:
            if name.startswith('constant/polyMesh/'):
                _copy_hashing(folder / name,
                              staging / name[len('constant/polyMesh/'):], cancel)
    except BaseException:
        _remove(staging)
        raise
    retired = mesh.with_name('polyMesh.replay-retired')
    _remove(retired)
    if mesh.exists():
        os.replace(mesh, retired)
    if fault:
        fault('retired')
    os.replace(staging, mesh)
    _remove(retired)
    zero = case / '0'
    for name in LEVEL_FIELDS:
        for candidate in (zero / name, zero / f'{name}.gz'):
            if candidate.is_file():
                candidate.unlink()
    for name in manifest['files']:
        if name.startswith('0/'):
            _copy_hashing(folder / name, case / name, cancel)
    _write_json(stages_root(case) / 'active.json', {
        'revision': revision, 'stage': stage, 'digest': manifest['digest'],
        'mesh_identity': _live_identity(case), 'at': _now(),
        'restored': True})
    return {'stage': stage, 'revision': revision, 'bytes': manifest['bytes']}
