"""Configuration state to one immutable, hashed job intent.

Every derived value carries the calculation version that produced it, and
warnings accumulate rather than being dropped, so a job that degraded says so
instead of looking identical to one that did not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re

from foammesh.core.quantities import count_text

from .layers import BoundaryLayers, derive_boundary_layers
from .periodic import PeriodicPlan, derive_periodic_pairs
from .quality import QualityThresholds, derive_thresholds
from .size_fields import (SizeFieldPlan, derive_size_fields,
                          derive_surface_sizes, validate_field_graph)
from .sizing import GlobalSizing, derive_global_sizing
from .topology import Healing, derive_healing

JOB_SCHEMA_VERSION = 1

#: Gmsh's numeric algorithm codes. Named here once so nothing else carries a
#: magic number, and so the enum parity test can compare both directions.
SURFACE_ALGORITHM = {
    'meshadapt': 1,
    'automatic': 2,
    'delaunay': 5,
    'frontal_delaunay': 6,
    'frontal_delaunay_quads': 8,
    'packing_parallelograms': 9,
    'quasi_structured_quad': 11,
}
#: Plan 31 FC-B. Surface algorithms that write a quadrilateral surface, and so
#: belong to the same target gate recombination does: the polyMesh route reads
#: the tetrahedral family, and a quad surface is what it refuses.
#:
#: The membership is MEASURED, not read off the option's name, and the
#: measurement removed one of the two the package expected to find here.
#: Counting the elements in the written ``.msh``
#: (``plans/evidence/plan31/fcb-algorithms/surface.json``):
#:
#:   * ``quasi_structured_quad`` (11) wrote 6008 quadrilaterals and 0 triangles
#:     on the duct, 4596 and 0 on the elbow, 8216 and 0 on the sphere, with
#:     recombination off. It always writes quads, so it is gated.
#:   * ``frontal_delaunay_quads`` (8) wrote 0 quadrilaterals on every geometry
#:     with recombination off -- 2240 triangles on the duct, 2612 on the elbow.
#:     It is a triangle algorithm that lays out triangles a later recombination
#:     step can pair up, and it only reaches quads once recombination is on,
#:     which this same gate already refuses. Gating it as well would refuse the
#:     polyMesh route a mesh it can read perfectly well, so it is not here.
QUAD_SURFACE_ALGORITHMS = frozenset({'quasi_structured_quad'})
VOLUME_ALGORITHM = {
    'delaunay': 1,
    'frontal': 4,
    'hxt': 10,
    # Plan 31 FC-B. Shipped on the measurement, not on the name: at matched
    # cell count it beat both incumbents on non-orthogonality and skewness on
    # every geometry that meshed, and costs about twice Delaunay's wall clock.
    'mmg3d': 7,
}
#: Volume algorithms that can actually use more than one thread. Gmsh's classic
#: Delaunay and Frontal 3D kernels are sequential implementations: they ignore
#: ``Mesh.MaxNumThreads3D`` entirely. HXT is the parallel tetrahedraliser. So a
#: case asking for sixteen threads with Delaunay selected meshes on one thread
#: and reports no error, which is exactly what a measured sweep of five models
#: showed -- 1.00x speedup at sixteen threads across the board.
THREADED_VOLUME_ALGORITHMS = frozenset({'hxt'})
#: Plan 31 FC-B. MMG3D stays out of this set, and NOT on a measurement: the
#: attempt to make one failed its own positive control. Timing one thread
#: against eight, HXT -- which is in this set because it threads -- came back
#: 1.01x at 7.6k cells and 1.06x at 364k, because the harness times the whole
#: runner and the volume pass is a minority of that. A harness that cannot see
#: the known-parallel kernel thread cannot rule on the one in question. So the
#: entry is withheld on the conservative reading, which understates: a case
#: asking for eight threads is told it got one. See
#: fcb-algorithms/threads.json and threads-large.json.
#: Cell shape -> Mesh.SubdivisionAlgorithm. 0 leaves tetrahedra alone; 2
#: subdivides every cell into hexahedra.
CELL_SHAPE = {
    'tetrahedral': 0,
    'hexahedral': 2,
}
#: Plan 31 CP-08 item 3. ``Mesh.RecombinationAlgorithm``. MEASURED on
#: duct.step: the four produce four different meshes -- 306 quadrangles and 70
#: leftover triangles, 356 with none left, 460, and 408 -- and 4 read back as
#: 4 and then meshed *nothing*, surface and volume both empty, while 9 was
#: silently clamped to 0. So the four are the whole reachable set.
RECOMBINATION_ALGORITHM = {
    'simple': 0,
    'blossom': 1,
    'simple_full_quad': 2,
    'blossom_full_quad': 3,
}
#: What each combination is supposed to come out as, checked against what the
#: mesher produced. Surface family first, then volume.
#:
#: MEASURED, and the pyramids are the point: recombination gives a quadrangle
#: *surface* over a pyramid-and-tetrahedron interior, not a hex mesh. A run
#: that promised hexahedra and delivered pyramids would be lying, so the
#: expectation says pyramids.
EXPECTED_FAMILIES = {
    # (cell shape, recombine) -> (surface families, volume families)
    ('tetrahedral', False): (('Triangle',), ('Tetrahedron',)),
    ('tetrahedral', True): (('Quadrilateral',), ('Tetrahedron', 'Pyramid')),
    ('hexahedral', False): (('Quadrilateral',), ('Hexahedron',)),
    # Subdividing a recombined mesh was measured to change nothing: the same
    # 356 quadrangles and 2208 tetrahedra as recombination alone.
    ('hexahedral', True): (('Quadrilateral',), ('Tetrahedron', 'Pyramid')),
}
#: Plan 31 FC-C. What automatic structuring owes at the whole-mesh grain,
#: which is nothing -- and saying nothing here is the honest answer rather
#: than a weakened one.
#:
#: The family list is an *and*: every name in it must appear in the finished
#: mesh or the run warns. That works for a request whose answer is one family
#: for the whole model, and automatic structuring is not such a request.
#: MEASURED on Gmsh 4.15.2: a unit box came back as 150 quadrilaterals over
#: 125 hexahedra, with no triangle and no tetrahedron in it; the same request
#: on a box beside a sphere came back as 132 triangles and 150 quadrilaterals
#: over 207 tetrahedra and 125 hexahedra. Both are correct outcomes of the
#: same request. Naming all four families would make the first mesh warn for
#: the triangles it was right not to have; naming the hexahedral pair would
#: make the second warn for a sphere no interpolation could have structured.
#:
#: So the run makes no whole-mesh promise and makes a per-volume one instead:
#: the runner's ``measure_structuring`` counts every volume separately and
#: reports which ones came back with no tetrahedra in them. That is a
#: stricter reading than a family census, not a looser one -- it can tell a
#: model that went half structured from one that went fully structured, which
#: no whole-mesh list can.
AUTOMATIC_STRUCTURING_FAMILIES: tuple = ((), ())
QUALITY_MEASURE = {'sicn': 0, 'sige': 1, 'gamma': 2, 'disto': 3}
CURVE_LAW = {'progression': 'Progression', 'bump': 'Bump', 'beta': 'Beta'}
EXPORT_VERSION = 'gmsh.export.v1'
#: ``Mesh.Format`` for Gmsh's own SU2 writer. Named once so the runner and the
#: derivation cannot disagree about which code means what.
SU2_FORMAT_CODE = 42
#: ``Mesh.Format`` for "decide from the file extension", which is what every
#: route but a forced SU2 write wants.
AUTO_FORMAT_CODE = 10

#: What the Gmsh mesher itself produces and this application can read back.
#: Plan 31 CP-01 (C31-01/C31-02). Element order used to be a property of the
#: target solver alone -- one table, ``SOLVER_ELEMENT_ORDERS`` -- so an
#: *export* adapter decided whether Gmsh was allowed to mesh at all. That was
#: backwards in both directions: quadratic meshing was reachable only by naming
#: SU2 as the target, and it stopped being reachable the moment that optional
#: exporter was found unqualified (C31-02). There are two capabilities here and
#: they are now two tables: what Gmsh can mesh, and what each export adapter
#: can represent. MEASURED: ``core/mesh/census.py`` MSH_VOLUME_TYPES reads
#: types 11/12/13/14/17/18/19, so a second-order MSH is a mesh this application
#: counts, summarises and keeps; exporting it is a separate question from
#: meshing it.
MESHER_ELEMENT_ORDERS: tuple[int, ...] = (1, 2)

#: What each *export adapter* can represent. A target token absent from this
#: table names no exporter, so the mesher's own capability applies and the run
#: is native-only.
EXPORTER_ELEMENT_ORDERS: dict[str, tuple[int, ...]] = {
    'openfoam': (1,),
    'su2': (1,),
}
#: Per export adapter, why an order it refuses is refused. Written to be shown
#: to a user unchanged.
ELEMENT_ORDER_REFUSALS: dict[tuple[str, int], str] = {
    ('openfoam', 2): (
        'OpenFOAM 13 reads first-order MSH 2.2 and nothing else — gmshToFoam '
        'and the direct polyMesh publisher both do — so a quadratic mesh '
        'cannot be exported to it. Clear the target solver to mesh at order 2: '
        'the native MSH is kept and counted, and no polyMesh is published.'),
    ('su2', 2): (
        "FoamMesh's own SU2 reader and census hold the linear VTK type codes "
        'only — 10, 12, 13 and 14 — so a quadratic SU2 file, whose cells '
        'carry code 24, is one this application would write and then be unable '
        'either to open or to count. So no quadratic SU2 file is written: the '
        'mesh is made and exported at first order instead. Clear the target '
        'solver to mesh at order 2 natively; qualifying the SU2 exporter for '
        'second order is separate work.'),
}
#: The order every route accepts, and therefore what an unknown target clamps
#: to.
DEFAULT_ELEMENT_ORDER = 1


def _schema_default(*path, fallback=None):
    """The shipped default for one schema leaf.

    Plan 29 WP8. A default restated in a second place is a default that drifts:
    the thread count shipped as 1 in the schema and 4 here, so a project that
    had never touched the control derived a different job from the one the page
    showed.
    """
    from foammesh.db.configurations_schema import schema as CONFIGURATIONS_SCHEMA

    node = CONFIGURATIONS_SCHEMA
    for key in path:
        try:
            node = node[key]
        except (KeyError, TypeError):
            return fallback
    value = getattr(node, 'default', None)
    try:
        return value() if callable(value) else fallback
    except Exception:
        return fallback


#: The MSH versions this application's own readers parse. Plan 31 CP-01
#: (C31-01). MEASURED by grepping every consumer of the native MSH:
#: ``core/gmsh/publish.py`` ``read_msh`` refuses a header that does not start
#: "2." outright, and ``core/quality/geometry_fidelity/boundary.py`` calls the
#: same ``read_msh``. Nothing in the product turns 4.1 into a polyMesh.
#:
#: This tuple is about *publication*, not about reading. Plan 31 DP-17 taught
#: ``core/mesh/census.py`` ``msh_element_census`` both block grammars, because
#: as a 2.2 parser it did not refuse 4.1 -- it read the ``$Nodes`` header's
#: field 0, the entity-block count, as the node count, and invented a mesh.
#: Counting a file is not publishing it, so this stays at 2.2.
READABLE_MSH_VERSIONS: tuple[float, ...] = (2.2,)
#: The version every Gmsh run writes.
#:
#: Plan 30 WP12 made this a function of the target solver -- 4.1 for SU2, 2.2
#: otherwise -- on the reasoning that only 4.1 carries mid-side nodes. Two
#: things were wrong with that. The version travelled with the *target* while
#: :attr:`ExportSettings.publishes_poly_mesh` travelled with the *order*, so a
#: first-order SU2 run wrote 4.1 and was then handed to the polyMesh publisher,
#: which reads 2.2 only: C31-01, a route that could not complete. And the
#: premise does not hold -- MSH 2.2 carries second-order elements as types
#: 11/12/13/14/17/18/19, which is exactly the table ``core/mesh/census.py``
#: already reads out of 2.2 files. One version, the one every reader here
#: accepts, removes the disagreement rather than relabelling it.
NATIVE_MSH_VERSION = 2.2
DEFAULT_MSH_VERSION = NATIVE_MSH_VERSION

#: Targets whose export is a ``constant/polyMesh``. Plan 31 CP-01. SU2 is not
#: one of them: it reads the file Gmsh wrote, and publishing a polyMesh for it
#: made an optional file export depend on the OpenFOAM publisher and, through
#: the QA task, on a checkMesh binary that route never needs. ``unselected``
#: stays here because a run with no target still owes the user a mesh the
#: viewport and checkMesh can open; nothing about it is SU2-specific.
POLY_MESH_TARGETS = frozenset({'openfoam', 'unselected'})


class PlanDerivationError(ValueError):
    pass


#: The files a Gmsh run may be asked to write *beside* its native MSH, and what
#: a cold read-back measured each one still carrying.
#:
#: Plan 31 FC-A, ledger row ``export-formats-unexposed``. MEASURED 2026-09-06
#: with Gmsh 4.15.2 in the ``OpenFOAM13Runtime`` distro -- script and output at
#: ``plans/evidence/plan31/fca-format-io/formats.json`` -- on one box meshed to
#: 307 nodes, 984 tetrahedra and 204 named boundary triangles, carrying three
#: named faces and one named volume:
#:
#: ``med``   80309 bytes, and the reopened file returns 307 nodes, 984 tets and
#:           all four group names -- but padded with spaces to 80 characters,
#:           so a reader comparing names has to strip them first.
#: ``cgns``  62905 bytes, 307 nodes, 984 tets, all four group names exactly.
#:
#: ``vtk`` is writable and deliberately absent: it read back with the right
#: element counts and **no physical groups at all**, so it cannot carry the
#: patch identity this product publishes by, and a file whose identity cannot
#: be checked is not a mesh export here.
COMPANION_EXPORT_FORMATS: dict[str, dict] = {
    'med': {
        'suffix': '.med',
        'carries_group_names': True,
        'note': 'group names round-trip, padded to 80 characters',
    },
    'cgns': {
        'suffix': '.cgns',
        'carries_group_names': True,
        'note': 'group names round-trip exactly',
    },
}

#: The formats a run writes that are not companions: the native mesh every
#: reader here parses, and the SU2 file the SU2 route hands to the solver.
NATIVE_EXPORT_FORMATS = ('msh', 'su2')


def companion_export_formats(requested) -> tuple[str, ...]:
    """The companion tokens in *requested*, refusing any that is not measured.

    A companion is an addition and never a replacement. The MSH is what the
    polyMesh publisher, the element census and the geometry-fidelity check all
    read, so a run asked for a MED alone would finish having written nothing
    this application can open; :func:`execution.write_job` therefore always
    asks for the MSH too, and this names only the extras.

    Refusing an unmeasured token is the point of the function. Gmsh writes 39
    extensions in this build and can re-read 28 of them, and the difference
    matters here: a format the product cannot read back is a file whose
    identity it can never check.
    """
    unknown = sorted({str(token) for token in requested}
                     - set(COMPANION_EXPORT_FORMATS)
                     - set(NATIVE_EXPORT_FORMATS))
    if unknown:
        raise PlanDerivationError(
            'no measured Gmsh writer for: ' + ', '.join(unknown)
            + '. The measured companion formats are '
            + ', '.join(sorted(COMPANION_EXPORT_FORMATS)) + '.')
    return tuple(token for token in requested
                 if str(token) in COMPANION_EXPORT_FORMATS)


def solver_token(target_solver) -> str:
    """``TargetSolver``, enum member or string -> the token this module uses."""
    return str(getattr(target_solver, 'value', target_solver)
               or 'unselected').split('.')[-1].lower()


def supported_element_orders(target_solver) -> tuple[int, ...]:
    """The element orders this route may write.

    Plan 31 CP-01. Two capabilities, read in order: what Gmsh can mesh, then
    what the selected export adapter can represent. A target that names no
    exporter -- ``unselected`` -- constrains nothing, because there is no
    export to be incompatible with.
    """
    token = solver_token(target_solver)
    exporter = EXPORTER_ELEMENT_ORDERS.get(token)
    if exporter is None:
        return MESHER_ELEMENT_ORDERS
    return tuple(order for order in MESHER_ELEMENT_ORDERS
                 if order in exporter) or (DEFAULT_ELEMENT_ORDER,)


def element_order_options(target_solver) -> tuple[tuple[int, bool, str], ...]:
    """``(order, supported, reason)`` for every order Gmsh can write.

    What a page needs to grey an option and say why, taken from the same table
    the derivation clamps with, so the control cannot offer an order the job
    would silently drop.
    """
    token = solver_token(target_solver)
    allowed = supported_element_orders(token)
    return tuple(
        (order, order in allowed,
         '' if order in allowed else ELEMENT_ORDER_REFUSALS.get(
             (token, order),
             f'the {token} route accepts element order '
             + ' or '.join(str(item) for item in allowed) + ' only'))
        for order in (1, 2))


@dataclass(frozen=True)
class Algorithms:
    surface: str
    volume: str
    cell_shape: str
    #: Plan 30 WP12. Whether the surfaces are recombined into quads before the
    #: 3D pass. Only ever True for an SU2 target; :func:`derive_algorithms`
    #: clears it for the rest and says so in :attr:`warnings`.
    recombine: bool = False
    #: Plan 31 CP-08 item 3. Which recombiner, by name. Only consulted when
    #: :attr:`recombine` is set.
    recombination_algorithm: str = 'blossom'
    #: Plan 31 FC-B. Whether a surface that defeats the chosen algorithm may be
    #: retried with another one (``Mesh.AlgorithmSwitchOnFailure`` and
    #: ``Mesh.MaxRetries``). Gmsh's own default is on; what this adds is that
    #: the run says which algorithm each surface was finally meshed with.
    fallback: bool = True
    #: Plan 31 FC-C. Whether the recombined surfaces are split back into
    #: triangles before the volume pass. Only consulted when
    #: :attr:`recombine` is set, and the reason it exists is the route that
    #: cannot read quadrangles: instead of clearing the recombination and
    #: meshing as though it had never been asked for, the run recombines and
    #: splits, and the target gets tetrahedra out of a surface mesh the
    #: recombiner shaped.
    split_quadrangles: bool = False
    #: Plan 31 FC-C. Whether the run also asked Gmsh to structure whatever it
    #: could by itself. Carried here only because it changes the families the
    #: run owes; the control itself lives under ``gmsh/structuring``.
    automatic_structuring: bool = False
    warnings: tuple = ()

    @property
    def surface_code(self) -> int:
        return SURFACE_ALGORITHM[self.surface]

    @property
    def volume_code(self) -> int:
        return VOLUME_ALGORITHM[self.volume]

    @property
    def subdivision_code(self) -> int:
        return CELL_SHAPE[self.cell_shape]

    @property
    def recombination_code(self) -> int:
        return RECOMBINATION_ALGORITHM[self.recombination_algorithm]

    #: Plan 31 FC-B. Whether the *surface algorithm alone* writes quads.
    #: MEASURED: ``Mesh.Algorithm`` 11 writes quadrilaterals with
    #: ``Mesh.RecombineAll`` off; 8 writes triangles until something
    #: recombines them, which is exactly what "Frontal-Delaunay for quads"
    #: means -- a triangulation laid out to recombine well.
    @property
    def surface_writes_quads(self) -> bool:
        return self.surface == 'quasi_structured_quad'

    @property
    def expected_families(self) -> tuple:
        """``(surface families, volume families)`` this combination owes."""
        if self.automatic_structuring:
            return AUTOMATIC_STRUCTURING_FAMILIES
        # A split mesh owes what an unrecombined one owes: the quadrangles are
        # gone by the time the volume pass runs, so no pyramid can be made
        # against one. MEASURED, and the pyramids are how it is checked.
        recombined = bool(self.recombine) and not self.split_quadrangles
        surface, volume = EXPECTED_FAMILIES[(self.cell_shape, recombined)]
        if self.surface_writes_quads and not self.recombine:
            # The quad surfacer is closed onto the tetrahedral interior the
            # same way recombination is, so the volume owes pyramids too.
            # Guarded on `recombine` rather than on `recombined`: the runner
            # splits only when recombination is on, so a split run splits this
            # surfacer's quadrangles too, and the families EXPECTED_FAMILIES
            # already gave are the right ones.
            surface = ('Quadrilateral',)
            if self.cell_shape == 'tetrahedral':
                volume = ('Tetrahedron', 'Pyramid')
        return surface, volume

    def to_dict(self) -> dict:
        return {
            'surface': self.surface, 'surfaceCode': self.surface_code,
            'volume': self.volume, 'volumeCode': self.volume_code,
            'cellShape': self.cell_shape,
            'subdivisionAlgorithm': self.subdivision_code,
            'recombine': self.recombine,
            'recombinationAlgorithm': self.recombination_algorithm,
            'recombinationCode': self.recombination_code,
            'fallback': self.fallback,
            'splitQuadrangles': self.split_quadrangles,
            # Carried into the job so the runner can hold the mesh it produced
            # against the mesh this combination was supposed to produce. The
            # runner cannot work it out for itself: by the time it counts
            # elements the cell shape is a bare option code.
            'expectedSurfaceFamilies': list(self.expected_families[0]),
            'expectedVolumeFamilies': list(self.expected_families[1]),
        }


@dataclass(frozen=True)
class Structuring:
    """Structure the model asks for as a whole, rather than face by face.

    Plan 31 FC-C. ``mesh.setTransfiniteAutomatic`` is the only control here so
    far: it walks the model and structures every volume it can. The result is
    per volume, so nothing in this class claims the mesh came back structured
    -- that is counted out of the mesh by the runner.
    """

    automatic: bool = False
    #: Plan 31 FC-C. Structure the inside of a three-sided transfinite face
    #: as well as its boundary. MEASURED: 120 triangles became 64 -- 8x8 --
    #: on a triangle carrying nine nodes a side.
    transfinite_tri: bool = False
    warnings: tuple = ()

    def to_dict(self) -> dict:
        return {'automatic': self.automatic,
                'transfiniteTri': self.transfinite_tri}


@dataclass(frozen=True)
class Farfield:
    """An external-flow domain built by the runner, not supplied as a file.

    Plan 30 WP12. Section 4.1 recorded "Booleans, farfield box generation: N
    -- assembly fixtures ship a ``_farfield.stl`` because the app cannot make
    one". The box is described here as a multiple of the geometry's own size,
    and built by the runner, which is the only place that knows the imported
    bounding box.
    """

    enabled: bool = False
    #: Multiples of the bounding-box diagonal, added on every side.
    padding: float = 2.0
    #: Plan 31 CP-08 item 7. What the cut should do with a pocket it leaves
    #: inside the geometry: ``discard``, ``keep`` or ``refuse``.
    sealed_cavities: str = 'discard'

    def to_dict(self) -> dict:
        return {'enabled': self.enabled, 'padding': self.padding,
                'sealedCavities': self.sealed_cavities}


@dataclass(frozen=True)
class Dimensionality:
    """Whether this job meshes a volume, a planar section, or a wedge of one.

    FC-E. The dimension travels on the job because three separate things read
    it and none of them can infer it: the runner picks ``generate(2)`` or
    ``generate(3)``, the import check decides whether a model with no volumes
    is a failed CAD import or exactly what was asked for, and the publisher
    decides whether a surface-only mesh becomes one cell of thickness or a
    refusal. Inferring it from the mesh is what made the old refusal wrong --
    it named a cause ("the CAD imported without solids") for a fact it had
    measured ("no volume cells").
    """

    mode: str = 'three_d'
    thickness: float = 0.01
    wedge_angle: float = 5.0
    wedge_axis: str = 'x'
    front_patch: str = 'front'
    back_patch: str = 'back'
    warnings: tuple[str, ...] = ()
    #: DP-675. ``((name, (curve tag, ...)), ...)``: the patch each boundary
    #: curve of a section is published as. A curve not listed keeps
    #: ``edge_<tag>``.
    edge_names: tuple = ()

    @property
    def planar(self) -> bool:
        """Whether Gmsh should mesh a surface rather than a volume."""
        return self.mode in ('two_d', 'axisymmetric')

    @property
    def generate_dimension(self) -> int:
        return 2 if self.planar else 3

    def to_dict(self) -> dict:
        return {
            'mode': self.mode, 'planar': self.planar,
            'generateDimension': self.generate_dimension,
            'thickness': self.thickness, 'wedgeAngle': self.wedge_angle,
            'wedgeAxis': self.wedge_axis, 'frontPatch': self.front_patch,
            'backPatch': self.back_patch, 'warnings': list(self.warnings),
            'edgeNames': {name: list(tags) for name, tags in self.edge_names},
            'calculation_version': DIMENSION_VERSION,
        }


#: Every ``mode`` this derivation accepts, and what it means for the runner.
MESH_DIMENSIONS = ('three_d', 'two_d', 'axisymmetric')
DIMENSION_VERSION = 'gmsh.dimensionality.v1'
#: Above this the wedge transform stops being a small-angle one, and the two
#: faces are far enough apart that the cells against the axis are visibly
#: curved. OpenFOAM does not refuse a larger angle; this derivation says so
#: rather than letting the run look ordinary.
WEDGE_ANGLE_ADVISED_MAX = 5.0


def _number(value, default: float) -> float:
    """A typed number, or the default when nothing was typed at all."""
    if value is None or value == '':
        return default
    return float(value)


#: A patch name OpenFOAM and SU2 both take: a word, no spaces or punctuation.
_PATCH_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def parse_edge_names(text) -> tuple:
    """``'inlet: 1; walls: 2, 4'`` -> ``(('inlet', (1,)), ('walls', (2, 4)))``.

    DP-675 (field audit 0924 gmsh-generate-export D11). A section's boundary
    is curves, and the prepared-geometry store names faces, so there is no
    name to inherit: the user names the curves by the tag a first run
    publishes as ``edge_<tag>``. Refused with the offending entry named, never
    guessed.
    """
    names: list = []
    seen: dict = {}
    for entry in str(text or '').replace(chr(10), ';').split(';'):
        entry = entry.strip()
        if not entry:
            continue
        name, colon, tags = entry.partition(':')
        name = name.strip()
        if not colon or not _PATCH_NAME.match(name):
            raise PlanDerivationError(
                f'edge names: {entry!r} is not "name: tag, tag"; a name is '
                'one word of letters, digits and underscores, followed by a '
                'colon and the curve tags a first run named edge_<tag>')
        numbers = []
        for token in re.split(r'[\s,]+', tags.strip()):
            if not token:
                continue
            token = token[5:] if token.lower().startswith('edge_') else token
            if not token.isdigit() or int(token) <= 0:
                raise PlanDerivationError(
                    f'edge names: {token!r} in {entry!r} is not a curve tag; '
                    'tags are the positive numbers in edge_<tag>')
            tag = int(token)
            if tag in seen and seen[tag] != name:
                raise PlanDerivationError(
                    f'edge names: curve {tag} is named both {seen[tag]!r} '
                    f'and {name!r}; a curve belongs to one patch')
            seen[tag] = name
            numbers.append(tag)
        if not numbers:
            raise PlanDerivationError(
                f'edge names: {name!r} lists no curve tags')
        names.append((name, tuple(numbers)))
    merged: dict = {}
    for name, tags in names:
        merged.setdefault(name, [])
        merged[name].extend(tag for tag in tags if tag not in merged[name])
    return tuple((name, tuple(tags)) for name, tags in merged.items())


def derive_dimensionality(values: dict) -> Dimensionality:
    values = dict(values or {})
    mode = _enum(values.get('mode'), 'three_d')
    warnings: list[str] = []
    if mode not in MESH_DIMENSIONS:
        raise PlanDerivationError(
            f'{mode!r} is not a meshing dimensionality; this product meshes '
            + ', '.join(MESH_DIMENSIONS))
    # `or default` would be wrong here: it turns a typed zero into the
    # default, so a user who asked for a zero-thickness mesh or a zero-degree
    # wedge would get 0.01 m and 5 degrees with nothing said. Only an absent
    # or empty value defaults.
    thickness = _number(values.get('thickness'), 0.01)
    if thickness <= 0:
        raise PlanDerivationError(
            'the front-to-back thickness of a planar mesh has to be positive; '
            f'{thickness!r} would put both empty patches on the same plane')
    angle = _number(values.get('wedgeAngle'), 5.0)
    if mode == 'axisymmetric':
        if not 0 < angle <= 10.0:
            raise PlanDerivationError(
                f'a wedge angle of {angle:g} degrees is outside the range this '
                'product meshes (0 to 10); OpenFOAM applies a small-angle '
                'transform across a wedge pair')
        if angle > WEDGE_ANGLE_ADVISED_MAX:
            warnings.append(
                f'the wedge angle is {angle:g} degrees; OpenFOAM treats a '
                'wedge as a small-angle sector and its own cases use '
                f'{WEDGE_ANGLE_ADVISED_MAX:g}')
    axis = _enum(values.get('wedgeAxis'), 'x')
    if axis not in ('x', 'y', 'z'):
        raise PlanDerivationError(
            f'{axis!r} is not a coordinate axis; a wedge is revolved about '
            'x, y or z')
    front = str(values.get('frontPatch') or 'front')
    back = str(values.get('backPatch') or 'back')
    if front == back:
        raise PlanDerivationError(
            f'the front and back patches are both named {front!r}; the two '
            'faces of a planar mesh have to be two patches')
    edge_names = parse_edge_names(values.get('edgeNames'))
    for name, _tags in edge_names:
        if name in (front, back):
            raise PlanDerivationError(
                f'edge names: {name!r} is also the name of the front or back '
                'face; a boundary curve and a face cannot share a patch')
    return Dimensionality(
        mode=mode, thickness=thickness, wedge_angle=angle, wedge_axis=axis,
        front_patch=front, back_patch=back, warnings=tuple(warnings),
        edge_names=edge_names)


@dataclass(frozen=True)
class Parallel:
    threads: int
    #: Whether the chosen volume algorithm can use those threads. False means
    #: the 3D pass runs on one thread regardless; surface meshing still threads,
    #: because independent faces are meshed concurrently whatever the 3D kernel.
    volume_threaded: bool = True

    def to_dict(self) -> dict:
        return {'threads': self.threads,
                'volumeThreaded': self.volume_threaded,
                'effectiveVolumeThreads':
                    self.threads if self.volume_threaded else 1}


@dataclass(frozen=True)
class ExportSettings:
    """What Gmsh writes, and at what element order.

    Plan 29 WP8, corrected by Plan 30 WP-07, corrected again by Plan 31 CP-01.
    Order is a mesher capability constrained by whichever export adapter was
    selected: :data:`MESHER_ELEMENT_ORDERS` says what Gmsh can produce and this
    application can read back, :data:`EXPORTER_ELEMENT_ORDERS` says what each
    export can represent, and the derivation clamps to the intersection and
    says why in :attr:`warnings`.

    The three answers this class gives -- the file version, the format, and
    whether a polyMesh is published -- have to describe one run. They did not:
    :attr:`msh_version` read the target and :attr:`publishes_poly_mesh` read
    the order, so an order-1 SU2 case wrote MSH 4.1 and was then sent to a
    publisher that parses 2.2 only (C31-01). :meth:`__post_init__` now refuses
    to construct a settings object whose file no invoked reader could open.
    """

    target_solver: str
    element_order: int
    second_order_incomplete: bool
    mesh_format: str
    #: Plan 31 FC-D. Straight midside nodes rather than nodes projected onto
    #: the CAD. Optional with a False default because that is Gmsh's own, and
    #: because every stored case predates the control.
    second_order_linear: bool = False
    #: Plan 31 FC-D. Whether the nodes are renumbered before the file is
    #: written. Independent of the order: it is about the numbering, not the
    #: shapes, so every route may ask for it.
    renumber: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a combination whose file and reader disagree.

        The C31-01 invariant, enforced where the combination is made rather
        than discovered at publication time by a reader complaining that the
        mesh "declares 4.1 version". There is no route that should be able to
        produce such a settings object; if one appears, it is a bug in this
        module and it fails here with the numbers in the message.
        """
        if (self.publishes_poly_mesh
                and self.msh_version not in READABLE_MSH_VERSIONS):
            raise PlanDerivationError(
                f'the {self.target_solver} route would publish a polyMesh from '
                f'an MSH {self.msh_version:g} file, and the publisher reads '
                + ' or '.join(f'{item:g}' for item in READABLE_MSH_VERSIONS)
                + ' only')

    @property
    def msh_version(self) -> float:
        """``Mesh.MshFileVersion`` for this run.

        Plan 31 CP-01. One version, :data:`NATIVE_MSH_VERSION`, because it is
        the only one anything in this application reads: the polyMesh
        publisher, the element census and the geometry-fidelity check all parse
        MSH 2.2. Second order survives it -- 2.2 carries element types 11
        through 19 -- so nothing is lost by not writing 4.1, and what was lost
        by writing it was every reader.
        """
        return NATIVE_MSH_VERSION

    @property
    def format_code(self) -> int:
        """``Mesh.Format`` for this route.

        Gmsh picks a writer from the file extension unless told otherwise, so
        only the forced SU2 write names a code.
        """
        return (SU2_FORMAT_CODE if self.mesh_format == 'su2'
                else AUTO_FORMAT_CODE)

    @property
    def writes_su2(self) -> bool:
        """Whether this run produces ``mesh.su2`` beside the ``.msh``."""
        return self.mesh_format == 'su2'

    @property
    def publishes_poly_mesh(self) -> bool:
        """Whether this run's ``.msh`` becomes ``constant/polyMesh``.

        Two conditions, both of them about the *export*, not the mesh. The
        target has to be one whose export is a polyMesh at all
        (:data:`POLY_MESH_TARGETS`) -- an SU2 case reads the file Gmsh wrote,
        and publishing one for it dragged the OpenFOAM publisher and the
        checkMesh binary into a route that needs neither. And the mesh has to
        be linear, because polyMesh is a linear format and the publisher reads
        the four linear MSH volume types.

        Answering both here rather than at the publisher is the point: the
        publisher's refusal reads "the mesh has no volume cells", which
        describes a failed import and not a quadratic mesh.
        """
        return (self.target_solver in POLY_MESH_TARGETS
                and self.element_order == 1)

    def to_dict(self) -> dict:
        return {
            'targetSolver': self.target_solver,
            'elementOrder': self.element_order,
            'secondOrderIncomplete': self.second_order_incomplete,
            'secondOrderLinear': self.second_order_linear,
            'renumber': self.renumber,
            'meshFormat': self.mesh_format,
            'mshVersion': self.msh_version,
            'formatCode': self.format_code,
            'writesSu2': self.writes_su2,
            'publishesPolyMesh': self.publishes_poly_mesh,
            'warnings': list(self.warnings),
            'calculation_version': EXPORT_VERSION,
        }


@dataclass(frozen=True)
class CurveControl:
    control_id: str
    name: str
    scope_token: str
    mode: str
    segments: int
    law: str
    coefficient: float
    local_size: float
    order: int
    #: Plan 30 WP12. Whether the surfaces the scope names are made structured
    #: too. A transfinite curve alone only distributes nodes along an edge.
    transfinite_surface: bool = False
    #: Plan 31 CP-08. Put the small elements at the far end of the curve. The
    #: runner turns this into the sign Gmsh reads the direction from.
    reverse_grading: bool = False
    #: Plan 31 FC-C. The corner points Gmsh should interpolate between, empty
    #: to let it choose. Three or four tags; the runner checks they are on the
    #: face, because Gmsh does not and meshes something else instead.
    corner_points: tuple = ()

    @property
    def nodes(self) -> int:
        """What ``setTransfiniteCurve`` actually wants.

        F-26: the control is worded in elements and Gmsh counts nodes, and the
        runner passed the element count straight in, so a curve asked for ten
        segments came back with nine.
        """
        return self.segments + 1

    def to_dict(self) -> dict:
        return {
            'controlId': self.control_id, 'name': self.name,
            'scopeToken': self.scope_token, 'mode': self.mode,
            'segments': self.segments, 'nodes': self.nodes,
            'law': CURVE_LAW[self.law],
            'coefficient': self.coefficient, 'localSize': self.local_size,
            'reverseGrading': self.reverse_grading,
            'transfiniteSurface': self.transfinite_surface,
            'cornerPoints': list(self.corner_points),
            'order': self.order,
        }


@dataclass(frozen=True)
class VolumeControl:
    control_id: str
    name: str
    scope_token: str
    included: bool
    target_size: float | None
    transfinite: bool
    order: int

    def to_dict(self) -> dict:
        return {
            'controlId': self.control_id, 'name': self.name,
            'scopeToken': self.scope_token,
            'included': self.included, 'targetSize': self.target_size,
            'transfinite': self.transfinite, 'order': self.order,
        }


@dataclass(frozen=True)
class JobIntent:
    """Everything the runner needs, and nothing it has to look up."""

    schema_version: int
    sizing: GlobalSizing
    algorithms: Algorithms
    parallel: Parallel
    healing: Healing
    quality: QualityThresholds
    layers: BoundaryLayers
    size_fields: SizeFieldPlan
    curve_controls: tuple[CurveControl, ...]
    volume_controls: tuple[VolumeControl, ...]
    periodic: PeriodicPlan
    dimensionality: Dimensionality = field(default_factory=Dimensionality)
    farfield: Farfield = field(default_factory=Farfield)
    #: Plan 31 FC-C. Structure asked of the model as a whole.
    structuring: Structuring = field(default_factory=Structuring)
    export: ExportSettings = field(default_factory=lambda: ExportSettings(
        target_solver='unselected', element_order=1,
        second_order_incomplete=False, mesh_format='msh2.2'))
    warnings: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'schemaVersion': self.schema_version,
            'sizing': self.sizing.to_dict(),
            'algorithms': self.algorithms.to_dict(),
            'parallel': self.parallel.to_dict(),
            'healing': self.healing.to_dict(),
            'quality': self.quality.to_dict(),
            'layers': self.layers.to_dict(),
            'sizeFields': self.size_fields.to_dict(),
            'curveControls': [item.to_dict() for item in self.curve_controls],
            'volumeControls': [item.to_dict() for item in self.volume_controls],
            'periodic': self.periodic.to_dict(),
            'dimensionality': self.dimensionality.to_dict(),
            'farfield': self.farfield.to_dict(),
            'structuring': self.structuring.to_dict(),
            'export': self.export.to_dict(),
            'warnings': list(self.warnings),
            'metadata': dict(self.metadata),
        }

    @property
    def digest(self) -> str:
        """Content hash of the job, excluding volatile metadata."""
        document = self.to_dict()
        document.pop('metadata', None)
        encoded = json.dumps(
            document, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()

    def derived_settings_by_task(self) -> dict:
        """The derived values each planned task should carry."""
        return {
            'gmsh.describe_geometry': {
                'healing': self.healing.to_dict(),
                'farfield': self.farfield.to_dict(),
                'dimensionality': self.dimensionality.to_dict()},
            'gmsh.global_sizing': {
                'sizing': self.sizing.to_dict(),
                'algorithms': self.algorithms.to_dict(),
                'parallel': self.parallel.to_dict(),
            },
            'gmsh.size_fields': {'sizeFields': self.size_fields.to_dict()},
            'gmsh.curve_controls': {
                'curveControls': [item.to_dict() for item in self.curve_controls]},
            'gmsh.volume_controls': {
                'volumeControls': [item.to_dict() for item in self.volume_controls],
                'structuring': self.structuring.to_dict()},
            'gmsh.boundary_layers': {'layers': self.layers.to_dict()},
            'gmsh.periodic': {'periodic': self.periodic.to_dict()},
            'gmsh.compute': {
                'quality': self.quality.to_dict(),
                'export': self.export.to_dict(),
                'jobDigest': self.digest,
            },
        }

    def derived_warnings_by_task(self) -> dict:
        """Which task's page each derivation warning belongs on.

        Plan 26 WP2.1. ``warnings`` accumulated correctly from the start and
        rode into the job -- and no view read it, so a clamped size, a dropped
        periodic pair or a thread count the volume algorithm ignores reached
        the user as silence. Attribution mirrors
        :meth:`derived_settings_by_task` so a control and the warning about
        that control land on the same page.

        The thread warning is attributed to Global Sizing rather than to a
        parallel page because that is where both controls live: the algorithm
        and the thread count sit on one page with nothing linking them, which
        is the defect (P4).
        """
        by_task = {
            'gmsh.describe_geometry': list(self.healing.warnings)
            + list(self.dimensionality.warnings),
            'gmsh.global_sizing': list(self.sizing.warnings),
            'gmsh.size_fields': list(self.size_fields.warnings),
            'gmsh.boundary_layers': list(self.layers.warnings),
            'gmsh.periodic': list(self.periodic.warnings),
            'gmsh.compute': list(self.export.warnings),
        }
        # DP-612 (field audit 0924 gmsh-sizing D4): a local-size curve row
        # that "From points" switches off belongs to both pages involved.
        curve_texts = curve_size_warnings(self.curve_controls, self.sizing)
        if curve_texts:
            by_task['gmsh.curve_controls'] = list(curve_texts)
            by_task['gmsh.global_sizing'].extend(curve_texts)
        attributed = {text for texts in by_task.values() for text in texts}
        # Anything raised by the derivation itself rather than by a component
        # -- the threading warning today -- belongs where its controls are.
        by_task['gmsh.global_sizing'].extend(
            text for text in self.warnings if text not in attributed)
        return {task_id: tuple(texts) for task_id, texts in by_task.items()
                if texts}


def configured_tasks(intent) -> frozenset:
    """Which optional Gmsh tasks the configuration in one job asks for.

    DP-228. The recorder used to decide that an optional task was unused from
    the graph state alone. MEASURED on the `workflow-ux-20260915` audit: the
    Boundary layers page was saved with three layers on a named patch, the
    save was refused as locked by prerequisites and reported to the user as
    `Settings saved`, the run then grew 7,914 prisms, and the record read
    `gmsh.boundary_layers: skipped` because the task was still READY. What a
    run used is a property of the job the run consumed, so it is read off the
    job.

    Each test here is the test the runner itself makes. `apply_boundary_layers`
    in `src/resources/gmsh/runner_v1.py` returns before it builds anything
    unless `layers.enabled`; the other four are empty collections, which the
    runner walks zero times. The keys are the ones :meth:`JobIntent.to_dict`
    writes, and the task ids are the ones
    :meth:`JobIntent.derived_settings_by_task` already attributes them to.

    `intent` is whatever the job document carried, which may be nothing at all
    on a job written by an older build, so every lookup is defended: an
    unreadable intent names no task and the recorder falls back on the graph.
    """
    if not isinstance(intent, dict):
        return frozenset()

    def block(key):
        value = intent.get(key)
        return value if isinstance(value, dict) else {}

    asked = {
        'gmsh.size_fields': bool(block('sizeFields').get('fields')),
        'gmsh.curve_controls': bool(intent.get('curveControls')),
        'gmsh.volume_controls': bool(
            intent.get('volumeControls')
            or block('structuring').get('automatic')
            or block('structuring').get('transfiniteTri')),
        'gmsh.boundary_layers': bool(block('layers').get('enabled')),
        'gmsh.periodic': bool(block('periodic').get('pairs')),
    }
    return frozenset(task_id for task_id, used in asked.items() if used)


def _enum(value, default=''):
    if value is None:
        return default
    return str(getattr(value, 'value', value)).split('.')[-1].lower()


def _rows(value):
    if isinstance(value, dict):
        return tuple(
            dict(item, control_id=str(key))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(item, dict))
    if isinstance(value, (list, tuple)):
        return tuple(dict(item) for item in value if isinstance(item, dict))
    return ()


#: Plan 30 WP12. Targets whose reader accepts the quad/hex family Gmsh's
#: recombination produces. Everything else -- the polyMesh publisher above
#: all -- gets tetrahedra, because a recombined mesh fails checkMesh on every
#: geometry measured (see ``GmshCellShape`` in the schema).
RECOMBINING_SOLVERS = frozenset({'su2'})

#: DP-628. Said whenever hexahedra are asked for. It is a caution, not a
#: refusal: the same geometry passed at a finer size.
HEXAHEDRAL_ON_CURVES = (
    'hexahedra are made by subdividing the tetrahedra, and on a curved '
    'surface at a coarse size that can turn a cell inside out: on a box with '
    'a spherical hole at 0.2 mm, one of 5500 hexahedra came out inverted. '
    'checkMesh accepted that mesh, but the quality check refuses an inverted '
    'cell and the refusal cannot be waived. A smaller size on the curved '
    'faces avoided it (0.12 mm passed).')


def derive_structuring(values: dict,
                       target_solver: str = 'unselected') -> Structuring:
    """Whether the run asks Gmsh to structure what it can by itself.

    The SU2-only guard is the same one recombination carries and for the same
    measured reason: ``setTransfiniteAutomatic`` recombines as part of its
    work. On a unit box it produced 125 hexahedra behind 150 quadrilateral
    boundary faces, which is the mesh family the polyMesh route rejects.
    """
    values = dict(values or {})
    automatic = bool(values.get('automatic', False))
    solver = solver_token(target_solver)
    warnings: tuple = ()
    if automatic and solver not in RECOMBINING_SOLVERS:
        automatic = False
        warnings = (
            f'automatic structuring was requested but the {solver} route '
            'reads tetrahedra; it recombines as it structures, and the mesh '
            'it produced on a unit box was 125 hexahedra behind 150 '
            'quadrilateral boundary faces, which is the mesh that fails '
            'checkMesh with negative cell volumes. It was not applied. '
            'Choose the su2 target to mesh in hexahedra.',)
    # No guard on the triangular setting: it changes how a three-sided face
    # is filled, not what family it is filled with. MEASURED: 120 triangles
    # became 64, and both counts are triangles.
    return Structuring(automatic=automatic,
                       transfinite_tri=bool(values.get('transfiniteTri', False)),
                       warnings=warnings)


def derive_algorithms(values: dict,
                      target_solver: str = 'unselected',
                      *, automatic_structuring: bool = False) -> Algorithms:
    values = dict(values or {})
    surface = _enum(values.get('surface'), 'frontal_delaunay')
    volume = _enum(values.get('volume'), 'delaunay')
    cell_shape = _enum(values.get('cellShape'), 'tetrahedral')
    if surface not in SURFACE_ALGORITHM:
        raise PlanDerivationError(
            f'unknown surface algorithm {surface!r}; expected one of '
            f'{", ".join(sorted(SURFACE_ALGORITHM))}')
    if volume not in VOLUME_ALGORITHM:
        raise PlanDerivationError(
            f'unknown volume algorithm {volume!r}; expected one of '
            f'{", ".join(sorted(VOLUME_ALGORITHM))}')
    if cell_shape not in CELL_SHAPE:
        raise PlanDerivationError(
            f'unknown cell shape {cell_shape!r}; expected one of '
            f'{", ".join(sorted(CELL_SHAPE))}')
    solver = solver_token(target_solver)
    recombine = bool(values.get('recombine', False))
    recombiner = _enum(values.get('recombinationAlgorithm'), 'blossom')
    if recombiner not in RECOMBINATION_ALGORITHM:
        raise PlanDerivationError(
            f'unknown recombination algorithm {recombiner!r}; expected one of '
            f'{", ".join(sorted(RECOMBINATION_ALGORITHM))}')
    split = bool(values.get('splitQuadrangles', False))
    warnings: tuple = ()
    if cell_shape == 'hexahedral':
        # DP-628 (field audit 0924 gmsh-generate-export D5). See
        # GmshCellShape: subdivision can invert a cell on a curved face.
        warnings += (HEXAHEDRAL_ON_CURVES,)
    if split and not recombine:
        # A control that changes nothing must say so rather than sit on the
        # page looking as though it did something.
        split = False
        warnings += (
            'splitting quadrangles back into triangles was requested but '
            'nothing recombines them in the first place; the surfaces are '
            'meshed as triangles already, so the setting was not applied.',)
    elif recombine and solver not in RECOMBINING_SOLVERS and not split:
        # Refused here rather than in the runner so the reason reaches the
        # page while the user can still act on it -- and the reason now names
        # the remedy, because there is one.
        recombine = False
        warnings += (
            f'recombination was requested but the {solver} route reads '
            'tetrahedra; the recombined mesh fails checkMesh with negative '
            'cell volumes, so it was not applied. Choose the su2 target to '
            'mesh in quads and hexahedra, or split the quadrangles back into '
            'triangles to keep the recombination on this route.',)
    elif recombine and split and solver not in RECOMBINING_SOLVERS:
        # Kept, not cleared: the quadrangles never survive to the volume
        # pass, so the mesh this route publishes is the tetrahedral one it
        # reads.
        warnings += (
            f'the surfaces are recombined and then split back into triangles, '
            f'because the {solver} route reads tetrahedra; the mesh is '
            'tetrahedral, and it is the recombiner rather than the surface '
            'mesher that decided where its triangles are.',)
    # Plan 31 FC-B. The quad surfacers go through the same gate, and for the
    # same measured reason: a quadrilateral surface is not a mesh the polyMesh
    # route can read. Cleared here, not in the runner, so the user is told
    # while they can still act on it. `splitQuadrangles` does not rescue one:
    # the runner splits only what recombination made, and whether the split
    # also reaches a quad surfacer's own quadrangles is unmeasured.
    if surface in QUAD_SURFACE_ALGORITHMS and solver not in RECOMBINING_SOLVERS:
        warnings += (
            f'the {surface} surface algorithm meshes in quadrilaterals, which '
            f'the {solver} route cannot read; frontal_delaunay was used '
            'instead. Choose the su2 target to mesh the surfaces in quads.',)
        surface = 'frontal_delaunay'
    return Algorithms(surface=surface, volume=volume, cell_shape=cell_shape,
                      recombine=recombine, recombination_algorithm=recombiner,
                      fallback=bool(values.get('algorithmFallback', True)),
                      split_quadrangles=split,
                      automatic_structuring=bool(automatic_structuring),
                      warnings=warnings)


#: Plan 31 CP-08 item 7. The three answers to "the cut left a pocket inside
#: the geometry", kept here so the page, the job and the runner cannot each
#: invent their own spelling.
SEALED_CAVITY_POLICIES = ('discard', 'keep', 'refuse')


def derive_farfield(values: dict) -> Farfield:
    values = dict(values or {})
    enabled = bool(values.get('enabled', False))
    padding = float(values.get('padding', 2.0) or 0.0)
    if enabled and padding <= 0:
        raise PlanDerivationError(
            'the farfield padding must be greater than zero; a box the size '
            'of the geometry has no room for flow around it')
    policy = _enum(values.get('sealedCavities'), 'discard')
    if policy not in SEALED_CAVITY_POLICIES:
        raise PlanDerivationError(
            f'unknown sealed-cavity policy {policy!r}; expected one of '
            + ', '.join(SEALED_CAVITY_POLICIES))
    return Farfield(enabled=enabled, padding=padding, sealed_cavities=policy)


def resolve_parallel_threads(resource_policy, *, requested: int = 0) -> int:
    """How many threads the execution policy gives this Gmsh run.

    Plan 33 SETUP-03 / DP-X2. The runner reads ``intent.parallel.threads`` into
    ``General.NumThreads``, and that number came from ``gmsh/parallel/threads``
    alone -- a schema default of 1 that no control in the application writes.
    MEASURED: every Gmsh mesh started from the window ran on one thread, on a
    machine with sixteen, with the core control on the page reading whatever
    the user had set. The count is asked of the shared policy function so that
    the page, the plan and the job cannot answer differently.
    """
    from foammesh.core.execution.resources import effective_cpu_count

    return max(1, min(64, int(effective_cpu_count(
        resource_policy, requested=max(0, int(requested or 0))))))


def derive_parallel(values: dict, algorithms: Algorithms | None = None,
                    resource_policy=None) -> Parallel:
    # Plan 29 WP8. The fallback is the shipped schema default, not a number
    # chosen here: this line used to say 4 while the schema said 1 and the page
    # said 1, so a project that never touched the control derived a job nobody
    # had asked for.
    default = int(_schema_default('gmsh', 'parallel', 'threads', fallback=1) or 1)
    threads = int(dict(values or {}).get('threads', default) or default)
    if resource_policy is not None:
        # A saved thread count above the default is a request and is clamped;
        # the default is nobody having asked, and then the execution policy
        # decides. Either way one function answers.
        threads = resolve_parallel_threads(
            resource_policy, requested=threads if threads > 1 else 0)
    if not 1 <= threads <= 64:
        raise PlanDerivationError(f'threads {threads} is outside 1-64')
    volume_threaded = (algorithms is None
                       or algorithms.volume in THREADED_VOLUME_ALGORITHMS)
    return Parallel(threads=threads, volume_threaded=volume_threaded)


def derive_export(values: dict, target_solver: str = 'unselected'
                  ) -> ExportSettings:
    """Element order and output format for the case's target solver.

    Plan 29 WP8, corrected by Plan 30 WP-07 and Plan 31 CP-01. The order comes
    from :func:`supported_element_orders` rather than from an ``if`` written
    here, so the page that greys an option and the job that would have dropped
    it read one pair of tables.

    The format follows from the target, not from the order: an SU2 case is
    given Gmsh's own ``.su2`` writer, because that file is what the solver
    consumes and the run has no other way to be told to write it
    (``write_job`` adds ``su2`` to the requested formats when it sees this
    value). The native ``.msh`` is written beside it either way, at the one
    version this application reads.
    """
    values = dict(values or {})
    solver = solver_token(target_solver)
    try:
        requested = int(values.get('elementOrder', 1) or 1)
    except (TypeError, ValueError):
        raise PlanDerivationError(
            f'element order {values.get("elementOrder")!r} is not a number')
    if requested not in (1, 2):
        raise PlanDerivationError(
            f'element order {requested} is outside 1-2; Gmsh writes linear or '
            'quadratic elements only')
    incomplete = bool(values.get('secondOrderIncomplete', False))
    straight_midsides = bool(values.get('secondOrderLinear', False))
    renumber = bool(values.get('renumber', False))

    warnings: list[str] = []
    allowed = supported_element_orders(solver)
    if requested in allowed:
        order = requested
    else:
        order = max(item for item in allowed if item <= requested) \
            if any(item <= requested for item in allowed) else min(allowed)
        refusal = ELEMENT_ORDER_REFUSALS.get((solver, requested), '')
        warnings.append(
            f'element order {requested} was requested but the mesh was '
            f'written at order {order}: '
            + (refusal or f'the {solver} route accepts order '
                          + ' or '.join(str(item) for item in allowed)
                          + ' only.'))
    mesh_format = 'su2' if solver == 'su2' else 'msh2.2'
    if order != 2:
        incomplete = False
        straight_midsides = False
    # Plan 31 FC-D. Renumbering is an SU2-route option because MSH 2.2 is the
    # only other file this product writes and that writer does not carry it.
    # MEASURED on a 406-node sphere, one mesh renumbered and written three
    # ways: the widest node-number spread over a cell was 458 before, and
    # after RCMK it was 129 in the .su2 and in MSH 4.1 -- and 458 again in
    # MSH 2.2, whose writer indexes nodes in storage order and throws the new
    # numbering away. Offering the control on a route whose file cannot carry
    # it is the shape of defect C31-02, so it is cleared here with a reason
    # rather than set, recorded, and silently lost at the write.
    if renumber and mesh_format != 'su2':
        renumber = False
        warnings.append(
            'node renumbering was requested but not applied: this route '
            'writes MSH 2.2, and that writer numbers nodes in storage order, '
            'so a renumbering cannot reach the file. It applies on the SU2 '
            'route, whose writer carries it.')
    return ExportSettings(target_solver=solver, element_order=order,
                          second_order_incomplete=incomplete,
                          second_order_linear=straight_midsides,
                          renumber=renumber,
                          mesh_format=mesh_format, warnings=tuple(warnings))


def derive_corner_points(raw, name: str) -> tuple:
    """The corner tags a curve control names, as integers.

    Plan 31 FC-C. The field is free text because a corner is a point tag and
    there is nothing else to pick it with yet; everything after this point in
    the pipeline sees a tuple of integers or nothing at all.

    Three or four tags, because that is what ``setTransfiniteSurface`` takes.
    Anything else is refused here rather than in the runner, so the user reads
    it while they can still fix it.
    """
    if raw in (None, '', ()):
        return ()
    if isinstance(raw, (list, tuple)):
        tokens = [str(item).strip() for item in raw]
    else:
        tokens = [item.strip() for item in
                  str(raw).replace(';', ',').replace(' ', ',').split(',')]
    tokens = [item for item in tokens if item]
    if not tokens:
        return ()
    corners = []
    for token in tokens:
        try:
            corners.append(int(token))
        except (TypeError, ValueError):
            raise PlanDerivationError(
                f'curve control {name!r} names {token!r} as a corner, and a '
                'corner is a point tag, which is a whole number')
    if len(set(corners)) != len(corners):
        raise PlanDerivationError(
            f'curve control {name!r} names the same corner twice; a '
            'transfinite surface interpolates between distinct corners')
    if len(corners) not in (3, 4):
        raise PlanDerivationError(
            f'curve control {name!r} names '
            f'{count_text(len(corners), "corner")}; a '
            'transfinite surface interpolates between three or four')
    return tuple(corners)


def curve_size_warnings(curves, sizing) -> tuple[str, ...]:
    """DP-612 (field audit 0924 gmsh-sizing D4). Local sizes nobody will see.

    A curve row in local-size mode sets a size on its end points, and Gmsh
    reads point sizes only while Mesh.MeshSizeFromPoints is on. With "From
    points" unticked the row does nothing, so the runner skips it; this says
    so before the job runs.
    """
    if sizing.from_points:
        return ()
    return tuple(
        f'curve control {curve.name!r} sets a local size, which Gmsh reads '
        'only while "From points" is ticked on the Global sizing page; it is '
        'off, so the row will be skipped'
        for curve in curves if curve.mode == 'size')


def derive_curve_controls(rows) -> tuple[CurveControl, ...]:
    derived = []
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        if not bool(row.get('enabled', True)):
            continue
        mode = _enum(row.get('mode'), 'transfinite')
        if mode == 'none':
            continue
        if mode not in {'transfinite', 'size'}:
            raise PlanDerivationError(f'unknown curve mode {mode!r}')
        law = _enum(row.get('law'), 'progression')
        if law not in CURVE_LAW:
            raise PlanDerivationError(
                f'unknown transfinite law {law!r}; expected one of '
                f'{", ".join(sorted(CURVE_LAW))}')
        control_id = str(row.get('control_id') or row.get('controlId') or index)
        name = str(row.get('name') or f'curve-{control_id}')
        scope = str(row.get('scopeToken') or row.get('scope_token') or '').strip()
        if not scope:
            raise PlanDerivationError(
                f'curve control {name!r} needs a prepared geometry scope')
        segments = int(row.get('segments', 10) or 0)
        if mode == 'transfinite' and segments < 1:
            raise PlanDerivationError(
                f'curve control {name!r} needs at least one segment')
        local = float(row.get('localSize', 0.0) or 0.0)
        if mode == 'size' and local <= 0:
            raise PlanDerivationError(
                f'curve control {name!r} sets a local size, so it must be positive')
        corners = derive_corner_points(
            row.get('cornerPoints', row.get('corner_points')), name)
        structured_surface = (mode == 'transfinite' and bool(
            row.get('transfiniteSurface',
                    row.get('transfinite_surface', False))))
        if corners and not structured_surface:
            raise PlanDerivationError(
                f'curve control {name!r} names corner points, which say how '
                'to interpolate a structured surface, but does not ask for '
                'one; tick the structured surface or clear the corners')
        derived.append(CurveControl(
            control_id=control_id, name=name, scope_token=scope, mode=mode,
            segments=segments, law=law,
            coefficient=float(row.get('coefficient', 1.0) or 1.0),
            local_size=local, order=int(row.get('priority', 0) or 0),
            reverse_grading=bool(row.get('reverseGrading',
                                         row.get('reverse_grading', False))),
            transfinite_surface=structured_surface,
            corner_points=corners))
    return tuple(sorted(derived, key=lambda item: (-item.order, item.name)))


def derive_volume_controls(rows) -> tuple[VolumeControl, ...]:
    derived = []
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        if not bool(row.get('enabled', True)):
            continue
        control_id = str(row.get('control_id') or row.get('controlId') or index)
        name = str(row.get('name') or f'volume-{control_id}')
        scope = str(row.get('scopeToken') or row.get('scope_token') or '').strip()
        if not scope:
            raise PlanDerivationError(
                f'volume control {name!r} needs a prepared geometry scope')
        raw_size = row.get('targetSize')
        target = None if raw_size in (None, '') else float(raw_size)
        if target is not None and target <= 0:
            raise PlanDerivationError(
                f'volume control {name!r} needs a positive target size')
        derived.append(VolumeControl(
            control_id=control_id, name=name, scope_token=scope,
            included=bool(row.get('included', True)),
            target_size=target, transfinite=bool(row.get('transfinite', False)),
            order=int(row.get('priority', 0) or 0)))
    return tuple(sorted(derived, key=lambda item: (-item.order, item.name)))


def derive_job_intent(request) -> JobIntent:
    """Derive the immutable job from an :class:`EnginePlanRequest`."""
    carrier = dict(request.intent.native or {})
    native = dict(carrier.get('gmsh') or {})
    return derive_from_native(
        native, bbox=getattr(request, 'bbox', None),
        # Plan 28's key rides along with the native section rather than being
        # read from a database here: the plan request is the only thing this
        # function is given, and element order depends on the target solver.
        target_solver=carrier.get('targetSolver') or 'unselected',
        metadata={
            'engine_id': 'gmsh',
            'configuration_revision': getattr(
                request, 'configuration_revision', None),
        },
        # Plan 33 DP-X2. The policy reached the plan document and stopped
        # there; the thread count the plan reports is now the one the job
        # carries, because both are derived from it here.
        resource_policy=dict(getattr(request, 'resource_policy', None) or {})
        or None)


def derive_from_native(native: dict, *, bbox=None, metadata=None,
                       target_solver='unselected',
                       prepared_geometry=None,
                       resource_policy=None) -> JobIntent:
    """Derive from the raw ``gmsh`` configuration section.

    Split out from :func:`derive_job_intent` so the derivation can be unit
    tested per control without constructing a whole plan request.

    ``target_solver`` is the Plan 28 key. It reaches the derivation rather than
    the runner because the element order it gates has to be settled while the
    user can still see the reason.

    Plan 33 W-G1. ``prepared_geometry`` is the revision the job is being
    written against. Per-surface size rows name a prepared boundary, and the
    Gmsh tag that boundary holds is a property of the revision, so it is
    resolved here rather than saved.
    """
    native = dict(native or {})
    dimensionality = derive_dimensionality(native.get('dimensionality'))
    sizing = derive_global_sizing(native.get('globalSizing'), bbox)
    structuring = derive_structuring(native.get('structuring'), target_solver)
    algorithms = derive_algorithms(
        native.get('algorithms'), target_solver,
        automatic_structuring=structuring.automatic)
    parallel = derive_parallel(native.get('parallel'), algorithms,
                               resource_policy=resource_policy)
    healing = derive_healing(native.get('healing'))
    quality = derive_thresholds(native.get('optimization'))
    layers = derive_boundary_layers(
        native.get('boundaryLayers'), target_size=sizing.target_size)
    size_fields = derive_size_fields(_rows(native.get('sizeFields')), bbox)
    # Per-surface sizes are ordinary size fields once compiled, and they have
    # to join this plan rather than travel beside it: Gmsh keeps one background
    # field, so a second plan would mean a second background field replacing
    # the first.
    size_fields = size_fields.merged_with(derive_surface_sizes(
        _rows(native.get('surfaceSizes')), target_size=sizing.target_size,
        prepared_geometry=prepared_geometry))
    # Plan 31 CP-08 item 1. The wiring is checked here, on the merged plan,
    # because that is what reaches Gmsh: a reference to a field the plan does
    # not carry, or two fields reading each other, is a refusal now rather
    # than a mesh nobody asked for.
    validate_field_graph(size_fields.fields)
    export = derive_export(native.get('output'), target_solver)
    curves = derive_curve_controls(_rows(native.get('curveControls')))
    volumes = derive_volume_controls(_rows(native.get('volumeControls')))
    periodic = derive_periodic_pairs(_rows(native.get('periodicPairs')))
    farfield = derive_farfield(native.get('farfield'))

    if dimensionality.planar:
        # FC-E. Two controls in this job are about a volume Gmsh will not
        # mesh on this route, and a job that carried them anyway would reach
        # the runner asking for a boundary layer on a model with no volumes.
        # Cleared here, where the reason can still be read, rather than
        # silently ignored there. The expected volume families are left as
        # they are: they say what the cell shape owes, and the runner reads
        # the dimensionality when it decides whether that debt is owed.
        if layers.enabled:
            layers = derive_boundary_layers(None,
                                            target_size=sizing.target_size)
            dimensionality = replace(dimensionality, warnings=(
                *dimensionality.warnings,
                'boundary layers are not grown on a two-dimensional section: '
                'Gmsh extrudes a layer off a surface into a volume, and this '
                'route meshes the surface itself. The layer request was '
                'dropped.'))
        if farfield.enabled:
            farfield = Farfield()
            dimensionality = replace(dimensionality, warnings=(
                *dimensionality.warnings,
                'the farfield box is a solid cut against imported solids, so '
                'it cannot be built for a two-dimensional section. The '
                'farfield request was dropped.'))

    warnings = (
        *sizing.warnings, *healing.warnings, *layers.warnings,
        *size_fields.warnings, *periodic.warnings, *export.warnings,
        *algorithms.warnings, *dimensionality.warnings,
        *structuring.warnings, *curve_size_warnings(curves, sizing),
    )
    if parallel.threads > 1 and not parallel.volume_threaded:
        # Silence here reads as "sixteen threads were used". They were not.
        warnings = (*warnings, (
            f'the {algorithms.volume} volume algorithm is single-threaded, so '
            f'the requested {parallel.threads} threads apply to surface '
            f'meshing only; choose hxt for a threaded volume pass'))
    return JobIntent(
        schema_version=JOB_SCHEMA_VERSION, sizing=sizing, algorithms=algorithms,
        parallel=parallel, healing=healing, quality=quality, layers=layers,
        size_fields=size_fields, curve_controls=curves, volume_controls=volumes,
        periodic=periodic, dimensionality=dimensionality,
        farfield=farfield, structuring=structuring,
        export=export, warnings=warnings,
        metadata=dict(metadata or {}))
