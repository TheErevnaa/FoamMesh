"""The numbers that turn a measurement into a verdict, and what earns them.

Plan 23 WP8. Every other module in this package measures; this one is where a
measurement becomes `pass` or `fail`, which means it is the only place a
number can be wrong in a way that changes an answer without changing a
computation.

**A threshold is not a constant, it is a claim with evidence.** So a
:class:`ThresholdSet` carries its provenance — which corpus run justified it,
on how many cases, and whether the promotion gate passed — and
:func:`active_thresholds` refuses to hand back an unpromoted set for
enforcement. That refusal is the point. The failure mode this guards against
is not a typo; it is somebody plausibly guessing 0.5 mm, shipping it, and the
guess acquiring authority purely by sitting in the file for six months.

**The gate is five conditions, and every one is a way of being wrong.**

*Every negative control must fail.* A corpus of good meshes can only prove a
threshold is not too strict. Deliberately broken cases — a deleted feature, a
swapped patch, a unit error — are the only evidence that it is not too loose,
and a threshold that passes them is worse than no threshold, because it
certifies the defect.

*Every known-good control must pass.* The symmetric error. A threshold that
fails clean meshes trains people to waive, and a waiver reflex is
indistinguishable from having no gate at all.

*Scale copies must agree.* The same geometry at ×1 and ×100 must reach the
same verdict, or the threshold is measuring the unit system rather than the
mesh. This is the cheapest of the four to check and the easiest to get wrong,
because an absolute tolerance looks perfectly reasonable until someone models
in millimetres.

*The budget must hold.* A verdict nobody waits for is not a gate. If the check
cannot finish inside the diagnostic budget on the qualification hardware, the
honest state is `incomplete`, and promoting anyway would just relabel a
timeout as a pass.

*The corpus must be complete.* Added after the other four let the gate open on
a quarter of a corpus — see :data:`REQUIRED_TIERS`. Four honest conditions
evaluated over the wrong population are four honest answers to a question
nobody asked.

Until all five hold, thresholds stay `unrated` and report-only, which is
exactly what §9.1's mode setting exists to express.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Mapping, Sequence

THRESHOLD_SCHEMA_VERSION = 1

#: What a threshold set must justify before it may gate anything.
GATE_CONDITIONS = ('negative_controls_fail', 'known_good_controls_pass',
                   'scale_copies_agree', 'budget_holds', 'corpus_complete')

#: The corpus tiers WP8 requires. `corpus_complete` holds only when a run
#: covered every one.
#:
#: MEASURED, and this condition exists because its absence let the gate open:
#: a synthesized run over 12 retained meshes satisfied all four original
#: conditions and reported PROMOTED, while covering none of the cross-engine
#: rows, no mesher-induced feature loss, no thin-gap or sealed-hole controls
#: and no hardware matrix. Every individual condition was honestly true. The
#: corpus was a quarter of the one they were meant to be true *of*.
#:
#: The same shape as the empty-negative-controls hole guarded against above:
#: a condition evaluated over a subset is not a weaker claim, it is a claim
#: about something else.
REQUIRED_TIERS = ('analytic_primitives', 'negative_controls',
                  'scale_copies', 'assembly_cross_engine',
                  'mesher_feature_loss', 'hardware_matrix')


class ThresholdsNotPromoted(RuntimeError):
    """Enforcement was requested against thresholds that have not been earned."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Provenance:
    """Which corpus run justifies a threshold set, and how far it got."""

    corpus_run: str = ''
    cases: int = 0
    negative_controls: int = 0
    conditions: Mapping[str, bool] = field(default_factory=dict)
    tiers_covered: tuple[str, ...] = ()
    notes: str = ''

    @property
    def missing_tiers(self) -> tuple[str, ...]:
        covered = set(self.tiers_covered)
        return tuple(tier for tier in REQUIRED_TIERS if tier not in covered)

    @property
    def unmet(self) -> tuple[str, ...]:
        return tuple(name for name in GATE_CONDITIONS
                     if not self.conditions.get(name))

    @property
    def promoted(self) -> bool:
        """All gate conditions hold, on a corpus that actually ran.

        The case-count check is not ceremony: a run over zero cases satisfies
        "every negative control failed" vacuously, and vacuous truth is the
        most common way a gate like this ends up permanently open.
        """
        if self.cases <= 0 or self.negative_controls <= 0:
            return False
        return not self.unmet

    def to_dict(self) -> dict:
        return {'corpus_run': self.corpus_run, 'cases': self.cases,
                'negative_controls': self.negative_controls,
                'conditions': {name: bool(self.conditions.get(name))
                               for name in GATE_CONDITIONS},
                'tiers_covered': list(self.tiers_covered),
                'missing_tiers': list(self.missing_tiers),
                'notes': self.notes}


@dataclass(frozen=True)
class ThresholdSet:
    """One calibrated policy: the numbers, and what earned them.

    Tolerances are **relative to the model's own scale** rather than absolute.
    An absolute millimetre reads as concrete and precise, and it is precisely
    what makes a ×100 copy of the same part reach a different verdict.
    """

    #: Surface deviation as a **multiple of the applicable tolerance**
    #: (:mod:`.policy` resolves which one, by §6's five levels). At 1.0 the
    #: deviation exactly equals the tolerance, so `fail_ratio` is naturally
    #: 1.0 and not a tuned constant: a tolerance the user stated is the line
    #: they said they cared about, and a check that failed at 0.6 of it would
    #: be enforcing a stricter requirement than anyone asked for.
    #:
    #: MEASURED, and the reason this says "applicable tolerance" rather than
    #: any geometric extent: the coarse finned heat sink retained 4 of 24
    #: fins. Its worst fin deviation is 2.38 mm. Against the 1.5 mm fin that
    #: is 158% -- the fin is absent, not deviant. Against the 349 mm meshing
    #: domain it is 0.68%, which these bands call `warning`. A global
    #: denominator divides a local failure by the domain-to-feature ratio,
    #: here about 230, so the more local the defect the more thoroughly it is
    #: hidden. That is exactly backwards, and it is why §6 gives an explicit
    #: feature tolerance precedence over every default.
    warn_ratio: float
    fail_ratio: float
    #: Minimum cells across a channel (§6.6). Below `fail_cells` the region
    #: fails; between the two it warns.
    warn_cells: int
    fail_cells: int
    provenance: Provenance = field(default_factory=Provenance)
    schema_version: int = THRESHOLD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not 0 < self.warn_ratio <= self.fail_ratio:
            raise ValueError(
                'warn_ratio must be positive and no greater than fail_ratio; '
                f'got warn={self.warn_ratio}, fail={self.fail_ratio}')
        if not 0 < self.fail_cells <= self.warn_cells:
            raise ValueError(
                'fail_cells must be positive and no greater than warn_cells; '
                f'got fail={self.fail_cells}, warn={self.warn_cells}')

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_dict(), sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()

    def to_dict(self) -> dict:
        return {'schema_version': self.schema_version,
                'warn_ratio': self.warn_ratio, 'fail_ratio': self.fail_ratio,
                'warn_cells': self.warn_cells, 'fail_cells': self.fail_cells,
                'provenance': self.provenance.to_dict()}

    def fidelity_verdict(self, ratio: float | None) -> str:
        """A deviation ratio's verdict, or `unrated` when unpromoted.

        ``ratio`` is deviation divided by the *applicable tolerance* --
        :func:`.policy.ratio` computes it and returns ``None`` when §6's five
        levels resolve nothing. That ``None`` arrives here as `unrated`, which
        is the whole mechanism by which "no hidden default may turn an
        uncalibrated case green" is enforced rather than merely intended.

        Prefer :meth:`fidelity_of`, which takes the deviation and the resolved
        tolerance and cannot be handed a ratio against the wrong denominator.

        `unrated` rather than `pass` for an unmeasured section: the two are
        different statements, and only one of them is a claim about the mesh.
        """
        if not self.provenance.promoted:
            return 'unrated'
        if ratio is None:
            return 'unrated'
        if ratio > self.fail_ratio:
            return 'fail'
        return 'warning' if ratio > self.warn_ratio else 'pass'

    def fidelity_of(self, deviation: float, tolerance) -> str:
        """Verdict for a measured deviation under a resolved tolerance.

        The safe entry point: it takes the two quantities separately, so the
        denominator cannot be a model span by accident. An unresolved
        tolerance yields `unrated` with no arithmetic performed on it.
        """
        from .policy import ratio as tolerance_ratio

        return self.fidelity_verdict(tolerance_ratio(deviation, tolerance))

    def resolution_verdict(self, cells_across: int | None) -> str:
        if not self.provenance.promoted:
            return 'unrated'
        if cells_across is None:
            return 'unrated'
        if cells_across < self.fail_cells:
            return 'fail'
        return 'warning' if cells_across < self.warn_cells else 'pass'


#: The candidate policy. **Not promoted**, and deliberately so: no corpus run
#: has yet satisfied all four gate conditions, so every verdict it produces is
#: `unrated` and nothing gates. The numbers are starting points for WP8's
#: corpus to move, not answers.
#:
#: They are not arbitrary. `fail_ratio` 1% of characteristic length is roughly
#: where the retained-mesh sweep separates cases that visibly lost geometry
#: from those that did not (the drone quadcopter's worst reference sits at
#: 0.23%, the coarse heat sink at 1.59%, and the deliberately coarse repaired
#: propeller at 28%). `fail_cells` 3 is the classical minimum for a resolved
#: gradient across a channel. Both need the corpus before they mean anything.
#:
#: Recalibrated 2026-08-05. The earlier 0.005/0.01 were fractions of a model
#: span, and the WP8 feature-loss tier showed what that denominator costs: a
#: heat sink missing 20 of its 24 fins scored 0.0068 and read as `warning`.
#: Against the tolerance those same measurements are 1.58 versus 0.00, which
#: is the separation a gate needs and the span-relative form never had.
CANDIDATE = ThresholdSet(
    warn_ratio=0.5, fail_ratio=1.0, warn_cells=5, fail_cells=3,
    provenance=Provenance(
        notes='Recalibrated 2026-08-05 against the applicable tolerance '
              'rather than a model span. fail_ratio 1.0 is the stated '
              'tolerance itself; warn_ratio 0.5 flags a section that has '
              'consumed half its budget. Separates every corpus case: the '
              'coarse heat sink scores 1.58 against its 1.5 mm fin, the fine '
              'and Gmsh meshes score 0.00. Still unpromoted -- the gate is '
              'shut on corpus_complete.'))


def active_thresholds(*, enforcing: bool,
                      thresholds: ThresholdSet | None = None) -> ThresholdSet:
    """The policy to use, refusing to enforce one that has not been earned.

    In report-only this returns whatever it is given: computing and recording
    against a candidate is the entire purpose of shipping dark. In enforcing
    mode an unpromoted set raises, because the alternative is a gate whose
    numbers nobody has justified — and that gate would be believed.
    """
    policy = thresholds if thresholds is not None else CANDIDATE
    if enforcing and not policy.provenance.promoted:
        unmet = ', '.join(policy.provenance.unmet) or 'no corpus run recorded'
        raise ThresholdsNotPromoted(
            'geometry thresholds have not been promoted, so enforcing mode '
            f'has nothing justified to gate on; outstanding: {unmet}',
            reason='not_promoted')
    return policy


def evaluate_gate(*, negative_controls: Sequence[Mapping],
                  known_good: Sequence[Mapping],
                  scale_pairs: Sequence[Mapping],
                  budget: Mapping,
                  tiers_covered: Sequence[str] = ()) -> Provenance:
    """Judge a corpus run against the four conditions.

    Each argument is a sequence of result records; a record is a control's
    identity plus the verdict it received. The conditions are deliberately
    computed here rather than asserted by the caller, so a corpus run cannot
    declare itself passed.
    """
    conditions = {
        # Every one. A single negative control that passes means the threshold
        # certifies that defect, and one certified defect is enough.
        'negative_controls_fail': bool(negative_controls) and all(
            str(item.get('verdict')) in {'fail', 'warning'}
            for item in negative_controls),
        'known_good_controls_pass': bool(known_good) and all(
            str(item.get('verdict')) == 'pass' for item in known_good),
        'scale_copies_agree': bool(scale_pairs) and all(
            str(item.get('base_verdict')) == str(item.get('scaled_verdict'))
            for item in scale_pairs),
        'budget_holds': bool(budget.get('within_budget')),
        # Every required tier, not merely every tier that ran.
        'corpus_complete': not tuple(
            tier for tier in REQUIRED_TIERS if tier not in set(tiers_covered)),
    }
    return Provenance(
        corpus_run=str(budget.get('corpus_run') or ''),
        cases=len(negative_controls) + len(known_good),
        negative_controls=len(negative_controls),
        conditions=conditions, tiers_covered=tuple(tiers_covered))
