"""Presentation-neutral access to ``constant/polyMesh``.

The headless reader is the default here. ``PolyMeshLoader`` is the Qt rendering
loader and is resolved lazily on attribute access: importing this module must
not pull in PySide6, because Plan 23's checker runs in CLI, API and CI
processes where Qt is never initialised.
"""
from .poly_mesh_boundary import (
    OPTIONAL, REQUIRED, BoundaryPatch, PolyMesh, PolyMeshReadError, Zone,
    face_areas, patch_area, patch_polydata, read_poly_mesh, triangulate_faces,
)

__all__ = [
    'BoundaryPatch', 'OPTIONAL', 'PolyMesh', 'PolyMeshLoader',
    'PolyMeshReadError', 'REQUIRED', 'Zone', 'face_areas', 'patch_area',
    'patch_polydata', 'read_poly_mesh', 'triangulate_faces',
]


def __getattr__(name: str):
    # PEP 562. ``from ... import PolyMeshLoader`` still works for the render
    # path, while a headless import of this module never touches Qt.
    if name == 'PolyMeshLoader':
        from foammesh.openfoam.poly_mesh.poly_mesh_loader import PolyMeshLoader

        return PolyMeshLoader
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
