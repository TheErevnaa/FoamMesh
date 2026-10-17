"""Snapshots of a Gmsh session's passes, for stage compare (Plan 41 GN23, FM-08).

The snappy route keeps one mesh per stage (`core.jobs.stage_snapshots`); the
Gmsh route has one resident session whose mesh changes pass by pass. After
each pass that leaves a mesh (1D, 2D, 3D, Optimize, Set order) the session's
hand-over (``mesh.vtu``) is kept under::

    <case>/foammesh/gmsh/passes/<key>/mesh.vtu + manifest.json

A pass is kept once and replaced by the next run of the same pass; the
manifest holds the file's sha256 so a changed file is refused, never cut.
No Qt, no Gmsh: the caller hands in the function that writes the hand-over.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

__all__ = ['PASSES', 'PREFIX', 'pass_key', 'stage_name', 'root', 'capture',
           'read_manifest', 'verify', 'mesh_path', 'kept']

PREFIX = 'gmsh.'
#: ``(key, label)`` in the order the passes are run.
PASSES = (('1d', '1D'), ('2d', '2D'), ('3d', '3D'),
          ('optimise', 'Optimise'), ('order', 'Order'))
_ACTIONS = {'generate1d': '1d', 'generate2d': '2d', 'generate3d': '3d',
            'generate_all': '3d', 'optimize': 'optimise',
            'set_order': 'order'}


def pass_key(action_id: str) -> str | None:
    """The pass an action ends, or ``None`` for an action that keeps none."""
    return _ACTIONS.get(str(action_id))


def stage_name(key: str) -> str:
    return PREFIX + key


def root(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / 'passes'


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            sha.update(chunk)
    return sha.hexdigest()


def capture(case_path, key: str, write_handover, *, action: str = '',
            after: dict | None = None) -> dict:
    """Keep the mesh the session holds after pass *key*.

    ``write_handover(directory)`` writes ``mesh.vtu`` into *directory*.
    Returns the manifest. The previous copy of the pass is replaced only
    once the new one is complete.
    """
    base = root(case_path)
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{key}.partial-', dir=base))
    try:
        write_handover(staging)
        mesh = staging / 'mesh.vtu'
        if not mesh.is_file():
            raise RuntimeError('the hand-over wrote no mesh.vtu')
        manifest = {
            'schema_version': 1, 'pass': key, 'action': action,
            'files': {'mesh.vtu': [mesh.stat().st_size, _digest(mesh)]},
            'digest': _digest(mesh),
            'mesh_identity': dict(after or {}),
            'captured_at': datetime.now(timezone.utc).isoformat(),
        }
        (staging / 'manifest.json').write_text(
            json.dumps(manifest, indent=2), encoding='utf-8')
        target = base / key
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def capture_file(case_path, key: str, source, *, action: str = '',
                 after: dict | None = None) -> dict:
    """Keep the hand-over file *source* as the mesh of pass *key*.

    The copy lives here, behind the core, so no view touches the file
    system (the AF4 rule).
    """
    source = Path(source)

    def write(directory):
        shutil.copyfile(source, Path(directory) / 'mesh.vtu')
    return capture(case_path, key, write, action=action, after=after)


def read_manifest(case_path, key: str) -> dict | None:
    try:
        return json.loads((root(case_path) / key / 'manifest.json')
                          .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def mesh_path(case_path, key: str) -> Path | None:
    path = root(case_path) / key / 'mesh.vtu'
    return path if path.is_file() else None


def verify(case_path, key: str) -> dict:
    """``{'ok': bool, 'mismatched': [...], 'missing': [...]}``."""
    manifest = read_manifest(case_path, key)
    path = mesh_path(case_path, key)
    if manifest is None or path is None:
        return {'ok': False, 'mismatched': [], 'missing': ['mesh.vtu']}
    if _digest(path) != manifest.get('digest'):
        return {'ok': False, 'mismatched': ['mesh.vtu'], 'missing': []}
    return {'ok': True, 'mismatched': [], 'missing': []}


def kept(case_path) -> list[str]:
    """The pass keys that have a kept mesh, in pass order."""
    return [key for key, _label in PASSES
            if mesh_path(case_path, key) is not None]
