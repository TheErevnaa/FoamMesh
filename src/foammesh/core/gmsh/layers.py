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
* Thicknesses are cumulative, and their sign is a direction along the surface
  normal rather than a direction in the domain. On the outermost shell
  **negative** grows inward, into the fluid; positive inverted ten prisms on
  the elbow.
* DP-55. That sign is per shell, not per run. Each shell's normals face away
  from its own interior, so on an obstacle standing inside the domain the
  fluid is at *+n* and the same negative heights grow the layer into the
  solid. MEASURED on ``box_with_obstacle_two_files``: 2082 open cells, 2082
  wrongly oriented face pyramids, 2082 concave cells, four failed checkMesh
  checks, and 63.156 m3 of fluid where the analytic answer is 63.000 -- the
  shell around the obstacle covered twice over. ``runner_v1`` therefore grows
  each base along the direction that leaves the fluid: off the shell topology
  where it ran, which already records which shells are voids and which way
  each is wound, and off bounding-box containment on the CAD route, where
  nothing is meshed yet. Each direction group is extruded in its own call.

Growing prisms on an inlet or an outlet is wrong for every flow case, so the
scope is a real control: name the patches that get layers and the rest keep
their plain faces.
"""

from __future__ import annotations

from dataclasses import dataclass

from .layer_targets import (
    MODE_ALL_WALLS,
    MODE_SELECTED,
    normalise_mode,
)

CALCULATION_VERSION = 'gmsh.layers.v1'

#: What an empty selection used to mean. Kept because cases written before
#: Plan 33 recorded it, and the adoption pass reads it; no derivation writes
#: it any more.
SCOPE = 'all_boundary_surfaces'

#: R118. Layers grow only on the patches the plan names; every other surface
#: is rebuilt flat off the extrusion's inner rim so the core volume closes.
SCOPE_SELECTED = 'selected_patches'

#: Plan 33 section 1.1. Layers grow on every boundary the shared rule calls a
#: wall, resolved against the geometry the run imports rather than against a
#: list saved when the case was last edited.
SCOPE_WALLS = 'eligible_walls'


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
    #: R118. Patch names that get layers. Under ``selected`` this is the
    #: whole of the answer, empty included: growing on nothing is a state a
    #: user can ask for, and it is not the same as growing on everything.
    patches: tuple[str, ...] = ()
    #: Plan 33 section 1.1. ``selected`` or ``all_eligible_walls``. The
    #: second is resolved by the run against the surfaces it imported, so a
    #: case that gains a wall gains a layer with nobody editing a list.
    patch_mode: str = MODE_SELECTED
    scope: str = SCOPE_SELECTED
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
            'patchMode': self.patch_mode,
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


def _patch_mode(values, *, enabled, patches) -> str:
    """Which spelling of the selection this plan carries.

    A case saved before the choice existed carries none, and the reading it
    gets is the one the migration gives it: layers on with nothing named used
    to grow on every boundary, so it becomes an explicit request for every
    eligible wall rather than a silent request for nothing.
    """
    stored = normalise_mode(values.get('patchMode'))
    if stored:
        return stored
    if enabled and not patches:
        return MODE_ALL_WALLS
    return MODE_SELECTED


def derive_boundary_layers(values: dict, *, target_size: float | None = None
                           ) -> BoundaryLayers:
    """Turn persisted layer values into the stack the runner will extrude."""
    values = dict(values or {})
    mode = _enum_value(values.get('mode'), 'first_and_ratio')
    enabled = bool(values.get('enabled', False)) and mode != 'none'
    warnings: list[str] = []

    patches = _patch_names(values.get('patches'))
    patch_mode = _patch_mode(values, enabled=enabled, patches=patches)
    count = int(values.get('layerCount', 3) or 0)
    ratio = float(values.get('ratio', 1.2) or 1.0)
    first = float(values.get('firstHeight', 0.0) or 0.0)
    total = float(values.get('totalThickness', 0.0) or 0.0)

    if not enabled:
        return BoundaryLayers(
            enabled=False, mode='none', layer_count=0, first_height=0.0,
            ratio=1.0, total_thickness=0.0, patch_mode=patch_mode,
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
        # Plan 33 section 1.1. Naming no patch is an answer, not a blank.
        patches=patches,
        patch_mode=patch_mode,
        scope=(SCOPE_WALLS if patch_mode == MODE_ALL_WALLS
               else SCOPE_SELECTED),
        warnings=tuple(warnings))


def _median(spread) -> float:
    """The middle of one of the runner's ``{min, median, max}`` readings."""
    if not isinstance(spread, dict):
        return 0.0
    try:
        return float(spread.get('median') or 0.0)
    except (TypeError, ValueError):
        return 0.0


def achieved_coverage(statistics: dict) -> dict:
    """What one Gmsh run grew, per patch, in the shape snappy already writes.

    Plan 32 check 5. The runner has measured this since CP-08: it walks every
    prism stack from the wall face it stands on to its inner face and records
    ``statistics.layers.achieved.byPatch`` -- how many of the patch's faces
    grew a column, how deep the columns are, and how far the stack reaches
    perpendicular to the wall. The manifest keeps that verbatim. Nothing
    turned it into the one document the Quality page and the HTML report read,
    ``foammesh/quality/layer-coverage.json``, which only the snappy layers
    stage ever wrote -- so a Gmsh reader was told the run recorded no per-patch
    layer measurement while the run directory held exactly that.

    The projection is deliberately the snappy one, ``LayerReport.to_dict()``,
    because the page renders both through ``coverage_rows`` and two documents
    that describe a layer differently are how a reader ends up comparing a
    length against a percentage. So:

    * ``faces`` is the number of faces that actually grew a column, which is
      what the achieved table means by the word; on a patch where nothing grew
      it is the patch's own face count, so the sentence can say how big the
      wall that got nothing was.
    * ``layers`` is the shallowest stack on the patch. The runner records the
      distinct depths it found, not one per column, and a layer is only as
      deep as its thinnest part; a patch with more than one depth is noted.
    * ``thickness`` is the median perpendicular reach in metres, and
      ``coverage_pct`` is that reach as a share of the thickness that was
      asked for -- a share of a request, not of the wall.
    * partial area coverage is a warning of its own, because a full-thickness
      stack over half a wall reads as a complete layer in the numbers above
      and is worse for the solver than no layer at all.

    Returns ``{}`` when the run measured no layers, which is what a run that
    was never asked for any looks like.
    """
    from foammesh.core.quality.layer_report import (
        LayerReport,
        PatchLayerCoverage,
    )

    record = (statistics or {}).get('layers')
    if not isinstance(record, dict):
        return {}
    achieved = record.get('achieved')
    if not isinstance(achieved, dict):
        return {}
    by_patch = achieved.get('byPatch')
    if not isinstance(by_patch, dict) or not by_patch:
        return {}

    try:
        requested_layers = int(record.get('requestedLayers') or 0)
    except (TypeError, ValueError):
        requested_layers = 0
    try:
        requested_thickness = float(record.get('requestedTotalThickness') or 0)
    except (TypeError, ValueError):
        requested_thickness = 0.0

    rows, notes, warnings = [], [], []
    for name in sorted(by_patch):
        entry = by_patch[name] if isinstance(by_patch[name], dict) else {}
        depths = [int(value) for value in (entry.get('layersPerColumn') or ())]
        columns = int(entry.get('columns') or 0)
        total_faces = int(entry.get('faces') or 0)
        thickness = _median(entry.get('reach')) if columns else 0.0
        rows.append(PatchLayerCoverage(
            patch=str(name),
            faces=columns or total_faces,
            layers=float(min(depths)) if depths else 0.0,
            thickness=thickness,
            coverage_pct=(100.0 * thickness / requested_thickness
                          if requested_thickness else 0.0),
            requested_layers=(requested_layers
                              if entry.get('selected') and requested_layers
                              else None)))
        if len(depths) > 1:
            notes.append(
                f'{name}: the stacks are {min(depths)} to {max(depths)} '
                'layers deep; the shallowest is reported.')
        if columns and total_faces and columns < total_faces:
            warnings.append(
                f'{name}: the layer grew on {columns} of {total_faces} faces; '
                'the near-wall spacing jumps where the stack stops.')

    report = LayerReport(rows)
    document = report.to_dict()
    document['notes'] = list(report.notes) + notes
    document['warnings'] = list(report.warnings) + warnings
    return document
