"""Gmsh workflow page: gmsh.qa -- the shared Quality page, bound to Gmsh.

Plan 42 SG18. The page class is :class:`foammesh.view.quality_page.QualityPage`;
the Gmsh route's own items (SU2 readiness, the gate's further limits, the mesh
statistics tier, the run's warnings) are in :mod:`.quality_items`. This module
keeps the names the rest of the package and its tests import.
"""
from __future__ import annotations

from foammesh.view.quality_page import QualityPage, register_quality_items
from foammesh.view.workflow_controls.checkmesh_findings import (  # noqa: F401
    CheckMeshFindings)
from foammesh.view.quality_panels import (  # Plan 37 UF18
    CheckHighlightsPanel, _CheckMeshGroup)

from .compute_page import GmshComputePage
from .quality_items import GmshQualityItems, Su2ReadinessFindings

register_quality_items('gmsh', GmshQualityItems)


class GmshQaPage(QualityPage):
    """``gmsh.qa``: the Quality page bound to the Gmsh engine."""

    engine_id = 'gmsh'
    task_id_default = 'gmsh.qa'
    run_all_task_id = 'gmsh.compute'
    #: The scalar gate limits; ``extra_limits`` is the table below them.
    extra_field_ids = tuple(
        field_id for field_id in GmshComputePage.GATE_EDITED_ON_QUALITY
        if field_id != 'gmsh.compute.extra_limits')
