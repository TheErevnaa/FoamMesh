#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""What a stage's mesh should look like, and what it should say it is.

DP-95. Every actor this product builds is constructed with
``DisplayMode.SURFACE`` and nothing ever changed it, so a castellated mesh,
a snapped mesh and a layered mesh were three pictures of the same smooth
blob: the cells the stage had just spent ninety seconds making were behind a
skin with no edges drawn on it. Seeing the mesh meant opening Display
Control, selecting every row and picking "Surface with edges" -- by hand,
after every stage, in a workflow whose entire subject is the mesh.

DP-96. Nothing on screen said how big that mesh was in model units. The
toolbar's cell count answers "how many"; "is this 0.42 m or 0.42 mm" was
answerable only by going back to the Base Grid page, and what *kind* of
artifact a stage had produced -- a surface triangulation or a volume -- was
never stated at all.

The rules live here, as data, so they can be measured without a renderer and
without a window: which kind of artifact a load produced, how it should be
drawn, and the one sentence that names it.
"""

from __future__ import annotations

import math

# DP-199 moved ``count_text`` to :mod:`foammesh.core.quantities`, beside
# the other rules for how a number is written, so that the core -- where
# most of the counts are -- can reach it without importing the mesh
# package. It is still imported here, and still reachable from here.
from foammesh.core.quantities import count_text, format_group

#: A mesh with cells: the stage produced a volume.
VOLUME = 'volume'
#: A mesh with faces and no cells: a surface triangulation.
SURFACE = 'surface'
#: Nothing was loaded.
EMPTY = 'empty'

#: How each kind is drawn on arrival. The values name ``DisplayMode``
#: members; this module deliberately does not import the renderer.
#:
#: Both meshes arrive with their edges drawn, because the edges are the
#: mesh -- that is the whole of DP-95. What separates the two pictures is
#: what they are pictures *of*, and the sentence beside them says which.
DISPLAY_MODES = {
    VOLUME: 'SURFACE_EDGE',
    SURFACE: 'SURFACE_EDGE',
    EMPTY: '',
}


def artifact_kind(cells: int, faces: int) -> str:
    """Which of the three kinds a freshly loaded scene is."""
    if int(cells or 0) > 0:
        return VOLUME
    if int(faces or 0) > 0:
        return SURFACE
    return EMPTY


def display_mode_name(kind: str) -> str:
    """The ``DisplayMode`` member a mesh of this kind arrives in, or ''."""
    return DISPLAY_MODES.get(kind, '')


def hides_geometry(kind: str) -> bool:
    """Whether the geometry it was built from steps out of the way.

    It has to. The imported surfaces stay in the same scene as the mesh --
    ``MainWindow._countedParts`` says so in as many words -- and a snapped
    mesh sits exactly where the STL it was snapped to sits, so the two
    z-fight and the one the user wants to look at is the one behind.
    """
    return kind in (VOLUME, SURFACE)


def extent_text(size) -> str:
    """"0.42 × 0.18 × 0.18 m" -- the three dimensions, in one unit.

    One unit for all three, chosen from the largest, so the numbers can be
    compared with each other by eye, and (DP-165) one decimal count for all
    three, for the same reason. An empty string when there is nothing
    measurable to say.
    """
    try:
        dims = [float(value) for value in size]
    except (TypeError, ValueError):
        return ''
    if len(dims) != 3 or any(not math.isfinite(value) for value in dims):
        return ''
    dims = [abs(value) for value in dims]
    largest = max(dims)
    if largest <= 0:
        return ''
    return format_group(dims)


def summary_text(stage: str, kind: str, cells: int, faces: int,
                 points: int, size=None) -> str:
    """The sentence that goes beside the picture, or '' when there is none.

    ``stage`` is what the user just pressed -- "Castellation", "Boundary
    layers". Without one the kind names itself, which is what a result
    loaded from a run rather than from a stage button gets.
    """
    if kind == EMPTY:
        return ''
    head = str(stage or '').strip()
    if not head:
        head = 'Volume mesh' if kind == VOLUME else 'Surface mesh'
    parts = []
    if kind == VOLUME:
        parts.append(count_text(cells, 'cell'))
        if int(faces or 0) > 0:
            parts.append(count_text(faces, 'boundary face'))
    else:
        parts.append(count_text(faces, 'face'))
    # Points are counted on the volume dataset, which holds them once. The
    # patches hold their own copies of the points they share, so summing
    # those would report a surface mesh as larger than it is; when there is
    # no volume to ask, the clause is left out rather than guessed at.
    if int(points or 0) > 0:
        parts.append(count_text(points, 'point'))
    extent = extent_text(size) if size is not None else ''
    if extent:
        parts.append(extent)
    return f'{head}: ' + ', '.join(parts)


#: How solid an outer boundary is drawn once it is known to be enclosing
#: something. Low enough that the mesh inside reads through it, high enough
#: that the domain's own extent and refinement pattern are still there.
ENCLOSURE_OPACITY = 0.3
#: How far back a region's volume steps so its own boundary patches win the
#: depth test where the two describe the same faces (DP-141). In OpenGL
#: polygon-offset units, relative to every other actor in the scene: large
#: enough to settle the tie at any camera distance these meshes are viewed
#: from, small enough that nothing genuinely in front of the volume -- a
#: failed-cell overlay, a clipped section face -- is pushed behind it.
VOLUME_DEPTH_BIAS = 4.0
#: How far inside an outer boundary a part has to sit before that boundary is
#: called an enclosure, as a fraction of the scene's longest side. A pipe's
#: inlet cap lies *on* the wall's bounding box; a body in a wind tunnel sits
#: clear of it, and that clearance is the whole difference.
ENCLOSURE_CLEARANCE = 0.02
#: How close to the scene's own extent a part has to reach to count as
#: spanning it, as the same fraction. A snapped outer boundary misses the
#: block's corner by a cell or two.
_SPAN_TOLERANCE = 5e-3


def _bounding_box(value):
    """``(lows, highs)`` from a VTK-shaped ``(xmin, xmax, ...)``, or ``None``.

    VTK reports an empty data set as ``(1, -1, 1, -1, 1, -1)``, which is not a
    box and must not be treated as one.
    """
    try:
        numbers = [float(number) for number in value]
    except (TypeError, ValueError):
        return None
    if len(numbers) != 6:
        return None
    lows, highs = tuple(numbers[0::2]), tuple(numbers[1::2])
    if any(high < low for low, high in zip(lows, highs)):
        return None
    return lows, highs


def enclosing_part_ids(bounds_by_id, *, clearance=ENCLOSURE_CLEARANCE):
    """Which of the drawn parts is an outer boundary with something inside it.

    The instruction is that the mesh be visible as it is built. For a case
    whose domain is a box around a body -- a cyclone in a tunnel, a cascade in
    a farfield -- it is not: the geometry view draws every surface at less than
    full opacity so the body reads through the enclosure, and the mesh view
    draws every patch solid, so from blockMesh onwards the user sees a grey box
    and nothing else, for every remaining stage of the run.

    What separates that case from an ordinary internal-flow one is not the
    patch's name, its type or its ``inGroups`` -- MEASURED on the ``45db9031``
    corpus, the enclosure of a snapped farfield case is a ``wall`` carrying
    ``inGroups (wall)``, indistinguishable by any of those from the pipe wall
    of the case beside it. It is the geometry: an enclosure spans the whole
    scene *and* holds another part clear of it on all six sides. A pipe's wall
    spans the scene too, but its inlet and outlet caps lie on the bounding box
    rather than inside it.

    ``bounds_by_id`` maps each drawn part to its VTK bounds. Returns the ids in
    input order; an empty result is the ordinary case and means nothing is
    dimmed.
    """
    boxes = {}
    for key, value in (bounds_by_id or {}).items():
        box = _bounding_box(value)
        if box is not None:
            boxes[key] = box
    if len(boxes) < 2:
        return ()
    lows = [min(box[0][axis] for box in boxes.values()) for axis in range(3)]
    highs = [max(box[1][axis] for box in boxes.values()) for axis in range(3)]
    longest = max(high - low for low, high in zip(lows, highs))
    if longest <= 0:
        return ()
    reach = _SPAN_TOLERANCE * longest
    gap = float(clearance) * longest
    found = []
    for key, (low, high) in boxes.items():
        spans = all(low[axis] <= lows[axis] + reach
                    and high[axis] >= highs[axis] - reach
                    for axis in range(3))
        if not spans:
            continue
        for other, (inner_low, inner_high) in boxes.items():
            if other == key:
                continue
            if all(inner_low[axis] > low[axis] + gap
                   and inner_high[axis] < high[axis] - gap
                   for axis in range(3)):
                found.append(key)
                break
    return tuple(found)
