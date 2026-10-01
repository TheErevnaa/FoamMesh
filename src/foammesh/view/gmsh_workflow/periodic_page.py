"""Gmsh workflow page: gmsh.periodic."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class GmshPeriodicPage(GmshTaskPage):
    """Periodic pairs, published as matched OpenFOAM cyclic patches."""

    task_id_default = 'gmsh.periodic'

    #: DP-560 (0924 rerun). MEASURED at the 420 px settings column: seven
    #: columns scrolled 282 px sideways. A pair is read as which surface maps
    #: onto which and how; the switch, the rotation angle and the match
    #: tolerance are in the editor the row opens.
    COLUMNS = ('name', 'master_scope_token', 'slave_scope_token', 'transform')

    #: DP-560. The transform maps the master surface onto the slave, as the
    #: note below says; `Master geometry scope` named the store, not that.
    HEADINGS = {'master_scope_token': 'Master surface',
                'slave_scope_token': 'Slave surface'}

    #: W-O1. What the transform column means, and what a pair pointing the
    #: wrong way does. It explains one column of the table below, so it is
    #: said on the table and in the step help rather than above them.
    TRANSFORM_NOTE = (
        'The transform maps the master surface onto the slave. A pair '
        'pointing the wrong way is rejected by Gmsh with "no corresponding '
        'point", so check the direction if a run fails there.')

    def build_sections(self, layout) -> None:
        note = QLabel(self)
        note.setText(self.tr(self.TRANSFORM_NOTE))
        note.setObjectName('gmshPeriodicTransformNote')
        note.setWordWrap(True)
        note.setVisible(False)
        self._transformNote = note
        layout.addWidget(note)

        self.panel = ChildControlPanel(
            self._client, 'gmsh.periodic_pairs.controls',
            self.tr('Periodic pairs'), columns=self.COLUMNS, parent=self,
            headings=self.HEADINGS)
        self.panel.childrenChanged.connect(self.refresh_keeping_edits)
        self.panel.setToolTip(self.tr(self.TRANSFORM_NOTE))
        self.panel.setAccessibleDescription(self.tr(self.TRANSFORM_NOTE))
        layout.addWidget(self.panel)

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()

    def _moveProseBehindHelp(self) -> None:
        """The transform sentence, said through the help control DP-230 built."""
        described = self._description.text().strip()
        if self.TRANSFORM_NOTE not in described:
            described = (described + ' ' + self.TRANSFORM_NOTE).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())
