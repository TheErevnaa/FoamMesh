#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Authored export page as a thin desktop-facade client."""
import asyncio

import qasync
from PySide6.QtCore import Signal

from foammesh.app import app
from foammesh.view.step_page import StepPage
from widgets.progress_dialog import ProgressDialog

from .export_form import (
    EXPORT_OPERATIONS, MESH_FORMATS, extrude_payload, inline_export_page)
from .export_2D_plane_dialog import Export2DPlaneDialog
from .export_2D_wedge_dialog import Export2DWedgeDialog
from foammesh.view.facade_client import query


class ExportPage(StepPage):
    OUTPUT_TIME = 4

    #: R104/R162. A file was written. Measured on both engines: the dialog
    #: reported "Export completed" over a full `case/` tree and the Export
    #: row of the outline stayed grey, because nothing told the workflow
    #: that the thing it was waiting for had happened.
    exported = Signal()

    def __init__(self, ui):
        super().__init__(ui, ui.exportPage)
        self._dialog = None
        self._ui.export_.setText(self.tr('Export mesh'))
        self._connectSignalsSlots()

    def isNextStepAvailable(self):
        return False

    def canExport(self) -> bool:
        """Whether this case holds a mesh worth writing out (R50).

        The Export buttons were live on a case with no geometry, no region
        and no mesh, where pressing one can only produce a failure or an
        empty case directory. A probe that cannot answer never takes the
        buttons away: a disabled Export the user cannot explain is worse
        than an Export that fails with a message.

        DP-62. It used to ask only whether there is a ``constant/polyMesh``,
        and a Gmsh run targeting SU2 publishes none: the solver reads the
        file Gmsh wrote. MEASURED in leg t7-su2 -- ten cases meshed, ten
        exports refused, every one of them on this line, with a ``mesh.su2``
        sitting in the run directory the whole time. So the question is the
        wider one, and the older key is still the fallback for a payload
        written before there was a wider one to ask.
        """
        try:
            payload = query(app.facadeClient, 'case.classify').payload or {}
        except Exception:                                     # noqa: BLE001
            return True
        return bool(payload.get('has_exportable_mesh',
                                payload.get('has_mesh', True)))

    def _updateControlButtons(self):
        # R50. Called from `updateWorkingStatus()` every time the page is
        # shown, which is when the mesh either exists or does not.
        exportable = self.canExport()
        for button in (self._ui.export_, self._ui.export2DPlane,
                       self._ui.export2DWedge):
            button.setEnabled(exportable)
        message = getattr(self._ui, 'validationMessage', None)
        if message is not None:
            message.setText('' if exportable else self.tr(
                'There is no mesh in this case yet, so there is nothing to '
                'export. Mesh it first.'))

    def _connectSignalsSlots(self):
        self._ui.export_.clicked.connect(self._openExport3DDialog)
        self._ui.export2DPlane.clicked.connect(self._openExport2DPlaneDialog)
        self._ui.export2DWedge.clicked.connect(self._openExport2DWedgeDialog)

    #: Plan 28 WP3. The mesh-file formats this page offers. The in-case
    #: utilities (``openfoam_format``, ``fluent``) rewrite the case in place
    #: rather than producing a deliverable at a destination, so they do not
    #: belong behind a "where do you want it" dialog; they stay on the menu.
    #:
    #: Plan 31 FC-F, ledger row ``export-formats-unexposed``. MED and UNV are
    #: here because FC-A measured that the Gmsh writer keeps every physical
    #: group name in them and this list was the reason no user could ask for
    #: one: MEASURED on the merged tree before this change, the entries the
    #: dialog could list were openfoam, vtk, gmsh, cgns and su2, and nothing
    #: else -- the two ``FormatSpec`` rows FC-A added fed a registry the
    #: dialog does not read. ``.vtk`` from the same writer is deliberately
    #: still absent: FC-A measured it back with the right element counts and
    #: no physical groups at all.
    #:
    #: Plan 33 EXPORT-04. Both lists live in ``export_form`` now, beside the
    #: form that reads them, and are bound here under the names this page has
    #: always offered them under. Two copies of "which operation writes which
    #: format" is exactly the second home this package exists to remove.
    MESH_FORMATS = MESH_FORMATS

    #: entry_id -> the operation that writes it. OpenFOAM keeps the authored
    #: pipeline it has always used; the rest are single-file writers.
    EXPORT_OPERATIONS = EXPORT_OPERATIONS

    @qasync.asyncSlot()
    async def _openExport3DDialog(self):
        await self.openExportDialog()

    async def openExportDialog(self) -> bool:
        """Choose a format and a destination, then write it. Awaitable.

        The wizard's Export row needs to know whether an export actually
        happened before it marks the workflow complete, so this resolves to
        that answer rather than firing a signal into the dark.

        Plan 33 EXPORT-04. When the Export step is on screen, its form has
        already been answered and the press is an export of what the reader
        can see; opening a window to ask the same questions a second time is
        the fault this package exists to remove. The window stays for the
        menu route, where there is no form on screen to answer.
        """
        step = inline_export_page()
        if step is not None:
            # UXA7 (#56). Once the mesh is written and nothing has been
            # changed, the press opens the folder and exports nothing.
            if step.openFolderMode():
                step.openFolder()
                return False
            return bool(await step.runExport())
        # Plan 43 LY13 (D6). There is no second window: the Export page is
        # the one place an export is asked for. A press that arrives while
        # that page is not on screen takes the reader to it, and the export
        # is then the reader's to start with what the page shows.
        if not self._showExportPage():
            return False
        step = inline_export_page()
        if step is None:
            return False
        return bool(await step.runExport())

    def _showExportPage(self) -> bool:
        """Select the Export task of the workflow so its page is on screen."""
        window = getattr(app, 'window', None)
        manager = getattr(window, '_stepManager', None)
        branch = getattr(getattr(manager, '_methodBranch', None),
                         'branch', None)
        view = getattr(manager, '_methodBranch', None)
        select = getattr(view, 'select', None)
        if branch is None or select is None:
            return False
        try:
            return bool(select('common.export'))
        except Exception:                                    # noqa: BLE001
            return False

    def _openExport2DPlaneDialog(self):
        self._openExportDialog(Export2DPlaneDialog(self._widget), True)

    def _openExport2DWedgeDialog(self):
        self._openExportDialog(Export2DWedgeDialog(self._widget), True)

    def open2DExtrudeDialog(self, mode: str):
        if mode == 'plane':
            self._openExport2DPlaneDialog()
        elif mode == 'wedge':
            self._openExport2DWedgeDialog()
        else:
            raise ValueError('2D extrusion mode must be plane or wedge')

    def _openExportDialog(self, dialog, to2d=False):
        self._dialog = dialog
        self._dialog.accepted.connect(lambda: self._export(to2d))
        self._dialog.rejected.connect(self._ui.menubar.repaint)
        self._dialog.open()

    @staticmethod
    def _options_payload(options):
        # Plan 33 EXPORT-04. One spelling of the extrusion payload, in the
        # module the form lives in, because the inline step writes the same
        # one and a second copy is a second chance to send a solver a wedge
        # with somebody else's angle in it.
        return extrude_payload(options)

    def _chosenOperation(self, to2d: bool) -> str:
        """Which writer the user picked.

        The 2D dialogs extrude into an OpenFOAM case and offer no format, so
        they always take the authored route; so does a 3D dialog opened
        before the format list arrived.
        """
        if to2d or not hasattr(self._dialog, 'selectedFormat'):
            return 'case.export.authored'
        return self.EXPORT_OPERATIONS.get(
            self._dialog.selectedFormat(), 'case.export.authored')

    @qasync.asyncSlot()
    async def _export(self, to2d=False):
        await self._runExport(to2d)

    async def _runExport(self, to2d=False) -> bool:
        """Write what the open dialog asked for."""
        operation = self._chosenOperation(to2d)
        parameters = {'destination': str(self._dialog.projectPath())}
        # The 2D dialogs are an extrusion by construction; the shared form
        # carries the same choice as a setting, and either way the regions
        # and the extrusion travel with the destination.
        extruding = to2d or bool(
            getattr(self._dialog, 'dimensionality', lambda: '')())
        if extruding:
            boundaries, options = self._dialog.extrudeOptions()
            parameters.update({'boundaries': boundaries,
                               'options': self._options_payload(options)})
        return await self.runExportOperation(operation, parameters)

    async def runExportOperation(self, operation, parameters) -> bool:
        """Run one export operation, and say whether a file was produced.

        Plan 33 EXPORT-04. The step page reaches this rather than repeating
        it: what an export needs around it -- a saved case, a progress
        window, a cleared stage, the console, and the one announcement that
        settles the Export row -- is the same act wherever it was asked for.
        """
        # Exporting from a case that lives in the temporary directory produces
        # a real deliverable whose source is about to be swept, so the case
        # gets a home first.
        if not await app.window._requireSavedCase(self.tr('exporting')):
            return False
        # Plan 39 VI1 (F6b). Cancel asks the case to stop what it is running
        # (the OpenFOAM utilities of the authored export, a snapshot copy), the
        # same request every other progress surface makes. A writer that runs in
        # this process and is already writing finishes its file, and the dialog
        # then says so rather than claiming a stop that did not happen.
        progress = ProgressDialog(self._widget, self.tr('Mesh exporting'),
                                  cancelable=True, autoCloseOnCancel=False,
                                  onViewport=True)
        progress.setLabelText(self.tr('Preparing…'))
        progress.open()
        cancelRequested = []

        def _cancelExport():
            cancelRequested.append(True)
            progress.setLabelText(self.tr('Cancelling the export…'))
            self._cancelTask = asyncio.ensure_future(
                app.facadeClient.cancel_active_jobs())

        progress.cancelClicked.connect(_cancelExport)
        self.lock()
        # C31-12. `clearResult()` schedules its write now, and this one clears
        # the stage *before* the export writes it again, so it is awaited: a
        # clear that landed after the export would drop the result the export
        # had just recorded.
        cleared = self.clearResult()
        if cleared is not None:
            await cleared
        console = app.consoleView
        console.clear()
        try:
            if operation == 'case.export.authored':
                # The authored pipeline runs OpenFOAM utilities and has
                # progress to report. The single-file writers do not run
                # anything, and a callback they never call is only noise.
                parameters = dict(parameters,
                                  on_line=console.append,
                                  on_progress=progress.setLabelText)
            result = await app.facadeClient.run(operation, parameters)
            if result.status != 'accepted':
                raise RuntimeError(result.payload.get('error', 'export operation failed'))
            progress.finish(self.tr('Export completed'))
            # R104/R162. The one moment at which the Export task is finished,
            # whichever button opened the dialog. It used to be announced only
            # to the progress dialog the user was about to dismiss.
            self.exported.emit()
            return True
        except Exception as error:
            # Plan 32 check 8. This branch used to call `clearResult()`, and
            # `artifact.stage.clear` is not a private tidy-up: when it removes
            # anything it returns through `_artifact_result` with
            # `invalidates=('mesh', 'quality')` and publishes
            # ARTIFACT_MESH_CHANGED. So the one branch that runs when an
            # export produced no file was the branch that told the rest of the
            # application the mesh had changed -- staling the quality reports
            # and dropping the viewport actor over an SU2 writer that was
            # unavailable or a destination that was not writable. A failed
            # optional export is a statement about the export. The accepted
            # native result stays inspectable; the pre-write clear above is
            # the only clear this method performs.
            console.appendError(str(error))
            if cancelRequested:
                console.appendError(self.tr('Export cancelled.'))
                progress.finish(self.tr('Export cancelled'))
            else:
                progress.finish(self.tr('Export failed: {0}').format(error))
            return False
        finally:
            self.unlock()
