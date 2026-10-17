#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Colour mesh by > Layers grown (Plan 39 VI16, I6).

The most common snappy question is "did my layers grow, and where did they
collapse". The answer already exists per patch in the layer report
(`core/quality/layer_report.py`); this module decides what each patch is
painted with, with no Qt and no VTK scene:

* a patch that asked for layers is painted with the count the run **achieved**
  on it, on one table spanning 0 to the largest request, so a collapsed patch
  is the red end and a complete one the green end, and two patches with the
  same count have the same colour;
* a patch that asked for none (nothing requested, or frozen) is grey -- not on
  the table, because "0 of 0" is not a collapse;
* a patch the report does not name is grey too: nobody measured it.
"""
from __future__ import annotations

from dataclasses import dataclass

from vtkmodules.vtkCommonCore import vtkLookupTable

#: The colour of a patch that asked for no layers (r, g, b).
GREY = (0.62, 0.62, 0.62)

#: The key the legend is filed under in the viewport's stack of legends.
LEGEND_KEY = 'layersGrown'

#: The choices of *Colour mesh by*: (key, label).
SOLID = 'solid'
LAYERS_GROWN = 'layers_grown'
#: The volume's exterior, by the section's own choices (`section_colour`).
VOLUME_CHOICES = (('cell_type', 'Cell type'),
                  ('cell_level', 'Refinement level'),
                  ('cell_zone', 'Region / zone'),
                  ('quality.cellAspectRatio', 'Quality: Aspect ratio'),
                  ('quality.nonOrthoAngle', 'Quality: Non-orthogonality'),
                  ('quality.skewness', 'Quality: Skewness'),
                  ('quality.cellVolume', 'Quality: Cell volume'))
CHOICES = ((SOLID, 'Solid'), (LAYERS_GROWN, 'Layers grown'),
           *VOLUME_CHOICES)


def is_volume_choice(key) -> bool:
    return any(key == choice for choice, _label in VOLUME_CHOICES)


@dataclass(frozen=True)
class LayerPaint:
    """What one patch is painted with."""
    patch: str
    #: The achieved layer count, or ``None`` when the patch is grey.
    achieved: float | None
    requested: float = 0.0


def asked(row) -> bool:
    """Whether a layer-report row is a patch that asked for layers."""
    if getattr(row, 'frozen', False):
        return False
    # A report that recorded no request still shows layers that grew.
    return (float(getattr(row, 'requested', 0) or 0) > 0
            or float(getattr(row, 'layers', 0) or 0) > 0)


def plan(rows, actorIds) -> dict:
    """``actor id -> LayerPaint`` for every patch actor in *actorIds*.

    *rows* are the `view_modes.LayerRow`s of the stored report. An actor id is
    a patch name, or ``region:patch`` on a multi-region mesh.
    """
    byPatch = {str(row.patch): row for row in rows or ()}
    painted = {}
    for actorId in actorIds:
        name = str(actorId).rsplit(':', 1)[-1]
        row = byPatch.get(name)
        if row is not None and asked(row):
            painted[actorId] = LayerPaint(
                name, float(row.layers),
                max(float(row.requested or 0), float(row.layers)))
        else:
            painted[actorId] = LayerPaint(name, None)
    return painted


def top(painted) -> float:
    """The top of the table: the largest request among the painted patches."""
    return max([p.requested for p in painted.values()
                if p.achieved is not None] or [1.0])


def lookup_table(high: float) -> vtkLookupTable:
    """The standard scale, red where nothing grew."""
    from foammesh.support import colour_standard
    # Low coverage is the red end.
    return colour_standard.lookup_table(
        'layers', 0.0, max(float(high), 1.0), worst_is_high=False)


def colour_of(table: vtkLookupTable, value: float) -> tuple:
    """The (r, g, b) the table paints *value* with."""
    rgb = [0.0, 0.0, 0.0]
    table.GetColor(float(value), rgb)
    return tuple(rgb)


def legend_title() -> str:
    return 'Layers grown'
