"""Snappy's binding of the shared Plan 23 qualification pages.

Plan 26 WP5.0. These five tasks are declared in ``SNAPPY_WORKFLOW`` and were
absent from the tree: ``common.reference_readiness``, ``snappy.fidelity_snap``,
``common.fidelity``, ``common.resolution`` and ``common.summary``. Four are
``run_gated``, three ``accepts_override``, and they gate ``common.export``
through ``depends_on`` -- so their absence turned an export refusal into a dead
end that named tasks the user could not see, run, or waive.

Gmsh already mounted its equivalents, and the behaviour is engine-neutral, so
these are the same classes with the engine named. Two-line subclasses rather
than a dict of the base classes, so the registry holds real classes and a page
cannot be mounted against the wrong engine by omission -- a snappy page
inheriting the base default would send its transitions to whichever engine the
base happens to name.
"""
from __future__ import annotations

from foammesh.view.qualification_pages import (
    GeometryFidelityPage, QualificationSummaryPage, ReferenceReadinessPage,
    ResolutionAdequacyPage, SnapFidelityPage,
)


class SnappyReferenceReadinessPage(ReferenceReadinessPage):
    engine_id = 'snappy'


class SnappySnapFidelityPage(SnapFidelityPage):
    """GF1, the blocking gate.

    ``SnapFidelityPage`` was written for this task and had no registry entry on
    either engine, so the one page whose absence left a *blocking* refusal
    unactionable already existed and was mounted nowhere.
    """

    engine_id = 'snappy'


class SnappyGeometryFidelityPage(GeometryFidelityPage):
    engine_id = 'snappy'


class SnappyResolutionAdequacyPage(ResolutionAdequacyPage):
    engine_id = 'snappy'


class SnappyQualificationSummaryPage(QualificationSummaryPage):
    engine_id = 'snappy'


SNAPPY_QUALIFICATION_PAGES = {
    'common.reference_readiness': SnappyReferenceReadinessPage,
    'snappy.fidelity_snap': SnappySnapFidelityPage,
    'common.fidelity': SnappyGeometryFidelityPage,
    'common.resolution': SnappyResolutionAdequacyPage,
    'common.summary': SnappyQualificationSummaryPage,
}
