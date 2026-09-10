"""Snappy workflow page: ``snappy.qa`` -- the values snappy meshes against.

Plan 26 WP5.1. This node resolved to the **Export** widget. Two consequences,
and the second is worse than the first: a user clicking "Snappy QA" got the
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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDoubleSpinBox, QScrollArea

from foammesh.view.workflow_controls.checkmesh_findings import (
    CheckMeshFindings,
)
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.workflow_controls.field_group_page import FieldGroupPage

#: The values snappyHexMesh judges every candidate cell against, derived from
#: the one quality policy rather than listed again here.
#:
#: Plan 30 WP-04 (F-29). This was a hand-written tuple of twenty-seven
#: semantic ids, and the Mesh menu dialog held its own hand-written list of
#: the same record's leaves. Two lists, one record: nothing made them agree,
#: so the QA page and the summary page could show different numbers for the
#: same case (R191). Both are now built from
#: ``core/quality/policy.storage_paths()``, so a leaf added to the policy
#: reaches both editors and a leaf missing from the schema is an import-time
#: error rather than a silently absent control.
def _mesh_quality_fields() -> tuple[str, ...]:
    from foammesh.core.facade import FIELD_REGISTRY
    from foammesh.core.quality.policy import storage_paths

    ids = []
    for path in storage_paths():
        descriptor = FIELD_REGISTRY.by_storage_path(path)
        if descriptor is None:
            raise RuntimeError(
                f'mesh quality policy names {path}, which the configurations '
                'schema does not have')
        ids.append(descriptor.id)
    return tuple(ids)


MESH_QUALITY_FIELDS = _mesh_quality_fields()

#: Plan 31 (``checkmesh.thresholds_and_region``, ``checkmesh.write_surfaces``).
#: checkMesh's own command line. Every one of these was a key in the
#: configuration with no control anywhere in the product: the thresholds the
#: verdict is computed against were OpenFOAM's defaults and unreachable, and
#: the two artefact switches were never passed at all.
CHECK_MESH_FIELDS = (
    'quality.check.non_orth_threshold',
    'quality.check.skew_threshold',
    'quality.check.user_defined_checks',
    'quality.check.skip_topology',
    'quality.check.write_surfaces',
)


class SnappyQaPage(EngineTaskPage):
    """The QA task, with the mesh-quality limits it actually judges against."""

    engine_id = 'snappy'
    task_id_default = 'snappy.qa'
    #: The task is run-gated (D23): "Run this step" is checkMesh on the mesh
    #: in the case root, and the only thing that advances the row.
    run_stage = 'checkMesh'

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)

    def build_sections(self, layout) -> None:
        """Mount what checkMesh found, then the limits snappy meshes against."""
        self._findings = CheckMeshFindings(self._client, self)
        layout.addWidget(self._findings)
        self._check = _CheckMeshGroup(self._client, self)
        layout.addWidget(self._check)
        self._quality = _MeshQualityGroup(self._client, self)
        layout.addWidget(self._quality)

    def refresh(self) -> None:
        super().refresh()
        for name in ('_check', '_quality'):
            group = getattr(self, name, None)
            if group is not None:
                group.reload()

    def refresh_status(self) -> None:
        """Re-read the task's state *and* the report that produced it.

        R100: the run finished `warning`, and the finding behind it -- 1,451
        concave cells and one failed mesh check -- appeared on no surface at
        all. The status line moved and the page did not, so the only way to
        learn what the warning was about was to open the checkMesh log.
        """
        super().refresh_status()
        findings = getattr(self, '_findings', None)
        if findings is not None:
            findings.refresh()


class _CheckMeshGroup(FieldGroupPage):
    """How checkMesh is run, as a panel above the limits it may compare with.

    Plan 31. Six keys reached the dictionary layer and no control: a user who
    wanted a stricter non-orthogonality report, or the offending faces written
    out as a surface they could open, had no way to ask for either. The
    defaults here are OpenFOAM 13's own, and a value left at its default
    writes no flag at all, so a case nobody has touched runs the command line
    it has always run.
    """

    token = 'workflow.quality'
    field_ids = CHECK_MESH_FIELDS
    heading = 'Mesh check'
    purpose = (
        'How the finished mesh is checked. These change the verdict and the '
        'evidence, never the mesh: they are checkMesh\'s own command line, '
        'so re-running the check with a lower threshold re-judges the mesh '
        'you already have.')
    caveat = (
        'Judging against the mesher\'s own limits writes '
        'system/meshQualityDict from the values below and needs the case '
        'dictionaries generated again before it takes effect. The problem '
        'faces are always written as sets into constant/polyMesh/sets, which '
        'is what the viewport highlights; the surface is the extra copy, '
        'under postProcessing/checkMesh.')


class _MeshQualityGroup(FieldGroupPage):
    """The ``meshQuality`` values, as a panel inside the QA page."""

    token = 'workflow.quality'
    field_ids = MESH_QUALITY_FIELDS
    heading = 'Mesh quality limits'
    purpose = (
        'Every candidate cell is judged against these. snappyHexMesh reverts '
        'a snap or a layer insertion that would breach one, so they decide '
        'what the mesher is allowed to produce rather than only what is '
        'reported afterwards.')
    caveat = (
        'These are the mesher\'s own limits, written into '
        'snappyHexMeshDict/meshQualityControls. They are not the checkMesh '
        'reporting thresholds above -- but "Judge against the mesher\'s own '
        'limits" makes checkMesh read exactly these values, from a '
        'meshQualityDict written beside the case, so the finished mesh is '
        'held to what the mesher was asked for.')

    def build(self) -> None:
        super().build()
        self._widenNumericEditors()

    def _widenNumericEditors(self) -> None:
        """Give every limit room for the whole number it can hold.

        R102. `Min Vol -100000000000.0000000` was cut mid-digit at the right
        edge of the panel: the spin box is built with nine decimals and, where
        the schema leaves a side unbounded, a range of +/-1e12 -- so its widest
        text is twenty-four characters and nothing had reserved the width for
        them. A quality threshold the user cannot read in full is one they
        cannot check, and these particular numbers are the ones snappy reverts
        cells against.

        The width comes from the widest value the box can actually display,
        measured in its own font, and the group's scroll area is allowed a
        horizontal bar for the case where the panel is still narrower. It is
        the innermost scrollable thing here, which is the one C5 says may
        scroll.
        """
        for editor in self._editors.values():
            widget = editor.editor
            if not isinstance(widget, QDoubleSpinBox):
                continue
            widest = max(widget.textFromValue(widget.minimum()),
                         widget.textFromValue(widget.maximum()), key=len)
            widget.setMinimumWidth(
                widget.fontMetrics().horizontalAdvance(widest) + 48)
        scroll = self.findChild(QScrollArea)
        if scroll is not None:
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)
