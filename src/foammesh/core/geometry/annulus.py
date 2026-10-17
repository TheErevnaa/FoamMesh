"""Hollow cylinder (annulus) tessellation (Plan 43 LY13-D3, register A-088a).

The cylinder primitive stores ``radius`` (the outer radius) and, since LY13,
an optional ``innerRadius``. ``0`` or absent is the solid cylinder every
earlier project holds. A positive inner radius makes a closed pipe wall: an
outer wall, a bore wall facing inward, and two annular end caps. One
tessellation serves the viewport preview and the snappyHexMesh geometry, so
both engines see the same body.
"""
from __future__ import annotations

import math

SIDES = 64


class AnnulusError(ValueError):
    """The radii or axis do not describe a hollow cylinder."""


def validate(point1, point2, outer, inner):
    outer, inner = float(outer), float(inner)
    if inner < 0:
        raise AnnulusError('Inner R cannot be negative')
    if inner > 0 and inner >= outer:
        raise AnnulusError('Inner R must be smaller than Outer R')
    axis = [float(b) - float(a) for a, b in zip(point1, point2)]
    if math.sqrt(sum(c * c for c in axis)) <= 0:
        raise AnnulusError('The two axis points coincide')
    return outer, inner


def _frame(point1, point2):
    axis = [float(b) - float(a) for a, b in zip(point1, point2)]
    length = math.sqrt(sum(c * c for c in axis))
    axis = [c / length for c in axis]
    helper = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = [axis[1] * helper[2] - axis[2] * helper[1],
         axis[2] * helper[0] - axis[0] * helper[2],
         axis[0] * helper[1] - axis[1] * helper[0]]
    norm = math.sqrt(sum(c * c for c in u))
    u = [c / norm for c in u]
    v = [axis[1] * u[2] - axis[2] * u[1],
         axis[2] * u[0] - axis[0] * u[2],
         axis[0] * u[1] - axis[1] * u[0]]
    return u, v


def annulus_triangles(point1, point2, outer, inner, sides=SIDES):
    """Outward-oriented triangles of a closed hollow cylinder."""
    outer, inner = validate(point1, point2, outer, inner)
    if inner <= 0:
        raise AnnulusError('A solid cylinder has no annulus; Inner R is 0')
    u, v = _frame(point1, point2)

    def ring(base, radius, k):
        a = 2.0 * math.pi * (k % sides) / sides
        c, s = math.cos(a) * radius, math.sin(a) * radius
        return tuple(float(base[i]) + c * u[i] + s * v[i] for i in range(3))

    triangles = []
    for k in range(sides):
        o0, o1 = ring(point1, outer, k), ring(point1, outer, k + 1)
        p0, p1 = ring(point2, outer, k), ring(point2, outer, k + 1)
        i0, i1 = ring(point1, inner, k), ring(point1, inner, k + 1)
        j0, j1 = ring(point2, inner, k), ring(point2, inner, k + 1)
        triangles += [(o0, o1, p1), (o0, p1, p0)]      # outer wall, outward
        triangles += [(i0, j1, i1), (i0, j0, j1)]      # bore wall, inward
        triangles += [(o0, i1, o1), (o0, i0, i1)]      # cap at point1
        triangles += [(p0, p1, j1), (p0, j1, j0)]      # cap at point2
    return triangles


def annulus_volume(point1, point2, outer, inner):
    outer, inner = validate(point1, point2, outer, inner)
    length = math.dist([float(c) for c in point1], [float(c) for c in point2])
    return math.pi * (outer * outer - inner * inner) * length


def signed_volume(triangles):
    """Divergence-theorem volume; positive when the faces point outward."""
    total = 0.0
    for a, b, c in triangles:
        total += (a[0] * (b[1] * c[2] - b[2] * c[1])
                  - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    return total


def stl_text(name, triangles):
    out = [f'solid {name}']
    for a, b, c in triangles:
        n = ((b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
             (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
             (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        m = math.sqrt(sum(x * x for x in n)) or 1.0
        out.append('facet normal %.9g %.9g %.9g' % tuple(x / m for x in n))
        out.append(' outer loop')
        for p in (a, b, c):
            out.append('  vertex %.9g %.9g %.9g' % tuple(p))
        out.append(' endloop')
        out.append('endfacet')
    out.append(f'endsolid {name}')
    return '\n'.join(out) + '\n'
