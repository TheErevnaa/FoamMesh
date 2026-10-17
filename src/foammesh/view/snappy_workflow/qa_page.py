"""Snappy workflow page: ``snappy.qa`` -- the values snappy meshes against.

Plan 26 WP5.1. This node resolved to the **Export** widget. Two consequences,
and the second is worse than the first: a user clicking the Quality row got the
export page, and the strict-GUI harness recorded the node as a visited pass
because it resolved to *a* widget, so an empty gap register was compatible with
the defect.

The values themselves were reachable only through the Mesh ▸ Parameters *menu*
dialog -- sixteen, not four: the whole ``meshQualityControls`` block plus
``mergeTolerance`` -- even though ``field_metadata`` declares
``ui_location = workflow.quality`` for a page that did not exist. The menu
dialog stays as a shortcut; this is the page that location always named.
"""
from __future__ import annotations

from foammesh.view.quality_page import (
    QualityItems, QualityPage, register_quality_items)
from foammesh.view.workflow_controls.checkmesh_findings import (  # noqa: F401
    CheckMeshFindings)
from foammesh.view.quality_panels import (            # noqa: F401
    CHECK_MESH_FIELDS, CheckHighlightsPanel, _CheckMeshGroup)

#: Plan 40 UXD9. The limits snappyHexMesh judges every candidate cell against
#: are edited on Snap (strict) and Layers (relaxed); the tuple stays here as
#: the page's list of what it hands to them, so the QA task still binds the
#: record and the page draws none of it.
from .mesher_limits import (                                   # noqa: E402
    MESH_QUALITY_FIELDS, RELAXED_LIMIT_FIELDS, STRICT_LIMIT_FIELDS)

_MESH_QUALITY_IDS = frozenset(MESH_QUALITY_FIELDS)

HELP_DETAIL = (
    'How the finished mesh is checked: these change the verdict and the '
    'evidence, never the mesh. They are checkMesh\'s own command line, '
    'so re-running the check with a lower threshold re-judges the mesh '
    'you already have. Judging against the mesher\'s own limits writes '
    'system/meshQualityDict from the mesher limits on Snap and Layers '
    'and needs the case dictionaries generated again before it takes '
    'effect. The '
    'failed sets are written into constant/polyMesh/sets, with the '
    'point sets under postProcessing/checkMesh; the problem faces reach '
    'postProcessing/checkMesh only as a surface, and those two are what '
    'the check highlights draw.')


class SnappyQualityItems(QualityItems):
    """What the snappyHexMesh route says on the Quality page."""

    help_detail = HELP_DETAIL

    def renders_field(self, field_id: str) -> bool:
        """The limits are edited on Snap and Layers, not here (UXD9).

        The task still binds the record, so it stays part of this task; the
        editors are on the stages that read them, and the ownership gate
        (``tests/unit/test_uxd9_one_place_per_concept.py``) holds each field
        to one editor in the whole workflow.
        """
        return field_id not in _MESH_QUALITY_IDS


register_quality_items('snappy', SnappyQualityItems)


class SnappyQaPage(QualityPage):
    """``snappy.qa``: the Quality page bound to the snappyHexMesh engine."""

    engine_id = 'snappy'
    task_id_default = 'snappy.qa'
    HELP_DETAIL = HELP_DETAIL
