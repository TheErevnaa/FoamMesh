#!/usr/bin/env python
# -*- coding: utf-8 -*-


from enum import Enum, auto, IntEnum

from foammesh.support.simple_db.simple_schema import FloatType, IdReference, IntKeyList, EnumType, IntType, TextType, BoolType, VectorComposite


CURRENT_CONFIGURATIONS_VERSION = 14
CONFIGURATIONS_VERSION_KEY = 'version'


class Step(IntEnum):
    NONE = -1

    GEOMETRY = 0
    GEOMETRY_REPAIR = auto()
    REGION = auto()
    BASE_GRID = auto()
    CASTELLATION = auto()
    SNAP = auto()
    BOUNDARY_LAYER = auto()
    EXPORT = auto()

    LAST_STEP = EXPORT


class MeshEngine(Enum):
    """Persisted meshing-method choice.

    New projects remain unselected until the user explicitly chooses an
    engine.
    """

    UNSELECTED = 'unselected'
    SNAPPY = 'snappy'
    GMSH = 'gmsh'


class TargetSolver(Enum):
    """The solver this mesh is being built for.

    Plan 28. The choice is not cosmetic: SU2 reads tetrahedra, hexahedra,
    prisms and pyramids and nothing else, so an engine that emits polyhedral
    cells cannot serve it. Which engines are offered, which quality check runs
    and which export format is the default all follow from this one key.

    Existing projects predate the key and read UNSELECTED, which keeps the
    permissive pre-Plan-28 behaviour: everything is offered, nothing is
    refused.
    """

    UNSELECTED = 'unselected'
    OPENFOAM = 'openfoam'
    SU2 = 'su2'


class ExecutionMode(Enum):
    AUTO = 'auto'
    SERIAL = 'serial'
    PARALLEL = 'parallel'


class BoundaryCategory(Enum):
    """Engine-neutral boundary classification carried by prepared geometry.

    Authored before a mesher is chosen and consumed by every engine's
    publication step to give published patches real OpenFOAM types.
    """

    WALL = 'wall'
    INLET = 'inlet'
    OUTLET = 'outlet'
    SYMMETRY = 'symmetry'
    # Plan 30 WP12. An axisymmetric wedge: the two planes of a one-cell-thick
    # sector. OpenFOAM has a distinct patch type for it -- `wedge` applies the
    # axisymmetric transform, `symmetryPlane` does not -- and without this the
    # only reachable answer published a wedge case as two symmetry planes,
    # which solves a different problem quietly.
    WEDGE = 'wedge'
    FAR_FIELD = 'far_field'
    INTERFACE = 'interface'
    UNCLASSIFIED = 'unclassified'


class InterfaceCoupling(Enum):
    """Engine-neutral geometry connector authored before mesher selection."""

    CONFORMAL = 'conformal'
    CYCLIC = 'cyclic'
    NON_CONFORMAL = 'non_conformal'


class InterfaceTransform(Enum):
    COINCIDENT = 'coincident'
    TRANSLATIONAL = 'translational'
    ROTATIONAL = 'rotational'


class GmshSurfaceAlgorithm(Enum):
    """Gmsh ``Mesh.Algorithm`` values, by name rather than magic number.

    Plan 31 FC-B. The last two are the quad surfacers, and they are gated the
    way recombination is: offered for an SU2 target and cleared with a warning
    for every other one, because a quadrilateral surface is what the OpenFOAM
    route refuses. MEASURED on the catalogue rather than assumed -- see
    ``plans/evidence/plan31/fcb-algorithms/surface.json`` for the element
    families each one wrote.
    """

    MESHADAPT = 'meshadapt'
    AUTOMATIC = 'automatic'
    DELAUNAY = 'delaunay'
    FRONTAL_DELAUNAY = 'frontal_delaunay'
    PACKING_PARALLELOGRAMS = 'packing_parallelograms'
    #: ``Mesh.Algorithm`` 8. Triangles laid out for recombination.
    FRONTAL_DELAUNAY_QUADS = 'frontal_delaunay_quads'
    #: ``Mesh.Algorithm`` 11. Writes quadrilaterals on its own.
    QUASI_STRUCTURED_QUAD = 'quasi_structured_quad'


class GmshVolumeAlgorithm(Enum):
    """Gmsh ``Mesh.Algorithm3D`` values.

    Measured over the fifteen-geometry catalogue: Delaunay reaches a median
    maximum non-orthogonality of 50.1 against HXT's 51.9, and HXT is roughly
    twice as fast and is the threaded one. Quality is the gate, so Delaunay is
    the default and HXT is offered for speed.

    Plan 31 FC-B added MMG3D on the same gate, and only because it won on it.
    Run across the whole catalogue twice -- once at the common target size,
    then again with Delaunay and HXT re-run at the length scale that gives
    them MMG3D's cell count, because the first sweep bought MMG3D's angles
    with roughly twice the cells -- it came out ahead at equal size on every
    geometry that meshed: median maximum non-orthogonality 45.0 against
    Delaunay's 49.2 and HXT's 50.3, median maximum skewness 0.484 against
    0.586 and 0.595, and the worst case in the catalogue 59.4 against 70.6.
    It is not the default, for two measured reasons: it is roughly twice
    Delaunay's wall clock and three times HXT's, and it does not always
    deliver -- of the sixteen it failed outright on ``annulus_shell``
    ("Mmg3d: unable to set mesh size") and on ``box_with_cavity`` produced a
    mesh the polyMesh publisher rejects as non-manifold. The tables are in
    ``plans/evidence/plan31/fcb-algorithms/``.
    """

    DELAUNAY = 'delaunay'
    FRONTAL = 'frontal'
    HXT = 'hxt'
    #: ``Mesh.Algorithm3D`` 7. Best angles measured, at twice the wall clock,
    #: and it refuses some geometry the other two mesh.
    MMG3D = 'mmg3d'


class GmshSealedCavityPolicy(Enum):
    """What to do with a volume the farfield cut leaves inside the geometry.

    Plan 31 CP-08 item 7. A cut against a solid with internal voids leaves
    the external domain and one volume per void. MEASURED on the six
    assembly fixtures at padding 1.5: every one leaves at least one, and the
    drone quadcopter leaves five. Discarding them is right for external
    flow -- the solver would otherwise be handed a region no inlet reaches --
    and wrong for a case about the internal passages, so which it is has to
    be asked rather than assumed.
    """

    #: Mesh the external domain alone. The pockets are named in a warning.
    DISCARD = 'discard'
    #: Mesh the pockets too, as regions disconnected from the domain.
    KEEP = 'keep'
    #: Stop, and say what was found, so the geometry can be looked at.
    REFUSE = 'refuse'


class GmshRecombinationAlgorithm(Enum):
    """How Gmsh turns the surface triangles into quadrangles.

    ``Mesh.RecombinationAlgorithm``, and the four here are the four that were
    MEASURED to produce a mesh. On duct.step at a 0.04 m target with
    ``Mesh.RecombineAll`` on: simple (0) gave 306 quadrangles and 70 leftover
    triangles; blossom (1) gave 356 quadrangles and no triangles; simple
    full-quad (2) gave 460; blossom full-quad (3) gave 408. Four values, four
    different meshes -- so the choice is real, and blossom is the default
    because it is the one that left no triangles behind. It is also the value
    the runner hard-coded before this control existed.

    Values outside the four are not offered because the option accepts them
    and the mesh does not survive: 4 read back as 4 and then ``generate(3)``
    produced **no elements at all**, surface and volume both empty, without
    raising; 9 was clamped back to 0 in silence. Reading the option back is
    not evidence that a mesh exists.

    Note what the volume was in every one of those runs: pyramids and
    tetrahedra, never hexahedra. Recombination gives a quadrangle *surface*
    mesh, and the pyramids are how Gmsh closes those quads onto a tetrahedral
    interior. That is the mesh SU2 reads. It is not a hex mesh, and
    :class:`GmshCellShape` is where hexahedra come from.
    """

    SIMPLE = 'simple'
    BLOSSOM = 'blossom'
    SIMPLE_FULL_QUAD = 'simple_full_quad'
    BLOSSOM_FULL_QUAD = 'blossom_full_quad'


class GmshCellShape(Enum):
    """What the volume mesh is made of.

    ``hexahedral`` subdivides the tetrahedral mesh into hexahedra
    (``Mesh.SubdivisionAlgorithm``). Measured on four geometries: all pass
    checkMesh, at roughly four times the cell count and higher
    non-orthogonality than tetrahedra.

    Gmsh's 3D *recombination* (``Mesh.RecombineAll``) is deliberately absent.
    It produces tet/pyramid meshes that Gmsh reports as sound -- zero inverted
    elements -- and OpenFOAM rejects: negative cell volumes, open cells and
    non-orthogonality above 145 degrees on every geometry tried, and one that
    would not publish at all.

    Plan 31 CP-08 item 3. Gmsh's other two subdivisions are absent for a
    related reason, MEASURED on duct.step: ``Mesh.SubdivisionAlgorithm`` 1,
    whose Gmsh name is "all quadrangles", left the mesh exactly as it found it
    -- 682 triangles and 1044 tetrahedra, the same numbers as no subdivision
    at all, and no error. 3 (barycentric) split every tetrahedron into four,
    which is a refinement rather than a change of family. Only 0 and 2 do what
    their names say, so only 0 and 2 are reachable, and the runner refuses any
    other code rather than meshing something the user did not ask for.
    """

    TETRAHEDRAL = 'tetrahedral'
    HEXAHEDRAL = 'hexahedral'


class DecompositionMethod(Enum):
    """How ``decomposePar`` splits the mesh across ranks.

    Plan 26 WP9.2 exposed three of the nine ``decomposeParDict`` accepts.
    ``scotch`` is the shipped default and needs no coefficients;
    ``hierarchical`` and ``simple`` are offered because they are the ones a
    user can *steer* -- aligning the cuts with the geometry is the direct
    remedy for a decomposition-sensitive refinement.

    Plan 31 CP-07 item 4 adds the two that were withheld for the right reason
    stated the wrong way round. ``metis`` and ``kahip`` really are absent from
    some OpenFOAM builds, but omitting them everywhere meant a user with a
    build that *has* them could not reach them, and a user without them was
    told nothing. They are now declared here and answered against the selected
    runtime: present and loadable, the page offers them; missing, or shipped
    only as OpenFOAM's ``lib/dummy`` stub, the page shows the row disabled
    with the reason. The remaining four stay out because this product cannot
    write a complete dictionary for them at all -- ``manual`` needs a per-cell
    file, ``structured`` a patch list, ``multiLevel`` a per-level dictionary,
    and ``none`` is what running serially already does. See
    :data:`foammesh.openfoam.decomposition.REGISTRY`, which is where all nine
    are declared once.
    """

    SCOTCH = 'scotch'
    HIERARCHICAL = 'hierarchical'
    SIMPLE = 'simple'
    METIS = 'metis'
    KAHIP = 'kahip'


class GmshQualityMeasure(Enum):
    """Gmsh ``Mesh.QualityType``."""

    SICN = 'sicn'
    SIGE = 'sige'
    GAMMA = 'gamma'
    DISTO = 'disto'


class GmshSizeFieldType(Enum):
    DISTANCE_THRESHOLD = 'distance_threshold'
    BOX = 'box'
    BALL = 'ball'
    CYLINDER = 'cylinder'
    FRUSTUM = 'frustum'
    #: An arbitrary expression in x, y and z. The most expressive refinement
    #: Gmsh offers, and the easiest to misuse: a mistyped exponent asks for a
    #: mesh nobody wants, so the expression is validated and costed first.
    MATH_EVAL = 'math_eval'
    #: Plan 30 WP12. A size held inside the entities the scope names and
    #: nowhere else. Gmsh's `Restrict` wraps another field and clips it to a
    #: list of surfaces or volumes; the row supplies a constant inside size,
    #: which is what makes it a local override rather than a ramp.
    RESTRICT = 'restrict'
    #: Plan 30 WP12. Curvature of the distance to the scoped surfaces, mapped
    #: to a size. `Mesh.MeshSizeFromCurvature` does this globally; this does
    #: it for one face group, with its own range.
    CURVATURE = 'curvature'


class GmshFieldCombiner(Enum):
    """How the size fields are folded into one background field."""

    MIN = 'min'
    MAX = 'max'


class GmshMeshDimension(Enum):
    """How many dimensions the case is meshed in.

    FC-E. Gmsh meshes a planar section with ``generate(2)`` and OpenFOAM has
    no two-dimensional mesh format, so the two other values here are not
    "mesh in 2D" -- they are "mesh the section, then extrude it into the one
    cell of thickness OpenFOAM reads as two-dimensional".

    ``two_d`` translates the section along the coordinate direction it is flat
    in and types the two new faces ``empty``. ``axisymmetric`` revolves it
    about an in-plane coordinate axis, half the angle each way, and types them
    ``wedge``; the halving is not cosmetic, it is what puts the centre plane
    on a coordinate plane, which ``wedgePolyPatch`` requires.
    """

    THREE_D = 'three_d'
    TWO_D = 'two_d'
    AXISYMMETRIC = 'axisymmetric'


class GmshWedgeAxis(Enum):
    """The coordinate axis an axisymmetric section is revolved about."""

    X = 'x'
    Y = 'y'
    Z = 'z'


class GmshCurveMode(Enum):
    NONE = 'none'
    TRANSFINITE = 'transfinite'
    SIZE = 'size'


class GmshTransfiniteLaw(Enum):
    PROGRESSION = 'progression'
    BUMP = 'bump'
    BETA = 'beta'


class GmshLayerMode(Enum):
    """How the boundary-layer stack is specified."""

    NONE = 'none'
    FIRST_AND_RATIO = 'first_and_ratio'
    TOTAL_AND_COUNT = 'total_and_count'


class GmshPeriodicTransform(Enum):
    TRANSLATION = 'translation'
    ROTATION = 'rotation'


class GeometryPreparationDecision(Enum):
    UNDECIDED = 'undecided'
    AS_IS = 'as_is'
    REPAIRED = 'repaired'
    WRAPPED = 'wrapped'
    OVERRIDDEN = 'overridden'


class BaseGridSizingMode(Enum):
    COUNTS = 'counts'
    TARGET_SIZE = 'target_size'


class RefinementRegionMode(Enum):
    """The ``mode`` of one ``refinementRegions`` entry in Foundation 13.

    Read off ``refinementRegions.C``: ``refineModeNames_`` is exactly
    ``(inside outside distance insideSpan outsideSpan)``, and each mode reads a
    different level spelling.  ``inside``/``outside`` read a plain
    ``level <label>``; ``distance`` reads ``levels ((dist lvl) ...)`` in order
    of increasing distance and non-increasing level; ``insideSpan`` and
    ``outsideSpan`` read a *single* ``level (dist lvl)`` pair plus
    ``cellsAcrossSpan``, and require a closeness point field the mesher opens
    ``MUST_READ``.
    """
    INSIDE = 'inside'
    OUTSIDE = 'outside'
    DISTANCE = 'distance'
    #: Refine where the surface's *internal* span is thinner than the span the
    #: requested cell count needs. Foundation 13 only accepts this on a
    #: ``triSurface`` geometry entry (``refinementRegions.C:566-616``).
    INSIDE_SPAN = 'insideSpan'
    #: The same, measured across the *external* gap between two surfaces.
    OUTSIDE_SPAN = 'outsideSpan'

    @property
    def isSpan(self) -> bool:
        return self in (RefinementRegionMode.INSIDE_SPAN,
                        RefinementRegionMode.OUTSIDE_SPAN)


class GeometryType(Enum):
    SURFACE = 'surface'
    VOLUME = 'volume'


class Shape(Enum):
    TRI_SURFACE_MESH = 'triSurfaceMesh'
    HEX = 'hex'
    CYLINDER = 'cylinder'
    SPHERE = 'sphere'
    HEX6 = 'hex6'
    # Plan 31. The three open (non-closed) searchable surfaces OpenFOAM 13
    # offers. Their values are the v13 run-time selection names exactly, so
    # the writer emits the member value and nothing has to translate.
    # MEASURED on OpenFOAM 13:
    #   plane -- plane_searchableSurface.H, keys planeType/point/normal,
    #            read through Foam::plane(dict) at plane.C:123-146.
    #   disk  -- disk_searchableSurface.C:179-181, keys origin/normal/radius.
    #   plate -- plate_searchableSurface.C:257-258, keys origin/span, span
    #            requiring exactly one zero component (the plate normal).
    # None of the three answers inside/outside queries -- they have no
    # hasVolumeType() override, so the base searchableSurface false stands --
    # which is why the writer routes them to refinementSurfaces and never to
    # refinementRegions (refinementRegions.C:59,117 warns and drops a shell
    # whose surface has no volume type).
    PLANE = 'plane'
    DISK = 'disk'
    PLATE = 'plate'
    X_MIN = 'xMin'
    X_MAX = 'xMax'
    Y_MIN = 'yMin'
    Y_MAX = 'yMax'
    Z_MIN = 'zMin'
    Z_MAX = 'zMax'

    PLATES = [X_MIN, X_MAX, Y_MIN, Y_MAX, Z_MIN, Z_MAX]

    #: The primitives written as a plain, open searchable surface. They can be
    #: refined against and snapped to, but they enclose nothing.
    OPEN_PRIMITIVES = [PLANE, DISK, PLATE]


class TriSurfaceDeclaration(Enum):
    """Which searchable-surface class a staged tri-surface is written as.

    ``AS_IMPORTED`` writes ``type triSurface``, which is what every case has
    always written. OpenFOAM 13 then works out for itself whether the surface
    can answer inside/outside queries, by counting open edges
    (``triSurface_searchableSurface.C:637-651``).

    ``ASSUME_CLOSED`` writes ``type closedTriSurface``. Its *only* difference
    from ``triSurface`` is that ``hasVolumeType()`` returns true
    unconditionally (``closedTriSurface.H:112-115``); v13 describes the class
    as being for a surface "meant to be closed but contains some
    imperfections, e.g. small holes or multiple parts". That one bit decides
    whether a ``mode inside``/``outside`` refinement region does anything
    (``refinementRegions.C:59,117`` warns and drops the shell otherwise) and
    whether ``zoneInside`` can name a cellZone (``surfaceZonesInfo.C:96``).
    So this is the user asserting closedness on a surface with pinholes,
    rather than watching those two features silently do nothing.
    """
    AS_IMPORTED = 'asImported'
    ASSUME_CLOSED = 'assumeClosed'


class CFDType(Enum):
    NONE = 'none'
    CELL_ZONE = 'cellZone'
    BOUNDARY = 'boundary'
    INTERFACE = 'interface'


class ZoneMode(Enum):
    """Which side of a zone surface the cell zone is taken from.

    C31-11. ``surfaceZonesInfo.C:34-40`` registers exactly four names --
    ``(inside outside insidePoint none)`` -- and reads them at ``:70-82``
    through ``mode`` (or the older ``cellZoneInside``), but only when the
    surface entry also carries a ``faceZone``. The writer hard-coded
    ``inside``, so a cell zone could only ever be the volume enclosed by the
    surface: an annulus, a jacket, or anything meshed from the *outside* of a
    closed body was unreachable, and so was a zone picked by a seed point.

    ``inside`` is the default because it is what every case written before
    this control said, so an existing project produces the same dictionary.
    """
    #: The volume the closed surface encloses. Needs a closed surface.
    INSIDE = 'inside'
    #: Everything the closed surface does not enclose. Needs a closed surface.
    OUTSIDE = 'outside'
    #: The connected region containing ``insidePoint``; works on open surfaces.
    INSIDE_POINT = 'insidePoint'
    #: No geometric selection at all -- the faceZone is written, the cells are
    #: left to whatever other surfaces claim them.
    NONE = 'none'


class LayerPatchSelector(Enum):
    """How one layer group names the patches it covers.

    C31-11. ``layerParameters.C:265-282`` walks the ``layers`` sub-dictionary
    and turns *every* key into a ``wordRe`` before asking the boundary mesh
    for the patches it matches, so a quoted key is a regular expression and a
    bare one is a literal name. FoamMesh could only ever emit literal names
    resolved from prepared geometry, so "every wall gets three layers" had to
    be re-authored by hand each time the geometry changed.
    """
    #: Today's behaviour: the patches bound to this group on the Geometry page.
    GEOMETRY = 'geometry'
    #: A regular expression, written as a quoted key OpenFOAM reads as one.
    PATTERN = 'pattern'


class RegionType(Enum):
    FLUID = 'fluid'
    SOLID = 'solid'


class ThicknessModel(Enum):
    FIRST_AND_OVERALL = 'firstAndOverall'
    FIRST_AND_EXPANSION = 'firstAndExpansion'
    FINAL_AND_OVERALL = 'finalAndOverall'
    FINAL_AND_EXPANSION = 'finalAndExpansion'
    OVERALL_AND_EXPANSION = 'overallAndExpansion'
    FIRST_AND_RELATIVE_FINAL = 'firstAndRelativeFinal'


class FeatureSnapType(Enum):
    """Retired: ``snap`` now carries two independent switches.

    Kept because :func:`migrateDocument` has to read the value an existing
    project was saved with. OpenFOAM 13 accepts ``implicitFeatureSnap`` and
    ``explicitFeatureSnap`` both true -- the usual setting for an STL with
    extracted features -- and this enum could only ever express one of them.
    """
    EXPLICIT = 'explicit'
    IMPLICIT = 'implicit'


class LayerPolicy(Enum):
    """What a layer group asks snappy to do with the patches it covers.

    OpenFOAM 13's annotated ``snappyHexMeshDict`` (lines 373-399) states that
    a patch not mentioned in ``layers {}`` *slides* during layer addition,
    while a patch mentioned with ``nSurfaceLayers 0`` is frozen: "Disable any
    mesh shrinking and layer addition on any point of a patch by setting
    nSurfaceLayers to 0". Omission and zero are therefore two different
    instructions, and until this enum existed only omission was reachable.
    """
    #: Extrude ``nSurfaceLayers`` prisms on the group's patches.
    GROW = 'grow'
    #: Write ``nSurfaceLayers 0``: the patch neither slides nor gains layers.
    FREEZE = 'freeze'
    #: Leave the patch out of ``layers {}`` so it slides with its neighbours.
    INHERIT = 'inherit'


class GapRefinementMode(Enum):
    NONE = 'none'
    INSIDE = 'inside'
    OUTSIDE = 'outside'
    MIXED = 'mixed'


class BufferLayerPointSmoothingMethod(Enum):
    LAPLACIAN = 'laplacian'
    GETME = 'geometricElementTransform'


class MeshShrinker(Enum):
    # ``medialAxisMeshMover`` is the only class registered in the OpenFOAM 13
    # externalDisplacementMeshMover selection table, so it is the only value
    # snappyHexMesh can resolve for ``meshShrinker``.
    MEDIAL_AXIS = 'displacementMedialAxis'


class OptionalToggle(Enum):
    """A switch that can also be left alone.

    ``BoolType`` coerces to true or false, so it cannot say "I have no opinion".
    These controls are ones OpenFOAM already has a default for; writing our own
    value into every case would silently change meshes that were tuned before
    the control existed.  DEFAULT means the key is not written at all.
    """
    DEFAULT = 'default'
    ON = 'on'
    OFF = 'off'


class BoundaryPatchType(Enum):
    """The ``type`` of a background-mesh face in ``blockMeshDict``."""
    PATCH = 'patch'
    WALL = 'wall'
    SYMMETRY = 'symmetry'
    EMPTY = 'empty'


class BackgroundEdgeKind(Enum):
    """The shape a background block edge really follows.

    A straight edge between two vertices is blockMesh's default and needs no
    entry; these are the four interpolations it reads when the edge is curved.
    Without them a curved duct is a faceted one, and the faceting is on the
    *background* mesh, where snappy cannot repair it.
    """

    ARC = 'arc'
    SPLINE = 'spline'
    POLYLINE = 'polyLine'
    BSPLINE = 'BSpline'


#: One vertex of an authored background topology.
backgroundVertex = {
    'x': FloatType().setDefault(0.0),
    'y': FloatType().setDefault(0.0),
    'z': FloatType().setDefault(0.0),
}

#: One hexahedral block. ``vertices`` is the eight indices in blockMesh's own
#: order; the grading fields take either a ratio or a segmented profile such
#: as ``(0.2 0.3 4) (0.6 0.4 1) (0.2 0.3 0.25)``.
backgroundBlock = {
    'name': TextType().setOptional(),
    'vertices': TextType().setOptional(),
    'numCellsX': IntType().setLowLimit(1).setDefault(10),
    'numCellsY': IntType().setLowLimit(1).setDefault(10),
    'numCellsZ': IntType().setLowLimit(1).setDefault(10),
    'gradingX': TextType().setDefault('1'),
    'gradingY': TextType().setDefault('1'),
    'gradingZ': TextType().setDefault('1'),
    # blockMesh states an expansion ratio as last cell over first, so packing
    # cells at the *start* of an axis means typing a reciprocal. That is where
    # a graded boundary layer ends up on the wrong wall, so the direction is a
    # control rather than an arithmetic exercise.
    'gradingXTowardStart': BoolType(False),
    'gradingYTowardStart': BoolType(False),
    'gradingZTowardStart': BoolType(False),
    'zone': TextType().setOptional(),
}

#: One curved edge between two authored vertices.
backgroundEdge = {
    'kind': EnumType(BackgroundEdgeKind).setDefault(BackgroundEdgeKind.ARC),
    'start': IntType().setLowLimit(0).setDefault(0),
    'end': IntType().setLowLimit(0).setDefault(0),
    'points': TextType().setOptional(),
}

#: One named patch of an authored background topology. ``category`` is what
#: the user meant the face to be; the name is theirs, not a generated label.
backgroundPatch = {
    'name': TextType().setOptional(),
    'type': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
    'category': EnumType(BoundaryCategory).setDefault(
        BoundaryCategory.UNCLASSIFIED),
    'group': TextType().setOptional(),
    'faces': TextType().setOptional(),
}

#: One ``mergePatchPairs`` entry, joining two authored patches into an
#: internal face.
backgroundMergePair = {
    'master': TextType().setOptional(),
    'slave': TextType().setOptional(),
}


geometry = {
    'gType': EnumType(GeometryType),
    'volume': IntType().setOptional(),
    'name': TextType(),
    # Which geometry artifact this row stands for. A boundary lives in two
    # stores -- the artifact's patch manifest, which is what gets meshed, and
    # this row, which is what the tree lists -- and until now the only thing
    # tying them together was the name, so a rename or a split on the Repair
    # page silently orphaned the row (R169). Optional: a row a project saved
    # before this field existed simply has no id, and is matched by name as
    # it always was.
    'geometryId': TextType().setOptional(),
    'shape': EnumType(Shape),
    'cfdType': EnumType(CFDType),
    'nonConformal': BoolType(False),
    'interRegion': BoolType(False),
    'path': TextType().setOptional(),
    'point1': VectorComposite().schema(),
    'point2': VectorComposite().setDefault(1, 1, 1).schema(),
    'radius': FloatType().setDefault(1),
    'castellationGroup': IntType().setOptional().setDefault(None),
    'layerGroup': IntType().setOptional().setDefault(None),
    'slaveLayerGroup': IntType().setOptional().setDefault(None),
}

region = {
    'name': TextType(),
    'type': EnumType(RegionType),
    'point': VectorComposite().schema()
}

surfaceRefinement = {
    'groupName': TextType(),
        # surfaceFeatures selects edges whose faces meet at LESS than this angle;
        # 150 is the OpenFOAM standard (cube edges at 90 deg are captured).
        # Verified live on v13: 30 extracts zero edges from a cube.
        'includedAngle': FloatType().setRange(0, 180).setDefault(150),
    'surfaceRefinement': {
        'minimumLevel': IntType().setRange(0, 10).setDefault(1),
        'maximumLevel': IntType().setRange(1, 10).setDefault(1)
    },
    'featureEdgeRefinementLevel': IntType().setRange(1, 10).setDefault(1),
    # C31-08. Extra refinement inside a narrow gap, expressed the way
    # Foundation 13 expresses it: an *increment* on this surface's maximum
    # level. ``refinementSurfaces.C:100-110`` reads ``gapLevelIncrement`` per
    # surface (and again per ``regions`` entry) with the castellatedMeshControls
    # value as its default, and forms ``gapLevel = maxLevel + increment``.
    # The ESI ``gapLevel (a b c)`` triple and ``gapMode`` are *not* read by
    # Foundation 13 -- neither name appears in ``libsnappyHexMesh.so`` -- so
    # they stay absent. Unset means "inherit the case-wide increment".
    'gapLevelIncrement': IntType().setLowLimit(0).setOptional().setDefault(None),
    # C31-11. ``refinementSurfaces.C:141`` reads ``perpendicularAngle`` per
    # surface with ``readIfPresent``, and ``snappyRefineDriver.C:1005`` hands
    # it to the baffle removal pass: cells are refined where the surface meets
    # the base grid at less than this angle. Unset means the key is absent and
    # v13 keeps its own ``-great`` sentinel, i.e. the pass does nothing --
    # which is exactly what every case written before this control did.
    #
    # DP-21. Stored in degrees, as the label and the ``'unit': 'deg'`` field
    # metadata have always said; v13 reads this one key raw, so
    # ``CaseBuilder._add_perpendicular_angle`` converts on the way into the
    # dictionary. The stored number's meaning never changed, only the
    # writer's translation of it, so no saved project needs migrating.
    #
    # The range stays 0..180 and is deliberately not narrowed to 0..90. The
    # mesher compares ``mag(n & nearestNormal) < sin(angle)``, so the control
    # saturates at 90 degrees -- ``sin`` = 1, every face admitted -- and
    # 90..180 mirrors 0..90 rather than meaning anything new. Every value in
    # the shipped range is therefore single-valued and non-negative under
    # ``sin``; none of it is the pre-DP-21 lottery. Narrowing would make
    # ``setValue`` raise on a value a project already holds, which is a
    # harder migration than the units fix it would be riding along with.
    'perpendicularAngle': FloatType().setRange(0, 180).setOptional().setDefault(None),
    # C31-11. ``patchInfo`` is handed to ``polyPatch::New`` verbatim
    # (``meshRefinement.C:1947``), so ``inGroups`` in it is the patch group
    # the meshed patch joins -- the same mechanism that makes ``walls`` or
    # ``background`` addressable as one name in every later dictionary.
    # Space-separated; empty means no ``inGroups`` entry, so an existing
    # case's ``constant/polyMesh/boundary`` is unchanged.
    'patchGroups': TextType().setOptional().setDefault(''),
    # C31-11. Which side of a zone surface the cell zone is taken from. Only
    # read when the entry also writes a ``faceZone``, i.e. on a cellZone
    # surface. See ZoneMode.
    'zoneMode': EnumType(ZoneMode).setDefault(ZoneMode.INSIDE),
    # ``surfaceZonesInfo.C:79-82``: with ``mode insidePoint`` the point is
    # mandatory (``lookup<point>("insidePoint", dimLength)``), so it is
    # written whenever that mode is chosen and never otherwise.
    'zoneInsidePoint': VectorComposite().schema(),
}

#: One ``(distance level)`` pair of a ``refinementRegions`` distance ramp.
refinementBand = {
    'distance': FloatType().setLowLimit(0, False).setDefault(1.0),
    'level': IntType().setRange(0, 10).setDefault(1),
}

#: One ``(distance level)`` pair of a ``features`` entry's distance ramp.
#:
#: C31-11. ``refinementFeatures.C:188-231`` reads ``levels`` as a
#: ``List<Tuple2<scalar, label>>`` and refines each band in turn, falling back
#: to a single ``level`` only when ``levels`` is absent. The writer only ever
#: emitted the single ``level``, so refinement that eases off with distance
#: from a feature edge -- the thing that keeps a level-5 edge from dragging a
#: level-5 shell through the whole boundary layer -- had no control.
#:
#: ``groupName`` names the surface-refinement row this band belongs to, so a
#: ramp is per refinement group in the same way ``featureEdgeRefinementLevel``
#: already is. It is not nested inside that row because a nested keyed list is
#: not part of the AF2 collection surface: the facade cannot create or patch
#: one, so a band authored there could not be saved through the same path
#: every other repeated control uses.
featureBand = {
    'groupName': TextType(),
    'distance': FloatType().setLowLimit(0, False).setDefault(1.0),
    'level': IntType().setRange(0, 10).setDefault(1),
}

volumeRefinement = {
    'groupName': TextType(),
    'mode': EnumType(RefinementRegionMode).setDefault(RefinementRegionMode.INSIDE),
    'distance': FloatType().setLowLimit(0, False).setDefault(1.0),
    'volumeRefinementLevel': IntType().setRange(0, 10).setDefault(1),
    # C31-08. ``mode distance`` in Foundation 13 reads a *list* of
    # ``(distance level)`` pairs and refines each band separately, so one
    # region can hold a wake at level 3 close in and level 1 further out. The
    # writer only ever emitted one pair, which made the ramp unreachable.
    # Empty means "use ``distance``/``volumeRefinementLevel`` as the single
    # band", which is what every project saved before this field said.
    'bands': IntKeyList(refinementBand),
    # C31-08. ``insideSpan``/``outsideSpan`` size cells by how many of them
    # must fit across the local span of the surface; ``refinementRegions.C:574``
    # looks this up with ``dict.lookup``, i.e. it is mandatory for those two
    # modes and read for no other.
    'cellsAcrossSpan': IntType().setLowLimit(1).setDefault(5),
}

layer = {
    'groupName': TextType(),
    # F-39. `grow` writes the count, `freeze` writes `nSurfaceLayers 0`, and
    # `inherit` leaves the patch out of `layers {}` entirely. See LayerPolicy.
    'layerPolicy': EnumType(LayerPolicy).setDefault(LayerPolicy.GROW),
    # The low limit was 1, which made `nSurfaceLayers 0` -- v13's own spelling
    # for "freeze this patch" -- unsaveable and unwritable. Zero is now a
    # value, and it means the same thing the FREEZE policy does.
    'nSurfaceLayers': IntType().setLowLimit(0),
    'thicknessModel': EnumType(ThicknessModel).setDefault(ThicknessModel.FINAL_AND_EXPANSION),
    'relativeSizes': BoolType(True),
    'firstLayerThickness': FloatType().setDefault(0.3),
    'finalLayerThickness': FloatType().setDefault(0.5),
    'thickness': FloatType().setDefault(0.5),
    'expansionRatio': FloatType().setDefault(1.2),
    'minThickness': FloatType().setDefault(0.3),
    # C31-11. See LayerPatchSelector. ``geometry`` is the default because it
    # is what every layer group written before this field did, so an existing
    # project writes the same ``layers {}`` block.
    'patchSelector': EnumType(LayerPatchSelector).setDefault(
        LayerPatchSelector.GEOMETRY),
    # The regular expression, unquoted. The writer quotes it, because a
    # quoted key is how OpenFOAM tells a ``wordRe`` pattern from a literal
    # patch name (``layerParameters.C:271-275``).
    'patchPattern': TextType().setOptional().setDefault(''),
}


interfacePair = {
    'name': TextType(),
    'enabled': BoolType(True),
    'masterScopeToken': TextType(),
    'slaveScopeToken': TextType(),
    'coupling': EnumType(InterfaceCoupling).setDefault(InterfaceCoupling.CONFORMAL),
    'transform': EnumType(InterfaceTransform).setDefault(
        InterfaceTransform.COINCIDENT),
    'matchTolerance': FloatType().setLowLimit(0, False).setDefault(1e-6),
    'translationX': FloatType().setDefault(0.0),
    'translationY': FloatType().setDefault(0.0),
    'translationZ': FloatType().setDefault(0.0),
    'rotationCentreX': FloatType().setDefault(0.0),
    'rotationCentreY': FloatType().setDefault(0.0),
    'rotationCentreZ': FloatType().setDefault(0.0),
    'rotationAxisX': FloatType().setDefault(0.0),
    'rotationAxisY': FloatType().setDefault(0.0),
    'rotationAxisZ': FloatType().setDefault(1.0),
    'rotationAngleDegrees': FloatType().setRange(-360, 360).setDefault(0.0),
}


# --------------------------------------------------------------------------- #
# Gmsh collection element schemas
# --------------------------------------------------------------------------- #

gmshSizeField = {
    'name': TextType(),
    'enabled': BoolType(True),
    'priority': IntType().setRange(0, 100).setDefault(0),
    'fieldType': EnumType(GmshSizeFieldType).setDefault(
        GmshSizeFieldType.DISTANCE_THRESHOLD),
    # Scope names prepared surfaces for distance fields; analytic shapes
    # (box/ball/cylinder/frustum) ignore it and use their own geometry.
    'scopeToken': TextType(),
    'sizeInside': FloatType().setLowLimit(0, False).setDefault(0.01),
    'sizeOutside': FloatType().setLowLimit(0, False).setDefault(0.1),
    'distanceMin': FloatType().setLowLimit(0, True).setDefault(0.0),
    'distanceMax': FloatType().setLowLimit(0, False).setDefault(0.1),
    # Ball and cylinder centre; the frustum's first end point.
    'centre': VectorComposite().setDefault(0.0, 0.0, 0.0).schema(),
    # WP-01 F-16. A Gmsh Box field is two opposite corners. This used to be
    # `centre` plus an `extent`, and the runner then wrote XMin=centre.x and
    # XMax=centre.x+extent.x -- so the label said "centre" while the number
    # was a corner, and a box "centred" on the origin actually sat entirely in
    # the positive octant. The two corners are now asked for directly.
    'boxMin': VectorComposite().setDefault(0.0, 0.0, 0.0).schema(),
    'boxMax': VectorComposite().setDefault(0.1, 0.1, 0.1).schema(),
    'radius': FloatType().setLowLimit(0, False).setDefault(0.05),
    # WP-01 F-16. A frustum has a radius at each end -- that is what makes it a
    # frustum rather than a cylinder. The runner used to copy one radius pair
    # onto both ends, so every frustum meshed as a tube.
    'radiusEnd': FloatType().setLowLimit(0, False).setDefault(0.1),
    'axis': VectorComposite().setDefault(0.0, 0.0, 1.0).schema(),
    'thickness': FloatType().setLowLimit(0, True).setDefault(0.0),
    # Plan 30 WP12. `Distance.Sampling`: how many points each scoped entity is
    # sampled at when the distance field is built. Too few and the ramp is
    # blocky on a curved face; Gmsh's own default is 20.
    'sampling': IntType().setRange(2, 1000).setDefault(20),
    # Plan 30 WP12, curvature rows only. `Curvature.Delta` is the finite
    # difference step the curvature is evaluated over, and the two limits are
    # the curvature range that maps onto sizeInside..sizeOutside.
    'curvatureDelta': FloatType().setLowLimit(0, False).setDefault(0.001),
    'curvatureMin': FloatType().setLowLimit(0, True).setDefault(0.0),
    'curvatureMax': FloatType().setLowLimit(0, False).setDefault(1.0),
    # Only read by math_eval rows. Validated against an allowlist of names and
    # costed against the domain before a job is accepted.
    'expression': TextType().setDefault('0.01'),
}


#: Plan 29 WP8. One imported surface, one target size. A distance field with a
#: prepared scope could already express this, but only by hand and only for a
#: face group the geometry catalogue had prepared; refining a single face was
#: not reachable at all. Each row compiles to a Distance+Threshold pair in
#: `core/gmsh/size_fields.py`, which is why there is no field-type choice here.
gmshSurfaceSize = {
    'name': TextType(),
    'enabled': BoolType(True),
    # A Gmsh surface tag, 1-based in import order -- the same numbering the
    # prepared-geometry scope maps and `surfaceNames` are keyed on.
    'surfaceId': IntType().setLowLimit(1).setDefault(1),
    'targetSize': FloatType().setLowLimit(0, False).setDefault(0.005),
    # How far the size ramps back to the global target. Zero means "one global
    # cell", derived at plan time, because a Threshold with no ramp is a step
    # change in cell size and Gmsh rejects it outright.
    'blendDistance': FloatType().setLowLimit(0, True).setDefault(0.0),
    'priority': IntType().setRange(0, 100).setDefault(0),
}


gmshCurveControl = {
    'name': TextType(),
    'enabled': BoolType(True),
    # A surface scope is intentional: the control applies to the boundary
    # curves of the selected prepared face group, which is what the geometry
    # catalogue can highlight.
    'scopeToken': TextType(),
    'mode': EnumType(GmshCurveMode).setDefault(GmshCurveMode.TRANSFINITE),
    # Elements along the curve. Gmsh's setTransfiniteCurve takes *nodes*, so
    # the derivation adds one; the runner used to pass this straight through
    # and every transfinite curve came back one element short (F-26).
    'segments': IntType().setRange(1, 10000).setDefault(10),
    'law': EnumType(GmshTransfiniteLaw).setDefault(GmshTransfiniteLaw.PROGRESSION),
    'coefficient': FloatType().setLowLimit(0, False).setDefault(1.0),
    # Plan 31 CP-08. Which end of the curve the small elements go to.
    # MEASURED: Gmsh carries the grading direction in the *sign* of the
    # coefficient -- on an 0.8 m edge, Progression 1.4 spaced nine nodes
    # 0.023 .. 0.245 m and -1.4 spaced them 0.245 .. 0.023. The coefficient
    # here stays positive, because a negative growth rate is not something a
    # user means, and the direction is asked for on its own. Without this
    # there was no way to put the fine end where the wall is.
    'reverseGrading': BoolType(False),
    # Plan 30 WP12. A transfinite curve on its own only distributes nodes
    # along an edge; the surface still meshes unstructured. This asks for the
    # structured surface as well, which needs a CAD surface with three or four
    # corners -- a tessellated import has none, and the run says so.
    'transfiniteSurface': BoolType(False),
    # Plan 31 FC-C. Which corners Gmsh should interpolate between, as point
    # tags, blank to let it choose. It only matters on a face with more than
    # four candidate corners, and there it is the whole difference between a
    # mesh and no mesh.
    #
    # MEASURED on Gmsh 4.15.2, on a box whose front face is split in the
    # middle of one edge so that the face has five corner points and four
    # real ones (plans/evidence/plan31/fcc-structured/transfinite-tuning.json):
    #
    #   * named nothing -- `generate` raised "Surface 6 is transfinite but
    #     has 5 corners" and the whole run died;
    #   * named the four real corners -- 32 triangles, which is the 4x4 grid
    #     of 16 quadrilaterals the node counts asked for;
    #   * named the same four scrambled -- 32 again, so the order is Gmsh's
    #     business and not the user's;
    #   * named three real corners and one point that is not on the face at
    #     all -- 44 triangles, silently. Gmsh does not check membership, so
    #     the run does, and says which corners the face actually has.
    'cornerPoints': TextType(),
    'localSize': FloatType().setLowLimit(0, False).setDefault(0.01),
    'priority': IntType().setRange(0, 100).setDefault(0),
}


gmshVolumeControl = {
    'name': TextType(),
    'enabled': BoolType(True),
    'scopeToken': TextType(),
    # Plan 30 WP12, F-26. `regionType` used to live here. Nothing read it: the
    # only `region_type` the polyMesh writer consumes comes from the prepared
    # geometry's regions, never from a Gmsh volume control, so choosing
    # "solid" here published exactly the same cell zone as "fluid". Excluding
    # a volume is what `included` does, and that one is real.
    'included': BoolType(True),
    'targetSize': FloatType().setOptional().setDefault(None).setLowLimit(0, False),
    'transfinite': BoolType(False),
    'priority': IntType().setRange(0, 100).setDefault(0),
}


gmshPeriodicPair = {
    'name': TextType(),
    'enabled': BoolType(True),
    'masterScopeToken': TextType(),
    'slaveScopeToken': TextType(),
    'transform': EnumType(GmshPeriodicTransform).setDefault(
        GmshPeriodicTransform.TRANSLATION),
    'translation': VectorComposite().setDefault(0.0, 0.0, 0.0).schema(),
    'rotationCentre': VectorComposite().setDefault(0.0, 0.0, 0.0).schema(),
    'rotationAxis': VectorComposite().setDefault(0.0, 0.0, 1.0).schema(),
    'rotationAngleDegrees': FloatType().setRange(-360, 360).setDefault(0.0),
    'matchTolerance': FloatType().setLowLimit(0, False).setDefault(1e-6),
}


schema = {
    CONFIGURATIONS_VERSION_KEY: IntType().setDefault(CURRENT_CONFIGURATIONS_VERSION),
    'step': EnumType(Step).setDefault(Step.GEOMETRY),
    'mesh': {
        'engine': EnumType(MeshEngine).setDefault(MeshEngine.UNSELECTED),
        # Plan 28. Added without bumping CURRENT_CONFIGURATIONS_VERSION on
        # purpose: the loader has no migration ladder and refuses any version
        # it does not recognise, so a bump would make every existing project
        # unopenable. It does fill absent leaves from the schema, so an old
        # project simply reads the default.
        'targetSolver': EnumType(TargetSolver).setDefault(
            TargetSolver.UNSELECTED),
        'execution': {
            'mode': EnumType(ExecutionMode).setDefault(ExecutionMode.AUTO),
            'maxCpuCores': IntType().setLowLimit(0).setDefault(0),
            'maxMemoryBytes': IntType().setLowLimit(0).setDefault(0),
            'allowDistributed': BoolType(False),
            'preferredBackend': TextType().setDefault('local'),
            # Plan 26 WP9.2. Snappy's refinement is decomposition-sensitive:
            # where the partition cuts fall relative to the refined region
            # changes the mesh, so this is a meshing input and it was a source
            # constant. Measured: `duct` moves +12.4% in cell count between
            # serial and 16 ranks while a 609k-cell `annulus` moves -0.04%.
            # `hierarchical` is the direct remedy -- it lets a user align the
            # cuts with the geometry instead of leaving them to the partitioner.
            'decompositionMethod': EnumType(DecompositionMethod).setDefault(
                DecompositionMethod.SCOTCH),
            # Read only by `hierarchical` and `simple`, which refuse to run
            # without their coefficients. Shipping the combo without these
            # would write a decomposeParDict that decomposePar rejects.
            'decompositionOrder': TextType().setDefault('xyz'),
            # Empty means "derive a balanced split from the rank count", which
            # is the only sane default: a fixed n vector would be wrong for
            # every rank count but one.
            'decompositionCells': TextType().setOptional().setDefault(''),
            # Plan 31 (parallel.decompose_extras). The `constraints {}` block
            # of decomposeParDict. Every one of these is off or empty by
            # default and nothing is written unless one is set, so a case that
            # never opens this page gets the dictionary it always got.
            #
            # Verified live on OpenFOAM 13 (13-58ed5c2046ef) through
            # `decomposePar` on a zoned single block; see
            # openfoam/decomposition.DecompositionConstraints for the measured
            # output of each.
            #
            # A faceZone split across processors is the one that bites: the
            # castellation step creates zones for `cfdType none` and
            # `interface` surfaces, the partitioner has no reason to respect
            # them, and nothing says so until a solver refuses the case.
            'preserveFaceZones': BoolType(False),
            'preserveBaffles': BoolType(False),
            # Patch names, whitespace- or comma-separated. A text field rather
            # than a picker because the useful names are ones the user gave.
            'preservePatches': TextType().setOptional().setDefault(''),
            'preserveRefinementHistory': BoolType(False),
            # A volScalarField to weight cells by. Read MUST_READ
            # (domainDecompositionDecompose.C:144-160), so naming a field the
            # case does not carry aborts decomposePar -- measured: "cannot
            # find file <case>/0/cellWeight". Empty writes no key.
            'decompositionWeightField': TextType().setOptional().setDefault(''),
        },
        # Plan 30 WP-09 (F-23): `mesh/intent` is gone. Six fields --
        # globalTargetSize, minimumSize, maximumCells, growthRate,
        # curvaturePolicy, qualityPolicy -- were validated, serialised and
        # carried on the engine contract, and no engine read any of them:
        # Gmsh sizes from `gmsh/globalSizing` and snappy from its base grid
        # and refinement levels. `migrateDocument` drops them from saved
        # cases. What the mesh is *for* is `mesh/targetSolver`, above.
    },
    'geometryPreparation': {
        'decision': EnumType(GeometryPreparationDecision).setDefault(
            GeometryPreparationDecision.UNDECIDED),
        'geometryFingerprint': TextType().setOptional().setDefault(None),
        'acknowledgment': TextType().setOptional().setDefault(None),
        'rulesVersion': IntType().setDefault(1),
        # Category adopted by prepared patches whose name matches no known
        # category. It lives with the geometry, not with an engine: every
        # engine's publication step needs it to give patches real solver types.
        'defaultBoundaryCategory': EnumType(BoundaryCategory).setDefault(
            BoundaryCategory.WALL),
        # Plan 28 WP6. The project-wide fallback tolerance -- level 5 of the
        # five the fidelity report resolves. Zero means unset, and unset is
        # not a hidden default: with no level in force, fidelity and
        # resolution report `unrated` and the summary stays report-only.
        # Added without bumping CURRENT_CONFIGURATIONS_VERSION for the same
        # reason `mesh/targetSolver` was: the loader has no migration ladder,
        # and it fills an absent leaf from the schema.
        'qualificationToleranceM': FloatType().setLowLimit(0, True).setDefault(
            0.0),
    },
    'geometry': IntKeyList(geometry),
    # Shared geometry-level interface pairing.  Both engines consume this
    # connector model; it is not an engine-specific duplicate.
    'interfacePairs': IntKeyList(interfacePair),
    'region': IntKeyList(region),
    'gmsh': {
        # FC-E. How many dimensions this case is meshed in. It is the first
        # thing the runner reads, because it decides which `generate` call
        # runs and whether a model with no volumes is an error or the point.
        'dimensionality': {
            'mode': EnumType(GmshMeshDimension).setDefault(
                GmshMeshDimension.THREE_D),
            # The distance between the two `empty` patches. A planar case's
            # result does not depend on it, but the cell aspect ratio does,
            # so it defaults to roughly one target cell rather than to a
            # number that makes every cell a sliver.
            'thickness': FloatType().setLowLimit(0, False).setDefault(0.01),
            # The whole included angle of the wedge, in degrees. OpenFOAM's
            # own tutorials use five and its wedge transform is a small-angle
            # one; the upper limit is where the assumption stops being
            # defensible rather than where OpenFOAM refuses.
            'wedgeAngle': FloatType().setRange(0.1, 10.0).setDefault(5.0),
            'wedgeAxis': EnumType(GmshWedgeAxis).setDefault(GmshWedgeAxis.X),
            # The two faces the extrusion makes. Named so a case whose
            # section already carries a curve called `front` can move them
            # out of the way rather than colliding.
            'frontPatch': TextType().setDefault('front'),
            'backPatch': TextType().setDefault('back'),
        },
        'globalSizing': {
            # Derived from the bounding box when the user has not set it;
            # derive_global_sizing warns rather than silently using 1.0.
            'targetSize': FloatType().setLowLimit(0, False).setDefault(0.01),
            'minimumSize': FloatType().setLowLimit(0, False).setDefault(0.001),
            'sizeFactor': FloatType().setRange(0.01, 100).setDefault(1.0),
            'fromCurvature': IntType().setRange(0, 50).setDefault(12),
            'fromPoints': BoolType(True),
            'extendFromBoundary': BoolType(True),
            # Plan 31 FC-B. The curve-discretisation floors: how few elements
            # a small circle, a short curve or a full turn may get, whatever
            # the target size says. Defaults are Gmsh 4.15.2's own, probed
            # 2026-09-06 (7, 3, 0 and off), so a case that never touches them
            # meshes exactly as it did before.
            'minimumCirclePoints': IntType().setRange(3, 200).setDefault(7),
            'minimumCurvePoints': IntType().setRange(2, 200).setDefault(3),
            # 0 is Gmsh's "unset": the curvature setting alone decides.
            'minimumElementsPerTwoPi': IntType().setRange(0, 200).setDefault(0),
            # Plan 30 WP12. How the size fields are combined into the one
            # background field Gmsh keeps. `Min` -- the finest request wins --
            # is what shipped and stays the default; `Max` is the Gmsh `Max`
            # field, and is what a user coarsening away from a feature needs.
            'fieldCombiner': EnumType(GmshFieldCombiner).setDefault(
                GmshFieldCombiner.MIN),
            # Plan 31 FC-C. Gmsh's barycentric subdivision,
            # `Mesh.SubdivisionAlgorithm` 3. It sits with sizing rather than
            # with cell shape because of what it MEASURED as: on a one-metre
            # box the boundary came back with the same 260 triangles and the
            # volume went from 415 tetrahedra to 1660 -- four times the cells,
            # every one still a tetrahedron. That is a refinement, not a
            # change of family, and it is the only whole-mesh refinement this
            # product can reach.
            #
            # It shares one Gmsh option with the cell shape, so the two cannot
            # both hold: the same box asked for hexahedra came back as 780
            # quadrangles and 1660 hexahedra, and asked for both it would come
            # back as tetrahedra. The run refuses the refinement in that case
            # and says which mesh it would have produced.
            'barycentricRefinement': BoolType(False),
        },
        'algorithms': {
            'surface': EnumType(GmshSurfaceAlgorithm).setDefault(
                GmshSurfaceAlgorithm.FRONTAL_DELAUNAY),
            'volume': EnumType(GmshVolumeAlgorithm).setDefault(
                GmshVolumeAlgorithm.DELAUNAY),
            'cellShape': EnumType(GmshCellShape).setDefault(
                GmshCellShape.TETRAHEDRAL),
            # Plan 30 WP12. Gmsh's recombination: quads on the surfaces and
            # the hex/prism family that follows from them. Off by default and
            # only honoured for an SU2 target -- the OpenFOAM route rejects
            # the result outright, for the reasons GmshCellShape records, so
            # the derivation clears this with a warning for every other
            # target rather than letting a run produce a mesh that cannot be
            # published.
            'recombine': BoolType(False),
            # Plan 31 CP-08 item 3. Which recombiner. The runner wrote 1
            # (blossom) and nothing could choose otherwise, while the four
            # values produce four measurably different meshes -- see
            # GmshRecombinationAlgorithm. Only consulted when 'recombine' is
            # on.
            'recombinationAlgorithm': EnumType(
                GmshRecombinationAlgorithm).setDefault(
                    GmshRecombinationAlgorithm.BLOSSOM),
            # Plan 31 FC-B. Mesh.AlgorithmSwitchOnFailure and Mesh.MaxRetries
            # from one control. Gmsh 4.15.2 defaults both on (1 and 10), so
            # this has always been happening; what was missing is that it was
            # invisible. The run manifest now names the algorithm each surface
            # was actually meshed with, read out of Gmsh's own log, so a
            # fallback shows up instead of passing for the chosen algorithm.
            'algorithmFallback': BoolType(True),
            # Plan 31 FC-C. `gmsh.model.mesh.splitQuadrangles`, the answer to
            # a recombined mesh the OpenFOAM route refuses. Until now that
            # request was simply cleared: the user asked for recombination,
            # got a warning, and got the mesh they would have had if they had
            # never asked. With this on, the surfaces are recombined as asked
            # and the quadrangles are split back into triangles before the
            # volume pass, so the request shapes the mesh and the target
            # still gets the tetrahedra it reads.
            #
            # The ordering is measured, not chosen: splitting after the
            # volume pass leaves the pyramids recombination created, which is
            # the family the polyMesh route rejects.
            'splitQuadrangles': BoolType(False),
        },
        # Plan 31 FC-C. Structure asked for by the model rather than face by
        # face. The curve and volume controls make one named scope structured;
        # this asks Gmsh to find every volume it can structure by itself.
        'structuring': {
            # `mesh.setTransfiniteAutomatic`. MEASURED on Gmsh 4.15.2
            # through the shipped runner: a one-metre box at a 0.25 m target
            # came back as 125 hexahedra behind 150 quadrilateral boundary
            # faces, where the same box meshes as 415 tetrahedra behind 260
            # triangles without it.
            #
            # The hexahedra are recombination, not structure: called with
            # `recombine=False` the same box came back as 2058 tetrahedra
            # behind 588 triangles -- structured, and not one hexahedron in
            # it. So it carries the same SU2-only guard recombination does,
            # for the same reason. (That third reading comes from
            # plans/evidence/plan31/fcc-structured/structuring-recombine.json,
            # which meshes the fixture directly and so counts more elements
            # than the runner, whose size and algorithm options differ.)
            #
            # It succeeds per volume, not per model. MEASURED on a box beside
            # a sphere: the box came back as 125 hexahedra and the sphere as
            # 207 tetrahedra, in one mesh, with no error. The run reports how
            # many volumes went structured and does not call such a mesh
            # structured.
            'automatic': BoolType(False),
            # `Mesh.TransfiniteTri`. A three-sided transfinite face is
            # interpolated between three corners, and Gmsh has two ways to do
            # it. MEASURED on Gmsh 4.15.2: a triangle whose three sides each
            # carry nine nodes came back as 120 triangles with this off and 64
            # with it on -- 64 being 8x8, the count a structured side of eight
            # elements owes. At five nodes a side the same pair reads 28 and
            # 16, and 16 is 4x4. Off, the face is a transfinite *boundary*
            # with an unstructured interior; on, it is a structured triangle.
            #
            # It is a setting rather than the only behaviour because the
            # unstructured interior has the better element shapes near the
            # corners, and a user who asked for three sides may want either.
            'transfiniteTri': BoolType(False),
        },
        # Plan 30 WP12. An external-flow domain the app builds itself.
        # The assembly fixtures ship a `_farfield.stl` beside every model
        # because nothing here could make one, so every external case
        # depended on a file authored outside the product. The box is cut
        # against the imported solids by OCC, which needs real CAD: a
        # tessellated import is refused with that reason rather than
        # half-applied.
        'farfield': {
            'enabled': BoolType(False),
            # A multiple of the geometry's bounding-box diagonal, added on
            # every side. 2.0 puts five diagonals between opposite faces,
            # which is the usual starting point for external aerodynamics;
            # it is a multiple rather than a length so the same value suits
            # a 40 mm bracket and a 4 m wing.
            'padding': FloatType().setLowLimit(0, False).setDefault(2.0),
            # What to do with a pocket the cut leaves inside the geometry.
            # MEASURED: all six assembly fixtures are solids with internal
            # voids, so every one of them leaves one, and the finned heat
            # sink's pocket is bounded by 126 faces -- the fins. Discarding
            # is right for external flow and wrong for a case about the
            # passages, and the product used to do it without asking.
            'sealedCavities': EnumType(
                GmshSealedCavityPolicy).setDefault(
                    GmshSealedCavityPolicy.DISCARD),
        },
        'parallel': {
            # One by default, to match the default volume algorithm. Gmsh's
            # Delaunay and Frontal 3D kernels are single-threaded, so the old
            # default of four asked for threads the shipped configuration could
            # never use -- and reported nothing. Raising this is worthwhile
            # once the volume algorithm is hxt, which is the threaded one.
            'threads': IntType().setRange(1, 64).setDefault(1),
        },
        'healing': {
            'importTolerance': FloatType().setLowLimit(0, False).setDefault(1e-6),
            # Measured: OCCSewFaces turns a closed solid into a bare face set,
            # volumes 1 -> 0, and generate(3) then produces a surface mesh with
            # no cells and no error. Off unless the user knowingly asks.
            'sewFaces': BoolType(False),
            # Measured: OCCFixDegenerated strips the degenerate seam at a
            # sphere's poles and the surface becomes unmeshable outright.
            'fixDegenerated': BoolType(False),
            # Rebuilds a solid from a closed shell, and is the remedy for CAD
            # that arrives as faces rather than a solid. Measured: sewing
            # alone leaves zero volumes; sewing plus this leaves one.
            'makeSolids': BoolType(False),
            'removeDuplicateNodes': BoolType(True),
            # Two solids sharing a face import as two copies of it, and the
            # volume mesher then fails outright with "Could not recover
            # boundary mesh". Fusing them makes the interface conformal.
            'removeDuplicateFaces': BoolType(True),
            # Dihedral angle used to recover patches from a tessellated
            # surface. Only read for STL/OBJ input, which Gmsh meshes as a
            # discrete geometry rather than through the CAD importer.
            'classificationAngle': FloatType().setRange(1, 180).setDefault(40),
            # -- Plan 31 CP "geometry kernels and healing" ------------------ #
            # The four above reconfigure the OCC *importer*. These are the
            # repair pass itself and the small-entity fixes it can apply.
            # Every default below is Gmsh 4.15.2's own measured default
            # (probed 2026-09-06), so a case that never asks for healing
            # meshes exactly as it did before this group existed.
            #
            # `gmsh.model.occ.healShapes` run explicitly after import, on
            # everything imported. Off by default: a repair pass that starts
            # running on cases that never asked for it changes results with
            # no way for the user to tell. Which repairs it performs are the
            # ones ticked above and below -- sewing, degenerate edges, solid
            # reconstruction, small edges, small faces -- so the pass never
            # does anything the user has not asked for on this page.
            'healShapes': BoolType(False),
            # `Geometry.OCCFixSmallEdges` / `.OCCFixSmallFaces`. Gmsh default
            # 0 for both. They are the classic dirty-STEP remedy: slivers
            # left by a translation that the mesher would otherwise have to
            # resolve, dragging the whole element size down to their length.
            'fixSmallEdges': BoolType(False),
            'fixSmallFaces': BoolType(False),
            # `Geometry.OCCAutoFix`. Gmsh default 1: the importer repairs a
            # shape OCC reports as invalid. Exposed so it can be turned OFF,
            # which is the only way to see what the CAD actually contains
            # when a repaired import is meshing wrongly.
            'autoFix': BoolType(True),
            # `Geometry.OCCUnionUnify`. Gmsh default 1: after a boolean, the
            # faces that came from one original face are unified back into
            # one. Off leaves the fragments, which is what a user wants when
            # the farfield cut has split a patch that must stay whole.
            'unionUnify': BoolType(True),
            # `Geometry.ToleranceBoolean`. Gmsh default 0, which means "use
            # the kernel's own". This application runs booleans -- the
            # farfield cut and the duplicate-face fuse -- so a dirty import
            # whose cut fails has a knob here rather than nowhere.
            'booleanTolerance': FloatType().setLowLimit(0, True).setDefault(0.0),
            # `Geometry.OCCScaling`. Gmsh default 1. A uniform factor applied
            # to the imported shape. `Geometry.OCCTargetUnit` handles CAD
            # that declares its unit; this handles CAD that does not, or
            # declares the wrong one.
            'importScaling': FloatType().setLowLimit(0, False).setDefault(1.0),
            # `Geometry.OCCImportLabels`. Gmsh default 1: STEP/BREP entity
            # names and colours are read in. Patch identity in this
            # application is built from them, so the default stays on; off is
            # the remedy for CAD whose labels collide.
            'importLabels': BoolType(True),
            # `Geometry.OCCParallel`. Gmsh default 0. Speed only: it lets OCC
            # thread its boolean and import work. It changes how long the
            # import takes, not what it produces.
            'occParallel': BoolType(False),
        },
        'optimization': {
            'optimize': BoolType(True),
            # Plan 30 WP12. `Mesh.OptimizeNetgen` is a boolean -- Gmsh runs its
            # own Netgen pass at the end of generate() when it is set. The
            # runner used to write the *pass count* into it, so asking for
            # three passes and asking for one set the same option to a
            # different truthy number and the pass loop then ran on top of it.
            # This is the boolean; `netgenPasses` below is the single pass
            # control, and it drives the explicit optimize('Netgen') loop.
            'netgen': BoolType(True),
            # Three Netgen passes lift mean gamma from about 0.65 to 0.85 and
            # bring median maximum non-orthogonality to 50.1.
            'netgenPasses': IntType().setRange(0, 10).setDefault(3),
            # `Mesh.Smoothing`: Laplacian smoothing steps applied to the
            # surface mesh. Gmsh's own default is 1.
            'smoothing': IntType().setRange(0, 100).setDefault(1),
            # `Mesh.OptimizeThreshold`: elements whose quality is below this
            # are the ones the optimiser tries to repair. Gmsh's default 0.3.
            'optimizeThreshold': FloatType().setRange(0, 1).setDefault(0.3),
            # `Mesh.HighOrderOptimize`: 0 none, 1 optimisation, 2 elastic+opt,
            # 3 elastic, 4 fast curving. Only read when the element order is 2,
            # which only the SU2 route allows.
            'highOrderOptimize': IntType().setRange(0, 4).setDefault(0),
            # Plan 31 FC-D. Gmsh's two repair optimisers, UntangleMeshGeometry
            # and Relocate3D, run together as one pass and only when the mesh
            # has already failed the quality limit -- which is the one moment
            # they have anything to do. Off by default: they move nodes on a
            # mesh the user has not been shown yet, and a mesh that passes
            # should not be quietly altered. Straight-sided meshes only; the
            # runner declines on a raised mesh and says why.
            'repairPoorElements': BoolType(False),
            'qualityType': EnumType(GmshQualityMeasure).setDefault(
                GmshQualityMeasure.SICN),
            'minQuality': FloatType().setRange(0, 1).setDefault(0.1),
            # Plan 26 WP1.1. The gate used to compare the single worst element
            # against minQuality, so three bad elements out of 141,486 read
            # exactly like thirty thousand and the mesh was discarded either
            # way. These say how much of the distribution may sit below the
            # limit before the mesh is refused. Defaults stay strict -- 0 and 0
            # reproduce the old behaviour exactly -- so no shipped mesh changes.
            'allowedFraction': FloatType().setRange(0, 1).setDefault(0.0),
            'allowedCount': IntType().setLowLimit(0, True).setDefault(0),
            # Below this the mesh is invalid whatever the allowance says: a
            # non-positive quality is an inverted or zero-volume element and no
            # user may accept one. Not the same control as minQuality.
            'hardFloor': FloatType().setRange(0, 1).setDefault(0.0),
        },
        'boundaryLayers': {
            # R118. Gmsh grows layers by extruding boundary surfaces and
            # rebuilding the core volume from the result. Restricting it to a
            # subset used to leave the surface loop open, so this was global;
            # the runner now deletes each un-extruded surface and rebuilds it
            # from the extrusion's inner rim, MEASURED closed on a live duct.
            # Empty means every boundary surface, which is what shipped.
            # Optional with an empty default, because empty is the
            # shipped meaning above and a bare TextType() is REQUIRED:
            # every case carrying the default then failed revalidation,
            # which is the path undo/redo, reopen and delta-commit all
            # take (38 suite failures, one cause).
            'patches': TextType().setOptional().setDefault(''),
            'enabled': BoolType(False),
            'mode': EnumType(GmshLayerMode).setDefault(GmshLayerMode.FIRST_AND_RATIO),
            'firstHeight': FloatType().setLowLimit(0, False).setDefault(0.0006),
            'ratio': FloatType().setRange(1.0, 5.0).setDefault(1.2),
            'layerCount': IntType().setRange(1, 100).setDefault(3),
            'totalThickness': FloatType().setLowLimit(0, False).setDefault(0.002),
            'quads': BoolType(True),
        },
        # Plan 29 WP8, corrected by CP-01. Element order is an exporter-facing
        # choice, not a mesher preference: Gmsh meshes at 1 or 2, and both
        # export adapters read first-order only -- OpenFOAM because
        # `gmshToFoam` and the polyMesh publisher take MSH 2.2, SU2 because
        # this application's own reader holds the linear VTK type codes. So
        # the derivation clamps the order to 1 on *both* named routes and says
        # why; the one route that permits 2 is the native run with no target
        # chosen. This comment said the opposite until 6 September 2026.
        # See MESHER_ELEMENT_ORDERS / EXPORTER_ELEMENT_ORDERS in
        # core/gmsh/plan_derivation.py.
        'output': {
            'elementOrder': IntType().setRange(1, 2).setDefault(1),
            # Serendipity elements: eight nodes on a hex face rather than
            # nine. Read only when the order is 2.
            'secondOrderIncomplete': BoolType(False),
            # `Mesh.SecondOrderLinear`. Plan 31 FC-D. Where the midside nodes
            # go: off, Gmsh projects them onto the CAD surface, which is the
            # point of a second-order mesh and also what tangles it on a
            # tightly curved boundary; on, they are interpolated onto the
            # straight edge and the element cannot invert. MEASURED on
            # test_cases/_geometry/cad/torus.step, the one live control of the
            # three high-order knobs the ledger asked for.
            'secondOrderLinear': BoolType(False),
            # Plan 31 FC-D. Reverse Cuthill-McKee over the mesh nodes, run
            # once after meshing and before the file is written, so the nodes
            # a cell joins carry numbers close together and the matrix a
            # solver assembles is narrower. SU2 route only, and MEASURED so:
            # one sphere renumbered and written three ways gave a widest
            # node-number spread of 129 in the .su2 and in MSH 4.1 against
            # 458 unrenumbered -- and 458 in MSH 2.2, because that writer
            # indexes nodes in storage order and discards the new numbers.
            # Off by default: it changes every node number in the file, and a
            # mesh whose numbering is stable between runs is easier to diff.
            'renumber': BoolType(False),
        },
        'sizeFields': IntKeyList(gmshSizeField),
        'surfaceSizes': IntKeyList(gmshSurfaceSize),
        'curveControls': IntKeyList(gmshCurveControl),
        'volumeControls': IntKeyList(gmshVolumeControl),
        'periodicPairs': IntKeyList(gmshPeriodicPair),
    },
    # Plan 31. The per-surface options snappyHexMesh reads out of each
    # `geometry` entry, plus the closed-surface declaration. Case-wide on
    # purpose: they describe how the *staged* geometry is searched, and the
    # staging step writes one tri-surface per artifact with one set of import
    # units, so a per-surface control here would be five copies of the same
    # answer. Every leaf is either "as imported" or None, and the writer only
    # emits a key the user actually set, so a project that never opens this
    # page writes byte-for-byte the geometry block it wrote before.
    'snappyGeometry': {
        'triSurfaceDeclaration': EnumType(TriSurfaceDeclaration).setDefault(
            TriSurfaceDeclaration.AS_IMPORTED),
        # MEASURED on OpenFOAM 13, triSurfaceSearch.C:154. Dimensionless
        # perturbation tolerance for the intersection octree; the default is
        # indexedOctree::perturbTol(). Raising it is the documented remedy for
        # a gappy surface (`annotated/snappyHexMeshDict`: "non-default
        # tolerance on intersections").
        'tolerance': FloatType().setLowLimit(0, False).setOptional().setDefault(
            None),
        # MEASURED on OpenFOAM 13, triSurfaceSearch.C:160; default 10
        # (triSurfaceSearch.C:150). The annotated dictionary says to decrease
        # it "only in case of memory limitations", hence no default of ours.
        'maxTreeDepth': IntType().setRange(1, 20).setOptional().setDefault(None),
        # MEASURED on OpenFOAM 13, triSurface_searchableSurface.C:359.
        # Triangles below this quality are ignored when surface normals are
        # calculated. v13 only applies it when the value is > 0.
        'minQuality': FloatType().setRange(0, 1).setOptional().setDefault(None),
        # MEASURED on OpenFOAM 13, triSurface_searchableSurface.C:347.
        # FoamMesh stages geometry in metres, so this should normally stay
        # unset -- it is here for a surface file that reached the case in
        # other units, which is the case v13 documents ("CAD geometries are
        # often done in millimeters").
        'scale': FloatType().setLowLimit(0, False).setOptional().setDefault(None),
        # MEASURED on OpenFOAM 13, withGaps_searchableSurface.C:197,200. A
        # `withGaps` surface wraps another one and reports a gap narrower than
        # `gap` as closed, so snappy stops threading cells through a louvre or
        # a door seal. Off by default: it doubles the intersection tests.
        'gapDetection': BoolType(False),
        'gapWidth': FloatType().setLowLimit(0, False).setDefault(0.0001),
    },
    'baseGrid': {
        'sizingMode': EnumType(BaseGridSizingMode).setDefault(BaseGridSizingMode.COUNTS),
        'targetCellSize': FloatType().setLowLimit(0, False).setDefault(1.0),
        'numCellsX': IntType().setLowLimit(1).setDefault(10),
        'numCellsY': IntType().setLowLimit(1).setDefault(10),
        'numCellsZ': IntType().setLowLimit(1).setDefault(10),
        'boundingHex6': IntType().setOptional().setDefault(None),
        # blockMeshDict's ``scale``: the vertices are written in whatever unit
        # the geometry was imported in, and this is the factor to metres.
        'scale': FloatType().setLowLimit(0, False).setDefault(1),
        # R167. How far the derived background block stands off the geometry,
        # as a fraction of the geometry's largest span, applied to all six
        # faces. The page warns that a flush block splits every surface that
        # touches it between the patch you named and the leftover background
        # patch, and then offered nothing to act on: the span was six
        # read-only labels and the only remedy on the page was to model a
        # bounding Hex6 by hand. Zero is the block the page has always
        # derived, so a project that never touches this writes what it wrote.
        'standoff': FloatType().setLowLimit(0).setDefault(0),
        # Per-axis ``simpleGrading``.  One means uniform, which is what the
        # background block was hard-coded to before these existed.
        'grading': {
            'x': FloatType().setLowLimit(0, False).setDefault(1),
            'y': FloatType().setLowLimit(0, False).setDefault(1),
            'z': FloatType().setLowLimit(0, False).setDefault(1),
        },
        # The ``type`` given to each face of the background block.  snappy
        # keeps these patches for whatever the geometry does not cover, so a
        # domain that is a wind tunnel wall or a symmetry plane needs to say so
        # here -- it cannot be fixed afterwards without remeshing.
        'boundaryTypes': {
            'xMin': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
            'xMax': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
            'yMin': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
            'yMax': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
            'zMin': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
            'zMax': EnumType(BoundaryPatchType).setDefault(BoundaryPatchType.PATCH),
        },
        # Plan 31 CP-07 item 3. ``xMin``..``zMax`` are labels this product
        # invented for the sides of a box it derived; they are not a user's
        # inlet. Naming one makes the ownership explicit, and the name and the
        # category travel together to the group manifest so a report can say
        # which of the two it is looking at instead of presenting a generated
        # label as an intended category.
        'boundaryNames': {
            'xMin': TextType().setOptional(), 'xMax': TextType().setOptional(),
            'yMin': TextType().setOptional(), 'yMax': TextType().setOptional(),
            'zMin': TextType().setOptional(), 'zMax': TextType().setOptional(),
        },
        'boundaryCategories': {
            'xMin': EnumType(BoundaryCategory).setDefault(
                BoundaryCategory.UNCLASSIFIED),
            'xMax': EnumType(BoundaryCategory).setDefault(
                BoundaryCategory.UNCLASSIFIED),
            'yMin': EnumType(BoundaryCategory).setDefault(
                BoundaryCategory.UNCLASSIFIED),
            'yMax': EnumType(BoundaryCategory).setDefault(
                BoundaryCategory.UNCLASSIFIED),
            'zMin': EnumType(BoundaryCategory).setDefault(
                BoundaryCategory.UNCLASSIFIED),
            'zMax': EnumType(BoundaryCategory).setDefault(
                BoundaryCategory.UNCLASSIFIED),
        },
        # Plan 31 CP-07 items 1 and 2: the background domain's own topology,
        # authored rather than derived. Empty means the derived single box,
        # which is what every existing project has.
        'vertices': IntKeyList(backgroundVertex),
        'blocks': IntKeyList(backgroundBlock),
        'edges': IntKeyList(backgroundEdge),
        'patches': IntKeyList(backgroundPatch),
        'mergePairs': IntKeyList(backgroundMergePair),
    },
    'castellation': {
        'nCellsBetweenLevels': IntType().setDefault(3).setLowLimit(1),
        'resolveFeatureAngle': FloatType().setRange(0, 180).setDefault(30),
        'maxGlobalCells': IntType().setLowLimit(1).setDefault('1e8'),
        'maxLocalCells': IntType().setLowLimit(1).setDefault('1e7'),
        'minRefinementCells': IntType().setLowLimit(0).setDefault(0),
        'maxLoadUnbalance': FloatType().setRange(0, 1).setDefault('0.5'),
        'allowFreeStandingZoneFaces': BoolType(True),
        # Extra refinement in gaps, as an increment on the surface level.
        # Foundation 13 reads ``gapLevelIncrement`` in castellatedMeshControls;
        # the per-surface ``gapLevel`` triple of the ESI releases is not read
        # and is deliberately absent.
        'gapLevelIncrement': IntType().setLowLimit(0).setOptional().setDefault(None),
        'planarAngle': FloatType().setRange(0, 180).setOptional().setDefault(None),
        # C31-08. ``meshRefinement.C:1142`` reads this Switch out of
        # castellatedMeshControls, defaulting to true, and hands it to
        # ``refinementSurfaces::setMinLevelFields``: it decides whether a span
        # region's refinement is allowed to extend past the span itself.
        # DEFAULT leaves the key out and keeps OpenFOAM's own default.
        'extendedRefinementSpan': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        'useTopologicalSnapDetection': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        'handleSnapProblems': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        'refinementSurfaces': IntKeyList(surfaceRefinement),
        'refinementVolumes': IntKeyList(volumeRefinement),
        # C31-11. The ``levels`` ramp of the ``features`` entries. Empty means
        # every feature file keeps the single ``level`` the writer has always
        # emitted. See ``featureBand``.
        'featureBands': IntKeyList(featureBand),
    },
    'snap': {
        'nSmoothPatch': IntType().setLowLimit(0).setDefault(0),
        # ``nSmoothInternal`` is not read by OpenFOAM Foundation 13 snapControls
        # and was removed rather than shown as a control with no effect.
        'nSolveIter': IntType().setLowLimit(0).setDefault(30),
        'nRelaxIter': IntType().setLowLimit(0).setDefault(5),
        'nFeatureSnapIter': IntType().setLowLimit(0).setDefault(15),
        # F-41. These were one enum, written as each other's complement, so
        # the pair OpenFOAM 13 recommends for an STL with extracted feature
        # edges -- both true -- could not be asked for. They are two keys in
        # snapControls and they are two switches here. `migrateDocument` maps
        # a project saved on the old enum onto them.
        'implicitFeatureSnap': BoolType(False),
        'explicitFeatureSnap': BoolType(True),
        'multiRegionFeatureSnap': BoolType(False),
        # C31-11. ``snapParameters.C:44-46`` reads this Switch out of
        # snapControls with ``lookupOrDefault(..., true)`` and
        # ``snappySnapDriver.C:2647,2664`` uses it to pull points onto a
        # surface that is near them but not the one they were assigned to --
        # which is what keeps two walls a fraction of a cell apart from
        # collapsing into each other. DEFAULT leaves the key out.
        'detectNearSurfacesSnap': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        'tolerance': FloatType().setLowLimit(0, False).setDefault(3),
    },
    'addLayers': {
        # Plan 29 WP7.3. Layer thickness is per group, not per case: the six
        # keys that used to sit here commented out live in the ``layer``
        # schema above, which is what ThicknessForm reads and writes.
        'layers': IntKeyList(layer),
        'nGrow': IntType().setLowLimit(0).setDefault(0),
        'featureAngle': FloatType().setRange(0, 180).setDefault(60),
        # v13 medialAxisMeshMover defaults this to featureAngle/2; carrying it
        # explicitly matches the Foundation tutorials, which all set it.
        'slipFeatureAngle': FloatType().setRange(0, 180).setDefault(30),
        'maxFaceThicknessRatio': FloatType().setRange(0, 1).setDefault(0.5),
        'nSmoothSurfaceNormals': IntType().setLowLimit(0).setDefault(1),
        'nSmoothThickness': IntType().setLowLimit(0).setDefault(10),
        # OpenFOAM 13's medialAxisMeshMover reads this through
        # ``lookupBackwardsCompatible({"minMedialAxisAngle",
        # "minMedianAxisAngle"})`` -- canonical spelling first, the old
        # misspelling kept only for compatibility. Plan 30 F-18: the
        # schema carried the misspelling, so every project and every
        # dictionary FoamMesh wrote spelled it the way the release
        # merely tolerates. ``migrateDocument`` renames it on load.
        'minMedialAxisAngle': FloatType().setRange(0, 180).setDefault(90),
        'maxThicknessToMedialRatio': FloatType().setRange(0, 1).setDefault(0.3),
        'nSmoothNormals': IntType().setLowLimit(0).setDefault(3),
        'nRelaxIter': IntType().setLowLimit(0).setDefault(10),
        'nBufferCellsNoExtrude': IntType().setLowLimit(0).setDefault(0),
        'nLayerIter': IntType().setLowLimit(0).setDefault(50),
        'nRelaxedIter': IntType().setLowLimit(0).setDefault(20),
        # The medial-axis mover's own iteration and smoothing limits.  Left
        # unset they keep OpenFOAM's defaults; a layer run that stalls on a
        # thin feature is usually short of nMedialAxisIter.
        'nMedialAxisIter': IntType().setLowLimit(0).setOptional().setDefault(None),
        'nSmoothDisplacement': IntType().setLowLimit(0).setOptional().setDefault(None),
        'detectExtrusionIsland': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        'additionalReporting': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        # ``nOuterIter`` is an ESI key; Foundation 13 does not read it, so
        # there is no control for it here.
        'meshShrinker': EnumType(MeshShrinker).setDefault(
            MeshShrinker.MEDIAL_AXIS),
    },
    'meshQuality': {
        'maxNonOrtho': FloatType().setDefault(65),
        'maxBoundarySkewness': FloatType().setDefault(20),
        'maxInternalSkewness': FloatType().setDefault(4),
        'maxConcave': FloatType().setDefault(80),
        'minVol': FloatType().setDefault('-1e30'),
        'minTetQuality': FloatType().setDefault('1e-15'),
        'minVolCollapseRatio': FloatType().setDefault(-1),
        'minArea': FloatType().setDefault(-1),
        'minTwist': FloatType().setDefault(0.02),
        'minDeterminant': FloatType().setDefault(0.001),
        'minFaceWeight': FloatType().setDefault(0.05),
        'minVolRatio': FloatType().setDefault(0.01),
        'nSmoothScale': IntType().setDefault(4),
        'errorReduction': FloatType().setDefault(0.75),
        'mergeTolerance': FloatType().setDefault(1e-6),
        # The thresholds snappy falls back to while adding layers, when the
        # strict ones cannot be met.  Only maxNonOrtho has ever been written;
        # the rest stay unset so an existing case keeps the mesh it had, and
        # each one that is set is written beside it.
        'relaxed': {
            'maxNonOrtho': FloatType().setDefault(75),
            'maxBoundarySkewness': FloatType().setOptional().setDefault(None),
            'maxInternalSkewness': FloatType().setOptional().setDefault(None),
            'maxConcave': FloatType().setOptional().setDefault(None),
            'minVol': FloatType().setOptional().setDefault(None),
            'minTetQuality': FloatType().setOptional().setDefault(None),
            'minVolCollapseRatio': FloatType().setOptional().setDefault(None),
            'minArea': FloatType().setOptional().setDefault(None),
            'minTwist': FloatType().setOptional().setDefault(None),
            'minDeterminant': FloatType().setOptional().setDefault(None),
            'minFaceWeight': FloatType().setOptional().setDefault(None),
            'minVolRatio': FloatType().setOptional().setDefault(None),
        }
    },
    # Plan 31 (checkmesh.thresholds_and_region + checkmesh.write_surfaces).
    # How the mesh is *judged*, which is not the same thing as the limits the
    # mesher was given. `meshQuality` above is snappyHexMesh's own
    # meshQualityControls; this block is checkMesh's command line. The two
    # were conflated on the QA page for as long as the page has existed, and
    # `userDefinedChecks` below is the one control that deliberately ties
    # them together -- it hands the mesher's limits back to checkMesh as
    # user-defined criteria, which is a thing a user may want and never a
    # thing that should happen behind their back.
    #
    # Every default here is OpenFOAM 13's own (`checkMesh -help`:
    # non-orthogonality 70, skewness 4) or off, so the command line a case
    # generates is byte-identical to the one it generated before this block
    # existed.
    'meshCheck': {
        # MEASURED on OpenFOAM 13, build 13-58ed5c2046ef, on a sheared block:
        # at the default 70 the mesh is "Mesh OK."; at 35 the same mesh
        # reports 1140 severely non-orthogonal faces and "Failed 1 mesh
        # checks." The threshold decides the verdict, and until now no user
        # could reach it.
        'nonOrthThreshold': FloatType().setRange(0, 180).setDefault(70),
        'skewThreshold': FloatType().setLowLimit(0).setDefault(4),
        # `-meshQuality`: read user-defined criteria from
        # system/meshQualityDict. MEASURED: enables "user-defined geometry
        # checks" and writes the offending faces to a `meshQualityFaces`
        # faceSet. Off by default; when on, the case writer emits
        # system/meshQualityDict from the same meshQuality record that fills
        # snappyHexMeshDict's meshQualityControls.
        'userDefinedChecks': BoolType(False),
        # `-noTopology`: skip the topology checks. For a mesh whose topology
        # is known-good and whose check is slow. MEASURED to skip the whole
        # "Checking topology" section.
        'skipTopology': BoolType(False),
        # `-writeSurfaces`: reconstruct and write the faceSets and cellSets of
        # the problem faces as a surface, under
        # postProcessing/checkMesh/<instance>/. MEASURED as strictly separate
        # from `-writeSets`, which writes the sets themselves into
        # constant/polyMesh/sets: with only `-writeSets` the sets appear and
        # postProcessing stays empty; with only `-writeSurfaces` the reverse.
        # There is no `surfaceFormat` key. v13's checkMesh reads that one
        # option into both its surface writer and its set writer
        # (checkMesh.C:164-168), and this product always passes -writeSets,
        # so any value but the default aborts the check. Measured: exit 1,
        # "Unknown write type obj".
        'writeSurfaces': BoolType(False),
    },
    # Plan 31 (surface_features.rest). Everything v13's ``surfaceFeatures``
    # reads beyond ``surfaces`` and ``includedAngle``, which were the only two
    # keys this product could write. VERIFIED against
    # ``applications/utilities/surface/surfaceFeatures/surfaceFeatures.C`` on
    # build ``13-58ed5c2046ef``, line numbers in the comments below.
    #
    # Every default here is the utility's own, and the writer emits a key only
    # where it differs, so an existing case gets the same surfaceFeaturesDict
    # it got before this block existed.
    #
    # Deliberately absent, with reasons:
    #   curvature       -- MEASURED to segfault (exit 139) on this build, on a
    #                      clean closed-manifold sphere, reproduced three
    #                      times. A control that crashes the utility is worse
    #                      than no control.
    #   subsetFeatures/{insideBox,outsideBox,plane}
    #                   -- each needs a spatial picker in the viewport; a
    #                      typed-in bounding box is a trap.
    #   addFeatures     -- reads an ``extendedFeatureEdgeMesh`` from
    #                      constant/, which nothing in this product produces.
    #                      (Note: v13 reads only ``name`` from it. There is no
    #                      ``flip`` key; that one is ESI's.)
    #   files, baffles  -- name external surfaces this product does not stage.
    'surfaceFeatures': {
        # L112. Extract from the geometry alone, ignoring the surface's own
        # region boundaries, so a tessellation split into patches does not
        # gain a feature edge at every patch seam.
        'geometricTestOnly': BoolType(False),
        # L222-224. Both are "off" at zero: the utility trims only when
        # ``minElem > 0 || minLen > 0``. This is the filter for the spurious
        # short edges a poor tessellation produces.
        'trimMinLength': FloatType().setLowLimit(0).setDefault(0),
        'trimMinElements': IntType().setLowLimit(0).setDefault(0),
        # L282, L294. The utility keeps both by default ("yes"); turning one
        # off *removes* those edges from the feature set. Named for what they
        # do rather than for the dictionary key, which reads backwards.
        'keepNonManifoldEdges': BoolType(True),
        'keepOpenEdges': BoolType(True),
        # L447-465. ``pointCloseness`` is not here: it is not a preference,
        # it is a requirement of span-based refinement and the case writer
        # turns it on for exactly the surfaces that need it.
        'faceCloseness': BoolType(False),
        'internalAngleTolerance': FloatType().setRange(0, 180).setDefault(80),
        'externalAngleTolerance': FloatType().setRange(0, 180).setDefault(80),
        # L174 and L617. ``maxFeatureProximity`` is MUST_READ once
        # ``featureProximity`` is on -- the utility aborts without it -- so
        # the writer emits the pair together or neither.
        'featureProximity': BoolType(False),
        'maxFeatureProximity': FloatType().setLowLimit(0).setDefault(1),
        # L165-169. Diagnostics. ``verboseObj`` is only meaningful with
        # ``writeObj``; the writer says so rather than silently dropping it.
        'writeObj': BoolType(False),
        'verboseObj': BoolType(False),
        # Stored as `writeVtk` so the facade's snake-case id reads
        # `write_vtk` rather than `write_v_t_k`; the dictionary key the
        # writer emits is v13's own `writeVTK`.
        'writeVtk': BoolType(False),
    },
    'snappyAdvanced': {
        # C31-11. ``snappyHexMesh.C:715`` reads ``keepPatches`` off the top
        # level of the dictionary with ``lookupOrDefault(..., false)``, and
        # three call sites (``:1168``, ``:1214``, ``:1271``) use it to decide
        # whether to delete patches that ended the run with no faces. With it
        # on, a patch that was named but never intersected still exists in
        # ``constant/polyMesh/boundary`` -- which is what a downstream case
        # setup expecting a fixed patch list needs. DEFAULT leaves the key
        # out, so a case written before this control is unchanged.
        'keepPatches': EnumType(OptionalToggle).setDefault(
            OptionalToggle.DEFAULT),
        # snappyHexMesh's own diagnostic output.  Every flag is off by default
        # and nothing is written unless one is on, so a case that never opens
        # this page produces the dictionary it always did.
        'writeFlags': {
            'scalarLevels': BoolType(False),
            'layerSets': BoolType(False),
            'layerFields': BoolType(False),
        },
        'debugFlags': {
            'mesh': BoolType(False),
            'intersections': BoolType(False),
            'featureSeeds': BoolType(False),
            'attraction': BoolType(False),
            'layerInfo': BoolType(False),
        },
    }
}


#: Every place this document quotes the key of one of its own lists.
#:
#: A list key is an identity, and a referring field is an ordinary
#: ``IntType()``: nothing in the schema distinguishes ``volume``, which names
#: another geometry row, from ``nSurfaceLayers``, which is a count. So the
#: quotations have to be declared, and this is the declaration. It is read by
#: the merge, which renumbers an element when two working copies both added
#: one at the same key (DP-16) and has to carry every reference with it.
#:
#: ``castellationGroup`` appears twice on purpose. One field keys into two
#: different lists -- a surface row's group is a ``refinementSurfaces`` key
#: and a volume row's is a ``refinementVolumes`` key -- and only ``gType``
#: says which, exactly as ``castellation_page`` reads it.
#:
#: A list left out here is one whose keys nothing quotes. That is true of
#: ``region``, ``interfacePairs`` and the ``gmsh`` collections, which reach
#: geometry through prepared scope *names* rather than row ids, and of the
#: ``baseGrid`` collections, whose ``vertices``/``start``/``end`` are
#: positions in the numerically sorted list rather than keys of it -- and a
#: renumber only ever appends, so those positions do not move.
ID_REFERENCES = (
    # A surface row names the volume row that owns it.
    IdReference('geometry', 'geometry/*/volume'),
    # A geometry row names its castellation refinement group.
    IdReference('castellation/refinementSurfaces', 'geometry/*/castellationGroup',
                condition=('gType', GeometryType.SURFACE.value)),
    IdReference('castellation/refinementVolumes', 'geometry/*/castellationGroup',
                condition=('gType', GeometryType.VOLUME.value)),
    # A geometry row names its layer group, once per side of a baffle.
    IdReference('addLayers/layers', 'geometry/*/layerGroup'),
    IdReference('addLayers/layers', 'geometry/*/slaveLayerGroup'),
)


#: Lists whose elements are named by *position* rather than by key, and which
#: therefore must never be renumbered behind an author's back.
#:
#: ``baseGrid/vertices`` is the one. A block's ``vertices`` is eight indices
#: into the vertex collection read in numeric key order, an edge's
#: ``start``/``end`` are two more, and a patch's ``faces`` are chunks of four
#: -- see ``background_mesh.from_records``, which builds the coordinate list
#: and then indexes into it. Nothing quotes a vertex *key*, so ``ID_REFERENCES``
#: has nothing to carry; what moves is every index at once.
#:
#: The DP-16 renumber puts an incoming addition above every key either side
#: holds. On a keyed list that is invisible to the referrers, which is the
#: whole point. On this one it is not: the other copy's vertices now occupy
#: the positions the incoming copy counted from, so the incoming blocks,
#: edges and patches silently name somebody else's corners. Two authors each
#: extending one background topology are not adding independent elements --
#: they are editing one indexed sequence -- so the merge reports the collision
#: instead of renumbering it away.
#:
#: A copy that adds only blocks, edges or patches is not affected: those are
#: renumbered as usual, because appending vertices never moves an existing
#: vertex's position, and only a collision *within the vertex list* shifts it.
POSITION_ADDRESSED_LISTS = (
    'baseGrid/vertices',
)


def migrateDocument(document):
    """Rewrite a saved document onto the current field names, in place.

    There is no version ladder here on purpose (see ``mesh/targetSolver``):
    the loader refuses any version it does not recognise, so bumping
    ``CURRENT_CONFIGURATIONS_VERSION`` for a field change would make every
    existing project unopenable. Absent leaves are filled from the schema by
    ``validateData(fillWithDefault=True)``, and keys the schema no longer
    names are dropped by it -- which is why a *renamed* field has to be read
    here, before validation throws the old spelling away.

    Three renames are handled:

    * ``snap/featureSnapType`` (an enum) becomes ``implicitFeatureSnap`` and
      ``explicitFeatureSnap``. ``implicit`` sets the implicit switch only,
      ``explicit`` sets the explicit switch only -- exactly the pair the old
      writer emitted, so no existing project changes mesh.
    * a layer group with no ``layerPolicy`` gets one from its count: one or
      more layers is ``grow``, zero is ``freeze``.
    * ``addLayers/minMedianAxisAngle`` becomes ``minMedialAxisAngle`` (Plan 30
      F-18). OpenFOAM 13 spells it *medial*; the misspelling survives in the
      release only as the second name of a ``lookupBackwardsCompatible`` pair,
      and it survived here because nothing ever compared the two. The value a
      user set is carried across, so no project loses a tuned angle.

    One removal is handled: ``mesh/intent`` (Plan 30 WP-09, F-23). Validation
    would drop it anyway; it is dropped here so that a case saved with the six
    sizing fields never reaches a reader that still expects them, and so that
    the removal has a name and a test rather than being a silent side effect
    of an unrelated pass.
    """
    if not isinstance(document, dict):
        return document
    mesh = document.get('mesh')
    if isinstance(mesh, dict):
        mesh.pop('intent', None)
    snap = document.get('snap')
    if isinstance(snap, dict) and 'featureSnapType' in snap:
        stored = snap.pop('featureSnapType')
        stored = getattr(stored, 'value', stored)
        implicit = str(stored) == FeatureSnapType.IMPLICIT.value
        snap.setdefault('implicitFeatureSnap', implicit)
        snap.setdefault('explicitFeatureSnap', not implicit)
    addLayers = document.get('addLayers')
    if isinstance(addLayers, dict) and 'minMedianAxisAngle' in addLayers:
        retired = addLayers.pop('minMedianAxisAngle')
        addLayers.setdefault('minMedialAxisAngle', retired)
    layers = addLayers.get('layers') if isinstance(addLayers, dict) else None
    if isinstance(layers, dict):
        for group in layers.values():
            if not isinstance(group, dict) or 'layerPolicy' in group:
                continue
            try:
                count = int(str(group.get('nSurfaceLayers', 1)).strip() or 1)
            except ValueError:
                count = 1
            group['layerPolicy'] = (
                LayerPolicy.GROW.value if count >= 1
                else LayerPolicy.FREEZE.value)
    return document
