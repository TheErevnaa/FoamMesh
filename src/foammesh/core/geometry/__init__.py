#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""FoamMesh geometry core (headless).

Surface import (STL/OBJ), unit handling, a non-destructive transform stack, and
bounding-box utilities — all independent of Qt so they are unit-testable and
reusable by the GUI, API, CLI and agent. CAD (STEP/IGES/BREP) lives in the
``cad`` subpackage (phase 05); diagnostics/repair in ``diagnostics`` (phase 06).
"""

from .bbox import BBox
from .store import GeometryArtifactStore
from .prepared import (
    GROUP_SCHEMA_VERSION, PREPARED_SCHEMA_VERSION, PreparedGeometryError,
    PreparedGeometryResult, PreparedGeometryStore, PreparedGroup, PreparedRegion,
)
from .units import si_factor, convert_length, suggest_unit, UNIT_TO_M
from .transform import TransformOp, TransformStack

__all__ = [
    'BBox', 'GeometryArtifactStore', 'GROUP_SCHEMA_VERSION',
    'PREPARED_SCHEMA_VERSION', 'PreparedGeometryError',
    'PreparedGeometryResult', 'PreparedGeometryStore', 'PreparedGroup',
    'PreparedRegion',
    'si_factor', 'convert_length', 'suggest_unit', 'UNIT_TO_M',
    'TransformOp', 'TransformStack',
]
