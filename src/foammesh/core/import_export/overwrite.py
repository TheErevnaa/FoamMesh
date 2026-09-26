"""Replacing an export that is already on disk, and nothing else (DP-680).

An export refuses a destination that exists (DP-562), and the Export page
offers the next free name, so there was no way to write a mesh where the last
one went. ``replacing`` is the one way this product overwrites an export.

It touches exactly one path, the target:

* the target must be what an export of that kind writes and must hold a mesh
  -- an OpenFOAM case folder with a ``polyMesh`` in it, or an ``.su2`` file
  with an SU2 header. Any other folder or file is refused, not emptied;
* the target may not be the meshing case, a folder holding it, or one of
  that case's own OpenFOAM directories;
* the old target is renamed aside, beside itself, before the export runs. If
  the export fails, whatever it left at the target is removed and the old
  target is renamed back; only after it succeeds is the old one deleted.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import uuid

from foammesh.support.openfoam.polymesh import isPolyMesh

FOLDER = 'folder'
SU2 = 'su2'

#: The directories an OpenFOAM case keeps at its own root. A target with one
#: of these names inside the meshing case is that case's own data.
_CASE_OWN = re.compile(r'^(constant|system|foammesh|case|processor\d+|\d+(\.\d+)?)$')


class OverwriteRefused(ValueError):
    """The target is not an export this product may replace."""


def holds_mesh(target, kind: str) -> bool:
    """Whether *target* is an export of *kind* with a mesh in it."""
    target = Path(target)
    if target.is_symlink():
        return False
    if kind == SU2:
        if not target.is_file() or target.suffix.lower() != '.su2':
            return False
        try:
            with target.open('rb') as handle:
                head = handle.read(4096)
        except OSError:
            return False
        return b'NDIME' in head
    if kind == FOLDER:
        if not target.is_dir():
            return False
        roots = [target, *sorted(target.glob('processor[0-9]*'))]
        for root in roots:
            constant = root / 'constant'
            if isPolyMesh(constant / 'polyMesh'):
                return True
            if constant.is_dir() and any(
                    isPolyMesh(region / 'polyMesh')
                    for region in constant.iterdir() if region.is_dir()):
                return True
        return False
    raise ValueError(f'unknown export kind {kind!r}')


def _require_replaceable(target: Path, kind: str, protected) -> None:
    resolved = target.resolve()
    for case in protected:
        case = Path(case).resolve()
        if resolved == case or case.is_relative_to(resolved):
            raise OverwriteRefused(
                f'{target} holds the case being meshed; it is not replaced.')
        if resolved.parent in (case, case / 'case') and _CASE_OWN.match(
                resolved.name):
            raise OverwriteRefused(
                f'{target} is part of the case being meshed; it is not '
                f'replaced.')
    if not holds_mesh(target, kind):
        what = ('an SU2 mesh file' if kind == SU2
                else 'an OpenFOAM case with a mesh')
        raise OverwriteRefused(
            f'{target} exists and is not {what}; it is not replaced. '
            f'Choose a new name; nothing has been exported.')


@contextmanager
def replacing(target, kind: str, *, overwrite: bool, protected=()):
    """Run the export body with *target* free, replacing it if asked.

    Without *overwrite*, or with no target on disk, this does nothing and the
    export's own "already exists" refusal stands.
    """
    target = Path(target)
    if not overwrite or not (target.exists() or target.is_symlink()):
        yield target
        return
    _require_replaceable(target, kind, protected)
    aside = target.with_name(
        f'.{target.name}.foammesh-replaced-{uuid.uuid4().hex}')
    os.replace(target, aside)
    try:
        yield target
    except BaseException:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            target.unlink(missing_ok=True)
        os.replace(aside, target)
        raise
    if aside.is_dir():
        shutil.rmtree(aside, ignore_errors=True)
    else:
        aside.unlink(missing_ok=True)
