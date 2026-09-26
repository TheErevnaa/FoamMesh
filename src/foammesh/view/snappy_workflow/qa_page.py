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
_MESH_QUALITY_IDS = frozenset(MESH_QUALITY_FIELDS)

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

    #: Plan 32 check 12. MEASURED on the page widget before this moved: 324
    #: words over 47 labels, of which 187 were the two panels' purpose and
    #: caveat paragraphs -- what the settings are for, which dictionary they
    #: reach and what re-running the check does. The page now opens on the
    #: verdict, the two headings and the thirty-two controls; the reasoning
    #: is one press away, where §4.5 puts it.
    HELP_DETAIL = (
        'How the finished mesh is checked: these change the verdict and the '
        'evidence, never the mesh. They are checkMesh\'s own command line, '
        'so re-running the check with a lower threshold re-judges the mesh '
        'you already have. Judging against the mesher\'s own limits writes '
        'system/meshQualityDict from the mesher limits below and needs the '
        'case dictionaries generated again before it takes effect. The '
        'problem faces are always written as sets into '
        'constant/polyMesh/sets, which is what the viewport highlights; the '
        'surface is the extra copy, under postProcessing/checkMesh. '
        'Every candidate cell is judged against the quality limits. '
        'snappyHexMesh reverts a snap or a layer insertion that would breach '
        'one, so they decide what the mesher is allowed to produce rather '
        'than only what is reported afterwards. They are written into '
        'snappyHexMeshDict/meshQualityControls and are not the checkMesh '
        'reporting thresholds above.')

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)

    def build_sections(self, layout) -> None:
        """Mount what checkMesh found, then the limits snappy meshes against."""
        self._findings = CheckMeshFindings(self._client, self)
        layout.addWidget(self._findings)
        self._check = self.adoptPanel(_CheckMeshGroup(self._client, self))
        layout.addWidget(self._check)
        self._quality = self.adoptPanel(
            _MeshQualityGroup(self._client, self))
        layout.addWidget(self._quality)

    def renders_field(self, field_id: str) -> bool:
        """The limits belong to the panel, not to the page's own form.

        DP-153. `snappy.qa` binds exactly ``MESH_QUALITY_FIELDS``, and
        `_MeshQualityGroup` renders exactly ``MESH_QUALITY_FIELDS``, so the
        page drew all twenty-seven twice -- once in Advanced, once in the
        panel -- with no link between the two copies. The panel is the
        surface that was designed for them: it carries the heading, the
        purpose, the caveat that says these are the mesher's own limits and
        not the checkMesh thresholds above, and the R102 widening that lets
        `Min Vol -100000000000.0000000` be read to its last digit.
        """
        return field_id not in _MESH_QUALITY_IDS

    def aligned_forms(self):
        """The two panels are one column: checkMesh's settings over the
        limits they may be compared against (DP-154)."""
        return tuple(group.form_layout()
                     for group in (getattr(self, '_check', None),
                                   getattr(self, '_quality', None))
                     if group is not None)

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()
        for name in ('_check', '_quality'):
            group = getattr(self, name, None)
            if group is not None:
                # DP-339. A refresh keeps an uncommitted edit.
                group.reload(discard_pending=False)
        # The panels have just rewritten their own labels -- CP-09 adds and
        # strips " (inactive)" on reload -- so the shared column is measured
        # again here rather than in `super().refresh()`.
        self._align_field_columns()

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the control DP-230 already built.

        `_description` is where a task page authors what the step is for, and
        `refresh()` rewrites it from the descriptor on every pass, so the two
        panels' paragraphs are appended after that and the help is re-read
        from the label rather than set beside it. The two therefore stay the
        same two strings, which is what the DP-230 gate asserts.
        """
        described = self._description.text().strip()
        if self.HELP_DETAIL not in described:
            described = (described + ' ' + self.HELP_DETAIL).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())

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
    #: DP-764. `Mesh check` over "Non-orthogonality reported above 70", and
    #: `Mesh quality limits` under it over "Max face non-orthogonality 65":
    #: two numbers for one quantity, and nothing on screen said the first is
    #: where checkMesh starts reporting a face and the second is what the
    #: mesher holds itself to (gui review 0925 workflow-pages #4).
    heading = 'checkMesh report thresholds'
    #: Plan 32 check 12. The paragraph that stood here and the caveat under
    #: it are `SnappyQaPage.HELP_DETAIL` now: they say what the settings are
    #: for and which dictionary they reach, which is reasoning rather than a
    #: blocker, and §4.5 puts reasoning behind the help control. What stays
    #: is one sentence, which SETUP-01 puts on the heading's tooltip and the
    #: accessible description rather than on screen.
    purpose = ('Where checkMesh starts reporting a face. Changing these '
               're-judges the mesh you have; it does not change the mesh.')
    caveat = ''


class _MeshQualityGroup(FieldGroupPage):
    """The ``meshQuality`` values, as a panel inside the QA page."""

    token = 'workflow.quality'
    field_ids = MESH_QUALITY_FIELDS
    #: DP-764. Named for the stages that read them (workflow-pages #4/#5):
    #: snappyHexMesh reverts a snap or layer step that would breach one, so
    #: they act before this page, not on it.
    heading = 'Mesher limits (Snap and Layers)'
    #: Plan 32 check 12, as above: the longer account is
    #: `SnappyQaPage.HELP_DETAIL`. This sentence is the one a reader editing
    #: a limit after the run needs, on the heading's tooltip (SETUP-01).
    purpose = ('The limits snappyHexMesh holds every Snap and Layers step '
               'to. A change takes effect when Snap and Layers run again; '
               'the mesh you have was made with the values set when it ran.')
    caveat = ''

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
