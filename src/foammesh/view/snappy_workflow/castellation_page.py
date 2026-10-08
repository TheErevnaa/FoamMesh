"""Snappy workflow page: ``snappy.castellation``.

Plan 30 WP-09 / F-17. The legacy Designer page wrote eleven ``castellation/*``
keys and the eight ``snappyAdvanced`` write/debug flags, all of which the task
descriptor declares, plus the two refinement collections it opened per-row
dialogs for. The collections are edited through the shared child-control table
here, which is the same editor the Gmsh size fields and curve controls use.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.facade_client import submit
from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from . import background_estimate_label, level_cell_size
from .base import SnappyTaskPage
from .refinement_membership import (
    SURFACE, VOLUME, RefinementMembership, bound_groups, unbind_group,
)


class RefinementGroupPanel(ChildControlPanel):
    """A refinement table whose row editor also says what the row refines.

    DP-490 (audit MA-04). The levels on a row are read only for the geometry
    rows bound to it, so the editor that sets the levels sets the binding too:
    the membership list is loaded for the row the editor opens on and written
    once the facade has accepted the row, when a new group has its id.
    """

    def __init__(self, facade_client, collection_id, title, kind, **kwargs):
        self.membership = RefinementMembership(facade_client, kind)
        annotations = dict(kwargs.pop('annotations', None) or {})
        annotations['group_name'] = self.membership
        super().__init__(facade_client, collection_id, title,
                         annotations=annotations, **kwargs)
        self._kind = kind

    def open_add_dialog(self) -> None:
        self.membership.load(None)
        super().open_add_dialog()

    def open_edit_dialog(self, *args) -> None:
        if self.selected_key() is None:
            return
        self.membership.load(self.selected_key())
        super().open_edit_dialog(*args)

    def _run(self, operation: str, parameters: dict) -> None:
        """The inherited write, followed by the binding it implies."""
        kind = operation.rsplit('.', 1)[-1]
        # Plan 37 UF3 DP-1035. Tracked from the press to the binding that
        # follows it, so Proceed waits for both and the task hears of them.
        write = self._open_write()

        def ran(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                self._close_write(write, False, result)
                return
            payload = getattr(result, 'payload', None) or {}
            group = (payload.get('entity_id') if kind == 'create'
                     else parameters.get('entity_id'))

            # DP-1190. A binding the facade refused (a step that locked
            # while the editor was open) is reported, not dropped: the table
            # is re-read either way, so it shows what the case holds.
            refusals = []

            def finished() -> None:
                self.refresh()
                self.childrenChanged.emit()
                if refusals:
                    self._close_write(write, False, refusals[0])
                else:
                    self._close_write(write, True)

            if kind == 'remove':
                unbind_group(self._client, self._kind,
                             parameters.get('entity_id'), then=finished,
                             refused=refusals.append)
            else:
                self.membership.commit(group, then=finished,
                                       refused=refusals.append)

        self._track_write(write, submit(self._client, operation, parameters,
                                        then=ran))


class SnappyCastellationPage(SnappyTaskPage):
    """Refinement levels and the cell-count ceilings that bound them."""

    task_id_default = 'snappy.castellation'
    run_stage = 'castellation'

    #: Plan 33 OF-05. MEASURED at a 560 px settings column: eight columns in
    #: 480 px of table scrolled 435 px sideways, and a fifth column still
    #: scrolls 312, so four is what this width holds. These four are the
    #: refinement a row asks for; the angles, the patch groups and the zone
    #: mode are read one row at a time, which is what the row editor is.
    #: DP-570 (0924 rerun follow-up). MEASURED at the 360 px settings column
    #: a window under 1600 px wide gets: the stretched last column fell below
    #: its own heading on both tables -- `Feature edge refinement level` to
    #: 73 px of 128, `Volume refinement level` to 104 of 128. A surface row is
    #: read by its level range and a volume row by its mode and level, so the
    #: feature edge level and the distance are in the row editor, and the
    #: group name takes the room left over.
    SURFACE_COLUMNS = ('group_name',
                       'surface_refinement.minimum_level',
                       'surface_refinement.maximum_level')
    VOLUME_COLUMNS = ('group_name', 'mode', 'volume_refinement_level')
    #: C31-11. A band grades the feature refinement of one surface group with
    #: distance, so it is read next to that group's row.
    BAND_COLUMNS = ('group_name', 'distance', 'level')
    #: DP-1251. The level fields each collection's row editor carries, every
    #: one of which gets a live "Cell size at level N" readout under it and a
    #: tooltip on its table column.
    SURFACE_LEVELS = ('surface_refinement.minimum_level',
                      'surface_refinement.maximum_level',
                      'feature_edge_refinement_level')
    VOLUME_LEVELS = ('volume_refinement_level',)
    BAND_LEVELS = ('level',)

    def build_sections(self, layout) -> None:
        # DP-1251. One base cell for the four tables, re-read on each page
        # refresh and each time a row editor opens (see `level_cell_size`).
        self.cell_size_source = level_cell_size.BaseCellSource()
        # Plan 33 OF-05. The three tables open the column, in front of the
        # ceilings and flags the task declares: what is refined is the
        # decision this page is for, and how far the refiner may go before it
        # gives up is the setting behind it. The two paragraphs that used to
        # stand above and between the tables are on the tables they described,
        # where a reader who wants them can ask.
        self.surface_panel = RefinementGroupPanel(
            self._client, 'meshing.castellation.surface_refinements',
            self.tr('Surface refinement'), SURFACE,
            columns=self.SURFACE_COLUMNS, parent=self, stretch='group_name')
        self.surface_panel.setToolTip(self.tr(
            'Splits base grid cells near a surface. Each level halves the '
            'cell size there, so a level costs roughly eight times the cells '
            'of the one before it.'))
        self.surface_panel.childrenChanged.connect(self.refresh_keeping_edits)
        layout.insertWidget(0, self.surface_panel)

        self.surface_panel.childrenChanged.connect(self.sync_band_groups)

        self.volume_panel = RefinementGroupPanel(
            self._client, 'meshing.castellation.volume_refinements',
            self.tr('Volume refinement'), VOLUME,
            columns=self.VOLUME_COLUMNS, parent=self, stretch='group_name')
        self.volume_panel.setToolTip(self.tr(
            'Splits base grid cells inside a named region rather than near a '
            'surface. A wake or a gap is resolved where no surface runs '
            'through it.'))
        self.volume_panel.childrenChanged.connect(self.refresh_keeping_edits)
        layout.insertWidget(1, self.volume_panel)

        self.band_panel = ChildControlPanel(
            self._client, 'meshing.castellation.feature_bands',
            self.tr('Feature refinement bands'), columns=self.BAND_COLUMNS,
            parent=self, choices={'group_name': self._group_choices()})
        self.band_panel.setToolTip(self.tr(
            'Grades the feature edge refinement of one surface group with '
            'distance: one row per step, each reaching further and refining '
            'less. A group with no bands keeps the single level on its '
            'surface refinement row.'))
        self.band_panel.childrenChanged.connect(self.refresh_keeping_edits)
        layout.insertWidget(2, self.band_panel)

        # DP-586 (field audit 0924 snappy-front D13). The volume distance
        # ramp was in the schema and the writer with no workflow control.
        self.volume_band_panel = ChildControlPanel(
            self._client, 'meshing.castellation.volume_bands',
            self.tr('Volume distance bands'), columns=self.BAND_COLUMNS,
            parent=self, choices={'group_name': self._volume_group_choices()})
        self.volume_band_panel.setToolTip(self.tr(
            'Grades a distance-mode volume group: one row per step, each '
            'reaching further and refining less. A group with no bands keeps '
            'its single distance and level; a group in another mode ignores '
            'its bands.'))
        self.volume_band_panel.childrenChanged.connect(self.refresh_keeping_edits)
        self.volume_panel.childrenChanged.connect(self.sync_volume_band_groups)
        layout.insertWidget(3, self.volume_band_panel)

        # DP-1251. MEASURED before: the four row editors asked for six level
        # fields between them and said nothing about the cell any of them
        # makes; the legacy dialogs had said it since R175.
        self.level_readouts = {}
        for panel, keys in ((self.surface_panel, self.SURFACE_LEVELS),
                            (self.volume_panel, self.VOLUME_LEVELS),
                            (self.band_panel, self.BAND_LEVELS),
                            (self.volume_band_panel, self.BAND_LEVELS)):
            self.level_readouts[panel.collection_id] = (
                level_cell_size.attach_readouts(
                    panel, keys, self.cell_size_source))

        # DP-490. A group bound to no geometry is saved, listed with its
        # levels, and never written: said here, where the list reads as
        # though it were configured. Hidden while every group refines
        # something, so an empty or complete page says nothing.
        self.unbound_label = QLabel()
        self.unbound_label.setObjectName('castellationUnboundGroups')
        self.unbound_label.setWordWrap(True)
        self.unbound_label.setVisible(False)
        layout.insertWidget(4, self.unbound_label)  # DP-586: after the bands
        self.sync_unbound_groups()
        # Plan 37 #7. The cells this stage starts from and the RAM they take,
        # the same estimate the base-grid page shows.
        self.estimate_label = QLabel(self)
        background_estimate_label.prepare(self.estimate_label, 'castellationBackgroundEstimate')
        layout.insertWidget(5, self.estimate_label)
        self.refresh_estimate()

    def refresh(self) -> None:
        source = getattr(self, 'cell_size_source', None)
        if source is not None:
            # DP-1251. The base grid may have changed since the last visit;
            # if it has, the level tooltips on the four tables are re-said.
            before = source.base()
            if source.refresh() != before:
                for name in ('surface_panel', 'volume_panel', 'band_panel',
                             'volume_band_panel'):
                    panel = getattr(self, name, None)
                    if panel is not None:
                        panel.refresh()
        super().refresh()
        self.sync_unbound_groups()
        self.refresh_estimate()

    def refresh_estimate(self):
        """Plan 37 #7: the base-grid page's estimate, said here too."""
        return background_estimate_label.show(
            getattr(self, 'estimate_label', None), self._client)

    def unbound_groups(self) -> list:
        """The names of the refinement rows no geometry row is bound to."""
        try:
            configuration = self._client.configuration() or {}
        except Exception:                                    # noqa: BLE001
            configuration = {}
        names = []
        for panel_name, kind in (('surface_panel', SURFACE),
                                 ('volume_panel', VOLUME)):
            panel = getattr(self, panel_name, None)
            if panel is None:
                continue
            bound = bound_groups(configuration, kind)
            names.extend(
                str(row.get('group_name') or row.get('__key__'))
                for row in panel.rows()
                if str(row.get('__key__')) not in bound)
        return names

    def sync_unbound_groups(self) -> None:
        label = getattr(self, 'unbound_label', None)
        if label is None:
            return
        names = self.unbound_groups()
        label.setText(self.tr(
            'Refines nothing yet, so it is not written to the mesher: %s. '
            'Edit the row and choose the geometry it refines.')
            % ', '.join(names))
        label.setVisible(bool(names))

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

    def _volume_group_choices(self) -> list:
        """DP-586: the volume refinement groups a distance band may name."""
        return [(str(row.get('group_name') or ''),
                 str(row.get('group_name') or ''),
                 self.tr('Volume refinement group'), True)
                for row in self.volume_panel.rows()
                if str(row.get('group_name') or '').strip()]

    def sync_volume_band_groups(self) -> None:
        """DP-586: re-offer the picker after the volume rows change."""
        panel = getattr(self, 'volume_band_panel', None)
        if panel is not None:
            panel.set_choices('group_name', self._volume_group_choices())
