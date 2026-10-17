"""The mesh-frame record: what Mesh > Scale / Translate / Rotate did to a case.

Plan 39 VI5 (F4c, F4d). The mesh transforms run ``transformPoints`` and
reload the mesh, but the geometry the mesh was made from stays where it was
imported, so every distance measured after a transform was taken against the
wrong place. The case therefore records one cumulative 4x4 matrix -- the
frame the mesh is in relative to the geometry -- in its sidecar provenance
under :data:`RECORD_KEY`, identity when the mesh was never transformed.

**The record dies with the mesh it describes.** A freshly generated or
imported mesh is in the geometry's frame again, so
``CaseMetadata.with_generated_mesh`` / ``with_external_mesh`` drop the key.

**The length unit follows from the scale.** The geometry and a generated mesh
are in metres. After a uniform scale ``s`` one model unit is ``1 / s`` metres,
which is what turns a distance in model units into a physical one: the figure
on screen is the same before and after ``Scale x0.001``.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

#: The sidecar provenance key the cumulative matrix is stored under.
RECORD_KEY = 'mesh_transform'

#: Metres per model unit for each unit name a caller may state.
_UNIT_METRES = {'m': 1.0, 'cm': 0.01, 'mm': 0.001, 'um': 1e-6}


def identity() -> np.ndarray:
    return np.eye(4, dtype=np.float64)


def request_matrix(request) -> np.ndarray:
    """The 4x4 affine matrix of a ``MeshTransformRequest``.

    Taken from the request's own point transform (the one the dialog's bounds
    preview uses), so the record and the preview cannot disagree.
    """
    origin = np.asarray(request._transform_point((0.0, 0.0, 0.0)), dtype=float)
    matrix = identity()
    matrix[:3, 3] = origin
    for axis in range(3):
        basis = [0.0, 0.0, 0.0]
        basis[axis] = 1.0
        image = np.asarray(request._transform_point(tuple(basis)), dtype=float)
        matrix[:3, axis] = image - origin
    return matrix


def parse(text) -> np.ndarray:
    """The matrix stored as ``text``; identity for anything unusable."""
    try:
        values = np.asarray(json.loads(text)['matrix'], dtype=np.float64)
        if values.size == 16 and np.all(np.isfinite(values)):
            return values.reshape(4, 4)
    except (TypeError, ValueError, KeyError, LookupError):
        pass
    return identity()


def dump(matrix) -> str:
    return json.dumps(
        {'matrix': [float(v) for v in np.asarray(matrix).ravel()]})


def compose(previous, request) -> np.ndarray:
    """``previous`` followed by ``request``."""
    return request_matrix(request) @ np.asarray(previous, dtype=np.float64)


def load(case_path) -> np.ndarray:
    """The case's cumulative mesh transform, identity when none is recorded."""
    from foammesh.core.case import CaseMetadataError, load_case_metadata
    try:
        metadata = load_case_metadata(Path(case_path))
    except (CaseMetadataError, OSError, ValueError):
        return identity()
    text = metadata.provenance.get(RECORD_KEY)
    return parse(text) if text else identity()


def is_identity(matrix, tolerance: float = 1e-12) -> bool:
    return bool(np.allclose(matrix, identity(), atol=tolerance))


def uniform_scale(matrix) -> float:
    """The scale factor of ``matrix`` (rotation and translation drop out)."""
    determinant = abs(float(np.linalg.det(np.asarray(matrix)[:3, :3])))
    return determinant ** (1.0 / 3.0) if determinant > 0.0 else 1.0


def metres_per_unit(matrix=None) -> float:
    """Metres in one unit of a mesh sitting in ``matrix``'s frame."""
    if matrix is None:
        return 1.0
    return 1.0 / uniform_scale(matrix)


def unit_metres(unit) -> float:
    """Metres per model unit for a unit name (``'mm'``) or a number."""
    if isinstance(unit, str):
        try:
            return _UNIT_METRES[unit]
        except KeyError:
            raise ValueError('unknown length unit: {0}'.format(unit)) from None
    value = float(unit)
    if not value > 0.0:
        raise ValueError('a length unit must be positive')
    return value
