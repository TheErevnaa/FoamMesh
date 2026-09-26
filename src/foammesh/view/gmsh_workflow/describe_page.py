"""Gmsh workflow page: gmsh.describe_geometry, now a host with no fields.

Plan 32 W3, point 5. This page asked the nineteen Gmsh import and healing
questions until Item A moved them onto `3. Preparation`, where they are asked
once, beside the readiness report that says whether they are needed. The task
stayed: the whole Gmsh chain declares `gmsh.describe_geometry` as its first
prerequisite (DP-228), and Plan 30 F-17 requires every declared task to have a
registered page, so deleting the module would have left a hole in one rule to
close nothing in the other -- the outline row is already gone.

What is left is the duplication. A page that still built editors for fields
Preparation now owns would give each of those values two controls and two
Apply buttons, which is exactly the fault DP-153 named: type in one, the other
still shows what was there before, and the mesh is decided by whichever was
pressed last. So this renders none of them, and says where they went.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from foammesh.app import app
from foammesh.db.configurations_schema import Step
from widgets.fit_to_text import fit_to_text

from .base import GmshTaskPage


class GmshDescribePage(GmshTaskPage):
    task_id_default = 'gmsh.describe_geometry'

    #: W-O1. Where this task's questions are asked. It explains the one
    #: control on the page -- the press that goes there -- so it is that
    #: control's description and the step's help rather than a paragraph
    #: above it. A constant because three surfaces are told the same thing.
    HOSTED_NOTE = (
        'The import and healing settings for this geometry are asked on '
        'Preparation, beside the readiness report that says whether they '
        'are needed. Nothing is asked twice.')

    def renders_field(self, field_id: str) -> bool:
        """None of them. Preparation owns every field this task binds.

        DP-153. The binding stays -- it is what makes the field part of the
        task, and what the run reads -- and only the duplicate editor goes.
        """
        return False

    def build_sections(self, layout) -> None:
        """Say where the questions went, and offer the way there.

        A page that asks nothing and says nothing reads as a page that
        failed to load. Anything can still open this task -- a saved route,
        a prerequisite walk -- so it answers for itself.
        """
        note = QLabel(self)
        note.setText(self.tr(self.HOSTED_NOTE))
        note.setObjectName('gmshDescribeHostedNote')
        note.setWordWrap(True)
        # W-O1. Carried, not drawn: the words are on the press below and in
        # the step help, and the page opens on the control instead of on a
        # paragraph about where the page's questions went.
        note.setVisible(False)
        self._hostedNote = note
        layout.addWidget(note)
        row = QHBoxLayout()
        button = QPushButton(self.tr('Open preparation'), self)
        button.setObjectName('gmshDescribeOpenPreparation')
        button.setAccessibleName(self.tr('Open preparation'))
        button.setToolTip(self.tr(self.HOSTED_NOTE))
        button.setAccessibleDescription(self.tr(self.HOSTED_NOTE))
        button.clicked.connect(self._openPreparation)
        fit_to_text(button)
        row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the help control DP-230 built.

        The same arrangement the snappy pages use: `_description` is where a
        task page authors what the step is for, `refresh()` rewrites it from
        the descriptor on every pass, so the sentence is appended after that
        and the help is re-read from the label rather than set beside it.
        """
        described = self._description.text().strip()
        if self.HOSTED_NOTE not in described:
            described = (described + ' ' + self.HOSTED_NOTE).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())

    def _openPreparation(self) -> None:
        """Move the window to `3. Preparation`.

        Through the outline rather than the page stack: the outline is the
        only map of where the user is, and moving the panel without it
        leaves the two disagreeing (R53).
        """
        navigation = getattr(app.window, '_navigationView', None)
        if navigation is None:
            return
        navigation.setCurrentStep(Step.GEOMETRY_REPAIR)
