"""Gmsh workflow page: gmsh.publish.

Plan 30 WP-09 / F-23. This was an empty two-line subclass: the task declares
no fields, so the page rendered a title, a description and three lifecycle
buttons with nothing behind them, and the one thing the task does -- write
``constant/polyMesh`` and its manifest -- had no control anywhere. Publishing
happened as a side effect of whatever needed the polyMesh next, which is the
same defect Plan 26 fixed for ``surface_features``: a stage that runs
invisibly cannot be inspected, and a task page for it is a page about nothing.

It is not folded into compute. ``gmsh.publish`` is a separate declared task
with its own ``run_gated`` flag, its own artifact contract and its own
``invalidates`` list, and republishing after a patch-category change without
re-running Gmsh is a real thing to want. It gets its action instead: the
generic ``workflow.run_stage`` op runs the ``publish`` stage, which is WP-07's
publisher, called rather than reimplemented.

Plan 31 CP-09 items 2 and 3 finish it. ``gmsh.publish`` is in
:attr:`GmshMeshingEngine.ATOMIC_RUN_TASKS`, so "Run to end" already published
before the user ever reached this page -- and the button said "Run this step",
which reads as a step still owed. It says "Publish again" now, and the page
says who already did it. And publication is not unconditional: an SU2 case
publishes nothing, and neither does a second-order mesh, so on those cases
the button is shut and carries the reason instead of writing a polyMesh
nothing in the case asked for.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from .base import GmshTaskPage


class GmshPublishPage(GmshTaskPage):
    """Write the Gmsh result out as an OpenFOAM polyMesh."""

    task_id_default = 'gmsh.publish'
    #: The `publish` stage of the Gmsh engine (`core.engine.gmsh._STAGES`).
    run_stage = 'publish'
    #: CP-09 item 2. Generate already ran this stage; this button is the
    #: re-run, and says so.
    run_stage_label = 'Publish again'

    def build_sections(self, layout) -> None:
        self._note = QLabel()
        self._note.setObjectName('gmshPublishNote')
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

    def refresh(self) -> None:
        super().refresh()
        note = getattr(self, '_note', None)
        if note is None:
            return
        publishes, reason = self.publication_plan()
        self.setRunStageAvailable(publishes, reason)
        if publishes:
            note.setText(self.tr(
                'Publishing converts the native Gmsh mesh into '
                'constant/polyMesh for {0} and writes the manifest that '
                'records which prepared surface each patch came from. It '
                're-reads the mesh Gmsh already produced; it does not '
                're-mesh.\n\n'
                'Run to end already publishes, so you do not have to: use '
                'Publish again only to republish after changing patch '
                'categories without re-running Gmsh.'
            ).format(self.target_solver_name()))
            note.setProperty('foammeshStatus', '')
        else:
            note.setText(reason + self.tr(
                '\n\nThe native Gmsh .msh is this run\'s result: it is '
                'counted, inspectable in the viewport and saveable.'))
            note.setProperty('foammeshStatus', 'warning')
        note.style().unpolish(note)
        note.style().polish(note)
