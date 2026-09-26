#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Aggregate geometry health: findings + a 0-100 mesh-readiness score."""
from __future__ import annotations

from dataclasses import dataclass, field

from .checks import Finding, Severity, check_all, is_watertight
from .readiness import (ReadinessReport, classify, conjugate_assembly,
                        explain_conjugate)


@dataclass
class GeometryHealth:
    findings: list[Finding] = field(default_factory=list)
    watertight: bool = False
    score: int = 0
    readiness: ReadinessReport | None = None

    @property
    def verdict(self) -> str:
        if self.score >= 90:
            return 'Ready: geometry looks mesh-ready.'
        if self.score >= 60:
            return 'Usable with caution: review the warnings before meshing.'
        return 'Not ready: fix the errors before meshing.'

    def to_dict(self) -> dict:
        return {
            'score': self.score,
            'watertight': self.watertight,
            'verdict': self.verdict,
            'readiness': self.readiness.to_dict() if self.readiness else None,
            'findings': [
                {'kind': f.kind, 'count': f.count,
                 'severity': f.severity.value, 'message': f.message,
                 'locations': [list(point) for point in f.locations],
                 'characteristic_size': f.characteristic_size,
                 'characteristic_unit': f.characteristic_unit,
                 'repairable_by': list(f.repairable_by),
                 'engine_impact': dict(f.engine_impact or {}),
                 'evaluated': f.evaluated, 'details': dict(f.details or {})}
                for f in self.findings
            ],
        }


def score_for(findings, *, watertight: bool) -> int:
    """The 0-100 score a finding set earns. One rule, so callers agree.

    DP-434. ``diagnose`` used to score in two passes -- the tessellation
    findings here, the CAD ones subtracted afterwards -- which was arithmetic
    that happened to come out right and had nowhere to put a re-grading. It is
    one pass now, taken after every finding is in hand.

    A proven conjugate assembly is not docked for the merged surface being
    open. ``watertight`` stays the measurement it is, because other readers
    want the fact; what changes is that the fact stops costing twenty points
    on a geometry whose openness is exactly the interface it was built to
    carry, and whose readiness state already reads `ready`.
    """
    score = 100
    for finding in findings:
        if finding.count <= 0:
            continue
        if finding.severity is Severity.ERROR:
            score -= 40
        elif finding.severity is Severity.WARNING:
            score -= 10
    if not watertight and conjugate_assembly(findings) is None:
        score -= 20
    return max(0, min(100, score))


def assess(polydata, *, target_cell_size: float | None = None,
           budget=None, source_file=None,
           engine: str | None = None) -> GeometryHealth:
    """Score a surface's readiness.

    ``budget`` carries the cancellation flag and progress channel for the whole
    assessment; passing ``None`` gives each bounded check its own default
    allowance, which is what non-interactive callers want.

    ``source_file`` is the artifact this surface came off disk as, and the one
    a mesher will be handed. Passing it lets the report notice facets the read
    dropped (DP-44); leaving it out grades the polydata alone, which is right
    for a surface that has not been written anywhere yet.
    """
    findings = check_all(polydata, target_cell_size=target_cell_size,
                         budget=budget, source_file=source_file)
    # DP-434. A proven conjugate assembly explains three of these, and it has
    # to explain them here, before the score is taken, or the panel and the
    # score contradict the verdict: three errors at -40 apiece is a score of
    # zero and "Not ready: fix the errors before meshing." on a geometry the
    # readiness state calls `ready`. `classify` is given the original findings
    # so its own proof is unaffected by the re-grading.
    graded = explain_conjugate(findings)
    watertight = is_watertight(polydata)
    score = score_for(graded, watertight=watertight)

    import math
    bounds = polydata.GetBounds()
    model_diagonal = (math.sqrt(sum((bounds[i * 2 + 1] - bounds[i * 2]) ** 2 for i in range(3)))
                      if polydata.GetNumberOfPoints() else 0.0)
    readiness = classify(findings, cell_count=int(polydata.GetNumberOfCells()),
                         model_diagonal=model_diagonal, engine=engine)
    return GeometryHealth(
        findings=graded, watertight=watertight, score=score,
        readiness=readiness)
