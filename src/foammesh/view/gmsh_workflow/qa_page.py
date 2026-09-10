"""Gmsh workflow page: gmsh.qa."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.core.engine.base import SU2_QA_OPERATION, qa_operation
from foammesh.view.workflow_controls.checkmesh_findings import (
    CheckMeshFindings,
)

from .base import GmshTaskPage


class GmshQaPage(GmshTaskPage):
    """The check that qualifies this mesh, and what it found.

    R208. The page carried the run button and nothing else: "Accepted." above
    an empty rectangle, on a case whose checkMesh report held four metrics and
    a failing sicn count. Same run, same report file and same panel as the
    snappy QA page -- there is no reason for the two engines to tell the user
    different amounts about the same check.

    Plan 31 CP-09 item 3. Which check that is depends on the target solver,
    and this page said "checkMesh" either way. On an SU2 case checkMesh never
    runs -- ``core.engine.base.qa_operation``, the seam the menu, the QA row,
    the CLI and the facade all route through, answers
    ``quality.su2_readiness`` instead, because SU2 reads the ``.msh`` Gmsh
    wrote and the case publishes no ``constant/polyMesh`` for checkMesh to
    open -- so the page described a mandatory step that does not exist on
    that route, above a findings table that would stay empty forever. The
    page now names the check it will actually run, for the solver it will run
    it for, and shows the checkMesh table only where checkMesh is the check.
    """

    task_id_default = 'gmsh.qa'
    #: Run-gated (D23): the button runs the qualifying check on this mesh.
    run_stage = 'checkMesh'

    def build_sections(self, layout) -> None:
        # Parentless: the layout reparents it, and CP-09's page tests build
        # `build_sections` on a bare instance whose QWidget base is not up.
        self._qaNote = QLabel()
        self._qaNote.setObjectName('gmshQaTargetNote')
        self._qaNote.setWordWrap(True)
        self._qaNote.setAccessibleName('Which check qualifies this mesh')
        layout.addWidget(self._qaNote)
        self._findings = CheckMeshFindings(self._client, self)
        layout.addWidget(self._findings)

    def refresh(self) -> None:
        super().refresh()
        self._describeCheck()

    def refresh_status(self) -> None:
        """Re-read the task's state *and* the report that produced it."""
        super().refresh_status()
        findings = getattr(self, '_findings', None)
        if findings is not None:
            findings.refresh()

    # -- which check, for which solver ------------------------------------- #

    def _describeCheck(self) -> None:
        note = getattr(self, '_qaNote', None)
        if note is None:
            return
        target = self.target_solver()
        # The rule is read off the engine seam, not re-decided here: a page
        # that made its own judgement could describe one check while the
        # button ran the other.
        if qa_operation(target) == SU2_QA_OPERATION:
            note.setText(self.tr(
                'This mesh is for SU2. It is qualified by the SU2 readiness '
                'check, which reads the .msh file Gmsh wrote and reports the '
                'element types, the boundary markers and anything SU2 will '
                'refuse. checkMesh is not run and no constant/polyMesh is '
                'published for this route, so neither is missing.'))
            self.setRunStageLabel(self.tr('Run the SU2 readiness check'))
            self.setRunStageAvailable(True)
            self._findings.setVisible(False)
            return
        note.setText(self.tr(
            'This mesh is for {0}. It is qualified by checkMesh, run on the '
            'published constant/polyMesh.').format(
                self.target_solver_name() if target != 'unselected'
                else self.tr('OpenFOAM by default, since no solver is chosen '
                             'yet')))
        self.setRunStageLabel(self.tr('Run checkMesh'))
        publishes, reason = self.publication_plan()
        self.setRunStageAvailable(publishes, reason + self.tr(
            ' checkMesh reads a polyMesh, so it has nothing to open.'))
        self._findings.setVisible(True)
