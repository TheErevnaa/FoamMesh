"""Compare one section across the kept meshing stages (Plan 37 UF11).

No Qt. The same world-space planes, mode and arrays are cut through the
**immutable stage snapshots** that `core.jobs.stage_snapshots` keeps under
``<case>/foammesh/stages/rNNNN/<stage>/`` -- each snapshot folder is a case
(``constant/polyMesh`` + ``case.foam``) the section worker reads as it is.

* Every result is labelled with the stage and the revision that holds it,
  plus the snapshot's digest: the provenance comes from the snapshot
  manifest, never from the live mesh.
* A stage with no kept snapshot, or one whose files no longer match their
  manifest, is **unavailable with the reason** -- it is never replaced by the
  latest mesh. A skip recorded when the snapshot was refused (quota, disk
  reserve, decomposed) gives that reason and its consequence.
* Equal cell counts between stages are legitimate (a snap moves points, not
  cells); the comparison is of geometry, not counts.
* One legend for all stages: :func:`shared_legend` maps the values of every
  stage together -- one scalar range, or the union of the categories.

All the available stages are cut in ONE worker job
(`section_jobs.default_batch_runner`: ``mesh.section_batch``), admitted once
through the resource budget at the largest stage's estimate. Inside it the
stages are cut one after the other, so a compare still never holds more
than one section's memory, and it starts one process instead of one per
stage (the start-up was most of a compare's time). A stage the worker
refuses is that stage's answer; the others are still drawn. A caller may
hand in a per-stage ``runner`` instead (``runner(request, args)``, one call
a stage, as the tests do) or another ``batch_runner``.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from foammesh.core.jobs import stage_snapshots as snapshots
from foammesh.core.section import section_colour
from foammesh.core.section.section_jobs import SectionRequest

__all__ = ['CompareTarget', 'StageSection', 'CompareResult',
           'compare_targets', 'compare_requests', 'run_compare',
           'shared_legend', 'STAGES']

#: The stages a snappy run keeps, in order.
STAGES = tuple(snapshots.STAGE_ORDER)

#: The prefix of a compare job's case id (never the live case's id, so a
#: compare can never be taken for, or cancel, the live section).
CASE_ID_PREFIX = 'compare'


@dataclass(frozen=True)
class CompareTarget:
    """One stage to cut: where its snapshot is, or why it cannot be cut."""

    stage: str
    revision: str | None = None
    path: str | None = None           # the snapshot folder (a case)
    digest: str | None = None         # the snapshot manifest's files digest
    mesh_identity: str | None = None  # the live identity it was taken from
    captured_at: str | None = None
    input: dict | None = None         # {stage, revision, digest} it came from
    reason: str = ''

    @property
    def available(self) -> bool:
        return self.path is not None and not self.reason

    @property
    def label(self) -> str:
        if self.available:
            return f'{self.stage} · {self.revision}'
        return f'{self.stage} · unavailable'

    @property
    def case_id(self) -> str:
        return f'{CASE_ID_PREFIX}:{self.stage}:{self.revision or "-"}'

    def to_dict(self) -> dict:
        return {'stage': self.stage, 'revision': self.revision,
                'path': self.path, 'digest': self.digest,
                'mesh_identity': self.mesh_identity,
                'captured_at': self.captured_at, 'input': self.input,
                'reason': self.reason, 'label': self.label}


def _missing_reason(case, stage, revision) -> str:
    """Why *stage* has no snapshot: the recorded skip, else plainly."""
    for entry in reversed(snapshots.skipped(case)):
        if entry.get('stage') != stage:
            continue
        why = {'quota': 'the stage snapshot quota was full',
               'free_space': 'the disk was down to its free-space reserve',
               'decomposed': 'the mesh was decomposed when the stage ended',
               }.get(str(entry.get('reason')), str(entry.get('reason') or ''))
        text = f'no {stage} snapshot was kept: {why}'
        if entry.get('consequence'):
            text += f' ({entry["consequence"]})'
        return text + '.'
    where = f' for revision {revision}' if revision else ''
    return f'no {stage} snapshot is kept{where}.'


def compare_targets(case_path, stages=STAGES, revision: str | None = None, *,
                    verify: bool = True, cancel=None) -> list[CompareTarget]:
    """Where each of *stages* is kept for *revision* (the newest by default).

    With *verify* every file is re-hashed against its manifest (the snapshot
    is the evidence; a changed one is refused, not cut).
    """
    case = Path(case_path)
    resolved = snapshots.resolve(case, revision)
    targets = []
    for stage in stages:
        holder = resolved.get(stage)
        manifest = snapshots.read_manifest(case, stage, holder) \
            if holder else None
        folder = snapshots.stage_mesh_path(case, stage, holder) \
            if holder else None
        if not holder or manifest is None or folder is None:
            targets.append(CompareTarget(
                stage, holder, reason=_missing_reason(case, stage, revision)))
            continue
        fields = dict(revision=holder, digest=manifest.get('digest'),
                      mesh_identity=manifest.get('mesh_identity'),
                      captured_at=manifest.get('captured_at'),
                      input=manifest.get('input'))
        if verify:
            check = snapshots.verify(case, stage, holder, cancel=cancel)
            if not check.get('ok'):
                broken = sorted(check.get('mismatched', [])
                                + check.get('missing', []))
                targets.append(CompareTarget(
                    stage, reason=(
                        f'the {stage} snapshot in {holder} cannot be '
                        'verified: ' + (', '.join(broken) or 'no manifest')
                        + ' no longer match what was kept.'), **fields))
                continue
        targets.append(CompareTarget(
            stage, path=str(Path(folder).parent.parent), **fields))
    return targets


def compare_requests(targets, *, planes, mode, arrays=(), active=0,
                     generation=0, budgets=None) -> list:
    """``[(target, SectionRequest | None)]``: the same planes for each."""
    planes = tuple(dict(plane) for plane in planes)
    requests = []
    for target in targets:
        if not target.available:
            requests.append((target, None))
            continue
        requests.append((target, SectionRequest(
            case_dir=target.path, case_id=target.case_id,
            generation=int(generation), mode=str(getattr(mode, 'value', mode)),
            planes=planes, active=int(active), arrays=tuple(arrays),
            budgets=dict(budgets or {}))))
    return requests


@dataclass
class StageSection:
    """One stage's section, or why there is none."""

    target: CompareTarget
    status: str                      # ok | empty | unavailable | <refusal>
    manifest: dict | None = None
    reason: str = ''
    message: str = ''
    seconds: float = 0.0

    @property
    def label(self) -> str:
        return self.target.label

    @property
    def surface(self) -> str | None:
        files = (self.manifest or {}).get('files') or {}
        return (files.get('surface') or {}).get('path')

    @property
    def cells(self) -> str | None:
        files = (self.manifest or {}).get('files') or {}
        return (files.get('cells') or {}).get('path')

    @property
    def shown(self) -> bool:
        return self.status == 'ok' and self.surface is not None

    @property
    def note(self) -> str:
        """What the row says: the label, and why when there is nothing."""
        if self.status == 'ok':
            return self.label
        if self.status == 'empty':
            empty = (self.manifest or {}).get('empty') or {}
            return f'{self.label}: {empty.get("message") or "empty"}'
        return f'{self.label}: {self.reason or self.message or self.status}'


@dataclass
class CompareResult:
    sections: list[StageSection] = field(default_factory=list)
    seconds: float = 0.0
    directory: str | None = None

    def release(self) -> None:
        """Remove the workers' files (the window has drawn them)."""
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)


async def run_compare(case_path, stages=STAGES, *, planes, mode, arrays=(),
                      active=0, generation=0, revision=None, runner=None,
                      scratch_root=None, verify=True, budgets=None,
                      clock=time.perf_counter,
                      batch_runner=None) -> CompareResult:
    """Cut every available stage with the same planes.

    By default in one worker job (``batch_runner``); with a per-stage
    ``runner`` one call a stage, one after another. A stage with no kept or
    verifiable snapshot is listed unavailable and never sent to a worker.
    """
    import asyncio

    started = clock()
    targets = await asyncio.to_thread(compare_targets, case_path, stages,
                                      revision, verify=verify)
    root = Path(scratch_root) if scratch_root else Path(
        tempfile.gettempdir()) / 'foammesh-section'
    directory = root / f'compare-{uuid.uuid4().hex[:12]}'
    directory.mkdir(parents=True, exist_ok=True)
    result = CompareResult(directory=str(directory))
    jobs = []
    for index, (target, request) in enumerate(compare_requests(
            targets, planes=planes, mode=mode, arrays=arrays, active=active,
            generation=generation, budgets=budgets)):
        if request is None:
            jobs.append((target, None, None))
            continue
        job = f'{index}-{target.stage}'
        jobs.append((target, request, request.args(
            directory, job, directory / f'{job}.cancel')))

    if runner is not None:
        for target, request, args in jobs:
            if request is None:
                result.sections.append(StageSection(
                    target, 'unavailable', reason=target.reason))
                continue
            began = clock()
            try:
                outcome = await runner(request, args)
            except Exception as error:                 # noqa: BLE001
                result.sections.append(StageSection(
                    target, 'failed', reason=str(error),
                    seconds=clock() - began))
                continue
            result.sections.append(_stage_section(target, outcome,
                                                  clock() - began))
        result.seconds = clock() - started
        return result

    from foammesh.core.section.section_jobs import default_batch_runner

    batch = [(request, args) for _target, request, args in jobs
             if request is not None]
    outcomes: list = []
    failure = None
    began = clock()
    if batch:
        try:
            outcomes = list(await (batch_runner or default_batch_runner)(
                batch))
            if len(outcomes) != len(batch):
                failure = (f'the comparison answered for {len(outcomes)} of '
                           f'{len(batch)} stages')
        except Exception as error:                     # noqa: BLE001
            failure = str(error)
    spent = clock() - began
    answers = iter(outcomes)
    for target, request, _args in jobs:
        if request is None:
            result.sections.append(StageSection(
                target, 'unavailable', reason=target.reason))
        elif failure is not None:
            result.sections.append(StageSection(
                target, 'failed', reason=failure,
                seconds=spent / len(batch)))
        else:
            outcome = next(answers)
            seconds = getattr(outcome, 'seconds', None)
            result.sections.append(_stage_section(
                target, outcome,
                float(seconds) if seconds else spent / len(batch)))
    result.seconds = clock() - started
    return result


def _stage_section(target, outcome, seconds: float) -> StageSection:
    if getattr(outcome, 'ok', False):
        manifest = dict(outcome.payload or {})
        return StageSection(target, str(manifest.get('status', 'ok')),
                            manifest, seconds=seconds)
    return StageSection(
        target, str(getattr(outcome, 'status', 'failed')),
        reason=str(getattr(outcome, 'reason', '') or ''),
        message=str(getattr(outcome, 'message', '') or ''),
        seconds=seconds)


def shared_legend(key, values_per_stage, *, names=None):
    """One mapping for every stage's values (`section_colour.mapping`).

    A scalar gets the range of all stages' finite values together; a
    categorical colour the union of the categories. ``None`` entries (a stage
    with nothing to show) are left out.
    """
    item = section_colour.choice(key)
    if item.key == section_colour.NONE:
        return {'kind': 'none'}
    arrays = [np.asarray(values).ravel() for values in values_per_stage
              if values is not None and len(values)]
    joined = np.concatenate(arrays) if arrays else np.asarray([])
    return section_colour.mapping(key, joined, names=names)
