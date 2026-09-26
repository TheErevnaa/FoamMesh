#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Mesh quality assessment (headless): checkMesh parsing, estimates, readiness."""

from .checkmesh_parser import CheckMeshResult, parse_checkmesh
from .estimator import estimate_cell_count, estimate_memory_gb
from .readiness import readiness_verdict
from .layer_report import (
    NO_LAYERS_MARKER, LayerReport, PatchLayerCoverage, parse_layer_log)
from .failed_cells import (
    parse_set_ids, read_set_file, set_entity_kind,
    discover_cell_set_files, discover_set_details, extract_selected_cells,
    discover_check_surfaces, face_owner_cells,
)
from .checkmesh_service import (
    CheckMeshProfile, CheckMeshRequest, MeshCheckRun, MeshCheckService,
    QualityReport, checkmesh_command, checkmesh_flags, checkmesh_request,
)
from .policy import (
    AcceptanceLimits, DEFAULT_POLICY, QualityPolicy, QualityPolicyError,
)
from .verdict import (apply_layer_coverage, apply_waivers,
                      layer_shortfall_line, verdict_from_report)

__all__ = [
    'CheckMeshResult', 'parse_checkmesh',
    'estimate_cell_count', 'estimate_memory_gb',
    'readiness_verdict',
    'NO_LAYERS_MARKER', 'LayerReport', 'PatchLayerCoverage', 'parse_layer_log',
    'parse_set_ids', 'read_set_file', 'set_entity_kind',
    'discover_cell_set_files', 'extract_selected_cells',
    'discover_set_details', 'discover_check_surfaces', 'face_owner_cells',
    'CheckMeshProfile', 'CheckMeshRequest', 'MeshCheckRun', 'MeshCheckService',
    'QualityReport', 'checkmesh_command', 'checkmesh_flags',
    'checkmesh_request',
    'verdict_from_report', 'apply_waivers',
    'apply_layer_coverage', 'layer_shortfall_line',
    'AcceptanceLimits', 'DEFAULT_POLICY', 'QualityPolicy',
    'QualityPolicyError',
]
