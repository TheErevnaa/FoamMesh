"""The small AF1 facade: registry, concurrency checks, and AF1V field patch."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from foammesh.core.project import Event, Source

from .application_session import ApplicationSession
from .commands import Actor, ActorKind, Command, CommandSource
from .domain_operations import DomainOperations, TaskLockedError
from foammesh.support.simple_db.simple_db import ConcurrentEditError

from .errors import (AuthorizationRequiredError, CaseNotFoundError, OperationNotFoundError,
                     PlanStaleError, RevisionConflictError, UndoNotAllowedError,
                     ValidationFailedError)
from .field_adapters import build_entity_adapters, coerce_for_apply, read_value, values_equal
from .field_metadata import (MESH_FINGERPRINT, MESH_STAGE_ENGINE_STAGES, earliest_stage,
                             expand_fingerprints, stales)
from .fields import REGISTRY as FIELD_REGISTRY
from .operations import OperationKind, build_operation_registry
from .presentation import PresentationOperations, PresentationState
from .results import OperationResult
from .session import CaseSession
from .plans import PlanState, PlanStore
from .vertical_slice import VerticalSliceOperations

logger = logging.getLogger(__name__)


#: Collections whose rows carry a stable prepared-geometry scope token. Every
#: engine registers its scoped collections here so the scope check stays in one
#: place rather than being restated per engine.
_SCOPED_COLLECTIONS = frozenset({
    'geometry.interface_pairs',
    'gmsh.size_fields.controls',
    'gmsh.curve_controls.controls',
    'gmsh.volume_controls.controls',
    'gmsh.periodic_pairs.controls',
})

#: The subset of those scoped to volume regions rather than surface groups.
_REGION_SCOPED_COLLECTIONS = frozenset({'gmsh.volume_controls.controls'})


def _scope_is_unused(collection_id: str, normalized: dict) -> bool:
    """True for a row whose kind takes no geometric scope (DP-665).

    Only an analytic size field -- Box, Ball, Cylinder, Frustum, MathEval --
    is placed by its own numbers rather than by a surface group, the same
    rule `SizeFieldControl.needs_scope` applies at plan time.
    """
    if collection_id != 'gmsh.size_fields.controls':
        return False
    from foammesh.core.gmsh.size_fields import ANALYTIC_KINDS
    return str(normalized.get('fieldType') or '') in ANALYTIC_KINDS


# The full AF2 semantic-ID -> storage-path map, generated from the schema.
FIELD_STORAGE = FIELD_REGISTRY.storage_map()


#: Plan 37 UF5. Collections a task's page edits but whose descriptor task
#: binds no field for them (a table page binds the rows, not the fields).
_SUPPLEMENTARY_COLLECTION_TASKS = {
    'gmsh': {
        'gmsh.size_fields.controls': 'gmsh.size_fields',
        'gmsh.surface_sizes.controls': 'gmsh.size_fields',
        'gmsh.curve_controls.controls': 'gmsh.curve_controls',
        'gmsh.volume_controls.controls': 'gmsh.volume_controls',
        'gmsh.periodic_pairs.controls': 'gmsh.periodic',
    },
    'snappy': {},
}

#: Plan 37 DP-1102. Storage paths no task's fields bind, yet a stage's
#: result was built from them. The farfield is one record both engines read
#: (edited from the Geometry page), so after publish an edit to it went
#: straight past the lock.
_SUPPLEMENTARY_PATH_TASKS = {
    'gmsh': {'gmsh/farfield': 'gmsh.describe_geometry'},
    'snappy': {'gmsh/farfield': 'snappy.domain_regions'},
}

#: Plan 37 UF5 DP-1063. The geometry and region lists are no one page's
#: fields -- no task binds them -- yet every stage's result was made from
#: them, so an edit to either went straight past the lock. Each list maps to
#: the tasks that consume it, first choice first: the first of them that is
#: locked names the refusal (a Gmsh volume page left at its defaults is not
#: published, and its regions were still meshed under the geometry step). A
#: few leaves map to the one stage that reads them. An empty tuple is a leaf
#: the mesh does not consume: a geometry's name is its patch name, which a
#: rename changes on the mesh too, without a new mesh.
_ITEM_LOCKS = {
    'snappy': {
        'roots': {'geometry': ('snappy.domain_regions',),
                  'region': ('snappy.domain_regions',)},
        'leaves': {'geometry': {'name': (),
                                'castellationGroup': ('snappy.castellation',),
                                'layerGroup': ('snappy.layers',),
                                'slaveLayerGroup': ('snappy.layers',)}},
    },
    'gmsh': {
        'roots': {'geometry': ('gmsh.describe_geometry',),
                  'region': ('gmsh.volume_controls', 'gmsh.describe_geometry')},
        'leaves': {'geometry': {'name': ()}},
    },
}


def _item_lock_task(engine_id: str, changed: str, locked):
    """The locked task an item-list path is refused under; ``None`` when it
    is exempt or nothing it feeds is locked, ``_UNMATCHED`` when the path is
    not in the geometry or region lists."""
    rules = _ITEM_LOCKS.get(engine_id)
    parts = str(changed).strip('/').split('/')
    if not rules or parts[0] not in rules['roots']:
        return _UNMATCHED
    leaves = rules['leaves'].get(parts[0], {})
    candidates = (leaves[parts[2]] if len(parts) > 2 and parts[2] in leaves
                  else rules['roots'][parts[0]])
    return next((task for task in candidates if task in locked), None)


_UNMATCHED = object()


def _changed_geometry_files(session, data) -> list:
    """Geometry surfaces (``_files``) a working copy added or replaced.

    A checkout shares the VTK objects, so a surface the copy did not touch is
    the same object in both; anything else is a new or re-cut surface.
    """
    mine = (getattr(data, '_files', None) or {}).get('geometry') or {}
    theirs = (getattr(session.state.db, '_files', None) or {}).get('geometry') or {}
    return sorted(f'_files/geometry/{key}' for key, value in mine.items()
                  if theirs.get(key) is not value)


_LOCK_PATHS: dict = {}


def _lock_paths(descriptor) -> tuple:
    """``(storage path, task id)`` for every input a task's result consumed."""
    key = (descriptor.engine_id, descriptor.digest)
    cached = _LOCK_PATHS.get(key)
    if cached is not None:
        return cached
    collections = FIELD_REGISTRY.collections
    pairs = []
    for task in descriptor.ordered_tasks():
        for binding in task.fields:
            if binding.field_id in FIELD_STORAGE:
                pairs.append((FIELD_STORAGE[binding.field_id], task.task_id))
            elif binding.field_id in collections:
                pairs.append((collections[binding.field_id].storage_path, task.task_id))
    for collection_id, task_id in _SUPPLEMENTARY_COLLECTION_TASKS.get(
            descriptor.engine_id, {}).items():
        if collection_id in collections:
            pairs.append((collections[collection_id].storage_path, task_id))
    pairs.extend(_SUPPLEMENTARY_PATH_TASKS.get(descriptor.engine_id, {}).items())
    # Plan 37 UF15. A field this engine reads at an earlier stage than the
    # task that shows it -- snappy's mesh-quality limits, set on Quality but
    # read by Snap and Layers -- is also an input of that stage's result.
    stage_tasks = {task.engine_stage: task.task_id
                   for task in descriptor.ordered_tasks() if task.engine_stage}
    for field_id, storage in FIELD_STORAGE.items():
        if field_id not in FIELD_REGISTRY:
            continue
        field = FIELD_REGISTRY.get(field_id)
        if not any(name == descriptor.engine_id
                   for name, _ in field.invalidates_by_engine):
            continue
        stage = earliest_stage(field.invalidates_for(descriptor.engine_id))
        task_id = stage_tasks.get(MESH_STAGE_ENGINE_STAGES.get(stage))
        if task_id is not None:
            pairs.append((storage, task_id))
    cached = tuple((path.strip('/'), task_id) for path, task_id in pairs)
    _LOCK_PATHS[key] = cached
    return cached


def _changed_paths(data) -> list:
    """The full storage paths a working copy changed, relative to the db root."""
    from foammesh.support.simple_db.simple_db import _diffLeaves
    base = str(getattr(data, '_base', '') or '').strip('/')
    leaves = _diffLeaves(data._schema, data._baseline, data._content)
    paths = []
    for leaf in leaves:
        leaf = str(leaf).strip('/')
        paths.append(f'{base}/{leaf}' if base and leaf else (base or leaf))
    return paths


def refuse_locked_edit(session, data) -> None:
    """Plan 37 UF5 DP-1040. Refuse an edit to a locked task's mesh inputs.

    The lock is the facade's, not the widgets': the GUI's page commits, the
    CLI's patches and an agent's collection edits all arrive here, and a task
    whose published result the edit would silently contradict refuses it with
    a typed error that names the task and the way out (unlock).
    """
    found = _locked_tasks_of(session)
    if found is None:
        return
    engine_id, descriptor, locked = found
    hits: dict = {}
    for changed in _changed_paths(data):
        item_task = _item_lock_task(engine_id, changed, locked)
        if item_task is not _UNMATCHED:
            if item_task is not None:
                hits.setdefault(item_task, []).append(changed)
            continue
        for path, task_id in _lock_paths(descriptor):
            if task_id not in locked:
                continue
            if (changed == path or changed.startswith(path + '/')
                    or path.startswith(changed + '/')):
                hits.setdefault(task_id, []).append(changed)
    surfaces = _changed_geometry_files(session, data)
    geometry_task = _item_lock_task(engine_id, 'geometry', locked)
    if surfaces and geometry_task not in (None, _UNMATCHED):
        hits.setdefault(geometry_task, []).extend(surfaces)
    _raise_locked(engine_id, descriptor, hits)


def refuse_locked_items(session, root: str, *, operation: str = '') -> None:
    """Plan 37 UF5 DP-1063. Refuse an operation that rewrites the geometry
    (``root='geometry'``) or the regions (``'region'``) of a locked case.

    For the operations that change the geometry artifacts on disk -- import,
    repair, wrap, split, patch cuts -- before they write anything, since
    there is no working copy to diff once the artifact store has changed.
    """
    found = _locked_tasks_of(session)
    if found is None:
        return
    engine_id, descriptor, locked = found
    task_id = _item_lock_task(engine_id, root, locked)
    if task_id in (None, _UNMATCHED):
        return
    _raise_locked(engine_id, descriptor,
                  {task_id: [f'{root} ({operation})' if operation else root]})


def _locked_tasks_of(session):
    """``(engine id, descriptor, locked task ids)``, or ``None`` when
    nothing is locked (or the case has no engine yet).

    This runs on the owner loop for every configuration edit, so the common
    case -- nothing ever published -- is answered from one directory listing
    before the engine registry and its workflow descriptor are touched (the
    AF1 owner-loop budget measured a 65-98 ms first edit when it was not).
    """
    workflow = Path(session.case_path) / 'foammesh' / 'workflow'
    if not any(workflow.glob('*-publications.json')):
        return None
    from foammesh.core.engine.registry import ENGINE_REGISTRY, configured_engine_id
    from foammesh.core.workflow.task_state_store import EngineTaskStateStore
    try:
        engine_id = str(configured_engine_id(session.state.db)).strip().lower()
        descriptor = ENGINE_REGISTRY.get(engine_id).workflow_descriptor()
    except Exception:
        return None
    store = EngineTaskStateStore(session.case_path, descriptor)
    if not store.publications_path.is_file():
        return None
    locked = set(store.locked_tasks())
    if not locked:
        return None
    return engine_id, descriptor, locked


def _raise_locked(engine_id, descriptor, hits: dict) -> None:
    if not hits:
        return
    tasks = [task.task_id for task in descriptor.ordered_tasks() if task.task_id in hits]
    titles = [descriptor.task(task_id).title for task_id in tasks]
    raise TaskLockedError(
        f'{", ".join(titles)} {"is" if len(titles) == 1 else "are"} locked: '
        'the mesh on disk was made from these settings. Unlock the step '
        '(discarding the results after it) to change them.',
        details={'tasks': tasks, 'titles': titles, 'engine_id': engine_id,
                 'paths': sorted({item for values in hits.values() for item in values}),
                 'unlock_operation': 'mesh.workflow.unlock'})

# The ten AF1V fields remain a named, frozen subset of the full registry.
AF1V_FIELDS = {
    field_id: FIELD_STORAGE[field_id] for field_id in (
        'meshing.base_grid.cells.x', 'meshing.base_grid.cells.y',
        'meshing.base_grid.cells.z', 'meshing.castellation.max_global_cells',
        'meshing.castellation.max_local_cells', 'meshing.castellation.cells_between_levels',
        'meshing.snap.tolerance', 'meshing.snap.smooth_patch_iterations',
        'meshing.layers.growth_cells', 'meshing.layers.feature_angle',
    )
}


#: What a commit that names no fields has to be assumed to have changed.
#: Staling from the first stage onwards is the whole mesh plus the reports,
#: which is what this call site said before the stages existed.
_WHOLE_WORKING_COPY_STALES = stales('mesh.base_grid', 'quality')


def _invalidation_for(field_ids, engine_id=None) -> tuple[str, ...]:
    """Union of the artifact fingerprints the changed fields stale (§6.7).

    Plan 37 UF15. Resolved on ``engine_id``'s route: a field may stale more
    on one engine than another (``FieldDescriptor.invalidates_for``) -- the
    mesh-quality limits stale the snapped mesh onward on snappy, which meshes
    against them, and only the quality report on Gmsh, which does not.

    Plan 32 W4 (DP-242). The union is resolved through
    ``expand_fingerprints``, so a field naming a snappyHexMesh stage stales
    that stage, every stage after it, and the delivered mesh -- and nothing
    before it. The resolution is idempotent and the registry rows already
    carry their closure, so this is a guarantee rather than a second
    derivation: a caller that hands in a bare stage name gets the same answer
    the registry would have given.
    """
    invalidated: set[str] = set()
    for field_id in field_ids:
        if field_id in FIELD_REGISTRY:
            invalidated.update(
                FIELD_REGISTRY.get(field_id).invalidates_for(engine_id))
    return expand_fingerprints(invalidated)


def _session_engine(session) -> str | None:
    """The engine the case is configured for, or ``None`` before one is."""
    from foammesh.core.engine.registry import configured_engine_id
    try:
        engine_id = str(configured_engine_id(session.state.db) or '').strip().lower()
    except Exception:                                        # noqa: BLE001
        return None
    return engine_id or None


def _engine_stage_tasks(engine_id, field_ids) -> tuple[str, ...]:
    """The engine tasks an engine-specific invalidation reaches back to.

    Plan 37 UF15. The task graph learns of an edit from the page it was made
    on, which configures that page's own task and stales what follows it.
    A field that stales an *earlier* stage on this engine than the task that
    owns it -- a mesh-quality limit, edited on Quality after Layers, that
    snappy reads at Snap -- has to stale that stage's task too, or the
    outline shows Snap and Layers done over a mesh made with the old limits.
    Only fields with an ``invalidates_by_engine`` row for this engine are
    asked: every other field's stage is the stage of the task it is bound to.
    """
    if not engine_id:
        return ()
    stages: set[str] = set()
    for field_id in field_ids:
        if field_id not in FIELD_REGISTRY:
            continue
        descriptor = FIELD_REGISTRY.get(field_id)
        if not any(name == engine_id for name, _ in descriptor.invalidates_by_engine):
            continue
        stage = earliest_stage(descriptor.invalidates_for(engine_id))
        if stage is not None:
            stages.add(MESH_STAGE_ENGINE_STAGES[stage])
    if not stages:
        return ()
    from foammesh.core.engine.registry import ENGINE_REGISTRY
    try:
        workflow = ENGINE_REGISTRY.get(engine_id).workflow_descriptor()
    except Exception:                                        # noqa: BLE001
        return ()
    return tuple(task.task_id for task in workflow.ordered_tasks()
                 if task.engine_stage in stages)


def _stale_engine_stages(session, engine_id, field_ids) -> tuple[str, ...]:
    """Stale the stage tasks ``_engine_stage_tasks`` names; returns what moved.

    A stage that never ran has no result to stale, and the graph's own
    ``invalidate`` leaves it alone, so a case not yet meshed is untouched.
    """
    task_ids = _engine_stage_tasks(engine_id, field_ids)
    if not task_ids:
        return ()
    from foammesh.core.engine.registry import ENGINE_REGISTRY
    from foammesh.core.workflow.task_state_store import EngineTaskStateStore
    store = EngineTaskStateStore(
        session.case_path, ENGINE_REGISTRY.get(engine_id).workflow_descriptor())
    if not store.path.is_file():
        return ()
    graph = store.load()
    changed: list[str] = []
    for task_id in task_ids:
        for stale in graph.invalidate(task_id, include_self=True):
            if stale not in changed:
                changed.append(stale)
    if changed:
        store.save(graph)
    return tuple(changed)


def _publish_verdict_staleness(session, invalidated, changed) -> None:
    """Say that the configuration moved on from the mesh currently on screen.

    Plan 26 WP3.1. ``Event.ARTIFACT_STALE`` means the opposite thing -- "the
    mesh fingerprint changed", invalidating quality and exports, published by
    ``Project.markArtifactChanged``. What the verdict strip needs is "the
    settings changed after the mesh was made", so it emits its own event rather
    than borrowing one whose meaning it would have to contradict.

    Only fired when the change actually invalidates the mesh: an edit to a
    field that cannot alter the mesh must not grey out a verdict that is still
    perfectly current.

    Plan 32 W4 (DP-242). The test is still membership of the delivered mesh,
    because the verdict on screen is a verdict about the delivered mesh: a
    stale stage means a stale verdict whichever stage it is. Resolving the
    closure first is what keeps that true -- every stage closure contains
    ``mesh``, so a caller handing in a bare stage name is answered the same
    way the registry would have answered it.
    """
    resolved = set(expand_fingerprints(invalidated))
    if MESH_FINGERPRINT not in resolved:
        return
    try:
        session.state.bus.publish(
            Event.MESH_VERDICT_STALE, fields=tuple(changed or ()),
            invalidated=expand_fingerprints(invalidated))
    except Exception:                                        # noqa: BLE001
        # A staleness notice that can break the edit it reports on would be a
        # worse defect than the stale verdict it exists to prevent.
        pass


def _source(source: CommandSource) -> Source:
    return {
        CommandSource.GUI: Source.GUI,
        CommandSource.REST: Source.API,
        CommandSource.CLI: Source.CLI,
        CommandSource.AUTOMATION: Source.AGENT,
        CommandSource.SYSTEM: Source.SYSTEM,
    }[source]


class FoamMeshFacade:
    #: Operations that must not wait behind the serialized write queue when
    #: the registry has nothing to say about them.
    #:
    #: Plan 30 WP-08 (F-09) retired this as the *rule*: which operations skip
    #: the queue is now operation metadata (``OperationDescriptor.kind``), so
    #: a read added to the registry is a read to the scheduler without anyone
    #: remembering to name it here. The set survives as the answer for a
    #: handler registered with no descriptor at all -- test doubles, mostly --
    #: and every member of it is still asserted read-only against the registry.
    UNSERIALIZED_OPERATIONS = frozenset({
        'job.cancel',
        'job.cancel_active',
        'geometry.prepare.cancel',
        'mesh.engine.probe',
        'openfoam.runtime.diagnostics',
        # Plan 31 CP-07 item 4. Reads a library listing out of the runtime and
        # writes nothing; serializing it would put the Execution page behind
        # whatever mesh is running.
        'mesh.execution.decomposition_methods',
    })

    def operation_kind(self, operation: str) -> OperationKind:
        """How the scheduler must treat one operation.

        The registry is the authority. An operation with no descriptor is a
        mutation unless it is one of the named safety paths above, because
        "unknown" must not mean "may run beside a mesh being written".
        """
        descriptor = self.operations.get(operation)
        if descriptor is not None:
            return descriptor.kind
        return (OperationKind.QUERY if operation in self.UNSERIALIZED_OPERATIONS
                else OperationKind.MUTATION)

    def bypasses_queue(self, operation: str) -> bool:
        return self.operation_kind(operation) is OperationKind.QUERY

    def __init__(self, *, application_session: ApplicationSession | None = None,
                 capabilities=None):
        self.application = application_session or ApplicationSession()
        self._cases: dict[str, CaseSession] = {}
        self._operations: dict[str, Callable] = {}
        self.plans = PlanStore()
        self.slice_operations = VerticalSliceOperations()
        self.domain = DomainOperations(capabilities=capabilities)
        self.presentation_ops = PresentationOperations()
        self.fields = FIELD_REGISTRY
        self.entities = build_entity_adapters(FIELD_REGISTRY.collections)
        self.operations = build_operation_registry(
            FIELD_REGISTRY.json_schema(), tuple(self.entities),
            self.presentation_ops.operations())
        self.register('configuration.patch', self._patch)
        self.register('configuration.dry_run', self._dry_run)
        self.register('configuration.commit_working_copy', self._commit_working_copy)
        self.register('history.undo', self._undo)
        self.register('history.redo', self._redo)
        self.register('history.revert_plan', self._revert_plan)
        self.register('history.revert_change_set', self._revert_change_set)
        self.register('artifact.report.generate', self.slice_operations.generate_report)
        self.register('job.test.start', self.slice_operations.start_job)
        self.register('job.cancel', self.slice_operations.cancel_job)
        self.register('job.cancel_active', self._cancel_active_jobs)
        for collection_id in self.entities:
            self.register(f'{collection_id}.create', self._make_collection_create(collection_id))
            self.register(f'{collection_id}.patch', self._make_collection_patch(collection_id))
            self.register(f'{collection_id}.remove', self._make_collection_remove(collection_id))
        self.register('geometry.primitive.create', self._geometry_primitive_create)
        self.register('geometry.edit', self._make_collection_patch('geometry.items'))
        self.register('geometry.delete', self._make_collection_remove('geometry.items'))
        self.domain.register_all(self.register)

    def attach(self, session: CaseSession) -> CaseSession:
        self._cases[session.case_id] = session
        # The lock check behind every configuration edit reads the engine
        # registry; importing it here, at attach, keeps that one-time cost
        # off the owner loop's first edit after a publish.
        import foammesh.core.engine.registry  # noqa: F401
        self._publish_prepared_catalogue(session)
        self._recover_interrupted_unlock(session)
        self._recover_interrupted_redistribute(session)
        return session

    def _recover_interrupted_redistribute(self, session: CaseSession) -> dict | None:
        """Plan 37 UF17. Settle a core-count change a crash interrupted.

        Before publication the live processor cases were never touched and
        the stage is discarded; mid-publication the swap is finished or
        undone by rename; after it the retired ranks are removed. A change
        whose files landed but whose core-count setting did not gets the
        setting now, so the next parallel stage reuses these ranks.
        """
        from foammesh.core.jobs import redistribute_transaction as transaction
        if session.read_only or not transaction.transactions_root(
                session.case_path).is_dir():
            return None
        try:
            report = transaction.recover_at_open(session.case_path)
        except OSError:
            logger.warning('core-count change recovery failed', exc_info=True)
            return None
        for item in report['settings']:
            try:
                transaction.apply_setting(session, item['target_ranks'],
                                          reason='redistribute recovery')
            except Exception:  # noqa: BLE001 - the executor's gate retries
                logger.warning('core-count setting of %s could not be published',
                               item['id'], exc_info=True)
                continue
            transaction.finish(session.case_path, item['id'])
        return report

    def _recover_interrupted_unlock(self, session: CaseSession) -> dict | None:
        """Plan 37 UF5 DP-1062. Settle an unlock or undo a crash interrupted.

        Every way a case is opened -- the desktop's in-place session, the
        facade's own ``open_case``, the CLI and the API -- attaches it here,
        so this is the one place a half-done unlock is rolled back (it is
        two small files) and a half-done undo finished, before anything can
        ask for a run. The executor's gate still refuses a run while an
        undo is waiting, so a case another flow holds right now is safe to
        leave to it.
        """
        from foammesh.core.jobs import stage_snapshots, unlock_transaction
        if session.read_only:
            return None
        try:
            # Plan 37 UF5: a stage snapshot copy or a replay restore that a
            # crash interrupted is removed or rolled back before any run.
            stage_snapshots.recover(session.case_path)
        except OSError:
            logger.warning('stage snapshot recovery failed', exc_info=True)
        if not unlock_transaction.pending_root(session.case_path).is_dir():
            return None
        report = unlock_transaction.recover_at_open(session.case_path)
        if not report['waiting'] or report['busy']:
            return report
        # An undo was interrupted: its settings go back through the project
        # state, so it is finished as the ordinary undo command -- queued on
        # the owner loop when there is one (the desktop), inline when not.
        command = Command('mesh.workflow.undo_unlock', session.case_id, {},
                          Actor('foammesh-recovery', ActorKind.SYSTEM),
                          CommandSource.SYSTEM)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            try:
                result = asyncio.run(self.execute(command))
                report['resumed'].extend([result.payload.get('operation_id')])
                report['waiting'] = unlock_transaction.blocking_recovery(
                    session.case_path)
            except Exception:  # noqa: BLE001 - the executor's gate still refuses runs
                logger.exception('could not finish the interrupted undo in %s',
                                 session.case_path)
            return report

        async def finish():
            try:
                await self.execute(command)
            except Exception:  # noqa: BLE001
                logger.exception('could not finish the interrupted undo in %s',
                                 session.case_path)
        loop.create_task(finish(), name='foammesh-unlock-recovery')
        return report

    @staticmethod
    def _publish_prepared_catalogue(session: CaseSession) -> None:
        """Tell the scope catalogue which case is in front now.

        Plan 33 CURVE-01/FIELD-02. The catalogue is application-lived and the
        case is not, so attaching a case is the moment the prepared scopes of
        the last one stop being true. Advisory: a case that cannot be read for
        scopes is still a case that opened.
        """
        from foammesh.core.selection.service import notify_prepared_case
        try:
            notify_prepared_case(session.case_path, session.state.db)
        except (AttributeError, FileNotFoundError, OSError, ValueError):
            pass

    def detach(self, case_id: str, *, save: bool = True) -> None:
        session = self._cases.pop(case_id, None)
        if session is not None:
            session.close(save=save)

    def case(self, case_id: str) -> CaseSession:
        try:
            return self._cases[case_id]
        except KeyError as error:
            raise CaseNotFoundError('case session was not found', details={'case_id': case_id}) from error

    def snapshot(self, case_id: str) -> dict:
        return self.case(case_id).snapshot()

    # -- case lifecycle (session ownership, not case-scoped commands) ------- #

    def create_case(self, path, *, attach_presentation: bool = False) -> CaseSession:
        """Create a new case and attach a persistent session (new_case)."""
        session = CaseSession.open(path, create=True)
        if attach_presentation:
            session.attach_presentation(PresentationState())
        return self.attach(session)

    def open_case(self, path, *, read_only_on_lock: bool = True,
                  attach_presentation: bool = False) -> CaseSession:
        """Open an existing case and attach a persistent session (open_project)."""
        session = CaseSession.open(path, read_only_on_lock=read_only_on_lock)
        if not session.read_only:
            # Plan 31 CP-05 item 7. A run that was publishing when the
            # application stopped is settled here, once, while the case is
            # being opened -- before anything reads the Runs list and reports
            # a publication in progress that no process is doing.
            from foammesh.core.gmsh.manifest import (
                reconcile_interrupted_publications,
            )
            try:
                reconcile_interrupted_publications(session.case_path)
            except OSError:                                  # noqa: BLE001
                pass
        if attach_presentation:
            session.attach_presentation(PresentationState())
        return self.attach(session)

    def close_case(self, case_id: str, *, save: bool = True) -> None:
        """Close and detach a case (close_project)."""
        self.detach(case_id, save=save)

    def validate_plan(self, case_id: str, commands: list[dict]) -> dict:
        required = set()
        for item in commands:
            descriptor = self.operations.get(item.get('operation', ''))
            if descriptor is not None:
                required.update(descriptor.capabilities)
        return self.plans.validate(
            self.case(case_id), commands, field_paths=FIELD_STORAGE,
            operation_registry=self.operations,
            capability_digest=self._capability_digest(required)).public()

    def _capability_digest(self, names) -> str:
        import hashlib
        import json
        snapshot = self.describe_capabilities(sorted(names))['capabilities'] if names else []
        return hashlib.sha256(json.dumps(
            snapshot, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()

    def describe_fields(self) -> dict:
        return {'fields': [descriptor.to_dict()
                           for descriptor in self.fields.descriptors()],
                'collections': {collection_id: [
                    element.descriptor.to_dict() for element in adapter.fields.values()]
                    for collection_id, adapter in self.entities.items()}}

    def describe_operations(self) -> dict:
        return {'operations': self.operations.to_list()}

    def describe_capabilities(self, names) -> dict:
        """Return explainable external-utility availability through the facade."""
        registry = self.domain._capabilities_registry()
        capabilities = (registry.utility(name) for name in names)
        return {'capabilities': [
            {'name': item.name, 'available': item.available,
             'executable': item.executable, 'reason': item.reason}
            for item in capabilities
        ]}

    # Case lifecycle runs through facade methods rather than the command
    # registry, since open/create precede a case_id.
    LIFECYCLE_OPERATIONS = ('case.create', 'case.open', 'case.close')
    APPLICATION_OPERATIONS = ('application.settings.patch',)

    def executable_operations(self) -> set[str]:
        """Every operation with an execution path: command handlers, lifecycle
        methods, presentation ops, and application-settings dispatch."""
        return (set(self._operations)
                | set(self.LIFECYCLE_OPERATIONS)
                | set(self.APPLICATION_OPERATIONS)
                | set(self.presentation_ops.operations()))

    def openapi(self) -> dict:
        """Generated OpenAPI/JSON-Schema components for the whole facade surface."""
        return {
            'openapi': '3.1.0',
            'info': {'title': 'FoamMesh facade', 'version': 'v1'},
            'paths': self.operations.openapi_paths(),
            'components': {'schemas': {
                'ConfigurationFields': self.fields.json_schema(),
                'OperationResult': {
                    'type': 'object',
                    'properties': {
                        'status': {'type': 'string'}, 'operation': {'type': 'string'},
                        'changed_fields': {'type': 'array', 'items': {'type': 'string'}},
                        'invalidated_outputs': {'type': 'array', 'items': {'type': 'string'}},
                    }},
            }},
        }

    def confirm_plan(self, case_id: str, plan_id: str, digest: str, *,
                     confirmed_by: str, ttl_seconds: int = 300) -> dict:
        plan = self.plans.get(plan_id)
        return self.plans.confirm(
            self.case(case_id), plan_id, digest, confirmed_by=confirmed_by,
            ttl_seconds=ttl_seconds,
            capability_digest=self._capability_digest(plan.required_capabilities))

    async def execute_plan(self, case_id: str, plan_id: str, token: str, *,
                           actor_id: str = 'external-agent') -> dict:
        session = self.case(case_id)
        authorization = {'plan_id': plan_id, 'token': token}
        pending = self.plans.get(plan_id)
        plan = self.plans.authorize_command(
            session, authorization,
            capability_digest=self._capability_digest(pending.required_capabilities))
        plan.state = PlanState.EXECUTING
        session._emit('plan.executing', plan_id=plan_id)
        results = []
        transaction_id = None
        try:
            for index, item in enumerate(plan.commands):
                command = Command(
                    item['operation'], case_id, item.get('parameters') or {},
                    Actor(actor_id, ActorKind.AGENT), CommandSource.AUTOMATION,
                    expected_revision=session.authored_revision,
                    idempotency_key=f'{plan_id}:{index}', authorization=authorization,
                    correlation_id=plan_id)
                result = (await self.execute(command)).to_dict()
                transaction_id = result['payload'].get('transaction_id') or transaction_id
                results.append(result)
        except Exception:
            plan.state = PlanState.FAILED
            session._emit('plan.failed', plan_id=plan_id)
            raise
        self.plans.mark_succeeded(session, plan, transaction_id=transaction_id)
        await session.flush_events()
        return {'plan': plan.public(), 'results': results}

    def plan(self, plan_id: str) -> dict:
        return self.plans.get(plan_id).public()

    def cancel_plan(self, case_id: str, plan_id: str) -> dict:
        return self.plans.cancel(self.case(case_id), plan_id)

    def field(self, case_id: str, field_id: str) -> dict:
        """A field descriptor plus the case's current value (GET .../fields/{id})."""
        from .field_adapters import read_value
        descriptor = self.fields.get(field_id)
        value = read_value(self.case(case_id).configuration(), descriptor.storage_path)
        return {**descriptor.to_dict(), 'value': value}

    async def history(self, case_id: str, *, limit: int = 200) -> dict:
        command = Command('history.query', case_id, {'limit': limit},
                          Actor('reader', ActorKind.API_CLIENT), CommandSource.REST)
        return (await self.execute(command)).payload

    def artifacts(self, case_id: str, *, limit: int = 200) -> dict:
        """Artifact-producing/mutating events recorded for the case."""
        session = self.case(case_id)
        artifact_events = [event for event in session.events_after(0, limit=10_000)
                           if event['event'].startswith('artifact.')
                           or event['event'] in ('state.restored',)]
        return {'artifacts': artifact_events[-limit:],
                'artifact_sequence': session.artifact_sequence}

    def register(self, operation: str, handler: Callable) -> None:
        if not operation or operation.startswith('/'):
            raise ValueError('operation must be a stable dotted identifier')
        self._operations[operation] = handler

    async def execute(self, command: Command) -> OperationResult:
        if command.scope == 'application' or command.operation.startswith('application.'):
            return self._execute_application(command)
        if command.scope == 'presentation' or command.operation.startswith('presentation.'):
            return self._execute_presentation(command)
        session = self.case(command.case_id)
        # §4.4/§6.5: every agent mutation must carry authorization from a
        # confirmed plan. Reads are unrestricted; any state-changing operation
        # (per the operation registry impact) requires a live authorized plan.
        # ``history.*`` operations (undo/redo/revert) enforce their own §6.8
        # authority and are handled inside their handler, not here.
        from .confirmation import requires_authorization
        if (command.actor.kind is ActorKind.AGENT
                and not command.operation.startswith('history.')
                and requires_authorization(self.operations, command.operation)):
            if not command.authorization:
                raise AuthorizationRequiredError('agent mutation requires a confirmed plan')
            pending = self.plans.get((command.authorization or {}).get('plan_id', ''))
            plan = self.plans.authorize_command(
                session, command.authorization,
                capability_digest=self._capability_digest(pending.required_capabilities))
            if plan.state not in (PlanState.AUTHORIZED, PlanState.EXECUTING):
                raise AuthorizationRequiredError('plan is not executable')
            if not any(command.operation == item['operation']
                       and command.parameters == (item.get('parameters') or {})
                       for item in plan.commands):
                raise AuthorizationRequiredError(
                    'operation parameters are not in the authorized plan',
                    details={'operation': command.operation})
        fingerprint = command.fingerprint()
        replay = session.get_idempotent(command.idempotency_key, fingerprint)
        if replay is not None:
            return OperationResult(
                replay.status, replay.operation, replay.revisions, replay.changed_fields,
                replay.invalidated_outputs, replay.warnings, replay.payload, True)
        try:
            handler = self._operations[command.operation]
        except KeyError as error:
            raise OperationNotFoundError('operation is not registered', details={
                'operation': command.operation,
            }) from error

        # Queries run beside the write queue rather than inside it (F-09).
        # Cancellation must not wait behind the command it stops; a runtime
        # probe boots a foreign runtime and used to make the next human edit
        # wait half a minute for it; and an ordinary read changes nothing, so
        # queueing it only ever bought the user a frozen dialog. The
        # configuration cache is deliberately not refreshed here -- a query
        # writes no configuration, so there is nothing to rebuild.
        if self.bypasses_queue(command.operation):
            session.set_event_context(command)
            try:
                session.assert_revision(command.expected_revision)
                # Measured like a queued command. Skipping the queue is not a
                # licence to sit on the event loop: a query that blocks for
                # 40 ms freezes every dialog just as thoroughly from here, and
                # the owner-loop budget is the only thing that says so.
                with session.monitor.measure(
                        'owner_loop_slice', context=command.operation):
                    value = handler(session, command)
                if inspect.isawaitable(value):
                    value = await value
                await session.flush_events()
                return value
            finally:
                session.clear_event_context()

        async def invoke():
            session.assert_revision(command.expected_revision)
            session.set_event_context(command)
            try:
                with session.monitor.measure(
                        'owner_loop_slice', context=command.operation):
                    value = handler(session, command)
                if inspect.isawaitable(value):
                    value = await value
                with session.monitor.measure(
                        'owner_loop_slice',
                        context=f'{command.operation} (bookkeeping)'):
                    if command.is_human_gui_command and value.changed_fields:
                        self.plans.supersede_for_human_edit(
                            session, actor_id=command.actor.id, command_id=command.command_id)
                    session.remember_idempotent(command.idempotency_key, fingerprint, value)
                await session.flush_events()
                # Rebuild the immutable snapshot DTO off-loop while the
                # scheduler still serializes access, so the next command
                # slice never parses the configuration on the owner loop.
                await session.refresh_configuration_cache()
                return value
            finally:
                session.clear_event_context()

        return await session.scheduler.submit(invoke, human_priority=command.is_human_gui_command)

    def execute_sync(self, command: Command) -> OperationResult:
        """Synchronous command path for the in-process desktop GUI's Qt slots.

        Some GUI actions run in synchronous Qt slots (dialog ``accept``, undo/
        redo, workflow-step change) that cannot ``await``. On the owner thread a
        synchronous handler runs atomically — it never yields to the loop — so
        it cannot interleave with async scheduled commands, and the ``local
        human`` GUI actor holds override priority. Only sync handlers are
        allowed here; anything awaitable must use :meth:`execute`.
        """
        if command.scope == 'application' or command.operation.startswith('application.'):
            return self._execute_application(command)
        if command.scope == 'presentation' or command.operation.startswith('presentation.'):
            return self._execute_presentation(command)
        session = self.case(command.case_id)
        session.assert_revision(command.expected_revision)
        session.set_event_context(command)
        try:
            value = self._operations[command.operation](session, command)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise RuntimeError(f'{command.operation} requires the async execute() path')
            if command.is_human_gui_command and value.changed_fields:
                self.plans.supersede_for_human_edit(
                    session, actor_id=command.actor.id, command_id=command.command_id)
            return value
        finally:
            session.clear_event_context()

    def _normalize_patch(self, session: CaseSession, patch, *, read_only_check: bool):
        """Validate a scalar patch against the registry; return the changed diff."""
        if not isinstance(patch, dict) or not patch:
            raise ValidationFailedError('patch must be a non-empty field/value object')
        unknown = sorted(set(patch) - set(FIELD_STORAGE))
        if unknown:
            raise ValidationFailedError('unknown facade field', details={'field_ids': unknown})
        configuration = session.configuration()
        changed = {}
        for field_id, value in patch.items():
            descriptor = self.fields.get(field_id)
            if read_only_check and descriptor.read_only:
                raise ValidationFailedError('field is read-only', details={'field_id': field_id})
            coerce_for_apply(descriptor, value)
            current = read_value(configuration, descriptor.storage_path)
            if not values_equal(current, value):
                changed[field_id] = {'before': current, 'after': value}
        return changed

    def _patch(self, session: CaseSession, command: Command) -> OperationResult:
        session.require_writable()
        patch = command.parameters.get('patch')
        session.field_claims.assert_available(
            patch if isinstance(patch, dict) else (), actor_id=command.actor.id)
        changed = self._normalize_patch(session, patch, read_only_check=True)
        if not changed:
            return OperationResult('accepted', command.operation, session.revisions,
                                   payload={'transaction_id': None, 'no_op': True})
        data = session.state.checkout()
        try:
            for field_id, diff in changed.items():
                data.setValue(FIELD_STORAGE[field_id], diff['after'], field_id)
        except Exception as error:
            raise ValidationFailedError('patch did not satisfy field validation', details={
                'error': str(error),
            }) from error
        self._validate_configuration_patch(changed, data)
        refuse_locked_edit(session, data)
        transaction = session.state.commit(
            data, action='facade configuration patch', source=_source(command.source),
            target=','.join(sorted(changed)), reason=f'actor={command.actor.id}')
        latest = session.latest_change_set()
        engine_id = _session_engine(session)
        invalidated = _invalidation_for(changed, engine_id)
        staled_tasks = _stale_engine_stages(session, engine_id, changed)
        _publish_verdict_staleness(session, invalidated, tuple(sorted(changed)))
        payload = {'transaction_id': transaction.tx_id,
                   'change_set_id': latest['change_set_id'] if latest else None}
        if staled_tasks:
            payload['staled_tasks'] = list(staled_tasks)
        return OperationResult('accepted', command.operation, session.revisions,
                               tuple(sorted(changed)), invalidated,
                               payload=payload)

    def _commit_working_copy(self, session: CaseSession, command: Command) -> OperationResult:
        """Commit a pre-built editable working copy through the facade (§4.6).

        The in-process desktop GUI is the highest authority and holds the real
        state object, so it may hand the facade an already-checked-out working
        copy (from ``client.checkout``) to commit atomically — the edit still
        goes through the command dispatcher, revision domains, change-set, and
        event journal, so views hold no direct ``ProjectState`` write path.

        This command is intentionally NOT reachable from the REST/agent surface:
        a serialized JSON body cannot supply a live editable db, and the type
        guard below rejects anything that is not one.
        """
        session.require_writable()
        data = command.parameters.get('working_copy')
        if not (hasattr(data, '_content') and hasattr(data, '_base') and getattr(data, '_editable', False)):
            raise ValidationFailedError('commit_working_copy requires an editable working copy')
        action = command.parameters.get('action') or 'gui edit'
        reason = command.parameters.get('reason') or f'actor={command.actor.id}'
        try:
            refuse_locked_edit(session, data)
            transaction = session.state.commit(
                data, action=action, source=_source(command.source),
                target=command.parameters.get('target'), reason=reason)
        except ConcurrentEditError as error:
            # The copy this dialog held is stale on the leaves it changed.
            # Same shape as any other revision conflict, plus the leaves, so
            # the dialog can name what to reopen instead of guessing.
            raise RevisionConflictError(
                'another change landed while this working copy was open',
                details={'paths': list(error.paths)}) from error
        latest = session.latest_change_set()
        # Plan 32 W4 (DP-242). A working copy handed in whole says nothing
        # about which fields moved, so there is no stage to scope to and the
        # honest answer is the widest one: from the first stage onwards, which
        # is every stage. A dialog that wants the narrow answer patches fields
        # through `configuration.patch`, where the registry supplies it.
        _publish_verdict_staleness(session, _WHOLE_WORKING_COPY_STALES, ())
        # DP-16. The merge renumbers an element when another copy had already
        # taken the key this one allocated. The caller is still holding the key
        # it was handed, so the move has to come back with the result rather
        # than only living on the working copy.
        return OperationResult('accepted', command.operation, session.revisions,
                               invalidated_outputs=_WHOLE_WORKING_COPY_STALES,
                               payload={'transaction_id': transaction.tx_id,
                                        'remapped_keys': getattr(
                                            data, 'remappedKeys', dict)(),
                                        'change_set_id': latest['change_set_id'] if latest else None})

    def _dry_run(self, session: CaseSession, command: Command) -> OperationResult:
        """Validate a patch and report the diff/invalidation without mutating."""
        changed = self._normalize_patch(session, command.parameters.get('patch'),
                                        read_only_check=True)
        diff = [{'field_id': field_id, **values} for field_id, values in sorted(changed.items())]
        return OperationResult('accepted', command.operation, session.revisions,
                               tuple(sorted(changed)),
                               _invalidation_for(changed, _session_engine(session)),
                               payload={'dry_run': True, 'diff': diff,
                                        'would_change': bool(changed)})

    # -- entity collection commands ---------------------------------------- #

    def _geometry_primitive_create(self, session: CaseSession,
                                   command: Command) -> OperationResult:
        fields = dict(command.parameters.get('fields') or {})
        primitive_shapes = {'hex', 'hex6', 'cylinder', 'sphere'}
        if fields.get('shape') not in primitive_shapes:
            raise ValidationFailedError('unsupported geometry primitive', details={
                'shape': fields.get('shape'), 'supported': sorted(primitive_shapes)})
        fields.setdefault('geometry_type', 'volume')
        fields.setdefault('cfd_type', 'none')
        translated = replace(command, parameters={**command.parameters, 'fields': fields})
        return self._make_collection_create('geometry.items')(session, translated)

    def _make_collection_create(self, collection_id: str) -> Callable:
        def handler(session: CaseSession, command: Command) -> OperationResult:
            session.require_writable()
            adapter = self.entities[collection_id]
            fields = command.parameters.get('fields') or {}
            normalized = adapter.normalize_patch(fields) if fields else {}
            self._validate_collection_scope(
                session, collection_id, normalized)
            self._stamp_surface_reference(session, collection_id, normalized)
            data = session.state.checkout()
            key, _element = data.addNewElement(adapter.storage_path)
            for relative_path, value in normalized.items():
                data.setValue(f'{adapter.storage_path}/{key}/{relative_path}', value, relative_path)
            self._share_layer_relative_sizes(  # DP-595
                collection_id, data, adapter.storage_path, key, normalized,
                created=True)
            self._validate_collection_row(
                collection_id, data, f'{adapter.storage_path}/{key}',
                normalized)
            refuse_locked_edit(session, data)
            transaction = session.state.commit(
                data, action=f'create {collection_id}', source=_source(command.source),
                target=f'{collection_id}/{key}', reason=f'actor={command.actor.id}')
            # DP-16. The entity id handed back has to be the one the element
            # ended up under: a merge that found the allocated key already
            # taken moves the new element rather than refusing it.
            key = data.remappedKey(adapter.storage_path, key)
            return self._collection_result(session, command, collection_id, key, transaction)
        return handler

    def _make_collection_patch(self, collection_id: str) -> Callable:
        def handler(session: CaseSession, command: Command) -> OperationResult:
            session.require_writable()
            adapter = self.entities[collection_id]
            key = str(command.parameters.get('entity_id', ''))
            data = session.state.checkout()
            if not data.hasElement(adapter.storage_path, key):
                raise ValidationFailedError('entity does not exist', details={
                    'collection': collection_id, 'entity_id': key})
            normalized = adapter.normalize_patch(command.parameters.get('fields') or {})
            self._validate_collection_scope(
                session, collection_id, normalized)
            self._stamp_surface_reference(session, collection_id, normalized)
            for relative_path, value in normalized.items():
                data.setValue(f'{adapter.storage_path}/{key}/{relative_path}', value, relative_path)
            self._share_layer_relative_sizes(  # DP-595
                collection_id, data, adapter.storage_path, key, normalized)
            self._validate_collection_row(
                collection_id, data, f'{adapter.storage_path}/{key}',
                normalized)
            refuse_locked_edit(session, data)
            transaction = session.state.commit(
                data, action=f'edit {collection_id}', source=_source(command.source),
                target=f'{collection_id}/{key}', reason=f'actor={command.actor.id}')
            return self._collection_result(session, command, collection_id, key, transaction)
        return handler

    @staticmethod
    def _validate_configuration_patch(changed: dict, data) -> None:
        """Rules a scalar field is judged by against the rest of the case.

        DP-577 (field audit 0924 snappy-front D4). The bounding Hex6 was a
        raw integer: an id naming no geometry row was saved and changed
        nothing, and the id of a plain ``hex`` refinement box was saved and
        dropped that box's refinement. Only a ``hex6`` volume can be the
        background block, so any other id is refused here, by name.
        """
        field_id = 'meshing.base_grid.bounding_hex6'
        if field_id not in changed:
            return
        selected = changed[field_id].get('after')
        if selected is None:
            return

        def shape_of(row):
            try:
                value = row.value('shape')
            except Exception:  # noqa: BLE001 - a row without a shape
                return None
            return getattr(value, 'value', value)

        try:
            rows = {str(key): row
                    for key, row in dict(data.getElements('geometry')).items()}
        except Exception:  # noqa: BLE001 - no geometry: nothing can match
            rows = {}
        row = rows.get(str(selected))
        shape = shape_of(row) if row is not None else None
        if shape == 'hex6':
            return
        hex6 = sorted(key for key, candidate in rows.items()
                      if shape_of(candidate) == 'hex6')
        found = ('names no geometry row' if row is None
                 else f'names a {shape} volume, not a hex6')
        offered = (', '.join(hex6) if hex6 else
                   'none; model a Hex6 volume first, or leave it empty for '
                   'the block derived from the geometry')
        raise ValidationFailedError(
            f'Bounding hex6 {selected} {found}. It must be the id of a Hex6 '
            f'volume in this case ({offered}).',
            details={'field': field_id, 'error': 'not_a_hex6',
                     'value': selected, 'hex6_ids': hex6})

    @staticmethod
    def _share_layer_relative_sizes(collection_id: str, data, storage_path: str,
                                    key: str, normalized,
                                    created: bool = False) -> None:
        """Keep ``relativeSizes`` one value across every layer group.

        DP-595 (field audit 0924 snappy-back D3). OpenFOAM 13 reads
        ``relativeSizes`` once, for the whole ``addLayersControls`` block, and
        the writer refuses to guess between groups that disagree. The box sat
        on each group, so two groups set differently were saved without a word
        and the dictionary build then failed. It is one setting shown on every
        group: changing it on one changes it on all, and a new group takes the
        value the others already share.
        """
        if collection_id != 'meshing.layers.groups':
            return
        try:
            others = [other for other in data.getKeys(storage_path)
                      if str(other) != str(key)]
        except Exception:  # noqa: BLE001 - no groups: nothing to share
            return
        if 'relativeSizes' in (normalized or {}):
            value = data.getValue(f'{storage_path}/{key}/relativeSizes')
            for other in others:
                data.setValue(f'{storage_path}/{other}/relativeSizes', value,
                              'relativeSizes')
        elif created and others:
            value = data.getValue(f'{storage_path}/{others[0]}/relativeSizes')
            data.setValue(f'{storage_path}/{key}/relativeSizes', value,
                          'relativeSizes')

    @staticmethod
    def _validate_collection_row(collection_id: str, data, row_path: str,
                                 normalized=None) -> None:
        """Rules that join two fields of one row, checked on the merged row.

        DP-575 (field audit 0924 snappy-front D2). Each surface level was
        validated alone, so a minimum above the maximum was saved and written
        as ``level (4 2)``, and OF13 then died at launch on it
        (``refinementSurfaces.C``: "Illegal level specification").
        """
        if collection_id == 'geometry.items':
            # DP-578: judged only when the binding is what this edit sets, so
            # a row bound before the rule can still be renamed or unbound.
            if 'castellationGroup' in (normalized or {}):
                FoamMeshFacade._validate_surface_group_binding(data, row_path)
            return
        if collection_id != 'meshing.castellation.surface_refinements':
            return
        try:
            minimum = data.getValue(f'{row_path}/surfaceRefinement/minimumLevel')
            maximum = data.getValue(f'{row_path}/surfaceRefinement/maximumLevel')
        except Exception:  # noqa: BLE001 - a row without levels has no rule
            return
        if minimum is None or maximum is None:
            return
        if int(minimum) > int(maximum):
            raise ValidationFailedError(
                f'Minimum level ({int(minimum)}) cannot be above Maximum level '
                f'({int(maximum)}): snappyHexMesh refines a surface between the '
                'two, so the minimum must be 0 to the maximum',
                details={'collection': collection_id,
                         'field': 'surface_refinement.minimum_level',
                         'error': 'minimum_above_maximum',
                         'minimum_level': int(minimum),
                         'maximum_level': int(maximum)})

    #: DP-578. The surface shapes the snappy writer can refine as a surface:
    #: a tessellated import and, since DP-668, the closed surface of a box,
    #: sphere or cylinder. The surface of a plane, disk or plate, or a Hex6
    #: face, has no such writer.
    _SURFACE_GROUP_SHAPES = ('triSurfaceMesh', '', None,
                             'hex', 'sphere', 'cylinder')

    @staticmethod
    def _validate_surface_group_binding(data, row_path: str) -> None:
        """Refuse binding a primitive's surface to a surface refinement group.

        DP-578 (field audit 0924 snappy-front D5). The picker offered every
        surface row, including the ``<name>_surface`` row the volume dialog
        makes for a box, sphere or cylinder; the writer only refines imported
        surfaces, so a group bound to one wrote nothing and said nothing.
        """
        def read(name):
            try:
                value = data.getValue(f'{row_path}/{name}')
            except Exception:  # noqa: BLE001 - the row has no such field
                return None
            return getattr(value, 'value', value)

        if read('gType') != 'surface' or read('castellationGroup') in (None, ''):
            return
        shape = read('shape')
        if shape in FoamMeshFacade._SURFACE_GROUP_SHAPES:
            return
        raise ValidationFailedError(
            f'Surface {read("name")} is the {shape} surface of a modelled '
            'shape, and a surface refinement group refines imported surfaces '
            'and the surface of a box, sphere or cylinder only; this binding '
            'would write nothing. Refine the shape through '
            'a volume refinement group instead.',
            details={'field': 'castellation_group', 'error': 'primitive_surface',
                     'shape': shape})

    @staticmethod
    def _stamp_surface_reference(session: CaseSession, collection_id: str,
                                 normalized: dict) -> None:
        """Record which prepared boundary a per-surface size row refines.

        Plan 33 W-G1 (FIELD-03). The row asks for a Gmsh surface tag, and a
        tag is a position in import order: renaming, merging or re-importing
        a boundary renumbers them, so every saved row moved onto a different
        face without saying anything. The boundary the tag stands for *now*
        is stamped beside it here, at the one place every authored row goes
        through, and the run resolves the tag from that instead.

        A tag no prepared boundary claims -- or one two of them claim, which
        is not an identity -- leaves the reference empty; such a row is read
        by its number, as it was before, and refused by name if that number
        reaches nothing when the run starts.
        """
        if collection_id != 'gmsh.surface_sizes.controls':
            return
        if 'surfaceId' not in normalized or normalized.get('surfaceRef'):
            return
        from foammesh.core.geometry import PreparedGeometryStore
        from foammesh.core.gmsh.size_fields import surface_reference_for_tag

        try:
            prepared = PreparedGeometryStore(session.case_path).current()
        except (OSError, ValueError):
            return
        if prepared is None:
            return
        reference = surface_reference_for_tag(
            prepared, normalized.get('surfaceId'))
        if reference:
            normalized['surfaceRef'] = reference

    @staticmethod
    def _validate_collection_scope(
            session: CaseSession, collection_id: str,
            normalized: dict) -> None:
        if collection_id not in _SCOPED_COLLECTIONS:
            return
        scope_fields = tuple(
            key for key in ('scopeToken', 'masterScopeToken', 'slaveScopeToken')
            if key in normalized)
        if not scope_fields:
            return
        from foammesh.core.geometry import PreparedGeometryStore
        try:
            prepared = PreparedGeometryStore(session.case_path).current()
        except (OSError, ValueError) as error:
            raise ValidationFailedError(
                f'prepared geometry scope catalogue is invalid: {error}'
            ) from error
        if prepared is None:
            raise ValidationFailedError(
                'prepare the current geometry before adding scoped '
                'controls', details={
                    'collection': collection_id,
                    'field': 'scope_token',
                    'error': 'prepared_geometry_required',
                })
        face_scopes = {
            str(group.get('patch_uuid') or '')
            for group in prepared.group_manifest.get('groups', ())}
        region_scopes = {
            str(region.get('region_uuid') or '')
            for region in prepared.group_manifest.get('regions', ())}
        allowed = (
            region_scopes if collection_id in _REGION_SCOPED_COLLECTIONS
            else face_scopes)
        for field in scope_fields:
            scope_id = str(normalized.get(field) or '').strip()
            if not scope_id and _scope_is_unused(collection_id, normalized):
                # DP-665. A Box, Ball, Cylinder, Frustum or MathEval size
                # field has no geometric scope; its dialog sends a blank one.
                continue
            if not scope_id:
                raise ValidationFailedError(
                    'a stable prepared geometry scope is required', details={
                        'collection': collection_id,
                        'field': field,
                        'error': 'missing_scope',
                    })
            if scope_id not in allowed:
                raise ValidationFailedError(
                    'scope is not a current stable prepared geometry ID',
                    details={
                        'collection': collection_id,
                        'field': field,
                        'scope_id': scope_id,
                        'error': 'orphan_scope',
                        'allowed_scope_ids': sorted(allowed),
                    })

    def _make_collection_remove(self, collection_id: str) -> Callable:
        def handler(session: CaseSession, command: Command) -> OperationResult:
            session.require_writable()
            adapter = self.entities[collection_id]
            key = str(command.parameters.get('entity_id', ''))
            data = session.state.checkout()
            if not data.hasElement(adapter.storage_path, key):
                raise ValidationFailedError('entity does not exist', details={
                    'collection': collection_id, 'entity_id': key})
            data.removeElement(adapter.storage_path, key)
            refuse_locked_edit(session, data)
            transaction = session.state.commit(
                data, action=f'remove {collection_id}', source=_source(command.source),
                target=f'{collection_id}/{key}', reason=f'actor={command.actor.id}')
            return self._collection_result(session, command, collection_id, key, transaction)
        return handler

    @staticmethod
    def _collection_result(session, command, collection_id, key, transaction) -> OperationResult:
        latest = session.latest_change_set()
        return OperationResult('accepted', command.operation, session.revisions,
                               (f'{collection_id}/{key}',), ('mesh', 'quality'),
                               payload={'entity_id': key, 'transaction_id': transaction.tx_id,
                                        'change_set_id': latest['change_set_id'] if latest else None})

    def _execute_application(self, command: Command) -> OperationResult:
        """Application settings live outside any case revision domain (§6.7)."""
        from .results import RevisionSnapshot
        if command.operation != 'application.settings.patch':
            raise OperationNotFoundError('application operation is not registered', details={
                'operation': command.operation})
        values = command.parameters.get('values')
        if not isinstance(values, dict) or not values:
            raise ValidationFailedError('values must be a non-empty settings object')
        outcome = self.application.patch(
            values, expected_revision=command.parameters.get('expected_settings_revision'))
        return OperationResult('accepted', command.operation, RevisionSnapshot(0, 0, 0, 0),
                               tuple(outcome['changed_settings']), payload=outcome)

    def _execute_presentation(self, command: Command) -> OperationResult:
        """Presentation ops mutate the attached desktop surface only (§6.7).

        They advance ``presentation_sequence`` and emit memory-only events, so
        they never stale an engineering plan. With no rendering surface
        attached, they return ``presentation_unavailable``.
        """
        from .errors import PresentationUnavailableError
        session = self.case(command.case_id)
        if session.presentation is None:
            raise PresentationUnavailableError('no desktop rendering session is attached')
        session.set_event_context(command)
        try:
            changed = self.presentation_ops.apply(
                session.presentation, command.operation, command.parameters)
            session.bump_presentation(command.operation, **changed)
        finally:
            session.clear_event_context()
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'presentation': changed,
                                        'state': session.presentation.to_dict()})

    def _undo(self, session: CaseSession, command: Command) -> OperationResult:
        if command.actor.kind is not ActorKind.HUMAN or command.source is not CommandSource.GUI:
            raise UndoNotAllowedError('only the local human GUI owner may use generic undo')
        session.require_writable()
        transaction = session.state.undo()
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'undone': transaction is not None,
                                        'transaction_id': transaction.tx_id if transaction else None})

    def _redo(self, session: CaseSession, command: Command) -> OperationResult:
        if command.actor.kind is not ActorKind.HUMAN or command.source is not CommandSource.GUI:
            raise UndoNotAllowedError('only the local human GUI owner may use generic redo')
        session.require_writable()
        transaction = session.state.redo()
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'redone': transaction is not None,
                                        'transaction_id': transaction.tx_id if transaction else None})

    def _revert_latest(self, session: CaseSession, command: Command,
                       change_set_id: str | None) -> list:
        """Undo every trailing transaction of the latest change set (§6.8).

        Reversion is headless-safe: any actor may revert only the latest
        reversible authored change set, never over a stale revision. Older
        work needs a compensating plan instead of a privileged rollback.
        """
        session.require_writable()
        if command.expected_revision is None:
            raise ValidationFailedError('revert requires expected_revision')
        latest = session.latest_change_set()
        if latest is None or not change_set_id or latest['change_set_id'] != change_set_id:
            raise UndoNotAllowedError('only the latest authored change set may be reverted', details={
                'requested_change_set': change_set_id,
                'latest_change_set': latest['change_set_id'] if latest else None,
            })
        if latest['kind'] != 'edit':
            raise UndoNotAllowedError('change set is not a reversible edit', details={
                'kind': latest['kind']})
        # A confirmed plan may have applied several transactions; they form one
        # atomic undo group, so revert every trailing entry of the change set.
        count = 0
        for record in reversed(session.change_sets):
            if record['change_set_id'] != change_set_id or record['kind'] != 'edit':
                break
            count += 1
        transactions = []
        for _ in range(count):
            transaction = session.state.undo()
            if transaction is None:
                break
            transactions.append(transaction)
        return transactions

    async def _cancel_active_jobs(self, session: CaseSession,
                                  command: Command) -> OperationResult:
        """Everything the case is running, and a stage snapshot being copied.

        Plan 37 UF5. A stage snapshot is copied in a worker thread after the
        stage's process has ended, so no job owns it; its copy loop checks a
        cancel event every chunk, which is what bounds the wait here.
        """
        from foammesh.core.jobs import stage_snapshots

        copies = stage_snapshots.cancel_all(session.case_path)
        result = await self.slice_operations.cancel_active_jobs(session, command)
        if not copies:
            return result
        payload = dict(result.payload)
        payload['snapshot_copies_cancelled'] = copies
        payload['cancelled'] = int(payload.get('cancelled') or 0) + copies
        return replace(result, payload=payload)

    def _revert_change_set(self, session: CaseSession, command: Command) -> OperationResult:
        change_set_id = command.parameters.get('change_set_id')
        transactions = self._revert_latest(session, command, change_set_id)
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'reverted_change_set': change_set_id,
                                        'transaction_ids': [t.tx_id for t in transactions]})

    def _revert_plan(self, session: CaseSession, command: Command) -> OperationResult:
        """`revert_plan(plan_id)`: safe reversion of an executed agent plan."""
        plan_id = command.parameters.get('plan_id')
        if not isinstance(plan_id, str) or not plan_id:
            raise ValidationFailedError('plan_id is required')
        plan = self.plans.get(plan_id or '')
        if plan.case_id != session.case_id:
            raise ValidationFailedError('plan does not belong to this case')
        if plan.state is not PlanState.SUCCEEDED:
            raise UndoNotAllowedError('only a succeeded plan can be reverted', details={
                'plan_id': plan_id, 'state': plan.state.value})
        latest = session.latest_change_set()
        if (plan.applied_revision != session.authored_revision
                or latest is None
                or latest.get('change_set_id') != plan_id):
            raise PlanStaleError('plan is no longer the latest reversible change', details={
                'plan_id': plan_id,
                'plan_revision': plan.applied_revision,
                'current_revision': session.authored_revision,
            })
        transactions = self._revert_latest(session, command, plan_id)
        self.plans.mark_reverted(
            session, plan,
            transaction_id=transactions[0].tx_id if transactions else None)
        return OperationResult('accepted', command.operation, session.revisions,
                               payload={'reverted': bool(transactions),
                                        'reverted_plan': plan_id,
                                        'transaction_ids': [t.tx_id for t in transactions]})
