"""Produce the fidelity report the summary reads and the waiver binds to.

Plan 23 §9. Everything else in this package measures something; nothing until
now *wrote* the artifact those measurements are for. The tasks were registered
(§8.4), the artifact contracts declared, the summary composed from
``fidelity.json`` and the export preflight authorized against it — and no code
produced the file. Exactly the shape of the missing summary producer, one
layer down, and found the same way: by asking who writes what something reads.

Three properties are the reason this is its own module rather than a few lines
in a handler.

**The verdict is per named section, and the report's verdict is the worst of
them.** A whole-mesh aggregate cannot see a lost patch — measured: deleting one
patch of 129 moves a global worst-deviation figure by almost nothing, because
128 patches are still exactly right. §4 makes the section the unit for this
reason.

**A section with no applicable tolerance is `unrated`, never `pass`.** The
denominator comes from :mod:`.policy`'s five levels and there is no fallback
below them. Any denominator broad enough to be safe for a whole model is broad
enough to hide a lost fin — the coarse finned heat sink, missing 20 of its 24
fins, reads as acceptable against its meshing domain and fails by 1.58× against
the fin. So an unresolved tolerance withholds the claim and keeps the numbers.

**A section that was never published is `missing`, not absent.** Identity is
reconciled separately from geometry because distance cannot answer it: on a
Gmsh mesh, patches are CAD faces sharing every edge, so a deleted face's points
all still lie on the remaining surface and the measured deviation is exactly
zero. No threshold catches that. Reconciliation does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from .policy import TolerancePolicy, ratio as tolerance_ratio
from .thresholds import CANDIDATE

FIDELITY_SCHEMA_VERSION = 1

#: Bumped whenever a change would move a verdict on unchanged inputs. Bound
#: into the report, so a waiver recorded under an older calculation stops
#: covering a mesh re-checked under a newer one.
CALCULATION_VERSION = '1'

#: Worst-last, so composing a report verdict is a max rather than a table.
SEVERITY = ('pass', 'warning', 'unrated', 'incomplete', 'fail')

#: Outcomes from §4's reconciliation that are failures of identity rather than
#: of geometry. They cannot be measured and must not be silently dropped.
IDENTITY_FAILURES = {'missing', 'unexpected'}


def _rank(verdict: str) -> int:
    try:
        return SEVERITY.index(verdict)
    except ValueError:
        return SEVERITY.index('unrated')


@dataclass(frozen=True)
class SectionResult:
    """One named section's verdict and the numbers behind it."""

    name: str
    patch_uuid: str
    verdict: str
    reason: str = ''
    deviation: float | None = None
    tolerance: float | None = None
    tolerance_source: str = ''
    ratio: float | None = None
    metrics: Mapping = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'name': self.name, 'patch_uuid': self.patch_uuid,
                'verdict': self.verdict, 'reason': self.reason,
                'deviation': self.deviation, 'tolerance': self.tolerance,
                'tolerance_source': self.tolerance_source,
                'ratio': self.ratio, 'metrics': dict(self.metrics)}


@dataclass(frozen=True)
class FidelityReport:
    """The artifact ``common.fidelity`` and ``snappy.fidelity_snap`` produce."""

    task_id: str
    sections: tuple[SectionResult, ...]
    evidence: Mapping[str, str]
    policy_fingerprint: str
    schema_version: int = FIDELITY_SCHEMA_VERSION

    @property
    def verdict(self) -> str:
        """The worst section. Never an average.

        §6: overall status is capped by the worst critical named section and
        cannot be rescued by a global average. An area-weighted mean would let
        one lost 1.5 mm fin disappear into a 120 mm plate that is perfect.
        """
        if not self.sections:
            return 'unrated'
        return max((item.verdict for item in self.sections), key=_rank)

    @property
    def report_fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(
            self._body(), sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()

    def _body(self) -> dict:
        return {'schema_version': self.schema_version,
                'task_id': self.task_id,
                'sections': [item.to_dict() for item in self.sections],
                'evidence': dict(sorted(self.evidence.items())),
                'policy_fingerprint': self.policy_fingerprint}

    def to_dict(self) -> dict:
        document = self._body()
        document['verdict'] = self.verdict
        document['report_fingerprint'] = self.report_fingerprint
        # Flattened for the summary and the waiver, which bind to these keys
        # at the top level rather than reaching into `evidence`.
        document.update(self.evidence)
        document['calculation_version'] = CALCULATION_VERSION
        document['policy_fingerprint'] = self.policy_fingerprint
        document['rated'] = sum(1 for item in self.sections
                                if item.verdict != 'unrated')
        document['unrated'] = sum(1 for item in self.sections
                                  if item.verdict == 'unrated')
        # R161. `rated` counts sections that carry a verdict, and `incomplete`
        # carries one -- so a report that measured nothing at all read
        # `rated: 4` beside four sections none of which had a distance
        # computed, and every reader took that for four measurements. This is
        # the count of sections that actually produced a number.
        document['measured'] = sum(1 for item in self.sections
                                   if item.deviation is not None)
        return document


def policy_fingerprint(policy: TolerancePolicy, thresholds=None) -> str:
    """Identity of the rules a report was computed under.

    Covers the tolerances *and* the bands, because changing either changes the
    verdict — and a waiver bound to one set of rules must not survive a switch
    to another.
    """
    payload = {'tolerances': policy.to_dict(),
               'thresholds': (thresholds or CANDIDATE).to_dict()}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def section_result(section, *, deviation, policy: TolerancePolicy,
                   thresholds=None, category: str = '',
                   region: str = '',
                   unmeasured_reason: str = '') -> SectionResult:
    """Judge one reconciled section.

    ``section`` is a :class:`~.boundary.BoundarySection`; ``deviation`` its
    measured worst deviation, or ``None`` when measurement did not complete.

    ``unmeasured_reason`` is why there is no deviation, when the caller knows.
    R39/R99/R121: this function used to answer "measurement did not complete
    inside the diagnostic budget" for *every* absent deviation, so a section
    that had no reference geometry at all -- the real cause, recorded in
    ``metrics['reason']`` as "no reference for section" -- was reported as a
    timeout. Two engines failed the same gate for two different stated reasons
    and neither statement was true, which is worse than an unmeasured section:
    it sends whoever reads it to the budget setting.
    """
    thresholds = thresholds or CANDIDATE
    # `BoundarySection`'s own field names. Read via getattr with defaults
    # this once looked at `name`/`outcome`, which the producer does not
    # write -- so every real section would have arrived nameless and
    # `matched`, and a `missing` patch would have been measured instead of
    # failed. The unit fixture had `name`/`outcome`, so it agreed with the
    # lookup and not with the producer, and the tests passed.
    name = getattr(section, 'solver_name', '')
    uuid = str(getattr(section, 'patch_uuid', '') or '')
    status = str(getattr(section, 'status', '') or '')
    if not status:
        raise ValueError(
            f'section {name!r} carries no reconciliation status; §4 requires '
            'one of matched/missing/unexpected/empty')

    if status in IDENTITY_FAILURES:
        # Identity, not geometry -- and a failure of it, not an absence of
        # data. `unrated` here would report "we could not check" about a
        # patch we know is wrong.
        # R142. The reason is shown to a user and round-trips through JSON,
        # a log and a console; the section sign arrived on screen as a
        # replacement character, so the one sentence explaining the failure
        # was unreadable. Plain words say the same thing everywhere.
        return SectionResult(
            name, uuid, 'fail',
            reason=f'section is {status}: reconciliation (section 4) against '
                   'the prepared manifest, which distance cannot answer')
    if status == 'empty':
        return SectionResult(name, uuid, 'unrated',
                             reason='section was published with no faces')
    if not uuid:
        return SectionResult(
            name, uuid, 'unrated',
            reason='patch identity is fabricated, so this section cannot be '
                   'joined to a prepared patch and cannot be rated')

    resolved = policy.resolve(patch_uuid=uuid, category=category,
                              region=region)
    if not resolved.resolved:
        return SectionResult(
            name, uuid, 'unrated',
            reason='no applicable tolerance (section 6 resolved none of its '
                   'five levels); raw metrics are recorded and no claim is '
                   'made',
            deviation=deviation)
    if deviation is None:
        return SectionResult(
            name, uuid, 'incomplete',
            reason=(unmeasured_reason
                    or 'measurement did not complete inside the diagnostic '
                       'budget'),
            tolerance=resolved.value, tolerance_source=resolved.source)

    ratio = tolerance_ratio(deviation, resolved)
    verdict = thresholds.fidelity_verdict(ratio)
    # R183. A fully measured section with a resolved tolerance still comes
    # back `unrated`, because the candidate thresholds are deliberately
    # unpromoted -- WP8's gate is shut on `corpus_complete`. That verdict is
    # right and its silence is not: MEASURED on the live tee, `inlet` sat at
    # 1.32x the project tolerance and reached the screen as `unrated` beside
    # an empty reason cell, which reads as a check that broke rather than one
    # that has not been calibrated. The verdict does not move here. Only the
    # blank does.
    reason = ''
    if verdict == 'unrated' and not thresholds.provenance.promoted:
        reason = ('measured, but the thresholds that would turn this into a '
                  'pass or a fail have not been promoted, so no claim is '
                  'made about it')
    return SectionResult(
        name, uuid, verdict, reason=reason,
        deviation=float(deviation), tolerance=resolved.value,
        tolerance_source=resolved.source, ratio=ratio)


def build(task_id: str, results: Sequence[SectionResult], *,
          evidence: Mapping[str, str], policy: TolerancePolicy,
          thresholds=None) -> FidelityReport:
    return FidelityReport(
        task_id=task_id, sections=tuple(results),
        evidence={key: str(value) for key, value in evidence.items()},
        policy_fingerprint=policy_fingerprint(policy, thresholds))


#: Where each task's report lives, relative to the case. These are the exact
#: paths §8.4's artifact contracts declare and the facade's report lookup
#: reads; a mismatch here is a report nothing can find.
REPORT_PATHS = {
    'common.fidelity': Path('foammesh/quality/fidelity.json'),
    'snappy.fidelity_snap': Path('foammesh/quality/fidelity-snap.json'),
    'gmsh.fidelity_native': Path('foammesh/quality/fidelity-native.json'),
    'common.resolution': Path('foammesh/quality/resolution.json'),
}


def write(case_path: str | Path, report: FidelityReport) -> Path:
    path = Path(case_path) / REPORT_PATHS[report.task_id]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    os.replace(temporary, path)
    return path


#: The fidelity tasks, newest-first preference when several exist. A snap or
#: native run describes the mesh more recently than the generic one.
FIDELITY_TASK_IDS = (
    'snappy.fidelity_snap', 'gmsh.fidelity_native', 'common.fidelity')


def latest(case_path: str | Path):
    """The most recently written fidelity report for this case, or ``None``.

    By file modification time rather than by task preference, because which
    task ran last is a fact about this case and any fixed order would show a
    stale report over a fresh one.
    """
    best = None
    for task_id in FIDELITY_TASK_IDS:
        path = Path(case_path) / REPORT_PATHS[task_id]
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if best is None or stamp > best[0]:
            document = read(case_path, task_id)
            if document is not None:
                best = (stamp, document)
    return best[1] if best else None


def read(case_path: str | Path, task_id: str):
    try:
        return json.loads((Path(case_path) / REPORT_PATHS[task_id]).read_text(
            encoding='utf-8'))
    except (OSError, ValueError, KeyError):
        return None
