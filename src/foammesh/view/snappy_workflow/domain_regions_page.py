"""Snappy workflow page: ``snappy.domain_regions``.

Plan 30 WP-09 / F-17. The legacy Designer page edited the ``region``
collection through cards of its own; the collection is registered
(``regions.items``) and the shared child-control table is what every other
collection in the app is edited with, so the port keeps one editor idiom
rather than a second one for this task alone.

Plan 31 adds the other half of the same question. A material point only means
something once OpenFOAM can tell inside from outside, and until now the app
never said which surfaces it could tell that for: every staged surface was
written ``type triSurface``, so a surface with a pinhole in it silently
answered "no" and any refinement region or cellZone built on it was warned
about in a log and dropped. The seven fields named here are the ones that
decide that -- whether the surfaces are declared closed, how narrow a gap
counts as closed, and how finely the surfaces are searched -- so they belong
on the page where the user is already reasoning about what encloses what.
"""
from __future__ import annotations

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import SnappyTaskPage


class SnappyDomainRegionsPage(SnappyTaskPage):
    """The material points that say which side of the surface is meshed."""

    task_id_default = 'snappy.domain_regions'

    #: ``regions.items`` element fields, in the order they are read in.
    #: DP-570 (0924 rerun follow-up). MEASURED at the 360 px settings
    #: column: 325 px of columns in a 314 px table, 11 px of scroll. The
    #: stretched `Z` was a number, so every column -- the name and the type
    #: too -- was floored at a coordinate's width (DP-535). The name
    #: stretches instead, the three coordinates keep their floor, and all
    #: five fit.
    COLUMNS = ('name', 'type', 'point.x', 'point.y', 'point.z')

    #: Plan 31. Written into every ``geometry`` entry of snappyHexMeshDict,
    #: absent from the task descriptor. They are not ``castellatedMeshControls``
    #: keys -- they describe the surfaces themselves -- so they are named here
    #: rather than declared on a stage that does not own them.
    extra_field_ids = (
        'meshing.geometry.tri_surface_declaration',
        'meshing.geometry.gap_detection',
        'meshing.geometry.gap_width',
        'meshing.geometry.tolerance',
        'meshing.geometry.max_tree_depth',
        'meshing.geometry.min_quality',
        'meshing.geometry.scale',
    )

    #: Plan 32 check 12. MEASURED on the page widget before this moved: 134
    #: words over four labels beside a single input. Two paragraphs of them
    #: were why a material point matters and how OpenFOAM decides a surface
    #: is closed -- reasoning, which §4.5 puts behind the help control.
    HELP_DETAIL = (
        'A region names a point inside the volume to keep. snappyHexMesh '
        'keeps the cells reachable from it, so a point on the wrong side '
        'of a surface produces the complement of the mesh you wanted. '
        'Inside and outside only exist for a surface OpenFOAM considers '
        'closed. It decides that by looking for open edges, so a surface '
        'meant to be watertight that has a pinhole or arrives in several '
        'parts answers "neither" — and refinement regions and cell zones '
        'built on it are then dropped with a note in the log. Declare the '
        'surfaces closed to override that, and close narrow gaps when the '
        'geometry has slots the base grid is too coarse to see.')

    def build_sections(self, layout) -> None:
        # Plan 33 OF-02. The table opens the column. The two sentences that
        # used to stand above it were a third copy of `HELP_DETAIL`, which
        # `_moveProseBehindHelp` already sends to the help control and to the
        # page tooltip, so what they said is still one press away and the
        # reader no longer scrolls past it to reach the only editor here.
        self.panel = ChildControlPanel(
            self._client, 'regions.items', self.tr('Regions'),
            columns=self.COLUMNS, parent=self, stretch='name')
        self.panel.childrenChanged.connect(self.refresh)
        layout.insertWidget(0, self.panel)

    #: DP-517. `CaseBuilder._gap_width` reads the width only while the
    #: switch is on, so the box is live only then.
    _GAP_SWITCH = 'meshing.geometry.gap_detection'
    _GAP_WIDTH = 'meshing.geometry.gap_width'

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()

    def reload_values(self) -> None:
        super().reload_values()
        self._syncGapWidth()

    def _on_field_changed(self, field_id: str, value) -> None:
        super()._on_field_changed(field_id, value)
        if field_id == self._GAP_SWITCH:
            self._syncGapWidth()

    def _syncGapWidth(self) -> None:
        """Gap width is editable only while Close narrow gaps is on.

        DP-517 (audit 2026-09-23, the user's annotated Domain & regions >
        Advanced): the width box was live with the switch off, where the
        writer never reads it. The switch's own box is read, not the stored
        value, so ticking it enables the width before Apply. The row stays --
        a greyed width says what the switch will bring -- and applicability
        (`FieldEditor.setApplicability`) still owns whether it is shown.
        """
        switch = self._editors.get(self._GAP_SWITCH)
        width = self._editors.get(self._GAP_WIDTH)
        if switch is None or width is None:
            return
        live = (bool(switch.value()) and width.applies()
                and not width.descriptor.read_only)
        width.editor.setEnabled(live)
        width.label.setEnabled(live)
        width.unit_label.setEnabled(live)

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the control DP-230 already built.

        `_description` is where a task page authors what the step is for, and
        `refresh()` rewrites it from the descriptor on every pass, so the
        page's own paragraphs are appended after that and the help is re-read
        from the label rather than set beside it. The two therefore stay the
        same two strings, which is what the DP-230 gate asserts.
        """
        described = self._description.text().strip()
        if self.HELP_DETAIL not in described:
            described = (described + ' ' + self.HELP_DETAIL).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())
