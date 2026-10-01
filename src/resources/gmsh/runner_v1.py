#!/usr/bin/env python3
"""FoamMesh Gmsh runner, executed inside the qualified WSL runtime.

Driven by one immutable JSON job. Nothing is looked up here: whatever the
runner needs, the job already carries, so the same job always produces the
same mesh and its digest identifies the result.

Everything below was measured in WP0, and each line marked MEASURED is there
because omitting it produced a plausible-looking wrong mesh rather than an
error:

* MEASURED ``Geometry.OCCTargetUnit`` must be ``M`` before ``importShapes``,
  or a 0.4 m pipe imports as 400 and a metre-scale target size asks for
  hundreds of millions of elements.
* MEASURED the import must be checked for volumes. ``OCCSewFaces`` turns a
  closed solid into a face set, after which ``generate(3)`` yields a surface
  mesh with no cells and raises nothing.
* MEASURED boundary layers require removing the imported solid and rebuilding
  the core volume from the layer's inner surfaces. Extruding without that
  leaves the original volume in place and stacks prisms on top of it, for
  8.75% too much volume with no inverted cells and healthy quality metrics.
* MEASURED layer thicknesses are cumulative and negative grows inward.

Every control records what was requested and what the mesh achieved, because
a runner that reports the request is a runner that certifies work it never did.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import math
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

# Shell classification lives next door because it is pure geometry: no Gmsh,
# so it can be tested without a runtime. Running this file as a script already
# puts its directory on the path; loading it as a module by file does not.
try:
    from shell_topology import ShellTopologyError, resolve_topology
    from field_graph import FieldGraphError, build_graph
    from layer_targets import (
        MODE_ALL_WALLS, eligible_wall_names, normalise_mode)
    from quantities import agreeing, count_text
    import farfield_primitives
except ImportError:  # pragma: no cover - exercised only by the module loader
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from shell_topology import ShellTopologyError, resolve_topology
    from field_graph import FieldGraphError, build_graph
    from layer_targets import (
        MODE_ALL_WALLS, eligible_wall_names, normalise_mode)
    from quantities import agreeing, count_text
    import farfield_primitives

SCHEMA_VERSION = 1
RUNNER_VERSION = 'gmsh-runner-v1'


class ContractFailure(RuntimeError):
    """The job does not satisfy the contract this runner implements."""


def _cavities(count) -> str:
    """``1 sealed cavity`` or ``3 sealed cavities``, said in four places."""
    return count_text(count, 'sealed cavity', 'sealed cavities')


def oriented_key(triple):
    """A triangle's node triple, rotated to its smallest node.

    DP-456. Winding-preserving, so a coincident pair -- the same three nodes
    walked the other way round, which is what a conformal interface is --
    reads as two triangles rather than one.
    """
    first, second, third = (int(value) for value in triple)
    if second < first and second <= third:
        return (second, third, first)
    if third < first and third <= second:
        return (third, first, second)
    return (first, second, third)


def regroup_classified(before: dict, after: dict) -> dict:
    """Classified surface tag -> the imported surface it was cut from.

    ``before`` and ``after`` map surface tags to sets of triangle keys. A
    classified surface goes to whichever import surface owns most of its
    triangles; one that shares no triangle with any is left out, and the
    caller names it as it would have before.
    """
    origin = {}
    index = {}
    for source, triangles in before.items():
        for key in triangles:
            index[key] = source
    # DP-456. Keys are oriented now, so a classified piece whose winding the
    # classifier flipped would match nothing. The unwound key is kept as a
    # second reading for exactly that piece, and only for it: it is the old
    # behaviour, so nothing that used to be placed stops being placed.
    unwound = {}
    for source, triangles in before.items():
        for key in triangles:
            unwound.setdefault(tuple(sorted(key)), set()).add(source)
    for tag, triangles in after.items():
        votes: dict = {}
        for key in triangles:
            source = index.get(key)
            if source is not None:
                votes[source] = votes.get(source, 0) + 1
        if not votes:
            for key in triangles:
                sources = unwound.get(tuple(sorted(key)))
                if sources and len(sources) == 1:
                    source = next(iter(sources))
                    votes[source] = votes.get(source, 0) + 1
        if votes:
            origin[tag] = max(votes, key=lambda item: (votes[item], -item))
    return origin


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


def _cross(first, second):
    return (first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0])


def _dot(first, second):
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _minus(first, second):
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _share_a_corner(one, other, tolerance=1e-12):
    """True when two faces meet at a corner, which is not a crossing."""
    for first in one:
        for second in other:
            if all(abs(first[axis] - second[axis]) <= tolerance
                   for axis in range(3)):
                return True
    return False


def _segment_hits_face(start, finish, face):
    """Moller-Trumbore, with both ends and all three edges held open.

    DP-75. Faces that share an edge touch along it, and faces of one surface
    meet at their edges everywhere; only a segment that passes through the
    inside of a face and strictly between its own ends is a crossing.
    """
    edge_one = _minus(face[1], face[0])
    edge_two = _minus(face[2], face[0])
    direction = _minus(finish, start)
    sideways = _cross(direction, edge_two)
    determinant = _dot(edge_one, sideways)
    if abs(determinant) < 1e-16:
        return False
    inverse = 1.0 / determinant
    offset = _minus(start, face[0])
    along = _dot(offset, sideways) * inverse
    if along < 1e-9 or along > 1.0 - 1e-9:
        return False
    across = _cross(offset, edge_one)
    sideward = _dot(direction, across) * inverse
    if sideward < 1e-9 or along + sideward > 1.0 - 1e-9:
        return False
    depth = _dot(edge_two, across) * inverse
    return 1e-9 < depth < 1.0 - 1e-9


def _faces_cross(one, other):
    """True when either face has an edge through the inside of the other."""
    for index in range(3):
        if _segment_hits_face(one[index], one[(index + 1) % 3], other):
            return True
    for index in range(3):
        if _segment_hits_face(other[index], other[(index + 1) % 3], one):
            return True
    return False


def layer_fold_parameter(base, top):
    """The fraction of the layer at which a facet turns inside out.

    DP-74. Every node of a boundary layer travels the same distance along its
    own normal -- MEASURED on ``centrifugal_impeller``, where the extrusion
    moved all 34026 wall nodes by exactly the 0.00133750785 m asked for, with
    no 1/cos relief where two walls meet. So the facet at fraction ``s`` of
    the requested thickness has vertices ``P + s * (P' - P)``, its normal
    dotted into the base normal is a quadratic in ``s`` that starts at the
    base area squared, and the first positive root is where the facet has
    folded flat. ``None`` where the facet never folds, however far it is
    grown.
    """
    edge = _minus(base[1], base[0])
    other = _minus(base[2], base[0])
    first = _minus(_minus(top[1], base[1]), _minus(top[0], base[0]))
    second = _minus(_minus(top[2], base[2]), _minus(top[0], base[0]))
    normal = _cross(edge, other)
    constant = _dot(normal, normal)
    if constant <= 0.0:
        return None
    linear = (_dot(_cross(edge, second), normal)
              + _dot(_cross(first, other), normal))
    square = _dot(_cross(first, second), normal)
    roots = []
    if abs(square) < 1e-30:
        if abs(linear) > 1e-30:
            roots.append(-constant / linear)
    else:
        discriminant = linear * linear - 4.0 * square * constant
        if discriminant >= 0.0:
            span = math.sqrt(discriminant)
            roots.append((-linear + span) / (2.0 * square))
            roots.append((-linear - span) / (2.0 * square))
    positive = [value for value in roots if value > 1e-12]
    return min(positive) if positive else None


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #

def utc_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Reporter:
    """JSON-lines progress on the shared FOAMMESH_PROGRESS protocol."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        #: DP-446. The mesh step beats from a second thread while the main
        #: one is inside Gmsh, and two writers sharing a sequence number
        #: and a file handle is how a log loses a line.
        self.lock = threading.Lock()

    def emit(self, kind, stage_id, fraction, message='', details=None):
        # DP-446. Held across the whole record, not only the counter: two
        # threads interleaving inside one `print` is a broken JSON line,
        # and a line the console cannot parse is a line the operator does
        # not see.
        with self.lock:
          event = {
              'schema_version': 1, 'sequence': self.sequence, 'kind': kind,
              'stage_id': stage_id, 'fraction': float(fraction),
              'message': str(message)[:4096], 'timestamp': utc_now(),
              'details': details or {},
          }
          line = json.dumps(event, sort_keys=True, separators=(',', ':'))
          if self.path is not None:
              with self.path.open('a', encoding='utf-8') as stream:
                  stream.write(line + '\n')
                  stream.flush()
          print('FOAMMESH_PROGRESS ' + line, flush=True)
          self.sequence += 1


# --------------------------------------------------------------------------- #
# Requested vs effective
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Job loading
# --------------------------------------------------------------------------- #

def load_job(path: Path) -> dict:
    try:
        job = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ContractFailure(f'job could not be read: {error}') from error
    if not isinstance(job, dict):
        raise ContractFailure('job must be a JSON object')
    if job.get('schema_version') != SCHEMA_VERSION:
        raise ContractFailure(
            f'unsupported job schema {job.get("schema_version")!r}; '
            f'this runner implements {SCHEMA_VERSION}')
    if job.get('engine_id') != 'gmsh':
        raise ContractFailure(f'job is not a Gmsh job: {job.get("engine_id")!r}')
    if job.get('units') != 'm':
        raise ContractFailure(
            f'job units must be metres, got {job.get("units")!r}')
    for key in ('geometry', 'intent', 'output'):
        if key not in job:
            raise ContractFailure(f'job is missing {key!r}')
    sources = job['geometry']
    sources = [sources] if isinstance(sources, str) else list(sources)
    if not sources:
        raise ContractFailure('job carries no geometry source')
    for item in sources:
        if not Path(item).is_file():
            raise ContractFailure(f'geometry file is missing: {item}')
    return job


# --------------------------------------------------------------------------- #
# Meshing
# --------------------------------------------------------------------------- #

SURFACE_ALGORITHM = {
    'meshadapt': 1, 'automatic': 2, 'delaunay': 5,
    'frontal_delaunay': 6, 'frontal_delaunay_quads': 8,
    'packing_parallelograms': 9, 'quasi_structured_quad': 11,
}
VOLUME_ALGORITHM = {'delaunay': 1, 'frontal': 4, 'mmg3d': 7, 'hxt': 10}
QUALITY_MEASURE = {'sicn': 0, 'sige': 1, 'gamma': 2, 'disto': 3}
#: Plan 31 CP-08 item 3. The ``Mesh.RecombinationAlgorithm`` values this
#: product has qualified, and the reason the set is closed. MEASURED on
#: duct.step at a 0.04 m target: 0 gave 306 quadrangles and left 70 triangles,
#: 1 gave 356 and left none, 2 gave 460, 3 gave 408 -- four values, four
#: different meshes. 4 read back as 4 and then ``generate(3)`` produced no
#: elements at all, surface and volume both empty, without raising; 9 was
#: clamped back to 0 in silence. An option that reads back is not a mesh.
TESTED_RECOMBINERS = frozenset({0, 1, 2, 3})
#: Plan 31 FC-B. What Gmsh calls each ``Mesh.Algorithm`` value in its own log.
#: Taken from the log of a real run, not from the documentation: this table
#: exists to compare against the strings Gmsh actually prints.
ALGORITHM_NAMES = {
    1: 'MeshAdapt',
    2: 'Automatic',
    5: 'Delaunay',
    6: 'Frontal-Delaunay',
    8: 'Frontal-Delaunay for Quads',
    9: 'Packing of Parallelograms',
    11: 'Quasi-structured Quad',
}
#: Plan 31 FC-B. ``Mesh.MaxRetries`` when algorithm fallback is on. Gmsh's own
#: default; named here so the runner and the register cannot disagree.
ALGORITHM_RETRIES = 10
#: Plan 31 FC-B. Surface algorithms that mesh THROUGH other algorithms as a
#: matter of design, so the algorithm Gmsh logs per surface is not evidence of
#: a fallback. Quasi-structured Quad builds an initial mesh with whatever
#: algorithm suits the patch -- Transfinite, Packing of Parallelograms,
#: MeshAdapt were all logged during the FC-B sweep -- and then remeshes it into
#: quadrilaterals. Measured: every one of those runs wrote an all-quad surface
#: (6008/0, 4596/0, 8216/0 quads/triangles), which is exactly what was asked
#: for, so calling it a fallback would have been a false alarm on every run.
PIPELINE_ALGORITHMS = frozenset({11})
#: DP-505. What Gmsh logs for a surface an extrusion made and meshed: the
#: sides and tops of a boundary layer. It is not an algorithm anyone chooses.
EXTRUDED_ALGORITHM = 'Extruded'
#: ``Mesh.SubdivisionAlgorithm`` values that change the element family.
#: MEASURED: 1 ("all quadrangles") left 682 triangles and 1044 tetrahedra --
#: the numbers no subdivision at all gives -- and 3 (barycentric) split each
#: tetrahedron into four without changing the family. Only 0 and 2 do what
#: their names say.
TESTED_SUBDIVISIONS = frozenset({0, 2})
#: Plan 31 FC-C. ``Mesh.SubdivisionAlgorithm`` 3, barycentric. Kept out of
#: :data:`TESTED_SUBDIVISIONS` deliberately: it is not a cell shape, and the
#: cell-shape control must go on refusing it. It reaches Gmsh through
#: ``gmsh/globalSizing/barycentricRefinement`` instead, where what it MEASURED
#: as -- four tetrahedra per tetrahedron, the boundary untouched -- is what it
#: is offered as.
BARYCENTRIC_SUBDIVISION = 3
#: Plan 30 WP12. Which Gmsh field folds every size source into the one
#: background field. Mirrors ``foammesh.core.gmsh.sizing.FIELD_COMBINERS``;
#: the runner cannot import from the application, so it is restated.
FIELD_COMBINERS = {'min': 'Min', 'max': 'Max'}
#: ``Mesh.Format`` for Gmsh's own SU2 writer, and for "pick the writer from the
#: file extension". Mirrors the same two names in
#: ``foammesh.core.gmsh.plan_derivation``; the runner cannot import from the
#: application, so the pair is restated rather than shared.
SU2_FORMAT_CODE = 42
AUTO_FORMAT_CODE = 10
#: What ``getElementQualities`` calls each measure. Measured against Gmsh
#: 4.15.2: ``gamma`` is accepted bare, ``sicn`` and ``sige`` need their
#: ``min…`` spellings, and there is no name for ``disto`` at all. The runner
#: used to translate only ``sicn``, so selecting ``sige`` or ``disto`` reached
#: the API as its configuration spelling and raised ``Unknown quality name``,
#: taking the whole meshing run with it.
QUALITY_QUERY_NAME = {'sicn': 'minSICN', 'sige': 'minSIGE', 'gamma': 'gamma'}
#: ``disto`` is a legitimate ``Mesh.QualityType`` for the optimiser -- 0..3 all
#: set cleanly -- but the gate cannot query it, so it is judged against this
#: instead and the substitution is recorded rather than performed silently.
QUALITY_SUBSTITUTE = 'sicn'
#: Offending elements recorded per metric. Enough to browse, bounded so a mesh
#: where every element is bad cannot produce a gigabyte of manifest.
OFFENDER_CAP = 1000

#: DP-76. Below this a cell resting on the layer top is not a poor cell, it
#: is not a cell. MEASURED on ``drone_quadcopter``: with the fitted layer the
#: worst tetrahedron is 3.363e-07 gamma, and with the layer off, on the same
#: tessellation, it is 3.504e-04. Three orders of magnitude separate the two
#: populations and this floor sits between them, so it catches the layer's
#: pancakes without touching the slivers the geometry brings of its own.
LAYER_LANDING_FLOOR = 1e-5

#: DP-121. The reason ``protected_surfaces`` gives for a layer surface, named
#: once so the merge can tell a layer apart from a periodic pair without
#: matching prose.
LAYER_PROTECTION = 'the boundary layer'


class _Classification:
    """Which surface-classification settings a triangulation survived."""

    __slots__ = ('angle', 'reparametrize')

    def __init__(self, angle, reparametrize):
        self.angle = float(angle)
        self.reparametrize = bool(reparametrize)


class GmshRun:
    def __init__(self, job: dict, reporter: Reporter):
        self.job = job
        self.intent = job['intent']
        #: FC-E. The dimension this job asked for, read before anything is
        #: imported because it changes what an import *means*: a section is a
        #: face and has no volume, so the refusal that protects a
        #: three-dimensional run has to ask which run this is rather than fire
        #: on both. `planar` covers two_d and axisymmetric alike -- both mesh
        #: a section here and are extruded one cell thick by the publisher,
        #: and they differ only in how that one cell is swept.
        self.dimensionality = dict(self.intent.get('dimensionality') or {})
        self.planar = bool(self.dimensionality.get('planar'))
        self.generate_dimension = int(
            self.dimensionality.get('generateDimension') or 3)
        #: DP-133. Where to leave the surface pass of a three-dimensional
        #: run, or `''` for a job that did not ask for one. Absent in a job
        #: written before this existed, which is why it is read with a
        #: default rather than indexed.
        self.surface_output = str(job.get('surfaceOutput') or '')
        #: FC-E. Curves bounding the section, for a planar job. The
        #: three-dimensional equivalent is `boundary_surfaces`.
        self.boundary_curves: list[int] = []
        self.reporter = reporter
        self.ledger = Ledger()
        self.statistics: dict = {}
        self.warnings: list[str] = []
        self.gmsh = None
        #: Scope token -> Gmsh surface tags, resolved after import.
        self.scope_surfaces: dict[str, list[int]] = {}
        #: Scope token -> Gmsh volume tags, likewise.
        self.scope_volumes: dict[str, list[int]] = {}
        self.unresolved_scopes: list[str] = []
        #: Plan 28 WP7. Tessellated import only: the surface tags in import
        #: order before classification (what `surfaceNames` and
        #: `scopeSurfaces` are keyed on), and classified tag -> origin tag.
        self.import_surface_tags: list[int] = []
        self.surface_origin: dict[int, int] = {}
        #: Plan 30 WP12. Surface tag -> patch name for faces this runner
        #: created, which the prepared geometry cannot have named.
        self.generated_names: dict[int, str] = {}
        #: Plan 30 WP12. Whether the geometry arrived as triangles. A discrete
        #: surface has no CAD corners and no OCC solid, so transfinite
        #: surfaces and boolean farfield boxes are refused with a reason
        #: rather than attempted and half-applied.
        self.tessellated = False
        #: WP-01 F-35, extended by C31-04. One receipt per entry of
        #: ``job['geometry']``, in the order the job listed them, published as
        #: ``statistics['import']['sources']`` and folded verbatim into the
        #: run manifest. Each receipt is::
        #:
        #:     {'schema_version': int,  # `entitySchemaVersion` of the job
        #:      'path':     str,   # the file as the runner opened it
        #:      'source_id': str,  # the prepared source this file is
        #:      'import_stage': str,  # 'cad' or 'tessellated'
        #:      'surfaces': int,   # model surfaces this file added
        #:      'volumes':  int,   # model volumes this file added
        #:      'shells':   int,   # closed shells it contributed: solids for
        #:                         # CAD, one per `solid` block for a
        #:                         # tessellated file
        #:      'entities': [{'entity_id': str, 'kind': str, 'dim': int,
        #:                    'tags': [int]}],  # identity -> what Gmsh made
        #:      'mapping_confidence': float,   # what the prepared revision
        #:                                     # claimed for this source
        #:      'mapping_status': str}         # 'exact' | 'partial' |
        #:                                     # 'unmapped'
        #:
        #: Without the counts a job listing several sources could not be
        #: audited: the manifest recorded one total and no way to see which
        #: file contributed nothing. Without the ``entities`` mapping a scope
        #: could not be resolved to the right file's entities at all -- the
        #: indices two single-body CAD sources carry are both zero.
        self.import_receipts: list[dict] = []
        #: C31-04. Entity ID -> ``{'kind', 'dim', 'tags'}``: the identity the
        #: prepared revision authored against, against the tags Gmsh actually
        #: made for it *now*. Retargeted by :meth:`retarget_entities` after
        #: every topology change, so a scope resolves through the change
        #: rather than through the numbering the import happened to have.
        self.entity_tags: dict[str, dict] = {}
        #: C31-04. Gmsh tag -> the prepared name of the entity that tag now
        #: carries, resolved through the identity map. Empty for a job that
        #: carries no entity names, which leaves the version-1 lookup by tag.
        self.entity_surface_names: dict[int, str] = {}
        self.entity_volume_names: dict[int, str] = {}
        #: DP-399. Prepared surface names that no surface in the model
        #: carries, filled by `describe_surfaces` once the model has stopped
        #: changing. A control scoped to one of these reaches nothing.
        self.names_without_surface: set[str] = set()
        #: How many faces the duplicate fusion took away, which is the one
        #: cause of the above this runner can name.
        self.duplicate_faces_fused = 0
        #: DP-410. ``{dropped tag: surviving tag}`` for every coincident face
        #: the fusion consumed, matched by centre of mass and area. The pair
        #: is one face now and the identity that pointed at the dropped tag
        #: has to point at the one that replaced it.
        self.duplicate_face_successors: dict[int, int] = {}
        self.duplicate_volume_successors: dict[int, int] = {}
        #: DP-410. ``{tag: [name, ...]}`` -- the further prepared names a
        #: fused surface carries beyond the one it publishes under. A face
        #: can belong to one patch, so the extras name the same faces rather
        #: than naming nothing.
        self.merged_surface_names: dict[int, list[str]] = {}
        #: C31-04. One record per topology change that moved entities:
        #: ``{'stage', 'entities': {entity_id: {'from': [...], 'to': [...]}}}``.
        #: Published as ``statistics['import']['topologyChanges']``.
        self.topology_changes: list[dict] = []
        #: R118. A surface that grew no boundary layer is deleted and rebuilt
        #: from the extrusion's inner rim, so this maps the original tag onto
        #: the faces that took its place and must inherit its patch name.
        self.layer_replacements: dict[int, list[int]] = {}
        #: R118. The same, one dimension up. The volume a layer is carved
        #: out of is deleted and replaced by the layer volumes and a rebuilt
        #: core, and all of them are still the region the user named. Maps
        #: the original tag onto the tags that took its place.
        self.volume_replacements: dict[int, list[int]] = {}
        #: Size fields awaiting combination into one background field. Size
        #: fields and per-volume sizes must share it, or whichever was set
        #: last would silently replace the other.
        self.background_fields: list[int] = []
        #: DP-504. ``(row name, local size)`` for each background source, so
        #: the combination can tell whether anything asked for a size the
        #: boundary could outvote. ``None`` is a size the runner cannot read
        #: off the row (a MathEval expression).
        self.background_sizes: list[tuple[str, float | None]] = []
        #: Every curve a transfinite control put a node count on, and what it
        #: asked for. Read back after meshing, because Gmsh answers a
        #: structured request it cannot honour by meshing unstructured
        #: without a word (see :meth:`structured_surface_refusal`).
        self.transfinite_curves: dict[int, dict] = {}
        #: Surfaces and volumes asked to be structured, by tag.
        self.transfinite_surfaces: dict[int, dict] = {}
        self.transfinite_volumes: dict[int, dict] = {}
        #: Plan 31 FC-C. The volumes ``setTransfiniteAutomatic`` was pointed
        #: at, kept so the finished mesh can be read back per volume. The call
        #: succeeds per volume and says nothing about which ones it took, so
        #: the only honest answer comes from the element families afterwards.
        self.automatic_structuring: dict | None = None
        #: Plan 31 FC-C. Whether barycentric refinement actually reached the
        #: option, so the finished mesh can be counted as the answer to it.
        self.barycentric_refinement = False
        #: What the boundary-layer extrusion made, kept so the finished mesh
        #: can be read back against it: the wall surfaces the stacks stand on,
        #: the volume(s) the layer occupies, and the sides of the stack --
        #: which is where the quad option shows up (CP-08 item 6).
        #: The pairs ``setPeriodic`` accepted, kept so the finished mesh can
        #: be read back against them: a pair is only a coupling once the two
        #: surfaces carry corresponding nodes and faces (CP-08 item 5).
        self.periodic_applied: list[dict] = []
        self.layer_bases: list[int] = []
        self.layer_volumes: list[int] = []
        self.layer_laterals: list[int] = []
        #: DP-74. Each wall surface paired with the inner surface its stack
        #: reaches, which is what the fit check reads once both are meshed.
        self.layer_columns: list[tuple[int, int]] = []
        #: DP-75. True when this run meshed the imported facets instead of a
        #: parametrised remesh of them. Set by :meth:`import_tessellated`.
        self.kept_tessellation = False
        #: DP-55. Surfaces that bound a void inside the volume being meshed.
        #: Their triangles are wound out of the void, which points into the
        #: fluid - the opposite of an outer shell - so a layer grown with the
        #: outer shell's sign lands in the hole instead of in the flow.
        self.void_surfaces: set[int] = set()
        #: DP-55. Surface tag -> +1 when its shell is wound outward, -1 when
        #: the shell is inverted, read off the signed enclosed volume the
        #: shell topology already computes.
        self.surface_winding: dict[int, int] = {}
        # DP-457. Which closed body each surface bounds, kept so that a
        # coincidence can be asked whether it spans two of them.
        self.surface_shell: dict[int, str] = {}

    # -- option helpers ---------------------------------------------------- #

    def set_number(self, name, value, *, control=None):
        self.gmsh.option.setNumber(name, float(value))
        effective = self.gmsh.option.getNumber(name)
        self.ledger.record(control or name, value, effective)
        return effective

    def set_string(self, name, value, *, control=None):
        self.gmsh.option.setString(name, str(value))
        effective = self.gmsh.option.getString(name)
        self.ledger.record(control or name, value, effective)
        return effective

    # -- stages ------------------------------------------------------------ #

    def initialise(self):
        import gmsh

        self.gmsh = gmsh
        gmsh.initialize()
        gmsh.option.setNumber('General.Terminal', 0)
        # Plan 31 FC-B. Gmsh names the algorithm it meshed each surface with,
        # and that line is the only place the *actual* algorithm appears: when
        # Mesh.AlgorithmSwitchOnFailure retries a surface with another one,
        # the option still reads back as the algorithm that failed. So the log
        # is captured and `measure_algorithms` reads it.
        try:
            gmsh.logger.start()
            self.logging = True
        except Exception:                                    # noqa: BLE001
            self.logging = False
        parallel = self.intent.get('parallel') or {}
        threads = int(parallel.get('threads', 1) or 1)
        self.set_number('General.NumThreads', threads,
                        control='gmsh/parallel/threads')
        # Plan 30 WP12. The 3D limit carries the *effective* count, not the
        # requested one. Delaunay and Frontal ignore this option entirely, so
        # writing 16 into it recorded a ledger line saying sixteen threads
        # meshed the volume when one did. The derivation already knows which
        # algorithm can use them; the ledger now says the same thing.
        volume_threads = int(parallel.get('effectiveVolumeThreads', threads)
                             or threads)
        gmsh.option.setNumber('Mesh.MaxNumThreads3D', float(volume_threads))
        self.ledger.record(
            'gmsh/parallel/threads.3d', volume_threads,
            gmsh.option.getNumber('Mesh.MaxNumThreads3D'),
            note='' if volume_threads == threads else (
                f'{threads} requested; the chosen volume algorithm is '
                'single-threaded, so the request applies to surface meshing '
                'only'))
        self.set_number('Mesh.MaxNumThreads2D', threads,
                        control='gmsh/parallel/threads.2d')
        # Plan 31 FC-B measured Gmsh's reproducible-mesh option here and
        # took it back out again. It was meant to make two runs of the
        # same job produce the same mesh, so that the manifest's
        # mesh_sha256 means something. By
        # differential, on the real Gmsh: at one thread the elbow wrote the
        # same bytes six times, three runs with the option and three without,
        # so there is nothing here for it to fix; at four threads three runs
        # WITH the option came back at 8901, 8901 and 8914 cells, so where
        # the nondeterminism actually lives it does not fix it. Threaded runs
        # differ in how many cells they contain, not in what order they are
        # written, and no reordering option can equalise that. See
        # `plans/evidence/plan31/fcb-algorithms/reproducible.json` and
        # `reproducible-one-thread.json`. Setting it would have left a comment
        # in this runner promising a guarantee the runner does not have.

    #: DP-60. Suffixes whose OCCT reader Gmsh constructs only after it has
    #: applied ``Geometry.OCCTargetUnit``, so at that moment there is nothing
    #: for the unit to be applied to. STEP is not one of them, which is how
    #: this was isolated.
    UNIT_PRIME_SUFFIXES = ('.iges', '.igs')

    def _prime_occ_target_unit(self, sources):
        """Read one empty file, so the next one can carry a unit.

        DP-60. MEASURED on Gmsh 4.15.2 in the ``wsl-openfoam13-gmsh``
        runtime, one scenario per fresh process:

        * with ``Geometry.OCCTargetUnit`` set to ``M``, every IGES import
          raises ``Could not set OpenCASCADE target unit 'M'`` and imports
          nothing -- ``pipe.iges``, ``pipe_mm.iges``, ``pipe_inch.iges`` and
          ``elbow.iges`` alike -- and a second attempt in the same process
          fails identically, so it is not a warm-up;
        * the same files import with the option unset, at the file's own
          unit: ``pipe.iges`` spans 100 x 100 x 600 where the STEP and the
          BREP of that same pipe both give 0.1 x 0.1 x 0.6;
        * a STEP import first, in the same process, makes the IGES import
          succeed and land at 0.1 x 0.1 x 0.6;
        * and so does a **zero-byte file**, which holds no geometry and is
          never parsed. Named ``.step`` it primes, but OCCT prints
          ``**** ERR StepFile : Undefined Parsing ... ****`` to the run's
          stderr and then raises. Named ``.iges`` it primes without raising,
          and the only thing it adds to the log is a second
          ``Total number of loaded entities 0.`` beside the one the real
          IGES import prints anyway. The IGES name is the one that ships:
          the STEP one puts an error in front of a user whose run is fine.

        The unit lives in an OCCT static that exists only once a STEP or IGES
        reader has been constructed in the process. Gmsh's STEP path
        constructs its reader before applying the unit; its IGES path does
        not. Priming on an empty file costs one open and no parse, which is
        why it is preferred over re-reading the real geometry -- that works
        too, and doubles the import of a large assembly.

        Only paid for when a source needs it, and never allowed to fail a
        run.
        """
        if not any(Path(str(item)).suffix.lower() in self.UNIT_PRIME_SUFFIXES
                   for item in sources):
            return
        gmsh = self.gmsh
        current = gmsh.model.getCurrent()
        gmsh.model.add('__foammesh_occ_unit_prime__')
        try:
            with tempfile.TemporaryDirectory() as directory:
                empty = Path(directory) / 'prime.iges'
                empty.touch()
                try:
                    gmsh.model.occ.importShapes(str(empty))
                except Exception:
                    # MEASURED not to happen for an empty IGES, and caught
                    # anyway: constructing the reader is the whole effect,
                    # and there is nothing in the file to read.
                    pass
        finally:
            gmsh.model.remove()
            gmsh.model.setCurrent(current)

    def set_import_tolerance(self, healing, tessellated):
        """Apply the healing page's import tolerance on the route it means.

        DP-401. `Geometry.Tolerance` configures Gmsh's OCC importer, and a
        tessellated import runs no OCC importer. What the value reaches
        instead is `removeDuplicateNodes`, whose welding distance it is, and
        through the surface that welding leaves, the parametrisation
        `createGeometry` solves.

        MEASURED on `naca0012` -- two STLs, Gmsh 4.15.2, 24,000 edges every
        one of which is shared by exactly two triangles before Gmsh touches
        it. At the page default of 1e-6 the weld takes 8,004 nodes to 7,982
        and leaves 15 edges shared by more than two; the run reads that as a
        non-manifold surface, turns reparametrisation off on that reading,
        and `createGeometry` then fails with `The linear system of equations
        did not converge (PETSc reason : -11)`. At Gmsh's own 1e-8 nothing is
        welded away, nothing is non-manifold, and the configured 40 degrees
        classifies into 9 surfaces. A NACA0012 is the ordinary case for this
        and not a pathological one: the two sides of a sharp trailing edge
        run within a micron of each other over the last of the chord.

        So on a tessellated import the tolerance is left where Gmsh has it,
        and the run says so -- as it already does for `healShapes`, which is
        the same sentence about the same absent kernel.
        """
        value = float(healing.get('importTolerance', 1e-6) or 1e-6)
        if not tessellated:
            self.set_number('Geometry.Tolerance', value,
                            control='gmsh/healing/importTolerance')
            return
        default = float(self.gmsh.option.getNumber('Geometry.Tolerance'))
        self.ledger.record(
            'gmsh/healing/importTolerance', value, default, applied=False,
            note='tessellated import: this tolerance configures the OCC '
                 'importer, and there is no CAD here for it to import')
        if value > default:
            self.warnings.append(
                f'the import tolerance of {value:g} m was not applied: it '
                f'configures the CAD importer and this import is '
                f'tessellated. On a surface import the same number is the '
                f'distance within which two nodes are welded into one, so a '
                f'sharp edge whose two sides run nearer than that is sewn '
                f'shut by it — which makes the surface non-manifold where it '
                f'was not, and can leave its patches with no parametrisation '
                f'to mesh on. Gmsh welds at {default:g} m here instead.')

    #: DP-629 (field audit 0924 D-SH-02). Options that configure Gmsh's CAD
    #: importer, ``occ.importShapes``. A tessellated source is read by
    #: ``gmsh.merge`` and never reaches it, so on that route these were set,
    #: read back and logged as honoured while changing nothing.
    OCC_IMPORT_OPTIONS = (
        ('Geometry.OCCSewFaces', 'sewFaces', False),
        ('Geometry.OCCFixDegenerated', 'fixDegenerated', False),
        # Rebuilds a solid from a closed shell. Measured: with sewing on and
        # this off, the import has zero volumes and meshes to a bare surface.
        ('Geometry.OCCMakeSolids', 'makeSolids', False),
        ('Geometry.OCCFixSmallEdges', 'fixSmallEdges', False),
        ('Geometry.OCCFixSmallFaces', 'fixSmallFaces', False),
        ('Geometry.OCCAutoFix', 'autoFix', True),
        ('Geometry.OCCUnionUnify', 'unionUnify', True),
        ('Geometry.OCCImportLabels', 'importLabels', True),
        ('Geometry.OCCParallel', 'occParallel', False),
    )

    def set_occ_import_options(self, healing, tessellated):
        """Configure the CAD importer, or say it is not being used."""
        skipped = []
        for option, key, default in self.OCC_IMPORT_OPTIONS:
            requested = bool(healing.get(key, default))
            control = f'gmsh/healing/{key}'
            if not tessellated:
                self.set_number(option, int(requested), control=control)
                continue
            self.ledger.record(
                control, requested, None, applied=False,
                note='tessellated import: this configures the CAD importer, '
                     'and a surface is read without it')
            if requested != default:
                skipped.append(key)
        if skipped:
            self.warnings.append(
                'these import settings were not applied because they '
                'configure the CAD importer and this import is a surface '
                f'(STL/OBJ/PLY): {", ".join(skipped)}. The classification '
                'angle and the duplicate-node weld are what act on a surface.')
        self.set_import_scaling(healing, tessellated)

    def set_import_scaling(self, healing, tessellated):
        """DP-631 (field audit 0924 D-SH-01). Geometry.OCCScaling is pinned.

        The store has already put the part in metres and the importer is told
        to read in metres, so a second factor here made the Gmsh mesh a
        different size from the viewport, the prepared geometry and every
        metre-valued size on the Gmsh pages. MEASURED (Gmsh 4.15.2): at 1000
        elbow.step went from 0.3247 m to 324.7 m, and elbow.stl stayed at
        0.2999 m because a surface is not read by the CAD importer at all.
        The unit is chosen once, at import; this option stays at 1.
        """
        requested = float(healing.get('importScaling', 1.0) or 1.0)
        if not tessellated:
            self.set_number('Geometry.OCCScaling', 1.0,
                            control='gmsh/healing/importScaling')
        if requested == 1.0:
            if tessellated:
                self.ledger.record(
                    'gmsh/healing/importScaling', requested, None,
                    applied=False,
                    note='tessellated import: read without the CAD importer')
            return
        self.ledger.record(
            'gmsh/healing/importScaling', requested, 1.0, applied=False,
            note='pinned at 1: the unit is set at import, and a second '
                 'factor would size this mesh apart from everything else')
        self.warnings.append(
            f'the import scaling of {requested:g} was not applied: the '
            'geometry is already in metres from its import unit, and a '
            'second factor would put this mesh at a different size from the '
            'viewport and every size on these pages. Re-import with the '
            'right unit instead.')

    def import_geometry(self):
        gmsh = self.gmsh
        healing = self.intent.get('healing') or {}

        sources = self.job['geometry']
        sources = [sources] if isinstance(sources, str) else list(sources)
        # Row `import-formats-unexposed`, and it comes first on purpose: every
        # option below configures an importer, and there is no point setting up
        # an import of a file that is not going to be imported. Gmsh's own
        # answer for these is `Unknown file type`, which tells a user nothing
        # about why a format it can demonstrably read is not accepted here. See
        # :data:`UNSUPPORTED_GEOMETRY` for what was measured about each.
        for item in sources:
            reason = self.UNSUPPORTED_GEOMETRY.get(
                Path(str(item)).suffix.lower())
            if reason:
                raise MeshFailure(
                    f'{Path(str(item)).name} cannot be used as geometry: '
                    f'{reason}')

        # MEASURED: without this, a 0.4 m pipe imports as 400.
        # DP-60: and for an IGES, without the prime, it does not import at all.
        # DP-401. Which route this import takes has to be settled before the
        # importer is configured, because one of the options below must not
        # be set on the tessellated route at all.
        tessellated = [item for item in sources
                       if str(item).lower().endswith(('.stl', '.obj', '.ply'))]

        self._prime_occ_target_unit(sources)
        self.set_string('Geometry.OCCTargetUnit', 'M',
                        control='geometry.unit')
        self.set_import_tolerance(healing, bool(tessellated))
        # DP-629. The importer switches, the scaling beside them (DP-631).
        self.set_occ_import_options(healing, bool(tessellated))

        # -- Plan 31: the rest of the Geometry.* family ------------------- #
        # Kept as its own block, in one place, so this file stays mergeable
        # with the Mesh.* work happening beside it. Every default below is
        # Gmsh 4.15.2's own, probed 2026-09-06, so setting them explicitly
        # changes nothing for a case that never asked.
        #
        # All of these have to be established before importShapes: they
        # configure the importer, not the model.
        # The OCC importer switches moved to set_occ_import_options (DP-629).
        # Read by the farfield cut and by removeAllDuplicates, both of which
        # are booleans, so it is set before either runs.
        self.set_number('Geometry.ToleranceBoolean',
                        float(healing.get('booleanTolerance', 0.0) or 0.0),
                        control='gmsh/healing/booleanTolerance')
        # -- end Geometry.* block ----------------------------------------- #

        self.reporter.emit('progress', 'import', 0.05, 'importing geometry')
        if tessellated:
            if len(tessellated) != len(sources):
                raise MeshFailure(
                    'CAD and tessellated geometry cannot be mixed in one job; '
                    'convert the CAD or supply all inputs as surfaces.')
            self.tessellated = True
            if healing.get('healShapes'):
                # `occ.healShapes` repairs OCC shapes, and a merged
                # triangulation has none: Gmsh meshes it as a discrete
                # geometry. Saying so beats a silent no-op on the one input
                # where the user is most likely to reach for repair.
                self.warnings.append(
                    'the explicit healing pass was requested but this import '
                    'is tessellated, and OCC healing needs CAD. The patch '
                    'classification angle is what repairs a surface import.')
                self.ledger.record(
                    'gmsh/healing/healShapes', True, False, applied=False,
                    note='tessellated import: there is no OCC shape to heal')
            self.import_tessellated(sources, healing)
        else:
            for index, item in enumerate(sources):
                before_surfaces = {tag for _dim, tag in gmsh.model.getEntities(2)}
                before_volumes = {tag for _dim, tag in gmsh.model.getEntities(3)}
                before = self._entity_counts()
                gmsh.model.occ.importShapes(str(item))
                gmsh.model.occ.synchronize()
                after = self._entity_counts()
                # C31-04. Which tags *this* file brought, not how many. The
                # prepared revision numbers a source's faces from zero within
                # the source, and Gmsh numbers them from wherever the previous
                # file stopped, so the join has to be made here or not at all.
                added_surfaces = sorted(
                    tag for _dim, tag in gmsh.model.getEntities(2)
                    if tag not in before_surfaces)
                added_volumes = sorted(
                    tag for _dim, tag in gmsh.model.getEntities(3)
                    if tag not in before_volumes)
                # A CAD solid is one closed shell, so the volumes this file
                # added are the shells it contributed.
                self.import_receipts.append(self._source_receipt(
                    index, item, stage='cad',
                    surfaces=after[2] - before[2],
                    volumes=after[3] - before[3],
                    shells=after[3] - before[3],
                    added={'surface': added_surfaces,
                           'volume': added_volumes}))
            gmsh.model.occ.synchronize()
            # Repair the imported bodies before the assembly is made
            # conformal: a sliver face that healing would have removed is a
            # face removeAllDuplicates would otherwise try to match.
            self.heal_shapes(healing)
            # Two solids that share a face import as two copies of that face,
            # and the volume mesher then fails with "Could not recover boundary
            # mesh". Fusing the duplicates makes the interface conformal, which
            # is what a multi-region assembly needs.
            if healing.get('removeDuplicateFaces', True):
                signatures = self._entity_signatures((2, 3))
                before = len(gmsh.model.getEntities(2))
                gmsh.model.occ.removeAllDuplicates()
                gmsh.model.occ.synchronize()
                after = len(gmsh.model.getEntities(2))
                self._follow_duplicate_fusion(signatures)
                if after != before:
                    self.duplicate_faces_fused = before - after
                    self.ledger.record(
                        'gmsh/healing/removeDuplicateFaces', True, True,
                        note=f'{count_text(before - after, "duplicate face")}'
                             ' fused')
            # DP-399. Both steps above can take a face away, and neither said
            # so to the identity map. MEASURED on two boxes sharing a face:
            # the fusion drops one tag of each duplicate pair and renumbers
            # nothing, so the identity whose face was the dropped one still
            # points at a tag the model no longer has -- and a prepared name
            # is then carried forward onto a dead tag, where the census, the
            # receipt and the boundary-layer scope all read over the surfaces
            # that remain, agree with each other, and are wrong. This is the
            # first point at which both are done, so it is where they are
            # followed. It costs nothing when nothing moved.
            self.revalidate_scopes('healing')

        volumes = gmsh.model.getEntities(3)
        surfaces = gmsh.model.getEntities(2)
        # MEASURED: a sewn import silently has no volumes and meshes to a
        # surface. Refusing here is the difference between an error and a
        # polyMesh with no cells.
        if self.planar:
            # FC-E. The refusal below is correct for a three-dimensional job
            # and wrong for this one, so the run asks which job it is instead
            # of dropping the check. A section is a face: no volumes is what
            # a correct import looks like here, and volumes are the error.
            mode = self.dimensionality.get('mode') or 'two_d'
            if volumes:
                raise MeshFailure(
                    f'the job asked for a {mode} mesh and the import '
                    f'produced {count_text(len(volumes), "volume")}. This '
                    'route meshes a planar section and extrudes it into '
                    'exactly one cell of '
                    'thickness, so a solid cannot be its input: supply the '
                    'section face, or mesh in three dimensions.')
            if not surfaces:
                raise MeshFailure(
                    f'the job asked for a {mode} mesh and the import produced '
                    'no surfaces, so there is no section to mesh.')
        elif not volumes:
            detail = ''
            if healing.get('sewFaces') and not healing.get('makeSolids'):
                detail = (' Face sewing is enabled and solid reconstruction is '
                          'not: sewing converts closed solids into face sets. '
                          'Turn sewing off, or turn on "rebuild solids" to '
                          'recover a solid from the sewn shell.')
            elif not healing.get('makeSolids'):
                detail = (' If this CAD is a surface model rather than a '
                          'solid, turn on "rebuild solids".')
            raise MeshFailure(
                f'the import produced {len(surfaces)} surfaces and no volumes, '
                f'so there is nothing to mesh.{detail}')

        if self.build_farfield():
            # The cut replaced every entity: the fluid domain is one new
            # volume and the body faces carry new tags. Re-read before
            # anything is measured or named off them.
            volumes = gmsh.model.getEntities(3)
            surfaces = gmsh.model.getEntities(2)

        bounds = gmsh.model.getBoundingBox(-1, -1)
        diagonal = math.dist(bounds[0:3], bounds[3:6])
        self._refresh_receipt_entities()
        self.statistics['import'] = {
            'volumes': len(volumes), 'surfaces': len(surfaces),
            'boundingBox': list(bounds), 'diagonal': diagonal,
            # WP-01 F-35, extended by C31-04. See `self.import_receipts` for
            # the schema.
            'entitySchemaVersion': int(
                self.job.get('entitySchemaVersion') or 1),
            'sources': list(self.import_receipts),
            'topologyChanges': list(self.topology_changes),
        }
        for receipt in self.import_receipts:
            if not receipt['surfaces'] and not receipt['volumes']:
                self.warnings.append(
                    f'{receipt["path"]} contributed no geometry to the model')
        self.resolve_scopes(surfaces, volumes)
        census = self.describe_surfaces([tag for _dim, tag in surfaces])
        self.statistics['import'].update(census['statistics'])
        self.reporter.emit(
            'progress', 'import', 0.15,
            f'imported {count_text(len(volumes), "volume")}, '
            f'{count_text(len(surfaces), "surface")}{census["message"]}',
            {'diagonal_m': round(diagonal, 6), **census['details']})

    def surface_patch_name(self, tag):
        """``(patch name, whether the prepared geometry named it)``.

        Plan 28 WP7: a classified piece publishes under the imported solid it
        was cut from, so the lookup goes through ``surface_origin`` first.
        """
        source = self.surface_origin.get(tag, tag)
        # C31-04. The identity map first: it names this exact tag, and it is
        # the only lookup that stays right once a second source has shifted
        # every tag away from the index the prepared revision recorded.
        named = self.entity_surface_names.get(int(tag))
        if not named:
            named = (self.job.get('surfaceNames') or {}).get(str(source))
        if not named:
            # Plan 30 WP12. A face the runner made rather than imported -- the
            # six sides of a generated farfield box -- has no prepared name to
            # look up, and `face_41` would publish as a wall.
            named = self.generated_names.get(tag)
        return (str(named) if named else f'face_{source}'), bool(named)

    def declared_surface_names(self) -> set[str]:
        """Every patch name the prepared geometry declared, alive or not.

        The counterpart to :meth:`surface_patch_name`, which asks a surface
        what it is called. DP-399: a name with no surface is not a surface
        with no name, and the census only ever counted the second kind.
        """
        names = (self.job.get('entityNames') or {}).get('surfaces') or {}
        if not names:
            # The version-1 form, keyed by a source-local tag. Its keys
            # collide across sources; its values do not, and the values are
            # the whole of the question here.
            names = self.job.get('surfaceNames') or {}
        return {str(label) for label in names.values() if label}

    @staticmethod
    def _tag_ranges(tags):
        """``[1, 2, 3, 7]`` as ``1-3, 7``: the numbering, short enough to read."""
        spans, ordered = [], sorted(tags)
        for tag in ordered:
            if spans and tag == spans[-1][1] + 1:
                spans[-1][1] = tag
            else:
                spans.append([tag, tag])
        return ', '.join(str(low) if low == high else f'{low}-{high}'
                         for low, high in spans)

    def describe_surfaces(self, tags):
        """R64. Say which surface is which, by the number the user must quote.

        MEASURED on venturi.stl: the run log read ``imported 1 volume(s), 7
        surface(s)`` for a geometry with five named boundaries, and nothing
        anywhere said what the other two were. Per-surface sizing addresses a
        surface by exactly this number -- it reaches Gmsh as a raw surface tag
        in ``SurfacesList`` -- so a numbering the app never prints is a
        numbering the user has to guess.
        """
        named: dict[str, list[int]] = {}
        unnamed: list[int] = []
        for tag in tags:
            name, known = self.surface_patch_name(tag)
            if known:
                named.setdefault(name, []).append(tag)
            else:
                unnamed.append(tag)
            # DP-410. A name the fusion merged into this one names these
            # faces too. It publishes under the other name -- a face belongs
            # to one patch -- but it is not a name without a surface, and
            # counting it as one is what refused the run.
            for alias in self.merged_surface_names.get(int(tag), ()):
                named.setdefault(alias, []).append(tag)
        parts = [f'{name} = {self._tag_ranges(owned)}'
                 for name, owned in sorted(named.items())]
        message = f': {"; ".join(parts)}' if parts else ''
        if unnamed:
            message += (
                f'{";" if parts else ":"} surface {self._tag_ranges(unnamed)} '
                f'{"is" if len(unnamed) == 1 else "are"} not named by the '
                'prepared geometry and will publish as '
                + ', '.join(f'face_{self.surface_origin.get(tag, tag)}'
                            for tag in unnamed))
        lost = sorted(self.declared_surface_names() - set(named))
        self.names_without_surface = set(lost)
        if lost:
            message += (
                f'{";" if parts or unnamed else ":"} '
                + ', '.join(lost)
                + (' is a prepared patch name' if len(lost) == 1
                   else ' are prepared patch names')
                + ' that no surface in this model carries')
            self.warnings.append(
                'the prepared geometry names ' + ', '.join(lost)
                + ', which no imported surface carries, so any control '
                'scoped to ' + ('it' if len(lost) == 1 else 'them')
                + ' reaches nothing')
        census = {'surfacesByPatch': {name: sorted(owned)
                                      for name, owned in named.items()},
                  'unnamedSurfaces': sorted(unnamed),
                  # DP-399. The count this is measured against is the
                  # receipt's own: 14 declared against 13 surfaces read as
                  # healthy because nobody subtracted them.
                  'namesWithoutSurface': lost}
        return {'message': message, 'details': census, 'statistics': census}

    #: Repairs `occ.healShapes` can perform, in the order its signature takes
    #: them, paired with the healing key on the page that selects each one.
    HEAL_REPAIRS = (
        ('fixDegenerated', 'fixDegenerated'),
        ('fixSmallEdges', 'fixSmallEdges'),
        ('fixSmallFaces', 'fixSmallFaces'),
        ('sewFaces', 'sewFaces'),
        ('makeSolids', 'makeSolids'),
    )

    def heal_shapes(self, healing):
        """Run the explicit OCC repair pass, if the case asked for one.

        Plan 31, "Geometry kernels and healing". `gmsh.model.occ.healShapes`
        is what repairs a dirty STEP -- slivers, unsewn faces, degenerate
        seams -- and until now nothing in this application called it: the
        page offered the four import flags and no pass at all. An audit of
        this repository has carried "heal_shape() uncalled" as a standing
        defect since long before this plan.

        The pass performs exactly the repairs ticked on the healing page.
        Handing Gmsh its own defaults instead would turn on face sewing and
        degenerate-edge fixing, both of which are MEASURED here to destroy a
        good import -- sewing drops a solid to a face set, and fixing
        degenerate edges makes a sphere unmeshable.

        Returns the ``(before, after)`` entity counts when it ran, else None.
        """
        gmsh = self.gmsh
        if not healing.get('healShapes'):
            return None
        repairs = {name: bool(healing.get(key))
                   for name, key in self.HEAL_REPAIRS}
        if not any(repairs.values()):
            # The derivation already warns about this; the run says so too,
            # because a ledger line reading "applied" against a pass that
            # changed nothing is the exact confusion this register exists to
            # prevent.
            self.warnings.append(
                'the explicit healing pass was requested with no repair '
                'selected, so nothing was healed')
            self.ledger.record(
                'gmsh/healing/healShapes', True, False, applied=False,
                note='no repair selected, so the pass was not run')
            return None
        tolerance = float(healing.get('importTolerance', 1e-6) or 1e-6)
        before = self._entity_counts()
        try:
            gmsh.model.occ.healShapes(
                dimTags=[], tolerance=tolerance, **repairs)
        except Exception as error:                           # noqa: BLE001
            raise MeshFailure(
                f'the explicit healing pass failed: {error}') from error
        gmsh.model.occ.synchronize()
        after = self._entity_counts()
        changed = ', '.join(
            f'{dimension}D {before[dimension]} to {after[dimension]}'
            for dimension in sorted(before) if before[dimension] != after[dimension])
        self.ledger.record(
            'gmsh/healing/healShapes', True, True,
            note=(f'healed with {", ".join(sorted(name for name, on in repairs.items() if on))} '
                  f'at tolerance {tolerance:g}; '
                  + (f'entity counts {changed}' if changed
                     else 'entity counts unchanged')))
        if not changed:
            # Not a failure: a clean import has nothing to repair. Saying so
            # is the difference between "healing did nothing" and "healing
            # was never asked for", which the user cannot otherwise tell
            # apart from a mesh that came out the same.
            self.warnings.append(
                'the explicit healing pass found nothing to repair; the '
                'import already had no small edges, small faces or open '
                'shells that these settings address')
        self.topology_changes.append({
            'stage': 'healShapes',
            # Healing runs before `resolve_scopes`, so no prepared identity
            # has been bound to a tag yet and none needs remapping. The key
            # is present because every reader of this list indexes it.
            'entities': {},
            'repairs': sorted(name for name, on in repairs.items() if on),
            'tolerance': tolerance,
            'before': {str(key): value for key, value in before.items()},
            'after': {str(key): value for key, value in after.items()},
        })
        return before, after

    #: Plan 31 FC-D. Marker the out-of-process classification probe prints
    #: its verdict on.
    CLASSIFY_MARKER = 'FOAMMESH_CLASSIFY '

    #: How long one probe gets before the settings it was trying are called
    #: non-terminating. MEASURED 2026-09-07, Gmsh 4.15.2: classifying
    #: `filleted_manifold.stl` (7424 triangles) took 0.67 s and
    #: `annulus_shell.stl` (512 triangles) had not returned after 15 minutes,
    #: so the gap between "slow" and "never" is wide enough for a flat budget.
    CLASSIFY_PROBE_SECONDS = 600

    #: How much memory one probe may commit before the kernel ends it.
    #: MEASURED: a diverging classification grows about 1.4 GB per minute, so
    #: this is reached at roughly the same time as ``CLASSIFY_PROBE_SECONDS``
    #: and is the backstop for the case where the timeout is not enough. It
    #: caps ``RLIMIT_DATA`` and not ``RLIMIT_AS``, because Gmsh reserves far
    #: more address space than it commits: an ``RLIMIT_AS`` of 8 GB makes
    #: ``gmsh.merge`` throw on a 512-triangle file.
    CLASSIFY_PROBE_BYTES = 16 * 1024 ** 3

    #: The probe: a second interpreter that merges the same sources and asks
    #: for the same classification, and says whether it survived it.
    CLASSIFY_PROBE = (
        'import json, math, sys\n'
        'try:\n'
        '    import resource\n'
        '    cap = int(sys.argv[3])\n'
        '    resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))\n'
        'except Exception:\n'
        '    pass\n'
        'import gmsh\n'
        'gmsh.initialize()\n'
        'gmsh.option.setNumber("General.Terminal", 0)\n'
        'angle = float(sys.argv[1])\n'
        'reparametrize = int(sys.argv[2])\n'
        # DP-401. The run's options stand when the run makes this call, so
        # they have to stand when the probe makes it: a probe that clears a
        # call under settings the run does not use has cleared nothing.
        'gmsh.option.setNumber("Geometry.Tolerance", float(sys.argv[4]))\n'
        'for item in sys.argv[5:]:\n'
        '    gmsh.merge(item)\n'
        '    gmsh.model.mesh.removeDuplicateNodes()\n'
        'verdict = {"ok": True, "error": ""}\n'
        'try:\n'
        '    gmsh.model.mesh.classifySurfaces(\n'
        '        angle * math.pi / 180.0, True, bool(reparametrize),\n'
        '        math.pi / 2)\n'
        '    gmsh.model.mesh.createGeometry()\n'
        '    verdict["surfaces"] = len(gmsh.model.getEntities(2))\n'
        'except Exception as error:\n'
        '    verdict = {"ok": False, "error": str(error) or '
        '"the classification failed without saying why"}\n'
        'print("FOAMMESH_CLASSIFY " + json.dumps(verdict))\n'
    )

    def classification_ladder(self, angle, reparametrize=True):
        """The classification settings to try, in order, as (angle, reparam).

        The first entry is always what the case asked for; nothing below it is
        reached unless that one is measured not to work.

        MEASURED 2026-09-07, Gmsh 4.15.2, both models from the `t3-redo`
        strict-GUI leg:

        * ``filleted_manifold.stl`` at the configured 40 deg classifies into 12
          patches, of which the two large ones are not topological disks
          (Euler characteristics -1 and -5, one and three boundary loops), and
          ``createGeometry`` raises `Wrong topology of boundary mesh for
          parametrization`. 20 deg and 25 deg fail the same way. **10 deg
          succeeds**, with 88 patches. So a quarter of the configured angle is
          the second entry.
        * ``annulus_shell.stl`` never gets that far: with reparametrisation on,
          `classifySurfaces` recurses without bound -- `Level N partition with
          2 triangles split in 2 parts` for over five million levels, each
          preceded by `Tolerance too large - aborting partitioning`, so the
          same two triangles are resubmitted for ever. It does that at 10, 15,
          20, 25, 30, 40 and 60 deg alike, which is why a finer angle is not
          enough on its own. **With reparametrisation off it returns at once**,
          173 patches, and `createGeometry` then parametrises them itself. So
          the third entry drops reparametrisation at the configured angle.
        """
        angle = float(angle)
        seen = []
        for candidate in ((angle, True), (angle / 4.0, True),
                          (angle, False), (angle / 4.0, False)):
            if candidate[0] <= 0.0 or candidate in seen:
                continue
            if candidate[1] and not reparametrize:
                # A non-manifold triangulation is the one case where the
                # reparametrising rungs are known not to terminate rather
                # than merely to fail, so they are not worth 10 minutes of
                # probe budget each. :meth:`census_triangulation` says so.
                continue
            seen.append(candidate)
            yield candidate

    def probe_classification(self, sources, angle, reparametrize):
        """Try one classification out of process; say what happened.

        Returns ``(ok, detail)``. ``ok`` is False both for a classification
        that raised and for one that never came back, and *detail* says which.

        It is a separate process for the same reason
        :meth:`read_back_census` is: the answer cannot be got any other way.
        A `classifySurfaces` that diverges holds the GIL in C++ for ever, so
        no timer, thread or signal inside this interpreter can end it -- the
        run this replaces was killed by the kernel at 64 GB after 47 minutes
        with the mesher still inside that one call, having emitted nothing
        since `importing geometry`.
        """
        if not sys.executable:
            raise MeshFailure(
                'there is no interpreter to check the surface classification '
                'with')
        argv = [sys.executable, '-c', self.CLASSIFY_PROBE,
                repr(float(angle)), '1' if reparametrize else '0',
                str(self.CLASSIFY_PROBE_BYTES),
                # DP-401: whatever tolerance is standing here, not Gmsh's
                # default, because it is what the run will classify under.
                repr(float(self.gmsh.option.getNumber('Geometry.Tolerance')))]
        argv.extend(str(item) for item in sources)
        try:
            completed = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.CLASSIFY_PROBE_SECONDS)
        except subprocess.TimeoutExpired:
            return False, (f'it had not returned after '
                           f'{self.CLASSIFY_PROBE_SECONDS} s')
        for line in completed.stdout.decode('utf-8', 'replace').splitlines():
            if line.startswith(self.CLASSIFY_MARKER):
                verdict = json.loads(line[len(self.CLASSIFY_MARKER):])
                if verdict.get('ok'):
                    return True, ''
                return False, str(verdict.get('error') or 'it failed')
        detail = completed.stderr.decode('utf-8', 'replace').strip()
        return False, (detail[-300:]
                       or f'the check exited {completed.returncode} without '
                          'saying what it found')

    def settle_classification(self, sources, angle, reparametrize=True):
        """Pick classification settings this triangulation survives.

        Plan 31 FC-D. Every entry of :meth:`classification_ladder` is tried
        out of process until one comes back clean, and that one is what the
        run then uses in process. A run whose configured settings work pays
        one extra classification for the certainty -- 0.67 s on the 7424
        triangles of `filleted_manifold.stl` -- and a run whose settings do
        not work gets a mesh instead of an opaque exception or a machine
        eaten alive.

        Raises :class:`MeshFailure` naming every setting tried, and what each
        one did, when none of them works.
        """
        configured = float(angle)
        attempts = []
        ladder = self.classification_ladder(configured, reparametrize)
        for candidate, reparametrize in ladder:
            ok, detail = self.probe_classification(
                sources, candidate, reparametrize)
            if ok:
                if attempts:
                    self.warnings.append(
                        'the surface classification this case asked for does '
                        'not work on this triangulation, so it was changed: '
                        + '; '.join(attempts)
                        + f'. What ran instead: {self._classification_words(candidate, reparametrize)}.'
                        + ('' if reparametrize else
                           ' Without reparametrisation Gmsh cuts the patches '
                           'without parametrising them as it goes, which is '
                           'slower to mesh and keeps more of the tessellated '
                           'boundary; the mesh is sound, it simply follows '
                           'the facets more closely.'))
                self.statistics['classification'] = {
                    'configuredAngle': configured,
                    'angle': candidate,
                    'reparametrize': bool(reparametrize),
                    'attempts': list(attempts),
                }
                return _Classification(candidate, bool(reparametrize))
            attempts.append(
                f'{self._classification_words(candidate, reparametrize)} '
                f'— {detail}')
        raise MeshFailure(
            'this triangulation cannot be classified into patches Gmsh can '
            'mesh. Every setting was tried:\n  '
            + '\n  '.join(attempts)
            + '\nThe surfaces are too tangled at this tessellation to '
              're-mesh. Re-export the geometry as CAD (STEP/IGES/BREP), or '
              'export the surfaces again at a finer, cleaner tessellation.')

    @staticmethod
    def _classification_words(angle, reparametrize):
        """One readable phrase for one pair of classification settings."""
        return (f'{angle:g} deg'
                + (' with reparametrisation' if reparametrize
                   else ' without reparametrisation'))

    def import_tessellated(self, sources, healing):
        """Build the meshable volumes of an STL/OBJ import from its shells.

        Gmsh's OCC importer reads CAD only, which is why a tessellated surface
        used to be refused outright. Gmsh itself has no such limit: a
        triangulation can be classified into surface patches and turned into a
        *discrete* geometry, which meshes like any other.

        Plan 30 WP-06a (fault F-38). What arrives is not one body: it is some
        number of closed shells, and which volume each one bounds is a
        question about the geometry rather than about the files. The shells
        are therefore split by triangle connectivity -- so a one-file and a
        two-file form of the same geometry classify identically -- nested by
        containment, and turned into one volume per shell that is not purely a
        void, with every directly enclosed shell cut out of it. Two separate
        bodies are two volumes; a box with an obstacle in it is one volume
        with one void. An open shell, or a pair whose containment cannot be
        decided, is refused by name in :mod:`shell_topology`.

        Two job keys steer the roles, both optional, and ``build_job`` writes
        both on every job: the seed from the prepared revision's recorded
        fluid seed, the roles from the geometries that declare one. Absent or
        empty means "infer", which is what a case that has said nothing gets:

        ``seedPoint``
            ``[x, y, z]`` in metres. The innermost shell containing it is the
            domain and meshes as fluid. A seed outside every shell is refused.
        ``shellRoles``
            ``{shell name: 'fluid' | 'solid' | 'void'}``. ``fluid`` and
            ``solid`` both mesh -- ``solid`` says the shell is a declared body
            rather than a void the nesting inferred -- and ``void`` is a hole
            and nothing else. A name that is not a shell of this geometry is
            reported with the names that are. Shell names are the source file
            stem, numbered ``#1``, ``#2`` when one file holds several, largest
            first, and they are listed in the run manifest under
            ``tessellatedImport.shells``.

        With neither key the roles alternate with nesting depth, which is what
        a single closed shell has always done: one volume of every surface.
        """
        gmsh = self.gmsh
        angle = float(healing.get('classificationAngle', 40.0))

        source_of_import = self.merge_tessellated(sources, healing)

        # Plan 28 WP7. Each `solid` of an STL arrives as its own discrete
        # surface, in file order, and those are the surfaces the job names
        # and scopes. Classification below cuts them by angle and numbers
        # the pieces afresh, so remember which triangles each solid owned
        # and rejoin the pieces to it afterwards.
        self.import_surface_tags = [tag for _dim, tag in gmsh.model.getEntities(2)]
        owned_before = {tag: self._surface_triangles(tag)
                        for tag in self.import_surface_tags}

        # Recover patch structure from the triangulation. Without
        # ``forReparametrization`` the surfaces carry no parametrisation and
        # cannot bound a volume.
        #
        # Plan 31 FC-D. Which settings get used is decided out of process
        # first, because this call has two failure modes on real STLs and one
        # of them never returns. See :meth:`settle_classification`.
        #
        # Plan 31 FC-E. How the triangles are joined is counted first,
        # because a surface with edges shared by more than two triangles is
        # the one input on which the reparametrising form of that call is
        # measured never to return.
        degenerate, opened, shared = self.census_triangulation()
        self.statistics['triangulation'] = {
            'degenerateTriangles': degenerate,
            'openEdges': opened,
            'nonManifoldEdges': shared,
        }
        reparametrize = not shared
        if shared:
            self.warnings.append(
                f'this surface is not manifold: {count_text(shared, "edge")} '
                f'{agreeing(shared, "is", "are")} shared by more than two '
                'triangles, so it pinches or doubles back on itself along '
                'them. Gmsh was therefore asked to classify '
                'it without reparametrisation, which is the only form of '
                'that call measured to finish on a surface like this; with '
                'reparametrisation it does not stop, and the run is killed '
                'for memory rather than told what is wrong. Repair the '
                'surface where those edges are if the mesh comes back wrong '
                'near them.')
        settled = self.settle_classification(sources, angle, reparametrize)
        self.ledger.record('gmsh/healing/classificationAngle',
                           angle, settled.angle,
                           applied=settled.angle == angle,
                           note='dihedral angle for surface classification')
        self.ledger.record('gmsh/healing/classifyForReparametrization',
                           reparametrize, settled.reparametrize,
                           applied=settled.reparametrize == reparametrize,
                           note='ask Gmsh to parametrise the classified '
                                'patches as it cuts them')
        gmsh.model.mesh.classifySurfaces(
            settled.angle * math.pi / 180.0, True, settled.reparametrize,
            math.pi / 2)
        # DP-75. ``createGeometry`` throws the imported facets away and
        # rebuilds every patch on a parametrisation Gmsh invents for it. A
        # patch that wraps -- a boss, a collar, anything whose facets face
        # more than one way round -- has no parametrisation that is one to
        # one, and the remesh built on it crosses itself. Classification is
        # still wanted, because the patches, the names and the shells are
        # read off it; only the remesh is not. So it is skipped, and the
        # discrete patches keep the triangles they were classified from.
        keep = bool(healing.get('keepTessellation'))
        self.ledger.record('gmsh/healing/keepTessellation', False, keep,
                           applied=keep,
                           note='mesh the imported facets rather than a '
                                'parametrised remesh of them')
        if keep:
            self.kept_tessellation = True
        else:
            gmsh.model.mesh.createGeometry()
        record = self.statistics.get('classification')
        if isinstance(record, dict):
            record['keptTessellation'] = keep
        angle = settled.angle

        surfaces = [tag for _dim, tag in gmsh.model.getEntities(2)]
        if not surfaces:
            raise MeshFailure(
                'the tessellated import produced no surfaces to mesh')
        owned_after = {tag: self._surface_triangles(tag) for tag in surfaces}
        self.surface_origin = regroup_classified(owned_before, owned_after)
        # C31-04. Classification is a topology change: it cuts each imported
        # solid into pieces and numbers them afresh. Following the identities
        # through it is what keeps a scope on the second STL on the second
        # STL's pieces.
        self.retarget_entities('classification',
                               _successors_of(self.surface_origin))
        unplaced = [tag for tag in surfaces if tag not in self.surface_origin]
        if unplaced:
            self.warnings.append(
                f'{count_text(len(unplaced), "classified surface")} could '
                'not be traced to an imported solid and kept '
                f'{agreeing(len(unplaced), "a generated name", "generated names")}')

        # Classification renumbers, so the shells are re-derived from the
        # triangulation that survived it rather than from the import order.
        sole = str(sources[0]) if len(sources) == 1 else ''
        surface_source = {
            tag: source_of_import.get(self.surface_origin.get(tag), sole)
            for tag in surfaces}
        triangles = {tag: self._surface_triangles_ordered(tag)
                     for tag in surfaces}
        seed = self.job.get('seedPoint')
        roles = self.job.get('shellRoles') or {}
        try:
            topology = resolve_topology(
                surfaces, triangles, self._node_coordinates(),
                sources=surface_source, roles=roles, seed=seed)
        except ShellTopologyError as error:
            raise MeshFailure(str(error)) from error

        for plan in topology.volumes:
            loops = [gmsh.model.geo.addSurfaceLoop(list(plan.outer))]
            # Every directly enclosed shell is a hole in this one, whatever it
            # meshes as itself, so two volumes never claim the same region.
            loops.extend(gmsh.model.geo.addSurfaceLoop(list(void))
                         for void in plan.voids)
            # DP-55. A hole's boundary faces the fluid from the other side,
            # and the boundary layer has to know that before it is extruded.
            for void in plan.voids:
                self.void_surfaces.update(int(tag) for tag in void)
            topology.shell(plan.name).volume_tag = int(
                gmsh.model.geo.addVolume(loops))
        for shell in topology.shells:
            winding = 1 if shell.volume >= 0.0 else -1
            for tag in shell.surfaces:
                self.surface_winding[int(tag)] = winding
                self.surface_shell[int(tag)] = str(shell.name)
        gmsh.model.geo.synchronize()

        # DP-59. The volumes exist now, and until this call none of them
        # belonged to anything: the receipts above declare `volume: []`,
        # truthfully, because the import did not make them. Registering them
        # here is the other half of that sentence, and without it every region
        # control scoped to a tessellated source refuses the run naming a
        # volume the run does contain.
        volume_identities = self._attribute_volumes(topology, sources)

        self.warnings.extend(topology.warnings)
        voids = sum(len(plan.voids) for plan in topology.volumes)
        self.statistics['tessellatedImport'] = {
            'sources': len(sources), 'shellCount': len(topology.shells),
            'volumes': len(topology.volumes), 'voids': voids,
            'domain': topology.domain or None,
            'shells': topology.to_list(),
            'calculation_version': topology.calculation_version,
            'surfaces': len(surfaces), 'classificationAngle': angle,
            'importedSolids': len(self.import_surface_tags),
            'tracedSurfaces': len(self.surface_origin),
            'volumeIdentities': dict(volume_identities),
        }
        self.reporter.emit(
            'progress', 'import', 0.12,
            f'{count_text(len(topology.shells), "closed shell")} -> '
            f'{count_text(len(topology.volumes), "volume")}, '
            f'{count_text(voids, "void")}',
            {'shells': topology.to_list()})

    def _attribute_volumes(self, topology, sources) -> dict:
        """Register ``<source_id>:volume:<n>`` for the volumes a file bounds.

        DP-59. An identity is registered by the import that adds the entity,
        and a tessellated import adds no volume: the volumes are made from the
        shells together, once every file has been read. Nothing attributed
        them back to the file whose shell bounds them, so no volume identity
        existed for any tessellated source. MEASURED across every Gmsh result
        on disk: 14 runs report scopes that matched nothing, 19 scopes in all,
        and every one of them is a volume scope.

        The join the CAD path gets for free is available here too. A shell
        records the file it came from, and ``surface_origin`` maps each
        classified piece back to the imported solid it was cut from. Imported
        solids arrive in file order, one per ``solid`` block, so ordering a
        source's volumes by the lowest imported solid their shell traces to
        numbers them the way the prepared side numbers a region: by body index
        within the file, and not by size, which is how the shells themselves
        are ordered. MEASURED across every published shell census: 27 of 28
        sources bound exactly one volume, where no ordering can disagree with
        another; the exception is ``two_cubes_one_file.stl``, whose two cubes
        are two bodies of one file and where the ordering is the question.
        """
        placed = {str(item): index for index, item in enumerate(sources)}
        ordered: dict[str, list] = {}
        for plan in topology.volumes:
            shell = topology.shell(plan.name)
            tag = int(getattr(shell, 'volume_tag', 0) or 0)
            if not tag:
                continue
            imported = sorted(
                self.surface_origin[surface] for surface in shell.surfaces
                if surface in self.surface_origin)
            if not imported:
                # A shell no piece of which traces to an imported solid
                # belongs to no file, and an identity that cannot be checked
                # is what these identities exist to stop being invented.
                continue
            ordered.setdefault(str(shell.source or ''), []).append(
                (imported[0], tag))

        identities: dict[str, int] = {}
        for path, entries in sorted(ordered.items()):
            index = placed.get(path)
            if index is None:
                # A shell spanning two files records both of them, joined.
                # It is one volume belonging to neither, and it keeps no
                # identity rather than an arbitrary one of the two.
                continue
            record = self._source_record(index, path)
            source_id = str(record.get('source_id') or '')
            if not source_id:
                continue
            for local, (_solid, tag) in enumerate(sorted(entries)):
                identity = f'{source_id}:volume:{local}'
                self.entity_tags[identity] = {
                    'kind': 'volume', 'dim': 3, 'tags': [tag]}
                identities[identity] = tag
        return identities

    def _node_coordinates(self):
        """Every mesh node as ``tag -> (x, y, z)``, for the shell geometry."""
        tags, flat, _parametric = self.gmsh.model.mesh.getNodes()
        table = {}
        for index, tag in enumerate(tags):
            base = 3 * index
            table[int(tag)] = (float(flat[base]), float(flat[base + 1]),
                               float(flat[base + 2]))
        return table

    def _surface_triangles_ordered(self, tag):
        """The triangles of one discrete surface, winding preserved.

        :meth:`_surface_triangles` sorts each triple, which is right for
        matching a triangle across classification and wrong for anything that
        needs the normal. Shell topology needs the winding: the sign of the
        enclosed volume is how an inward-facing shell is recognised.
        """
        triples = []
        types, _elements, nodes = self.gmsh.model.mesh.getElements(2, tag)
        for element_type, node_tags in zip(types, nodes):
            if int(element_type) != 2:
                continue
            flat = [int(value) for value in node_tags]
            for start in range(0, len(flat) - 2, 3):
                triples.append(tuple(flat[start:start + 3]))
        return triples

    # -- C31-04: source-aware entity identity ------------------------------ #

    def _source_record(self, index, path) -> dict:
        """The prepared source one entry of ``job['geometry']`` is.

        Positional, because ``build_job`` writes ``sources`` from the same
        prepared manifest order it writes ``geometry`` from, with the file
        name checked so a mismatched pair is reported rather than silently
        mapping one file's faces onto another file's identity.
        """
        table = self.job.get('sources') or ()
        if index >= len(table) or not isinstance(table[index], dict):
            return {}
        record = table[index]
        expected = str(record.get('prepared_name') or '')
        actual = str(path).replace('\\', '/').rsplit('/', 1)[-1]
        if expected and actual and expected != actual:
            self.warnings.append(
                f'the job lists {actual} as prepared source {expected!r}; its '
                'entity identities were not used, so any control scoped to '
                'that source resolves by position only')
            return {}
        return record

    def merge_tessellated(self, sources, healing) -> dict:
        """Merge each STL/OBJ file; return which file each surface came from.

        DP-613 (field audit 0924 gmsh-sizing D10). ``removeDuplicateNodes``
        ran after every merge whatever "Remove duplicate nodes" said. MEASURED
        on Gmsh 4.15.2 (field_fix_scratch/sizing/probe613): the STL reader
        already joins the points of one file -- a cube split into two solids
        in one file is 8 nodes with or without the call -- and the call only
        joins points *between* files: the same cube over two files is 16
        nodes without it and 8 with it. So the checkbox now decides that, and
        a multi-file import left unjoined says what it risks.
        """
        gmsh = self.gmsh
        weld = bool(healing.get('removeDuplicateNodes', True))
        source_of_import = {}
        for index, item in enumerate(sources):
            before = {tag for _dim, tag in gmsh.model.getEntities(2)}
            gmsh.merge(str(item))
            if weld:
                gmsh.model.mesh.removeDuplicateNodes()
            added = [tag for _dim, tag in gmsh.model.getEntities(2)
                     if tag not in before]
            for tag in added:
                source_of_import[tag] = str(item)
            # WP-01 F-35. Each `solid` block of an STL arrives as its own
            # discrete surface, so the surfaces this file added are the shells
            # it contributed. Volumes come later, from the shells together.
            self.import_receipts.append(self._source_receipt(
                index, item, stage='tessellated', surfaces=len(added),
                volumes=0, shells=len(added),
                added={'surface': sorted(added), 'volume': []}))
        self.ledger.record(
            'gmsh/healing/removeDuplicateNodes.betweenFiles', weld, weld,
            applied=weld,
            note=('coincident points joined between the imported files'
                  if weld else 'unticked: points shared between the imported '
                  'files were left apart (points within one file are always '
                  'joined by the STL reader)'))
        if not weld and len(sources) > 1:
            self.warnings.append(
                '"Remove duplicate nodes" is off, so the '
                f'{len(sources)} imported files were not joined where they '
                'meet; a body split across files will read as open shells')
        return source_of_import

    def _source_receipt(self, index, path, *, stage, surfaces, volumes,
                        shells, added) -> dict:
        """One import receipt, with the entity mapping this source produced.

        The counts are the WP-01 fields, unchanged and still read by the run
        manifest. What is added is the part a second source made necessary:
        which Gmsh tags carry which prepared entity, so a scope resolves
        against its own file.
        """
        record = self._source_record(index, path)
        source_id = str(record.get('source_id') or '')
        declared = int(record.get('declared_surfaces') or 0)
        found = len(added.get('surface') or ())
        shared = self._shared_listings(record, declared, found)
        entities = []
        if source_id:
            for kind, tags in added.items():
                dimension = 2 if kind == 'surface' else 3
                listings = self._listing_tags(
                    tags, shared if kind == 'surface' else {})
                for local, owned in enumerate(listings):
                    identity = f'{source_id}:{kind}:{local}'
                    self.entity_tags[identity] = {
                        'kind': kind, 'dim': dimension, 'tags': list(owned)}
                    entities.append({'entity_id': identity, 'kind': kind,
                                     'dim': dimension, 'tags': list(owned)})
        if shared:
            self.ledger.record(
                f'sharedFaces:{source_id}', len(shared), len(shared),
                note=(f'{count_text(len(shared), "face")} two solids share '
                      'imported once and answer to both listings: '
                      + ', '.join(f'{alias} = {first}' for alias, first
                                  in sorted(shared.items()))))
        if not source_id:
            status = 'unmapped'
        elif declared and declared - len(shared) != found:
            # The prepared revision named a different number of faces than
            # Gmsh made. Saying so is the difference between a scope that is
            # known to be approximate and one that quietly points elsewhere.
            status = 'partial'
        else:
            status = 'exact'
        return {
            'schema_version': int(self.job.get('entitySchemaVersion') or 1),
            'path': str(path),
            'source_id': source_id,
            'import_stage': stage,
            'surfaces': surfaces, 'volumes': volumes, 'shells': shells,
            'entities': entities,
            # DP-399. Kept so the status can be decided again later, against
            # the tags the model holds rather than the ones import made.
            'declared_surfaces': declared,
            'mapping_confidence': float(record.get('mapping_confidence', 1.0)
                                        if record else 0.0),
            'mapping_status': status,
        }

    @staticmethod
    def _shared_listings(record, declared, found) -> dict:
        """``{second listing: first listing}`` for this source's shared faces.

        DP-546. Used only when the import agrees with it: the file listed
        *declared* faces, the job says ``len(shared)`` of those are a face two
        solids share, and Gmsh added exactly the rest. Any other count means
        the import did something the prepared walk did not foresee, and the
        plain positional join -- with its ``partial`` status -- says so.
        """
        shared = {}
        for alias, first in ((record or {}).get('shared_surfaces')
                             or {}).items():
            try:
                shared[int(alias)] = int(first)
            except (TypeError, ValueError):
                return {}
        if not shared or declared - len(shared) != found:
            return {}
        return shared

    @staticmethod
    def _listing_tags(tags, shared) -> list:
        """The tags each listing of one source owns, listing by listing.

        DP-546. Gmsh binds a face two solids share at its first listing and
        numbers every later face from there, so the second listing takes the
        first one's tag and spends none of its own.
        """
        if not shared:
            return [[int(tag)] for tag in tags]
        listings: list[list[int]] = []
        remaining = iter(tags)
        for local in range(len(tags) + len(shared)):
            if local in shared:
                listings.append(list(listings[shared[local]]))
            else:
                listings.append([int(next(remaining))])
        return listings

    def retarget_entities(self, stage, successors, *, dimensions=(2,)):
        """Follow every entity identity through one topology change.

        C31-04/§4.1. Healing, classification and the farfield Boolean all
        renumber: the tags an identity was mapped to during import may be
        gone, replaced by several pieces, or consumed outright. *successors*
        is ``{old tag: [new tags]}`` for the dimensions given; an entity of
        those dimensions with no successor is recorded as consumed and its
        tag list emptied, so a control scoped to it is refused rather than
        applied to whatever now occupies that position.
        """
        moved: dict[str, dict] = {}
        for identity, record in self.entity_tags.items():
            if record['dim'] not in dimensions:
                continue
            before = list(record['tags'])
            after: list[int] = []
            for tag in before:
                after.extend(successors.get(tag, ()))
            after = sorted(set(after))
            if after != before:
                record['tags'] = after
                moved[identity] = {'from': before, 'to': after}
        if moved:
            self.topology_changes.append({'stage': stage, 'entities': moved})
        return moved

    def revalidate_scopes(self, stage, *, dimensions=(2, 3)):
        """Re-check every identity against the model, after it has changed.

        C31-04/§4.1: "resolve and validate mappings after every relevant
        topology change". Import, classification and the farfield cut are
        followed as they happen; this is the general re-check for a change
        made later -- a volume removed by an exclusion control is the one the
        runner performs today -- so a scope that was resolved before it is not
        still handing a later control a tag the model no longer has.

        The boundary-layer extrusion is deliberately not routed through here:
        it replaces surfaces with the rim faces that took their place, and
        ``layer_replacements`` already carries that lineage for naming.
        """
        alive = {dimension: {int(tag) for _dim, tag
                             in self.gmsh.model.getEntities(dimension)}
                 for dimension in dimensions}
        # DP-527. Per dimension: one tag-keyed map over both let a surviving
        # volume 7 keep a consumed surface 7 alive.
        moved: dict[str, dict] = {}
        for dimension in dimensions:
            moved.update(self.retarget_entities(
                stage, {tag: [tag] for tag in alive[dimension]},
                dimensions=(dimension,)))
        if not moved:
            return moved
        scoped = self.job.get('scopeEntities') or {}
        if not self.entity_tags or not (scoped.get('surfaces')
                                        or scoped.get('volumes')):
            return moved
        self.scope_surfaces, _missing = self._scopes_by_identity(
            scoped.get('surfaces') or {}, 'surface')
        self.scope_volumes, _absent = self._scopes_by_identity(
            scoped.get('volumes') or {}, 'volume')
        return moved

    def _refresh_receipt_entities(self) -> None:
        """Republish each receipt's entity tags after the topology changes.

        The receipt is written during import and read after it, so what it
        reports has to be what the model holds when the run is described --
        not the numbering the import happened to start with.
        """
        for receipt in self.import_receipts:
            for entry in receipt.get('entities') or ():
                current = self.entity_tags.get(entry['entity_id'])
                if current is not None:
                    entry['tags'] = list(current['tags'])
            # DP-399. The status used to be settled during import, before
            # healing and before the duplicate fusion, so a source that lost
            # a face to either still reported `exact` -- the one field a
            # reader would consult to ask whether the mapping held. It is the
            # same question; this is the first point at which it can be
            # answered.
            if receipt.get('mapping_status') == 'unmapped':
                continue
            declared = int(receipt.get('declared_surfaces') or 0)
            alive = sum(1 for entry in receipt.get('entities') or ()
                        if entry['kind'] == 'surface' and entry['tags'])
            receipt['mapping_status'] = (
                'exact' if not declared or declared == alive else 'partial')

    def _entity_signatures(self, dimensions=(2, 3)) -> dict:
        """Where each entity is and how big it is, keyed by tag.

        DP-410. The one thing that survives ``removeAllDuplicates``: the tag
        numbering does not, but two coincident faces have one centre of mass
        and one area between them, and that is still true of the single face
        the fusion leaves behind. Rounded, because the fused face is rebuilt
        rather than kept and the arithmetic is not bit-identical.

        DP-527. Keyed by ``(dim, tag)``, not by the tag alone. Gmsh numbers
        each dimension from 1, so surface 1 and volume 1 are two entities with
        one number, and a tag-keyed map let the volume's signature overwrite
        the surface's. MEASURED on G1 `jacketed_pipe.step`: tag 7, the jacket's
        copy of the core's cylinder, has exactly the centre and area of the
        surviving tag 1 -- but surface 1's signature had been replaced by
        volume 1's, so the fusion found "no survivor", face6 was reported as a
        name no surface carried and its scope as unresolved.
        """
        signatures: dict[tuple, tuple] = {}
        occ = self.gmsh.model.occ
        for dimension in dimensions:
            for _dim, tag in occ.getEntities(dimension):
                try:
                    centre = occ.getCenterOfMass(dimension, tag)
                    mass = occ.getMass(dimension, tag)
                except Exception:  # noqa: BLE001 - OCC raises many ways
                    continue
                signatures[(int(dimension), int(tag))] = (
                    dimension,
                    tuple(round(float(value), 9) for value in centre),
                    # Nine significant figures, not nine decimal places: the
                    # area of a large face carries its magnitude with it and
                    # an absolute rounding would compare the noise.
                    float('%.8e' % float(mass)))
        return signatures

    def _follow_duplicate_fusion(self, before: dict) -> dict:
        """Point each identity the fusion consumed at the face that replaced it.

        DP-410. ``removeAllDuplicates`` fuses two coincident faces into one,
        which is what makes a shared interface conformal and is why it is on
        by default. It also drops one tag of the pair. ``revalidate_scopes``
        maps every *surviving* tag to itself, so the dropped one had no
        successor at all: it was recorded as consumed, the prepared name it
        carried became a name no surface held, and DP-399 refused the run.

        MEASURED on `baffled_chamber.step` in the campaign runtime: 14
        surfaces in, tag 14 out, and the survivor -- tag 13, same centre of
        mass, same area, adjacent to the same single volume -- is an external
        face that can carry exactly the boundary layer the run was refused
        for. The two tags were one face all along. So the successor is found
        by that signature and the identity follows it.

        A dropped tag that matches no survivor, or more than one, is left
        consumed: that is the case this cannot read, and guessing it would
        put a user's patch name on a face chosen by arithmetic.
        """
        dimensions = tuple(sorted({dimension for dimension, _tag in before}))
        after = self._entity_signatures(dimensions)
        if not before:
            return {}
        by_signature: dict[tuple, list[tuple]] = {}
        for key, signature in after.items():
            by_signature.setdefault(signature, []).append(key)
        # One successor map per dimension, for the reason the signatures are
        # keyed by dimension: a surviving surface 3 is no successor for a
        # consumed volume 3.
        successors = {dimension: {tag: [tag] for dim, tag in after
                                  if dim == dimension}
                      for dimension in dimensions}
        for dimension, tag in sorted(set(before) - set(after)):
            candidates = by_signature.get(before[(dimension, tag)]) or []
            if len(candidates) != 1:
                self.ledger.record(
                    f'duplicateFusion:{tag}', tag, None, applied=False,
                    note=('the fusion consumed this entity and '
                          + ('no survivor' if not candidates
                             else f'{len(candidates)} survivors')
                          + ' share its centre of mass and size, so anything '
                            'scoped to it is refused rather than moved'))
                continue
            survivor = candidates[0][1]
            successors[dimension][tag] = [survivor]
            if dimension == 2:
                self.duplicate_face_successors[int(tag)] = int(survivor)
            else:
                self.duplicate_volume_successors[int(tag)] = int(survivor)
        if self.duplicate_face_successors or self.duplicate_volume_successors:
            for dimension in dimensions:
                self.retarget_entities('duplicate-fusion',
                                       successors[dimension],
                                       dimensions=(dimension,))
        return self.duplicate_face_successors

    def _announce_merged_names(self) -> None:
        """Say which prepared names the fusion made into one face.

        DP-410. Two names on one face is not an error -- the faces really are
        one face now -- but it is a thing the user did that the run quietly
        changed, and a patch they named that will not appear in the mesh
        under its own name. It is said once, with both names in it.
        """
        for tag in sorted(self.merged_surface_names):
            published = self.entity_surface_names.get(int(tag))
            extras = self.merged_surface_names[tag]
            if not published:
                continue
            if self._bounds_two_volumes(tag):
                # DP-527. The two sides of an interface between two solids
                # are the one pair the fusion exists for: each solid brings
                # its own copy, and fusing them is what makes the interface
                # conformal. Neither name publishes as a patch -- the face is
                # interior -- so "publishes under" was false and "turn off
                # remove duplicate faces" was advice that breaks the volume
                # mesh. MEASURED on G1 `jacketed_pipe`: face0/face6 is the
                # core-to-jacket interface, and the prepared group manifest
                # already declares it as an interface pair.
                # DP-557. The requested side is the list of names that fused
                # and the effective side the one surface they became, so the
                # ledger's string equality compared a list with a name and
                # filed every conformal interface under `controlMismatches`.
                # The fusion is what an interface between two volumes asks
                # for: it matched.
                self.ledger.record(
                    f'mergedInterface:{published}', sorted(extras), published,
                    note='the two sides of an interface between two volumes '
                         'fused into one conformal surface',
                    matched=True)
                continue
            self.warnings.append(
                'the import fused coincident faces, so '
                + ', '.join(sorted(extras))
                + (' names' if len(extras) == 1 else ' name')
                + f' the same surface as {published} and the patch publishes '
                  f'under {published}. Turn off "remove duplicate faces" if '
                  'these were meant to stay separate patches.')
            self.ledger.record(
                f'mergedPatch:{published}', sorted(extras), published,
                note='coincident faces fused into one surface')

    def _bounds_two_volumes(self, tag) -> bool:
        """Whether surface *tag* lies between two volumes, i.e. is interior."""
        try:
            upward, _down = self.gmsh.model.getAdjacencies(2, int(tag))
        except Exception:  # noqa: BLE001 - an unanswerable model is not one
            return False
        return len(upward) >= 2

    def _entity_counts(self) -> dict:
        """How many entities of each dimension the model holds right now.

        Differencing this across one ``importShapes`` is what turns "the model
        has 30 surfaces" into "this file brought 6 of them".
        """
        return {dimension: len(self.gmsh.model.getEntities(dimension))
                for dimension in (2, 3)}

    def _surface_triangles(self, tag):
        """The triangles of one discrete surface, as oriented node triples.

        Node tags survive classification; element tags do not. A triple is
        therefore the one key that identifies a triangle on both sides.

        DP-456. The triple used to be sorted, and on a conformal assembly
        that made two triangles one key: the interface is drawn once by each
        body, from the same welded nodes, and the two copies differ only in
        winding. MEASURED on `baffled_chamber`, whose two bodies are 180 and
        12 triangles: both copies of the interface voted for the same file,
        so :func:`regroup_classified` handed the downstream body's cap to the
        upstream one and the split came out 182 and 10 -- one body with five
        crowded edges and one with a hole, neither of them closed, and the
        run refused. Rotating to the smallest node instead of sorting keeps
        the cycle, so the two copies are two keys and each goes home.
        """
        triples = set()
        types, _elements, nodes = self.gmsh.model.mesh.getElements(2, tag)
        for element_type, node_tags in zip(types, nodes):
            if int(element_type) != 2:
                continue
            flat = [int(value) for value in node_tags]
            for start in range(0, len(flat) - 2, 3):
                triples.add(oriented_key(flat[start:start + 3]))
        return triples

    def census_triangulation(self):
        """How the imported triangulation is joined up, edge by edge.

        Returns ``(degenerate, open_edges, non_manifold_edges)`` counted over
        every surface the import brought in. An edge is keyed by its two node
        ids: ``removeDuplicateNodes`` has already run, so two triangles that
        meet along a seam share the ids for it.

        Plan 31 FC-E. This is counted *before* classification because how the
        triangles are joined decides whether Gmsh's reparametrising
        classification can terminate at all -- see
        :meth:`classification_ladder`.

        MEASURED 2026-09-07 over all 22 STLs of the `t3-redo` strict-GUI leg,
        both by node id and again with the nodes welded by position at
        1e-6 m: `annulus_shell.stl` is the only one that is not a clean closed
        manifold -- 512 triangles, 0 degenerate, 0 open edges, **168 edges
        shared by more than two triangles**. Every other model,
        `filleted_manifold.stl` (7424 triangles) included, counts 0 of each.
        """
        counts = {}
        degenerate = 0
        for tag in self.import_surface_tags:
            types, _elements, nodes = self.gmsh.model.mesh.getElements(2, tag)
            for element_type, node_tags in zip(types, nodes):
                if int(element_type) != 2:
                    continue
                flat = [int(value) for value in node_tags]
                for start in range(0, len(flat) - 2, 3):
                    first, second, third = flat[start:start + 3]
                    if first == second or second == third or first == third:
                        degenerate += 1
                        continue
                    for one, other in ((first, second), (second, third),
                                       (third, first)):
                        edge = (min(one, other), max(one, other))
                        counts[edge] = counts.get(edge, 0) + 1
        opened = sum(1 for used in counts.values() if used == 1)
        shared = sum(1 for used in counts.values() if used > 2)
        return degenerate, opened, shared

    def build_farfield(self):
        """Wrap the imported solids in a box, sphere or cylinder; cut them out.

        Plan 30 WP12, section 4.1. The assembly fixtures ship a
        ``_farfield.stl`` beside every model because nothing in the product
        could make one, so an external-flow case needed a file authored
        elsewhere. This builds the same thing from the geometry that is
        already loaded: a box ``padding`` bounding-box diagonals larger on
        every side, with the bodies subtracted, leaving one fluid volume whose
        inner boundary is the bodies.

        Returns True when the model changed, because every tag the caller is
        holding is then stale.

        Two things have to survive the cut. The body faces come back with new
        tags, so each is matched to the face it replaced by centre of mass and
        area and recorded in ``surface_origin`` -- the same mechanism a
        classified STL import uses, which is why the scopes and the patch
        names still resolve afterwards. The six new faces are named here,
        because nothing prepared them: ``far_field_xMin`` and its five
        siblings, spelled so the publisher's category rule reads them as a far
        field rather than defaulting them to walls.

        Plan 37 UF13. The primitive may also be a sphere or a cylinder, from
        the shared farfield specification (``farfield_primitives``). It is
        resolved against the bounds the CAD kernel reports and refused --
        before anything is added to the model -- when it does not contain
        every body with clearance. Its faces are named by where they are: a
        boolean renumbers every face, so the sphere's one face is
        ``far_field`` and the cylinder's are ``far_field_side``,
        ``far_field_inlet`` and ``far_field_outlet``, the caps ordered along
        the axis. The box is built, classified and named exactly as before.
        """
        farfield = self.intent.get('farfield') or {}
        if not farfield.get('enabled'):
            return False
        gmsh = self.gmsh
        padding = float(farfield.get('padding', 2.0) or 0.0)
        shape = str(farfield.get('shape') or farfield_primitives.BOX
                    ).strip().lower()
        if self.tessellated:
            reason = (f'a farfield {shape} is cut out of the imported solids '
                      'by the CAD kernel, and a tessellated import has no '
                      'solid to cut; supply the geometry as STEP, or supply '
                      'the farfield as a surface of its own')
            self.warnings.append(f'no farfield {shape} was built: {reason}')
            self.ledger.record('gmsh/farfield/enabled', True, False,
                               applied=False, note=reason)
            return False

        bodies = gmsh.model.getEntities(3)
        before = gmsh.model.getBoundingBox(-1, -1)
        diagonal = math.dist(before[0:3], before[3:6])
        try:
            primitive = farfield_primitives.resolve(farfield, before)
        except farfield_primitives.FarfieldError as error:
            # Refused before the model is touched: nothing was added, and
            # the imported solids are exactly as they were read.
            raise MeshFailure(f'no farfield {shape} was built: {error}'
                              ) from error
        signatures = {tag: self.surface_signature(tag)
                      for _dim, tag in gmsh.model.getEntities(2)}

        tool = self.add_farfield_primitive(primitive)
        try:
            gmsh.model.occ.cut([(3, tool)], list(bodies),
                               removeObject=True, removeTool=True)
            gmsh.model.occ.synchronize()
        except Exception as error:
            raise MeshFailure(
                f'the farfield {shape} could not be cut against the '
                f'geometry: {error}') from error
        volumes = gmsh.model.getEntities(3)
        if not volumes:
            raise MeshFailure(
                f'cutting the farfield {shape} against '
                f'{count_text(len(bodies), "solid")} left no volume at all; '
                f'the {shape} was consumed by the cut, '
                'which happens when a solid is larger than the farfield '
                'allows for')
        if shape == farfield_primitives.BOX:
            origin, span = primitive['origin'], primitive['span']
            domain, sealed = self.classify_cut_volumes(volumes, origin, span,
                                                       diagonal)
        else:
            domain, sealed = self.classify_cut_volumes_by(
                volumes,
                lambda face: self.primitive_face_name(primitive, face)
                is not None)
        if not domain:
            side, remedy = (('a side', 'increase the padding')
                            if shape == farfield_primitives.BOX
                            else ('a face', f'enlarge the {shape}'))
            raise MeshFailure(
                f'cutting the farfield {shape} against '
                f'{count_text(len(bodies), "solid")} left '
                f'{count_text(len(volumes), "volume")}, none of them bounded '
                f'by {side} of the generated {shape}, so none of them is the '
                f'external domain. A solid reaching outside the farfield is '
                f'the usual cause; {remedy} or supply the domain as its own '
                'surface.')
        if len(domain) > 1:
            raise MeshFailure(
                f'cutting the farfield {shape} against '
                f'{count_text(len(bodies), "solid")} '
                f'split the external domain into {len(domain)} separate '
                f'volumes, each touching the generated {shape}. Meshing one '
                'of them would silently drop the rest of the flow region, so '
                'the run stops here: the usual cause is a solid that spans '
                f'the {shape}, or overlapping and open solids.')
        sealed_policy = str((self.intent.get('farfield') or {}).get(
            'sealedCavities', 'discard') or 'discard').strip().lower()
        volumes = self.apply_cavity_policy(domain, sealed, sealed_policy)

        # The body faces, matched back to the tags their names are keyed on.
        self.import_surface_tags = list(signatures)
        inherited, box_faces = 0, []
        for _dim, tag in gmsh.model.getEntities(2):
            centre, area = self.surface_signature(tag)
            source = self.matching_surface(centre, area, signatures, diagonal)
            if source is None:
                box_faces.append((tag, centre))
            else:
                self.surface_origin[tag] = source
                inherited += 1
        # C31-04. The cut is a topology change of the strongest kind: the body
        # faces survive under new tags and every imported *volume* is consumed
        # -- the one volume that remains is the fluid around the bodies, which
        # no imported source owns. Following the surfaces and emptying the
        # volumes is what makes a region-scoped control on a cut-away body a
        # refusal instead of a size silently applied to the fluid domain.
        self.retarget_entities('farfield-cut',
                               _successors_of(self.surface_origin))
        self.retarget_entities('farfield-cut', {}, dimensions=(3,))
        if shape == farfield_primitives.BOX:
            self.name_farfield_faces(box_faces, origin, span, diagonal)
        else:
            self.name_primitive_faces(box_faces, primitive)

        self.record_farfield(primitive, bodies, padding)
        self.statistics['farfield'] = {
            'shape': shape,
            # Only the box reads a padding; a sphere or a cylinder is sized by
            # its radius and length, which ``primitive`` carries.
            'padding': padding if shape == farfield_primitives.BOX else None,
            'standoff': primitive.get('standoff'),
            'primitive': {key: value for key, value in primitive.items()
                          if key not in ('shape',)},
            'primitiveVolume': primitive['volume'],
            'clearance': primitive['clearance'],
            'boundingBox': list(gmsh.model.getBoundingBox(-1, -1)),
            'bodies': len(bodies),
            'domainVolumes': len(domain),
            'domainIdentifiedBy': (
                'faces lying on a side of the generated box'
                if shape == farfield_primitives.BOX
                else f'faces lying on the generated {shape}'),
            'sealedCavities': len(sealed),
            'sealedCavityPolicy': sealed_policy,
            'sealedCavityVolumes': [round(mass, 12) for mass, _tag in sealed],
            'volumes': len(volumes),
            'bodyFaces': inherited,
            'boxFaces': len(box_faces),
            'primitiveFaces': len(box_faces),
            'patches': sorted(self.generated_names.values()),
        }
        if shape != farfield_primitives.BOX:
            # ``boxFaces`` is the box's own count, kept for the readers of the
            # box statistics; a sphere or a cylinder has no box faces, so it
            # reports ``primitiveFaces`` alone. The box keeps both keys, in
            # the same order, so its statistics are unchanged.
            del self.statistics['farfield']['boxFaces']
        detail = ({'standoff_m': round(primitive['standoff'], 6)}
                  if shape == farfield_primitives.BOX
                  else {'clearance_m': round(primitive['clearance'], 6)})
        self.reporter.emit(
            'progress', 'import', 0.12,
            f'built a farfield {shape} around '
            f'{count_text(len(bodies), "solid")}', detail)
        return True

    def add_farfield_primitive(self, primitive):
        """Add the resolved primitive to the OCC model; return its tag."""
        occ = self.gmsh.model.occ
        shape = primitive['shape']
        if shape == farfield_primitives.BOX:
            return occ.addBox(*primitive['origin'], *primitive['span'])
        if shape == farfield_primitives.SPHERE:
            return occ.addSphere(*primitive['centre'], primitive['radius'])
        length = primitive['length']
        return occ.addCylinder(*primitive['base'],
                               *(item * length for item in primitive['axis']),
                               primitive['radius'])

    def record_farfield(self, primitive, bodies, padding):
        """The ledger lines: what was asked for and what was built."""
        shape = primitive['shape']
        solids = count_text(len(bodies), 'solid')
        if shape == farfield_primitives.BOX:
            standoff = primitive['standoff']
            self.ledger.record('gmsh/farfield/enabled', True, True,
                               note=f'{solids} cut out '
                                    f'of a box {padding:g} '
                                    f'{agreeing(padding, "diagonal")} larger '
                                    'on each side')
            self.ledger.record('gmsh/farfield/padding', padding, padding,
                               note=f'{standoff:.6g} m on each side')
        else:
            self.ledger.record('gmsh/farfield/enabled', True, True,
                               note=f'{solids} cut out of a {shape} of '
                                    f'{primitive["volume"]:.6g} m^3, '
                                    f'{primitive["clearance"]:.6g} m clear '
                                    'of the geometry')
            self.ledger.record('gmsh/farfield/radius', primitive['radius'],
                               primitive['radius'], note='m')
        self.ledger.record('gmsh/farfield/shape', shape, shape)
        farfield = self.intent.get('farfield') or {}
        mode = str(farfield.get('centreMode') or 'auto')
        self.ledger.record('gmsh/farfield/centreMode', mode, mode,
                           note=('the bounding-box centre' if mode == 'auto'
                                 else 'as authored'))
        if shape == farfield_primitives.CYLINDER:
            self.ledger.record('gmsh/farfield/length', primitive['length'],
                               primitive['length'], note='m')
            self.ledger.record('gmsh/farfield/axis', farfield.get('axis'),
                               [round(item, 12) for item in primitive['axis']],
                               note='normalised to a unit direction')

    def face_point(self, tag):
        """A point on surface ``tag`` itself.

        The centre of mass of a curved face lies off it -- a sphere's is its
        centre -- so the face is sampled at the middle of its own
        parametrisation instead. Falls back to the centre of mass where the
        kernel cannot say.
        """
        gmsh = self.gmsh
        try:
            low, high = gmsh.model.getParametrizationBounds(2, int(tag))
            middle = [(float(low[index]) + float(high[index])) / 2.0
                      for index in range(2)]
            point = gmsh.model.getValue(2, int(tag), middle)
            if len(point) >= 3:
                return tuple(float(item) for item in point[:3])
        except Exception:                                    # noqa: BLE001
            pass
        return tuple(gmsh.model.occ.getCenterOfMass(2, int(tag)))

    def primitive_face_name(self, primitive, tag):
        """The outer patch face ``tag`` is on, or ``None`` for a body face."""
        return farfield_primitives.classify_point(primitive,
                                                  self.face_point(tag))

    def classify_cut_volumes_by(self, volumes, on_primitive):
        """``classify_cut_volumes`` for a sphere or a cylinder.

        The same rule: the external domain is the volume the primitive
        bounds, and a volume bounded only by body faces is sealed inside the
        geometry. Returns ``(domain, sealed)``, each a list of ``(mass, tag)``.
        """
        domain, sealed = [], []
        for _dim, tag in volumes:
            faces = [int(face) for _face_dim, face
                     in self.gmsh.model.getBoundary([(3, int(tag))],
                                                    oriented=False)]
            entry = (float(self.gmsh.model.occ.getMass(3, int(tag))), int(tag))
            (domain if any(on_primitive(abs(face)) for face in faces)
             else sealed).append(entry)
        return domain, sealed

    def name_primitive_faces(self, faces, primitive):
        """Name the sphere's or cylinder's faces by where each one lies."""
        for tag, _centre in faces:
            name = self.primitive_face_name(primitive, tag)
            if name is not None:
                self.generated_names[int(tag)] = name
                continue
            self.warnings.append(
                f'surface {tag} appeared during the farfield cut and lies '
                f'on no face of the {primitive["shape"]}; it publishes '
                'unnamed')

    def classify_cut_volumes(self, volumes, origin, span, diagonal):
        """Split what the cut left into the external domain and the cavities.

        Plan 31 CP-08 item 7: "never silently assume the largest remaining
        volume is the user's intended domain". This used to sort the volumes
        by mass and call the biggest one the domain, which is right on every
        fixture here and right for the wrong reason -- it is a guess that
        happens to hold while the box contains everything. Touching the
        generated box is not a guess: the external domain is the volume the
        box bounds, and a volume bounded only by body faces is enclosed by
        the geometry.

        MEASURED on all six assembly fixtures at padding 1.5. Every one is a
        solid with internal voids, and every cut left exactly two volumes:
        the domain, with 12 faces of which 6 lie on the box and 6 are
        inherited body faces, and one sealed pocket whose faces are all
        inherited -- 126 on the finned heat sink, 147 on the lattice bracket,
        79 on the tube bundle, 111 on the turbine cascade, 34 on the
        centrifugal impeller. The drone quadcopter left six volumes: the
        domain, four three-faced pockets of 1.85e-06 m^3 and one of
        4.02e-04 m^3. The mass order and this classification agree on all six
        -- which is the point of measuring rather than assuming, because the
        case they disagree on is a domain the cut has split in two, and there
        the mass rule discards half the flow region without a word.

        Returns ``(domain, sealed)``, each a list of ``(mass, tag)``.
        """
        tolerance = max(diagonal, 1e-9) * 1e-6
        planes = [(axis, coordinate)
                  for axis in range(3)
                  for coordinate in (origin[axis], origin[axis] + span[axis])]

        def on_the_box(face):
            centre, _area = self.surface_signature(face)
            return any(abs(centre[axis] - coordinate) <= tolerance
                       for axis, coordinate in planes)

        domain, sealed = [], []
        for _dim, tag in volumes:
            faces = [int(face) for _face_dim, face
                     in self.gmsh.model.getBoundary([(3, int(tag))],
                                                    oriented=False)]
            entry = (float(self.gmsh.model.occ.getMass(3, int(tag))), int(tag))
            (domain if any(on_the_box(abs(face)) for face in faces)
             else sealed).append(entry)
        return domain, sealed

    def apply_cavity_policy(self, domain, sealed, policy):
        """Do what the case says about sealed cavities, and say it was done.

        Three answers, because a sealed pocket inside the geometry is a real
        region and which of the three is right is the user's question, not
        this runner's: ``discard`` meshes the external domain alone,
        ``keep`` meshes the pockets too as separate regions, and ``refuse``
        stops so the geometry can be looked at. Discarding stays the default
        -- an external-flow case that meshes an unreachable pocket hands the
        solver a disconnected region -- but it is now a recorded decision
        with an alternative, not the only thing the product can do.
        """
        self.ledger.record('gmsh/farfield/sealedCavities', policy, policy,
                           note=f'{_cavities(len(sealed))} found')
        if not sealed:
            return self.gmsh.model.getEntities(3)
        sizes = ', '.join(f'{mass:.3g} m^3' for mass, _tag in sealed)
        if policy == 'refuse':
            raise MeshFailure(
                f'{_cavities(len(sealed))} inside the geometry '
                f'{agreeing(len(sealed), "was", "were")} '
                f'left by the farfield cut ({sizes}), and this case asks to '
                'be told rather than have them decided. External flow does '
                'not reach them. Choose to discard them to mesh the outside '
                'only, or to keep them to mesh them as separate regions.')
        if policy == 'keep':
            self.warnings.append(
                f'{_cavities(len(sealed))} inside the geometry '
                f'{agreeing(len(sealed), "was", "were")} '
                'left by the farfield cut and kept, as this case asks: they '
                'mesh as regions disconnected from the external domain, so '
                f'the mesh has {len(domain) + len(sealed)} volumes. Their '
                f'volumes are {sizes}')
            self.ledger.record('gmsh/farfield/enabled.cavities',
                               len(sealed), len(sealed),
                               note='sealed internal cavities kept')
            return self.gmsh.model.getEntities(3)
        for _mass, tag in sealed:
            self.gmsh.model.occ.remove([(3, tag)], recursive=True)
        self.gmsh.model.occ.synchronize()
        self.warnings.append(
            f'{_cavities(len(sealed))} inside the geometry '
            f'{agreeing(len(sealed), "was", "were")} '
            'left by the farfield cut and discarded: external flow does not '
            'reach them, and meshing them would produce a second, '
            f'disconnected region. Their volumes were {sizes}')
        self.ledger.record('gmsh/farfield/enabled.cavities',
                           len(sealed), len(sealed),
                           note='sealed internal cavities discarded')
        return self.gmsh.model.getEntities(3)

    def surface_signature(self, tag):
        """``(centre of mass, area)``: what a face still is after a cut."""
        return (tuple(self.gmsh.model.occ.getCenterOfMass(2, int(tag))),
                float(self.gmsh.model.occ.getMass(2, int(tag))))

    @staticmethod
    def matching_surface(centre, area, signatures, diagonal):
        """The pre-cut face this one came from, or ``None`` if it is new.

        A cut does not move the faces it keeps, so the centre of mass and the
        area are the same to within the kernel's own tolerance. The tolerance
        is scaled by the model so it means the same thing on a 40 mm bracket
        and a 4 m wing.
        """
        tolerance = max(diagonal, 1e-9) * 1e-6
        for source, (source_centre, source_area) in signatures.items():
            if (math.dist(centre, source_centre) <= tolerance
                    and abs(area - source_area)
                    <= max(source_area, 1e-12) * 1e-6):
                return source
        return None

    def name_farfield_faces(self, box_faces, origin, span, diagonal):
        """Name the six sides of the generated box by the plane each lies on.

        ``far_field_xMin`` rather than ``farfield_xMin``: the publisher reads
        a patch's category out of its name, and ``far_field`` is the category
        it knows. Spelled the other way every side of the domain would publish
        as a wall.
        """
        tolerance = max(diagonal, 1e-9) * 1e-6
        planes = []
        for axis, name in enumerate('xyz'):
            planes.append((axis, origin[axis], f'far_field_{name}Min'))
            planes.append((axis, origin[axis] + span[axis],
                           f'far_field_{name}Max'))
        for tag, centre in box_faces:
            for axis, coordinate, name in planes:
                if abs(centre[axis] - coordinate) <= tolerance:
                    self.generated_names[int(tag)] = name
                    break
            else:
                # Not on a side of the box: an inner face the match missed.
                # Naming it far field would put a wall on the boundary list.
                self.warnings.append(
                    f'surface {tag} appeared during the farfield cut and lies '
                    'on no side of the box; it publishes unnamed')

    def resolve_scopes(self, surfaces, volumes=()):
        """Map prepared scope tokens onto Gmsh surface and volume tags.

        C31-04. Two routes, and the identity route wins wherever the job
        carries one. ``scopeEntities`` names entities by source, and
        :attr:`entity_tags` says which tags each source actually produced --
        followed through every topology change since. ``scopeSurfaces`` and
        ``scopeVolumes`` are the version-1 form: bare indices into one global
        tag list, which are only the right entity while a case holds a single
        imported file. MEASURED on ``duct.step`` + ``pipe.step`` prepared
        together: both regions carried volume index 0 and the pipe's three
        faces carried surface indices 0-2, the duct's numbers, so a control
        authored on the pipe reached the duct.

        C31-05. A scope that resolves to nothing is never simply skipped. A
        token from the prepared maps that no control mentions is a warning --
        those maps name every prepared group, and most groups are not scoped
        by anything. A token an *enabled* control authored stops the run, in
        :meth:`refuse_unresolved_scopes`.
        """
        surface_tags = [tag for _dim, tag in surfaces]
        volume_tags = [tag for _dim, tag in volumes]
        scoped = self.job.get('scopeEntities') or {}
        by_identity = bool(self.entity_tags) and bool(
            scoped.get('surfaces') or scoped.get('volumes'))
        if by_identity:
            self.scope_surfaces, missing = self._scopes_by_identity(
                scoped.get('surfaces') or {}, 'surface')
            self.scope_volumes, absent = self._scopes_by_identity(
                scoped.get('volumes') or {}, 'volume')
            self.unresolved_scopes.extend(missing + absent)
        else:
            mapping = self.job.get('scopeSurfaces') or {}
            # Plan 28 WP7. After a tessellated import the indices name the
            # solids as imported; the surfaces that exist now are their
            # pieces. Widening one authored entity onto the pieces it was cut
            # into is the mapping working, not a scope failing to resolve:
            # what C31-05 refuses is a scope that reaches *no* entity.
            if self.surface_origin:
                origins = self.import_surface_tags
                pieces: dict[int, list[int]] = {}
                for piece, origin in self.surface_origin.items():
                    pieces.setdefault(origin, []).append(piece)

                def expand(indices):
                    out = []
                    for index in indices:
                        if isinstance(index, int) and 0 <= index < len(origins):
                            out.extend(pieces.get(origins[index], []))
                    return out
            else:
                def expand(indices):
                    return [surface_tags[index] for index in indices
                            if isinstance(index, int)
                            and 0 <= index < len(surface_tags)]
            for token, indices in mapping.items():
                resolved = expand(indices)
                if resolved:
                    self.scope_surfaces[token] = resolved
                else:
                    self.unresolved_scopes.append(token)
            for token, indices in (self.job.get('scopeVolumes') or {}).items():
                resolved = [volume_tags[index] for index in indices
                            if isinstance(index, int)
                            and 0 <= index < len(volume_tags)]
                if resolved:
                    self.scope_volumes[token] = resolved
                else:
                    self.unresolved_scopes.append(token)
        # DP-58. This was inside the identity branch above, which is
        # reached only when the job carries a scope. A case with no scoped
        # control -- 19 of the 33 tessellated runs this repository has
        # published -- therefore threw away names the job was carrying for it.
        # Naming an entity and scoping a control to it are two different
        # questions about the same map.
        self._name_entities()
        if self.unresolved_scopes:
            self.warnings.append(self._unresolved_scope_sentence())
        self.refuse_unresolved_scopes()

    def _scopes_by_identity(self, mapping, kind):
        """``{token: tags}`` and the tokens that reached no entity at all."""
        placed: dict[str, list[int]] = {}
        missing: list[str] = []
        for token, identities in mapping.items():
            tags: list[int] = []
            for identity in identities or ():
                record = self.entity_tags.get(str(identity))
                if record is not None and record['kind'] == kind:
                    tags.extend(record['tags'])
            tags = sorted(set(tags))
            if tags:
                placed[str(token)] = tags
            else:
                missing.append(str(token))
        return placed, missing

    def _name_entities(self) -> None:
        """Gmsh tag -> the name the prepared revision gave that entity.

        C31-04. ``surfaceNames``/``volumeNames`` are keyed on a tag computed
        from a source-local index, which is the same collision as the scopes:
        MEASURED on duct + pipe, six names for nine surfaces, three of the
        duct's overwritten by the pipe's and surfaces 7-9 unnamed. Keyed on
        the entity identity there is one name per entity and it survives
        classification and the farfield cut with it.
        """
        names = self.job.get('entityNames') or {}
        for kind, table in (('surface', names.get('surfaces') or {}),
                            ('volume', names.get('volumes') or {})):
            target = (self.entity_surface_names if kind == 'surface'
                      else self.entity_volume_names)
            for identity, label in sorted(table.items()):
                record = self.entity_tags.get(str(identity))
                if record is None or record['kind'] != kind or not label:
                    continue
                for tag in record['tags']:
                    # DP-410. Two identities can share a tag: the duplicate
                    # fusion makes one face out of two, and both prepared
                    # names then belong to it. `target[tag] = label` alone
                    # let whichever came last win, which is a patch name
                    # decided by dictionary order. The first in sorted order
                    # publishes and the rest are kept as names of the same
                    # faces.
                    held = target.get(int(tag))
                    if held is None:
                        target[int(tag)] = str(label)
                    elif held != str(label) and kind == 'surface':
                        merged = self.merged_surface_names.setdefault(
                            int(tag), [])
                        if str(label) not in merged:
                            merged.append(str(label))
        self._announce_merged_names()
        self._name_unclaimed_volumes()

    def _name_unclaimed_volumes(self) -> None:
        """The volumes a source made beyond the one its region claimed.

        DP-423. A surface that closes into more than one volume still gets a
        single region record, so only ``{source}:volume:0`` is ever named and
        every other volume of that file fell through to ``volume_<tag>`` --
        134 of 140 zones across six meshes, each called after a Gmsh tag
        number with no relation to the model.

        Nothing about those volumes is unknown here. The import recorded
        which file produced each one and in what order, and the job carries
        what that file's single region is called, so the name is that name
        with the volume's own position after it. Volume ``0`` is skipped: the
        region already named it, and renaming it would move a zone name that
        meshes already carry.
        """
        fallback = self.job.get('volumeFallback') or {}
        if not fallback:
            return
        for identity, record in self.entity_tags.items():
            if record.get('kind') != 'volume':
                continue
            source, _, local = str(identity).rpartition(':volume:')
            if not source or not local.isdigit() or int(local) == 0:
                continue
            label = str(fallback.get(source) or '').strip()
            if not label:
                continue
            for tag in record['tags']:
                # Only where nothing else named it. An explicit name from
                # `entityNames` is a name the user's own tree gave this
                # volume and outranks a position.
                self.entity_volume_names.setdefault(
                    int(tag), f'{label}_{int(local) + 1}')

    def authored_scopes(self) -> dict:
        """Scope token -> ``[(what named it, the entity kind it needs)]``.

        Only the intent is walked. The derivation drops every disabled row
        before the job is written, so a token reached here belongs to a
        control the user turned on -- unlike the job's scope maps, which name
        every prepared group whether or not anything scopes to it.
        """
        intent = self.job.get('intent') or {}
        authored: dict[str, list[tuple[str, str]]] = {}

        def claim(token, label, kind):
            token = str(token or '').strip()
            if token:
                authored.setdefault(token, []).append((label, kind))

        for row in (intent.get('sizeFields') or {}).get('fields') or ():
            # Plan 29 WP8: a row naming its own Gmsh surface tags never
            # consults a prepared scope, so an unresolvable token on it is
            # not what stops the run.
            if row.get('surfaces'):
                continue
            claim(row.get('scopeToken'), f'size field {row.get("name")!r}',
                  'any')
        for row in intent.get('curveControls') or ():
            claim(row.get('scopeToken'),
                  f'curve control {row.get("name")!r}', 'surface')
        for row in intent.get('volumeControls') or ():
            claim(row.get('scopeToken'),
                  f'volume control {row.get("name")!r}', 'volume')
        for pair in (intent.get('periodic') or {}).get('pairs') or ():
            name = pair.get('name')
            claim(pair.get('masterScope'),
                  f'periodic pair {name!r} (master)', 'surface')
            claim(pair.get('slaveScope'),
                  f'periodic pair {name!r} (slave)', 'surface')
        return authored

    def _unresolved_scope_sentence(self) -> str:
        """Why each scope reached nothing, where the geometry says why.

        DP-84. "matched no imported entity and were skipped" reads as a
        wiring fault, and on a domain with a body standing in it the most
        common cause is not a fault at all: the scope names a shell the
        nesting made a void, and a void is a hole, so it never becomes a
        volume for a scope to reach. MEASURED on gmsh `drone_quadcopter`,
        where `region-bdfdd4053357f840b89899c8` names the drone body -- five
        shells, every one of them `void` -- and the run meshed correctly
        while reporting a sentence that says something went wrong.

        Saying which is which matters because the two want opposite actions.
        A void wants the control moved to the fluid; a genuinely missing
        entity wants the geometry looked at.
        """
        roles = {}
        record = self.statistics.get('tessellatedImport')
        for shell in (record or {}).get('shells') or ():
            name = str(shell.get('name') or '')
            key = name.split('#', 1)[0]
            roles.setdefault(key, set()).add(str(shell.get('role') or ''))
        scoped = (self.job.get('scopeEntities') or {}).get('volumes') or {}
        voided, plain = [], []
        for token in sorted(set(self.unresolved_scopes)):
            keys = {str(item).split(':', 1)[0]
                    for item in scoped.get(token) or ()}
            seen = set()
            for key in keys:
                seen |= roles.get(key, set())
            if seen and seen <= {'void'}:
                voided.append(token)
            else:
                plain.append(token)
        parts = []
        if plain:
            parts.append('these scopes matched no imported entity and were '
                         'skipped: ' + ', '.join(self._scope_holder(token)
                                                 for token in plain))
        if voided:
            parts.append(
                'these scopes name a shell this domain treats as a void, so '
                'there is no volume for them to reach and nothing scoped to '
                'them was applied — move the control to the fluid volume if '
                'it was meant for the space around the body: '
                + ', '.join(voided))
        return '; '.join(parts)

    def _scope_holder(self, token) -> str:
        """What a user can find a scope token under: its control, its patch.

        DP-547. G6 (audit 0924) warned ``...were skipped:
        640cae55-a49b-4f43-98f5-51a8d5fade54``. That token was the prepared
        patch ``body1_face4``, which no control scoped at all -- the job's
        scope maps name every prepared group -- and nothing on screen let the
        user find that out. The bare token is kept only when the job has no
        word for it.
        """
        token = str(token)
        holders = [label for label, _kind
                   in self.authored_scopes().get(token, ())]
        # DP-548. A pair's scope is not a control the run can apply, so it
        # never stops the run -- but it is what the user authored the token
        # on, and the name they can look for.
        for pair in self.job.get('interfacePairs') or ():
            for side in ('master', 'slave'):
                if str(pair.get(f'{side}Scope') or '') == token:
                    holders.append(
                        f'interface pair {pair.get("name")!r} ({side})')
        scoped = self.job.get('scopeEntities') or {}
        names = self.job.get('entityNames') or {}
        labels = []
        for kind in ('surfaces', 'volumes'):
            for identity in (scoped.get(kind) or {}).get(token) or ():
                label = (names.get(kind) or {}).get(identity)
                if label and label not in labels:
                    labels.append(str(label))
        patch = ', '.join(labels)
        if holders:
            return f'{", ".join(holders)} on {patch or token}'
        if patch:
            return f'{patch} (a prepared patch no enabled control uses)'
        return token

    def refuse_unresolved_scopes(self) -> None:
        """Stop the run when an enabled control's scope reached no entity.

        C31-05. This used to append the token to ``unresolved_scopes``, warn
        once, and mesh anyway: every consumer then found an empty scope, wrote
        its own "was skipped" warning, and the run finished successfully with
        the control absent from the mesh. MEASURED in the job the audit built,
        the whole record of it was one line in ``warnings`` naming a UUID.

        Refusing is the right end of that choice rather than a louder report.
        The alternative -- returning the unresolved list to the caller to act
        on -- is what the code already did, and no caller acted: the run
        published as a candidate, its ledger recorded the control as not
        applied, and the mesh looked like the one the user asked for. A mesh
        that silently omits a refinement the user enabled is worse than no
        mesh, because nothing downstream can tell the difference.

        A scope the prepared geometry carries but no control uses is *not*
        this: it stays a warning. Neither is a scope that widened onto several
        classified pieces, which is the mapping working.
        """
        blocked = []
        for token, claims in self.authored_scopes().items():
            for label, kind in claims:
                if kind in ('any', 'surface') and token in self.scope_surfaces:
                    continue
                if kind in ('any', 'volume') and token in self.scope_volumes:
                    continue
                blocked.append((label, token, kind))
        if not blocked:
            return
        lines = []
        for label, token, kind in blocked:
            lines.append(f'{label} is scoped to {self._scope_description(token, kind)}')
        raise MeshFailure(
            'these enabled controls name geometry this run does not contain, '
            'so the mesh would silently be missing them:\n  '
            + '\n  '.join(lines)
            + '\nRe-prepare the geometry, or turn the control off, and run '
              'again.')

    def _scope_description(self, token, kind) -> str:
        """What a user can act on: the entity names behind a scope token."""
        scoped = self.job.get('scopeEntities') or {}
        names = self.job.get('entityNames') or {}
        identities = list((scoped.get('surfaces') or {}).get(token) or ())
        identities += list((scoped.get('volumes') or {}).get(token) or ())
        labels = []
        for identity in identities:
            label = ((names.get('surfaces') or {}).get(identity)
                     or (names.get('volumes') or {}).get(identity))
            labels.append(f'{label} ({identity})' if label else str(identity))
        consumed = sorted({
            change['stage'] for change in self.topology_changes
            for identity in identities
            if identity in change['entities']
            and not change['entities'][identity]['to']})
        if not labels:
            labels = [f'the prepared scope {token}']
        detail = ', '.join(labels)
        if consumed:
            return f'{detail}, which {" and ".join(consumed)} consumed'
        return f'{detail}, which no imported {kind} carries'

    def apply_sizing(self):
        sizing = self.intent.get('sizing') or {}
        algorithms = self.intent.get('algorithms') or {}
        self.set_number('Mesh.MeshSizeMax', sizing.get('targetSize'),
                        control='gmsh/globalSizing/targetSize')
        self.set_number('Mesh.MeshSizeMin', sizing.get('minimumSize'),
                        control='gmsh/globalSizing/minimumSize')
        self.set_number('Mesh.MeshSizeFactor', sizing.get('sizeFactor', 1.0),
                        control='gmsh/globalSizing/sizeFactor')
        self.set_number('Mesh.MeshSizeFromCurvature',
                        sizing.get('fromCurvature', 12),
                        control='gmsh/globalSizing/fromCurvature')
        self.set_number('Mesh.MeshSizeFromPoints',
                        int(bool(sizing.get('fromPoints', True))),
                        control='gmsh/globalSizing/fromPoints')
        self.set_number('Mesh.MeshSizeExtendFromBoundary',
                        int(bool(sizing.get('extendFromBoundary', True))),
                        control='gmsh/globalSizing/extendFromBoundary')
        # Plan 31 FC-B. The curve-discretisation floors. These decide whether a
        # small hole gets three elements or twelve, whatever the target size
        # says, and nothing but Mesh.MeshSizeFromCurvature used to reach the
        # 1D pass at all. Every default is Gmsh 4.15.2's own.
        self.set_number('Mesh.MinimumCirclePoints',
                        int(sizing.get('minimumCirclePoints', 7) or 7),
                        control='gmsh/globalSizing/minimumCirclePoints')
        self.set_number('Mesh.MinimumCurvePoints',
                        int(sizing.get('minimumCurvePoints', 3) or 3),
                        control='gmsh/globalSizing/minimumCurvePoints')
        self.set_number('Mesh.MinimumElementsPerTwoPi',
                        int(sizing.get('minimumElementsPerTwoPi', 0) or 0),
                        control='gmsh/globalSizing/minimumElementsPerTwoPi')
        self.set_number('Mesh.Algorithm', algorithms.get('surfaceCode', 6),
                        control='gmsh/algorithms/surface')
        self.set_number('Mesh.Algorithm3D', algorithms.get('volumeCode', 1),
                        control='gmsh/algorithms/volume')
        # Plan 31 FC-B. Retry a surface that defeats the chosen algorithm with
        # another one. Gmsh 4.15.2 already defaults both of these on (1 and
        # 10), so this control does not switch the behaviour on so much as
        # make it sayable -- and `measure_algorithms` below reads the
        # algorithm each surface was finally meshed with out of Gmsh's own
        # log, so a fallback is recorded rather than passing for the
        # algorithm that was asked for.
        fallback = bool(algorithms.get('fallback', True))
        self.set_number('Mesh.AlgorithmSwitchOnFailure', int(fallback),
                        control='gmsh/algorithms/algorithmFallback')
        self.set_number('Mesh.MaxRetries', ALGORITHM_RETRIES if fallback else 0,
                        control='gmsh/algorithms/algorithmFallback.retries')
        # MEASURED on duct.step: `Mesh.SubdivisionAlgorithm` 1, whose Gmsh
        # name is "all quadrangles", left the mesh exactly as it found it --
        # 682 triangles and 1044 tetrahedra, identical to no subdivision, and
        # no error. 3 (barycentric) split every tetrahedron into four without
        # changing the family. Only 0 and 2 do what their names say, so a job
        # carrying anything else is meshed as tetrahedra with the reason said
        # out loud rather than silently meshed as if the request had landed.
        subdivision = int(algorithms.get('subdivisionAlgorithm', 0) or 0)
        if subdivision not in TESTED_SUBDIVISIONS:
            self.warnings.append(
                f'subdivision algorithm {subdivision} was asked for and is '
                'not one this product has qualified; Gmsh accepts it and '
                'leaves the element family unchanged, so the run meshed '
                'tetrahedra. Choose a hexahedral cell shape for hexahedra')
            self.ledger.record('gmsh/algorithms/cellShape', subdivision, 0,
                               applied=False,
                               note='only 0 and 2 change the element family')
            subdivision = 0
        self.set_number('Mesh.SubdivisionAlgorithm', subdivision,
                        control='gmsh/algorithms/cellShape')
        # Plan 31 FC-C. Barycentric subdivision asks the same option for a
        # different thing. MEASURED on a one-metre box: the boundary came back
        # with the same triangles it had and the volume went from 415
        # tetrahedra to 1660, four times as many and every one still a
        # tetrahedron. So it is a refinement, and it is written here rather
        # than beside the cell shape because it must lose to it: one option
        # holds one value, and the mesh a user asking for both would get is
        # tetrahedra, not the hexahedra they chose.
        if sizing.get('barycentricRefinement'):
            if subdivision:
                reason = (
                    'barycentric refinement and a hexahedral cell shape are '
                    'the same Gmsh setting, so only one of them can hold; the '
                    'mesh was built as hexahedra, because refining '
                    'barycentrically would have produced four tetrahedra per '
                    'cell instead. Refine with a size field to keep the '
                    'hexahedra')
                self.warnings.append(reason)
                self.ledger.record('gmsh/globalSizing/barycentricRefinement',
                                   True, False, applied=False, note=reason)
            else:
                self.set_number(
                    'Mesh.SubdivisionAlgorithm', BARYCENTRIC_SUBDIVISION,
                    control='gmsh/globalSizing/barycentricRefinement')
                self.barycentric_refinement = True

        quality = self.intent.get('quality') or {}
        optimize = bool(quality.get('optimize', True))
        self.set_number('Mesh.Optimize', int(optimize),
                        control='gmsh/optimization/optimize')
        # Plan 30 WP12. `Mesh.OptimizeNetgen` is a boolean. The pass count
        # used to be written into it, so "three passes" and "one pass" set the
        # same flag to different truthy numbers and the explicit loop in
        # generate() then ran on top of whatever that did.
        #
        # DP-620 (field audit 0924 gmsh-generate-export D3). "Optimize" is the
        # switch the page offers for optimisation as a whole, and it only ever
        # reached `Mesh.Optimize`: the Netgen flag, on by default, still ran
        # Netgen after the volume pass. MEASURED on a box minus a sphere
        # through this runner: Optimize off still logged "Optimizing mesh
        # (Netgen)" and three explicit passes. Off now means off, and the
        # ledger says the Netgen request was not applied and why.
        netgen = bool(quality.get('netgen', True))
        if netgen and not optimize:
            self.gmsh.option.setNumber('Mesh.OptimizeNetgen', 0.0)
            self.ledger.record(
                'gmsh/optimization/netgen', 1,
                self.gmsh.option.getNumber('Mesh.OptimizeNetgen'),
                applied=False,
                note='Optimize is off, so the Netgen optimiser was not run')
        else:
            self.set_number('Mesh.OptimizeNetgen', int(netgen),
                            control='gmsh/optimization/netgen')
        self.set_number('Mesh.Smoothing', int(quality.get('smoothing', 1) or 0),
                        control='gmsh/optimization/smoothing')
        self.set_number('Mesh.OptimizeThreshold',
                        float(quality.get('optimizeThreshold', 0.3)),
                        control='gmsh/optimization/optimizeThreshold')
        self.set_number('Mesh.QualityType',
                        QUALITY_MEASURE.get(quality.get('qualityType', 'sicn'), 0),
                        control='gmsh/optimization/qualityType')
        # DP-623 (field audit 0924 gmsh-generate-export D7). The point
        # perturbation Delaunay uses on large near-flat faces decides whether
        # such a face meshes at all, and nothing in the product set it or
        # said what it was. It is not a FoamMesh setting; the value Gmsh used
        # is read back and recorded so a run that failed there can be read.
        self.ledger.record(
            'gmsh/default/randomFactor', None,
            self.gmsh.option.getNumber('Mesh.RandomFactor'),
            note='Gmsh default (Mesh.RandomFactor), not set by FoamMesh')

    def apply_recombination(self):
        """Quads on the surfaces, and the cell family that follows from them.

        Plan 30 WP12. ``Mesh.RecombineAll`` alone is not enough: it asks the
        2D pass to recombine, but a surface the mesher decides against is left
        as triangles and nothing says so. Each surface is therefore marked
        explicitly with ``setRecombine`` and the count is recorded, so a run
        that recombined nine faces of ten is visible instead of being reported
        as a quad mesh.

        The derivation has already refused this for every target but SU2, so
        this method only ever runs on the route whose reader accepts the
        result.
        """
        algorithms = self.intent.get('algorithms') or {}
        if not algorithms.get('recombine'):
            return
        gmsh = self.gmsh
        self.set_number('Mesh.RecombineAll', 1,
                        control='gmsh/algorithms/recombine')
        # Which recombiner, chosen rather than assumed. MEASURED on
        # duct.step at a 0.04 m target: 0 gave 306 quadrangles and left 70
        # triangles, 1 gave 356 and left none, 2 gave 460, 3 gave 408. Four
        # values, four different meshes -- and 4 read back as 4 and then
        # meshed nothing at all, surface and volume both empty, without
        # raising, while 9 was clamped to 0 in silence. So anything outside
        # the qualified four is refused here with its reason rather than
        # handed to Gmsh to see what happens.
        recombiner = int(algorithms.get('recombinationCode', 1) or 0)
        if recombiner not in TESTED_RECOMBINERS:
            self.warnings.append(
                f'recombination algorithm {recombiner} was asked for and is '
                'not one this product has qualified; Gmsh takes the value and '
                'measured meshes came back empty, so blossom was used instead')
            self.ledger.record('gmsh/algorithms/recombinationAlgorithm',
                               recombiner, 1, applied=False,
                               note='qualified values are '
                                    + ', '.join(str(item) for item
                                                in sorted(TESTED_RECOMBINERS)))
            recombiner = 1
        self.set_number('Mesh.RecombinationAlgorithm', recombiner,
                        control='gmsh/algorithms/recombinationAlgorithm')
        surfaces = [int(tag) for _dim, tag in gmsh.model.getEntities(2)]
        touched = 0
        for tag in surfaces:
            try:
                gmsh.model.mesh.setRecombine(2, tag)
                touched += 1
            except Exception as error:
                self.warnings.append(
                    f'surface {tag} could not be marked for recombination: '
                    f'{error}')
        self.ledger.record('gmsh/algorithms/recombine.surfaces',
                           len(surfaces), touched,
                           applied=bool(touched),
                           note=f'{touched:,} of '
                                f'{count_text(len(surfaces), "surface")} '
                                'marked for recombination')
        self.statistics['recombination'] = {
            'surfaces': len(surfaces), 'recombined': touched,
            'algorithm': recombiner}

    def apply_structuring_options(self):
        """The one structuring setting that is a Gmsh option.

        Plan 31 FC-C. ``Mesh.TransfiniteTri`` decides how a three-sided
        transfinite face is filled, and it is read at meshing time, so it is
        set here rather than beside ``setTransfiniteAutomatic`` -- a face made
        transfinite by a curve control obeys it too, with automatic
        structuring switched off.

        MEASURED on Gmsh 4.15.2: a triangle carrying nine nodes on each side
        came back as 120 triangles with this off and 64 with it on, and 64 is
        8x8 -- the count a structured side of eight elements owes. At five
        nodes a side the pair reads 28 and 16, and 16 is 4x4. What the option
        produced is counted out of the mesh either way; nothing here reads it
        back.
        """
        structuring = self.intent.get('structuring') or {}
        if not structuring.get('transfiniteTri'):
            return
        self.set_number('Mesh.TransfiniteTri', 1,
                        control='gmsh/structuring/transfiniteTri')

    def apply_automatic_structuring(self):
        """Ask Gmsh to structure every volume it can, and remember which.

        Plan 31 FC-C. ``setTransfiniteAutomatic`` walks the model, finds the
        volumes whose faces it can interpolate between, constrains them, and
        recombines them. It returns nothing and raises nothing when it takes
        some volumes and leaves others.

        MEASURED on Gmsh 4.15.2. A unit box came back as 125 hexahedra behind
        150 quadrilateral boundary faces, where the same box is 415 tetrahedra
        behind 260 triangles without it. A model holding that box beside a
        sphere came back as 125 hexahedra *and* 207 tetrahedra -- one volume
        of two structured, in one mesh, silently. So the volumes are recorded
        here and counted out of the mesh in :meth:`measure_structuring`; this
        method never claims the model was structured.
        """
        structuring = self.intent.get('structuring') or {}
        if not structuring.get('automatic'):
            return
        volumes = [int(tag) for _dim, tag in self.gmsh.model.getEntities(3)]
        if not volumes:
            reason = ('automatic structuring works volume by volume and this '
                      'model has no volumes yet')
            self.warnings.append(reason)
            self.ledger.record('gmsh/structuring/automatic', True, False,
                               applied=False, note=reason)
            return
        try:
            self.gmsh.model.mesh.setTransfiniteAutomatic()
        except Exception as error:                           # noqa: BLE001
            reason = f'Gmsh would not structure this model automatically ({error})'
            self.warnings.append(reason)
            self.ledger.record('gmsh/structuring/automatic', True, False,
                               applied=False, note=reason)
            return
        self.automatic_structuring = {'volumes': volumes}

    def measure_structuring(self):
        """How many volumes automatic structuring actually took.

        The request is one flag; the result is one answer per volume, and the
        two are recorded separately. MEASURED: a volume Gmsh structured comes
        back with no tetrahedra in it -- 125 hexahedra for a unit box -- and
        one it declined comes back as tetrahedra, 209 of them for a sphere in
        the same run. That difference is the reading; the option cannot be
        asked, because there is no option.
        """
        if not self.automatic_structuring:
            return
        gmsh = self.gmsh
        families, structured, unstructured = {}, [], []
        for tag in self.automatic_structuring['volumes']:
            try:
                types, groups, _n = gmsh.model.mesh.getElements(3, int(tag))
            except Exception:                                # noqa: BLE001
                # The volume is gone -- excluded, or consumed by the farfield
                # cut. It was not left unstructured; it was not there.
                continue
            found: dict = {}
            for etype, group in zip(types, groups):
                name = gmsh.model.mesh.getElementProperties(etype)[0]
                found[name] = found.get(name, 0) + len(group)
            families[str(tag)] = found
            went = bool(found) and not any(
                'Tetrahedron' in name for name in found)
            (structured if went else unstructured).append(int(tag))
        total = len(families)
        whole = bool(total) and not unstructured
        self.statistics['automaticStructuring'] = {
            'requestedVolumes': total,
            'structuredVolumes': sorted(structured),
            'unstructuredVolumes': sorted(unstructured),
            'structured': whole,
            'families': families,
        }
        if unstructured:
            # Reporting this mesh as structured is the failure the control was
            # written to avoid.
            self.warnings.append(
                f'automatic structuring took {len(structured):,} of '
                f'{count_text(total, "volume")}; '
                f'{agreeing(len(unstructured), "volume")} '
                + ', '.join(str(tag) for tag in sorted(unstructured))
                + ' came back as tetrahedra, so the mesh is partly structured '
                  'and is not reported as a structured mesh')
        self.ledger.record(
            'gmsh/structuring/automatic', total, len(structured),
            applied=bool(structured),
            note='volumes asked for against volumes that came back with no '
                 'tetrahedra in them')

    def apply_size_fields(self):
        """Build the size fields the plan asks for, wiring checked first.

        Plan 31 CP-08 item 1. The wiring used to be written here field by
        field, and nothing checked it: a ``restrict`` row pointed at a Constant
        that was never given a value it could return, so the row was inert and
        the run reported it as applied. The graph in ``field_graph`` is built
        and validated before a single Gmsh field exists -- every reference must
        name a node in the plan, the references must not form a cycle, and no
        node may feed nothing -- and this method only walks it.
        """
        plan = (self.intent.get('sizeFields') or {}).get('fields') or []
        if not plan:
            return
        try:
            graph = build_graph(plan)
        except FieldGraphError as error:
            raise MeshFailure(
                f'the size fields cannot be wired into a mesh size: {error}'
            ) from error

        field = self.gmsh.model.mesh.field

        # -- resolve every scope first ------------------------------------- #
        # A row whose scope reaches no geometry is dropped whole: building the
        # Distance but not the Threshold would leave Gmsh holding a field that
        # answers for nowhere.
        resolved: dict = {}
        dropped: dict = {}
        for node in graph.nodes:
            if not node.scopes:
                continue
            found: dict = {}
            for option, dimension, token, explicit in node.scopes:
                tags = [int(tag) for tag in explicit]
                if not tags:
                    source = (self.scope_surfaces if dimension == 'surfaces'
                              else self.scope_volumes)
                    tags = [int(tag) for tag in (source.get(token) or ())]
                if tags:
                    found[option] = tags
            resolved[node.node_id] = found
            if node.requires_scope and not found:
                dropped[node.row] = token

        if dropped:
            # Plan 33 W-G1 (FIELD-06). This used to warn once and mesh
            # anyway: the row was dropped whole, the ledger recorded it as
            # not applied, and the run published a mesh that looked like the
            # one the user asked for. Nothing downstream could tell the
            # difference, which is what made it worse than no mesh at all.
            # The refusal the enabled-scope check already makes for a control
            # the job knows about (C31-05), made here for the one shape that
            # reaches Gmsh without passing through it.
            for row_name, token in sorted(dropped.items()):
                self.ledger.record(f'sizeField:{row_name}', token, None,
                                   applied=False,
                                   note='scope did not resolve')
            raise MeshFailure(
                'these size fields name geometry this run does not contain, '
                'so the mesh would silently be missing them:\n  '
                + '\n  '.join(f'{row_name!r} is scoped to {token!r}'
                              for row_name, token in sorted(dropped.items()))
                + '\nRe-prepare the geometry, or turn the field off, and run '
                  'again.')

        # -- build in dependency order ------------------------------------- #
        index = graph.by_id()
        tags: dict = {}
        created: list[int] = []
        for node_id in graph.order:
            node = index[node_id]
            if node.row in dropped:
                continue
            try:
                tag = field.add(node.kind)
                for option, value in node.numbers:
                    field.setNumber(tag, option, float(value))
                for option, value in node.strings:
                    field.setString(tag, option, str(value))
                for option, source in node.inputs:
                    field.setNumber(tag, option, tags[source])
                for option, sources in node.input_lists:
                    field.setNumbers(tag, option,
                                     [float(tags[item]) for item in sources])
                for option, entities in (resolved.get(node_id) or {}).items():
                    field.setNumbers(tag, option,
                                     [float(item) for item in entities])
            except Exception as error:
                raise MeshFailure(
                    f'size field {node.row!r} ({node.kind}) was rejected by '
                    f'Gmsh: {error}') from error
            tags[node_id] = tag
            if node.output:
                created.append(tag)

        for row in plan:
            name = str(row.get('name') or '')
            if name in dropped:
                continue
            self.ledger.record(f'sizeField:{name}', row.get('fieldType'),
                               row.get('fieldType'), note='field created')
            self.background_sizes.append(
                (name, None if row.get('fieldType') == 'math_eval'
                 else float(row.get('sizeInside') or 0.0)))

        self.background_fields.extend(created)
        # DP-501. The graph's `scopes` carry the tags a row named itself and
        # the prepared token it was scoped to; a row scoped by token names no
        # tags of its own, so its entry read `SurfacesList: []` while Gmsh was
        # handed the resolved surface. MEASURED on G4: the face0 Distance was
        # given native tag 1 and the result said []. `entities` is what each
        # node was actually given, keyed by the Gmsh option.
        report = graph.to_dict()
        for entry in report['nodes']:
            entry['entities'] = {
                option: [int(item) for item in entities]
                for option, entities
                in (resolved.get(entry['nodeId']) or {}).items()}
        self.statistics['sizeFields'] = {
            'created': len(created),
            'gmshFields': len(tags),
            'skipped': len(dropped),
            'graph': report,
        }

    def assemble_background_field(self):
        """Combine every size source into one background field.

        Gmsh keeps a single background mesh field, so size fields and
        per-volume sizes have to be merged here; setting one after the other
        would silently discard whichever went first.
        """
        if not self.background_fields:
            return
        field = self.gmsh.model.mesh.field
        # Plan 30 WP12. Which way the combination runs is a control now. Min
        # means the finest request at any point wins, which is what shipped;
        # Max is the Gmsh Max field, and it is what a row asking to *coarsen*
        # a region needs -- under Min every other field outvoted it.
        sizing = self.intent.get('sizing') or {}
        combiner = str(sizing.get('fieldCombiner') or 'min').lower()
        native = FIELD_COMBINERS.get(combiner, 'Min')
        combined = field.add(native)
        field.setNumbers(combined, 'FieldsList',
                         [float(tag) for tag in self.background_fields])
        field.setAsBackgroundMesh(combined)
        self.ledger.record(
            'gmsh/globalSizing/fieldCombiner', combiner, combiner,
            note=f'{native} of '
                 f'{count_text(len(self.background_fields), "field")}')
        # Plan 29 WP8. This used to force MeshSizeExtendFromBoundary and
        # MeshSizeFromPoints to 0 here, behind the ledger: both had already
        # been recorded as applied, so the run certified a value the mesher did
        # not have and the user's own choice vanished without a word. The
        # interaction is real -- boundary and point sizes can outvote a
        # background field -- so it is reported instead of enforced. The ledger
        # is the last word.
        outvoting = [name for name, key in
                     (('extend from boundary', 'extendFromBoundary'),
                      ('sizes from points', 'fromPoints'))
                     if bool(sizing.get(key, True))]
        # DP-504. Those sources can only outvote a row asking for a larger
        # size than the smallest they can give. Without curvature sizing or a
        # sized curve that is the global target, so a row at or below it is
        # honoured whatever they say; with them it is the finest of those.
        # The warning used to fire on every run with any size source and
        # name none of them -- G1 had two volume controls and no size field,
        # and was told a "size field" might look ignored.
        target = float(sizing.get('targetSize') or 0.0)
        floor, finer = self.competing_size_floor(target)
        outvotable = [name for name, size in self.background_sizes
                      if size is None or floor <= 0
                      or size > floor * (1.0 + 1e-9)]
        if outvoting and outvotable:
            reason = (f' ({"; ".join(finer)})' if finer else '')
            self.warnings.append(
                'the background size field shares the mesh with '
                + ' and '.join(outvoting)
                + f'; where those give a smaller size they win{reason}, so '
                + ', '.join(repr(name) for name in outvotable)
                + ' may come out finer than asked — turn them off on Global '
                  'sizing if that row looks ignored')
        self.statistics['backgroundField'] = {
            'sources': len(self.background_fields),
            'competingSizeSources': outvoting,
            'competingSizeFloor': floor,
            'outvotable': outvotable}

    def competing_size_floor(self, target):
        """The smallest size boundary and point sizes can give, and why.

        DP-504. `(floor, reasons)`: the global target, lowered to the minimum
        size when curvature sizing is on and to a sized curve's local size.
        """
        sizing = self.intent.get('sizing') or {}
        floor, reasons = target, []
        if float(sizing.get('fromCurvature', 12) or 0) > 0:
            minimum = float(sizing.get('minimumSize') or 0.0)
            if target <= 0 or minimum < floor:
                floor = minimum
                reasons.append(f'curvature sizing can go down to {minimum:g} m')
        for row in self.intent.get('curveControls') or ():
            if row.get('mode') == 'transfinite':
                continue
            try:
                local = float(row.get('localSize'))
            except (TypeError, ValueError):
                continue
            if local > 0 and (floor <= 0 or local < floor):
                floor = local
                reasons.append(f'curve control {row.get("name")!r} sets '
                               f'{local:g} m')
        return floor, reasons

    def apply_curve_controls(self):
        """Put node counts on curves, then make the asked-for surfaces structured.

        Two passes, and the order is a prerequisite rather than tidiness.
        MEASURED on Gmsh 4.15.2: a transfinite surface is only honoured when
        every one of its bounding curves already carries a count and the
        opposing sides agree. Applying each row's surface request inside that
        row's own iteration meant a later row could change a shared curve and
        turn an already-accepted surface unstructured -- silently, because
        Gmsh does not complain (see :meth:`structured_surface_refusal`).
        """
        rows = self.intent.get('curveControls') or []
        if not rows:
            return
        applied = structured = 0
        pending: list[tuple] = []
        # DP-611 (field audit 0924 gmsh-sizing D2). Rows arrive highest
        # priority first, and each setTransfiniteCurve / setSize overwrote the
        # one before, so on a curve two rows share -- every edge between two
        # adjacent faces -- the LOWEST priority won. MEASURED (audit probe,
        # Gmsh 4.15.2): 21 nodes at priority 100 then 6 at priority 0 left 6.
        # A curve (and, in size mode, an end point) now belongs to the first
        # row that reaches it in priority order; a lower row skips it and
        # says so. Sorted here too, so an older job's row order cannot matter.
        rows = sorted(rows, key=lambda item: -int(item.get('order') or 0))
        claimed_curves: dict[int, str] = {}
        claimed_points: dict[int, str] = {}
        # DP-612 (field audit 0924 gmsh-sizing D4). A local size is a size on
        # the curve's end points, and Gmsh reads point sizes only while
        # Mesh.MeshSizeFromPoints is on. With "From points" off the setSize
        # call changed nothing and the row was still logged as applied.
        from_points = bool((self.intent.get('sizing') or {}).get(
            'fromPoints', True))
        for row in rows:
            if row['mode'] == 'size' and not from_points:
                self.warnings.append(
                    f'curve control {row["name"]!r} sets a local size, which '
                    'Gmsh reads only while "From points" is ticked on the '
                    'Global sizing page; it is off, so the row was skipped')
                self.ledger.record(f'curveControl:{row["name"]}', 'size',
                                   None, applied=False,
                                   note='"From points" is off, so Gmsh '
                                        'ignores point sizes')
                continue
            surfaces = self.scope_surfaces.get(row['scopeToken'])
            if not surfaces:
                self.warnings.append(
                    f'curve control {row["name"]!r} has no resolvable scope '
                    'and was skipped')
                self.ledger.record(f'curveControl:{row["name"]}',
                                   row['scopeToken'], None, applied=False,
                                   note='scope did not resolve')
                continue
            curves: set[int] = set()
            for tag in surfaces:
                _up, down = self.gmsh.model.getAdjacencies(2, tag)
                curves.update(int(item) for item in down)
            # F-26: `setTransfiniteCurve` counts *nodes*, the control counts
            # elements, and the two used to be the same number -- so a curve
            # asked for ten segments came back with nine. The derivation adds
            # the one; older jobs without the key are corrected here.
            nodes = int(row.get('nodes') or int(row['segments']) + 1)
            coefficient = self.grading_coefficient(row)
            kept_by: dict[int, str] = {curve: claimed_curves[curve]
                                       for curve in curves
                                       if curve in claimed_curves}
            mine = sorted(curves - set(kept_by))
            for curve in mine:
                claimed_curves[curve] = row['name']
                if row['mode'] == 'transfinite':
                    self.gmsh.model.mesh.setTransfiniteCurve(
                        curve, nodes, row['law'], coefficient)
                    self.transfinite_curves[curve] = {
                        'control': row['name'], 'nodes': nodes,
                        'law': row['law'], 'coefficient': coefficient}
                else:
                    points = []
                    for _d, point in self.gmsh.model.getBoundary(
                            [(1, curve)], oriented=False):
                        point = int(point)
                        owner = claimed_points.setdefault(point, row['name'])
                        if owner == row['name']:
                            points.append((0, point))
                    if points:
                        self.gmsh.model.mesh.setSize(
                            points, float(row['localSize']))
                applied += 1
            note = (f'{count_text(len(mine), "curve")}, '
                    f'{count_text(nodes, "node")} each'
                    if row['mode'] == 'transfinite'
                    else count_text(len(mine), 'curve'))
            if kept_by:
                owners = ', '.join(repr(name)
                                   for name in sorted(set(kept_by.values())))
                skipped = count_text(len(kept_by), 'shared curve')
                note += (f'; {skipped} left to higher-priority {owners}')
                self.warnings.append(
                    f'curve control {row["name"]!r} left {skipped} to '
                    f'{owners}, which ranks higher (priority, then name)')
            self.ledger.record(
                f'curveControl:{row["name"]}', row['mode'],
                row['mode'] if mine else None, applied=bool(mine),
                note=note)
            if row.get('transfiniteSurface'):
                pending.append((row, surfaces))
        for row, surfaces in pending:
            structured += self.apply_transfinite_surfaces(row, surfaces)
        self.statistics['curveControls'] = {
            'curvesTouched': applied, 'surfacesStructured': structured}

    def grading_coefficient(self, row) -> float:
        """The coefficient Gmsh wants, sign and all.

        MEASURED: ``setTransfiniteCurve`` carries the grading *direction* in
        the sign of the coefficient. On an 0.8 m edge given nine nodes,
        Progression 1.4 spaced them 0.023 .. 0.245 m and Progression -1.4
        spaced them 0.245 .. 0.023 -- the exact reverse. Beta behaves the
        same way. Bump is symmetric about the middle and came back identical
        for both signs, so a reversal there is reported rather than obeyed.

        The schema keeps the coefficient positive, because a negative growth
        rate is not something a user means; the direction is its own flag and
        the sign convention lives here, where it meets Gmsh.
        """
        coefficient = float(row['coefficient'])
        if not row.get('reverseGrading'):
            return coefficient
        law = str(row.get('law') or 'Progression')
        if law == 'Bump':
            self.warnings.append(
                f'curve control {row["name"]!r} asks for the grading to be '
                'reversed, but a Bump law is symmetric about the middle of '
                'the curve and comes back the same either way')
            self.ledger.record(f'curveControl:{row["name"]}.reverseGrading',
                               True, False, applied=False,
                               note='a Bump distribution is symmetric')
            return coefficient
        self.ledger.record(f'curveControl:{row["name"]}.reverseGrading',
                           True, True,
                           note='Gmsh takes the direction as the sign of the '
                                'coefficient')
        return -coefficient

    def side_nodes(self, tag, corners):
        """The node counts of surface *tag*'s sides, corners already checked.

        Kept apart from :meth:`transfinite_sides` so the walk can be given a
        boundary rather than having to fetch one, which is what makes it
        testable without a model.
        """
        gmsh = self.gmsh
        _up, down = gmsh.model.getAdjacencies(2, int(tag))
        curves = [int(item) for item in down]
        ends = {}
        for curve in curves:
            _u, points = gmsh.model.getAdjacencies(1, curve)
            ends[curve] = {int(point) for point in points}
        sides = self.transfinite_sides(curves, ends, corners)
        if sides is None:
            # The refusal already walked this boundary and accepted it, so
            # reaching here means the model changed underneath. Report the
            # curves instead of guessing at sides.
            return sorted(self.transfinite_curves[curve]['nodes']
                          for curve in curves)
        return sides

    def transfinite_sides(self, curves, ends, corners):
        """Node counts along each side of a face, corner to corner.

        Plan 31 FC-C. A side is the run of bounding curves between two named
        corners, so a five-curve face with four corners has one side made of
        two curves. Two curves of 3 nodes meeting at a corner that is not a
        corner make a side of 5 nodes, not 6: the shared node is one node.

        Returns the counts in the order the boundary walks, so opposing sides
        are two apart, or ``None`` if the boundary does not close -- which is
        not a judgement, only a refusal to guess.
        """
        wanted = set(int(item) for item in corners)
        remaining = set(curves)
        start = None
        for curve in curves:
            for point in ends[curve]:
                if point in wanted:
                    start = point
                    break
            if start is not None:
                break
        if start is None:
            return None
        sides, point, side_nodes, side_curves = [], start, 0, 0
        while remaining:
            step = None
            for curve in sorted(remaining):
                if point in ends[curve]:
                    step = curve
                    break
            if step is None:
                return None
            remaining.discard(step)
            nodes = self.transfinite_curves[step]['nodes']
            # Shared nodes are counted once: two curves of n nodes meeting
            # end to end run 2n-1 nodes, not 2n.
            side_nodes = nodes if not side_curves else side_nodes + nodes - 1
            side_curves += 1
            far = [item for item in ends[step] if item != point]
            point = far[0] if far else point
            if point in wanted:
                sides.append(side_nodes)
                side_nodes, side_curves = 0, 0
        if side_curves or point != start or len(sides) != len(wanted):
            return None
        return sides

    def structured_surface_refusal(self, tag, named_corners=()) -> str:
        """Why surface *tag* cannot be structured as asked, or ``''``.

        MEASURED against Gmsh 4.15.2 on duct.step, and this is the reason the
        check exists at all: Gmsh does not refuse a transfinite surface whose
        prerequisites are unmet. Neither ``setTransfiniteSurface`` nor
        ``generate`` raised when a face's two opposing sides were given 5 and
        9 nodes -- it came back with 46 triangles where a structured 4x8 grid
        is 64 -- nor when only two of the four bounding curves carried a count
        at all, which came back with 179. The run then recorded the control
        applied, on an unstructured mesh.

        Plan 31 FC-C: *named_corners* are the corners the control asked Gmsh
        to interpolate between. Naming them is what makes a face with more
        than four corners meshable at all, so a face is only refused for
        having five corners when nobody said which four they meant.
        """
        gmsh = self.gmsh
        _up, down = gmsh.model.getAdjacencies(2, int(tag))
        curves = [int(item) for item in down]
        ends = {}
        corners = set()
        for curve in curves:
            _u, points = gmsh.model.getAdjacencies(1, curve)
            ends[curve] = {int(point) for point in points}
            corners |= ends[curve]
        named = [int(item) for item in named_corners or ()]
        if named:
            # Plan 31 FC-C. MEASURED on Gmsh 4.15.2: given a corner that
            # belongs to another face, `setTransfiniteSurface` takes it and
            # `generate` does not raise -- the face came back as 44 triangles
            # where the node counts asked for a 4x4 grid, which is 32. So the
            # membership Gmsh does not check is checked here.
            stray = [item for item in named if item not in corners]
            if stray:
                return ('corner point '
                        + ', '.join(str(item) for item in stray)
                        + ' is not on this surface, whose corners are '
                        + ', '.join(str(item) for item in sorted(corners))
                        + '; Gmsh takes a corner from elsewhere without '
                          'complaint and interpolates a different grid')
        elif len(corners) not in (3, 4):
            return (f'a transfinite surface interpolates between three or '
                    f'four corners and this one has {len(corners)}; name '
                    f'three or four of '
                    + ', '.join(str(item) for item in sorted(corners))
                    + ' as the corners to say which')
        loose = [curve for curve in curves
                 if curve not in self.transfinite_curves]
        if loose:
            return ('every bounding curve needs a node count before the '
                    'surface can be structured, and '
                    + ', '.join(str(curve) for curve in sorted(loose))
                    + ' has none')
        if named:
            sides = self.transfinite_sides(curves, ends, named)
            if sides is None:
                return ('the corners named do not divide the boundary of '
                        'this surface into sides that close; a side runs '
                        'from one named corner to the next along the '
                        'bounding curves')
            if len(sides) == 4 and (sides[0] != sides[2]
                                    or sides[1] != sides[3]):
                return (f'opposing sides must carry the same node count, and '
                        f'the sides between the corners named run '
                        + ', '.join(str(item) for item in sides)
                        + ' nodes')
            return ''
        if len(curves) == 4:
            # Two curves of a four-sided face that share no corner are the
            # opposing pair. Read from the corners rather than from the order
            # the boundary comes back in, so a differently wound face is
            # judged the same way.
            for curve in curves:
                facing = [other for other in curves
                          if other != curve and not (ends[curve] & ends[other])]
                for other in facing:
                    mine = self.transfinite_curves[curve]['nodes']
                    theirs = self.transfinite_curves[other]['nodes']
                    if mine != theirs:
                        return (f'opposing sides must carry the same node '
                                f'count; curve {curve} has {mine} and the '
                                f'curve facing it, {other}, has {theirs}')
        return ''

    def apply_transfinite_surfaces(self, row, surfaces):
        """Make the scoped surfaces structured, or say why they cannot be.

        Plan 30 WP12, F-26. A transfinite surface interpolates between the
        corner points of a three- or four-sided CAD face. A tessellated import
        has no such faces -- Gmsh meshes it as a discrete surface with no
        parametrisation -- so the whole control is refused up front with a
        reason, rather than applied face by face and failing on each.
        """
        name = row['name']
        if self.tessellated:
            reason = ('a transfinite surface interpolates between the corner '
                      'points of a CAD face, and this geometry was imported '
                      'as triangles; re-import it as STEP/IGES/BREP, or leave '
                      'the surface unstructured')
            self.warnings.append(f'curve control {name!r}: {reason}')
            self.ledger.record(f'curveControl:{name}.transfiniteSurface',
                               True, False, applied=False, note=reason)
            return 0
        corners = [int(item) for item in row.get('cornerPoints') or ()]
        accepted = 0
        for tag in surfaces:
            refusal = self.structured_surface_refusal(int(tag), corners)
            if refusal:
                # Gmsh would take the request and quietly mesh the face
                # unstructured, so the run says which face and why instead of
                # certifying a structure that is not there.
                self.warnings.append(
                    f'curve control {name!r}: surface {tag} was left '
                    f'unstructured because {refusal}')
                continue
            try:
                if corners:
                    # MEASURED: the order of the tags makes no difference --
                    # the same four corners scrambled produced the same 32
                    # triangles -- so they go in as the user wrote them.
                    self.gmsh.model.mesh.setTransfiniteSurface(
                        int(tag), 'Left', list(corners))
                    sides = self.side_nodes(int(tag), corners)
                else:
                    self.gmsh.model.mesh.setTransfiniteSurface(int(tag))
                    sides = sorted(
                        self.transfinite_curves[abs(int(curve))]['nodes']
                        for _d, curve in self.gmsh.model.getBoundary(
                            [(2, int(tag))], oriented=False))
                self.transfinite_surfaces[int(tag)] = {
                    'control': name, 'nodes': sides, 'corners': list(corners),
                }
                accepted += 1
            except Exception as error:                       # noqa: BLE001
                # Gmsh takes three- and four-cornered faces only. Refusing the
                # whole run over one face is worse than naming the face.
                self.warnings.append(
                    f'curve control {name!r}: Gmsh would not make surface '
                    f'{tag} transfinite ({error})')
        self.ledger.record(
            f'curveControl:{name}.transfiniteSurface', len(surfaces), accepted,
            applied=bool(accepted),
            note='a transfinite surface needs three or four corners, a node '
                 'count on every bounding curve, and equal counts on '
                 'opposing sides')
        if corners:
            # Requested and produced, separately: the corners asked for
            # against the faces they were actually applied to.
            self.ledger.record(
                f'curveControl:{name}.cornerPoints',
                ', '.join(str(item) for item in corners), accepted,
                applied=bool(accepted),
                note='the corners Gmsh was told to interpolate between, and '
                     'the number of surfaces that took them')
        return accepted

    def unscoped_volume_warning(self, name):
        """Why a volume control sized nothing, and what to do about it.

        Plan 37 UF13. This said "has no resolvable volume scope and was
        skipped", which names the runner's bookkeeping rather than the case.
        With a farfield on, the usual cause is the cut itself: every imported
        volume is consumed and the fluid around the bodies is a new volume no
        control was scoped to.
        """
        farfield = self.statistics.get('farfield') or {}
        shape = farfield.get('shape')
        if shape:
            return (f'volume control {name!r} was skipped: the farfield '
                    f'{shape} cut the volume it is scoped to out of the '
                    'domain, so there is nothing left for it to act on. '
                    'Remove the control, or turn the farfield off to mesh '
                    'that volume.')
        return (f'volume control {name!r} was skipped: the volume it is '
                'scoped to is not in the imported geometry. Choose its volume '
                'again on the Volume controls page, or remove the control.')

    def apply_volume_controls(self):
        """Per-volume sizing, structure and exclusion.

        A volume control is scoped to a region rather than a surface, so it
        resolves against the imported volumes. Sizing goes through a Restrict
        field rather than Mesh.setSize: setSize acts on points, and a point
        shared with the neighbouring volume would carry the size across the
        boundary.
        """
        rows = self.intent.get('volumeControls') or []
        if not rows:
            return
        gmsh = self.gmsh
        field = gmsh.model.mesh.field
        applied = {'sized': 0, 'transfinite': 0, 'excluded': 0, 'skipped': 0}

        for row in rows:
            name = row['name']
            volumes = self.scope_volumes.get(row['scopeToken'])
            if not volumes:
                self.warnings.append(self.unscoped_volume_warning(name))
                self.ledger.record(f'volumeControl:{name}', row['scopeToken'],
                                   None, applied=False,
                                   note='scope did not resolve')
                applied['skipped'] += 1
                continue

            if not row.get('included', True):
                gmsh.model.occ.remove([(3, tag) for tag in volumes],
                                      recursive=False)
                gmsh.model.occ.synchronize()
                # C31-04. Removing a volume is a topology change like any
                # other: the identities it carried are gone and the scope maps
                # every later control reads have to say so.
                self.revalidate_scopes(f'volume-excluded:{name}',
                                       dimensions=(3,))
                applied['excluded'] += len(volumes)
                self.ledger.record(f'volumeControl:{name}.included', False,
                                   False,
                                   note=f'{count_text(len(volumes), "volume")}'
                                        ' removed')
                continue

            size = row.get('targetSize')
            if size:
                constant = field.add('Constant')
                field.setNumber(constant, 'VIn', float(size))
                field.setNumbers(constant, 'VolumesList',
                                 [float(tag) for tag in volumes])
                self.background_fields.append(constant)
                self.background_sizes.append((str(name), float(size)))
                applied['sized'] += 1
                self.ledger.record(f'volumeControl:{name}.targetSize', size,
                                   size,
                                   note=count_text(len(volumes), 'volume'))

            if row.get('transfinite'):
                accepted = 0
                if self.tessellated:
                    # Plan 30 WP12. A discrete surface has no corners to
                    # interpolate between, so the volume cannot be structured
                    # either. Say so once instead of failing per volume.
                    reason = ('a transfinite volume is built from transfinite '
                              'faces, and a tessellated import has no CAD '
                              'faces to make structured')
                    self.warnings.append(f'volume control {name!r}: {reason}')
                    self.ledger.record(f'volumeControl:{name}.transfinite',
                                       len(volumes), 0, applied=False,
                                       note=reason)
                    continue
                for tag in volumes:
                    try:
                        # Plan 30 WP12, section 4.4: setTransfiniteVolume on
                        # its own was ineffective, because Gmsh builds a
                        # structured volume out of structured *faces* and the
                        # faces were still unstructured. Every bounding face
                        # is made transfinite first.
                        for _dim, face in gmsh.model.getBoundary(
                                [(3, tag)], oriented=False):
                            gmsh.model.mesh.setTransfiniteSurface(int(abs(face)))
                        gmsh.model.mesh.setTransfiniteVolume(tag)
                        self.transfinite_volumes[int(tag)] = {'control': name}
                        accepted += 1
                    except Exception as error:
                        # Only 5- and 6-faced volumes can be transfinite.
                        # Refusing the whole run over one is worse than saying
                        # which one Gmsh would not take.
                        self.warnings.append(
                            f'volume control {name!r}: Gmsh would not make '
                            f'volume {tag} transfinite ({error})')
                applied['transfinite'] += accepted
                self.ledger.record(
                    f'volumeControl:{name}.transfinite', len(volumes), accepted,
                    applied=bool(accepted),
                    note='transfinite needs a 5- or 6-faced volume whose '
                         'faces are transfinite too')

        self.statistics['volumeControls'] = {**applied, 'requested': len(rows)}

    def apply_boundary_layers(self):
        """Grow prism layers, carving them out of the imported volume.

        MEASURED: skipping the carve leaves the original volume intact and
        stacks the prisms on top of it. The tet mesh comes back bit-identical
        to the unlayered one, no cell is inverted, and the total volume is
        8.75% too large.
        """
        layers = self.intent.get('layers') or {}
        if not layers.get('enabled'):
            return
        gmsh = self.gmsh
        heights = [float(item) for item in layers['cumulativeHeights']]
        count = int(layers['layerCount'])

        surfaces = [tag for _dim, tag in gmsh.model.getEntities(2)]
        volumes = [tag for _dim, tag in gmsh.model.getEntities(3)]
        selected, skipped = self.layer_surfaces(surfaces, layers)
        if not selected:
            # Growing on nothing is not the same as growing on everything, and
            # quietly doing the latter is how a user gets prisms on an inlet.
            #
            # DP-57. This warned and returned, and the run then published as
            # `succeeded` with `statistics.layers: null`. MEASURED across the
            # 66 Gmsh runs this repository has published: 23 asked for layers
            # and grew none, and 20 of those reported success. The page
            # pre-ticks the wall patches, so a selection is nearly always
            # present; where the import carries no patch names of its own,
            # every offered name misses and the whole selection empties. A
            # mesh with no near-wall resolution is not the mesh that was asked
            # for, and a warning on a run marked successful did not stop one
            # of those twenty from being used.
            self.ledger.record('gmsh/boundaryLayers/layerCount', count, 0,
                               applied=False,
                               note='no selected patch matched a surface')
            asked = ', '.join(
                str(item).strip() for item in (layers.get('patches') or ())
                if str(item).strip())
            offer = ', '.join(sorted({self.surface_patch_name(tag)[0]
                                      for tag in surfaces})) or 'none'
            if not asked:
                # Plan 33 section 1.1. This case used to mesh, growing layers
                # on every boundary surface including the inlet and the
                # outlet. Silence is not consent to that.
                raise MeshFailure(
                    'boundary layers are switched on and no surface was '
                    'chosen to grow them, so no layer could be grown and the '
                    'mesh would have had no near-wall resolution at all. The '
                    f'surfaces this run imported are: {offer}. Choose one of '
                    'those, ask for every eligible wall, or turn boundary '
                    'layers off.')
            raise MeshFailure(
                f'boundary layers were asked for on {asked}, which matched no '
                'imported surface, so no layer could be grown and the mesh '
                'would have had no near-wall resolution at all. The surfaces '
                f'this run imported are: {offer}. Name one of those, or turn '
                'boundary layers off.')
        # R118. The carve removes the volume the layer grows into and rebuilds
        # it from the layer's inner surfaces. That can be done on an assembly
        # as long as every base bounds the same volume: the others are left
        # exactly as they were imported, and the interface is the very surface
        # the prisms grew from, so the two sides stay conformal. MEASURED on
        # `annulus_shell.step`, three layers, the two cylindrical walls only
        # and the end caps rebuilt -- solid 21769 tets, interface layer 10716
        # prisms, outer layer 17754 prisms, fluid core 34065 tets, 0 inverted,
        # and 12550829 mm3 against an analytic 12566371, 0.12% low, which is
        # the faceting of the two cylinders. What cannot be done is a
        # selection that spans volumes: only one core is rebuilt.
        carved = list(volumes)
        if len(volumes) > 1:
            core, candidates = self.volume_the_layer_grows_into(volumes,
                                                                selected)
            if core is None and candidates:
                raise MeshFailure(
                    'the boundary layer was asked for on surfaces that bound '
                    f'{len(candidates)} volumes and on nothing else, so there '
                    'is no way to tell which side of the interface it belongs '
                    'on. Name a patch that bounds only the volume the layer '
                    'is for as well.')
            if core is None:
                raise MeshFailure(
                    f'boundary layers are not supported on a {len(volumes)}'
                    '-volume assembly unless every patch they grow on bounds '
                    'one and the same volume: the layer is carved out of a '
                    'single core, and a selection spanning more than one '
                    'leaves neither side closed. Name the patches of one '
                    'volume, mesh this geometry without layers, or split it '
                    'into one job per volume.')
            carved = [core]
            surfaces = self.volume_surfaces(core)
            chosen = set(int(tag) for tag in selected)
            skipped = [tag for tag in surfaces if int(tag) not in chosen]
            selected, skipped = self.keep_shared_surfaces(volumes, selected,
                                                          skipped)
        # The bounding curves have to be read while the surface still exists;
        # they are what ties the faces that replace it back to its patch name.
        # The boxes are read here for the same reason, and check that what the
        # rebuild closes really is where the un-layered patch was.
        skipped_curves = {tag: self.surface_curves(tag) for tag in skipped}
        skipped_boxes = {tag: gmsh.model.getBoundingBox(2, tag)
                         for tag in skipped}
        # DP-55. The sign of `heights` is a direction along the surface
        # normal, and one sign cannot serve two shells. Each shell's normals
        # face away from its own interior, so on the outermost one the fluid
        # lies at -n and on an obstacle inside it the fluid lies at +n. Grown
        # with a single sign the obstacle's layer goes into the solid: MEASURED
        # on `box_with_obstacle_two_files`, 2082 open cells, 2082 wrongly
        # oriented face pyramids, 2082 concave cells, four failed checkMesh
        # checks -- and a volume of 63.156 where the fluid is 63.000, because
        # the shell around the obstacle was covered twice.
        #
        # `void_surfaces` and `surface_winding` answer this where the shell
        # topology ran. It does not run on the CAD route, which arrives here
        # with nothing meshed at all, so the boundary is split into shells and
        # the enclosed ones handed to `layer_height_groups` as well.
        shells = self.boundary_shells(surfaces)
        internal = self.internal_shells(shells)
        # DP-124. A shell of the core the layer never touches needs no
        # rebuild at all. MEASURED on `two_solid_block.stl`, a box inside a
        # box: the farfield is one volume bounded by two disjoint shells, the
        # layer covers the outer one entirely, and so there is no rim
        # anywhere -- every curve of every layer top is shared with another
        # top. The inner shell was handed to the rebuild regardless, which
        # found no rim to stand in for it and refused the run. It is already
        # closed, already meshed and already named; it just has to be handed
        # to `addVolume` as the hole it is.
        kept_shells = self.shells_kept_whole(shells, selected, internal)
        # DP-127. The mirror of the same reading. On `turbine_cascade` the
        # layer is on the blades and the farfield around them carries none,
        # so the shell the layer never reached is the enclosing one. It needs
        # no rim either; it is the exterior the fluid is bounded by, and the
        # layer tops are the holes in it.
        outer_shell = self.shell_kept_as_exterior(shells, selected, internal)
        retained = {tag for group in kept_shells for tag in group}
        retained.update(int(tag) for tag in outer_shell)
        if retained:
            skipped = [tag for tag in skipped if int(tag) not in retained]
            skipped_curves = {tag: curves for tag, curves
                              in skipped_curves.items()
                              if int(tag) not in retained}
            skipped_boxes = {tag: box for tag, box in skipped_boxes.items()
                             if int(tag) not in retained}
        # DP-71. Nesting says a surface bounds a hole; it does not say which
        # way that surface faces, and the flip needs both. `outward_normals`
        # asks the solid directly, while it is still in the model.
        outward = ({} if self.surface_winding
                   else self.outward_normals(selected, carved))
        self.remove_entities([(3, tag) for tag in carved])

        groups = self.layer_height_groups(selected, heights, internal=internal,
                                          outward=outward)
        # DP-91. Two directions that meet on a shared edge extrude that edge
        # twice, and Gmsh says so only part-way through the 3D pass, in a
        # sentence with no patch name in it. Asked here, while the patches
        # still have names and before anything has been built.
        meetings = self.directions_that_meet(groups)
        inward = [tag for _signed, tags, flipped in groups if flipped
                  for tag in tags]
        # DP-404. The refusal below used to stand here, in front of the
        # extrusion. It stands behind it now, because the premise it was
        # written on is only half true: the *height* is signed once per call,
        # the *call* is not limited to one direction. A dim-tag carries a sign
        # of its own and `extrudeBoundaryLayer` reads it.
        refusal = ''
        if meetings:
            refusal = self.opposed_directions_refusal(selected, groups,
                                                      meetings, skipped)
            groups = self.one_call_for_both_directions(groups)
        try:
            extruded = []
            columns = []
            for signed, tags, _flipped in groups:
                produced = list(gmsh.model.geo.extrudeBoundaryLayer(
                    [(2, tag) for tag in tags], [1] * count, signed,
                    bool(layers.get('quads', True))))
                extruded.extend(produced)
                # DP-74. The inner face of each stack, paired with the wall it
                # grew from. The pairing only holds within one call: the two
                # directions are extruded separately and the returned list
                # runs in the order of the surfaces handed to that call.
                # DP-404. A tag in this list may be negative, which is how
                # the direction is carried when both senses go in one call.
                # What is paired with the stack is the surface, not the sense.
                columns.extend(zip([abs(int(tag)) for tag in tags], [
                    produced[index - 1][1]
                    for index in range(1, len(produced))
                    if produced[index][0] == 3
                    and produced[index - 1][0] == 2]))
            gmsh.model.geo.synchronize()
        except Exception as error:
            # DP-404. Where the two senses were put in one call and the call
            # still would not run, the contact is the thing to say: it is the
            # reading that names patches and offers a selection, and `error`
            # names neither.
            raise MeshFailure(
                refusal or f'the boundary layer could not be extruded: {error}'
            ) from error

        tops = [extruded[index - 1] for index in range(1, len(extruded))
                if extruded[index][0] == 3 and extruded[index - 1][0] == 2]
        if not tops:
            raise MeshFailure(
                'the boundary-layer extrusion produced no inner surfaces, so '
                'the core volume cannot be rebuilt')
        top_tags = [tag for _dim, tag in tops]
        laterals = list(dict.fromkeys(
            tag for dim, tag in extruded
            if dim == 2 and tag not in set(top_tags)))
        rebuilt_loops = (self.rebuild_unextruded(skipped, top_tags,
                                                 skipped_boxes, columns)
                         if skipped else {})
        rebuilt = list(rebuilt_loops)
        if outer_shell:
            # DP-127. The layer is inside, on bodies the kept shell encloses.
            # The exterior is that shell, whole, and each body's layer tops
            # close around it to make one hole -- so they are grouped by the
            # shell their bases stood on rather than poured into one loop.
            loops = [gmsh.model.geo.addSurfaceLoop(list(outer_shell))]
            loops.extend(gmsh.model.geo.addSurfaceLoop(group)
                         for group in self.layer_tops_by_shell(shells,
                                                               columns))
        else:
            loops = [gmsh.model.geo.addSurfaceLoop(top_tags + rebuilt)]
        # DP-124. Every kept shell is enclosed by the one the layer grew on --
        # `shells_kept_whole` will not keep one that is not -- so it is a hole
        # in the core, and the geo kernel spells a hole as a surface loop
        # after the first. Same shape the tessellated import builds its voids
        # with, above.
        loops.extend(gmsh.model.geo.addSurfaceLoop(list(group))
                     for group in kept_shells)
        core_volume = gmsh.model.geo.addVolume(loops)
        gmsh.model.geo.synchronize()
        if skipped:
            self.name_layer_replacements(skipped_curves, rebuilt_loops,
                                         laterals)

        self.layer_bases = [int(tag) for tag in selected]
        self.layer_columns = [(int(base), int(top)) for base, top in columns]
        self.layer_volumes = [int(tag) for dim, tag in extruded if dim == 3]
        # R118. The carved volume is gone; these are what it became. A
        # region is not renamed by having a layer grown in it -- MEASURED on
        # `annulus_shell` before this: the fluid's own name landed on one of
        # its two layer volumes by tag reuse and its core published as
        # `volume_4`, and on the wrapped single-solid route 17 of the 18
        # volumes published as `volume_N`.
        self.volume_replacements = {
            int(tag): [int(core_volume)] + list(self.layer_volumes)
            for tag in carved}
        self.layer_laterals = [int(tag) for tag in laterals]
        self.statistics['layers'] = {
            'requestedLayers': count,
            'requestedFirstHeight': abs(heights[0]),
            'requestedTotalThickness': abs(heights[-1]),
            'extrudedSurfaces': len(selected),
            'skippedSurfaces': len(skipped),
            'rebuiltSurfaces': len(rebuilt),
            # DP-55. How the boundary divided, and how many bases had to be
            # grown the other way because they bound a hole rather than the
            # outside of the fluid, so a finished mesh can be read back
            # against the decision.
            'shells': len(shells),
            'reversedSurfaces': len(inward),
            # DP-71. How many of the bases had their direction measured
            # against the solid rather than inferred from nesting.
            'measuredSurfaces': len(outward),
            'patches': sorted({self.surface_patch_name(tag)[0]
                               for tag in selected}),
            'scope': layers.get('scope', 'all_boundary_surfaces'),
            # R118. Which volume was carved and rebuilt, and which ones were
            # left as they were imported. On a single-solid case this is the
            # only volume there was; on an assembly it says which side of the
            # interface the layer was grown into.
            'coreVolume': int(carved[0]) if len(carved) == 1 else None,
            'volumesLeftInPlace': sorted(
                int(tag) for tag in volumes if int(tag) not in
                {int(item) for item in carved}),
        }
        note = f'extruded off {count_text(len(selected), "surface")}'
        if inward:
            note += (f', {len(inward)} of them bounding a hole and grown the '
                     'other way, into the fluid')
        self.ledger.record('gmsh/boundaryLayers/layerCount', count, count,
                           note=note)

    def surface_facets(self, tag, points):
        """One surface's faces as corner triangles, in element order.

        DP-74. A recombined layer bounds its stacks with quadrilaterals and an
        unrecombined one with triangles, and the fold reading has to compare
        like with like, so both are fanned from their first corner. Midside
        nodes are dropped: a second-order face carries them and the fold is a
        property of the corners.
        """
        kinds, _tags, nodes = self.gmsh.model.mesh.getElements(2, tag)
        facets = []
        for kind, block in zip(kinds, nodes):
            facts = self.gmsh.model.mesh.getElementProperties(kind)
            shape, _dim, _order, per = facts[:4]
            corners = 3 if shape.startswith('Triangle') else (
                4 if shape.startswith('Quadrilateral') else 0)
            if not corners:
                continue
            block = [int(item) for item in block]
            for index in range(0, len(block), per):
                face = [points[item]
                        for item in block[index:index + corners]]
                for step in range(1, corners - 1):
                    facets.append((face[0], face[step], face[step + 1]))
        return facets

    def read_surface_crossings(self, error, tags=None):
        """Name the patches whose own faces cross, or return None.

        DP-75. Only a refusal that reads like a crossing boundary is worth
        the scan, and only the surface mesh is scanned: every face of every
        2D entity against every other face near it, in a grid whose cell is
        the model diagonal over 96, which is what bounds the work.

        MEASURED on ``drone_quadcopter``, Gmsh 4.15.2. Handed the classified
        patches alone (``tags`` from :meth:`classified_surfaces`) it reads
        32543 faces in 1.9 s and reports 85 crossing pairs. Handed the whole
        model (``tags`` None, which is the path :meth:`generate` takes) it
        reads 389 entities and 21148 faces in 37 s and reports 1131 pairs.
        Fewer faces and twenty times the cost, because the whole model adds
        the boundary-layer laterals, and a lateral is a long thin face that
        lands in many cells at once; the grid bounds the comparison but does
        not make it linear. That is the reason the fold oracle passes the
        classified patches rather than letting the scan see everything.
        """
        if error is not None and not crossing_like(error):
            return None
        wanted = set(tags) if tags is not None else None
        points = self._node_coordinates()
        facets = []
        for _dim, tag in self.gmsh.model.getEntities(2):
            if wanted is not None and int(tag) not in wanted:
                continue
            for face in self.surface_facets(tag, points):
                facets.append((int(tag), face))
        if not facets:
            return None
        low = [min(corner[axis] for _t, face in facets for corner in face)
               for axis in range(3)]
        high = [max(corner[axis] for _t, face in facets for corner in face)
                for axis in range(3)]
        step = max(high[axis] - low[axis] for axis in range(3)) / 96.0
        if step <= 0.0:
            return None
        grid: dict = {}
        for number, (_tag, face) in enumerate(facets):
            span = [(int((min(c[axis] for c in face) - low[axis]) / step),
                     int((max(c[axis] for c in face) - low[axis]) / step))
                    for axis in range(3)]
            for i in range(span[0][0], span[0][1] + 1):
                for j in range(span[1][0], span[1][1] + 1):
                    for k in range(span[2][0], span[2][1] + 1):
                        grid.setdefault((i, j, k), []).append(number)
        return self._crossing_verdict(facets, grid, error)

    def _crossing_verdict(self, facets, grid, error):
        """Turn the crossing pairs found into a refusal that names them."""
        pairs = set()
        for members in grid.values():
            for first in range(len(members)):
                for second in range(first + 1, len(members)):
                    one, other = members[first], members[second]
                    if (one, other) in pairs:
                        continue
                    if _share_a_corner(facets[one][1], facets[other][1]):
                        continue
                    if _faces_cross(facets[one][1], facets[other][1]):
                        pairs.add((one, other))
        if not pairs:
            return None
        tally: dict = {}
        for one, other in pairs:
            for number in (one, other):
                tag = facets[number][0]
                tally[tag] = tally.get(tag, 0) + 1
        worst = max(tally, key=lambda tag: tally[tag])
        where = next(facets[one][1][0] for one, other in pairs
                     if facets[one][0] == worst or facets[other][0] == worst)
        name = self.surface_patch_name(worst)[0]
        tail = f' The volume mesher refused it: {error}' if error else ''
        return SurfaceCrossing(
            f'the surface mesh crosses itself: {count_text(len(pairs), "pair")} '
            f'of faces {agreeing(len(pairs), "passes", "pass")} through each '
            f'other, most of them on {name} near '
            f'({where[0]:.6g}, {where[1]:.6g}, {where[2]:.6g}).' + tail,
            patch=name, at=tuple(where), pairs=len(pairs),
            remeshed=not self.kept_tessellation)

    def classified_surfaces(self):
        """The patches the import left, without anything the layer added.

        DP-75. A layer that folds puts crossing faces on its own tops and
        sides, and those say nothing about the geometry underneath. Only the
        patches that were there before the extrusion answer the question the
        fallback turns on: did the remesh of the *imported* surface cross
        itself, whatever the layer did.
        """
        grown = {int(top) for _base, top in self.layer_columns}
        grown.update(int(tag) for tag in self.layer_laterals)
        return [int(tag) for _dim, tag in self.gmsh.model.getEntities(2)
                if int(tag) not in grown]

    def measure_layer_fit(self):
        """Read the grown layer back and refuse one that folded over itself.

        DP-74. MEASURED on ``centrifugal_impeller``, reproduced on
        ``drone_quadcopter``: the run died in the volume mesher with
        ``PLC Error:  A segment and a facet intersect at point`` and nothing
        more -- tetgen prints the point in a second call that Gmsh's logger
        drops, so the message names neither the place nor the cause, and the
        name it invites is the wrong one. The geometry is not at fault. The
        impeller's STL carries 616 triangles with no self-intersecting pair,
        no degenerate triangle, no open edge and no non-manifold edge, and the
        same job meshes on both volume algorithms once ``layers.enabled`` is
        turned off. What crosses is the layer: of the 34026 facets the
        extrusion grew, 8 turn inside out before they reach the
        0.00133750785 m of stack that was asked for, the first at
        0.000438 m -- a third of the way up. Bisecting the same job agrees:
        it meshes at 0.000334 m and refuses at 0.000669 m.

        So the fold is measured here, between the surface pass that creates
        the inner surface and the volume pass that chokes on it, and what it
        measures is the thickness this geometry carries.
        ``execute_with_layer_fit`` regrows the stack at that thickness rather
        than refusing outright, which is what snappyHexMesh does with a layer
        that will not fit.
        """
        stack = (self.intent.get('layers') or {}).get('cumulativeHeights')
        if not stack or not self.layer_columns:
            return
        asked = abs(float(stack[-1]))
        if asked <= 0.0:
            return
        tags, flat, _ = self.gmsh.model.mesh.getNodes()
        points = {int(tag): (flat[3 * index], flat[3 * index + 1],
                             flat[3 * index + 2])
                  for index, tag in enumerate(tags)}
        worst = None
        folded = 0
        total = 0
        for base, top in self.layer_columns:
            low = self.surface_facets(base, points)
            high = self.surface_facets(top, points)
            if len(low) != len(high):
                # Nothing to compare against, and guessing a correspondence
                # would invent a fold or hide one.
                continue
            for one, other in zip(low, high):
                total += 1
                value = layer_fold_parameter(one, other)
                if value is None:
                    continue
                if value <= 1.0:
                    folded += 1
                if worst is None or value < worst[0]:
                    worst = (value, base, tuple(
                        sum(point[axis] for point in one) / 3.0
                        for axis in range(3)))
        self.record_layer_fit(asked, total, folded, worst)

    def record_layer_fit(self, asked, total, folded, worst):
        """Publish what the fit reading found, and refuse if it folded."""
        if worst is None:
            return
        carries = worst[0] * asked
        patch = self.surface_patch_name(worst[1])[0]
        place = tuple(round(value, 9) for value in worst[2])
        record = self.statistics.get('layers')
        if isinstance(record, dict):
            record['fit'] = {
                'askedTotalThickness': asked,
                'carriesTotalThickness': carries,
                'facets': total,
                'foldedFacets': folded,
                'limitPatch': patch,
                'limitAt': list(place),
            }
        if not folded:
            return
        # DP-75. A fold reading this bad is usually a layer too thick for the
        # wall, and refitting it is what DP-74 does. It is sometimes a patch
        # whose remesh already crossed itself, on which no layer of any
        # thickness can stand, and the two are told apart by looking at the
        # patches alone -- so the scan is paid for only here, where the run
        # is about to be refused either way.
        crossing = self.read_surface_crossings(None,
                                               self.classified_surfaces())
        if crossing is not None:
            raise crossing
        raise LayerFold(
            f'the boundary layer does not fit this geometry: {folded} of the '
            f'{total} faces it grew turn inside out before they reach the '
            f'{asked:.6g} m of stack that was asked for. The first fold is on '
            f'{patch} at ({place[0]:.6g}, {place[1]:.6g}, {place[2]:.6g}), '
            f'where the wall carries {carries:.6g} m.',
            carries=carries, asked=asked, folded=folded, patch=patch,
            at=place)

    def layer_cell_tags(self):
        """The cells the boundary-layer extrusion made, by element tag.

        DP-76. Read from the volume entities the extrusion returned rather
        than from the element shape, because a shape is not provenance: a
        recombined volume is full of prisms nobody grew, and a layer over a
        quadrilateral wall is full of hexahedra. Empty when no layer was
        grown, which is the case the callers treat as 'judge everything'.
        """
        if not self.layer_volumes:
            return frozenset()
        found = set()
        for tag in self.layer_volumes:
            try:
                _types, groups, _nodes = self.gmsh.model.mesh.getElements(
                    3, int(tag))
            except Exception:                                # noqa: BLE001
                continue
            for group in groups:
                found.update(int(item) for item in group)
        return frozenset(found)

    def layer_top_nodes(self):
        """The nodes on the inner surface of the layer.

        DP-76. This is the sheet the volume mesh has to start from, and a
        cell all of whose corners lie on it is a cell with no height.
        """
        found = set()
        for _base, top in self.layer_columns:
            try:
                nodes, _coords, _params = self.gmsh.model.mesh.getNodes(
                    2, int(top), includeBoundary=True)
            except Exception:                                # noqa: BLE001
                continue
            found.update(int(item) for item in nodes)
        return frozenset(found)

    def cells_resting_on_the_layer(self):
        """Volume cells every corner of which lies on the layer top.

        DP-76. A cell like this spans the inner surface without leaving it,
        so it has the plan of a surface element and none of its height. Read
        from the flat node block ``getElements`` returns rather than element
        by element: on ``drone_quadcopter`` that is 276574 cells, and asking
        Gmsh for each one's nodes separately costs more than the mesh did.
        """
        tops = self.layer_top_nodes()
        if not tops:
            return []
        skip = self.layer_cell_tags()
        resting = []
        types, groups, nodes = self.gmsh.model.mesh.getElements(3)
        for _etype, tags, block in zip(types, groups, nodes):
            if not len(tags):
                continue
            width = len(block) // len(tags)
            if width <= 0:
                continue
            for index in range(len(tags)):
                tag = int(tags[index])
                if tag in skip:
                    continue
                corners = block[index * width:(index + 1) * width]
                if all(int(node) in tops for node in corners):
                    resting.append(tag)
        return resting

    def measure_layer_landing(self):
        """Read the volume mesh back against the layer it was built on.

        DP-76. :meth:`measure_layer_fit` asks whether the stack folds, which
        is a question about the layer alone and is answered before the volume
        pass. It is not the whole question. MEASURED on ``drone_quadcopter``:
        a stack refitted to 0.00015995 m -- 9.4% of the 0.0016950 m asked for
        -- grows without folding a single facet, and the volume mesh above it
        then comes out with cells 22 mm across and 13 um thick. Gmsh calls
        none of them inverted; ``checkMesh`` reads 40 of them as negative
        volume and 64 cells as open, and fails five checks on a mesh that,
        with the layer off and nothing else changed, fails one.

        So a layer thin enough to fit is not thereby a layer the volume
        mesher can build on, and this is the check that says which it was.
        """
        record = self.statistics.get('layers')
        if not record or not self.layer_columns:
            return
        resting = self.cells_resting_on_the_layer()
        if not resting:
            record['landing'] = {'restingCells': 0, 'degenerateCells': 0,
                                 'floor': LAYER_LANDING_FLOOR}
            return
        values = self.gmsh.model.mesh.getElementQualities(resting, 'gamma')
        flat = sorted((float(value), int(tag))
                      for value, tag in zip(values, resting)
                      if value < LAYER_LANDING_FLOOR)
        record['landing'] = {
            'restingCells': len(resting),
            'degenerateCells': len(flat),
            'worst': float(min(values)),
            'floor': LAYER_LANDING_FLOOR,
            'measure': 'gamma',
        }
        if not flat:
            return
        worst, tag = flat[0]
        place = self._element_centroid(tag)
        record['landing']['worstAt'] = list(place)
        grew = float(record.get('requestedTotalThickness') or 0.0)
        # DP-76. Not ``requestedTotalThickness``: inside a refitted run that
        # is what the refit asked for, not what the user did. DP-74 scales
        # the intent and leaves the untouched ask beside it, so the two
        # numbers in the sentence below are a real comparison and not the
        # same number written twice.
        layers = self.intent.get('layers') or {}
        asked = float(layers.get('originalTotalThickness')
                      or layers.get('totalThickness') or grew)
        against = ''
        if asked > grew * 1.001:
            against = f' against the {asked:.6g} m asked for'
        raise LayerLanding(
            f'the boundary layer fitted and then did not land: {len(flat)} of '
            f'the {len(resting)} cells resting on the inner surface of the '
            f'layer have no height. The worst measures {worst:.3e} gamma at '
            f'({place[0]:.6g}, {place[1]:.6g}, {place[2]:.6g}) — flat enough '
            f'that OpenFOAM reads cells like it as negative volume. The '
            f'stack grew {grew:.6g} m{against}, and a layer that thin under '
            f'the surface mesh around it leaves the volume mesher a gap it '
            f'can fill only with flat cells. A thinner layer will not clear '
            f'this, because the gap only gets thinner with it: refine the '
            f'surface mesh at the wall so its elements come nearer the layer '
            f'in size, or grow no layer on this geometry.',
            cells=len(flat), worst=worst, thickness=grew, asked=asked,
            at=place)

    def layer_height_groups(self, selected, heights, internal=(),
                            outward=None):
        """Split the layer bases by the direction that grows into the fluid.

        DP-55. MEASURED on ``box_with_obstacle.stl``, a 4x4x4 box holed by a
        1x1x1 cube, three layers, every boundary surface selected. All 12
        facets of the inner shell are wound out of the cube, so their normals
        point into the fluid; the outer box's 12 are wound out of the fluid.
        One sign for both put 6258 of 500430 cells inside the obstacle - three
        layers off its 2082 wall faces, plus the twelve tetrahedra that closed
        the corner - 1040 of them at negative volume, and checkMesh named
        every cell that held the inner cap non-closed at an openness of
        exactly 1. A prism's two triangular caps carry all the area in the cap
        direction and cancel; reverse one and the sum equals the sum of the
        magnitudes, which is what openness 1 means. Measured again on the
        two-file form of the same geometry in leg t6-r2 of the Plan 31
        campaign: 2082 open cells, 2082 wrongly oriented face pyramids, 2082
        concave cells, four failed checkMesh checks, and 63.156 m3 of fluid
        where the analytic answer is 63.000.

        *internal* is the CAD route's answer to the same question. The shell
        topology runs on the tessellated route only, so an OCC import arrives
        here with no voids and no windings recorded; the surfaces of every
        shell the outermost one encloses are passed in instead. A nested shell
        can only be a hole by this point, because a multi-volume assembly was
        already refused above.

        DP-71. *internal* alone was never that answer, and reading it as one
        reversed the surfaces it should have left alone. `hole != inverted` is
        two readings and the tessellated route supplies both: a void shell is
        wound out of the solid it cuts, so its normals already point into the
        fluid, and the two readings cancel. `surface_winding` is written by
        the tessellated import and by nothing else, so on the CAD route
        `inverted` was False for every surface in the run and every enclosed
        shell was reversed on the strength of half a test. MEASURED on
        `box_with_cavity.step`: three layers of prisms grown into a spherical
        cavity, 5974 open cells, and 0.0712 m3 of fluid where the analytic
        answer is 0.0568.

        *outward* is the reading that was missing, and it is a measurement
        rather than an inference: for each base, whether the surface's own
        normal -- the direction ``extrudeBoundaryLayer`` follows -- leaves the
        fluid. Where it answers it decides, because it answers the question
        the other two only approach. Probed across the 22 CAD files of the
        catalogue, one of them holed: every surface of every single-solid file
        answers, and every one of them faces out of the fluid.

        The sign the intent carries is kept as the outer shell's direction, so
        a geometry with no holes extrudes exactly as it did before.
        """
        base = list(heights)
        opposite = [-value for value in heights]
        enclosed = {int(tag) for tag in internal}
        measured = dict(outward or {})
        keep, flip = [], []
        for tag in selected:
            if int(tag) in measured:
                (keep if measured[int(tag)] else flip).append(tag)
                continue
            inverted = self.surface_winding.get(int(tag), 1) < 0
            hole = int(tag) in self.void_surfaces or int(tag) in enclosed
            # A void shell and an inverted shell each reverse the normal; both
            # together cancel back to the outer shell's direction.
            (flip if hole != inverted else keep).append(tag)
        groups = []
        if keep:
            groups.append((base, keep, False))
        if flip:
            groups.append((opposite, flip, True))
            self.warnings.append(
                f'{count_text(len(flip), "boundary layer base")} '
                f'{agreeing(len(flip), "bounds", "bound")} a hole in the '
                f'volume and {agreeing(len(flip), "was", "were")} grown '
                'inwards from it, away from the void')
        return groups

    def directions_that_meet(self, groups):
        """Which layer bases grow opposite ways and share an edge.

        DP-91. ``extrudeBoundaryLayer`` takes one signed height per call, so
        bases that grow towards each other are extruded in two calls -- and a
        curve on the boundary between them is then extruded twice, once in
        each direction. Gmsh does not notice while the geometry is built; it
        notices during the 3D pass, when the second call looks for a node the
        first one moved somewhere else.

        MEASURED on `annulus_shell.step` with layers on every wall of the
        fluid annulus. The interface with the bore is wound out of the bore,
        so its normal points into the fluid and it is grown the other way;
        the outer cylinder and the two end caps are grown along -n. The
        interface shares curve 3 with one cap and curve 2 with the other, and
        the run failed 12 s in with `Could not find extruded node
        (0.06016981, 1.1304e-08, -0.00017653) in surface 63` -- a point on the
        bore radius, one layer height off the cap. Nothing in that names a
        patch, and the same selection is what the Boundary Layers page
        proposes for this geometry.

        Returns the pairs that touch, as ``(tag, other tag)``.
        """
        if len(groups) < 2:
            return []
        curves = {}
        for _signed, tags, _flipped in groups:
            for tag in tags:
                curves[int(tag)] = set(self.surface_curves(tag))
        meetings = set()
        for index in range(len(groups)):
            for other in range(index + 1, len(groups)):
                for tag in groups[index][1]:
                    for mate in groups[other][1]:
                        if curves[int(tag)] & curves[int(mate)]:
                            meetings.add((int(tag), int(mate)))
        return sorted(meetings)

    def one_call_for_both_directions(self, groups):
        """Both senses in one group, the sense carried on each tag.

        DP-404. `directions_that_meet` finds bases that grow opposite ways
        along their own normals and share a curve, and DP-91 refused them,
        because two senses meant two calls to `extrudeBoundaryLayer` and a
        curve handed to both is extruded twice. The height is indeed signed
        once per call. The call is not: a dim-tag carries a sign of its own.

        MEASURED on Gmsh 4.15.2, one unit square, one layer, reading back
        where the nodes landed:

            tag +1  height +0.1  ->  nodes span z 0.0 .. 0.1
            tag +1  height -0.1  ->  nodes span z -0.1 .. 0.0
            tag -1  height +0.1  ->  nodes span z -0.1 .. 0.0
            tag -1  height -0.1  ->  nodes span z 0.0 .. 0.1

        Negating the tag is exactly negating the height, so `(2, -tag)` at the
        base heights grows where `(2, tag)` at the opposite heights grows, and
        one call can hold both.

        MEASURED again on the contact itself -- two unit boxes fragmented into
        a shared face, the core taken as the volume that face_s normal enters,
        layers of 3 on its five outer walls and on its half of the partition,
        which share the partition_s whole rim:

            two calls, one signed height each
                FAILED  Could not find extruded node
                        (1.051477373670381, 0.2, 1.051477373670381)
                        in surface 152
            one call, the sense carried on each tag
                MESHED  1,200 prisms

        The first line is DP-91_s failure, in DP-91_s words. The second is the
        same geometry, the same heights and the same selection.

        This is reached only where the two-call arrangement is measured not to
        work -- a contact was found -- so every selection that extrudes today
        extrudes exactly as it did.
        """
        base = next((list(signed) for signed, _tags, flipped in groups
                     if not flipped), None)
        if base is None:
            # Every group flipped: there is no contact to resolve, but the
            # sense the tags will carry has to be read against something.
            base = [-value for value in groups[0][0]] if groups else []
        tags = []
        for _signed, group, flipped in groups:
            tags.extend((-abs(int(tag)) if flipped else abs(int(tag)))
                        for tag in group)
        return [(base, tags, False)]

    def opposed_directions_refusal(self, selected, groups, meetings, skipped):
        """What to say when the contact cannot be extruded at all.

        DP-91 wrote this, and DP-404 moved it: it is the sentence the run
        makes when a contact was found *and* the one call that holds both
        senses would not run either. Every fact in it is read before anything
        is built, which is why it can still name patches -- the Gmsh failure
        it stands in for arrives part-way through the 3D pass and names a
        coordinate.
        """
        gmsh = self.gmsh
        boxes = {int(tag): gmsh.model.getBoundingBox(2, int(tag))
                 for tag in list(selected) + list(skipped)}
        touching = sorted({self.surface_patch_name(tag)[0]
                           for pair in meetings for tag in pair})
        workable = self.selection_that_would_mesh(
            selected, groups, meetings, skipped, boxes)
        advice = ('Grow layers on a set of patches that all face the same '
                  'way, mesh one volume per job, or turn boundary layers '
                  'off.')
        if workable:
            names = sorted({self.surface_patch_name(tag)[0]
                            for tag in workable})
            advice = ('Layers on ' + ', '.join(names) + ' would mesh: '
                      'what is left over is flat, and a flat opening is '
                      'one the rebuild can close. Or mesh one volume per '
                      'job, or turn boundary layers off.')
        return ('boundary layers were asked for on both sides of an edge '
                f'shared by {", ".join(touching)}, and those patches are '
                'wound opposite ways: one layer grows along its surface '
                'normal and the other against it. A shared edge cannot be '
                'extruded in two directions at once. ' + advice)

    def selection_that_would_mesh(self, selected, groups, meetings,
                                  skipped, boxes):
        """A subset of this selection whose bases no longer meet head-on.

        DP-91. The refusal above is a dead end unless it says what to do
        instead, and the run holds every fact the answer needs. Dropping the
        patches on one side of the contact removes it; which side to drop is
        decided by :meth:`patch_lies_in_a_plane`, because whatever is dropped
        joins what :meth:`rebuild_unextruded` has to close with a flat face,
        and DP-90 refuses a curved opening.

        MEASURED on `annulus_shell.step` with layers on the whole fluid
        annulus: dropping the interface leaves the outer cylinder uncovered
        and curved, which DP-90 refuses; dropping the two end caps leaves the
        interface and the outer cylinder, which meshes -- 302474 cells and
        0.01256056 m3 against an analytic 0.01256637. Returns the surviving
        tags, the largest surviving selection first, or an empty list where
        neither side can be dropped.
        """
        chosen = {int(tag) for tag in selected}
        touching = {int(tag) for pair in meetings for tag in pair}
        left_out = [int(tag) for tag in skipped]
        curves = {tag: set(self.surface_curves(tag)) for tag in chosen}
        candidates = []
        for index, (_signed, tags, _flipped) in enumerate(groups):
            dropped = sorted(touching & {int(tag) for tag in tags})
            kept = sorted(chosen - set(dropped))
            if not dropped or not kept:
                continue
            # Dropping one side has to leave the other side clear of every
            # contact, not only of the ones this side was in.
            if any(curves[one] & curves[other] for one in kept
                   for other in kept if one < other
                   and self.grow_the_same_way(groups, one, other) is False):
                continue
            if all(self.patch_lies_in_a_plane(tag, boxes[tag])
                   for tag in dropped + left_out if tag in boxes):
                candidates.append((len(kept), index, kept))
        if not candidates:
            return []
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    @staticmethod
    def grow_the_same_way(groups, one, other):
        """Are these two bases extruded in the same call, and so the same way?"""
        homes = {}
        for index, (_signed, tags, _flipped) in enumerate(groups):
            for tag in tags:
                homes[int(tag)] = index
        if one not in homes or other not in homes:
            return None
        return homes[one] == homes[other]

    #: Where on a surface to sample it, as fractions of its parametric range.
    NORMAL_SAMPLES = (0.25, 0.5, 0.75)

    def outward_normals(self, selected, volumes):
        """Which of these surfaces have their own normal leaving the fluid.

        DP-71. ``extrudeBoundaryLayer`` walks each stack along the surface's
        own normal, so the only thing the direction decision needs to know is
        where that normal points -- and on the CAD route the solid it points
        out of, or into, is still in the model when the layers are grown.
        Nothing is meshed yet, which is why the nesting reading was reached
        for; a solid can be asked about a point without meshing it.

        Returns the surfaces it could answer for and leaves the rest to the
        readings that were here before.
        """
        gmsh = self.gmsh
        if not volumes:
            return {}
        try:
            box = gmsh.model.getBoundingBox(-1, -1)
            span = max(box[axis + 3] - box[axis] for axis in range(3))
        except Exception:
            return {}
        if not span:
            return {}
        answers = {}
        for tag in selected:
            verdict = self.normal_leaves_the_fluid(
                int(tag), volumes, span * 1e-4, span * 1e-6)
            if verdict is not None:
                answers[int(tag)] = verdict
        return answers

    def normal_leaves_the_fluid(self, tag, volumes, step, slack):
        """True, False, or None where the samples do not agree on one.

        DP-71. Sample the surface, step off the point along its normal by
        *step*, and ask the solid which side each step landed on. Leaving the
        fluid forwards and entering it backwards is a normal that faces out;
        the reverse is a normal that faces in; anything else is not an answer.

        Parametric bounds are a rectangle and a trimmed face is not, so a
        sample is dropped on its coordinates rather than trusted: a point
        outside the surface's own bounding box is not on the surface. MEASURED
        across the CAD catalogue -- every surface of every single-solid file
        answers, and the two that do not belong to the one file holding two
        solids, which cannot grow layers at all.

        ``getBoundary(..., oriented=True)`` was tried for this first and
        cannot serve: probed live on Gmsh 4.15.2 its signs are mixed within a
        single shell, and on ``box_with_cavity.step`` three of the box's six
        faces carry the sign opposite to the other three while all six face
        the same way.
        """
        gmsh = self.gmsh
        try:
            low, high = gmsh.model.getParametrizationBounds(2, tag)
            corners = gmsh.model.getBoundingBox(2, tag)
        except Exception:
            return None
        votes = []
        for first in self.NORMAL_SAMPLES:
            for second in self.NORMAL_SAMPLES:
                params = [low[0] + (high[0] - low[0]) * first,
                          low[1] + (high[1] - low[1]) * second]
                try:
                    point = gmsh.model.getValue(2, tag, params)
                    normal = gmsh.model.getNormal(tag, params)
                    if any(not corners[axis] - slack <= point[axis]
                           <= corners[axis + 3] + slack for axis in range(3)):
                        continue
                    ahead = [point[axis] + normal[axis] * step
                             for axis in range(3)]
                    behind = [point[axis] - normal[axis] * step
                              for axis in range(3)]
                    forward = any(gmsh.model.isInside(3, volume, ahead)
                                  for volume in volumes)
                    backward = any(gmsh.model.isInside(3, volume, behind)
                                   for volume in volumes)
                except Exception:
                    return None
                if bool(forward) == bool(backward):
                    continue
                votes.append(not forward)
        if not votes or len(set(votes)) != 1:
            return None
        return votes[0]

    def volume_surfaces(self, volume):
        """The surfaces bounding one volume, unsigned.

        The sign `getBoundary` returns is not read anywhere: probed live on
        Gmsh 4.15.2 it is mixed within a single shell, which is why
        `normal_leaves_the_fluid` exists.
        """
        return [abs(int(tag)) for _dim, tag in
                self.gmsh.model.getBoundary([(3, int(volume))],
                                            oriented=False)]

    def surface_owners(self, volumes):
        """Which volumes each surface bounds, as ``{surface: {volume, ...}}``.

        One notion of adjacency for the two questions that need it: which
        single volume every layer base bounds, and which surfaces bound more
        than one. `getBoundary` is the source for both, so the second cannot
        answer differently from the first.
        """
        owners: dict[int, set] = {}
        for volume in volumes:
            for tag in self.volume_surfaces(volume):
                owners.setdefault(int(tag), set()).add(int(volume))
        return owners

    def volume_the_layer_grows_into(self, volumes, selected):
        """Which volume every selected base bounds, if there is just one.

        R118. A face shared by two volumes bounds both, so the answer is the
        intersection and not the union: on `annulus_shell.step` the fluid
        annulus is named by its outer wall alone, while the interface it
        shares with the solid bore names both and settles nothing.

        Returns ``(volume, None)`` when one volume carries every base,
        ``(None, candidates)`` when more than one still could -- a selection
        made entirely of shared faces -- and ``(None, [])`` when the bases
        span volumes and no single core can be rebuilt.
        """
        owners = self.surface_owners(volumes)
        common = {int(volume) for volume in volumes}
        for tag in selected:
            common &= owners.get(int(tag), set())
        if len(common) == 1:
            return next(iter(common)), None
        return None, sorted(common)

    def keep_shared_surfaces(self, volumes, selected, skipped):
        """Move a surface two volumes share out of ``skipped`` and into it.

        DP-398. `rebuild_unextruded` deletes every skipped surface and builds
        a plane across the rim in its place, because the layer's laterals lie
        in the neighbouring patch's plane and reusing the patch would cover it
        twice. On the outside of a volume that is right. On a surface two
        volumes share it is not: only the volume the layer is carved out of is
        rebuilt, so the carved side gets the new plane and the other side
        keeps the original, and an interface that was one face becomes two
        triangulations that meet nowhere.

        MEASURED on the four multiregion STEP fixtures. `shell_and_tube` and
        `coaxial_ducts` name their interface as a layer base -- `wall1` in
        both -- and publish, with the interface internal and every boundary
        face named. `tee_with_plug` names `wall4`..`wall7` and not its
        interface `wall3`; `baffled_chamber` names `wall7`..`wall12` and
        `wall14`, the copy the fusion had already deleted. Both refuse, with
        884 and 4,200 unnamed boundary faces covering 0.0020289 m2 and
        0.0159999 m2 -- twice 0.0010179 and twice 0.008, their two interface
        areas, present once from each side.

        A base survives the carve: the prisms stand on it and it goes on
        bounding both volumes. So a shared surface is made a base. The cost is
        a layer grown on the interface, which is what the two fixtures that
        already work already do, and what a conjugate interface wants.

        This is called once the core volume is known and `skipped` has been
        recomputed from that core's surfaces, so every tag it can reach bounds
        the core already; what it tests is whether the tag bounds something
        else as well.
        """
        # DP-57's refusal stands: growing on nothing is an answer, and this
        # is not the place to turn it into something. The caller has returned
        # on an empty selection long before here; the guard is kept so that
        # staying true of this method does not depend on the caller.
        if not selected:
            return selected, skipped
        owners = self.surface_owners(volumes)
        shared = [tag for tag in skipped if len(owners.get(int(tag), ())) > 1]
        if not shared:
            return selected, skipped
        names = sorted({self.surface_patch_name(tag)[0] for tag in shared})
        self.warnings.append(
            'boundary layers were not asked for on ' + ', '.join(names)
            + ', and each of those is shared by two regions. A patch left out '
            'of the layer is rebuilt for the region the layer is carved out '
            'of alone, which would leave the two regions meeting across two '
            'separate surfaces instead of one, so the layer was grown on '
            'them as well to keep the interface conformal.')
        for tag in shared:
            self.ledger.record(
                f'layerPatch:{self.surface_patch_name(tag)[0]}',
                False, True,
                note='shared by two regions: not asked for, grown anyway to '
                     'keep the interface one surface')
        keep = {int(tag) for tag in shared}
        return (list(selected) + list(shared),
                [tag for tag in skipped if int(tag) not in keep])

    def layer_surfaces(self, surfaces, layers):
        """Split the boundary into the surfaces that grow layers and the rest.

        R118. MEASURED on venturi.stl: with no selection every boundary surface
        is extruded, and the worst elements of the finished mesh sat on the
        inlet plane (z=0.004999) and the outlet plane (z=0.5937 / 0.5967) --
        the very stack that failed the quality gate of R113. Prism layers on an
        inlet and an outlet are wrong for every flow case, so the plan may name
        the patches that get them.

        Plan 33 section 1.1 retired the rest of that rule. An empty selection
        used to mean every boundary surface, which is the defect above with
        the user's own silence for a cause, and no reader of the page could
        tell the two apart. The plan now carries the choice as well as the
        list: ``selected`` is the list and nothing else, empty included, and
        ``all_eligible_walls`` reads the walls off the surfaces this run
        actually imported, through the same rule the page ticks its rows
        with.
        """
        mode = normalise_mode(layers.get('patchMode'))
        wanted = {str(item).strip() for item in (layers.get('patches') or ())
                  if str(item).strip()}
        if not mode:
            # A job written before the choice existed. Read it the way the
            # saved case it came from is read, so one plan cannot mean two
            # things depending on which side of the seam is looking.
            mode = MODE_ALL_WALLS if not wanted else 'selected'
        if mode == MODE_ALL_WALLS:
            # DP-867. Judged by the category each patch publishes with,
            # which the job carries; a name alone (`face0`) says nothing.
            categories = self.job.get('surfaceCategories') or {}
            names = [self.surface_patch_name(tag)[0] for tag in surfaces]
            wanted = set(eligible_wall_names(
                (name, categories.get(name, '')) for name in names))
            if not wanted:
                self.warnings.append(
                    'every eligible wall was asked for and this import names '
                    'no boundary as a wall, so no layer could be grown. Name '
                    'the surfaces that are walls, or choose them here by '
                    'hand.')
                return [], list(surfaces)
        elif not wanted:
            # Growing on nothing is an answer. It is refused above this, in
            # `apply_boundary_layers`, rather than turned into everything.
            return [], list(surfaces)
        # Plan 30 WP12, F-26. The comparison was exact, and on a tessellated
        # import the surfaces are called `face_3` while the page offers the
        # prepared patch names, so a selection matched nothing and the run
        # said only "matched no imported surface" -- with no way to learn what
        # the surfaces were called instead. Matching is now case- and
        # separator-insensitive, the generated `face_N` spelling is accepted
        # as well as the prepared name, and a miss says what was on offer.
        folded = {self._fold_patch_name(item): item for item in wanted}
        # DP-500. The generated spelling is a Gmsh tag, numbered from 1, and
        # a prepared CAD import names its faces `face0`, `face1`, ... from 0.
        # Folded, `face_2` (the tag of prepared `face1`) *is* `face2`, so a
        # selection of face2-face5 also grew prisms off the unselected outlet
        # face1. MEASURED on G4 `duct.step`: five surfaces extruded for four
        # selected, 37 prism columns standing on the outlet plane. A generated
        # alias that spells a name the prepared geometry declared belongs to
        # that name's surface, never to this one.
        declared = {self._fold_patch_name(item)
                    for item in self.declared_surface_names()}
        selected, skipped, present, matched = [], [], {}, set()
        for tag in surfaces:
            name, known = self.surface_patch_name(tag)
            present[name] = present.get(name, 0) + 1
            own = self._fold_patch_name(name)
            alias = self._fold_patch_name(
                f'face_{self.surface_origin.get(tag, tag)}')
            keys = {own}
            if alias == own or alias not in declared:
                keys.add(alias)
            # DP-410. Ditto: the face the fusion kept answers to both names.
            keys.update(self._fold_patch_name(alias) for alias
                        in self.merged_surface_names.get(int(tag), ()))
            hit = keys & set(folded)
            if hit:
                matched.update(folded[key] for key in hit)
                selected.append(tag)
            else:
                skipped.append(tag)
            if not known and hit:
                # The name matched the fallback, not a prepared patch. Worth
                # recording: the same job on renamed geometry would not match.
                self.ledger.record(
                    f'layerPatch:{name}', name, name,
                    note='matched a generated surface name, not a prepared '
                         'patch name')
        missing = sorted(wanted - matched)
        if missing:
            # DP-399. Two unlike things arrive here. A name the prepared
            # geometry never declared is a selection pointing at nothing, and
            # the list of what was imported answers it. A name the prepared
            # geometry *did* declare, whose surface this import no longer
            # has, is the run losing work the user did: the wall publishes
            # bare, and the layer summary -- which counts the patches it grew
            # on against the patches it found -- reports full coverage over
            # the ones that remain. Nothing in the finished case records that
            # a wall was ever asked for, which is why this one is a refusal.
            lost = [name for name in missing
                    if name in self.names_without_surface]
            offer = ', '.join(sorted(present)) or 'none'
            if lost:
                fused = (
                    f' Import fused '
                    f'{count_text(self.duplicate_faces_fused, "face")} as '
                    'duplicates, which is what takes a prepared name away; '
                    'turn off "remove duplicate faces" if these surfaces are '
                    'meant to stay separate.'
                    if self.duplicate_faces_fused else '')
                raise MeshFailure(
                    'boundary layers were asked for on ' + ', '.join(lost)
                    + ', which the prepared geometry names but no surface in '
                    'this model carries, so the layers cannot be grown and '
                    f'the patch would publish bare.{fused} The surfaces this '
                    f'run imported are: {offer}.')
            reason = (
                'boundary layers were asked for on ' + ', '.join(missing)
                + f', which matched no imported surface. The surfaces this '
                f'run imported are: {offer}.')
            if self.tessellated and not any(
                    self.surface_patch_name(tag)[1] for tag in surfaces):
                reason += (' A tessellated import carries no patch names of '
                           'its own, so the surfaces are numbered rather than '
                           'named; prepare the geometry with named groups, or '
                           'name the numbered faces above.')
            self.warnings.append(reason)
        return selected, skipped

    @staticmethod
    def _fold_patch_name(name) -> str:
        """``Inlet-Top`` and ``inlet_top`` are the same patch to a user."""
        return ''.join(character for character in str(name).strip().lower()
                       if character.isalnum())

    def remove_entities(self, dim_tags):
        """Delete entities, whichever kernel is holding them.

        MEASURED (R118) on a discrete import: ``model.removeEntities`` alone
        leaves a classified surface in place while the geo kernel still names
        it in the surface loop of a volume, and the next ``geo.synchronize()``
        puts the surface back complete with its triangles. The z=0 plane then
        carried 0.02 m2 of facets for a 0.01 m2 face and the mesher refused the
        domain with "PLC Error: a segment and a facet intersect". The owning
        kernel has to forget the entity too.
        """
        dim_tags = list(dim_tags)
        if not dim_tags:
            return
        for kernel in (self.gmsh.model.geo, self.gmsh.model.occ):
            try:
                kernel.remove(dim_tags, recursive=False)
                kernel.synchronize()
            except Exception:                    # noqa: BLE001 - other kernel
                pass
        try:
            self.gmsh.model.removeEntities(dim_tags, False)
        except Exception:                        # noqa: BLE001 - already gone
            pass

    def surface_curves(self, tag):
        """The curves bounding one surface, unsigned."""
        return {abs(curve) for _dim, curve in self.gmsh.model.getBoundary(
            [(2, tag)], oriented=False, recursive=False)}

    def boundary_shells(self, surfaces):
        """The boundary split into connected shells, by shared curves.

        DP-55. Two surfaces that share a bounding curve are two faces of the
        same closed surface, so the transitive closure of that relation is a
        shell. A box with an obstacle inside it is two of them, and they want
        their layers grown in opposite directions.
        """
        parent = {tag: tag for tag in surfaces}

        def find(tag):
            while parent[tag] != tag:
                parent[tag] = parent[parent[tag]]
                tag = parent[tag]
            return tag

        owners: dict[int, int] = {}
        for tag in surfaces:
            for curve in self.surface_curves(tag):
                other = owners.setdefault(curve, tag)
                left, right = find(tag), find(other)
                if left != right:
                    parent[left] = right
        shells: dict[int, list[int]] = {}
        for tag in surfaces:
            shells.setdefault(find(tag), []).append(tag)
        return [sorted(group) for group in shells.values()]

    def internal_shells(self, shells):
        """The surfaces of every shell the outermost one encloses.

        DP-55. Bounding-box containment, because it answers from geometry the
        model already has. Where the tessellated route ran, the shell topology
        has already said which shells are voids and which way each is wound,
        and that reading is the better one; this is the CAD route's answer,
        where nothing is meshed until after the layers are grown. The sign of
        each shell's own volume read off its surface mesh would also serve
        (+64 for a 4 m box, +1.0 for the 1 m cube cut out of it, measured
        live), and there is no surface mesh here to read it from.

        A nested shell can only be a hole, because a multi-volume assembly is
        refused before this point.

        Empty where there is one shell, or where no single shell encloses all
        the others: then every surface keeps the direction the plan asked for,
        which is what this did before it could tell them apart.
        """
        if len(shells) < 2:
            return set()
        boxes = []
        for group in shells:
            corners = [self.gmsh.model.getBoundingBox(2, tag) for tag in group]
            boxes.append(tuple(
                [min(corner[axis] for corner in corners) for axis in range(3)]
                + [max(corner[axis] for corner in corners)
                   for axis in range(3, 6)]))
        slack = max(box[axis + 3] - box[axis]
                    for box in boxes for axis in range(3)) * 1e-9

        def encloses(outer, inner):
            return (all(outer[axis] <= inner[axis] + slack
                        for axis in range(3))
                    and all(outer[axis] >= inner[axis] - slack
                            for axis in range(3, 6)))

        enclosing = [index for index, box in enumerate(boxes)
                     if all(encloses(box, other)
                            for position, other in enumerate(boxes)
                            if position != index)]
        if len(enclosing) != 1:
            return set()
        return {tag for index, group in enumerate(shells)
                if index != enclosing[0] for tag in group}

    def shells_kept_whole(self, shells, selected, internal):
        """The core's shells that close it without being rebuilt.

        DP-124. :meth:`rebuild_unextruded` replaces every un-layered surface
        with a flat face stitched into the rim the layer stopped at, because
        the layer's own lateral faces lie in the plane of the surface it grew
        beside and the two would overlap. That reasoning is about a surface
        the layer *reaches*. A shell it never touches is not in the way of
        anything: it is still closed, still meshed and still named, and
        rebuilding it is neither possible nor wanted.

        MEASURED on `two_solid_block.stl`, one wrapped file holding a box
        inside a box. The farfield is a single volume bounded by two disjoint
        shells; with layers on the outer one every curve of every layer top is
        shared with another top, so `rim_loops` returns nothing at all and the
        rebuild refused with "the boundary layer left no rim to rebuild the
        un-layered patches from". Kept whole instead, the inner shell goes to
        `addVolume` as the hole it always was.

        Only a shell the layered one encloses is kept. The geo kernel reads
        the first surface loop of a volume as its exterior and the rest as
        holes, so an un-layered shell *outside* the layer -- an obstacle given
        layers inside a box that was not -- cannot be a loop after the first.
        That one is left in `skipped` and refused by the rebuild as before,
        rather than built into a volume turned inside out.
        """
        chosen = {int(tag) for tag in selected}
        inside = {int(tag) for tag in internal}
        kept = []
        for group in shells:
            tags = sorted(int(tag) for tag in group)
            if any(tag in chosen for tag in tags):
                continue
            if tags and all(tag in inside for tag in tags):
                kept.append(tags)
        return kept

    def shell_kept_as_exterior(self, shells, selected, internal):
        """The un-layered shell the core is bounded by from outside.

        DP-127. :meth:`shells_kept_whole` reads the same fact -- a shell the
        layer never reached needs no rebuild -- and only acts on it where the
        shell is a hole. MEASURED on `turbine_cascade`, two files and one
        fluid volume: the layer is on the blades (`99 boundary layer base(s)
        bound a hole in the volume and were grown inwards from it`) and the
        farfield around them carries none. So the untouched shell is the
        enclosing one, DP-124 declined it, the rebuild was handed it, and a
        layer that wraps a blade leaves no rim for a flat face to stitch into:
        the run died on "the boundary layer left no rim to rebuild the
        un-layered patches from".

        That volume is not ill-defined. It is bounded outside by the farfield
        and inside by the layer tops, and `addVolume` spells exactly that --
        the kept shell first, one loop of tops per body after it.

        Kept only where the split is clean: one shell that encloses all the
        others and carries no layer, and every enclosed shell layered whole.
        A shell the layer reached in part has its own faces in the way of the
        layer's laterals, which is what the rebuild exists for, so a mixture
        goes there as before rather than into a volume that covers a plane
        twice.
        """
        if len(shells) < 2 or not internal:
            return []
        chosen = {int(tag) for tag in selected}
        inside = {int(tag) for tag in internal}
        outside, enclosed = [], []
        for group in shells:
            tags = sorted(int(tag) for tag in group)
            (enclosed if tags and all(tag in inside for tag in tags)
             else outside).append(tags)
        if len(outside) != 1 or not enclosed:
            return []
        if any(tag in chosen for tag in outside[0]):
            return []
        if not all(all(tag in chosen for tag in group) for group in enclosed):
            return []
        return outside[0]

    def layer_tops_by_shell(self, shells, columns):
        """Each shell's layer tops, grouped so they can close one hole.

        DP-127. `addVolume` takes one surface loop per hole, and a cascade
        has a hole per blade. The grouping is read off the columns rather
        than the model: the top of a column stands on its base, so the tops
        of a shell's bases close around that shell and nothing else.
        """
        top_of = {int(base): int(top) for base, top in columns}
        groups = []
        for group in shells:
            tops = [top_of[int(tag)] for tag in group if int(tag) in top_of]
            if tops:
                groups.append(tops)
        return groups

    def rim_owners(self, tops):
        """Which layer top each rim curve belongs to.

        Where two extruded surfaces meet, their tops share a curve. A curve
        owned by a single top is on the rim where the layer stops.
        """
        seen: dict[int, list] = {}
        for tag in tops:
            for curve in self.surface_curves(tag):
                seen.setdefault(curve, []).append(int(tag))
        return {curve: tags[0] for curve, tags in seen.items()
                if len(tags) == 1}

    def rim_loops(self, tops):
        """Closed loops of the curves that bound exactly one layer top.

        Where two extruded surfaces meet, their tops share a curve. A curve
        owned by a single top is therefore on the rim where the layer stops --
        the outline of the hole the un-extruded surface has to fill.
        """
        rim = list(self.rim_owners(tops))
        parent = {curve: curve for curve in rim}

        def find(curve):
            while parent[curve] != curve:
                parent[curve] = parent[parent[curve]]
                curve = parent[curve]
            return curve

        by_point: dict[int, list[int]] = {}
        for curve in rim:
            for _dim, point in self.gmsh.model.getBoundary(
                    [(1, curve)], oriented=False, recursive=False):
                by_point.setdefault(abs(point), []).append(curve)
        for shared in by_point.values():
            for other in shared[1:]:
                parent[find(other)] = find(shared[0])
        loops: dict[int, list[int]] = {}
        for curve in rim:
            loops.setdefault(find(curve), []).append(curve)
        return list(loops.values())

    def patch_lies_in_a_plane(self, tag, box):
        """Is this patch flat enough to be stood in for by a plane face?

        DP-90. :meth:`rebuild_unextruded` closes the opening a layer leaves
        with ``addPlaneSurface``, and every guard it had reads the *rim*
        rather than the patch. MEASURED on `annulus_shell.step`, layers on
        `wall4`, `wall5` and `wall6` -- the three faces of the fluid annulus
        that are not its interface with the bore. Each rim of that interface
        is a single closed circle, so it bounds exactly one point:
        `_box_holding` attributes it freely and `_out_of_plane` reports 0.0
        over fewer than four points. Both guards passed vacuously and the two
        rims, 0.4 m apart, were welded into one plane surface as outline and
        hole. The core came out open on 124 edges and the 3D pass spun on it
        for over 45 minutes emitting nothing -- `generate` has no heartbeat,
        so the GUI showed a run at 35% that never moved and had to be killed.

        The same blindness is worse where it finishes. With layers on `wall4`
        alone the interface is left with no rim at all, dropped, and the core
        closed as though the bore were not there: 0.01707852 m3 of cells
        against an analytic 0.01256637, the bore's 0.00452 m3 meshed twice
        and the two regions overlapping -- and the run published `succeeded`.

        The normal of a plane is the same everywhere on it. Sampled the way
        :meth:`normal_leaves_the_fluid` samples, that answers for a slanted
        face as well as an axis-aligned one, which a bounding box does not.
        A patch that cannot be asked is called flat: an unanswerable one
        keeps the shipped behaviour rather than refusing a run that worked.
        """
        gmsh = self.gmsh
        try:
            low, high = gmsh.model.getParametrizationBounds(2, tag)
        except Exception:                    # noqa: BLE001 - discrete surface
            return True
        span = max(box[axis + 3] - box[axis] for axis in range(3))
        slack = max(span * 1e-6, 1e-9)
        normals = []
        for first in self.NORMAL_SAMPLES:
            for second in self.NORMAL_SAMPLES:
                params = [low[0] + (high[0] - low[0]) * first,
                          low[1] + (high[1] - low[1]) * second]
                try:
                    point = gmsh.model.getValue(2, tag, params)
                    normal = gmsh.model.getNormal(tag, params)
                except Exception:            # noqa: BLE001 - not parametrised
                    return True
                # Parametric bounds are a rectangle and a trimmed face is
                # not, so a sample off the patch says nothing about it.
                if any(not box[axis] - slack <= point[axis]
                       <= box[axis + 3] + slack for axis in range(3)):
                    continue
                length = math.sqrt(sum(value * value for value in normal))
                if length <= 0.0:
                    continue
                normals.append([value / length for value in normal])
        if len(normals) < 2:
            return True
        first = normals[0]
        # Unsigned: a parametrisation may flip the normal across the patch
        # without the patch bending at all.
        return all(abs(sum(first[axis] * other[axis] for axis in range(3)))
                   >= 1.0 - 1e-4 for other in normals[1:])

    def rebuild_unextruded(self, skipped, tops, boxes, columns):
        """Close the core volume across the surfaces that grew no layer.

        R118. The core is rebuilt from the extrusion's *top* surfaces, and with
        only some surfaces extruded that loop is open. The un-extruded surface
        cannot simply join it: the layer's own lateral faces lie in the same
        plane, so the two overlap. MEASURED on a 0.1 x 0.1 x 0.6 m duct, layers
        on the four walls only -- reusing the caps gave "PLC Error: a segment
        and a facet intersect", adding the laterals gave "Invalid boundary mesh
        (overlapping facets)", and the z=0 plane was covered twice over,
        0.02 m2 of facets for a 0.01 m2 face. Rebuilding each un-extruded
        surface from the rim the extrusion left, and deleting the original,
        closes it: 14,199 tets + 16,712 prisms, 0.005999988 m3 against an
        analytic 0.006, and not one prism rooted on either cap (against 388 on
        the same duct with every surface extruded).
        """
        gmsh = self.gmsh
        rebuilt = {}
        # R118. Every geometric read below -- the rim's points,
        # its plane, its span -- goes through `gmsh.model`, and
        # the extrusion built its rims in the `geo` kernel.
        # MEASURED on `annulus_shell` without this call: all four
        # rim curves answer `Empty bounding box`, parametrise as
        # (0, 1) with `getValue` returning the origin, and bound
        # no points at all -- so every guard here passed
        # vacuously, all four rims were attributed to one cap and
        # the plane surface built across them held two concentric
        # circles in one loop. Gmsh then spun on `8 intersections
        # in the 1D mesh (curves 10 10 12 12 33 33 35 35)` for
        # over thirty minutes without growing by a byte.
        gmsh.model.geo.synchronize()
        owners = self.rim_owners(tops)
        bases = {int(top): int(base) for base, top in columns}
        names = sorted({self.surface_patch_name(tag)[0] for tag in skipped})
        # DP-90. The closure built below is a plane, so a patch that is not
        # one cannot be stood in for. Refused here, where the patch still has
        # a name, rather than left to a 3D pass that does not return.
        curved = [tag for tag in skipped
                  if not self.patch_lies_in_a_plane(tag, boxes[tag])]
        if curved:
            bent = sorted({self.surface_patch_name(tag)[0] for tag in curved})
            raise MeshFailure(
                'the patches left without a boundary layer — '
                + ', '.join(bent) + ' — are not flat, and the opening a '
                'layer leaves can only be closed with a flat face. A curved '
                'patch cannot be left uncovered: the core would be rebuilt as '
                'though the curve were not there. Name a set of patches that '
                'leaves only flat ones uncovered, or turn layers off.')
        rings = []
        for curves in self.rim_loops(tops):
            # MEASURED: the rim only outlines the un-layered patch when the
            # layer surrounds it. Asking for layers on the inlet of the duct
            # and *not* on its walls leaves a rim that loops round the tube
            # instead, and the plane surface built across it cuts through the
            # domain -- the run died four stages later on "Invalid boundary
            # mesh (overlapping facets) on surface 63 surface 64". A rim that
            # does not sit inside the patch it is standing in for, or that is
            # not flat, is refused here where the cause can still be named.
            points = self._loop_points(curves)
            owner = self._box_holding(boxes, points)
            if owner is None:
                raise MeshFailure(
                    'the boundary layer leaves an opening that does not lie '
                    'on any of the patches without one (' + ', '.join(names)
                    + '), so it cannot be closed: the layer has to surround '
                    'them. Grow the layer on the patches around them too, or '
                    'turn layers off.')
            deviation, extent = self._out_of_plane(points)
            if deviation > max(extent * 1e-4, 1e-12):
                raise MeshFailure(
                    'the patches left without a boundary layer — '
                    + ', '.join(names) + f' — span {deviation:.4g} m out of '
                    'any one plane, and the opening the layer leaves can only '
                    'be closed with a flat face. Grow the layer on them too, '
                    'or turn layers off.')
            rings.append((owner, curves))
        grouped: dict[int, list[list[int]]] = {}
        for owner, curves in rings:
            grouped.setdefault(owner, []).append(curves)
        for owner, members in grouped.items():
            # An annular opening leaves two rims in one plane, and one plane
            # surface per rim lays a disc across the bore. MEASURED on
            # `annulus_shell`: the fluid is the space between two cylinders,
            # so each end cap is bounded by the inner layer's rim and the
            # outer layer's rim together. The widest rim is the outline and
            # every other one is a hole in it.
            flat = self._flat_axis(boxes[owner])
            members.sort(reverse=True, key=lambda ring: self._rim_rank(
                ring, owners, bases, flat))
            try:
                loops = [gmsh.model.geo.addCurveLoop(ring, reorient=True)
                         for ring in members]
                surface = gmsh.model.geo.addPlaneSurface(loops)
            except Exception as error:
                raise MeshFailure(
                    'the patches left without a boundary layer could not be '
                    f'rebuilt to close the core volume: {error}. Grow the '
                    'layer on every patch, or turn layers off.') from error
            rebuilt[surface] = [curve for ring in members for curve in ring]
        if not rebuilt:
            raise MeshFailure(
                'the boundary layer left no rim to rebuild the un-layered '
                'patches from, so the core volume cannot be closed')
        gmsh.model.geo.synchronize()
        # The originals still carry their triangles; clearing the mesh first is
        # what keeps them out of the z=0 plane when the kernel is discrete.
        gmsh.model.mesh.clear([(2, tag) for tag in skipped])
        self.remove_entities([(2, tag) for tag in skipped])
        for tag in skipped:
            self.layer_replacements.setdefault(tag, [])
        return rebuilt

    def _loop_points(self, curves):
        """Coordinates of the points bounding a set of curves.

        R118. `combined` defaults to true, and a closed curve begins and
        ends at one point: combining cancels the two occurrences against
        each other and the curve is reported as bounding nothing at all.
        MEASURED on `annulus_shell`, whose four rims are full circles --
        every one of them returned no points, `_box_holding` then held
        them all to be inside the first cap it looked at, and the plane
        surface built across them carried two concentric circles in one
        loop. Gmsh spun on `8 intersections in the 1D mesh` for over
        thirty minutes.
        """
        seen, points = set(), []
        for curve in curves:
            for _dim, point in self.gmsh.model.getBoundary(
                    [(1, curve)], oriented=False, combined=False,
                    recursive=False):
                tag = abs(point)
                if tag not in seen:
                    seen.add(tag)
                    points.append(list(self.gmsh.model.getValue(0, tag, [])))
        return points

    def _flat_axis(self, box):
        """The axis an un-layered patch has no thickness in."""
        extents = [box[axis + 3] - box[axis] for axis in range(3)]
        return extents.index(min(extents))

    def _rim_rank(self, curves, owners, bases, flat):
        """How wide the layer this rim belongs to started out, in the cap.

        R118. The rim itself cannot be measured: it is a `geo` curve the
        extrusion made, and MEASURED on `annulus_shell` those answer `Empty
        bounding box`, parametrise as (0, 1) and evaluate to the origin. The
        surface the layer grew from is the imported one, and that has real
        coordinates -- so the rims of one cap are ranked by the bases behind
        them, measured across the cap rather than along it.

        This is exact for the shape that needs it, nested shells, where the
        rim is its base offset by the layer thickness. It would not be for a
        base that narrows away from the cap, and no such case is meshed here:
        a cap with a single rim never asks the question.
        """
        spans = []
        for curve in curves:
            base = bases.get(owners.get(int(curve)))
            if base is None:
                continue
            try:
                box = self.gmsh.model.getBoundingBox(2, int(base))
            except Exception:                                # noqa: BLE001
                continue
            spans.append(max(box[axis + 3] - box[axis]
                             for axis in range(3) if axis != flat))
        return max(spans) if spans else 0.0

    @staticmethod
    def _box_holding(boxes, points):
        """Which un-layered surface's box holds all of these points.

        `_within_any` answered whether any of them did. The rebuild now has
        to group the rims by the patch each one stands in, because a patch
        can be left with more than one -- so the answer has to say which.
        """
        # An empty set of points is held by every box: `all` over nothing
        # is true. MEASURED, that is not a hypothetical -- a rim made of
        # full circles bounds no points at all, and the first box in the
        # dictionary then claimed all four of them.
        if not points:
            return None
        for tag, box in boxes.items():
            span = max(box[index + 3] - box[index] for index in range(3))
            slack = max(span * 1e-6, 1e-9)
            if all(box[index] - slack <= point[index] <= box[index + 3] + slack
                   for point in points for index in range(3)):
                return tag
        return None

    @staticmethod
    def _within_any(boxes, points):
        """Do all these points sit inside one un-layered surface's box?

        The rebuilt face stands in for a patch, so it has to be where that
        patch was. Its own bounding box cannot be asked for -- a geo surface
        that has not been meshed yet answers +-DBL_MAX -- but the points of
        its rim have real coordinates.
        """
        # An empty set of points is held by every box: `all` over nothing
        # is true. MEASURED, that is not a hypothetical -- a rim made of
        # full circles bounds no points at all, and the first box in the
        # dictionary then claimed all four of them.
        if not points:
            return False
        for box in boxes.values():
            span = max(box[index + 3] - box[index] for index in range(3))
            slack = max(span * 1e-6, 1e-9)
            if all(box[index] - slack <= point[index] <= box[index + 3] + slack
                   for point in points for index in range(3)):
                return True
        return False

    @staticmethod
    def _out_of_plane(points):
        """``(worst distance from the best plane, size of the rim)``, metres.

        Three points always lie in a plane, so a rim of three or fewer is flat
        by definition and reports zero.
        """
        if len(points) < 4:
            return 0.0, 0.0
        origin = points[0]

        def offset(point):
            return [point[index] - origin[index] for index in range(3)]

        def norm(vector):
            return math.sqrt(sum(value * value for value in vector))

        extent = max(norm(offset(point)) for point in points)
        first = max(points, key=lambda point: norm(offset(point)))
        base = offset(first)

        def cross(left, right):
            return [left[1] * right[2] - left[2] * right[1],
                    left[2] * right[0] - left[0] * right[2],
                    left[0] * right[1] - left[1] * right[0]]

        normal = max((cross(base, offset(point)) for point in points),
                     key=norm)
        length = norm(normal)
        if length <= 0.0:
            return 0.0, extent           # every point on one line: still flat
        unit = [value / length for value in normal]
        deviation = max(abs(sum(unit[index] * offset(point)[index]
                                for index in range(3)))
                        for point in points)
        return deviation, extent

    def name_layer_replacements(self, skipped_curves, rebuilt_loops, laterals):
        """Hand each new face the patch name of the surface it replaced.

        An un-layered inlet ends up covered by the extrusion's own lateral
        faces -- the frame the layer leaves in that plane -- plus the rebuilt
        inner face. Both are real boundary faces and both must carry the patch
        name, or publication refuses the mesh for boundary faces no physical
        group names.

        MEASURED (R118): geometry cannot do this attribution. Asked right after
        the rebuild, ``getBoundingBox`` returns +-DBL_MAX for a freshly added
        plane surface and raises "Empty bounding box" for every extruded
        lateral, so the two caps of the live duct came back unnamed and the
        mesh published with no inlet and no outlet at all. Topology answers:
        a lateral was extruded along a curve that bounded the surface it now
        stands in for, and the rebuilt face is stitched to the far edge of
        those same laterals.
        """
        gmsh = self.gmsh
        owner_of_curve: dict[int, list[int]] = {}
        for tag, curves in skipped_curves.items():
            for curve in curves:
                owner_of_curve.setdefault(curve, []).append(tag)
        lateral_owner: dict[int, int] = {}
        for tag in laterals:
            owners = {owner for curve in self.surface_curves(tag)
                      for owner in owner_of_curve.get(curve, ())}
            if len(owners) == 1:
                owner = owners.pop()
                lateral_owner[tag] = owner
                self.layer_replacements[owner].append(tag)
        for surface, curves in rebuilt_loops.items():
            votes: dict[int, int] = {}
            for curve in curves:
                upward, _down = gmsh.model.getAdjacencies(1, curve)
                for neighbour in upward:
                    owner = lateral_owner.get(int(neighbour))
                    if owner is not None:
                        votes[owner] = votes.get(owner, 0) + 1
            if not votes:
                continue
            owner = max(votes, key=lambda tag: (votes[tag], -tag))
            names = sorted({self.surface_patch_name(tag)[0] for tag in votes})
            if len(names) > 1:
                # Two un-layered patches meeting edge to edge share one hole,
                # and one rebuilt face cannot belong to both.
                self.warnings.append(
                    'patches ' + ', '.join(names) + ' grew no boundary layer '
                    'and meet across the same opening; the face rebuilt to '
                    f'close it publishes as {self.surface_patch_name(owner)[0]}')
            self.layer_replacements[owner].append(surface)
        for tag, members in self.layer_replacements.items():
            if not members:
                name, _known = self.surface_patch_name(tag)
                self.warnings.append(
                    f'patch {name!r} grew no boundary layer and no rebuilt '
                    'face could be traced back to it, so it may publish with '
                    'fewer faces than it had')

    def apply_periodic(self):
        pairs = (self.intent.get('periodic') or {}).get('pairs') or []
        if not pairs:
            return
        applied = 0
        for pair in pairs:
            master = self.scope_surfaces.get(pair['masterScope'])
            slave = self.scope_surfaces.get(pair['slaveScope'])
            if not master or not slave:
                self.warnings.append(
                    f'periodic pair {pair["name"]!r} could not resolve both '
                    'scopes and was skipped')
                self.ledger.record(f'periodic:{pair["name"]}',
                                   pair['transform'], None, applied=False,
                                   note='scope did not resolve')
                continue
            try:
                self.gmsh.model.mesh.setPeriodic(
                    2, slave, master, [float(item) for item in pair['affine']])
            except Exception as error:
                # The commonest cause by far is a transform pointing the wrong
                # way: it must map the master surface onto the slave.
                raise MeshFailure(
                    f'periodic pair {pair["name"]!r} was rejected by Gmsh: '
                    f'{error}. Check that the transform maps the master '
                    f'surface onto the slave and not the reverse.') from error
            applied += 1
            self.periodic_applied.append({
                'name': pair['name'], 'transform': pair['transform'],
                'master': [int(tag) for tag in master],
                'slave': [int(tag) for tag in slave],
                'affine': [float(item) for item in pair['affine']]})
            # Accepted, not achieved. What this call establishes is a request
            # Gmsh agreed to honour while meshing; whether the two surfaces
            # ended up corresponding is read out of the mesh afterwards, in
            # :meth:`measure_periodic`.
            self.ledger.record(f'periodic:{pair["name"]}', pair['transform'],
                               pair['transform'], note='setPeriodic accepted')
        self.statistics['periodic'] = {'applied': applied,
                                       'requested': len(pairs)}

    def record_boundary_surfaces(self):
        """Remember which surfaces bound the volume, while that is still true.

        R65. The docstring below always said the adjacency test had to happen
        "before any extrusion has confused it", but ``execute`` called it
        after ``apply_boundary_layers``, which removes the imported volume,
        extrudes every surface and rebuilds the core from the inner faces.
        MEASURED on a five-patch tee: no surface passed ``len(upward) == 1``
        any more, so the run recorded ``boundarySurfaces: 0``, wrote no 2-D
        physical group at all, and publication refused the finished
        90,724-cell mesh with "11118 boundary faces but only 0 are named by a
        physical group". The surface tags themselves survive the extrusion --
        they are its base -- so the list is taken here and used to name them
        later.
        """
        if self.planar:
            # FC-E. One dimension down: a curve bounding exactly one surface
            # is the section's edge, exactly as a surface bounding exactly one
            # volume is a solid's. A curve two surfaces share is interior and
            # stays unnamed, which is also what keeps its line elements out of
            # the .msh -- Gmsh writes only what a physical group claims.
            curves = []
            for _dim, tag in self.gmsh.model.getEntities(1):
                upward, _down = self.gmsh.model.getAdjacencies(1, tag)
                if len(upward) == 1:
                    curves.append(tag)
            self.boundary_curves = curves
            self.boundary_surfaces = []
            return []
        boundary = []
        for _dim, tag in self.gmsh.model.getEntities(2):
            upward, _down = self.gmsh.model.getAdjacencies(2, tag)
            if len(upward) == 1:
                boundary.append(tag)
        self.boundary_surfaces = boundary
        return boundary

    def tag_physical_groups(self):
        """Name the real boundary and the volumes so publication can type them.

        Boundary surfaces are those adjacent to exactly one volume, which is
        established by ``record_boundary_surfaces`` before any extrusion can
        confuse the adjacency; the publication step re-derives the boundary
        from cell ownership and checks the two agree.
        """
        if self.planar:
            return self.tag_planar_groups()
        gmsh = self.gmsh
        boundary = getattr(self, 'boundary_surfaces', None)
        if boundary is None:
            boundary = self.record_boundary_surfaces()
        # Plan 28 WP7. Every piece classification cut out of one imported
        # solid goes into that solid's group, so a named STL surface is one
        # patch in the polyMesh however many pieces it became.
        grouped: dict[str, list[int]] = {}
        for tag in boundary:
            name, _known = self.surface_patch_name(tag)
            # R118. A patch left out of the boundary layer was deleted and
            # rebuilt from the extrusion's rim; the faces that replaced it are
            # the patch now, so the name follows them rather than a tag the
            # model no longer has.
            members = self.layer_replacements.get(tag)
            grouped.setdefault(name, []).extend(
                members if members is not None else [tag])
        named = []
        for name, tags in grouped.items():
            if not tags:
                continue
            gmsh.model.addPhysicalGroup(2, tags, name=name)
            named.append(name)
        volumes = [tag for _dim, tag in gmsh.model.getEntities(3)]
        volume_names = self.job.get('volumeNames') or {}

        def volume_name(tag):
            # C31-04. As for the surfaces: the identity map names this tag,
            # the version-1 map names a position two sources both claim.
            return (self.entity_volume_names.get(int(tag))
                    or volume_names.get(str(tag)) or f'volume_{tag}')

        # R118. A volume a layer was carved out of no longer exists, and the
        # layer volumes and the rebuilt core that replaced it are all still
        # the region the user named -- so they publish as one physical group
        # under that name, and none of them is named after a tag the model
        # reused underneath it.
        present = {int(tag) for tag in volumes}
        claimed = set()
        for original, members in self.volume_replacements.items():
            members = [int(tag) for tag in members if int(tag) in present]
            if not members:
                continue
            gmsh.model.addPhysicalGroup(3, members, name=volume_name(original))
            claimed.update(members)
        for tag in volumes:
            if int(tag) in claimed:
                continue
            gmsh.model.addPhysicalGroup(3, [tag], name=volume_name(tag))
        # DP-63. Two different numbers, and a reader downstream needs the
        # second one. `boundarySurfaces` is how many surface entities the
        # model has; `boundaryGroups` is how many patches they were grouped
        # into three lines above, which is what becomes a physical group and
        # therefore what a `.msh` or a `.su2` carries as a marker. They are
        # equal only when every surface has its own name. The SU2 export held
        # its marker count against the first and refused every mesh made from
        # a single-solid STL.
        self.statistics['groups'] = {
            'boundarySurfaces': len(boundary),
            'boundaryGroups': len(named),
            'boundaryGroupNames': sorted(named),
            'volumes': len(volumes)}

    def tag_planar_groups(self):
        """Name a section's boundary curves and its faces.

        FC-E. Everything drops one dimension on this route: the curves are the
        patches and the faces are the regions, because the publisher extrudes
        the section and each of them gains a dimension in doing so. Naming
        them is not cosmetic -- Gmsh writes only elements a physical group
        claims, so a boundary curve left unnamed here is a patch missing from
        the published polyMesh rather than a patch with a dull name.
        """
        gmsh = self.gmsh
        if not self.boundary_curves:
            self.record_boundary_surfaces()
        curves = self.boundary_curves
        # Named by tag, in import order. The prepared-geometry store names
        # surfaces and volumes, because a group in it is a set of faces; it
        # has nothing to say about a curve. DP-675 (field audit 0924
        # gmsh-generate-export D11): the user names them instead, by the tag
        # a first run published as `edge_<tag>`; a curve not named keeps it.
        names = self.planar_curve_names(curves)
        grouped = {}
        for tag in curves:
            grouped.setdefault(names.get(int(tag), f'edge_{tag}'),
                               []).append(tag)
        for name, tags in grouped.items():
            gmsh.model.addPhysicalGroup(1, tags, name=name)
        sections = [tag for _dim, tag in gmsh.model.getEntities(2)]
        region_names = self.job.get('volumeNames') or {}
        for tag in sections:
            name = (self.entity_surface_names.get(int(tag))
                    or region_names.get(str(tag)) or f'region_{tag}')
            gmsh.model.addPhysicalGroup(2, [tag], name=name)
        # DP-63. One group per curve on this route, so the two counts agree
        # here -- they are recorded separately anyway, because a reader that
        # has to know which of them it is holding is the fault being fixed.
        self.statistics['groups'] = {
            'boundarySurfaces': 0, 'volumes': 0,
            'boundaryGroups': len(grouped),
            'boundaryGroupNames': sorted(grouped),
            'boundaryCurves': len(curves), 'sections': len(sections),
            'curves': self.describe_curves(curves, names)}

    def planar_curve_names(self, curves):
        """``{curve tag: patch name}`` from the job, checked against the model.

        DP-675. A tag that is not a boundary curve of this section names
        nothing; that is said in the ledger and the warnings rather than
        dropped, and every name that did land is recorded as applied.
        """
        wanted = self.dimensionality.get('edgeNames') or {}
        present = {int(tag) for tag in curves}
        names, missing = {}, []
        for name, tags in wanted.items():
            for tag in tags:
                if int(tag) in present:
                    names[int(tag)] = str(name)
                else:
                    missing.append(f'{name}: {tag}')
        if wanted:
            self.ledger.record(
                'gmsh/dimensionality/edgeNames',
                {str(name): list(tags) for name, tags in wanted.items()},
                {name: sorted(tag for tag, other in names.items()
                              if other == name)
                 for name in wanted},
                applied=bool(names), matched=not missing,
                note=('' if not missing else
                      'no boundary curve of this section has tag '
                      + ', '.join(missing) + '; the boundary curves are '
                      + ', '.join(str(tag) for tag in sorted(present))))
        if missing:
            self.warnings.append(
                'edge names: ' + ', '.join(missing) + ' named no boundary '
                'curve of this section; its boundary curves are '
                + ', '.join(f'edge_{tag}' for tag in sorted(present)))
        return names

    def describe_curves(self, curves, names):
        """Each boundary curve's tag, patch, length and midpoint.

        DP-675. The tag is what the edge-name field is keyed by, so the run
        says where each one is.
        """
        described = []
        for tag in curves:
            item = {'tag': int(tag),
                    'patch': names.get(int(tag), f'edge_{tag}')}
            try:
                low, high = self.gmsh.model.getParametrizationBounds(1, tag)
                mid = self.gmsh.model.getValue(
                    1, tag, [0.5 * (low[0] + high[0])])
                item['midpoint'] = [round(float(value), 9) for value in mid]
                item['length'] = round(float(self.gmsh.model.occ.getMass(
                    1, tag)), 9)
            except Exception:
                pass
            described.append(item)
        return described

    def measure_quality(self, all_tags, limit):
        """Measure one metric across the volume and name the elements that fail.

        Counting how many elements are bad is not enough to act on: a user told
        "3 of 141486 fall below gamma 0.1" cannot find those three, so the mesh
        is a dead end however small the defect. This records the offending
        tags, their values and their centroids -- capped, worst first -- so a
        viewer can put a camera on them.
        """
        gmsh = self.gmsh
        measure = str(limit.get('measure') or limit.get('qualityType')
                      or 'sicn').lower()
        substituted_for = ''
        if measure not in QUALITY_QUERY_NAME:
            substituted_for, measure = measure, QUALITY_SUBSTITUTE
            self.warnings.append(
                f'Gmsh cannot report {substituted_for} per element, so the '
                f'quality gate measured {measure} against the same limit')
        values = gmsh.model.mesh.getElementQualities(
            all_tags, QUALITY_QUERY_NAME[measure])

        threshold = float(limit.get('minQuality', limit.get('minimum', 0.0)) or 0.0)
        inverted = sum(1 for item in values if item <= 0)
        judged, layer_block = self.split_off_the_layer(values, all_tags,
                                                       threshold)
        below = [index for index in judged if values[index] < threshold]
        # Worst first, so the cap keeps the elements a user most wants to see.
        below.sort(key=lambda index: values[index])
        offending = []
        for index in below[:OFFENDER_CAP]:
            tag = int(all_tags[index])
            offending.append({
                'tag': tag, 'value': float(values[index]), 'measure': measure,
                'centroid': self._element_centroid(tag),
            })
        kept = [values[index] for index in judged]
        block = {
            'measure': measure,
            'minimum': min(kept), 'mean': sum(kept) / len(kept),
            'below_threshold': len(below), 'total': len(kept),
            'inverted': inverted, 'offending': offending,
            'offendingTruncated': len(below) > OFFENDER_CAP,
        }
        surface = self.measure_the_surface_it_was_built_on(measure,
                                                          threshold)
        if surface is not None:
            block['surface'] = surface
        if layer_block is not None:
            block['layerCells'] = layer_block
        if substituted_for:
            block['substituted_for'] = substituted_for
        return block

    def measure_the_surface_it_was_built_on(self, measure, threshold):
        """The boundary mesh, judged by the reading the volume is judged by.

        DP-82. A tetrahedron resting on a sliver triangle is a sliver: its
        inscribed radius cannot exceed the face it sits on. So a volume mesh
        refused on quality may be carrying nothing worse than the surface it
        was given, and the remedy the gate would otherwise advise -- the
        repair pass, which moves nodes -- cannot lift it, because those nodes
        are where the surface put them. MEASURED on gmsh drone_quadcopter
        with the recommended preparation applied: 1405 of the 5228 imported
        facets (26.9%) read below gamma 0.1 themselves, worst 0.00889, and
        the volume built on them came out 5097 of 259863 (1.96%) below the
        same limit. Turning the repair pass on moved the untangler not at
        all and left 5741 cells below the limit instead of 5097.

        Recorded beside the volume block rather than instead of it: the gate
        still judges the mesh it was asked to judge, and now it can say where
        the fault came from.
        """
        gmsh = self.gmsh
        if measure not in QUALITY_QUERY_NAME:
            return None
        tags = []
        _types, groups, _nodes = gmsh.model.mesh.getElements(2)
        for group in groups:
            tags.extend(int(item) for item in group)
        if not tags:
            return None
        values = list(gmsh.model.mesh.getElementQualities(
            tags, QUALITY_QUERY_NAME[measure]))
        if not values:
            return None
        record = getattr(self, 'statistics', {}).get('classification')
        kept = bool(isinstance(record, dict)
                    and record.get('keptTessellation'))
        return {
            'measure': measure,
            'total': len(values),
            'minimum': float(min(values)),
            'mean': float(sum(values) / len(values)),
            'below_threshold': sum(1 for item in values if item < threshold),
            # True when the surface is the imported triangulation itself, in
            # which case repairing it means repairing the geometry.
            'keptTessellation': kept,
        }

    def split_off_the_layer(self, values, all_tags, threshold):
        """Which elements the limit is judged against, and what the rest were.

        DP-76. ``gamma`` is the ratio of a cell's inscribed radius to its
        circumscribed one: it measures how far a cell is from equilateral,
        and it condemns anisotropy as such. A boundary-layer cell is
        anisotropic on purpose -- that is the whole of what a boundary layer
        is -- so judging one by gamma is asking it to stop being a layer.
        MEASURED on ``drone_quadcopter``: of the 12062 cells the gate refused
        at gamma 0.1, 7143 were the layer prisms the user had asked for, and
        the best-formed prism in a stack 0.04 mm thick on a 5 mm wall reads
        0.00426. So the limit is judged against the cells it means something
        for, and the layer is counted beside it, not inside it.

        The layer keeps two tests it cannot argue with: it must not invert,
        which is counted across every cell whatever its family, and it must
        not fold, which :meth:`measure_layer_fit` measured before the volume
        pass ever ran.

        Returns the indices to judge and the layer's own block, or every
        index and ``None`` when there is no layer to set aside -- including
        the hollow-shell case where the layer is all there is, which
        :meth:`generate` refuses a few lines later for that reason.
        """
        every = list(range(len(values)))
        layer_cells = self.layer_cell_tags()
        if not layer_cells:
            return every, None
        judged, mine = [], []
        for index in every:
            if int(all_tags[index]) in layer_cells:
                mine.append(values[index])
            else:
                judged.append(index)
        if not mine or not judged:
            return every, None
        block = {
            'total': len(mine),
            'below_threshold': sum(1 for item in mine if item < threshold),
            'minimum': min(mine), 'mean': sum(mine) / len(mine),
            'inverted': sum(1 for item in mine if item <= 0),
        }
        if block['below_threshold']:
            inverted = block['inverted']
            stood = (f'{inverted} of them inverted' if inverted
                     else 'none of them inverted')
            note = (f"{block['below_threshold']} of the {len(mine)} "
                    f'boundary-layer cells fall below the limit, and are '
                    f'counted separately rather than judged by it: the '
                    f'measure reads a deliberately thin cell as a bad one. '
                    f'They are still required not to invert, and {stood}.')
            if note not in self.warnings:
                self.warnings.append(note)
        return judged, block

    def _element_centroid(self, tag):
        """Where to point a camera. Never fatal: a missing node loses a marker,
        not the run."""
        try:
            nodes = self.gmsh.model.mesh.getElement(tag)[1]
            points = [self.gmsh.model.mesh.getNode(node)[0] for node in nodes]
        except Exception:                                    # noqa: BLE001
            return [0.0, 0.0, 0.0]
        if not points:
            return [0.0, 0.0, 0.0]
        return [round(sum(point[axis] for point in points) / len(points), 9)
                for axis in range(3)]

    def raise_element_order(self):
        """Turn the meshed elements into second-order ones, once meshed.

        **`Mesh.ElementOrder` alone does nothing through the API.** Setting the
        option and calling `generate(3)` produced 76,923 first-order tetrahedra
        on the duct -- the ledger read the option back as 2 and the `.su2` file
        listed four nodes per cell. The option is what the GUI and the command
        line read; the API raises the order only when asked directly. So both
        happen: the option is recorded before meshing, and the order is raised
        here, after optimisation (Netgen optimises linear meshes) and before
        the census, so what is counted is what is written.

        MEASURED again in Plan 31 FC-D, and the picture is narrower than
        that paragraph reads. On torus.step with `Mesh.ElementOrder` 2 set
        before meshing, `generate(3)` raised the order by itself: the mesh
        after `generate` and the mesh after `setOrder` were identical, 240
        order-2 elements either way. So on that geometry this call is a no-op
        and the option was enough. It is kept because the duct case above is
        the one that cannot be allowed to regress, and a `setOrder` that has
        nothing to do costs nothing. What it is *not* is the place the
        high-order optimiser runs -- see `record_high_order_optimizer`.
        """
        export = self.intent.get('export') or {}
        order = int(export.get('elementOrder', 1) or 1)
        if order < 2:
            return
        before = self.worst_quality()
        try:
            self.gmsh.model.mesh.setOrder(order)
        except Exception as error:
            raise MeshFailure(
                f'Gmsh could not raise the mesh to order {order}: {error}'
            ) from error
        self.ledger.record('gmsh/output/elementOrder', order, order,
                           note='raised after meshing by setOrder')
        self.record_high_order_optimizer(before)

    #: What each `Mesh.HighOrderOptimize` setting was measured to do to the
    #: mesh this route writes. Plan 31 FC-D, torus.step at 0.11, 240 elements,
    #: worst element by minSICN read back out of the written file:
    #:
    #:   0  -0.297582, 26 inverted   (off)
    #:   1   0.002620,  0 inverted   (optimisation)
    #:   2   0.002568,  0 inverted   (elastic, then optimisation)
    #:   3  -0.148486, 26 inverted   (elastic only)
    #:   4  -0.297582, 26 inverted   (fast curving -- inert on this mesh)
    #:
    #: Kept as a table rather than prose because it is the answer to "which
    #: setting should I pick", and 4 being bit-identical to 0 is the kind of
    #: thing that is worth being able to point at.
    HIGH_ORDER_MEASURED = {
        0: 'off',
        1: 'optimisation pass',
        2: 'elastic pass, then optimisation pass',
        3: 'elastic pass only',
        4: 'fast curving',
    }

    def record_high_order_optimizer(self, before):
        """Measure what `Mesh.HighOrderOptimize` did while the order was raised.

        Plan 31 FC-D, ledger row `optimizer-high-order-tuning`.

        **`generate` reads this option, and it works.** That took three
        measurements to get right, and the two wrong ones are recorded because
        each was wrong in a way worth not repeating.

        The first measured settings 0, 1 and 4 on a mesh with no inverted
        elements. All three wrote an identical worst element (0.278704) and an
        identical mean (0.757140), and that was read as the option being inert
        on this route -- answered by dispatching `mesh.optimize("HighOrder")`
        explicitly after `setOrder`. The fixture was the flaw: a high-order
        optimiser has nothing to do on a mesh that is not tangled, so every
        setting "passes" and nothing is learned.

        The second measured a torus that *is* tangled -- 26 inverted elements
        at order 2 -- and found the option acting on its own. It looked as
        though `setOrder` read it. It does not: with `Mesh.ElementOrder` left
        at 1 and the order raised only by `setOrder`, every setting of the
        option and of both its tuning knobs wrote the same uncurved mesh,
        worst element -0.297582. `generate(3)` is what raises the order and
        runs the optimiser, and it only does so when `Mesh.ElementOrder` is
        already 2 -- which `apply_export_settings` sets.

        Measured properly, on the torus, by minSICN read back out of the
        written file: setting 0 leaves 26 inverted elements and a worst of
        -0.297582; setting 1 leaves none and 0.002620; setting 2 none and
        0.002568; setting 3 twenty-six and -0.148486; setting 4 is
        bit-identical to off.

        The explicit dispatch is gone, and not only because it was
        unnecessary. It made the result worse -- setting 1 alone gave 0.002620
        and the same run followed by `optimize("HighOrder")` gave 0.001576 --
        and running the pass twice is one of the ways measured to abort the
        process outright: "Failed to reach critical value in pass 0 for
        measure(s): ScaledJac", exit 134, no mesh written and no result
        document. The others are the two tuning knobs the same ledger row
        names -- `Mesh.HighOrderPassMax` at 1, 2 or 3, and
        `Mesh.HighOrderNumLayers` at 0, each of which aborts from inside
        `generate` with no explicit call anywhere -- which is one of the
        reasons neither knob is offered. None of them is reachable on the
        shipped route: the pass runs once, at the default 25 passes over the
        default 6 layers.

        What stays is the measurement. The quality of the raised mesh is
        recorded next to the setting that produced it, because an option read
        back out of the session it was set in is not evidence about a mesh --
        this build accepts `optimize("Bogus")` on a 3D mesh without complaint.
        """
        quality = self.intent.get('quality') or {}
        setting = int(quality.get('highOrderOptimize', 0) or 0)
        after = self.worst_quality()
        self.statistics.setdefault('highOrderOptimize', {}).update({
            'setting': setting,
            'does': self.HIGH_ORDER_MEASURED.get(setting, 'unknown setting'),
            'readBy': 'gmsh.model.mesh.generate',
            'worstBeforeRaising': before, 'worstAfterRaising': after,
        })
        raised = ('' if after is None
                  else f'; the worst element of the raised mesh is {after:.6f}')
        self.ledger.record(
            'gmsh/optimization/highOrderOptimize', setting, setting,
            applied=setting > 0,
            note=(f'{self.HIGH_ORDER_MEASURED.get(setting, "unknown setting")}'
                  f', read by generate{raised}'))

    def worst_quality(self, measure='minSICN'):
        """The worst volume element, or ``None`` when nothing can be measured.

        Never fatal: this exists to describe an optimiser's effect, and a mesh
        that cannot be measured still has to be written and reported on.
        """
        try:
            _types, groups, _nodes = self.gmsh.model.mesh.getElements(3)
            tags = [int(item) for group in groups for item in group]
            if not tags:
                return None
            return float(min(self.gmsh.model.mesh.getElementQualities(
                tags, measure)))
        except Exception:                                    # noqa: BLE001
            return None

    def renumber_nodes(self):
        """Renumber the mesh nodes so a solver's matrix is narrower.

        Plan 31 FC-D, ledger row `renumbering`. Reverse Cuthill-McKee reorders
        the node tags so the nodes an element joins carry numbers close
        together; the width of that spread is the bandwidth of the matrix the
        solver assembles from the file. Run last -- after the order is raised
        and after coincident nodes are merged, both of which change which tags
        exist -- and before the census and the write, so what is counted and
        what is written carry the same numbering.

        The claim is checked against the file, not the option: the run records
        the bandwidth it measured either side, and the gate reads the node
        order back out of the written mesh. `Mesh.Renumber` is not used --
        it is an option `generate()` reads, and by here generate() has run.
        """
        export = self.intent.get('export') or {}
        if not bool(export.get('renumber', False)):
            return
        # MEASURED, and the reason this is guarded rather than trusted: the
        # renumbering is real inside the session either way, but the MSH 2.2
        # writer indexes nodes in storage order, so the file it produces
        # carries the old numbering. One sphere, one renumbering, three
        # writers: widest node-number spread over a cell 458 -> 129 in the
        # .su2 and in MSH 4.1, 458 -> 458 in MSH 2.2. Recording this control
        # as applied on a run whose only output is MSH 2.2 would be a report
        # of the option rather than of the file.
        carriers = self.renumbering_carriers()
        if not carriers:
            reason = ('the renumbering was not run: this run writes MSH 2.2 '
                      'only, and that writer numbers nodes in storage order, '
                      'so the file cannot carry it')
            self.warnings.append(reason)
            self.ledger.record('gmsh/output/renumber', True, False,
                               applied=False, note=reason)
            return
        before = self.node_bandwidth()
        try:
            old_tags, new_tags = self.gmsh.model.mesh.computeRenumbering('RCMK')
            self.gmsh.model.mesh.renumberNodes(old_tags, new_tags)
        except Exception as error:                           # noqa: BLE001
            self.warnings.append(f'renumbering did not run: {error}')
            self.ledger.record('gmsh/output/renumber', True, False,
                               applied=False,
                               note=f'RCMK renumbering failed: {error}')
            return
        after = self.node_bandwidth()
        self.statistics['renumbering'] = {
            'method': 'RCMK', 'bandwidthBefore': before,
            'bandwidthAfter': after,
        }
        moved = ''
        if before is not None and after is not None:
            moved = f'; node bandwidth {before} -> {after}'
        self.statistics['renumbering']['carriedBy'] = carriers
        self.ledger.record(
            'gmsh/output/renumber', True, True,
            note='RCMK, by computeRenumbering, carried by '
                 + ', '.join(carriers) + moved)

    def renumbering_carriers(self):
        """Which of this run's outputs would carry a renumbering.

        Gmsh's SU2 writer and its MSH 4.1 writer write the node tags the model
        holds. The MSH 2.2 writer does not -- it indexes the nodes in storage
        order -- and MSH 2.2 is what every non-SU2 route here writes.
        """
        export = self.intent.get('export') or {}
        mesh_format = str(export.get('meshFormat') or 'msh2.2')
        version = float(export.get('mshVersion') or 2.2)
        carriers = []
        for key, destination in (self.job.get('output') or {}).items():
            suffix = Path(destination).suffix.lower()
            if mesh_format == 'su2' and suffix == '.su2':
                carriers.append(key)
            elif suffix != '.su2' and version >= 4.0:
                carriers.append(key)
        return carriers

    def node_bandwidth(self):
        """The widest node-number spread of any volume element, or ``None``.

        This is what a renumbering claims to reduce, and it is a property of
        the numbers rather than of the shapes, so it is read from the element
        connectivity as it stands. Never fatal.
        """
        try:
            _types, groups, nodes = self.gmsh.model.mesh.getElements(3)
        except Exception:                                    # noqa: BLE001
            return None
        widest = 0
        for etype, _group, node_group in zip(_types, groups, nodes):
            try:
                per = self.gmsh.model.mesh.getElementProperties(etype)[3]
            except Exception:                                # noqa: BLE001
                continue
            flat = [int(item) for item in node_group]
            for index in range(0, len(flat), per):
                cell = flat[index:index + per]
                if cell:
                    widest = max(widest, max(cell) - min(cell))
        return widest or None

    def meshed_nodes(self):
        """The node tags an element actually uses.

        A Gmsh model carries more nodes than the mesh is made of. The boundary
        layer extrusion, for one, leaves a point entity behind for every
        corner it grew from, each holding a node no element ever references;
        MEASURED on the layered duct, the model holds 854 nodes and the mesh
        is built from 846 of them. Those eight sit on the eight corners they
        were copied from, which is what an earlier reading of this code called
        coincident and refused the merge over -- they reach no written file,
        and nothing can be collapsed into them.
        """
        used = set()
        for dim in (1, 2, 3):
            try:
                _types, _tags, nodes = self.gmsh.model.mesh.getElements(dim)
            except Exception:                                # noqa: BLE001
                continue
            for group in nodes:
                used.update(int(item) for item in group)
        return used

    def _coincident_buckets(self, tolerance):
        """``{place: [node tags]}`` for every place holding more than one.

        DP-457. :meth:`coincident_nodes` used to build this and throw the
        grouping away, and the grouping is the whole question: a node is not
        interesting because it is coincident, it is interesting because of
        what the node it is coincident *with* belongs to.
        """
        try:
            tags, coords, _p = self.gmsh.model.mesh.getNodes()
        except Exception:                                    # noqa: BLE001
            return {}
        tags = [int(item) for item in tags]
        if len(coords) < 3 * len(tags):
            return {}
        used = self.meshed_nodes()
        places: dict = {}
        for index, tag in enumerate(tags):
            if used and tag not in used:
                continue
            key = tuple(round(float(coords[3 * index + axis]) / tolerance)
                        for axis in range(3))
            places.setdefault(key, []).append(tag)
        return {key: group for key, group in places.items() if len(group) > 1}

    def interface_coincidences(self, tolerance):
        """The coincident nodes that are a conformal interface.

        DP-457. Two nodes in the same place are a doubled import when they
        belong to the same body and an interface when they belong to two --
        and an interface is the one coincidence in a mesh that exists to be
        welded, because welding it is what conformal means.

        MEASURED on `refined baffled_chamber/gmsh`, the fixture built to
        carry one. Its two bodies meet on the rectangle at x = 0.09; each
        grows its own copy of that face, so 1102 nodes stand in 551 places,
        two to a place, one node from each body. The guard read them as a
        boundary layer that had grown into itself and refused the run -- with
        arithmetic in the same sentence saying no wall faced any of them
        within four times the asked thickness, which is to say there was no
        collision to find.

        The reading is the shells, not the distance: a place holding nodes
        from two different closed bodies is where those bodies meet. A layer
        that really has grown into itself collides inside one body, so it
        spans one shell and is still refused, in its own words.
        """
        if not getattr(self, 'surface_shell', None):
            return set()
        buckets = self._coincident_buckets(tolerance)
        if not buckets:
            return set()
        duplicates = {tag for group in buckets.values() for tag in group}
        owner: dict = {}
        for tag, shell in self.surface_shell.items():
            for node in self.surface_node_set(tag) & duplicates:
                owner.setdefault(node, set()).add(shell)
        interface = set()
        for group in buckets.values():
            shells = set()
            for node in group:
                shells |= owner.get(node, set())
            if len(shells) > 1:
                interface.update(group)
        return interface

    def coincident_nodes(self, tolerance):
        """The mesh node tags that sit on top of another, within *tolerance*.

        Bucketed rather than compared pair by pair, because the mesh this runs
        on has hundreds of thousands of nodes. Two nodes either side of a
        bucket edge are missed, which is why the count is used only to decide
        whether there is anything at all to merge: when the scan finds nothing
        the merge is a no-op, so a miss costs nothing, and when it finds
        something the nodes are looked up by entity before anything is done.

        Only nodes an element uses are scanned; see ``meshed_nodes``.
        """
        return {tag for group in self._coincident_buckets(tolerance).values()
                for tag in group}


    def merge_tolerance(self):
        """The distance Gmsh itself treats as "the same place".

        ``Geometry.Tolerance`` is relative: Gmsh scales it by the size of the
        model, so the same 1e-8 means a micron on a metre and a nanometre on a
        millimetre. Measuring coincidence on the absolute number would call
        every node distinct on a small part and merge across a large one.
        """
        relative = float(self.gmsh.option.getNumber('Geometry.Tolerance')
                         or 1e-8)
        try:
            box = self.gmsh.model.getBoundingBox(-1, -1)
            diagonal = math.dist(box[:3], box[3:]) or 1.0
        except Exception:                                    # noqa: BLE001
            diagonal = 1.0
        return max(relative * diagonal, 1e-12)

    def protected_surfaces(self):
        """``{surface tag: why merging must not collapse it}``.

        Both answers come from what the run actually did -- the pairs Gmsh
        accepted and the surfaces the layer was extruded from -- so a feature
        that was asked for and did not apply protects nothing.
        """
        reasons = {}
        for pair in self.periodic_applied:
            for tag in list(pair['master']) + list(pair['slave']):
                reasons.setdefault(int(tag), f'periodic pair {pair["name"]!r}')
        if self.statistics.get('layers'):
            for tag in list(self.layer_bases) + list(self.layer_laterals):
                reasons.setdefault(int(tag), LAYER_PROTECTION)
        return reasons

    def surface_node_set(self, tag):
        try:
            tags, _coords, _p = self.gmsh.model.mesh.getNodes(
                2, int(tag), includeBoundary=True)
        except Exception:                                    # noqa: BLE001
            return set()
        return {int(item) for item in tags}

    def periodic_node_census(self):
        """How many nodes each periodic surface has a partner for, right now."""
        census = {}
        for pair in self.periodic_applied:
            for tag in pair['slave']:
                try:
                    _master, slaves, _masters, _affine = \
                        self.gmsh.model.mesh.getPeriodicNodes(2, int(tag))
                except Exception:                            # noqa: BLE001
                    slaves = []
                census[int(tag)] = len(slaves)
        return census

    def merge_duplicate_nodes(self):
        """Merge coincident nodes, unless this mesh has some that must stay.

        Plan 31 CP-08 item 4. Merging is destructive on two kinds of mesh --
        a periodic pair whose transform leaves its two node sets on top of
        each other, and a layer whose stack is a separate body from the core
        it stands in -- and the reading this replaces refused the merge
        whenever a pair or a layer existed *at all*.

        DP-457. There is a third kind, and it is the opposite case: two
        bodies that share a face each draw it, so the interface stands two
        nodes to a place, and welding those is exactly what makes the mesh
        conformal across it. Those are taken out before the protected
        surfaces are asked -- see :meth:`interface_coincidences` -- so what
        the refusals below judge is only coincidence inside one body.

        MEASURED, on the meshes this runner produces, that guard fires on
        meshes it cannot help. A duct with three layers on four walls comes
        out with 846 nodes and no two of them in the same place; the 0.8 m
        translation between the duct's two ends puts its 15 paired nodes
        nowhere near each other. Running ``removeDuplicateNodes`` over either
        mesh changes nothing at all -- not a node, not one of the 1200 prisms,
        not one of the 15 pairs (``merge_counterfactual.py`` in the evidence
        directory is that measurement). So the old refusal turned a healing
        step off on meshes that had nothing to heal, and told the user a
        reason that was not true of the mesh in front of them.

        So the decision is now made from the nodes. Nothing coincident,
        nothing to do. Something coincident on a surface a pair or a layer
        depends on, and the merge is refused with that count named -- the
        mesh that does this is one where a part was imported twice, so both
        copies of the periodic surface carry a full set of nodes in the same
        places and merging welds the two bodies into one. Otherwise the merge
        runs, and the pairs and the prisms are counted again afterwards to
        check that it left them alone.
        """
        healing = self.intent.get('healing') or {}
        if not healing.get('removeDuplicateNodes', True):
            return
        gmsh = self.gmsh
        tolerance = self.merge_tolerance()
        duplicates = self.coincident_nodes(tolerance)
        if not duplicates:
            self.ledger.record(
                'gmsh/healing/removeDuplicateNodes', True, True,
                note='no coincident nodes to merge')
            return

        # DP-457. The coincidence an assembly is built to have. Two bodies
        # that meet on a face each draw it, so its nodes stand two to a place
        # -- and welding them is not damage to be refused, it is the step
        # that makes the interface conformal. Taken out of the reckoning
        # before the protected surfaces are asked, so what remains is only
        # the coincidence inside a single body, which is what the refusals
        # below are about.
        interface = self.interface_coincidences(tolerance) & duplicates
        if interface:
            self.statistics.setdefault('interface', {})['weldedNodes'] =                 len(interface)
            self.warnings.append(
                f'{count_text(len(interface), "coincident node")} on the '
                'interface between two bodies were welded, which is what '
                'makes it conformal')

        at_risk, where = {}, {}
        for tag, reason in self.protected_surfaces().items():
            hit = (duplicates - interface) & self.surface_node_set(tag)
            if hit:
                # A corner node lies on several of these surfaces at once, so
                # the tally is a union of nodes rather than a sum of surface
                # memberships: the reader is told how many of their nodes are
                # at risk, which is the number the count beside it is out of.
                at_risk.setdefault(reason, set()).update(hit)
                where.setdefault(reason, set()).add(
                    self.surface_patch_name(int(tag))[0])
        if at_risk:
            reason = '; '.join(
                f'{len(nodes):,} of '
                f'{count_text(len(duplicates - interface), "coincident node")} '
                f'{agreeing(len(nodes), "lies", "lie")} on '
                f'{name} ({", ".join(sorted(where[name]))}), which merging '
                'would collapse'
                for name, nodes in sorted(at_risk.items()))
            self.ledger.record('gmsh/healing/removeDuplicateNodes',
                               True, False, applied=False, note=reason)
            self.warnings.append('duplicate nodes were not merged: ' + reason)
            self.refuse_collided_layer(at_risk, where, duplicates)
            return

        # An API call, not an option: Gmsh has no Mesh.RemoveDuplicateNodes
        # setting, and claiming otherwise is how the first run of this runner
        # failed.
        before = len(gmsh.model.mesh.getNodes()[0])
        pairs_before = self.periodic_node_census()
        prisms_before = self.prism_count()
        gmsh.model.mesh.removeDuplicateNodes()
        after = len(gmsh.model.mesh.getNodes()[0])
        pairs_after = self.periodic_node_census()
        prisms_after = self.prism_count()
        self.ledger.record(
            'gmsh/healing/removeDuplicateNodes', True, True,
            note=f'{before - after:,} of '
                 f'{count_text(len(duplicates), "coincident node")} merged')
        # The check the refusal above cannot make for itself: the surfaces
        # were clear of duplicates, so the merge should not have touched them.
        if pairs_before != pairs_after:
            self.warnings.append(
                'merging coincident nodes changed the periodic node map from '
                f'{pairs_before} to {pairs_after}; the coupling is no longer '
                'the one Gmsh was asked for')
        if prisms_before != prisms_after:
            self.warnings.append(
                f'merging coincident nodes changed the boundary layer from '
                f'{prisms_before:,} to {count_text(prisms_after, "prism")}')

    def stalled_columns(self, collided):
        """``{base surface: [node, ...]}`` for stacks that grew by nothing.

        DP-381. A base node coincident with a node on its own column's
        top is not two stacks meeting across a gap. It is one column that
        Gmsh extruded by zero: the top node was written at the base node's
        own coordinates, to the last bit.

        MEASURED on ``two_cubes_two_files`` at ``targetSize`` 0.04145781 --
        two cubes a whole unit apart, so there is no gap for a stack to
        meet another across, and :meth:`measure_layer_fit` reports 0.0327 m
        of room against the 0.0066166 asked. Of the 4,400 top nodes the
        eight columns produced, **4,398 travelled 0.00661666488 m -- the
        entire ask, to every digit -- and 2 travelled 0.0**. Those 2 are
        the whole collision, and both are interior to their surface: no
        curve and no point owns them, so no seam explains them either.
        """
        columns = getattr(self, 'layer_columns', ())
        if not columns:
            return {}
        gmsh = self.gmsh
        tolerance = self.merge_tolerance()

        def key(tag):
            try:
                point = gmsh.model.mesh.getNode(int(tag))[0]
            except Exception:                                # noqa: BLE001
                return None
            return tuple(round(float(value) / tolerance) for value in point)

        stalled = {}
        for base, top in columns:
            standing = collided & self.surface_node_set(int(base))
            if not standing:
                continue
            # This column's own top and no other. A base node that landed
            # on some *other* stack is the fault DP-121 filed, and it
            # keeps that name and that advice.
            places = {key(tag) for tag in self.surface_node_set(int(top))}
            places.discard(None)
            landed = [int(tag) for tag in sorted(standing)
                      if key(tag) in places]
            if landed:
                stalled[int(base)] = landed
        return stalled

    #: How square-on two walls must be to count as facing each other across a
    #: gap: the cosine between each wall's normal and the line joining them.
    #: 0.5 admits anything within 60 degrees of square, which is generous --
    #: the question being asked is whether a wall stands *across* from the
    #: node, not whether it is parallel to it.
    FACING_COSINE = 0.5

    def walls_within_reach(self, collided, reach):
        """How many colliding nodes have a wall *facing* them within ``reach``.

        DP-403. The fall-through arm of :meth:`refuse_collided_layer` advised
        on the assumption that two stacks had met across a narrow gap, without
        ever asking whether the model had one. On ``flange`` it does not, and
        the refusal sent the user to change a thing that is not there.

        Facing is the whole reading, and a nearness test alone will not do it.
        MEASURED on ``flange`` at the asked size: every one of the 894
        colliding nodes has a node of *some other* layer base surface within
        0.00114621 m, the nearest 7.18e-05 m away -- and none of those is a
        wall across a gap. They are the neighbouring faces of the same wall.
        A STEP solid arrives split into hundreds of faces, so on any CAD model
        "another surface is nearby" is true everywhere and discriminates
        nothing.

        So a candidate counts only when the line joining the two nodes runs
        square-on to *both* walls -- :attr:`FACING_COSINE` against each node's
        surface normal, taken as the area-weighted sum of its facets'. That
        rejects the two cases a nearness test confuses with a gap: a
        neighbouring face continuing the same wall, where the join lies in the
        surface and the cosine is near zero, and a corner, where it is small
        against at least one of the two. Sign is not read, so an inconsistently
        oriented surface cannot flip the answer.

        The node's own surfaces are excluded outright: its neighbours on them
        are one mesh spacing away by construction. A surface that folded into
        itself therefore reads as alone, which is the right answer for it too
        -- what clears a fold is the resolution that stops meshing it there.

        Returns ``None`` when there is nothing to measure -- no columns, no
        reach, a mesh that will not hand over its nodes -- so the caller can
        tell "no wall facing" from "not asked". `nearby` is the weaker count
        the facing test discards, kept because it is what makes the reading
        legible: on `flange` it is 894 against 0.
        """
        columns = getattr(self, 'layer_columns', ())
        if not columns or not collided or reach <= 0.0:
            return None
        try:
            tags, flat, _params = self.gmsh.model.mesh.getNodes()
            points = {int(tag): (float(flat[3 * index]),
                                 float(flat[3 * index + 1]),
                                 float(flat[3 * index + 2]))
                      for index, tag in enumerate(tags)}
        except Exception:                                    # noqa: BLE001
            return None
        if not points:
            return None

        on = {}
        normals = {}
        for base, _top in columns:
            base = int(base)
            for node in self.surface_node_set(base):
                on.setdefault(int(node), set()).add(base)
            try:
                kinds, _etags, blocks = self.gmsh.model.mesh.getElements(
                    2, base)
            except Exception:                                # noqa: BLE001
                continue
            for kind, block in zip(kinds, blocks):
                facts = self.gmsh.model.mesh.getElementProperties(kind)
                shape, _dim, _order, per = facts[:4]
                if not (shape.startswith('Triangle')
                        or shape.startswith('Quadrilateral')):
                    continue
                block = [int(item) for item in block]
                for index in range(0, len(block), per):
                    corners = block[index:index + 3]
                    try:
                        one, two, three = (points[tag] for tag in corners)
                    except KeyError:
                        continue
                    a = [two[axis] - one[axis] for axis in range(3)]
                    b = [three[axis] - one[axis] for axis in range(3)]
                    # Area-weighted, so a sliver counts for what it is.
                    cross = (a[1] * b[2] - a[2] * b[1],
                             a[2] * b[0] - a[0] * b[2],
                             a[0] * b[1] - a[1] * b[0])
                    for tag in block[index:index + per]:
                        running = normals.setdefault(tag, [0.0, 0.0, 0.0])
                        for axis in range(3):
                            running[axis] += cross[axis]
        if not on:
            return None

        def unit(vector):
            length = math.sqrt(sum(value * value for value in vector))
            if length <= 0.0:
                return None
            return tuple(value / length for value in vector)

        facing = {node: unit(vector) for node, vector in normals.items()}
        if not any(facing.values()):
            # No facets handed over, so which way the walls point cannot be
            # read. Say "not asked" rather than "nothing faces them".
            return None

        cell = float(reach)

        def box(point):
            return tuple(int(math.floor(value / cell)) for value in point)

        grid = {}
        for node in on:
            point = points.get(node)
            if point is not None:
                grid.setdefault(box(point), []).append(node)

        within = 0
        nearby = 0
        nearest = None
        for node in collided:
            node = int(node)
            point = points.get(node)
            if point is None:
                continue
            mine = on.get(node) or set()
            normal = facing.get(node)
            home = box(point)
            best = None
            saw = False
            for x in (-1, 0, 1):
                for y in (-1, 0, 1):
                    for z in (-1, 0, 1):
                        neighbours = grid.get(
                            (home[0] + x, home[1] + y, home[2] + z), ())
                        for other in neighbours:
                            if other == node or (on.get(other) or set()) & mine:
                                continue
                            gap = math.dist(point, points[other])
                            if gap > cell:
                                continue
                            saw = True
                            if normal is None:
                                continue
                            across = facing.get(other)
                            if across is None:
                                continue
                            line = unit([points[other][axis] - point[axis]
                                         for axis in range(3)])
                            if line is None:
                                continue
                            here = abs(sum(normal[axis] * line[axis]
                                           for axis in range(3)))
                            there = abs(sum(across[axis] * line[axis]
                                            for axis in range(3)))
                            if (here < self.FACING_COSINE
                                    or there < self.FACING_COSINE):
                                continue
                            if best is None or gap < best:
                                best = gap
            nearby += int(saw)
            if best is not None:
                within += 1
                if nearest is None or best < nearest:
                    nearest = best
        return {'measured': len(collided), 'within': within, 'nearby': nearby,
                'reach': cell, 'nearest': nearest}

    def refuse_collided_layer(self, at_risk, where, duplicates):
        """Refuse a mesh whose layer stacks have grown into one another.

        DP-121. The merge above declines on two readings and only one of them
        describes a mesh a user can have. A periodic pair with coincident
        nodes is the part imported twice that the merge's own docstring
        names -- the mesh is whole, the merge would weld it, so declining is
        the right answer and a warning is the right weight.

        A *layer* with coincident nodes is not that. The extruded stack gets
        its own nodes and, on a layer that fits, no two of them share a place:
        MEASURED across the `9f6abca1` corpus, 16 of the 17 layered meshes
        have no coincident nodes at all. Two others -- `annulus_shell` and
        `centrifugal_impeller` -- grew their bases inwards from a hole and
        still came out clean, which is why the direction is not the reading
        and this is. The one mesh that collides is the one checkMesh then
        called not runnable, and the one whose msh export lost 448 nodes on
        the way back in.

        So the layer arm refuses. Immediately, and without a refit: DP-374
        measured what a thinner layer does here, and the answer is nothing,
        and DP-76 is the standing example of a layer fault that a halving
        walks further into rather than out of. The message says what to
        change instead.

        DP-381. *Which* message depends on a reading this refusal used to
        skip. Coincident nodes on a layer are two faults wearing one name:
        two stacks that met, and one column that never grew.
        :meth:`stalled_columns` tells them apart, and the second is refused
        in its own words -- because the advice this arm gives (thinner,
        fewer, mind the gap) is measurably inert against it. MEASURED on
        ``two_cubes_two_files``: at half the asked thickness and at a tenth
        of it the refusal is identical, the same 2 nodes at the same point;
        at one layer instead of three Gmsh fails elsewhere in the extrusion;
        and the two cubes are a unit apart, so there is no gap to mind. What
        does clear it is the surface resolution -- 0.030 and 0.055 both mesh
        where 0.04145781 does not.
        """
        collided = at_risk.get(LAYER_PROTECTION)
        if not collided:
            return
        patches = sorted(where.get(LAYER_PROTECTION) or ())

        def place_of(nodes):
            try:
                return tuple(float(value) for value in
                             self.gmsh.model.mesh.getNode(min(nodes))[0])
            except Exception:                                # noqa: BLE001
                return ()

        def first(point):
            return (f' first at ({point[0]:.6g}, {point[1]:.6g}, '
                    f'{point[2]:.6g}),' if len(point) == 3 else '')

        # DP-381. Which of the two faults this is, measured rather than
        # assumed. Both leave coincident nodes on a protected surface and
        # the old arm read every one of them as the first.
        stalled = self.stalled_columns(collided)
        if stalled:
            standing = sorted({node for nodes in stalled.values()
                               for node in nodes})
            grown = sorted({self.surface_patch_name(base)[0]
                            for base in stalled})
            named = ', '.join(grown)
            asked = float((self.statistics.get('layers') or {}).get(
                'requestedTotalThickness') or 0.0)
            beside = (f'the columns beside them grew the whole '
                      f'{asked:.6g} m that was asked for, and ' if asked else '')
            point = place_of(standing)
            # DP-375. The second half of the same lever, and the cheaper
            # half. MEASURED on `two_cubes_one_file`, the same job run five
            # times with nothing changed but this setting: frontal_delaunay
            # refuses at (0.6, 0.445744, 0); mesh_adapt, delaunay and
            # frontal_delaunay_quads all mesh it, at full coverage, and in
            # none of their surface meshes does a node stand within 3.8 mm of
            # that point. The node that cannot grow is one this algorithm
            # put there.
            algorithm = str((self.intent.get('algorithms') or {}).get(
                'surface') or '')
            ran = f' (this ran {algorithm})' if algorithm else ''
            raise LayerCollision(
                f'the boundary layer grew by nothing at {len(standing)} of '
                f'its nodes: the top of the stack was written at the '
                f'coordinates of the base node it grew from,{first(point)} '
                f'on {count_text(len(stalled), "surface")} it was grown '
                f'from ({named}). A column of no height is not a cell, and '
                f'the mesh cannot be written down either — the exporter '
                f'emits both nodes of every pair and any reader welds them '
                f'back. This is not a layer that ran out of room: '
                f'{beside}the nodes that did not move have nothing standing '
                f'within reach of them. Scaling the ask scales these '
                f'columns by nothing, so a thinner layer lands in the same '
                f'place. What moves them is where the surface mesh puts '
                f'them: change the surface resolution, or the surface '
                f'algorithm{ran}, so these nodes are not meshed where they '
                f'are — or take the layer off {named}.',
                nodes=len(standing), coincident=len(duplicates),
                patches=tuple(patches), at=point, stalled=True)

        place = place_of(collided)
        at = first(place)
        named = ', '.join(patches[:6]) + ('...' if len(patches) > 6 else '')

        # DP-403. Measure before advising, as the stalled arm above does.
        # Two stacks growing `asked` each can only meet across a gap narrower
        # than twice it; the reach below is twice that again, so a wall this
        # does not find could not have been the wall the stacks met.
        asked = float((self.statistics.get('layers') or {}).get(
            'requestedTotalThickness') or 0.0)
        census = self.walls_within_reach(collided, 4.0 * asked)
        record = self.statistics.get('layers')
        if isinstance(record, dict) and census is not None:
            record['collision'] = dict(census)

        if census is not None and not census['within']:
            # The count a nearness test would have returned, quoted so the
            # claim is checkable rather than asserted: on `flange` it is 894
            # against 0, and it is the whole reason the nearness test alone
            # could not be trusted.
            crowd = (f' {census["nearby"]} of them do have another layer '
                     f'surface that close, but not one of those stands across '
                     f'from the node — they are the neighbouring faces of the '
                     f'same wall, which a CAD solid arrives split into.'
                     if census['nearby'] else '')
            raise LayerCollision(
                f'the boundary layer grew into itself: {len(collided)} of its '
                f'nodes stand on top of another node,{at} across '
                f'{len(patches)} of the surfaces it was grown from ({named}). '
                f'Stacks that meet leave cells with no volume between them, '
                f'and the mesh cannot be written down either — the exporter '
                f'emits both nodes of every pair and any reader welds them '
                f'back. This is not a layer that ran out of room between two '
                f'walls: not one of those {census["measured"]} nodes has a '
                f'wall facing it within {census["reach"]:.6g} m, which is '
                f'four times the {asked:.6g} m the layer asked for, and two '
                f'stacks can only meet across a gap narrower than twice the '
                f'ask.{crowd} They met with nothing across from them, so a '
                f'thinner layer meets in the same place — change the surface '
                f'resolution so these nodes are not meshed where they are, or '
                f'take the layer off {named}.',
                nodes=len(collided), coincident=len(duplicates),
                patches=tuple(patches), at=place)

        measured = ''
        if census is not None:
            near = (f', the nearest {census["nearest"]:.6g} m away'
                    if census['nearest'] is not None else '')
            measured = (f' {census["within"]} of {census["measured"]} of '
                        f'these nodes have a wall facing them within '
                        f'{census["reach"]:.6g} m{near}, so there is a gap '
                        f'here to mind.')
        raise LayerCollision(
            f'the boundary layer grew into itself: {len(collided)} of its '
            f'nodes stand on top of another node,{at} across '
            f'{len(patches)} of the surfaces it was grown from ({named}). '
            f'Stacks that meet leave cells with no volume between them, and '
            f'the mesh cannot be written down either — the exporter emits '
            f'both nodes of every pair and any reader welds them back.'
            f'{measured} Grow a '
            f'thinner layer, grow fewer of them, or take the layer off the '
            f'surfaces that face each other across a narrow gap.',
            nodes=len(collided), coincident=len(duplicates),
            patches=tuple(patches), at=place)

    def prism_count(self):
        try:
            types, tags, _nodes = self.gmsh.model.mesh.getElements(3)
        except Exception:                                    # noqa: BLE001
            return 0
        found = 0
        for etype, group in zip(types, tags):
            name = self.gmsh.model.mesh.getElementProperties(etype)[0]
            if str(name).startswith('Prism'):
                found += len(group)
        return found

    def surface_census(self) -> dict:
        """What the surface mesh is made of, by family name."""
        census: dict = {}
        try:
            types, tags, _nodes = self.gmsh.model.mesh.getElements(2)
        except Exception:                                    # noqa: BLE001
            return census
        for etype, group in zip(types, tags):
            name = str(self.gmsh.model.mesh.getElementProperties(etype)[0])
            census[name.split()[0]] = census.get(name.split()[0], 0) + len(group)
        return census

    def split_quadrangles(self):
        """Split the recombined surface mesh back into triangles.

        Plan 31 FC-C, ledger row ``split-quadrangles``. This is the answer to
        a recombined mesh the OpenFOAM route refuses. The derivation used to
        clear the recombination and warn; with this on it keeps it, and the
        quadrangles are cut in two before the volume pass runs, so the mesh
        that reaches the publisher is tetrahedral.

        The position matters and is MEASURED, not chosen. Called after
        ``generate(3)`` the quadrangles do split, but the pyramids the volume
        pass already built against them stay -- and the pyramids are the
        family the polyMesh route rejects. Called between the two passes there
        are none to leave behind.

        Nothing here reads an option back: the split is recorded as the
        surface census before and after it, and the volume families the run
        ends with are counted separately in :meth:`measure_families`.
        """
        before = self.surface_census()
        quads = before.get('Quadrilateral', 0)
        if not quads:
            reason = ('splitting quadrangles was asked for and the surface '
                      'mesh has none: '
                      + (', '.join(f'{name}={count}' for name, count
                                   in sorted(before.items())) or 'no surface '
                         'elements at all')
                      + '. Nothing was split')
            self.warnings.append(reason)
            self.ledger.record('gmsh/algorithms/splitQuadrangles',
                               'the recombined surface mesh, as triangles',
                               'no quadrangles to split', applied=False,
                               note=reason)
            return
        try:
            self.gmsh.model.mesh.splitQuadrangles()
        except Exception as error:                           # noqa: BLE001
            reason = (f'the recombined surface mesh could not be split back '
                      f'into triangles: {error}')
            self.warnings.append(reason)
            self.ledger.record('gmsh/algorithms/splitQuadrangles',
                               f'Quadrilateral={quads}', 'not split',
                               applied=False, note=reason)
            return
        after = self.surface_census()
        census = ', '.join(f'{name}={count}'
                           for name, count in sorted(after.items()))
        self.ledger.record(
            'gmsh/algorithms/splitQuadrangles',
            f'Quadrilateral={quads}', census, applied=not after.get(
                'Quadrilateral', 0),
            note=f'{count_text(quads, "quadrangle")} became '
                 f'{count_text(after.get("Triangle", 0), "triangle")} '
                 'before the volume pass')
        self.statistics['splitQuadrangles'] = {
            'before': before, 'after': after,
            'quadranglesLeft': after.get('Quadrilateral', 0)}
        if after.get('Quadrilateral', 0):
            self.warnings.append(
                f'{count_text(after["Quadrilateral"], "quadrangle")} '
                'survived the split; the volume pass will build pyramids '
                'against them')

    def write_surface_pass(self):
        """DP-133. Leave the surface pass on disk so it can be looked at.

        The instruction was that the mesh be visible as it is built, and that
        a surface mesh and a volume mesh be distinguishable. MEASURED on the
        `45db9031` sweep, a Gmsh leg captured two stages against snappy's
        five, and the reason was not the drawing: this mesh already existed,
        in memory, for the length of the volume pass, and was then discarded
        unshown.

        Written here and not in :meth:`write_outputs` because by the time that
        runs the volume pass has replaced these elements. Failure is recorded
        and never raised -- a run that meshed correctly does not fail because
        a convenience file could not be written.

        Written without the :meth:`refuse_unsafe_write` guard that stands in
        front of every export, and deliberately: that guard reads the volume
        census, which does not exist yet here, so calling it would always
        return `''` and read as protection that was not protecting anything.
        It has nothing to do either way -- the trap it contains is a Medit
        `.mesh` write, and this path is named by :attr:`RunLayout.surface`
        rather than by the user, so it is always `.msh`.
        """
        path = Path(self.surface_output)
        try:
            # The same three writer options `write_outputs` pins, for the same
            # reasons, and pinned again here because that method has not run
            # yet: MSH 2.2 is the one version every reader in this product
            # opens, `SaveAll=0` keeps the physical groups that make the
            # patches pickable, and a binary file is one this application
            # cannot read.
            self.set_number('Mesh.MshFileVersion', 2.2)
            self.set_number('Mesh.SaveAll', 0)
            self.set_number('Mesh.Binary', 0)
            self.gmsh.option.setNumber('Mesh.Format', AUTO_FORMAT_CODE)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.gmsh.write(str(path))
        except Exception as error:                        # noqa: BLE001
            self.warnings.append(f'surface pass not kept: {error}')
            return
        try:
            size = path.stat().st_size
        except OSError:
            self.warnings.append('surface pass not kept: it was not written')
            return
        self.statistics['surface'] = {'path': str(path), 'bytes': int(size)}

    #: DP-446. How often the mesh step says it is still there, in seconds.
    #: Long enough that an ordinary two-minute mesh writes four lines rather
    #: than a page, short enough that the two legs this row was written from
    #: -- 2,700 s of silence apiece -- would have written ninety.
    HEARTBEAT_SECONDS = 30.0

    @contextlib.contextmanager
    def meshing_heartbeat(self, dimension):
        """Say the mesher is still there, and let it say where it has got to.

        DP-446. `naca0012/gmsh/refined` and `sphere/gmsh/refined` both ran
        exactly 2700.5 s against the harness ceiling, and both got to the
        same place first: sixteen authored steps, the last of them
        `gmsh threads :: threads=12`, and then nothing at all for the
        remaining thirty-nine minutes. The reading the evidence supports is
        narrow -- authoring finished and is timestamped, so the stop is in
        the mesh execution -- and what it cannot say is whether the mesher
        was working or hung. Neither could the operator, who sees exactly
        what the log sees.

        Two signals, because one of them alone answers the wrong question.
        A timer says the process is alive, which a hung mesher also is. So
        Gmsh's own terminal output is turned on for the duration: it writes
        `Meshing 1D...`, `Meshing 2D...`, `Meshing 3D...` and a line per
        entity, flushed as it goes, and those lines reach the console
        verbatim because the console passes through anything that is not a
        progress record. Between them the two answer it -- Gmsh lines still
        arriving is work, the heartbeat alone is a stop, and the last Gmsh
        line says where.

        The timer runs in a thread and touches nothing of Gmsh's: the option
        is set from this thread before the mesher starts and restored after
        it ends, and the beat itself only reads a clock and reports. The
        reporter takes a lock for the same reason.
        """
        stop = threading.Event()
        started = time.perf_counter()
        beats = [0]

        def beat():
            while not stop.wait(self.HEARTBEAT_SECONDS):
                beats[0] += 1
                elapsed = time.perf_counter() - started
                # The fraction does not move. A number that climbed on a
                # clock rather than on work would be a picture of progress
                # rather than progress, which is the thing this row is about.
                self.reporter.emit(
                    'progress', 'mesh', 0.35,
                    f'still meshing {dimension}D, {elapsed:,.0f}s elapsed',
                    details={'heartbeat': beats[0],
                             'elapsedSeconds': round(elapsed, 1)})

        terminal = None
        if self.gmsh is not None:
            try:
                terminal = self.gmsh.option.getNumber('General.Terminal')
                self.gmsh.option.setNumber('General.Terminal', 1)
            except Exception:                                # noqa: BLE001
                terminal = None
        worker = threading.Thread(target=beat, name='gmsh-heartbeat',
                                  daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=5.0)
            if terminal is not None:
                try:
                    self.gmsh.option.setNumber('General.Terminal', terminal)
                except Exception:                            # noqa: BLE001
                    pass
            self.statistics.setdefault('heartbeat', {})['mesh'] = {
                'beats': beats[0],
                'everySeconds': self.HEARTBEAT_SECONDS,
                'seconds': round(time.perf_counter() - started, 1),
            }

    def generate(self):
        gmsh = self.gmsh
        quality = self.intent.get('quality') or {}
        algorithms = self.intent.get('algorithms') or {}
        # FC-E. One line, and the whole of what "generate 2D" costs at this
        # end. What it costs elsewhere is the four decisions around it: the
        # import check, the census, the optimiser and the family count all
        # read a dimension that used to be a constant.
        dimension = self.generate_dimension
        subject = 'section' if dimension < 3 else 'volume'
        self.reporter.emit('progress', 'mesh', 0.35,
                           f'meshing {dimension}D')
        started = time.perf_counter()
        split = bool(dimension >= 2 and algorithms.get('splitQuadrangles'))
        # DP-74. The inner surface of a boundary layer only exists once the
        # walls are meshed, and whether it folded back through itself can only
        # be read there -- before the volume pass, which is what a fold
        # breaks, and which says nothing about the layer when it does.
        fit = bool(dimension > 2 and self.layer_columns)
        # DP-133. A 3D run now always takes the two-pass route, because the
        # surface mesh is written between the passes and this is the only
        # place it exists. It was always two passes inside Gmsh; what changed
        # is that the boundary between them is now ours to stand on. The
        # extra `generate(2)` costs nothing that was not already being spent
        # -- `generate(3)` meshes the surfaces first regardless, and calling
        # it explicitly does not mesh them twice.
        keep_surface = bool(dimension > 2 and self.surface_output)
        try:
          # DP-446. Everything the mesher does is inside this block, which is
          # exactly the span that used to be silent.
          with self.meshing_heartbeat(dimension):
            if split or fit or keep_surface:
                  # Two passes rather than one, because the split has to happen
                  # between them; see :meth:`split_quadrangles`. A section has no
                  # second pass, so for it the split is simply the last thing
                  # done to the surfaces -- the same call in the same place.
                  gmsh.model.mesh.generate(2)
                  if split:
                      self.split_quadrangles()
                  if fit:
                      self.measure_layer_fit()
                  if keep_surface:
                      # After the split, because the split is what the volume
                      # pass will see, and a surface mesh a user is shown that
                      # is not the one the volume was built on would be a
                      # picture of a mesh that never existed.
                      self.write_surface_pass()
                  if dimension > 2:
                      gmsh.model.mesh.generate(dimension)
            else:
                  gmsh.model.mesh.generate(dimension)
        except MeshFailure:
            raise
        except Exception as error:
            # DP-75. tetgen says only that a segment and a facet intersect,
            # at a point in space with no name on it. The surfaces are still
            # in memory here and nowhere else, so this is the one place the
            # question 'which patch crosses itself' can be answered.
            crossing = self.read_surface_crossings(error)
            if crossing is not None:
                raise crossing from error
            raise MeshFailure(
                f'Gmsh could not mesh the {subject}: {error}') from error
        passes = int(quality.get('netgenPasses', 0) or 0)
        if passes and dimension < 3:
            # The Netgen optimiser moves tetrahedra, and a section has none.
            self.ledger.record(
                'gmsh/optimization/netgenPasses', passes, 0, applied=False,
                note='the Netgen optimiser acts on a volume mesh; this job '
                     'meshes a section')
            passes = 0
        if passes and not bool(quality.get('optimize', True)):
            # DP-620. The explicit Netgen passes are optimisation too, and a
            # user who switched optimisation off did not ask for them.
            self.ledger.record(
                'gmsh/optimization/netgenPasses', passes, 0, applied=False,
                note='Optimize is off, so no optimisation pass was run')
            passes = 0
        for index in range(passes):
            self.reporter.emit(
                'progress', 'optimize', 0.6 + 0.1 * index / max(passes, 1),
                f'optimising, pass {index + 1} of {passes}')
            gmsh.model.mesh.optimize('Netgen')
        # DP-76. After the optimiser, because the optimiser is the last thing
        # that can rescue a cell, and before the order is raised, because a
        # curved cell is measured against a different quality altogether.
        if fit:
            self.measure_layer_landing()
        self.raise_element_order()
        elapsed = time.perf_counter() - started

        self.merge_duplicate_nodes()
        self.renumber_nodes()

        types, tags, _nodes = gmsh.model.mesh.getElements(dimension)
        census, all_tags = {}, []
        for etype, group in zip(types, tags):
            census[gmsh.model.mesh.getElementProperties(etype)[0]] = len(group)
            all_tags.extend(int(item) for item in group)
        if not all_tags:
            raise MeshFailure(
                'meshing finished with no volume elements; the geometry may '
                'have imported as surfaces only'
                if dimension >= 3 else
                'meshing finished with no surface elements, so the section '
                'did not mesh; there is nothing for the publisher to extrude')

        # Every configured limit is measured, not only the primary one. A mesh
        # that passes on SICN and fails on gamma has failed: passing one metric
        # must not mask another.
        limits = quality.get('limits') or [{
            'measure': quality.get('qualityType', 'sicn'),
            'minQuality': quality.get('minQuality', 0.0),
        }]
        metrics, primary_block, inverted = self.measure_limits(all_tags,
                                                               limits)
        # Plan 31 FC-D. The repair pass is reached from the condition the
        # quality gate refuses on, not from a page of its own: if nothing is
        # below the limit there is nothing to rescue, and if something is,
        # this is the last moment the mesh can still be changed.
        if self.repair_poor_elements(primary_block):
            all_tags = []
            types, tags, _nodes = gmsh.model.mesh.getElements(3)
            census = {}
            for etype, group in zip(types, tags):
                name = gmsh.model.mesh.getElementProperties(etype)[0]
                census[name] = len(group)
                all_tags.extend(int(item) for item in group)
            metrics, primary_block, inverted = self.measure_limits(all_tags,
                                                                   limits)

        node_tags, _coords, _params = gmsh.model.mesh.getNodes()
        self.statistics['mesh'] = {
            'seconds': round(elapsed, 3),
            'nodes': len(node_tags),
            'cells': len(all_tags),
            'cellsByType': census,
            'inverted': inverted,
            # FC-E. Which dimension this census counted. On a section these
            # are faces, and they become cells one for one when the publisher
            # extrudes them, so the count is the cell count either way -- but
            # a reader should not have to infer that.
            'dimension': dimension,
        }
        # The achieved block is measured from the mesh, never read back from
        # the request. Reporting the request is defect 7 of section 10. The
        # primary metric stays at the top level so every existing reader keeps
        # working; `metrics` carries the rest.
        self.statistics['achievedQuality'] = dict(primary_block, metrics=metrics)
        if inverted:
            self.warnings.append(
                f'{count_text(inverted, "element")} '
                f'{agreeing(inverted, "is", "are")} inverted; the mesh is '
                'not usable')
        # A layered run whose census is prisms alone is a hollow shell: the
        # layer meshed and the core did not. Measured when a boundary layer is
        # grown off a subset of the boundary, which leaves the core volume
        # unbounded -- Gmsh returns 9.3% of the domain and reports success.
        if (dimension >= 3 and self.statistics.get('layers')
                and not any('Tetrahedron' in name or 'Hexahedron' in name
                            for name in census)):
            raise MeshFailure(
                'the mesh contains boundary-layer cells but no interior cells, '
                'so the core volume was never meshed. This happens when the '
                'layer does not enclose a closed volume.')
        self.reporter.emit(
            'progress', 'mesh', 0.75,
            f'{len(all_tags)} cells in {elapsed:.2f}s',
            {'cells': len(all_tags),
             'minQuality': round(primary_block['minimum'], 6)})

    def measure_limits(self, all_tags, limits):
        """Measure every configured limit, and say which one is primary.

        Split out of `generate` so the mesh can be measured twice by the same
        code -- once as it left the mesher, once after a repair pass. A repair
        judged by a different measurement from the refusal it answers would
        prove nothing.
        """
        metrics, primary_block, inverted = {}, None, 0
        for row in limits:
            block = self.measure_quality(all_tags, row)
            metrics[block['measure']] = block
            inverted = max(inverted, block['inverted'])
            if primary_block is None:
                primary_block = block
        return metrics, primary_block, inverted

    #: The two optimisers Gmsh offers for a mesh that is already bad, as
    #: opposed to one that is merely coarse. Order matters: untangling frees
    #: the inverted elements, relocation then smooths what is left.
    REPAIR_METHODS = ('UntangleMeshGeometry', 'Relocate3D')

    def repair_poor_elements(self, primary_block):
        """Try to rescue the elements the quality gate is about to refuse.

        Plan 31 FC-D, ledger row `optimizer-other-methods`. Returns whether
        the mesh changed, so the caller re-measures rather than reporting
        numbers the mesh no longer has.

        Three measured facts shape this.

        * On a poor **straight-sided** mesh both methods work. MEASURED on a
          coarse sphere whose worst element was 0.021467 minSICN:
          `UntangleMeshGeometry` took it to 0.249006 and `Relocate3D` to
          0.103702 -- both across a 0.05 floor the mesh had failed.
        * On a **curved** mesh they do not. `UntangleMeshGeometry` refuses
          outright, printing "case not supported, abort", and `Relocate3D`
          made a tangled order-2 torus worse, 31 inverted elements to 41. So
          the pass declines on a raised mesh and names what does help there.
        * An accepted call is not evidence. This build accepted
          `optimize("Bogus")` on a 3D mesh without complaint, so each method
          is judged by the quality measured either side of it and the numbers
          are recorded whichever way they went.
        """
        quality = self.intent.get('quality') or {}
        export = self.intent.get('export') or {}
        enabled = bool(quality.get('repairPoorElements', False))
        block = primary_block or {}
        failing = bool(block.get('below_threshold', 0)
                       or block.get('inverted', 0))
        if not failing:
            if enabled:
                self.ledger.record('gmsh/optimization/repairPoorElements',
                                   True, False, applied=False,
                                   note='no element was below the limit, so '
                                        'there was nothing to repair')
            return False
        if not enabled:
            # The refusal names its own remedy. Without this the user is told
            # the mesh is too poor and left to find the control alone -- but
            # DP-76: only where the remedy can act. On a mesh with a boundary
            # layer it cannot, and naming it there is worse than naming
            # nothing, because the user spends a whole second run finding out.
            if not quality.get('optimize', True) and not self.layer_volumes:
                # DP-780. Measured on mesh campaign 0925 G9A: with Optimize
                # off the repair pass left 3 inverted elements, and turning
                # Optimize back on passed the same mesh.
                self.warnings.append(
                    'elements fall below the requested quality with Optimize '
                    'off, so no optimisation pass ran; turn Optimize back on '
                    '(its default) before anything else')
            elif self.layer_volumes:
                self.warnings.append(
                    'elements fall below the requested quality. The repair '
                    'pass (Repair poor elements) is off, and on this mesh it '
                    'would not help: MEASURED on a layered mesh, '
                    'UntangleMeshGeometry refuses outright — "prism not '
                    'supported yet, abort" — and Relocate3D ran without '
                    'moving a node. Change the layer or the surface sizing '
                    'instead')
            else:
                self.warnings.append(
                    'elements fall below the requested quality; the repair '
                    'pass (Repair poor elements) is off, and it is what runs '
                    'Gmsh\'s untangling and relocation optimisers on exactly '
                    'this mesh')
            return False
        order = int(export.get('elementOrder', 1) or 1)
        if order > 1:
            reason = ('the repair pass acts on straight-sided elements only: '
                      'UntangleMeshGeometry refuses a curved mesh outright, '
                      'and Relocate3D was measured making a tangled order-2 '
                      'torus worse, 31 inverted elements to 41. Use the '
                      'high-order optimiser instead')
            self.warnings.append(
                'the repair pass did not run: ' + reason)
            self.ledger.record('gmsh/optimization/repairPoorElements',
                               True, False, applied=False, note=reason)
            return False

        measure = str(block.get('measure') or 'sicn').lower()
        native = QUALITY_QUERY_NAME.get(measure,
                                        QUALITY_QUERY_NAME[QUALITY_SUBSTITUTE])
        start = self.worst_quality(native)
        steps, changed = [], False
        for name in self.REPAIR_METHODS:
            before = self.worst_quality(native)
            try:
                self.gmsh.model.mesh.optimize(name)
            except Exception as error:                       # noqa: BLE001
                steps.append({'method': name, 'ran': False,
                              'error': str(error)[:200]})
                self.warnings.append(
                    f'the repair method {name} did not run: {error}')
                continue
            after = self.worst_quality(native)
            moved = (before is not None and after is not None
                     and abs(after - before) > 1e-12)
            changed = changed or moved
            steps.append({'method': name, 'ran': True, 'worstBefore': before,
                          'worstAfter': after, 'movedTheMesh': bool(moved)})
        finish = self.worst_quality(native)
        self.statistics['repairPoorElements'] = {
            'measure': native, 'worstBefore': start, 'worstAfter': finish,
            'steps': steps,
        }
        moved_note = ''
        if start is not None and finish is not None:
            moved_note = f'; worst element {start:.6f} -> {finish:.6f}'
            if finish < start - 1e-12:
                self.warnings.append(
                    f'the repair pass left the worst element worse than it '
                    f'found it, {start:.6f} to {finish:.6f}')
        ran = [step['method'] for step in steps if step.get('ran')]
        self.ledger.record(
            'gmsh/optimization/repairPoorElements', True, changed,
            applied=changed,
            note=('ran ' + ', '.join(ran) if ran else 'no repair method ran')
                 + moved_note)
        return changed

    def measure_families(self):
        """The element families the mesh has, against the ones it was to have.

        Plan 31 CP-08 item 3. A recombination or a subdivision is a request
        for a *family* of element, and until now the run recorded that the
        option was set. MEASURED on duct.step: ``Mesh.SubdivisionAlgorithm``
        1 is named "all quadrangles", was accepted, read back as 1, and left
        682 triangles and 1044 tetrahedra -- the same mesh as no subdivision
        at all. Nothing raised and nothing said so.

        The expected families travel in the job because they follow from the
        cell shape and the recombination flag together, and by the time the
        runner is counting elements both are bare option codes.
        """
        algorithms = self.intent.get('algorithms') or {}
        wanted_surface = tuple(algorithms.get('expectedSurfaceFamilies') or ())
        wanted_volume = tuple(algorithms.get('expectedVolumeFamilies') or ())
        if self.planar and wanted_volume:
            # FC-E. The expected families follow from the cell shape, and the
            # cell shape describes a volume pass this job never runs. Cleared
            # here rather than in the plan so the job still records what the
            # cell shape would have owed in three dimensions.
            self.ledger.record(
                'gmsh/algorithms/family.volume',
                ' + '.join(wanted_volume), 'none', applied=False,
                note='not counted: this job meshes a section, and the volume '
                     'cells are made by the publisher when it extrudes it')
            wanted_volume = ()
        gmsh = self.gmsh

        def families(dim):
            try:
                types, tags, _n = gmsh.model.mesh.getElements(dim)
            except Exception:                                # noqa: BLE001
                return {}
            found = {}
            for etype, group in zip(types, tags):
                name = gmsh.model.mesh.getElementProperties(etype)[0]
                # Gmsh names a family by shape and node count, so
                # 'Tetrahedron 4' and 'Tetrahedron 10' are one family.
                shape = str(name).split()[0]
                found[shape] = found.get(shape, 0) + len(group)
            return found

        produced = {'surface': families(2), 'volume': families(3)}
        self.statistics['families'] = {
            'requestedSurface': list(wanted_surface),
            'requestedVolume': list(wanted_volume),
            'produced': produced,
            'barycentricRefinement': self.barycentric_refinement}
        if self.barycentric_refinement:
            # Plan 31 FC-C. A refinement is qualified by the cells it left
            # behind, so the run records the census rather than the option it
            # set: the requested side says what was asked for and the
            # effective side is the mesh. Whether it is four times the cells
            # is a comparison between two runs, and lives in the matrix.
            census = ', '.join(f'{name}={count}' for name, count
                               in sorted(produced['volume'].items())) or 'none'
            self.ledger.record(
                'gmsh/globalSizing/barycentricRefinement.produced',
                'a finer mesh of the same family', census,
                applied=bool(produced['volume']),
                note='the volume cells counted after the subdivision; '
                     'barycentric refinement splits each of them into four '
                     'of the same family')
        if not (wanted_surface or wanted_volume):
            return
        for where, wanted in (('surface', wanted_surface),
                              ('volume', wanted_volume)):
            if not wanted:
                continue
            found = produced[where]
            missing = [name for name in wanted if not found.get(name)]
            note = ', '.join(f'{name}={count}'
                             for name, count in sorted(found.items())) or 'none'
            self.ledger.record(
                f'gmsh/algorithms/family.{where}',
                ' + '.join(wanted), note, applied=not missing,
                matched=not missing,
                note='the element families counted out of the finished mesh')
            if missing:
                self.warnings.append(
                    f'the {where} mesh was asked for '
                    + ' and '.join(missing)
                    + f' and produced {note}; the option was accepted, so '
                      'this is what the mesher decided rather than an error '
                      'it reported')

    #: ``Info: Meshing surface 12 (Plane, Frontal-Delaunay)``. The algorithm
    #: is the last field, and a surface that was retried is logged again with
    #: the algorithm that was retried with, so the last line for a tag is the
    #: one that produced the mesh.
    SURFACE_LOG = re.compile(
        r'Meshing surface (\d+) \(([^)]*)\)')

    def measure_algorithms(self):
        """Which algorithm actually meshed each surface.

        Plan 31 FC-B. ``Mesh.AlgorithmSwitchOnFailure`` is on -- it is on by
        default in Gmsh and has been for every run this product has ever made
        -- so a surface the chosen algorithm cannot mesh is quietly meshed by
        a different one. Reading ``Mesh.Algorithm`` back afterwards returns
        the algorithm that was *asked for*, so it cannot tell the difference.
        Gmsh's own log can: it names the algorithm on the line where the
        surface is meshed.
        """
        requested = int((self.intent.get('algorithms') or {}).get(
            'surfaceCode', 6) or 6)
        wanted = ALGORITHM_NAMES.get(requested, '')
        record = {'requested': requested, 'requestedName': wanted,
                  'fallbackAllowed': bool(
                      (self.intent.get('algorithms') or {}).get(
                          'fallback', True)),
                  'bySurface': {}, 'used': [], 'switched': [],
                  'observed': bool(self.logging)}
        lines = []
        if self.logging:
            try:
                lines = list(self.gmsh.logger.get())
            except Exception:                                # noqa: BLE001
                lines = []
                record['observed'] = False
        for line in lines:
            match = self.SURFACE_LOG.search(line)
            if not match:
                continue
            # 'Plane, Frontal-Delaunay' -- the shape first, the algorithm last.
            algorithm = match.group(2).split(',')[-1].strip()
            record['bySurface'][match.group(1)] = algorithm
        record['used'] = sorted(set(record['bySurface'].values()))
        record['pipeline'] = requested in PIPELINE_ALGORITHMS
        # DP-505. A surface an extrusion made is meshed by that extrusion --
        # the prism tops and sides of a boundary layer, the far face of a
        # planar section -- and Gmsh logs it as `Extruded`. No 2D algorithm
        # is ever chosen for it, so it is not one the fallback switched to.
        # MEASURED on G4 `duct.step`: all 17 surfaces reported as "retried
        # with Extruded" were the 5 layer tops and 12 layer sides; every
        # surface the chosen algorithm was asked to mesh used it.
        record['extruded'] = sorted(
            tag for tag, name in record['bySurface'].items()
            if name == EXTRUDED_ALGORITHM)
        meshed = {tag: name for tag, name in record['bySurface'].items()
                  if name != EXTRUDED_ALGORITHM}
        if wanted and not record['pipeline']:
            record['switched'] = sorted(
                tag for tag, name in meshed.items() if name != wanted)
        self.statistics['algorithms'] = record
        if record['switched']:
            self.warnings.append(
                f'{len(record["switched"])} of {len(meshed)} '
                f'surfaces were not meshed by the {wanted} algorithm that was '
                'chosen: Gmsh retried them with '
                + ' and '.join(sorted({meshed[tag]
                                       for tag in record['switched']}))
                + '. The mesh is sound; it is not the mesh that was asked for.')
        note = 'the algorithm Gmsh logged for each surface it meshed'
        if record['extruded']:
            note += (f'; {count_text(len(record["extruded"]), "surface")} '
                     'made by an extrusion took its mesh from it and are not '
                     'counted')
        observed = sorted(set(meshed.values()))
        self.ledger.record(
            'gmsh/algorithms/surface.used', wanted or requested,
            ', '.join(observed) or (
                'extruded only' if record['extruded'] else 'not observed'),
            applied=not record['switched'],
            matched=(None if record['pipeline']
                     or not (observed or record['extruded'])
                     else not record['switched']),
            note=note)

    def measure_structure(self):
        """Read back what the structured controls actually produced.

        MEASURED: this is the only honest answer available. Gmsh accepts a
        transfinite request it cannot honour and meshes unstructured without
        raising, so the requested count and the produced count are different
        facts and the run reports both. The 1-D elements survive
        ``generate(3)`` and can be counted per curve, even though the written
        ``.msh`` carries none of them -- the exporter saves only elements that
        belong to a physical group, and no curve has one.
        """
        if not (self.transfinite_curves or self.transfinite_surfaces
                or self.transfinite_volumes):
            return
        gmsh = self.gmsh

        def elements_on(dim, tag):
            """``{family: count}`` for one entity, or ``None`` if it is gone."""
            try:
                types, tags, _n = gmsh.model.mesh.getElements(dim, int(tag))
            except Exception:                                # noqa: BLE001
                # The layer extrusion and the farfield cut replace entities.
                # A tag that no longer exists was not left unstructured; it
                # was consumed, and saying so is not the same as failing.
                return None
            found = {}
            for etype, group in zip(types, tags):
                name = gmsh.model.mesh.getElementProperties(etype)[0]
                found[name] = found.get(name, 0) + len(group)
            return found

        curves = {}
        short = []
        for tag, want in sorted(self.transfinite_curves.items()):
            found = elements_on(1, tag)
            produced = None if found is None else sum(found.values())
            curves[str(tag)] = {'control': want['control'],
                                'requested': want['nodes'] - 1,
                                'produced': produced,
                                'law': want['law'],
                                'coefficient': want['coefficient']}
            if produced is not None and produced != want['nodes'] - 1:
                short.append(f'{tag} ({produced} of {want["nodes"] - 1})')
        if short:
            self.warnings.append(
                'a transfinite curve did not produce the element count it was '
                'asked for: curve ' + ', '.join(short))

        surfaces = {}
        unstructured = []
        for tag, want in sorted(self.transfinite_surfaces.items()):
            found = elements_on(2, tag)
            counts = sorted(set(want['nodes']))
            # A structured four-sided face is exactly (n1-1)*(n2-1) quads, or
            # twice that in triangles. Comparing against that number catches
            # every prerequisite Gmsh has and this runner does not know about.
            expected = None
            if len(counts) <= 2 and len(want['nodes']) == 4:
                sides = counts * 2 if len(counts) == 1 else counts
                expected = (sides[0] - 1) * (sides[1] - 1)
            quads = 0 if found is None else sum(
                value for name, value in found.items() if 'Quad' in name)
            tris = 0 if found is None else sum(
                value for name, value in found.items() if 'Triangle' in name)
            structured = None
            if found is not None and expected:
                structured = quads == expected or tris == 2 * expected
            surfaces[str(tag)] = {'control': want['control'],
                                  'requestedCells': expected,
                                  'families': found, 'structured': structured}
            if structured is False:
                unstructured.append(str(tag))
        if unstructured:
            self.warnings.append(
                'a surface was asked to be structured and came back '
                'unstructured: surface ' + ', '.join(unstructured)
                + '; Gmsh does not refuse such a request, so check the node '
                  'counts on the bounding curves')

        volumes = {}
        for tag, want in sorted(self.transfinite_volumes.items()):
            found = elements_on(3, tag)
            volumes[str(tag)] = {'control': want['control'], 'families': found}

        self.statistics['structured'] = {
            'curves': curves, 'surfaces': surfaces, 'volumes': volumes}
        for tag, entry in curves.items():
            self.ledger.record(
                f'curveControl:{entry["control"]}.produced/{tag}',
                entry['requested'], entry['produced'],
                applied=entry['produced'] == entry['requested'],
                note='element count read back out of the meshed curve')
        for tag, entry in surfaces.items():
            self.ledger.record(
                f'curveControl:{entry["control"]}.structured/{tag}',
                True, entry['structured'],
                applied=bool(entry['structured']),
                note='counted against the cells a structured face of these '
                     'node counts has')
        for tag, entry in volumes.items():
            families = entry['families'] or {}
            self.ledger.record(
                f'volumeControl:{entry["control"]}.family/{tag}',
                'structured', ', '.join(sorted(families)) or None,
                applied=bool(families),
                note='the element family the structured volume produced')

    def measure_layers(self):
        """Read the layer back out of the mesh, wall by wall.

        CP-08 item 6. The reading this replaces was a single number -- the
        shortest lateral edge of every prism -- and it answered none of the
        questions a boundary layer raises. MEASURED on a 0.15 x 0.1 x 0.8 m
        duct with three layers on the four walls: it reported a first height
        of 0.002 m rising to 0.00288 m and called that the achieved layer. The
        0.00288 m is not an overshoot. It is the node the two walls of a
        corner share, which moves along their bisector and so travels 1/cos45
        further than the layer is thick, in a mesh that is exactly right. What
        the reading never said is that all 400 wall faces grew a stack, that
        neither cap grew one, or that the stack reaches 0.00728 m into the
        domain away from the corners and 0.00586 m at them.

        So each stack is walked from the wall face it stands on to its inner
        face, and the reading is per patch: how much of the wall is covered,
        how many layers deep, how far the nodes travelled, and how far the
        stack reaches perpendicular to the wall it grew from. The last is the
        thickness the flow sees and the only one of the two that a junction
        changes, which is why both are kept.
        """
        if 'layers' not in self.statistics:
            return
        gmsh = self.gmsh
        record = self.statistics['layers']
        record['achieved'] = None
        cache: dict[int, tuple] = {}

        def point(tag):
            if tag not in cache:
                cache[tag] = gmsh.model.mesh.getNode(tag)[0]
            return cache[tag]

        # The sides of the stack are where the quad option shows up: a
        # recombined layer bounds each prism with a quadrilateral, an
        # unrecombined one with two triangles.
        lateral = self.surface_families(self.layer_laterals)
        record['lateralFaces'] = lateral
        quads = bool((self.intent.get('layers') or {}).get('quads', True))
        recombined = any(name.startswith('Quadrilateral') for name in lateral)
        self.ledger.record(
            'gmsh/boundaryLayers/quads', quads,
            ', '.join(sorted(lateral)) or None,
            applied=bool(lateral) and recombined == quads,
            matched=bool(lateral) and recombined == quads,
            note='the family the sides of the layer came out as')

        owner, faces = {}, {}
        for _dim, tag in gmsh.model.getEntities(2):
            tag = int(tag)
            name = self.surface_patch_name(tag)[0]
            types, _tags, nodes = gmsh.model.mesh.getElements(2, tag)
            for etype, node_group in zip(types, nodes):
                shape, _d, _o, per, *_rest = \
                    gmsh.model.mesh.getElementProperties(etype)
                corners = 3 if shape.startswith('Triangle') else (
                    4 if shape.startswith('Quadrilateral') else 0)
                if not corners:
                    continue
                flat = [int(item) for item in node_group]
                for index in range(0, len(flat), per):
                    # Corner nodes only: a second-order face carries midside
                    # nodes that a prism's bottom triangle does not, and the
                    # two would never match on the full node list.
                    owner[frozenset(flat[index:index + corners])] = name
                    faces[name] = faces.get(name, 0) + 1

        by_bottom, tops, prisms = {}, set(), 0
        types, _tags, nodes = gmsh.model.mesh.getElements(3)
        for etype, node_group in zip(types, nodes):
            shape, _d, _o, per, *_rest = \
                gmsh.model.mesh.getElementProperties(etype)
            if not shape.startswith('Prism'):
                continue
            flat = [int(item) for item in node_group]
            for index in range(0, len(flat), per):
                cell = flat[index:index + per]
                by_bottom[frozenset(cell[:3])] = (cell[:3], cell[3:6])
                tops.add(frozenset(cell[3:6]))
                prisms += 1
        if not prisms:
            self.report_layerless_extrusion(lateral, quads)
            return

        # A stack stands on a face that is nobody's top, and each prism's top
        # face is the next one's bottom.
        columns: dict[str, dict] = {}
        unattributed = 0
        for start in [key for key in by_bottom if key not in tops]:
            base, key, steps = by_bottom[start][0], start, []
            while key in by_bottom:
                bottom, top = by_bottom.pop(key)
                steps.append(sum(math.dist(point(bottom[k]), point(top[k]))
                                 for k in range(3)) / 3.0)
                key = frozenset(top)
            normal = self.face_normal([point(tag) for tag in base])
            head = self.centroid([point(tag) for tag in base])
            tail = self.centroid([point(tag) for tag in key])
            name = owner.get(start)
            if name is None:
                unattributed += 1
                name = '(unnamed surface)'
            entry = columns.setdefault(name, {
                'columns': 0, 'layers': set(), 'first': [], 'travelled': [],
                'reach': []})
            entry['columns'] += 1
            entry['layers'].add(len(steps))
            entry['first'].append(steps[0])
            entry['travelled'].append(sum(steps))
            entry['reach'].append(abs(sum(
                normal[k] * (tail[k] - head[k]) for k in range(3))))

        wanted = set(record.get('patches') or ())
        by_patch, short, unasked = {}, [], []
        first_all, travelled_all, reach_all, depths = [], [], [], set()
        for name, entry in sorted(columns.items()):
            total = faces.get(name)
            coverage = (entry['columns'] / total) if total else None
            by_patch[name] = {
                'selected': name in wanted, 'faces': total,
                'columns': entry['columns'], 'coverage': coverage,
                'layersPerColumn': sorted(entry['layers']),
                'firstHeight': self.spread(entry['first']),
                'travelled': self.spread(entry['travelled']),
                'reach': self.spread(entry['reach']),
            }
            first_all += entry['first']
            travelled_all += entry['travelled']
            reach_all += entry['reach']
            depths |= entry['layers']
            if name not in wanted:
                unasked.append(
                    f'{name} ({count_text(entry["columns"], "column")})')
            elif coverage is not None and coverage < 1 - 1e-9:
                short.append(f'{name} ({entry["columns"]} of {total} faces)')
        for name in sorted(wanted - set(by_patch)):
            by_patch[name] = {'selected': True, 'faces': faces.get(name),
                              'columns': 0, 'coverage': 0.0,
                              'layersPerColumn': [], 'firstHeight': None,
                              'travelled': None, 'reach': None}
            short.append(f'{name} (none of its faces)')

        record['achieved'] = {
            'prisms': prisms, 'columns': sum(item['columns'] for item
                                             in by_patch.values()),
            'layersPerColumn': sorted(depths),
            'firstHeight': self.spread(first_all),
            'travelled': self.spread(travelled_all),
            'reach': self.spread(reach_all),
            'unattributed': unattributed, 'byPatch': by_patch,
        }

        # A layer over part of a wall is worse than no layer over it: the
        # near-wall cell size jumps where the stack stops, and nothing in the
        # run said so before.
        if short:
            self.warnings.append(
                'the boundary layer covers only part of '
                + ', '.join(short) + ': the near-wall spacing jumps where the '
                'stack stops')
        if unasked:
            self.warnings.append(
                'a boundary layer grew on ' + ', '.join(unasked)
                + ', which the plan did not select')
        requested = record['requestedTotalThickness']
        reach = record['achieved']['reach']
        if requested and reach and reach['min'] < .5 * requested:
            self.warnings.append(
                f'the boundary layer is pinched: it reaches {reach["min"]:.3g} '
                f'm into the domain where it was asked for {requested:.3g} m, '
                'which happens where walls meet at a sharp angle')
        self.ledger.record(
            'gmsh/boundaryLayers/totalThickness', requested,
            reach and reach['median'],
            note='measured perpendicular to the wall the stack grew from; at '
                 'a junction the shared node moves along the bisector, so the '
                 'stack reaches less far than its nodes travelled')
        self.ledger.record(
            'gmsh/boundaryLayers/firstHeight', record['requestedFirstHeight'],
            record['achieved']['firstHeight']['median'],
            note='measured from paired prism nodes')
        covered = [item['coverage'] for item in by_patch.values()
                   if item['selected'] and item['coverage'] is not None]
        self.ledger.record(
            'boundaryLayer:coverage', 1.0,
            min(covered) if covered else None, applied=bool(covered)
            and min(covered) >= 1 - 1e-9,
            note='the smallest fraction of a selected wall that grew a stack')

    def report_layerless_extrusion(self, lateral, quads):
        """No prisms came out. Say which of the two reasons it was.

        MEASURED with ``layers.quads`` off on the duct: the extrusion is
        there -- the four walls carry it and the meshed volume is right to
        1e-14 -- but every layer cell came out a tetrahedron, 3,600 of them
        where the same job with the flag on makes 1,200 prisms. The reading
        before this said "the mesh contains no prisms", which reads as a layer
        that failed to grow, and set the achieved figures to null. A layer of
        tetrahedra is a real thing to have made and a poor thing to have asked
        for, so it is named as what it is.
        """
        gmsh = self.gmsh
        counts: dict[str, int] = {}
        for tag in self.layer_volumes:
            types, groups, _nodes = gmsh.model.mesh.getElements(3, int(tag))
            for etype, group in zip(types, groups):
                shape, *_rest = gmsh.model.mesh.getElementProperties(etype)
                counts[shape] = counts.get(shape, 0) + len(group)
        record = self.statistics['layers']
        record['achieved'] = {'prisms': 0, 'columns': 0,
                              'cells': sum(counts.values()),
                              'families': counts, 'lateralFaces': lateral,
                              'byPatch': {}}
        if not counts:
            self.warnings.append(
                'boundary layers were requested but the mesh contains no '
                'layer cells at all')
            return
        family = max(counts, key=counts.get).split()[0].lower()
        self.warnings.append(
            f'the boundary layer was meshed as {family} cells rather than '
            'prisms'
            + ('' if quads else ', because the quad option is off')
            + ': there is no prism stack, so how thick the layer ended up '
              'cannot be read back from the mesh')

    def surface_families(self, tags):
        """Count the element families on a set of surfaces."""
        counts: dict[str, int] = {}
        for tag in tags:
            types, groups, _nodes = self.gmsh.model.mesh.getElements(2,
                                                                     int(tag))
            for etype, group in zip(types, groups):
                shape, *_rest = self.gmsh.model.mesh.getElementProperties(
                    etype)
                counts[shape] = counts.get(shape, 0) + len(group)
        return counts

    @staticmethod
    def spread(values):
        """The min, median and max of a measurement, or None if there is none."""
        values = sorted(values)
        if not values:
            return None
        return {'min': values[0], 'median': values[len(values) // 2],
                'max': values[-1]}

    @staticmethod
    def centroid(coords):
        return [sum(item[k] for item in coords) / len(coords)
                for k in range(3)]

    @staticmethod
    def face_normal(coords):
        """The unit normal of a face, from its first three corners."""
        a, b, c = coords[0], coords[1], coords[2]
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        normal = [u[1] * v[2] - u[2] * v[1],
                  u[2] * v[0] - u[0] * v[2],
                  u[0] * v[1] - u[1] * v[0]]
        length = math.sqrt(sum(value * value for value in normal)) or 1.0
        return [value / length for value in normal]

    def surface_faces(self, tag):
        """Every face of one surface, keyed by its corner nodes."""
        faces = set()
        types, _tags, nodes = self.gmsh.model.mesh.getElements(2, int(tag))
        for etype, node_group in zip(types, nodes):
            shape, _d, _o, per, *_rest = \
                self.gmsh.model.mesh.getElementProperties(etype)
            flat = [int(item) for item in node_group]
            corners = 4 if shape.startswith('Quadrilateral') else 3
            for index in range(0, len(flat), per):
                faces.add(frozenset(flat[index:index + corners]))
        return faces

    @staticmethod
    def transform_point(affine, point):
        """The 4x4 row-major transform Gmsh stores with a periodic pair."""
        return [affine[row * 4 + 0] * point[0] + affine[row * 4 + 1] * point[1]
                + affine[row * 4 + 2] * point[2] + affine[row * 4 + 3]
                for row in range(3)]

    #: DP-548. What the Gmsh route does with an interface pair, said once.
    INTERFACE_PAIR_ACTION = (
        'the Gmsh runner builds no coupling from a pair: two solids are '
        'conformal where they share one CAD face or where duplicate-face '
        'fusion merged their copies, and nowhere else; a translated or '
        'rotated cyclic pair is built by setPeriodic (DP-641)')

    def trace_interface_pairs(self):
        """Say, pair by pair, what the finished mesh holds where it points.

        DP-548. The job never carried the pairs, so a run could not tell a
        pair that took effect from one that did nothing. MEASURED on G6
        ``tee_with_plug.brep`` (audit 0924): the mesh held a 36-face interface
        and the saved pair ``tee_plug_contact``, and nothing said which caused
        which. Neither did: nothing in this runner reads a pair. The interface
        is the plug face the two solids share (DP-546), and the pair's slave
        ``body1_face2`` is the tee's outer end cap -- a boundary face of one
        volume, not the contact.

        So each pair is resolved and graded, not applied. A conformal,
        coincident pair whose two sides are one surface between two volumes is
        ``conformal``, with the faces meshed there counted; any other pair is
        ``not applied`` or ``skipped``, with the reason, and warned about --
        the user asked for a coupling this mesh does not have.
        """
        pairs = [pair for pair in self.job.get('interfacePairs') or ()
                 if isinstance(pair, dict)]
        if not pairs:
            return
        traced = []
        for pair in pairs:
            row = self._trace_interface_pair(pair)
            traced.append(row)
            applied = row['state'] in ('conformal', 'periodic')
            self.ledger.record(
                f'interfacePair:{row["name"]}', row['coupling'],
                row['state'], applied=applied, matched=applied,
                note=row['reason'])
            if not applied:
                self.warnings.append(
                    f'interface pair {row["name"]!r} {row["state"]}: '
                    f'{row["reason"]}')
        self.statistics['interfacePairs'] = {
            'action': self.INTERFACE_PAIR_ACTION, 'pairs': traced}

    def _trace_interface_pair(self, pair) -> dict:
        name = str(pair.get('name') or pair.get('pairId') or '')
        coupling = str(pair.get('coupling') or 'conformal')
        transform = str(pair.get('transform') or 'coincident')
        sides = {}
        for side in ('master', 'slave'):
            token = str(pair.get(f'{side}Scope') or '')
            tags = sorted({int(tag) for tag
                           in self.scope_surfaces.get(token) or ()})
            sides[side] = {
                'scope': token, 'dim': 2, 'tags': tags,
                'names': [self.entity_surface_names.get(tag, f'surface {tag}')
                          for tag in tags],
                # DP-566. What the user picked, before duplicate-face fusion
                # renamed it: G6's slave body1_face4 is meshed as body0_face2,
                # and naming it by the survivor reads as a pair of one face.
                'authored': self._authored_surface_names(token)}
        row = {'name': name, 'coupling': coupling, 'transform': transform,
               'master': sides['master'], 'slave': sides['slave'],
               'state': 'skipped', 'matchedFaces': 0, 'reason': ''}
        missing = [side for side in ('master', 'slave')
                   if not sides[side]['tags']]
        if missing:
            row['reason'] = (
                f'the {" and ".join(missing)} side reached no imported '
                'surface, so there is nothing to grade')
            return row
        master, slave = sides['master']['tags'], sides['slave']['tags']

        def described(side):
            return ', '.join(
                f'{label} (surface {tag}, '
                f'{"between two volumes" if self._bounds_two_volumes(tag) else "a boundary"})'
                for tag, label in zip(sides[side]['tags'],
                                      sides[side]['names']))

        row['state'] = 'not applied'
        if coupling == 'cyclic':
            # DP-641. A transformed cyclic pair is joined to the job's
            # periodic pairs (execution.py) and built by setPeriodic.
            for applied in getattr(self, 'periodic_applied', ()) or ():
                if (str(applied.get('name') or '') == name
                        and sorted(int(tag) for tag
                                   in applied.get('master') or ()) == master
                        and sorted(int(tag) for tag
                                   in applied.get('slave') or ()) == slave):
                    row['state'] = 'periodic'
                    row['reason'] = (
                        f'built by setPeriodic as a {applied.get("transform")}'
                        f' periodic pair; master is {described("master")}, '
                        f'slave is {described("slave")}')
                    return row
        if coupling == 'non_conformal':
            row['reason'] = (
                'Gmsh builds no non-conformal (NCC) coupling, so this pair '
                'is not built on the Gmsh route; master '
                f'is {described("master")}, slave is {described("slave")}')
            return row
        if coupling != 'conformal' or transform != 'coincident':
            row['reason'] = (
                f'a {coupling} {transform} coupling is not built on the Gmsh '
                'route (a transformed match is authored on the Periodic '
                'page); master '
                f'is {described("master")}, slave is {described("slave")}')
            return row
        if set(master) == set(slave) and all(
                self._bounds_two_volumes(tag) for tag in master):
            row['state'] = 'conformal'
            row['matchedFaces'] = sum(self._surface_face_count(tag)
                                      for tag in master)
            row['reason'] = (
                'the two bodies share this face, so the mesh is conformal '
                'there')
            return row
        row['reason'] = (
            f'master is {described("master")} and slave is '
            f'{described("slave")}; they are not one face between two '
            'volumes, and the Gmsh runner does not merge faces for a pair')
        return row

    def _authored_surface_names(self, token) -> list:
        """The names the user's tree gave the surfaces behind *token*."""
        identities = ((self.job.get('scopeEntities') or {}).get('surfaces')
                      or {}).get(str(token)) or ()
        names = (self.job.get('entityNames') or {}).get('surfaces') or {}
        labels = []
        for identity in identities:
            label = names.get(identity)
            if label and str(label) not in labels:
                labels.append(str(label))
        return labels

    def _surface_face_count(self, tag) -> int:
        """How many 2D elements the finished mesh holds on surface *tag*."""
        try:
            _types, elements, _nodes = self.gmsh.model.mesh.getElements(
                2, int(tag))
        except Exception:  # noqa: BLE001 - an unanswerable model counts none
            return 0
        return sum(len(group) for group in elements)

    def measure_periodic(self):
        """Read each periodic pair back out of the finished mesh.

        CP-08 item 5. What the run reported before was ``applied: 1`` --
        that ``setPeriodic`` returned without raising, which is a readback of
        the request and not a fact about the mesh. A pair only becomes a
        coupling when three things hold, and the mesh is the only place any
        of them can be read:

        * every node of the slave surface is in a pair. A partial map still
          reports a healthy node count, and leaves the solver with faces it
          cannot resolve;
        * the transform stored with the pair carries each master node onto
          its partner. This is measured node by node rather than from the
          spread of the offsets, because a rotation moves every node in a
          different direction by design -- MEASURED on a 30 degree sector,
          where the offsets differ across the face and the residual is 0.0;
        * the two triangulations correspond face for face. Matching nodes
          without matching faces is the failure a solver reports at run time,
          long after the mesher has said yes.

        The correspondence is written in the ``.msh``, but only version 4.1
        can be read back: Gmsh 4.15.2 writes a ``$Periodic`` section into a
        2.2 file and returns nothing for it on reload. Publication therefore
        pairs the faces geometrically rather than trusting the file, which is
        why the exported side of this is measured on the boundary file.
        """
        pairs = self.periodic_applied
        if not pairs:
            return
        gmsh = self.gmsh
        cache: dict[int, tuple] = {}

        def point(tag):
            if tag not in cache:
                cache[tag] = gmsh.model.mesh.getNode(tag)[0]
            return cache[tag]

        achieved = []
        for pair in pairs:
            paired = total = matched = slave_faces = 0
            residual = 0.0
            surfaces = []
            for slave_tag in pair['slave']:
                try:
                    master, slaves, masters, affine = \
                        gmsh.model.mesh.getPeriodicNodes(2, slave_tag)
                except Exception:                                # noqa: BLE001
                    master, slaves, masters, affine = 0, [], [], []
                affine = [float(item) for item in affine]
                nodes = [(int(a), int(b)) for a, b in zip(slaves, masters)]
                try:
                    own, _coords, _param = gmsh.model.mesh.getNodes(
                        2, int(slave_tag), includeBoundary=True)
                    here_total = len({int(item) for item in own})
                except Exception:                                # noqa: BLE001
                    here_total = 0
                here_residual = 0.0
                if len(affine) == 16:
                    for slave_node, master_node in nodes:
                        moved = self.transform_point(affine, point(master_node))
                        landed = point(slave_node)
                        here_residual = max(
                            here_residual,
                            max(abs(moved[k] - landed[k]) for k in range(3)))
                mapping = dict(nodes)
                mine = self.surface_faces(slave_tag)
                theirs = self.surface_faces(int(master)) if master else set()
                here_matched = sum(
                    1 for face in mine
                    if None not in (moved := {mapping.get(node)
                                              for node in face})
                    and frozenset(moved) in theirs)
                paired += len(nodes)
                total += here_total
                matched += here_matched
                slave_faces += len(mine)
                residual = max(residual, here_residual)
                surfaces.append({
                    'slave': int(slave_tag), 'master': int(master),
                    'slavePatch': self.surface_patch_name(int(slave_tag))[0],
                    'masterPatch': (self.surface_patch_name(int(master))[0]
                                    if master else ''),
                    'pairedNodes': len(nodes), 'slaveNodes': here_total,
                    'faces': len(mine), 'matchedFaces': here_matched,
                    'residualMax': here_residual})
            record = {'name': pair['name'], 'transform': pair['transform'],
                      'pairedNodes': paired, 'slaveNodes': total,
                      'faces': slave_faces, 'matchedFaces': matched,
                      'residualMax': residual, 'surfaces': surfaces}
            achieved.append(record)

            name = pair['name']
            if not paired:
                self.warnings.append(
                    f'periodic pair {name!r} was accepted before meshing but '
                    'the finished mesh carries no node correspondence for '
                    'it, so nothing downstream can couple the two surfaces')
            elif total and paired < total:
                self.warnings.append(
                    f'periodic pair {name!r} matched {paired} of {total} '
                    'nodes on its slave surface: the part that is left over '
                    'cannot be coupled')
            if slave_faces and matched < slave_faces:
                self.warnings.append(
                    f'periodic pair {name!r} pairs its nodes but only '
                    f'{matched} of {slave_faces} faces correspond, so the '
                    'two sides are meshed differently and a solver will '
                    'refuse the coupling')
            self.ledger.record(
                f'periodicPair:{name}',
                f'{pair["transform"]} correspondence',
                (f'{paired}/{total} {agreeing(total, "node")}, '
                 f'{matched}/{slave_faces} {agreeing(slave_faces, "face")}, '
                 f'residual {residual:.3g}'),
                applied=bool(paired) and matched == slave_faces
                and (not total or paired == total),
                note='measured in the written mesh, not read back from the '
                     'call that requested it')
        self.statistics['periodic']['achieved'] = achieved

    def measure_area(self):
        """Sum the section's face areas. FC-E, the planar twin of below.

        The extruded volume is deliberately not predicted from it: this run
        made a section and the publisher makes the cells, so the number
        measured here is the number this run is answerable for.
        """
        gmsh = self.gmsh
        corners = {'Triangle': 3, 'Quadrangle': 4}
        cache: dict[int, tuple] = {}

        def point(tag):
            if tag not in cache:
                cache[tag] = gmsh.model.mesh.getNode(tag)[0]
            return cache[tag]

        total = 0.0
        types, tags, nodes = gmsh.model.mesh.getElements(2)
        for etype, _group, node_group in zip(types, tags, nodes):
            name, _dim, _order, per, *_rest = (
                gmsh.model.mesh.getElementProperties(etype))
            sides = next((count for shape, count in corners.items()
                          if shape in name), 0)
            if not sides:
                continue
            flat = list(node_group)
            for index in range(0, len(flat), per):
                # A quadratic element carries its mid-side nodes after its
                # corners, so the corners alone give the same polygon.
                face = [point(tag) for tag in flat[index:index + sides]]
                for k in range(1, sides - 1):
                    u = [face[k][j] - face[0][j] for j in range(3)]
                    v = [face[k + 1][j] - face[0][j] for j in range(3)]
                    cross = (u[1] * v[2] - u[2] * v[1],
                             u[2] * v[0] - u[0] * v[2],
                             u[0] * v[1] - u[1] * v[0])
                    total += math.sqrt(sum(item * item
                                           for item in cross)) / 2.0
        self.statistics['mesh']['area'] = total

    def measure_volume(self):
        """Sum the cell volumes, so an overlapping layer cannot pass unseen."""
        if self.planar:
            return self.measure_area()
        gmsh = self.gmsh
        shapes = {
            'Tetrahedron': [(0, 1, 2, 3)],
            'Prism': [(0, 1, 2, 3), (1, 4, 2, 3), (2, 4, 5, 3)],
            'Hexahedron': [(0, 1, 3, 4), (1, 2, 3, 6), (1, 3, 4, 6),
                           (1, 4, 5, 6), (3, 4, 6, 7)],
            'Pyramid': [(0, 1, 2, 4), (0, 2, 3, 4)],
        }
        cache: dict[int, tuple] = {}

        def point(tag):
            if tag not in cache:
                cache[tag] = gmsh.model.mesh.getNode(tag)[0]
            return cache[tag]

        total = 0.0
        types, tags, nodes = gmsh.model.mesh.getElements(3)
        for etype, _group, node_group in zip(types, tags, nodes):
            name, _dim, _order, per, *_rest = gmsh.model.mesh.getElementProperties(etype)
            key = next((item for item in shapes if item in name), None)
            if key is None:
                continue
            flat = list(node_group)
            for index in range(0, len(flat), per):
                cell = flat[index:index + per]
                for a, b, c, d in shapes[key]:
                    pa, pb, pc, pd = (point(cell[a]), point(cell[b]),
                                      point(cell[c]), point(cell[d]))
                    u = [pb[k] - pa[k] for k in range(3)]
                    v = [pc[k] - pa[k] for k in range(3)]
                    w = [pd[k] - pa[k] for k in range(3)]
                    total += abs(
                        u[0] * (v[1] * w[2] - v[2] * w[1])
                        - u[1] * (v[0] * w[2] - v[2] * w[0])
                        + u[2] * (v[0] * w[1] - v[1] * w[0])) / 6.0
        self.statistics['mesh']['volume'] = total

    def apply_export_settings(self):
        """Element order, set before meshing because it decides what is meshed.

        Plan 29 WP8, Plan 31 CP-01. The order is not read from the user's
        control but from the derivation, which has already clamped it to what
        the mesher can produce and the selected export can represent -- both
        exporters read first-order elements only, so second order survives on
        a native run and is clamped where an export was asked for. Where it
        was clamped the options are not written at all, so a stale value left
        in a Gmsh session cannot leak into an exported run.
        """
        export = self.intent.get('export') or {}
        quality = self.intent.get('quality') or {}
        order = int(export.get('elementOrder', 1) or 1)
        requested_high_order = int(quality.get('highOrderOptimize', 0) or 0)
        if order < 2:
            # DP-666. The note names the export this run is on, if any: an
            # SU2 run used to be told order 1 was all "the OpenFOAM route"
            # could read.
            route = {'openfoam': 'OpenFOAM', 'su2': 'SU2'}.get(
                str(export.get('targetSolver') or '').lower())
            note = ('first order; the default' + (
                f' and the only order the {route} export can read'
                if route else ''))
            self.ledger.record('gmsh/output/elementOrder', order, 1,
                               note=note)
            # Plan 30 WP12. The high-order optimiser has nothing to curve on a
            # first-order mesh. Recorded as requested-but-not-effective rather
            # than set and ignored.
            self.ledger.record('gmsh/optimization/highOrderOptimize',
                               requested_high_order, 0,
                               note='not set: the high-order optimiser only '
                                    'acts on an order-2 mesh')
            return
        self.set_number('Mesh.ElementOrder', order,
                        control='gmsh/output/elementOrder')
        self.set_number('Mesh.SecondOrderIncomplete',
                        int(bool(export.get('secondOrderIncomplete', False))),
                        control='gmsh/output/secondOrderIncomplete')
        # Plan 31 FC-D. Read by `setOrder` itself, so unlike the option below
        # this one does reach the mesh from here.
        self.set_number('Mesh.SecondOrderLinear',
                        int(bool(export.get('secondOrderLinear', False))),
                        control='gmsh/output/secondOrderLinear')
        # Plan 31 FC-D. Read by `generate`, and only when `Mesh.ElementOrder`
        # above is already 2 -- which is why this line has to stay above the
        # meshing call rather than move next to `setOrder`. MEASURED on the
        # torus at 0.11: setting 0 left 26 inverted elements and a worst of
        # -0.297582, setting 1 left 0 inverted and 0.002620.
        # `record_high_order_optimizer` measures the mesh it produced.
        self.set_number('Mesh.HighOrderOptimize', requested_high_order,
                        control='gmsh/optimization/highOrderOptimize')

    def write_outputs(self):
        outputs = dict(self.job['output'])
        written = {}
        export = self.intent.get('export') or {}
        mesh_format = str(export.get('meshFormat') or 'msh2.2')
        # Plan 30 WP12, corrected by Plan 31 CP-01. The MSH version is derived,
        # never a free preference, and it is now one version for every run:
        # MSH 2.2, which is what OpenFOAM 13's gmshToFoam, this codebase's
        # direct polyMesh publisher, its element census and its
        # geometry-fidelity check all read. WP12 wrote 4.1 for an SU2 target on
        # the reasoning that only 4.1 carries mid-side nodes; 2.2 carries them
        # as element types 11-19, and what 4.1 actually did was make the
        # companion file unreadable by every reader in the product.
        msh_version = float(export.get('mshVersion') or 2.2)
        self.set_number('Mesh.MshFileVersion', msh_version,
                        control='gmsh/output/mshVersion')
        # Plan 31 FC-A, ledger row `export-option-family`. Two writer options
        # decide whether the file that lands on disk is still the mesh that was
        # made, so both are pinned here as run constants instead of inherited
        # from whatever a future Gmsh build happens to default to.
        #
        # MEASURED, Gmsh 4.15.2, `plans/evidence/plan31/fca-format-io/
        # saveall.json`: one box, three named faces and one named volume,
        # written to MSH 2.2 and read back cold. With `Mesh.SaveAll=0` the file
        # returns 4 physical groups and 204 triangles. With `Mesh.SaveAll=1` it
        # returns **0 physical groups** and 508 triangles plus 72 lines -- the
        # unnamed entities are saved and the names are dropped. Every patch in
        # this product is a physical group, so SaveAll=1 does not make a fuller
        # file, it makes a mesh whose boundaries the publisher cannot name.
        #
        # `Mesh.Binary` is pinned for a different reason: the direct polyMesh
        # publisher reads the MSH as text, so a binary file is one this
        # application cannot open. It is not even the smaller file at this
        # scale -- measured 55477 bytes binary against 44676 ASCII for the same
        # mesh -- so there is nothing to trade away by pinning it off.
        self.set_number('Mesh.SaveAll', 0)
        self.set_number('Mesh.Binary', 0)
        for key, destination in outputs.items():
            path = Path(destination)
            refusal = self.refuse_unsafe_write(path)
            if refusal:
                written[key] = {'path': str(path), 'error': refusal}
                self.warnings.append(f'{key} export refused: {refusal}')
                continue
            # A second-order mesh survives only in Gmsh's own SU2 writer, so
            # that one file is written by format code rather than by extension.
            su2 = mesh_format == 'su2' and path.suffix.lower() == '.su2'
            self.gmsh.option.setNumber(
                'Mesh.Format', SU2_FORMAT_CODE if su2 else AUTO_FORMAT_CODE)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.gmsh.write(str(path))
            except Exception as error:
                written[key] = {'path': str(path), 'error': str(error)[:300]}
                self.warnings.append(f'{key} export failed: {error}')
                continue
            if su2:
                fmt = 'su2'
            elif path.suffix.lower() == '.msh':
                fmt = f'msh{msh_version:g}'
            else:
                fmt = path.suffix.lower().lstrip('.')
            row = {'path': str(path), 'bytes': path.stat().st_size,
                   'format': fmt}
            identity = self.verify_written(path)
            if identity:
                row['identity'] = identity
                if identity.get('matched') is False:
                    self.warnings.append(
                        f'{key} export does not read back as the mesh that '
                        f'was made: {identity.get("difference", "")}')
            written[key] = row
        self.statistics['outputs'] = written

    #: Suffixes this build writes *and* reads, so a written file can be
    #: compared with the mesh that produced it instead of merely weighed.
    #:
    #: Plan 31 FC-A, row `export-formats-unexposed`. MEASURED 2026-09-06 with
    #: Gmsh 4.15.2 (`plans/evidence/plan31/fca-format-io/formats.json`): one
    #: box, three named faces, one named volume, written and reopened cold.
    #: MSH 2.2, MED, CGNS and UNV each returned 307 nodes, 984 tetrahedra and
    #: all four group names. `.vtk` is writable and is not here: it came back
    #: with the right element counts and **no physical groups at all**.
    #: `.su2` is not here either -- this build writes it and does not read it,
    #: which is why the SU2 route has its own census rather than this check.
    VERIFIABLE_SUFFIXES = ('.msh', '.med', '.cgns', '.unv')

    #: The line the out-of-process reader prints its census on.
    VERIFY_MARKER = 'FOAMMESH_CENSUS '

    #: How long that reader gets before the check is abandoned as unanswerable.
    VERIFY_TIMEOUT_SECONDS = 900

    #: The reader itself: a second interpreter that opens the written file and
    #: prints what is in it. It is a separate process on purpose.
    #:
    #: MEASURED 2026-09-06, Gmsh 4.15.2, and this is the whole reason the check
    #: is shaped this way -- see
    #: `plans/evidence/plan31/fca-format-io/isolation.json`. Reading a written
    #: file back inside the meshing session was tried first, into a scratch
    #: model added for the purpose. For MSH, CGNS and UNV that isolates: the
    #: merge lands in the scratch model and the live one is unchanged. **For
    #: MED it does not.** Gmsh's MED reader selects the model by the mesh name
    #: stored in the file, and that name is the name of the model the file was
    #: written from -- so the merge went straight back into the live model,
    #: taking it from 307 nodes and 984 cells to 614 and 1968, and leaving
    #: eight physical groups where there were four: the originals plus MED's
    #: 80-character space-padded copies of them. The next CGNS write then died
    #: outright with `Name exceeds 32 characters limit: inlet` followed by 75
    #: spaces. In other words the in-session check destroyed the mesh it was
    #: checking. Out of process, the same four formats each returned 307 nodes,
    #: 984 cells and all four names with the live model measurably untouched
    #: before and after, so that is how it is done.
    VERIFY_READER = (
        'import json, sys\n'
        'import gmsh\n'
        'gmsh.initialize()\n'
        'gmsh.option.setNumber("General.Terminal", 0)\n'
        'gmsh.merge(sys.argv[1])\n'
        'tags, _c, _p = gmsh.model.mesh.getNodes()\n'
        # DP-674. A planar run's cells are its two-dimensional elements; the
        # dimension to count arrives as the second argument.
        'dim = int(sys.argv[2]) if len(sys.argv) > 2 else 3\n'
        '_t, cells, _n = gmsh.model.mesh.getElements(dim)\n'
        'names = []\n'
        'for dim, tag in gmsh.model.getPhysicalGroups():\n'
        '    label = str(gmsh.model.getPhysicalName(dim, tag)).strip()\n'
        '    if label:\n'
        '        names.append(label)\n'
        'print("FOAMMESH_CENSUS " + json.dumps({\n'
        '    "nodes": len(tags),\n'
        '    "cells": sum(len(item) for item in cells),\n'
        '    "groups": sorted(names)}))\n'
        'gmsh.finalize()\n'
    )

    def model_group_names(self):
        """``[(dim, name)]`` for the current model, with MED's padding removed.

        MEASURED: MED writes every physical name space-padded to 80 characters
        and hands the padding straight back on the read, so `inlet` returns as
        `'inlet' + ' ' * 75`. An identity check that did not strip would call a
        perfectly good file wrong, and the fix belongs here rather than in the
        comparison, because every reader of these names wants the same thing.
        """
        names = set()
        for dim, tag in self.gmsh.model.getPhysicalGroups():
            label = str(self.gmsh.model.getPhysicalName(dim, tag)).strip()
            if label:
                names.add((int(dim), label))
        return sorted(names)

    def named_node_count(self):
        """How many nodes an element of a named group uses, or None.

        That is what a file written with `Mesh.SaveAll=0` carries: MEASURED on
        the G4 `duct_fields_layers` MSH, 2,903 nodes and every one of them
        used by an element, from a model of 2,911. None when this Gmsh cannot
        be asked which entities a group holds.
        """
        model = self.gmsh.model
        if not hasattr(model, 'getEntitiesForPhysicalGroup'):
            return None
        used = set()
        try:
            for dim, tag in model.getPhysicalGroups():
                for entity in model.getEntitiesForPhysicalGroup(dim, tag):
                    _types, _tags, nodes = model.mesh.getElements(dim, entity)
                    for group in nodes:
                        used.update(int(node) for node in group)
        except Exception:
            return None
        return len(used)

    def read_back_census(self, path, dimension=3):
        """Open *path* in a second interpreter and return what is inside it.

        `{'nodes': int, 'cells': int, 'groups': [str]}`, raising when the file
        could not be read at all. The work is one subprocess per written file,
        which costs a Gmsh start-up each time and buys the only guarantee that
        matters here: a read that cannot touch the model being written from.
        See :data:`VERIFY_READER` for the measurement that forced it.
        """
        if not sys.executable:
            raise RuntimeError('there is no interpreter to read the file with')
        completed = subprocess.run(
            [sys.executable, '-c', self.VERIFY_READER, str(path),
             str(int(dimension))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=self.VERIFY_TIMEOUT_SECONDS)
        output = completed.stdout.decode('utf-8', 'replace')
        for line in output.splitlines():
            if line.startswith(self.VERIFY_MARKER):
                return json.loads(line[len(self.VERIFY_MARKER):])
        detail = completed.stderr.decode('utf-8', 'replace').strip()
        raise RuntimeError(
            detail[-300:] or f'the reader exited {completed.returncode} '
                             'without saying what it found')

    def verify_written(self, path):
        """Read *path* back and say whether it is the mesh that was just made.

        Plan 31 FC-A, row `export-formats-unexposed`. The SU2 route already
        answers this question about its own file; every other format was
        reported by size alone -- a number equally happy to describe a file
        with none of the patch names left in it. So each written file is
        reopened and asked the three questions publication depends on: the node
        count, the volume-element count, and the physical group names.

        The comparison is against what the run measured on its own mesh, never
        against the options it set: `statistics['mesh']` is counted from the
        model after meshing, and the names are read off the live model. The
        reopening happens in a separate interpreter, for a measured reason
        recorded on :data:`VERIFY_READER`.

        Returns `{'checked': False, 'reason': ...}` when the question cannot be
        put -- a suffix this build does not read, or a reader that would not
        run -- `{}` when there is nobody to ask, a driver that cannot report
        its physical groups having no identity to compare, and never raises. A file that fails its check is still on disk,
        and the user is owed the reason rather than a traceback in place of an
        export.
        """
        if not hasattr(self.gmsh.model, 'getPhysicalGroups'):
            return {}
        suffix = Path(path).suffix
        if not str(path).lower().endswith(self.VERIFIABLE_SUFFIXES):
            return {'checked': False,
                    'reason': f'{suffix} is written by this build '
                              'and not read by it'}
        mesh = self.statistics.get('mesh') or {}
        expected = {'nodes': int(mesh.get('nodes') or 0),
                    'cells': int(mesh.get('cells') or 0),
                    'groups': [name for _dim, name in self.model_group_names()]}
        try:
            # DP-674. `statistics['mesh']['cells']` counts the elements of the
            # dimension the run meshed, so a planar section is read back as
            # its triangles and quads. Counting volumes instead called every
            # 2D export "486 cells written, 0 read back".
            found = (self.read_back_census(path, dimension=2)
                     if getattr(self, 'planar', False)
                     else self.read_back_census(path))
        except subprocess.TimeoutExpired:
            return {'checked': False,
                    'reason': 'reopening the file took longer than '
                              f'{self.VERIFY_TIMEOUT_SECONDS}s'}
        except Exception as failure:
            return {'checked': True, 'matched': False,
                    'expected': expected,
                    'difference': 'the file could not be reopened: '
                                  f'{str(failure)[:300]}'}
        # DP-505 (G4-P3, `duct_fields_layers`). `Mesh.SaveAll=0` writes the
        # nodes a named element uses and no others, so a model holding nodes
        # no named element touches -- 8 of 2,911 on G4, left on unnamed
        # entities by the layer extrusion -- is written as 2,903 and read back
        # as 2,903, every one of them used, every cell and name intact. The
        # model total was the wrong thing to hold the file to: it called a
        # faithful export a different mesh. A writer that keeps them is still
        # right, so either count is the mesh that was made.
        named = self.named_node_count()
        if named is not None and named != expected['nodes']:
            expected['namedNodes'] = named
        differences = []
        if found['nodes'] not in {expected['nodes'], named}:
            written_nodes = named if named is not None else expected['nodes']
            differences.append(f'{written_nodes} nodes written, '
                               f'{found["nodes"]} read back')
        if found['cells'] != expected['cells']:
            differences.append(f'{expected["cells"]} cells written, '
                               f'{found["cells"]} read back')
        lost = [name for name in expected['groups']
                if name not in set(found['groups'])]
        if lost:
            differences.append('these names did not survive the write: '
                               + ', '.join(sorted(lost)))
        return {
            'checked': True,
            'matched': not differences,
            'nodes': found['nodes'],
            'cells': found['cells'],
            'groups': list(found['groups']),
            'expected': expected,
            'difference': '; '.join(differences),
        }

    #: Geometry suffixes Gmsh can read that this product deliberately does
    #: not accept, and the measured reason for each.
    #:
    #: Plan 31 FC-A, row `import-formats-unexposed`. Gmsh merges 28 of the 39
    #: extensions it writes, so the question is never "can Gmsh read it" but
    #: "is what comes back something this application can mesh and name". Each
    #: entry below was asked that directly -- MEASURED 2026-09-06, Gmsh 4.15.2,
    #: `plans/evidence/plan31/fca-format-io/import_meshability.json`: import
    #: the file, then mesh it twice with the element size set to 0.30 and to
    #: 0.10, and see whether the mesh changes.
    #:
    #: MED, CGNS and UNV do not change: 984 tetrahedra at both sizes, because
    #: they arrive as a finished mesh with an OCC kernel of exactly zero
    #: entities. Accepting one would put every control this mesher offers --
    #: element size, curvature refinement, layers, the farfield cut -- in front
    #: of a user with nothing behind any of them.
    #:
    #: XAO is the opposite and is refused for a different reason. It imports as
    #: real geometry (8 vertices, 12 edges, 6 faces, 1 solid), keeps its group
    #: names on the right entities, and meshes to 238 tetrahedra coarse against
    #: 2584 fine, so the controls do bite. What it has no route through is this
    #: product: a CAD source reaches the mesher by being staged from the
    #: geometry store, whose CAD branch tessellates through OCCT for the
    #: viewport and for per-face identity, and the installed `OCC.Core` has no
    #: XAO reader at all. Offering it would be a menu entry no file could
    #: travel through, so the refusal says what is actually missing.
    UNSUPPORTED_GEOMETRY = {
        '.xao': ('Gmsh imports XAO as geometry and keeps its group names, but '
                 'FoamMesh stages CAD through Open CASCADE, which has no XAO '
                 'reader — so there is no path that could hand this file to '
                 'the mesher. Convert it to STEP or BREP.'),
        '.med': ('a MED file is a finished mesh, not geometry: it imports with '
                 'no CAD kernel behind it, so element size, refinement and '
                 'layers would all have nothing to act on. Import it as a mesh '
                 'instead of meshing it.'),
        '.cgns': ('a CGNS file is a finished mesh, not geometry: it imports '
                  'with no CAD kernel behind it, so element size, refinement '
                  'and layers would all have nothing to act on. Import it as a '
                  'mesh instead of meshing it.'),
        '.unv': ('a UNV file is a finished mesh, not geometry: it imports with '
                 'no CAD kernel behind it, so element size, refinement and '
                 'layers would all have nothing to act on. Import it as a mesh '
                 'instead of meshing it.'),
    }

    #: Suffixes Gmsh's Medit writer handles. Plan 26 WP10.2.
    MEDIT_SUFFIXES = ('.mesh', '.meshb')

    def refuse_unsafe_write(self, path):
        """Refuse a write Gmsh would take the whole process down performing.

        **Gmsh 4.15.2 writing a prism+tet mesh to `.mesh` drops every volume
        element and then segfaults**, leaving a truncated file with no `End`;
        `Mesh.SaveAll=1` changes nothing, and the pure-tet control is fine.
        Measured in ``plans/evidence/plan26-mmg-prisms/``.

        This is an upstream defect, so it cannot be fixed here -- only
        contained. Containment has to happen *before* the call rather than
        around it: a segfault is not an exception, so the `except` clause below
        this cannot catch it, and the crash takes the whole meshing process
        with it along with every result the run had already produced.

        Returns a stated reason, or `''` when the write is safe.
        """
        if str(path).lower().endswith(self.MEDIT_SUFFIXES):
            census = (self.statistics.get('mesh') or {}).get('cellsByType') or {}
            hybrid = sorted(
                name for name in census
                if 'Tetrahedron' not in name and census[name])
            if hybrid:
                return (
                    'Gmsh 4.15.2 crashes writing a mesh containing '
                    f'{", ".join(hybrid)} to the Medit .mesh format, and '
                    'silently drops every volume element before it does. The '
                    'mesh was not written; export it as .msh, .su2 or '
                    'polyMesh instead.')
        return ''

    # -- orchestration ----------------------------------------------------- #

    def execute(self) -> dict:
        self.reporter.emit('started', 'gmsh', 0.0, 'starting Gmsh')
        self.initialise()
        try:
            self.import_geometry()
            self.apply_sizing()
            self.apply_size_fields()
            # Before the curve controls: a three-sided face they make
            # transfinite is filled according to this option.
            self.apply_structuring_options()
            self.apply_curve_controls()
            self.apply_volume_controls()
            self.assemble_background_field()
            # R65. The boundary is read off the intact volume; the layer
            # extrusion below destroys the adjacency that identifies it.
            self.record_boundary_surfaces()
            self.apply_boundary_layers()
            self.apply_periodic()
            self.tag_physical_groups()
            # Element order decides what is meshed, not only what is written,
            # so it has to be set before the mesher runs.
            self.apply_export_settings()
            # After the layers and the physical groups: recombination marks
            # the surfaces that exist at meshing time, and the layer extrusion
            # replaces some of them.
            self.apply_recombination()
            # After recombination: setTransfiniteAutomatic recombines
            # what it structures, so it has to see the surfaces that
            # exist at meshing time.
            self.apply_automatic_structuring()
            self.generate()
            self.measure_algorithms()
            self.measure_families()
            self.measure_structuring()
            self.measure_structure()
            self.measure_layers()
            self.measure_periodic()
            self.trace_interface_pairs()
            self.measure_volume()
            self.write_outputs()
        finally:
            try:
                self.gmsh.finalize()
            except Exception:
                pass
        self.reporter.emit('finished', 'gmsh', 1.0, 'mesh complete')
        return self.result('succeeded')

    def result(self, status: str, error: str = '') -> dict:
        # C31-04. The receipt is published once, at the end, so what it says
        # about each identity is where that identity ended up -- not where the
        # import left it before classification, the farfield cut or an
        # exclusion moved it.
        if isinstance(self.statistics.get('import'), dict):
            self._refresh_receipt_entities()
            self.statistics['import']['sources'] = list(self.import_receipts)
            self.statistics['import']['topologyChanges'] = list(
                self.topology_changes)
        return {
            'schema_version': SCHEMA_VERSION,
            'runner': RUNNER_VERSION,
            'status': status,
            'error': error,
            'job_digest': self.job.get('job_digest', ''),
            'statistics': self.statistics,
            'controls': self.ledger.to_list(),
            'controlMismatches': self.ledger.mismatches(),
            'unresolvedScopes': sorted(set(self.unresolved_scopes)),
            'warnings': list(self.warnings),
            'finished_at': utc_now(),
        }


#: DP-74. How much of the fold limit a refitted layer actually grows. The
#: limit is the thickness at which the facet turns flat, and a facet meshed
#: at exactly that thickness has no area left. Bisecting the impeller put the
#: largest stack that meshes at 0.763 of its limit, so the refit stays under
#: it.
LAYER_FIT_MARGIN = 0.7

#: One measured refit, one halving after it, and then the run is refused.
LAYER_FIT_ATTEMPTS = 3


def folded_like_a_layer(error) -> bool:
    """A volume-mesher refusal that a thinner boundary layer could clear.

    DP-74. The fit reading catches a layer that crosses itself. A layer can
    also reach a wall it did not grow from, which leaves the same tetgen
    refusal with no fold in it at all, so the text is read as well.
    """
    text = str(error).lower()
    return 'plc error' in text and 'intersect' in text


def crossing_like(error) -> bool:
    """A refusal that says the boundary handed to the mesher crosses itself.

    DP-75. tetgen's wording and Gmsh's own wording for the same complaint.
    """
    text = str(error).lower()
    if 'plc error' in text and 'intersect' in text:
        return True
    return 'overlapping facets' in text or 'invalid boundary mesh' in text


def a_remesh_of_imported_facets(job):
    """True when this job builds a remesh of a triangulation it imported.

    DP-93. The fallback below meshes the facets the user supplied, so it can
    only be reached by a job that has facets and chose not to mesh them. A
    CAD job has none, and a job already keeping its tessellation has nothing
    left to fall back to.
    """
    healing = (job.get('intent') or {}).get('healing') or {}
    if healing.get('keepTessellation'):
        return False
    sources = job.get('geometry') or ()
    return bool(sources) and all(
        str(item).lower().endswith(('.stl', '.obj', '.ply'))
        for item in sources)


def kept_tessellation_job(job):
    """A copy of the job that meshes the imported facets as they are."""
    document = copy.deepcopy(job)
    intent = document.setdefault('intent', {})
    healing = intent.get('healing')
    if not isinstance(healing, dict):
        healing = {}
        intent['healing'] = healing
    healing['keepTessellation'] = True
    return document


def layerless_job(job):
    """A copy of the job that meshes this geometry without a boundary layer.

    DP-81. Not the same as a job that never asked for one: the ask is kept
    beside the switch so the run can say what it dropped and what the user
    had wanted, and the receipt is not left claiming the user never asked.
    """
    document = copy.deepcopy(job)
    intent = document.setdefault('intent', {})
    layers = intent.get('layers')
    if not isinstance(layers, dict):
        layers = {}
        intent['layers'] = layers
    layers['requestedEnabled'] = bool(layers.get('enabled'))
    layers['enabled'] = False
    return document


def refitted_job(job, factor):
    """A copy of the job whose layer stack is scaled to what will fit."""
    document = copy.deepcopy(job)
    layers = (document.get('intent') or {}).get('layers') or {}
    # DP-76. Keep what the user asked for beside what is about to replace it.
    # A refitted run cannot otherwise tell the two apart -- every number it
    # can see has already been scaled -- and a refusal raised inside it has
    # to be able to say how far the layer had been cut back to get there.
    stack = layers.get('cumulativeHeights') or ()
    original = layers.get('totalThickness')
    if original is None and stack:
        original = abs(float(stack[-1]))
    if original is not None:
        layers.setdefault('originalTotalThickness', abs(float(original)))
    layers['cumulativeHeights'] = [
        float(value) * factor
        for value in layers.get('cumulativeHeights') or ()]
    for key in ('firstHeight', 'totalThickness'):
        if layers.get(key) is not None:
            layers[key] = float(layers[key]) * factor
    return document


def record_layer_refit(document, asked, first, factor, note):
    """Say in the receipt what was asked for and what was grown instead."""
    record = (document.get('statistics') or {}).get('layers')
    if isinstance(record, dict):
        record['fittedTotalThickness'] = record.get('requestedTotalThickness')
        record['fittedFirstHeight'] = record.get('requestedFirstHeight')
        record['requestedTotalThickness'] = asked
        record['requestedFirstHeight'] = first
        record['fitScale'] = factor
    document.setdefault('warnings', []).insert(0, note)


def execute_with_layer_fit(job, reporter, attempts=None):
    """Run the job, regrowing a boundary layer the geometry cannot carry.

    DP-74. A layer thicker than the wall it grows on can only be discovered
    by growing it, so the fit is a property of a run rather than of a job,
    and the repair is another run. The first refit is measured -- the fold
    reading says what the geometry carries -- and a refusal with no fold in
    it is halved once, because a layer that reaches a wall it did not grow
    from leaves the same message and no fold. Three runs at the outside, and
    every one of them says in the receipt what it grew.
    """
    layers = (job.get('intent') or {}).get('layers') or {}
    stack = list(layers.get('cumulativeHeights') or ())
    carried = bool(layers.get('enabled')) and bool(stack)
    asked = abs(float(stack[-1])) if carried else 0.0
    first = abs(float(stack[0])) if carried else 0.0
    factor = 1.0
    tried = []
    note = ''
    crossing = None
    current = job
    for attempt in range(LAYER_FIT_ATTEMPTS):
        run = GmshRun(current, reporter)
        if attempts is not None:
            attempts.append(run)
        tried.append(asked * factor)
        try:
            document = run.execute()
        except LayerFold as fold:
            following = fold.carries * LAYER_FIT_MARGIN / asked
            note = (
                f'the boundary layer asked for reaches {asked:.6g} m, which '
                f'this geometry cannot carry: {fold.folded} of the faces it '
                f'grew fold back through themselves, first on {fold.patch} '
                f'at ({fold.at[0]:.6g}, {fold.at[1]:.6g}, {fold.at[2]:.6g}), '
                f'where the wall carries {fold.carries:.6g} m.')
        except LayerLanding:
            # DP-76. A layer that did not land is already too thin for the
            # mesh above it; halving it again walks further into the fault
            # the reading just named, so this one leaves immediately and
            # the message it carries is the message the user gets.
            raise
        except SurfaceCrossing as error:
            crossing = error
            if not carried:
                raise
            following = factor * 0.5
            note = (
                f'the volume mesher refused the boundary layer grown at '
                f'{asked * factor:.6g} m as a crossing surface, with no fold '
                f'in the layer itself to measure.')
        except MeshFailure as error:
            if not carried or not folded_like_a_layer(error):
                raise
            following = factor * 0.5
            note = (
                f'the volume mesher refused the boundary layer grown at '
                f'{asked * factor:.6g} m as a crossing surface, with no fold '
                f'in the layer itself to measure.')
        else:
            if factor < 1.0:
                record_layer_refit(document, asked, first, factor, note)
            return document
        if following < 0.05 or attempt + 1 >= LAYER_FIT_ATTEMPTS:
            break
        factor = following
        note += (f' The layer was regrown at {asked * factor:.6g} m, '
                 f'{factor:.1%} of what was asked, keeping the layer count '
                 f'and the growth ratio.')
        reporter.emit('progress', 'mesh', 0.3,
                      'refitting the boundary layer to '
                      f'{asked * factor:.4g} m')
        current = refitted_job(job, factor)
    detail = note.strip()
    if len(tried) > 1:
        detail += (' Thicknesses of '
                   + ', '.join(f'{value:.6g}' for value in tried)
                   + ' m were tried and every one was refused.')
    refusal = MeshFailure(
        'the boundary layer could not be fitted to this geometry: ' + detail
        + ' Mesh this geometry with a thinner layer, or with layers turned '
          'off.')
    # DP-75. A layer refit cannot clear a boundary that crossed itself before
    # the layer was grown, so what the scan found is carried out of here for
    # the caller to act on rather than being spent on another thinner layer.
    refusal.surface_crossing = crossing
    raise refusal


def execute_with_surface_fallback(job, reporter, attempts=None):
    """Run the job, and mesh the imported facets if the remesh will not mesh.

    DP-75. MEASURED on ``drone_quadcopter``, Gmsh 4.15.2: classification at
    40 deg leaves a patch with three boundary loops -- a motor boss, wrapped
    all the way round -- and ``createGeometry`` parametrises it anyway. 85 of
    that patch's own faces then cross each other, 356 more cross the layer
    grown from it, and the volume mesher refuses with `PLC Error: A segment
    and a facet intersect at point`, which names nothing. The imported STL is
    manifold, closed and carries no crossing facets of its own, so the
    boundary the user supplied was never the problem; meshing it as supplied
    gives 276574 cells with no inverted element and full layer coverage on
    both patches.

    DP-93. A crossing was the only failure this fallback listened for, so
    the rung was reachable only when the scan happened to name one. MEASURED
    on the same model with the preparation the product recommends applied:
    the recommended weld drops nine exactly-degenerate facets and rewrites
    the file, the classified remesh built on the result dies with `Some NULL
    points exist in 2D mesh`, and the run was reported failed -- while this
    rung, which had already been written, tested and measured, meshed the
    same job as 262197 cells with no inverted element in 72.9s. What the
    weld does to the surface does not explain that: connectivity is five
    components before and after -- [4581, 164, 164, 164, 164] becomes
    [4572, 164, 164, 164, 164], the four motor bosses being separate shells
    in the supplied file already -- and both files carry 0 boundary edges
    and 0 non-manifold edges. Why the remesh fails is still open. This rung
    does not rest on knowing that; it rests on the rung below meshing the
    job the rung above could not.
    """
    crossing = None
    unmeshable = None
    try:
        return execute_with_layer_fit(job, reporter, attempts)
    except SurfaceCrossing as error:
        crossing = error
    except LayerCollision:
        # DP-374. A collision is not a remesh that could not be meshed, and
        # this rung answers only that question. MEASURED on
        # `two_cubes_one_file`, the same job three ways: with layers off the
        # remesh meshes 127961 cells and never reaches this rung; with one
        # layer the remesh refuses a 2-node collision on one surface and this
        # rung meshed the raw 24 authored triangles instead, reporting
        # `succeeded` with 36 cells, `coverage 1.0` and every quality reading
        # met; with three layers the same fall lands on a collision of 32
        # nodes across 14 surfaces, because the facets are coarser than the
        # remesh and the stacks are relatively thicker. So the rung cannot
        # cure a collision, it discards the surface sizing that was asked for
        # -- 127961 cells becomes 36 -- and the warning it writes says the
        # remesh `could not be meshed`, which on a collision is not true.
        # The refusal below it already names the measurement and what to do
        # about it, so it is left to travel.
        raise
    except MeshFailure as error:
        crossing = getattr(error, 'surface_crossing', None)
        if crossing is None:
            # DP-93. A crossing is one way the remesh fails and not the only
            # one, and the rung below is the same rung either way. MEASURED
            # on drone_quadcopter with the preparation the product itself
            # recommends applied: the classified remesh dies with `Some NULL
            # points exist in 2D mesh` and the run was reported failed, while
            # the same job with the tessellation kept meshes 262197 cells
            # with no inverted element in 72.9s. Gated on the job actually
            # having built a remesh of imported facets, because a CAD job has
            # no tessellation to fall back to and would only pay twice.
            if not a_remesh_of_imported_facets(job):
                raise
            unmeshable = error
    if crossing is not None and not crossing.remeshed:
        raise crossing
    reporter.emit('progress', 'mesh', 0.3,
                  'meshing the imported facets rather than a remesh of them')
    document = execute_with_layer_fit(
        kept_tessellation_job(job), reporter, attempts)
    document.setdefault('warnings', []).insert(0, (
        f'the remesh Gmsh built from its own parametrisation of this '
        f'geometry could not be meshed — {unmeshable} — so the imported '
        f'triangulation was meshed as it was supplied instead. The mesh '
        f'follows the imported facets exactly and the surface sizing asked '
        f'for did not shape it.'
        if crossing is None else
        f'the remesh Gmsh built from its own parametrisation of this '
        f'geometry crossed itself — {count_text(crossing.pairs, "pair")} '
        f'of faces passed through each other, most of them on '
        f'{crossing.patch} — so '
        f'the imported triangulation was meshed as it was supplied instead. '
        f'The mesh follows the imported facets exactly and the surface '
        f'sizing asked for did not shape it.'))
    return document


#: DP-484. The surface algorithms tried, in order, when a layer column was
#: extruded by nothing. MEASURED on `two_cubes_one_file` (DP-375): Delaunay,
#: MeshAdapt and Frontal-Delaunay for Quads all mesh the job that
#: Frontal-Delaunay refuses.
STALL_ALGORITHMS = (('delaunay', 5), ('mesh_adapt', 1))


def another_surface_algorithm_job(job, name, code):
    """A copy of the job that meshes its surfaces with another algorithm."""
    document = copy.deepcopy(job)
    algorithms = document.setdefault('intent', {}).setdefault('algorithms', {})
    algorithms.setdefault('requestedSurface', algorithms.get('surface'))
    algorithms.setdefault('requestedSurfaceCode', algorithms.get('surfaceCode'))
    algorithms['surface'] = name
    algorithms['surfaceCode'] = code
    return document


def execute_with_another_surface_algorithm(job, reporter, attempts=None):
    """Run the job, and re-mesh the surface if a layer column did not grow.

    DP-484. MEASURED on the representative sweep, `two_cubes_one_file`, Gmsh
    4.15.2, targetSize 0.04145781, three layers: two nodes interior to
    `two_cubes_one_file_wall1` were extruded by exactly zero while the 4,398
    beside them grew the whole ask. DP-381 made the refusal say so and DP-375
    measured the remedy -- the nodes are where Frontal-Delaunay put them, and
    every other surface algorithm tried meshes the job. The refusal then told
    the user to change the algorithm; the run can do that itself and say it
    did. Only a stalled column is retried: two stacks that met across a gap
    are not moved by a different surface mesh.
    """
    try:
        return execute_with_surface_fallback(job, reporter, attempts)
    except LayerCollision as collision:
        if not collision.stalled:
            raise
        first = collision
    algorithms = (job.get('intent') or {}).get('algorithms') or {}
    asked = int(algorithms.get('surfaceCode', 6) or 6)
    asked_name = ALGORITHM_NAMES.get(asked, str(asked))
    tried = [asked_name]
    for name, code in STALL_ALGORITHMS:
        if code == asked:
            continue
        label = ALGORITHM_NAMES.get(code, name)
        tried.append(label)
        reporter.emit('progress', 'mesh', 0.3,
                      f're-meshing the surface with {label}: a boundary-layer '
                      f'column did not grow on the {asked_name} surface mesh')
        try:
            document = execute_with_surface_fallback(
                another_surface_algorithm_job(job, name, code), reporter,
                attempts)
        except LayerCollision as collision:
            if not collision.stalled:
                raise
            continue
        document.setdefault('warnings', []).insert(0, (
            f'the surface was meshed with {label}, not the {asked_name} that '
            f'was asked for. On the {asked_name} surface mesh the boundary '
            f'layer grew by nothing at {first.nodes} of its nodes '
            f'({", ".join(first.patches)}), and a column of no height is '
            f'not a cell. The {label} surface mesh grew every column.'))
        return document
    raise LayerCollision(
        f'{first} Surface algorithms {", ".join(tried)} were each tried and '
        f'every one left a column that did not grow.',
        nodes=first.nodes, coincident=first.coincident,
        patches=first.patches, at=first.at, stalled=True) from first


def execute_without_a_layer_that_will_not_land(job, reporter, attempts=None):
    """Run the job, and mesh without a layer that cannot be landed.

    DP-81. MEASURED on ``drone_quadcopter``, Gmsh 4.15.2, with the
    preparation the product recommends applied. The wall carries about
    0.00023 m of layer before it folds, so :func:`execute_with_layer_fit`
    refits the 0.001695 m asked for down to 0.00016 m -- and the surface
    elements resting on that stack are 22.7 mm across, so 32 of the 961
    cells sitting on the layer top come out flat and the landing reading
    refuses. Thinning again only widens that ratio, which is why the
    landing handler in the fit loop leaves immediately rather than halving;
    and the one move that would close it, refining the wall to the layer's
    own size, is a million surface elements on a 150 mm body for a layer
    9% as thick as the one asked for. No thickness both fits and lands
    here.

    Spending that on the whole mesh is the fault. The same geometry meshes
    to 276574 cells with no inverted element once the layer is off, so a
    refusal here throws away everything the user asked for to avoid
    delivering one optional part of it short. What the run owes them is the
    mesh and a receipt that says, in the reading's own numbers, that the
    layer was dropped and why -- which is what :func:`execute_with_surface_fallback`
    already does for a remesh that crosses itself.
    """
    try:
        return execute_with_another_surface_algorithm(job, reporter, attempts)
    except LayerLanding as landing:
        asked = landing.asked or landing.thickness
        share = (f'{landing.thickness / asked:.1%} of the {asked:.6g} m '
                 f'asked for' if asked else 'all this wall would carry')
        reporter.emit('progress', 'mesh', 0.3,
                      'meshing without the boundary layer, which this '
                      'geometry cannot carry at a thickness the mesh above '
                      'it can rest on')
        document = execute_with_surface_fallback(
            layerless_job(job), reporter, attempts)
        # MEASURED: a run that grew no layer publishes no `layers` block at
        # all, so writing into the one that is there would have dropped this
        # reading on exactly the runs it describes.
        statistics = document.setdefault('statistics', {})
        record = statistics.get('layers')
        if not isinstance(record, dict):
            record = {'enabled': False}
            statistics['layers'] = record
        record['droppedAfterLanding'] = {
            'requestedTotalThickness': asked,
            'grownTotalThickness': landing.thickness,
            'degenerateCells': landing.cells,
            'worst': landing.worst,
            'measure': 'gamma',
        }
        document.setdefault('warnings', []).insert(0, (
            f'the boundary layer asked for was not grown: this wall carries '
            f'a stack only {landing.thickness:.6g} m thick before it folds '
            f'— {share} — and a stack that thin under the surface mesh '
            f'around it leaves the volume mesher a gap it can fill only with '
            f'flat cells: {landing.cells} of the cells resting on it came out '
            f'with no height, the worst at {landing.worst:.3e} gamma. The '
            f'mesh below was built without a layer. To grow one here, refine '
            f'the surface mesh at the wall so its elements come nearer the '
            f'layer in size, or ask for a layer this wall can carry.'))
        return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='FoamMesh Gmsh runner')
    parser.add_argument('job')
    parser.add_argument('--result')
    parser.add_argument('--progress')
    arguments = parser.parse_args(argv)

    reporter = Reporter(arguments.progress)
    attempts: list = []
    try:
        job = load_job(Path(arguments.job))
        result_path = Path(arguments.result or job.get('resultPath')
                           or Path(arguments.job).with_name('result.json'))
        # DP-74. One call, one or more runs: a boundary layer the geometry
        # cannot carry is refitted and the job re-run, and it is the last of
        # those runs that has to report if the refit does not land either.
        document = execute_without_a_layer_that_will_not_land(
            job, reporter, attempts)
        status = 0
    except Exception as error:  # noqa: BLE001 - the runner reports, never crashes
        detail = f'{type(error).__name__}: {error}'
        reporter.emit('failed', 'gmsh', 1.0, detail)
        run = attempts[-1] if attempts else None
        document = (run.result('failed', detail) if run is not None else {
            'schema_version': SCHEMA_VERSION, 'runner': RUNNER_VERSION,
            'status': 'failed', 'error': detail, 'statistics': {},
            'controls': [], 'warnings': [], 'finished_at': utc_now(),
        })
        document['traceback'] = traceback.format_exc()[-4000:]
        try:
            result_path = Path(arguments.result
                               or Path(arguments.job).with_name('result.json'))
        except Exception:
            result_path = Path('result.json')
        status = 1

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return status


if __name__ == '__main__':
    sys.exit(main())
