"""Mesh stage runs, geometry operations and accepted steps in the one undo
history (Plan 40 UXD7, F7d).

Undo used to cover the configuration only, so undoing past a Castellation run
changed the settings and left the Castellation mesh and its "done" task behind.
A stage run is now a step in the same history as a settings edit. The step
holds two things the settings snapshot cannot:

* the mesh actions for each direction -- undo returns the snapshot the run
  replaced (kept by Plan 37 UF5), redo the one the run made;
* the task-state and publication documents as they stood with each of those
  meshes, so the step list and the mesh agree after the move.

One Undo applies the mesh, the documents and the settings together; if the
mesh cannot be restored (the kept copy is damaged or gone) nothing moves and
the refusal says why.

Two more kinds of step share the history (DP-1381):

* ``geometry`` -- a geometry operation (transform, repair, split, combine, a
  boundary edit, ...). The geometry store writes each result as a new file
  and records it in one manifest, so the step holds the manifest as it stood
  before and after; Undo puts the manifest back and the files it names are
  still there.
* ``task`` -- accepting a step, finishing it with Proceed, skipping it or
  reopening it. The step holds the task states and the finish locks before
  and after. Undoing an acceptance restores them from before it and marks
  every later result that held a state stale; it never deletes a result,
  a mesh or a publication record, and never un-runs a mesher.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from foammesh.core.jobs import stage_snapshots as snapshots
from foammesh.core.project import Event

from .errors import UndoNotAllowedError

logger = logging.getLogger(__name__)

STAGE_NAMES = {'blockMesh': 'Base grid', 'castellation': 'Castellation',
               'snappyHexMesh': 'Snappy', 'snap': 'Snap',
               'layers': 'Layers'}   # DP-1311: the step's name


#: The geometry operations that are steps of the history, and the label each
#: gets. Import and delete are not here: they follow the project tree, which
#: the settings snapshot already holds.
GEOMETRY_STEPS = {
    'geometry.split': 'Split geometry',
    'geometry.combine': 'Combine geometry',
    'geometry.transform': 'Transform geometry',
    'geometry.repair': 'Repair geometry',
    'geometry.repair.apply': 'Repair geometry',
    'geometry.repair.rollback': 'Restore geometry revision',
    'geometry.wrap.apply': 'Wrap geometry',
    'geometry.rename': 'Rename geometry',
    'geometry.patches.rename': 'Rename boundary',
    'geometry.patches.merge': 'Merge boundaries',
    'geometry.patches.split': 'Split boundary',
    'geometry.patches.split_by_angle': 'Split boundaries by angle',
    'geometry.split_interfaces': 'Split interfaces',
    'geometry.surface_intent.auto_pair': 'Pair touching walls',
    'geometry.cad.convert_to_surface': 'Convert CAD to surface',
}


class StepRefused(Exception):
    """A geometry or task step cannot be taken; nothing was moved."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def stage_title(stage: str) -> str:
    return STAGE_NAMES.get(stage, stage)


def label_for(stage: str, cells: int | None) -> str:
    """``Castellation run · 4,374 cells`` -- what the History panel shows."""
    if cells is None:
        return f'{stage_title(stage)} run'
    return f'{stage_title(stage)} run · {cells:,} cells'


def task_store(session, engine_id: str | None):
    from foammesh.core.engine.registry import ENGINE_REGISTRY
    from foammesh.core.workflow.task_state_store import EngineTaskStateStore
    return EngineTaskStateStore(
        session.case_path, ENGINE_REGISTRY.get(engine_id or 'snappy').workflow_descriptor())


def _documents_path(case_path, revision: str, stage: str) -> Path:
    return snapshots.stages_root(case_path) / revision / f'{stage}.documents.json'


def write_documents(case_path, revision: str, stage: str, store) -> None:
    """Keep the task-state documents beside the snapshot they describe."""
    tasks, publications = store.document_texts()
    path = _documents_path(case_path, revision, stage)
    try:
        path.write_text(json.dumps({'tasks': tasks, 'publications': publications}),
                        encoding='utf-8')
    except OSError:
        logger.warning('could not keep the task state beside %s/%s',
                       revision, stage, exc_info=True)


def read_documents(case_path, revision: str, stage: str) -> dict | None:
    try:
        document = json.loads(
            _documents_path(case_path, revision, stage).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def record_stage_run(session, store, stage: str, captured: dict,
                     previous: dict | None, cells: int | None) -> bool:
    """Add the run that just published *stage* to the undo history.

    ``previous`` is ``active.json`` as it stood before the capture: the
    snapshot the live mesh was a copy of. Without one there is nothing to go
    back to, and no step is recorded.
    """
    revision = captured.get('revision')
    if not revision or not previous:
        return False
    before = (str(previous.get('revision') or ''), str(previous.get('stage') or ''))
    if not all(before) or before == (str(revision), stage):
        return False
    if snapshots.read_manifest(session.case_path, before[1], before[0]) is None:
        return False
    write_documents(session.case_path, str(revision), stage, store)
    companion = {
        'kind': 'stage', 'stage': stage, 'revision': str(revision),
        'engine_id': store.descriptor.engine_id,
        'undo': {'revision': before[0], 'stage': before[1]},
        'redo': {'revision': str(revision), 'stage': stage},
    }
    session.state.record_stage(label_for(stage, cells), companion)
    sync_pins(session)
    return True


def sync_pins(session) -> None:
    """Keep every snapshot the history can still reach."""
    pairs = set()
    for companion in session.state.history.companions():
        if companion.get('kind', 'stage') != 'stage':
            continue
        for key in ('undo', 'redo'):
            action = companion.get(key)
            if action:
                pairs.add((action['revision'], action['stage']))
    snapshots.set_pins(session.case_path, pairs)


def _restore_documents(session, store, action: dict) -> None:
    documents = read_documents(session.case_path, action['revision'], action['stage'])
    if documents is not None:
        store.restore_documents(documents.get('tasks'), documents.get('publications'))


def _apply_stage(session, store, action: dict) -> None:
    snapshots.restore(session.case_path, action['stage'], action['revision'])
    _restore_documents(session, store, action)


def _apply(session, companion: dict, direction: str) -> None:
    """Move what the settings snapshot cannot hold, for one step."""
    kind = companion.get('kind', 'stage')
    if kind == 'geometry':
        _apply_geometry(session, companion[direction])
    elif kind == 'task':
        _apply_task(session, companion, direction)
    else:
        _apply_stage(session, task_store(session, companion.get('engine_id')),
                     companion[direction])


async def step(session, direction: str, count: int,
               moved: set | None = None) -> int:
    """Take *count* steps back (``'undo'``) or forward (``'redo'``).

    Returns how many were taken. A step that moves more than the settings
    restores it first, off the owner loop; the history only moves once that
    has worked. ``moved``, when given, receives the kinds of step taken.
    """
    history = session.state.history
    taken = 0
    kinds: set = moved if moved is not None else set()
    for _ in range(count):
        companion = (history.undo_companion() if direction == 'undo'
                     else history.redo_companion())
        label = history.undo_label() if direction == 'undo' else history.redo_label()
        if direction == 'undo' and not history.can_undo():
            break
        if direction == 'redo' and not history.can_redo():
            break
        if companion:
            try:
                await asyncio.to_thread(_apply, session, companion, direction)
            except (snapshots.SnapshotError, StepRefused) as error:
                raise UndoNotAllowedError(
                    f'{label!r} cannot be '
                    f'{"undone" if direction == "undo" else "redone"}: {error}',
                    details={'reason': error.code, 'label': label}) from error
            kinds.add(companion.get('kind', 'stage'))
        if direction == 'undo':
            session.state.undo(stage_applied=bool(companion))
        else:
            session.state.redo(stage_applied=bool(companion))
        taken += 1
    operation = f'history.{direction}'
    if 'stage' in kinds:
        sync_pins(session)
        session.state.bus.publish(Event.ARTIFACT_MESH_CHANGED, operation=operation)
    if 'geometry' in kinds:
        session.state.bus.publish(Event.ARTIFACT_GEOMETRY_CHANGED,
                                  operation=operation, geometry_id=None)
    if kinds & {'stage', 'task'}:
        session.state.bus.publish(Event.ARTIFACT_QUALITY_CHANGED,
                                  operation=operation,
                                  job_id=None, artifacts=[], quality=None)
    return taken


# ---- geometry steps ------------------------------------------------------- #

def manifest_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'geometry' / 'manifest.json'


def manifest_text(case_path) -> str | None:
    """The geometry manifest as it is on disk, or ``None`` when there is none."""
    try:
        return manifest_path(case_path).read_text(encoding='utf-8')
    except OSError:
        return None


def manifest_artifacts(text: str | None) -> set[str]:
    """Every file a manifest names: current artifacts and kept revisions."""
    if not text:
        return set()
    try:
        document = json.loads(text)
    except ValueError:
        return set()
    paths: set[str] = set()
    for key in ('geometries', 'removed'):
        for entry in document.get(key) or ():
            if not isinstance(entry, dict):
                continue
            for record in (entry, *(entry.get('revisions') or ())):
                if not isinstance(record, dict):
                    continue
                for name in ('artifact', 'cad_artifact'):
                    if record.get(name):
                        paths.add(str(record[name]))
    return paths


def record_geometry_step(session, operation: str, before: str | None,
                         after: str | None, mark: int) -> bool:
    """Add the geometry operation that just ran to the undo history.

    Nothing is recorded when the manifest did not change: such an operation
    moved no geometry (a refused edit, a preview) and its settings commits, if
    any, remain steps of their own.
    """
    if before == after or operation not in GEOMETRY_STEPS:
        return False
    session.state.record_step(
        GEOMETRY_STEPS[operation],
        {'kind': 'geometry', 'operation': operation,
         'undo': {'manifest': before}, 'redo': {'manifest': after}},
        mark)
    return True


def protected_geometry_paths(session) -> set[str]:
    """Files a geometry revision clean-up must keep: the history can reach them."""
    paths: set[str] = set()
    for companion in session.state.history.companions():
        if companion.get('kind') != 'geometry':
            continue
        for key in ('undo', 'redo'):
            paths |= manifest_artifacts((companion.get(key) or {}).get('manifest'))
    return paths


def _apply_geometry(session, action: dict) -> None:
    text = action.get('manifest')
    missing = sorted(path for path in manifest_artifacts(text)
                     if not Path(path).is_file())
    if missing:
        raise StepRefused(
            f'a geometry file this step needs is gone ({Path(missing[0]).name})',
            'geometry_file_missing')
    path = manifest_path(session.case_path)
    if text is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


# ---- task steps ----------------------------------------------------------- #

def task_documents(store) -> dict:
    """The two documents a step's state lives in, as they are on disk.

    The publication records are not among them: accepting or finishing a step
    does not write them, and they describe meshes that undo must leave alone.
    """
    tasks, _publications = store.document_texts()
    return {'tasks': tasks, 'completion': store.completion_text()}


def record_task_step(session, store, task_id: str, label: str,
                     before: dict, after: dict, mark: int) -> bool:
    """Add an acceptance, finish, skip or reopen to the undo history."""
    if before == after:
        return False
    session.state.record_step(
        label,
        {'kind': 'task', 'engine_id': store.descriptor.engine_id,
         'task_id': task_id, 'undo': before, 'redo': after},
        mark)
    return True


def _apply_task(session, companion: dict, direction: str) -> None:
    store = task_store(session, companion.get('engine_id'))
    action = companion[direction]
    # Undoing an acceptance marks what was reached through it stale; redoing
    # one puts the documents back as they were.
    store.restore_step(action.get('tasks'), action.get('completion'),
                       reopened=companion.get('task_id')
                       if direction == 'undo' else None)
