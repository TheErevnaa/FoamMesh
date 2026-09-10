"""The shared ``common.export`` task page.

Plan 30 WP-09 / F-17. ``common.export`` is declared by both engines and had a
page in neither registry: it was served by the legacy Designer widget through
``StepManager``'s hand-written table, so ``EngineBranchView._rebuild`` skipped
the last row of both workflows and the branch could not show, run or settle it.

The writers themselves are not duplicated here. Choosing a format, choosing a
destination and running the writer is one implementation living on
``ExportPage``; this page is the task surface that hosts it, so the app keeps
one export path and one set of dialogs rather than two that can disagree about
what "Export" means.
"""
from __future__ import annotations

import qasync
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from foammesh.app import app
from foammesh.view.workflow_controls.task_page import EngineTaskPage


class ExportTaskPage(EngineTaskPage):
    """Write the finished mesh out, in the format the target solver reads."""

    task_id_default = 'common.export'
    #: Nothing to run: this task writes a file, it does not mesh. §7.1 keeps
    #: Run out of this page's vocabulary entirely.
    run_stage = None
    run_all_task_id = None

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)

    # -- construction ------------------------------------------------------ #

    def build_sections(self, layout) -> None:
        self._note = QLabel(self.tr(
            'Export writes the mesh to a destination you choose, in a format '
            'the target solver reads. It does not re-mesh: the mesh that is '
            'in the case is the mesh that is written.'), self)
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        self._readiness = QLabel('', self)
        self._readiness.setObjectName('exportReadiness')
        self._readiness.setWordWrap(True)
        self._readiness.setProperty('foammeshStatus', 'warning')
        layout.addWidget(self._readiness)

        self._export = QPushButton(self.tr('Export Mesh'), self)
        self._export.setObjectName('exportMesh')
        self._plane = QPushButton(self.tr('Export as 2D (plane)'), self)
        self._plane.setObjectName('export2DPlane')
        self._wedge = QPushButton(self.tr('Export as 2D (wedge)'), self)
        self._wedge.setObjectName('export2DWedge')
        self._export.clicked.connect(self._onExport)
        self._plane.clicked.connect(lambda: self._open2D('plane'))
        self._wedge.clicked.connect(lambda: self._open2D('wedge'))
        row = QHBoxLayout()
        for button in (self._export, self._plane, self._wedge):
            row.addWidget(button)
        row.addStretch(1)
        holder = QWidget(self)
        holder.setLayout(row)
        layout.addWidget(holder)

    # -- the one export implementation ------------------------------------- #

    @staticmethod
    def legacyPage():
        """The object that owns the format chooser and the writers.

        Reached rather than reimplemented: two copies of "which operation
        writes which format" is exactly the kind of second home this work
        package exists to remove.
        """
        window = getattr(app, 'window', None)
        manager = getattr(window, '_stepManager', None)
        accessor = getattr(manager, 'exportPage', None)
        return accessor() if accessor is not None else None

    def canExport(self) -> bool:
        page = self.legacyPage()
        ask = getattr(page, 'canExport', None)
        if ask is None:
            return True
        try:
            return bool(ask())
        except Exception:                                    # noqa: BLE001
            return True

    def refresh(self) -> None:
        super().refresh()
        if not hasattr(self, '_export'):
            return
        exportable = self.canExport()
        for button in (self._export, self._plane, self._wedge):
            button.setEnabled(exportable)
        # R50 over again on the new surface: a disabled button the user
        # cannot explain is worse than one that fails with a message.
        self._readiness.setText('' if exportable else self.tr(
            'There is no mesh in this case yet, so there is nothing to '
            'export. Mesh it first.'))
        self._readiness.setVisible(not exportable)

    @qasync.asyncSlot()
    async def _onExport(self):
        page = self.legacyPage()
        opener = getattr(page, 'openExportDialog', None)
        if opener is None:
            return
        await opener()
        self.refresh()

    def _open2D(self, mode: str) -> None:
        page = self.legacyPage()
        opener = getattr(page, 'open2DExtrudeDialog', None)
        if opener is not None:
            opener(mode)


class SnappyExportPage(ExportTaskPage):
    engine_id = 'snappy'


class GmshExportPage(ExportTaskPage):
    engine_id = 'gmsh'
