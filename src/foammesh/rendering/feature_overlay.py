#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Draw the features the geometry declared, coloured by whether they survived.

Plan 27 WP3.1. A blade's leading edge rounded by a millimetre moves almost no
area, so the area-weighted surface statistic barely registers it -- which is why
Plan 23 §6.3 measures features separately and lets a critical one below
threshold fail its section outright.

Until now that check never ran, so there was nothing to draw. With it wired,
"did the mesher keep my features" becomes a question you answer by looking:
the declared feature lines are drawn over the mesh, green where they survived,
amber where they moved, red where they were rounded away.

Drawn as a single actor per verdict rather than one per feature: a case can
declare hundreds, and hundreds of props is a frame-rate problem for a picture
that is three colours.
"""
from __future__ import annotations

from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData, vtkPolyLine
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

from foammesh.view.theming.vtk_theme import rgb


#: Feature verdict -> the status token its colour comes from. Roles rather than
#: hex, so this follows the theme like every other coloured thing.
VERDICT_ROLES = {
    'pass': 'status.success',
    'warning': 'status.warning',
    'incomplete': 'status.warning',
    'fail': 'status.error',
    'unrated': 'foreground.muted',
}

#: Feature lines sit *on* the surface they belong to, so they need to win the
#: depth fight or they strobe through it as the camera moves.
LINE_WIDTH = 3.0
CRITICAL_LINE_WIDTH = 5.0


def _polyline_data(records) -> vtkPolyData | None:
    """One polydata holding every record's polyline."""
    points = vtkPoints()
    lines = vtkCellArray()
    total = 0
    for record in records:
        coordinates = record.get('points') or ()
        if len(coordinates) < 2:
            continue
        polyline = vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(coordinates))
        for index, point in enumerate(coordinates):
            points.InsertNextPoint(float(point[0]), float(point[1]),
                                   float(point[2]))
            polyline.GetPointIds().SetId(index, total + index)
        total += len(coordinates)
        lines.InsertNextCell(polyline)

    if not total:
        return None
    data = vtkPolyData()
    data.SetPoints(points)
    data.SetLines(lines)
    return data


def build_actors(sections: dict, tokens=None) -> list:
    """Actors for every drawable feature, grouped by verdict.

    ``sections`` is ``solver_name -> [feature records]`` as written beside the
    fidelity report. Returns an empty list when nothing is drawable, which is
    the normal state for a case whose features were never measured.
    """
    byVerdict: dict[tuple[str, bool], list] = {}
    for records in (sections or {}).values():
        for record in records or ():
            verdict = str(record.get('verdict') or 'unrated')
            critical = bool(record.get('critical'))
            byVerdict.setdefault((verdict, critical), []).append(record)

    actors = []
    for (verdict, critical), records in sorted(byVerdict.items()):
        data = _polyline_data(records)
        if data is None:
            continue
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        mapper.ScalarVisibilityOff()
        # Feature lines lie exactly on the surface they describe; without the
        # offset they z-fight it and flicker as the camera moves.
        mapper.SetResolveCoincidentTopologyToPolygonOffset()

        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName(f'features:{verdict}')
        prop = actor.GetProperty()
        prop.SetLighting(False)
        prop.SetRenderLinesAsTubes(True)
        prop.SetLineWidth(
            CRITICAL_LINE_WIDTH if critical else LINE_WIDTH)
        role = VERDICT_ROLES.get(verdict, 'foreground.muted')
        colour = tokens.value(role) if tokens is not None else '#c62828'
        prop.SetColor(*rgb(colour))
        # Clicking must still resolve to a patch; there is no Display Control
        # row for a feature line.
        actor.PickableOff()
        actors.append(actor)

    return actors


def summarise(sections: dict) -> dict:
    """``verdict -> count`` over every drawable feature."""
    counts: dict[str, int] = {}
    for records in (sections or {}).values():
        for record in records or ():
            verdict = str(record.get('verdict') or 'unrated')
            counts[verdict] = counts.get(verdict, 0) + 1
    return counts
