"""The one Quality page, for both engines.

Plan 42 SG18. ``snappy.qa`` and ``gmsh.qa`` were two page classes (the snappy
one, and a Gmsh one built on the Gmsh task base) that drew the same checkMesh
findings, the same checkMesh settings and the same check highlights, each with
its own run buttons. This is the one class. What belongs to one engine only
(Gmsh's SU2 readiness check, its quality gate and further limits, its mesh
statistics, its run warnings; snappy's wording about the mesher limits) comes
from the engine's *items*, looked up in :data:`QUALITY_ENGINE_ITEMS` by the
page's engine, so the page itself names no engine.

Two names remain for the engine branch's registries (``SnappyQaPage`` and
``GmshQualityGatePage``). They are bindings in the precedent of the shared
qualification pages: they say which engine and which task, and add nothing.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QWidget,
)

from foammesh.view.main_window.flow_layout import FlowLayout

from foammesh.view.gmsh_run_readout import GmshOptionsTier, GmshRunReadout
from foammesh.view.quality_panels import (
    CheckHighlightsPanel, _CheckMeshGroup)
from foammesh.view.workflow_controls.checkmesh_findings import (
    CheckMeshFindings)
from foammesh.view.workflow_controls.task_page import EngineTaskPage

#: engine id -> the class that supplies that engine's Quality items.
#: Registered by each engine's workflow package when it is imported.
QUALITY_ENGINE_ITEMS: dict[str, type] = {}


class _RunButtons(FlowLayout):
    """Plan 44 F4. The run row wraps: two run buttons side by side asked
    265 px in the 233 px of a 1280 px window's sheet, and the page scroller
    has no sideways bar, so the second button was cut. ``addWidget`` takes
    (and ignores) the stretch a box layout is handed, so the engine items
    mount their buttons as before."""

    def addWidget(self, widget, stretch=0, *_args):  # noqa: N802 - Qt name
        super().addWidget(widget)


def register_quality_items(engine_id: str, items_class: type) -> None:
    """Make *items_class* the source of *engine_id*'s Quality items."""
    QUALITY_ENGINE_ITEMS[str(engine_id)] = items_class


class QualityItems:
    """What an engine adds to the Quality page. The base adds nothing."""

    #: The paragraph the page's help control carries for this engine.
    help_detail = ''
    #: The footer button's wording, when the engine words it itself.
    run_stage_label = ''

    def __init__(self, page) -> None:
        self.page = page

    def after_init(self) -> None:
        """Called once the page is built."""

    def mount_beside_checkmesh(self, row, layout) -> None:
        """Extra actions in the run row, and panels under the findings."""

    def mount_findings_extras(self, layout) -> None:
        """Panels under the findings, above the checkMesh settings."""

    def mount_below(self, layout) -> None:
        """Panels under the checkMesh settings."""

    def renders_field(self, field_id: str) -> bool:
        return True

    def refresh(self) -> None:
        """Re-read the engine's own panels."""

    def set_busy(self, busy: bool) -> None:
        """Enable or disable the engine's own actions."""

    def checkmesh_availability(self) -> tuple[bool, str]:
        """``(can run, why not)`` for checkMesh on this engine's mesh."""
        return True, ''

    def findings_check(self) -> str:
        """Which report the findings panel reads ('' = the preset's own)."""
        return ''

    def mount_check(self, layout, group) -> None:
        """Put the checkMesh report thresholds panel on the page.

        Plan 44 CHR2 (G-FITS-1). MEASURED live at 1280x800 on a meshed snappy
        case: the open panel (305 px, with its long tier prose) took most of
        the 471 px the Quality page ran past its scroller. The thresholds
        re-judge a mesh rather than make one, so, as on the Gmsh route
        (FX2), every field stays in the panel behind one section fold.
        """
        from PySide6.QtWidgets import QWidget
        from foammesh.view.gmsh_workflow.folds import add_fold

        page = self.page
        if not isinstance(group, QWidget):       # a test's stand-in panel
            layout.addWidget(group)
            return
        page._foldCheckThresholds = add_fold(
            page, layout, name='CheckThresholds',
            title=page.tr('checkMesh report thresholds'),
            description=page.tr(
                'Where checkMesh starts reporting a face. Changing these '
                're-judges the mesh you have; it does not change the mesh.'),
            panel=group)


class QualityPage(GmshRunReadout, GmshOptionsTier, EngineTaskPage):
    """``<engine>.qa``: how the mesh is checked, and what the check found."""

    engine_id = 'snappy'
    task_id_default = 'snappy.qa'
    #: Run-gated (D23): the button runs the qualifying check on this mesh.
    run_stage = 'checkMesh'
    SHOWS_EVERY_RUN_WARNING = True
    run_all_task_id = None

    @property
    def _items(self):
        # Built on first use: the pages' tests call `build_sections` on a
        # bare instance whose constructor has not run.
        items = self.__dict__.get('_itemsObject')
        if items is None:
            items = QUALITY_ENGINE_ITEMS.get(self.engine_id, QualityItems)(self)
            self.__dict__['_itemsObject'] = items
        return items

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        engine = engine_id or self.engine_id
        self.engine_id = engine
        from foammesh.core.engine.base import qa_task_id
        task_id = (self.task_id_default
                   if engine == type(self).engine_id else qa_task_id(engine))
        super().__init__(facade_client, task_id, parent, engine_id=engine)
        self._items.after_init()

    # -- what the page is made of ------------------------------------------ #

    def build_sections(self, layout) -> None:
        # A container widget rather than a nested layout: `build_sections` is
        # handed the page's own layout, and the pages' tests hand it a stub
        # that takes widgets.
        row = QWidget()
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        buttons = QWidget(row)
        status = _RunButtons(buttons)
        column.addWidget(buttons)
        self._runCheckMesh = QPushButton(self.tr('Run checkMesh'))
        self._runCheckMesh.setObjectName('qualityRunCheckMesh')
        self._runCheckMesh.setAccessibleDescription(
            'Run checkMesh on the mesh in the case.')
        self._runCheckMesh.clicked.connect(
            lambda: self._runCheck('mesh.check'))
        status.addWidget(self._runCheckMesh, 0)
        self._items.mount_beside_checkmesh(status, layout)
        self._qaNote = QLabel()
        self._qaNote.setObjectName('qualityTargetNote')
        self._qaNote.setWordWrap(True)
        self._qaNote.setAccessibleName('Result of the last check you ran here')
        # W-O1. Said only when a check pressed here was refused.
        self._qaNote.setVisible(False)
        column.addWidget(self._qaNote)
        layout.addWidget(row)
        self._findings = CheckMeshFindings(
            self._client, self, check=self._items.findings_check())
        layout.addWidget(self._findings)
        self._items.mount_findings_extras(layout)
        self._check = self.adoptPanel(_CheckMeshGroup(self._client))
        self._items.mount_check(layout, self._check)
        # Plan 37 UF18: what the check wrote, drawn over the mesh on demand.
        self._highlights = CheckHighlightsPanel(self._client)
        layout.addWidget(self._highlights)
        self._items.mount_below(layout)

    def renders_field(self, field_id: str) -> bool:
        return (self._items.renders_field(field_id)
                and super().renders_field(field_id))

    def aligned_forms(self):
        group = getattr(self, '_check', None)
        form = getattr(group, 'form_layout', None)
        return (form(),) if callable(form) else ()

    def refresh(self) -> None:
        super().refresh()
        group = getattr(self, '_check', None)
        if group is not None and hasattr(group, 'reload'):
            # DP-339. A refresh keeps an uncommitted edit.
            group.reload(discard_pending=False)
            # DP-156. The panel's reload aligns its own unit column alone;
            # the page puts it back in the page's one column after it.
            self._align_field_columns()
        self._moveProseBehindHelp()
        self._describeCheck()
        self._align_field_columns()

    def refresh_status(self) -> None:
        """Re-read the task's state *and* the report that produced it.

        R100/DP-528. When a pipeline ends the window refreshes the branch
        through ``refresh_states``, which calls this and not ``refresh()``,
        so a page built before the run kept what it had then.
        """
        super().refresh_status()
        for name in ('_findings', '_highlights'):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.refresh()
        self._items.refresh()

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the control DP-230 already built."""
        detail = self._items.help_detail
        if not detail:
            return
        described = self._description.text().strip()
        if detail not in described:
            described = (described + ' ' + detail).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())

    # -- the one checkMesh action ------------------------------------------ #

    _checkRunning = False

    def _runCheck(self, operation: str) -> None:
        """Run one check on the user's request and show what it said.

        The operation is the user's choice, not the target's: ``mesh.check``
        for checkMesh, ``quality.su2_readiness`` for the readiness check.
        """
        if self._checkRunning:
            return
        from foammesh.view.mesh_check_action import run_mesh_check

        self._checkRunning = True
        self._setCheckButtons(False)

        def done(result) -> None:
            self._checkRunning = False
            payload = getattr(result, 'payload', None) or {}
            verdict = (isinstance(payload.get('parsed'), dict)
                       or isinstance(payload.get('readiness'), dict))
            if getattr(result, 'status', '') == 'accepted' or verdict:
                # A verdict on the mesh, drawn by the panel below.
                self._qaNote.setVisible(False)
            else:
                self._qaNote.setText(str(
                    getattr(result, 'message', '') or payload.get('reason')
                    or self.tr('The check could not run.')))
                self._qaNote.setVisible(True)
            self._setCheckButtons(True)
            self.refresh_status()

        run_mesh_check(self._client, done, operation=operation)

    def _setCheckButtons(self, enabled: bool) -> None:
        self._runCheckMesh.setEnabled(bool(enabled))
        self._items.set_busy(not enabled)
        if enabled:
            self._describeCheck()

    def _describeCheck(self) -> None:
        """Say why an action cannot run, and show the panels that have a report.

        Availability follows the mesh, never the target.
        """
        checkmesh = getattr(self, '_runCheckMesh', None)
        if checkmesh is None:
            return
        can_run, reason = self._items.checkmesh_availability()
        if not self._checkRunning:
            checkmesh.setEnabled(can_run)
        checkmesh.setToolTip('' if can_run else reason + self.tr(
            ' checkMesh reads a polyMesh, so it has nothing to open.'))
        if self._items.run_stage_label:
            self.setRunStageLabel(self.tr(self._items.run_stage_label))
            self.setRunStageAvailable(True)
        self._findings.refresh()
        self._items.refresh()
