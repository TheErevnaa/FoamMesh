"""Load the accepted run's mesh into the resident Gmsh session (Plan 42 SG15A).

The guided Generate runs through the facade, so the resident session holds no
mesh when the Export page opens and the Gmsh writers had nothing to read.  With
the Accepted-run source chosen, the run's own ``mesh.msh`` is opened in the
session; the writers then carry the mesh the user just made, and the formats
that mesh cannot be written in are greyed with their reasons.

No Qt in here: the panel runs :func:`load` on a worker thread.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RunMeshRefused(Exception):
    """The accepted run has no mesh the session can open; the sentence."""


@dataclass(frozen=True)
class RunMesh:
    run_id: str
    path: Path
    mtime_ns: int

    @property
    def key(self) -> tuple:
        return (self.run_id, str(self.path), self.mtime_ns)


def accepted_mesh(case_path) -> RunMesh:
    """The ``mesh.msh`` of the accepted run, checked; else a plain refusal."""
    from .manifest import accepted_artifact

    if case_path is None:
        raise RunMeshRefused('open a project first; there is no run to load.')
    try:
        row = accepted_artifact(Path(case_path), 'msh')
    except Exception:                                    # noqa: BLE001
        row = None
    if not row:
        raise RunMeshRefused(
            'There is no accepted Gmsh run with a mesh.msh to load.')
    path = Path(row['path'])
    run_id = str(row.get('run_id') or path.parent.name)
    try:
        size = path.stat().st_size
    except OSError:
        raise RunMeshRefused(
            f'The mesh of run {run_id} is missing ({path}); run the mesh '
            'again.') from None
    if size == 0:
        raise RunMeshRefused(
            f'The mesh of run {run_id} is empty ({path}); run the mesh '
            'again.')
    try:
        with open(path, 'rb') as handle:
            head = handle.read(16)
    except OSError as error:
        raise RunMeshRefused(
            f'The mesh of run {run_id} cannot be read: {error}.') from None
    if not head.startswith(b'$MeshFormat'):
        raise RunMeshRefused(
            f'The mesh of run {run_id} ({path.name}) is not a Gmsh .msh '
            'file; run the mesh again.')
    record_provenance(Path(case_path), path, row)
    return RunMesh(run_id, path, path.stat().st_mtime_ns)


def record_provenance(case_path, mesh_path, row=None) -> bool:
    """Plan 48 GM12 (DP-4359, DP-4358, DP-4360): keep how the run's mesh was
    made, keyed by the mesh file, so the viewport tools read the layers the run
    asked for instead of "unavailable".

    The request comes from the run's own ``job.json``. Once per mesh file: a
    record that is already there is left alone. Never raises (a missing job
    only means less is recorded). Returns whether a record was written.
    """
    import json

    from foammesh.core import provenance

    case_path, mesh_path = Path(case_path), Path(mesh_path)
    roots = [case_path]
    if mesh_path.parent != case_path:
        roots.append(mesh_path.parent)
    try:
        if provenance.find(roots, mesh_path).found:
            return False
        job = {}
        run_path = (row or {}).get('run_path') or str(mesh_path.parent)
        try:
            job = json.loads((Path(run_path) / 'job.json').read_text(
                encoding='utf-8'))
        except (OSError, ValueError):
            job = {}
        intent = job.get('intent') or {}
        layers = intent.get('layers') or {}
        requested = None
        if layers.get('enabled') and layers.get('patches'):
            requested = {str(name): int(layers.get('layerCount') or 0)
                         for name in layers['patches']}
        cad = next((item for item in job.get('geometry') or ()
                    if Path(str(item)).is_file()), None)
        for root in roots:
            provenance.record_mesh(
                root, mesh_path, route='gmsh', settings=intent or None,
                requested_layers=requested, cad_reference=cad)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def loaded(controller) -> tuple | None:
    """The key of the run mesh this controller's session was loaded with."""
    return getattr(controller, '_loadedRunMeshKey', None)


def held(controller) -> str:
    """What the session holds, in words; empty when it holds nothing."""
    session = getattr(controller, 'session', None)
    if session is None or not session.alive:
        return ''
    try:
        facts = session.export_facts()
    except Exception:                                    # noqa: BLE001
        return 'a model of unknown content'
    nodes = int(facts.get('nodes') or 0)
    entities = int(facts.get('entities') or 0)
    if not nodes and not entities:
        return ''
    opened = list(getattr(controller, '_opened', None) or ())
    if loaded(controller):
        what = f'the mesh of run {loaded(controller)[0]}'
    elif opened:
        what = f'the model opened from {Path(opened[-1]).name}'
    else:
        what = 'a model'
    return (f'{what} ({nodes:,} nodes, {entities:,} entities)')


def needs_load(controller, run: RunMesh) -> bool:
    """False when the session already holds exactly this run's mesh."""
    if loaded(controller) != run.key:
        return True
    return not held(controller)


def question(controller, run: RunMesh) -> str:
    """The sentence of the confirm dialog, or empty when nothing is replaced."""
    now = held(controller)
    if not now:
        return ''
    return (f'The Gmsh session holds {now}. Loading the mesh of the accepted '
            f'run {run.run_id} ({run.path.name}) replaces it. Replace it?')


def load(controller, run: RunMesh, progress=None) -> dict:
    """Open the run's mesh in the session, replacing its model.

    Raises :class:`RunMeshRefused` with a plain sentence when Gmsh refuses it.
    """
    from .import_formats import ImportRefusal

    def say(text, fraction):
        if progress is not None:
            progress(text, fraction)

    say(f'Reading the mesh of run {run.run_id}', 0.1)
    try:
        result = controller.open_run_mesh(str(run.path))
    except ImportRefusal as error:
        raise RunMeshRefused(str(error)) from error
    except Exception as error:                           # noqa: BLE001
        raise RunMeshRefused(
            f'Gmsh could not open the mesh of run {run.run_id}: '
            f'{str(error).strip().splitlines()[-1] if str(error).strip() else type(error).__name__}.'
        ) from error
    controller._loadedRunMeshKey = run.key
    say('Mesh loaded into the Gmsh session', 1.0)
    return result
