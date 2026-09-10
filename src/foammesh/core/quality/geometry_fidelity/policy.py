"""Which tolerance applies here, and what happens when none does.

Plan 23 §6. Every fidelity verdict is a deviation divided by a tolerance, and
this module answers the second half. It exists because the WP8 corpus showed
what happens when the denominator is chosen by convenience instead: the coarse
finned heat sink, which retained 4 of 24 fins, measured a worst fin deviation
of 2.38 mm. Divided by the 1.5 mm fin that is 158% — the fin is absent, not
deviant. Divided by the 349 mm meshing domain it is 0.68%, and the mesh reads
`warning`.

Nothing about the measurement changed between those two numbers. A global
denominator divides a local failure by the ratio of the domain to the feature —
about 230 here — so **the more local the defect, the more thoroughly it is
hidden**, which is precisely backwards for a check whose whole purpose is
catching small lost features.

So the denominator is never a geometric extent. It is the applicable
*tolerance*, resolved by §6's five levels, most specific first:

1. a named feature override
2. a named patch UUID override
3. a boundary-category policy
4. a named region or body policy
5. the project fallback

**And when none of the five applies, the answer is `None`.** Not the model
span, not the local cell size, not a plausible constant. §6 is explicit that no
hidden default may turn an uncalibrated case green, and the reason is visible in
the heat sink: any denominator large enough to be safe for a whole model is
large enough to hide a lost fin. A case with no applicable tolerance gets an
`unrated` verdict and its full raw metrics, so the numbers are all there and the
*claim* is withheld.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

#: §6's levels, most specific first. Order is the rule.
PRECEDENCE = ('feature', 'patch', 'category', 'region', 'project')

#: What a section is told when no level applies.
UNRESOLVED = 'no applicable tolerance'


@dataclass(frozen=True)
class ResolvedTolerance:
    """The tolerance that applies, and which level supplied it."""

    value: float | None
    source: str = UNRESOLVED
    #: The key that matched, for a report that has to explain itself.
    key: str = ''

    @property
    def resolved(self) -> bool:
        return self.value is not None and self.value > 0

    def to_dict(self) -> dict:
        return {'tolerance': self.value, 'source': self.source,
                'key': self.key, 'resolved': self.resolved}


@dataclass(frozen=True)
class TolerancePolicy:
    """The five levels, as data.

    Each mapping is keyed by the identity that level names: feature UUID,
    patch UUID, boundary category, region name. ``project`` is a single value
    and is still an explicit setting — a project that has not set one has no
    level 5, rather than a default nobody chose.
    """

    feature: Mapping[str, float] = field(default_factory=dict)
    patch: Mapping[str, float] = field(default_factory=dict)
    category: Mapping[str, float] = field(default_factory=dict)
    region: Mapping[str, float] = field(default_factory=dict)
    project: float | None = None

    def resolve(self, *, feature_uuid: str = '', patch_uuid: str = '',
                category: str = '', region: str = '') -> ResolvedTolerance:
        """The most specific tolerance that applies, or an unresolved one.

        Levels are tried in order and the first hit wins outright — a coarser
        level never softens a finer one. A user who set 20 µm on a critical
        edge means 20 µm on that edge, whatever the region policy says, and a
        rule that averaged or widened it would silently overrule an explicit
        instruction.
        """
        for level, key in (('feature', feature_uuid), ('patch', patch_uuid),
                           ('category', category), ('region', region)):
            if not key:
                continue
            value = getattr(self, level).get(key)
            if value is not None and float(value) > 0:
                return ResolvedTolerance(float(value), level, str(key))
        if self.project is not None and float(self.project) > 0:
            return ResolvedTolerance(float(self.project), 'project')
        return ResolvedTolerance(None)

    def to_dict(self) -> dict:
        return {'feature': dict(self.feature), 'patch': dict(self.patch),
                'category': dict(self.category), 'region': dict(self.region),
                'project': self.project}


def ratio(deviation: float, tolerance: ResolvedTolerance | float | None):
    """Deviation as a multiple of the applicable tolerance, or ``None``.

    ``None`` propagates rather than defaulting: a caller that receives it must
    report `unrated`, and there is deliberately no numeric value it could pass
    on that would let an unresolved case reach a verdict.
    """
    value = (tolerance.value if isinstance(tolerance, ResolvedTolerance)
             else tolerance)
    if value is None or float(value) <= 0:
        return None
    return float(deviation) / float(value)
