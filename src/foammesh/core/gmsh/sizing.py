"""Global sizing derivation, bounds-aware and never silently degrading.

The pipeline this replaces carried a bookkeeping helper that returned ``None``
for years without anyone noticing, so every derived size fell back to a
constant. Every derivation here records where its value came from, and any
degradation appends a warning that travels with the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

CALCULATION_VERSION = 'gmsh.sizing.v1'

#: Target element size as a fraction of the bounding-box diagonal when the
#: user has not chosen one. Measured over the catalogue: a divisor of 40 gives
#: 8k-47k cells, which meshes in seconds and passes checkMesh on all fifteen.
DEFAULT_DIAGONAL_DIVISOR = 40.0

#: Refuse to ask Gmsh for a mesh this large; it is a runaway, not a request.
MAXIMUM_ELEMENT_ESTIMATE = 50_000_000

#: Plan 30 WP12. The Gmsh field each combiner choice becomes. The runner used
#: to hard-code ``Min``, so a user asking a size field to *coarsen* a region
#: was outvoted by every other field in the list and nothing said why.
FIELD_COMBINERS = {'min': 'Min', 'max': 'Max'}


class SizingError(ValueError):
    pass


@dataclass(frozen=True)
class GlobalSizing:
    target_size: float
    minimum_size: float
    size_factor: float
    from_curvature: int
    from_points: bool
    extend_from_boundary: bool
    #: Plan 30 WP12. 'min' or 'max': which Gmsh field folds the size fields
    #: into the single background field. Always Min before this.
    field_combiner: str = 'min'
    #: Plan 31 FC-B. The curve-discretisation floors. Gmsh's own defaults
    #: (7, 3, 0, off) so an untouched case meshes exactly as before.
    minimum_circle_points: int = 7
    minimum_curve_points: int = 3
    #: 0 means "unset": Mesh.MeshSizeFromCurvature alone decides.
    minimum_elements_per_two_pi: int = 0
    #: Plan 31 FC-C. Gmsh's barycentric subdivision, which MEASURED as a
    #: refinement rather than a change of family -- 415 tetrahedra became
    #: 1660 tetrahedra on a one-metre box, behind the same 260 boundary
    #: triangles -- so it travels with the sizing rather than the cell shape.
    barycentric_refinement: bool = False
    #: Where each value came from: 'configured' or 'derived'.
    sources: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    calculation_version: str = CALCULATION_VERSION

    def to_dict(self) -> dict:
        return {
            'targetSize': self.target_size,
            'minimumSize': self.minimum_size,
            'sizeFactor': self.size_factor,
            'fromCurvature': self.from_curvature,
            'fromPoints': self.from_points,
            'extendFromBoundary': self.extend_from_boundary,
            'fieldCombiner': self.field_combiner,
            'minimumCirclePoints': self.minimum_circle_points,
            'minimumCurvePoints': self.minimum_curve_points,
            'minimumElementsPerTwoPi': self.minimum_elements_per_two_pi,
            'barycentricRefinement': self.barycentric_refinement,
            'sources': dict(self.sources),
            'warnings': list(self.warnings),
            'calculation_version': self.calculation_version,
        }


def bounding_box_diagonal(bbox) -> float | None:
    """Diagonal length of a bounding box, or ``None`` when it is unusable.

    Returning ``None`` rather than a plausible number is deliberate: callers
    must warn about the fallback instead of quietly meshing at the wrong scale.
    """
    if bbox is None:
        return None
    try:
        spans = (
            float(bbox.xmax) - float(bbox.xmin),
            float(bbox.ymax) - float(bbox.ymin),
            float(bbox.zmax) - float(bbox.zmin),
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in spans):
        return None
    diagonal = math.sqrt(sum(item * item for item in spans))
    return diagonal if diagonal > 0 else None


def derive_global_sizing(values: dict, bbox=None) -> GlobalSizing:
    """Turn persisted sizing values into the numbers the runner will set."""
    values = dict(values or {})
    warnings: list[str] = []
    sources: dict[str, str] = {}
    diagonal = bounding_box_diagonal(bbox)

    target = _positive(values.get('targetSize'))
    if target is None:
        if diagonal is None:
            raise SizingError(
                'a target element size is required: it could not be derived '
                'because the geometry has no usable bounding box')
        target = diagonal / DEFAULT_DIAGONAL_DIVISOR
        sources['targetSize'] = 'derived'
        # DP-614: the number itself, because this is where the Global
        # Sizing page tells the user what "Auto" came to.
        warnings.append(
            f'target size {target:.4g} m derived from the bounding-box '
            f'diagonal ({diagonal:.6g} m / {DEFAULT_DIAGONAL_DIVISOR:g}); '
            'type a size to override it')
    else:
        sources['targetSize'] = 'configured'

    minimum = _positive(values.get('minimumSize'))
    if minimum is None:
        minimum = target / 10.0
        sources['minimumSize'] = 'derived'
    else:
        sources['minimumSize'] = 'configured'
    if minimum > target:
        warnings.append(
            f'minimum size {minimum:.6g} exceeded the target {target:.6g}; '
            'clamped to the target')
        minimum = target
        sources['minimumSize'] = 'clamped'

    factor = _positive(values.get('sizeFactor')) or 1.0
    if not 0.01 <= factor <= 100:
        raise SizingError(f'sizeFactor {factor} is outside 0.01-100')

    curvature = int(values.get('fromCurvature', 12) or 0)
    if not 0 <= curvature <= 50:
        raise SizingError(f'fromCurvature {curvature} is outside 0-50')

    if diagonal is not None:
        estimate = _element_estimate(diagonal, target * factor)
        if estimate > MAXIMUM_ELEMENT_ESTIMATE:
            raise SizingError(
                f'a target size of {target * factor:.6g} m across a '
                f'{diagonal:.6g} m domain asks for roughly {estimate:,} '
                'elements; increase the target size or refine locally with a '
                'size field')

    combiner = str(getattr(values.get('fieldCombiner', 'min'),
                           'value', values.get('fieldCombiner', 'min'))
                   or 'min').split('.')[-1].lower()
    if combiner not in FIELD_COMBINERS:
        raise SizingError(
            f'unknown field combiner {combiner!r}; expected one of '
            + ', '.join(sorted(FIELD_COMBINERS)))

    circle_points = _bounded(values, 'minimumCirclePoints', 7, 3, 200)
    curve_points = _bounded(values, 'minimumCurvePoints', 3, 2, 200)
    # Plan 31 FC-B. This warned that a per-turn floor set with
    # ``fromCurvature`` at zero does nothing, on the reasoning that Gmsh reads
    # the floor through the curvature pass. Measured, that was wrong: on the
    # annulus a floor of 60 raised the mesh from 17,697 elements to 83,798 with
    # ``MeshSizeFromCurvature`` at zero, against 83,490 with it at twelve --
    # the floor drives the curvature adaptation on its own. The warning would
    # have told users to change a setting to make something work that was
    # already working, so it is gone rather than reworded.
    per_two_pi = _bounded(values, 'minimumElementsPerTwoPi', 0, 0, 200)

    return GlobalSizing(
        target_size=target, minimum_size=minimum, size_factor=factor,
        from_curvature=curvature,
        from_points=bool(values.get('fromPoints', True)),
        extend_from_boundary=bool(values.get('extendFromBoundary', True)),
        field_combiner=combiner,
        minimum_circle_points=circle_points,
        minimum_curve_points=curve_points,
        minimum_elements_per_two_pi=per_two_pi,
        barycentric_refinement=bool(values.get('barycentricRefinement',
                                               False)),
        sources=sources, warnings=tuple(warnings))


def _bounded(values: dict, key: str, default: int, low: int, high: int) -> int:
    """An integer within its range, or a refusal naming the range."""
    raw = values.get(key, default)
    if raw is None or raw == '':
        return default
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise SizingError(f'{key} must be a whole number, not {raw!r}') from None
    if not low <= number <= high:
        raise SizingError(f'{key} {number} is outside {low}-{high}')
    return number


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _element_estimate(diagonal: float, size: float) -> int:
    """Very rough tetrahedron count for a domain of this diagonal.

    Only ever used to reject a runaway, so an order of magnitude is enough.
    """
    if size <= 0:
        return MAXIMUM_ELEMENT_ESTIMATE + 1
    per_edge = diagonal / size
    return int(min(per_edge ** 3 * 6, float(MAXIMUM_ELEMENT_ESTIMATE) * 10))
