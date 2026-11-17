"""Which mesh is on screen, so a refresh can tell it has not changed.

Plan 50 IP9 (UI-1, DP-6271).  Every committed edit used to re-read the mesh
on screen: ``MeshManager.reload`` had no same-mesh test, so a field commit
on a multi-million-cell case opened "Loading mesh", ran the preview worker
and swapped the whole scene for an identical one.

The identity has two halves:

* the **request** -- what was asked to be drawn: the case root and time (or
  the native file), the artifact id and stage, and the region points the
  reader splits regions by (a Regions edit changes the picture of the same
  files);
* the **files** -- for each polyMesh directory the reader may read
  (``constant/polyMesh``, ``<time>/polyMesh``, every
  ``processor*/constant/polyMesh``) the names, sizes and ``st_mtime_ns`` of
  its files, plus whether the pending-gather marker is there; for a native
  result the file's size and ``st_mtime_ns``.

Why that and not a hash: hashing the mesh is the cost being removed.  Why it
is trustworthy enough: ``fingerprint_poly_mesh`` (core/case/model.py, Plan 45)
already keys its cache on exactly (name, size, mtime_ns) per file.  The
PENDING_GATHER note in ``domain_operations`` says WSL mtimes are too coarse
to *order* the root against the processor cases; this compares each file
with its own earlier self, where a rewrite has to keep every size and land in
the same timestamp tick to go unseen.  And it is only half of the guard: an
announced mesh change (ARTIFACT_MESH_CHANGED, ARTIFACT_RESTORED) never asks
it -- the window reloads those unconditionally.  Anything this cannot read
is ``None``, which never matches, so an unknown answer reloads.

The file half runs on a worker thread (``asyncio.to_thread``); nothing here
touches Qt.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Kept in step with ``DomainOperations.PENDING_GATHER``: whether the root or
#: the processor cases are the newer mesh is decided by this marker.
PENDING_GATHER = 'foammesh/pending-gather.json'


def _listing(directory: Path):
    """(name, size, mtime_ns) of each file in *directory*; ``'missing'``."""
    try:
        with os.scandir(directory) as entries:
            rows = []
            for entry in entries:
                if not entry.is_file():
                    continue
                stat = entry.stat()
                rows.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except FileNotFoundError:
        return 'missing'
    except NotADirectoryError:
        return 'missing'
    return tuple(sorted(rows))


def _stat(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 'missing'
    return (stat.st_size, stat.st_mtime_ns)


def case_files(root, time) -> tuple | None:
    """The file half for a case-root load; ``None`` if it cannot be read."""
    try:
        root = Path(root)
        rows = [('constant', _listing(root / 'constant' / 'polyMesh'))]
        if time not in (0, '0', None):
            rows.append((str(time), _listing(root / str(time) / 'polyMesh')))
        processors = sorted(p.name for p in root.glob('processor*')
                            if p.is_dir())
        for name in processors:
            rows.append((name, _listing(root / name / 'constant' / 'polyMesh')))
        rows.append(('pending-gather', _stat(root / PENDING_GATHER)))
        return tuple(rows)
    except Exception:                                  # noqa: BLE001
        return None


def native_files(path) -> tuple | None:
    """The file half for a native (``.msh`` / hand-over) result."""
    try:
        path = Path(path)
        if path.is_dir():
            return ('dir', _listing(path))
        return ('file', _stat(path))
    except Exception:                                  # noqa: BLE001
        return None


def seeds_key(seeds) -> tuple:
    """The region points as a comparable value."""
    key = []
    for name, point in seeds or ():
        try:
            key.append((str(name), tuple(float(x) for x in point)))
        except Exception:                              # noqa: BLE001
            key.append((str(name), repr(point)))
    return tuple(key)


def case_request(root, time, artifact_id, stage, seeds) -> tuple:
    return ('case', str(Path(root)) if root else '', time,
            str(artifact_id or ''), str(stage or ''), seeds_key(seeds))


def native_request(path, artifact_id, stage, surface_only) -> tuple:
    return ('native', str(Path(path)), str(artifact_id or ''),
            str(stage or ''), bool(surface_only))
