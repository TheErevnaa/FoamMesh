"""Find the features a fidelity check should watch.

Plan 23 §7.1 and §16.6. Detection runs automatically as part of reference
preparation, so the checker can run headlessly on a first attempt; a user may
then promote, demote or add features, and their intent survives re-detection
through the carry-forward map in :mod:`.manifest`.

What is detected here:

* **creases** -- edges whose incident faces turn by at least the manifest's
  ``feature_angle_deg``. These are the edges snapping rounds away, which is the
  defect §6.3 exists to catch;
* **boundary edges** -- an edge with one incident face is a hole rim or a patch
  border, and is a feature by construction;
* **corners** -- points where three or more crease edges meet, inheriting
  criticality from the creases that formed them.

Criticality follows §16.6: creases at or above the angle are critical, smoother
seams are not, and anything inferred with less than exact confidence is
advisory until a user or a policy says otherwise.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib

import numpy as np

from .manifest import (
    DEFAULT_FEATURE_ANGLE_DEG, Feature, FeaturePolicy, default_detection, mint,
)


def _quantise(value: float, places: int = 9) -> float:
    """Round a coordinate so a signature survives floating-point noise.

    A signature has to be stable across a re-run and across a rebuild that
    moves a vertex by an ulp, or carry-forward would mint new identities for
    features that did not change.
    """
    rounded = round(float(value), places)
    return 0.0 if rounded == 0 else rounded


def edge_signature(a, b) -> str:
    """A stable name for an edge, independent of which end comes first."""
    left = tuple(_quantise(value) for value in a)
    right = tuple(_quantise(value) for value in b)
    low, high = sorted((left, right))
    return hashlib.sha256(repr((low, high)).encode('ascii')).hexdigest()[:32]


def point_signature(point) -> str:
    """A stable name for a corner."""
    quantised = tuple(_quantise(value) for value in point)
    return hashlib.sha256(repr(quantised).encode('ascii')).hexdigest()[:32]


def from_cad_edges(edges, *, owner_patch_uuids=(), owner_region_uuids=(),
                   critical: bool = True) -> list[Feature]:
    """Features from CAD topology, where the model still has topology.

    A CAD edge is a *declared* feature rather than one inferred from a
    dihedral angle, so it is the better source when available: it survives a
    tessellation that would smooth a crease below the detection threshold.

    ``edges`` is a sequence of polylines -- sequences of points -- so the OCCT
    traversal stays outside this module and the identity, signature and
    ownership rules are testable without it.
    """
    features: list[Feature] = []
    for polyline in edges:
        points = [tuple(float(value) for value in point) for point in polyline]
        if len(points) < 2:
            continue
        features.append(Feature(
            feature_uuid=mint(), origin='cad_edge',
            owner_patch_uuids=tuple(owner_patch_uuids),
            owner_region_uuids=tuple(owner_region_uuids),
            critical=bool(critical),
            geometry={'kind': 'polyline', 'reason': 'cad_topology',
                      'points': [list(point) for point in points]},
            # Named by its endpoints, so a re-import that re-parameterises the
            # curve without moving it keeps the same identity.
            signature=edge_signature(points[0], points[-1])))
    return features


def authored(entries, *, owner_patch_uuids=()) -> list[Feature]:
    """Features a user declared, and the policy they attached.

    §16.6 lets a user promote, demote or add features. They arrive here in the
    same shape as a detected one -- differing in ``origin`` and in defaulting
    to critical -- so one code path serves both and carry-forward treats them
    alike.
    """
    features: list[Feature] = []
    for entry in entries:
        points = [tuple(float(value) for value in point)
                  for point in (entry.get('points') or ())]
        if len(points) < 2:
            raise ValueError('an authored feature needs at least two points')
        features.append(Feature(
            feature_uuid=str(entry.get('feature_uuid') or mint()),
            origin='user',
            owner_patch_uuids=tuple(
                entry.get('owner_patch_uuids') or owner_patch_uuids),
            owner_region_uuids=tuple(entry.get('owner_region_uuids') or ()),
            critical=bool(entry.get('critical', True)),
            geometry={'kind': 'polyline', 'reason': 'authored',
                      'points': [list(point) for point in points]},
            policy=FeaturePolicy.from_dict(entry.get('policy')),
            signature=edge_signature(points[0], points[-1])))
    return features


def _triangle_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    a, b, c = (vertices[triangles[:, 0]], vertices[triangles[:, 1]],
               vertices[triangles[:, 2]])
    normals = np.cross(b - a, c - a)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    return np.divide(normals, lengths, out=np.zeros_like(normals),
                     where=lengths > 0)


def detect(vertices, triangles, *, owner_patch_uuids=(),
           owner_region_uuids=(), feature_angle_deg: float | None = None,
           detection=None) -> tuple[list[Feature], dict]:
    """Return candidate features for one triangulated section.

    Candidates carry a freshly minted identity; :func:`.manifest.carry_forward`
    replaces it with the previous one wherever a signature matches.
    """
    vertices = np.ascontiguousarray(vertices, dtype=np.float64)
    triangles = np.ascontiguousarray(triangles, dtype=np.int64)
    settings = dict(detection or default_detection())
    angle = float(feature_angle_deg
                  if feature_angle_deg is not None
                  else settings.get('feature_angle_deg',
                                    DEFAULT_FEATURE_ANGLE_DEG))
    settings['feature_angle_deg'] = angle
    owners = tuple(owner_patch_uuids)
    regions = tuple(owner_region_uuids)

    if triangles.size == 0:
        return [], settings

    normals = _triangle_normals(vertices, triangles)
    incident: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, tri in enumerate(triangles):
        for first, second in ((0, 1), (1, 2), (2, 0)):
            key = (int(tri[first]), int(tri[second]))
            incident[tuple(sorted(key))].append(index)

    features: list[Feature] = []
    crease_points: dict[int, int] = defaultdict(int)
    threshold = np.cos(np.radians(angle))

    # Sorted so detection is deterministic: the same surface yields the same
    # candidates in the same order, which is what makes a signature match
    # reproducible.
    for (low, high) in sorted(incident):
        faces = incident[(low, high)]
        if len(faces) == 1:
            origin, critical = 'crease', True
            reason = 'boundary'
        elif len(faces) == 2:
            cosine = float(np.dot(normals[faces[0]], normals[faces[1]]))
            if cosine >= threshold:
                continue
            origin, critical = 'crease', True
            reason = 'dihedral'
        else:
            # A non-manifold edge is a defect, not a feature; the geometry
            # diagnostics own it and reporting it twice would double-count.
            continue

        crease_points[low] += 1
        crease_points[high] += 1
        features.append(Feature(
            feature_uuid=mint(), origin=origin,
            owner_patch_uuids=owners, owner_region_uuids=regions,
            critical=critical,
            geometry={'kind': 'polyline', 'reason': reason,
                      'points': [vertices[low].tolist(), vertices[high].tolist()]},
            signature=edge_signature(vertices[low], vertices[high])))

    for point, count in sorted(crease_points.items()):
        if count < 3:
            continue
        features.append(Feature(
            feature_uuid=mint(), origin='corner',
            owner_patch_uuids=owners, owner_region_uuids=regions,
            # A corner formed by critical creases is critical: it is exactly
            # the point snapping rounds off first.
            critical=True,
            geometry={'kind': 'point', 'crease_count': int(count),
                      'point': vertices[point].tolist()},
            signature=point_signature(vertices[point])))

    return features, settings
