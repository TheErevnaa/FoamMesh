"""Plan 40 UXD8 (section 4.6 item 4): the physics presets.

A preset is a named set of field values for one kind of problem. Choosing it
fills only the fields still at their registry default (the user has not touched
them) and lists what it filled; a field the user changed is left alone and is
listed as kept. "A derived value never overwrites a typed one" (Plan 40 §1).

This module is pure: it decides, the caller writes the patch.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from foammesh.core.quantities import count_text

_BL = 'gmsh.boundary_layers.'
_FF = 'gmsh.describe_geometry.'
_CA = 'meshing.castellation.'
_SN = 'meshing.snap.'
_LA = 'meshing.layers.'


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    summary: str
    values: tuple  # ((field id, value), ...)


PRESETS: dict[str, Preset] = {p.key: p for p in (
    Preset(
        'internal_flow', 'Internal flow',
        'Pipes, ducts and manifolds: wall layers on, three cells between '
        'refinement levels, no far field.',
        ((_BL + 'enabled', True), (_BL + 'layer_count', 4),
         (_BL + 'ratio', 1.25), (_FF + 'enabled', False),
         (_CA + 'cells_between_levels', 3), (_LA + 'n_smooth_normals', 5))),
    Preset(
        'external_aero', 'External aerodynamics',
        'A body in open air: a box far field three diagonals out, five '
        'wall layers, four cells between levels so the wake is resolved.',
        ((_FF + 'enabled', True), (_FF + 'shape', 'box'),
         (_FF + 'padding', 3.0),
         (_BL + 'enabled', True), (_BL + 'layer_count', 5),
         (_BL + 'ratio', 1.2),
         (_CA + 'cells_between_levels', 4), (_CA + 'resolve_feature_angle', 45),
         (_LA + 'feature_angle', 70))),
    Preset(
        'heat_exchanger', 'Heat exchanger / multi-region',
        'Fluid and solid regions that share walls: snap features across '
        'regions, thin wall layers, four cells between levels.',
        ((_SN + 'multi_region_feature_snap', True),
         (_BL + 'enabled', True), (_BL + 'layer_count', 3),
         (_BL + 'ratio', 1.3), (_CA + 'cells_between_levels', 4),
         (_CA + 'resolve_feature_angle', 20))),
    Preset(
        'rotating_zone', 'Rotating zone',
        'A rotor or impeller in a cell zone: snap across the zone '
        'interface, four cells between levels, sharper feature angle.',
        ((_SN + 'multi_region_feature_snap', True),
         (_CA + 'cells_between_levels', 4),
         (_CA + 'resolve_feature_angle', 25),
         (_BL + 'enabled', True), (_BL + 'layer_count', 3),
         (_BL + 'ratio', 1.25))),
)}


@dataclass(frozen=True)
class PresetPlan:
    key: str
    #: field id -> value the preset will write (untouched fields only)
    fill: dict = field(default_factory=dict)
    #: field id -> (value the user has, value the preset would have set)
    kept: dict = field(default_factory=dict)
    #: field ids already at the preset's value; nothing to write
    already: tuple = ()

    def summary(self) -> str:
        """The list shown after applying: what was filled, what was kept."""
        title = PRESETS[self.key].title
        lines = [f'{title}: filled {count_text(len(self.fill), "field")}.']
        lines += [f'  set {fid} = {value}' for fid, value in self.fill.items()]
        if self.kept:
            lines.append(f'Kept {len(self.kept)} you changed:')
            lines += [f'  {fid} = {have} (preset: {want})'
                      for fid, (have, want) in self.kept.items()]
        return '\n'.join(lines)


def preset_titles() -> dict[str, str]:
    return {key: preset.title for key, preset in PRESETS.items()}


def same_as_default(value, default) -> bool:
    """Whether a value is the registry default (the shared "untouched" test)."""
    value = None if value == '' else value
    default = None if default == '' else default
    if default is None or value is None:
        return default is None and value is None
    if isinstance(default, bool) or isinstance(value, bool):
        return bool(value) == bool(default)
    try:
        return math.isclose(float(value), float(default),
                            rel_tol=1e-9, abs_tol=1e-15)
    except (TypeError, ValueError):
        return str(value) == str(default)


def plan_preset(key: str, current: dict, defaults: dict) -> PresetPlan:
    """Decide what ``key`` writes. ``current`` and ``defaults`` map field ids.

    A field is untouched when its current value equals its default. A field
    that already holds the preset's value is neither filled nor kept.
    """
    preset = PRESETS[key]
    fill: dict = {}
    kept: dict = {}
    already: list = []
    for field_id, want in preset.values:
        have = current.get(field_id)
        if same_as_default(have, want):
            already.append(field_id)
        elif same_as_default(have, defaults.get(field_id)):
            fill[field_id] = want
        else:
            kept[field_id] = (have, want)
    return PresetPlan(key, fill, kept, tuple(already))
