"""Periodic pair derivation.

Gmsh's ``setPeriodic(dim, tags, tagsMaster, affine)`` takes a 16-value
row-major affine transform mapping the master surface onto the slave: a point
on the master, moved by the translation or turned through the angle, lands on
the slave (DP-622; this said slave onto master, which is the inverse and
would have a user enter every translation with the wrong sign). Building that
matrix here, rather than in the
runner, means a malformed pair is rejected while the user can still fix it,
and the same numbers are reused to write the OpenFOAM ``cyclic`` patches.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

CALCULATION_VERSION = 'gmsh.periodic.v1'


class PeriodicError(ValueError):
    pass


@dataclass(frozen=True)
class PeriodicPair:
    control_id: str
    name: str
    master_scope: str
    slave_scope: str
    transform: str
    #: Row-major 4x4, as ``gmsh.model.mesh.setPeriodic`` expects.
    affine: tuple[float, ...]
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_centre: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    rotation_angle_degrees: float = 0.0
    match_tolerance: float = 1e-6
    #: Plan 41 GN11 (M-33). The three rows of the linear part of an affine
    #: pair; empty for a translation or a rotation.
    linear_rows: tuple = ()

    def to_dict(self) -> dict:
        return {
            'controlId': self.control_id, 'name': self.name,
            'masterScope': self.master_scope, 'slaveScope': self.slave_scope,
            'transform': self.transform, 'affine': list(self.affine),
            'translation': list(self.translation),
            'rotationCentre': list(self.rotation_centre),
            'rotationAxis': list(self.rotation_axis),
            'rotationAngleDegrees': self.rotation_angle_degrees,
            'matchTolerance': self.match_tolerance,
            'linearRows': [list(row) for row in self.linear_rows],
        }


@dataclass(frozen=True)
class PeriodicPlan:
    pairs: tuple[PeriodicPair, ...] = ()
    warnings: tuple[str, ...] = ()
    calculation_version: str = CALCULATION_VERSION

    def to_dict(self) -> dict:
        return {
            'pairs': [item.to_dict() for item in self.pairs],
            'warnings': list(self.warnings),
            'calculation_version': self.calculation_version,
        }


def translation_matrix(vector) -> tuple[float, ...]:
    x, y, z = vector
    return (1.0, 0.0, 0.0, float(x),
            0.0, 1.0, 0.0, float(y),
            0.0, 0.0, 1.0, float(z),
            0.0, 0.0, 0.0, 1.0)


def affine_matrix(rows, translation) -> tuple[float, ...]:
    """A general affine map: the 3x3 linear *rows* and the *translation*.

    The bottom row of the 4x4 Gmsh takes is fixed at 0 0 0 1.
    """
    out: list[float] = []
    for row, shift in zip(rows, translation):
        out.extend(float(item) for item in row)
        out.append(float(shift))
    out.extend((0.0, 0.0, 0.0, 1.0))
    return tuple(out)


def apply_affine(affine, point) -> tuple[float, float, float]:
    """*point* moved by the row-major 4x4 *affine*."""
    return tuple(
        sum(float(affine[row * 4 + k]) * float(point[k]) for k in range(3))
        + float(affine[row * 4 + 3]) for row in range(3))


def _determinant(rows) -> float:
    (a, b, c), (d, e, f), (g, h, i) = rows
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


@dataclass(frozen=True)
class PeriodicArrow:
    """What the pair does to the source surface, for drawing.

    ``start`` is the source surface's centre, ``end`` the target's, ``mapped``
    where the transform carries the source centre. ``miss`` is how far
    ``mapped`` lands from ``end``: near zero for a pair pointing the right
    way, large for one pointing the wrong way.
    """
    start: tuple
    end: tuple
    mapped: tuple
    miss: float

    def to_dict(self) -> dict:
        return {'start': list(self.start), 'end': list(self.end),
                'mapped': list(self.mapped), 'miss': self.miss}


def arrow_for(affine, source_centre, target_centre=None) -> PeriodicArrow:
    """The source -> target arrow of a pair.

    With no target centre (the geometry is not loaded) the arrow ends where
    the transform sends the source centre, and ``miss`` is 0.
    """
    start = tuple(float(item) for item in source_centre)
    mapped = apply_affine(affine, start)
    end = mapped if target_centre is None else tuple(
        float(item) for item in target_centre)
    miss = math.sqrt(sum((a - b) ** 2 for a, b in zip(mapped, end)))
    return PeriodicArrow(start, end, mapped, miss)


def rotation_matrix(axis, centre, degrees: float) -> tuple[float, ...]:
    """Rotation about an arbitrary axis through ``centre`` (Rodrigues)."""
    length = math.sqrt(sum(float(item) ** 2 for item in axis))
    if length <= 0:
        raise PeriodicError('a rotational pair needs a non-zero rotation axis')
    ux, uy, uz = (float(item) / length for item in axis)
    angle = math.radians(float(degrees))
    cos, sin = math.cos(angle), math.sin(angle)
    one = 1.0 - cos
    rotation = (
        (cos + ux * ux * one, ux * uy * one - uz * sin, ux * uz * one + uy * sin),
        (uy * ux * one + uz * sin, cos + uy * uy * one, uy * uz * one - ux * sin),
        (uz * ux * one - uy * sin, uz * uy * one + ux * sin, cos + uz * uz * one),
    )
    cx, cy, cz = (float(item) for item in centre)
    # Translate to the origin, rotate, translate back.
    offsets = tuple(
        (cx, cy, cz)[index] - sum(rotation[index][k] * (cx, cy, cz)[k]
                                  for k in range(3))
        for index in range(3))
    return (
        rotation[0][0], rotation[0][1], rotation[0][2], offsets[0],
        rotation[1][0], rotation[1][1], rotation[1][2], offsets[1],
        rotation[2][0], rotation[2][1], rotation[2][2], offsets[2],
        0.0, 0.0, 0.0, 1.0,
    )


def _enum(value, default=''):
    if value is None:
        return default
    return str(getattr(value, 'value', value)).split('.')[-1].lower()


def _vector(value, default=(0.0, 0.0, 0.0)):
    if isinstance(value, dict):
        return (float(value.get('x', default[0])),
                float(value.get('y', default[1])),
                float(value.get('z', default[2])))
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(float(item) for item in value)
    return tuple(float(item) for item in default)


def derive_periodic_pairs(rows) -> PeriodicPlan:
    pairs: list[PeriodicPair] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows or ()):
        row = dict(row or {})
        if not bool(row.get('enabled', True)):
            continue
        control_id = str(row.get('control_id') or row.get('controlId') or index)
        name = str(row.get('name') or f'periodic-{control_id}')
        master = str(row.get('masterScopeToken')
                     or row.get('master_scope_token') or '').strip()
        slave = str(row.get('slaveScopeToken')
                    or row.get('slave_scope_token') or '').strip()
        if not master or not slave:
            raise PeriodicError(
                f'periodic pair {name!r} needs both a master and a slave scope')
        if master == slave:
            raise PeriodicError(
                f'periodic pair {name!r} maps a surface onto itself')
        key = tuple(sorted((master, slave)))
        if key in seen:
            raise PeriodicError(
                f'periodic pair {name!r} repeats a pairing already declared')
        seen.add(key)

        kind = _enum(row.get('transform'), 'translation')
        translation = _vector(row.get('translation'))
        centre = _vector(row.get('rotationCentre'))
        axis = _vector(row.get('rotationAxis'), (0.0, 0.0, 1.0))
        angle = float(row.get('rotationAngleDegrees', 0.0) or 0.0)
        linear = ()

        if kind == 'translation':
            if not any(abs(item) > 0 for item in translation):
                raise PeriodicError(
                    f'periodic pair {name!r} is translational but its '
                    'translation vector is zero')
            affine = translation_matrix(translation)
        elif kind == 'rotation':
            if abs(angle) < 1e-12:
                raise PeriodicError(
                    f'periodic pair {name!r} is rotational but its angle is zero')
            affine = rotation_matrix(axis, centre, angle)
        elif kind == 'affine':
            linear = tuple(
                _vector(row.get(key), default)
                for key, default in (('affineRowX', (1.0, 0.0, 0.0)),
                                     ('affineRowY', (0.0, 1.0, 0.0)),
                                     ('affineRowZ', (0.0, 0.0, 1.0))))
            values = [item for part in linear for item in part] + list(
                translation)
            if not all(math.isfinite(item) for item in values):
                raise PeriodicError(
                    f'periodic pair {name!r} has a matrix entry that is not '
                    'a finite number')
            if abs(_determinant(linear)) < 1e-12:
                raise PeriodicError(
                    f'periodic pair {name!r} has a singular matrix: it '
                    'flattens the source surface, so no surface maps onto '
                    'the target')
            identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
            if linear == identity and not any(
                    abs(item) > 0 for item in translation):
                raise PeriodicError(
                    f'periodic pair {name!r} is affine but its matrix is the '
                    'identity: it maps the source onto itself')
            affine = affine_matrix(linear, translation)
        else:
            raise PeriodicError(
                f'periodic pair {name!r} has unknown transform {kind!r}')

        tolerance = float(row.get('matchTolerance', 1e-6) or 1e-6)
        if tolerance <= 0:
            raise PeriodicError(
                f'periodic pair {name!r} needs a positive match tolerance')

        pairs.append(PeriodicPair(
            control_id=control_id, name=name, master_scope=master,
            slave_scope=slave, transform=kind, affine=affine,
            translation=translation, rotation_centre=centre,
            rotation_axis=axis, rotation_angle_degrees=angle,
            match_tolerance=tolerance,
            linear_rows=linear if kind == 'affine' else ()))

    return PeriodicPlan(pairs=tuple(pairs), warnings=tuple(warnings))
