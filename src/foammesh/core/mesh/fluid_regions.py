"""From the labelled domain to region seeds: Plan 36 RP7 and RP8.

`fluid_spaces` (RP5) labels every connected space the surfaces cut the domain
box into. This module answers the two questions a case asks of that field:

* "How many fluid regions?" -- :func:`propose` picks ``count`` spaces, the
  largest first, and says plainly when the geometry has more or fewer and why
  (`mismatch`), instead of guessing (D3, D4);
* "Do two regions sit in one space?" -- :func:`same_space_conflicts` groups
  region seeds by the space they fall in, which snappy would mesh once;
  :func:`graded_conflicts` says how sure that is (RP13 #1: a second field at
  h/2 must agree before a conflict can refuse a launch).

Detection itself is never run on the main thread: that is the Qt GUI thread
in the desktop, and labelling a million voxels there froze the window for
the length of the run. :func:`run_detection` refuses to, and
:func:`detect_blocking` hands the work to the VTK worker thread when a
synchronous caller has no loop to await on.
"""
from __future__ import annotations

import concurrent.futures
import math
import threading
from pathlib import Path

import numpy as np

#: The mismatch reasons, named from the data (Plan 36 RP7 point 4).
CORE_OPEN_TO_OUTSIDE = 'core_open_to_outside'
FEWER_SPACES = 'fewer_spaces'
MORE_SPACES = 'more_spaces'
NO_CLOSED_SURFACE = 'no_closed_surface'
DOMAIN_CUTS_GEOMETRY = 'domain_cuts_geometry'
SURFACE_LEAKS = 'surface_leaks'
#: DP-915: on Gmsh with the far-field box on, the cut subtracts every
#: imported solid; the one fluid region is the space around them.
FARFIELD_IS_THE_FLUID = 'farfield_is_the_fluid'

#: An outside space wrapped by the surface on at least this many axes, over
#: at least this fraction of the geometry's bounding box, runs *through* the
#: geometry: the open core of an annulus, the bore of a pipe (RP7 point 4).
TUNNEL_AXES = 2
TUNNEL_FRACTION = 0.05

#: RP13 #6. How near, in voxels, a wrapped component must come to a face of
#: the geometry's bounding box to reach it: the wall band keeps the outside
#: a voxel or two off the surface.
THROUGH_REACH_VOXELS = 2

#: RP13 #1: how sure a same-space conflict is. The voxel field is an
#: approximation: a passage narrower than the wall band is lost (DP-861's
#: 6 mm neck joined two chambers snappy meshed as one) and a wall thinner
#: than it can be too. A conflict is checked again at h/2.
CONFIRMED = 'confirmed'      # both resolutions agree, through a wide passage
APPROXIMATE = 'approximate'  # the resolutions disagree, or the passage is
                             # narrower than PASSAGE_VOXELS at h/2
UNRESOLVED = 'unresolved'    # no h/2 field in time (or none is possible)
#: A conflict is confirmed only through a passage at least this many h/2
#: voxels wide.
PASSAGE_VOXELS = 2
#: The h/2 re-check is given at most this long; past it the conflict is
#: unresolved, which warns instead of refusing.
RECHECK_SECONDS = 10.0
#: A finer field coarser than this fraction of h is no second resolution.
RECHECK_FRACTION = 0.9


class MainThreadDetectionError(RuntimeError):
    """Detection was asked to run on the main (GUI) thread."""


def on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def run_detection(surfaces, box, **options):
    """:func:`fluid_spaces.detect`, refusing the main thread (RP7 gate).

    The facade runs this on the VTK worker thread; the assertion is here, in
    the function that does the work, so no route to it can forget it.
    """
    if on_main_thread():
        raise MainThreadDetectionError(
            'fluid-space detection must not run on the main (GUI) thread')
    from .fluid_spaces import detect

    return detect(surfaces, box, **options)


def detect_blocking(surfaces, box, *, timeout=None, **options):
    """Run detection from synchronous code, off the main thread.

    Off the main thread it simply runs. On the main thread the work goes to
    the single VTK worker thread and this waits for it -- up to *timeout*
    seconds, after which the run is told to stop and ``TimeoutError`` is
    raised. An asynchronous caller should await
    ``vtk_run_in_thread(run_detection, ...)`` instead.
    """
    if not on_main_thread():
        return run_detection(surfaces, box, **options)
    from foammesh.support.vtk_threads import _pool

    stop = threading.Event()
    asked = options.pop('cancelled', None)

    def cancelled():
        return stop.is_set() or bool(asked and asked())

    future = _pool.submit(run_detection, surfaces, box,
                          cancelled=cancelled, **options)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        stop.set()
        raise TimeoutError('fluid-space detection ran over its budget') from None


def surface_key(surfaces) -> str:
    """The content hash `fluid_spaces.detect` keys *surfaces* by (RP13 #3).

    A detection record fingerprints it, so an apply can tell whether the
    staged surfaces are still the ones labelled. VTK work: off the GUI
    thread, like the detection.
    """
    from .fluid_spaces import _assemble, surface_digest

    return surface_digest(_assemble(surfaces))


def cached_detection(surfaces, box, **options):
    """The field for these inputs if a cache holds it, else ``None``.

    Never labels anything: the run is cancelled before its first stage, so
    only the in-memory and ``.npz`` caches can answer. Cheap enough for the
    main thread.
    """
    from .fluid_spaces import FluidSpacesCancelled, detect

    try:
        return detect(surfaces, box, cancelled=lambda: True, **options)
    except (FluidSpacesCancelled, ValueError):
        return None


# -- inputs ----------------------------------------------------------------- #

def union_bounds(surfaces) -> tuple | None:
    """The extent of every surface, ``(xmin, xmax, ymin, ymax, zmin, zmax)``."""
    bounds = None
    for surface in surfaces or ():
        if surface is None or not surface.GetNumberOfCells():
            continue
        values = surface.GetBounds()
        if bounds is None:
            bounds = list(values)
            continue
        for axis in range(3):
            bounds[2 * axis] = min(bounds[2 * axis], values[2 * axis])
            bounds[2 * axis + 1] = max(bounds[2 * axis + 1],
                                       values[2 * axis + 1])
    return None if bounds is None else tuple(float(value) for value in bounds)


def detection_box(db, case_path, geometry_bounds):
    """The box to label: the background mesh's (RP1), in geometry units.

    Falls back to a written ``blockMeshDict`` and then to the geometry's own
    extent, so a case with no configuration to read still has a box. The
    box is used as it is, even where it lies on the geometry: with no
    standoff the corners around a round body really are separate pockets of
    the mesh blockMesh and snappy will build, and the field says so.

    RP13 #5: authored blocks that do not fill their hull (an L) come back as
    a `fluid_spaces.DomainExtent` carrying the domain, so detection leaves
    the part of the hull without background cells out of every space.
    """
    from .domain_box import domain_box, written_domain_box
    from .fluid_spaces import DomainExtent

    box = None
    try:
        resolved = domain_box(db, case_path, geometry_bounds)
    except Exception:  # noqa: BLE001 - a configuration the writer cannot read
        resolved = None
        try:
            resolved = written_domain_box(case_path)
        except Exception:  # noqa: BLE001
            resolved = None
    if resolved is not None:
        box = [float(value) for value in resolved.bounds]
    if box is None and geometry_bounds is not None:
        box = [float(value) for value in geometry_bounds]
    if box is None:
        return None
    if (resolved is not None and not getattr(resolved, 'cuboid', True)
            and getattr(resolved, 'blocks', ())):
        return DomainExtent(box, resolved)
    return tuple(box)


def base_cell_size(db, box) -> float | None:
    """The smallest background-cell edge the writer would use, or ``None``."""
    if db is None or box is None:
        return None
    try:
        from foammesh.core.geometry import BBox
        from foammesh.openfoam.case_builder import CaseBuilder

        extent = [BBox(*(float(value) for value in box))]
        counts = CaseBuilder(db, extent[0])._background_cell_counts(extent[0])
    except Exception:  # noqa: BLE001 - no configuration to size a grid from
        return None
    try:
        sizes = [(box[2 * axis + 1] - box[2 * axis]) / int(counts[axis])
                 for axis in range(3)]
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None
    sizes = [size for size in sizes if math.isfinite(size) and size > 0]
    return min(sizes) if sizes else None


def box_cuts_geometry(box, geometry_bounds) -> bool:
    """True when the geometry reaches past the domain box on any face."""
    if box is None or geometry_bounds is None:
        return False
    spans = [box[2 * axis + 1] - box[2 * axis] for axis in range(3)]
    tolerance = 1e-6 * max(spans + [0.0])
    return any(geometry_bounds[2 * axis] < box[2 * axis] - tolerance
               or geometry_bounds[2 * axis + 1] > box[2 * axis + 1] + tolerance
               for axis in range(3))


# -- the proposal ----------------------------------------------------------- #

def too_thin(space, cell) -> bool:
    """A space that cannot hold one cell of size *cell*.

    ``depth`` is the distance from the seed to the nearest wall, so the
    widest sphere the space holds is ``2 * depth`` across; a cell fits when
    that is at least one cell. RP13 #4: the cell is the finest the case
    refines to, base / 2^maxLevel (`finest_cell`), and a space too thin for
    it is still listed and counted, only flagged.
    """
    floor = cell
    if floor is None or not floor > 0:
        return False
    return 2.0 * float(space.depth) < float(floor)


def finest_cell(base_cell, max_level) -> float | None:
    """base / 2^maxLevel: the finest cell snappy can put into a space."""
    if base_cell is None or not base_cell > 0:
        return None
    return float(base_cell) / 2 ** max(0, int(max_level or 0))


def resolution_warning(space, base_cell) -> bool:
    """RP13 #4: the space is kept, but resolving it is not assured.

    Either the voxels could not see it at all (recovered from its closed
    shell, DP-863), or it is thinner than one base cell, so only the
    refinement around it can fit cells into it.
    """
    if getattr(space, 'resolution_warning', False):
        return True
    return too_thin(space, base_cell)


def tunnel_volume(field) -> float:
    """Volume of outside voxels the surface wraps on at least two axes.

    An outside voxel that has non-outside voxels (wall or an enclosed space)
    on both sides along an axis sits in a hollow of the geometry; along two
    axes, in a bore that runs through it. The corners of a round body's
    bounding box are outside but never between two walls, so they do not
    count -- which is why the plan's "outside voxels inside the geometry's
    bounding box" is not read literally here.
    """
    wrapped = _wrapped(field)
    if wrapped is None:
        return 0.0
    count = int(np.count_nonzero(wrapped))
    return count * float(np.prod(field.spacing))


def _wrapped(field):
    """The outside voxels the surface wraps on `TUNNEL_AXES` axes, or None."""
    outside_ids = [space.id for space in field.spaces if space.outside]
    if not outside_ids:
        return None
    labels = field.labels
    outside = np.isin(labels, outside_ids)
    other = ~outside
    wrapped = np.zeros(labels.shape, dtype=np.uint8)
    for axis in range(3):
        before = np.maximum.accumulate(other, axis=axis)
        after = np.flip(np.maximum.accumulate(
            np.flip(other, axis=axis), axis=axis), axis=axis)
        wrapped += (outside & before & after).astype(np.uint8)
    return wrapped >= TUNNEL_AXES


def wrapped_at(field, point) -> bool:
    """DP-923: whether *point* is in an outside voxel the surface wraps.

    The one-voxel form of `_wrapped`: the seed is in an outside space, and
    on at least `TUNNEL_AXES` axes the line through it meets something that
    is not the outside (a wall, another space) on both sides. That is the
    open core of an annulus, the bore of a pipe, the hollow of a cup -- a
    hole in the geometry. A point beside an elbow, in its bounding box but
    beyond the surface, is wrapped on no axis at all. Three line reads, so
    it is cheap enough for every frame of a drag.
    """
    if field is None or point is None:
        return False
    try:
        index = field._index(tuple(float(value) for value in point))
    except (TypeError, ValueError):
        return False
    if index is None:
        return False
    outside_ids = [space.id for space in field.spaces if space.outside]
    if not outside_ids:
        return False
    labels = field.labels                      # (nz, ny, nx)
    nx, ny, nz = field.dimensions
    i, j, k = index % nx, (index // nx) % ny, index // (nx * ny)
    if int(labels[k, j, i]) not in outside_ids:
        return False

    def wrapped(i, j, k):
        if int(labels[k, j, i]) not in outside_ids:
            return False
        axes = 0
        for line, at in ((labels[k, j, :], i), (labels[k, :, i], j),
                         (labels[:, j, i], k)):
            other = ~np.isin(line, outside_ids)
            if other[:at].any() and other[at + 1:].any():
                axes += 1
        return axes >= TUNNEL_AXES

    # The point's voxel, or one face-neighbour of it: a seed typed on the
    # mouth of a core -- the annulus's (0, 0, 0), in the plane of its end --
    # sits in a voxel centred a hair outside the end, which no wall wraps.
    for di, dj, dk in ((0, 0, 0), (1, 0, 0), (-1, 0, 0), (0, 1, 0),
                       (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        a, b, c = i + di, j + dj, k + dk
        if 0 <= a < nx and 0 <= b < ny and 0 <= c < nz and wrapped(a, b, c):
            return True
    return False


def tunnel_through(field, geometry_bounds) -> bool:
    """RP13 #6: whether the wrapped outside is a bore *through* the geometry.

    The wrapped-outside measure alone called the hollow of a cup -- open at
    one end only -- an open core, and told a closed body asked for two
    regions that its core was open. A bore is one connected set of wrapped
    outside voxels that reaches two opposite faces of the geometry's
    bounding box (within `THROUGH_REACH_VOXELS`); anything less is not.
    """
    from .fluid_spaces import _connected

    wrapped = _wrapped(field)
    if wrapped is None or not wrapped.any():
        return False
    first = tuple(float(field.box[2 * axis]) + field.spacing[axis] / 2
                  for axis in range(3))
    components = _connected(wrapped, field.spacing, first)
    if geometry_bounds is None:
        solid = field.labels == 0
        if solid.any():
            indices = np.nonzero(solid)            # (k, j, i)
            geometry_bounds = []
            for axis, which in ((0, 2), (1, 1), (2, 0)):
                geometry_bounds.extend((
                    first[axis] + indices[which].min() * field.spacing[axis],
                    first[axis] + indices[which].max() * field.spacing[axis]))
        if geometry_bounds is None:
            return False
    count = int(components.max())
    for axis, dim in ((0, 2), (1, 1), (2, 0)):      # x is the last index
        low = (float(geometry_bounds[2 * axis]) - first[axis])             / field.spacing[axis]
        high = (float(geometry_bounds[2 * axis + 1]) - first[axis])             / field.spacing[axis]
        # Per label, the lowest and highest index it occupies on this axis.
        planes = np.moveaxis(components, dim, 0)
        lowest = np.full(count + 1, np.inf)
        highest = np.full(count + 1, -np.inf)
        for index in range(planes.shape[0]):
            found = np.unique(planes[index])
            found = found[found > 0]
            if found.size:
                lowest[found] = np.minimum(lowest[found], index)
                highest[found] = index
        reach = THROUGH_REACH_VOXELS
        if np.any((lowest[1:] <= low + reach)
                  & (highest[1:] >= high - reach)):
            return True
    return False


def _box_volume(bounds) -> float:
    if bounds is None:
        return 0.0
    return float(np.prod([max(0.0, bounds[2 * axis + 1] - bounds[2 * axis])
                          for axis in range(3)]))


def space_row(space, base_cell, finest=None) -> dict:
    """One space as the detect result lists it (RP7 point 1)."""
    finest = base_cell if finest is None else finest
    return {'id': int(space.id), 'volume': float(space.volume),
            'depth': float(space.depth),
            'seed': [float(value) for value in space.seed],
            'outside': bool(space.outside),
            'too_thin': bool(too_thin(space, finest)),
            'resolution_warning': bool(resolution_warning(space, base_cell)),
            'bounds': [float(value) for value in space.bounds]}


def propose(field, count, *, external=False, base_cell=None,
            geometry_bounds=None, box=None, finest=None) -> dict:
    """The detect payload: every space, ``count`` proposed, any mismatch.

    Candidates are the enclosed spaces, largest first, preceded by the
    largest outside space when *external* (external flow is never proposed
    silently, D3). RP13 #4: a thin space is never dropped -- it is flagged
    ``too_thin`` against *finest* (base / 2^maxLevel) and
    ``resolution_warning`` against the base cell -- so the counts include
    it. Fewer candidates than asked stops
    and says why; more proposes the ``count`` largest and the rest stay
    listed in ``spaces`` (D4) -- a mismatch only for internal flow, since in
    external flow the extra enclosed spaces are the bodies (DP-864).
    """
    count = int(count)
    base = base_cell if base_cell and base_cell > 0 else None
    enclosed = list(field.enclosed)
    outside = list(field.outside)
    candidates = ([outside[0]] if external and outside else []) + enclosed
    proposed = [int(space.id) for space in candidates[:count]]
    found = len(candidates)
    mismatch = None
    reason = None
    if box_cuts_geometry(box, geometry_bounds):
        reason = DOMAIN_CUTS_GEOMETRY
    elif found < count:
        if field.leak:
            reason = SURFACE_LEAKS
        elif (not external and outside
              and tunnel_volume(field) > TUNNEL_FRACTION * _box_volume(
                  geometry_bounds)
              and tunnel_through(field, geometry_bounds)):
            # RP13 #6: the measure, and a bore through the geometry.
            reason = CORE_OPEN_TO_OUTSIDE
        elif not enclosed:
            reason = NO_CLOSED_SURFACE
        else:
            reason = FEWER_SPACES
    elif found > count and not external:
        # RP13 / DP-864: with external flow the enclosed spaces past the
        # count are the bodies' own interiors -- a closed sphere in a
        # farfield encloses one -- which are solid, not a surplus region.
        # The outside is proposed first and they stay listed in ``spaces``.
        reason = MORE_SPACES
    if reason is not None:
        mismatch = {'asked': count, 'found': found, 'reason': reason}
    ordered = sorted(field.spaces, key=lambda space: (
        space.outside, -space.volume, space.id))
    return {'spaces': [space_row(space, base, finest) for space in ordered],
            'proposed': proposed,
            'found_enclosed': len(enclosed),
            'mismatch': mismatch,
            'h': float(field.h), 'voxels': int(field.voxels),
            'elapsed': float(field.elapsed)}


def empty_proposal(count) -> dict:
    """The detect payload for a case with no surface to label against."""
    return {'spaces': [], 'proposed': [], 'found_enclosed': 0,
            'mismatch': {'asked': int(count), 'found': 0,
                         'reason': NO_CLOSED_SURFACE},
            'h': None, 'voxels': 0, 'elapsed': 0.0}


# -- RP8: seeds sharing a space --------------------------------------------- #

def same_space_conflicts(field, seeds) -> list[dict]:
    """Groups of seeds that fall in one space, snappy keeps it once.

    *seeds* is ``[(label, type, point), ...]``. Each group with two or more
    seeds is ``{'space': id, 'outside': bool, 'regions': [labels],
    'types': [types], 'clash': bool}``; ``clash`` is a Fluid and a Solid seed
    in the same space, which cannot both be kept. Seeds on a wall voxel or
    off the box belong to no space and are left to the other launch checks.
    """
    groups: dict[int, list] = {}
    for label, kind, point in seeds:
        found = field.space_at(tuple(float(value) for value in point))
        if found is None or found.label == 0:
            continue
        groups.setdefault(int(found.label), []).append((label, kind))
    conflicts = []
    for space_id, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        space = field.space(space_id)
        types = [str(kind) for _label, kind in members]
        conflicts.append({
            'space': space_id,
            'outside': bool(space.outside) if space is not None else False,
            'regions': [str(label) for label, _kind in members],
            'types': types,
            'clash': len(set(types)) > 1})
    return conflicts


def recheck_h(field, *, max_voxels=None) -> float | None:
    """The voxel size *field* is checked again at: h/2 within the voxel cap.

    RP13 #1. ``None`` when the cap leaves no second resolution worth the
    name (finer than ``RECHECK_FRACTION`` of h).
    """
    from .fluid_spaces import MAX_VOXELS

    cap = int(max_voxels or MAX_VOXELS)
    box = field.box
    lengths = [box[2 * axis + 1] - box[2 * axis] for axis in range(3)]
    if not all(length > 0 for length in lengths):
        return None
    floor = (math.prod(lengths) / cap) ** (1 / 3)
    finer = max(float(field.h) / 2, floor)
    return finer if finer < RECHECK_FRACTION * float(field.h) else None


def _blocks(mask, width):
    """Voxels starting a ``width``-cube wholly inside *mask*, same shape."""
    blocks = mask
    for axis in range(3):
        size = blocks.shape[axis]
        if size < width:
            return np.zeros(mask.shape, dtype=bool)
        keep = size - width + 1
        run = np.take(blocks, range(keep), axis=axis).copy()
        for step in range(1, width):
            run &= np.take(blocks, range(step, step + keep), axis=axis)
        pad = [(0, 0)] * 3
        pad[axis] = (0, width - 1)
        blocks = np.pad(run, pad)
    return blocks


def passage_holds(field, points, width=PASSAGE_VOXELS) -> bool:
    """Do *points* share one space of *field* through ``width``-wide ways?

    RP13 #1. A ``width``-voxel cube is slid through the space; the points
    are joined when the cubes that hold them are 6-connected. A neck
    narrower than ``width`` voxels -- a passage the field may have invented
    or snappy may not resolve -- does not join them.
    """
    from .fluid_spaces import _connected

    labels = [field.label_at(tuple(float(value) for value in point))
              for point in points]
    if not labels or None in labels or 0 in labels or len(set(labels)) > 1:
        return False
    width = max(1, int(width))
    nx, ny, nz = field.dimensions
    blocks = _blocks(field.labels == labels[0], width)
    if not blocks.any():
        return False
    try:
        joined = _connected(blocks, field.spacing, (0.0, 0.0, 0.0))
    except ValueError:            # more pieces than a label holds
        return False
    reach = None
    for point in points:
        index = [int((float(point[axis]) - field.box[2 * axis])
                     / field.spacing[axis]) for axis in range(3)]
        i, j, k = (min(max(value, 0), size - 1)
                   for value, size in zip(index, (nx, ny, nz)))
        near = joined[max(0, k - width + 1):k + 1,
                      max(0, j - width + 1):j + 1,
                      max(0, i - width + 1):i + 1]
        found = {int(value) for value in np.unique(near)} - {0}
        reach = found if reach is None else reach & found
        if not reach:
            return False
    return True


def graded_conflicts(field, seeds, finer=None) -> list[dict]:
    """Same-space conflicts with how sure each one is (RP13 #1).

    *field* is the launch's field at h, *finer* the same domain at h/2 (or
    ``None`` when that re-check did not finish). Seeds that share a space in
    either field form a group; each group is a `same_space_conflicts` row
    plus ``confidence``: `CONFIRMED` when both fields put the whole group in
    one space and :func:`passage_holds` at h/2, `UNRESOLVED` without a finer
    field, else `APPROXIMATE`. A group only the finer field joins -- two
    chambers the voxels at h separated at a narrow neck, DP-861 -- is
    reported too, as approximate.
    """
    points = [tuple(float(value) for value in point)
              for _label, _kind, point in seeds]

    def spaces(one):
        found = []
        for point in points:
            at = one.space_at(point) if one is not None else None
            found.append(None if at is None or at.label == 0 else at.label)
        return found

    coarse, fine = spaces(field), spaces(finer)
    parent = list(range(len(seeds)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for labels in (coarse, fine):
        first = {}
        for index, label in enumerate(labels):
            if label is None:
                continue
            if label in first:
                parent[root(index)] = root(first[label])
            else:
                first[label] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(seeds)):
        groups.setdefault(root(index), []).append(index)
    conflicts = []
    for members in groups.values():
        if len(members) < 2:
            continue
        coarse_one = (coarse[members[0]] is not None
                      and len({coarse[index] for index in members}) == 1)
        fine_one = (fine[members[0]] is not None
                    and len({fine[index] for index in members}) == 1)
        if finer is None:
            confidence = UNRESOLVED
        elif coarse_one and fine_one and passage_holds(
                finer, [points[index] for index in members]):
            confidence = CONFIRMED
        else:
            confidence = APPROXIMATE
        if coarse_one or finer is None or not fine_one:
            space_id, source = coarse[members[0]], field
        else:
            space_id, source = fine[members[0]], finer
        space = source.space(space_id) if space_id is not None else None
        types = [str(seeds[index][1]) for index in members]
        conflicts.append({
            'space': space_id,
            'outside': bool(space.outside) if space is not None else False,
            'regions': [str(seeds[index][0]) for index in members],
            'types': types,
            'clash': len(set(types)) > 1,
            'confidence': confidence})
    conflicts.sort(key=lambda group: (group['space'] is None,
                                      group['space'] or 0))
    return conflicts


def cache_dir(case_path) -> Path:
    """Where a case keeps its fluid-space fields (RP5 point 8)."""
    return Path(case_path) / 'foammesh' / 'cache'
