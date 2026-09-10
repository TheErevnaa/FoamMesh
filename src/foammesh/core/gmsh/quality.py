"""Quality thresholds and the gate that actually reads them.

The pipeline this replaces shipped quality evaluators with no production
caller: ``evaluate_surface``, ``accept_surface_quality``,
``assess_layer_feasibility`` and ``check_compute_budget`` were all dead. The
control existed, the number was editable, and nothing ever compared anything
to it.

:func:`assess` is called from the compute stage's result handling in
``core/facade/domain_operations.py`` (``_mesh_gmsh_run``), and its verdict is
recorded on the run manifest. There is no ``results.py`` in this package; an
earlier docstring said there was.

Plan 26 WP1 changed what the gate judges. It used to be::

    accepted = minimum >= thresholds.minimum

-- the single worst element decided everything, so a 141,486-element mesh was
discarded because *three* elements fell below the requested gamma, and the
user could neither see those three nor accept them. ``below_threshold`` and
``total`` were computed, written into the reason string, and played no part in
the decision. The gate now judges the distribution against a stated allowance
and returns one of four verdicts, only one of which a human may override.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CALCULATION_VERSION = 'gmsh.quality.v2'

#: Gmsh quality measures and whether a higher value is better.
MEASURES = {
    'sicn': True,    # signed inverse condition number, 1 is perfect
    'sige': True,    # signed inverse gradient error
    'gamma': True,   # inscribed/circumscribed radius ratio
    'disto': True,   # distortion
}

#: What ``gmsh.model.mesh.getElementQualities`` calls each measure.
#:
#: Measured against Gmsh 4.15.2, because the runner used to pass three of the
#: four straight through: only ``sicn`` was translated, and ``sige`` and
#: ``disto`` reached the API as their configuration spellings, where they raise
#: ``Unknown quality name`` and take the whole meshing run with them. ``gamma``
#: is accepted bare; ``sicn`` and ``sige`` need their ``min…`` spellings.
GMSH_QUERY_NAME = {
    'sicn': 'minSICN',
    'sige': 'minSIGE',
    'gamma': 'gamma',
}
#: ``disto`` is a valid ``Mesh.QualityType`` for the *optimiser* (0..3 all set
#: cleanly) but ``getElementQualities`` has no name for it, so the gate cannot
#: measure what it asked the optimiser to improve. Rather than crash or judge
#: silently against something else, the run substitutes this and says so.
UNQUERYABLE_SUBSTITUTE = 'sicn'

#: Verdicts. Ordered worst to best so ``max`` over a set of metric verdicts
#: yields the governing one.
INVALID = 'invalid'
FAIL = 'fail'
BLEMISH = 'blemish'
PASS = 'pass'
VERDICT_SEVERITY = {PASS: 0, BLEMISH: 1, FAIL: 2, INVALID: 3}
#: A blemish publishes on its own -- it is inside the allowance the user
#: authored. A fail may be accepted by a human, recorded as a waiver. An
#: invalid element is never acceptable by anyone: a solver cannot integrate
#: over an inverted or zero-volume cell, so there is nothing to consent to.
OVERRIDABLE = frozenset({BLEMISH, FAIL})

#: How many offending elements the runner records. Enough to browse, bounded so
#: a mesh where every element is bad cannot produce a gigabyte of manifest.
OFFENDER_CAP = 1000


class QualityError(ValueError):
    pass


@dataclass(frozen=True)
class QualityLimit:
    """One metric, its limit, and how much of the distribution may miss it."""

    measure: str
    minimum: float
    allowed_fraction: float = 0.0
    allowed_count: int = 0
    hard_floor: float = 0.0

    def to_dict(self) -> dict:
        return {
            'measure': self.measure,
            'minimum': self.minimum,
            'allowedFraction': self.allowed_fraction,
            'allowedCount': self.allowed_count,
            'hardFloor': self.hard_floor,
        }

    @property
    def query_name(self) -> str:
        """The name to hand ``getElementQualities``.

        Falls back to the substitute for a measure Gmsh cannot report, so the
        caller never passes a spelling that raises.
        """
        return GMSH_QUERY_NAME.get(
            self.measure, GMSH_QUERY_NAME[UNQUERYABLE_SUBSTITUTE])

    @property
    def queryable(self) -> bool:
        return self.measure in GMSH_QUERY_NAME

    def allowance(self, total: int) -> int:
        """How many elements may sit below :attr:`minimum` on a mesh of *total*.

        The two allowances are alternatives, not a conjunction: a user who sets
        a count means the count and a user who sets a fraction means the
        fraction, so the more permissive of the two governs. Both default to
        zero, which reproduces the old strict behaviour exactly.
        """
        by_fraction = int(self.allowed_fraction * total)
        return max(self.allowed_count, by_fraction)


@dataclass(frozen=True)
class QualityThresholds:
    measure: str
    minimum: float
    optimize: bool
    netgen_passes: int
    #: Plan 30 WP12. `Mesh.OptimizeNetgen` is a boolean; the runner used to
    #: write `netgen_passes` into it, so the flag and the pass count were the
    #: same number wearing two hats.
    netgen: bool = True
    #: `Mesh.Smoothing`, `Mesh.OptimizeThreshold`, `Mesh.HighOrderOptimize`.
    smoothing: int = 1
    optimize_threshold: float = 0.3
    high_order_optimize: int = 0
    #: Plan 31 FC-D. Gmsh's repair optimisers, run only on a mesh that has
    #: already failed. Held here rather than beside the mesher's own
    #: optimisers because the condition that starts it is a quality refusal.
    repair_poor_elements: bool = False
    allowed_fraction: float = 0.0
    allowed_count: int = 0
    hard_floor: float = 0.0
    #: WP1.3. Additional metrics judged independently. Orthogonal quality,
    #: skewness and volume ratio fail for different reasons and mean different
    #: things to a solver, so passing one must not mask another.
    extra_limits: tuple[QualityLimit, ...] = ()
    calculation_version: str = CALCULATION_VERSION

    @property
    def primary(self) -> QualityLimit:
        return QualityLimit(
            measure=self.measure, minimum=self.minimum,
            allowed_fraction=self.allowed_fraction,
            allowed_count=self.allowed_count, hard_floor=self.hard_floor)

    @property
    def limits(self) -> tuple[QualityLimit, ...]:
        return (self.primary, *self.extra_limits)

    def to_dict(self) -> dict:
        return {
            'qualityType': self.measure,
            'minQuality': self.minimum,
            'optimize': self.optimize,
            'netgen': self.netgen,
            'netgenPasses': self.netgen_passes,
            'smoothing': self.smoothing,
            'optimizeThreshold': self.optimize_threshold,
            'highOrderOptimize': self.high_order_optimize,
            'repairPoorElements': self.repair_poor_elements,
            'allowedFraction': self.allowed_fraction,
            'allowedCount': self.allowed_count,
            'hardFloor': self.hard_floor,
            'limits': [item.to_dict() for item in self.limits],
            'calculation_version': self.calculation_version,
        }


@dataclass(frozen=True)
class OffendingElement:
    """One element that fell below a limit, and where to look for it."""

    tag: int
    value: float
    measure: str
    centroid: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def to_dict(self) -> dict:
        return {'tag': self.tag, 'value': self.value, 'measure': self.measure,
                'centroid': list(self.centroid)}

    @classmethod
    def from_dict(cls, values: dict) -> 'OffendingElement':
        values = dict(values or {})
        centroid = tuple(float(item) for item in
                         (values.get('centroid') or (0.0, 0.0, 0.0)))[:3]
        while len(centroid) < 3:
            centroid = (*centroid, 0.0)
        return cls(tag=int(values.get('tag', 0) or 0),
                   value=float(values.get('value', 0.0) or 0.0),
                   measure=str(values.get('measure', '')),
                   centroid=centroid)                       # type: ignore[arg-type]


@dataclass(frozen=True)
class MetricVerdict:
    """One metric's outcome, judged on its own limit and its own allowance."""

    measure: str
    requested_minimum: float
    achieved_minimum: float
    achieved_mean: float
    below_threshold: int
    total: int
    allowance: int
    verdict: str
    reason: str = ''
    substituted_for: str = ''
    offending: tuple[OffendingElement, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.verdict in (PASS, BLEMISH)

    def to_dict(self) -> dict:
        return {
            'measure': self.measure,
            'requestedMinimum': self.requested_minimum,
            'achievedMinimum': self.achieved_minimum,
            'achievedMean': self.achieved_mean,
            'belowThreshold': self.below_threshold,
            'total': self.total,
            'allowance': self.allowance,
            'verdict': self.verdict,
            'accepted': self.accepted,
            'reason': self.reason,
            'substitutedFor': self.substituted_for,
            'offending': [item.to_dict() for item in self.offending],
        }


@dataclass(frozen=True)
class QualityVerdict:
    """What the mesh achieved, against what was asked for.

    ``accepted`` is retained with its original meaning -- may this mesh be
    published without a human decision -- so existing readers keep working.
    ``verdict`` is the new four-state answer, and ``overridable`` says whether
    a human is even allowed to accept it.
    """

    measure: str
    requested_minimum: float
    achieved_minimum: float
    achieved_mean: float
    below_threshold: int
    total: int
    accepted: bool
    reason: str = ''
    verdict: str = PASS
    allowance: int = 0
    overridable: bool = False
    metrics: tuple[MetricVerdict, ...] = ()
    warnings: tuple[str, ...] = ()
    offending: tuple[OffendingElement, ...] = field(default=())

    @property
    def governing(self) -> MetricVerdict | None:
        """The metric that decided the verdict, worst first."""
        if not self.metrics:
            return None
        return max(self.metrics,
                   key=lambda item: (VERDICT_SEVERITY[item.verdict],
                                     item.below_threshold))

    def to_dict(self) -> dict:
        return {
            'measure': self.measure,
            'requestedMinimum': self.requested_minimum,
            'achievedMinimum': self.achieved_minimum,
            'achievedMean': self.achieved_mean,
            'belowThreshold': self.below_threshold,
            'total': self.total,
            'accepted': self.accepted,
            'reason': self.reason,
            'verdict': self.verdict,
            'allowance': self.allowance,
            'overridable': self.overridable,
            'metrics': [item.to_dict() for item in self.metrics],
            'warnings': list(self.warnings),
            'offending': [item.to_dict() for item in self.offending],
        }


#: Where the mesh-quality report lives, and the task it belongs to. Named in a
#: table rather than derived from the task id: deriving it happened to agree
#: once and agreeing by coincidence is how this codebase has repeatedly ended
#: up with a lookup keyed on something its producer does not write.
REPORT_TASK_ID = 'gmsh.compute'
REPORT_PATH = 'foammesh/quality/mesh-quality.json'
#: Verdicts a human may waive, spelled the way :mod:`core.quality.waiver`
#: spells them. ``blemish`` never reaches a waiver -- it publishes on its own.
WAIVER_VERDICT = {FAIL: 'fail', BLEMISH: 'warning'}


def _enum(value, default=''):
    if value is None:
        return default
    return str(getattr(value, 'value', value)).split('.')[-1].lower()


def _measure(value, *, context: str) -> str:
    measure = _enum(value, 'sicn')
    if measure not in MEASURES:
        raise QualityError(
            f'unknown quality measure {measure!r} in {context}; expected one '
            f'of {", ".join(sorted(MEASURES))}')
    return measure


def _fraction(value, *, name: str) -> float:
    fraction = float(value if value not in (None, '') else 0.0)
    if not 0.0 <= fraction <= 1.0:
        raise QualityError(f'{name} {fraction} is outside 0-1')
    return fraction


def derive_limit(values: dict, *, context: str = 'optimization') -> QualityLimit:
    """One metric/limit/allowance triple from its configuration block."""
    values = dict(values or {})
    measure = _measure(values.get('qualityType') or values.get('measure'),
                       context=context)
    minimum = _fraction(values.get('minQuality', values.get('minimum', 0.1)),
                        name='minQuality')
    hard_floor = _fraction(values.get('hardFloor', 0.0), name='hardFloor')
    if hard_floor > minimum:
        raise QualityError(
            f'hardFloor {hard_floor:g} is above minQuality {minimum:g}; the '
            'floor is the point below which no allowance applies, so it '
            'cannot be the stricter of the two')
    allowed_count = int(values.get('allowedCount', 0) or 0)
    if allowed_count < 0:
        raise QualityError(f'allowedCount {allowed_count} is negative')
    return QualityLimit(
        measure=measure, minimum=minimum,
        allowed_fraction=_fraction(values.get('allowedFraction', 0.0),
                                   name='allowedFraction'),
        allowed_count=allowed_count, hard_floor=hard_floor)


def derive_thresholds(values: dict) -> QualityThresholds:
    values = dict(values or {})
    primary = derive_limit(values)
    passes = int(values.get('netgenPasses', 3) or 0)
    if not 0 <= passes <= 10:
        raise QualityError(f'netgenPasses {passes} is outside 0-10')
    extra = []
    seen = {primary.measure}
    for index, row in enumerate(values.get('extraLimits') or ()):
        limit = derive_limit(row, context=f'extraLimits[{index}]')
        if limit.measure in seen:
            raise QualityError(
                f'quality measure {limit.measure!r} is limited twice; each '
                'metric carries one limit')
        seen.add(limit.measure)
        extra.append(limit)
    smoothing = int(values.get('smoothing', 1) or 0)
    if not 0 <= smoothing <= 100:
        raise QualityError(f'smoothing {smoothing} is outside 0-100')
    high_order = int(values.get('highOrderOptimize', 0) or 0)
    if not 0 <= high_order <= 4:
        raise QualityError(f'highOrderOptimize {high_order} is outside 0-4')
    return QualityThresholds(
        measure=primary.measure, minimum=primary.minimum,
        optimize=bool(values.get('optimize', True)), netgen_passes=passes,
        netgen=bool(values.get('netgen', True)), smoothing=smoothing,
        optimize_threshold=_fraction(values.get('optimizeThreshold', 0.3),
                                     name='optimizeThreshold'),
        high_order_optimize=high_order,
        repair_poor_elements=bool(values.get('repairPoorElements', False)),
        allowed_fraction=primary.allowed_fraction,
        allowed_count=primary.allowed_count, hard_floor=primary.hard_floor,
        extra_limits=tuple(extra))


def _offenders(block: dict, measure: str) -> tuple[OffendingElement, ...]:
    rows = block.get('offending') or block.get('offendingElements') or ()
    return tuple(
        OffendingElement.from_dict(dict(row, measure=row.get('measure') or measure))
        for row in rows if isinstance(row, dict))


def _judge(limit: QualityLimit, block: dict) -> MetricVerdict:
    """Judge one metric's measured distribution against its own allowance."""
    total = int(block.get('total', 0) or 0)
    minimum = float(block.get('minimum', 0.0) or 0.0)
    mean = float(block.get('mean', 0.0) or 0.0)
    below = int(block.get('below_threshold', block.get('belowThreshold', 0)) or 0)
    allowance = limit.allowance(total)
    offending = _offenders(block, limit.measure)
    substituted = str(block.get('substituted_for')
                      or block.get('substitutedFor') or '')

    share = 100.0 * below / total if total else 0.0
    if minimum <= limit.hard_floor:
        # An inverted or zero-volume element. No allowance reaches this and no
        # human may accept it -- a solver cannot integrate over it at all.
        inverted = int(block.get('inverted', 0) or 0) or None
        counted = inverted if inverted is not None else below
        verdict, reason = INVALID, (
            f'{counted} element(s) are at or below the hard floor of '
            f'{limit.hard_floor:g} for {limit.measure} (worst {minimum:.4g}); '
            'an inverted or zero-volume cell cannot be accepted')
    elif below == 0:
        verdict, reason = PASS, ''
    elif below <= allowance:
        verdict, reason = BLEMISH, (
            f'{below} of {total} elements ({share:.2f}%) fall below the '
            f'requested {limit.measure} of {limit.minimum:g}, within the '
            f'allowance of {allowance}; the worst is {minimum:.4g}')
    else:
        verdict, reason = FAIL, (
            f'{below} of {total} elements ({share:.2f}%) fall below the '
            f'requested {limit.measure} of {limit.minimum:g}; the worst is '
            f'{minimum:.4g}')
        if allowance:
            reason += f' and the allowance is {allowance}'
    return MetricVerdict(
        measure=limit.measure, requested_minimum=limit.minimum,
        achieved_minimum=minimum, achieved_mean=mean, below_threshold=below,
        total=total, allowance=allowance, verdict=verdict, reason=reason,
        substituted_for=substituted, offending=offending)


def assess(thresholds: QualityThresholds, achieved: dict | None) -> QualityVerdict:
    """Judge a run's achieved element qualities against the requested limits.

    ``achieved`` is the block the runner records. Either the single-metric
    shape -- minimum, mean, below_threshold, total -- or, when more than one
    limit was configured, a ``metrics`` mapping of that same shape per measure.
    """
    achieved = dict(achieved or {})
    per_measure = dict(achieved.get('metrics') or {})
    if not per_measure:
        # Single-metric shape. Attribute it to whatever measure the block says
        # it measured, so a substituted run is judged under its own name.
        per_measure = {str(achieved.get('measure') or thresholds.measure):
                       achieved}

    total = max((int((block or {}).get('total', 0) or 0)
                 for block in per_measure.values()), default=0)
    if not total:
        return QualityVerdict(
            measure=thresholds.measure, requested_minimum=thresholds.minimum,
            achieved_minimum=0.0, achieved_mean=0.0, below_threshold=0,
            total=0, accepted=False, verdict=FAIL,
            reason='the run recorded no element qualities to judge')

    verdicts, warnings = [], []
    for limit in thresholds.limits:
        block = per_measure.get(limit.measure)
        if block is None and limit.measure not in GMSH_QUERY_NAME:
            # A measure Gmsh cannot report. The runner should have substituted
            # and said so; if it did not, say so here rather than judge nothing.
            block = per_measure.get(UNQUERYABLE_SUBSTITUTE)
            if block is not None:
                block = dict(block, substituted_for=limit.measure)
                limit = QualityLimit(
                    measure=UNQUERYABLE_SUBSTITUTE, minimum=limit.minimum,
                    allowed_fraction=limit.allowed_fraction,
                    allowed_count=limit.allowed_count,
                    hard_floor=limit.hard_floor)
        if block is None:
            warnings.append(
                f'{limit.measure} was limited but the run recorded no '
                f'measurement for it, so it was not judged')
            continue
        judged = _judge(limit, dict(block))
        if judged.substituted_for:
            warnings.append(
                f'Gmsh cannot report {judged.substituted_for} per element, so '
                f'the gate judged {judged.measure} against the same limit')
        verdicts.append(judged)

    if not verdicts:
        return QualityVerdict(
            measure=thresholds.measure, requested_minimum=thresholds.minimum,
            achieved_minimum=0.0, achieved_mean=0.0, below_threshold=0,
            total=total, accepted=False, verdict=FAIL,
            reason='no configured quality limit could be judged against the '
                   'measurements this run recorded',
            warnings=tuple(warnings))

    governing = max(verdicts,
                    key=lambda item: (VERDICT_SEVERITY[item.verdict],
                                      item.below_threshold))
    overall = governing.verdict
    # Plan 31 FC-D. A refusal that does not name its own remedy leaves the
    # user to find it. Gmsh's repair optimisers exist for exactly this mesh,
    # and this is the only place that knows the mesh was refused.
    if overall in (FAIL, INVALID) and not thresholds.repair_poor_elements:
        warnings.append(
            'the mesh was refused on quality and the repair pass was off; '
            "turning on 'Repair poor elements' runs Gmsh's untangling and "
            'relocation optimisers on the elements that failed, before the '
            'mesh is judged. It acts on straight-sided elements only.')
    return QualityVerdict(
        measure=governing.measure,
        requested_minimum=governing.requested_minimum,
        achieved_minimum=governing.achieved_minimum,
        achieved_mean=governing.achieved_mean,
        below_threshold=governing.below_threshold,
        total=governing.total,
        # A blemish is inside the allowance the user authored, so it publishes
        # without a further decision -- but it is recorded as a blemish, never
        # as a clean pass.
        accepted=overall in (PASS, BLEMISH),
        reason=governing.reason, verdict=overall,
        allowance=governing.allowance,
        overridable=overall in OVERRIDABLE,
        metrics=tuple(verdicts), warnings=tuple(warnings),
        offending=governing.offending)


def build_report(verdict: QualityVerdict, thresholds: QualityThresholds, *,
                 subject_mesh_fingerprint: str, checkpoint_fingerprint: str,
                 prepared_revision: str) -> dict:
    """The stored report a waiver can bind itself to.

    WP1.4 reuses :mod:`core.quality.waiver` rather than inventing a second
    override record, and that module binds a decision to five evidence keys so
    that re-meshing stops the waiver covering the case. All five are real here
    -- nothing is synthesised to satisfy the check:

    ``checkpoint_fingerprint``
        the derived job digest: what was asked for.
    ``subject_mesh_fingerprint``
        the hash of the mesh that was produced.
    ``prepared_revision``
        the geometry it was meshed from.
    ``policy_fingerprint``
        the limits and allowances it was judged against, so widening an
        allowance after the fact invalidates the decision made under the old
        one.
    ``calculation_version``
        this module's, so a gate that changes its mind stops being covered.
    """
    document = verdict.to_dict()
    document.update({
        'schema_version': 1,
        'task_id': REPORT_TASK_ID,
        'checkpoint_fingerprint': str(checkpoint_fingerprint or ''),
        'subject_mesh_fingerprint': str(subject_mesh_fingerprint or ''),
        'prepared_revision': str(prepared_revision or ''),
        'policy_fingerprint': policy_fingerprint(thresholds),
        'calculation_version': thresholds.calculation_version,
        'thresholds': thresholds.to_dict(),
    })
    # The waiver refuses a verdict it does not recognise, which is the point:
    # `pass` has nothing to waive and `invalid` may not be waived at all, so
    # neither gets a spelling here.
    document['verdict'] = WAIVER_VERDICT.get(verdict.verdict, verdict.verdict)
    document['gate_verdict'] = verdict.verdict
    document['report_fingerprint'] = _fingerprint(document)
    return document


def policy_fingerprint(thresholds: QualityThresholds) -> str:
    """Content address of the limits a verdict was judged against."""
    return _fingerprint({'limits': [item.to_dict() for item in thresholds.limits],
                         'calculation_version': thresholds.calculation_version})


def _fingerprint(document) -> str:
    import hashlib
    import json

    payload = {key: value for key, value in dict(document).items()
               if key != 'report_fingerprint'}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(',', ':'),
        default=str).encode('utf-8')).hexdigest()
