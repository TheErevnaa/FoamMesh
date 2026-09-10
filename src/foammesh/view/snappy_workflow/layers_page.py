"""Snappy workflow page: ``snappy.layers``.

Plan 30 WP-09 / F-17. The legacy Designer page wrote nineteen ``addLayers/*``
keys, all declared on this task, and edited the per-patch layer groups through
a dialog. The groups are a registered collection (``addLayers/layers``) and are
edited here through the shared child-control table, which surfaces WP-11's
``layer_policy`` (F-42) directly: a patch can be told to grow layers, to freeze
at none, or to inherit, and ``surface_layers`` may legitimately be 0.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import SnappyTaskPage
from .layer_pattern_preview import LayerPatternPreview


class SnappyLayersPage(SnappyTaskPage):
    """Prism layers on the patches that asked for them."""

    task_id_default = 'snappy.layers'
    run_stage = 'layers'

    COLUMNS = ('group_name', 'layer_policy', 'patch_selector',
               'patch_pattern', 'surface_layers',
               'thickness_model', 'relative_sizes', 'first_layer_thickness',
               'final_layer_thickness', 'thickness', 'expansion_ratio',
               'min_thickness')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'Layers are added last and can be refused patch by patch. A group '
            'set to freeze grows no layers however many are asked for '
            'globally; one set to inherit takes the global count.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        # C31-11. Built before the panel, because the panel wires it into
        # the editor form as it constructs the editors.
        self.pattern_preview = LayerPatternPreview(self)

        self.panel = ChildControlPanel(
            self._client, 'meshing.layers.groups', self.tr('Layer groups'),
            columns=self.COLUMNS, parent=self,
            annotations={'patch_pattern': self.pattern_preview})
        self.panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.panel)

        editor = self.panel.editor('patch_pattern')
        if editor is not None:
            editor.valueChanged.connect(
                lambda _field, value: self.pattern_preview.setPattern(value))
        # Selecting a row loads it into the editors with signals blocked, so
        # the preview has to be told; without this, opening the dialog on a
        # second group shows the first group's matches.
        self.panel.table.itemSelectionChanged.connect(self.sync_preview)
        self.sync_preview()

    def sync_preview(self) -> None:
        """Re-read the case's patch names and re-run the current pattern."""
        preview = getattr(self, 'pattern_preview', None)
        if preview is None:
            return
        preview.refreshNames()
        editor = self.panel.editor('patch_pattern')
        preview.setPattern(editor.value() if editor is not None else '')

    def refresh(self) -> None:
        super().refresh()
        self.sync_preview()
