#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path

import qasync
from PySide6.QtCore import QObject, Signal

from foammesh.app import app
from foammesh.view.facade_client import submit
from foammesh.view.main_window.main_window_ui import Ui_MainWindow


class StepPage(QObject):
    OUTPUT_TIME = -1

    stepCompleted = Signal()
    stepReset = Signal()

    def __init__(self, ui, page):
        super().__init__()
        self._ui: Ui_MainWindow = ui

        self._widget = page
        self._loaded = False
        self._locked = False

        self._batchRunning = False

    def isNextStepAvailable(self):
        root = app.facadeClient.case_root
        return ((root / str(self.OUTPUT_TIME)).exists()
                or (root / 'processor0' / str(self.OUTPUT_TIME)).exists())

    def lock(self):
        self._disableStep()
        self._locked = True

    def unlock(self):
        self._enableStep()
        self._locked = False

    def open(self):
        return

    async def requireSavedCase(self) -> bool:
        """Give an untitled case a home before this step writes a mesh (H11).

        The two engines asked for this at different moments: Gmsh before it
        meshed, snappy not until export -- twenty minutes later, by which
        time the work lived in a temporary folder described as "cleaned up
        automatically". Gmsh's placement is the right one, so every stage a
        snappy page runs asks here too. Already-saved cases never see it.
        """
        return await app.window._requireSavedCase(self.tr('meshing'))

    def stageFailureDetail(self, execution) -> str:
        """Say why a stage failed, and where the rest of the story is (R36).

        Every snappy stage page reported a failure in four words -- "Snapping
        failed.", "Castellation refinement failed.", "Failed to apply boundary
        layers." The utility's own message, which is the only text that
        identifies the failure, went to `foammesh/logs/workflow-run_stage-*`
        and nowhere else; a core-dumping snappyHexMesh looked exactly like a
        cancelled one. The utility's message and the log's path now travel
        with the dialog, and the log is echoed to the Console so the stack
        trace is a scroll away rather than a file hunt.
        """
        payload = getattr(execution, 'payload', None) or {}
        job = payload.get('job') or {}
        parts = []
        reason = str(payload.get('reason') or job.get('error') or '').strip()
        if reason:
            parts.append(reason)
        elif job.get('status') == 'cancelled':
            parts.append(str(self.tr('The run was cancelled.')))
        log_path = job.get('log_path')
        if log_path:
            self._echoLog(Path(log_path))
            parts.append(str(self.tr('Log: {0}')).format(log_path))
        if not parts:
            parts.append(str(self.tr('The Console pane holds the output.')))
        return '\n\n'.join(parts)

    def _echoLog(self, path: Path) -> None:
        """Put a stage log into the Console, where the user is looking."""
        console = getattr(app, 'consoleView', None)
        if console is None or not path.is_file():
            return
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return
        for line in text.splitlines():
            console.append(line)

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        self.updateWorkingStatus()

    async def hide(self):
        if not self._loaded:
            return True

        return await self.save()

    @qasync.asyncSlot()
    async def save(self):
        return True

    def unload(self):
        self._loaded = False
        self._locked = False
        self._clear()

    def load(self):
        pass
    
    def retranslate(self):
        return

    def clearResult(self):
        """Drop this stage's recorded result.

        C31-12. Scheduled, not blocking: clearing a stage deletes a time
        directory, and every page above the working step is cleared on each
        reload, so this ran as a burst of blocking deletes during a redraw.

        Returns the task when the clear was scheduled, so a coroutine that
        clears *before* it writes can await it and keep the old order. Every
        other caller clears after its run and does not need to.
        """
        return submit(app.facadeClient, 'artifact.stage.clear',
                      {'output_time': self.OUTPUT_TIME})

    def updateWorkingStatus(self):
        self.updateMesh()
        self._updateControlButtons()

    def _outputPath(self) -> Path:
        return app.facadeClient.case_root / str(self.OUTPUT_TIME)

    def _updateNextStepAvailable(self):
        if self.isNextStepAvailable():
            self.stepCompleted.emit()
        else:
            self.stepReset.emit()

    def _displayTime(self, time: int) -> int:
        """Which time the mesh a page wants to show actually lives at.

        A numbered time directory is honoured when one is really there, so a
        case written by the old model still displays the stage it asks for.
        Otherwise the answer is 0 -- `constant/polyMesh` -- because that is
        where every in-place stage puts its result (R29/R63/R70).
        """
        if time <= 0:
            return 0
        root = app.facadeClient.case_root
        if ((root / str(time)).exists()
                or (root / 'processor0' / str(time)).exists()):
            return time
        return 0

    def _showResultMesh(self):
        if self.OUTPUT_TIME >= 0:
            app.window.meshManager.show(self._displayTime(self.OUTPUT_TIME))

    def _showPreviousMesh(self):
        if self.OUTPUT_TIME > 0:
            app.window.meshManager.show(
                self._displayTime(self.OUTPUT_TIME - 1))

    async def _reloadResultMesh(self, stage: str = '') -> None:
        """Put the mesh a stage just wrote in front of the user (R95).

        This page hierarchy still speaks the numbered-time-directory model
        it was born with: `updateMesh()` asks `meshManager.show(OUTPUT_TIME)`
        for a mesh under `<case>/1`, `<case>/2`, `<case>/3`. The snappy
        engine has not written those in a long time -- every phase overwrites
        `constant/polyMesh` in place, which is why R84 had to snapshot the
        input mesh at all -- so those directories never exist, `show()` finds
        no actors, and the viewport keeps whatever it was already holding.
        MEASURED on venturi.stl: castellation (36,533 cells), snap, and
        layers (65,320 cells) each completed with the toolbar beside them
        reading `0 cells` and the viewport still showing only the STL.

        Time 0 reads `constant/polyMesh`, which is where the result of every
        one of those stages actually is.
        """
        manager = getattr(app.window, 'meshManager', None)
        load = getattr(manager, 'load', None)
        if load is None:
            return
        # DP-96. ``stage`` names the button that made this mesh, so the
        # line beside it says which of the three snappy phases the user
        # is looking at. Three consecutive meshes of the same object are
        # otherwise told apart only by their cell counts.
        await load(0, stage=stage)

    def updateMesh(self):
        if self.isNextStepAvailable():
            self._showResultMesh()
        else:
            self._showPreviousMesh()

    def _updateControlButtons(self):
        return

    def _enableStep(self):
        self._widget.setEnabled(True)

    def _disableStep(self):
        self._widget.setEnabled(False)

    def _clear(self):
        return
