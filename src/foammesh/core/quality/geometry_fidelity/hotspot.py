"""Per-face deviation: *where* on a patch the mesh left the reference.

A section-level deviation answers "which patch is wrong". It cannot answer
"where", and on a patch of any size that is the question you actually have --
a single rounded edge and a whole face lifted off the surface produce the same
maximum.

The mapping back to faces was designed for from the start and then not used:
``triangulate_faces`` returns ``source_face_ids`` so every triangle knows the
polygon it came from, and ``SampleSet.source_triangles`` carries the same for
samples. ``run._triangles_of`` dropped the third return value, so the field was
computed and thrown away at the last step.

**Aggregation is by maximum, never by mean.** A face is as wrong as its worst
point; averaging would let one bad corner of a large face disappear into three
good ones, which is the same defect as grading a mesh on its mean quality.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

#: Where a run's per-face field lives, beside the report it belongs to.
HOTSPOT_DIRNAME = Path('foammesh') / 'quality' / 'fidelity'


class HotspotError(ValueError):
    """A per-face field could not be built or read."""


def per_face_deviation(triangles: np.ndarray, source_face_ids: np.ndarray,
                       point_ids: np.ndarray, point_distances: np.ndarray,
                       face_ids: np.ndarray) -> np.ndarray:
    """Worst deviation on each face of ``face_ids``.

    ``point_ids`` are the vertex indices the distances were measured at (the
    unique vertices of ``triangles``), and ``point_distances`` the distance for
    each. Faces with no measured point come back as ``nan`` -- absent, not zero,
    because zero is a claim of perfection.
    """
    triangles = np.asarray(triangles, dtype=np.int64)
    source_face_ids = np.asarray(source_face_ids, dtype=np.int64)
    face_ids = np.asarray(face_ids, dtype=np.int64)
    point_ids = np.asarray(point_ids, dtype=np.int64)
    point_distances = np.asarray(point_distances, dtype=np.float64)

    if triangles.size and len(source_face_ids) != len(triangles):
        raise HotspotError(
            'every triangle must name the face it came from')
    if len(point_ids) != len(point_distances):
        raise HotspotError('each measured point needs exactly one distance')

    result = np.full(len(face_ids), np.nan, dtype=np.float64)
    if not triangles.size or not point_ids.size:
        return result

    # Vertex index -> its distance. The vertex array is extended with one fan
    # apex per decomposed polygon, so it is sparse in the mesh's own numbering.
    span = int(max(point_ids.max(), triangles.max())) + 1
    byPoint = np.full(span, np.nan, dtype=np.float64)
    byPoint[point_ids] = point_distances

    perTriangle = np.nanmax(byPoint[triangles], axis=1)

    # Face id -> row in the output, so this stays one pass over the triangles
    # rather than a lookup per face.
    order = np.full(int(face_ids.max()) + 1, -1, dtype=np.int64)
    order[face_ids] = np.arange(len(face_ids))
    rows = order[source_face_ids]
    keep = rows >= 0
    if not keep.any():
        return result

    # Accumulate in -inf, not nan: `np.maximum` propagates nan, so seeding the
    # accumulator with "no measurement yet" would make every face nan forever.
    # -inf survives the maximum and is turned back into absence at the end.
    accumulator = np.full(len(face_ids), -np.inf, dtype=np.float64)
    np.maximum.at(accumulator, rows[keep],
                  np.nan_to_num(perTriangle[keep], nan=-np.inf))
    measured = np.isfinite(accumulator)
    result[measured] = accumulator[measured]
    return result


def hotspot_path(case_path: Path | str, task_id: str) -> Path:
    return Path(case_path) / HOTSPOT_DIRNAME / f'{task_id}-hotspot.npz'


def write(case_path: Path | str, task_id: str,
          fields: dict[str, tuple[np.ndarray, np.ndarray]]) -> Path | None:
    """Persist ``solver_name -> (face_ids, deviations)``.

    Compressed and out of the JSON report on purpose: one float per boundary
    face is millions of numbers on the meshes this matters for, and a report a
    human is meant to read should not carry them.
    """
    if not fields:
        return None
    path = hotspot_path(case_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    for name, (ids, values) in fields.items():
        payload[f'{name}::faces'] = np.asarray(ids, dtype=np.int64)
        payload[f'{name}::deviation'] = np.asarray(values, dtype=np.float64)
    np.savez_compressed(path, **payload)
    return path


def read(case_path: Path | str, task_id: str) -> dict:
    """``solver_name -> (face_ids, deviations)``, or ``{}`` when absent.

    A missing field is the normal state -- the check is a deliberate
    qualification step, not something every mesh has had run on it -- so it is
    reported as absence, never as an error.
    """
    path = hotspot_path(case_path, task_id)
    if not path.is_file():
        return {}
    try:
        with np.load(path) as archive:
            names = {key.split('::', 1)[0] for key in archive.files}
            return {
                name: (archive[f'{name}::faces'],
                       archive[f'{name}::deviation'])
                for name in sorted(names)
                if f'{name}::faces' in archive.files
                and f'{name}::deviation' in archive.files
            }
    except (OSError, ValueError, KeyError):
        return {}


def available(case_path: Path | str, task_id: str) -> bool:
    return hotspot_path(case_path, task_id).is_file()


# --------------------------------------------------------------------------- #
# Feature capture, for drawing
# --------------------------------------------------------------------------- #
#
# Small enough for JSON -- a case has tens of features, not millions of faces.
# Kept beside the report for the same reason the per-face field is: the report
# is what a human reads, and this is what the viewport draws.

def features_path(case_path: Path | str, task_id: str) -> Path:
    return Path(case_path) / HOTSPOT_DIRNAME / f'{task_id}-features.json'


def write_features(case_path: Path | str, task_id: str,
                   sections: dict) -> Path | None:
    """Persist ``solver_name -> [{feature_uuid, verdict, critical, points}]``.

    The polyline is stored with the verdict rather than looked up again at draw
    time: the manifest belongs to a *prepared revision*, and a later revision
    would move the geometry out from under a verdict measured on the old one.
    """
    import json

    if not sections:
        return None
    path = features_path(case_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sections, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')
    return path


def read_features(case_path: Path | str, task_id: str) -> dict:
    """``solver_name -> [feature records]``, or ``{}`` when absent."""
    import json

    path = features_path(case_path, task_id)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}
