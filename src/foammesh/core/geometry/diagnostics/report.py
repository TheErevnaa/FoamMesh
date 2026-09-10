#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Aggregate geometry health: findings + a 0-100 mesh-readiness score."""
from __future__ import annotations

from dataclasses import dataclass, field

from .checks import Finding, Severity, check_all, is_watertight
from .readiness import ReadinessReport, classify


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
                 'repairable_by': list(f.repairable_by),
                 'engine_impact': dict(f.engine_impact or {}),
                 'evaluated': f.evaluated, 'details': dict(f.details or {})}
                for f in self.findings
            ],
        }


def assess(polydata, *, target_cell_size: float | None = None,
           budget=None) -> GeometryHealth:
    """Score a surface's readiness.

    ``budget`` carries the cancellation flag and progress channel for the whole
    assessment; passing ``None`` gives each bounded check its own default
    allowance, which is what non-interactive callers want.
    """
    findings = check_all(polydata, target_cell_size=target_cell_size,
                         budget=budget)
    watertight = is_watertight(polydata)

    score = 100
    for f in findings:
        if f.severity is Severity.ERROR and f.count > 0:
            score -= 40
        elif f.severity is Severity.WARNING and f.count > 0:
            score -= 10
    if not watertight:
        score -= 20
    score = max(0, min(100, score))

    import math
    bounds = polydata.GetBounds()
    model_diagonal = (math.sqrt(sum((bounds[i * 2 + 1] - bounds[i * 2]) ** 2 for i in range(3)))
                      if polydata.GetNumberOfPoints() else 0.0)
    readiness = classify(findings, cell_count=int(polydata.GetNumberOfCells()),
                         model_diagonal=model_diagonal)
    return GeometryHealth(
        findings=findings, watertight=watertight, score=score,
        readiness=readiness)
