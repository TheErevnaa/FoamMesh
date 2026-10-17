"""O5: feature edges by dihedral angle on the prepared STLs.

Plan 41 section 4.8.1. Not part of the lifted Gmsh code: this is FoamMesh's
own extraction with the angle the card shows. The angle is the project's
feature angle (``core/facade/feature_angle.py``): degrees of turn from flat,
so an edge is a feature when its two faces' normals differ by at least it.
``includedAngle`` for surfaceFeatures is ``180 - angle`` (``included_angle``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FeatureEdges:
    #: (n, 2, 3) end points.
    segments: np.ndarray
    #: (n,) turn from flat in degrees (boundary edges report 180).
    angles: np.ndarray

    def __len__(self) -> int:
        return int(len(self.angles))

    def midpoints(self) -> np.ndarray:
        if not len(self):
            return np.zeros((0, 3))
        return self.segments.mean(axis=1)

    def lengths(self) -> np.ndarray:
        if not len(self):
            return np.zeros(0)
        return np.linalg.norm(self.segments[:, 1] - self.segments[:, 0], axis=1)


def _weld(triangles: np.ndarray, tolerance: float):
    flat = triangles.reshape(-1, 3)
    scale = max(float(np.ptp(flat, axis=0).max()), 1e-30)
    quantum = tolerance * scale
    keys = np.round(flat / quantum).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True,
                                  return_inverse=True)
    return flat[first], inverse.reshape(-1, 3)


def feature_edges(triangles, angle: float, *, include_boundary=False,
                  tolerance: float = 1e-9) -> FeatureEdges:
    """Edges whose two faces turn by at least *angle* degrees."""
    triangles = np.asarray(triangles, dtype=float).reshape(-1, 3, 3)
    if not len(triangles):
        return FeatureEdges(np.zeros((0, 2, 3)), np.zeros(0))
    points, faces = _weld(triangles, tolerance)
    normals = np.cross(points[faces[:, 1]] - points[faces[:, 0]],
                       points[faces[:, 2]] - points[faces[:, 0]])
    length = np.linalg.norm(normals, axis=1)
    normals = normals / np.where(length > 0, length, 1.0)[:, None]
    pairs = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]],
                            faces[:, [2, 0]]])
    owner = np.tile(np.arange(len(faces)), 3)
    low, high = pairs.min(axis=1), pairs.max(axis=1)
    order = np.lexsort((high, low))
    low, high, owner = low[order], high[order], owner[order]
    same = (low[1:] == low[:-1]) & (high[1:] == high[:-1])
    starts = np.nonzero(np.concatenate([[True], ~same]))[0]
    counts = np.diff(np.concatenate([starts, [len(low)]]))
    segments, angles = [], []
    for start, count in zip(starts, counts):
        if count == 2:
            n1, n2 = normals[owner[start]], normals[owner[start + 1]]
            turn = np.degrees(np.arccos(np.clip(float(n1 @ n2), -1.0, 1.0)))
            if turn >= angle:
                segments.append((points[low[start]], points[high[start]]))
                angles.append(turn)
        elif include_boundary or count > 2:
            segments.append((points[low[start]], points[high[start]]))
            angles.append(180.0)
    if not segments:
        return FeatureEdges(np.zeros((0, 2, 3)), np.zeros(0))
    return FeatureEdges(np.array(segments, dtype=float),
                        np.array(angles, dtype=float))
