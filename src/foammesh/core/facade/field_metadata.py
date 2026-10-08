"""Hand-authored, reviewed metadata overlay for the field registry (AF2).

The SimpleDB schema carries types and ranges but not units, documentation,
staleness/invalidation, applicability, or UI location. The plan requires this
metadata to be hand-authored and reviewed; it is expressed here as reviewed
per-group defaults plus targeted per-field overrides so the full inventory is
covered without inferring semantics from widget classes.

``invalidates`` values are the artifact fingerprints a change stales:
``mesh`` (the delivered mesh), the five ``mesh.*`` snappyHexMesh stages that
build it, ``quality`` (the checkMesh report), ``exports`` (the files written
from the mesh), ``engine`` and ``engine_plan``.
``ui_location`` names the workflow step/page that renders the field.
"""
from __future__ import annotations

#: The delivered mesh, whatever built it. Every mesh-staling closure contains
#: it, so a reader that only ever asked "is the mesh stale" keeps its answer.
MESH_FINGERPRINT = 'mesh'

#: Plan 32 W4 (DP-242). The snappyHexMesh stages, in the order the runner
#: executes them: `core/engine/snappy.py` declares `snappy.surface_features`
#: depends_on `snappy.base_grid`, `snappy.castellation` on
#: `snappy.surface_features`, `snappy.snap` on `snappy.castellation` and
#: `snappy.layers` on `snappy.snap`. Each stage is handed the result of the
#: one above, so staling a stage stales every stage after it and nothing
#: before it -- which is an ordering, and therefore this one table rather
#: than a closure spelled out on 180 field rows.
#:
#: Gmsh is deliberately not staged here. It configures its pages first and
#: runs once at Generate mesh (plan 32 section 4.5), so there is no
#: intermediate Gmsh artifact a later edit could leave standing: a Gmsh field
#: says `mesh` and means all of it.
MESH_STAGE_ORDER: tuple[str, ...] = (
    'mesh.base_grid',
    'mesh.surface_features',
    'mesh.castellated',
    'mesh.snapped',
    'mesh.layers',
)

#: The fingerprints that are not mesh artifacts: the checkMesh report, the
#: files written from the mesh, the engine identity and the engine plan.
REPORT_FINGERPRINTS: tuple[str, ...] = (
    'quality', 'exports', 'engine', 'engine_plan',
)

#: The whole reviewed vocabulary. A value in `invalidates` that is not in
#: here names an artifact nothing else in the product spells, so it stales
#: nothing -- which is what `export` did before DP-243.
FINGERPRINTS: tuple[str, ...] = (
    (MESH_FINGERPRINT,) + MESH_STAGE_ORDER + REPORT_FINGERPRINTS
)


#: Plan 37 UF15. The snappyHexMesh stage each staged fingerprint names, as
#: the engine descriptor spells it (``WorkflowTask.engine_stage``), so an
#: engine-specific invalidation can find the task whose result it stales.
MESH_STAGE_ENGINE_STAGES: dict[str, str] = {
    'mesh.base_grid': 'blockMesh',
    'mesh.surface_features': 'surfaceFeatures',
    'mesh.castellated': 'castellation',
    'mesh.snapped': 'snap',
    'mesh.layers': 'layers',
}


def earliest_stage(fingerprints) -> str | None:
    """The first meshing stage among ``fingerprints``, or ``None``."""
    named = set(fingerprints or ())
    return next((stage for stage in MESH_STAGE_ORDER if stage in named), None)


def stage_closure(fingerprint: str) -> tuple[str, ...]:
    """The fingerprints staling ``fingerprint`` also stales.

    A snappy stage stales itself, every stage after it, and the delivered
    mesh. Anything else stands alone: `mesh` is the finished artifact rather
    than a sixth stage, so naming it does not reach back into the stages that
    happen to have produced it on the snappy route.

    A `mesh.*` name with no place in ``MESH_STAGE_ORDER`` has no closure to
    derive, and answering "nothing follows it" would quietly hand back a
    stage edit that stales less than it should.
    """
    fingerprint = str(fingerprint)
    if fingerprint in MESH_STAGE_ORDER:
        index = MESH_STAGE_ORDER.index(fingerprint)
        return (MESH_FINGERPRINT,) + MESH_STAGE_ORDER[index:]
    if fingerprint.startswith(MESH_FINGERPRINT + '.'):
        raise ValueError(
            f'{fingerprint!r} is not a meshing stage; the stages, in order, '
            f'are {", ".join(MESH_STAGE_ORDER)}')
    return (fingerprint,)


def expand_fingerprints(fingerprints) -> tuple[str, ...]:
    """The union of the closures of ``fingerprints``, sorted.

    Idempotent, so a caller holding an already-expanded tuple -- a stored
    `invalidates`, an `invalidated_outputs` coming back from an operation --
    may pass it straight back in.
    """
    resolved: set[str] = set()
    for fingerprint in fingerprints or ():
        resolved.update(stage_closure(fingerprint))
    return tuple(sorted(resolved))


def stales(*fingerprints: str) -> tuple[str, ...]:
    """What a field declaring ``fingerprints`` stales, closure included.

    Used at the group rows below so the closure is derived from
    ``MESH_STAGE_ORDER`` in one place. A group names the earliest stage it
    disturbs; the table supplies the rest.
    """
    return expand_fingerprints(fingerprints)


# Fallback when a field's group is not listed below. An unclassified field is
# assumed to reach the mesher, so it stales from the first stage onwards.
DEFAULT_INVALIDATION = stales('mesh.base_grid', 'quality')


# Keyed by storage-path prefix (longest match wins). ``units`` is keyed by the
# semantic leaf (the final dotted segment of the field ID).
GROUP_METADATA: dict[str, dict] = {
    'step': {
        'invalidates': (),
        'ui_location': 'workflow.step',
    },
    'mesh': {
        'invalidates': (),
        'ui_location': 'application.diagnostics',
    },
    'mesh/execution': {
        # How the run is decomposed and how many ranks it gets. The mesher
        # starts over from the background grid, so this names the first stage
        # and the table stales the rest.
        'invalidates': stales('engine_plan', 'mesh.base_grid', 'quality'),
        'ui_location': 'preferences.execution',
    },
    'geometryPreparation': {
        'invalidates': (),
        'ui_location': 'workflow.geometry_repair',
    },
    'geometryPreparation/qualificationToleranceM': {
        # Plan 28 WP6. The project-wide fidelity tolerance. It changes what the
        # fidelity and resolution reports conclude, not what the mesher does,
        # so the mesh stands and only the verdicts have to be recomputed.
        # Edited on Reference Readiness, which is where the rest of "something
        # to measure against" lives.
        'invalidates': ('quality',),
        # Not `workflow.quality`: that page is snappyHexMesh's sixteen
        # meshQuality thresholds, and this is a project-wide geometry
        # tolerance rendered on the reference-readiness task.
        'ui_location': 'workflow.reference_readiness',
    },
    'geometryPreparation/defaultBoundaryCategory': {
        # Changing the fallback category retypes published patches, so any
        # mesh already published from this geometry is no longer trustworthy.
        #
        # DP-243. This row said `export` where the other 77 fields said
        # `exports`. `exports` is the name `Project.markArtifactChanged`
        # publishes and every export operation reports, so `export` named an
        # artifact nothing compares against and a retyped patch never staled
        # a file that had already been written from the old typing.
        'invalidates': stales('mesh.base_grid', 'quality', 'exports'),
        'ui_location': 'workflow.geometry_repair',
    },
    'gmsh': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh',
        'applies_when': ('mesh.engine == gmsh',),
    },
    # Plan 32 section 4.6. Eight engine-level Gmsh scalars sat at the bare
    # `workflow.gmsh` location, which names no first-column tab, so the
    # inventory could not say which page owned them. Neither group is a
    # floating preference: each belongs to a page in section 4.3.
    'gmsh/dimensionality': {
        # Whether the model is meshed as a volume, a plane or a wedge, and
        # the numbers the publisher builds the second layer of nodes from.
        # `core/gmsh/fields.py` marks `mode` RUNNER -- it chooses the
        # argument to `gmsh.model.mesh.generate` -- and the other five
        # PUBLISH, read by the publisher that turns the section into an
        # OpenFOAM cell. Both happen at Generate mesh (section 4.4:
        # `gmsh.compute` plus `gmsh.publish` when the target needs it).
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.compute',
        'applies_when': ('mesh.engine == gmsh',),
    },
    'gmsh/structuring': {
        # Transfinite structuring: what shape the cells come out, not where
        # they are. `core/gmsh/fields.py` marks both RUNNER, and `automatic`
        # carries the same target guard recombination carries -- section 4.3
        # puts cell-shape and recombination on Global sizing, beside the
        # algorithms that decide the same question.
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
    },
    'gmsh/healing': {
        # Healing changes what Gmsh imports, so everything downstream is stale.
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.describe_geometry',
        'applies_when': ('mesh.engine == gmsh',),
        # Plan 31. `booleanTolerance` is a length in model units like the
        # import tolerance; `importScaling` multiplies the imported shape, so
        # it is a ratio and not a length.
        'units': {'import_tolerance': 'm', 'boolean_tolerance': 'm',
                  'import_scaling': 'ratio',
                  # DP-204. One to 180, and nothing on screen said of what.
                  'classification_angle': 'deg'},
    },
    'gmsh/globalSizing': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
        # DP-204. Three more were authored here: points on a circle,
        # points on a curve, elements per two pi. Each named a noun its own
        # label already says, and a control names its quantity once, so the
        # label keeps them and the column beside it stays empty.
        'units': {'target_size': 'm', 'minimum_size': 'm',
                  'size_factor': 'ratio', 'from_curvature': 'elements'},
        # The combiner is a choice, not a quantity, so it carries no unit.
    },
    'gmsh/algorithms': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
    },
    'gmsh/farfield': {
        # It changes the domain itself, so everything downstream of the mesh
        # is a different problem, not merely a different discretisation.
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.describe_geometry',
        # Plan 37 UF14: snappyHexMesh reads the same record as its outer
        # boundary, so the one engine that ignores it is none at all.
        'applies_when': ('mesh.engine != unselected',),
        'units': {'padding': 'diagonals', 'radius': 'm', 'length': 'm'},
    },
    'gmsh/parallel': {
        # Not just wall clock: HXT partitions the domain by thread count, so
        # the same job at a different thread count is a different mesh.
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
        # DP-204. The one field here is called Threads, so a column
        # saying threads said it twice.
    },
    'gmsh/optimization': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.compute',
        'applies_when': ('mesh.engine == gmsh',),
        # DP-204. Both of these run 0 to 1, which is this product's
        # fraction and not its ratio -- a ratio here is a growth factor and
        # goes above 1. Netgen passes lost its column because its label
        # already counts passes, and the high-order optimiser lost one
        # because a Gmsh mode number is a code, not a quantity.
        'units': {'min_quality': 'fraction', 'optimize_threshold': 'fraction',
                  'smoothing': 'steps'},
    },
    'gmsh/boundaryLayers': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.boundary_layers',
        'applies_when': ('mesh.engine == gmsh',),
        # DP-204. The layer count counts layers in its own label, and
        # the growth ratio says ratio in its own, so neither takes a column.
        'units': {'first_height': 'm', 'total_thickness': 'm'},
    },
    'gmsh/sizeFields': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.size_fields',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'size_inside': 'm', 'size_outside': 'm',
                  'distance_min': 'm', 'distance_max': 'm',
                  'radius': 'm', 'radius_end': 'm', 'thickness': 'm',
                  'sampling': 'points', 'curvature_delta': 'm',
                  'curvature_min': '1/m', 'curvature_max': '1/m',
                  # DP-606. A vector's components share the vector's unit.
                  'centre': 'm', 'box_min': 'm', 'box_max': 'm',
                  # DP-607. The axis is a length vector, not a direction.
                  'axis': 'm'},
    },
    'gmsh/surfaceSizes': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.size_fields',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'target_size': 'm', 'blend_distance': 'm'},
    },
    'gmsh/output': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.compute',
        # C31-14d. This clause used to read `target_solver == su2`, written
        # when SU2 was believed to be the route that carried second-order
        # elements. CP-01 measured that it is not: EXPORTER_ELEMENT_ORDERS in
        # core/gmsh/plan_derivation.py gives openfoam (1,) and su2 (1,), and
        # FoamMesh's own SU2 reader holds the linear VTK codes only, so a
        # quadratic SU2 file is one this product would write and then be
        # unable to open. Both named exporters refuse order 2; a target that
        # names no exporter constrains nothing, and the mesher itself does
        # both orders. So the control is live exactly where no exporter has
        # a say -- which is the opposite of what the old clause said, and the
        # old clause greyed the spin box out on the only route that works.
        'applies_when': ('mesh.engine == gmsh',
                         'mesh.target_solver != openfoam',
                         'mesh.target_solver != su2'),
        # DP-204. The element order is 1 or 2 and order is not a
        # dimension, so the number stands on its own.
    },
    'gmsh/curveControls': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.curve_controls',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'local_size': 'm', 'segments': 'elements',
                  'coefficient': 'ratio'},
    },
    'gmsh/volumeControls': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.volume_controls',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'target_size': 'm'},
    },
    'gmsh/periodicPairs': {
        'invalidates': stales('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.periodic',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'match_tolerance': 'm', 'rotation_angle_degrees': 'deg',
                  'translation': 'm', 'rotation_centre': 'm'},
    },
    'geometry': {
        # The shape everything downstream is built on, so the mesher starts
        # again from the background grid.
        'invalidates': stales('mesh.base_grid', 'quality'),
        'ui_location': 'workflow.geometry',
    },
    'interfacePairs': {
        # DP-243. `export` here was the same misspelling of `exports` that
        # `defaultBoundaryCategory` carried.
        'invalidates': stales('engine_plan', 'mesh.base_grid', 'quality',
                              'exports'),
        'ui_location': 'workflow.geometry.interfaces',
    },
    'region': {
        'invalidates': stales('mesh.base_grid', 'quality'),
        'ui_location': 'workflow.region',
    },
    'baseGrid': {
        # The first snappy stage: the background hex grid every later stage
        # refines, snaps and layers. Nothing precedes it, so nothing it
        # stales is behind it.
        'invalidates': stales('mesh.base_grid', 'quality'),
        'ui_location': 'workflow.base_grid',
    },
    'castellation': {
        # The twelve castellatedMeshControls entries the runner reads at
        # `snappy.castellation` (`core/engine/snappy.py`) and writes into
        # snappyHexMeshDict (`openfoam/case_builder.py`). They change which
        # cells the refinement keeps, so the checkMesh verdict recorded on
        # the old cells no longer describes them -- which is why these carry
        # `quality` and the nine `snappyAdvanced` diagnostics below do not.
        'invalidates': stales('mesh.castellated', 'quality'),
        'ui_location': 'workflow.castellation',
        'units': {'resolve_feature_angle': 'deg', 'max_load_unbalance': 'fraction',
                  'planar_angle': 'deg'},
    },
    # Plan 37 UF16. The exclude points sit under `castellation` because
    # castellation is the stage that reads them (`outsidePoints`), and they
    # are authored on Domain & regions beside the seeds they answer to.
    # Editing one stales the castellated mesh onward, never the base grid.
    'castellation/excludePoints': {
        'invalidates': stales('mesh.castellated', 'quality'),
        'ui_location': 'workflow.region',
        'units': {'point': 'm'},
    },
    'snap': {
        'invalidates': stales('mesh.snapped', 'quality'),
        'ui_location': 'workflow.snap',
        # ``concaveAngle`` and ``minAreaRatio`` are ESI snapControls keys.
        # Foundation 13 does not read either, the controls were removed, and
        # these two entries described nothing. (Plan 37 UF15: Foundation 13
        # does read a ``concaveAngle`` -- in addLayersControls, below.)
        'units': {},
    },
    'addLayers': {
        # The last snappy stage. Plan 32 check 17: editing one of these after
        # Snap stales the layers and the reports, and leaves the base grid,
        # the feature edges, the castellated cells and the snapped surface
        # the run already produced exactly as valid as they were.
        'invalidates': stales('mesh.layers', 'quality'),
        'ui_location': 'workflow.layers',
        # ``min_medial_axis_angle``: Plan 30 WP-04 (F-18) renamed the schema
        # path to OpenFOAM 13's own spelling, so the unit names it too. The
        # misspelling survives in the release only as the second name of a
        # ``lookupBackwardsCompatible`` pair, and in saved projects only until
        # ``migrateDocument`` reads them.
        'units': {'feature_angle': 'deg', 'min_medial_axis_angle': 'deg',
                  'slip_feature_angle': 'deg',
                  'max_face_thickness_ratio': 'fraction',
                  'max_thickness_to_medial_ratio': 'fraction',
                  # Plan 37 UF15: read with ``unitDegrees``.
                  'concave_angle': 'deg'},
    },
    'snappyGeometry': {
        # Plan 31. These change what snappyHexMesh is told about the staged
        # surfaces -- whether they enclose a volume, how finely they are
        # searched, how narrow a gap counts as closed -- so the mesh already
        # produced was built on a different description and no longer stands.
        # The description is read before the background grid is refined, so
        # this names the first stage and every stage after it follows.
        'invalidates': stales('mesh.base_grid', 'quality', 'exports'),
        'ui_location': 'workflow.domain_regions',
        'applies_when': ('mesh.engine == snappy',),
        'units': {'gap_width': 'm'},
    },
    'snappyAdvanced': {
        # snappyHexMesh's own diagnostic output. The cells a finished run
        # produced would come out identical, but the files asked for here are
        # written *during* meshing and cannot be recovered from a mesh already
        # on disk -- asking for them means the mesh output is now incomplete,
        # and only a re-run fills it in. The checkMesh verdict is about the
        # cells, so it still stands.
        #
        # Plan 32 W4 settles the Castellation page's split. The nine rows
        # here and the twelve above share a page and differ in `quality` for
        # the reason above; they share a *stage* because the runner reads all
        # twenty-one at `snappy.castellation` (`core/engine/snappy.py`) and
        # `case_builder.py` writes them into the one snappyHexMeshDict --
        # `keepPatches`, `writeFlags` and `debugFlags` at the top level, the
        # twelve inside `castellatedMeshControls`. So the stage that has to
        # run again is the castellated one, and the two stages after it.
        'invalidates': stales('mesh.castellated'),
        'ui_location': 'workflow.castellation',
    },
    'meshQuality': {
        # Quality thresholds gate the checkMesh report; on an engine that
        # does not mesh against them they do not invalidate the mesh already
        # produced.
        'invalidates': ('quality',),
        # Plan 37 UF15. snappyHexMesh does mesh against them: OpenFOAM 13's
        # snap motion smoother and addLayers both read meshQualityControls
        # and revert a step that breaches one. So on the snappy route an
        # edit makes the snapped mesh onward stale, and Gmsh -- which never
        # reads snappy's meshQualityControls -- keeps the quality-only rule.
        'invalidates_by_engine': {
            'snappy': stales('mesh.snapped', 'quality'),
        },
        'ui_location': 'workflow.quality',
        # DP-170. Two of these are angles and two are not. OpenFOAM 13
        # says so itself: `src/meshCheck/checkMesh.C` reads maxNonOrtho
        # and maxConcave with `unitDegrees` and every other limit as a
        # bare scalar, and `meshQualityDict` introduces the pair below
        # as "Max skewness allowed" -- a ratio of distances, not an
        # angle. minVol is not a volume either: checkMesh multiplies it
        # by the bounding box's smallest dimension cubed before use, so
        # it is a fraction. minArea is the one that really is measured
        # in metres, compared against a face area as it stands.
        'units': {'max_non_orthogonality': 'deg', 'max_concave': 'deg',
                  'min_area': 'm²'},
    },
    # Plan 31. checkMesh's own command line. Changing it changes the verdict
    # on a mesh that is already built, so it invalidates the quality report
    # and nothing else -- the same rule `meshQuality` above follows for the
    # same reason.
    'meshCheck': {
        'invalidates': ('quality',),
        'ui_location': 'workflow.quality',
        'units': {'non_orth_threshold': 'deg'},
    },
    # Plan 31. Changing what the feature extraction keeps changes the .eMesh
    # every later stage snaps to, so it invalidates the mesh itself -- not
    # only the report.
    'surfaceFeatures': {
        'invalidates': stales('mesh.surface_features'),
        'ui_location': 'workflow.surface_features',
        'units': {'internal_angle_tolerance': 'deg',
                  'external_angle_tolerance': 'deg',
                  'trim_min_length': 'm',
                  'max_feature_proximity': 'm'},
    },
}


# Per-field overrides keyed by semantic field ID. Any FieldDescriptor attribute
# may be overridden; unspecified keys fall back to the generated/group value.
#: DP-609. Why the priority box is greyed on the rows the combiner merges.
_INERT_PRIORITY = ('Not used: this row is combined with the others by the '
                   'background-field combiner (Field combiner, Min or Max, '
                   'on the Size page), which gives the same size in any order, so '
                   'a priority cannot make one row override another. '
                   'Under Min the finest size wins.')


FIELD_OVERRIDES: dict[str, dict] = {
    # DP-170. The mesh-quality record has two editors -- the snappy QA
    # page and the Mesh menu dialog -- and Plan 30 WP-04 already made them
    # read one field list so they could not hold different fields. They
    # still held different *names* for them: thirteen of the fifteen
    # limits read one way on the page and another in the dialog, because
    # the page took the generated name off the storage key and the dialog
    # carried wording somebody wrote in Designer. The wording below is the
    # dialog's, which is the one that says what the limit is about, in the
    # sentence case DP-163 settled; the page and the dialog now both read
    # it from here. The relaxed copies inherit it -- see `_title`.
    'quality.thresholds.max_non_orthogonality':
        {'title': 'Max face non-orthogonality'},
    'quality.thresholds.max_internal_skewness':
        {'title': 'Max internal face skewness'},
    'quality.thresholds.max_concave': {'title': 'Max cell concavity'},
    'quality.thresholds.min_vol': {'title': 'Min cell pyramid volume'},
    'quality.thresholds.min_tet_quality':
        {'title': 'Min tetrahedron quality'},
    'quality.thresholds.min_vol_collapse_ratio':
        {'title': 'Min volume collapse ratio'},
    'quality.thresholds.min_area': {'title': 'Min face area'},
    'quality.thresholds.min_twist': {'title': 'Min face twist'},
    'quality.thresholds.min_determinant':
        {'title': 'Min cell determinant'},
    'quality.thresholds.min_face_weight':
        {'title': 'Min face interpolation weight'},
    'quality.thresholds.min_vol_ratio': {'title': 'Min volume ratio'},
    # Plan 37 UF15. DP-213 hid this row as "not consumed by Foundation 13";
    # it is -- src/meshCheck/checkMesh.C:78-83 -- and the evidence is in
    # plans/evidence/plan37/uf15-v13-controls.md, including the measured
    # quirk the documentation below warns about.
    'quality.thresholds.min_face_flatness': {
        'title': 'Min face flatness',
        'documentation': (
            'Rejects a snapping or layer step that leaves a face less flat '
            'than this, between 0 and 1. Empty leaves the check off, which '
            'is the OpenFOAM default. OpenFOAM 13 refuses a value outside 0 '
            'to 1. As measured in OpenFOAM 13, snappyHexMesh flags no face '
            'at any value below 1, and at 1 it flags faces through rounding '
            'alone, so 1 is the only value that changes the mesh.'),
    },
    'quality.thresholds.relaxed.min_face_flatness': {
        'documentation': (
            'The flatness limit used once layer addition switches to the '
            'relaxed limits. Unlike the other relaxed limits it does not '
            'inherit the strict value: OpenFOAM 13 looks for it in the '
            'relaxed block alone, so empty means no flatness check in the '
            'relaxed phase. Between 0 and 1.'),
    },
    'quality.thresholds.n_smooth_scale': {'title': 'Smoothing iterations'},
    # Plan 31 -- the geometry-entry options. Every key named here was read
    # out of the OpenFOAM 13 source, not from documentation; the file and line
    # are in the schema comment beside each field.
    'meshing.geometry.tri_surface_declaration': {
        'title': 'Surface closure',
        'documentation': 'Whether the staged surfaces are declared closed. '
                         '"As imported" lets OpenFOAM decide by counting open '
                         'edges, which is right for a surface that really is '
                         'watertight. "Assume closed" writes closedTriSurface, '
                         'which asserts closure on a surface with pinholes or '
                         'several parts so that inside/outside refinement '
                         'regions and zoneInside cellZones still work instead '
                         'of being warned away and silently dropped.',
    },
    'meshing.geometry.tolerance': {
        'title': 'Intersection tolerance',
        'documentation': 'Non-default perturbation tolerance for the surface '
                         'intersection octree. Unset uses the OpenFOAM '
                         'default; raise it when a gappy surface leaks.',
    },
    'meshing.geometry.max_tree_depth': {
        'title': 'Octree depth',
        'documentation': 'Maximum depth of the surface search octree. Unset '
                         'uses the OpenFOAM default of 10; lower it only to '
                         'cut memory, at the cost of slower searches.',
    },
    'meshing.geometry.min_quality': {
        'title': 'Triangle quality floor',
        'documentation': 'Triangles below this quality are ignored when '
                         'surface normals are calculated. Unset leaves every '
                         'triangle in, which is what every case wrote before '
                         'this control existed.',
    },
    'meshing.geometry.scale': {
        'title': 'Surface scale factor',
        'documentation': 'Scaling applied to the staged surface points. '
                         'FoamMesh stages geometry in metres, so leave this '
                         'unset unless the surface file itself is in other '
                         'units — setting it scales again on top of the '
                         'import.',
    },
    'meshing.geometry.gap_detection': {
        'title': 'Close narrow gaps',
        'documentation': 'Wrap each staged surface in a withGaps surface, so '
                         'a gap narrower than the gap width is reported as '
                         'closed and snappyHexMesh stops threading cells '
                         'through it. Costs extra intersection tests.',
    },
    'meshing.geometry.gap_width': {
        'title': 'Gap width',
        'documentation': 'How wide a gap still counts as closed. Read only '
                         'when "Close narrow gaps" is on.',
    },
    'workflow.step': {
        'title': 'Current workflow step',
        'documentation': 'The furthest meshing step the case has reached. '
                         'Advanced by workflow transitions, not edited directly.',
        'read_only': True,
        'invalidates': (),
    },
    'mesh.engine': {
        'title': 'Meshing engine',
        'documentation': 'Persisted engine identity for this project. It is '
                         'changed only by mesh.engine.select so capability and '
                         'artifact invalidation can be previewed.',
        'default': 'unselected',
        'read_only': True,
        'invalidates': (),
    },
    'mesh.target_solver': {
        'title': 'Target solver',
        'documentation': 'Which solver this mesh is being built for. It '
                         'decides which meshing engines are offered — SU2 '
                         'reads tetrahedra, hexahedra, prisms and pyramids '
                         'only, so snappyHexMesh cannot serve it — which '
                         'export format is the default, and which check the '
                         'QA row runs. Changed only by '
                         'mesh.target_solver.set, so the engine it strands '
                         'can be reported at the same time.',
        'default': 'unselected',
        'read_only': True,
        # Plan 30 WP-07 (F-07) added this, because the switch used to
        # invalidate nothing at all: the export record kept describing a file
        # for the other solver and a stranded engine went unremarked.
        #
        # Plan 31 CP-05 item 2 takes `mesh` back out. "An export-target change
        # invalidates only export-specific evidence unless the user also
        # changes native meshing settings" -- and `mesh` in this tuple is the
        # one thing `_publish_verdict_staleness` keys on, so picking SU2 on a
        # case holding a good Gmsh mesh published MESH_VERDICT_STALE and the
        # window greyed out the verdict strip. Nothing about that mesh had
        # changed: the same elements, judged by the same native gate, against
        # the same limits. What changed is what would be *written from* it,
        # which is what `export` says, and which engines may serve the new
        # target, which is what `engine` says.
        #
        # DP-243. The token was spelled `export`, singular, where the other
        # 77 fields spelled it `exports`. Nothing else in the product answers
        # to `export`, so the one thing this switch was supposed to stale --
        # the files already written for the other solver -- was never staled
        # at all. The reasoning above is unchanged; only the spelling is.
        'invalidates': stales('exports', 'engine'),
        'ui_location': 'meshing_method',
    },
    'geometry_preparation.decision': {
        'read_only': True,
        'invalidates': (),
    },
    'geometry_preparation.geometry_fingerprint': {
        'read_only': True,
        'invalidates': (),
    },
    'geometry_preparation.acknowledgment': {
        'read_only': True,
        'invalidates': (),
    },
    'geometry_preparation.rules_version': {
        'read_only': True,
        'invalidates': (),
    },
    # DP-152. `baseGrid/numCellsX` and `baseGrid/grading/x` both reduce to a
    # leaf of `x`, and the generated title is the leaf, so the Base Grid page
    # -- the first page of the snappy pipeline -- drew six rows labelled
    # X, Y, Z, X, Y, Z with nothing on screen to say which trio counted cells
    # and which trio graded them.
    # DP-579 (field audit 0924 snappy-front D6). The writer reads the three
    # counts or the target size, never both (`CaseBuilder` background cell
    # counts), and both sets stayed live whichever sizing mode was chosen.
    'meshing.base_grid.cells.x': {
        'title': 'Cells X',
        'documentation': (
            'Number of background mesh cells along X, across the block '
            '(the geometry plus the standoff). Read when Sizing mode is '
            'counts only; with target_size the count follows from the '
            'target.'),
        'applies_when': ('meshing.base_grid.sizing_mode == counts',),
    },
    'meshing.base_grid.cells.y': {
        'title': 'Cells Y',
        'documentation': (
            'Number of background mesh cells along Y, across the block '
            '(the geometry plus the standoff). Read when Sizing mode is '
            'counts only; with target_size the count follows from the '
            'target.'),
        'applies_when': ('meshing.base_grid.sizing_mode == counts',),
    },
    'meshing.base_grid.cells.z': {
        'title': 'Cells Z',
        'documentation': (
            'Number of background mesh cells along Z, across the block '
            '(the geometry plus the standoff). Read when Sizing mode is '
            'counts only; with target_size the count follows from the '
            'target.'),
        'applies_when': ('meshing.base_grid.sizing_mode == counts',),
    },
    'meshing.base_grid.target_cell_size': {
        'applies_when': ('meshing.base_grid.sizing_mode == target_size',),
        # DP-587 (field audit 0924 shared-and-harness D-SH-08): metres after
        # the import conversion, like the Gmsh target size beside it.
        'unit': 'm',
        'documentation': (
            'Edge length of a background cell, in metres. Each axis gets '
            'span / size cells, rounded up, so the cells are this size or '
            'just under. Auto (empty) uses the block diagonal / 40. Read when '
            'Sizing mode is target_size only.'),
    },
    # Plan 37 #9. The fields below reached the user with no unit and no
    # words in describe_fields.
    'meshing.base_grid.sizing_mode': {
        'documentation': (
            'How the background block is divided: counts takes the number '
            'of cells along each axis; target_size takes one edge length '
            'and derives the counts from it.'),
    },
    'meshing.base_grid.standoff': {
        'unit': 'ratio',
        'documentation': (
            'Gap between the geometry and the derived block, as a fraction of '
            'the largest span of the geometry, added on all six faces: 0.1 on a '
            'part 2 m long puts each face 0.2 m out. 0 makes the block the '
            'bounding box of the geometry. Not applied to a bounding Hex6 or to '
            'blocks written by hand.'),
    },
    'meshing.base_grid.scale': {
        'unit': 'ratio',
        'documentation': (
            'Multiplies the vertices you typed by hand: blockMesh builds '
            'every vertex times Scale, in metres, so vertices typed in '
            'millimetres take 0.001. Held at 1 for the one block around the '
            'geometry and for a bounding Hex6 (Plan 37 #9): their vertices '
            'are already in metres — the geometry is put in metres when it '
            'is imported — so any other value moved and resized the block '
            'off the geometry. There a stored value other than 1 is kept so '
            'old projects open, reported, and not applied.'),
    },
    'meshing.base_grid.bounding_hex6': {
        'documentation': (
            'A Hex6 volume from the geometry list to use as the background '
            'block instead of the derived box; its two corners, in metres, '
            'are the block. Empty derives the block from the geometry and '
            'the standoff.'),
    },
    # Plan 37 UF12. The ratio is always the largest cell over the smallest,
    # and the side the small cells go on is its own control, so nobody types
    # a reciprocal to move them.
    'meshing.base_grid.grading.x': {
        'title': 'Grading ratio X',
        'documentation': (
            'Largest cell divided by the smallest along X, never below 1; 1 is '
            'uniform. Fine cells at, beside it, says which side the small '
            'cells go on. MEASURED on OpenFOAM 13, ratio 4 over eight cells '
            'of a unit edge: 0.0565 at the fine side, 0.226 at the other.'),
    },
    'meshing.base_grid.grading_fine.x': {
        'title': 'Fine cells at X',
        'documentation': (
            'Where the small cells go along X. Minus side (start) is the '
            'low-X face; plus side (end) the high-X face; Centre puts them '
            'in the middle and Both edges at both faces, each half graded by '
            'the ratio. MEASURED on OpenFOAM 13, ratio 4 over eight cells: '
            'Centre 0.220 at the faces and 0.055 in the middle, Both edges '
            'the reverse. A two-sided choice needs at least four cells, two '
            'per half, and an odd count gives the larger half to the second.'),
    },
    # Plan 37 UF12. The ratio is always the largest cell over the smallest,
    # and the side the small cells go on is its own control, so nobody types
    # a reciprocal to move them.
    'meshing.base_grid.grading.y': {
        'title': 'Grading ratio Y',
        'documentation': (
            'Largest cell divided by the smallest along Y, never below 1; 1 is '
            'uniform. Fine cells at, beside it, says which side the small '
            'cells go on. MEASURED on OpenFOAM 13, ratio 4 over eight cells '
            'of a unit edge: 0.0565 at the fine side, 0.226 at the other.'),
    },
    'meshing.base_grid.grading_fine.y': {
        'title': 'Fine cells at Y',
        'documentation': (
            'Where the small cells go along Y. Minus side (start) is the '
            'low-Y face; plus side (end) the high-Y face; Centre puts them '
            'in the middle and Both edges at both faces, each half graded by '
            'the ratio. MEASURED on OpenFOAM 13, ratio 4 over eight cells: '
            'Centre 0.220 at the faces and 0.055 in the middle, Both edges '
            'the reverse. A two-sided choice needs at least four cells, two '
            'per half, and an odd count gives the larger half to the second.'),
    },
    # Plan 37 UF12. The ratio is always the largest cell over the smallest,
    # and the side the small cells go on is its own control, so nobody types
    # a reciprocal to move them.
    'meshing.base_grid.grading.z': {
        'title': 'Grading ratio Z',
        'documentation': (
            'Largest cell divided by the smallest along Z, never below 1; 1 is '
            'uniform. Fine cells at, beside it, says which side the small '
            'cells go on. MEASURED on OpenFOAM 13, ratio 4 over eight cells '
            'of a unit edge: 0.0565 at the fine side, 0.226 at the other.'),
    },
    'meshing.base_grid.grading_fine.z': {
        'title': 'Fine cells at Z',
        'documentation': (
            'Where the small cells go along Z. Minus side (start) is the '
            'low-Z face; plus side (end) the high-Z face; Centre puts them '
            'in the middle and Both edges at both faces, each half graded by '
            'the ratio. MEASURED on OpenFOAM 13, ratio 4 over eight cells: '
            'Centre 0.220 at the faces and 0.055 in the middle, Both edges '
            'the reverse. A two-sided choice needs at least four cells, two '
            'per half, and an odd count gives the larger half to the second.'),
    },
    'meshing.base_grid.grading_notice': {
        'title': 'Grading note',
        'documentation': (
            'What reading this project with the Fine cells at control changed '
            'on screen. The written mesh did not change. A record of the '
            'migration, so it is read-only; dismissing it on the page puts it '
            'away for this user on this machine.'),
        'read_only': True,
        'invalidates': (),
    },
    # FS-B. The six background faces carry three coupled fields each, and
    # the schema generates the same title for all three -- 'X Min' over a
    # type, a name and a category. Side by side in one group that is not a
    # usable control, so each says which half of the face it sets.
    'meshing.base_grid.boundary_types.x_min': {
        'title': 'X min type',
        'documentation': (
            'OpenFOAM patch type written for the low-X background face. '
            'A category the type cannot carry is refused: only a patch can be '
            'an inlet or an outlet, and only a wall can be a wall.'),
    },
    'meshing.base_grid.boundary_names.x_min': {
        'title': 'X min name',
        'documentation': (
            'Name the low-X background face carries in the delivered '
            'boundary. Left empty it keeps the label this product generated. '
            'A face given a category must be named, or the case builder refuses '
            'to write the mesh rather than offer a generated label as your inlet.'),
    },
    'meshing.base_grid.boundary_categories.x_min': {
        'title': 'X min is for',
        'documentation': (
            'What the low-X background face is for. Anything but '
            'unclassified needs a name on the same face and a patch type that '
            'can honestly carry it.'),
    },
    'meshing.base_grid.boundary_types.x_max': {
        'title': 'X max type',
        'documentation': (
            'OpenFOAM patch type written for the high-X background face. '
            'A category the type cannot carry is refused: only a patch can be '
            'an inlet or an outlet, and only a wall can be a wall.'),
    },
    'meshing.base_grid.boundary_names.x_max': {
        'title': 'X max name',
        'documentation': (
            'Name the high-X background face carries in the delivered '
            'boundary. Left empty it keeps the label this product generated. '
            'A face given a category must be named, or the case builder refuses '
            'to write the mesh rather than offer a generated label as your inlet.'),
    },
    'meshing.base_grid.boundary_categories.x_max': {
        'title': 'X max is for',
        'documentation': (
            'What the high-X background face is for. Anything but '
            'unclassified needs a name on the same face and a patch type that '
            'can honestly carry it.'),
    },
    'meshing.base_grid.boundary_types.y_min': {
        'title': 'Y min type',
        'documentation': (
            'OpenFOAM patch type written for the low-Y background face. '
            'A category the type cannot carry is refused: only a patch can be '
            'an inlet or an outlet, and only a wall can be a wall.'),
    },
    'meshing.base_grid.boundary_names.y_min': {
        'title': 'Y min name',
        'documentation': (
            'Name the low-Y background face carries in the delivered '
            'boundary. Left empty it keeps the label this product generated. '
            'A face given a category must be named, or the case builder refuses '
            'to write the mesh rather than offer a generated label as your inlet.'),
    },
    'meshing.base_grid.boundary_categories.y_min': {
        'title': 'Y min is for',
        'documentation': (
            'What the low-Y background face is for. Anything but '
            'unclassified needs a name on the same face and a patch type that '
            'can honestly carry it.'),
    },
    'meshing.base_grid.boundary_types.y_max': {
        'title': 'Y max type',
        'documentation': (
            'OpenFOAM patch type written for the high-Y background face. '
            'A category the type cannot carry is refused: only a patch can be '
            'an inlet or an outlet, and only a wall can be a wall.'),
    },
    'meshing.base_grid.boundary_names.y_max': {
        'title': 'Y max name',
        'documentation': (
            'Name the high-Y background face carries in the delivered '
            'boundary. Left empty it keeps the label this product generated. '
            'A face given a category must be named, or the case builder refuses '
            'to write the mesh rather than offer a generated label as your inlet.'),
    },
    'meshing.base_grid.boundary_categories.y_max': {
        'title': 'Y max is for',
        'documentation': (
            'What the high-Y background face is for. Anything but '
            'unclassified needs a name on the same face and a patch type that '
            'can honestly carry it.'),
    },
    'meshing.base_grid.boundary_types.z_min': {
        'title': 'Z min type',
        'documentation': (
            'OpenFOAM patch type written for the low-Z background face. '
            'A category the type cannot carry is refused: only a patch can be '
            'an inlet or an outlet, and only a wall can be a wall.'),
    },
    'meshing.base_grid.boundary_names.z_min': {
        'title': 'Z min name',
        'documentation': (
            'Name the low-Z background face carries in the delivered '
            'boundary. Left empty it keeps the label this product generated. '
            'A face given a category must be named, or the case builder refuses '
            'to write the mesh rather than offer a generated label as your inlet.'),
    },
    'meshing.base_grid.boundary_categories.z_min': {
        'title': 'Z min is for',
        'documentation': (
            'What the low-Z background face is for. Anything but '
            'unclassified needs a name on the same face and a patch type that '
            'can honestly carry it.'),
    },
    'meshing.base_grid.boundary_types.z_max': {
        'title': 'Z max type',
        'documentation': (
            'OpenFOAM patch type written for the high-Z background face. '
            'A category the type cannot carry is refused: only a patch can be '
            'an inlet or an outlet, and only a wall can be a wall.'),
    },
    'meshing.base_grid.boundary_names.z_max': {
        'title': 'Z max name',
        'documentation': (
            'Name the high-Z background face carries in the delivered '
            'boundary. Left empty it keeps the label this product generated. '
            'A face given a category must be named, or the case builder refuses '
            'to write the mesh rather than offer a generated label as your inlet.'),
    },
    'meshing.base_grid.boundary_categories.z_max': {
        'title': 'Z max is for',
        'documentation': (
            'What the high-Z background face is for. Anything but '
            'unclassified needs a name on the same face and a patch type that '
            'can honestly carry it.'),
    },
    # FS-B. The authored background mesh: the five collections blockMesh
    # reads. Their element titles come from the schema key, which says
    # 'Vertices' for eight indices in a required order and 'Points' for an
    # arc's midpoint -- the syntax has to be documented where it is typed.
    'base_grid.vertices/{id}/x': {
        'documentation': (
            'X coordinate of this background vertex, before the '
            'base grid scale is applied.'),
    },
    'base_grid.vertices/{id}/y': {
        'documentation': (
            'Y coordinate of this background vertex, before the '
            'base grid scale is applied.'),
    },
    'base_grid.vertices/{id}/z': {
        'documentation': (
            'Z coordinate of this background vertex, before the '
            'base grid scale is applied.'),
    },
    'base_grid.blocks/{id}/name': {
        'documentation': (
            'Your label for this block. It is not written to blockMeshDict; it '
            'is how the block is found again in this table.'),
    },
    'base_grid.blocks/{id}/vertices': {
        'documentation': (
            'Eight vertex numbers in blockMesh hex order: the four corners of '
            'one face, then the four of the opposite face reached along the '
            'local third direction — 0 1 2 3 4 5 6 7. Rows are numbered from '
            'zero in the Vertices table above.'),
    },
    # DP-204. The label said Num cells X and a column beside it said
    # cells, so the box named its quantity twice and abbreviated it
    # on the way. The block form now reads Cells X beside Grading X,
    # the same pair the simple grid above it uses.
    'base_grid.blocks/{id}/num_cells_x': {
        'title': 'Cells X',
        'documentation': 'Cells along the block local X direction.',
    },
    # Plan 37 UF12 (DP-1033). "Fine end of X is the start" wrote the
    # reciprocal, which put the small cells at the END: blockMesh's ratio is
    # last cell over first. A side and a ratio replace it.
    'base_grid.blocks/{id}/grading_x': {
        'title': 'Grading X',
        'documentation': (
            'The block local X direction as blockMeshDict writes it. With '
            'Fine cells at on Custom profile this text is the grading: a '
            'plain ratio of last cell to first (4 puts the small cells at '
            'the start), or a segmented profile — (0.2 0.3 4) (0.6 0.4 1) '
            '(0.2 0.3 0.25) — whose length and cell fractions each add up to '
            '1. With any other choice it shows what that choice writes, and '
            'typing in it switches back to Custom profile.'),
    },
    'base_grid.blocks/{id}/grading_x_fine': {
        'title': 'Fine cells at X',
        'documentation': (
            'Where the small cells go along this block local X direction. '
            'Start and End follow the block own vertex order — from its '
            'first vertex toward the next along X — not the world axes, so '
            'a block turned round needs the opposite choice to meet its '
            'neighbour. Centre and Both edges grade each half by the ratio '
            'and need at least four cells. Custom profile writes the text in '
            'Grading X as typed.'),
    },
    'base_grid.blocks/{id}/grading_x_ratio': {
        'title': 'Grading ratio X',
        'documentation': (
            'Largest cell divided by the smallest along this block local X '
            'direction, never below 1. Used unless Fine cells at is Custom '
            'profile.'),
    },
    # DP-204. The label said Num cells Y and a column beside it said
    # cells, so the box named its quantity twice and abbreviated it
    # on the way. The block form now reads Cells Y beside Grading Y,
    # the same pair the simple grid above it uses.
    'base_grid.blocks/{id}/num_cells_y': {
        'title': 'Cells Y',
        'documentation': 'Cells along the block local Y direction.',
    },
    # Plan 37 UF12 (DP-1033). "Fine end of Y is the start" wrote the
    # reciprocal, which put the small cells at the END: blockMesh's ratio is
    # last cell over first. A side and a ratio replace it.
    'base_grid.blocks/{id}/grading_y': {
        'title': 'Grading Y',
        'documentation': (
            'The block local Y direction as blockMeshDict writes it. With '
            'Fine cells at on Custom profile this text is the grading: a '
            'plain ratio of last cell to first (4 puts the small cells at '
            'the start), or a segmented profile — (0.2 0.3 4) (0.6 0.4 1) '
            '(0.2 0.3 0.25) — whose length and cell fractions each add up to '
            '1. With any other choice it shows what that choice writes, and '
            'typing in it switches back to Custom profile.'),
    },
    'base_grid.blocks/{id}/grading_y_fine': {
        'title': 'Fine cells at Y',
        'documentation': (
            'Where the small cells go along this block local Y direction. '
            'Start and End follow the block own vertex order — from its '
            'first vertex toward the next along Y — not the world axes, so '
            'a block turned round needs the opposite choice to meet its '
            'neighbour. Centre and Both edges grade each half by the ratio '
            'and need at least four cells. Custom profile writes the text in '
            'Grading Y as typed.'),
    },
    'base_grid.blocks/{id}/grading_y_ratio': {
        'title': 'Grading ratio Y',
        'documentation': (
            'Largest cell divided by the smallest along this block local Y '
            'direction, never below 1. Used unless Fine cells at is Custom '
            'profile.'),
    },
    # DP-204. The label said Num cells Z and a column beside it said
    # cells, so the box named its quantity twice and abbreviated it
    # on the way. The block form now reads Cells Z beside Grading Z,
    # the same pair the simple grid above it uses.
    'base_grid.blocks/{id}/num_cells_z': {
        'title': 'Cells Z',
        'documentation': 'Cells along the block local Z direction.',
    },
    # Plan 37 UF12 (DP-1033). "Fine end of Z is the start" wrote the
    # reciprocal, which put the small cells at the END: blockMesh's ratio is
    # last cell over first. A side and a ratio replace it.
    'base_grid.blocks/{id}/grading_z': {
        'title': 'Grading Z',
        'documentation': (
            'The block local Z direction as blockMeshDict writes it. With '
            'Fine cells at on Custom profile this text is the grading: a '
            'plain ratio of last cell to first (4 puts the small cells at '
            'the start), or a segmented profile — (0.2 0.3 4) (0.6 0.4 1) '
            '(0.2 0.3 0.25) — whose length and cell fractions each add up to '
            '1. With any other choice it shows what that choice writes, and '
            'typing in it switches back to Custom profile.'),
    },
    'base_grid.blocks/{id}/grading_z_fine': {
        'title': 'Fine cells at Z',
        'documentation': (
            'Where the small cells go along this block local Z direction. '
            'Start and End follow the block own vertex order — from its '
            'first vertex toward the next along Z — not the world axes, so '
            'a block turned round needs the opposite choice to meet its '
            'neighbour. Centre and Both edges grade each half by the ratio '
            'and need at least four cells. Custom profile writes the text in '
            'Grading Z as typed.'),
    },
    'base_grid.blocks/{id}/grading_z_ratio': {
        'title': 'Grading ratio Z',
        'documentation': (
            'Largest cell divided by the smallest along this block local Z '
            'direction, never below 1. Used unless Fine cells at is Custom '
            'profile.'),
    },
    'base_grid.blocks/{id}/zone': {
        'documentation': (
            'Cell zone name written after the block hex, when this block is '
            'meant to be addressable on its own. Empty writes no zone.'),
    },
    'base_grid.edges/{id}/kind': {
        'documentation': (
            'How the edge between the two vertices is curved. An arc takes one '
            'point it passes through; the spline kinds take a list.'),
    },
    'base_grid.edges/{id}/start': {
        'title': 'From vertex',
        'documentation': 
            'Row number of the vertex this edge starts at.',
    },
    'base_grid.edges/{id}/end': {
        'title': 'To vertex',
        'documentation': 
            'Row number of the vertex this edge ends at.',
    },
    'base_grid.edges/{id}/points': {
        'title': 'Through points',
        'documentation': (
            'Points the edge passes through, before scale: 0.5 1.15 0 for an '
            'arc, or (x y z) (x y z) for a spline. An arc takes exactly one.'),
    },
    'base_grid.patches/{id}/name': {
        'documentation': (
            'Name this patch carries in the delivered boundary, and the name a '
            'merge pair refers to it by.'),
    },
    'base_grid.patches/{id}/type': {
        'documentation': (
            'OpenFOAM patch type. A category the type cannot carry is refused.'),
    },
    'base_grid.patches/{id}/category': {
        'documentation': (
            'What this patch is for. Anything but unclassified needs a name '
            'and a patch type that can honestly carry it.'),
    },
    'base_grid.patches/{id}/group': {
        'documentation': (
            'Optional inGroups entry, so a set of patches can be addressed by '
            'one name downstream. Empty writes no group.'),
    },
    'base_grid.patches/{id}/faces': {
        'documentation': (
            'The faces of this patch, each four vertex row numbers: '
            '(0 4 7 3) (1 2 6 5). Every external face of every block must '
            'belong to exactly one patch.'),
    },
    'base_grid.merge_pairs/{id}/master': {
        'documentation': (
            'Name of the patch on the master side of the merge. blockMesh '
            'keeps the master face positions and removes both patches from the '
            'delivered boundary.'),
    },
    'base_grid.merge_pairs/{id}/slave': {
        'documentation': (
            'Name of the patch on the slave side of the merge. Its points move '
            'onto the master face.'),
    },
    # DP-204. Each of these three says cells in its own label, so the
    # column that also said cells has gone. The rule the whole registry is
    # now held to is that a control names its quantity once: either the
    # label carries the noun or the column does, and never both.
    # DP-1264. Users looked for a curvature refinement setting and found
    # none; OpenFOAM v13's snappyHexMesh has no separate one, and this is
    # the control that does that job.
    'meshing.castellation.resolve_feature_angle': {
        'documentation': (
            'Cells whose surface intersections differ in normal by more than '
            'this angle are refined up to the surface maximum level. OpenFOAM '
            'v13 has no separate curvature refinement: curvature-driven '
            'refinement is controlled by resolveFeatureAngle together with '
            'the surface min/max levels. A smaller angle refines more of a '
            'curved surface.'),
    },
    'meshing.castellation.max_global_cells': {
        'documentation': 'Hard ceiling on total cell count across all processors.',
    },
    'meshing.castellation.max_local_cells': {
        'documentation': 'Ceiling on cells per processor during refinement.',
    },
    'meshing.castellation.cells_between_levels': {
        'documentation': 'Buffer cells inserted between adjacent refinement levels.',
    },
    'meshing.snap.tolerance': {
        'documentation': 'Snapping distance as a multiple of local edge length.',
    },
    'meshing.snap.smooth_patch_iterations': {
        'documentation': 'Patch-smoothing iterations applied after snapping.',
    },
    # DP-600 (field audit 0924 snappy-back D7). This said "layer cells grown
    # outward", the opposite of the annotated OF13 snappyHexMeshDict: nGrow
    # is a buffer of faces that are also left without layers.
    'meshing.layers.growth_cells': {
        'title': 'Faces left bare around a stop',
        'documentation': 'Where layer addition cannot extrude a point, this '
                         'many rings of connected faces around it are left '
                         'without layers as well. It adds no layers; a '
                         'buffer helps layer addition converge near '
                         'features. OpenFOAM writes it as nGrow.',
    },
    # Plan 37 UF2 (DP-1028). Bookkeeping: whether the snappy layers page has
    # already given this case its default "Walls" group. The mesher never
    # reads it; it keeps the layer stage's invalidation because it is only
    # ever written beside a layer group being created or removed, which
    # stales that stage anyway.
    'meshing.layers.defaulted': {
        'title': 'Default layer group made',
        'documentation': 'Set once the Boundary layers step has given the '
                         'case its default wall group, or a group has been '
                         'removed, so a group the user deletes is not made '
                         'again. The mesher never reads it.',
    },
    'meshing.layers.feature_angle': {
        'documentation': 'Angle above which layer growth stops at a feature edge.',
        'unit': 'deg',
    },

    # DP-204. Twelve controls across the layers and snap pages had no
    # authored title, so each one was shown the only name the registry could
    # build from its storage key: the OpenFOAM keyword with the camel taken
    # out. `nSolveIter` reached the screen as `N solve iter`, and a reader
    # who did not already know the dictionary had nothing to go on -- the
    # leading N is a Hungarian prefix meaning number of, and iter is a word
    # only because the dictionary author was typing in the 1990s. These say
    # what the number counts.
    'meshing.layers.n_layer_iter': {
        'title': 'Layer addition iterations',
    },
    'meshing.layers.n_relax_iter': {
        'title': 'Layer relaxation iterations',
    },
    'meshing.layers.n_relaxed_iter': {
        'title': 'Iterations before the relaxed limits apply',
    },
    'meshing.layers.n_medial_axis_iter': {
        'title': 'Medial axis iterations',
    },
    'meshing.layers.n_smooth_displacement': {
        'title': 'Displacement smoothing iterations',
    },
    # Plan 37 UF15. addLayersControls keys OpenFOAM 13 reads in
    # layerParameters.C; measured effect in
    # plans/evidence/plan37/uf15-v13-controls.md.
    'meshing.layers.concave_angle': {
        'title': 'Max concavity of a merged layer face',
        'documentation': (
            'When layer addition merges the faces a cell has on one patch, '
            'the merged face may not turn concave by more than this angle. '
            'Empty uses the OpenFOAM default of 90 degrees. A larger angle '
            'merges more faces; a smaller one fewer.'),
    },
    'meshing.layers.merge_faces': {
        'title': 'Merge layer faces on one cell',
        'documentation': (
            'Whether layer addition merges the faces a cell has on one '
            'patch into one face. Default, which is OpenFOAM\'s own rule, '
            'merges them only on patches that are being given layers; On '
            'merges them on every '
            'patch; Off never merges them, which keeps more, smaller faces.'),
    },
    'meshing.layers.n_smooth_normals': {
        'title': 'Interior normal smoothing iterations',
    },
    'meshing.layers.n_smooth_surface_normals': {
        'title': 'Surface normal smoothing iterations',
    },
    'meshing.layers.n_smooth_thickness': {
        'title': 'Thickness smoothing iterations',
    },
    'meshing.layers.n_buffer_cells_no_extrude': {
        'title': 'Buffer cells where layers stop',
    },
    'meshing.snap.n_solve_iter': {
        'title': 'Mesh displacement iterations',
    },
    'meshing.snap.n_relax_iter': {
        'title': 'Snapping relaxation iterations',
    },
    'meshing.snap.n_feature_snap_iter': {
        'title': 'Feature snapping iterations',
    },

    # DP-204. Ratio of what? It is the growth ratio of the boundary layer,
    # and the column beside it said ratio a second time.
    'gmsh.boundary_layers.ratio': {
        'title': 'Growth ratio',
    },

    # Plan 33 section 1.1. The generated name would be `Patch mode`, which
    # says what the field is called rather than what it decides.
    'gmsh.boundary_layers.patch_mode': {
        'title': 'Grow layers on',
    },

    # DP-204. The two numbers that set the shape of a 2D run carried no
    # dimension at all. The extrusion is a length in model units and the
    # wedge opening is an angle, and a reader given 0.01 and 5 with nothing
    # beside them has to guess which is which.
    'gmsh.dimensionality.thickness': {
        'unit': 'm',
        # DP-672 (field audit 0924 gmsh-generate-export D2). The five numbers
        # and names below are read by the polyMesh publisher only, which
        # turns the meshed section into one layer of cells. An SU2 export is
        # the section itself (NDIME= 2) and has no front, back, thickness or
        # wedge, so on that route they are offered as what they are: unused.
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.dimensionality.mode == two_d',
                         'mesh.target_solver != su2'),
        'documentation': 'Distance between the front and back empty patches '
                         'of the one-cell-thick OpenFOAM mesh. Not used for '
                         'SU2, which reads the section as a 2D mesh.',
    },
    'gmsh.dimensionality.wedge_angle': {
        'unit': 'deg',
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.dimensionality.mode == axisymmetric',
                         'mesh.target_solver != su2'),
        'documentation': 'Opening angle of the one-cell OpenFOAM wedge, 0.1 '
                         'to 10 degrees (5 is advised). Not used for SU2, '
                         'which reads the section as a 2D mesh and is run '
                         'with AXISYMMETRIC= YES.',
    },
    'gmsh.dimensionality.mode': {
        'title': 'Mesh dimension',
        'documentation': 'Three-dimensional meshes the CAD volume. Planar '
                         'and axisymmetric mesh one planar face (a section): '
                         'for OpenFOAM it is published one cell thick between '
                         'empty patches, or as a wedge between wedge patches; '
                         'for SU2 it is written as a 2D mesh (NDIME= 2). '
                         'Boundary layers and the farfield box are not '
                         'applied to a section.',
    },
    'gmsh.dimensionality.wedge_axis': {
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.dimensionality.mode == axisymmetric',
                         'mesh.target_solver != su2'),
        'documentation': 'Axis the section is revolved about. The section '
                         'must lie on one side of it, in a plane through it. '
                         'Not used for SU2, whose AXISYMMETRIC= YES option '
                         'always revolves about x.',
    },
    'gmsh.dimensionality.front_patch': {
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.dimensionality.mode != three_d',
                         'mesh.target_solver != su2'),
        'documentation': 'Name of the patch on the front face of the '
                         'one-cell OpenFOAM mesh (empty, or wedge for an '
                         'axisymmetric mesh). SU2 has no front face.',
    },
    'gmsh.dimensionality.edge_names': {
        # DP-675 (field audit 0924 gmsh-generate-export D11). A section's
        # boundary is curves, which the prepared geometry does not name, so
        # they are named here by tag; both routes read it, since the names
        # become the SU2 markers as well as the polyMesh patches.
        'title': 'Edge names',
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.dimensionality.mode != three_d'),
        'documentation': 'Patch names for the boundary curves of the '
                         'section, as "inlet: 1; outlet: 3; walls: 2, 4". '
                         'The numbers are the curve tags a run without names '
                         'publishes as edge_1, edge_2, ...; a curve not '
                         'listed keeps that name. The names become the '
                         'OpenFOAM patches and the SU2 markers, and a name '
                         'starting with a boundary category (inlet, outlet, '
                         'symmetry, wall ...) is typed as one.',
    },
    'gmsh.dimensionality.back_patch': {
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.dimensionality.mode != three_d',
                         'mesh.target_solver != su2'),
        'documentation': 'Name of the patch on the back face of the '
                         'one-cell OpenFOAM mesh. SU2 has no back face.',
    },

    # DP-204. The sibling above it reads Max CPU cores, so this one reads
    # Max memory, and the dimension it is counted in moves to the column
    # where every other dimension in this product lives.
    'mesh.execution.max_memory_bytes': {
        'title': 'Max memory',
        'unit': 'bytes',
        # DP-592 (field audit 0924 shared-and-harness D-SH-05). Stored and
        # read by nothing that runs; read-only with the reason until a
        # launcher can enforce it.
        'read_only': True,
        'documentation': 'No run reads this: no meshing runtime enforces a '
                         'memory ceiling, so a value here would bound '
                         'nothing.',
    },
    'mesh.execution.allow_distributed': {
        'read_only': True,
        'documentation': 'No run reads this: meshing runs on this machine, '
                         'so there is nothing to distribute.',
    },
    'mesh.execution.preferred_backend': {
        'read_only': True,
        'documentation': 'No run reads this: each mesher has one runtime, '
                         'and the run takes it from the meshing method.',
    },

    # DP-163. The other name with its unit inside it. `Qualification
    # Tolerance M` read as a capital letter nobody could place; the `m` is
    # metres, and metres belong in the unit column beside the box.
    'geometry_preparation.qualification_tolerance_m': {
        'title': 'Qualification tolerance',
        'unit': 'm',
    },

    # DP-162. The only angle in the product that carried its unit in its name.
    # Every other one -- the castellation perpendicular angle, the feature
    # included angle -- says `deg` in the unit column beside the box, so this
    # one said `Rotation Angle Degrees` over a column and a box that were
    # otherwise identical to theirs.
    # DP-606 (field audit 0924 gmsh-sizing D8). The Gmsh row editors had a
    # tooltip for one setting in five collections; the rest were a generated
    # label and nothing else. Each now says what it sizes and what it reads.
    'gmsh.size_fields.controls/{id}/size_inside': {
        'documentation': 'Element size the field asks for inside its region '
                         '(on the scope, inside the ball, box or cylinder).',
    },
    'gmsh.size_fields.controls/{id}/size_outside': {
        'documentation': 'Element size the field asks for away from its '
                         'region. Under the Min combiner a size larger than '
                         'the global target does not coarsen anything.',
    },
    'gmsh.size_fields.controls/{id}/distance_min': {
        'documentation': 'Distance threshold only: up to this distance from '
                         'the scope the inside size holds.',
    },
    'gmsh.size_fields.controls/{id}/distance_max': {
        'documentation': 'Distance threshold only: beyond this distance the '
                         'outside size holds; between the two the size '
                         'blends linearly.',
    },
    'gmsh.size_fields.controls/{id}/radius': {
        'documentation': 'Ball and cylinder: radius of the region. Frustum: '
                         'radius at the first end.',
    },
    'gmsh.size_fields.controls/{id}/radius_end': {
        'documentation': 'Frustum only: radius at the second end.',
    },
    'gmsh.size_fields.controls/{id}/thickness': {
        'documentation': 'Ball only: width of the blend from the inside size '
                         'to the outside size across the ball surface. 0 is '
                         'a sharp step.',
    },
    'gmsh.surface_sizes.controls/{id}/target_size': {
        'documentation': 'Element size on this surface.',
    },
    'gmsh.surface_sizes.controls/{id}/blend_distance': {
        'documentation': 'Distance over which the size grows from this '
                         'surface back to the global target. 0 uses one '
                         'global target size as the distance.',
    },
    'gmsh.curve_controls.controls/{id}/segments': {
        'documentation': 'Transfinite mode: number of elements along each '
                         'curve in scope.',
    },
    'gmsh.curve_controls.controls/{id}/coefficient': {
        'documentation': 'Transfinite mode: growth ratio of the progression '
                         'or bump law. 1 spaces the nodes evenly.',
    },
    # DP-612 (field audit 0924 gmsh-sizing D4).
    'gmsh.curve_controls.controls/{id}/local_size': {
        'documentation': 'Local size mode: element size at the end points of '
                         'each curve in scope. Gmsh reads point sizes only '
                         'while "From points" is ticked on the Global sizing '
                         'page; with it off the row is skipped with a '
                         'warning.',
    },
    'gmsh.volume_controls.controls/{id}/target_size': {
        'documentation': 'Element size inside this volume. Auto uses the '
                         'global target size.',
    },
    # Plan 36 RP11.
    'gmsh.volume_controls.controls/{id}/volume_type': {
        'title': 'Type',
        'documentation': 'Whether this solid is a fluid or a solid region. '
                         'Published as the region type of its cell zone. Blank: '
                         'not typed yet (published as fluid).',
    },
    'gmsh.periodic_pairs.controls/{id}/match_tolerance': {
        'documentation': 'How far apart two points may be after the '
                         'transform and still be matched as one periodic '
                         'pair.',
    },

    # DP-611 (field audit 0924 gmsh-sizing D2). The one priority that
    # decides something: which row sets a curve two rows share.
    'gmsh.curve_controls.controls/{id}/priority': {
        'documentation': 'Where two curve controls reach the same curve (the '
                         'edge between two faces), the higher priority sets '
                         'it and the other leaves it, with a warning. Equal '
                         'priorities go to the first by name.',
    },

    # DP-609 (field audit 0924 gmsh-sizing D3). Size fields, per-surface sizes
    # and volume-control sizes all meet in the background field's Min (or
    # Max), which does not depend on order, so their priority changed
    # nothing (field audit diff_rows: changes_gmsh=False for every type). The
    # box is greyed out with the reason rather than offering a lever that is
    # not attached. The curve-control priority does decide a shared curve
    # (DP-611) and stays editable.
    'gmsh.size_fields.controls/{id}/priority': {
        'read_only': True,
        'documentation': _INERT_PRIORITY,
    },
    'gmsh.surface_sizes.controls/{id}/priority': {
        'read_only': True,
        'documentation': _INERT_PRIORITY,
    },
    'gmsh.volume_controls.controls/{id}/priority': {
        'read_only': True,
        'documentation': _INERT_PRIORITY,
    },

    # DP-608 (field audit 0924 gmsh-sizing D12). The farfield switch sits
    # among the healing switches on the Preparation panel and was titled
    # "Enabled", which said nothing about what it turns on.
    # Plan 37 UF13: the switch now encloses in a box, sphere or cylinder, so
    # the title no longer says box.
    'gmsh.describe_geometry.enabled': {
        'title': 'Enclose in farfield',
        'documentation': 'Build an external-flow domain: a box, sphere or '
                         'cylinder around the geometry (a box grown by '
                         'Padding on every side by default), with the solids '
                         'cut out of it. Gmsh needs CAD (STEP/IGES) and '
                         'refuses a tessellated import; snappyHexMesh takes '
                         'either.',
    },
    # Plan 37 UF13 (section 4.4). The shared farfield specification: one
    # authored primitive every engine builds (`core/mesh/farfield_spec.py`).
    'gmsh.describe_geometry.shape': {
        'title': 'Farfield shape',
        'documentation': 'Box, sphere or cylinder. The shape is cut against '
                         'the solids. On Gmsh its faces publish as '
                         'far_field_xMin .. far_field_zMax (box), far_field '
                         '(sphere), or far_field_side, far_field_inlet and '
                         'far_field_outlet (cylinder, the caps ordered along '
                         'the axis); on snappyHexMesh the whole shape is one '
                         'patch, far_field. Those are names, not boundary '
                         'conditions.',
    },
    'gmsh.describe_geometry.centre_mode': {
        'title': 'Farfield centre',
        'documentation': 'Auto centres the farfield on the bounding box of '
                         'the imported geometry, re-read on every run; '
                         'Explicit uses the centre below.',
    },
    **{
        f'gmsh.describe_geometry.centre.{axis}': {
            'title': f'Farfield centre {axis.upper()}',
            'documentation': f'{axis.upper()} of the farfield centre, in '
                             f'metres. For a cylinder it is the midpoint '
                             f'between the two caps.',
            'unit': 'm',
            'applies_when': ('mesh.engine != unselected',
                             'gmsh.describe_geometry.centre_mode == explicit'),
        }
        for axis in 'xyz'
    },
    'gmsh.describe_geometry.padding': {
        'documentation': 'Box only: the gap added on every side of the '
                         'geometry, as a multiple of its bounding-box '
                         'diagonal.',
        'applies_when': ('mesh.engine != unselected',
                         'gmsh.describe_geometry.shape == box'),
    },
    'gmsh.describe_geometry.radius': {
        'title': 'Farfield radius',
        'documentation': 'Sphere or cylinder radius, in metres. It must hold '
                         'every body with clearance; a radius too small is '
                         'refused before meshing with the radius that would '
                         'fit, never clipped.',
        'unit': 'm',
        'applies_when': ('mesh.engine != unselected',
                         'gmsh.describe_geometry.shape != box'),
    },
    'gmsh.describe_geometry.length': {
        'title': 'Cylinder length',
        'documentation': 'Cap to cap, in metres, centred on the farfield '
                         'centre. It must hold every body with clearance '
                         'along the axis.',
        'unit': 'm',
        'applies_when': ('mesh.engine != unselected',
                         'gmsh.describe_geometry.shape == cylinder'),
    },
    **{
        f'gmsh.describe_geometry.axis.{axis}': {
            'title': f'Cylinder axis {axis.upper()}',
            'documentation': f'{axis.upper()} of the cylinder axis direction. '
                             f'Any direction; only the direction is read, '
                             f'not the length, and a zero axis is refused. '
                             f'The inlet cap is at the negative end.',
            'applies_when': ('mesh.engine != unselected',
                             'gmsh.describe_geometry.shape == cylinder'),
        }
        for axis in 'xyz'
    },
    # DP-613 (field audit 0924 gmsh-sizing D10).
    'gmsh.describe_geometry.remove_duplicate_nodes': {
        'documentation': 'Merge nodes that stand in the same place. On an '
                         'STL/OBJ import it also joins the files where they '
                         'meet (the points within one file are always '
                         'joined); unticked, a body split across files reads '
                         'as open shells. After meshing it welds coincident '
                         'mesh nodes, unless that would collapse a periodic '
                         'or layered surface.',
    },

    # DP-607 (field audit 0924 gmsh-sizing D5). Gmsh reads the Cylinder axis
    # as a half-length vector and the Frustum's second end as centre + axis,
    # so the vector's length is metres of region; titled "Axis" with no unit
    # it read as a direction, and a unit vector on a 0.1 m part refined a
    # region 2 m long.
    'gmsh.size_fields.controls/{id}/axis.x': {
        'title': 'Axis extent X',
        'documentation': 'Cylinder: the vector from the centre to one end, so its '
                         'length is the half-length and the region '
                         'reaches that far either side of the centre. '
                         'Frustum: the vector from the first end (the '
                         'centre) to the second, so its length is the '
                         'full length. (0, 0, 1) is 1 m, not a '
                         'direction.',
    },
    'gmsh.size_fields.controls/{id}/axis.y': {
        'title': 'Axis extent Y',
        'documentation': 'Cylinder: the vector from the centre to one end, so its '
                         'length is the half-length and the region '
                         'reaches that far either side of the centre. '
                         'Frustum: the vector from the first end (the '
                         'centre) to the second, so its length is the '
                         'full length. (0, 0, 1) is 1 m, not a '
                         'direction.',
    },
    'gmsh.size_fields.controls/{id}/axis.z': {
        'title': 'Axis extent Z',
        'documentation': 'Cylinder: the vector from the centre to one end, so its '
                         'length is the half-length and the region '
                         'reaches that far either side of the centre. '
                         'Frustum: the vector from the first end (the '
                         'centre) to the second, so its length is the '
                         'full length. (0, 0, 1) is 1 m, not a '
                         'direction.',
    },

    'gmsh.periodic_pairs.controls/{id}/rotation_angle_degrees': {
        'title': 'Rotation angle',
        'unit': 'deg',
        'documentation': 'Angle turned about the rotation axis to carry the '
                         'master surface onto the slave one. Read only by the '
                         'rotational transform; the translational one ignores '
                         'it.',
    },

    # ---------------------------------------------------------------- C31-11 --
    # Seven snappyHexMesh settings v13 reads and FoamMesh had no control for.
    # Each of these is obscure enough that a bare label would leave the user
    # guessing, so the tooltip says what the key does and what leaving it alone
    # means -- every one of them is optional and defaults to the behaviour the
    # previous build hard-coded.
    'meshing.castellation.surface_refinements/{id}/perpendicular_angle': {
        'documentation': 'Extra refinement where this surface meets the '
                         'background grid at a shallow angle, in degrees. '
                         'The control widens as the angle grows and is fully '
                         'open at 90 degrees; above 90 it mirrors back down. '
                         'Left empty, snappyHexMesh keeps its own default and '
                         'does no angle-driven refinement here.',
        'unit': 'deg',
    },
    'meshing.castellation.surface_refinements/{id}/patch_groups': {
        'title': 'Patch groups',
        'documentation': 'Patch groups the meshed patch joins, separated by '
                         'spaces or commas. A boundary condition set on the '
                         'group then reaches this patch without naming it. '
                         'Only written for a surface that produces a patch.',
    },
    'meshing.castellation.surface_refinements/{id}/zone_mode': {
        'title': 'Cell zone side',
        'documentation': 'Which side of the surface the cell zone is taken '
                         'from: inside the surface, outside it, the region '
                         'reached from a seed point, or none. Only read for a '
                         'surface that creates a zone.',
    },
    'meshing.castellation.surface_refinements/{id}/zone_inside_point.x': {
        'documentation': 'X of the point that seeds the cell zone. Required '
                         'by the insidePoint mode and ignored by the others.',
        'unit': 'm',  # DP-584
    },
    'meshing.castellation.surface_refinements/{id}/zone_inside_point.y': {
        'documentation': 'Y of the point that seeds the cell zone. Required '
                         'by the insidePoint mode and ignored by the others.',
        'unit': 'm',  # DP-584
    },
    'meshing.castellation.surface_refinements/{id}/zone_inside_point.z': {
        'documentation': 'Z of the point that seeds the cell zone. Required '
                         'by the insidePoint mode and ignored by the others.',
        'unit': 'm',  # DP-584
    },
    # DP-584 (field audit 0924 snappy-front D11). Three lengths drew no unit
    # while the feature-band distance beside them said `m`: the region
    # point, the zone seed point above, and a volume group's distance.
    'regions.items/{id}/point.x': {'unit': 'm'},
    'regions.items/{id}/point.y': {'unit': 'm'},
    'regions.items/{id}/point.z': {'unit': 'm'},
    # Plan 37 UF16.
    'meshing.castellation.exclude_points/{id}/name': {
        'title': 'Exclude point',
        'documentation': 'A name for the space this point removes, shown '
                         'beside its marker in the viewport.',
    },
    'meshing.castellation.exclude_points/{id}/point.x': {
        'documentation': 'X of a point inside a space to remove from the '
                         'mesh (snappyHexMesh outsidePoints). A space that '
                         'also holds a region seed is kept: the seed wins.',
        'unit': 'm',
    },
    'meshing.castellation.exclude_points/{id}/point.y': {
        'documentation': 'Y of a point inside a space to remove from the '
                         'mesh (snappyHexMesh outsidePoints).',
        'unit': 'm',
    },
    'meshing.castellation.exclude_points/{id}/point.z': {
        'documentation': 'Z of a point inside a space to remove from the '
                         'mesh (snappyHexMesh outsidePoints).',
        'unit': 'm',
    },
    'meshing.castellation.volume_refinements/{id}/distance': {'unit': 'm'},
    'meshing.castellation.feature_bands/{id}/group_name': {
        'title': 'Surface refinement group',
        'documentation': 'The surface refinement group whose feature edges '
                         'this band grades. A band naming a group that has no '
                         'surface refinement row is never read.',
    },
    'meshing.castellation.feature_bands/{id}/distance': {
        'documentation': 'How far from the feature edge this band reaches. '
                         'Bands are written in increasing distance, and '
                         'OpenFOAM stops the run if a further band asks for '
                         'more refinement than a nearer one.',
        'unit': 'm',
    },
    'meshing.castellation.feature_bands/{id}/level': {
        'documentation': 'Refinement level applied out to this distance. It '
                         'must not rise as the distance grows.',
    },
    # DP-586 (field audit 0924 snappy-front D13). The volume distance ramp.
    'meshing.castellation.volume_bands/{id}/group_name': {
        'title': 'Volume refinement group',
        'documentation': 'The volume refinement group this band grades. It '
                         'is read only while that group is in distance '
                         'mode; in any other mode the group keeps its single '
                         'level and the band is reported as unused.',
    },
    'meshing.castellation.volume_bands/{id}/distance': {
        'documentation': 'How far from the group\'s geometry this band '
                         'reaches. Bands are written in increasing distance, '
                         'and OpenFOAM stops the run if a further band asks '
                         'for more refinement than a nearer one.',
        'unit': 'm',
    },
    'meshing.castellation.volume_bands/{id}/level': {
        'documentation': 'Refinement level applied out to this distance. It '
                         'must not rise as the distance grows.',
    },
    'meshing.layers.groups/{id}/patch_selector': {
        'title': 'Selects patches by',
        'documentation': 'Whether this group covers the patches of its '
                         'geometry, or every patch whose name matches a '
                         'regular expression. A pattern is resolved against '
                         'the mesh at run time, so the editor shows what it '
                         'currently matches.',
    },
    # DP-595: OpenFOAM 13 reads relativeSizes once for all the layers.
    'meshing.layers.groups/{id}/relative_sizes': {
        'documentation': 'Whether the thicknesses are fractions of the local '
                         'cell size rather than lengths. OpenFOAM reads this '
                         'once for every layer group, so it is one setting '
                         'shown on each group: changing it here changes it '
                         'on all of them.',
    },
    'meshing.layers.groups/{id}/patch_pattern': {
        'title': 'Patch name pattern',
        'documentation': 'Regular expression matched against whole patch '
                         'names, written into the layers dictionary as a '
                         'quoted key. A pattern that matches nothing leaves '
                         'the mesh with no layers and only a warning in the '
                         'log, so check the preview beneath the field.',
    },
    'meshing.snap.detect_near_surfaces_snap': {
        'documentation': 'Whether snapping also attracts points to surfaces '
                         'that are near but not the closest, which helps thin '
                         'gaps and hurts nothing else. OpenFOAM 13 defaults '
                         'it on; leave this on Default to keep the key out of '
                         'the dictionary.',
    },
    'meshing.snappy_advanced.keep_patches': {
        'documentation': 'Whether patches that end up with no faces survive '
                         'into the written mesh. Keeping them lets a boundary '
                         'condition still refer to a patch the mesh did not '
                         'reach. OpenFOAM 13 defaults it off.',
    },

    # Plan 31 (parallel.decompose_extras). The `constraints {}` block of
    # decomposeParDict: what the partitioner is forbidden to cut. Every one
    # is off or empty by default, so a case that never touches them writes
    # the dictionary it always wrote. Verified against OpenFOAM 13 by
    # running decomposePar with each constraint present.
    'mesh.execution.preserve_face_zones': {
        'title': 'Keep faceZones whole',
        'documentation': 'Keep the owner and neighbour cell of every faceZone '
                         'face on one processor. The zone names come from the '
                         'case — snappyHexMesh creates one per interface, '
                         'cell zone and internal surface — so there is '
                         'nothing to type. Writes the preserveFaceZones '
                         'constraint.',
    },
    'mesh.execution.preserve_baffles': {
        'title': 'Keep baffle pairs together',
        'documentation': 'Keep the two duplicate faces of every baffle on the '
                         'same processor. Writes the preserveBaffles '
                         'constraint.',
    },
    'mesh.execution.preserve_patches': {
        'title': 'Patches to keep whole',
        'documentation': 'Patch names, separated by spaces or commas, whose '
                         'coupled faces must stay on one processor — cyclics '
                         'above all. Writes the preservePatches constraint. '
                         'Empty writes nothing.',
    },
    'mesh.execution.preserve_refinement_history': {
        'title': 'Keep refinement history intact',
        'documentation': 'Keep each refined cell with its siblings so the '
                         'refinement history stays unsplit and the mesh can '
                         'still be unrefined. Writes the refinementHistory '
                         'constraint.',
    },
    # Plan 31 (checkmesh.thresholds_and_region). checkMesh's reporting
    # thresholds and its optional checks, verified against OpenFOAM 13's own
    # `checkMesh -help` and by running each flag.
    'quality.check.non_orth_threshold': {
        'title': 'Non-orthogonality reported above',
        'documentation': 'Faces above this angle are reported as severely '
                         'non-orthogonal and fail the check. OpenFOAM 13 '
                         'defaults to 70. Lowering it does not change the '
                         'mesh, only the verdict on it.',
    },
    'quality.check.skew_threshold': {
        'title': 'Skewness reported above',
        'documentation': 'Faces above this skewness are reported as highly '
                         'skew. OpenFOAM 13 defaults to 4.',
    },
    'quality.check.user_defined_checks': {
        'title': 'Judge against the mesher\'s own limits',
        'documentation': 'Runs checkMesh with -meshQuality, which reads the '
                         'criteria from system/meshQualityDict. FoamMesh '
                         'writes that file from the very limits above that '
                         'snappyHexMesh was given, so the mesh is checked '
                         'against what it was asked for. Offending faces are '
                         'written to a meshQualityFaces set.',
    },
    'quality.check.skip_topology': {
        'title': 'Skip the topology checks',
        'documentation': 'Runs checkMesh with -noTopology. Only for a mesh '
                         'whose connectivity is already known good; the '
                         'topology checks are the ones that catch a broken '
                         'mesh rather than a poor one.',
    },
    'quality.check.write_surfaces': {
        'title': 'Write the problem faces as a surface',
        'documentation': 'Runs checkMesh with -writeSurfaces, which '
                         'reconstructs the faceSets and cellSets of the '
                         'problem faces and writes them under '
                         'postProcessing/checkMesh/, where the viewport '
                         'can highlight them. Separate from Write failed '
                         'sets, which writes the sets into '
                         'constant/polyMesh/sets and never a face surface.',
    },
    # Plan 37 UF18. The three switches every run carried and no one could
    # reach; on by default, so the command line is unchanged until a user
    # turns one off. MEASURED on v13: plans/evidence/plan37/
    # uf18-v13-checkmesh-sets.md.
    'quality.check.all_topology': {
        'title': 'Run every topology check',
        'documentation': 'Runs checkMesh with -allTopology: the extra '
                         'topology checks, such as the upper-triangular face '
                         'order and the points shared by separate regions. '
                         'Off runs only the standard topology checks.',
    },
    'quality.check.all_geometry': {
        'title': 'Run every geometry check',
        'documentation': 'Runs checkMesh with -allGeometry: the extra '
                         'geometry checks, such as face warpage, concave '
                         'cells, short edges and near points. Off runs only '
                         'the standard geometry checks, which can change the '
                         'verdict on the same mesh.',
    },
    'quality.check.write_sets': {
        'title': 'Write failed sets',
        'documentation': 'Runs checkMesh with -writeSets, which writes every '
                         'failing face, cell and point set into '
                         'constant/polyMesh/sets and the point sets (unused '
                         'points, short edges, near points) under '
                         'postProcessing/checkMesh/, where the viewport can '
                         'highlight them. It writes no face surface: that is '
                         'Write the problem faces as a surface.',
    },

    # Plan 31 (surface_features.rest). Verified against v13's
    # surfaceFeatures.C; the line numbers are in the schema comment.
    'meshing.surface_features.geometric_test_only': {
        'title': 'Ignore the surface\'s own region boundaries',
        'documentation': 'Extracts features from the geometry alone, so a '
                         'tessellation that arrives already split into '
                         'patches does not gain a feature edge along every '
                         'patch seam.',
    },
    'meshing.surface_features.trim_min_length': {
        'title': 'Drop feature lines shorter than',
        'documentation': 'Feature lines shorter than this are removed before '
                         'the .eMesh is written. Zero, the default, trims '
                         'nothing. This is the filter for the short spurious '
                         'edges a coarse tessellation produces.',
    },
    'meshing.surface_features.trim_min_elements': {
        'title': 'Drop feature lines with fewer edges than',
        'documentation': 'Feature lines made of fewer than this many edges '
                         'are removed. Zero, the default, trims nothing.',
    },
    'meshing.surface_features.keep_non_manifold_edges': {
        'title': 'Keep non-manifold edges',
        'documentation': 'On by default, which is OpenFOAM\'s own default. '
                         'Turn it off to drop edges where more than two '
                         'faces meet — usually a defect of the '
                         'tessellation rather than a feature of the shape.',
    },
    'meshing.surface_features.keep_open_edges': {
        'title': 'Keep open edges',
        'documentation': 'On by default, which is OpenFOAM\'s own default. '
                         'Turn it off to drop edges with only one adjacent '
                         'face — the border of a hole in the surface.',
    },
    'meshing.surface_features.face_closeness': {
        'title': 'Write the face-closeness field',
        'documentation': 'Writes internal and external closeness per face '
                         'alongside the feature edges. The point-closeness '
                         'field span-based refinement needs is written '
                         'automatically for the surfaces that need it; this '
                         'is the per-face field, for inspection.',
    },
    'meshing.surface_features.internal_angle_tolerance': {
        'title': 'Internal closeness angle tolerance',
        'documentation': 'How far from directly opposite two faces may be '
                         'and still count as bounding the same internal gap. '
                         'OpenFOAM 13 defaults to 80 degrees.',
    },
    'meshing.surface_features.external_angle_tolerance': {
        'title': 'External closeness angle tolerance',
        'documentation': 'The same tolerance, for closeness measured outside '
                         'the surface. OpenFOAM 13 defaults to 80 degrees.',
    },
    'meshing.surface_features.feature_proximity': {
        'title': 'Write the feature-proximity field',
        'documentation': 'Writes, per face, how close the nearest feature '
                         'point or edge is — which is how you find the '
                         'places a cell size will not resolve.',
    },
    'meshing.surface_features.max_feature_proximity': {
        'title': 'Feature-proximity search distance',
        'documentation': 'How far to look for a nearby feature; faces with '
                         'nothing closer report this value. OpenFOAM 13 '
                         'requires it whenever the proximity field is asked '
                         'for and aborts the extraction if it is missing, so '
                         'it is always written alongside.',
    },
    'meshing.surface_features.write_obj': {
        'title': 'Write the extracted edges as OBJ',
        'documentation': 'Writes the feature edges as OBJ files beside the '
                         'surface, for opening in another tool or attaching '
                         'to a support request.',
    },
    'meshing.surface_features.verbose_obj': {
        'title': 'Include the per-classification OBJ files',
        'documentation': 'Adds the region, external and internal edge files '
                         'to the OBJ output. It has no effect on its own: '
                         'surfaceFeatures writes these only alongside the '
                         'plain OBJ output.',
    },
    'meshing.surface_features.write_vtk': {
        'title': 'Write the extracted edges as VTK',
        'documentation': 'Writes the feature edges, and any closeness or '
                         'proximity fields asked for above, as VTK.',
    },
    # Plan 37 UF19. subsetFeatures box/plane and addFeatures; the v13
    # behaviour is pinned in plans/evidence/plan37/uf19-v13-surface-features.md.
    'meshing.surface_features.subset_box': {
        'title': 'Keep feature edges in a box',
        'documentation': 'Inside box keeps only the feature edges whose '
                         'midpoint is inside the box; Outside box keeps only '
                         'those whose midpoint is outside it. None, the '
                         'default, keeps every edge. Coordinates are in the '
                         'surface file\'s own frame: surfaceFeatures does not '
                         'apply the geometry scale snappyHexMesh uses. A box '
                         'whose minimum is above its maximum is refused — '
                         'OpenFOAM 13 would keep no edges and not say so.',
    },
    **{
        f'meshing.surface_features.subset_box_{end}.{axis}': {
            'title': f'Box {label} {axis.upper()}',
            'documentation': f'The {label} {axis.upper()} corner of the '
                             f'feature subset box, in the surface file\'s '
                             f'frame.',
            'unit': 'm',
            'applies_when': ('meshing.surface_features.subset_box != none',),
        }
        for end, label in (('min', 'minimum'), ('max', 'maximum'))
        for axis in 'xyz'
    },
    'meshing.surface_features.subset_plane': {
        'title': 'Keep feature edges crossing a plane',
        'documentation': 'Keeps only the feature edges that cross the plane '
                         'through the point below with the normal below; '
                         'an edge lying beside the plane is dropped. Applied '
                         'after the box and the open / non-manifold edge '
                         'switches, as OpenFOAM 13 applies it.',
    },
    **{
        f'meshing.surface_features.subset_plane_point.{axis}': {
            'title': f'Plane point {axis.upper()}',
            'documentation': f'{axis.upper()} of a point on the subset plane, '
                             f'in the surface file\'s frame.',
            'unit': 'm',
            'applies_when': ('meshing.surface_features.subset_plane == true',),
        }
        for axis in 'xyz'
    },
    **{
        f'meshing.surface_features.subset_plane_normal.{axis}': {
            'title': f'Plane normal {axis.upper()}',
            'documentation': f'{axis.upper()} of the subset plane\'s normal. '
                             f'Its length does not matter, but it cannot be '
                             f'zero: OpenFOAM 13 stops on a zero normal.',
            'applies_when': ('meshing.surface_features.subset_plane == true',),
        }
        for axis in 'xyz'
    },
    'meshing.surface_features.add_features_file': {
        'title': 'Add feature edges from file',
        'documentation': 'An OpenFOAM extendedFeatureEdgeMesh file whose '
                         'edges are added to the first surface\'s feature '
                         'set, after the subset (so they are never filtered '
                         'out). It is copied into the case under a name '
                         'carrying its content hash: editing or deleting the '
                         'file makes feature extraction, and every mesh '
                         'built on it, out of date. A .eMesh is not '
                         'accepted; OpenFOAM 13 cannot read one there. '
                         'Empty, the default, adds nothing.',
    },

    'mesh.execution.decomposition_weight_field': {
        'title': 'Cell weight field',
        'documentation': 'Name of a volScalarField the decomposition weights '
                         'cells by, so heavier cells are spread across ranks. '
                         'OpenFOAM 13 reads it MUST_READ: naming a field the '
                         'case does not carry aborts decomposePar with '
                         '"cannot find file". Empty, the default, writes no '
                         'key.',
    },
    # Plan 33 VOLUME-03/04. Two booleans that decided how the mesh comes out
    # and said neither what they do nor where they apply. `Automatic` named
    # the automation and not what is automated; `Transfinite tri` named a
    # Gmsh API call. And `automatic` carries the same target guard
    # recombination carries -- `derive_structuring` clears it on every route
    # but SU2, with a warning -- so on the OpenFOAM route it was a switch
    # the run throws away, offered as a choice. The clause is the guard the
    # derivation applies, written where the form can read it.
    'gmsh.volume_controls.automatic': {
        'title': 'Automatic structured meshing',
        'applies_when': ('mesh.engine == gmsh', 'mesh.target_solver == su2'),
    },
    # No guard: this one changes how a three-sided face is filled, not what
    # family it is filled with. MEASURED on the elbow: 120 triangles became
    # 64, and both counts are triangles, so the polyMesh route keeps it.
    'gmsh.volume_controls.transfinite_tri': {
        'title': 'Structured triangular surfaces',
    },
    # DP-621 (field audit 0924 gmsh-generate-export D1). Renumbering sat in
    # `gmsh/output` and inherited the element-order clause, which rules out
    # both named targets -- while `derive_export` keeps the renumbering on
    # the SU2 route only, because MSH 2.2 throws the numbering away. So no
    # target both offered the control and applied it. Its clause is the
    # derivation's own guard.
    'gmsh.compute.renumber': {
        'applies_when': ('mesh.engine == gmsh', 'mesh.target_solver == su2'),
    },
    # DP-624 (field audit 0924 gmsh-generate-export D4). The high-order
    # optimiser moves the midside nodes of curved elements, so the runner
    # applies it only at element order 2 -- yet it was live at order 1 and
    # on both named targets, which pin the order to 1. It now follows the
    # element order's own clause and asks for order 2 on top of it.
    'gmsh.compute.high_order_optimize': {
        'applies_when': ('mesh.engine == gmsh',
                         'mesh.target_solver != openfoam',
                         'mesh.target_solver != su2',
                         'gmsh.compute.element_order == 2'),
    },
    # DP-625 (field audit 0924 gmsh-generate-export D6). Which recombiner,
    # and whether to split the quads again, mean something only when the
    # surfaces are recombined; both stayed live with Recombine off. Recombine
    # itself stays live on OpenFOAM: recombine plus split is a route the
    # derivation keeps there.
    'gmsh.global_sizing.recombination_algorithm': {
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.global_sizing.recombine == true'),
    },
    'gmsh.global_sizing.split_quadrangles': {
        'applies_when': ('mesh.engine == gmsh',
                         'gmsh.global_sizing.recombine == true'),
    },
    # DP-626 (field audit 0924 gmsh-generate-export D8). Order 2 is offered
    # only while no target solver is chosen, and nothing said so.
    'gmsh.compute.element_order': {
        'documentation': 'Second order needs no target solver: both '
                         'solver routes read first-order elements only, so '
                         'this is offered only while Target solver is '
                         'unselected.',
    },
    # DP-627 (field audit 0924 gmsh-generate-export D2). Netgen, the Netgen
    # passes and the threshold belong to the optimiser that Optimize turns
    # on; since DP-620 none of them runs with it off, so none is offered.
    'gmsh.compute.netgen': {
        'title': 'Netgen optimiser',
        'applies_when': ('mesh.engine == gmsh', 'gmsh.compute.optimize == true'),
        'documentation': 'Also run the Netgen tetrahedron optimiser after '
                         'Gmsh\'s own.',
    },
    'gmsh.compute.netgen_passes': {
        'applies_when': ('mesh.engine == gmsh', 'gmsh.compute.optimize == true'),
    },
    'gmsh.compute.optimize_threshold': {
        'applies_when': ('mesh.engine == gmsh', 'gmsh.compute.optimize == true'),
        'documentation': 'Tetrahedra whose quality is below this fraction '
                         'are the ones the optimiser works on.',
    },
    'gmsh.compute.smoothing': {
        'documentation': 'Number of smoothing steps Gmsh applies to the '
                         'final mesh (Mesh.Smoothing).',
    },
}
