"""Failures and the ledger shared by the runner and the stages (Plan 48 RR)."""
from __future__ import annotations

import math
import re

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from quantities import count_text


#: Plan 31 CP-08 item 3. The ``Mesh.RecombinationAlgorithm`` values this
#: product has qualified, and the reason the set is closed. MEASURED on
#: duct.step at a 0.04 m target: 0 gave 306 quadrangles and left 70 triangles,
#: 1 gave 356 and left none, 2 gave 460, 3 gave 408 -- four values, four
#: different meshes. 4 read back as 4 and then ``generate(3)`` produced no
#: elements at all, surface and volume both empty, without raising; 9 was
#: clamped back to 0 in silence. An option that reads back is not a mesh.
#: Plan 48 D4 (user, 2026-10-21): the set is open to Gmsh's own 0-4. The run's
#: element count is what tells the user about a value that meshes nothing.
TESTED_RECOMBINERS = frozenset({0, 1, 2, 3, 4})


def _cavities(count) -> str:
    """``1 sealed cavity`` or ``3 sealed cavities``, said in four places."""
    return count_text(count, 'sealed cavity', 'sealed cavities')


def _successors_of(origin: dict) -> dict:
    """``{piece: origin}`` read the other way: ``{origin: [pieces]}``.

    C31-04. The runner records where each surviving surface came from; an
    entity identity has to be pushed forward instead, onto everything that
    replaced it.
    """
    successors: dict[int, list[int]] = {}
    for piece, source in origin.items():
        successors.setdefault(int(source), []).append(int(piece))
    for pieces in successors.values():
        pieces.sort()
    return successors


class MeshFailure(RuntimeError):
    """Gmsh could not produce a usable mesh."""


class LayerFold(MeshFailure):
    """The boundary layer folded back through itself.

    DP-74. Raised between the surface pass and the volume pass, and it
    carries what the fold measured so the run can be refitted rather than
    only refused: ``carries`` is the thickness the geometry holds, ``asked``
    the thickness this attempt grew.
    """

    def __init__(self, message, carries=0.0, asked=0.0, folded=0,
                 patch='', at=()):
        super().__init__(message)
        self.carries = float(carries)
        self.asked = float(asked)
        self.folded = int(folded)
        self.patch = str(patch)
        self.at = tuple(at)


class LayerLanding(MeshFailure):
    """The boundary layer fitted, and then the mesh above it degenerated.

    DP-76. A stack thin enough not to fold is not thereby a stack the volume
    mesher can build on: where the fitted thickness is a small fraction of
    the surface elements around it, the cells resting on the inner surface
    come out as pancakes -- wide in the plane of the layer top and flat
    across it. Carries the measurement so the refusal can quote it: ``cells``
    how many landed degenerate, ``worst`` the worst of them, ``thickness``
    what the layer grew, ``asked`` what was asked for.
    """

    def __init__(self, message, cells=0, worst=0.0, thickness=0.0,
                 asked=0.0, at=()):
        super().__init__(message)
        self.cells = int(cells)
        self.worst = float(worst)
        self.thickness = float(thickness)
        self.asked = float(asked)
        self.at = tuple(at)


class LayerCollision(MeshFailure):
    """Two boundary layer stacks grew into the same space.

    DP-121. ``extrudeBoundaryLayer`` gives the stack its own nodes, and on a
    layer that fits, none of them land on top of another: MEASURED across the
    `9f6abca1` corpus, **16 of the 17 layered meshes have zero coincident
    nodes anywhere**, and the seventeenth -- `turbine_cascade` -- has 896, all
    of them on the layer. So a coincidence on the layer is not the separate
    body the merge guard was written to protect; it is two stacks meeting.

    The mesh that comes out of one cannot even be written down. On that run
    the exporter reported `40557 nodes written, 40109 read back`: the reader
    welds the 448 coincident pairs the writer emitted, and 896 is exactly
    twice 448. checkMesh then called the result not runnable, and the runner
    called it ``succeeded``.

    Carries the measurement so the refusal can quote it: ``nodes`` how many
    coincide, ``coincident`` how many the scan found in the whole mesh,
    ``patches`` where they sit, ``at`` one of them.
    """

    def __init__(self, message, nodes=0, coincident=0, patches=(), at=(),
                 stalled=False):
        super().__init__(message)
        self.nodes = int(nodes)
        self.coincident = int(coincident)
        self.patches = tuple(patches)
        self.at = tuple(at)
        # DP-484. True when the stacks did not meet anything: a column Gmsh
        # extruded by zero. That one is cleared by a different surface mesh,
        # which is what :func:`execute_with_another_surface_algorithm` tries.
        self.stalled = bool(stalled)


class SurfaceCrossing(MeshFailure):
    """The surface mesh passes through itself.

    DP-75. Raised from the volume pass, carrying what the scan found so the
    run can be redone on the imported facets rather than only refused.
    ``remeshed`` is False when those facets were already what was meshed, and
    then there is nothing left to fall back to.
    """

    def __init__(self, message, patch='', at=(), pairs=0, remeshed=True):
        super().__init__(message)
        self.patch = str(patch)
        self.at = tuple(at)
        self.pairs = int(pairs)
        self.remeshed = bool(remeshed)


class InvalidSurfaceMesh(MeshFailure):
    """The surface pass left elements Gmsh itself calls invalid.

    DP-6085. MEASURED on the hot fluid / pipe / cold fluid tubes split by
    angle, Gmsh 4.15.2: Frontal-Delaunay fails on a 0.5 m tube patch of
    7,680 triangles, MeshAdapt retries it and logs "88 elements remain
    invalid in surface 18", and the volume pass handed that surface spins in
    "Recovering boundary" at 100% CPU with no end. Raised between the two
    passes so the volume mesher never sees it. ``surfaces`` maps a surface
    tag to its invalid element count, ``patches`` names them, ``triangles``
    is the largest number of imported triangles on one of them, and
    ``remeshed`` is False when the imported facets were what was meshed.
    """

    def __init__(self, message, surfaces=None, patches=(), triangles=0,
                 remeshed=True):
        super().__init__(message)
        self.surfaces = dict(surfaces or {})
        self.patches = tuple(patches)
        self.triangles = int(triangles)
        self.remeshed = bool(remeshed)


#: DP-6085. Gmsh's own words for a surface it could not mesh validly.
INVALID_SURFACE_LOG = re.compile(
    r'(\d+) elements? remains? invalid in surface (\d+)')


def invalid_surface_counts(lines) -> dict:
    """``{surface tag: invalid elements}`` from Gmsh log lines.

    A surface met twice keeps the last count, because a later pass replaces
    the mesh an earlier one left.
    """
    found: dict = {}
    for line in lines or ():
        match = INVALID_SURFACE_LOG.search(str(line))
        if match:
            found[int(match.group(2))] = int(match.group(1))
    return {tag: count for tag, count in found.items() if count > 0}


class Ledger:
    """What was asked for, and what the runtime actually holds.

    Values are read back from Gmsh after being set, so a rejected option shows
    as a mismatch rather than as a success.
    """

    def __init__(self):
        self.entries: list[dict] = []

    def record(self, control, requested, effective, *, applied=True, note='',
               matched=None):
        """``matched`` is compared from the two values unless it is given.

        DP-503. A read-back option compares like with like, and equality is
        the right test. A census does not: ``Triangle`` against
        ``Triangle=1838`` is the family that was asked for, produced, and
        string equality called it a mismatch. A caller whose effective side
        is a description rather than a value says whether it matched.
        """
        if matched is not None:
            matched = bool(matched)
        elif isinstance(requested, (int, float)) and isinstance(effective, (int, float)):
            matched = math.isclose(float(requested), float(effective),
                                   rel_tol=1e-9, abs_tol=1e-12)
        elif requested is not None and effective is not None:
            matched = str(requested) == str(effective)
        self.entries.append({
            'control': control,
            'requested': requested,
            'effective': effective,
            'applied': bool(applied),
            'matched': matched,
            'note': note,
        })

    def mismatches(self):
        return [item for item in self.entries if item['matched'] is False]

    def to_list(self):
        return list(self.entries)
