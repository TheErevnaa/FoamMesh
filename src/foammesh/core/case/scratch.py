"""Cases that exist before the user has decided where they live.

Every door into FoamMesh used to demand a directory first. ``New`` insisted on
an *empty* one, ``Open`` rejected anything that was not already a case, and
geometry import is gated on an open, writable, locked case at five independent
layers -- so a user holding a STEP file and a folder full of CAD could not get
through any of them. The folder they had was refused by New for not being
empty and by Open for not being a case.

A scratch case is a real case in every respect -- sidecar, skeleton, lock,
journal -- that happens to live in a temporary directory. The workflow runs
against it normally, and the question "where does this go" is deferred to the
moment it starts to matter, which is when something is produced worth keeping.

**Scratch-ness is location, not a flag.** A case is scratch exactly while it
sits under :func:`scratch_root`, so relocating it out makes it permanent with
nothing to unset -- and no way for a stale marker to leave a case in the user's
own directory claiming to be disposable.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

#: Sits under the system temp directory, in one named place rather than
#: scattered ``tmpXXXX`` siblings, so a user who goes looking can tell what
#: these are and delete them.
SCRATCH_DIRNAME = 'foammesh-scratch'

#: How long an abandoned scratch case survives. Long enough that a crash does
#: not lose yesterday's work before the user notices it is gone, short enough
#: that temp does not accumulate meshes forever.
STALE_AFTER_SECONDS = 7 * 24 * 60 * 60


def scratch_root() -> Path:
    return Path(tempfile.gettempdir()) / SCRATCH_DIRNAME


def create_scratch_dir(name: str = 'untitled') -> Path:
    """An empty directory for a new scratch case.

    Named for what it holds rather than randomly, so the temp folder reads as
    a list of cases; the random suffix only keeps two of the same name apart.
    """
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f'{_slug(name)}-', dir=root))


def is_scratch_case(path: str | Path | None) -> bool:
    """Whether *path* is a case that has not been given a home yet."""
    if path is None:
        return False
    try:
        candidate = Path(path).expanduser().resolve()
        root = scratch_root().resolve()
    except (OSError, TypeError, ValueError):
        # This is asked on every window-title refresh and of anything a caller
        # happens to be holding. Something that is not a path is not a scratch
        # case; it is not a reason to take the title bar down.
        return False
    return candidate == root or root in candidate.parents


def suggested_name(path: str | Path | None) -> str:
    """A starting point for the Save-as name, taken from the scratch folder.

    ``wing-3f9a2c`` was created from ``wing.step``, so offering ``wing`` beats
    offering ``untitled`` -- the user already told us what this is by importing
    it.
    """
    if path is None:
        return 'case'
    stem = Path(path).name
    head = stem.rsplit('-', 1)[0] if '-' in stem else stem
    return head or 'case'


def discard_scratch_case(path: str | Path) -> bool:
    """Delete a scratch case once its contents have been saved elsewhere.

    **Refuses anything outside the scratch root.** This is called with a path
    that came from an open project, moments after a copy, and a bug upstream
    would otherwise point a recursive delete at the user's own directory. The
    check costs nothing and bounds the blast radius to the temporary folder.
    """
    if not is_scratch_case(path):
        return False
    target = Path(path).expanduser().resolve()
    if target == scratch_root().resolve():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def prune_stale(*, older_than: float = STALE_AFTER_SECONDS,
                keep: str | Path | None = None) -> list[Path]:
    """Delete abandoned scratch cases, returning what was removed.

    A locked case is left alone: a lock means another FoamMesh has it open, and
    deleting a case out from under a running process is a far worse failure
    than leaving a directory in temp.
    """
    root = scratch_root()
    if not root.is_dir():
        return []
    protected = None
    if keep is not None:
        try:
            protected = Path(keep).expanduser().resolve()
        except OSError:
            protected = None

    removed: list[Path] = []
    cutoff = time.time() - older_than
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            if protected is not None and entry.resolve() == protected:
                continue
            if entry.stat().st_mtime > cutoff:
                continue
            if _is_locked(entry):
                continue
            shutil.rmtree(entry)
        except OSError:
            # Housekeeping never fails a launch. A directory that will not
            # delete is left for the next run or for the operating system.
            continue
        removed.append(entry)
    return removed


def _is_locked(case_path: Path) -> bool:
    lock = case_path / 'foammesh' / 'case.lock'
    if not lock.exists():
        return False
    try:
        # On Windows an open lock file cannot be renamed; on POSIX this
        # succeeds and is undone immediately. Either way nothing is destroyed.
        probe = lock.with_name('case.lock.prune-probe')
        os.replace(lock, probe)
        os.replace(probe, lock)
    except OSError:
        return True
    return False


def _slug(name: str) -> str:
    cleaned = ''.join(
        character if character.isalnum() or character in '-_' else '-'
        for character in str(name).strip())
    return cleaned.strip('-').lower() or 'untitled'
