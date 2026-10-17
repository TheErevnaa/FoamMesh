"""Ghost prisms for the layer stacks (Plan 41 GN10, M-21).

A preview, not the mesh: the surface of a patch is offset along its vertex
normals to each layer boundary of the patch's stack, so the user sees how deep
the stack reaches and whether it will meet the next one before a run.  The
normals are turned toward ``toward`` (the model's centre) because a surface
carries no inside; the runner takes the real direction from the solid.
"""
from __future__ import annotations

import numpy as np


def vertex_normals(points, triangles):
    """Unit area-weighted normal of every vertex."""
    p = np.asarray(points, float)
    t = np.asarray(triangles, int).reshape(-1, 3)
    n = np.zeros_like(p)
    face = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    for k in range(3):
        np.add.at(n, t[:, k], face)
    length = np.linalg.norm(n, axis=1)
    length[length == 0] = 1.0
    return n / length[:, None]


def offset_shells(points, triangles, depths, toward=None):
    """One offset copy of the surface per depth (metres, positive inward).

    Returns ``[(depth, points)]``; the triangles are the input's.
    """
    p = np.asarray(points, float)
    normals = vertex_normals(p, triangles)
    if toward is not None:
        inward = np.asarray(toward, float) - p
        flip = np.einsum('ij,ij->i', normals, inward) < 0
        normals = np.where(flip[:, None], -normals, normals)
    else:
        normals = -normals
    return [(float(d), p + normals * float(d)) for d in depths]


def stack_depths(stack) -> list:
    """Positive depth of every layer boundary of one derived stack."""
    return [abs(float(v)) for v in stack.get('cumulativeHeights') or ()]


def reach(stack) -> float:
    depths = stack_depths(stack)
    return depths[-1] if depths else 0.0
