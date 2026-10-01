"""Unlock a step and undo the unlock (Plan 37 UF5, DP-1041/DP-1042).

Unlocking a step reopens it and every step that depends on it. The mesh on
disk stays where it is ("previous result -- edits not applied"); what changes
is the record of which settings it was made from. Undo -- **Restore previous
mesh and settings** -- puts the settings, the task states, the publication
record and the mesh back together, and stays available until a new mesher
run is admitted.

Neither can be one filesystem transaction, so both are written as an
explicit, durable two-phase operation under ``foammesh/workflow/pending``::

    pending/<operation id>/
        manifest.json      state: prepared -> committed (unlock)
                                  restoring -> gone     (undo)
        settings.yaml      the authored settings at unlock
        tasks.json         the task-state file at unlock (absent = none)
        publications.json  the publication record at unlock (absent = none)
        mesh/              an independent copy of constant/polyMesh
        geometry/          an independent copy of foammesh/geometry (the
                           imported, repaired and prepared surfaces)
        surfaces/          the project's in-memory geometry surfaces, as the
                           caller's ``capture_surfaces`` wrote them

``foammesh/workflow/undo.json`` names the committed operation that undo
restores. There is at most one: a new unlock replaces it only after its own
operation is durable. :func:`recover` finishes or rolls back whatever a crash
left half-done, and runs before any unlock, undo or mesher run.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import case_lease
from .run_records import _write_durably

logger = logging.getLogger(__name__)

PENDING_DIR = ('foammesh', 'workflow', 'pending')
UNDO_POINTER = ('foammesh', 'workflow', 'undo.json')
PREPARED = 'prepared'
COMMITTED = 'committed'
RESTORING = 'restoring'
SCHEMA_VERSION = 1
#: Free space kept in reserve beyond the copy itself.
DISK_RESERVE_BYTES = 64 * 1024 * 1024
UNLOCK_OPERATION = 'mesh.workflow.unlock'
UNDO_OPERATION = 'mesh.workflow.undo_unlock'


class UnlockError(RuntimeError):
    """An unlock or undo that changed nothing, and why."""

    def __init__(self, message: str, *, code: str = 'unlock_refused',
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


# --------------------------------------------------------------------------- #
# Paths and small helpers
# --------------------------------------------------------------------------- #

def pending_root(case_path) -> Path:
    return Path(case_path).joinpath(*PENDING_DIR)


def undo_pointer_path(case_path) -> Path:
    return Path(case_path).joinpath(*UNDO_POINTER)


def _mesh_dir(case_path) -> Path:
    return Path(case_path) / 'constant' / 'polyMesh'


def _geometry_dir(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'geometry'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def tree_files(root: Path) -> dict:
    """``{relative posix path: (bytes, sha256)}`` for every file under *root*."""
    files = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob('*')):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = (
                path.stat().st_size, _sha256(path))
    return files


def tree_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(path.stat().st_size for path in root.rglob('*') if path.is_file())


def _copy_tree(source: Path, destination: Path) -> None:
    """An independent copy -- never a hard link (a later stage or a patch
    rename rewrites mesh files in place)."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        logger.exception('could not remove %s', path)


def _undo_record(case_path) -> dict | None:
    pointer = _read_json(undo_pointer_path(case_path))
    if not isinstance(pointer, dict) or not pointer.get('operation_id'):
        return None
    folder = pending_root(case_path) / str(pointer['operation_id'])
    manifest = _read_json(folder / 'manifest.json')
    if not isinstance(manifest, dict) or manifest.get('state') not in (
            COMMITTED, RESTORING):
        return None
    return dict(manifest, folder=str(folder))


def undo_available(case_path) -> dict | None:
    """What undo would restore, or ``None``."""
    record = _undo_record(case_path)
    if record is None:
        return None
    return {key: record.get(key) for key in (
        'operation_id', 'task_id', 'title', 'scope', 'titles', 'committed_at',
        'engine_id', 'mesh_bytes')}


# --------------------------------------------------------------------------- #
# Preview (what the confirmation names)
# --------------------------------------------------------------------------- #

def preview_unlock(case_path, store, task_id: str) -> dict:
    """The tasks, the artifacts and the disk cost of unlocking *task_id*."""
    descriptor = store.descriptor
    descriptor.task(task_id)
    locked = store.locked_tasks()
    scope = store.unlock_scope(task_id)
    lock_state = store.lock_state()
    mesh = _mesh_dir(case_path)
    mesh_bytes = tree_bytes(mesh)
    geometry_bytes = tree_bytes(_geometry_dir(case_path))
    artifacts = []
    if mesh.is_dir():
        artifacts.append({'path': 'constant/polyMesh', 'bytes': mesh_bytes,
                          'becomes': 'previous result — kept on disk, edits '
                                     'not applied, restorable by undo'})
    if geometry_bytes:
        artifacts.append({'path': 'foammesh/geometry', 'bytes': geometry_bytes,
                          'becomes': 'kept as it is; a copy is kept so undo '
                                     'restores it after geometry edits'})
    try:
        free = shutil.disk_usage(Path(case_path)).free
    except OSError:
        free = None
    existing = undo_available(case_path)
    reserve = _reserve_bytes()
    exported = _exports_of_live_mesh(case_path)
    return {
        'task_id': task_id,
        'title': descriptor.task(task_id).title,
        'locked': task_id in locked,
        'scope': scope,
        'titles': [descriptor.task(item).title for item in scope],
        'published': [item for item in scope
                      if (lock_state.get(item) or {}).get('locked')],
        'artifacts': artifacts,
        'disk_cost_bytes': mesh_bytes + geometry_bytes,
        'free_bytes': free,
        'fits': free is None or free >= (mesh_bytes + geometry_bytes
                                         + reserve),
        'reserve_bytes': reserve,
        'replaces_undo': existing,
        'publication_revision': int(store.publications().get('revision') or 0),
        'replay': replay_point(case_path, store, scope),
        'stale_exports': [item for item in exported if item.get('case_managed')],
        'external_exports': [item for item in exported
                             if not item.get('case_managed')],
    }


def _reserve_bytes() -> int:
    from . import stage_snapshots
    return max(DISK_RESERVE_BYTES, stage_snapshots.load_policy().reserve_bytes)


def _exports_of_live_mesh(case_path) -> list[dict]:
    from . import export_provenance
    try:
        return export_provenance.of_live_mesh(case_path)
    except (OSError, ValueError):
        return []


def replay_point(case_path, store, scope) -> dict | None:
    """Which kept stage snapshot re-running the unlocked stages starts from.

    Plan 37 UF5. The first mesh stage in *scope* is the first one re-run; the
    stages before it keep their published results, so their newest kept
    snapshot is where it starts. ``None`` for an engine with no stages to
    replay (Gmsh keeps its generated mesh in its run folder instead).
    """
    from . import stage_snapshots

    by_task = {task: stage for stage, task in stage_snapshots.STAGE_TASKS.items()
               if stage != 'snappyHexMesh'}
    order = [task.task_id for task in store.descriptor.ordered_tasks()]
    first = next((by_task[task] for task in order
                  if task in scope and task in by_task), None)
    if first is None:
        return None
    valid = [stage for stage, task in stage_snapshots.STAGE_TASKS.items()
             if task not in scope]
    plan = stage_snapshots.plan_replay(case_path, first, valid_stages=valid)
    source = plan['restore']
    if source is not None:
        source = dict(source, path=str(stage_snapshots.stages_root(case_path)
                                       / source['revision'] / source['stage']))
    return {'stage': first, 'from': source, 'regenerate': plan['regenerate']}


def settings_changes(db, settings_text: str) -> list[str]:
    """The settings leaves undo would put back (edits made since unlock)."""
    import yaml

    from foammesh.support.simple_db.simple_db import _diffLeaves
    captured = db.validateData(yaml.full_load(settings_text), fillWithDefault=True)
    return sorted(str(leaf) for leaf in _diffLeaves(db._schema, db._content, captured))


def preview_undo(case_path, db) -> dict:
    record = _undo_record(case_path)
    if record is None:
        raise UnlockError('there is no unlock to undo', code='undo_unavailable')
    folder = Path(record['folder'])
    text = (folder / 'settings.yaml').read_text(encoding='utf-8')
    return dict(undo_available(case_path),
                discarded_settings=settings_changes(db, text),
                restores_mesh=(folder / 'mesh').is_dir(),
                restores_geometry=(folder / 'geometry').is_dir())


# --------------------------------------------------------------------------- #
# Unlock
# --------------------------------------------------------------------------- #

def _preflight(case_path, operation: str, recovery=None) -> None:
    from .run_records import RecoveryGate
    verdict = RecoveryGate(case_path, recovery=recovery).check()
    if verdict.blocked:
        raise UnlockError(
            f'{operation} was not started: an earlier run of this case is '
            'not resolved yet', code='case_busy',
            details={'records': [record.get('run_id') for record in verdict.records]})


def unlock(case_path, store, task_id: str, *, settings_text: str,
           expected_revision: int | None = None, recovery=None,
           disk_usage=shutil.disk_usage, capture_surfaces=None) -> dict:
    """Reopen *task_id* and its dependants, keeping one complete undo.

    Refuses -- changing nothing -- when the task is not locked, the
    publication record moved since the confirmation was drawn, the case is
    held by a run or a check, an earlier run is unresolved, or the undo copy
    would not fit on disk.

    ``capture_surfaces(directory)`` writes the project's in-memory geometry
    surfaces (which live in the configuration, not on disk) for undo.
    """
    case_path = Path(case_path)
    with _exclusive(case_path, UNLOCK_OPERATION):
        recover(case_path, store)
        _preflight(case_path, UNLOCK_OPERATION, recovery)
        descriptor = store.descriptor
        descriptor.task(task_id)
        if not store.is_locked(task_id):
            raise UnlockError(f'{descriptor.task(task_id).title} is not locked',
                              code='not_locked', details={'task_id': task_id})
        revision = int(store.publications().get('revision') or 0)
        if expected_revision is not None and int(expected_revision) != revision:
            raise UnlockError(
                'the published results changed since this was confirmed',
                code='revision_conflict',
                details={'expected': int(expected_revision), 'actual': revision})
        mesh = _mesh_dir(case_path)
        mesh_bytes = tree_bytes(mesh)
        geometry = _geometry_dir(case_path)
        geometry_bytes = tree_bytes(geometry)
        try:
            free = disk_usage(case_path).free
        except OSError:
            free = None
        required = mesh_bytes + geometry_bytes + _reserve_bytes()
        if free is not None and free < required:
            raise UnlockError(
                'not enough free disk space to keep the previous mesh for undo',
                code='insufficient_disk',
                details={'required_bytes': required, 'free_bytes': free})

        operation_id = uuid4().hex
        folder = pending_root(case_path) / operation_id
        scope = store.unlock_scope(task_id)
        manifest = {
            'schema_version': SCHEMA_VERSION, 'operation': UNLOCK_OPERATION,
            'operation_id': operation_id, 'state': PREPARED,
            'engine_id': descriptor.engine_id, 'task_id': task_id,
            'title': descriptor.task(task_id).title, 'scope': scope,
            'titles': [descriptor.task(item).title for item in scope],
            'created_at': _now(), 'publication_revision': revision,
            'mesh_bytes': mesh_bytes, 'geometry_bytes': geometry_bytes,
        }
        try:
            folder.mkdir(parents=True)
            tasks_text, publications_text = store.document_texts()
            _write_text(folder / 'settings.yaml', settings_text)
            for name, text in (('tasks.json', tasks_text),
                               ('publications.json', publications_text)):
                if text is not None:
                    _write_text(folder / name, text)
            if mesh.is_dir():
                _copy_tree(mesh, folder / 'mesh')
                manifest['mesh_files'] = {
                    name: list(value) for name, value in
                    tree_files(folder / 'mesh').items()}
            if geometry.is_dir():
                _copy_tree(geometry, folder / 'geometry')
                manifest['geometry_files'] = {
                    name: list(value) for name, value in
                    tree_files(folder / 'geometry').items()}
            if capture_surfaces is not None:
                capture_surfaces(folder / 'surfaces')
                manifest['surfaces'] = True
            # The manifest is written last: a folder without one is a copy
            # that never became an operation, and recovery deletes it.
            _write_durably(folder / 'manifest.json', manifest)
        except (OSError, ValueError) as error:
            _remove(folder)
            raise UnlockError(f'the undo copy could not be written: {error}',
                              code='undo_not_durable') from error

        # The exports of the mesh this unlock discards are marked stale in
        # their provenance record -- the exported files are never touched.
        # Read before the store moves, so a crash below leaves them unmarked
        # rather than marked for an unlock that never happened.
        from . import export_provenance
        result = store.unlock(task_id, operation_id=operation_id)
        try:
            staled = export_provenance.mark_stale(
                case_path, reason=f'unlocked {descriptor.task(task_id).title}',
                operation_id=operation_id)
        except (OSError, ValueError):
            staled = []
        manifest.update(state=COMMITTED, committed_at=_now(),
                        invalidated=list(result['invalidated']),
                        deactivated=list(result['deactivated']),
                        staled_exports=[item['destination'] for item in staled])
        _write_durably(folder / 'manifest.json', manifest)
        _adopt(case_path, operation_id)
        return dict(result, operation_id=operation_id,
                    undo=undo_available(case_path), staled_exports=staled)


def _adopt(case_path: Path, operation_id: str) -> None:
    """Make *operation_id* the one undo, then drop the one it replaces."""
    previous = _read_json(undo_pointer_path(case_path))
    _write_durably(undo_pointer_path(case_path),
                   {'operation_id': operation_id, 'adopted_at': _now()})
    if isinstance(previous, dict) and previous.get('operation_id') not in (
            None, operation_id):
        _remove(pending_root(case_path) / str(previous['operation_id']))


@contextlib.contextmanager
def _exclusive(case_path: Path, operation: str):
    """The case, exclusively, now -- a running mesher or check refuses."""
    try:
        with case_lease.hold(case_path, case_lease.EXCLUSIVE, operation):
            yield
    except case_lease.CaseBusyError as busy:
        raise UnlockError(str(busy), code='case_busy',
                          details={'holders': list(busy.holders)}) from None


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #

def undo(case_path, store, *, restore_settings, recovery=None,
         restore_surfaces=None) -> dict:
    """Restore the settings, task states, publications and mesh of the undo.

    ``restore_settings(text)`` puts the captured settings back through the
    project state (one recorded change). The mesh is swapped in from the
    verified copy; the copy is checked against its hashes first, so a
    damaged undo refuses instead of restoring a damaged mesh. The geometry
    tree is restored the same way, and ``restore_surfaces(directory)`` puts
    the captured in-memory surfaces back before the settings that name them.
    """
    case_path = Path(case_path)
    with _exclusive(case_path, UNDO_OPERATION):
        resumed = recover(case_path, store, restore_settings=restore_settings,
                          restore_surfaces=restore_surfaces)['resumed']
        record = _undo_record(case_path)
        if record is None:
            if resumed:
                # An interrupted undo was the undo asked for: it is finished.
                return {'operation_id': resumed[-1], 'task_id': None,
                        'restored_mesh': True, 'resumed': True,
                        'locked': store.locked_tasks()}
            raise UnlockError('there is no unlock to undo', code='undo_unavailable')
        _preflight(case_path, UNDO_OPERATION, recovery)
        folder = Path(record['folder'])
        for key, name in (('mesh_files', 'mesh'), ('geometry_files', 'geometry')):
            expected = record.get(key)
            if expected is None:
                continue
            actual = {item: list(value) for item, value in
                      tree_files(folder / name).items()}
            if actual != expected:
                raise UnlockError(f'the saved {name} for undo is damaged; '
                                  'nothing was restored', code='undo_damaged',
                                  details={'copy': name})
        manifest = {key: value for key, value in record.items() if key != 'folder'}
        manifest.update(state=RESTORING, restoring_at=_now())
        _write_durably(folder / 'manifest.json', manifest)
        return _finish_undo(case_path, store, folder, manifest, restore_settings,
                            restore_surfaces)


def _swap_in(saved: Path, target: Path) -> bool:
    """Replace *target* with a fresh copy of *saved* (staged, then renamed)."""
    if not saved.is_dir():
        return False
    staging = target.with_name(target.name + '.undo-staging')
    _copy_tree(saved, staging)
    retired = target.with_name(target.name + '.undo-retired')
    _remove(retired)
    if target.exists():
        os.replace(target, retired)
    os.replace(staging, target)
    _remove(retired)
    return True


def _finish_undo(case_path: Path, store, folder: Path, manifest: dict,
                 restore_settings, restore_surfaces=None) -> dict:
    """Idempotent: a crash anywhere in here is finished by :func:`recover`."""
    restored_mesh = _swap_in(folder / 'mesh', _mesh_dir(case_path))
    restored_geometry = _swap_in(folder / 'geometry', _geometry_dir(case_path))
    if restore_surfaces is not None and (folder / 'surfaces').is_dir():
        restore_surfaces(folder / 'surfaces')
    settings_text = (folder / 'settings.yaml').read_text(encoding='utf-8')
    restore_settings(settings_text)
    tasks = folder / 'tasks.json'
    publications = folder / 'publications.json'
    store.restore_documents(
        tasks.read_text(encoding='utf-8') if tasks.is_file() else None,
        publications.read_text(encoding='utf-8') if publications.is_file() else None)
    from . import export_provenance
    try:
        export_provenance.unmark(case_path, manifest['operation_id'])
    except (OSError, ValueError):
        pass
    _remove(undo_pointer_path(case_path))
    _remove(folder)
    return {'operation_id': manifest['operation_id'],
            'task_id': manifest.get('task_id'),
            'restored_mesh': restored_mesh,
            'restored_geometry': restored_geometry,
            'locked': store.locked_tasks()}


def consume_undo(case_path, *, reason: str) -> str | None:
    """A new mesher run was admitted: the undo no longer describes a state
    the user can go back to. Called only after that run's own rollback
    protection is durable; a refused launch never gets here."""
    pointer = _read_json(undo_pointer_path(case_path))
    if not isinstance(pointer, dict) or not pointer.get('operation_id'):
        return None
    operation_id = str(pointer['operation_id'])
    _remove(undo_pointer_path(case_path))
    _remove(pending_root(case_path) / operation_id)
    logger.info('undo %s consumed by %s', operation_id, reason)
    return operation_id


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #

def recover(case_path, store=None, *, restore_settings=None,
            restore_surfaces=None) -> dict:
    """Finish or roll back what a crash left under ``pending/``.

    *store* is the task-state store of one engine, or a resolver
    ``engine_id -> store`` (case-open recovery passes one, so an unlock made
    on the other engine's branch is rolled back into its own files).

    * no manifest -- the copy never became an operation: deleted.
    * ``prepared`` -- the unlock may have half-applied: the task-state and
      publication files are put back as captured, and the folder deleted.
    * ``committed`` and not the current undo -- a crash between committing
      and naming it: the newest is adopted, older ones deleted.
    * ``restoring`` -- an undo was interrupted: finished when the caller can
      restore settings, otherwise left for the next caller that can.
    """
    case_path = Path(case_path)
    root = pending_root(case_path)
    report = {'rolled_back': [], 'adopted': [], 'removed': [], 'resumed': [],
              'waiting': []}
    store_for = _store_resolver(store)
    if not root.is_dir():
        return report
    pointer = _read_json(undo_pointer_path(case_path))
    current = pointer.get('operation_id') if isinstance(pointer, dict) else None
    committed = []
    for folder in sorted(item for item in root.iterdir() if item.is_dir()):
        manifest = _read_json(folder / 'manifest.json')
        if not isinstance(manifest, dict):
            _remove(folder)
            report['removed'].append(folder.name)
            continue
        state = manifest.get('state')
        target = store_for(manifest.get('engine_id'))
        if state == PREPARED:
            if target is None:
                report['waiting'].append(folder.name)
                continue
            tasks = folder / 'tasks.json'
            publications = folder / 'publications.json'
            target.restore_documents(
                tasks.read_text(encoding='utf-8') if tasks.is_file() else None,
                publications.read_text(encoding='utf-8')
                if publications.is_file() else None)
            _remove(folder)
            report['rolled_back'].append(folder.name)
        elif state == RESTORING:
            if restore_settings is None or target is None:
                report['waiting'].append(folder.name)
                continue
            _finish_undo(case_path, target, folder, manifest, restore_settings,
                         restore_surfaces)
            report['resumed'].append(folder.name)
        elif state == COMMITTED and folder.name != current:
            committed.append((str(manifest.get('committed_at') or ''), folder.name))
    if committed:
        current_manifest = _read_json(root / str(current) / 'manifest.json') if current else None
        current_at = str((current_manifest or {}).get('committed_at') or '')
        for stamp, name in sorted(committed):
            if stamp > current_at:
                _adopt(case_path, name)
                report['adopted'].append(name)
                current_at = stamp
            else:
                _remove(root / name)
                report['removed'].append(name)
    return report


def _store_resolver(store):
    """``engine id -> store or None`` from a store, a resolver or nothing.

    A pending operation is rolled back into the task-state files of the
    engine it was made on, never into whichever engine is configured now.
    """
    if store is None:
        return lambda _engine_id: None
    if callable(store) and not hasattr(store, 'descriptor'):
        def resolve(engine_id):
            try:
                return store(engine_id) if engine_id else None
            except Exception:  # noqa: BLE001 - an unknown engine rolls nothing
                return None
        return resolve
    return lambda engine_id: (
        store if store.descriptor.engine_id == engine_id else None)


def engine_store(case_path, engine_id):
    """The task-state store of *engine_id* in *case_path* (``None`` if unknown)."""
    from foammesh.core.engine.registry import ENGINE_REGISTRY
    from foammesh.core.workflow.task_state_store import EngineTaskStateStore
    try:
        descriptor = ENGINE_REGISTRY.get(str(engine_id)).workflow_descriptor()
    except Exception:  # noqa: BLE001
        return None
    return EngineTaskStateStore(case_path, descriptor)


def recover_at_open(case_path, *, restore_settings=None) -> dict:
    """Case-open recovery (Plan 37 UF5 DP-1062).

    Rolls back every PREPARED unlock into the task-state files of the engine
    it was made on, adopts a committed-but-unnamed undo, and -- when
    *restore_settings* is given -- finishes an interrupted undo. Held under
    the exclusive case lease; a case some other flow holds is left to the
    executor's own gate (``report['busy']``). Whatever is still pending
    afterwards is in ``report['waiting']`` and refuses mesher runs.
    """
    case_path = Path(case_path)
    report = {'rolled_back': [], 'adopted': [], 'removed': [], 'resumed': [],
              'waiting': [], 'busy': False}
    if not pending_root(case_path).is_dir():
        return report
    try:
        with case_lease.hold(case_path, case_lease.EXCLUSIVE,
                             'mesh.workflow.recover'):
            report.update(recover(
                case_path, lambda engine_id: engine_store(case_path, engine_id),
                restore_settings=restore_settings))
    except case_lease.CaseBusyError:
        report['busy'] = True
    return report


def blocking_recovery(case_path) -> list[str]:
    """Pending operations a mesher run must not start over."""
    root = pending_root(case_path)
    if not root.is_dir():
        return []
    waiting = []
    for folder in root.iterdir():
        manifest = _read_json(folder / 'manifest.json')
        if isinstance(manifest, dict) and manifest.get('state') in (PREPARED, RESTORING):
            waiting.append(folder.name)
    return waiting
