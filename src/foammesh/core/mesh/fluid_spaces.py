"""Which connected space is a point in, how big is it, where is its deepest point.

Plan 36 RP5. A snappy region is a seed, and snappy keeps the connected space
that contains it. :class:`~foammesh.core.mesh.seed_classifier.SeedClassifier`
judges one point (inside / outside / on the surface). This module answers a
different question for every point of the domain at once: it labels each
connected space the surfaces cut the domain box into, once per geometry and
box, and then answers "which space, how big, how deep" with one array read.

The pipeline (Plan 36 §5 RP5):

1. **Walls** -- a voxel is wall when the surface passes within ``0.75 h`` of
   its centre. The distance is sampled coarsely first and finely only near
   the surface (:func:`foammesh.core.geometry.occupancy.narrow_band_distance`,
   the implementation the surface wrapper shares).
2. **Components** -- ``vtkImageConnectivityFilter`` over the free voxels,
   face (6-) connected, size-ranked, so label 1 is the largest. A component
   is *outside* when it reaches the domain box's boundary.
3. **Volumes** -- the wall band is given back to the component on its side,
   so a space is not reported short by the band's width (13 % on the annulus
   at 3.2 mm, measured in §3.4). A band voxel takes the label of a labelled
   face neighbour, layer by layer; where two components compete for it, the
   one whose walls it shares a side with (the sign of its signed distance)
   wins.
4. **Depth and seed** -- ``vtkImageEuclideanDistance`` over the free voxels,
   with the box walls counted as walls. Each component's seed is its deepest
   voxel, ties broken towards the component's centroid (D8).
5. **Surfaces** -- ``vtkDiscreteFlyingEdges3D`` per component, smoothed with
   ``vtkWindowedSincPolyDataFilter`` (10 iterations, pass band 0.1) and
   decimated to at most 60 k triangles.

Nothing here touches Qt or a render window. :func:`detect` is safe to run on
a worker thread; :func:`detect_in_vtk_thread` runs it on the VTK thread the
way ``support/vtk_threads.py`` asks (D6). A result is cached in memory and,
given a directory, in ``fluid_spaces-<key>.npz`` beside the case.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

import numpy as np

#: Bumped whenever a stored field would be read differently.
CACHE_VERSION = 3
#: The voxel cap (Plan 36 §7: ~128^3).
MAX_VOXELS = 2_000_000
#: h is never finer than the domain diagonal over this.
FLOOR_DIVISIONS = 256
#: The voxel count aimed at when there is no base-grid cell to follow.
DEFAULT_VOXELS = 1_000_000
#: A wall is the band ``|d| <= WALL_BAND * h`` (the wrapper's 0.75 h).
WALL_BAND = 0.75
#: The coarse pass samples every this-many fine voxels along each axis.
COARSE_FACTOR = 4
#: Triangles per component surface, at most.
MAX_SURFACE_TRIANGLES = 60_000
#: RP13 #7: triangles across every surface drawn at once, at most.
TOTAL_SURFACE_TRIANGLES = 300_000
#: A space past its cap is drawn as its bounding box: twelve triangles.
BOX_TRIANGLES = 12
#: The field-data array that marks a surface drawn as a bounding box.
BOXED_ARRAY = 'foammeshBoundsBox'
#: Harder decimation is tried this many times before the box is drawn.
DECIMATION_PASSES = 12
#: Planes of the grid labelled per connectivity call: cancel is polled
#: between slabs (RP13 #7: cancel within 250 ms at 2 M voxels).
LABEL_SLAB_PLANES = 16
#: Seconds of distance evaluation between cancellation polls.
DISTANCE_SECONDS = 0.05
#: A space thinner than this many voxels triggers one halving of h.
THINNEST_GAP_VOXELS = 3
#: An outside space that meets the surface's inner side on more than this
#: fraction of its wall contacts has leaked in through an opening.
LEAK_FRACTION = 0.05
#: RP13 #4: closed shells looked at for a space the voxels lost, at most.
MAX_SHELLS = 64
#: RP13 #4: voxel centres tested against one lost shell, at most.
MAX_SHELL_VOXELS = 200_000


class FluidSpacesCancelled(InterruptedError):
    """The caller's ``cancelled()`` said stop."""


class SpaceAt(NamedTuple):
    """What :meth:`FluidSpaces.space_at` answers for one point.

    ``label`` 0 is a wall voxel no space claimed (a gap thinner than the
    band); it then reads ``outside=False, volume=0.0, depth=0.0``.

    ``outside_domain`` (RP13 #5): the point is inside the hull of a domain
    that is not a cuboid, where no block makes background cells. It reads
    label 0 as well -- neither a fluid space nor the outside.
    """
    label: int
    outside: bool
    volume: float
    depth: float
    outside_domain: bool = False


#: RP13 #5: `FluidSpaces.space_at` inside the hull but off every block.
OUTSIDE_DOMAIN = SpaceAt(0, False, 0.0, 0.0, True)


class DomainExtent(tuple):
    """A detection box that carries the domain's own shape (RP13 #5).

    Still the ``(xmin, xmax, ymin, ymax, zmin, zmax)`` tuple every caller
    passes around; ``domain`` is the `DomainBox` whose blocks do not fill
    it (``mask(points)`` says which points have background cells), or
    ``None`` for a domain that is the box itself.
    """

    def __new__(cls, box, domain=None):
        extent = super().__new__(cls, (float(value) for value in box))
        extent.domain = domain
        return extent

    def __reduce__(self):
        return (DomainExtent, (tuple(self), self.domain))


@dataclass(frozen=True)
class Space:
    """One connected space of the domain."""
    id: int
    volume: float
    depth: float
    seed: tuple
    outside: bool
    voxels: int
    bounds: tuple
    leaked: bool = False
    #: RP13 #4 / DP-863: the voxels could not resolve this space -- a closed
    #: shell thinner than the wall band -- so it was recovered from the
    #: surface; its volume is the shell's own.
    resolution_warning: bool = False

    def to_dict(self) -> dict:
        return {'id': self.id, 'volume': self.volume, 'depth': self.depth,
                'seed': list(self.seed), 'outside': self.outside,
                'voxels': self.voxels, 'bounds': list(self.bounds),
                'leaked': self.leaked,
                'resolution_warning': self.resolution_warning}


@dataclass
class FluidSpaces:
    """The labelled domain: every space, and an O(1) point lookup.

    ``labels`` is ``uint16`` shaped ``(nz, ny, nx)``; voxel ``(k, j, i)`` is
    centred at ``box_min + (index + 0.5) * spacing``. Band voxels carry the
    label of the space on their side; 0 is wall no space claimed.
    """
    key: str
    box: tuple
    h: float
    spacing: tuple
    dimensions: tuple
    labels: np.ndarray
    spaces: tuple
    open_edges: int = 0
    evaluated: int = 0
    unassigned: int = 0
    timings: dict = field(default_factory=dict)
    elapsed: float = 0.0
    from_cache: bool = False
    #: RP13 #3. The content hash of the surfaces labelled, which a detection
    #: record fingerprints; set by `detect`.
    surface_key: str = ''
    #: RP13 #5: ``(nz, ny, nx)`` booleans, True where the domain has no
    #: background cells (inside the hull, off every block); ``None`` when
    #: the domain is its box.
    excluded: object = None
    _surfaces: dict = field(default_factory=dict, repr=False)
    #: RP13 #7. Label -> the cap its surface was boxed under.
    _boxed: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        self.labels = np.ascontiguousarray(self.labels, dtype=np.uint16)
        self.labels.setflags(write=False)
        self._flat = self.labels.ravel()
        self._origin = tuple(float(self.box[2 * axis]) for axis in range(3))
        self._inverse = tuple(1.0 / step for step in self.spacing)
        lookup = [SpaceAt(0, False, 0.0, 0.0)] * (
            max((space.id for space in self.spaces), default=0) + 1)
        for space in self.spaces:
            lookup[space.id] = SpaceAt(
                space.id, space.outside, space.volume, space.depth)
        self._lookup = lookup
        self._byId = {space.id: space for space in self.spaces}
        if self.excluded is not None:
            self.excluded = np.ascontiguousarray(self.excluded, dtype=bool)
            self.excluded.setflags(write=False)
            if not self.excluded.any():
                self.excluded = None
        self._excluded = (None if self.excluded is None
                          else self.excluded.ravel())

    # -- reading ----------------------------------------------------------

    @property
    def voxels(self) -> int:
        return int(self.labels.size)

    @property
    def enclosed(self) -> tuple:
        """The spaces that do not reach the domain boundary, largest first."""
        return tuple(sorted((space for space in self.spaces
                             if not space.outside),
                            key=lambda space: (-space.volume, space.id)))

    @property
    def outside(self) -> tuple:
        """The spaces that reach the domain boundary, largest first."""
        return tuple(sorted((space for space in self.spaces
                             if space.outside),
                            key=lambda space: (-space.volume, space.id)))

    @property
    def leak(self) -> bool:
        """The surface is open, and the outside has come in through it."""
        return any(space.leaked for space in self.spaces)

    def space(self, label: int):
        return self._byId.get(int(label))

    def _index(self, point):
        """The flat index of the voxel containing *point*, or ``None``."""
        x, y, z = point
        nx, ny, nz = self.dimensions
        i = int((x - self._origin[0]) * self._inverse[0])
        j = int((y - self._origin[1]) * self._inverse[1])
        k = int((z - self._origin[2]) * self._inverse[2])
        # ``int`` truncates towards zero, so a point just below the box's
        # lower face would read index 0 without the explicit sign check.
        if (x < self._origin[0] or y < self._origin[1] or z < self._origin[2]
                or i >= nx or j >= ny or k >= nz):
            return None
        return i + nx * (j + ny * k)

    def label_at(self, point):
        """The label of the voxel containing *point*, or ``None`` off the box."""
        index = self._index(point)
        return None if index is None else int(self._flat[index])

    def outside_domain(self, point) -> bool:
        """True when *point* is in the box but has no background cells.

        RP13 #5: only a domain that is not a cuboid has such points.
        """
        if self._excluded is None:
            return False
        index = self._index(point)
        return index is not None and bool(self._excluded[index])

    def space_at(self, point):
        """``(label, outside, volume, depth)`` for *point*; ``None`` off the box.

        One index computation and one array read: cheap enough to call on
        every frame of a drag (Plan 36 I4). In the hull of a domain that is
        not a cuboid but off its blocks it is `OUTSIDE_DOMAIN` (RP13 #5).
        """
        index = self._index(point)
        if index is None:
            return None
        if self._excluded is not None and self._excluded[index]:
            return OUTSIDE_DOMAIN
        return self._lookup[int(self._flat[index])]

    def to_dict(self) -> dict:
        return {'key': self.key, 'box': list(self.box), 'h': self.h,
                'spacing': list(self.spacing),
                'dimensions': list(self.dimensions), 'voxels': self.voxels,
                'open_edges': self.open_edges, 'leak': self.leak,
                'evaluated': self.evaluated, 'unassigned': self.unassigned,
                'found_enclosed': len(self.enclosed),
                'spaces': [space.to_dict() for space in self.spaces],
                'timings': dict(self.timings), 'elapsed': self.elapsed,
                'from_cache': self.from_cache,
                'outside_domain_voxels': (
                    0 if self.excluded is None
                    else int(np.count_nonzero(self.excluded)))}

    # -- surfaces ---------------------------------------------------------

    def surface(self, label: int, *, cap=MAX_SURFACE_TRIANGLES,
                cancelled=None):
        """The smoothed boundary of one space as ``vtkPolyData`` (cached).

        At most *cap* triangles (RP13 #7): the surface is decimated harder
        until it fits, and a space whose surface cannot be brought under the
        cap is drawn as its bounding box instead (:meth:`boxed` says so).
        *cancelled* is polled between the contour, smoothing and every
        decimation pass.
        """
        label = int(label)
        cap = max(BOX_TRIANGLES, int(cap))
        with self._lock:
            cached = self._surfaces.get(label)
            boxed_under = self._boxed.get(label)
        # A box built under a smaller cap is rebuilt when there is more room.
        if (cached is not None and cached.GetNumberOfCells() <= cap
                and (boxed_under is None or cap <= boxed_under)):
            return cached
        built = _component_surface(self, label, cap=cap, cancelled=cancelled)
        with self._lock:
            if built.GetFieldData().GetAbstractArray(BOXED_ARRAY) is not None:
                self._boxed[label] = cap
            else:
                self._boxed.pop(label, None)
            self._surfaces[label] = built
            return built

    def boxed(self, label: int) -> bool:
        """True when *label* is drawn as its bounds: past the triangle cap."""
        with self._lock:
            return int(label) in self._boxed

    def surfaces(self, labels=None, *, budget=TOTAL_SURFACE_TRIANGLES,
                 cancelled=None) -> dict:
        """The surfaces of *labels* (every space when ``None``), by label.

        RP13 #7: together they hold at most *budget* triangles. Each gets an
        equal share, capped at `MAX_SURFACE_TRIANGLES`; when there are more
        spaces than bounding boxes fit in the budget, the smallest are left
        out (absent from the answer). Surfaces are built only for the labels
        asked for, so a caller builds what it shows, proposes or selects.
        """
        if labels is None:
            labels = [space.id for space in self.spaces]
        labels = [int(label) for label in labels if int(label) in self._byId]
        if len(labels) * BOX_TRIANGLES > budget:
            labels = sorted(labels, key=lambda label: (
                -self._byId[label].volume, label))[:budget // BOX_TRIANGLES]
        if not labels:
            return {}
        cap = min(MAX_SURFACE_TRIANGLES, budget // len(labels))
        built = {}
        for label in labels:
            _check(cancelled, 'surfaces')
            built[label] = self.surface(label, cap=cap, cancelled=cancelled)
        return built

    # -- the file cache ---------------------------------------------------

    def save(self, path) -> Path:
        """Write the field to *path* (``.npz``); surfaces are rebuilt on read."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        values, lengths = _run_length_encode(self.labels.ravel())
        meta = {'version': CACHE_VERSION, 'key': self.key,
                'box': list(self.box), 'h': self.h,
                'spacing': list(self.spacing),
                'dimensions': list(self.dimensions),
                'open_edges': self.open_edges, 'evaluated': self.evaluated,
                'unassigned': self.unassigned, 'timings': self.timings,
                'elapsed': self.elapsed,
                'spaces': [space.to_dict() for space in self.spaces]}
        arrays = {}
        if self.excluded is not None:
            outValues, outLengths = _run_length_encode(
                self.excluded.ravel().astype(np.uint8))
            arrays = {'excluded_values': outValues,
                      'excluded_lengths': outLengths}
        temporary = path.with_name(path.name + '.part')
        with open(temporary, 'wb') as stream:
            np.savez_compressed(stream, values=values, lengths=lengths,
                                meta=np.frombuffer(
                                    json.dumps(meta).encode('utf-8'),
                                    dtype=np.uint8), **arrays)
        temporary.replace(path)
        return path

    @classmethod
    def load(cls, path, *, key=None):
        """The field stored at *path*, or ``None`` if absent, stale or unreadable."""
        path = Path(path)
        if not path.is_file():
            return None
        try:
            with np.load(path) as data:
                meta = json.loads(bytes(data['meta']).decode('utf-8'))
                if meta.get('version') != CACHE_VERSION:
                    return None
                if key is not None and meta.get('key') != key:
                    return None
                nx, ny, nz = (int(value) for value in meta['dimensions'])
                labels = _run_length_decode(
                    data['values'], data['lengths']).reshape(nz, ny, nx)
                excluded = None
                if 'excluded_values' in data.files:
                    excluded = _run_length_decode(
                        data['excluded_values'], data['excluded_lengths']
                    ).reshape(nz, ny, nx).astype(bool)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        spaces = tuple(Space(
            id=int(item['id']), volume=float(item['volume']),
            depth=float(item['depth']), seed=tuple(item['seed']),
            outside=bool(item['outside']), voxels=int(item['voxels']),
            bounds=tuple(item['bounds']), leaked=bool(item.get('leaked')),
            resolution_warning=bool(item.get('resolution_warning')))
            for item in meta['spaces'])
        return cls(key=meta['key'], box=tuple(meta['box']), h=float(meta['h']),
                   spacing=tuple(meta['spacing']),
                   dimensions=(nx, ny, nz), labels=labels, spaces=spaces,
                   open_edges=int(meta['open_edges']),
                   evaluated=int(meta['evaluated']),
                   unassigned=int(meta['unassigned']),
                   timings=dict(meta.get('timings') or {}),
                   elapsed=float(meta.get('elapsed') or 0.0),
                   from_cache=True, excluded=excluded)


# -- resolution and keys -------------------------------------------------- #

def _box(box) -> tuple:
    box = tuple(float(value) for value in box)
    if len(box) != 6 or not all(math.isfinite(value) for value in box):
        raise ValueError('the domain box must be six finite numbers')
    if any(box[2 * axis + 1] <= box[2 * axis] for axis in range(3)):
        raise ValueError('the domain box has no volume')
    return box


def choose_h(box, *, base_cell=None, max_voxels=MAX_VOXELS) -> float:
    """The voxel size for *box* (D2).

    Half the base-grid cell when there is one, else the size that gives about
    a million voxels; never so fine that the grid passes *max_voxels*, and
    never finer than the domain diagonal over 256.
    """
    box = _box(box)
    lengths = [box[2 * axis + 1] - box[2 * axis] for axis in range(3)]
    volume = math.prod(lengths)
    diagonal = math.sqrt(sum(length * length for length in lengths))
    if base_cell is not None and float(base_cell) > 0:
        h = float(base_cell) / 2
    else:
        h = (volume / DEFAULT_VOXELS) ** (1 / 3)
    return max(h, diagonal / FLOOR_DIVISIONS,
               (volume / int(max_voxels)) ** (1 / 3))


def surface_digest(surface) -> str:
    """A content hash of a surface's points and cells, stable across sessions."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    digest = hashlib.sha1()
    points = surface.GetPoints()
    if points is not None and points.GetNumberOfPoints():
        digest.update(np.ascontiguousarray(
            vtk_to_numpy(points.GetData()), dtype=np.float64).tobytes())
    for cells in (surface.GetPolys(), surface.GetStrips()):
        if cells is None or not cells.GetNumberOfCells():
            continue
        offsets = cells.GetOffsetsArray()
        connectivity = cells.GetConnectivityArray()
        digest.update(np.ascontiguousarray(
            vtk_to_numpy(offsets), dtype=np.int64).tobytes())
        digest.update(np.ascontiguousarray(
            vtk_to_numpy(connectivity), dtype=np.int64).tobytes())
    return digest.hexdigest()


def domain_key(domain) -> str | None:
    """What a non-cuboid *domain* adds to the cache key: its blocks."""
    if domain is None or getattr(domain, 'cuboid', True):
        return None
    return json.dumps([[[repr(float(value)) for value in corner]
                        for corner in block]
                       for block in getattr(domain, 'blocks', ())])


def cache_key(surface_key, box, h, domain=None) -> str:
    """The key a field is cached under: surface, domain box and h.

    RP13 #5: a domain that is not a cuboid adds its blocks, so an L and the
    box around it are two fields; a box's key is what it always was.
    """
    parts = [CACHE_VERSION, str(surface_key),
             [repr(float(value)) for value in box], repr(float(h))]
    shape = domain_key(domain)
    if shape is not None:
        parts.append(shape)
    payload = json.dumps(parts)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:20]


def cache_path(case_path, key) -> Path:
    """Where a case keeps the field for *key* (Plan 36 RP5 point 8)."""
    return Path(case_path) / 'foammesh' / 'cache' / f'fluid_spaces-{key}.npz'


# -- the in-memory cache -------------------------------------------------- #

_MEMORY_LIMIT = 4
_memory: 'OrderedDict[str, FluidSpaces]' = OrderedDict()
_memoryLock = threading.Lock()


def _remember(result: FluidSpaces):
    with _memoryLock:
        _memory[result.key] = result
        _memory.move_to_end(result.key)
        while len(_memory) > _MEMORY_LIMIT:
            _memory.popitem(last=False)


def _recall(key):
    with _memoryLock:
        result = _memory.get(key)
        if result is not None:
            _memory.move_to_end(key)
        return result


def forget_cached():
    """Drop every field held in memory (tests; a geometry reload)."""
    with _memoryLock:
        _memory.clear()


# -- detection ------------------------------------------------------------ #

def _assemble(surfaces):
    from .seed_classifier import _assembled

    if surfaces is None:
        raise ValueError('no surface to label the domain against')
    if hasattr(surfaces, 'GetNumberOfCells'):
        surfaces = [surfaces]
    surfaces = [surface for surface in surfaces
                if surface is not None and surface.GetNumberOfCells() > 0]
    if not surfaces:
        raise ValueError('no surface to label the domain against')
    return _assembled(surfaces)


def detect(surfaces, box, *, h=None, base_cell=None, max_voxels=MAX_VOXELS,
           refine=True, build_surfaces=False, surface_key=None,
           cache_dir=None, use_cache=True, cancelled=None, progress=None,
           domain=None):
    """Label every connected space of *box* cut by *surfaces*.

    *surfaces* is one ``vtkPolyData`` or a sequence of them (see
    :func:`foammesh.core.mesh.seed_classifier.seed_components` for the
    choice the viewport makes); *box* is ``(xmin, xmax, ymin, ymax, zmin,
    zmax)`` -- the background mesh's extent, which the caller resolves.

    *h* fixes the voxel size. Left ``None`` it is :func:`choose_h`, and with
    *refine* it is halved (within the voxel cap) while an enclosed space is
    under three voxels across. *cancelled* is polled between stages, between
    distance chunks, connectivity slabs and surface passes (RP13 #7: a cancel
    is honoured within 250 ms at 2 M voxels); *progress* is called
    ``progress(stage, fraction)``. With *cache_dir* the field is read from
    and written to ``cache_dir/fluid_spaces-<key>.npz``.

    *domain* (RP13 #5; read from a `DomainExtent` *box* when not given) is a
    domain that is not a cuboid: voxels off its blocks have no background
    cells, are neither a space nor the outside, and bound the outside the
    way the box's faces do.
    """
    if domain is None:
        domain = getattr(box, 'domain', None)
    if domain_key(domain) is None:
        domain = None
    box = _box(box)
    started = time.perf_counter()
    assembled = _assemble(surfaces)
    if surface_key is None:
        surface_key = surface_digest(assembled)
    automatic = h is None
    if automatic:
        h = choose_h(box, base_cell=base_cell, max_voxels=max_voxels)
    h = float(h)
    if not h > 0 or not math.isfinite(h):
        raise ValueError('the voxel size must be positive and finite')
    lengths = [box[2 * axis + 1] - box[2 * axis] for axis in range(3)]
    floor = max(math.sqrt(sum(length * length for length in lengths))
                / FLOOR_DIVISIONS,
                (math.prod(lengths) / int(max_voxels)) ** (1 / 3))

    while True:
        key = cache_key(surface_key, box, h, domain)
        result = _cached(key, cache_dir) if use_cache else None
        if result is None:
            result = _label(assembled, box, h, key, cancelled=cancelled,
                            progress=progress, domain=domain)
            # Stamped before the field is remembered and saved, so a later
            # answer from either cache reports what the labelling cost rather
            # than 0 or the few milliseconds of the lookup (Plan 36 RP7: the
            # CLI must print the facade's JSON).
            result.elapsed = time.perf_counter() - started
            _remember(result)
            if cache_dir is not None:
                try:
                    result.save(Path(cache_dir) / f'fluid_spaces-{key}.npz')
                except OSError:
                    pass
        result.surface_key = str(surface_key)
        if not (automatic and refine):
            break
        thinnest = min((2 * space.depth for space in result.enclosed),
                       default=math.inf)
        if thinnest >= THINNEST_GAP_VOXELS * h or h / 2 < floor:
            break
        h = h / 2
    if build_surfaces:
        _stage(progress, 'surfaces', 0.0)
        began = time.perf_counter()
        # True builds every space; an iterable of labels builds just those
        # (RP13 #7: the shown, proposed and selected spaces).
        result.surfaces(None if build_surfaces is True else build_surfaces,
                        cancelled=cancelled)
        result.timings['surfaces'] = time.perf_counter() - began
        _stage(progress, 'surfaces', 1.0)
    return result


async def detect_in_vtk_thread(surfaces, box, **options):
    """:func:`detect` on the VTK worker thread, holding rendering meanwhile.

    D6: until Plan 35 CR2's worker process exists, detection runs on the
    single VTK thread of ``support/vtk_threads.py``; the caller receives the
    finished arrays and polydata only.
    """
    from foammesh.support.vtk_threads import vtk_run_in_thread

    return await vtk_run_in_thread(detect, surfaces, box, **options)


def _cached(key, cache_dir):
    result = _recall(key)
    if result is None and cache_dir is not None:
        result = FluidSpaces.load(
            Path(cache_dir) / f'fluid_spaces-{key}.npz', key=key)
        if result is not None:
            _remember(result)
    return result


def _stage(progress, stage, fraction):
    if progress is not None:
        progress(stage, float(fraction))


def _check(cancelled, stage):
    if cancelled is not None and cancelled():
        raise FluidSpacesCancelled(f'fluid-space detection cancelled at {stage}')


def _image(array, spacing, origin, name='labels'):
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonDataModel import vtkImageData

    nz, ny, nx = array.shape
    image = vtkImageData()
    image.SetDimensions(nx, ny, nz)
    image.SetSpacing(*spacing)
    image.SetOrigin(*origin)
    scalars = numpy_to_vtk(np.ascontiguousarray(array).ravel(), deep=1)
    scalars.SetName(name)
    image.GetPointData().SetScalars(scalars)
    return image


def _excluded(domain, first, spacing, dims, cancelled=None):
    """RP13 #5: ``(nz, ny, nx)``, True where *domain* has no background cells.

    ``None`` when every voxel centre is in a block. One z-slab at a time, so
    a cancel is heard between slabs.
    """
    if domain is None:
        return None
    nx, ny, nz = dims
    xs = first[0] + np.arange(nx) * spacing[0]
    ys = first[1] + np.arange(ny) * spacing[1]
    gx, gy = np.meshgrid(xs, ys)
    plane = np.column_stack([gx.ravel(), gy.ravel(), np.zeros(nx * ny)])
    excluded = np.zeros((nz, ny, nx), dtype=bool)
    for k in range(nz):
        _check(cancelled, 'domain')
        plane[:, 2] = first[2] + k * spacing[2]
        excluded[k] = ~np.asarray(domain.mask(plane), dtype=bool).reshape(
            ny, nx)
    return excluded if excluded.any() else None


def _rim(excluded):
    """The voxels next to *excluded* (6-neighbours), themselves not in it."""
    if excluded is None:
        return None
    rim = np.zeros_like(excluded)
    rim[1:] |= excluded[:-1]
    rim[:-1] |= excluded[1:]
    rim[:, 1:] |= excluded[:, :-1]
    rim[:, :-1] |= excluded[:, 1:]
    rim[:, :, 1:] |= excluded[:, :, :-1]
    rim[:, :, :-1] |= excluded[:, :, 1:]
    return rim & ~excluded


def _label(surface, box, h, key, *, cancelled=None, progress=None,
           domain=None):
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance
    from vtkmodules.vtkImagingGeneral import vtkImageEuclideanDistance

    from foammesh.core.geometry.occupancy import narrow_band_distance
    from .seed_classifier import _open_edge_count

    timings = {}
    lengths = [box[2 * axis + 1] - box[2 * axis] for axis in range(3)]
    dims = tuple(max(3, int(round(length / h))) for length in lengths)
    spacing = tuple(length / count for length, count in zip(lengths, dims))
    nx, ny, nz = dims
    first = tuple(box[2 * axis] + spacing[axis] / 2 for axis in range(3))
    centres = []
    for axis in range(3):
        centres.extend((first[axis],
                        first[axis] + (dims[axis] - 1) * spacing[axis]))
    band = WALL_BAND * max(spacing)
    # RP13 #5: the part of the box without background cells, and its rim,
    # which is the domain's boundary as much as the box's faces are.
    excluded = _excluded(domain, first, spacing, dims, cancelled)
    rim = _rim(excluded)

    # 1. Walls, sampled near the surface only.
    _check(cancelled, 'start')
    _stage(progress, 'distance', 0.0)
    began = time.perf_counter()
    implicit = vtkImplicitPolyDataDistance()
    implicit.SetInput(surface)
    try:
        wall, distance, evaluated = narrow_band_distance(
            surface, centres, dims, band, coarse_factor=COARSE_FACTOR,
            implicit=implicit, cancelled=cancelled,
            seconds=DISTANCE_SECONDS,
            progress=lambda done, total: _stage(
                progress, 'distance', done / max(1, total)))
    except InterruptedError as error:
        raise FluidSpacesCancelled(str(error)) from error
    timings['distance'] = time.perf_counter() - began
    _check(cancelled, 'distance')

    # 2. Components of the free voxels, 6-connected, size-ranked.
    _stage(progress, 'labelling', 0.0)
    began = time.perf_counter()
    free = (wall == 0)
    if excluded is not None:
        free &= ~excluded
    labels = _one_outside(_connected(free, spacing, first, cancelled), rim)
    count = int(labels.max()) if labels.size else 0
    outside = np.zeros(count + 1, dtype=bool)
    outside[np.unique(_face_labels(labels, rim))] = True
    outside[0] = False
    timings['labelling'] = time.perf_counter() - began
    _check(cancelled, 'labelling')

    # 3. The band goes back to the space on its side.
    _stage(progress, 'band', 0.0)
    began = time.perf_counter()
    assigned, sides, unassigned = _assign_band(
        labels, wall, distance, count,
        reach=max(spacing) - band + 0.05 * max(spacing))
    if excluded is not None:
        # Band voxels off the blocks have no cells to give back.
        assigned = np.where(excluded, 0, assigned).astype(assigned.dtype)
    timings['band'] = time.perf_counter() - began
    _check(cancelled, 'band')

    # 4. Depth, and each space's deepest voxel.
    _stage(progress, 'depth', 0.0)
    began = time.perf_counter()
    _check(cancelled, 'depth')
    padded = np.pad(free.astype(np.float32), 1)
    # The filter's "infinite" start value is not scaled by the spacing, so a
    # physical spacing of millimetres caps every distance at about one voxel
    # (measured: 0.0032 m^2 flat across a 28-voxel block at 3 mm). It is run
    # on spacing relative to the finest axis and scaled back.
    unit = min(spacing)
    edt = vtkImageEuclideanDistance()
    edt.SetInputData(_image(padded, tuple(step / unit for step in spacing),
                            (0.0, 0.0, 0.0)))
    edt.InitializeOn()
    edt.ConsiderAnisotropyOn()
    edt.SetAlgorithmToSaito()
    edt.Update()
    depth2 = vtk_to_numpy(edt.GetOutput().GetPointData().GetScalars()
                          ).reshape(nz + 2, ny + 2, nx + 2)[1:-1, 1:-1, 1:-1]
    depth2 = depth2 * (unit * unit)
    _check(cancelled, 'depth')
    spaces = _spaces(labels, assigned, depth2, count, outside, sides,
                     spacing, first, box, implicit, cancelled=cancelled)
    timings['depth'] = time.perf_counter() - began
    began = time.perf_counter()
    assigned, spaces = _lost_shells(surface, labels, assigned, spaces,
                                    spacing, first, implicit,
                                    cancelled=cancelled, excluded=excluded)
    timings['shells'] = time.perf_counter() - began
    _stage(progress, 'depth', 1.0)

    return FluidSpaces(
        key=key, box=box, h=float(h), spacing=spacing, dimensions=dims,
        labels=assigned, spaces=spaces,
        open_edges=_open_edge_count(surface), evaluated=evaluated,
        unassigned=unassigned, timings=timings, excluded=excluded)


def _face_labels(labels, rim=None):
    """Every label on the six faces of the grid (and on *rim*, RP13 #5)."""
    faces = [labels[0].ravel(), labels[-1].ravel(), labels[:, 0].ravel(),
             labels[:, -1].ravel(), labels[:, :, 0].ravel(),
             labels[:, :, -1].ravel()]
    if rim is not None:
        faces.append(labels[rim])
    return np.concatenate(faces)


def _one_outside(labels, rim=None):
    """*labels* with every component that reaches the grid's faces joined.

    RP13 / DP-866. Beyond the grid is outside, so whatever reaches its faces
    is one outside -- as if the grid had one more free layer all round. With
    a base grid flush against a round body (standoff 0) the outside reaches
    the faces only in the corners and through a bore, pockets the grid does
    not join inside (MEASURED: the annulus read 6 spaces, not 2). The
    labels are ranked by size again, the joined outside at its new size.
    """
    touching = np.unique(_face_labels(labels, rim))
    touching = touching[touching > 0]
    if len(touching) < 2:
        return labels
    count = int(labels.max())
    merged = np.arange(count + 1, dtype=np.int64)
    merged[touching] = int(touching.min())
    joined = merged[labels]
    counts = np.bincount(joined.ravel(), minlength=count + 1)
    counts[0] = 0
    order = np.argsort(-counts, kind='stable')
    rank = np.zeros(count + 1, dtype=np.int64)
    kept = order[counts[order] > 0]
    rank[kept] = np.arange(1, len(kept) + 1)
    return rank[joined].astype(labels.dtype)


def _slab_labels(free, spacing, first):
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkImagingMorphological import vtkImageConnectivityFilter

    nz, ny, nx = free.shape
    connectivity = vtkImageConnectivityFilter()
    connectivity.SetInputData(_image(free.astype(np.uint8), spacing, first))
    connectivity.SetScalarRange(1, 1)
    connectivity.SetExtractionModeToAllRegions()
    connectivity.SetLabelModeToSizeRank()
    connectivity.SetLabelScalarTypeToUnsignedShort()
    connectivity.Update()
    return vtk_to_numpy(connectivity.GetOutput().GetPointData().GetScalars()
                        ).reshape(nz, ny, nx)


def _connected(free, spacing, first, cancelled=None,
               planes=LABEL_SLAB_PLANES):
    """The 6-connected components of *free*, size-ranked (1 is the largest).

    RP13 #7. One connectivity call over 2 M voxels takes a third of a second
    with no way to stop it (MEASURED 0.33 s, VTK 9.5), so the grid is
    labelled a slab of *planes* z-planes at a time, *cancelled* polled
    between slabs, and the slabs joined where free voxels meet across a slab
    boundary. The labels are then ranked by size, as the one call ranked
    them; equal sizes keep the order the slabs found them in.
    """
    nz = free.shape[0]
    labels = np.zeros(free.shape, dtype=np.int64)
    offset, pairs = 0, []
    for start in range(0, nz, planes):
        _check(cancelled, 'labelling')
        slab = _slab_labels(free[start:start + planes], spacing, first
                            ).astype(np.int64)
        slab[slab > 0] += offset
        labels[start:start + planes] = slab
        if start:
            below, above = labels[start - 1], labels[start]
            both = (below > 0) & (above > 0)
            pairs.append(np.stack((below[both], above[both]), axis=1))
        offset = max(offset, int(slab.max()) if slab.size else 0)
    _check(cancelled, 'labelling')
    parent = np.arange(offset + 1, dtype=np.int64)
    joined = [pair for pair in pairs if len(pair)]
    if joined:
        joins = np.unique(np.concatenate(joined), axis=0)
        # Each label points at the smallest label it is joined to, until no
        # join spans two roots.
        while True:
            left, right = parent[joins[:, 0]], parent[joins[:, 1]]
            if np.array_equal(left, right):
                break
            lowest = np.minimum(left, right)
            np.minimum.at(parent, left, lowest)
            np.minimum.at(parent, right, lowest)
            while True:
                deeper = parent[parent]
                if np.array_equal(deeper, parent):
                    break
                parent = deeper
    root = parent[labels]
    _check(cancelled, 'labelling')
    counts = np.bincount(root.ravel(), minlength=offset + 1)
    counts[0] = 0
    order = np.argsort(-counts, kind='stable')
    rank = np.zeros(len(counts), dtype=np.int64)
    kept = order[counts[order] > 0]
    if len(kept) > np.iinfo(np.uint16).max:
        raise ValueError('more connected spaces than a label can hold')
    rank[kept] = np.arange(1, len(kept) + 1)
    return rank[root].astype(np.uint16)


_OFFSETS = ((0, 0, 1), (0, 0, -1), (0, 1, 0), (0, -1, 0), (1, 0, 0),
            (-1, 0, 0))


def _assign_band(labels, wall, distance, count, reach):
    """Give each wall voxel to the space on its side of the surface.

    Returns ``(assigned, sides, unassigned)``: the label image with the band
    filled in, a ``(count + 1, 2)`` table of each space's wall contacts on the
    surface's negative and positive side, and how many wall voxels no space
    reached (a gap thinner than the band).
    """
    nz, ny, nx = labels.shape
    strides = (1, nx + 2, (nx + 2) * (ny + 2))
    padded = np.pad(labels, 1).ravel()
    wall_index = np.flatnonzero(np.pad(wall, 1).ravel())
    signed = np.nan_to_num(
        np.pad(distance, 1, constant_values=np.nan).ravel()[wall_index])
    sign = np.sign(signed)
    shifts = [dz * strides[2] + dy * strides[1] + dx * strides[0]
              for dz, dy, dx in _OFFSETS]

    # Which side of the surface each space's walls face. A free voxel is at
    # least ``band`` from the surface and its face neighbour one spacing
    # away, so a neighbour can sit up to ``spacing - band`` across the
    # surface from it. Only contacts further from the surface than that are
    # counted: they are on the space's own side, whatever the band's width.
    sure = np.abs(signed) > reach
    sides = np.zeros((count + 1, 2), dtype=np.int64)
    for shift in shifts:
        neighbour = padded[wall_index + shift]
        touching = (neighbour > 0) & sure
        for column, which in enumerate((sign < 0, sign > 0)):
            sides[:, column] += np.bincount(
                neighbour[touching & which], minlength=count + 1)
    side = np.zeros(count + 1, dtype=np.int8)
    side[sides[:, 0] > sides[:, 1]] = -1
    side[sides[:, 1] > sides[:, 0]] = 1
    side[0] = 0

    pending = np.ones(len(wall_index), dtype=bool)
    for _layer in range(8):
        todo = np.flatnonzero(pending)
        if not len(todo):
            break
        cells = wall_index[todo]
        cellSign = sign[todo].astype(np.int8)
        best = np.zeros(len(todo), dtype=np.uint16)
        score = np.zeros(len(todo), dtype=np.int8)
        for shift in shifts:
            neighbour = padded[cells + shift]
            candidate = (neighbour > 0).astype(np.int8)
            candidate += 2 * ((side[neighbour] == cellSign)
                              & (side[neighbour] != 0) & (neighbour > 0))
            better = candidate > score
            best[better] = neighbour[better]
            score[better] = candidate[better]
        taken = best > 0
        if not taken.any():
            break
        # Jacobi: every voxel of this layer reads the previous layer only.
        padded[cells[taken]] = best[taken]
        pending[todo[taken]] = False
    assigned = padded.reshape(nz + 2, ny + 2, nx + 2)[1:-1, 1:-1, 1:-1]
    return np.ascontiguousarray(assigned), sides, int(pending.sum())


def _spaces(labels, assigned, depth2, count, outside, sides, spacing, first,
            box, implicit, cancelled=None):
    nz, ny, nx = labels.shape
    if count == 0:
        return ()
    flat = labels.ravel()
    index = np.flatnonzero(flat)
    owner = flat[index].astype(np.int64)
    i = index % nx
    j = (index // nx) % ny
    k = index // (nx * ny)
    counts = np.bincount(owner, minlength=count + 1)
    safe = np.maximum(counts, 1)
    centroid = [np.bincount(owner, weights=axis, minlength=count + 1) / safe
                for axis in (i, j, k)]

    # The deepest voxel, ties towards the centroid (D8).
    d2 = depth2.ravel()[index]
    deepest = np.zeros(count + 1, dtype=d2.dtype)
    np.maximum.at(deepest, owner, d2)
    tied = np.flatnonzero(d2 == deepest[owner])
    tiedOwner = owner[tied]
    offset = sum(((axis[tied] - centre[tiedOwner]) * step) ** 2
                 for axis, centre, step in zip((i, j, k), centroid, spacing))
    order = np.lexsort((index[tied], offset, tiedOwner))
    _unique, firstOf = np.unique(tiedOwner[order], return_index=True)
    seedIndex = np.zeros(count + 1, dtype=np.int64)
    seedIndex[tiedOwner[order][firstOf]] = index[tied][order][firstOf]

    _check(cancelled, 'depth')
    # Volumes and extents count the band a space was given back.
    volumes = np.bincount(assigned.ravel(), minlength=count + 1)[:count + 1]
    voxelVolume = spacing[0] * spacing[1] * spacing[2]
    whole = assigned.ravel()
    claimed = np.flatnonzero(whole)
    claimedOwner = whole[claimed].astype(np.int64)
    lower = [np.full(count + 1, np.iinfo(np.int64).max, dtype=np.int64)
             for _axis in range(3)]
    upper = [np.full(count + 1, -1, dtype=np.int64) for _axis in range(3)]
    for axis, values in enumerate((claimed % nx, (claimed // nx) % ny,
                                   claimed // (nx * ny))):
        np.minimum.at(lower[axis], claimedOwner, values)
        np.maximum.at(upper[axis], claimedOwner, values)

    spaces = []
    for label in range(1, count + 1):
        if not counts[label]:
            continue
        cell = int(seedIndex[label])
        seed = (float(first[0] + (cell % nx) * spacing[0]),
                float(first[1] + ((cell // nx) % ny) * spacing[1]),
                float(first[2] + (cell // (nx * ny)) * spacing[2]))
        depth = abs(float(implicit.EvaluateFunction(seed)))
        if outside[label]:
            depth = min(depth, *(min(seed[axis] - box[2 * axis],
                                     box[2 * axis + 1] - seed[axis])
                                 for axis in range(3)))
        bounds = []
        for axis in range(3):
            bounds.extend((
                float(first[axis] + (lower[axis][label] - 0.5) * spacing[axis]),
                float(first[axis] + (upper[axis][label] + 0.5) * spacing[axis])))
        contacts = int(sides[label].sum())
        # With consistently oriented walls every contact of one space is on
        # the same side. An outside space touching both sides has come in
        # through an opening; the minority side decides, so a surface whose
        # normals all point inwards reads the same.
        leaked = bool(outside[label] and contacts
                      and min(sides[label]) > LEAK_FRACTION * contacts)
        spaces.append(Space(
            id=label, volume=float(volumes[label] * voxelVolume),
            depth=depth, seed=seed, outside=bool(outside[label]),
            voxels=int(counts[label]), bounds=tuple(bounds), leaked=leaked))
    return tuple(spaces)


def _shells(surface):
    """The closed connected pieces of *surface*, at most `MAX_SHELLS`."""
    from vtkmodules.vtkFiltersCore import (
        vtkCleanPolyData, vtkPolyDataConnectivityFilter,
    )

    from .seed_classifier import _open_edge_count

    connectivity = vtkPolyDataConnectivityFilter()
    connectivity.SetInputData(surface)
    connectivity.SetExtractionModeToAllRegions()
    connectivity.Update()
    count = connectivity.GetNumberOfExtractedRegions()
    if count > MAX_SHELLS:
        return []
    shells = []
    for region in range(count):
        one = vtkPolyDataConnectivityFilter()
        one.SetInputData(surface)
        one.SetExtractionModeToSpecifiedRegions()
        one.AddSpecifiedRegion(region)
        clean = vtkCleanPolyData()
        clean.SetInputConnection(one.GetOutputPort())
        clean.Update()
        piece = clean.GetOutput()
        if piece.GetNumberOfCells() >= 4 and not _open_edge_count(piece):
            shells.append(piece)
    return shells


def _enclosed(shell, points) -> list:
    """Which of *points* the closed *shell* encloses."""
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints

    probe = vtkPoints()
    for point in points:
        probe.InsertNextPoint(*point)
    data = vtkPolyData()
    data.SetPoints(probe)
    enclosed = vtkSelectEnclosedPoints()
    enclosed.SetInputData(data)
    enclosed.SetSurfaceData(shell)
    enclosed.Update()
    return [bool(enclosed.IsInside(index)) for index in range(len(points))]


def _inside_point(shell):
    """``(point, half_thickness)`` inside the closed *shell*, or ``None``.

    From the centres of its largest triangles a ray is cast both ways along
    the normal; the midpoint to the first wall it meets, where the shell
    encloses it, is inside. The one deepest in the shell wins (a chord along
    a slot is long, but its midpoint sits off the slot's middle plane).
    """
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkFiltersGeneral import vtkOBBTree

    bounds = shell.GetBounds()
    reach = 2.0 * math.sqrt(sum((bounds[2 * axis + 1] - bounds[2 * axis]) ** 2
                                for axis in range(3)))
    if not reach > 0:
        return None
    triangles = []
    for cell in range(shell.GetNumberOfCells()):
        corners = shell.GetCell(cell).GetPoints()
        if corners.GetNumberOfPoints() < 3:
            continue
        a, b, c = (np.array(corners.GetPoint(index)) for index in range(3))
        normal = np.cross(b - a, c - a)
        area = float(np.linalg.norm(normal))
        if area > 0:
            triangles.append((area, (a + b + c) / 3, normal / area))
    triangles.sort(key=lambda item: -item[0])
    tree = vtkOBBTree()
    tree.SetDataSet(shell)
    tree.BuildLocator()
    candidates = []
    for _area, centre, normal in triangles[:8]:
        for direction in (normal, -normal):
            hits = vtkPoints()
            tree.IntersectWithLine(centre + direction * 1e-9 * reach,
                                   centre + direction * reach, hits, None)
            if not hits.GetNumberOfPoints():
                continue
            hit = np.array(hits.GetPoint(0))
            candidates.append(((centre + hit) / 2,
                               float(np.linalg.norm(hit - centre)) / 2))
    if not candidates:
        return None
    inside = [candidate for candidate, enclosed in zip(
        candidates, _enclosed(shell, [point for point, _ in candidates]))
        if enclosed]
    if not inside:
        return None
    from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance

    distance = vtkImplicitPolyDataDistance()
    distance.SetInput(shell)
    depths = [abs(float(distance.EvaluateFunction(*point)))
              for point, _half in inside]
    best = max(range(len(inside)), key=depths.__getitem__)
    return tuple(float(value) for value in inside[best][0]), depths[best]


def _enclosed_voxels(shell, spacing, first, dims) -> list:
    """``(k, j, i)`` of every voxel whose centre *shell* encloses."""
    bounds = shell.GetBounds()
    ranges = []
    for axis in range(3):
        low = max(0, int(math.floor(
            (bounds[2 * axis] - first[axis]) / spacing[axis])))
        high = min(dims[axis] - 1, int(math.ceil(
            (bounds[2 * axis + 1] - first[axis]) / spacing[axis])))
        if high < low:
            return []
        ranges.append(range(low, high + 1))
    if math.prod(len(values) for values in ranges) > MAX_SHELL_VOXELS:
        return []
    cells = [(k, j, i) for k in ranges[2] for j in ranges[1]
             for i in ranges[0]]
    points = [(first[0] + i * spacing[0], first[1] + j * spacing[1],
               first[2] + k * spacing[2]) for k, j, i in cells]
    return [cell for cell, enclosed in zip(cells, _enclosed(shell, points))
            if enclosed]


def _lost_shells(surface, labels, assigned, spaces, spacing, first,
                 implicit, cancelled=None, excluded=None):
    """Spaces the voxels lost, recovered from the closed shells (RP13 #4).

    A closed shell thinner than the wall band -- DP-863's 4 mm slot under
    2 to 9 mm voxels -- has no free voxel inside it: its voxels are wall,
    the band hands them to the space around it, and the slot was neither
    listed nor flagged. Each closed piece of the surface whose inside point
    falls on a wall voxel is such a space. It is listed with
    ``resolution_warning``, the shell's own volume and the voxels whose
    centres it encloses plus the one holding its inside point, and the
    space around it gives that volume back.
    """
    from vtkmodules.vtkFiltersCore import vtkMassProperties

    shells = _shells(surface)
    if not shells:
        return assigned, spaces
    nz, ny, nx = labels.shape
    origin = [first[axis] - spacing[axis] / 2 for axis in range(3)]
    byId = {space.id: space for space in spaces}
    nextId = max(byId, default=0) + 1
    added = False
    for shell in shells:
        _check(cancelled, 'depth')
        found = _inside_point(shell)
        if found is None:
            continue
        point, _half = found
        i, j, k = (int(math.floor((point[axis] - origin[axis])
                                  / spacing[axis])) for axis in range(3))
        if not (0 <= i < nx and 0 <= j < ny and 0 <= k < nz):
            continue
        if labels[k, j, i] != 0:
            continue    # a free voxel: the labelling holds this space
        if excluded is not None and excluded[k, j, i]:
            continue    # RP13 #5: no background cells there to mesh it
        if nextId > np.iinfo(np.uint16).max:
            break
        # The voxel holding the seed is always the space's, so the launch's
        # same-space check reads a seed placed there as this space.
        cells = _enclosed_voxels(shell, spacing, first, (nx, ny, nz))
        if (k, j, i) not in cells:
            cells.append((k, j, i))
        if not added:
            assigned = np.array(assigned, copy=True)
        owners = [int(assigned[cell]) for cell in cells]
        for cell in cells:
            assigned[cell] = nextId
        mass = vtkMassProperties()
        mass.SetInputData(shell)
        mass.Update()
        volume = abs(float(mass.GetVolume()))
        container = max(set(owners), key=owners.count)
        if container in byId:
            around = byId[container]
            byId[container] = replace(
                around, volume=max(0.0, around.volume - volume))
        byId[nextId] = Space(
            id=nextId, volume=volume,
            depth=abs(float(implicit.EvaluateFunction(point))),
            seed=point, outside=False, voxels=len(cells),
            bounds=tuple(float(value) for value in shell.GetBounds()),
            resolution_warning=True)
        nextId += 1
        added = True
    if not added:
        return assigned, spaces
    return assigned, tuple(byId[key] for key in sorted(byId))


def _bounds_box(bounds):
    """A closed box over *bounds*, twelve triangles, marked `BOXED_ARRAY`."""
    from vtkmodules.vtkCommonCore import vtkIntArray
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersCore import vtkTriangleFilter
    from vtkmodules.vtkFiltersSources import vtkCubeSource

    cube = vtkCubeSource()
    cube.SetBounds(*bounds)
    triangles = vtkTriangleFilter()
    triangles.SetInputConnection(cube.GetOutputPort())
    triangles.Update()
    result = vtkPolyData()
    result.DeepCopy(triangles.GetOutput())
    result.GetPointData().Initialize()
    marker = vtkIntArray()
    marker.SetName(BOXED_ARRAY)
    marker.InsertNextValue(1)
    result.GetFieldData().AddArray(marker)
    return result


def _component_surface(field: FluidSpaces, label: int, *,
                       cap=MAX_SURFACE_TRIANGLES, cancelled=None):
    """One space's smoothed, decimated boundary, from its voxels alone.

    At most *cap* triangles; past `DECIMATION_PASSES` harder decimations the
    space's bounding box is answered instead (RP13 #7).
    """
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersCore import (
        vtkQuadricClustering, vtkTriangleFilter, vtkWindowedSincPolyDataFilter)
    from vtkmodules.vtkFiltersGeneral import vtkDiscreteFlyingEdges3D

    labels = field.labels
    where = np.nonzero(labels == label)
    if not len(where[0]):
        return vtkPolyData()
    lo = [max(0, int(axis.min()) - 1) for axis in where]
    hi = [int(axis.max()) + 2 for axis in where]
    crop = np.pad(
        (labels[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]] == label
         ).astype(np.uint8), 1)
    spacing = field.spacing
    origin = tuple(field.box[2 * axis] + spacing[axis] / 2
                   + (lo[2 - axis] - 1) * spacing[axis] for axis in range(3))
    contour = vtkDiscreteFlyingEdges3D()
    contour.SetInputData(_image(crop, spacing, origin))
    contour.SetValue(0, 1)
    contour.ComputeNormalsOff()
    contour.ComputeGradientsOff()
    contour.ComputeScalarsOff()
    _check(cancelled, 'surfaces')
    contour.Update()
    _check(cancelled, 'surfaces')
    smooth = vtkWindowedSincPolyDataFilter()
    smooth.SetInputData(contour.GetOutput())
    smooth.SetNumberOfIterations(10)
    smooth.SetPassBand(0.1)
    smooth.NonManifoldSmoothingOn()
    smooth.NormalizeCoordinatesOn()
    smooth.BoundarySmoothingOff()
    triangles = vtkTriangleFilter()
    triangles.SetInputConnection(smooth.GetOutputPort())
    triangles.Update()
    output = triangles.GetOutput()
    cells = output.GetNumberOfCells()
    # Quadric clustering, not quadric decimation: MEASURED on the annulus at
    # 2.5 mm, reducing 385 k triangles to 60 k took 7.5 s by edge collapse
    # (2.0 s with vtkDecimatePro) and 0.08 s by clustering. The bin is grown
    # until the budget holds, or the space is drawn as its box.
    factor = math.sqrt(cells / cap)
    passes = 0
    while cells > cap:
        _check(cancelled, 'surfaces')
        if passes >= DECIMATION_PASSES:
            space = field.space(label)
            return _bounds_box(space.bounds if space is not None else [
                value for axis in range(3) for value in (
                    origin[axis], origin[axis]
                    + crop.shape[2 - axis] * spacing[axis])])
        passes += 1
        decimate = vtkQuadricClustering()
        decimate.SetInputData(triangles.GetOutput())
        decimate.SetDivisionSpacing(*(step * factor for step in spacing))
        decimate.Update()
        output = decimate.GetOutput()
        cells = output.GetNumberOfCells()
        factor *= 1.25
    result = vtkPolyData()
    result.DeepCopy(output)
    return result


# -- run-length encoding for the cache ------------------------------------ #

def _run_length_encode(values):
    values = np.asarray(values)
    if not values.size:
        return values.astype(np.uint16), np.zeros(0, dtype=np.uint32)
    starts = np.flatnonzero(np.concatenate(([True], values[1:] != values[:-1])))
    lengths = np.diff(np.concatenate((starts, [values.size])))
    return values[starts].astype(np.uint16), lengths.astype(np.uint32)


def _run_length_decode(values, lengths):
    return np.repeat(np.asarray(values, dtype=np.uint16),
                     np.asarray(lengths, dtype=np.int64))
