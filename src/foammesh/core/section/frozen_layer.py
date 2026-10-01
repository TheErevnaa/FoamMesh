"""Freeze a layer of cells to the mesh they came from (Plan 37 UF11).

No Qt. Freezing takes the ``sourceCellId`` s of the cells the section shows
(the cut-cells layer) and binds them to **one mesh revision**: the live
polyMesh's identity (`task_state_store.mesh_identity`) and, when the live
mesh is still the copy of a kept stage snapshot (`stage_snapshots.
live_mesh_is`), that snapshot's ``{stage, revision, digest}``.

The frozen cells stay on screen while the planes move. When the mesh
changes underneath (a re-mesh):

* the frozen snapshot still kept and verified -> the cells are drawn **from
  that snapshot**, labelled with its stage and revision;
* otherwise the selection is **invalid** with the reason, and nothing is
  drawn. Cell numbers are never matched to the new mesh (no nearest-index
  remapping): cell 17 of a re-meshed case is another cell.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from foammesh.core.jobs import stage_snapshots as snapshots
from foammesh.core.section.modes import SectionMode
from foammesh.core.section.named_sections import FrozenSelection, pack_ids

__all__ = ['freeze', 'FrozenStatus', 'status', 'CURRENT', 'SNAPSHOT',
           'INVALID', 'request_cells', 'worker_read_is_live', 'FrozenRun',
           'run_frozen']

CURRENT = 'current'
SNAPSHOT = 'snapshot'
INVALID = 'invalid'


def _identity(case):
    from foammesh.core.workflow.task_state_store import mesh_identity
    return mesh_identity(case)


def freeze(case_path, cells, *, plane: int = 0,
           mode=SectionMode.CUT_CELLS) -> FrozenSelection:
    """Bind *cells* (source cell ids of the live mesh) to that mesh.

    Raises ``ValueError`` when the case has no polyMesh to bind to.
    """
    case = Path(case_path)
    identity = _identity(case)
    if not identity:
        raise ValueError('the case has no polyMesh to freeze cells of')
    ids = tuple(int(i) for i in np.unique(np.asarray(cells, dtype=np.int64)))
    if not ids:
        raise ValueError('there are no cells to freeze')
    snapshot = None
    record = snapshots.active(case)
    if record and snapshots.live_mesh_is(case, record.get('stage'),
                                         record.get('revision')):
        snapshot = {'stage': record['stage'],
                    'revision': record['revision'],
                    'digest': record.get('digest')}
    return FrozenSelection(ids, identity, snapshot,
                           str(getattr(mode, 'value', mode)), int(plane))


@dataclass(frozen=True)
class FrozenStatus:
    state: str                   # current | snapshot | invalid
    case_dir: str | None = None  # the case the cells are read from
    label: str = ''
    reason: str = ''

    @property
    def drawable(self) -> bool:
        return self.state in (CURRENT, SNAPSHOT) and self.case_dir is not None


def status(case_path, frozen: FrozenSelection, *,
           verify: bool = True) -> FrozenStatus:
    """Where *frozen*'s cells can be drawn from now, or why nowhere."""
    case = Path(case_path)
    count = len(frozen.cells)
    if _identity(case) == frozen.mesh_identity:
        where = ''
        if frozen.snapshot:
            where = ' ({0} · {1})'.format(frozen.snapshot.get('stage'),
                                            frozen.snapshot.get('revision'))
        return FrozenStatus(CURRENT, str(case),
                            f'{count} frozen cells of this mesh{where}')
    ref = frozen.snapshot or {}
    stage, revision = ref.get('stage'), ref.get('revision')
    if not stage or not revision:
        return FrozenStatus(INVALID, reason=(
            'The mesh has changed since these cells were frozen, and that '
            'mesh was not a kept stage snapshot; the frozen cells are not '
            'shown rather than matched to cells of the new mesh.'))
    manifest = snapshots.read_manifest(case, stage, revision)
    if manifest is None or snapshots.stage_mesh_path(
            case, stage, revision) is None:
        return FrozenStatus(INVALID, reason=(
            f'The mesh has changed since these cells were frozen, and the '
            f'{stage} snapshot of {revision} they came from is no longer '
            'kept; the frozen cells are not shown rather than matched to '
            'cells of the new mesh.'))
    if manifest.get('digest') != ref.get('digest') or (
            manifest.get('mesh_identity') != frozen.mesh_identity):
        return FrozenStatus(INVALID, reason=(
            f'The {stage} snapshot of {revision} is not the mesh these cells '
            'were frozen on; the frozen cells are not shown.'))
    if verify:
        check = snapshots.verify(case, stage, revision)
        if not check.get('ok'):
            return FrozenStatus(INVALID, reason=(
                f'The {stage} snapshot of {revision} the cells were frozen on '
                'cannot be verified (' + ', '.join(
                    check.get('mismatched', []) + check.get('missing', []))
                + '); the frozen cells are not shown.'))
    folder = snapshots.stages_root(case) / revision / stage
    return FrozenStatus(
        SNAPSHOT, str(folder),
        f'{count} frozen cells from the {stage} snapshot · {revision} '
        '(the mesh has changed since)')


def request_cells(frozen: FrozenSelection) -> tuple:
    """The worker's ``cells`` argument: inclusive runs."""
    return tuple(tuple(run) for run in pack_ids(frozen.cells))


def worker_read_is_live(case_path, manifest) -> bool:
    """Whether the section worker's answer was read from the polyMesh on
    disk now -- so its ``sourceCellId`` s are cells of the live mesh.

    The worker's ``mesh.revision`` is the stat lease it read under; the same
    lease taken now is equal only if no member was rewritten since.
    """
    from foammesh.core.mesh.poly_mesh_lease import ReadLease
    from foammesh.core.mesh.poly_mesh_topology import (
        CELL_LEVEL, CELL_ZONES, REQUIRED)

    mesh = (manifest or {}).get('mesh') or {}
    revision = mesh.get('revision')
    if not revision:
        return False
    arrays = ((manifest or {}).get('key') or {}).get('arrays') or ()
    names = list(REQUIRED)
    if 'cell_zone' in arrays:
        names.append(CELL_ZONES)
    if 'cell_level' in arrays:
        names.append(CELL_LEVEL)
    lease = ReadLease.take(Path(case_path) / 'constant' / 'polyMesh', names)
    return lease.revision == revision


@dataclass
class FrozenRun:
    """The worker's drawing of the frozen cells: its surface file and job
    folder (removed by `release`), or why nothing was drawn."""
    surface: str | None = None
    directory: str | None = None
    reason: str = ''

    def release(self) -> None:
        if self.directory:
            import shutil
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None


async def run_frozen(frozen: FrozenSelection, state: FrozenStatus, *,
                     runner=None, scratch_root=None) -> FrozenRun:
    """Have the section worker draw exactly *frozen*'s cells, whole, from
    *state*'s case (the live mesh or the kept snapshot). A cell that mesh
    does not have refuses (``stale_input``); nothing is remapped."""
    import tempfile
    import uuid

    from foammesh.core.section.plane_state import PlaneState
    from foammesh.core.section.section_jobs import (
        SectionRequest, default_runner)

    root = Path(scratch_root) if scratch_root else Path(
        tempfile.gettempdir()) / 'foammesh-section'
    folder = root / f'frozen-{uuid.uuid4().hex[:12]}'
    folder.mkdir(parents=True, exist_ok=True)
    run = FrozenRun(directory=str(folder))
    # The planes choose nothing: the cells are the answer. One plane is
    # named because a request names one.
    plane = dict(PlaneState.through((0.0, 0.0, 0.0),
                                    (0.0, 0.0, 1.0)).to_dict(), enabled=True)
    request = SectionRequest(
        case_dir=state.case_dir, case_id=f'frozen:{frozen.mesh_identity}',
        generation=0, mode=SectionMode.CUT_CELLS.value, planes=(plane,),
        active=0, cells=request_cells(frozen))
    try:
        outcome = await (runner or default_runner)(request, request.args(
            folder, 'frozen', folder / 'frozen.cancel'))
    except Exception as error:                              # noqa: BLE001
        run.reason = str(error)
        return run
    if not getattr(outcome, 'ok', False):
        run.reason = ('The frozen cells could not be read: '
                      + str(getattr(outcome, 'message', '')
                            or getattr(outcome, 'reason', '') or outcome))
        return run
    manifest = outcome.payload or {}
    run.surface = ((manifest.get('files') or {}).get('surface') or {}).get(
        'path')
    if not run.surface:
        run.reason = 'The frozen cells drew nothing.'
    return run
