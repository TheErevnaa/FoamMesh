"""Shared base for the Gmsh task pages."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.core.gmsh.plan_derivation import POLY_MESH_TARGETS
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query


#: What the user calls each target. ``unselected`` is deliberately absent: it
#: is a state, not a solver, and every sentence that reaches for it needs its
#: own words rather than a solver-shaped placeholder.
SOLVER_NAMES = {'openfoam': 'OpenFOAM', 'su2': 'SU2'}


class GmshTaskPage(EngineTaskPage):
    """One Gmsh workflow task.

    The signature is ``(facade_client, parent)``: the engine branch constructs
    every page positionally as ``page_class(client, self)``, so a page that
    took its task id second would receive the parent widget as a task id.
    Each concrete page names its own task instead.
    """

    engine_id = 'gmsh'
    run_all_task_id = 'gmsh.compute'
    task_id_default = ''

    #: Which page a runner warning belongs to, matched on the wording the
    #: runner writes (``runner_v1.apply_volume_controls`` and friends). R66/
    #: R67: ``result.json`` carried ``volume control 'tee_fluid' has no
    #: resolvable volume scope and was skipped`` and the background-field
    #: warning that names Global Sizing, and neither reached any surface in
    #: the GUI -- the Console tab showed nine progress lines and the run
    #: reported success. First match wins, so the background-size-field
    #: warning is attributed before the generic ``size field`` rule.
    RUN_WARNING_TASKS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ('gmsh.global_sizing', ('background size field',)),
        ('gmsh.size_fields', ('size field',)),
        ('gmsh.curve_controls', ('curve control',)),
        ('gmsh.volume_controls', ('volume control',)),
        ('gmsh.boundary_layers', ('boundary layer', 'prism')),
        ('gmsh.periodic', ('periodic pair',)),
        # Plan 31 CP-08 item 7. The farfield cut is part of describing
        # the geometry, and what it did with the volumes it left is the
        # first thing a reader of that page needs: a discarded sealed
        # cavity is a region the mesh does not contain.
        ('gmsh.describe_geometry',
         ('classified surface', 'sealed cavity', 'farfield')),
    )

    def __init__(self, facade_client, parent=None):
        super().__init__(facade_client, self.task_id_default, parent)
        self._runWarnings = QLabel(self)
        self._runWarnings.setObjectName('gmshRunWarnings')
        self._runWarnings.setWordWrap(True)
        self._runWarnings.setProperty('foammeshStatus', 'warning')
        self._runWarnings.setAccessibleName(
            self.tr('Warnings from the last Gmsh run'))
        self._runWarnings.setVisible(False)
        # Above the calculated-settings table: a control that the run threw
        # away has to be read before the settings it belongs to, not after.
        self._body_layout.insertWidget(0, self._runWarnings)
        self.updateRunWarnings()

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, '_runWarnings'):
            self.updateRunWarnings()

    # -- what this case is being meshed for -------------------------------- #

    def target_solver(self) -> str:
        """Which solver the case is meshed for: openfoam, su2 or unselected.

        CP-09 item 3: the target has to be visible in QA and export wording,
        and an SU2 page must not describe checkMesh or polyMesh as mandatory.
        A page that cannot read it says ``unselected`` rather than assuming
        OpenFOAM, because assuming OpenFOAM is what put a polyMesh sentence
        on the SU2 route in the first place.
        """
        try:
            values = self._client.field_values(('mesh.target_solver',))
        except Exception:                                    # noqa: BLE001
            return 'unselected'
        return str(values.get('mesh.target_solver') or 'unselected')

    def target_solver_name(self) -> str:
        return SOLVER_NAMES.get(self.target_solver(),
                                self.tr('no solver chosen yet'))

    def element_order(self) -> int:
        try:
            values = self._client.field_values(('gmsh.compute.element_order',))
        except Exception:                                    # noqa: BLE001
            return 1
        try:
            return int(float(values.get('gmsh.compute.element_order') or 1))
        except (TypeError, ValueError):
            return 1

    def publication_plan(self) -> tuple[bool, str]:
        """``(publishes, why not)`` for a run of this case.

        The rule is the one ``core.gmsh.plan_derivation`` applies, read here
        rather than restated: a target in :data:`POLY_MESH_TARGETS` at first
        order publishes ``constant/polyMesh``, and nothing else does.
        """
        if self.element_order() > 1:
            return False, self.tr(
                'This mesh is second order and constant/polyMesh is a '
                'first-order format, so the run keeps its native Gmsh .msh. '
                'There is nothing to publish.')
        if self.target_solver() not in POLY_MESH_TARGETS:
            return False, self.tr(
                'The SU2 route reads the mesh file Gmsh wrote, so this case '
                'publishes no polyMesh. Nothing downstream of it needs one.')
        return True, ''

    # -- warnings the run produced ----------------------------------------- #

    def last_run(self) -> dict:
        """The newest Gmsh run manifest, or ``{}`` when there is none."""
        try:
            payload = query(self._client, 'mesh.gmsh.runs', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory note
            return {}
        runs = [item for item in (payload.get('runs') or ())
                if isinstance(item, dict)]
        if not runs:
            return {}
        # `created_at` has one-second resolution, so the listing order settles
        # ties -- `list_runs` walks the run directory in sorted order.
        return max(enumerate(runs),
                   key=lambda item: (str(item[1].get('created_at') or ''),
                                     item[0]))[1]

    def warning_task(self, text: str) -> str:
        """Which task page a runner warning is about."""
        lowered = str(text).lower()
        for task_id, keywords in self.RUN_WARNING_TASKS:
            if any(keyword in lowered for keyword in keywords):
                return task_id
        return self.run_all_task_id or ''

    def run_warnings(self) -> tuple[str, tuple[str, ...]]:
        """``(run_id, warnings)`` from the last run, for this page.

        The Compute Mesh page shows every warning -- it is the page the run
        happened on -- and each other page shows the ones about its own
        controls, so a discarded volume control is reported where the user
        would go to check it.
        """
        run = self.last_run()
        if not run:
            return '', ()
        texts, seen = [], set()
        for value in (list(run.get('warnings') or ())
                      + list(run.get('runner_warnings') or ())):
            text = str(value).strip()
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
        if self.task_id != self.run_all_task_id:
            texts = [text for text in texts
                     if self.warning_task(text) == self.task_id]
        return str(run.get('run_id') or ''), tuple(texts)

    def updateRunWarnings(self) -> None:
        run_id, texts = self.run_warnings()
        if not texts:
            self._runWarnings.setText('')
            self._runWarnings.setVisible(False)
            return
        heading = (self.tr('The last run (%s) reported:') % run_id if run_id
                   else self.tr('The last run reported:'))
        self._runWarnings.setText(
            heading + '\n' + '\n'.join(f'• {text}' for text in texts))
        self._runWarnings.setVisible(True)
