"""Persisted engine task state beneath ``foammesh/workflow/``.

An engine branch that rebuilds its :class:`EngineWorkflowGraph` stateless on
every refresh persists, loads and advances nothing, so every task past the
first stays locked forever and "reopen on the same visible stage" is
unimplemented.  This store is that persistence seam.

State lives in a versioned JSON file inside the case, not in the SimpleDB
configuration: task progress is derived runtime state tied to artifacts, and
putting it in configuration would publish it through the AF2 field registry as
if it were an editable engineering field (which gate 10 forbids).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

from foammesh.core.engine.contracts import (
    TaskCardinality, TaskState, WorkflowDescriptor,
)

from .dynamic import COMPLETABLE, EngineWorkflowGraph


#: v2 adds a per-task ``evidence`` map beside ``tasks``.
SCHEMA_VERSION = 2

#: v1 is read without a reset. It carries ``tasks`` in exactly the shape v2
#: uses and simply has no evidence, so it upgrades on the next save. Treating
#: the bump as a migration would discard every in-flight case's progress for a
#: field that was purely additive -- a reset users would experience as data
#: loss and that buys nothing.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)

#: States a recorder leaves alone: the task already carries a result, so
#: recording again would either fail or overwrite evidence with weaker evidence.
#:
#: R184. ``SKIPPED`` is in this set and is the one member for which that
#: reasoning inverts. The "result" it records is *that nothing ran*, so a run
#: arriving afterwards is not weaker evidence over stronger -- it is the only
#: evidence there is. MEASURED on the live tee: Boundary Layers was proceeded
#: past unconfigured, a layer group was then added and ``addLayers`` ran to
#: completion (1,392 -> 3,316 cells, 2.7 layers on the wall), and the run was
#: filed as ``already recorded`` against the skip. So a skipped task is settled
#: against everything except a run that directly performed it; see
#: ``performed`` on :meth:`_record`.
_SETTLED = frozenset({
    TaskState.PASSED, TaskState.WARNING, TaskState.SKIPPED,
    TaskState.COMPLETED, TaskState.WAIVED,
})

def run_gated_task_ids(descriptor: WorkflowDescriptor) -> frozenset[str]:
    """Tasks this engine only lets a recorded run accept.

    Declared on the task rather than listed here, so an engine cannot gain a
    compute stage that a manual Update can mark passed.
    """
    return frozenset(task.task_id for task in descriptor.ordered_tasks()
                     if task.run_gated)


class TaskStateError(ValueError):
    pass


@dataclass(frozen=True)
class TaskStateLoadResult:
    """A loaded graph, and why it is not the one on disk if it is not.

    ``load`` returned only a graph, so a reset was indistinguishable from a
    clean read and could not be explained to anyone. Since a reset silently
    discards accepted stages that a user can see disappear, the reason has to
    travel with the graph as far as the GUI, CLI and API.
    """

    graph: EngineWorkflowGraph
    reset_reason: str = ''
    prior: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)
    #: What the document was refused for, in the loader's own words, when the
    #: refusal was about something inside it. DP-241, left standing: the
    #: reason code says a file was inconsistent and cannot say with what, so
    #: the one contradiction that caused a reset was named in an exception
    #: nobody catches and shown to nobody. Empty for the resets that are about
    #: the file as a whole -- an older schema, a changed workflow -- which
    #: have no task pair behind them.
    detail: str = ''

    @property
    def was_reset(self) -> bool:
        return bool(self.reset_reason)

    def notice(self) -> dict | None:
        """What a surface shows the user, or ``None`` when nothing happened."""
        if not self.was_reset:
            return None
        message = _RESET_MESSAGES.get(
            self.reset_reason, 'Saved workflow progress could not be read '
                               'and has been reset.')
        if self.detail:
            message = f'{message} ({self.detail})'
        return {
            'reason': self.reset_reason,
            'message': message,
            'detail': self.detail,
            'prior': dict(self.prior),
            'current': dict(self.current),
        }


#: Why a reset happened, in words a user can act on. Kept distinct rather than
#: collapsed into "the workflow changed", because the remedies differ: an older
#: build wrote the file, versus the workflow itself was redefined.
_RESET_MESSAGES = {
    'schema_version': (
        'Saved workflow progress was written by a different version of '
        'FoamMesh and has been reset. Meshes and reports are untouched.'),
    'workflow_version': (
        'The meshing workflow advanced to a new version, so saved task '
        'progress has been reset. Meshes and reports are untouched.'),
    'workflow_digest': (
        'The meshing workflow changed, so saved task progress has been reset. '
        'Meshes and reports are untouched.'),
    'unreadable': (
        'Saved workflow progress could not be read and has been reset. '
        'Meshes and reports are untouched.'),
    'inconsistent_state': (
        'Saved workflow progress was inconsistent with the current workflow '
        'and has been reset. Meshes and reports are untouched.'),
}


class EngineTaskStateStore:
    """Load, transition, and atomically persist one engine's task states."""

    def __init__(self, case_path: str | Path, descriptor: WorkflowDescriptor):
        self.descriptor = descriptor
        self.path = (Path(case_path) / 'foammesh' / 'workflow' /
                     f'{descriptor.engine_id}-tasks.json')

    # -- persistence ------------------------------------------------------- #

    def load(self) -> EngineWorkflowGraph:
        """The graph alone, for callers that do not surface the reset."""
        return self.load_result().graph

    def load_result(self) -> TaskStateLoadResult:
        """Load, and say why the file on disk was not used if it was not.

        The checks run **schema first**. ``load`` compared only the digest and
        returned immediately on a mismatch, so a file written by an older build
        could never reach a schema check placed after it -- and ``SCHEMA_VERSION``
        was written by ``save`` and read nowhere at all.
        """
        current = self._contract()
        fresh = EngineWorkflowGraph(self.descriptor)
        if not self.path.is_file():
            return TaskStateLoadResult(fresh, current=current)
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return TaskStateLoadResult(fresh, 'unreadable', current=current)
        if not isinstance(document, dict):
            return TaskStateLoadResult(fresh, 'unreadable', current=current)

        prior = {
            'schema_version': document.get('schema_version'),
            'workflow_version': document.get('workflow_version'),
            'workflow_digest': document.get('workflow_digest'),
        }
        if prior['schema_version'] not in SUPPORTED_SCHEMA_VERSIONS:
            return TaskStateLoadResult(fresh, 'schema_version', prior, current)
        if (prior['workflow_version'] is not None
                and prior['workflow_version'] != self.descriptor.version):
            return TaskStateLoadResult(
                fresh, 'workflow_version', prior, current)
        # A descriptor change invalidates persisted states: stale task IDs or
        # reordered dependencies must not resurrect acceptance.
        if prior['workflow_digest'] != self.descriptor.digest:
            return TaskStateLoadResult(fresh, 'workflow_digest', prior, current)
        try:
            fresh.load(document)
        except (KeyError, ValueError) as error:
            return TaskStateLoadResult(
                EngineWorkflowGraph(self.descriptor), 'inconsistent_state',
                prior, current, str(error))
        return TaskStateLoadResult(fresh, prior=prior, current=current)

    def _contract(self) -> dict:
        return {
            'schema_version': SCHEMA_VERSION,
            'workflow_version': self.descriptor.version,
            'workflow_digest': self.descriptor.digest,
        }

    def evidence(self) -> dict:
        """Per-task evidence, in a map parallel to the task states.

        Deliberately *beside* ``tasks`` rather than nested inside it:
        ``EngineWorkflowGraph.load`` reads ``tasks`` as ``{id: state-string}``
        and calls ``TaskState(value)`` on each, so nesting a record there would
        make every v2 file unreadable by the graph that owns the states.
        """
        if not self.path.is_file():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        stored = document.get('evidence')
        return dict(stored) if isinstance(stored, dict) else {}

    def save(self, graph: EngineWorkflowGraph, *,
             evidence: dict | None = None) -> dict:
        retained = self.evidence()
        for task_id, record in dict(evidence or {}).items():
            retained[task_id] = dict(record)
        # Evidence for a task that no longer holds a result is not evidence.
        live = {
            task_id: record for task_id, record in retained.items()
            if task_id in graph._state and graph.state(task_id) in _SETTLED}
        document = dict(graph.to_dict(), schema_version=SCHEMA_VERSION,
                        evidence=live)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        # Plan 35 CR9: the bytes are on disk before the rename publishes them,
        # so a crash leaves the old file or the new one, never an empty one.
        with open(temporary, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(document, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        return document

    # -- transitions ------------------------------------------------------- #

    def apply(self, task_id: str, transition: str) -> dict:
        """Apply one named §5.17 transition and persist the result."""
        loaded = self.load_result()
        graph = loaded.graph
        self.descriptor.task(task_id)  # raises for unknown IDs
        if transition == 'accept':
            if self.descriptor.task(task_id).run_gated:
                raise TaskStateError(
                    f'{task_id} is accepted by a successful run result, '
                    'not a manual update')
            state = graph.state(task_id).value
            if state not in ('configured',):
                graph.configure(task_id)
            change = graph.finish(task_id)
        elif transition == 'configure':
            change = graph.configure(task_id)
        elif transition == 'skip':
            change = graph.skip(task_id)
        elif transition == 'revert':
            change = graph.revert_and_edit(task_id)
        elif transition == 'fail':
            graph.configure(task_id)
            graph.start(task_id)
            change = graph.fail(task_id)
        else:
            raise TaskStateError(f'unknown task transition: {transition}')
        document = self.save(graph)
        return {'transition': change.to_dict(), 'tasks': document['tasks'],
                'workflow_reset_notice': loaded.notice()}

    def record_stage_success(self, task_id: str, *,
                             warning: bool = False, reasons=()) -> dict:
        """Advance exactly the one task a stage run performed.

        Plan 23 §8.4. A stage is evidence for itself and nothing else: a
        successful ``snap`` says nothing about whether layers were added, and
        with a gate now sitting between them, advancing the chain would pass a
        gate on the strength of the run it is supposed to judge.
        """
        self.descriptor.task(task_id)
        return self._record(
            [task_id], warning=warning, evidence={'kind': 'stage'},
            reasons=reasons)

    def stage_chain(self, task_id: str) -> list[str]:
        """The tasks a successful run of ``task_id``'s stage physically implies.

        A stage cannot succeed unless the stages before it ran and the
        manual configuration they consumed existed, so those are part of the
        evidence. The walk follows ``depends_on`` only through tasks that
        carry an engine stage and stops at every check task (run-gated,
        stage-less): a check is a gate, and a stage running behind it is not
        proof the gate was judged. The gate is left to block, and the caller
        reports it. Manual tasks are included only when reached along a
        stage path, so a readiness confirmation that hangs off a gate is
        never accepted by implication.
        """
        self.descriptor.task(task_id)
        wanted: set[str] = set()
        pending = [task_id]
        while pending:
            current = pending.pop()
            if current in wanted:
                continue
            task = self.descriptor.task(current)
            is_root = current == task_id
            is_check = task.run_gated and not task.engine_stage
            if is_check and not is_root:
                continue
            wanted.add(current)
            # DP-35. The walk used to continue only through tasks that carry an
            # engine stage, which stops one hop into a manual task. That fits
            # snappy, where each stage's manual inputs hang directly off a
            # stage. It does not fit gmsh, which runs once behind a chain of
            # seven manual tasks: `stage_chain('gmsh.compute')` came back as
            # `['gmsh.periodic', 'gmsh.compute']`, periodic was locked too, and
            # so a mesh that was built, checked and published advanced nothing
            # at all. Configuration a run consumed is part of what the run
            # proves however many of them are stacked; a check is still a gate
            # and still ends the walk, so no gate is crossed by implication.
            pending.extend(task.depends_on)
        return [task.task_id for task in self.descriptor.ordered_tasks()
                if task.task_id in wanted]

    def record_stage_chain_success(self, task_id: str, *,
                                   warning: bool = False,
                                   reasons=()) -> dict:
        """Advance a stage task together with the chain its success proves.

        Used by ``workflow.run_stage``, which the legacy step pages call:
        a user who ran snap from the Snap page never visited the tree, so
        this is the only place the tree can learn that the base grid,
        surface features and castellation preceded it. Gates are not
        crossed; see :meth:`stage_chain`.
        """
        chain = self.stage_chain(task_id)
        # A warning belongs to the stage that raised it; the stages it proves
        # ran before it are recorded as the plain passes they were.
        #
        # ``performed`` is the same distinction applied to skips: this run
        # performed exactly one stage and merely implies the rest, so a stage
        # the user deliberately skipped is not un-skipped because the stage
        # after it succeeded. Layers on the record of a mesh that has none is
        # the mirror image of R184 and no better.
        return self._record(
            chain, warning=warning, warning_for={task_id},
            performed={task_id}, reasons=reasons,
            evidence={'kind': 'stage', 'stage_task': task_id})

    def record_atomic_run_success(self, task_ids, *,
                                  warning: bool = False,
                                  waived=(), configured=(),
                                  warning_for=None, reasons=()) -> dict:
        """Advance the tasks one atomic engine run actually performed.

        Some engines do several tasks in one invocation -- ``mesh.gmsh.run``
        computes, publishes and checks in a single process -- so the caller
        names what its run covered. It is still bounded: a task the run did not
        perform is not advanced because it happened to declare a stage.

        ``waived`` names the tasks whose own gate failed and which a human
        accepted anyway. R119/R158: without it, a run published through
        **Accept anyway** recorded ``PASSED`` on the very task whose gate had
        just refused the mesh, so the outline painted the plain ``COMPLETED``
        tick over a recorded override and the ``WAIVED`` glyph the app already
        defines was never drawn for the one case it exists for.

        ``configured`` names the optional tasks this run's configuration asked
        for (DP-228). The graph cannot answer that question: a page save that
        was refused as locked leaves its task READY, and DP-35 grades a READY
        optional task as unused. The job the runner consumed can answer it, so
        that is what the caller reads and hands here.

        ``warning_for`` names the tasks the run's warning is about (DP-817).
        It defaults to every task the run performed, which is right only when
        the warning really is about all of them: a checkMesh verdict judges
        the finished mesh and belongs to the QA task, not to the surface
        features, castellation, snap and layers stages that ran before it.
        """
        performed = set(task_ids)
        known = {task.task_id for task in self.descriptor.ordered_tasks()}
        # A configuration naming a task this engine does not have is dropped
        # rather than raised: it is derived from a job document, and a job that
        # named one must not take down a run that produced a mesh.
        configured = frozenset(configured) & known
        unknown = sorted(performed - known)
        if unknown:
            raise TaskStateError(
                f'atomic run claims tasks this engine does not have: {unknown}')
        # DP-35. The run's own tasks, plus the configuration each of them
        # physically consumed. Naming only the covered tasks was correct about
        # what ran and useless in practice on an engine that runs once: the
        # first of them sat behind seven manual tasks nothing else accepts, so
        # the recorder was blocked at its first step and a mesh that had been
        # built, checked and published moved the outline not at all. `performed`
        # keeps the distinction that matters -- these three were done, the rest
        # are implied -- so an optional step nobody used is still recorded as
        # unused rather than as a pass.
        wanted: set[str] = set()
        for task_id in performed:
            wanted.update(self.stage_chain(task_id))
        ordered = [task.task_id for task in self.descriptor.ordered_tasks()
                   if task.task_id in wanted]
        return self._record(
            ordered, warning=warning,
            warning_for=(performed if warning_for is None
                         else set(warning_for) & performed),
            performed=performed, evidence={'kind': 'atomic_run'},
            reasons=reasons,
            configured=configured, waived=frozenset(waived))

    def record_check_result(self, task_id: str, *, evidence: dict,
                            warning: bool = False) -> dict:
        """Record one stage-less report task from a validated report.

        The only way a run-gated task with no ``engine_stage`` can be accepted:
        a manual ``accept`` is refused, and the stage recorders never reach it.
        ``evidence`` carries the report fingerprints the caller already
        validated, so what advanced the task is recoverable afterwards.
        """
        task = self.descriptor.task(task_id)
        if task.engine_stage:
            raise TaskStateError(
                f'{task_id} is an engine stage; use record_stage_success')
        if not evidence:
            raise TaskStateError(
                f'{task_id} cannot be completed without report evidence')
        return self._record([task_id], warning=warning,
                            evidence={'kind': 'check', **dict(evidence)},
                            complete=True)

    def _record(self, task_ids, *, warning: bool, evidence: dict,
                complete: bool = False, warning_for=None, performed=None,
                configured=frozenset(), waived=frozenset(),
                reasons=()) -> dict:
        """Advance the named tasks, stopping cleanly and keeping what advanced.

        The previous recorder raised into a caller that swallowed the exception
        *and* discarded the graph, because it saved only after the loop. A run
        that hit an unmet prerequisite therefore advanced nothing at all and
        said nothing about it. Here the first task that cannot advance ends the
        run, and everything before it is persisted with a stated reason.

        ``performed`` names the tasks this run actually carried out, as opposed
        to the ones it merely implies ran. It defaults to all of them, which is
        true of every caller but the chain recorder. Its only effect is on
        ``SKIPPED``: a skip says nothing ran, so a run that performed the task
        replaces it, while a run that only implies it leaves the skip standing.

        ``configured`` names the optional tasks whose configuration this run
        consumed, read back from the job the runner was given. An optional task
        in it was used however the graph reads, so it is recorded as the pass
        it was rather than as the skip DP-35 writes for a task nobody
        configured. It is not the same claim as ``performed``: a warning still
        belongs to the stage that raised it, so a task named here and nowhere
        else is recorded PASSED and not WARNING.

        DP-817. A task this run performed that already holds a run result
        (PASSED or WARNING) has that result replaced by this run's. It used to
        be filed as ``already recorded``, so a WARNING outlived every clean
        rerun of the stage that raised it and the outline kept a warning
        nothing on disk could explain. A task the run merely implies keeps
        its result: nothing about it was measured again.

        ``reasons`` are the warning texts, kept in the evidence of each task
        this run leaves at WARNING so the page can say what the warning was.
        """
        reasons = [str(text) for text in (reasons or ()) if str(text).strip()]
        performed = set(task_ids) if performed is None else set(performed)
        configured = frozenset(configured)
        loaded = self.load_result()
        graph = loaded.graph
        advanced: list[str] = []
        warned: set[str] = set()
        skipped: list[dict] = []
        blocked: dict | None = None

        for task_id in task_ids:
            task = self.descriptor.task(task_id)
            state = graph.state(task_id)
            warns = warning and (warning_for is None or task_id in warning_for)
            rerun = (state in {TaskState.PASSED, TaskState.WARNING}
                     and task_id in performed and task_id not in waived
                     and not complete
                     and not (state is TaskState.PASSED and not warns))
            if rerun:
                graph.restate(task_id, warning=warns)
                advanced.append(task_id)
                if warns:
                    warned.add(task_id)
                continue
            # A waived task is never "already recorded": the previous run left
            # it PASSED, which is exactly the record R119/R158 says is wrong.
            # Nor is a skipped task this run performed (R184): the skip is the
            # record that nothing ran, and something just did.
            ran_after_skip = (state is TaskState.SKIPPED
                              and (task_id in performed
                                   or task_id in configured))
            if state in _SETTLED and task_id not in waived and not ran_after_skip:
                skipped.append({'task_id': task_id, 'reason': 'already recorded',
                                'state': state.value})
                continue
            # DP-228. "Unused" is a fact about the configuration the run
            # consumed, not about the graph. MEASURED on the
            # `workflow-ux-20260915` audit: the Boundary layers page was saved
            # with three layers, the save was refused as locked by
            # prerequisites and reported as `Settings saved`, the run grew
            # 7,914 prisms, and this graded the task SKIPPED because it was
            # still READY. A task whose settings the run consumed was used,
            # whatever the page save left behind.
            unused = (state in {TaskState.READY, TaskState.LOCKED}
                      and task_id not in configured)
            if task.cardinality is TaskCardinality.REPEATABLE and unused:
                # An unconfigured repeatable task was explicitly not used.
                graph.skip(task_id)
                advanced.append(task_id)
                continue
            if (task.cardinality is TaskCardinality.OPTIONAL and unused
                    and task_id not in performed):
                # DP-35. The same rule, for the same reason. An optional task
                # the run did not perform and nobody configured was not done,
                # and recording it as PASSED would write "Boundary Layers:
                # passed" onto every mesh that has no boundary layers -- a
                # worse record than the LOCKED it replaces, not a better one.
                # An optional task the user *did* configure is CONFIGURED here,
                # so it falls through and is recorded as the pass it was.
                graph.skip(task_id)
                advanced.append(task_id)
                continue
            try:
                if task_id in waived:
                    # R119/R158. The gate refused and a human overrode it, so
                    # the run is recorded as what it was: a failure somebody
                    # decided to ship. Driven through FAILED rather than
                    # jumping to WAIVED, because `waive` refuses any other
                    # origin -- a waiver over a pass is not a decision.
                    if state in _SETTLED:
                        graph.revert_and_edit(task_id)
                    if state is not TaskState.CONFIGURED:
                        graph.configure(task_id)
                    graph.start(task_id)
                    graph.fail(task_id)
                    graph.waive(task_id)
                elif complete:
                    # R180. `complete` refuses to run from EDITING, and a
                    # check task reaches EDITING the moment anyone presses
                    # Revert and edit on it. From there it is a dead end: a
                    # run-gated task refuses `accept`, and the run that should
                    # settle it rewrote its report and was then blocked here
                    # -- silently, because `_record` reports a block in its
                    # return value and every caller looked only at the state.
                    # MEASURED on tee_snappy_v2: snappy.fidelity_snap sat at
                    # `editing` with no recorded evidence and a current
                    # report on disk, so Layers, QA, Fidelity, Resolution,
                    # Summary and Export were all locked behind a gate that
                    # had in fact been run. Settings entered against a task a
                    # run must judge are CONFIGURED (R134), which is exactly
                    # the state `complete` accepts, so that is the step taken
                    # before it.
                    if state not in COMPLETABLE:
                        graph.configure(task_id)
                    graph.complete(task_id, verdict=str(
                        evidence.get('verdict') or ''))
                else:
                    if state is not TaskState.CONFIGURED:
                        graph.configure(task_id)
                    graph.finish(task_id, warning=warns)
                    if warns:
                        warned.add(task_id)
            except ValueError as error:
                blocked = {'task_id': task_id, 'reason': str(error),
                           'state': state.value}
                break
            advanced.append(task_id)

        document = self.save(graph, evidence={
            task_id: (dict(evidence, warnings=list(reasons))
                      if task_id in warned and reasons else dict(evidence))
            for task_id in advanced})
        return {'tasks': document['tasks'], 'advanced': advanced,
                'skipped': skipped, 'blocked': blocked,
                'evidence': dict(evidence),
                'stored_evidence': document['evidence'],
                'workflow_reset_notice': loaded.notice()}

    def snapshot(self) -> dict:
        loaded = self.load_result()
        graph = loaded.graph
        return dict(graph.to_dict(), schema_version=SCHEMA_VERSION,
                    next_runnable=graph.next_runnable(),
                    evidence=self.evidence(),
                    workflow_reset_notice=loaded.notice())
