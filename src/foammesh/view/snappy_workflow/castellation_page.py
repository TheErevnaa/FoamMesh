"""Snappy workflow page: ``snappy.castellation``.

Plan 30 WP-09 / F-17. The legacy Designer page wrote eleven ``castellation/*``
keys and the eight ``snappyAdvanced`` write/debug flags, all of which the task
descriptor declares, plus the two refinement collections it opened per-row
dialogs for. The collections are edited through the shared child-control table
here, which is the same editor the Gmsh size fields and curve controls use.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import SnappyTaskPage


class SnappyCastellationPage(SnappyTaskPage):
    """Refinement levels and the cell-count ceilings that bound them."""

    task_id_default = 'snappy.castellation'
    run_stage = 'castellation'

    SURFACE_COLUMNS = ('group_name', 'included_angle',
                       'surface_refinement.minimum_level',
                       'surface_refinement.maximum_level',
                       'feature_edge_refinement_level',
                       'perpendicular_angle', 'patch_groups', 'zone_mode')
    VOLUME_COLUMNS = ('group_name', 'mode', 'distance',
                      'volume_refinement_level')
    #: C31-11. A band grades the feature refinement of one surface group with
    #: distance, so it is read next to that group's row.
    BAND_COLUMNS = ('group_name', 'distance', 'level')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'Castellation splits base-grid cells near the surfaces and inside '
            'the volumes listed below. Each level halves the cell size, so a '
            'level costs roughly eight times the cells of the one before it.'),
            self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.surface_panel = ChildControlPanel(
            self._client, 'meshing.castellation.surface_refinements',
            self.tr('Surface refinement'), columns=self.SURFACE_COLUMNS,
            parent=self)
        self.surface_panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.surface_panel)

        self.surface_panel.childrenChanged.connect(self.sync_band_groups)

        self.volume_panel = ChildControlPanel(
            self._client, 'meshing.castellation.volume_refinements',
            self.tr('Volume refinement'), columns=self.VOLUME_COLUMNS,
            parent=self)
        self.volume_panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.volume_panel)

        band_note = QLabel(self.tr(
            'A feature refinement band grades the refinement of a surface '
            'group\'s feature edges with distance: one row per step, each '
            'reaching further and refining less. A group with no bands keeps '
            'the single level on its surface refinement row.'), self)
        band_note.setWordWrap(True)
        layout.addWidget(band_note)

        self.band_panel = ChildControlPanel(
            self._client, 'meshing.castellation.feature_bands',
            self.tr('Feature refinement bands'), columns=self.BAND_COLUMNS,
            parent=self, choices={'group_name': self._group_choices()})
        self.band_panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.band_panel)

    def _group_choices(self) -> list:
        """The surface refinement groups a band may be attached to.

        A band that names a group with no surface refinement row is never
        read, and nothing about the dictionary says so, so the field is a
        picker over the rows that exist rather than a free-text name.
        """
        return [(str(row.get('group_name') or ''),
                 str(row.get('group_name') or ''),
                 self.tr('Surface refinement group'), True)
                for row in self.surface_panel.rows()
                if str(row.get('group_name') or '').strip()]

    def sync_band_groups(self) -> None:
        """Re-offer the band group picker after the surface rows change."""
        panel = getattr(self, 'band_panel', None)
        if panel is not None:
            panel.set_choices('group_name', self._group_choices())
