"""Plan 40 UXD5 (F3e): the words behind the help pane, one per field.

The help pane at the foot of a settings page follows the field that has focus
and shows what the registry says about it: a two-line explanation, a diagram
for the geometric fields, the OpenFOAM or Gmsh keyword and a manual link. The
text lives here, in the registry, so the tooltip (which is built from
``FieldDescriptor.documentation``) and the pane say the same thing.

``FIELD_HELP`` is the explanation of a field whose ``FIELD_OVERRIDES`` row
carries no ``documentation`` of its own; an override wins when it has one.
Every explanation is at most two sentences: what the setting changes, and
what to do with it.
"""
from __future__ import annotations

import re

#: id -> the explanation, for the fields whose override says nothing.
FIELD_HELP: dict[str, str] = {
    # ---- Geometry preparation decision -------------------------------
    'sizing.field.anisotropic': (
        'Also compute the anisotropic metric from the curvature directions. '
        'Experimental; used only with the MMG3D algorithm.'),
    'sizing.field.bulk_size': (
        'Cell size far from every surface. 0 takes one twentieth of the '
        'model length.'),
    'sizing.field.gradation': (
        'The largest growth of the size from one cell to the next, away '
        'from a surface. 1.1 grows 10 % per cell.'),
    'sizing.field.maximum_size': (
        'The largest size the field may give. 0 takes the bulk size.'),
    'sizing.field.minimum_size': (
        'The smallest size the field may give. 0 takes one thousandth of '
        'the model length.'),
    'sizing.field.nodes_per_gap': (
        'Cells to place across a thin gap or wall. 0 turns the thin-region '
        'rule off.'),
    'sizing.field.nodes_per_two_pi': (
        'Points around a full circle on curved surfaces; sets how fine the '
        'field is where the surface bends.'),
    'sizing.field.smoothing': (
        'Smooth the size field after it is limited, so neighbouring cells '
        'differ gently.'),
    'sizing.field.use_as_background': (
        'Mesh with the computed size field as the background size, so the '
        'mesh follows the shape without hand-made size fields. While on, '
        'size from points, from curvature and extend from boundary are held '
        'at 0 for the run; your own size fields still combine with it. '
        'Needs a computed field.'),
    'geometry_preparation.acknowledgment': (
        'The note kept with a decision to go on with a geometry that has '
        'open findings. It is stored with the project so a later reader '
        'knows why.'),
    'geometry_preparation.decision': (
        'What you decided to do with the imported surface: use it as it is, '
        'repair it, wrap it, or override the findings. Until you decide, '
        'the next steps wait.'),
    'geometry_preparation.default_boundary_category': (
        'The kind of boundary given to every surface you have not '
        'classified yourself. Wall is right for most solid bodies.'),
    'geometry_preparation.geometry_fingerprint': (
        'A short code of the geometry the decision was made on. If the '
        'geometry changes, the code changes and the decision is asked '
        'again.'),
    'geometry_preparation.qualification_tolerance_m': (
        'The gap, in metres, below which two surface points count as the '
        'same point when the surface is checked. Zero uses a tolerance '
        'taken from the size of the model.'),
    'geometry_preparation.rules_version': (
        'The version of the checking rules the decision was made under. '
        'A newer version asks for the decision again.'),
    # ---- Gmsh boundary layers ---------------------------------------
    'gmsh.boundary_layers.enabled': (
        'Grow thin layers of cells along walls so the flow next to the wall '
        'is resolved. Off meshes straight to the wall.'),
    'gmsh.boundary_layers.first_height': (
        'The thickness of the layer of cells touching the wall, in metres. '
        'Pick it from the wall distance your turbulence model wants.'),
    'gmsh.boundary_layers.layer_count': (
        'How many layers of cells are grown from the wall. More layers '
        'reach further into the flow and cost more cells.'),
    'gmsh.boundary_layers.mode': (
        'How the layer stack is described: first height with a growth '
        'ratio, or a total thickness with a layer count. The other '
        'numbers are worked out from the two you give.'),
    'gmsh.boundary_layers.elements_per_layer': (
        'How many cells each layer is divided into. 1 keeps one cell '
        'per layer; 2 splits every layer in two, with the same total '
        'thickness.'),
    'gmsh.boundary_layers.stacks': (
        'A stack of its own for a named wall: its layer count, first '
        'height, ratio, total thickness, cells per layer and cell '
        'shape. A wall with no row of its own keeps the stack above.'),
    'gmsh.boundary_layers.patch_mode': (
        'Grow layers on the walls you pick in the table, or on every wall '
        'the mesher finds eligible. Eligible walls leave out inlets, '
        'outlets and symmetry planes.'),
    'gmsh.boundary_layers.patches': (
        'The boundary patches that get layers, kept as the list the '
        'patch table shows. Only used when layers are grown on selected '
        'walls.'),
    'gmsh.boundary_layers.quads': (
        'Make the layer cells prisms and bricks instead of splitting them '
        'into tetrahedra. Prisms follow the wall with fewer cells.'),
    'gmsh.boundary_layers.ratio': (
        'How much thicker each layer is than the one below it. 1.2 grows '
        'each layer by a fifth; values near 1 keep the layers even.'),
    'gmsh.boundary_layers.total_thickness': (
        'The thickness of the whole layer stack, in metres. Used when '
        'layers are described by total thickness and count.'),
    # ---- Gmsh compute -----------------------------------------------
    'gmsh.compute.allowed_count': (
        'How many cells may stay below the quality limit before the '
        'mesh is refused. Zero means none.'),
    'gmsh.compute.allowed_fraction': (
        'The share of cells that may stay below the quality limit, as a '
        'fraction of all cells. Zero means none.'),
    'gmsh.compute.extra_limits': (
        'Further quality limits, one per line, each a measure and the '
        'worst value allowed. They are checked in addition to the main '
        'limit.'),
    'gmsh.compute.hard_floor': (
        'A quality no cell may fall below, whatever the allowed count or '
        'share says. Zero turns the floor off.'),
    'gmsh.compute.high_order_optimize': (
        'How hard Gmsh smooths curved higher-order elements: 0 not at '
        'all, higher numbers run more passes. Only matters for second '
        'order meshes.'),
    'gmsh.compute.min_quality': (
        'The worst cell quality the mesh may contain, from 0 to 1. Higher '
        'is stricter; the quality measure below says how it is scored.'),
    'gmsh.compute.netgen_passes': (
        'How many times the Netgen optimiser is run over the tetrahedra. '
        'More passes improve poor cells and take longer.'),
    'gmsh.compute.optimize': (
        'Run Gmsh\'s own tetrahedron optimiser after meshing. It swaps '
        'and moves nodes to lift the worst cells.'),
    'gmsh.compute.quality_type': (
        'The measure used to score a cell: SICN (inverse condition '
        'number), SIGE (gradient error), gamma (inscribed to '
        'circumscribed radius) or distortion.'),
    'gmsh.compute.renumber': (
        'Renumber nodes and elements after meshing to cut the bandwidth '
        'of the matrices a solver builds. It changes numbering only, not '
        'the mesh.'),
    'gmsh.compute.renumber_method': (
        'How the nodes are renumbered: RCMK (reverse Cuthill-McKee, the '
        'default), Hilbert (along a space-filling curve) or Metis. These '
        'are the three methods Gmsh 4.15.2 offers.'),
    'gmsh.compute.repair_poor_elements': (
        'Try to repair cells below the quality limit by local remeshing '
        'before the mesh is refused.'),
    'gmsh.compute.second_order_incomplete': (
        'Leave out the mid-face and mid-volume nodes of second order '
        'elements, keeping only the mid-edge ones.'),
    'gmsh.compute.second_order_linear': (
        'Put the second order nodes on the straight lines of the '
        'first order mesh instead of on the curved geometry.'),
    # ---- Gmsh geometry preparation ----------------------------------
    'gmsh.describe_geometry.auto_fix': (
        'Let the CAD kernel run its own automatic repair on imported '
        'shapes. Leave it on unless it changes a shape you need exactly.'),
    'gmsh.describe_geometry.boolean_tolerance': (
        'The tolerance, in metres, used when bodies are fused or cut. '
        'Zero lets the kernel choose.'),
    'gmsh.describe_geometry.classification_angle': (
        'When a triangulated surface is split into faces, two triangles '
        'meeting at more than this angle start a new face.'),
    'gmsh.describe_geometry.fix_degenerated': (
        'Remove degenerated edges and faces, those of zero length or '
        'area, from imported shapes.'),
    'gmsh.describe_geometry.fix_small_edges': (
        'Remove edges shorter than the import tolerance from imported '
        'shapes, closing the gaps they leave.'),
    'gmsh.describe_geometry.fix_small_faces': (
        'Remove faces smaller than the import tolerance from imported '
        'shapes.'),
    'gmsh.describe_geometry.heal_shapes': (
        'Run the shape healing pass on the imported CAD, which repairs '
        'small gaps and bad tolerances before meshing.'),
    'gmsh.describe_geometry.import_labels': (
        'Keep the names and colours stored in a STEP file so surfaces '
        'arrive with their names.'),
    'gmsh.describe_geometry.import_scaling': (
        'A factor applied to the geometry when it is read. Use 0.001 to '
        'read a file drawn in millimetres as metres.'),
    'gmsh.describe_geometry.import_tolerance': (
        'Points closer than this, in metres, are treated as the same '
        'point when the CAD is read.'),
    'gmsh.describe_geometry.make_solids': (
        'Sew closed sets of faces into solid volumes after import, so '
        'they can be meshed as volumes.'),
    'gmsh.describe_geometry.occ_parallel': (
        'Let the CAD kernel work on shapes with several threads. It '
        'speeds up large assemblies and can change the order faces are '
        'numbered in.'),
    'gmsh.describe_geometry.remove_duplicate_faces': (
        'Delete faces that appear twice in the imported shape, which '
        'otherwise leave non-manifold interfaces.'),
    'gmsh.describe_geometry.sealed_cavities': (
        'What to do with a fluid pocket fully enclosed by solids: '
        'discard it, keep it as its own region, or refuse the geometry.'),
    'gmsh.describe_geometry.sew_faces': (
        'Join faces that share edges into one shell when the CAD is '
        'read. Turn it on for files that arrive as loose faces.'),
    'gmsh.describe_geometry.union_unify': (
        'After a fuse, merge faces and edges that lie in the same '
        'surface or curve, which gives cleaner patches.'),
    # ---- Gmsh tables ------------------------------------------------
    'gmsh.entity_controls.rows': (
        'Size controls attached to named surfaces, curves or volumes, '
        'one row each, kept as the table shows them.'),
    'gmsh.native_fields.rows': (
        'Gmsh background size fields (Distance, Threshold, Box and so '
        'on), one row each, combined by the rule in Sizing.'),
    'gmsh.options.overrides': (
        'Raw Gmsh options set by name, one per line, applied last. They '
        'win over every other control on the pages.'),
    'gmsh.point_sizes.rows': (
        'A size at chosen points; the mesh around each point grows from '
        'it. One row per point, kept as the table shows them.'),
    'gmsh.structure.rows': (
        'Structured (transfinite) meshing controls for chosen curves '
        'and surfaces, one row each, kept as the table shows them.'),
    # ---- Gmsh global sizing -----------------------------------------
    'gmsh.global_sizing.algorithm_fallback': (
        'If the chosen algorithm fails on a surface, try another one '
        'instead of stopping the run.'),
    'gmsh.global_sizing.barycentric_refinement': (
        'Split every cell about its centre after meshing: each tetra '
        'becomes four, each triangle three. Cell count rises by that '
        'factor.'),
    'gmsh.global_sizing.cell_shape': (
        'Tetrahedral fills the volume with tetrahedra. Hexahedral asks '
        'for hexahedra and needs a surface meshed with quadrilaterals.'),
    'gmsh.global_sizing.extend_from_boundary': (
        'Let the sizes on the surfaces spread into the volume. Turn it '
        'off to size the volume from the other controls alone.'),
    'gmsh.global_sizing.field_combiner': (
        'When several size controls overlap, take the smallest size '
        '(min) or the largest (max). Min keeps every refinement.'),
    'gmsh.global_sizing.from_curvature': (
        'How many cells fit around a full circle of the surface. A '
        'circular hole of that radius gets this many edges; 0 turns the '
        'rule off.'),
    'gmsh.global_sizing.from_points': (
        'Take sizes from the sizes you set at points. Off ignores the '
        'point sizes.'),
    'gmsh.global_sizing.minimum_circle_points': (
        'The fewest nodes put on a full circle, whatever the size '
        'says. Raises the resolution of small round features.'),
    'gmsh.global_sizing.minimum_curve_points': (
        'The fewest nodes put on any curve, whatever the size says. '
        'Keeps short curves from being left with one cell.'),
    'gmsh.global_sizing.minimum_elements_per_two_pi': (
        'The fewest cells around a full circle of curvature. 0 leaves '
        'this to the curvature rule above.'),
    'gmsh.global_sizing.minimum_size': (
        'No cell edge is made shorter than this, in metres. Leave it '
        'unset to let curvature and size fields go as small as they '
        'ask.'),
    'gmsh.global_sizing.recombination_algorithm': (
        'How triangles are merged into quadrilaterals: simple pairs '
        'them greedily, blossom finds the best pairing, and the full-quad '
        'variants subdivide to get an all-quad surface.'),
    'gmsh.global_sizing.recombine': (
        'Merge surface triangles into quadrilaterals. Needed for '
        'hexahedral cells and for structured-looking surfaces.'),
    'gmsh.global_sizing.size_factor': (
        'Multiplies every size in the model. 0.5 doubles the '
        'resolution in each direction and multiplies the cell count '
        'by about eight.'),
    'gmsh.global_sizing.split_quadrangles': (
        'Split every quadrilateral into four smaller ones after '
        'meshing, which makes the mesh finer without changing its '
        'pattern.'),
    'gmsh.global_sizing.surface': (
        'The algorithm that meshes the surfaces. Frontal-Delaunay is a '
        'good default; MeshAdapt copes with strongly curved surfaces.'),
    'gmsh.global_sizing.target_size': (
        'The typical cell edge length, in metres, away from refined '
        'regions. Every other size is measured against it.'),
    'gmsh.global_sizing.threads': (
        'How many threads Gmsh uses to mesh. The Mesh setup page sets '
        'the default for the whole run.'),
    'gmsh.global_sizing.volume': (
        'The algorithm that fills the volume. Delaunay is robust; HXT '
        'is fast and uses several threads; Frontal gives smoother '
        'sizes.'),
    # ---- Gmsh volume controls ---------------------------------------
    'gmsh.volume_controls.automatic': (
        'Mesh simple volumes, those with six sides, as a structured '
        'grid when Gmsh can recognise them.'),
    'gmsh.volume_controls.transfinite_tri': (
        'Mesh three-sided surfaces with structured triangles instead of '
        'the unstructured surface algorithm.'),
    # ---- Execution ---------------------------------------------------
    'mesh.execution.decomposition_cells': (
        'The cell counts of the regions in a hierarchical or simple '
        'split, such as 2 2 1 for four parts. Empty lets FoamMesh '
        'choose from the core count.'),
    'mesh.execution.decomposition_method': (
        'How the domain is cut into parts for a parallel run. Scotch '
        'balances the parts without any further input.'),
    'mesh.execution.decomposition_order': (
        'The order of the axes a hierarchical split cuts along. Cutting '
        'the longest direction first usually gives the fewest '
        'interfaces.'),
    'mesh.execution.max_cpu_cores': (
        'The most processor cores a mesh run may use. 0 lets '
        'FoamMesh choose from the machine.'),
    'mesh.execution.mode': (
        'Auto runs in parallel when the case is big enough to gain from '
        'it. Serial and Parallel force one or the other.'),
    # ---- snappy: castellation ---------------------------------------
    'meshing.castellation.allow_free_standing_zone_faces': (
        'Allow faces of a zone that do not border a cell zone to stay '
        'in the mesh, rather than being removed.'),
    'meshing.castellation.extended_refinement_span': (
        'Whether refinement spreads out to cover the extra cells that '
        'a refinement level asks for. Default leaves OpenFOAM\'s own '
        'choice.'),
    'meshing.castellation.gap_level_increment': (
        'The extra refinement levels given to cells inside a narrow '
        'gap. Unset leaves the gap rule at OpenFOAM\'s default.'),
    'meshing.castellation.handle_snap_problems': (
        'Whether cells that would later snap badly are fixed during '
        'castellation. Default leaves OpenFOAM\'s own choice.'),
    'meshing.castellation.max_load_unbalance': (
        'How uneven the cell count may be between processors before a '
        'parallel run rebalances it. 0.1 is 10 percent.'),
    'meshing.castellation.min_refinement_cells': (
        'Refinement stops once fewer than this many cells would still '
        'be refined in a pass, which ends the loop on small leftovers.'),
    'meshing.castellation.planar_angle': (
        'Surface patches that bend less than this angle count as flat '
        'when refinement is placed. Unset uses the resolve feature '
        'angle.'),
    'meshing.castellation.use_topological_snap_detection': (
        'Use the cell topology to find where the surface will snap. '
        'Default leaves OpenFOAM\'s own choice.'),
    # ---- snappy: layers ---------------------------------------------
    'meshing.layers.additional_reporting': (
        'Print more detail about each layer attempt in the log. It '
        'does not change the mesh.'),
    'meshing.layers.detect_extrusion_island': (
        'Find groups of cells that would be grown as an island and '
        'handle them separately. Default leaves OpenFOAM\'s own '
        'choice.'),
    'meshing.layers.max_face_thickness_ratio': (
        'Layers are cut back on faces where the thickness varies by '
        'more than this ratio across the face. Lower is stricter.'),
    'meshing.layers.max_thickness_to_medial_ratio': (
        'The layer may be no thicker than this fraction of the distance '
        'to the medial axis, which stops layers from colliding in '
        'narrow gaps.'),
    'meshing.layers.mesh_shrinker': (
        'The method that moves the surface inwards to make room for '
        'layers. The medial-axis displacement shrinker is the one '
        'OpenFOAM 13 offers.'),
    'meshing.layers.min_medial_axis_angle': (
        'The angle between surface normals above which a point is '
        'treated as lying on the medial axis, where layers are cut '
        'back.'),
    'meshing.layers.n_buffer_cells_no_extrude': (
        'Cells kept without layers beside a stopped layer, so the '
        'stack tapers off instead of ending in a cliff. 0 allows a '
        'sudden end.'),
    'meshing.layers.n_layer_iter': (
        'The most iterations spent adding the layers. If the layers are '
        'incomplete, raising it can help.'),
    'meshing.layers.n_medial_axis_iter': (
        'How many iterations smooth the medial axis. Unset uses the '
        'layer smoothing count.'),
    'meshing.layers.n_relax_iter': (
        'Iterations spent relaxing the mesh around the layers, using '
        'the stricter quality limits first.'),
    'meshing.layers.n_relaxed_iter': (
        'After this many iterations the relaxed quality limits apply '
        'instead of the strict ones.'),
    'meshing.layers.n_smooth_displacement': (
        'Smoothing passes on the displacement of the surface. Unset '
        'uses the normal smoothing count.'),
    'meshing.layers.n_smooth_normals': (
        'Smoothing passes on the interior normals along which layers '
        'grow. More passes give smoother layers on curved walls.'),
    'meshing.layers.n_smooth_surface_normals': (
        'Smoothing passes on the surface normals before the layers are '
        'grown.'),
    'meshing.layers.n_smooth_thickness': (
        'Smoothing passes over the layer thickness, which evens out '
        'steps between neighbouring points.'),
    'meshing.layers.slip_feature_angle': (
        'On slip patches, edges sharper than this angle keep their '
        'layer points fixed to the feature.'),
    # ---- snappy: snap -----------------------------------------------
    'meshing.snap.explicit_feature_snap': (
        'Snap to the feature edges listed in the .eMesh files from '
        'Surface features. Needs those edges to have been extracted.'),
    'meshing.snap.implicit_feature_snap': (
        'Find sharp edges from the surface itself while snapping, '
        'without extracted edge files. Slower, and no files needed.'),
    'meshing.snap.multi_region_feature_snap': (
        'Snap to feature edges that lie between different regions of '
        'the same surface as well as on its outer edges.'),
    'meshing.snap.n_feature_snap_iter': (
        'The iterations spent pulling cells onto the feature edges. '
        'Raise it if edges are still rounded after snapping.'),
    'meshing.snap.n_relax_iter': (
        'The iterations that relax the mesh while snapping. When '
        'quality checks fail, more iterations can help.'),
    'meshing.snap.n_solve_iter': (
        'The iterations solving the mesh displacement toward the '
        'surface. Larger values follow the surface more closely.'),
    # ---- snappy: advanced flags -------------------------------------
    'meshing.snappy_advanced.debug_flags.attraction': (
        'Write the attraction fields used while snapping, for '
        'debugging in ParaView. Adds output files and time.'),
    'meshing.snappy_advanced.debug_flags.feature_seeds': (
        'Write the points where feature edges were seeded, for '
        'debugging. Adds output files.'),
    'meshing.snappy_advanced.debug_flags.intersections': (
        'Write the cell faces cut by the surface, for debugging. '
        'Adds output files.'),
    'meshing.snappy_advanced.debug_flags.layer_info': (
        'Write information about the layer addition for debugging. '
        'Adds output files.'),
    'meshing.snappy_advanced.debug_flags.mesh': (
        'Write the mesh after each stage, for debugging. Adds one '
        'mesh copy per stage on disk.'),
    'meshing.snappy_advanced.write_flags.layer_fields': (
        'Write fields showing how far each layer got, so incomplete '
        'layers can be inspected.'),
    'meshing.snappy_advanced.write_flags.layer_sets': (
        'Write cell and point sets of the layers that were added or '
        'could not be.'),
    'meshing.snappy_advanced.write_flags.scalar_levels': (
        'Write the refinement level of each cell as a field, to see '
        'where the mesh was refined.'),
    # ---- Quality -----------------------------------------------------
    'quality.thresholds.error_reduction': (
        'The fraction of a snap displacement that is kept at a cell '
        'that would break the quality limits. Lower backs off more.'),
    'quality.thresholds.max_boundary_skewness': (
        'The most a boundary face may be skewed. Cells that exceed it '
        'are moved back or removed during snapping.'),
    'quality.thresholds.max_concave': (
        'The largest angle, in degrees, that a face may be concave by. '
        '80 is the usual limit; 180 turns the check off.'),
    'quality.thresholds.max_internal_skewness': (
        'The most an inner face may be skewed, measured as the distance '
        'of the face centre from the line between cell centres.'),
    'quality.thresholds.max_non_orthogonality': (
        'The largest angle, in degrees, between a face normal and the '
        'line between the two cell centres. 65 is the usual limit; '
        '180 turns the check off.'),
    'quality.thresholds.merge_tolerance': (
        'Points closer than this fraction of the bounding box are '
        'merged when the mesh is checked.'),
    'quality.thresholds.min_area': (
        'The smallest face area allowed, in square metres. A negative '
        'value turns the check off.'),
    'quality.thresholds.min_determinant': (
        'The smallest cell determinant allowed, which measures how '
        'flat a cell is. 0.001 is the usual limit.'),
    'quality.thresholds.min_face_weight': (
        'The smallest interpolation weight between two cells a face '
        'may have. 0.05 is the usual limit.'),
    'quality.thresholds.min_tet_quality': (
        'The smallest quality of the tetrahedra each cell breaks into. '
        'A tiny positive value only rejects inverted cells.'),
    'quality.thresholds.min_twist': (
        'The smallest twist of a face, as a cosine. A twisted face '
        'cannot be split cleanly into triangles. 0.02 is the usual '
        'limit.'),
    'quality.thresholds.min_vol': (
        'The smallest volume of the pyramid made from the cell centre '
        'to each face. The very large negative default turns the check '
        'off.'),
    'quality.thresholds.min_vol_collapse_ratio': (
        'A cell is flagged when it has collapsed to less than this '
        'ratio of its original volume. A negative value turns the '
        'check off.'),
    'quality.thresholds.min_vol_ratio': (
        'The smallest allowed ratio of the volumes of two '
        'neighbouring cells. 0.01 is the usual limit.'),
    'quality.thresholds.n_smooth_scale': (
        'Passes that spread the error at a bad cell to its '
        'neighbours so a displacement is reduced there too.'),
    'quality.thresholds.relaxed.max_boundary_skewness': (
        'The boundary skewness limit used once the relaxed limits '
        'take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.max_concave': (
        'The concavity limit, in degrees, used once the relaxed limits '
        'take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.max_internal_skewness': (
        'The inner face skewness limit used once the relaxed limits '
        'take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.max_non_orthogonality': (
        'The non-orthogonality limit, in degrees, used once the '
        'relaxed limits take over. It should not be below the strict '
        'one.'),
    'quality.thresholds.relaxed.min_area': (
        'The smallest face area, in square metres, used once the '
        'relaxed limits take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_determinant': (
        'The smallest cell determinant used once the relaxed limits '
        'take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_face_weight': (
        'The smallest face interpolation weight used once the relaxed '
        'limits take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_tet_quality': (
        'The smallest tetrahedron quality used once the relaxed '
        'limits take over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_twist': (
        'The smallest face twist used once the relaxed limits take '
        'over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_vol': (
        'The smallest pyramid volume used once the relaxed limits take '
        'over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_vol_collapse_ratio': (
        'The volume collapse ratio used once the relaxed limits take '
        'over. Unset keeps the strict value.'),
    'quality.thresholds.relaxed.min_vol_ratio': (
        'The smallest neighbour volume ratio used once the relaxed '
        'limits take over. Unset keeps the strict value.'),
}

_GMSH_KEYWORDS: dict[str, str] = {
    'gmsh.global_sizing.target_size': 'Mesh.MeshSizeMax',
    'gmsh.global_sizing.minimum_size': 'Mesh.MeshSizeMin',
    'gmsh.global_sizing.size_factor': 'Mesh.MeshSizeFactor',
    'gmsh.global_sizing.from_curvature': 'Mesh.MeshSizeFromCurvature',
    'gmsh.global_sizing.from_points': 'Mesh.MeshSizeFromPoints',
    'gmsh.global_sizing.extend_from_boundary':
        'Mesh.MeshSizeExtendFromBoundary',
    'gmsh.global_sizing.minimum_circle_points': 'Mesh.MinimumCirclePoints',
    'gmsh.global_sizing.minimum_curve_points': 'Mesh.MinimumCurvePoints',
    'gmsh.global_sizing.minimum_elements_per_two_pi':
        'Mesh.MinimumElementsPerTwoPi',
    'gmsh.global_sizing.surface': 'Mesh.Algorithm',
    'gmsh.global_sizing.volume': 'Mesh.Algorithm3D',
    'gmsh.global_sizing.algorithm_fallback': 'Mesh.AlgorithmSwitchOnFailure',
    'gmsh.global_sizing.recombine': 'Mesh.RecombineAll',
    'gmsh.global_sizing.recombination_algorithm':
        'Mesh.RecombinationAlgorithm',
    'gmsh.global_sizing.split_quadrangles': 'Mesh.SubdivisionAlgorithm',
    'gmsh.global_sizing.barycentric_refinement': 'Mesh.SubdivisionAlgorithm',
    'gmsh.global_sizing.threads': 'Mesh.MaxNumThreads3D',
    'gmsh.compute.optimize': 'Mesh.Optimize',
    'gmsh.compute.netgen': 'Mesh.OptimizeNetgen',
    'gmsh.compute.optimize_threshold': 'Mesh.OptimizeThreshold',
    'gmsh.compute.quality_type': 'Mesh.QualityType',
    'gmsh.compute.high_order_optimize': 'Mesh.HighOrderOptimize',
    'gmsh.compute.element_order': 'Mesh.ElementOrder',
    'gmsh.compute.smoothing': 'Mesh.Smoothing',
    'gmsh.compute.renumber': 'Mesh.Renumber',
    'gmsh.compute.renumber_method': 'Mesh.Renumber',
    'gmsh.compute.second_order_incomplete': 'Mesh.SecondOrderIncomplete',
    'gmsh.compute.second_order_linear': 'Mesh.SecondOrderLinear',
    'gmsh.describe_geometry.auto_fix': 'Geometry.OCCAutoFix',
    'gmsh.describe_geometry.boolean_tolerance': 'Geometry.ToleranceBoolean',
    'gmsh.describe_geometry.fix_degenerated': 'Geometry.OCCFixDegenerated',
    'gmsh.describe_geometry.fix_small_edges': 'Geometry.OCCFixSmallEdges',
    'gmsh.describe_geometry.fix_small_faces': 'Geometry.OCCFixSmallFaces',
    'gmsh.describe_geometry.import_labels': 'Geometry.OCCImportLabels',
    'gmsh.describe_geometry.import_scaling': 'Geometry.OCCScaling',
    'gmsh.describe_geometry.import_tolerance': 'Geometry.Tolerance',
    'gmsh.describe_geometry.make_solids': 'Geometry.OCCMakeSolids',
    'gmsh.describe_geometry.occ_parallel': 'Geometry.OCCParallel',
    'gmsh.describe_geometry.sew_faces': 'Geometry.OCCSewFaces',
    'gmsh.describe_geometry.union_unify': 'Geometry.OCCUnionUnify',
    'gmsh.volume_controls.transfinite_tri': 'Mesh.TransfiniteTri',
}

#: The snappyHexMesh dictionary each registry area writes into.
_SNAPPY_SECTIONS: tuple[tuple[str, str], ...] = (
    ('castellation/', 'snappyHexMeshDict / castellatedMeshControls'),
    ('snap/', 'snappyHexMeshDict / snapControls'),
    ('addLayers/', 'snappyHexMeshDict / addLayersControls'),
    ('meshQuality/relaxed/',
     'snappyHexMeshDict / addLayersControls / meshQualityControls / relaxed'),
    ('meshQuality/', 'snappyHexMeshDict / meshQualityControls'),
    ('snappyAdvanced/debugFlags/', 'snappyHexMeshDict / debugFlags'),
    ('snappyAdvanced/writeFlags/', 'snappyHexMeshDict / writeFlags'),
)

#: id -> the picture the pane draws for a geometric field. Kinds are drawn by
#: ``view/main_window/help_pane.py``.
DIAGRAMS: dict[str, str] = {
    'gmsh.boundary_layers.first_height': 'layers',
    'gmsh.boundary_layers.ratio': 'layers',
    'gmsh.boundary_layers.layer_count': 'layers',
    'gmsh.boundary_layers.total_thickness': 'layers',
    'meshing.layers.feature_angle': 'angle',
    'meshing.layers.concave_angle': 'angle',
    'meshing.layers.slip_feature_angle': 'angle',
    'meshing.layers.min_medial_axis_angle': 'angle',
    'meshing.castellation.resolve_feature_angle': 'angle',
    'meshing.castellation.planar_angle': 'angle',
    'mesh.feature_angle': 'angle',
    'gmsh.describe_geometry.classification_angle': 'angle',
    'quality.thresholds.max_non_orthogonality': 'nonorth',
    'quality.thresholds.relaxed.max_non_orthogonality': 'nonorth',
    'quality.check.non_orth_threshold': 'nonorth',
    'quality.thresholds.max_boundary_skewness': 'skew',
    'quality.thresholds.max_internal_skewness': 'skew',
    'quality.check.skew_threshold': 'skew',
    'quality.thresholds.max_concave': 'angle',
    'gmsh.global_sizing.target_size': 'size',
    'gmsh.global_sizing.minimum_size': 'size',
    'gmsh.global_sizing.size_factor': 'size',
    'gmsh.global_sizing.from_curvature': 'curvature',
    'gmsh.global_sizing.minimum_circle_points': 'curvature',
    'gmsh.global_sizing.minimum_elements_per_two_pi': 'curvature',
    'meshing.base_grid.target_cell_size': 'size',
    'meshing.snap.tolerance': 'snap',
    'meshing.castellation.cells_between_levels': 'levels',
    'gmsh.describe_geometry.padding': 'box',
    'meshing.base_grid.standoff': 'box',
}

_SNAPPY_MANUAL = ('https://doc.cfd.direct/openfoam/user-guide-v13/'
                  'snappyhexmesh')
_GMSH_MANUAL = 'https://gmsh.info/doc/texinfo/gmsh.html#Mesh-options'
_GMSH_GEOMETRY_MANUAL = 'https://gmsh.info/doc/texinfo/gmsh.html#Geometry-options'
_OPENFOAM_QUALITY_MANUAL = ('https://doc.cfd.direct/openfoam/user-guide-v13/'
                            'checkmesh')



def keyword_for(field_id: str, storage_path: str = '') -> str:
    """The OpenFOAM or Gmsh keyword the field is written as, or ``''``."""
    if field_id in _GMSH_KEYWORDS:
        return _GMSH_KEYWORDS[field_id]
    path = str(storage_path or '')
    for prefix, section in _SNAPPY_SECTIONS:
        if path.startswith(prefix):
            return f'{section} / {path.rsplit("/", 1)[-1]}'
    return ''


def manual_url(field_id: str, storage_path: str = '') -> str:
    """The manual page that explains the field, or ``''``."""
    if field_id.startswith('gmsh.describe_geometry.'):
        return _GMSH_GEOMETRY_MANUAL if field_id in _GMSH_KEYWORDS \
            and _GMSH_KEYWORDS[field_id].startswith('Geometry.') else ''
    if field_id in _GMSH_KEYWORDS:
        return _GMSH_MANUAL
    path = str(storage_path or '')
    if path.startswith('meshQuality/'):
        return _OPENFOAM_QUALITY_MANUAL
    if keyword_for(field_id, path):
        return _SNAPPY_MANUAL
    return ''


def diagram_for(field_id: str) -> str:
    """The kind of diagram the pane draws for the field, or ``''``."""
    return DIAGRAMS.get(field_id, '')


def two_lines(text: str) -> str:
    """The first two sentences of ``text``: the pane's explanation."""
    sentences = re.split(r'(?<=[.!?])\s+', str(text or '').strip())
    return ' '.join(sentences[:2]).strip()


__all__ = ['FIELD_HELP', 'DIAGRAMS', 'keyword_for', 'manual_url',
           'diagram_for', 'two_lines']
