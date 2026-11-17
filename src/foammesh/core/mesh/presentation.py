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


def hides_geometry(kind: str, *, base_grid: bool = False) -> bool:
    """Whether the geometry it was built from steps out of the way.

    It has to. The imported surfaces stay in the same scene as the mesh --
    ``MainWindow._countedParts`` says so in as many words -- and a snapped
    mesh sits exactly where the STL it was snapped to sits, so the two
    z-fight and the one the user wants to look at is the one behind.

    Plan 50 IP1 (DP-6220). Not for the base grid. The blockMesh box does not
    follow the geometry at all -- it is the block the geometry will be cut
    out of -- so nothing z-fights, and hiding the model left the user looking
    at a box with no idea where the body sat inside it, at the one stage
    whose question is whether the box around the body is right.
    """
    if base_grid:
        return False
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


# -- Plan 50 IP1: the base grid does not hide the model --------------------- #
#
# DP-6220..DP-6224. The base grid is a box *around* the model. Drawn like
# every other stage's mesh -- every patch solid, the region's volume solid
# behind them, the geometry hidden -- the stage showed a grey block and
# nothing else. The rules below keep the model in view: the grid is drawn as
# its far walls only (the walls between the camera and the model are culled),
# the faces faint and the lines carrying the grid, and the lines fade out
# where a cell is only a few pixels wide on screen and would fill the wall.

#: The snapshot stage that is the base grid (``stage_snapshots.STAGE_ORDER``).
BASE_GRID_STAGE = 'blockMesh'
#: The workflow task that publishes it.
BASE_GRID_TASK = 'snappy.base_grid'
#: The tasks that publish a mesh made *from* the base grid. If one of them
#: was published on the very mesh on screen (an all-in-one run publishes
#: every task it reached with the final mesh), that mesh is not a base grid.
LATER_MESH_TASKS = ('snappy.castellation', 'snappy.snap',
                    'snappy.fidelity_snap', 'snappy.layers')

#: How solid a base-grid wall is drawn. Its lines share this alpha (VTK draws
#: a prop's edges with the prop's opacity), so it is the lowest the lines can
#: still be read at; the faces at this alpha are a tint.
BASE_GRID_WALL_OPACITY = 0.35
#: The region's ``internalMesh`` on the base grid. Its exterior is the same
#: box the patches already draw; drawn at all, it is a second box in front of
#: the model (the failed-cells overlay set the precedent, DP-2725).
BASE_GRID_VOLUME_OPACITY = 0.0

#: A cell this many pixels wide or less: its lines are gone (drawn, they
#: would be a solid fill of line colour).
GRID_FADE_GONE_PX = 2.0
#: A cell this many pixels wide or more: its lines are at full strength.
GRID_FADE_FULL_PX = 8.0
#: How long the camera is left to settle before the fade is worked out again.
GRID_FADE_THROTTLE_MS = 120


def base_grid_on_screen(live_identity, active_record=None,
                        publications=None) -> bool:
    """Whether the live ``constant/polyMesh`` is the base grid (blockMesh).

    Decided from what is on disk, never from text on screen:

    1. ``active_record`` is ``foammesh/stages/active.json``: which stage
       snapshot the live mesh is a copy of, with the mesh identity it was
       written for. While that identity is the live one it answers outright:
       base grid if the stage is ``blockMesh``, not if it is any other.
    2. Otherwise (a snapshot skipped for quota, free space or a decomposed
       case leaves ``active.json`` behind the live mesh) ``publications`` --
       the ``publications`` map of ``snappy-publications.json`` -- answers:
       the base-grid task was published on this very mesh and no later
       stage was.

    ``live_identity`` is ``task_state_store.mesh_identity`` of the case; with
    none (no mesh) the answer is ``False``.
    """
    if not live_identity:
        return False
    record = active_record if isinstance(active_record, dict) else {}
    if record.get('mesh_identity') and record.get('mesh_identity') == live_identity:
        return record.get('stage') == BASE_GRID_STAGE
    table = publications if isinstance(publications, dict) else {}

    def _on_live(task_id) -> bool:
        entry = table.get(task_id)
        return (isinstance(entry, dict) and bool(entry.get('active', True))
                and entry.get('mesh_revision') == live_identity)

    if not _on_live(BASE_GRID_TASK):
        return False
    return not any(_on_live(task_id) for task_id in LATER_MESH_TASKS)


def world_per_pixel(parallel, parallel_scale, distance, view_angle_deg,
                    height_px):
    """How much model length one screen pixel covers at the focal point.

    Perspective: the view's height at the focal distance is
    ``2 d tan(angle / 2)``; parallel: ``2 * parallel scale``; divided by the
    viewport's height in pixels. ``None`` when it cannot be said (no
    viewport yet, a degenerate camera).
    """
    try:
        height = float(height_px)
        if parallel:
            span = 2.0 * float(parallel_scale)
        else:
            angle = math.radians(float(view_angle_deg))
            span = 2.0 * float(distance) * math.tan(angle / 2.0)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(span) and math.isfinite(height)):
        return None
    if span <= 0 or height <= 0:
        return None
    return span / height


def typical_face_size(total_area, faces):
    """The side of a typical face, ``sqrt(area / faces)``, or ``None``."""
    try:
        area, count = float(total_area), int(faces)
    except (TypeError, ValueError):
        return None
    if count <= 0 or not math.isfinite(area) or area <= 0:
        return None
    return math.sqrt(area / count)


def grid_cell_pixels(face_size, per_pixel):
    """How many pixels wide a face of ``face_size`` is drawn, or ``None``."""
    try:
        size, scale = float(face_size), float(per_pixel)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(size) and math.isfinite(scale)) or scale <= 0:
        return None
    return size / scale


def grid_line_fade(cell_px) -> float:
    """How strongly a base-grid wall's lines are drawn, 0 to 1.

    0 at :data:`GRID_FADE_GONE_PX` pixels a cell or less, 1 at
    :data:`GRID_FADE_FULL_PX` or more, a straight line between. A size that
    cannot be measured draws the lines at full strength: fading them on a
    guess would hide the grid the stage is about.
    """
    try:
        pixels = float(cell_px)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(pixels):
        return 1.0
    if pixels <= GRID_FADE_GONE_PX:
        return 0.0
    if pixels >= GRID_FADE_FULL_PX:
        return 1.0
    return (pixels - GRID_FADE_GONE_PX) / (GRID_FADE_FULL_PX - GRID_FADE_GONE_PX)


def grid_edge_mix(line_opacity, fade, default_opacity) -> float:
    """How much of the line colour a base-grid wall's edges carry, 0 to 1.

    On every other mesh the edges are mixed into an opaque face at the
    user's *Mesh lines* strength (``line_opacity``; ``default_opacity`` when
    untouched). A base-grid wall's face is already faint, so at the default
    strength the lines carry the whole line colour, and a user who turned
    the lines down gets them weaker in the same ratio. Then the pixel fade.
    """
    try:
        line, default = float(line_opacity), float(default_opacity)
        fade = float(fade)
    except (TypeError, ValueError):
        return 1.0
    if not (math.isfinite(line) and math.isfinite(default)) or default <= 0:
        strength = 1.0
    else:
        strength = min(max(line / default, 0.0), 1.0)
    if not math.isfinite(fade):
        fade = 1.0
    return strength * min(max(fade, 0.0), 1.0)
