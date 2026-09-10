"""Hand-authored, reviewed metadata overlay for the field registry (AF2).

The SimpleDB schema carries types and ranges but not units, documentation,
staleness/invalidation, applicability, or UI location. The plan requires this
metadata to be hand-authored and reviewed; it is expressed here as reviewed
per-group defaults plus targeted per-field overrides so the full inventory is
covered without inferring semantics from widget classes.

``invalidates`` values are the artifact fingerprints a change stales:
``mesh`` (blockMesh/snappyHexMesh output) and ``quality`` (checkMesh report).
``ui_location`` names the workflow step/page that renders the field.
"""
from __future__ import annotations

# Fallback when a field's group is not listed below.
DEFAULT_INVALIDATION = ('mesh', 'quality')


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
        'invalidates': ('engine_plan', 'mesh', 'quality'),
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
        'invalidates': ('mesh', 'quality', 'export'),
        'ui_location': 'workflow.geometry_repair',
    },
    'gmsh': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh',
        'applies_when': ('mesh.engine == gmsh',),
    },
    'gmsh/healing': {
        # Healing changes what Gmsh imports, so everything downstream is stale.
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.describe_geometry',
        'applies_when': ('mesh.engine == gmsh',),
        # Plan 31. `booleanTolerance` is a length in model units like the
        # import tolerance; `importScaling` multiplies the imported shape, so
        # it is a ratio and not a length.
        'units': {'importTolerance': 'm', 'booleanTolerance': 'm',
                  'importScaling': 'ratio'},
    },
    'gmsh/globalSizing': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'targetSize': 'm', 'minimumSize': 'm',
                  'sizeFactor': 'ratio', 'fromCurvature': 'elements',
                  'minimumCirclePoints': 'points',
                  'minimumCurvePoints': 'points',
                  'minimumElementsPerTwoPi': 'elements'},
        # The combiner is a choice, not a quantity, so it carries no unit.
    },
    'gmsh/algorithms': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
    },
    'gmsh/farfield': {
        # It changes the domain itself, so everything downstream of the mesh
        # is a different problem, not merely a different discretisation.
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.describe_geometry',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'padding': 'diagonals'},
    },
    'gmsh/parallel': {
        # Not just wall clock: HXT partitions the domain by thread count, so
        # the same job at a different thread count is a different mesh.
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.global_sizing',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'threads': 'threads'},
    },
    'gmsh/optimization': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.compute',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'minQuality': 'ratio', 'netgenPasses': 'passes',
                  'optimizeThreshold': 'ratio',
                  'smoothing': 'steps', 'highOrderOptimize': 'mode'},
    },
    'gmsh/boundaryLayers': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.boundary_layers',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'firstHeight': 'm', 'totalThickness': 'm',
                  'ratio': 'ratio', 'layerCount': 'layers'},
    },
    'gmsh/sizeFields': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.size_fields',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'sizeInside': 'm', 'sizeOutside': 'm',
                  'distanceMin': 'm', 'distanceMax': 'm',
                  'radius': 'm', 'radiusEnd': 'm', 'thickness': 'm',
                  'sampling': 'points', 'curvatureDelta': 'm',
                  'curvatureMin': '1/m', 'curvatureMax': '1/m'},
    },
    'gmsh/surfaceSizes': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.size_fields',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'targetSize': 'm', 'blendDistance': 'm'},
    },
    'gmsh/output': {
        'invalidates': ('mesh', 'quality', 'exports'),
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
        'units': {'elementOrder': 'order'},
    },
    'gmsh/curveControls': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.curve_controls',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'localSize': 'm', 'segments': 'elements',
                  'coefficient': 'ratio'},
    },
    'gmsh/volumeControls': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.volume_controls',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'targetSize': 'm'},
    },
    'gmsh/periodicPairs': {
        'invalidates': ('mesh', 'quality', 'exports'),
        'ui_location': 'workflow.gmsh.periodic',
        'applies_when': ('mesh.engine == gmsh',),
        'units': {'matchTolerance': 'm', 'rotationAngleDegrees': 'deg'},
    },
    'geometry': {
        'invalidates': ('mesh', 'quality'),
        'ui_location': 'workflow.geometry',
    },
    'interfacePairs': {
        'invalidates': ('engine_plan', 'mesh', 'quality', 'export'),
        'ui_location': 'workflow.geometry.interfaces',
    },
    'region': {
        'invalidates': ('mesh', 'quality'),
        'ui_location': 'workflow.region',
    },
    'baseGrid': {
        'invalidates': ('mesh', 'quality'),
        'ui_location': 'workflow.base_grid',
    },
    'castellation': {
        'invalidates': ('mesh', 'quality'),
        'ui_location': 'workflow.castellation',
        'units': {'resolve_feature_angle': 'deg', 'max_load_unbalance': 'fraction',
                  'planar_angle': 'deg'},
    },
    'snap': {
        'invalidates': ('mesh', 'quality'),
        'ui_location': 'workflow.snap',
        # ``concaveAngle`` and ``minAreaRatio`` are ESI snapControls keys.
        # Foundation 13 does not read either, the controls were removed, and
        # these two entries described nothing.
        'units': {},
    },
    'addLayers': {
        'invalidates': ('mesh', 'quality'),
        'ui_location': 'workflow.layers',
        # ``min_medial_axis_angle``: Plan 30 WP-04 (F-18) renamed the schema
        # path to OpenFOAM 13's own spelling, so the unit names it too. The
        # misspelling survives in the release only as the second name of a
        # ``lookupBackwardsCompatible`` pair, and in saved projects only until
        # ``migrateDocument`` reads them.
        'units': {'feature_angle': 'deg', 'min_medial_axis_angle': 'deg',
                  'slip_feature_angle': 'deg',
                  'max_face_thickness_ratio': 'fraction',
                  'max_thickness_to_medial_ratio': 'fraction'},
    },
    'snappyGeometry': {
        # Plan 31. These change what snappyHexMesh is told about the staged
        # surfaces -- whether they enclose a volume, how finely they are
        # searched, how narrow a gap counts as closed -- so the mesh already
        # produced was built on a different description and no longer stands.
        'invalidates': ('mesh', 'quality', 'exports'),
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
        'invalidates': ('mesh',),
        'ui_location': 'workflow.castellation',
    },
    'meshQuality': {
        # Quality thresholds gate the checkMesh report; they do not by
        # themselves invalidate the mesh geometry already produced.
        'invalidates': ('quality',),
        'ui_location': 'workflow.quality',
        'units': {'max_non_orthogonality': 'deg', 'max_boundary_skewness': 'deg',
                  'max_internal_skewness': 'deg', 'max_concave': 'deg'},
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
        'invalidates': ('mesh',),
        'ui_location': 'workflow.surface_features',
        'units': {'internal_angle_tolerance': 'deg',
                  'external_angle_tolerance': 'deg',
                  'trim_min_length': 'm',
                  'max_feature_proximity': 'm'},
    },
}


# Per-field overrides keyed by semantic field ID. Any FieldDescriptor attribute
# may be overridden; unspecified keys fall back to the generated/group value.
FIELD_OVERRIDES: dict[str, dict] = {
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
                         'units -- setting it scales again on top of the '
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
                         'decides which meshing engines are offered -- SU2 '
                         'reads tetrahedra, hexahedra, prisms and pyramids '
                         'only, so snappyHexMesh cannot serve it -- which '
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
        'invalidates': ('export', 'engine'),
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
    'meshing.base_grid.cells.x': {
        'documentation': 'Number of background mesh cells along X.',
        'unit': 'cells',
    },
    'meshing.base_grid.cells.y': {
        'documentation': 'Number of background mesh cells along Y.',
        'unit': 'cells',
    },
    'meshing.base_grid.cells.z': {
        'documentation': 'Number of background mesh cells along Z.',
        'unit': 'cells',
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
            'local third direction -- 0 1 2 3 4 5 6 7. Rows are numbered from '
            'zero in the Vertices table above.'),
    },
    'base_grid.blocks/{id}/num_cells_x': {
        'documentation': 
            'Cells along the block local X direction.',
        'unit': 'cells',
    },
    'base_grid.blocks/{id}/grading_x': {
        'documentation': (
            'Expansion along the block local X direction: a plain ratio '
            'of last cell to first (4), or a segmented profile written the '
            'way blockMeshDict writes one -- (0.2 0.3 4) (0.6 0.4 1) '
            '(0.2 0.3 0.25), whose length and cell fractions each add up to 1.'),
    },
    'base_grid.blocks/{id}/grading_x_toward_start': {
        'title': 'Fine end of X is the start',
        'documentation': (
            'Take the reciprocal of the ratio, so the small cells sit at the '
            'start of the direction instead of the end. Inverting it by hand '
            'is where the direction usually gets flipped.'),
    },
    'base_grid.blocks/{id}/num_cells_y': {
        'documentation': 
            'Cells along the block local Y direction.',
        'unit': 'cells',
    },
    'base_grid.blocks/{id}/grading_y': {
        'documentation': (
            'Expansion along the block local Y direction: a plain ratio '
            'of last cell to first (4), or a segmented profile written the '
            'way blockMeshDict writes one -- (0.2 0.3 4) (0.6 0.4 1) '
            '(0.2 0.3 0.25), whose length and cell fractions each add up to 1.'),
    },
    'base_grid.blocks/{id}/grading_y_toward_start': {
        'title': 'Fine end of Y is the start',
        'documentation': (
            'Take the reciprocal of the ratio, so the small cells sit at the '
            'start of the direction instead of the end. Inverting it by hand '
            'is where the direction usually gets flipped.'),
    },
    'base_grid.blocks/{id}/num_cells_z': {
        'documentation': 
            'Cells along the block local Z direction.',
        'unit': 'cells',
    },
    'base_grid.blocks/{id}/grading_z': {
        'documentation': (
            'Expansion along the block local Z direction: a plain ratio '
            'of last cell to first (4), or a segmented profile written the '
            'way blockMeshDict writes one -- (0.2 0.3 4) (0.6 0.4 1) '
            '(0.2 0.3 0.25), whose length and cell fractions each add up to 1.'),
    },
    'base_grid.blocks/{id}/grading_z_toward_start': {
        'title': 'Fine end of Z is the start',
        'documentation': (
            'Take the reciprocal of the ratio, so the small cells sit at the '
            'start of the direction instead of the end. Inverting it by hand '
            'is where the direction usually gets flipped.'),
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
    'meshing.castellation.max_global_cells': {
        'documentation': 'Hard ceiling on total cell count across all processors.',
        'unit': 'cells',
    },
    'meshing.castellation.max_local_cells': {
        'documentation': 'Ceiling on cells per processor during refinement.',
        'unit': 'cells',
    },
    'meshing.castellation.cells_between_levels': {
        'documentation': 'Buffer cells inserted between adjacent refinement levels.',
        'unit': 'cells',
    },
    'meshing.snap.tolerance': {
        'documentation': 'Snapping distance as a multiple of local edge length.',
    },
    'meshing.snap.smooth_patch_iterations': {
        'documentation': 'Patch-smoothing iterations applied after snapping.',
        'unit': 'iterations',
    },
    'meshing.layers.growth_cells': {
        'documentation': 'Number of layer cells grown outward from the surface.',
        'unit': 'cells',
    },
    'meshing.layers.feature_angle': {
        'documentation': 'Angle above which layer growth stops at a feature edge.',
        'unit': 'deg',
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
    },
    'meshing.castellation.surface_refinements/{id}/zone_inside_point.y': {
        'documentation': 'Y of the point that seeds the cell zone. Required '
                         'by the insidePoint mode and ignored by the others.',
    },
    'meshing.castellation.surface_refinements/{id}/zone_inside_point.z': {
        'documentation': 'Z of the point that seeds the cell zone. Required '
                         'by the insidePoint mode and ignored by the others.',
    },
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
    'meshing.layers.groups/{id}/patch_selector': {
        'title': 'Selects patches by',
        'documentation': 'Whether this group covers the patches of its '
                         'geometry, or every patch whose name matches a '
                         'regular expression. A pattern is resolved against '
                         'the mesh at run time, so the editor shows what it '
                         'currently matches.',
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
                         'case -- snappyHexMesh creates one per interface, '
                         'cell zone and internal surface -- so there is '
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
                         'coupled faces must stay on one processor -- cyclics '
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
                         'postProcessing/checkMesh/. Separate from the sets '
                         'themselves, which are always written into '
                         'constant/polyMesh/sets.',
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
                         'faces meet -- usually a defect of the '
                         'tessellation rather than a feature of the shape.',
    },
    'meshing.surface_features.keep_open_edges': {
        'title': 'Keep open edges',
        'documentation': 'On by default, which is OpenFOAM\'s own default. '
                         'Turn it off to drop edges with only one adjacent '
                         'face -- the border of a hole in the surface.',
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
                         'point or edge is -- which is how you find the '
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

    'mesh.execution.decomposition_weight_field': {
        'title': 'Cell weight field',
        'documentation': 'Name of a volScalarField the decomposition weights '
                         'cells by, so heavier cells are spread across ranks. '
                         'OpenFOAM 13 reads it MUST_READ: naming a field the '
                         'case does not carry aborts decomposePar with '
                         '"cannot find file". Empty, the default, writes no '
                         'key.',
    },
}
