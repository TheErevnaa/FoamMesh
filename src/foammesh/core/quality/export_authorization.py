"""Whether a mesh may leave this case, and what claim leaves with it.

Plan 23 §8.6. ``common.export depends_on common.summary`` orders the UI and
enforces nothing: today every ``case.export.*`` handler calls its service
without consulting workflow state, so a mesh that failed every gate exports
exactly as readily as one that passed. This module is the check those handlers
were missing, and the classification that decides which of them need it.

**Every outbound path is classified, never inferred from its name.** Four
classes, and the boundaries between them are the whole design:

``ENGINEERING`` -- a solver mesh going to someone who will compute on it.
    Authorization required. These are the paths where an unqualified mesh
    becomes somebody else's input.
``INTERNAL`` -- mutates or republishes *this* case rather than emitting an
    artifact. ``format_convert`` and ``canonical.export.openfoam`` look like
    exports and are not; gating them would block the pipeline from advancing
    itself, and they invalidate the mesh fingerprint anyway.
``DIAGNOSTIC`` -- evidence about a mesh rather than a mesh. Always allowed,
    and stamped with the report fingerprint. Blocking these would remove the
    means of diagnosing the failure that blocked the export.
``TRANSFER`` -- backup and archive. Always allowed, because refusing to let
    someone back up an unqualified case protects nothing. But the manifest
    carries the qualification state, so a restored copy cannot present itself
    as a qualified export.

The registry is exhaustive by test, not by intention: an unclassified
outbound operation is a failure, so adding one forces the decision rather than
defaulting it. Defaulting it either way is wrong -- defaulting to
``ENGINEERING`` blocks diagnostics during an incident, defaulting to exempt
silently reopens the hole this module closes.

**Report-only mode still stamps.** It permits every export, but it records
``qualified: false`` and attaches whatever report fingerprints exist. An
artifact that left during rollout can therefore still be traced to what was
known about it, which is the point of shipping the capability dark rather than
absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping

from .qualification import QualificationMode, qualification_mode
from . import waiver as waiver_module


class OutboundClass(str, Enum):
    ENGINEERING = 'engineering'
    INTERNAL = 'internal'
    DIAGNOSTIC = 'diagnostic'
    TRANSFER = 'transfer'


#: Every operation that writes outside the case, and what it is. §8.6 names
#: each of these explicitly; the coverage test fails on an outbound operation
#: that is absent here.
OUTBOUND_REGISTRY: Mapping[str, OutboundClass] = {
    'case.export.native': OutboundClass.ENGINEERING,
    'case.export.vtk': OutboundClass.ENGINEERING,
    'case.export.cgns': OutboundClass.ENGINEERING,
    'case.export.gmsh': OutboundClass.ENGINEERING,
    'case.export.su2': OutboundClass.ENGINEERING,
    'case.export.med': OutboundClass.ENGINEERING,
    'case.export.unv': OutboundClass.ENGINEERING,
    'case.export.fluent': OutboundClass.ENGINEERING,
    'case.export.authored': OutboundClass.ENGINEERING,
    # Republishes this case's own mesh; invalidates the fingerprint rather
    # than emitting an artifact anyone computes on elsewhere.
    'case.export.format_convert': OutboundClass.INTERNAL,
    # Plan 28 WP3. A listing, not an artifact: it answers "which formats can
    # this case produce" so the export dialog can offer the target solver's
    # first. Nothing leaves the case, so there is nothing to qualify.
    'case.export.entries': OutboundClass.INTERNAL,
    'mesh.canonical.export.openfoam': OutboundClass.INTERNAL,
    # Evidence about a mesh, not a mesh. The failed-set export emits the
    # offending cells from `volume-quality.json`, which is what an engineer
    # opens *because* an export was refused -- gating it would make a blocked
    # case undiagnosable. Found by the coverage test below, not by this list.
    'quality.report.export': OutboundClass.DIAGNOSTIC,
    'quality.canonical.failed_set.export': OutboundClass.DIAGNOSTIC,
    'case.copy': OutboundClass.TRANSFER,
    'case.archive': OutboundClass.TRANSFER,
}


class ExportRefused(RuntimeError):
    """Enforcing mode refused an export. Carries a stable ``reason``."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Authorization:
    """What an export is permitted to claim about itself."""

    mode: str
    qualified: bool
    summary_fingerprint: str = ''
    waiver_fingerprints: tuple[str, ...] = ()
    report_fingerprints: Mapping[str, str] = field(default_factory=dict)
    reason: str = ''

    def to_manifest(self) -> dict:
        """The stamp that travels with the artifact."""
        return {
            'qualification_mode': self.mode,
            'qualified': self.qualified,
            'summary_fingerprint': self.summary_fingerprint,
            'waiver_fingerprints': list(self.waiver_fingerprints),
            'report_fingerprints': dict(sorted(
                self.report_fingerprints.items())),
            'reason': self.reason,
        }


def classify(operation_id: str) -> OutboundClass | None:
    """What kind of outbound operation this is, or ``None`` if not outbound."""
    return OUTBOUND_REGISTRY.get(operation_id)


def authorize(operation_id: str, *, summary: Mapping | None,
              case_path: str | Path, mode: QualificationMode | None = None,
              waivers=None) -> Authorization:
    """Decide whether *operation_id* may proceed, and under what claim.

    Raises :class:`ExportRefused` only in enforcing mode and only for an
    engineering export. Every other combination returns an
    :class:`Authorization` describing what may honestly be claimed -- which for
    an unqualified case is ``qualified: false``, not a refusal.
    """
    outbound = classify(operation_id)
    resolved = mode if mode is not None else qualification_mode()

    if outbound is None or outbound is OutboundClass.INTERNAL:
        return Authorization(mode=resolved.value, qualified=False,
                             reason='not an outbound engineering artifact')

    fingerprints = _report_fingerprints(summary)
    summary_fingerprint = str((summary or {}).get('summary_fingerprint') or '')
    qualified = bool((summary or {}).get('qualified'))

    if outbound is OutboundClass.DIAGNOSTIC:
        return Authorization(
            mode=resolved.value, qualified=False,
            summary_fingerprint=summary_fingerprint,
            report_fingerprints=fingerprints,
            reason='diagnostic evidence is always exportable')

    if outbound is OutboundClass.TRANSFER:
        # Allowed in every mode; the state travels so the copy cannot later
        # be mistaken for a qualified export.
        return Authorization(
            mode=resolved.value, qualified=qualified,
            summary_fingerprint=summary_fingerprint,
            report_fingerprints=fingerprints,
            reason=_transfer_state(summary))

    # Engineering, from here down.
    if not resolved.enforces:
        return Authorization(
            mode=QualificationMode.REPORT_ONLY.value, qualified=False,
            summary_fingerprint=summary_fingerprint,
            report_fingerprints=fingerprints,
            reason='report-only mode makes no qualification claim')

    if not summary:
        raise ExportRefused(
            'enforcing mode requires a current qualification summary, and '
            'this case has none; run the quality summary first',
            reason='missing_summary')
    if (summary or {}).get('stale'):
        raise ExportRefused(
            'the qualification summary is stale — the mesh or its inputs '
            'changed after it was computed; re-run the summary',
            reason='stale_summary')

    if qualified:
        return Authorization(
            mode='qualified', qualified=True,
            summary_fingerprint=summary_fingerprint,
            report_fingerprints=fingerprints,
            reason='all required blocks passed')

    # Not qualified: every non-pass required block needs a covering waiver.
    outstanding, covering = _waiver_coverage(summary, case_path, waivers)
    if outstanding:
        raise ExportRefused(
            'enforcing mode requires a waiver for every required block and '
            'blocking gate that did not pass; still uncovered: '
            f'{", ".join(sorted(outstanding))}',
            reason='unwaived_blocks')

    return Authorization(
        mode='waived', qualified=False,
        summary_fingerprint=summary_fingerprint,
        waiver_fingerprints=tuple(sorted(covering)),
        report_fingerprints=fingerprints,
        reason='exported as waived; not qualified')


def _report_fingerprints(summary: Mapping | None) -> dict:
    blocks = (summary or {}).get('blocks') or {}
    return {str(key): str((value or {}).get('report_fingerprint') or '')
            for key, value in blocks.items()
            if isinstance(value, Mapping)}


def _transfer_state(summary: Mapping | None) -> str:
    if not summary:
        return 'not evaluated'
    if summary.get('stale'):
        return 'stale'
    if summary.get('qualified'):
        return 'qualified'
    return 'waived' if summary.get('waived') else 'not qualified'


#: Blocking-gate states that are outright failures, spelled the way §9's
#: summary records them. ``WARNING`` and ``WAIVED`` are deliberately absent:
#: §9 already turns those into the ``waived`` disposition, and a gate somebody
#: opened is not the same thing as a gate that failed.
_GATE_FAILED = frozenset({'FAIL', 'FAILED', 'INVALID'})


def _waiver_coverage(summary: Mapping, case_path, waivers):
    """Required non-pass blocks with no covering waiver, and the ones with."""
    evidence = {key: str(value) for key, value in
                (summary.get('evidence') or {}).items()}
    available = (waiver_module.load_all(case_path) if waivers is None
                 else tuple(waivers))
    outstanding: set[str] = set()
    covering: set[str] = set()

    # R144. snappy's GF1 is a blocking gate and it is not one of §9's blocks,
    # so the loop below never saw it: a summary recording
    # `blocking_gate_evidence: {task_id: snappy.fidelity_snap, state: FAIL}`
    # with three passing blocks produced no outstanding names at all, and the
    # export left claiming `mode: waived` over a gate nobody had waived.
    gate = summary.get('blocking_gate_evidence') or {}
    if isinstance(gate, Mapping) and str(
            gate.get('state') or '').upper() in _GATE_FAILED:
        gate_task = str(gate.get('task_id') or '')
        found = (waiver_module.covering(available, gate_task, evidence)
                 if gate_task else None)
        if found is None:
            outstanding.add(gate_task or 'blocking_gate')
        else:
            covering.add(found.fingerprint)
    for name, block in (summary.get('blocks') or {}).items():
        if not isinstance(block, Mapping) or not block.get('required'):
            continue
        if str(block.get('verdict') or '').lower() == 'pass':
            continue
        # A waiver is recorded against the *task* that produced the block
        # (`common.fidelity`), not against §9's block name
        # (`geometry_fidelity`). Matching on the name meant no real waiver
        # ever covered a real block: the unit test passed because its fixture
        # used task ids as block keys, so the lookup agreed with the fixture
        # and not with the producer. Found by the first integration test to
        # drive a genuine report through a genuine waiver.
        task_id = str(block.get('task_id') or '') or str(name)
        found = waiver_module.covering(available, task_id, evidence)
        if found is None:
            outstanding.add(str(name))
        else:
            covering.add(found.fingerprint)
    return outstanding, covering
