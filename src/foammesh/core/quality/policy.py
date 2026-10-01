#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""One quality policy: what the mesher may produce, and what QA will accept.

Plan 30 WP-04 (F-25, F-29). Three unrelated threshold tables decided what a
FoamMesh mesh was allowed to be:

* ``openfoam/target.py`` held the *generator* limits -- the
  ``meshQualityControls`` block snappyHexMesh judges every candidate cell
  against, strict ``maxNonOrtho 65`` with a relaxed ``75`` for layer addition;
* ``core/quality/readiness.py`` held the *acceptance* limits the OpenFOAM QA
  verdict is read against, ``70`` and skewness ``4``;
* ``core/quality/su2_readiness.py`` held a second copy of the acceptance
  limits, the same two numbers written again for the SU2 route.

Nothing tied them together, so nothing noticed that the second copy could
drift from the first, and nothing stated the one relation that makes the
numbers mean something:

    strict generator limit  <=  acceptance limit  <=  relaxed generator limit
                       65   <=        70          <=          75

That ordering is the policy. The mesher is held to a limit stricter than the
one QA applies, so an ordinary cell passes both; the relaxed band above the
acceptance limit is the width of the disagreement -- cells snappy is allowed
to keep while adding layers that QA will then call out. Written down in one
place, that band is a decision. Split across three modules it was an accident.

The numbers themselves are unchanged by this consolidation. Every one of them
was verified against OpenFOAM 13 (the generator defaults are the Foundation
tutorial values; ``checkMesh`` itself calls a face severely non-orthogonal
past 70), so WP-04 moves them rather than re-picking them, and the
:func:`QualityPolicy.check` invariant is what stops a later edit from breaking
the ordering silently.

Pure data -- no Qt, no OpenFOAM process, no database. The database *defaults*
are asserted equal to this module by a test rather than generated from it,
because the schema is a project-file contract that must not move when this
module is edited.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

#: The ``meshQualityControls`` block, OpenFOAM Foundation 13 defaults. Values
#: are strings because they are written straight into a dictionary file.
GENERATOR_LIMITS: Mapping[str, str] = MappingProxyType({
    'maxNonOrtho': '65',
    'maxBoundarySkewness': '20',
    'maxInternalSkewness': '4',
    'maxConcave': '80',
    'minVol': '-1e30',
    'minTetQuality': '1e-15',
    'minVolCollapseRatio': '-1',
    'minArea': '-1',
    'minTwist': '0.02',
    'minDeterminant': '0.001',
    'minFaceWeight': '0.05',
    'minVolRatio': '0.01',
    'nSmoothScale': '4',
    'errorReduction': '0.75',
})

#: The ``relaxed`` sub-dictionary snappyLayerDriver merges over the strict one
#: after ``nRelaxedIter``. Only ``maxNonOrtho`` carries a shipped default; a
#: key left out here keeps its strict value, which is OpenFOAM's own rule.
GENERATOR_RELAXED_LIMITS: Mapping[str, str] = MappingProxyType({
    'maxNonOrtho': '75',
})

#: Every ``meshQualityControls`` leaf a user can edit, in dictionary order.
#: The mesh-quality menu dialog and the QA page both build their editors from
#: this tuple, which is what makes them one record rather than two (F-29).
EDITABLE_LIMITS: tuple[str, ...] = (
    'maxNonOrtho',
    'maxBoundarySkewness',
    'maxInternalSkewness',
    'maxConcave',
    'minVol',
    'minTetQuality',
    'minVolCollapseRatio',
    'minArea',
    'minTwist',
    'minDeterminant',
    'minFaceWeight',
    'minVolRatio',
    # Plan 37 UF15. The one limit with no shipped value: see OPTIONAL_LIMITS.
    'minFaceFlatness',
    'nSmoothScale',
    'errorReduction',
)

#: Plan 37 UF15. Strict limits OpenFOAM ships no value for. OpenFOAM 13 runs
#: the flatness check only when the key is present (src/meshCheck/checkMesh.C
#: 78-83), so an empty editor box stores nothing, writes nothing and leaves
#: the check off -- the dictionary every existing project already wrote.
OPTIONAL_LIMITS: tuple[str, ...] = ('minFaceFlatness',)

#: Relaxed limits snappyHexMesh does not inherit from the strict block. Every
#: other relaxed limit is looked up recursively, so an empty relaxed box means
#: the strict value governs; ``minFaceFlatness`` is found without recursion,
#: so an empty relaxed box means no flatness check in the relaxed phase.
NOT_INHERITED_BY_RELAXED: tuple[str, ...] = ('minFaceFlatness',)

#: ``mergeTolerance`` sits beside the quality block in the same dictionary and
#: in the same editors, but it is not a quality limit and is not relaxed.
EDITABLE_TOLERANCES: tuple[str, ...] = ('mergeTolerance',)

#: The limits that have a relaxed counterpart. ``nSmoothScale`` and
#: ``errorReduction`` are smoothing controls, not per-cell tests, so
#: snappyHexMesh reads them from the strict block only.
RELAXABLE_LIMITS: tuple[str, ...] = tuple(
    name for name in EDITABLE_LIMITS
    if name not in ('nSmoothScale', 'errorReduction'))

#: The relaxed limits OpenFOAM ships a value for. Every other relaxed key is
#: optional: left unset it is not written, and snappyLayerDriver then falls
#: back to the strict limit, which is what an empty editor box means.
RELAXED_WITH_DEFAULT: tuple[str, ...] = ('maxNonOrtho',)

#: Storage root of the record both editors write.
STORAGE_ROOT = 'meshQuality'


def storage_paths() -> tuple[str, ...]:
    """Every ``configurations`` path the quality editors read and write.

    One list, in one order, so "the dialog and the page edit the same record"
    is a statement a test can check rather than a claim about two files.
    """
    strict = tuple(f'{STORAGE_ROOT}/{name}'
                   for name in EDITABLE_LIMITS + EDITABLE_TOLERANCES)
    relaxed = tuple(f'{STORAGE_ROOT}/relaxed/{name}'
                    for name in RELAXABLE_LIMITS)
    return strict + relaxed


@dataclass(frozen=True)
class AcceptanceLimits:
    """What QA calls acceptable in a finished mesh, per target solver."""

    max_non_ortho: float
    max_skewness: float
    #: Why these numbers and not others -- carried so a report can quote it.
    rationale: str

    def as_dict(self) -> dict:
        return {'max_non_ortho': self.max_non_ortho,
                'max_skewness': self.max_skewness,
                'rationale': self.rationale}


_OPENFOAM_ACCEPTANCE = AcceptanceLimits(
    max_non_ortho=70.0, max_skewness=4.0,
    rationale=(
        "OpenFOAM's own checkMesh calls a face severely non-orthogonal past "
        '70 degrees and severely skewed past 4.'))

#: SU2 gets the same two numbers, and it is the same reason rather than a
#: coincidence: the gradient reconstruction has the same difficulty with the
#: same geometry. Kept as a separate entry so a future SU2-specific limit has
#: somewhere to go that is not a second copy of the OpenFOAM one.
_SU2_ACCEPTANCE = AcceptanceLimits(
    max_non_ortho=70.0, max_skewness=4.0,
    rationale=(
        "SU2's gradient reconstruction degrades on the same geometry "
        "OpenFOAM's checkMesh flags, so the checkMesh limits carry over."))

_ACCEPTANCE_BY_SOLVER: Mapping[str, AcceptanceLimits] = MappingProxyType({
    'openfoam': _OPENFOAM_ACCEPTANCE,
    'su2': _SU2_ACCEPTANCE,
    '': _OPENFOAM_ACCEPTANCE,
    'unselected': _OPENFOAM_ACCEPTANCE,
})


class QualityPolicyError(ValueError):
    """The generator and acceptance limits contradict each other."""


@dataclass(frozen=True)
class QualityPolicy:
    """The generator limits and the acceptance limits, as one decision."""

    target_solver: str
    generator: Mapping[str, str]
    generator_relaxed: Mapping[str, str]
    acceptance: AcceptanceLimits

    @classmethod
    def for_solver(cls, target_solver=None) -> 'QualityPolicy':
        """The policy in force for a target solver.

        ``target_solver`` may be the enum, its value, or ``None``; an
        unselected or unknown solver gets the OpenFOAM policy, because that is
        the route a case takes until it says otherwise.
        """
        token = str(getattr(target_solver, 'value', target_solver) or '').lower()
        acceptance = _ACCEPTANCE_BY_SOLVER.get(token, _OPENFOAM_ACCEPTANCE)
        policy = cls(target_solver=token or 'unselected',
                     generator=GENERATOR_LIMITS,
                     generator_relaxed=GENERATOR_RELAXED_LIMITS,
                     acceptance=acceptance)
        policy.check()
        return policy

    @classmethod
    def for_case(cls, db) -> 'QualityPolicy':
        """The policy in force for a project, read from ``mesh/targetSolver``."""
        from foammesh.core.engine.registry import configured_target_solver
        return cls.for_solver(configured_target_solver(db))

    def check(self) -> None:
        """Assert the one relation the three tables never stated.

        Strict generator limit <= acceptance limit <= relaxed generator limit.
        A mesher held to a *looser* limit than QA applies would produce cells
        QA rejects as a matter of course; an acceptance limit above the
        relaxed band would accept anything the mesher can make and so measure
        nothing.
        """
        strict = float(self.generator['maxNonOrtho'])
        relaxed = float(self.generator_relaxed.get('maxNonOrtho', strict))
        limit = self.acceptance.max_non_ortho
        if not strict <= limit <= relaxed:
            raise QualityPolicyError(
                'mesh quality policy is inconsistent: snappyHexMesh is held to '
                f'maxNonOrtho {strict:g} (relaxed {relaxed:g}) while QA accepts '
                f'up to {limit:g}; the acceptance limit must sit between them')
        skew = float(self.generator['maxInternalSkewness'])
        if self.acceptance.max_skewness < skew:
            raise QualityPolicyError(
                'mesh quality policy is inconsistent: QA accepts skewness up '
                f'to {self.acceptance.max_skewness:g} while the mesher is '
                f'allowed {skew:g}')

    def mesh_quality_controls(self, overrides: Mapping | None = None) -> dict:
        """The ``meshQualityControls`` dictionary block, overrides merged in.

        Unknown override keys are kept -- snappyHexMesh tolerates extra
        quality keys -- but every shipped default is present, so the written
        block is always complete.
        """
        controls = dict(self.generator)
        if overrides:
            controls.update({key: str(value)
                             for key, value in overrides.items()
                             if value is not None})
        controls['relaxed'] = dict(self.generator_relaxed)
        return controls

    def to_dict(self) -> dict:
        return {
            'target_solver': self.target_solver,
            'generator': dict(self.generator),
            'generator_relaxed': dict(self.generator_relaxed),
            'acceptance': self.acceptance.as_dict(),
        }


#: The default policy, for the many callers that have no case in hand.
DEFAULT_POLICY = QualityPolicy.for_solver(None)
