"""Gmsh workflow page: gmsh.periodic."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class GmshPeriodicPage(GmshTaskPage):
    """Periodic pairs, published as matched OpenFOAM cyclic patches."""

    task_id_default = 'gmsh.periodic'

    COLUMNS = ('name', 'enabled', 'master_scope_token', 'slave_scope_token',
               'transform', 'rotation_angle_degrees', 'match_tolerance')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'The transform maps the master surface onto the slave. A pair '
            'pointing the wrong way is rejected by Gmsh with "no '
            'corresponding point", so check the direction if a run fails '
            'there.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.panel = ChildControlPanel(
            self._client, 'gmsh.periodic_pairs.controls',
            self.tr('Periodic pairs'), columns=self.COLUMNS, parent=self)
        self.panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.panel)
