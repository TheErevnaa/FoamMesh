"""One derivation of mesh sizes from one choice, for both engines.

Plan 40 section 4.1.1. *Coarse / Medium / Fine* map the model's bounding
diagonal to the snappy background cell, the wall refinement levels, the
feature and curvature levels, the first-layer height, and the Gmsh target
size, minimum size and curvature points. Both engines read this module, so one
choice gives comparable meshes.

Every value carries its source (``'bounding_box'`` today; Plan 41 GN25 adds
the size field as a second source behind the same interface) and whether the
user typed over it. A derived value never overwrites a value the user typed:
the caller passes what the user typed in ``typed`` and gets it back marked.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

PRESETS = ('coarse', 'medium', 'fine')
CUSTOM = 'custom'
DEFAULT_PRESET = 'medium'

#: Plan 40 Q1. Background cell as a fraction of the bounding diagonal.
PRESET_DIVISORS = {'coarse': 40.0, 'medium': 80.0, 'fine': 160.0}

#: The divisor used where the preset cannot be read: the rule that existed
#: before the preset did (DP-614, DP-669), so nothing changes silently.
LEGACY_DIVISOR = 40.0

#: Plan 40 Q1. Walls are two levels finer than the background.
WALL_LEVELS = 2

#: Plan 40 Q1. First layer from a y+ of about 30 for air at 1 m/s.
TARGET_Y_PLUS = 30.0
AIR_VISCOSITY = 1.5e-5     # m^2/s
FREE_STREAM = 1.0          # m/s

SOURCE_BOUNDING_BOX = 'bounding_box'
#: Plan 41 GN25: the automatic size field, the second source.
SOURCE_FIELD = 'field'

#: Gmsh curvature points per full circle for each preset.
CURVATURE_POINTS = {'coarse': 12, 'medium': 16, 'fine': 24}


def curvature_points(preset) -> int:
    """Gmsh curvature points per circle an unset ``fromCurvature`` resolves to."""
    token = normalise_preset(preset)
    if token is None:
        return 12
    return CURVATURE_POINTS.get(token, CURVATURE_POINTS[DEFAULT_PRESET])


def normalise_preset(value) -> str | None:
    """``'coarse'|'medium'|'fine'|'custom'`` from an enum, a string or None."""
    value = getattr(value, 'value', value)
    token = str(value or '').strip().split('.')[-1].lower()
    return token if token in PRESETS or token == CUSTOM else None


def diagonal_divisor(preset) -> float:
    """The divisor an unset ("Auto") size resolves with.

    *Custom* means the user typed some sizes; whatever they left unset is
    resolved as Medium. An unreadable preset keeps the pre-preset rule.
    """
    token = normalise_preset(preset)
    if token is None:
        return LEGACY_DIVISOR
    return PRESET_DIVISORS.get(token, PRESET_DIVISORS[DEFAULT_PRESET])


def effective_preset(preset, typed_sizes=()) -> str:
    """What the control reads: *custom* once a derived size was typed."""
    token = normalise_preset(preset) or DEFAULT_PRESET
    if token == CUSTOM:
        return CUSTOM
    for item in typed_sizes or ():
        if item not in (None, ''):
            return CUSTOM
    return token


@dataclass(frozen=True)
class SizeValue:
    value: float
    source: str = SOURCE_BOUNDING_BOX
    typed: bool = False

    def to_dict(self) -> dict:
        return {'value': self.value, 'source': self.source,
                'typed': self.typed}


@dataclass(frozen=True)
class SizeProposal:
    preset: str
    diagonal: float
    background_cell: SizeValue
    wall_cell: SizeValue
    surface_min_level: int
    surface_max_level: int
    feature_level: int
    curvature_level: int
    first_layer_height: SizeValue
    gmsh_target_size: SizeValue
    gmsh_minimum_size: SizeValue
    gmsh_curvature_points: int
    background_cells: int
    sources: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'preset': self.preset, 'diagonal': self.diagonal,
            'backgroundCell': self.background_cell.to_dict(),
            'wallCell': self.wall_cell.to_dict(),
            'surfaceMinLevel': self.surface_min_level,
            'surfaceMaxLevel': self.surface_max_level,
            'featureLevel': self.feature_level,
            'curvatureLevel': self.curvature_level,
            'firstLayerHeight': self.first_layer_height.to_dict(),
            'gmshTargetSize': self.gmsh_target_size.to_dict(),
            'gmshMinimumSize': self.gmsh_minimum_size.to_dict(),
            'gmshCurvaturePoints': self.gmsh_curvature_points,
            'backgroundCells': self.background_cells,
            'sources': dict(self.sources),
        }


def first_layer_height(length: float, wall_cell: float) -> float:
    """First-layer thickness for y+ ~ 30 on a flat plate of *length* in air.

    Cf = 0.058 Re^-0.2 (turbulent flat plate), u_tau = U sqrt(Cf / 2),
    y = y+ nu / u_tau, and the layer is twice that so its centre sits at y.
    Never taller than half the wall cell, so a layer always fits under it.
    """
    reynolds = max(FREE_STREAM * length / AIR_VISCOSITY, 1.0e3)
    skin = 0.058 * reynolds ** -0.2
    friction = FREE_STREAM * math.sqrt(skin / 2.0)
    height = 2.0 * TARGET_Y_PLUS * AIR_VISCOSITY / friction
    return min(height, 0.5 * wall_cell)


def derive_sizes(preset, bounds=None, *, diagonal=None, typed=None,
                 smallest_feature=None, field=None) -> SizeProposal | None:
    """Propose sizes for *preset* from the model's extent.

    ``bounds`` is ``(xmin, xmax, ymin, ymax, zmin, zmax)``; ``diagonal``
    is used when only the length is known. ``typed`` maps the names
    ``background_cell``, ``gmsh_target_size``, ``gmsh_minimum_size`` and
    ``first_layer_height`` to what the user typed: those win and are marked.
    ``field`` (a ``SizeField``) makes the size field the source of the sizes
    it knows; see :func:`apply_field_source`. ``smallest_feature`` (metres, from the readiness report) lowers the wall
    cell so no feature is smaller than one wall cell. ``None`` when the extent
    is unusable, so a caller says why rather than meshing at an invented scale.
    """
    token = normalise_preset(preset) or DEFAULT_PRESET
    spans = None
    if bounds is not None:
        try:
            values = [float(item) for item in bounds]
            spans = [values[1] - values[0], values[3] - values[2],
                     values[5] - values[4]]
            diagonal = math.sqrt(sum(span * span for span in spans))
        except (TypeError, ValueError, IndexError):
            return None
    try:
        diagonal = float(diagonal)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(diagonal) or diagonal <= 0:
        return None
    typed = dict(typed or {})
    divisor = diagonal_divisor(token)

    def pick(name, derived):
        given = typed.get(name)
        if given not in (None, ''):
            return SizeValue(float(given), 'typed', True)
        return SizeValue(float(derived))

    background = pick('background_cell', diagonal / divisor)
    wall = background.value / float(2 ** WALL_LEVELS)
    if smallest_feature:
        try:
            feature = float(smallest_feature)
            if math.isfinite(feature) and 0 < feature < wall:
                wall = feature
        except (TypeError, ValueError):
            pass
    target = pick('gmsh_target_size', diagonal / divisor)
    minimum = pick('gmsh_minimum_size', target.value / 10.0)
    layer = pick('first_layer_height', first_layer_height(diagonal, wall))
    cells = 0
    if spans is not None and background.value > 0:
        cells = 1
        for span in spans:
            cells *= max(1, math.ceil(abs(span) / background.value))
    proposal = SizeProposal(
        preset=token, diagonal=diagonal, background_cell=background,
        wall_cell=SizeValue(wall), surface_min_level=WALL_LEVELS,
        surface_max_level=WALL_LEVELS, feature_level=WALL_LEVELS,
        curvature_level=WALL_LEVELS, first_layer_height=layer,
        gmsh_target_size=target, gmsh_minimum_size=minimum,
        gmsh_curvature_points=CURVATURE_POINTS.get(token, 12),
        background_cells=cells,
        sources={'all': SOURCE_BOUNDING_BOX})
    if field is not None:
        proposal = apply_field_source(proposal, field, typed=typed,
                                      spans=spans)
    return proposal


def apply_field_source(proposal: SizeProposal, field, *, typed=None,
                       spans=None) -> SizeProposal:
    """Replace the sizes the size field knows, keeping what the user typed.

    Background cell and Gmsh target = ``hbulk``; Gmsh minimum = ``hmin``; the
    wall cell is the finest per-surface p10 size; the first layer is 0.3 of
    it. Each replaced value is marked ``source='field'``.
    """
    typed = dict(typed or {})
    sources = dict(proposal.sources)

    def pick(name, current, derived):
        if typed.get(name) not in (None, ''):
            return SizeValue(float(typed[name]), 'typed', True)
        sources[name] = SOURCE_FIELD
        return SizeValue(float(derived), SOURCE_FIELD)

    background = pick('background_cell', proposal.background_cell,
                      field.hbulk)
    walls = [row['size_p10'] for row in field.per_surface().values()
             if row.get('size_p10')]
    wall_size = min(walls) if walls else background.value / 2 ** WALL_LEVELS
    sources['wall_cell'] = SOURCE_FIELD
    target = pick('gmsh_target_size', proposal.gmsh_target_size, field.hbulk)
    minimum = pick('gmsh_minimum_size', proposal.gmsh_minimum_size,
                   field.hmin)
    layer = pick('first_layer_height', proposal.first_layer_height,
                 0.3 * wall_size)
    cells = proposal.background_cells
    if spans is not None and background.value > 0:
        cells = 1
        for span in spans:
            cells *= max(1, math.ceil(abs(span) / background.value))
    sources.pop('all', None)
    return replace(
        proposal, background_cell=background,
        wall_cell=SizeValue(wall_size, SOURCE_FIELD),
        first_layer_height=layer, gmsh_target_size=target,
        gmsh_minimum_size=minimum, background_cells=cells, sources=sources)


def _length_text(metres: float) -> str:
    if metres >= 1.0:
        return f'{metres:.3g} m'
    if metres >= 1.0e-3:
        return f'{metres * 1.0e3:.3g} mm'
    return f'{metres * 1.0e6:.3g} um'


def _count_text(count: int) -> str:
    if count >= 1.0e6:
        return f'{count / 1.0e6:.3g} M'
    if count >= 1.0e3:
        return f'{count / 1.0e3:.3g} k'
    return str(count)


def readout_text(proposal: SizeProposal | None) -> str:
    """"Background cell 7.5 mm, walls 1.9 mm, ~120 k background cells"."""
    if proposal is None:
        return 'Import a geometry to see the sizes this choice gives.'
    parts = [f'Background cell {_length_text(proposal.background_cell.value)}',
             f'walls {_length_text(proposal.wall_cell.value)}']
    if proposal.background_cells:
        parts.append(f'~{_count_text(proposal.background_cells)} '
                     'background cells')
    return ' · '.join(parts)
