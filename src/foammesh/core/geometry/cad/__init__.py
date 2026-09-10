#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD import for FoamMesh: STEP / IGES / BREP via OpenCASCADE (OCCT).

It reads CAD
boundary representation, preserves the assembly/face structure and names, applies
controlled tessellation to a tri-surface for snappyHexMesh, and recognises units.

OpenCASCADE (``pythonocc-core``) is an **optional** dependency installed via the
``[cad]`` extra. All OCCT use is lazy and guarded so the base app runs without it;
calling a CAD function without OCCT raises a clear "install CAD support" error.

Naming: this package uses ``cad`` / ``brep`` terms, never ``step`` — ``Step`` in
the schema is the meshing workflow-stage enum, unrelated to STEP files.
"""

from .availability import is_available, require, CAD_INSTALL_HINT
from .formats import CAD_SUFFIXES, detect_format
from .model import CadFace, CadBody, CadModel
from .tessellate import TessellationParams
from .cad_importer import (
    build_model_from_parts, PartData, FaceData, read_cad,
)

__all__ = [
    'is_available', 'require', 'CAD_INSTALL_HINT',
    'CAD_SUFFIXES', 'detect_format',
    'CadFace', 'CadBody', 'CadModel',
    'TessellationParams',
    'build_model_from_parts', 'PartData', 'FaceData', 'read_cad',
]
