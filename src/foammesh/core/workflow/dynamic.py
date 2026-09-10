"""Dependency-aware runtime state for engine-published workflow descriptors."""
from __future__ import annotations

from dataclasses import dataclass

from foammesh.core.engine.contracts import (
    TaskCardinality, TaskState, WorkflowDescriptor,
)


#: States that unblock a dependent task.
_ACCEPTED = {TaskState.PASSED, TaskState.WARNING, TaskState.SKIPPED,
             TaskState.COMPLETED, TaskState.WAIVED}
#: States that hold a result, and so can be staled or reverted.
#:
#: ``COMPLETED`` and ``WAIVED`` belong in **both** sets, and the second is the
#: one that is easy to miss. This set gates every staling and edit path --
#: ``_invalidate_descendants``, the public ``invalidate``, and
#: ``revert_and_edit``. Omitting them here would mean a ``COMPLETED`` GF2 never
#: goes ``STALE`` when the tolerance policy or the mesh changes, and could never
#: be reverted -- and ``COMPLETED`` is the state the report tasks spend
#: virtually their whole lives in, so invalidation would skip its main subjects.
_HAS_ARTIFACT = {TaskState.PASSED, TaskState.WARNING, TaskState.STALE,
                 TaskState.COMPLETED, TaskState.WAIVED}
#: States a report task may be completed from. Public because the recorder has
#: to know which states need configuring first, and a second copy of this set
#: living there is how the two come to disagree.
COMPLETABLE = frozenset({TaskState.RUNNING, TaskState.CONFIGURED,
                         TaskState.STALE, TaskState.READY, TaskState.FAILED})


@dataclass(frozen=True)
class TaskTransition:
    task_id: str
    before: TaskState
    after: TaskState
    invalidated: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'before': self.before.value,
            'after': self.after.value,
            'invalidated': list(self.invalidated),
        }


class EngineWorkflowGraph:
    """Mutable state machine over an immutable :class:`WorkflowDescriptor`."""

    def __init__(self, descriptor: WorkflowDescriptor, state: dict | None = None):
        self.descriptor = descriptor
        self._state = {
            task.task_id: TaskState.LOCKED for task in descriptor.ordered_tasks()
        }
        self._refresh_ready()
        if state:
            self.load(state)

    def state(self, task_id: str) -> TaskState:
        self.descriptor.task(task_id)
        return self._state[task_id]

    def is_runnable(self, task_id: str) -> bool:
        task = self.descriptor.task(task_id)
        return all(self._state[parent] in _ACCEPTED for parent in task.depends_on)

    def blocking_prerequisites(self, task_id: str) -> tuple[str, ...]:
        """The prerequisites of ``task_id`` that are not accepted yet.

        The answer to the question every lock refusal used to leave hanging.
        Plan 31 DP-39, MEASURED on every Gmsh run of the `t3` and `t3-redo`
        legs: a user who saved settings on Global Sizing, Boundary Layers or
        Compute Mesh was shown `task is locked by prerequisites:
        gmsh.boundary_layers` -- the id of the task they were already looking
        at, and nothing about what was holding it. Which of the seven manual
        Gmsh tasks is unaccepted is knowable here and nowhere else, so it is
        computed here and carried in the refusal.
        """
        task = self.descriptor.task(task_id)
        return tuple(parent for parent in task.depends_on
                     if self._state[parent] not in _ACCEPTED)

    def _locked(self, task_id: str) -> ValueError:
        """The refusal a locked task raises, naming what it waits on.

        The leading clause is unchanged and load-bearing: the facade, the CLI
        and the recorders all match on it to tell a lock apart from every
        other refusal.
        """
        blocking = self.blocking_prerequisites(task_id)
        names = ', '.join(self.descriptor.task(parent).title
                          for parent in blocking)
        return ValueError(
            f'task is locked by prerequisites: {task_id}'
            + (f' (waiting on {names})' if names else ''))

    def begin_edit(self, task_id: str) -> TaskTransition:
        if not self.is_runnable(task_id):
            raise self._locked(task_id)
        before = self._state[task_id]
        if before is TaskState.RUNNING:
            raise ValueError(f'task is already running: {task_id}')
        self._state[task_id] = TaskState.EDITING
        return TaskTransition(task_id, before, TaskState.EDITING)

    def configure(self, task_id: str) -> TaskTransition:
        if not self.is_runnable(task_id):
            raise self._locked(task_id)
        before = self._state[task_id]
        self._state[task_id] = TaskState.CONFIGURED
        invalidated = self._invalidate_descendants(task_id)
        self._refresh_ready()
        return TaskTransition(task_id, before, TaskState.CONFIGURED, invalidated)

    def start(self, task_id: str) -> TaskTransition:
        if not self.is_runnable(task_id):
            raise self._locked(task_id)
        before = self._state[task_id]
        if before not in {TaskState.READY, TaskState.CONFIGURED, TaskState.STALE,
                          TaskState.FAILED, TaskState.WARNING}:
            raise ValueError(f'task cannot start from {before.value}: {task_id}')
        self._state[task_id] = TaskState.RUNNING
        return TaskTransition(task_id, before, TaskState.RUNNING)

    def finish(self, task_id: str, *, warning: bool = False) -> TaskTransition:
        before = self._state[task_id]
        if before not in {TaskState.RUNNING, TaskState.CONFIGURED}:
            raise ValueError(f'task is not running/configured: {task_id}')
        after = TaskState.WARNING if warning else TaskState.PASSED
        self._state[task_id] = after
        self._refresh_ready()
        return TaskTransition(task_id, before, after)

    def fail(self, task_id: str) -> TaskTransition:
        before = self._state[task_id]
        if before is not TaskState.RUNNING:
            raise ValueError(f'task is not running: {task_id}')
        self._state[task_id] = TaskState.FAILED
        self._refresh_ready()
        return TaskTransition(task_id, before, TaskState.FAILED)

    def complete(self, task_id: str, *, verdict: str = '') -> TaskTransition:
        """Record that a task produced a valid report (Plan 23 §8.5).

        Distinct from :meth:`finish`: this says the evidence exists, not that
        the mesh passed. ``verdict`` is the report's own engineering result and
        is carried for display only -- it never changes the state, because a
        ``fail`` report is still complete evidence.
        """
        task = self.descriptor.task(task_id)
        before = self._state[task_id]
        # Prerequisites first: when both checks would refuse, an unmet
        # prerequisite is the actionable reason, and the task's own state is
        # merely a consequence of it. Completing behind a stale parent would
        # otherwise create an accepted task with an unaccepted prerequisite.
        if not self.is_runnable(task_id):
            raise self._locked(task_id)
        if before not in COMPLETABLE:
            raise ValueError(
                f'task cannot complete from {before.value}: {task_id}')
        self._state[task_id] = TaskState.COMPLETED
        invalidated = self._invalidate_descendants(task_id)
        self._refresh_ready()
        _ = task, verdict
        return TaskTransition(task_id, before, TaskState.COMPLETED, invalidated)

    def waive(self, task_id: str, *, waiver: str = '') -> TaskTransition:
        """Let a non-passing report proceed, on the record (Plan 23 §8.6).

        Only a task that declares ``accepts_override`` may be waived, and the
        waiver never becomes a pass: the state is its own, and the report's
        verdict is untouched.
        """
        task = self.descriptor.task(task_id)
        if not task.accepts_override:
            raise ValueError(f'task does not accept an override: {task_id}')
        before = self._state[task_id]
        if not self.is_runnable(task_id):
            raise self._locked(task_id)
        if before not in {TaskState.FAILED, TaskState.WARNING,
                          TaskState.COMPLETED}:
            raise ValueError(
                f'task cannot be waived from {before.value}: {task_id}')
        self._state[task_id] = TaskState.WAIVED
        self._refresh_ready()
        _ = waiver
        return TaskTransition(task_id, before, TaskState.WAIVED)

    def supersede(self, task_id: str, *, run_id: str = '',
                  reason: str = '') -> TaskTransition:
        """Stale a task whose inputs changed while it was running.

        ``invalidate`` ignores ``RUNNING``, so without this a check whose inputs
        changed mid-flight would finish and publish evidence for a subject that
        no longer exists. Recorded as its own transition rather than by adding
        ``RUNNING`` to :data:`_HAS_ARTIFACT`, which would lose which run was
        superseded.
        """
        self.descriptor.task(task_id)
        before = self._state[task_id]
        if before not in {TaskState.RUNNING, TaskState.CONFIGURED}:
            raise ValueError(
                f'task cannot be superseded from {before.value}: {task_id}')
        self._state[task_id] = TaskState.STALE
        invalidated = self._invalidate_descendants(task_id)
        self._refresh_ready()
        _ = run_id, reason
        return TaskTransition(task_id, before, TaskState.STALE, invalidated)

    def skip(self, task_id: str) -> TaskTransition:
        task = self.descriptor.task(task_id)
        if task.cardinality is TaskCardinality.REQUIRED:
            raise ValueError(f'required task cannot be skipped: {task_id}')
        if not self.is_runnable(task_id):
            raise self._locked(task_id)
        before = self._state[task_id]
        self._state[task_id] = TaskState.SKIPPED
        invalidated = self._invalidate_descendants(task_id)
        self._refresh_ready()
        return TaskTransition(task_id, before, TaskState.SKIPPED, invalidated)

    def revert_and_edit(self, task_id: str) -> TaskTransition:
        before = self._state[task_id]
        if before not in _HAS_ARTIFACT | {TaskState.FAILED, TaskState.SKIPPED}:
            raise ValueError(f'task has no accepted revision to edit: {task_id}')
        invalidated = self._invalidate_descendants(task_id)
        self._state[task_id] = TaskState.EDITING
        self._refresh_ready()
        return TaskTransition(task_id, before, TaskState.EDITING, invalidated)

    def invalidate(self, task_id: str, *, include_self: bool = True) -> tuple[str, ...]:
        targets = list(self.descriptor.descendants(task_id))
        if include_self:
            targets.insert(0, task_id)
        changed = []
        for target in targets:
            if self._state[target] in _HAS_ARTIFACT | {
                    TaskState.SKIPPED, TaskState.CONFIGURED, TaskState.FAILED}:
                self._state[target] = TaskState.STALE
                changed.append(target)
        self._refresh_ready()
        return tuple(changed)

    def next_runnable(self) -> str | None:
        for task in self.descriptor.ordered_tasks():
            if self._state[task.task_id] in {
                    TaskState.READY, TaskState.CONFIGURED, TaskState.STALE,
                    TaskState.FAILED} and self.is_runnable(task.task_id):
                return task.task_id
        return None

    def to_dict(self) -> dict:
        return {
            'engine_id': self.descriptor.engine_id,
            'workflow_version': self.descriptor.version,
            'workflow_digest': self.descriptor.digest,
            'tasks': {task.task_id: self._state[task.task_id].value
                      for task in self.descriptor.ordered_tasks()},
        }

    def load(self, document: dict) -> None:
        if document.get('engine_id', self.descriptor.engine_id) != self.descriptor.engine_id:
            raise ValueError('workflow state belongs to a different engine')
        if int(document.get('workflow_version', self.descriptor.version)) != self.descriptor.version:
            raise ValueError('workflow state uses an unsupported descriptor version')
        states = document.get('tasks', document)
        if not isinstance(states, dict):
            raise ValueError('workflow task state must be an object')
        for task_id, value in states.items():
            if task_id not in self._state:
                continue
            self._state[task_id] = TaskState(value)
        self._validate_loaded_dependencies()
        self._refresh_ready()

    def _invalidate_descendants(self, task_id: str) -> tuple[str, ...]:
        changed = []
        for target in self.descriptor.descendants(task_id):
            if self._state[target] in _HAS_ARTIFACT | {
                    TaskState.SKIPPED, TaskState.CONFIGURED, TaskState.FAILED}:
                self._state[target] = TaskState.STALE
                changed.append(target)
        return tuple(changed)

    def _refresh_ready(self) -> None:
        for task in self.descriptor.ordered_tasks():
            state = self._state[task.task_id]
            runnable = self.is_runnable(task.task_id)
            if state is TaskState.LOCKED and runnable:
                self._state[task.task_id] = TaskState.READY
            elif state is TaskState.READY and not runnable:
                self._state[task.task_id] = TaskState.LOCKED

    def _validate_loaded_dependencies(self) -> None:
        for task in self.descriptor.ordered_tasks():
            state = self._state[task.task_id]
            if state in _ACCEPTED | {TaskState.RUNNING, TaskState.CONFIGURED}:
                missing = [parent for parent in task.depends_on
                           if self._state[parent] not in _ACCEPTED]
                if missing:
                    raise ValueError(
                        f'task {task.task_id!r} is {state.value} before {missing}')
