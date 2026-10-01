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
* **Said on screen.** The view emits ``detailReduced(True)`` when it swaps.

``vtkQuadricLODActor`` (``MeshActor``) built its copy on the GUI thread on
the first interactive frame -- seconds on a large mesh; the view no longer
raises the render rate that triggers it.

No Qt here; ``RenderingWidget`` owns the timing.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math

logger = logging.getLogger(__name__)

#: A part with fewer cells than this is drawn in full while moving: reducing
#: it saves nothing worth the copy.
MIN_PART_CELLS = 50_000
#: The most clustering divisions along one axis.
MAX_DIVISIONS = 2048
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
    __slots__ = ('actor', 'data', 'mtime', 'lod', 'hidden')

    def __init__(self, actor, data, mtime, lod):
        self.actor = actor
        self.data = data
        self.mtime = mtime
        self.lod = lod
        self.hidden = False


class InteractionDetail:
    """The reduced copies of one renderer's large parts, and the swap."""

    def __init__(self, renderer, budget=None):
        self._renderer = renderer
        #: A callable returning the ``DisplayBudget`` in force.
        self._budget = budget
        self._records: dict[int, _Record] = {}
        self._lods: set[int] = set()
        self._active = False
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

    def wanted(self) -> bool:
        """True when a drag of this scene should draw reduced copies."""
        cells = self.sceneCells()
        if cells <= 0:
            return False
        if self.forced:
            return cells > self.budget().lod_target_triangles
        return cells > self.budget().interactive_lod_faces

    def _current(self, record: _Record) -> bool:
        try:
            data = _polydata_input(record.actor)
            return data is record.data and int(data.GetMTime()) == record.mtime
        except Exception:                                  # noqa: BLE001
            return False

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
            record = self._records.get(id(actor))
            if record is not None and self._current(record):
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
        old = self._records.pop(id(actor), None)
        if old is not None:
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
        self._records[id(actor)] = _Record(actor, job.data, job.mtime, lod)
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
        """Forget copies whose part left the scene or changed."""
        for key, record in list(self._records.items()):
            if record.hidden:
                continue
            if not self._renderer.HasViewProp(record.actor) or \
                    not self._current(record):
                self._drop(self._records.pop(key))

    def clear(self) -> None:
        self.end()
        for record in self._records.values():
            self._drop(record)
        self._records.clear()

    def ready(self) -> int:
        return sum(1 for r in self._records.values() if self._current(r))

    # -- the swap ---------------------------------------------------------

    def begin(self) -> int:
        """Draw the copies instead of their parts; the number swapped.

        Only parts that are visible and still match their copy; any other
        part is drawn in full.
        """
        if self._active:
            return sum(1 for r in self._records.values() if r.hidden)
        if not self.wanted():
            return 0
        swapped = 0
        for record in self._records.values():
            actor = record.actor
            try:
                if not actor.GetVisibility() or not self._current(record):
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
        for record in self._records.values():
            if record.hidden:
                record.lod.SetVisibility(False)
                record.actor.SetVisibility(True)
                record.hidden = False
        self._active = False
        return True

    def active(self) -> bool:
        return self._active

    def isCopy(self, actor) -> bool:
        return id(actor) in self._lods


def _followMapper(lod, source) -> None:
    """Colour the copy the way the part is coloured now."""
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


__all__ = ['InteractionDetail', 'Job', 'LOD_SUFFIX', 'MIN_PART_CELLS',
           'divisions_for', 'reduce', 'shared_view']
