"""Whether the regions two or more seeds name can touch at all.

DP-543. MEASURED on the 24 Sep 2026 audit case S6: two closed cubes 1 m
apart, one Fluid seed in each. The Domain page said ``No Inter-region
interface is configured while region points are configured`` from the moment
the second seed went in and the sentence stood through Quality and Export --
while checkMesh read back two fully disconnected regions of 4,864 cells each
with no shared face. There is no interface to configure between two volumes
that never meet, so the warning asked for something the geometry cannot have.

The question is answered from the geometry the mesher is handed, with the
same shell classifier both engines use (``shell_topology.build_shells``), and
it is answered conservatively. Two regions are called *separated* only when

* every seed lies inside a closed shell,
* no two seeds lie inside the same innermost shell, and
* the bounding boxes of those shells are pairwise apart by more than a
  tolerance.

Disjoint boxes cannot hold touching surfaces, so a ``separated`` answer is
never wrong. Everything else -- a shared or coincident face, nesting, boxes
that merely overlap, a seed outside every closed shell, CAD geometry with no
tessellation to read -- is reported as not separated, and the page keeps its
warning. A warning shown once too often costs a sentence; one withheld from
regions that do touch would mesh them uncoupled.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import domain_topology
from .interface_split import inside

#: Keyed on the artifact files (path, size, mtime) and the seeds, because the
#: page asks every time it redraws and the answer only moves with those.
_CACHE: dict = {}
_CACHE_LIMIT = 32

#: A box gap smaller than this share of the whole geometry's extent counts as
#: contact: coincident faces written to two files do not agree to the last bit.
RELATIVE_TOLERANCE = 1e-6


def _files(entries) -> list[str]:
    paths = []
    for entry in entries or ():
        path = domain_topology.entry_path(entry)
        if not path:
            continue
        if Path(path).suffix.lower() not in domain_topology.TESSELLATED_SUFFIXES:
            # A CAD source has no triangulation here to test a seed against,
            # so the case cannot be judged and the warning stands.
            return []
        paths.append(path)
    return paths


def _key(paths, points) -> tuple:
    stamp = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            stamp.append((path, None, None))
        else:
            stamp.append((path, stat.st_size, stat.st_mtime_ns))
    return tuple(stamp), tuple(tuple(float(v) for v in p) for p in points)


def _apart(first, second, tolerance: float) -> bool:
    """Are two ``(xmin, ymin, zmin, xmax, ymax, zmax)`` boxes clear of each other?"""
    return any(first[axis] - second[axis + 3] > tolerance
               or second[axis] - first[axis + 3] > tolerance
               for axis in range(3))


def regions_separated(entries, points) -> bool:
    """True only when the seeds' regions provably share no surface.

    ``entries`` are the geometry store's manifest entries, ``points`` the
    region seeds. Any geometry this cannot read answers False.
    """
    points = [tuple(float(v) for v in point) for point in points or ()]
    if len(points) < 2:
        return False
    paths = _files(entries)
    if not paths:
        return False
    key = _key(paths, points)
    if key in _CACHE:
        return _CACHE[key]
    try:
        answer = _separated(paths, points)
    except Exception:                                        # noqa: BLE001
        # Unreadable geometry is not evidence of separation.
        answer = False
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = answer
    return answer


def _separated(paths, points) -> bool:
    topology = domain_topology._classifier()
    surfaces, triangles, coordinates, sources = (
        domain_topology.read_tessellation(paths))
    if not surfaces:
        return False
    shells, owned = topology.build_shells(
        surfaces, triangles, coordinates, sources)
    closed = [shell for shell in shells if shell.closed]
    if not closed:
        return False
    extent = max(max(shell.box[axis + 3] - shell.box[axis]
                     for axis in range(3)) for shell in shells) or 1.0
    tolerance = extent * RELATIVE_TOLERANCE

    chosen = []
    for point in points:
        holders = [shell for shell in closed
                   if all(shell.box[axis] <= point[axis] <= shell.box[axis + 3]
                          for axis in range(3))
                   and inside(point, owned[shell.name], coordinates)]
        if not holders:
            return False
        # The innermost shell is the region the seed selects.
        innermost = min(holders, key=lambda shell: abs(shell.volume))
        if any(innermost is other for other in chosen):
            return False
        chosen.append(innermost)

    for index, first in enumerate(chosen):
        for second in chosen[index + 1:]:
            if not _apart(first.box, second.box, tolerance):
                return False
    return True
