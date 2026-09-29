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

    def _blocking(self, task_id: str, seen: set | None = None) -> list:
        """The prerequisites genuinely holding ``task_id``, in descriptor order.

        Plan 31 DP-144. An untouched optional prerequisite is not one. The run
        already agrees: `task_state_store._record` skips every OPTIONAL or
        REPEATABLE task nobody configured (DP-35), so "Run to end" walks
        straight past them. Only the manual walk disagreed, and because Gmsh
        chains its five optional tasks in a line, that disagreement reached
        every page after them -- Compute Mesh sat LOCKED behind Periodic
        Pairs, and a user who wanted boundary layers and nothing else had to
        press Save on three pages of settings they did not want, with no Skip
        control anywhere in the interface to decline them.

        An untouched optional parent is stepped *through*, not ignored: its
        own blockers are this task's blockers, so `gmsh.compute` is still held
        by Global Sizing until Global Sizing is accepted. LOCKED and READY are
        the two untouched states; anything further along (CONFIGURED, RUNNING,
        FAILED) is a task the user opted into, and those still hold.
        """
        seen = set() if seen is None else seen
        found: list[str] = []
        for parent in self.descriptor.task(task_id).depends_on:
            if parent in seen:
                continue
            seen.add(parent)
            state = self._state[parent]
            if state in _ACCEPTED:
                continue
            untouched = state in {TaskState.LOCKED, TaskState.READY}
            if untouched and (self.descriptor.task(parent).cardinality
                              is not TaskCardinality.REQUIRED):
                found.extend(self._blocking(parent, seen))
                continue
            found.append(parent)
        order = {task.task_id: task.order
                 for task in self.descriptor.ordered_tasks()}
        return sorted(found, key=lambda item: order[item])

    def is_runnable(self, task_id: str) -> bool:
        return not self._blocking(task_id)

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

        DP-144 narrowed "not accepted" to "not accepted and not an optional
        step the run would skip"; see :meth:`_blocking`.
        """
        return tuple(self._blocking(task_id))

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

    def restate(self, task_id: str, *, warning: bool = False) -> TaskTransition:
        """Replace a finished stage's result with the result of its rerun.

        DP-817. A stage that has run again has a new result, and the old one
        is no longer evidence of anything. ``finish`` cannot say so, because
        it only leaves RUNNING or CONFIGURED, and reaching those from PASSED
        goes through ``configure``, which stales every descendant. A rerun
        that reproduces the mesh its descendants were built on has not made
        them stale. So this moves between PASSED and WARNING and nothing else:
        no other state is a stage result, and no descendant is touched.
        """
        before = self._state[task_id]
        if before not in {TaskState.PASSED, TaskState.WARNING}:
            raise ValueError(
                f'task has no run result to replace: {task_id}')
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

    #: What a parent may be in while the task below it is SKIPPED (DP-241).
    #:
    #: A skip says nothing ran here, and nothing running does not require the
    #: prerequisites to have run either. DP-144 made the optional chain
    #: walkable -- an untouched optional parent does not hold the task behind
    #: it shut -- so a user may legitimately decline `Curve controls` while
    #: `Size fields` is still untouched, and the product writes that document
    #: itself. Judged by the rule below, it could not read it back: the load
    #: raised, the store reported `inconsistent_state`, and every accepted
    #: stage on every row was replaced with a fresh document.
    #:
    #: LOCKED is deliberately not here. LOCKED means the workflow never
    #: offered the row, so a skip recorded under one did not come from a press
    #: the user made, and neither are EDITING, FAILED, RUNNING or STALE: each
    #: of those is a parent mid-flight, which is not a state a considered
    #: decision about the row behind it was taken in.
    _SKIP_PARENTS = _ACCEPTED | {TaskState.READY, TaskState.CONFIGURED}

    def _validate_loaded_dependencies(self) -> None:
        for task in self.descriptor.ordered_tasks():
            state = self._state[task.task_id]
            if state is TaskState.SKIPPED:
                allowed = self._SKIP_PARENTS
            elif state in _ACCEPTED | {TaskState.RUNNING, TaskState.CONFIGURED}:
                allowed = _ACCEPTED
            else:
                continue
            missing = [parent for parent in task.depends_on
                       if self._state[parent] not in allowed]
            if missing:
                # DP-241, left standing. This said `is skipped before
                # ['gmsh.size_fields']`: it named the pair of ids and neither
                # of the two states, and the store turns it into one reason
                # code, so the sentence a user reads after their saved
                # progress has been discarded said only that something was
                # inconsistent. Both halves of the contradiction are named,
                # with the state each is in, because that is the whole of
                # what makes it a contradiction.
                detail = ', '.join(
                    f'{parent!r} is {self._state[parent].value}'
                    for parent in missing)
                raise ValueError(
                    f'task {task.task_id!r} is {state.value} while {detail}')
