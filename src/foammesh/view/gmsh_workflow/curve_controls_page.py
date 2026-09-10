"""Gmsh workflow page: gmsh.curve_controls."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class GmshCurveControlsPage(GmshTaskPage):
    """Structured or locally sized curves, scoped to a prepared face group."""

    task_id_default = 'gmsh.curve_controls'

    COLUMNS = ('name', 'enabled', 'scope_token', 'mode', 'segments', 'law',
               'coefficient', 'priority')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'A control applies to the boundary curves of the selected face '
            'group. Transfinite mode fixes the node count along each curve; '
            'size mode sets a local element size instead.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.panel = ChildControlPanel(
            self._client, 'gmsh.curve_controls.controls',
            self.tr('Curve controls'), columns=self.COLUMNS, parent=self)
        self.panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.panel)
