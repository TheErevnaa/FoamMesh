#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The four things a section can show (Plan 37 UF7, DP-1046).

Two radio buttons and a *Whole cells* tick box offered three pictures and
hid a fourth: the cells the plane passes through, on both sides of it, which
is what a user counting prism layers across a wall wants to look at. The
tick box also meant "whole cells" said nothing while *Slice* was chosen.
One selector, four modes, each named for what is drawn.
"""
from __future__ import annotations

from enum import Enum

__all__ = ['SectionMode', 'MODE_LABELS', 'MODE_TOOLTIPS']


class SectionMode(Enum):
    SLICE = 'slice'
    CUT_CELLS = 'cut_cells'
    CLIP = 'clip'
    CLIP_WHOLE_CELLS = 'clip_whole_cells'

    @property
    def clips(self) -> bool:
        """Whether a half-space is kept (the other modes draw the plane)."""
        return self in (SectionMode.CLIP, SectionMode.CLIP_WHOLE_CELLS)


#: In the order the selector lists them.
MODE_LABELS = {
    SectionMode.SLICE: 'Slice',
    SectionMode.CUT_CELLS: 'Cut cells',
    SectionMode.CLIP: 'Clip',
    SectionMode.CLIP_WHOLE_CELLS: 'Clip by whole cells',
}

MODE_TOOLTIPS = {
    SectionMode.SLICE:
        'Only the surface where the plane meets the mesh, with the cell '
        'outlines on it.',
    SectionMode.CUT_CELLS:
        'Every cell the plane passes through, whole, on both sides of it. '
        'A cell touching the plane with a face counts; the layer is as thick '
        'as the cells are, not uniform.',
    SectionMode.CLIP:
        'Remove everything on one side of the plane, cutting cells flat at '
        'the plane.',
    SectionMode.CLIP_WHOLE_CELLS:
        'Remove everything on one side of the plane, keeping every cell the '
        'plane passes through whole: a stepped face whose cell shapes stay '
        'readable.',
}
