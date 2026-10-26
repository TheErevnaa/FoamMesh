"""Does a periodic pair's transform map one side onto the other?

Plan 49 SR3 (DP-5834). A periodic (cyclic) pair was saved with whatever two
faces and whatever translation or rotation the form held, and nothing ever
looked: a translation of the wrong length, a rotation about the wrong axis or
two faces of different size all reached the mesher, where snappy's
``createPatch`` cyclic fails to match faces (Plan 49 SR0 probe 5: 402/398)
and Gmsh stops with "no corresponding point".

This module measures the pair on the prepared tessellation, each face read
from its own geometry file, so a pair across two files (the CHT tube's
separate STLs) is checked like a pair within one:

* the two faces' areas must agree;
* every vertex, edge midpoint and triangle centroid of the source face,
  moved by the transform, must lie on the target face; and every such point
  of the target, moved back, on the source -- both ways, so a small face
  mapped into the middle of a large one fails;
* the transform maps the *owner* (source, master) onto the *neighbour*
  (target, slave), the direction the Gmsh Periodic page has always stated.
  A transform that only maps the other way is reported as reversed.

Triangle counts are measured and reported, not compared: two files
tessellate the same face differently, and neither mesher copies the input
tessellation (Gmsh meshes the source and copies it; snappy snaps to it).

The tolerance (``plans/evidence/plan49/sr3/tolerance.json``, measured by
``plans/evidence/plan49/sr3/measure_tolerance.py``): the gap
allowed is the largest of the pair's own match tolerance, the tessellation's
linear deflection on either side, and :data:`RELATIVE_TOLERANCE` times the
pair's size (the diagonal of the two faces' bounding box).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from .interface_contact import (
    _deflection, _distances, _groups, face_triangles, group_manifest_of)

__all__ = ('AREA_TOLERANCE', 'MAX_SAMPLES', 'PeriodicMatch',
           'RELATIVE_TOLERANCE', 'periodic_match', 'transform_points')

#: The gap allowed between a mapped face and its partner, as a fraction of
#: the pair's size. MEASURED (``plans/evidence/plan49/sr3/tolerance.json``,
#: 30 correct and 90 wrong pairs: a translated channel and a 30 degree
#: rotated sector, at 1 mm, 1 m and 1 km, in one file and across two): a
#: correct pair stands off by at most 1.3e-7 of its size written as a
#: six-significant-digit ASCII STL (what most exporters write), 1.4e-7 as a
#: float32 binary STL and 7.6e-16 in double precision; a transform 1e-4 of
#: the pair's reach out leaves at least 8.7e-5. 1e-5 sits seventy times
#: above the worst correct file and nine times below the smallest error.
RELATIVE_TOLERANCE = 1e-5

#: The relative area mismatch allowed. MEASURED: correct pairs agree to
#: 1.4e-7 or better.
AREA_TOLERANCE = 1e-4

#: At most this many points of each face are tested. A dense face is
#: sampled evenly; the measurement is a check of the transform, not of every
#: facet, and an unbounded face would make saving a pair take minutes.
MAX_SAMPLES = 4000


@dataclass(frozen=True)
class PeriodicMatch:
    """What measuring a periodic pair found.

    ``verified`` is ``True`` (the transform maps the source onto the target
    within the tolerance), ``False`` (it does not; ``reason`` says how) or
    ``None`` (nothing could be measured, which is not evidence either way).
    ``measures`` holds the numbers: areas, triangle counts, both gaps, the
    gap allowed and the size it was taken from.
    """

    verified: bool | None
    reason: str
    measures: dict = field(default_factory=dict)


def _number(values: Mapping, key: str, default: float = 0.0) -> float:
    value = values.get(key, default)
    value = getattr(value, 'value', value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _vector(values: Mapping, prefix: str, default=(0.0, 0.0, 0.0)):
    return tuple(_number(values, f'{prefix}{axis}', fallback)
                 for axis, fallback in zip('XYZ', default))


def transform_kind(values: Mapping) -> str:
    """``translational``, ``rotational`` or the stored word, lower case."""
    value = values.get('transform')
    return str(getattr(value, 'value', value) or '').strip()


def _rotation(values: Mapping, inverse: bool = False):
    import numpy as np

    axis = np.asarray(_vector(values, 'rotationAxis', (0.0, 0.0, 1.0)))
    norm = float(np.linalg.norm(axis))
    if norm == 0.0:
        return None
    k = axis / norm
    angle = math.radians(_number(values, 'rotationAngleDegrees'))
    if inverse:
        angle = -angle
    cross = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]],
                      [-k[1], k[0], 0.0]])
    return (np.eye(3) + math.sin(angle) * cross
            + (1.0 - math.cos(angle)) * cross @ cross)


def transform_points(points, values: Mapping, inverse: bool = False):
    """*points* moved by the pair's transform (or its inverse).

    ``values`` holds the relation's fields: ``transform`` and the
    ``translation*``/``rotationCentre*``/``rotationAxis*``/
    ``rotationAngleDegrees`` numbers. A rotation turns about the axis through
    the centre by the angle, right-handed.
    """
    import numpy as np

    points = np.asarray(points, dtype=float)
    kind = transform_kind(values)
    if kind == 'translational':
        shift = np.asarray(_vector(values, 'translation'))
        return points - shift if inverse else points + shift
    if kind == 'rotational':
        matrix = _rotation(values, inverse)
        if matrix is None:
            raise ValueError('the rotation axis is zero')
        centre = np.asarray(_vector(values, 'rotationCentre'))
        return (points - centre) @ matrix.T + centre
    raise ValueError(f'a periodic pair needs a translation or a rotation, '
                     f'not "{kind or "none"}"')


def _samples(triangles):
    """Vertices, edge midpoints and centroids of *triangles*, evenly capped."""
    import numpy as np

    points = np.unique(np.concatenate([
        triangles.reshape(-1, 3), triangles.mean(axis=1),
        0.5 * (triangles + np.roll(triangles, 1, axis=1)).reshape(-1, 3)]),
        axis=0)
    if len(points) > MAX_SAMPLES:
        index = np.linspace(0, len(points) - 1, MAX_SAMPLES).astype(int)
        points = points[index]
    return points


def _area(triangles) -> float:
    import numpy as np

    return float(0.5 * np.linalg.norm(np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]),
        axis=1).sum())


def _gaps(source, target, values, inverse=False):
    """``(source→target gap, target→source gap)`` under the transform."""
    moved_source = transform_points(source.reshape(-1, 3), values,
                                    inverse).reshape(source.shape)
    forward = float(_distances(_samples(moved_source), target).max())
    backward = float(_distances(_samples(target), moved_source).max())
    return forward, backward


def _degenerate(values: Mapping) -> str:
    kind = transform_kind(values)
    if kind == 'translational':
        if not any(_vector(values, 'translation')):
            return 'the translation is zero'
    elif kind == 'rotational':
        if not any(_vector(values, 'rotationAxis', (0.0, 0.0, 1.0))):
            return 'the rotation axis is zero'
        if _number(values, 'rotationAngleDegrees') % 360.0 == 0.0:
            return 'the rotation angle is zero'
    else:
        return ('a periodic pair needs a translation or a rotation, not '
                f'"{kind or "none"}"')
    return ''


def periodic_match(prepared: Mapping, source: str, target: str,
                   values: Mapping, tolerance: float | None = None
                   ) -> PeriodicMatch:
    """Measure whether *values*' transform maps face *source* onto *target*.

    *source* and *target* are prepared patch uuids (the pair's master and
    slave scope tokens, the relation's owner and neighbour patch uuids).
    *values* holds the transform fields; *tolerance* overrides its
    ``matchTolerance``.
    """
    import numpy as np

    source, target = str(source or ''), str(target or '')
    if not source or not target:
        return PeriodicMatch(None, 'a side is not chosen')
    if source == target:
        return PeriodicMatch(False, 'the source and the target are the same '
                                    'face')
    degenerate = _degenerate(values)
    if degenerate:
        return PeriodicMatch(False, degenerate)
    groups = _groups(group_manifest_of(prepared))
    if source not in groups or target not in groups:
        return PeriodicMatch(None, 'a face is not in the prepared geometry')
    first, reason = face_triangles(prepared, source)
    if first is None:
        return PeriodicMatch(None, reason)
    second, reason = face_triangles(prepared, target)
    if second is None:
        return PeriodicMatch(None, reason)

    every = np.concatenate([first.reshape(-1, 3), second.reshape(-1, 3)])
    size = float(np.linalg.norm(every.max(axis=0) - every.min(axis=0)))
    own = (_number(values, 'matchTolerance', 0.0) if tolerance is None
           else abs(float(tolerance)))
    allowed = max(abs(own), RELATIVE_TOLERANCE * size,
                  _deflection(prepared, str(groups[source].get('geometry_id')
                                            or '')),
                  _deflection(prepared, str(groups[target].get('geometry_id')
                                            or '')))
    source_area, target_area = _area(first), _area(second)
    measures = {
        'source_area': source_area, 'target_area': target_area,
        'source_triangles': int(len(first)),
        'target_triangles': int(len(second)),
        'size': size, 'allowed_gap': allowed,
        'same_file': (str(groups[source].get('geometry_id') or '')
                      == str(groups[target].get('geometry_id') or '')),
    }
    try:
        forward, backward = _gaps(first, second, values)
    except ValueError as error:
        return PeriodicMatch(False, str(error), measures)
    measures.update(gap=max(forward, backward), source_to_target_gap=forward,
                    target_to_source_gap=backward)
    larger = max(source_area, target_area, 1e-300)
    mismatch = abs(source_area - target_area) / larger
    measures['area_mismatch'] = mismatch
    if mismatch > AREA_TOLERANCE:
        return PeriodicMatch(False, (
            f'the faces differ in area: {source_area:.6g} and '
            f'{target_area:.6g} m², {mismatch:.3g} apart'), measures)
    gap = max(forward, backward)
    if gap <= allowed:
        return PeriodicMatch(True, (
            f'the transform maps the source onto the target within '
            f'{gap:.3g} m ({allowed:.3g} m allowed)'), measures)
    reverse = max(_gaps(first, second, values, inverse=True))
    measures['reversed_gap'] = reverse
    if reverse <= allowed:
        return PeriodicMatch(False, (
            f'the transform maps the target onto the source, not the source '
            f'onto the target (gap {gap:.3g} m, reversed {reverse:.3g} m); '
            'reverse the transform or swap the two sides'), measures)
    return PeriodicMatch(False, (
        f'the transform leaves the faces up to {gap:.3g} m apart, more than '
        f'the {allowed:.3g} m allowed'), measures)
