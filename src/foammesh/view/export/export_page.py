#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Authored export page as a thin desktop-facade client."""
import asyncio

import qasync
from PySide6.QtCore import Signal

from foammesh.app import app
from foammesh.view.step_page import StepPage
from widgets.progress_dialog import ProgressDialog

from .export_dialog import ExportDialog
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
        self._ui.export_.setText(self.tr('Export Mesh'))
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
        """
        try:
            payload = query(app.facadeClient, 'case.classify').payload or {}
        except Exception:                                     # noqa: BLE001
            return True
        return bool(payload.get('has_mesh', True))

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
    MESH_FORMATS = ('openfoam', 'su2', 'gmsh', 'vtk', 'cgns', 'med', 'unv')

    #: entry_id -> the operation that writes it. OpenFOAM keeps the authored
    #: pipeline it has always used; the rest are single-file writers.
    EXPORT_OPERATIONS = {
        'openfoam': 'case.export.authored',
        'su2': 'case.export.su2',
        'gmsh': 'case.export.gmsh',
        'vtk': 'case.export.vtk',
        'cgns': 'case.export.cgns',
        'med': 'case.export.med',
        'unv': 'case.export.unv',
    }

    @qasync.asyncSlot()
    async def _openExport3DDialog(self):
        await self.openExportDialog()

    async def openExportDialog(self) -> bool:
        """Choose a format and a destination, then write it. Awaitable.

        The wizard's Export row needs to know whether an export actually
        happened before it marks the workflow complete, so this resolves to
        that answer rather than firing a signal into the dark.
        """
        entries, recommended, solver_name = await self._exportFormats()
        dialog = ExportDialog(self._widget, entries, recommended, solver_name)
        if not await self._askDialog(dialog):
            return False
        return await self._runExport()

    async def _askDialog(self, dialog) -> bool:
        """Show a modeless dialog and wait for the user to answer it."""
        self._dialog = dialog
        answered = asyncio.get_event_loop().create_future()

        def settle(accepted):
            if not answered.done():
                answered.set_result(accepted)

        dialog.accepted.connect(lambda: settle(True))
        dialog.rejected.connect(lambda: settle(False))
        dialog.open()
        accepted = await answered
        if not accepted:
            self._ui.menubar.repaint()
        return accepted

    async def _exportFormats(self):
        """Ask the facade what this case can be exported as, and to what.

        A failure here is not a reason to refuse to export: the dialog falls
        back to its historical single-format behaviour, which is the OpenFOAM
        case it could always write.
        """
        try:
            result = await app.facadeClient.run('case.export.entries')
        except Exception:
            return (), 'openfoam', ''
        payload = result.payload or {}
        entries = tuple(entry for entry in payload.get('entries', ())
                        if entry.get('entry_id') in self.MESH_FORMATS)
        solver = str(payload.get('target_solver') or 'unselected')
        return (entries,
                str(payload.get('recommended_entry_id') or 'openfoam'),
                str(payload.get('target_solver_name') or '')
                if solver != 'unselected' else '')

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
        return {
            'model': options.model.value, 'thickness': options.thickness,
            'point': options.point, 'axis': options.axis, 'angle': options.angle,
        }

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
        """Write the chosen format, and say whether a file was produced."""
        # Exporting from a case that lives in the temporary directory produces
        # a real deliverable whose source is about to be swept, so the case
        # gets a home first.
        if not await app.window._requireSavedCase(self.tr('exporting')):
            return False
        progress = ProgressDialog(self._widget, self.tr('Mesh Exporting'))
        progress.setLabelText(self.tr('Preparing'))
        progress.open()
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
            operation = self._chosenOperation(to2d)
            parameters = {'destination': str(self._dialog.projectPath())}
            if operation == 'case.export.authored':
                # The authored pipeline runs OpenFOAM utilities and has
                # progress to report. The single-file writers do not run
                # anything, and a callback they never call is only noise.
                parameters.update({'on_line': console.append,
                                   'on_progress': progress.setLabelText})
            if to2d:
                boundaries, options = self._dialog.extrudeOptions()
                parameters.update({
                    'boundaries': boundaries,
                    'options': self._options_payload(options),
                })
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
            self.clearResult()
            console.appendError(str(error))
            progress.finish(self.tr('Export failed. ') + str(error))
            return False
        finally:
            self.unlock()
