"""Plan 40 UXD4: the tier of every field (Essential, Common, Expert).

The tier is a registry attribute, so a gate can count the Essential fields of
a page without opening a window. Each page groups its fields as:

* Essential: three to six fields, always shown;
* Common: folded behind a disclosure that says how many are off their default;
* Expert: hidden unless *Show expert settings* is on.

A field that names no tier is Common: nothing is hidden by accident, and a new
field lands in the middle until somebody decides it is one of the few a first
mesh needs or one only a specialist touches. "A page" here is the field's
``ui_location``: the registry's own answer to where it is edited.
"""
from __future__ import annotations

ESSENTIAL = 'essential'
COMMON = 'common'
EXPERT = 'expert'
TIERS: tuple[str, ...] = (ESSENTIAL, COMMON, EXPERT)

#: The most Essential fields one page may carry (Plan 40 section 7).
MAX_ESSENTIAL_PER_PAGE = 6

_G = 'gmsh.global_sizing.'
_D = 'gmsh.describe_geometry.'
_C = 'gmsh.compute.'
_B = 'gmsh.boundary_layers.'
_BG = 'meshing.base_grid.'
_CA = 'meshing.castellation.'
_SN = 'meshing.snap.'
_LA = 'meshing.layers.'
_Q = 'quality.thresholds.'
_QC = 'quality.check.'
_SF = 'meshing.surface_features.'
_GE = 'meshing.geometry.'

_ESSENTIAL_IDS = frozenset((
    'mesh.engine',
    'mesh.size_preset', 'mesh.target_solver',
    'mesh.execution.mode', 'mesh.execution.max_cpu_cores',
    'mesh.execution.max_memory_bytes',
    'mesh.feature_angle',
    # Preparation (Gmsh): import and the farfield.
    _D + 'import_scaling', _D + 'import_tolerance', _D + 'heal_shapes',
    _D + 'enabled', _D + 'shape', _D + 'padding',
    # Global sizing.
    _G + 'target_size', _G + 'minimum_size', _G + 'size_factor',
    _G + 'from_curvature', _G + 'minimum_elements_per_two_pi',
    _G + 'cell_shape',
    # Boundary layers (Gmsh).
    _B + 'enabled', _B + 'patch_mode', _B + 'patches', _B + 'first_height',
    _B + 'ratio', _B + 'layer_count',
    # Compute (Gmsh).
    _C + 'optimize', _C + 'element_order', _C + 'min_quality',
    'gmsh.dimensionality.mode',
    # Domain and regions.
    _GE + 'tri_surface_declaration', _GE + 'gap_detection', _GE + 'gap_width',
    'geometry_preparation.decision',
    'geometry_preparation.default_boundary_category',
    # Base grid.
    _BG + 'sizing_mode', _BG + 'target_cell_size', _BG + 'bounding_hex6',
    _BG + 'standoff',
    # Surface features.
    _SF + 'keep_open_edges', _SF + 'keep_non_manifold_edges',
    _SF + 'trim_min_length',
    # Castellation.
    _CA + 'cells_between_levels', _CA + 'resolve_feature_angle',
    _CA + 'max_local_cells',
    # Snap.
    _SN + 'tolerance', _SN + 'n_solve_iter', _SN + 'n_feature_snap_iter',
    _SN + 'implicit_feature_snap',
    # Layers (snappy).
    _LA + 'feature_angle', _LA + 'concave_angle', _LA + 'merge_faces',
    # Quality. Plan 40 UXD9 moved the strict mesher limits onto the Snap page
    # (ui_location workflow.snap), which already has four Essential snap
    # controls, so only the two limits a first snap is refused on stay
    # Essential there (six in all); boundary skewness and concavity are Common.
    _Q + 'max_non_orthogonality', _Q + 'max_internal_skewness',
    _QC + 'non_orth_threshold', _QC + 'skew_threshold',
))

_EXPERT_IDS = frozenset((
    'mesh.execution.decomposition_cells',
    'mesh.execution.decomposition_method',
    'mesh.execution.decomposition_order',
    'mesh.execution.decomposition_weight_field',
    'mesh.execution.preferred_backend',
    'mesh.execution.preserve_baffles', 'mesh.execution.preserve_face_zones',
    'mesh.execution.preserve_patches',
    'mesh.execution.preserve_refinement_history',
    'geometry_preparation.acknowledgment',
    'geometry_preparation.geometry_fingerprint',
    'geometry_preparation.rules_version',
    'workflow.step',
    # Preparation (Gmsh).
    _D + 'occ_parallel', _D + 'boolean_tolerance', _D + 'union_unify',
    _D + 'remove_duplicate_faces', _D + 'remove_duplicate_nodes',
    _D + 'import_labels', _D + 'fix_small_edges', _D + 'fix_small_faces',
    _D + 'make_solids',
    # Global sizing.
    _G + 'threads', _G + 'minimum_circle_points', _G + 'minimum_curve_points',
    _G + 'algorithm_fallback', _G + 'barycentric_refinement',
    _G + 'split_quadrangles', _G + 'recombination_algorithm',
    # Compute (Gmsh).
    _C + 'netgen', _C + 'netgen_passes', _C + 'optimize_threshold',
    _C + 'quality_type', _C + 'high_order_optimize', _C + 'renumber', _C + 'renumber_method',
    _C + 'second_order_incomplete', _C + 'second_order_linear',
    'gmsh.options.overrides',
    # Domain and regions.
    _GE + 'max_tree_depth', _GE + 'min_quality', _GE + 'scale',
    _GE + 'tolerance',
    # Base grid.
    _BG + 'scale',
    # Castellation.
    _CA + 'max_load_unbalance', _CA + 'allow_free_standing_zone_faces',
    _CA + 'use_topological_snap_detection', _CA + 'handle_snap_problems',
    _CA + 'extended_refinement_span', _CA + 'gap_level_increment',
    _CA + 'planar_angle',
    # Snap.
    _SN + 'multi_region_feature_snap', _SN + 'detect_near_surfaces_snap',
    _SN + 'n_relax_iter',
    # Layers (snappy).
    _LA + 'slip_feature_angle', _LA + 'min_medial_axis_angle',
    _LA + 'max_thickness_to_medial_ratio', _LA + 'n_medial_axis_iter',
    _LA + 'n_smooth_displacement', _LA + 'detect_extrusion_island',
    _LA + 'additional_reporting', _LA + 'mesh_shrinker',
    _LA + 'n_relaxed_iter', _LA + 'n_smooth_normals',
    _LA + 'n_smooth_surface_normals', _LA + 'n_smooth_thickness',
    _LA + 'n_relax_iter',
    # Quality.
    _Q + 'error_reduction', _Q + 'n_smooth_scale', _Q + 'merge_tolerance',
    _QC + 'all_geometry', _QC + 'all_topology', _QC + 'skip_topology',
    _QC + 'write_sets',
    # Surface features.
    _SF + 'verbose_obj', _SF + 'write_vtk', _SF + 'write_obj',
    _SF + 'face_closeness', _SF + 'feature_proximity',
    _SF + 'max_feature_proximity', _SF + 'geometric_test_only',
    _SF + 'internal_angle_tolerance', _SF + 'external_angle_tolerance',
))

#: A whole family is Expert by its prefix: the snappyHexMesh debug and write
#: flags, the relaxed mesh-quality block and the surface-features subset
#: boxes and planes are specialist controls, and a new member of the family
#: is one too.
_EXPERT_PREFIXES: tuple[str, ...] = (
    'meshing.snappy_advanced.',
    'quality.thresholds.relaxed.',
    'meshing.surface_features.subset_',
    'meshing.base_grid.grading.', 'meshing.base_grid.grading_fine.',
    'meshing.base_grid.boundary_types.',
)


def tier_of(field_id: str) -> str:
    """The tier of one field: Essential, Common or Expert."""
    field_id = str(field_id or '')
    if field_id in _ESSENTIAL_IDS:
        return ESSENTIAL
    if field_id in _EXPERT_IDS or field_id.startswith(_EXPERT_PREFIXES):
        return EXPERT
    return COMMON


def essential_ids() -> frozenset:
    """Every field that is Essential, for a gate that counts them."""
    return _ESSENTIAL_IDS
