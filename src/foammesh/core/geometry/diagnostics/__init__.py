#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Geometry diagnostics & repair (headless, VTK).

Tells the user whether a surface is mesh-ready (open edges, non-manifold edges,
duplicate points, watertightness) with a 0-100 readiness score, and provides
in-place repairs (clean/dedup, fill holes, consistent normals).
"""

from .checks import (
    Finding, Severity,
    open_edges, surface_not_closed, non_manifold_edges, duplicate_points, dropped_facets,
    is_watertight, check_all,
)
from .report import GeometryHealth, assess
from .readiness import RULES_VERSION, ReadinessReport, ReadinessState, classify
from . import repair

__all__ = [
    'Finding', 'Severity',
    'open_edges', 'surface_not_closed', 'non_manifold_edges', 'duplicate_points', 'dropped_facets',
    'is_watertight',
    'check_all', 'GeometryHealth', 'assess', 'repair',
    'RULES_VERSION', 'ReadinessReport', 'ReadinessState', 'classify',
]
