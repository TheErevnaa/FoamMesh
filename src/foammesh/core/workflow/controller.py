#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Workflow controller: run only stale/empty stages, in dependency order.

Orchestrates the WorkflowGraph: given a per-stage *run* callable (which actually
invokes blockMesh/snappy/etc.), it runs the next runnable stage, marks it DONE,
and optionally checkpoints it. Non-destructive: upstream edits only mark
downstream STALE (handled by the graph), and re-running rebuilds just those.

Headless and unit-testable with a fake run callable; the GUI step_manager and the
OpenFOAM runners plug in the real callable.
"""
from __future__ import annotations

from typing import Callable

from .graph import Stage, StageStatus, WorkflowGraph


class WorkflowController:
    def __init__(self, graph: WorkflowGraph | None = None,
                 run: Callable[[Stage], bool] | None = None,
                 checkpoints=None):
        self.graph = graph or WorkflowGraph()
        self._run = run or (lambda stage: True)
        self.checkpoints = checkpoints

    def run_stage(self, stage: Stage) -> bool:
        if not self.graph.is_runnable(stage):
            raise RuntimeError(f'{stage.name} not runnable: upstream stages incomplete')
        ok = self._run(stage)
        if ok:
            self.graph.complete(stage)
            if self.checkpoints is not None:
                try:
                    self.checkpoints.save(stage.name)
                except FileNotFoundError:
                    pass  # nothing to checkpoint yet (e.g. geometry stage)
        return ok

    def run_pending(self) -> list[Stage]:
        """Run every empty/stale runnable stage to completion, in order."""
        ran: list[Stage] = []
        while True:
            stage = self.graph.next_runnable()
            if stage is None:
                break
            if not self.run_stage(stage):
                break
            ran.append(stage)
        return ran

    def edit_stage(self, stage: Stage) -> list[Stage]:
        """Record an edit to *stage*; return the now-stale downstream stages."""
        self.graph.edit(stage)
        return self.graph.stale_stages()

    def rollback(self, stage: Stage) -> bool:
        """Restore a stage's checkpoint and mark it (and downstream) stale-from-here."""
        if self.checkpoints is None or not self.checkpoints.restore(stage.name):
            return False
        # everything strictly after the restored stage is now stale
        for s in Stage:
            if s > stage and self.graph.status(s) is StageStatus.DONE:
                self.graph.invalidate(s)
        return True
