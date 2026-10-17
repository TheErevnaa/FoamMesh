"""A section plane typed as ``a x + b y + c z + d`` (Plan 42 SG12).

The Gmsh Clip tab took four numbers per plane and kept the side where
``a x + b y + c z + d >= 0``.  The Section tool places a plane by a point and
a normal that points into the kept half (`PlaneState.vtk_normal`).  The two
are the same plane::

    n = (a, b, c)               the kept side is n . x + d >= 0
    point = -d n / |n|^2        the foot of the origin on the plane
    vtk normal = n / |n|

No Qt and no VTK.  All lengths share one unit: ``d`` is in the unit of the
coordinates the equation is written in.
"""
from __future__ import annotations

import math

__all__ = ['from_equation', 'to_equation']


def from_equation(a, b, c, d):
    """``(point, unit_normal)`` of the plane ``a x + b y + c z + d = 0``,
    the normal pointing into the kept half.  ValueError for a zero normal or
    anything not finite."""
    values = [float(value) for value in (a, b, c, d)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('the plane equation must be four finite numbers')
    a, b, c, d = values
    square = a * a + b * b + c * c
    if square == 0.0:
        raise ValueError('the plane equation has a zero normal')
    length = math.sqrt(square)
    point = (-d * a / square, -d * b / square, -d * c / square)
    return point, (a / length, b / length, c / length)


def to_equation(point, normal):
    """``(a, b, c, d)`` with a unit ``(a, b, c)`` for the plane through
    *point* whose *normal* points into the kept half."""
    length = math.sqrt(sum(float(v) * float(v) for v in normal))
    if length == 0.0 or not math.isfinite(length):
        raise ValueError('the plane has a zero normal')
    n = [float(v) / length for v in normal]
    d = -sum(n[i] * float(point[i]) for i in range(3))
    return n[0], n[1], n[2], d
