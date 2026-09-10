"""Gmsh workflow page: gmsh.volume_controls."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class GmshVolumeControlsPage(GmshTaskPage):
    """Per-volume sizing, inclusion and region typing."""

    task_id_default = 'gmsh.volume_controls'

    COLUMNS = ('name', 'enabled', 'scope_token', 'included',
               'target_size', 'transfinite', 'priority')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'Each included volume publishes as its own cell zone. Clear '
            'Included to drop a volume from the mesh entirely.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.panel = ChildControlPanel(
            self._client, 'gmsh.volume_controls.controls',
            self.tr('Volume controls'), columns=self.COLUMNS, parent=self)
        self.panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.panel)
