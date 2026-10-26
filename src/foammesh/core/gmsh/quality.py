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
from pathlib import Path

from foammesh.core.quantities import agreeing, count_text

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
    'disto': 'disto',
}
#: Plan 41 GN14 (F5a). ``disto`` has no ``getElementQualities`` name, but it is
#: Gmsh's ratio of an element's smallest to its largest Jacobian determinant and
#: both are named (``minDetJac``, ``maxDetJac``), so it is measured and nothing
#: stands in for it. Only a measure that is in none of the tables is judged
#: against this, and the run says so.
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


def extra_limit_rows(value) -> list:
    """The further-limits rows from a list or from the stored JSON text."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        import json

        try:
            value = json.loads(text)
        except ValueError as error:
            raise QualityError(
                f'the further quality limits are not valid JSON: {error}')
    if not isinstance(value, (list, tuple)):
        if value in (None, ''):
            return []
        raise QualityError('the further quality limits are not a list')
    return [dict(row) for row in value if isinstance(row, dict)]


def derive_thresholds(values: dict) -> QualityThresholds:
    values = dict(values or {})
    primary = derive_limit(values)
    passes = int(values.get('netgenPasses', 3) or 0)
    if not 0 <= passes <= 10:
        raise QualityError(f'netgenPasses {passes} is outside 0-10')
    extra = []
    seen = {primary.measure}
    for index, row in enumerate(extra_limit_rows(values.get('extraLimits'))):
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
            f'{count_text(counted, "element")} '
            f'{agreeing(counted, "is", "are")} at or below the hard '
            f'floor of {limit.hard_floor:g} for {limit.measure} '
            f'(worst {minimum:.4g}); an inverted or zero-volume cell cannot '
            'be accepted')
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


def _the_remedy_that_can_act(governing, per_measure: dict, *,
                             optimize: bool = True) -> str:
    """What to advise a user whose mesh was refused and whose repair pass is off.

    DP-82. The repair pass moves nodes, so it can only help where moving a
    node helps. When the surface the volume was built on reads no better than
    the volume does, moving nodes cannot: a cell resting on a sliver face is a
    sliver, and the face is where the surface put it. MEASURED on gmsh
    drone_quadcopter -- 1405 of 5228 boundary faces below gamma 0.1 against
    5097 of 259863 cells, and turning the repair pass on left 5741 cells below
    the limit instead of 5097, worst 0.000344 to 0.000388. Advice that costs a
    second run to disprove is worse than no advice, which is the same reason
    DP-76 stopped advising it on a layered mesh.
    """
    surface = {}
    for block in per_measure.values():
        candidate = dict(block or {}).get('surface') or {}
        if int(candidate.get('total', 0) or 0):
            surface = candidate
            break
    faces = int(surface.get('total', 0) or 0)
    bad_faces = int(surface.get('below_threshold', 0) or 0)
    cells = int(governing.total or 0)
    bad_cells = int(governing.below_threshold or 0)
    surface_share = (bad_faces / faces) if faces else 0.0
    volume_share = (bad_cells / cells) if cells else 0.0
    if (not bad_faces or surface_share < volume_share) and not optimize:
        # DP-780. MEASURED on mesh campaign 0925 run G9A (elbow, Optimize and
        # Netgen off): refused at 24 of 9150 below sICN 0.1. Re-run with the
        # repair pass on it came back INVALID, 3 inverted; with Optimize on
        # it passed, 0 of 8482 below, worst 0.4932.
        return ("the mesh was refused on quality with 'Optimize' switched "
                'off, so Gmsh ran no optimisation pass over the tetrahedra it '
                "built. Turn 'Optimize' back on (its default) under Advanced "
                'on the Generate mesh step; the repair pass is not a '
                'substitute for it.')
    if not bad_faces or surface_share < volume_share:
        return ('the mesh was refused on quality and the repair pass was off; '
                "turning on 'Repair poor elements' runs Gmsh's untangling and "
                'relocation optimisers on the elements that failed, before '
                'the mesh is judged. It acts on straight-sided elements only.')
    geometry = (
        ' This run meshed the imported triangulation as it was supplied, so '
        'that surface is the geometry: repair or replace the tessellation, or '
        'raise the allowance for this case.'
        if surface.get('keptTessellation') else
        ' Refine or remesh the surface before the volume is built on it, or '
        'raise the allowance for this case.')
    return (
        f'the mesh was refused on quality, and the surface it was built on '
        f'carries the same fault: {bad_faces:,} of '
        f'{count_text(faces, "boundary face")} '
        f'({surface_share:.1%}) {agreeing(bad_faces, "is", "are")} '
        'themselves below the requested '
        f'{governing.measure} of {governing.requested_minimum:g}, the worst at '
        f"{float(surface.get('minimum', 0.0) or 0.0):.3g}, against "
        f'{bad_cells} of {cells} ({volume_share:.1%}) in the volume. A cell '
        f'resting on a sliver face is a sliver, so the repair pass cannot lift '
        f'it: it moves nodes, and these nodes are where the surface put them.'
        + geometry)


def refusal_words(verdict) -> str:
    """The refusal a user reads: the verdict's reason and the remedy it found.

    DP-94. :func:`assess` is the only place that knows both why a mesh was
    refused and what can act on it, and it writes the second half into
    ``warnings`` -- which the failure the user is shown never carried.
    MEASURED on gmsh drone_quadcopter: the window said `4991 of 260945
    elements (1.91%) fall below the requested gamma of 0.1; the worst is
    0.0003504 and the allowance is 1304` and stopped there, while the
    verdict beside it held `the surface it was built on carries the same
    fault: 1703 of 5240 boundary face(s) (32.5%) are themselves below the
    requested gamma of 0.1 ... repair or replace the tessellation, or raise
    the allowance for this case`. The count is the symptom; the sentence
    that was dropped is the only one that says what to do next.

    Each piece was written to sit alone, so each is given a stop and an
    opening capital here rather than running the second one on in the middle
    of a sentence it did not start.
    """
    parts: list[str] = []
    for text in (verdict.reason, *verdict.warnings):
        text = str(text or '').strip()
        if not text:
            continue
        if text[:1].islower():
            text = text[:1].upper() + text[1:]
        if not text.endswith(('.', '!', '?')):
            text += '.'
        if text in parts:
            continue
        parts.append(text)
    return ' '.join(parts)


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

    # DP-76. The runner judges the limit against the cells it means something
    # for and records the boundary layer beside them, because gamma condemns
    # anisotropy and a layer cell is anisotropic on purpose. The gate reads
    # that here for two reasons: to say the layer was counted and not judged,
    # and because the remedy it would otherwise advise cannot touch a prism.
    layers = [dict(block or {}).get('layerCells') or {}
              for block in per_measure.values()]
    layers = [block for block in layers if int(block.get('total', 0) or 0)]
    layered = bool(layers)

    verdicts, warnings = [], []
    if layered:
        seen = layers[0]
        warnings.append(
            count_text(int(seen.get('total', 0) or 0), 'boundary-layer cell')
            + agreeing(int(seen.get('total', 0) or 0), ' is', ' are')
            + ' counted beside this judgement rather than inside it '
            f"({int(seen.get('below_threshold', 0) or 0)} of them fall below "
            f'the limit): the measure reads a deliberately thin cell as a bad '
            f'one. They are still required not to invert, and '
            f"{int(seen.get('inverted', 0) or 0)} did.")
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
    # DP-780. With the mesher's own optimiser switched off, the refusal is
    # judged too: that switch, not the repair pass, is the remedy to name.
    if overall in (FAIL, INVALID) and (not thresholds.repair_poor_elements
                                       or not thresholds.optimize):
        # DP-76. Only where the remedy can act. A mesh carrying a boundary
        # layer is one it cannot: MEASURED on drone_quadcopter, with the
        # repair pass turned on, UntangleMeshGeometry refused the mesh
        # outright -- 'prism not supported yet, abort' -- and Relocate3D ran
        # over 276574 cells without moving one. Advice that costs the user a
        # second run to disprove is worse than no advice.
        if layered and thresholds.repair_poor_elements:
            pass                    # the pass ran; there is nothing to name
        elif layered:
            warnings.append(
                'the mesh was refused on quality and the repair pass was '
                "off, but turning on 'Repair poor elements' would not "
                'rescue this mesh: it carries a boundary layer, and Gmsh '
                "refuses to untangle prisms ('prism not supported yet') "
                'while the relocation pass was measured moving nothing. '
                'Change the layer, the surface sizing, or the geometry.')
        else:
            warnings.append(_the_remedy_that_can_act(
                governing, per_measure, optimize=thresholds.optimize))
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


# -- the gate reading the hand-over's arrays (Plan 41 GN14, section 4.3) ------ #

#: The cell array each measure is read from. ``disto`` is the ratio of the two
#: Jacobian arrays rather than an array of its own.
ARRAY_FOR_MEASURE = {'sicn': 'gmshSICN', 'sige': 'gmshSIGE',
                     'gamma': 'gmshGamma'}


def disto_from_jacobians(smallest, largest):
    """Gmsh's distortion: smallest over largest Jacobian determinant, per cell.

    At most 1. A cell whose largest determinant is zero reads 0 and one whose
    largest is negative (inverted throughout) reads -1, so ``value <= 0`` still
    counts as inverted exactly as it does for the other measures.
    """
    import numpy as np

    smallest = np.asarray(smallest, dtype=np.float64)
    largest = np.asarray(largest, dtype=np.float64)
    out = np.where(largest < 0, -1.0, 0.0)
    positive = largest > 0
    out[positive] = np.minimum(1.0, smallest[positive] / largest[positive])
    return out


def measure_values(arrays: dict, measure: str):
    """The per-cell values of *measure* from hand-over *arrays*, or ``None``."""
    if measure == 'disto':
        low, high = arrays.get('gmshMinDetJac'), arrays.get('gmshMaxDetJac')
        if low is None or high is None:
            return None
        return disto_from_jacobians(low, high)
    name = ARRAY_FOR_MEASURE.get(measure)
    return arrays.get(name) if name else None


def block_from_arrays(measure: str, values, tags, threshold: float, *,
                      centroids=None) -> dict:
    """One metric's achieved block from its full per-cell array.

    The runner's block carries at most :data:`OFFENDER_CAP` offenders because
    it travels in ``result.json``. This reads the array itself, so every
    element below *threshold* is named, worst first.
    """
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    tags = np.asarray(tags)
    finite = np.isfinite(values)
    judged = values[finite]
    if not len(judged):
        return {'measure': measure, 'minimum': 0.0, 'mean': 0.0,
                'below_threshold': 0, 'total': 0, 'inverted': 0,
                'offending': []}
    below = np.nonzero(finite & (values < float(threshold)))[0]
    below = below[np.argsort(values[below], kind='stable')]
    offending = []
    for index in below:
        centroid = (list(map(float, centroids[index]))
                    if centroids is not None else [0.0, 0.0, 0.0])
        offending.append({'tag': int(tags[index]),
                          'value': float(values[index]),
                          'measure': measure, 'centroid': centroid})
    return {'measure': measure, 'minimum': float(judged.min()),
            'mean': float(judged.mean()), 'below_threshold': int(len(below)),
            'total': int(len(judged)),
            'inverted': int(np.count_nonzero(judged <= 0)),
            'offending': offending, 'offendingTruncated': False}


def achieved_from_arrays(thresholds: QualityThresholds, arrays: dict, *,
                         centroids=None) -> dict:
    """The ``achieved`` mapping :func:`assess` reads, built from the arrays.

    The highest-dimension elements are judged (the volume of a 3D mesh, the
    surface of a 2D one), as the runner judges the volume.
    """
    import numpy as np

    dims = np.asarray(arrays.get('gmshEntityDim', ()))
    judged_dim = int(dims.max()) if len(dims) else 0
    mask = dims == judged_dim
    tags = np.asarray(arrays['gmshElementTag'])[mask]
    local = None if centroids is None else np.asarray(centroids)[mask]
    metrics = {}
    for limit in thresholds.limits:
        values = measure_values(arrays, limit.measure)
        if values is None:
            continue
        metrics[limit.measure] = block_from_arrays(
            limit.measure, np.asarray(values)[mask], tags, limit.minimum,
            centroids=local)
    return {'metrics': metrics}


def assess_arrays(thresholds: QualityThresholds, arrays: dict, *,
                  centroids=None) -> QualityVerdict:
    """Judge the hand-over's own per-element arrays against the limits."""
    return assess(thresholds, achieved_from_arrays(
        thresholds, arrays, centroids=centroids))


def gate_verdict_dict(report: dict | None) -> dict:
    """The strip's verdict shape from a stored gate report.

    ``mesh-quality.json`` spells the verdict the way waivers do (``fail``,
    ``warning``); the real four-state word is ``gate_verdict``.
    """
    document = dict(report or {})
    if not document:
        return {}
    word = str(document.get('gate_verdict') or document.get('verdict') or '')
    document['verdict'] = {'warning': BLEMISH}.get(word, word)
    document.setdefault('offending', [])
    return document


# -- Plan 48 GM12: surface deviation of a Gmsh mesh (plan 4.10.1, R7) ----------- #
#
# The shared deviation tool measured the centres of linear facets against a
# tessellated reference.  On Gmsh it is reworked to measure what a Gmsh mesh is:
#
# * **curved elements are sampled as curved** (any element order): points on the
#   element are taken from its Lagrange basis, the lattice is refined per element
#   until two levels differ by less than a declared sampling tolerance, and the
#   tolerance, the levels and the number of elements that did not settle are
#   reported;
# * **the distance is to the trimmed CAD faces**, not to the tessellation:
#   ``getClosestPoint`` projects on the carrier surface, which can lie outside
#   the trimmed face (beyond an edge, inside a hole), so each projection is
#   checked with ``isInside`` and an outside one is replaced by the distance to
#   the face's boundary curves; the smallest valid distance over the faces wins;
# * it works on a bare ``.msh``, an SU2 export, a second-order mesh and the
#   2D surface mesh straight after "Mesh 2D".
#
# The measuring runs in the qualified Gmsh runtime (it needs OpenCASCADE), as a
# worker whose source is below; the pieces that decide (the lattice, the
# convergence rule, the trimmed distance) are plain functions in that source so
# a test can run them against a stand-in for the Gmsh model.

#: Share of the deviation tolerance the sampling must settle to (plan 4.10.1 (4)).
SAMPLING_SHARE = 0.1

DEVIATION_WORKER = r'''
import json, sys, math
import numpy as np
import gmsh

def lattice(is_triangle, n):
    """Local coordinates of the n-times subdivided reference element."""
    if is_triangle:
        return np.array([(i / n, j / n, 0.0) for j in range(n + 1)
                         for i in range(n + 1 - j)])
    return np.array([(-1.0 + 2.0 * i / n, -1.0 + 2.0 * j / n, 0.0)
                     for j in range(n + 1) for i in range(n + 1)])

def triangle_distance(points, tris):
    """Distance of each point to the nearest of the triangles (T by 3 by 3)."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    best = np.full(len(points), np.inf)
    step = max(1, int(2000000 // max(len(tris), 1)))
    for lo in range(0, len(points), step):
        p = points[lo:lo + step][:, None, :]
        ab, ac, ap = b - a, c - a, p - a
        d1, d2 = (ab * ap).sum(-1), (ac * ap).sum(-1)
        bp = p - b
        d3, d4 = (ab * bp).sum(-1), (ac * bp).sum(-1)
        cp = p - c
        d5, d6 = (ab * cp).sum(-1), (ac * cp).sum(-1)
        vc = d1 * d4 - d3 * d2
        vb = d5 * d2 - d1 * d6
        va = d3 * d6 - d5 * d4
        denom = va + vb + vc
        denom = np.where(denom == 0, 1e-300, denom)
        v, w = vb / denom, vc / denom
        q = a + ab * v[..., None] + ac * w[..., None]
        # vertex and edge regions
        q = np.where(((d1 <= 0) & (d2 <= 0))[..., None], a, q)
        q = np.where(((d3 >= 0) & (d4 <= d3))[..., None], b, q)
        q = np.where(((d6 >= 0) & (d5 <= d6))[..., None], c, q)
        t = d1 / np.where(d1 - d3 == 0, 1e-300, d1 - d3)
        q = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None], a + ab * t[..., None], q)
        t = d2 / np.where(d2 - d6 == 0, 1e-300, d2 - d6)
        q = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None], a + ac * t[..., None], q)
        t = (d4 - d3) / np.where((d4 - d3) + (d5 - d6) == 0, 1e-300, (d4 - d3) + (d5 - d6))
        q = np.where(((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0))[..., None], b + (c - b) * t[..., None], q)
        best[lo:lo + step] = np.sqrt(((p - q) ** 2).sum(-1)).min(axis=1)
    return best

class TrimmedCad:
    """Distance to the trimmed faces of the model that is current in Gmsh."""
    def __init__(self, model, margin):
        self.model = model
        self.margin = margin
        self.faces = [t for _d, t in model.getEntities(2)]
        self.box = {}
        self.curves = {}
        for f in self.faces:
            x0, y0, z0, x1, y1, z1 = model.getBoundingBox(2, f)
            self.box[f] = (np.array([x0, y0, z0]), np.array([x1, y1, z1]))
            self.curves[f] = sorted({abs(t) for _d, t in model.getBoundary([(2, f)], False, False, False)})
        self.stats = {'points': 0, 'outside_trimmed_face': 0, 'no_valid_face': 0}

    def _pass(self, pts, best, via, margin):
        model = self.model
        for f in self.faces:
            lo, hi = self.box[f]
            if margin is None:
                near = np.arange(len(pts))
            else:
                near = np.nonzero(np.all((pts >= lo - margin) & (pts <= hi + margin), axis=1))[0]
            if not len(near):
                continue
            sub = pts[near]
            closest, param = model.getClosestPoint(2, f, sub.reshape(-1).tolist())
            closest = np.asarray(closest, dtype=float).reshape(-1, 3)
            param = np.asarray(param, dtype=float).reshape(-1, 2)
            carrier = np.sqrt(((sub - closest) ** 2).sum(axis=1))
            # a trimmed distance is never less than the carrier distance, so a
            # face whose carrier is already farther than the best cannot win
            todo = np.nonzero(carrier < best[near])[0]
            if not len(todo):
                continue
            outside = []
            for k in todo:
                if model.isInside(2, f, param[k].tolist(), True):
                    if carrier[k] < best[near[k]]:
                        best[near[k]] = carrier[k]
                        via[near[k]] = False
                else:
                    outside.append(k)
            if outside:
                outside = np.asarray(outside)
                cand = np.full(len(outside), np.inf)
                for c in self.curves[f]:
                    cc, _p = model.getClosestPoint(1, c, sub[outside].reshape(-1).tolist())
                    cc = np.asarray(cc, dtype=float).reshape(-1, 3)
                    cand = np.minimum(cand, np.sqrt(((sub[outside] - cc) ** 2).sum(axis=1)))
                gain = cand < best[near[outside]]
                best[near[outside[gain]]] = cand[gain]
                via[near[outside[gain]]] = True

    def distance(self, pts):
        pts = np.asarray(pts, dtype=float).reshape(-1, 3)
        best = np.full(len(pts), np.inf)
        via = np.zeros(len(pts), bool)
        self._pass(pts, best, via, self.margin)
        far = np.nonzero(best > self.margin)[0]
        if len(far):
            sub, sub_via = best[far], via[far]
            self._pass(pts[far], sub, sub_via, None)
            best[far], via[far] = sub, sub_via
        self.stats['points'] += int(len(pts))
        self.stats['outside_trimmed_face'] += int(np.count_nonzero(via & np.isfinite(best)))
        self.stats['no_valid_face'] += int(np.count_nonzero(~np.isfinite(best)))
        return best

def settle(distance_at, active, tolerance, max_level, is_tri_of):
    """Refine the lattice of every element in `active` until two levels agree.

    `distance_at(indices, level)` returns the largest distance found on each
    listed element's lattice of 2**level subdivisions.  Lattices are nested,
    so the value never decreases.  Returns (deviation, converged, level).
    """
    count = int(max(active) + 1) if len(active) else 0
    deviation = np.full(count, np.nan)
    converged = np.zeros(count, bool)
    level = np.zeros(count, np.int32)
    previous = np.full(count, np.nan)
    active = np.asarray(active, dtype=np.int64)
    for k in range(1, int(max_level) + 1):
        if not len(active):
            break
        now = distance_at(active, k)
        deviation[active] = now
        level[active] = k
        if k > 1:
            done = np.abs(now - previous[active]) <= tolerance
            converged[active[done]] = True
            keep = ~done
        else:
            keep = np.ones(len(active), bool)
        previous[active] = now
        active = active[keep]
    return deviation, converged, level

def read_surface_mesh(only):
    tags, coords, _ = gmsh.model.mesh.getNodes()
    tags = np.asarray(tags, dtype=np.int64)
    coords = np.asarray(coords, dtype=float).reshape(-1, 3)
    lookup = np.full(int(tags.max()) + 1 if len(tags) else 1, -1, np.int64)
    if len(tags):
        lookup[tags] = np.arange(len(tags))
    phys = {}
    for dim, tag in gmsh.model.getPhysicalGroups():
        for entity in gmsh.model.getEntitiesForPhysicalGroup(dim, tag):
            phys.setdefault((dim, int(entity)), tag)
    blocks = []
    for _d, entity in gmsh.model.getEntities(2):
        etypes, etags, enodes = gmsh.model.mesh.getElements(2, entity)
        group = phys.get((2, entity), 0)
        name = gmsh.model.getPhysicalName(2, group) if group else ''
        if only and name not in only:
            continue
        for etype, el, nodes in zip(etypes, etags, enodes):
            props = gmsh.model.mesh.getElementProperties(int(etype))
            if not props[0].startswith(('Triangle', 'Quadrangle')):
                continue
            size = int(props[3])
            nodes = np.asarray(nodes, dtype=np.int64).reshape(-1, size)
            blocks.append({'type': int(etype), 'tri': props[0].startswith('Triangle'),
                           'order': int(props[2]), 'tags': np.asarray(el, dtype=np.int64),
                           'xyz': coords[lookup[nodes]], 'entity': entity,
                           'physical': group})
    return blocks, coords

def sample(block, rows, n):
    """Points on the (possibly curved) elements `rows` of a block, lattice n."""
    local = lattice(block['tri'], n)
    nb, basis, _o = gmsh.model.mesh.getBasisFunctions(block['type'], local.reshape(-1).tolist(), 'Lagrange')
    basis = np.asarray(basis, dtype=float).reshape(len(local), -1)
    return np.einsum('pn,enx->epx', basis, block['xyz'][rows])

def main(job):
    gmsh.initialize([], readConfigFiles=False, run=False)
    gmsh.option.setNumber('General.Terminal', 0)
    out = {'error': None}
    try:
        gmsh.model.add('gm12_mesh')
        gmsh.merge(job['mesh'])
        blocks, coords = read_surface_mesh(set(job.get('only') or []))
        if not blocks:
            raise ValueError('the mesh has no surface elements to measure')
        names = {'%d:%d' % (d, t): gmsh.model.getPhysicalName(d, t) for d, t in gmsh.model.getPhysicalGroups()}
        box = np.concatenate([coords.min(axis=0), coords.max(axis=0)])
        diagonal = float(np.linalg.norm(box[3:] - box[:3])) or 1.0
        tolerance = job.get('tolerance')
        sampling = job.get('sampling_tolerance')
        if sampling is None:
            sampling = (job['share'] * tolerance) if tolerance else 1e-4 * diagonal
        ref = job['reference']
        if ref['kind'] == 'cad':
            if job.get('occ_target_unit') is not None:
                gmsh.option.setString('Geometry.OCCTargetUnit', job['occ_target_unit'])
            gmsh.model.add('gm12_cad')
            gmsh.model.occ.importShapes(ref['path'])
            gmsh.model.occ.synchronize()
            measure = TrimmedCad(gmsh.model, 0.05 * diagonal).distance
            kind = 'trimmed CAD faces'
        else:
            tris = np.load(ref['npz'])['tris']
            measure = lambda p: triangle_distance(np.asarray(p).reshape(-1, 3), tris)
            kind = 'tessellated reference (triangles)'
        # sampling positions use the mesh basis, not the current model
        result = []
        for block in blocks:
            count = len(block['tags'])
            def distance_at(rows, level, block=block):
                best = np.zeros(len(rows))
                for lo in range(0, len(rows), 4000):
                    part = rows[lo:lo + 4000]
                    pts = sample(block, part, 2 ** level)
                    d = measure(pts.reshape(-1, 3)).reshape(len(part), -1)
                    best[lo:lo + 4000] = d.max(axis=1)
                return best
            dev, conv, lev = settle(distance_at, np.arange(count), sampling, job['max_level'], None)
            result.append((block, dev, conv, lev))
        cols = {k: np.concatenate([np.full(len(r[0]['tags']), r[0][k]) if k != 'tags' else r[0]['tags'] for r in result])
                for k in ('tags', 'entity', 'physical', 'type', 'order')}
        cols['deviation'] = np.concatenate([r[1] for r in result])
        cols['converged'] = np.concatenate([r[2] for r in result])
        cols['level'] = np.concatenate([r[3] for r in result])
        np.savez(job['out'] + '/deviation.npz', **cols)
        stats = getattr(measure, '__self__', None)
        out.update(
            names=names, distance=kind, sampling_tolerance=float(sampling),
            tolerance=tolerance, max_level=int(job['max_level']),
            elements=int(len(cols['tags'])), converged=int(cols['converged'].sum()),
            version=gmsh.option.getString('General.Version'),
            orders=sorted({int(o) for o in cols['order']}),
            cad=dict(stats.stats) if stats is not None else {})
    except Exception as error:
        out['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        gmsh.finalize()
    with open(job['out'] + '/deviation.json', 'w') as handle:
        json.dump(out, handle)
    print('RESULT ' + json.dumps(out))

if __name__ == '__main__':
    main(json.load(open(sys.argv[1])))
'''


class DeviationError(RuntimeError):
    """The deviation could not be measured; the sentence says why."""


def worker_namespace(gmsh_module) -> dict:
    """The worker's functions with *gmsh_module* standing in for ``gmsh`` (tests)."""
    import sys

    saved = sys.modules.get('gmsh')
    sys.modules['gmsh'] = gmsh_module
    try:
        space: dict = {'__name__': 'gm12_worker'}
        exec(compile(DEVIATION_WORKER, '<gm12 deviation worker>', 'exec'), space)
    finally:
        if saved is None:
            sys.modules.pop('gmsh', None)
        else:
            sys.modules['gmsh'] = saved
    return space


def sampling_tolerance(tolerance_model_units=None, diagonal=None) -> float:
    """The declared sampling tolerance: a tenth of the deviation tolerance.

    With no deviation tolerance, a ten-thousandth of the model's diagonal.
    """
    if tolerance_model_units:
        return SAMPLING_SHARE * abs(float(tolerance_model_units))
    return 1e-4 * float(diagonal or 1.0)


def deviation_availability(*, has_mesh: bool, session_reference=False,
                           stored_report=False, geometry_reference=False,
                           cad_reference=False) -> tuple:
    """``(enabled, reason)`` for the Surface deviation button.

    Plan 4.10.1 (1): a Gmsh session's own reference (``gmshSessionReference()``)
    and a CAD file enable the tool exactly as a stored fidelity report or a
    loaded geometry do; the reason names what to do when none is there.
    """
    if not has_mesh:
        return False, 'No mesh is loaded'
    if session_reference or cad_reference or stored_report or geometry_reference:
        return True, ''
    return False, ('This mesh has no reference to be measured against. Import '
                   'the geometry it was made from, run the geometry fidelity '
                   'task, or give a CAD file as the reference.')


def latest_report(roots):
    """The newest stored fidelity report across *roots* (case root, native store).

    Plan 4.10.1 (2): the lookup used ``caseRoot()`` only, so a Gmsh project
    whose reports sit in the native store never found one.
    """
    from foammesh.core.quality.geometry_fidelity import report as fidelity

    best = None
    for root in roots or ():
        if root is None:
            continue
        found = fidelity.latest(root)
        if found is None:
            continue
        stamp = 0.0
        for task in fidelity.FIDELITY_TASK_IDS:
            try:
                stamp = max(stamp, (Path(root) / fidelity.REPORT_PATHS[task]
                                    ).stat().st_mtime)
            except OSError:
                continue
        if best is None or stamp > best[0]:
            best = (stamp, found)
    return best[1] if best else None


def surface_deviation(mesh, reference, *, tolerance_mm=None, unit='m',
                      sampling_tolerance_model=None, max_level=5, only=None,
                      occ_target_unit=None, work=None, profile=None,
                      runner=None) -> dict:
    """Deviation of the surface elements of a mesh file from a reference.

    *mesh* is a ``.msh`` (any order; a 2D-only mesh is fine) or an SU2 mesh.
    *reference* is a CAD file (``.brep``/``.step``, measured to the trimmed
    faces) or a vtkPolyData of triangles (measured to the triangles). The
    answer is in millimetres: ``fields`` is ``{patch: array}`` in the order
    of ``elements``; ``sampling`` states the tolerance the high-order sampling
    settled to, the levels and how many elements did not settle.
    """
    import tempfile
    import numpy as np
    from foammesh.core.gmsh.element_quality import (
        QualityFileError, run_worker, su2_to_msh, _profile)
    from foammesh.core.quality.geometry_fidelity.live_distance import unit_to_mm

    mesh = Path(mesh).resolve()
    if not mesh.is_file():
        raise DeviationError('%s: no such file' % mesh.name)
    folder = (Path(work) if work else Path(tempfile.mkdtemp(prefix='gm12d'))
              ).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    kind_of_mesh = 'su2' if mesh.suffix.lower() == '.su2' else 'msh'
    source = su2_to_msh(mesh, folder / 'from_su2.msh') if kind_of_mesh == 'su2' \
        else mesh
    factor = unit_to_mm(unit)                  # mm per model unit
    tolerance = (abs(float(tolerance_mm)) / factor
                 if tolerance_mm else None)
    try:
        profile = _profile(profile) if runner is None else profile
        to_rt = (profile.translate_host_path if profile is not None
                 else (lambda p: str(p)))
        if isinstance(reference, (str, Path)):
            ref = Path(reference).resolve()
            if not ref.is_file():
                raise DeviationError('%s: no such file' % ref.name)
            description = {'kind': 'cad', 'path': to_rt(ref)}
        else:
            from vtkmodules.util.numpy_support import vtk_to_numpy
            from vtkmodules.vtkFiltersCore import vtkTriangleFilter
            tri = vtkTriangleFilter()
            tri.SetInputData(reference)
            tri.Update()
            poly = tri.GetOutput()
            points = vtk_to_numpy(poly.GetPoints().GetData()).astype(float)
            ids = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()
                               ).reshape(-1, 3)
            np.savez(folder / 'reference.npz', tris=points[ids])
            description = {'kind': 'triangles',
                           'npz': to_rt(folder / 'reference.npz')}
        job = {'mesh': to_rt(source), 'reference': description,
               'tolerance': tolerance, 'share': SAMPLING_SHARE,
               'sampling_tolerance': sampling_tolerance_model,
               'max_level': int(max_level), 'only': list(only or []),
               'occ_target_unit': occ_target_unit, 'out': to_rt(folder)}
        if runner is None:
            report = run_worker(DEVIATION_WORKER, job, folder, profile=profile)
        else:
            report = runner(job, folder)
    except QualityFileError as error:
        raise DeviationError(str(error))
    if report.get('error'):
        raise DeviationError('%s: %s' % (mesh.name, report['error']))
    table = np.load(folder / 'deviation.npz')
    names = report.get('names') or {}
    physical = np.asarray(table['physical'])
    entity = np.asarray(table['entity'])
    deviation = np.asarray(table['deviation'], dtype=float) * factor
    labels = np.empty(len(physical), dtype=object)
    for p, e in sorted({(int(p), int(e)) for p, e in zip(physical, entity)}):
        pick = (physical == p) & (entity == e)
        labels[pick] = ((names.get('2:%d' % p) or 'surface%d' % p) if p
                        else 'surface%d' % e)
    fields = {}
    index = {}
    for label in sorted(set(labels.tolist())):
        mask = labels == label
        fields[label] = deviation[mask]
        index[label] = np.nonzero(mask)[0]
    level = np.asarray(table['level'])
    converged = np.asarray(table['converged'], dtype=bool)
    sampling_model = float(report['sampling_tolerance'])
    return {
        'unit': 'mm', 'model_unit_mm': factor,
        'distance': report['distance'],
        'signed': False,
        'fields': fields, 'index': index,
        'elements': {'tag': np.asarray(table['tags']),
                     'entity': entity, 'physical': physical,
                     'type': np.asarray(table['type']),
                     'order': np.asarray(table['order']),
                     'level': level, 'converged': converged},
        'tolerance_mm': float(tolerance_mm) if tolerance_mm else None,
        'sampling': {
            'tolerance_mm': sampling_model * factor,
            'rule': ('a tenth of the deviation tolerance'
                     if tolerance else 'a ten-thousandth of the model size'),
            'max_level': int(report['max_level']),
            'levels_used': int(level.max()) if level.size else 0,
            'elements': int(level.size),
            'converged': int(converged.sum()),
            'unconverged': int(level.size - converged.sum()),
            'orders': report.get('orders') or [],
        },
        'cad': report.get('cad') or {},
        'mesh': {'file': str(mesh), 'format': kind_of_mesh},
        'measured_by': 'Gmsh %s' % report.get('version', ''),
    }


def deviation_summary(result: dict) -> dict:
    """Max, mean, RMS and the share within the tolerance, per patch and overall."""
    import numpy as np

    tol = result.get('tolerance_mm')
    rows = {}
    every = []
    for name, values in result['fields'].items():
        values = np.asarray(values, dtype=float)
        finite = values[np.isfinite(values)]
        every.append(finite)
        rows[name] = _spread_row(finite, values.size, tol)
    whole = np.concatenate(every) if every else np.zeros(0)
    return {'unit': 'mm', 'tolerance_mm': tol,
            'total': _spread_row(whole, int(sum(len(v) for v in
                                                result['fields'].values())), tol),
            'patches': rows}


def _spread_row(finite, count, tol) -> dict:
    import numpy as np

    if not finite.size:
        return {'elements': int(count), 'measured': 0, 'max': None,
                'mean': None, 'rms': None, 'within': None}
    return {'elements': int(count), 'measured': int(finite.size),
            'max': float(finite.max()), 'mean': float(finite.mean()),
            'rms': float(np.sqrt(np.mean(finite * finite))),
            'within': (float(np.count_nonzero(finite <= tol) / finite.size)
                       if tol else None)}


def deviation_readout(result: dict, scale=None):
    """The result as the ``DeviationReadout`` the shared panel and legend take."""
    import numpy as np
    from foammesh.core.quality.geometry_fidelity import live_distance as live

    fields = {n: np.asarray(v, dtype=float) for n, v in result['fields'].items()}
    tolerance = float(result.get('tolerance_mm') or live.default_tolerance(
        extent=None, unit='mm'))
    top = float(scale) if scale else live.absolute_scale(fields.values())
    rng = live.scale_range(top, False)
    note = ('Distance to the trimmed CAD faces, unsigned. High-order faces '
            'sampled to %.3g mm.' % result['sampling']['tolerance_mm']
            if result['distance'].startswith('trimmed')
            else 'Distance to the tessellated reference, unsigned.')
    if result['sampling']['unconverged']:
        note += ' %s did not settle within the sampling tolerance.' \
            % count_text(result['sampling']['unconverged'], 'element')
    unavailable = {name: 'No element of this patch could be measured: '
                   'the reference has no surface near it.'
                   for name, values in fields.items()
                   if not np.isfinite(values).any()}
    readout = live.DeviationReadout(
        fields=fields, value_range=rng,
        lookup_table=live.deviation_lut(top, tolerance, False),
        source='live', signed=False, scale=top, tolerance=tolerance,
        notice=note,
        surfaces=tuple((name, True, True, '') for name in fields),
        signed_fields=None)
    readout.unavailable = unavailable
    return readout
