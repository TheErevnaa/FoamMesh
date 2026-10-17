"""Less detail while the camera moves, full detail at rest (Plan 37, 2026-10-01).

The user's rule: no fixed cell limit; the view leans on the GPU and must not
hang. A still frame draws everything. While the camera is dragged, a scene
above the GPU's budget (``gpu_profile.DisplayBudget.interactive_lod_faces``)
draws a reduced copy of each large part instead, and the full parts come back
the moment the mouse is let go.

How, so that nothing heavy runs on the GUI thread:

* **Built off the GUI thread.** Each large part's reduced copy is made by
  ``vtkQuadricClustering`` in a plain worker thread (VTK releases the GIL
  while it runs: measured, a 9 M-quad surface reduced in 0.8 s with the GUI
  thread's 5 ms ticks never late by more than 1 ms). The worker reads a new
  ``vtkPolyData`` that *shares* the part's points and cells -- nothing is
  copied, and the part on screen is never touched by the worker.
* **Drawn by a second actor.** The copy gets its own actor sharing the part's
  property (colour, opacity, display style follow it) and lives in the
  renderer hidden. A drag swaps visibility; neither actor's buffers are
  rebuilt, so the full part is not re-uploaded to the GPU on release.
* **Only while it matches.** A copy remembers the input it was made from and
  that input's modification time. A clip, a slice, a threshold or a reload
  changes the input, the copy is stale, and the part is drawn in full until a
  new copy is ready -- a drag never shows a cut that is not there (DP-814).
* **A clip does not cost the copy (Plan 39 VI3, DP-1270).** The copy follows
  the part's mapper clipping planes, so a half-space clip leaves it current
  and it is drawn clipped. A cut that changes the part's input (a filter)
  keeps the copy of the input it replaced, hidden, beside the new one
  (at most :data:`VARIANTS_KEPT` per part, matched by address and
  modification time so the old output is not kept alive), so taking
  the cut off brings the copy back without making it again.
* **Engaged on frame time, not only on faces (VI3, DP-1270).** The view tells
  :meth:`InteractionDetail.noteFrame` how long each full-detail frame took
  (the view waits for the GPU to finish the frame; MEASURED, the renderer's own
  ``GetLastRenderTimeInSeconds`` reads 1 ms for a frame that took 32 ms). When
  the last three averaged
  more than :data:`FRAME_BUDGET_SECONDS` the scene wants its copies, whatever
  its face count; they are made once idle, as before.
* **Said on screen.** The view emits ``detailReduced(True)`` when it swaps.

``vtkQuadricLODActor`` (``MeshActor``) built its copy on the GUI thread on
the first interactive frame -- seconds on a large mesh; the view no longer
raises the render rate that triggers it.

No Qt here; ``RenderingWidget`` owns the timing.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import logging
import math

logger = logging.getLogger(__name__)

#: A part with fewer cells than this is drawn in full while moving: reducing
#: it saves nothing worth the copy.
MIN_PART_CELLS = 50_000
#: The most clustering divisions along one axis.
MAX_DIVISIONS = 2048
#: A full-detail frame slower than this (33 ms, 30 fps) is a stuttering one.
FRAME_BUDGET_SECONDS = 0.033
#: How many full-detail frames are averaged to decide a scene is too slow.
FRAMES_AVERAGED = 3
#: Copies kept per part: the one for what it shows now and the one for what it
#: showed before a cut, so taking the cut off needs no new copy.
VARIANTS_KEPT = 2
#: The object name the reduced copies carry; ``gl_health.visible_faces`` and
#: pickers see them only while they are shown.
LOD_SUFFIX = ':interaction-detail'


def _cells(data) -> int:
    try:
        return int(data.GetNumberOfPolys() + data.GetNumberOfStrips()
                   + data.GetNumberOfLines())
    except Exception:                                      # noqa: BLE001
        try:
            return int(data.GetNumberOfCells())
        except Exception:                                  # noqa: BLE001
            return 0


def _address(data) -> str:
    try:
        return str(data.__this__)
    except Exception:                                      # noqa: BLE001
        return str(id(data))


def _polydata_input(actor):
    mapper = actor.GetMapper()
    if mapper is None:
        return None
    data = mapper.GetInput()
    if data is None or not data.IsA('vtkPolyData'):
        return None
    return data


def _colours_by_points(mapper, data) -> bool:
    """True when the mapper colours by point data, which the copy lacks."""
    try:
        if not mapper.GetScalarVisibility():
            return False
        mode = mapper.GetScalarMode()
    except Exception:                                      # noqa: BLE001
        return False
    # VTK_SCALAR_MODE_DEFAULT 0, USE_POINT_DATA 1, USE_CELL_DATA 2,
    # USE_POINT_FIELD_DATA 3, USE_CELL_FIELD_DATA 4, USE_FIELD_DATA 5.
    if mode in (1, 3):
        return True
    if mode == 0:
        return data.GetPointData().GetScalars() is not None
    return False


def divisions_for(bounds, target_triangles: int) -> tuple[int, int, int]:
    """Clustering divisions that leave about ``target_triangles``.

    A surface clustered into cells of size ``h`` keeps about one vertex per
    occupied cell and two triangles per vertex; the occupied cells are
    estimated from the bounding box's surface area.
    """
    sizes = [max(0.0, float(bounds[2 * i + 1]) - float(bounds[2 * i]))
             for i in range(3)]
    a, b, c = sizes
    area = 2.0 * (a * b + b * c + c * a)
    vertices = max(1.0, target_triangles / 2.0)
    if area <= 0.0:
        longest = max(sizes) or 1.0
        h = longest / max(1.0, vertices)
    else:
        h = math.sqrt(area / vertices)
    if h <= 0.0:
        return (1, 1, 1)
    return tuple(max(1, min(MAX_DIVISIONS, int(math.ceil(size / h))))
                 for size in sizes)


def reduce(source, target_triangles: int):
    """A reduced copy of the polydata ``source`` (safe in a worker thread).

    ``source`` should be a ``vtkPolyData`` no other thread modifies; the
    caller hands one that shares the part's arrays (:func:`shared_view`).
    Cell data is carried over so a cell-coloured part keeps its colours.
    """
    from vtkmodules.vtkFiltersCore import vtkQuadricClustering

    clustering = vtkQuadricClustering()
    clustering.SetInputData(source)
    clustering.AutoAdjustNumberOfDivisionsOff()
    clustering.SetNumberOfDivisions(*divisions_for(source.GetBounds(),
                                                    target_triangles))
    clustering.CopyCellDataOn()
    clustering.UseInputPointsOff()
    clustering.Update()
    output = clustering.GetOutput()
    clustering.SetInputData(None)
    return output


def shared_view(data):
    """A new polydata over ``data``'s points and cells: nothing is copied.

    The worker computes bounds and walks the cells of this object, never of
    the one the renderer draws: each cell list is a new ``vtkCellArray``
    over the part's own offset and connectivity arrays, so the legacy
    traversal cursor the clustering moves is not the part's.
    """
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    def cells(source):
        copy = vtkCellArray()
        if source is not None and source.GetNumberOfCells():
            copy.SetData(source.GetOffsetsArray(),
                         source.GetConnectivityArray())
        return copy

    view = vtkPolyData()
    view.SetPoints(data.GetPoints())
    view.SetPolys(cells(data.GetPolys()))
    view.SetStrips(cells(data.GetStrips()))
    view.SetLines(cells(data.GetLines()))
    view.GetCellData().ShallowCopy(data.GetCellData())
    return view


@dataclass
class Job:
    """One part's copy to make: run :meth:`run` in a worker thread."""
    actor: object
    data: object
    mtime: int
    view: object
    target: int

    def run(self):
        return reduce(self.view, self.target)


class _Record:
    __slots__ = ('actor', 'key', 'mtime', 'lod', 'hidden')

    def __init__(self, actor, data, mtime, lod):
        self.actor = actor
        #: Which data object it was made from, by address: a cut's old output
        #: is not kept alive by its copy. A freed address can only come back
        #: with a newer modification time, so the pair never matches wrongly.
        self.key = _address(data)
        self.mtime = mtime
        self.lod = lod
        self.hidden = False


class InteractionDetail:
    """The reduced copies of one renderer's large parts, and the swap."""

    def __init__(self, renderer, budget=None):
        self._renderer = renderer
        #: A callable returning the ``DisplayBudget`` in force.
        self._budget = budget
        #: Per part (by ``id(actor)``): its copies, newest last.
        self._records: dict[int, list[_Record]] = {}
        self._lods: set[int] = set()
        self._active = False
        #: Seconds of the last full-detail frames (copies not drawn).
        self._frames: deque = deque(maxlen=FRAMES_AVERAGED)
        #: "Reduce detail while moving" on: copies for every large part,
        #: whatever the scene's size.
        self.forced = False

    # -- what the scene needs ----------------------------------------------

    def budget(self):
        if self._budget is not None:
            return self._budget()
        from foammesh.rendering import gpu_profile
        return gpu_profile.display_budget()

    def _sceneActors(self):
        actors = self._renderer.GetActors()
        actors.InitTraversal()
        actor = actors.GetNextActor()
        while actor is not None:
            if id(actor) not in self._lods:
                yield actor
            actor = actors.GetNextActor()

    def parts(self):
        """``[(actor, polydata, cells)]`` for every visible drawn part."""
        found = []
        for actor in self._sceneActors():
            try:
                if not actor.GetVisibility():
                    continue
                data = _polydata_input(actor)
                if data is None:
                    continue
                found.append((actor, data, _cells(data)))
            except Exception:                              # noqa: BLE001
                continue
        return found

    def sceneCells(self) -> int:
        return sum(cells for _, _, cells in self.parts())

    # -- frame time -------------------------------------------------------

    def noteFrame(self, seconds: float) -> None:
        """Record how long a frame took to draw (Plan 39 VI3).

        Only full-detail frames count: a frame drawn from the copies says
        nothing about what the full part costs, and counting it would switch
        the copies off the moment they worked.
        """
        if self._active:
            return
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return
        if seconds > 0.0 and math.isfinite(seconds):
            self._frames.append(seconds)

    def frameSeconds(self):
        """The mean of the last full-detail frames, or None before three."""
        if len(self._frames) < FRAMES_AVERAGED:
            return None
        return sum(self._frames) / len(self._frames)

    def worthTiming(self) -> bool:
        """True when the scene is big enough that reducing it could help.

        Timing a frame means waiting for the GPU to finish it, so a scene
        smaller than the copy would be is not timed at all.
        """
        return self.sceneCells() > self.budget().lod_target_triangles

    def slow(self) -> bool:
        """True when the last three full-detail frames averaged over 33 ms."""
        mean = self.frameSeconds()
        return mean is not None and mean > FRAME_BUDGET_SECONDS

    def wanted(self) -> bool:
        """True when a drag of this scene should draw reduced copies.

        Faces over the GPU's budget, or "Reduce detail while moving", or --
        the case a face count cannot see -- frames that took too long. A scene
        no bigger than the copy would be is never reduced.
        """
        cells = self.sceneCells()
        if cells <= 0:
            return False
        target = self.budget().lod_target_triangles
        if self.forced:
            return cells > target
        if cells > self.budget().interactive_lod_faces:
            return True
        return cells > target and self.slow()

    def _isCurrent(self, record: _Record, actor=None) -> bool:
        """The copy matches the part's input now."""
        try:
            data = _polydata_input(record.actor if actor is None else actor)
            return (data is not None and _address(data) == record.key
                    and int(data.GetMTime()) == record.mtime)
        except Exception:                                  # noqa: BLE001
            return False

    def _stale(self, record: _Record) -> bool:
        """The input it was made from is still shown, but has changed."""
        try:
            data = _polydata_input(record.actor)
            return (data is not None and _address(data) == record.key
                    and int(data.GetMTime()) != record.mtime)
        except Exception:                                  # noqa: BLE001
            return True

    def _current(self, record: _Record) -> bool:
        return self._isCurrent(record, record.actor)

    def _currentFor(self, actor):
        for record in reversed(self._records.get(id(actor), ())):
            if self._current(record):
                return record
        return None

    def _all(self):
        for records in self._records.values():
            yield from records

    def jobs(self) -> list[Job]:
        """The copies this scene still lacks, sized to share the budget."""
        self.prune()
        if not self.wanted():
            return []
        parts = self.parts()
        total = sum(cells for _, _, cells in parts) or 1
        target = self.budget().lod_target_triangles
        wanted = []
        for actor, data, cells in parts:
            share = max(1, int(target * cells / total))
            if cells < MIN_PART_CELLS or cells < 2 * share:
                continue
            if self._currentFor(actor) is not None:
                continue
            if _colours_by_points(actor.GetMapper(), data):
                continue
            wanted.append(Job(actor, data, int(data.GetMTime()),
                              shared_view(data), share))
        return wanted

    # -- the copies -------------------------------------------------------

    def install(self, job: Job, reduced) -> bool:
        """Put the copy ``reduced`` of ``job`` in the scene, hidden.

        False when the part changed or left the scene while it was made.
        """
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        actor = job.actor
        if not self._renderer.HasViewProp(actor):
            return False
        if _polydata_input(actor) is not job.data or \
                int(job.data.GetMTime()) != job.mtime:
            return False
        records = self._records.setdefault(id(actor), [])
        for old in [r for r in records if r.key == _address(job.data)]:
            records.remove(old)
            self._drop(old)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(reduced)
        mapper.SetStatic(True)
        lod = vtkActor()
        lod.SetMapper(mapper)
        lod.SetProperty(actor.GetProperty())
        backface = actor.GetBackfaceProperty()
        if backface is not None:
            lod.SetBackfaceProperty(backface)
        lod.PickableOff()
        lod.VisibilityOff()
        try:
            name = actor.GetObjectName()
            lod.SetObjectName(f'{name}{LOD_SUFFIX}')
        except Exception:                                  # noqa: BLE001
            pass
        self._renderer.AddActor(lod)
        self._lods.add(id(lod))
        records.append(_Record(actor, job.data, job.mtime, lod))
        # Keep the newest few; a copy being drawn is never the one to go.
        while len(records) > VARIANTS_KEPT:
            victim = next((r for r in records if not r.hidden), None)
            if victim is None:
                break
            records.remove(victim)
            self._drop(victim)
        return True

    def _drop(self, record: _Record) -> None:
        if record.hidden:
            record.actor.SetVisibility(True)
        self._lods.discard(id(record.lod))
        try:
            self._renderer.RemoveActor(record.lod)
        except Exception:                                  # noqa: BLE001
            pass

    def prune(self) -> None:
        """Forget copies whose part left the scene or whose input changed.

        A copy of an input the part no longer shows stays while that input is
        alive and unchanged (a cut that was taken off brings it back).
        """
        for key, records in list(self._records.items()):
            for record in list(records):
                if record.hidden:
                    continue
                if not self._renderer.HasViewProp(record.actor) or \
                        self._stale(record):
                    records.remove(record)
                    self._drop(record)
            if not records:
                del self._records[key]

    def clear(self) -> None:
        self.end()
        for record in list(self._all()):
            self._drop(record)
        self._records.clear()
        self._frames.clear()

    def ready(self) -> int:
        """Parts that have a copy matching what they show now."""
        return sum(1 for records in self._records.values()
                   if any(self._current(r) for r in records))

    # -- the swap ---------------------------------------------------------

    def begin(self) -> int:
        """Draw the copies instead of their parts; the number swapped.

        Only parts that are visible and still match their copy; any other
        part is drawn in full.
        """
        if self._active:
            return sum(1 for r in self._all() if r.hidden)
        if not self.wanted():
            return 0
        swapped = 0
        for key in list(self._records):
            record = self._currentFor(self._records[key][0].actor)
            if record is None:
                continue
            actor = record.actor
            try:
                if not actor.GetVisibility():
                    continue
                _followMapper(record.lod.GetMapper(), actor.GetMapper())
                record.lod.SetUserMatrix(actor.GetUserMatrix())
                record.lod.SetPosition(actor.GetPosition())
                record.lod.SetOrientation(actor.GetOrientation())
                record.lod.SetScale(actor.GetScale())
                record.lod.SetOrigin(actor.GetOrigin())
                actor.SetVisibility(False)
                record.lod.SetVisibility(True)
                record.hidden = True
                swapped += 1
            except Exception:                              # noqa: BLE001
                logger.debug('interaction detail swap failed', exc_info=True)
        self._active = swapped > 0
        return swapped

    def end(self) -> bool:
        """Every part back in full; True if anything was swapped."""
        if not self._active:
            return False
        for record in self._all():
            if record.hidden:
                record.lod.SetVisibility(False)
                record.actor.SetVisibility(True)
                record.hidden = False
        self._active = False
        return True

    def active(self) -> bool:
        return self._active

    def standIn(self, actor):
        """The reduced copy drawn in place of ``actor`` right now, or None.

        Plan 39 VI14c. A section plane dragged while the copies stand in is
        applied to these and not to the full parts, whose mappers would
        otherwise be modified (every buffer of a multi-million-face part
        rebuilt on the next full frame) for a picture nobody sees.
        """
        for record in self._all():
            if record.hidden and record.actor is actor:
                return record.lod
        return None

    def isCopy(self, actor) -> bool:
        return id(actor) in self._lods


def _followMapper(lod, source) -> None:
    """Colour the copy the way the part is coloured now."""
    # A mapper clipping plane is a clip that costs the part nothing and
    # leaves its input alone, so the copy is clipped by the same planes.
    try:
        planes = source.GetClippingPlanes()
        lod.RemoveAllClippingPlanes()
        if planes is not None:
            for index in range(planes.GetNumberOfItems()):
                lod.AddClippingPlane(planes.GetItemAsObject(index))
    except Exception:                                      # noqa: BLE001
        logger.debug('interaction detail clip follow failed', exc_info=True)
    try:
        lod.SetScalarVisibility(source.GetScalarVisibility())
        lod.SetScalarMode(source.GetScalarMode())
        lod.SetColorMode(source.GetColorMode())
        lod.SetLookupTable(source.GetLookupTable())
        lod.SetScalarRange(source.GetScalarRange())
        lod.SetUseLookupTableScalarRange(source.GetUseLookupTableScalarRange())
        lod.SetArrayAccessMode(source.GetArrayAccessMode())
        lod.SetArrayName(source.GetArrayName())
        lod.SetArrayId(source.GetArrayId())
        lod.SetArrayComponent(source.GetArrayComponent())
    except Exception:                                      # noqa: BLE001
        logger.debug('interaction detail colour follow failed', exc_info=True)
    # The part's depth bias (ActorInfo's polygon offset), so patches that
    # share faces do not fight on the copy either.
    try:
        from vtkmodules.vtkCommonCore import reference
        for name in ('Polygon', 'Line'):
            factor, units = reference(0.0), reference(0.0)
            getattr(source, f'GetRelativeCoincidentTopology{name}'
                            'OffsetParameters')(factor, units)
            getattr(lod, f'SetRelativeCoincidentTopology{name}'
                         'OffsetParameters')(factor.get(), units.get())
    except Exception:                                      # noqa: BLE001
        logger.debug('interaction detail offset follow failed', exc_info=True)


__all__ = ['FRAME_BUDGET_SECONDS', 'FRAMES_AVERAGED', 'InteractionDetail',
           'Job', 'LOD_SUFFIX', 'MIN_PART_CELLS', 'VARIANTS_KEPT',
           'divisions_for', 'reduce', 'shared_view']
