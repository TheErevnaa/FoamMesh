"""Gmsh's binding of the shared Plan 23 qualification pages.

Two-line subclasses, matching every other page in this package. The behaviour
lives in :mod:`foammesh.view.qualification_pages` and is not repeated; what is
declared here is only which engine's workflow these pages transition, so the
registry holds real classes and a page can never be mounted against the wrong
engine by omission.
"""
from __future__ import annotations

from foammesh.view.qualification_pages import (
    GeometryFidelityPage, NativeFidelityPage, QualificationSummaryPage,
    ReferenceReadinessPage, ResolutionAdequacyPage,
)


class GmshReferenceReadinessPage(ReferenceReadinessPage):
    engine_id = 'gmsh'


class GmshNativeFidelityPage(NativeFidelityPage):
    engine_id = 'gmsh'


class GmshGeometryFidelityPage(GeometryFidelityPage):
    engine_id = 'gmsh'


class GmshResolutionAdequacyPage(ResolutionAdequacyPage):
    engine_id = 'gmsh'


class GmshQualificationSummaryPage(QualificationSummaryPage):
    engine_id = 'gmsh'


GMSH_QUALIFICATION_PAGES = {
    'common.reference_readiness': GmshReferenceReadinessPage,
    'gmsh.fidelity_native': GmshNativeFidelityPage,
    'common.fidelity': GmshGeometryFidelityPage,
    'common.resolution': GmshResolutionAdequacyPage,
    'common.summary': GmshQualificationSummaryPage,
}
