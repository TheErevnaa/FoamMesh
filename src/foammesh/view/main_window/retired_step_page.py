#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stand-ins for the five snappy step pages the guided workflow replaced.

Plan 43 decision D1 (LY13, DP-2260). Region, Base Grid, Castellation, Snap and
Boundary Layer each had a page of their own behind `StepManager`. Every one of
their tools now lives in the guided route (`snappy.*` task pages), and the
pages were unreachable. What the step machinery still asks of them is small
and is kept here, so deleting the pages does not change a single answer:

* ``OUTPUT_TIME``: the numbered time directory the legacy model wrote the
  stage to. `StepManager.load` clears every directory above the working
  step with ``artifact.stage.clear {output_time}``, and a case saved by the
  old model still has them.
* ``isNextStepAvailable``: whether that stage has a result, with the legacy
  meaning per step (the loop that enables the outline rows reads it).
* the lock, open, show, hide, save, load and unload hooks, which do nothing:
  there is no widget content left to lock, draw or save.

The five stack pages in ``main_window.ui`` remain, empty, because the
`Step` enum is persisted and the stack index *is* the step value.
"""

from PySide6.QtWidgets import QWidget

from foammesh.app import app
from foammesh.db.configurations_schema import Step
from foammesh.view.step_page import StepPage


class RetiredStepPage(StepPage):
    """A step with no page of its own any more."""

    #: Legacy numbered output time of each retired step.
    OUTPUT_TIMES = {
        Step.REGION: -1,
        Step.BASE_GRID: 0,
        Step.CASTELLATION: 1,
        Step.SNAP: 2,
        Step.BOUNDARY_LAYER: 3,
    }

    def __init__(self, ui, step):
        shell = getattr(ui, _SHELLS[step], None)
        super().__init__(ui, shell if shell is not None else QWidget())
        self.step = Step(step)
        self.OUTPUT_TIME = self.OUTPUT_TIMES[self.step]

    def isNextStepAvailable(self):
        root = app.facadeClient.case_root
        if self.step == Step.REGION:
            # The legacy region page was "done" once the case had a region.
            return app.facadeClient.checkout().elementCount('region') > 0
        if self.step == Step.BASE_GRID:
            return (root / 'constant' / 'polyMesh' / 'boundary').exists()
        return super().isNextStepAvailable()

    # There is no widget content to lock, show, save or draw.
    def lock(self):
        self._locked = True

    def unlock(self):
        self._locked = False

    def open(self):
        return

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        return

    async def hide(self):
        return True

    def load(self):
        self._loaded = True

    def updateWorkingStatus(self):
        return

    def _enableStep(self):
        return

    def _disableStep(self):
        return


_SHELLS = {
    Step.REGION: 'regionPage',
    Step.BASE_GRID: 'baseGridPage',
    Step.CASTELLATION: 'castellationPage',
    Step.SNAP: 'snapPage',
    Step.BOUNDARY_LAYER: 'boundaryLayerPage',
}
