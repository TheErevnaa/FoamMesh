"""The window's side of ``mesh.section`` (Plan 37 UF10): one job per case.

No Qt: the section tool drives this and draws what it hands back.

* **At most one section worker per case**, and **one replaceable newest
  request** waiting behind it. Moving a plane while a section is computed
  replaces the waiting request; it neither starts a second worker nor
  cancels the running one (its answer is still the nearest thing to what is
  wanted, and is shown only if it is still wanted -- see below).
* **A completion is shown only if its key is the wanted key**: (case id,
  mesh revision, plane generation, mode, arrays). The window says what it
  wants with :meth:`SectionJobs.want`; an answer for anything else is
  dropped and its files removed. The mesh revision in the key is the one
  the worker read, so a mesh rewritten underneath is never shown as current.
* **Cancel** (case close, unlock, remesh, plane delete): the running worker
  is stopped, the waiting request dropped and the job's scratch removed.
  Nothing that finishes afterwards is delivered.

The runner is injectable: the default one admits the job through the
resource budget and runs it with :func:`local_worker.run_worker`.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from foammesh.core.section import notices

logger = logging.getLogger(__name__)

OPERATION = 'mesh.section'
#: UF11 Compare: several stage sections cut in one worker process.
BATCH_OPERATION = 'mesh.section_batch'

#: What the section tool shows while a job runs and when its answer is on
#: screen (the plan's words, `core.section.notices`).
RUNNING_NOTICE = notices.COMPUTING
DONE_NOTICE = notices.EXACT

_jobNumbers = itertools.count(1)


@dataclass(frozen=True)
class SectionRequest:
    """What the window asks the worker for."""

    case_dir: str
    case_id: str
    generation: int
    mode: str
    planes: tuple              # SectionPlaneState dicts, in order
    active: int
    arrays: tuple = ()
    mesh_revision: str | None = None
    budgets: dict = field(default_factory=dict)
    tolerance: float | None = None
    #: UF11. A frozen layer: runs ``((first, last), ...)`` of the cells to
    #: draw whole, wherever the planes are (``None``: a section).
    cells: tuple | None = None

    def key(self, revision=None) -> tuple:
        return section_key(self.case_id, revision or self.mesh_revision,
                           self.generation, self.mode, self.arrays)

    def args(self, out_dir: Path, job: str, cancel_path: Path) -> dict:
        args = {'case': str(self.case_dir), 'case_id': self.case_id,
                'generation': self.generation, 'mode': self.mode,
                'planes': [dict(p) for p in self.planes],
                'active': int(self.active), 'arrays': list(self.arrays),
                'out_dir': str(out_dir), 'job': job,
                'cancel_path': str(cancel_path),
                'budgets': dict(self.budgets)}
        if self.mesh_revision:
            args['mesh_revision'] = self.mesh_revision
        if self.tolerance:
            args['tolerance'] = self.tolerance
        if self.cells is not None:
            args['cells'] = [list(run) for run in self.cells]
        return args


def section_key(case_id, revision, generation, mode, arrays) -> tuple:
    return (case_id, revision, int(generation), str(mode),
            tuple(sorted(arrays or ())))


def is_current(key, wanted) -> bool:
    """Whether an answer with *key* is the section *wanted* shows.

    A refusal carries the request's key, with no revision read; and a window
    that did not know the revision (``None``) takes any.
    """
    return (key is not None and wanted is not None and key[0] == wanted[0]
            and key[2:] == wanted[2:]
            and (wanted[1] is None or key[1] in (None, wanted[1])))


def manifest_key(manifest: dict) -> tuple | None:
    key = (manifest or {}).get('key')
    if not isinstance(key, dict):
        return None
    return section_key(key.get('case_id'), key.get('mesh_revision'),
                       key.get('generation') or 0, key.get('mode'),
                       key.get('arrays'))


@dataclass
class SectionCompletion:
    """What a finished job hands the window."""

    request: SectionRequest
    #: ``'ok'`` / ``'empty'`` (manifest in hand) or the worker outcome's
    #: status: ``'failed'``, ``'over_budget'``, ``'crashed'``, ...
    status: str
    manifest: dict | None = None
    reason: str = ''
    message: str = ''
    details: dict = field(default_factory=dict)

    @property
    def retainsPrevious(self) -> bool:
        """The window keeps the section it shows (every refusal does)."""
        return self.manifest is None


@dataclass
class _Running:
    request: SectionRequest
    task: asyncio.Task
    job_dir: Path
    cancel_path: Path


#: The resource-budget allowance of a section coloured by a quality field.
QUALITY_ALLOWANCE = 'mesh.section.quality'


def allowance(args: dict) -> str:
    """The ``PEAK_FACTORS`` entry a section job is admitted with: the
    quality one when any requested colour is a quality field (the worker
    then computes the quality of every cell), the plain one otherwise."""
    if any(str(name).startswith('quality.')
           for name in args.get('arrays') or ()):
        return QUALITY_ALLOWANCE
    return OPERATION


async def default_runner(request: SectionRequest, args: dict):
    """Admit through the resource budget, then run the worker.

    Returns a :class:`~foammesh.core.jobs.local_worker.WorkerOutcome`-like
    object (``ok``, ``status``, ``payload``, ``reason``, ``message``,
    ``details``).
    """
    from foammesh.core.jobs import local_worker
    from foammesh.core.mesh import mesh_preview
    from foammesh.support import resource_budget as budget

    counts = await asyncio.to_thread(mesh_preview.case_counts,
                                     request.case_dir)
    # 600 bytes a cell: the top of the measured range (resource_budget); a
    # quality colour adds what computing the quality fields measured.
    estimate = budget.estimate_peak_bytes(allowance(args), counts)
    try:
        grant = await budget.controller().admit(
            OPERATION, estimate, priority=budget.PRIORITY_PREVIEW)
    except budget.OverBudget as refusal:
        return _Refused(
            'over_budget',
            'The section was not computed: it needs about {0} and {1} is '
            'free.'.format(budget.format_bytes(refusal.estimate.peak_bytes),
                           budget.format_bytes(refusal.snapshot.budget)),
            {'stage': 'admission', 'retained_previous': True})
    async with grant:
        return await local_worker.run_worker(
            OPERATION, args, cap_bytes=grant.cap_bytes,
            group=str(request.case_dir))


async def default_batch_runner(pairs) -> list:
    """Run ``[(request, args)]`` as ONE worker job (UF11 Compare).

    Returns one outcome per pair, in order (``ok``, ``status``, ``payload``,
    ``reason``, ``message``, ``details``, ``seconds``). The stages are cut
    one after another inside the worker, so the job is admitted once, at the
    largest of the stages' estimates, and holds the one heavy section slot;
    starting one process instead of one per stage is most of the saving.
    When the job as a whole is refused or fails, every stage gets that
    answer.
    """
    from foammesh.core.jobs import local_worker
    from foammesh.core.mesh import mesh_preview
    from foammesh.support import resource_budget as budget

    pairs = list(pairs)
    if not pairs:
        return []
    estimates = []
    for request, args in pairs:
        counts = await asyncio.to_thread(mesh_preview.case_counts,
                                         request.case_dir)
        estimates.append(budget.estimate_peak_bytes(allowance(args), counts))
    estimate = max(estimates, key=lambda item: item.peak_bytes)
    try:
        grant = await budget.controller().admit(
            OPERATION, estimate, priority=budget.PRIORITY_PREVIEW)
    except budget.OverBudget as refusal:
        return [_Refused(
            'over_budget',
            'The section was not computed: it needs about {0} and {1} is '
            'free.'.format(budget.format_bytes(refusal.estimate.peak_bytes),
                           budget.format_bytes(refusal.snapshot.budget)),
            {'stage': 'admission', 'retained_previous': True})] * len(pairs)
    async with grant:
        outcome = await local_worker.run_worker(
            BATCH_OPERATION, {'jobs': [args for _request, args in pairs]},
            cap_bytes=grant.cap_bytes, group=str(pairs[0][0].case_dir))
    return batch_outcomes(outcome, len(pairs))


def batch_outcomes(outcome, count: int) -> list:
    """Split a ``mesh.section_batch`` outcome into one per stage."""
    payload = getattr(outcome, 'payload', None) or {}
    stages = payload.get('stages') if getattr(outcome, 'ok', False) else None
    if not isinstance(stages, list) or len(stages) != count:
        if getattr(outcome, 'ok', False):
            outcome = _Refused('failed', 'the worker answered for '
                               f'{len(stages or [])} of {count} stages', {})
        return [outcome] * count
    answers = []
    for entry in stages:
        if entry.get('ok'):
            answers.append(StageOutcome(
                True, 'ok', dict(entry.get('manifest') or {}),
                seconds=float(entry.get('seconds') or 0.0)))
        else:
            answers.append(StageOutcome(
                False, str(entry.get('status') or 'failed'), None,
                reason=str(entry.get('reason') or ''),
                message=str(entry.get('message') or ''),
                details=dict(entry.get('details') or {}),
                seconds=float(entry.get('seconds') or 0.0)))
    return answers


@dataclass
class StageOutcome:
    """One stage's answer from a batch job (``WorkerOutcome``-like)."""

    ok: bool
    status: str
    payload: dict | None
    reason: str = ''
    message: str = ''
    details: dict = field(default_factory=dict)
    seconds: float = 0.0


@dataclass
class _Refused:
    reason: str
    message: str
    details: dict
    ok: bool = False
    payload: dict | None = None

    @property
    def status(self):
        return self.reason


class SectionJobs:
    """One section worker per case plus one waiting newest request."""

    def __init__(self, deliver, *, runner=None, scratch_root=None):
        #: ``deliver(completion)`` -- called on the event loop, only for
        #: completions whose key is the wanted key.
        self._deliver = deliver
        self._runner = runner or default_runner
        self._scratch = Path(scratch_root) if scratch_root else Path(
            tempfile.gettempdir()) / 'foammesh-section'
        self._running: dict[str, _Running] = {}
        self._pending: dict[str, SectionRequest] = {}
        self._wanted: dict[str, tuple] = {}
        #: Keys of completions dropped as stale, newest last (for tests and
        #: the log).
        self.dropped: list[tuple] = []

    # -- what the window wants ---------------------------------------------- #

    def want(self, case_id: str, key: tuple | None) -> None:
        """The section the window would show now (``None``: nothing)."""
        if key is None:
            self._wanted.pop(case_id, None)
        else:
            self._wanted[case_id] = key

    def wanted(self, case_id: str):
        return self._wanted.get(case_id)

    def isRunning(self, case_id: str) -> bool:
        return case_id in self._running

    def pending(self, case_id: str) -> SectionRequest | None:
        return self._pending.get(case_id)

    def notice(self, case_id: str) -> str | None:
        """``Computing section`` while a job for the case runs."""
        return RUNNING_NOTICE if case_id in self._running else None

    # -- submitting ----------------------------------------------------------- #

    def submit(self, request: SectionRequest) -> str:
        """``'started'`` or ``'queued'`` (replacing any waiting request)."""
        case = request.case_id
        self.want(case, request.key())
        if case in self._running:
            self._pending[case] = request
            return 'queued'
        self._start(request)
        return 'started'

    def _start(self, request: SectionRequest) -> None:
        case_dir = self._scratch / _safe(request.case_id)
        case_dir.mkdir(parents=True, exist_ok=True)
        job = f'section-{next(_jobNumbers)}'
        job_dir = case_dir / job
        cancel_path = case_dir / f'{job}.cancel'
        args = request.args(case_dir, job, cancel_path)
        task = asyncio.ensure_future(self._run(request, args))
        self._running[request.case_id] = _Running(request, task, job_dir,
                                                  cancel_path)

    async def _run(self, request: SectionRequest, args: dict) -> None:
        case = request.case_id
        completion = None
        try:
            outcome = await self._runner(request, args)
            completion = self._completion(request, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as error:                # noqa: BLE001
            logger.exception('section job failed')
            completion = SectionCompletion(request, 'failed',
                                           reason='worker_error',
                                           message=str(error))
        finally:
            running = self._running.get(case)
            mine = running is not None and running.request is request
            if mine:
                del self._running[case]
            if mine and completion is not None:
                self._offer(completion, running)
            if mine:
                waiting = self._pending.pop(case, None)
                if waiting is not None:
                    self._start(waiting)

    @staticmethod
    def _completion(request, outcome) -> SectionCompletion:
        if getattr(outcome, 'ok', False):
            manifest = dict(outcome.payload or {})
            return SectionCompletion(request, manifest.get('status', 'ok'),
                                     manifest=manifest)
        return SectionCompletion(
            request, str(getattr(outcome, 'status', 'failed')),
            reason=str(getattr(outcome, 'reason', '') or ''),
            message=str(getattr(outcome, 'message', '') or ''),
            details=dict(getattr(outcome, 'details', None) or {}))

    def _offer(self, completion: SectionCompletion, running: _Running):
        case = completion.request.case_id
        wanted = self._wanted.get(case)
        if completion.manifest is not None:
            key = manifest_key(completion.manifest)
        else:
            key = completion.request.key()
        if not is_current(key, wanted):
            self.dropped.append(key)
            _remove(running.job_dir)
            return
        self._deliver(completion)

    # -- cancelling ----------------------------------------------------------- #

    def cancel(self, case_id: str, reason: str = '') -> bool:
        """Stop the case's worker, drop its waiting request and its scratch.

        For case close, unlock, remesh and plane delete. Returns whether a
        job was running.
        """
        self._pending.pop(case_id, None)
        self._wanted.pop(case_id, None)
        running = self._running.pop(case_id, None)
        if running is None:
            return False
        logger.info('section job for %s cancelled: %s', case_id, reason)
        try:
            running.cancel_path.parent.mkdir(parents=True, exist_ok=True)
            running.cancel_path.touch()
        except OSError:
            pass
        running.task.cancel()

        def scrub(task=None, job_dir=running.job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
            shutil.rmtree(job_dir.with_name(job_dir.name + '.part'),
                          ignore_errors=True)
            if task is not None:          # the worker is gone: the flag too
                _remove(job_dir)

        scrub()
        # The worker is killed inside the task's cancellation; scrub again
        # once it is gone, in case it published between the two.
        running.task.add_done_callback(scrub)
        return True

    def close(self, case_id: str) -> None:
        """The case closed: cancel, then remove all of its section scratch."""
        self.cancel(case_id, 'case closed')
        _remove(self._scratch / _safe(case_id))

    def release(self, completion: SectionCompletion) -> None:
        """The window has drawn (or dropped) a completion: remove its files."""
        manifest = completion.manifest or {}
        directory = manifest.get('directory')
        if directory:
            _remove(Path(directory))


def _safe(name: str) -> str:
    return ''.join(c if c.isalnum() or c in '-_.' else '_'
                   for c in str(name)) or 'case'


def _remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    for suffix in ('.cancel',):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass
