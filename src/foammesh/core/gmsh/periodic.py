"""Periodic pair derivation.

Gmsh's ``setPeriodic`` takes a 16-value row-major affine transform mapping the
slave surface onto the master. Building that matrix here, rather than in the
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
            match_tolerance=tolerance))

    return PeriodicPlan(pairs=tuple(pairs), warnings=tuple(warnings))
