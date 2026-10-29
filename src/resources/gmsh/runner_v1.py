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
    from shell_topology import (
        ShellTopologyError, plan_interface_sheets, resolve_topology)
    from field_graph import FieldGraphError, build_graph
    from layer_targets import (
        MODE_ALL_WALLS, eligible_wall_names, normalise_mode)
    from quantities import agreeing, count_text
    import farfield_primitives
    import native_fields_v1
    import point_sizes_v1
    import entity_controls_v1
    import structure_v1
    import sizefield_consumer_v1
    from stages import (
        StageRun, InvalidSurfaceMesh, Ledger, LayerCollision, LayerFold,
        LayerLanding, MeshFailure, SurfaceCrossing, TESTED_RECOMBINERS,
        _cavities, _successors_of, invalid_surface_counts)
except ImportError:  # pragma: no cover - exercised only by the module loader
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from shell_topology import (
        ShellTopologyError, plan_interface_sheets, resolve_topology)
    from field_graph import FieldGraphError, build_graph
    from layer_targets import (
        MODE_ALL_WALLS, eligible_wall_names, normalise_mode)
    from quantities import agreeing, count_text
    import farfield_primitives
    import native_fields_v1
    import point_sizes_v1
    import entity_controls_v1
    import structure_v1
    import sizefield_consumer_v1
    from stages import (
        StageRun, InvalidSurfaceMesh, Ledger, LayerCollision, LayerFold,
        LayerLanding, MeshFailure, SurfaceCrossing, TESTED_RECOMBINERS,
        _cavities, _successors_of, invalid_surface_counts)

#: Plan 48 seam. One list of hooks per stage of a job, filled from the modules
#: of ``runner_parts`` (see its docstring) so that an area adds its stage work
#: in its own file and ``runner_v1.py`` stays unedited. A hook is called as
#: ``hook(run, **context)``. With no part registered nothing runs, and the
#: job passes through exactly as before.
RUNNER_STAGES = ('before_import', 'after_import', 'geometry', 'fields',
                 'before_mesh_pass', 'after_mesh_pass', 'export')
STAGE_HOOKS: dict = {stage: [] for stage in RUNNER_STAGES}
_PARTS_LOADED: list = []


def add_stage_hook(stage, hook):
    if stage not in STAGE_HOOKS:
        raise ValueError(f'unknown runner stage {stage!r}')
    STAGE_HOOKS[stage].append(hook)


def load_runner_parts():
    """Import ``runner_parts`` once and let each part register its hooks."""
    if _PARTS_LOADED:
        return list(_PARTS_LOADED)
    try:
        import runner_parts
    except ImportError:  # pragma: no cover - exercised only by the module loader
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import runner_parts
    _PARTS_LOADED.extend(runner_parts.load_parts(add_stage_hook))
    return list(_PARTS_LOADED)


def run_stage_hooks(stage, run, **context):
    for hook in tuple(STAGE_HOOKS[stage]):
        hook(run, **context)


SCHEMA_VERSION = 1
RUNNER_VERSION = 'gmsh-runner-v1'


class ContractFailure(RuntimeError):
    """The job does not satisfy the contract this runner implements."""



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


def plan_shared_faces(triangles: dict) -> tuple[dict, dict]:
    """Which triangles two imported solids both carry, and what each keeps.

    DP-2876. *triangles* maps an imported surface tag to its triangles as
    node triples, nodes already welded. A triangle two surfaces both hold,
    walked the opposite way round, is a face two closed bodies share: each
    body draws it, outward from itself.

    Returns ``(shared, kept)``. ``shared`` is ``{(first, second): [triples]}``
    for every pair of surfaces with such faces, in the first surface's
    winding, and ``kept`` is ``{tag: [triples]}`` for every surface that lost
    any, without them. A triangle held twice the *same* way round is a doubled
    import, not an interface, and is left where it is for the refusals that
    are about exactly that.
    """
    owners: dict = {}
    for tag in sorted(triangles):
        for triple in triangles[tag]:
            owners.setdefault(frozenset(int(node) for node in triple),
                              []).append((tag, tuple(int(node)
                                                     for node in triple)))
    shared: dict = {}
    moved: dict = {}
    for held in owners.values():
        if len(held) != 2:
            continue
        (first, one), (second, other) = held
        if first == second or oriented_key(one) == oriented_key(other):
            continue
        shared.setdefault((first, second), []).append(one)
        moved.setdefault(first, set()).add(oriented_key(one))
        moved.setdefault(second, set()).add(oriented_key(other))
    kept = {tag: [triple for triple in triangles[tag]
                  if oriented_key(triple) not in moved[tag]]
            for tag in moved}
    return shared, kept


def find_interface_sheets(gmsh, shared_faces=(), source_of_import=None) -> dict:
    """:func:`plan_interface_sheets` on the model as imported.

    The faces :func:`separate_shared_faces` made take part in the proof that
    a body closes, because they close it too, and are not reported again.
    *source_of_import* (surface tag -> file) groups a body's named solids,
    so a body imported as several of them is measured whole (DP-6060).
    """
    triangles = {}
    for _dim, tag in gmsh.model.getEntities(2):
        _elements, nodes = gmsh.model.mesh.getElementsByType(2, tag)
        flat = [int(value) for value in nodes]
        triangles[int(tag)] = [tuple(flat[start:start + 3])
                               for start in range(0, len(flat) - 2, 3)]
    made = {int(tag) for tag in shared_faces}
    groups = None
    if source_of_import:
        # A face DP-2876 made belongs to no file: it stays a surface alone.
        groups = {tag: str(source_of_import[tag]) for tag in triangles
                  if tag in source_of_import and tag not in made}
    return {sheet: owners for sheet, owners
            in plan_interface_sheets(triangles, groups).items()
            if sheet not in made}


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


def separate_shared_faces(gmsh) -> dict:
    """Give each face two imported bodies share one surface of its own.

    DP-2876. MEASURED on Gmsh 4.15.2, the guided walk's `jacketed_pipe`
    (fluid core and solid jacket, two files, welded): the jacket's bore and
    the core's wall are the same 84 triangles drawn once by each body, so
    every edge of the bore is used by four triangles. That is a non-manifold
    surface to `classifySurfaces`, which then cuts the bore along every one
    of those edges -- 86 patches a body, one or two triangles each -- and
    without reparametrisation, because with it the call does not return
    (:meth:`GmshRun.classification_ladder`). Each 3 mm strip was meshed on
    its own at a 2.7 mm size: 1008 triangles below 0.1 on the bore and 800
    tetrahedra below the gate's 0.1, the worst 0.013.

    Moved into one surface the shared triangles leave only the two rims
    where the bore meets the end faces, each edge used three times; the
    reparametrising classification then returns in 4 s with six patches, and
    the two volumes meshed against that one surface share 5218 nodes, so the
    interface is conformal by construction rather than by welding two
    meshes that happened to agree.

    Called by the run and by the out-of-process classification probe, so
    the probe judges the triangulation the run classifies. Returns
    ``{new surface tag: (first owner, second owner)}``; empty, and the model
    untouched, when no two bodies share a face.
    """
    triangles = {}
    for _dim, tag in gmsh.model.getEntities(2):
        _elements, nodes = gmsh.model.mesh.getElementsByType(2, tag)
        flat = [int(value) for value in nodes]
        triangles[int(tag)] = [tuple(flat[start:start + 3])
                               for start in range(0, len(flat) - 2, 3)]
    shared, kept = plan_shared_faces(triangles)
    if not shared:
        return {}
    for tag, triples in kept.items():
        gmsh.model.mesh.removeElements(2, tag)
        gmsh.model.mesh.addElementsByType(
            tag, 2, [], [node for triple in triples for node in triple])
    made = {}
    for (first, second), triples in sorted(shared.items()):
        tag = gmsh.model.addDiscreteEntity(2)
        gmsh.model.mesh.addElementsByType(
            tag, 2, [], [node for triple in triples for node in triple])
        made[int(tag)] = (int(first), int(second))
    gmsh.model.mesh.reclassifyNodes()
    return made


def shared_edge_census(triangles: dict, shared_tags) -> tuple[int, int]:
    """``(open edges, non-manifold edges)`` with shared faces read as such.

    DP-2876. A face two bodies share is one surface after
    :func:`separate_shared_faces`, so where it meets a wall of each body an
    edge is used three times: once by each wall and once by the face. That is
    every body closed on its own, which is what the reparametrising
    classification needs, and it is not counted. An edge used more often than
    two plus its shared-face uses is the pinch it always was.
    """
    shared_tags = {int(tag) for tag in shared_tags}
    uses: dict = {}
    by_shared: dict = {}
    for tag, triples in triangles.items():
        for triple in triples:
            for index in range(3):
                one, other = triple[index], triple[(index + 1) % 3]
                edge = (min(one, other), max(one, other))
                uses[edge] = uses.get(edge, 0) + 1
                if int(tag) in shared_tags:
                    by_shared[edge] = by_shared.get(edge, 0) + 1
    opened = sum(1 for used in uses.values() if used == 1)
    pinched = sum(1 for edge, used in uses.items()
                  if used > 2 + by_shared.get(edge, 0))
    return opened, pinched







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
    # Plan 41 GN2. MEASURED in the log of a real run: 3 and 7 are logged as
    # `Initial Mesh Only` and `Bamg`.
    3: 'Initial Mesh Only',
    7: 'Bamg',
    11: 'Quasi-structured Quad',
}
#: Plan 31 FC-B. ``Mesh.MaxRetries`` when algorithm fallback is on. Gmsh's own
#: default; named here so the runner and the register cannot disagree.
#: Plan 48 DP-2952: only for a job written before the page had the number.
ALGORITHM_RETRIES = 10


def option_code(value):
    """A coded Gmsh choice as a number: the job's int, or the True / False a
    job written before Plan 48 DP-2952 holds (1 / 0)."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(bool(value))
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
#: Plan 48 D4 (user, 2026-10-21): open to Gmsh's own 0-3; the ledger records
#: what Gmsh applied, with no warning text.
TESTED_SUBDIVISIONS = frozenset({0, 1, 2, 3})
#: Plan 31 FC-C. ``Mesh.SubdivisionAlgorithm`` 3, barycentric. Also reaches
#: Gmsh through ``gmsh/globalSizing/barycentricRefinement``, where what it
#: MEASURED as -- four tetrahedra per tetrahedron, the boundary untouched -- is
#: what it is offered as.
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
QUALITY_QUERY_NAME = {'sicn': 'minSICN', 'sige': 'minSIGE', 'gamma': 'gamma',
                      'disto': 'disto'}
#: Plan 41 GN14 (F5a). ``getElementQualities`` has no name for ``disto``, but
#: Gmsh's own distortion measure is the ratio of an element's smallest to its
#: largest Jacobian determinant, and both of those it does name. ``disto`` is
#: therefore measured from ``minDetJac`` and ``maxDetJac`` (see
#: :func:`element_qualities`) and nothing stands in for it. A measure that is
#: in neither table is still judged against this, recorded rather than silent.
QUALITY_SUBSTITUTE = 'sicn'


#: DP-5718. Gmsh 4.15.2 defines ``gamma`` (inscribed over circumscribed
#: radius) on simplices and prisms only: every hexahedron and pyramid reads
#: exactly 0. MEASURED on a one-cell-thick slab with in-plane layers: 704
#: hexahedra, gamma 0 on all of them, minSJ 0.058..1, minSICN 0.010..0.31,
#: minDetJac > 0 on all -- none inverted, and checkMesh "Mesh OK"; the gate
#: had refused the mesh as 264 inverted cells.
GAMMA_UNDEFINED = ('Hexahedron', 'Pyramid', 'Quadrilateral')


def gamma_or_sicn(mesh, tags):
    """``gamma`` per element, with SICN for the cells it is not defined on.

    Only a value of exactly 0 is looked at again: an element whose type has
    no gamma is judged by its SICN, which is signed (an inverted hexahedron
    still reads at or below 0, so the gate's inverted count keeps its
    meaning)."""
    values = list(mesh.getElementQualities(tags, 'gamma'))
    zero = [index for index, value in enumerate(values) if value == 0.0]
    if not zero:
        return values
    names = {}
    redo = []
    for index in zero:
        etype = mesh.getElement(int(tags[index]))[0]
        if etype not in names:
            names[etype] = mesh.getElementProperties(etype)[0]
        if names[etype].startswith(GAMMA_UNDEFINED):
            redo.append(index)
    if redo:
        sicn = mesh.getElementQualities([tags[index] for index in redo],
                                        'minSICN')
        for index, value in zip(redo, sicn):
            values[index] = value
    return values


def element_qualities(mesh, tags, native):
    """``mesh.getElementQualities`` plus the one measure it has no name for.

    ``disto`` is min / max of the Jacobian determinant over the element, at
    most 1; an element whose determinant changes sign or is zero reads zero or
    below, so the gate's inverted count still means what it says.
    """
    if native == 'gamma':
        return gamma_or_sicn(mesh, tags)
    if native != 'disto':
        return mesh.getElementQualities(tags, native)
    low = mesh.getElementQualities(tags, 'minDetJac')
    high = mesh.getElementQualities(tags, 'maxDetJac')
    values = []
    for small, large in zip(low, high):
        if large > 0:
            values.append(min(1.0, small / large))
        else:
            values.append(0.0 if large == 0 else -1.0)
    return values
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


class GmshRun(StageRun):
    def __init__(self, job: dict, reporter: Reporter):
        self.init_stage_state(job, reporter)

    # -- option helpers ---------------------------------------------------- #


    def apply_option_overrides(self, *, geometry=False):
        """The options the user set from the option catalogue, applied last.

        ``geometry=True`` applies the ``Geometry.*`` rows only, and is called
        once before ``importShapes``: they configure the importer, so a value
        set after the import changes nothing. The other call applies every
        other row, after every page control.

        Plan 41 GN2 (G3). Every page control has already been written, so a
        catalogue value wins -- a value the user typed is never overwritten --
        and the ledger records each one, requested against what Gmsh reads
        back. A name this build does not know is refused by the derivation
        before the run; one that slips through is recorded as not applied.
        """
        for row in self.intent.get('options') or ():
            name = row.get('name')
            if str(name).startswith('Geometry.') != bool(geometry):
                continue
            storage = row.get('storage', 'number')
            value = row.get('value')
            control = f'gmsh/options/{name}'
            try:
                if storage == 'string':
                    effective = self.set_string(name, value, control=control)
                elif storage == 'color':
                    self.gmsh.option.setColor(name, *[int(v) for v in value])
                    effective = list(self.gmsh.option.getColor(name))
                    self.ledger.record(control, list(value), effective)
                else:
                    self.set_number(name, value, control=control)
            except Exception as error:                   # noqa: BLE001
                self.warnings.append(
                    f'option {name} could not be set to {value!r}: {error}')
                self.ledger.record(control, value, None, applied=False,
                                   note=str(error))


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
        # Plan 48 DP-2952: each stage takes its own count when one is set
        # (curveThreads, surfaceThreads, volumeThreads) and Threads when it is
        # automatic (0, or absent from a job written before).
        asked_volume = int(parallel.get('volumeThreads') or 0) or threads
        volume_threads = int(parallel.get('effectiveVolumeThreads',
                                          asked_volume) or asked_volume)
        gmsh.option.setNumber('Mesh.MaxNumThreads3D', float(volume_threads))
        self.ledger.record(
            'gmsh/parallel/threads.3d', volume_threads,
            gmsh.option.getNumber('Mesh.MaxNumThreads3D'),
            note='' if volume_threads == asked_volume else (
                f'{asked_volume} requested; the chosen volume algorithm is '
                'single-threaded, so the request applies to surface meshing '
                'only'))
        self.set_number('Mesh.MaxNumThreads2D',
                        int(parallel.get('surfaceThreads') or 0) or threads,
                        control='gmsh/parallel/threads.2d')
        if parallel.get('reproducible'):
            # Plan 48 ST (DP-4605). The curve stage is left at Gmsh's own
            # setting otherwise; a reproducible run puts it on one thread
            # too, so no stage of the run is threaded.
            self.set_number('Mesh.MaxNumThreads1D', 1,
                            control='gmsh/parallel/threads.1d')
        elif int(parallel.get('curveThreads') or 0):
            self.set_number('Mesh.MaxNumThreads1D',
                            int(parallel['curveThreads']),
                            control='gmsh/parallel/threads.1d')
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
        ('Geometry.OCCBooleanSimplify', 'unionUnify', True),
        ('Geometry.OCCImportLabels', 'importLabels', True),
        ('Geometry.OCCParallel', 'occParallel', False),
    )

    def set_occ_import_options(self, healing, tessellated):
        """Configure the CAD importer, or say it is not being used."""
        skipped = []
        for option, key, default in self.OCC_IMPORT_OPTIONS:
            requested = healing.get(key, default)
            # Plan 48 DP-2952: OCCImportLabels takes Gmsh's 0, 1 or 2; a job
            # written before holds True / False (1 / 0).
            requested = (option_code(requested) if key == 'importLabels'
                         else bool(requested))
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
        # DP-3034. The unit is a visible setting: the page's value when the
        # job carries one, and metres (the unit every FoamMesh case is in)
        # otherwise. The ledger row below names it.
        target_unit = str((self.intent.get('geometry') or {}).get(
            'targetUnit') or 'M')
        self.set_string('Geometry.OCCTargetUnit', target_unit,
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
        # Plan 41 GN2. The importer options the user chose in the catalogue
        # win over the ones just written, and must precede importShapes.
        self.apply_option_overrides(geometry=True)

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
            self.import_cad_sources(sources, healing)
        self.settle_import(healing)

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
        'for item in sys.argv[8:]:\n'
        '    gmsh.merge(item)\n'
        '    gmsh.model.mesh.removeDuplicateNodes()\n'
        # DP-2876. The run makes a face two bodies share one surface before
        # it classifies, so the probe does too, with the run's own code.
        'if int(sys.argv[6]):\n'
        '    sys.path.insert(0, sys.argv[5])\n'
        '    from runner_v1 import separate_shared_faces\n'
        '    separate_shared_faces(gmsh)\n'
        # DP-6064. And imprints a role sheet's rim on the walls, as the run
        # does, so what is probed is the triangulation that is classified.
        'sheets = json.loads(sys.argv[7])\n'
        'if sheets.get("tags"):\n'
        '    sys.path.insert(0, sys.argv[5])\n'
        '    from sheet_imprint import imprint_model\n'
        '    imprint_model(gmsh, sheets["tags"], sheets["tolerance"])\n'
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
                repr(float(self.gmsh.option.getNumber('Geometry.Tolerance'))),
                # DP-2876: where the run's own code is, and whether to use it.
                str(Path(__file__).resolve().parent),
                '1' if self.shared_faces else '0',
                # DP-6064: the sheets the run imprints, and at what tolerance.
                json.dumps({'tags': sorted(int(tag) for tag
                                           in self.role_sheet_imports),
                            'tolerance': float(self.role_sheet_tolerance)})]
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
        # DP-2876. A face two bodies share becomes one surface of its own,
        # unless a non-conformal pair asks for the bodies to be meshed apart,
        # where its two copies are the two patches that pair couples.
        if self.non_conformal_pairs():
            self.shared_faces = {}
            self.interface_sheets = {}
        else:
            self.shared_faces = separate_shared_faces(gmsh)
            # DP-6056. The other form of the same face: drawn once, in a file
            # of its own, as the Split tab hands an assembly over.
            self.interface_sheets = find_interface_sheets(
                gmsh, self.shared_faces, source_of_import)
        if self.interface_sheets:
            self.ledger.record(
                'gmsh/healing/interfaceSheets', True, True,
                note=f'{count_text(len(self.interface_sheets), "imported face")} '
                     'between two bodies read as the surface they share, so '
                     'each body closes on it and the interface meshes once')
        if self.shared_faces:
            moved = sum(len(self._surface_triangles(tag))
                        for tag in self.shared_faces)
            self.ledger.record(
                'gmsh/healing/sharedFaces', True, True,
                note=f'{count_text(moved, "triangle")} drawn by two bodies '
                     'made one surface between them, so each body classifies '
                     'closed and the interface meshes once')
        # DP-6064. An internal face or a baffle drawn as an open sheet is
        # kept out of the shells, and its rim imprinted on the walls it
        # touches, before anything is classified.
        self.set_aside_role_sheets(
            set(self.shared_faces) | set(self.interface_sheets))
        owned_before = {tag: self._surface_triangles(tag)
                        for tag in list(self.import_surface_tags)
                        + sorted(self.shared_faces)}

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
        # DP-6085. A retry after a surface pass Gmsh left invalid cuts every
        # patch into pieces of at most this many triangles before each is
        # parametrised; see :func:`execute_with_surface_fallback`.
        patch_limit = int(healing.get('reparamMaxTriangles') or 0)
        if patch_limit > 0 and settled.reparametrize:
            self.set_number('Mesh.ReparamMaxTriangles', patch_limit,
                            control='gmsh/healing/reparamMaxTriangles')
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
        # DP-6085. How many imported triangles each patch was built from,
        # which sizes the pieces a retry cuts a patch into.
        self.patch_triangles = {int(tag): len(owned)
                                for tag, owned in owned_after.items()}
        self.surface_origin = regroup_classified(owned_before, owned_after)
        # DP-2876. A piece of a shared face is traced to the body whose
        # winding it kept, and belongs to the other body as well: both of
        # their identities follow it, as both names follow a face the CAD
        # route's fusion made one (DP-410).
        self.shared_pieces = {}
        sheet_body = {}
        for piece, origin in list(self.surface_origin.items()):
            owners = self.shared_faces.get(origin)
            if owners:
                self.surface_origin[piece] = owners[0]
                self.shared_pieces[int(piece)] = owners[1]
            # DP-6056. A piece of an imported face keeps the face's own
            # identity; it closes the first body as itself and the second
            # reversed, as a piece of a shared face does.
            owners = self.interface_sheets.get(origin)
            if owners:
                self.shared_pieces[int(piece)] = owners[1]
                sheet_body[int(piece)] = owners[0]
        successors = _successors_of(self.surface_origin)
        for piece, second in sorted(self.shared_pieces.items()):
            if piece not in sheet_body:
                successors.setdefault(second, []).append(piece)
        # C31-04. Classification is a topology change: it cuts each imported
        # solid into pieces and numbers them afresh. Following the identities
        # through it is what keeps a scope on the second STL on the second
        # STL's pieces.
        self.retarget_entities('classification', successors)
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
            tag: source_of_import.get(
                sheet_body.get(tag, self.surface_origin.get(tag)), sole)
            for tag in surfaces}
        triangles = {tag: self._surface_triangles_ordered(tag)
                     for tag in surfaces}
        # DP-2876. The shell topology reads every body as the closed surface
        # it is, so a piece of a shared face is handed to it twice: as itself
        # for the body whose winding it kept, and as its own reverse, under
        # the negated tag, for the other. That is the pair of copies a shared
        # face used to arrive as (DP-456), so nothing about how shells are
        # found changes; the volumes are then built on the one surface.
        # DP-6064. The pieces of a sheet set aside bound nothing: they are
        # embedded once the volumes exist.
        sheet_pieces = sorted(
            tag for tag in surfaces
            if self.surface_origin.get(tag) in self.role_sheet_imports)
        topology_surfaces = [tag for tag in surfaces
                             if tag not in set(sheet_pieces)]
        for piece, second in sorted(self.shared_pieces.items()):
            topology_surfaces.append(-piece)
            triangles[-piece] = [(c, b, a) for a, b, c in triangles[piece]]
            surface_source[-piece] = source_of_import.get(second, sole)
        seed = self.job.get('seedPoint')
        roles = self.job.get('shellRoles') or {}
        coordinates = self._node_coordinates()
        try:
            topology = resolve_topology(
                topology_surfaces, triangles, coordinates,
                sources=surface_source, roles=roles, seed=seed)
        except ShellTopologyError as error:
            raise MeshFailure(str(error)) from error

        def real(group):
            return [abs(int(tag)) for tag in group]

        for plan in topology.volumes:
            loops = [gmsh.model.geo.addSurfaceLoop(real(plan.outer))]
            # Every directly enclosed shell is a hole in this one, whatever it
            # meshes as itself, so two volumes never claim the same region.
            loops.extend(gmsh.model.geo.addSurfaceLoop(real(void))
                         for void in plan.voids)
            # DP-55. A hole's boundary faces the fluid from the other side,
            # and the boundary layer has to know that before it is extruded.
            for void in plan.voids:
                self.void_surfaces.update(real(void))
            topology.shell(plan.name).volume_tag = int(
                gmsh.model.geo.addVolume(loops))
        for shell in topology.shells:
            winding = 1 if shell.volume >= 0.0 else -1
            for tag in shell.surfaces:
                if int(tag) < 0:
                    # The reverse reading of a shared piece: the piece
                    # itself keeps the winding and shell it was traced to.
                    continue
                self.surface_winding[int(tag)] = winding
                self.surface_shell[int(tag)] = str(shell.name)
        gmsh.model.geo.synchronize()
        self.embed_tessellated_sheets(topology, triangles, coordinates,
                                      sheet_pieces)

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
        degenerate = 0
        triangles = {}
        # DP-2876. The faces two bodies share were moved into surfaces of
        # their own before this count, and are read as shared faces.
        for tag in list(self.import_surface_tags) + sorted(self.shared_faces):
            if int(tag) in self.role_sheet_imports:
                # DP-6064. A sheet set aside is open by design, and meets the
                # walls along edges a third triangle uses on purpose.
                continue
            kept = triangles.setdefault(int(tag), [])
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
                    kept.append((first, second, third))
        opened, shared = shared_edge_census(
            triangles, set(self.shared_faces) | set(self.interface_sheets))
        return degenerate, opened, shared

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
        # Plan 48 DP-2952: Gmsh's 0, 1, 2, -2 or -3; True / False (1 / 0) in
        # a job written before.
        self.set_number('Mesh.MeshSizeExtendFromBoundary',
                        option_code(sizing.get('extendFromBoundary', True)),
                        control='gmsh/globalSizing/extendFromBoundary')
        # Plan 31 FC-B. The curve-discretisation floors. These decide whether a
        # small hole gets three elements or twelve, whatever the target size
        # says, and nothing but Mesh.MeshSizeFromCurvature used to reach the
        # 1D pass at all. Every default is Gmsh 4.15.2's own.
        self.set_number('Mesh.MinCircleNodes',
                        int(sizing.get('minimumCirclePoints', 7) or 7),
                        control='gmsh/globalSizing/minimumCirclePoints')
        self.set_number('Mesh.MinCurveNodes',
                        int(sizing.get('minimumCurvePoints', 3) or 3),
                        control='gmsh/globalSizing/minimumCurvePoints')
        # Plan 47 DP-2868, Plan 48 DP-3035. Mesh.MinimumElementsPerTwoPi is
        # Gmsh's older name for Mesh.MeshSizeFromCurvature. There is one
        # control (fromCurvature); the old one is never written.
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
        # Plan 48 DP-2952. Mesh.MaxRetries is its own number on the page; a
        # job written before it holds none and gets what the box gave.
        retries = algorithms.get('maxRetries')
        if retries is None:
            retries = ALGORITHM_RETRIES if fallback else 0
        self.set_number('Mesh.MaxRetries', int(retries),
                        control='gmsh/algorithms/maxRetries')
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
                'not one Gmsh has (0-3), so the run meshed tetrahedra. '
                'Choose a hexahedral cell shape for hexahedra')
            self.ledger.record('gmsh/algorithms/cellShape', subdivision, 0,
                               applied=False,
                               note='Gmsh subdivision algorithms are 0-3')
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
        if netgen and optimize and self.embeds_role_sheets():
            # Plan 49 SR5. MEASURED (Gmsh 4.15.2, WSL): the Netgen optimiser
            # segfaults on a volume with an embedded sheet -- a 1 m box with
            # a 0.6 m sheet inside, plain gmsh, `OptimizeNetgen 1` or one
            # `optimize('Netgen')`, both killed the process. Gmsh's own
            # optimiser still runs.
            self.gmsh.option.setNumber('Mesh.OptimizeNetgen', 0.0)
            self.ledger.record(
                'gmsh/optimization/netgen', 1, 0, applied=False,
                note=self.NETGEN_EMBEDDED_NOTE)
            self.warnings.append(self.NETGEN_EMBEDDED_NOTE)
        elif netgen and not optimize:
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
        # Plan 48 DP-2953. When the job carries a value (quality or
        # algorithms) it is set, and the ledger records it against what Gmsh
        # reads back; otherwise Gmsh's own default is recorded as before.
        random_factor = quality.get('randomFactor')
        if random_factor is None:
            random_factor = (self.intent.get('algorithms') or {}).get(
                'randomFactor')
        if random_factor is not None:
            self.set_number('Mesh.RandomFactor', float(random_factor),
                            control='gmsh/default/randomFactor')
        else:
            self.ledger.record(
                'gmsh/default/randomFactor', None,
                self.gmsh.option.getNumber('Mesh.RandomFactor'),
                note='Gmsh default (Mesh.RandomFactor), not set by FoamMesh')

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

    def apply_point_sizes(self):
        """Plan 41 GN7 (M-01). Size at the points the user picked.

        Independent of "From points" on Global sizing: Gmsh reads point sizes
        only while ``Mesh.MeshSizeFromPoints`` is on, and a row here is an
        explicit request, so the option is switched on and the ledger shows the
        value asked for, the value read back, and that it was switched. The
        size set is read back from Gmsh (``mesh.getSizes``).
        """
        rows = self.intent.get('pointSizes') or []
        if not [r for r in rows if r.get('enabled', True)]:
            return
        result = point_sizes_v1.apply(self.gmsh, rows)
        for item in result['applied']:
            self.ledger.record(
                f'pointSize:{item["name"]}', item['requested'],
                item['effective'], note=(item['note'] or f'point {item["tag"]}'))
        for item in result['skipped']:
            self.warnings.append(
                f'point size {item["name"]!r} was skipped: {item["reason"]}')
            self.ledger.record(f'pointSize:{item["name"]}', None, None,
                               applied=False, note=item['reason'])
        if result['filled']:
            self.ledger.record(
                'gmsh/pointSizes/otherPoints', result['fillSize'],
                result['fillSize'],
                note=f'{count_text(len(result["filled"]), "other point")} '
                     'without a size took the global maximum size; Gmsh '
                     'blends a sized point with an unsized neighbour as if '
                     'that were unlimited, which left the picked point '
                     'coarser than the rest')
        if result['applied']:
            was, now = point_sizes_v1.switch_on_point_sizes(self.gmsh)
            note = ('Gmsh reads point sizes only with this on; '
                    + ('it was off and is on for these rows'
                       if not was else 'it was already on'))
            self.ledger.record('gmsh/pointSizes/fromPoints', True, bool(now),
                               note=note)
        self.statistics['pointSizes'] = {
            'applied': len(result['applied']),
            'skipped': len(result['skipped'])}

    def apply_entity_controls(self):
        """Plan 41 GN9 (M-28..M-32, A-17). Embedded entities and the per-entity
        meshing constraints, set before anything is meshed.

        The tags are those of the geometry as it was imported; an entity the
        model lacks is a failure that names the row. Where Gmsh has a getter
        the ledger carries what it read back (``getEmbedded``, the compound
        options); the rest are recorded as set.
        """
        ops = self.intent.get('entityControls') or []
        if not [o for o in ops if o.get('enabled', True)]:
            return
        try:
            result = entity_controls_v1.apply(self.gmsh, ops)
        except entity_controls_v1.BuildError as error:
            raise MeshFailure(str(error)) from error
        for name, option, requested, effective in result['applied']:
            self.ledger.record(f'entityControl:{name}/{option}', requested,
                               effective)
        self.statistics['entityControls'] = {'applied': result['count']}

    def apply_structure(self):
        """Plan 41 GN8 (M-24..M-26). Transfinite surfaces and volumes with
        their corners and arrangement, and automatic transfinite, set before
        anything is meshed.

        Gmsh has no getter for these, so the ledger says they were set and
        :meth:`measure_structure_rows` proves them on the mesh afterwards. A
        curve count a curve control already set is not overwritten by a row's
        ``curve_elements``.
        """
        ops = self.intent.get('structure') or []
        if not [o for o in ops if o.get('enabled', True)]:
            return
        try:
            result = structure_v1.apply(
                self.gmsh, ops, skip_curves=list(self.transfinite_curves))
        except structure_v1.BuildError as error:
            raise MeshFailure(str(error)) from error
        for name, option, requested, effective in result['applied']:
            self.ledger.record(f'structure:{name}/{option}', requested,
                               effective)
        self.statistics['structure'] = {'applied': result['count']}

    def measure_structure_rows(self):
        """Plan 41 GN8. What Gmsh made of each structure row, off the mesh."""
        ops = self.intent.get('structure') or []
        if not [o for o in ops if o.get('enabled', True)]:
            return
        measured = structure_v1.measure(self.gmsh, ops)
        unstructured = []
        for item in measured:
            self.ledger.record(
                f'structure:{item["name"]}/{item["kind"]}.produced/'
                f'{item["dim"]}:{item["tag"]}',
                'structured', item['structured'],
                applied=item['structured'] is not False,
                note=item['note'])
            if item['structured'] is False:
                unstructured.append(f'{("surface", "volume")[item["dim"] - 2]}'
                                    f' {item["tag"]} ({item["name"]})')
        if unstructured:
            self.warnings.append(
                'a transfinite constraint was set and the mesh did not come '
                'out structured: ' + ', '.join(unstructured)
                + '; Gmsh does not refuse such a request, so check the '
                  'element counts on the bounding curves')
        self.statistics.setdefault('structure', {})['measured'] = measured

    def apply_native_fields(self):
        """Plan 41 GN6. Build the fields made from any Gmsh field type.

        The nodes arrive validated and name each other; they are built in
        dependency order, every option is read back into the ledger, and the
        ones marked background join the background (isotropic sizes with the
        other size fields, metrics on their own).
        """
        nodes = self.intent.get('nativeFields') or []
        if not nodes:
            return
        try:
            built = native_fields_v1.build(self.gmsh, nodes)
        except native_fields_v1.BuildError as error:
            raise MeshFailure(str(error)) from error
        for name, option, requested, effective in built['applied']:
            self.ledger.record(f'nativeField:{name}/{option}', requested,
                               effective)
        anisotropic = set(built['anisotropic'])
        for tag in built['background']:
            if tag in anisotropic:
                self.anisotropic_fields.append(tag)
            else:
                self.background_fields.append(tag)
        field = self.gmsh.model.mesh.field
        for tag in built['layer']:
            field.setAsBoundaryLayer(tag)
        self.statistics['nativeFields'] = {
            'created': len(built['tags']),
            'background': len(built['background']),
            'layer': len(built['layer'])}

    def apply_automatic_field(self):
        """Plan 41 GN25f. The saved automatic size field as a background field.

        ``automaticField`` is in the job only when the project switched
        ``sizeField/useAsBackground`` on. The field joins the user's own
        fields in the one background (combined as they chose), and the three
        options that would outvote it are held at 0 -- recorded, not hidden:
        the ledger says what each was and that it was held.
        """
        block = self.intent.get('automaticField') or {}
        self.automatic_field = None
        if not block:
            return
        consumer = sizefield_consumer_v1
        try:
            built = consumer.build(self.gmsh, str(block['directory']),
                                   octree=bool(block.get('octree')))
        except consumer.FieldError as error:
            raise MeshFailure(
                f'the automatic size field cannot be used: {error}') from error
        held = consumer.hold(self.gmsh)
        self.automatic_field = {'built': built, 'held': held}
        # Plan 47 DP-2867. A size limit inside the field's range clamps it.
        # The limit is the user's (or the field's own proposal, which is
        # chosen not to clamp), so it is kept and the ledger says so.
        for option, bound, clamps in (
                ('Mesh.MeshSizeMax', built['sizeMax'],
                 lambda limit, size: 0 < limit < size),
                ('Mesh.MeshSizeMin', built['sizeMin'],
                 lambda limit, size: limit > size)):
            limit = self.gmsh.option.getNumber(option)
            if clamps(limit, bound):
                self.ledger.record(
                    f'gmsh/sizeField/clamp/{option}', limit, limit,
                    note=f'{option} {limit:g} m clamps the automatic field, '
                         f'which reaches {bound:g} m')
        self.background_fields.append(built['field'])
        self.ledger.record(
            'gmsh/sizeField/useAsBackground', True, True,
            note=f"{built['hexes']} cells, sizes {built['sizeMin']:g} to "
                 f"{built['sizeMax']:g} m, key {str(block.get('key'))[:12]}")
        for name, was in held.items():
            self.ledger.record(
                f'gmsh/sizeField/held/{name}', was, 0.0,
                note='held at 0 while the automatic size field is the '
                     'background; it would outvote the field')
        self.statistics['automaticField'] = {
            'nodes': built['nodes'], 'cells': built['hexes'],
            'sizeMin': built['sizeMin'], 'sizeMax': built['sizeMax'],
            'held': {name: 0.0 for name in held},
            'was': dict(held)}

    def assemble_background_field(self):
        """Combine every size source into one background field.

        Gmsh keeps a single background mesh field, so size fields and
        per-volume sizes have to be merged here; setting one after the other
        would silently discard whichever went first.
        """
        if self.anisotropic_fields:
            # The derivation refuses a metric beside isotropic sizes.
            chosen = native_fields_v1.set_background(
                self.gmsh, self.anisotropic_fields, anisotropic=True)
            self.ledger.record('gmsh/nativeFields/background', 'metric',
                               'metric', note=f'field {chosen}')
            return
        if not self.background_fields:
            return
        if getattr(self, 'automatic_field', None):
            # A later step (point sizes) may have switched one back on.
            sizefield_consumer_v1.hold(self.gmsh,
                                       self.automatic_field['held'])
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
        if getattr(self, 'automatic_field', None):
            outvoting = []      # held at 0 by apply_automatic_field
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
        # Plan 41 GN10. A patch with a deeper stack of its own is the one the
        # fold reading has to hold the measured thickness against.
        for row in (self.intent.get('layers') or {}).get('stacks') or ():
            if row.get('cumulativeHeights'):
                asked = max(asked, abs(float(row['cumulativeHeights'][-1])))
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
        if getattr(self, 'layers_in_plane', None):
            return self.in_plane_layer_cells()          # DP-5718
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
        values = element_qualities(
            gmsh.model.mesh, all_tags, QUALITY_QUERY_NAME[measure])

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
        values = list(element_qualities(
            gmsh.model.mesh, tags, QUALITY_QUERY_NAME[measure]))
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
            return float(min(element_qualities(
                self.gmsh.model.mesh, tags, measure)))
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
        method = str(export.get('renumberMethod') or 'RCMK')
        before = self.node_bandwidth()
        try:
            old_tags, new_tags = self.gmsh.model.mesh.computeRenumbering(method)
            self.gmsh.model.mesh.renumberNodes(old_tags, new_tags)
        except Exception as error:                           # noqa: BLE001
            self.warnings.append(f'renumbering did not run: {error}')
            self.ledger.record('gmsh/output/renumber', True, False,
                               applied=False,
                               note=f'{method} renumbering failed: {error}')
            return
        after = self.node_bandwidth()
        self.statistics['renumbering'] = {
            'method': method, 'bandwidthBefore': before,
            'bandwidthAfter': after,
        }
        moved = ''
        if before is not None and after is not None:
            moved = f'; node bandwidth {before} -> {after}'
        self.statistics['renumbering']['carriedBy'] = carriers
        self.ledger.record(
            'gmsh/output/renumber', True, True,
            note=f'{method}, by computeRenumbering, carried by '
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
        if self.nonconformal_unfused:
            # Plan 42 SG22 Q2. The two copies of a non-conformal pair's
            # contact face are meshed alike (same shape, same size), so
            # welding their coincident nodes would glue the two bodies into
            # one conformal mesh and leave no patch for
            # createNonConformalCouples to couple. MEASURED on
            # jacketed_pipe: the weld turned the interface into 918
            # internal faces and both patches vanished from the publication.
            self.ledger.record(
                'gmsh/healing/removeDuplicateNodes', True, False,
                applied=False,
                note='not applied: a non-conformal pair keeps its two bodies '
                     'apart, so the nodes on their contact are not welded')
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
        It has nothing to do either way -- what it refuses is a Medit
        `.mesh` write (retired, DP-1437), and this path is named by
        :attr:`RunLayout.surface` rather than by the user, so it is always
        `.msh`.
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

    def mesh_pass(self, dimension):
        """One call of the mesher, with the Plan 48 stage hooks around it."""
        run_stage_hooks('before_mesh_pass', self, dimension=dimension)
        self.gmsh.model.mesh.generate(dimension)
        if dimension >= 2:
            self.refuse_empty_bipartite_recombination()
        run_stage_hooks('after_mesh_pass', self, dimension=dimension)

    def log_lines(self) -> list:
        """Every line Gmsh has logged in this run, or ``[]`` unread."""
        if not getattr(self, 'logging', False):
            return []
        try:
            return list(self.gmsh.logger.get())
        except Exception:                                    # noqa: BLE001
            return []

    def log_mark(self) -> int:
        return len(self.log_lines())

    def refuse_invalid_surfaces(self, mark):
        """Stop before the volume pass if the surface pass left invalid elements.

        DP-6085. On a remesh of imported facets there is somewhere to go --
        smaller patches, then the facets themselves -- so the run raises and
        :func:`execute_with_surface_fallback` takes it there. A CAD surface
        has no such rung, and is only reported: Gmsh's volume pass has meshed
        CAD surfaces with a few invalid elements, and refusing those would
        take away meshes that work.
        """
        found = invalid_surface_counts(self.log_lines()[mark:])
        if not found:
            return
        names = []
        for tag in sorted(found, key=lambda item: -found[item]):
            try:
                name = self.surface_patch_name(tag)[0]
            except Exception:                                # noqa: BLE001
                name = f'surface {tag}'
            if name not in names:
                names.append(name)
        total = sum(found.values())
        counts = getattr(self, 'patch_triangles', {}) or {}
        triangles = max((counts.get(int(tag), 0) for tag in found), default=0)
        message = (
            f'the surface mesh Gmsh made is not valid: '
            f'{count_text(total, "element")} on '
            f'{count_text(len(found), "surface")} ({", ".join(names)}) '
            f'{agreeing(total, "is", "are")} still inverted after Gmsh '
            f'retried {agreeing(len(found), "it", "them")} with MeshAdapt, and '
            f'a volume mesher handed such a surface can run without end')
        self.statistics['invalidSurfaceElements'] = {
            'surfaces': {str(tag): count for tag, count in found.items()},
            'patches': names}
        if self.kept_tessellation or not a_remesh_of_imported_facets(self.job):
            self.warnings.append(message + '. The volume pass was run on it.')
            return
        raise InvalidSurfaceMesh(message + '.', surfaces=found, patches=names,
                                 triangles=triangles,
                                 remeshed=not self.kept_tessellation)

    def report_invalid_volume_surfaces(self, mark):
        """Read what the volume pass's own surface remesh left invalid.

        DP-6085. MEASURED: with patches of at most 480 triangles the checked
        surface pass was clean and the volume pass's remesh of it left 4
        invalid elements, which became 4 cells under the publication floor.
        On a remesh of imported facets that is a failed attempt like the
        other, and the run moves to the next rung; on any other run the
        volume exists and the quality census says whether it can be used.
        """
        found = invalid_surface_counts(self.log_lines()[mark:])
        if not found:
            return
        total = sum(found.values())
        self.statistics['invalidVolumeSurfaceElements'] = {
            str(tag): count for tag, count in found.items()}
        message = (
            f'the volume pass remeshed the imported surfaces and left '
            f'{count_text(total, "element")} on '
            f'{count_text(len(found), "surface")} invalid')
        if self.kept_tessellation or not a_remesh_of_imported_facets(self.job):
            self.warnings.append(
                message + '; the volume mesh was built on it, and its quality '
                'figures are measured on the result.')
            return
        names = []
        for tag in found:
            try:
                name = self.surface_patch_name(tag)[0]
            except Exception:                                # noqa: BLE001
                name = f'surface {tag}'
            if name not in names:
                names.append(name)
        raise InvalidSurfaceMesh(message + f' ({", ".join(names)}).',
                                 surfaces=found, patches=names)

    def refuse_empty_bipartite_recombination(self):
        """Say why a surface pass left no surface element, when it is option 4.

        Plan 48 FIX3 (DP-2964). MEASURED on Gmsh 4.15.2 (``fragments/gm2/
        live-enums.json``): ``Mesh.RecombinationAlgorithm`` 4 (bipartite
        labelling) reads back as 4 and logs "quads created", then leaves no 2D
        element in the model, so the volume pass fails with a message that does
        not name the cause. The value stays allowed (decision D4); the run says
        so here, before the next pass, in words about the setting.
        """
        gmsh = self.gmsh
        if int(gmsh.option.getNumber('Mesh.RecombinationAlgorithm') or 0) != 4:
            return
        if not gmsh.model.getEntities(2):
            return
        types, _tags, _nodes = gmsh.model.mesh.getElements(2)
        if len(types):
            return
        raise MeshFailure(
            'Mesh.RecombinationAlgorithm 4 (bipartite labelling) left no '
            'surface element on this Gmsh (4.15.2): it reports the quadrangles '
            'it made and then removes the surface mesh. Choose simple, blossom, '
            'simple full-quad or blossom full-quad (0-3) instead.')

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
        # DP-6054. A periodic side the layer rebuilt is made to correspond
        # between the passes; see :meth:`copy_periodic_surfaces`.
        copy = bool(dimension > 2 and self.periodic_copies)
        # DP-6085. A remesh of imported facets is never handed to the volume
        # pass with a surface Gmsh itself left invalid: on one it can spin
        # without end instead of failing. Only such a job has a rung to go
        # to (smaller patches, then the facets); any other run keeps its
        # passes and has what the volume pass logs reported.
        guard = bool(dimension > 2 and not self.kept_tessellation
                     and a_remesh_of_imported_facets(self.job))
        try:
          # DP-446. Everything the mesher does is inside this block, which is
          # exactly the span that used to be silent.
          with self.meshing_heartbeat(dimension):
            if split or fit or keep_surface or copy or guard:
                  # Two passes rather than one, because the split has to happen
                  # between them; see :meth:`split_quadrangles`. A section has no
                  # second pass, so for it the split is simply the last thing
                  # done to the surfaces -- the same call in the same place.
                  mark = self.log_mark()
                  self.mesh_pass(2)
                  if guard:
                      self.refuse_invalid_surfaces(mark)
                  if split:
                      self.split_quadrangles()
                  if copy:
                      self.copy_periodic_surfaces()
                  if fit:
                      self.measure_layer_fit()
                  if keep_surface:
                      # After the split, because the split is what the volume
                      # pass will see, and a surface mesh a user is shown that
                      # is not the one the volume was built on would be a
                      # picture of a mesh that never existed.
                      self.write_surface_pass()
                  if dimension > 2:
                      # DP-6085. MEASURED, Gmsh 4.15.2: a model whose
                      # surfaces are all discrete (every STL import) does
                      # not count them as meshed, so `generate(3)` meshes
                      # them again from the same parametrisation, and
                      # Mesh.MeshOnlyEmpty does not stop it. The pass
                      # checked above is therefore a proxy for the one the
                      # volume is built on; what that one logs is read too.
                      mark = self.log_mark()
                      self.mesh_pass(dimension)
                      self.report_invalid_volume_surfaces(mark)
                  if copy:
                      # DP-6066. The strips only once their stacks exist.
                      self.declare_periodic_strips()
            else:
                  mark = self.log_mark()
                  self.mesh_pass(dimension)
                  if dimension > 2:
                      self.report_invalid_volume_surfaces(mark)
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
        if passes and self.embeds_role_sheets():
            # Plan 49 SR5. The same segfault as `Mesh.OptimizeNetgen`.
            self.ledger.record(
                'gmsh/optimization/netgenPasses', passes, 0, applied=False,
                note=self.NETGEN_EMBEDDED_NOTE)
            passes = 0
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
        if getattr(self, 'layers_in_plane', None):
            # DP-5718. A one-cell-thick case grows its layer in the plane.
            self.measure_in_plane_layers()
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
        # Plan 41 GN10. Stacks may differ in cell shape: the sides then come
        # out as both families, and the ledger asks for both.
        asked = {quads} | {bool(row.get('quads', True)) for row in
                           (self.intent.get('layers') or {}).get('stacks')
                           or ()}
        triangles = any(name.startswith('Triangle') for name in lateral)
        agreed = (recombined and triangles if len(asked) == 2
                  else recombined == quads)
        self.ledger.record(
            'gmsh/boundaryLayers/quads', quads,
            ', '.join(sorted(lateral)) or None,
            applied=bool(lateral) and agreed,
            matched=bool(lateral) and agreed,
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
            # Plan 41 GN10. A column holds `elementsPerLayer` cells per
            # layer; the patch is read in layers, against its own request.
            asked = (record.get('requestedByPatch') or {}).get(name) or {}
            split = int(asked.get('elementsPerLayer') or 1)
            by_patch[name] = {
                'selected': name in wanted, 'faces': total,
                'columns': entry['columns'], 'coverage': coverage,
                'layersPerColumn': sorted(
                    {n // split if n % split == 0 else n
                     for n in entry['layers']}),
                'cellsPerColumn': sorted(entry['layers']),
                'requested': asked or None,
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
        first_measured = record['achieved']['firstHeight']
        # The first cell is the first layer divided by its cells per layer.
        first_asked = (record['requestedFirstHeight']
                       / max(1, int(record.get('elementsPerLayer') or 1)))
        asked_by_patch = record.get('requestedByPatch') or {}
        # Plan 41 GN10 (M-34). With stacks of its own on some patches the
        # run-wide reading mixes unlike stacks, so the list's rows are read
        # over the patches that kept the list's stack, and each patch with a
        # stack of its own is read against that stack on rows of its own.
        stacked = any(item.get('stack') for item in asked_by_patch.values())
        if stacked:
            plain = [by_patch[name] for name, item in asked_by_patch.items()
                     if not item.get('stack') and name in by_patch
                     and by_patch[name].get('reach')]
            reach = self.spread([item['reach']['median'] for item in plain
                                 if item.get('reach')]) if plain else None
            first_measured = self.spread(
                [item['firstHeight']['median'] for item in plain
                 if item.get('firstHeight')]) if plain else None
            for name, item in sorted(asked_by_patch.items()):
                if not item.get('stack') or name not in by_patch:
                    continue
                measured = by_patch[name]
                depth = (measured.get('layersPerColumn') or [None])[0]
                cell = (measured.get('firstHeight') or {}).get('median')
                got = (measured.get('reach') or {}).get('median')
                prefix = f'gmsh/boundaryLayers/stacks/{name}'
                split = int(item.get('elementsPerLayer') or 1)
                self.ledger.record(
                    f'{prefix}/layerCount', item['layers'], depth,
                    note=f'layers on {name}, read from its columns '
                         f'({count_text(split, "cell")} each)')
                self.ledger.record(
                    f'{prefix}/firstHeight', item['firstHeight'] / split,
                    cell, matched=bool(cell) and abs(
                        cell - item['firstHeight'] / split)
                    <= 0.02 * item['firstHeight'] / split,
                    note='the first cell of the stack, measured from paired '
                         'prism nodes')
                self.ledger.record(
                    f'{prefix}/totalThickness', item['totalThickness'], got,
                    matched=bool(got) and abs(
                        got - item['totalThickness'])
                    <= 0.1 * item['totalThickness'],
                    note='measured perpendicular to the wall the stack grew '
                         'from')
        if requested and reach and reach['min'] < .5 * requested:
            self.warnings.append(
                f'the boundary layer is pinched: it reaches {reach["min"]:.3g} '
                f'm into the domain where it was asked for {requested:.3g} m, '
                'which happens where walls meet at a sharp angle')
        if reach:
            self.ledger.record(
                'gmsh/boundaryLayers/totalThickness', requested,
                reach['median'],
                note='measured perpendicular to the wall the stack grew '
                     'from; at a junction the shared node moves along the '
                     'bisector, so the stack reaches less far than its nodes '
                     'travelled')
        if first_measured:
            self.ledger.record(
                'gmsh/boundaryLayers/firstHeight', first_asked,
                first_measured['median'],
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
        'two solids are conformal where they share one CAD face or where '
        'duplicate-face fusion merged their copies; a non-conformal pair '
        'keeps its two bodies apart (no fusion) and OpenFOAM '
        'createNonConformalCouples couples them after conversion (SG22); a '
        'translated or rotated cyclic pair is built by setPeriodic (DP-641)')

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
            applied = row['state'] in ('conformal', 'periodic',
                                       'non_conformal')
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
        if pair.get('relationId'):
            # Plan 49 SR5. A pair lowered from a surface relation: its
            # coupling is made after the region split, not before it.
            row['relationId'] = str(pair['relationId'])
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
        if coupling == 'non_conformal' and transform == 'coincident':
            return self._trace_non_conformal_pair(row, master, slave,
                                                  described)
        if coupling == 'non_conformal':
            row['reason'] = (
                f'a non-conformal {transform} coupling is not built on the '
                'Gmsh route (a transformed match is authored on the Periodic '
                f'page); master is {described("master")}, slave is '
                f'{described("slave")}')
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

    def _trace_non_conformal_pair(self, row, master, slave, described) -> dict:
        """Grade a non-conformal pair the job kept apart (Plan 42 SG22 Q2).

        Each side must be exactly one surface bounding one volume: the two
        copies of the contact face, now two boundary patches, which
        createNonConformalCouples takes by name. The pair is ``non_conformal``
        (coupled after the OpenFOAM conversion) for an OpenFOAM target, and
        ``unmatched`` for any other output, which has no such coupling and is
        never reported as coupled.
        """
        if len(master) != 1 or len(slave) != 1 or master == slave:
            row['reason'] = (
                'each side of a non-conformal pair must be exactly one '
                f'boundary surface, but master is {described("master")} and '
                f'slave is {described("slave")}; group the faces of a side '
                'into one named boundary on the Geometry page')
            return row
        sides = (master[0], slave[0])
        if any(self._bounds_two_volumes(tag) for tag in sides):
            row['reason'] = (
                f'master is {described("master")} and slave is '
                f'{described("slave")}; a side that is shared by two volumes '
                'was fused, so the bodies were not kept apart there')
            return row
        row['masterPatch'] = self.surface_patch_name(master[0])[0]
        row['slavePatch'] = self.surface_patch_name(slave[0])[0]
        row['matchedFaces'] = sum(self._surface_face_count(tag)
                                  for tag in sides)
        target = str((self.intent.get('export') or {}).get(
            'targetSolver') or '').lower()
        if target == 'openfoam':
            row['state'] = 'non_conformal'
            row['label'] = 'non-conformal'
            row['reason'] = (
                f'{row["masterPatch"]} and {row["slavePatch"]} were meshed '
                'apart as two boundary patches; OpenFOAM '
                'createNonConformalCouples couples them after conversion')
        else:
            row['state'] = 'unmatched'
            row['label'] = 'unmatched surfaces'
            row['reason'] = (
                f'{row["masterPatch"]} and {row["slavePatch"]} were meshed '
                'apart; only an OpenFOAM target couples them, so for this '
                'output they are unmatched surfaces and no coupling is '
                'claimed')
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
                if not len(slaves) and int(slave_tag) in self.periodic_node_maps:
                    # DP-6054. A side the layer rebuilt was copied by
                    # FoamMesh; when Gmsh holds no node map for it, the
                    # map is read off the mesh by the transform.
                    master, slaves, masters, affine = \
                        self.copied_periodic_nodes(slave_tag)
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

    #: Suffixes of Gmsh's Medit writer. Plan 26 WP10.2; DP-1437 (user,
    #: 2026-10-23): the Medit export is retired from FoamMesh.
    MEDIT_SUFFIXES = ('.mesh', '.meshb')
    #: The sentence every Medit write is refused with (same words as
    #: ``option_catalogue.MEDIT_RETIRED``; the runner imports nothing of
    #: ``foammesh``).
    MEDIT_RETIRED = ("Medit (.mesh) export was retired in 1.1.4: Gmsh "
                     "4.15.2's writer crashes; export .msh, .su2, VTK or "
                     'polyMesh instead.')

    def refuse_unsafe_write(self, path):
        """Refuse a write Gmsh would take the whole process down performing.

        **Gmsh 4.15.2's Medit writer segfaults** on a prism+tet mesh (it drops
        every volume element first and leaves a truncated file with no `End`)
        and on some triangle/quadrangle surface meshes; measured in
        ``plans/evidence/plan26-mmg-prisms/``. A segfault is not an exception,
        so the `except` around the write cannot catch it: containment has to
        happen *before* the call. FoamMesh no longer offers the Medit export
        at all (DP-1437), so every Medit path a job names is refused here,
        whatever the mesh holds -- a job written by an older FoamMesh, or by
        hand, still cannot reach the writer.

        Returns a stated reason, or `''` when the write is safe.
        """
        if str(path).lower().endswith(self.MEDIT_SUFFIXES):
            return self.MEDIT_RETIRED
        return ''

    # -- orchestration ----------------------------------------------------- #

    def execute(self) -> dict:
        self.reporter.emit('started', 'gmsh', 0.0, 'starting Gmsh')
        self.initialise()
        load_runner_parts()
        try:
            run_stage_hooks('before_import', self)
            self.import_geometry()
            run_stage_hooks('after_import', self)
            run_stage_hooks('geometry', self)
            self.apply_sizing()
            self.apply_size_fields()
            self.apply_native_fields()
            self.apply_automatic_field()
            run_stage_hooks('fields', self)
            # Before the curve controls: a three-sided face they make
            # transfinite is filled according to this option.
            self.apply_structuring_options()
            self.apply_curve_controls()
            # After the curve controls: a size the user put on a picked point
            # outranks the end-point size a curve control gave it.
            self.apply_point_sizes()
            self.apply_volume_controls()
            self.apply_entity_controls()
            self.apply_structure()
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
            # Last, so a value the user chose from the option catalogue is
            # the one Gmsh meshes with.
            self.apply_option_overrides()
            self.generate()
            # Plan 49 SR5. After the weld inside generate(), which would
            # merge the two sides back: each baffle split by Crack.
            self.crack_baffles()
            self.record_internal_faces()
            self.measure_algorithms()
            self.measure_families()
            self.measure_structuring()
            self.measure_structure()
            self.measure_structure_rows()
            self.measure_layers()
            self.measure_periodic()
            self.trace_interface_pairs()
            self.measure_volume()
            run_stage_hooks('export', self)
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
    # Plan 41 GN10. The per-patch stacks are cut back by the same factor.
    for row in layers.get('stacks') or ():
        row['cumulativeHeights'] = [
            float(value) * factor for value in row.get('cumulativeHeights')
            or ()]
        for key in ('firstHeight', 'totalThickness'):
            if row.get(key) is not None:
                row[key] = float(row[key]) * factor
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
    except InvalidSurfaceMesh as error:
        unmeshable = error
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
    if isinstance(unmeshable, InvalidSurfaceMesh):
        if not a_remesh_of_imported_facets(job):
            raise unmeshable
        document = execute_with_smaller_patches(
            job, reporter, attempts, unmeshable)
        if document is not None:
            return document
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


#: DP-6085. The piece sizes, in imported triangles, a retry parametrises the
#: patches in, tried in this order. MEASURED on the angle-split tube stack
#: (worst patch 7,680 triangles), each checked on the surface pass and on the
#: volume pass's own remesh of it: 500, 600 and 750 mesh clean (0 inverted,
#: minimum SICN 0.12, 0.05, 0.11); 120, 240, 400, 480, 1,000 and 2,000 all
#: leave invalid elements. Smaller is not better, so this is a short list of
#: sizes that worked, not a sequence that shrinks.
PATCH_LIMITS = (500, 750, 600)


def smaller_patches_job(job, limit):
    """A copy of the job whose patches are parametrised in smaller pieces."""
    document = copy.deepcopy(job)
    intent = document.setdefault('intent', {})
    healing = intent.get('healing')
    if not isinstance(healing, dict):
        healing = {}
        intent['healing'] = healing
    healing['reparamMaxTriangles'] = int(limit)
    return document


def execute_with_smaller_patches(job, reporter, attempts, invalid):
    """Remesh with every patch cut smaller; ``None`` if no size meshes.

    DP-6085. Gmsh parametrises each classified patch as one piece. A long
    tube -- 0.5 m by 75 mm round -- maps to a ring so stretched that the
    surface mesher finds no triangle under its own points ("Triangle not
    found for curvatures", thousands of times) and leaves elements inverted.
    Cut into short pieces, each maps cleanly. Each size in
    :data:`PATCH_LIMITS` is tried until one leaves no invalid element; a job
    that already set the piece size has had this retry, and goes to the
    facets.
    """
    healing = (job.get('intent') or {}).get('healing') or {}
    if healing.get('reparamMaxTriangles'):
        return None
    tried, best = [], None
    for limit in PATCH_LIMITS:
        reporter.emit('progress', 'mesh', 0.3,
                      f'remeshing the surface in patches of at most {limit} '
                      f'triangles: '
                      + ', '.join(invalid.patches or ('a patch',))
                      + ' came out invalid')
        try:
            document = execute_with_layer_fit(
                smaller_patches_job(job, limit), reporter, attempts)
        except LayerCollision:
            raise
        except MeshFailure:
            tried.append((limit, None))
            continue
        # MEASURED on cht-tube: pieces of 500 meshed both passes clean and
        # left one cell of SICN 0.0001 under the 0.008 floor, which the
        # publication refused. A size whose cells fall under the floor is
        # kept only if no other size does better.
        score = patch_retry_score(document)
        if best is None or score < best[0]:
            best = (score, limit, document, list(tried))
        if score == (0, 0):
            break
        tried.append((limit, score))
    if best is None:
        return None
    score, limit, document, before = best
    earlier = ''
    if before:
        earlier = ' Tried first: ' + '; '.join(
            f'pieces of at most {size} ('
            + ('invalid surface elements remained' if result is None else
               f'{count_text(result[0], "inverted cell")}, '
               f'{count_text(result[1], "cell")} under the quality floor')
            + ')' for size, result in before) + '.'
    document.setdefault('warnings', []).insert(0, (
        f'{invalid} The surface was therefore remeshed with every patch '
        f'cut into pieces of at most {limit} imported triangles, each '
        f'parametrised on its own, and neither the surface pass nor the '
        f'volume pass left an invalid element on it.{earlier} The patch '
        f'names are unchanged; the pieces add edges inside the patches '
        f'that the mesh follows.'))
    return document


def patch_retry_score(document) -> tuple:
    """``(inverted, under the floor)`` of a run's mesh; lower is better."""
    quality = ((document or {}).get('statistics') or {}).get(
        'achievedQuality') or {}
    return (int(quality.get('inverted') or 0),
            int(quality.get('below_threshold') or 0))


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


def peak_resources() -> dict:
    """``{'peak_rss_bytes', 'source'}`` of this process, or ``{}`` if unreadable.

    Plan 48 FIX3 (DP-4406). Linux reports the high-water mark of the resident
    set as ``VmHWM`` in ``/proc/self/status`` (kB); ``getrusage`` carries the
    same figure as ``ru_maxrss`` (kB on Linux). The first is read, the second
    is the fallback. Gmsh runs inside this process, so the figure is the
    mesher's. Nothing is guessed: no reading, no entry.
    """
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmHWM:'):
                kilobytes = int(line.split()[1])
                if kilobytes > 0:
                    return {'peak_rss_bytes': kilobytes * 1024,
                            'source': '/proc/self/status VmHWM'}
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        kilobytes = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if kilobytes > 0:
            return {'peak_rss_bytes': kilobytes * 1024,
                    'source': 'getrusage ru_maxrss'}
    except (ImportError, OSError, ValueError):
        pass
    return {}


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

    # Plan 48 FIX3 (DP-4406). The mesher runs here, inside the WSL distro,
    # outside any Windows job object, so nobody else can measure it: this
    # process is the only one that sees its own peak resident set.
    resources = peak_resources()
    if resources:
        document['resources'] = resources
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return status


if __name__ == '__main__':
    sys.exit(main())
