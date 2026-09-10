#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Linear-dependency workflow graph with staleness propagation.

The meshing stages mirror the snappy pipeline (and the existing ``Step`` enum)
but the semantics change: editing/invalidating a stage marks it and everything
downstream STALE — previous results are not deleted, just flagged for re-run.
"""
from __future__ import annotations

from enum import Enum, IntEnum


class Stage(IntEnum):
    GEOMETRY = 0
    GEOMETRY_REPAIR = 1
    REGION = 2
    BASE_GRID = 3
    CASTELLATION = 4
    SNAP = 5
    BOUNDARY_LAYER = 6
    EXPORT = 7


class StageStatus(str, Enum):
    EMPTY = 'empty'     # never run
    DONE = 'done'       # completed and up-to-date
    STALE = 'stale'     # completed but an upstream change invalidated it


class WorkflowGraph:
    """Each stage depends on the immediately preceding one (a chain)."""

    def __init__(self):
        self._status: dict[Stage, StageStatus] = {s: StageStatus.EMPTY for s in Stage}

    def status(self, stage: Stage) -> StageStatus:
        return self._status[stage]

    def complete(self, stage: Stage) -> None:
        self._status[stage] = StageStatus.DONE

    def invalidate(self, stage: Stage) -> None:
        """Mark *stage* and all downstream DONE stages STALE (never deletes)."""
        for s in Stage:
            if s >= stage and self._status[s] is StageStatus.DONE:
                self._status[s] = StageStatus.STALE

    def edit(self, stage: Stage) -> None:
        """An edit to *stage* invalidates strictly downstream stages."""
        for s in Stage:
            if s > stage and self._status[s] is StageStatus.DONE:
                self._status[s] = StageStatus.STALE

    def stale_stages(self) -> list[Stage]:
        return [s for s in Stage if self._status[s] is StageStatus.STALE]

    def is_runnable(self, stage: Stage) -> bool:
        """A stage can run once every upstream stage is DONE."""
        return all(self._status[s] is StageStatus.DONE for s in Stage if s < stage)

    def next_runnable(self) -> Stage | None:
        for s in Stage:
            if self._status[s] in (StageStatus.EMPTY, StageStatus.STALE) and self.is_runnable(s):
                return s
        return None

    def to_dict(self) -> dict:
        return {s.name: self._status[s].value for s in Stage}

    def load(self, d: dict) -> None:
        for name, value in (d or {}).items():
            try:
                self._status[Stage[name]] = StageStatus(value)
            except (KeyError, ValueError):
                pass
