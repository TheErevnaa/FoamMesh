#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Solver-readiness verdict from a parsed checkMesh result + v13 thresholds."""
from __future__ import annotations

from dataclasses import dataclass

from .checkmesh_parser import CheckMeshResult
from .policy import DEFAULT_POLICY, QualityPolicy
from foammesh.core.quantities import count_text

# The acceptability thresholds, from the one policy that also fixes the limits
# snappyHexMesh meshes against (Plan 30 F-25). Kept as module constants because
# callers and tests read them by name; they are now views onto the policy
# rather than a table of their own.
MAX_NON_ORTHO_OK = DEFAULT_POLICY.acceptance.max_non_ortho
MAX_SKEWNESS_OK = DEFAULT_POLICY.acceptance.max_skewness


@dataclass
class Readiness:
    ok: bool
    reasons: list[str]


def readiness_verdict(result: CheckMeshResult,
                      policy: QualityPolicy | None = None) -> Readiness:
    """Judge a parsed checkMesh result against the acceptance limits.

    ``policy`` selects the target solver's limits; without one the default
    (OpenFOAM) policy applies, which is what every caller wanted when the
    numbers were literals in this module.
    """
    limits = (policy or DEFAULT_POLICY).acceptance
    reasons: list[str] = []

    if result.mesh_ok is False or (result.failed_checks or 0) > 0:
        reasons.append(
            'checkMesh reported '
            + count_text(result.failed_checks, 'failed check') + '.')
    if result.max_non_ortho is not None and result.max_non_ortho > limits.max_non_ortho:
        reasons.append(
            f'Max non-orthogonality {result.max_non_ortho} > {limits.max_non_ortho}.')
    if result.max_skewness is not None and result.max_skewness > limits.max_skewness:
        reasons.append(f'Max skewness {result.max_skewness} > {limits.max_skewness}.')

    ok = not reasons
    if ok:
        reasons.append('Mesh passes quality thresholds; ready for the solver.')
    return Readiness(ok=ok, reasons=reasons)
