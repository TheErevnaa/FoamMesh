"""Boundary-layer derivation.

Measured in WP0, and the reason this module has the shape it does:

* ``geo.extrudeBoundaryLayer`` grows real prisms off OCC-imported CAD, but only
  when the imported solid is removed first and the core volume is rebuilt from
  the layer's inner surfaces. Skip that and the tet mesh is bit-identical to
  the unlayered one with the prisms stacked on top -- 8.75% too much volume,
  no inverted cells, and every quality metric looking healthy.
* R118. The extrusion **can** be restricted to selected patches, but only if
  the surfaces left out are rebuilt. The earlier audit
  (``plans/evidence/plan22-audit/per-patch-layers.txt``) concluded otherwise
  because every closure it tried -- ``tops_only``, ``tops_plus_remaining``,
  ``tops_plus_laterals_plus_remaining``, ``free_boundary``, keeping the
  original volume -- reused the *original* un-extruded cap, which lies in the
  same plane as the extrusion's own lateral faces and double-covers it; the
  result was 8022 prisms, zero tets, 9.3% of the analytic volume. MEASURED
  live on Gmsh 4.15.2 with a 0.1x0.1x0.6 m duct (0.006 m3): every surface
  extruded gives 13891 tets + 17888 prisms with **392 prisms rooted on the
  inlet and outlet planes**; walls only, with each skipped cap deleted and
  rebuilt from the extrusion's inner rim, gives 13911 tets + 16320 prisms,
  the same 0.006 m3, and **zero prisms on the caps**. The discrete/STL path
  measures the same way (14199 tets + 16712 prisms, 0 cap prisms).
* Multi-volume assemblies get no layers at all. The layer is carved out of a
  single core volume, so a shared interface leaves neither side closed --
  ``runner_v1`` refuses outright rather than producing a shell.
* Thicknesses are cumulative and **negative** grows inward. Positive inverted
  ten prisms on the elbow.

Growing prisms on an inlet or an outlet is wrong for every flow case, so the
scope is a real control: name the patches that get layers and the rest keep
their plain faces.
"""

from __future__ import annotations

from dataclasses import dataclass

CALCULATION_VERSION = 'gmsh.layers.v1'

#: Layers grow on the whole boundary. Recorded on the derived job so the run
#: manifest states the scope that was actually applied.
SCOPE = 'all_boundary_surfaces'

#: R118. Layers grow only on the patches the plan names; every other surface
#: is rebuilt flat off the extrusion's inner rim so the core volume closes.
SCOPE_SELECTED = 'selected_patches'


class LayerError(ValueError):
    pass


@dataclass(frozen=True)
class BoundaryLayers:
    enabled: bool
    mode: str
    layer_count: int
    first_height: float
    ratio: float
    total_thickness: float
    quads: bool
    #: Cumulative depths handed to Gmsh, already negated to grow inward.
    cumulative_heights: tuple[float, ...] = ()
    #: R118. Patch names that get layers. Empty means the whole boundary,
    #: which is what shipped before and what a case with no inlet still wants.
    patches: tuple[str, ...] = ()
    scope: str = SCOPE
    warnings: tuple[str, ...] = ()
    calculation_version: str = CALCULATION_VERSION

    def to_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'mode': self.mode,
            'layerCount': self.layer_count,
            'firstHeight': self.first_height,
            'ratio': self.ratio,
            'totalThickness': self.total_thickness,
            'quads': self.quads,
            'cumulativeHeights': list(self.cumulative_heights),
            'patches': list(self.patches),
            'scope': self.scope,
            'warnings': list(self.warnings),
            'calculation_version': self.calculation_version,
        }


def _enum_value(value, default=''):
    if value is None:
        return default
    token = str(getattr(value, 'value', value))
    return token.split('.')[-1].lower()


def _patch_names(value) -> tuple[str, ...]:
    """R118. The patch selection, however the store happens to hold it.

    The GUI writes a comma-separated string because that is what the schema
    field is; the facade and the tests pass a list. Both mean the same thing,
    and order is kept so the run manifest reads the way the user picked.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = value.replace('\n', ',').split(',')
    else:
        candidates = list(value)
    names, seen = [], set()
    for candidate in candidates:
        name = str(candidate).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return tuple(names)


def derive_boundary_layers(values: dict, *, target_size: float | None = None
                           ) -> BoundaryLayers:
    """Turn persisted layer values into the stack the runner will extrude."""
    values = dict(values or {})
    mode = _enum_value(values.get('mode'), 'first_and_ratio')
    enabled = bool(values.get('enabled', False)) and mode != 'none'
    warnings: list[str] = []

    patches = _patch_names(values.get('patches'))
    count = int(values.get('layerCount', 3) or 0)
    ratio = float(values.get('ratio', 1.2) or 1.0)
    first = float(values.get('firstHeight', 0.0) or 0.0)
    total = float(values.get('totalThickness', 0.0) or 0.0)

    if not enabled:
        return BoundaryLayers(
            enabled=False, mode='none', layer_count=0, first_height=0.0,
            ratio=1.0, total_thickness=0.0,
            quads=bool(values.get('quads', True)))

    if count < 1:
        raise LayerError('a boundary layer needs at least one layer')
    if ratio < 1.0:
        raise LayerError(
            f'growth ratio {ratio} would shrink each layer; it must be >= 1')

    if mode == 'first_and_ratio':
        if first <= 0:
            raise LayerError('first layer height must be positive')
    elif mode == 'total_and_count':
        if total <= 0:
            raise LayerError('total thickness must be positive')
        # Invert the geometric series to recover the first height.
        if abs(ratio - 1.0) < 1e-12:
            first = total / count
        else:
            first = total * (ratio - 1.0) / (ratio ** count - 1.0)
    else:
        raise LayerError(f'unknown boundary-layer mode: {mode!r}')

    heights, running, step = [], 0.0, first
    for _ in range(count):
        running += step
        heights.append(running)
        step *= ratio
    total_actual = heights[-1]

    if target_size and total_actual > target_size * 2.0:
        warnings.append(
            f'the layer stack is {total_actual:.6g} m deep against a target '
            f'element size of {target_size:.6g} m; the core mesh may be '
            'crowded out')

    return BoundaryLayers(
        enabled=True, mode=mode, layer_count=count, first_height=first,
        ratio=ratio, total_thickness=total_actual,
        quads=bool(values.get('quads', True)),
        # Negative grows into the volume. Positive inverts cells.
        cumulative_heights=tuple(-value for value in heights),
        # R118. Naming no patch keeps the shipped whole-boundary behaviour.
        patches=patches,
        scope=SCOPE_SELECTED if patches else SCOPE,
        warnings=tuple(warnings))
