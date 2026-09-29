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
    'BoundaryPatch', 'MeshPreviewLoader', 'OPTIONAL', 'PolyMesh',
    'PolyMeshLoader',
    'PolyMeshReadError', 'REQUIRED', 'Zone', 'face_areas', 'patch_area',
    'patch_polydata', 'read_poly_mesh', 'triangulate_faces',
]


def __getattr__(name: str):
    # PEP 562. ``from ... import PolyMeshLoader`` still works for the render
    # path, while a headless import of this module never touches Qt.
    if name == 'PolyMeshLoader':
        from foammesh.openfoam.poly_mesh.poly_mesh_loader import PolyMeshLoader

        return PolyMeshLoader
    if name == 'MeshPreviewLoader':
        # Plan 35 CR3: the viewport's loader, which builds the picture in a
        # worker and never reads the volume in the window's process.
        from foammesh.openfoam.poly_mesh.mesh_preview_loader import (
            MeshPreviewLoader)

        return MeshPreviewLoader
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
