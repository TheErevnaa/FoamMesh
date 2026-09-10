"""The small AF1 facade: registry, concurrency checks, and AF1V field patch."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace

from foammesh.core.project import Event, Source

from .application_session import ApplicationSession
from .commands import Actor, ActorKind, Command, CommandSource
from .domain_operations import DomainOperations
from foammesh.support.simple_db.simple_db import ConcurrentEditError

from .errors import (AuthorizationRequiredError, CaseNotFoundError, OperationNotFoundError,
                     PlanStaleError, RevisionConflictError, UndoNotAllowedError,
                     ValidationFailedError)
from .field_adapters import build_entity_adapters, coerce_for_apply, read_value, values_equal
from .fields import REGISTRY as FIELD_REGISTRY
from .operations import OperationKind, build_operation_registry
from .presentation import PresentationOperations, PresentationState
from .results import OperationResult
from .session import CaseSession
from .plans import PlanState, PlanStore
from .vertical_slice import VerticalSliceOperations


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


# The full AF2 semantic-ID -> storage-path map, generated from the schema.
FIELD_STORAGE = FIELD_REGISTRY.storage_map()

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


def _invalidation_for(field_ids) -> tuple[str, ...]:
    """Union of the artifact fingerprints the changed fields stale (§6.7)."""
    invalidated: set[str] = set()
    for field_id in field_ids:
        if field_id in FIELD_REGISTRY:
            invalidated.update(FIELD_REGISTRY.get(field_id).invalidates)
    return tuple(sorted(invalidated))


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
    """
    if 'mesh' not in set(invalidated or ()):
        return
    try:
        session.state.bus.publish(
            Event.MESH_VERDICT_STALE, fields=tuple(changed or ()),
            invalidated=tuple(invalidated or ()))
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
        self.register('job.cancel_active', self.slice_operations.cancel_active_jobs)
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
        return session

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
                with session.monitor.measure('owner_loop_slice'):
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
                with session.monitor.measure('owner_loop_slice'):
                    value = handler(session, command)
                if inspect.isawaitable(value):
                    value = await value
                with session.monitor.measure('owner_loop_slice'):
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
        transaction = session.state.commit(
            data, action='facade configuration patch', source=_source(command.source),
            target=','.join(sorted(changed)), reason=f'actor={command.actor.id}')
        latest = session.latest_change_set()
        invalidated = _invalidation_for(changed)
        _publish_verdict_staleness(session, invalidated, tuple(sorted(changed)))
        return OperationResult('accepted', command.operation, session.revisions,
                               tuple(sorted(changed)), invalidated,
                               payload={'transaction_id': transaction.tx_id,
                                        'change_set_id': latest['change_set_id'] if latest else None})

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
        _publish_verdict_staleness(session, ('mesh', 'quality'), ())
        # DP-16. The merge renumbers an element when another copy had already
        # taken the key this one allocated. The caller is still holding the key
        # it was handed, so the move has to come back with the result rather
        # than only living on the working copy.
        return OperationResult('accepted', command.operation, session.revisions,
                               invalidated_outputs=('mesh', 'quality'),
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
                               tuple(sorted(changed)), _invalidation_for(changed),
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
            data = session.state.checkout()
            key, _element = data.addNewElement(adapter.storage_path)
            for relative_path, value in normalized.items():
                data.setValue(f'{adapter.storage_path}/{key}/{relative_path}', value, relative_path)
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
            for relative_path, value in normalized.items():
                data.setValue(f'{adapter.storage_path}/{key}/{relative_path}', value, relative_path)
            transaction = session.state.commit(
                data, action=f'edit {collection_id}', source=_source(command.source),
                target=f'{collection_id}/{key}', reason=f'actor={command.actor.id}')
            return self._collection_result(session, command, collection_id, key, transaction)
        return handler

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
