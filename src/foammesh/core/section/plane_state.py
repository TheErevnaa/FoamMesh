#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One section plane, held one way (Plan 37 UF7, DP-1045).

The panel kept a plane as an origin and a normal, and every control
re-derived what it showed: the offset label measured from whatever the model
centre was that moment, *Flip* negated the normal and so moved nothing but the
kept half, and the origin fields edited one world coordinate at a time. On a
model a kilometre from the origin, turned off the axes, those readings were
approximations of each other rather than views of one number.

The plane here is the plan's canonical state (§4.2)::

    n . (x - p_ref) = d          the plane
    s * (n . (x - p_ref) - d) >= 0   the half a clip keeps

``n`` is a unit normal, ``p_ref`` a reference point that is chosen once (the
model centre when the section is made) and then kept, ``d`` the signed
distance in metres and ``s`` the keep side (+1 or -1). VTK wants an origin on
the plane and a normal pointing into the kept half: ``p_ref + d n`` and
``s n``. *Flip* negates ``n`` and ``d`` and leaves ``s``: the same plane,
the other half kept. Turning the normal holds a pivot on the plane still and
recomputes ``d``. A zero, or anything not finite, is refused -- a plane with
no direction is not a plane.

Everything is metres; the display unit is a conversion at the edge
(`display_unit`, `to_display`, `from_display`). No Qt and no VTK, so the
section worker (UF10) reads the same state the panel writes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from foammesh.core.quantities import unit_for

__all__ = ['PlaneState', 'local_basis', 'rotated_normal', 'display_unit',
           'to_display', 'from_display', 'domain_step', 'unit']

#: The labelled fallback step: this fraction of the model's extent along n.
DOMAIN_STEP_FRACTION = 0.01
#: A fine step (Shift) is this fraction of the step.
FINE_FRACTION = 0.1


def _vector(values, what):
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        raise ValueError(f'{what} must be three numbers') from None
    if len(vector) != 3:
        raise ValueError(f'{what} must be three numbers')
    if not all(math.isfinite(value) for value in vector):
        raise ValueError(f'{what} must be finite')
    return vector


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def unit(values, what='normal'):
    """*values* scaled to length one; ValueError for zero or nonfinite."""
    vector = _vector(values, what)
    length = math.sqrt(_dot(vector, vector))
    if not math.isfinite(length) or length == 0.0:
        raise ValueError(f'{what} must not be zero')
    return tuple(value / length for value in vector)


def _finite(value, what):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what} must be a number') from None
    if not math.isfinite(number):
        raise ValueError(f'{what} must be finite')
    return number


@dataclass(frozen=True)
class PlaneState:
    """``n . (x - p_ref) = d``, keeping ``s (n . (x - p_ref) - d) >= 0``."""
    normal: tuple
    reference: tuple
    distance: float = 0.0
    keep: int = 1

    def __post_init__(self):
        object.__setattr__(self, 'normal', unit(self.normal))
        object.__setattr__(self, 'reference',
                           _vector(self.reference, 'reference point'))
        object.__setattr__(self, 'distance',
                           _finite(self.distance, 'offset'))
        if self.keep not in (1, -1):
            raise ValueError('keep side must be +1 or -1')
        object.__setattr__(self, 'keep', int(self.keep))

    @classmethod
    def through(cls, point, normal, reference=None, keep=1):
        """The plane through *point* with *normal*, measured from *reference*
        (the point itself when none is given)."""
        point = _vector(point, 'point')
        reference = point if reference is None else _vector(
            reference, 'reference point')
        n = unit(normal)
        return cls(n, reference,
                   _dot(n, tuple(p - r for p, r in zip(point, reference))),
                   keep)

    # -- reading ----------------------------------------------------------- #

    def origin(self):
        """The foot of ``p_ref`` on the plane: ``p_ref + d n``."""
        return tuple(r + self.distance * n
                     for r, n in zip(self.reference, self.normal))

    def vtk_normal(self):
        """The normal VTK clips with: it points into the kept half."""
        return tuple(self.keep * value for value in self.normal)

    def signed(self, point):
        """``n . (x - p_ref) - d``: how far *point* is past the plane."""
        point = _vector(point, 'point')
        return _dot(self.normal,
                    tuple(p - r for p, r in zip(point, self.reference))) \
            - self.distance

    def keeps(self, point, tolerance=0.0):
        return self.keep * self.signed(point) >= -tolerance

    def project(self, point):
        """*point* moved along n onto the plane."""
        offset = self.signed(point)
        return tuple(p - offset * n for p, n in zip(
            _vector(point, 'point'), self.normal))

    # -- changing ---------------------------------------------------------- #

    def flipped(self):
        """The same plane with the other half kept: -n, -d, same s."""
        return PlaneState(tuple(-value for value in self.normal),
                          self.reference, -self.distance, self.keep)

    def with_distance(self, distance):
        return PlaneState(self.normal, self.reference, distance, self.keep)

    def moved(self, delta):
        """Carried *delta* metres along n."""
        return self.with_distance(self.distance + _finite(delta, 'step'))

    def with_normal(self, normal, pivot=None):
        """Turned to *normal* about *pivot* (a point that stays on the plane;
        by default the foot of the reference)."""
        pivot = self.origin() if pivot is None else _vector(pivot, 'pivot')
        return PlaneState.through(pivot, normal, self.reference, self.keep)

    def with_reference(self, reference):
        """The same plane measured from another reference point."""
        return PlaneState.through(self.origin(), self.normal, reference,
                                  self.keep)

    def with_keep(self, keep):
        return PlaneState(self.normal, self.reference, self.distance, keep)

    # -- persisting -------------------------------------------------------- #

    def to_dict(self):
        return {'normal': list(self.normal),
                'reference': list(self.reference),
                'distance': self.distance,
                'keep': self.keep}

    @classmethod
    def from_dict(cls, data):
        return cls(tuple(data['normal']), tuple(data['reference']),
                   data.get('distance', 0.0), data.get('keep', 1))


# -- relative rotation -------------------------------------------------------- #

def local_basis(normal):
    """``(u, v, n)``: the right-handed frame relative turns are made in.

    ``u`` is the world axis least aligned with n (the lowest index on a tie),
    with its n component removed; ``v = n x u``. So for X it is (Y, Z, X), for
    Y (X, -Z, Y), for Z (X, Y, Z), and for a view normal whatever axis the
    view is most across. ``u x v = n`` always.
    """
    n = unit(normal)
    axis = min(range(3), key=lambda index: (abs(n[index]), index))
    e = [0.0, 0.0, 0.0]
    e[axis] = 1.0
    along = _dot(e, n)
    u = unit(tuple(e[i] - along * n[i] for i in range(3)), 'basis')
    v = _cross(n, u)
    return u, v, n


def _rotate(vector, axis, degrees):
    """*vector* turned right-handedly about the unit *axis* (Rodrigues)."""
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    cross = _cross(axis, vector)
    along = _dot(axis, vector)
    return tuple(vector[i] * c + cross[i] * s + axis[i] * along * (1 - c)
                 for i in range(3))


def rotated_normal(normal, tilt=0.0, turn=0.0):
    """*normal* turned by *tilt* degrees about u, then *turn* about v.

    Both about the basis of the normal as given (`local_basis`), in that
    order, each right-handed. The result is what is persisted, not the
    angles.
    """
    u, v, n = local_basis(normal)
    tilt = _finite(tilt, 'angle')
    turn = _finite(turn, 'angle')
    return unit(_rotate(_rotate(n, u, tilt), v, turn))


# -- display ------------------------------------------------------------------ #

def display_unit(extent):
    """``(scale, suffix)`` for offsets on a model this long along n.

    Chosen by the model, not by the offset: an offset read in metres at one
    end of the slider and in millimetres near zero is one number in two
    units.
    """
    return unit_for(extent if extent else 1.0)


def to_display(metres, scale):
    return '{:.6g}'.format(float(metres) * scale)


def from_display(text, scale):
    """Metres for *text* read in the unit *scale* converts to; ValueError for
    anything that is not a finite number."""
    value = _finite(str(text).strip(), 'offset')
    return value / scale


def domain_step(extent):
    """The labelled fallback step: a hundredth of the extent along n."""
    extent = abs(float(extent))
    return extent * DOMAIN_STEP_FRACTION if extent else 0.0
