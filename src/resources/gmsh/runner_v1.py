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
import json
import math
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

# Shell classification lives next door because it is pure geometry: no Gmsh,
# so it can be tested without a runtime. Running this file as a script already
# puts its directory on the path; loading it as a module by file does not.
try:
    from shell_topology import ShellTopologyError, resolve_topology
    from field_graph import FieldGraphError, build_graph
except ImportError:  # pragma: no cover - exercised only by the module loader
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from shell_topology import ShellTopologyError, resolve_topology
    from field_graph import FieldGraphError, build_graph

SCHEMA_VERSION = 1
RUNNER_VERSION = 'gmsh-runner-v1'


class ContractFailure(RuntimeError):
    """The job does not satisfy the contract this runner implements."""


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
    for tag, triangles in after.items():
        votes: dict = {}
        for key in triangles:
            source = index.get(key)
            if source is not None:
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

    def emit(self, kind, stage_id, fraction, message='', details=None):
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

    def record(self, control, requested, effective, *, applied=True, note=''):
        matched = None
        if isinstance(requested, (int, float)) and isinstance(effective, (int, float)):
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
        #: C31-04. One record per topology change that moved entities:
        #: ``{'stage', 'entities': {entity_id: {'from': [...], 'to': [...]}}}``.
        #: Published as ``statistics['import']['topologyChanges']``.
        self.topology_changes: list[dict] = []
        #: R118. A surface that grew no boundary layer is deleted and rebuilt
        #: from the extrusion's inner rim, so this maps the original tag onto
        #: the faces that took its place and must inherit its patch name.
        self.layer_replacements: dict[int, list[int]] = {}
        #: Size fields awaiting combination into one background field. Size
        #: fields and per-volume sizes must share it, or whichever was set
        #: last would silently replace the other.
        self.background_fields: list[int] = []
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
        self.set_string('Geometry.OCCTargetUnit', 'M',
                        control='geometry.unit')
        self.set_number('Geometry.Tolerance',
                        healing.get('importTolerance', 1e-6),
                        control='gmsh/healing/importTolerance')
        self.set_number('Geometry.OCCSewFaces',
                        int(bool(healing.get('sewFaces', False))),
                        control='gmsh/healing/sewFaces')
        self.set_number('Geometry.OCCFixDegenerated',
                        int(bool(healing.get('fixDegenerated', False))),
                        control='gmsh/healing/fixDegenerated')
        # Rebuilds a solid from a closed shell. Measured: with sewing on and
        # this off, the import has zero volumes and meshes to a bare surface.
        self.set_number('Geometry.OCCMakeSolids',
                        int(bool(healing.get('makeSolids', False))),
                        control='gmsh/healing/makeSolids')

        # -- Plan 31: the rest of the Geometry.* family ------------------- #
        # Kept as its own block, in one place, so this file stays mergeable
        # with the Mesh.* work happening beside it. Every default below is
        # Gmsh 4.15.2's own, probed 2026-09-06, so setting them explicitly
        # changes nothing for a case that never asked.
        #
        # All of these have to be established before importShapes: they
        # configure the importer, not the model.
        self.set_number('Geometry.OCCFixSmallEdges',
                        int(bool(healing.get('fixSmallEdges', False))),
                        control='gmsh/healing/fixSmallEdges')
        self.set_number('Geometry.OCCFixSmallFaces',
                        int(bool(healing.get('fixSmallFaces', False))),
                        control='gmsh/healing/fixSmallFaces')
        self.set_number('Geometry.OCCAutoFix',
                        int(bool(healing.get('autoFix', True))),
                        control='gmsh/healing/autoFix')
        self.set_number('Geometry.OCCUnionUnify',
                        int(bool(healing.get('unionUnify', True))),
                        control='gmsh/healing/unionUnify')
        self.set_number('Geometry.OCCImportLabels',
                        int(bool(healing.get('importLabels', True))),
                        control='gmsh/healing/importLabels')
        self.set_number('Geometry.OCCScaling',
                        float(healing.get('importScaling', 1.0) or 1.0),
                        control='gmsh/healing/importScaling')
        self.set_number('Geometry.OCCParallel',
                        int(bool(healing.get('occParallel', False))),
                        control='gmsh/healing/occParallel')
        # Read by the farfield cut and by removeAllDuplicates, both of which
        # are booleans, so it is set before either runs.
        self.set_number('Geometry.ToleranceBoolean',
                        float(healing.get('booleanTolerance', 0.0) or 0.0),
                        control='gmsh/healing/booleanTolerance')
        # -- end Geometry.* block ----------------------------------------- #

        self.reporter.emit('progress', 'import', 0.05, 'importing geometry')
        tessellated = [item for item in sources
                       if str(item).lower().endswith(('.stl', '.obj', '.ply'))]
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
                before = len(gmsh.model.getEntities(2))
                gmsh.model.occ.removeAllDuplicates()
                gmsh.model.occ.synchronize()
                after = len(gmsh.model.getEntities(2))
                if after != before:
                    self.ledger.record(
                        'gmsh/healing/removeDuplicateFaces', True, True,
                        note=f'{before - after} duplicate face(s) fused')

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
                    f'the job asked for a {mode} mesh and the import produced '
                    f'{len(volumes)} volume(s). This route meshes a planar '
                    'section and extrudes it into exactly one cell of '
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
            f'imported {len(volumes)} volume(s), {len(surfaces)} surface(s)'
            f'{census["message"]}',
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
        census = {'surfacesByPatch': {name: sorted(owned)
                                      for name, owned in named.items()},
                  'unnamedSurfaces': sorted(unnamed)}
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
        'for item in sys.argv[4:]:\n'
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
                str(self.CLASSIFY_PROBE_BYTES)]
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
                f'-- {detail}')
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

        source_of_import = {}
        for index, item in enumerate(sources):
            before = {tag for _dim, tag in gmsh.model.getEntities(2)}
            gmsh.merge(str(item))
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
                f'this surface is not manifold: {shared} edge(s) are shared '
                'by more than two triangles, so it pinches or doubles back '
                'on itself along them. Gmsh was therefore asked to classify '
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
        gmsh.model.mesh.createGeometry()
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
                f'{len(unplaced)} classified surface(s) could not be traced to '
                'an imported solid and keep a generated name')

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
            topology.shell(plan.name).volume_tag = int(
                gmsh.model.geo.addVolume(loops))
        gmsh.model.geo.synchronize()

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
        }
        self.reporter.emit(
            'progress', 'import', 0.12,
            f'{len(topology.shells)} closed shell(s) -> '
            f'{len(topology.volumes)} volume(s), {voids} void(s)',
            {'shells': topology.to_list()})

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
        entities = []
        if source_id:
            for kind, tags in added.items():
                dimension = 2 if kind == 'surface' else 3
                for local, tag in enumerate(tags):
                    identity = f'{source_id}:{kind}:{local}'
                    self.entity_tags[identity] = {
                        'kind': kind, 'dim': dimension, 'tags': [int(tag)]}
                    entities.append({'entity_id': identity, 'kind': kind,
                                     'dim': dimension, 'tags': [int(tag)]})
        declared = int(record.get('declared_surfaces') or 0)
        found = len(added.get('surface') or ())
        if not source_id:
            status = 'unmapped'
        elif declared and declared != found:
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
            'mapping_confidence': float(record.get('mapping_confidence', 1.0)
                                        if record else 0.0),
            'mapping_status': status,
        }

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
        successors = {tag: [tag] for tags in alive.values() for tag in tags}
        moved = self.retarget_entities(stage, successors,
                                       dimensions=dimensions)
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

    def _entity_counts(self) -> dict:
        """How many entities of each dimension the model holds right now.

        Differencing this across one ``importShapes`` is what turns "the model
        has 30 surfaces" into "this file brought 6 of them".
        """
        return {dimension: len(self.gmsh.model.getEntities(dimension))
                for dimension in (2, 3)}

    def _surface_triangles(self, tag):
        """The triangles of one discrete surface, as sorted node triples.

        Node tags survive classification; element tags do not. A triple is
        therefore the one key that identifies a triangle on both sides.
        """
        triples = set()
        types, _elements, nodes = self.gmsh.model.mesh.getElements(2, tag)
        for element_type, node_tags in zip(types, nodes):
            if int(element_type) != 2:
                continue
            flat = [int(value) for value in node_tags]
            for start in range(0, len(flat) - 2, 3):
                triples.add(tuple(sorted(flat[start:start + 3])))
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
        """Wrap the imported solids in a box and cut them out of it.

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
        """
        farfield = self.intent.get('farfield') or {}
        if not farfield.get('enabled'):
            return False
        gmsh = self.gmsh
        padding = float(farfield.get('padding', 2.0) or 0.0)
        if self.tessellated:
            reason = ('a farfield box is cut out of the imported solids by '
                      'the CAD kernel, and a tessellated import has no solid '
                      'to cut; supply the geometry as STEP, or supply the '
                      'farfield as a surface of its own')
            self.warnings.append(f'no farfield box was built: {reason}')
            self.ledger.record('gmsh/farfield/enabled', True, False,
                               applied=False, note=reason)
            return False

        bodies = gmsh.model.getEntities(3)
        before = gmsh.model.getBoundingBox(-1, -1)
        diagonal = math.dist(before[0:3], before[3:6])
        standoff = padding * diagonal
        origin = [before[index] - standoff for index in range(3)]
        span = [before[index + 3] - before[index] + 2 * standoff
                for index in range(3)]
        signatures = {tag: self.surface_signature(tag)
                      for _dim, tag in gmsh.model.getEntities(2)}

        box = gmsh.model.occ.addBox(*origin, *span)
        try:
            gmsh.model.occ.cut([(3, box)], list(bodies),
                               removeObject=True, removeTool=True)
            gmsh.model.occ.synchronize()
        except Exception as error:
            raise MeshFailure(
                f'the farfield box could not be cut against the geometry: '
                f'{error}') from error
        volumes = gmsh.model.getEntities(3)
        if not volumes:
            raise MeshFailure(
                f'cutting the farfield box against {len(bodies)} solid(s) '
                'left no volume at all; the box was consumed by the cut, '
                'which happens when a solid is larger than the padding '
                'allows for')
        domain, sealed = self.classify_cut_volumes(volumes, origin, span,
                                                   diagonal)
        if not domain:
            raise MeshFailure(
                f'cutting the farfield box against {len(bodies)} solid(s) '
                f'left {len(volumes)} volume(s), none of them bounded by a '
                'side of the generated box, so none of them is the external '
                'domain. A solid reaching outside the padding is the usual '
                'cause; increase the padding or supply the domain as its '
                'own surface.')
        if len(domain) > 1:
            raise MeshFailure(
                f'cutting the farfield box against {len(bodies)} solid(s) '
                f'split the external domain into {len(domain)} separate '
                'volumes, each touching the generated box. Meshing one of '
                'them would silently drop the rest of the flow region, so '
                'the run stops here: the usual cause is a solid that spans '
                'the box, or overlapping and open solids.')
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
        self.name_farfield_faces(box_faces, origin, span, diagonal)

        self.ledger.record('gmsh/farfield/enabled', True, True,
                           note=f'{len(bodies)} solid(s) cut out of a box '
                                f'{padding:g} diagonal(s) larger on each side')
        self.ledger.record('gmsh/farfield/padding', padding, padding,
                           note=f'{standoff:.6g} m on each side')
        self.statistics['farfield'] = {
            'padding': padding,
            'standoff': standoff,
            'boundingBox': list(gmsh.model.getBoundingBox(-1, -1)),
            'bodies': len(bodies),
            'domainVolumes': len(domain),
            'domainIdentifiedBy': 'faces lying on a side of the generated box',
            'sealedCavities': len(sealed),
            'sealedCavityPolicy': sealed_policy,
            'sealedCavityVolumes': [round(mass, 12) for mass, _tag in sealed],
            'volumes': len(volumes),
            'bodyFaces': inherited,
            'boxFaces': len(box_faces),
            'patches': sorted(self.generated_names.values()),
        }
        self.reporter.emit(
            'progress', 'import', 0.12,
            f'built a farfield box around {len(bodies)} solid(s)',
            {'standoff_m': round(standoff, 6)})
        return True

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
                           note=f'{len(sealed)} sealed cavity(ies) found')
        if not sealed:
            return self.gmsh.model.getEntities(3)
        sizes = ', '.join(f'{mass:.3g} m^3' for mass, _tag in sealed)
        if policy == 'refuse':
            raise MeshFailure(
                f'{len(sealed)} sealed cavity(ies) inside the geometry were '
                f'left by the farfield cut ({sizes}), and this case asks to '
                'be told rather than have them decided. External flow does '
                'not reach them. Choose to discard them to mesh the outside '
                'only, or to keep them to mesh them as separate regions.')
        if policy == 'keep':
            self.warnings.append(
                f'{len(sealed)} sealed cavity(ies) inside the geometry were '
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
            f'{len(sealed)} sealed cavity(ies) inside the geometry were '
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
            self._name_entities()
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
        if self.unresolved_scopes:
            self.warnings.append(
                'these scopes matched no imported entity and were skipped: '
                + ', '.join(sorted(set(self.unresolved_scopes))))
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
            for identity, label in table.items():
                record = self.entity_tags.get(str(identity))
                if record is None or record['kind'] != kind or not label:
                    continue
                for tag in record['tags']:
                    target[int(tag)] = str(label)

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
        self.set_number('Mesh.Optimize', int(bool(quality.get('optimize', True))),
                        control='gmsh/optimization/optimize')
        # Plan 30 WP12. `Mesh.OptimizeNetgen` is a boolean. The pass count
        # used to be written into it, so "three passes" and "one pass" set the
        # same flag to different truthy numbers and the explicit loop in
        # generate() then ran on top of whatever that did.
        self.set_number('Mesh.OptimizeNetgen',
                        int(bool(quality.get('netgen', True))),
                        control='gmsh/optimization/netgen')
        self.set_number('Mesh.Smoothing', int(quality.get('smoothing', 1) or 0),
                        control='gmsh/optimization/smoothing')
        self.set_number('Mesh.OptimizeThreshold',
                        float(quality.get('optimizeThreshold', 0.3)),
                        control='gmsh/optimization/optimizeThreshold')
        self.set_number('Mesh.QualityType',
                        QUALITY_MEASURE.get(quality.get('qualityType', 'sicn'), 0),
                        control='gmsh/optimization/qualityType')

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
                           note=f'{touched} of {len(surfaces)} surface(s) '
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
                f'automatic structuring took {len(structured)} of {total} '
                'volume(s); volume(s) '
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

        for row_name, token in sorted(dropped.items()):
            self.warnings.append(
                f'size field {row_name!r} has no resolvable geometry scope '
                'and was skipped')
            self.ledger.record(f'sizeField:{row_name}', token, None,
                               applied=False, note='scope did not resolve')

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

        self.background_fields.extend(created)
        self.statistics['sizeFields'] = {
            'created': len(created),
            'gmshFields': len(tags),
            'skipped': len(dropped),
            'graph': graph.to_dict(),
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
            note=f'{native} of {len(self.background_fields)} field(s)')
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
        if outvoting:
            self.warnings.append(
                'the background size field shares the mesh with '
                + ' and '.join(outvoting)
                + '; where those give a smaller size they win, so turn them '
                  'off on Global Sizing if a size field looks ignored')
        self.statistics['backgroundField'] = {
            'sources': len(self.background_fields),
            'competingSizeSources': outvoting}

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
        for row in rows:
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
            for curve in sorted(curves):
                if row['mode'] == 'transfinite':
                    self.gmsh.model.mesh.setTransfiniteCurve(
                        curve, nodes, row['law'], coefficient)
                    self.transfinite_curves[curve] = {
                        'control': row['name'], 'nodes': nodes,
                        'law': row['law'], 'coefficient': coefficient}
                else:
                    self.gmsh.model.mesh.setSize(
                        [(0, point) for _d, point in
                         self.gmsh.model.getBoundary([(1, curve)],
                                                     oriented=False)],
                        float(row['localSize']))
                applied += 1
            self.ledger.record(
                f'curveControl:{row["name"]}', row['mode'], row['mode'],
                note=f'{len(curves)} curve(s), {nodes} node(s) each'
                     if row['mode'] == 'transfinite' else f'{len(curves)} curve(s)')
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
                self.warnings.append(
                    f'volume control {name!r} has no resolvable volume scope '
                    'and was skipped')
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
                                   False, note=f'{len(volumes)} volume(s) removed')
                continue

            size = row.get('targetSize')
            if size:
                constant = field.add('Constant')
                field.setNumber(constant, 'VIn', float(size))
                field.setNumbers(constant, 'VolumesList',
                                 [float(tag) for tag in volumes])
                self.background_fields.append(constant)
                applied['sized'] += 1
                self.ledger.record(f'volumeControl:{name}.targetSize', size,
                                   size, note=f'{len(volumes)} volume(s)')

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
            self.warnings.append(
                'no boundary layer was grown: the selected patches matched no '
                'imported surface')
            self.ledger.record('gmsh/boundaryLayers/layerCount', count, 0,
                               applied=False,
                               note='no selected patch matched a surface')
            return
        # MEASURED: the carve below removes every volume and rebuilds one core
        # from the layer's inner surfaces. With two volumes sharing an
        # interface that rebuild cannot close either of them, and the run ends
        # as a hollow shell of prisms several stages later. Refusing here
        # names the cause instead of leaving a confusing post-hoc symptom.
        if len(volumes) > 1:
            raise MeshFailure(
                f'boundary layers are not supported on a {len(volumes)}-volume '
                'assembly: the layer is carved out of a single core volume, '
                'and a shared interface leaves neither side closed. Mesh this '
                'geometry without layers, or split it into one job per volume.')
        # The bounding curves have to be read while the surface still exists;
        # they are what ties the faces that replace it back to its patch name.
        # The boxes are read here for the same reason, and check that what the
        # rebuild closes really is where the un-layered patch was.
        skipped_curves = {tag: self.surface_curves(tag) for tag in skipped}
        skipped_boxes = {tag: gmsh.model.getBoundingBox(2, tag)
                         for tag in skipped}
        self.remove_entities([(3, tag) for tag in volumes])

        try:
            extruded = gmsh.model.geo.extrudeBoundaryLayer(
                [(2, tag) for tag in selected], [1] * count, heights,
                bool(layers.get('quads', True)))
            gmsh.model.geo.synchronize()
        except Exception as error:
            raise MeshFailure(
                f'the boundary layer could not be extruded: {error}') from error

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
                                                 skipped_boxes)
                         if skipped else {})
        rebuilt = list(rebuilt_loops)
        loop = gmsh.model.geo.addSurfaceLoop(top_tags + rebuilt)
        gmsh.model.geo.addVolume([loop])
        gmsh.model.geo.synchronize()
        if skipped:
            self.name_layer_replacements(skipped_curves, rebuilt_loops,
                                         laterals)

        self.layer_bases = [int(tag) for tag in selected]
        self.layer_volumes = [int(tag) for dim, tag in extruded if dim == 3]
        self.layer_laterals = [int(tag) for tag in laterals]
        self.statistics['layers'] = {
            'requestedLayers': count,
            'requestedFirstHeight': abs(heights[0]),
            'requestedTotalThickness': abs(heights[-1]),
            'extrudedSurfaces': len(selected),
            'skippedSurfaces': len(skipped),
            'rebuiltSurfaces': len(rebuilt),
            'patches': sorted({self.surface_patch_name(tag)[0]
                               for tag in selected}),
            'scope': layers.get('scope', 'all_boundary_surfaces'),
        }
        self.ledger.record('gmsh/boundaryLayers/layerCount', count, count,
                           note=f'extruded off {len(selected)} surface(s)')

    def layer_surfaces(self, surfaces, layers):
        """Split the boundary into the surfaces that grow layers and the rest.

        R118. MEASURED on venturi.stl: with no selection every boundary surface
        is extruded, and the worst elements of the finished mesh sat on the
        inlet plane (z=0.004999) and the outlet plane (z=0.5937 / 0.5967) --
        the very stack that failed the quality gate of R113. Prism layers on an
        inlet and an outlet are wrong for every flow case, so the plan may name
        the patches that get them. An empty selection keeps the shipped
        behaviour: every boundary surface.
        """
        wanted = {str(item).strip() for item in (layers.get('patches') or ())
                  if str(item).strip()}
        if not wanted:
            return list(surfaces), []
        # Plan 30 WP12, F-26. The comparison was exact, and on a tessellated
        # import the surfaces are called `face_3` while the page offers the
        # prepared patch names, so a selection matched nothing and the run
        # said only "matched no imported surface" -- with no way to learn what
        # the surfaces were called instead. Matching is now case- and
        # separator-insensitive, the generated `face_N` spelling is accepted
        # as well as the prepared name, and a miss says what was on offer.
        folded = {self._fold_patch_name(item): item for item in wanted}
        selected, skipped, present, matched = [], [], {}, set()
        for tag in surfaces:
            name, known = self.surface_patch_name(tag)
            present[name] = present.get(name, 0) + 1
            keys = {self._fold_patch_name(name), self._fold_patch_name(
                f'face_{self.surface_origin.get(tag, tag)}')}
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
            offer = ', '.join(sorted(present)) or 'none'
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

    def rim_loops(self, tops):
        """Closed loops of the curves that bound exactly one layer top.

        Where two extruded surfaces meet, their tops share a curve. A curve
        owned by a single top is therefore on the rim where the layer stops --
        the outline of the hole the un-extruded surface has to fill.
        """
        owners: dict[int, int] = {}
        for tag in tops:
            for curve in self.surface_curves(tag):
                owners[curve] = owners.get(curve, 0) + 1
        rim = [curve for curve, count in owners.items() if count == 1]
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

    def rebuild_unextruded(self, skipped, tops, boxes):
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
        names = sorted({self.surface_patch_name(tag)[0] for tag in skipped})
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
            if not self._within_any(boxes, points):
                raise MeshFailure(
                    'the boundary layer leaves an opening that does not lie '
                    'on any of the patches without one (' + ', '.join(names)
                    + '), so it cannot be closed: the layer has to surround '
                    'them. Grow the layer on the patches around them too, or '
                    'turn layers off.')
            deviation, extent = self._out_of_plane(points)
            if deviation > max(extent * 1e-4, 1e-12):
                raise MeshFailure(
                    'the patches left without a boundary layer -- '
                    + ', '.join(names) + f' -- span {deviation:.4g} m out of '
                    'any one plane, and the opening the layer leaves can only '
                    'be closed with a flat face. Grow the layer on them too, '
                    'or turn layers off.')
            try:
                loop = gmsh.model.geo.addCurveLoop(curves, reorient=True)
                rebuilt[gmsh.model.geo.addPlaneSurface([loop])] = curves
            except Exception as error:
                raise MeshFailure(
                    'the patches left without a boundary layer could not be '
                    f'rebuilt to close the core volume: {error}. Grow the '
                    'layer on every patch, or turn layers off.') from error
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
        """Coordinates of the points bounding a set of curves."""
        seen, points = set(), []
        for curve in curves:
            for _dim, point in self.gmsh.model.getBoundary(
                    [(1, curve)], oriented=False, recursive=False):
                tag = abs(point)
                if tag not in seen:
                    seen.add(tag)
                    points.append(list(self.gmsh.model.getValue(0, tag, [])))
        return points

    @staticmethod
    def _within_any(boxes, points):
        """Do all these points sit inside one un-layered surface's box?

        The rebuilt face stands in for a patch, so it has to be where that
        patch was. Its own bounding box cannot be asked for -- a geo surface
        that has not been meshed yet answers +-DBL_MAX -- but the points of
        its rim have real coordinates.
        """
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
        for name, tags in grouped.items():
            if not tags:
                continue
            gmsh.model.addPhysicalGroup(2, tags, name=name)
        volumes = [tag for _dim, tag in gmsh.model.getEntities(3)]
        volume_names = self.job.get('volumeNames') or {}
        for tag in volumes:
            # C31-04. As for the surfaces: the identity map names this tag,
            # the version-1 map names a position two sources both claim.
            name = (self.entity_volume_names.get(int(tag))
                    or volume_names.get(str(tag)) or f'volume_{tag}')
            gmsh.model.addPhysicalGroup(3, [tag], name=name)
        self.statistics['groups'] = {
            'boundarySurfaces': len(boundary), 'volumes': len(volumes)}

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
        # has nothing to say about a curve, so there is no job key to read
        # here and inventing one would be a key nobody writes. A section's
        # boundary therefore publishes as `edge_<tag>` walls, and naming them
        # inlet and outlet is a separate piece of work.
        for tag in curves:
            gmsh.model.addPhysicalGroup(1, [tag], name=f'edge_{tag}')
        sections = [tag for _dim, tag in gmsh.model.getEntities(2)]
        region_names = self.job.get('volumeNames') or {}
        for tag in sections:
            name = (self.entity_surface_names.get(int(tag))
                    or region_names.get(str(tag)) or f'region_{tag}')
            gmsh.model.addPhysicalGroup(2, [tag], name=name)
        self.statistics['groups'] = {
            'boundarySurfaces': 0, 'volumes': 0,
            'boundaryCurves': len(curves), 'sections': len(sections)}

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
        below = [index for index, item in enumerate(values) if item < threshold]
        inverted = sum(1 for item in values if item <= 0)
        # Worst first, so the cap keeps the elements a user most wants to see.
        below.sort(key=lambda index: values[index])
        offending = []
        for index in below[:OFFENDER_CAP]:
            tag = int(all_tags[index])
            offending.append({
                'tag': tag, 'value': float(values[index]), 'measure': measure,
                'centroid': self._element_centroid(tag),
            })
        block = {
            'measure': measure,
            'minimum': min(values), 'mean': sum(values) / len(values),
            'below_threshold': len(below), 'total': len(values),
            'inverted': inverted, 'offending': offending,
            'offendingTruncated': len(below) > OFFENDER_CAP,
        }
        if substituted_for:
            block['substituted_for'] = substituted_for
        return block

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
        try:
            tags, coords, _p = self.gmsh.model.mesh.getNodes()
        except Exception:                                    # noqa: BLE001
            return set()
        tags = [int(item) for item in tags]
        if len(coords) < 3 * len(tags):
            return set()
        used = self.meshed_nodes()
        seen, duplicates = {}, set()
        for index, tag in enumerate(tags):
            if used and tag not in used:
                continue
            key = tuple(round(float(coords[3 * index + axis]) / tolerance)
                        for axis in range(3))
            if key in seen:
                duplicates.add(tag)
                duplicates.add(seen[key])
            else:
                seen[key] = tag
        return duplicates

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
                reasons.setdefault(int(tag), 'the boundary layer')
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
        duplicates = self.coincident_nodes(self.merge_tolerance())
        if not duplicates:
            self.ledger.record(
                'gmsh/healing/removeDuplicateNodes', True, True,
                note='no coincident nodes to merge')
            return

        at_risk, where = {}, {}
        for tag, reason in self.protected_surfaces().items():
            hit = duplicates & self.surface_node_set(tag)
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
                f'{len(nodes)} of {len(duplicates)} coincident node(s) lie on '
                f'{name} ({", ".join(sorted(where[name]))}), which merging '
                'would collapse'
                for name, nodes in sorted(at_risk.items()))
            self.ledger.record('gmsh/healing/removeDuplicateNodes',
                               True, False, applied=False, note=reason)
            self.warnings.append('duplicate nodes were not merged: ' + reason)
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
            note=f'{before - after} of {len(duplicates)} coincident node(s) '
                 'merged')
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
                f'{prisms_before} to {prisms_after} prism(s)')

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
            note=f'{quads} quadrangle(s) became {after.get("Triangle", 0)} '
                 'triangle(s) before the volume pass')
        self.statistics['splitQuadrangles'] = {
            'before': before, 'after': after,
            'quadranglesLeft': after.get('Quadrilateral', 0)}
        if after.get('Quadrilateral', 0):
            self.warnings.append(
                f'{after["Quadrilateral"]} quadrangle(s) survived the split; '
                'the volume pass will build pyramids against them')

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
        try:
            if dimension >= 2 and algorithms.get('splitQuadrangles'):
                # Two passes rather than one, because the split has to happen
                # between them; see :meth:`split_quadrangles`. A section has no
                # second pass, so for it the split is simply the last thing
                # done to the surfaces -- the same call in the same place.
                gmsh.model.mesh.generate(2)
                self.split_quadrangles()
                if dimension > 2:
                    gmsh.model.mesh.generate(dimension)
            else:
                gmsh.model.mesh.generate(dimension)
        except MeshFailure:
            raise
        except Exception as error:
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
        for index in range(passes):
            self.reporter.emit(
                'progress', 'optimize', 0.6 + 0.1 * index / max(passes, 1),
                f'optimising, pass {index + 1} of {passes}')
            gmsh.model.mesh.optimize('Netgen')
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
                f'{inverted} element(s) are inverted; the mesh is not usable')
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
            # the mesh is too poor and left to find the control alone.
            self.warnings.append(
                'elements fall below the requested quality; the repair pass '
                '(Repair poor elements) is off, and it is what runs Gmsh\'s '
                'untangling and relocation optimisers on exactly this mesh')
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
        if wanted and not record['pipeline']:
            record['switched'] = sorted(
                tag for tag, name in record['bySurface'].items()
                if name != wanted)
        self.statistics['algorithms'] = record
        if record['switched']:
            self.warnings.append(
                f'{len(record["switched"])} of {len(record["bySurface"])} '
                f'surfaces were not meshed by the {wanted} algorithm that was '
                'chosen: Gmsh retried them with '
                + ' and '.join(name for name in record['used']
                               if name != wanted)
                + '. The mesh is sound; it is not the mesh that was asked for.')
        self.ledger.record(
            'gmsh/algorithms/surface.used', wanted or requested,
            ', '.join(record['used']) or 'not observed',
            applied=not record['switched'],
            note='the algorithm Gmsh logged for each surface it meshed')

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
                unasked.append(f'{name} ({entry["columns"]} column(s))')
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
                (f'{paired}/{total} node(s), {matched}/{slave_faces} face(s), '
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
            self.ledger.record('gmsh/output/elementOrder', order, 1,
                               note='first order; the default and the only '
                                    'order the OpenFOAM route can read')
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
        '_t, cells, _n = gmsh.model.mesh.getElements(3)\n'
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

    def read_back_census(self, path):
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
            [sys.executable, '-c', self.VERIFY_READER, str(path)],
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
            found = self.read_back_census(path)
        except subprocess.TimeoutExpired:
            return {'checked': False,
                    'reason': 'reopening the file took longer than '
                              f'{self.VERIFY_TIMEOUT_SECONDS}s'}
        except Exception as failure:
            return {'checked': True, 'matched': False,
                    'expected': expected,
                    'difference': 'the file could not be reopened: '
                                  f'{str(failure)[:300]}'}
        differences = []
        if found['nodes'] != expected['nodes']:
            differences.append(f'{expected["nodes"]} nodes written, '
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
                 'reader -- so there is no path that could hand this file to '
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='FoamMesh Gmsh runner')
    parser.add_argument('job')
    parser.add_argument('--result')
    parser.add_argument('--progress')
    arguments = parser.parse_args(argv)

    reporter = Reporter(arguments.progress)
    run = None
    try:
        job = load_job(Path(arguments.job))
        result_path = Path(arguments.result or job.get('resultPath')
                           or Path(arguments.job).with_name('result.json'))
        run = GmshRun(job, reporter)
        document = run.execute()
        status = 0
    except Exception as error:  # noqa: BLE001 - the runner reports, never crashes
        detail = f'{type(error).__name__}: {error}'
        reporter.emit('failed', 'gmsh', 1.0, detail)
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
