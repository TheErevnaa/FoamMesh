"""Versioned, deterministic geometry-readiness classification."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .checks import Finding, Severity


RULES_VERSION = 1


class ReadinessState(str, Enum):
    READY = 'ready'
    REPAIRABLE = 'repairable'
    WRAP_RECOMMENDED = 'wrap_recommended'
    BLOCKED = 'blocked'


@dataclass(frozen=True)
class ReadinessReport:
    state: ReadinessState
    rules_version: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            'state': self.state.value,
            'rules_version': self.rules_version,
            'reasons': list(self.reasons),
        }


def classify(findings: list[Finding], *, cell_count: int,
             model_diagonal: float | None = None,
             large_hole_fraction: float = 0.5) -> ReadinessReport:
    """Classify findings without I/O or capability-dependent guesses.

    ``model_diagonal`` enables hole-size routing (Appendix A §2): a geometry
    with multiple openings each spanning a large fraction of the model is
    routed to wrapping, since exact hole-fill cannot reliably close it. A
    single planar opening stays ``repairable``.
    """
    active = [finding for finding in findings if finding.count > 0]
    if cell_count <= 0:
        return ReadinessReport(
            ReadinessState.BLOCKED, RULES_VERSION,
            ('Geometry contains no usable surface cells.',))
    # Only kinds that a check actually emits; overlapping/intersecting shells are
    # the exact-repair-is-unreliable signals.
    wrap_kinds = {'self_intersections', 'overlapping_shells'}
    wrapping = [finding for finding in active if finding.kind in wrap_kinds]
    if model_diagonal and model_diagonal > 0:
        for finding in active:
            if finding.kind != 'open_edges':
                continue
            loops = (finding.details or {}).get('loops', ())
            large = [loop for loop in loops
                     if loop.get('bbox_diagonal', 0) > large_hole_fraction * model_diagonal]
            if len(large) >= 2:
                wrapping.append(finding)
    if wrapping:
        return ReadinessReport(
            ReadinessState.WRAP_RECOMMENDED, RULES_VERSION,
            tuple(finding.message for finding in wrapping))
    errors = [finding for finding in active if finding.severity is Severity.ERROR]
    if errors and any(not finding.repairable_by for finding in errors):
        return ReadinessReport(
            ReadinessState.BLOCKED, RULES_VERSION,
            tuple(finding.message for finding in errors))
    repairable = [finding for finding in active if finding.repairable_by]
    if repairable:
        return ReadinessReport(
            ReadinessState.REPAIRABLE, RULES_VERSION,
            tuple(finding.message for finding in repairable))
    return ReadinessReport(ReadinessState.READY, RULES_VERSION, ())
