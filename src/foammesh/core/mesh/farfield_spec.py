"""The one authored farfield, shared by every engine adapter.

Plan 37 UF13, section 4.4 ("Engine parity and switching"). A farfield is a
closed primitive -- a box, a sphere or a cylinder -- around the model, with
the bodies cut out of it, leaving the external fluid. It is authored once and
each engine builds it its own way:

* Gmsh (UF13) cuts the primitive against the imported solids with the OCC
  kernel (``src/resources/gmsh/runner_v1.py``, ``build_farfield``);
* snappyHexMesh (UF14) writes it as a closed searchable surface carrying the
  "Outer boundary (farfield)" role, inside a real background block.

**Where it is stored.** ``gmsh/farfield`` in the configuration document. It
was the farfield store before this plan (the padded box), so extending it
means a project saved before UF13 loads unchanged: every new leaf is filled
from its schema default, and those defaults *are* today's padded box. The
path keeps its ``gmsh`` prefix for that reason only -- the specification is
engine neutral, and an engine switch never reads, rewrites or clears it.
:func:`read` is the only reader; a second, engine-specific copy of these
numbers is exactly what the section forbids.

**What is stored.** ``enabled`` (a farfield is in use), ``shape``,
``centreMode`` (``auto`` = the model's bounding-box centre, or ``explicit``),
``centre``, ``padding`` (box, in bounding-box diagonals), ``radius`` (sphere
and cylinder), ``length`` and ``axis`` (cylinder), and ``sealedCavities`` (a
Gmsh cut decision). Lengths are metres, converted once where a user types
them; nothing here scales a length.

The geometry -- validation, containment, which face is which -- lives in
``src/resources/gmsh/farfield_primitives.py`` so the runner, which executes
inside WSL, asks the same code as the host. This module is its host face plus
the parts only the host needs: the cache fingerprint and the engine-switch
contract.

No Qt, no facade.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Where the specification lives in the configuration document.
STORE = 'gmsh/farfield'

#: The role a primitive carries when it is the farfield. snappy (UF14) shows
#: it as :data:`ROLE_TITLE`; the fingerprint carries :data:`ROLE`.
ROLE = 'outer_boundary'
ROLE_TITLE = 'Outer boundary (farfield)'

GMSH = 'gmsh'
SNAPPY = 'snappy'

CAD = 'cad'
TESSELLATED = 'tessellated'

#: Engines whose adapter builds the farfield. Gmsh's is the runner. snappy's
#: arrives with UF14, which calls :func:`register_engine` when its adapter is
#: importable; until then a switch to snappy keeps the spec and says why it
#: is not built.
_ENGINES: dict[str, str] = {
    GMSH: 'the Gmsh runner cuts it with the OCC kernel',
    # Plan 37 UF14. Registered here rather than on import of the adapter, so
    # the answer to "does snappy build this?" never depends on which module a
    # process happened to load first (`core/mesh/snappy_farfield.py`).
    SNAPPY: 'snappyHexMesh bounds the mesh with it as a searchable surface',
}


def primitives():
    """The shared geometry rule, loaded once per process from the resources."""
    module = sys.modules.get('foammesh._farfield_primitives')
    if module is not None:
        return module
    from resources import resource

    location = Path(resource.file('gmsh/farfield_primitives.py')).resolve()
    spec = importlib.util.spec_from_file_location(
        'foammesh._farfield_primitives', location)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, as the layer-target loader explains.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(value)


def _enum_text(value, default: str) -> str:
    value = getattr(value, 'value', value)
    text = str(value if value not in (None, '') else default).strip().lower()
    return text


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _triple(value, default) -> tuple[float, float, float]:
    if isinstance(value, dict):
        value = (value.get('x'), value.get('y'), value.get('z'))
    try:
        items = list(value)
    except TypeError:
        return tuple(float(item) for item in default)
    if len(items) != 3:
        return tuple(float(item) for item in default)
    return tuple(_float(item, fallback) for item, fallback in zip(items, default))


@dataclass(frozen=True)
class FarfieldSpec:
    """One authored farfield, as stored. Not validated on construction.

    A spec that does not build (a zero radius, a zero axis) is still the
    user's spec: it is kept, shown and refused with the reason, never
    corrected or dropped.
    """

    enabled: bool = False
    shape: str = 'box'
    centre_mode: str = 'auto'
    centre: tuple = (0.0, 0.0, 0.0)
    padding: float = 2.0
    radius: float = 1.0
    length: float = 2.0
    axis: tuple = (1.0, 0.0, 0.0)
    sealed_cavities: str = 'discard'
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    # -- the store ---------------------------------------------------------

    @classmethod
    def from_values(cls, values) -> 'FarfieldSpec':
        """From the ``gmsh/farfield`` section; absent keys take the defaults."""
        values = dict(values or {})
        base = primitives()
        return cls(
            enabled=_truthy(values.get('enabled', False)),
            shape=_enum_text(values.get('shape'), base.BOX),
            centre_mode=_enum_text(values.get('centreMode'), base.CENTRE_AUTO),
            centre=_triple(values.get('centre'), (0.0, 0.0, 0.0)),
            padding=_float(values.get('padding'), base.DEFAULT_PADDING),
            radius=_float(values.get('radius'), base.DEFAULT_RADIUS),
            length=_float(values.get('length'), base.DEFAULT_LENGTH),
            axis=_triple(values.get('axis'), base.DEFAULT_AXIS),
            sealed_cavities=_enum_text(values.get('sealedCavities'), 'discard'),
        )

    def to_values(self) -> dict:
        """The schema's own keys, so writing this back changes nothing."""
        return {
            'enabled': self.enabled, 'shape': self.shape,
            'centreMode': self.centre_mode, 'centre': list(self.centre),
            'padding': self.padding, 'radius': self.radius,
            'length': self.length, 'axis': list(self.axis),
            'sealedCavities': self.sealed_cavities,
        }

    # -- geometry ----------------------------------------------------------

    def problems(self) -> list[str]:
        """Why this spec cannot be built on any geometry; empty when it can."""
        try:
            primitives().normalise(self.to_values())
        except primitives().FarfieldError as error:
            return [str(error)]
        return []

    def canonical(self) -> dict:
        """The validated canonical form; raises ``FarfieldError``."""
        return primitives().normalise(self.to_values())

    def resolve(self, bounds) -> dict:
        """The primitive around geometry spanning ``bounds`` (Gmsh order:
        ``xmin, ymin, zmin, xmax, ymax, zmax``); raises ``FarfieldError`` for
        an invalid spec or one that does not contain the model with
        clearance."""
        return primitives().resolve(self.to_values(), bounds)

    def is_legacy_box(self) -> bool:
        """Whether this is the padded box every pre-UF13 project holds."""
        return self.shape == 'box' and self.centre_mode == 'auto'

    def job_dict(self) -> dict:
        """What a Gmsh job carries under ``intent.farfield``.

        The pre-UF13 padded box writes exactly the three keys it always wrote,
        so the job digest of every existing project is unchanged and nothing
        re-meshes because the store grew. Any other shape or an explicit
        centre adds the keys that shape reads.
        """
        payload = {'enabled': self.enabled, 'padding': self.padding,
                   'sealedCavities': self.sealed_cavities}
        if self.is_legacy_box():
            return payload
        payload['shape'] = self.shape
        payload['centreMode'] = self.centre_mode
        if self.centre_mode == 'explicit':
            payload['centre'] = list(self.centre)
        if self.shape in ('sphere', 'cylinder'):
            payload['radius'] = self.radius
        if self.shape == 'cylinder':
            payload['length'] = self.length
            payload['axis'] = list(self.axis)
        return payload

    # -- caching -----------------------------------------------------------

    def fingerprint(self, geometry_revision: str = '') -> str:
        """The cache key of the primitive this spec builds.

        Covers the role, the shape, the dimensions the shape reads, its
        transform (centre mode, explicit centre, axis) and the geometry
        revision -- an auto centre and a padding both follow the model, so a
        new geometry is a new primitive even when no number here changed.
        Dimensions a shape does not read are left out, so a stale cylinder
        length does not invalidate a sphere. A disabled spec has one
        fingerprint whatever its dimensions: editing a farfield that is not
        in use changes no mesh.
        """
        payload: dict = {'role': ROLE, 'enabled': self.enabled,
                         'geometryRevision': str(geometry_revision or '')}
        if self.enabled:
            try:
                canonical = self.canonical()
            except primitives().FarfieldError:
                canonical = dict(self.to_values(), invalid=True)
                canonical.pop('enabled', None)
                canonical.pop('sealedCavities', None)
            if canonical.get('centreMode') == 'auto':
                canonical.pop('centre', None)
            payload['primitive'] = _canonical_numbers(canonical)
        text = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    # -- snappy --------------------------------------------------------------

    def geometry_primitive(self, bounds) -> dict:
        """The closed searchable surface snappy writes for this farfield.

        For UF14: ``shape`` is the Geometry page's own shape value (``hex``,
        ``sphere``, ``cylinder``) and ``point1``/``point2``/``radius`` carry
        the meaning ``case_builder`` already writes them with --
        ``searchableBox`` min/max, ``searchableSphere`` centre/radius,
        ``searchableCylinder`` end points/radius. The cylinder ends are
        ``base`` and ``base + axis * length``, so ``point1`` is the inlet cap.
        """
        primitive = self.resolve(bounds)
        shape = primitive['shape']
        if shape == 'box':
            low = list(primitive['origin'])
            high = [low[index] + primitive['span'][index] for index in range(3)]
            return {'shape': 'hex', 'point1': low, 'point2': high,
                    'role': ROLE}
        if shape == 'sphere':
            return {'shape': 'sphere', 'point1': list(primitive['centre']),
                    'point2': list(primitive['centre']),
                    'radius': primitive['radius'], 'role': ROLE}
        base = list(primitive['base'])
        top = [base[index] + primitive['axis'][index] * primitive['length']
               for index in range(3)]
        return {'shape': 'cylinder', 'point1': base, 'point2': top,
                'radius': primitive['radius'], 'role': ROLE}


def _canonical_numbers(value):
    if isinstance(value, float):
        return float(f'{value:.12g}')
    if isinstance(value, (list, tuple)):
        return [_canonical_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_numbers(item) for key, item in value.items()}
    return value


def read(db) -> FarfieldSpec:
    """The project's farfield specification; the only reader of the store."""
    if db is None:
        return FarfieldSpec()
    values: dict = {}
    for key in ('enabled', 'shape', 'centreMode', 'padding', 'radius',
                'length', 'sealedCavities'):
        try:
            values[key] = db.getValue(f'{STORE}/{key}')
        except Exception:  # noqa: BLE001 - an older document or a double
            continue
    for key in ('centre', 'axis'):
        try:
            values[key] = db.getVector(f'{STORE}/{key}')
        except Exception:  # noqa: BLE001
            continue
    return FarfieldSpec.from_values(values)


# --------------------------------------------------------------------------- #
# Engine switching
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Support:
    """Whether one engine builds this spec on this geometry, and why not."""

    engine: str
    supported: bool
    reason: str = ''


def register_engine(engine: str, how: str) -> None:
    """Declare that ``engine`` has a farfield adapter (UF14 calls this)."""
    _ENGINES[str(engine)] = str(how)


def registered_engines() -> tuple[str, ...]:
    return tuple(sorted(_ENGINES))


def support(spec: FarfieldSpec, engine: str, geometry_kind: str = CAD) -> Support:
    """Whether ``engine`` can build ``spec`` from geometry of ``geometry_kind``.

    Decision D5: the Gmsh farfield stays CAD only -- the cut needs a solid,
    and a tessellated import has none. The answer never changes the spec.
    """
    engine = str(engine or '').strip().lower()
    if not spec.enabled:
        return Support(engine, True, '')
    problems = spec.problems()
    if problems:
        return Support(engine, False, problems[0])
    if engine not in _ENGINES:
        return Support(
            engine, False,
            f'the {engine or "selected"} engine has no farfield adapter yet; '
            f'the {spec.shape} farfield is kept as authored and applies again '
            'on an engine that builds it')
    if engine == GMSH and geometry_kind == TESSELLATED:
        return Support(
            engine, False,
            f'Gmsh cuts the farfield {spec.shape} out of the imported solids '
            'with the CAD kernel, and a tessellated (STL/OBJ) import has no '
            'solid to cut; import the geometry as STEP or IGES. The '
            f'{spec.shape} is kept as authored')
    return Support(engine, True, '')


def on_engine_switch(spec: FarfieldSpec, engine: str,
                     geometry_kind: str = CAD) -> dict:
    """What an engine switch does to the farfield: nothing, and it says so.

    The specification is returned unchanged -- a switch never clears,
    converts or re-derives it -- together with whether the new engine can
    build it and, when it cannot, the reason to show beside it.
    """
    answer = support(spec, engine, geometry_kind)
    return {'kept': True, 'spec': spec.to_values(), 'engine': answer.engine,
            'supported': answer.supported, 'reason': answer.reason}


# --------------------------------------------------------------------------- #
# Authoring helpers (the Geometry page's Add -> Farfield... entry)
# --------------------------------------------------------------------------- #

def leaves_read(shape, centre_mode='auto') -> tuple[str, ...]:
    """The dimension leaves a farfield of *shape* reads, beyond the switch,
    the shape and the centre mode.

    The rule :func:`farfield_primitives.normalise` applies, said once for a
    page: a box reads its padding, a sphere its radius, a cylinder its
    radius, length and axis, and any shape reads its centre only when the
    centre is explicit. A control for a leaf not listed here changes nothing
    the engines build, so a page does not offer it.
    """
    base = primitives()
    shape = _enum_text(shape, base.BOX)
    mode = _enum_text(centre_mode, base.CENTRE_AUTO)
    leaves: list[str] = []
    if mode == base.CENTRE_EXPLICIT:
        leaves.append('centre')
    if shape == base.BOX:
        leaves.append('padding')
    elif shape == base.SPHERE:
        leaves.append('radius')
    elif shape == base.CYLINDER:
        leaves.extend(('radius', 'length', 'axis'))
    return tuple(leaves)


def check(spec: FarfieldSpec, bounds=None) -> list[str]:
    """Why *spec* would be refused, before anything is written.

    The dimension checks (:meth:`FarfieldSpec.problems`) always; the
    containment check too when *bounds* (Gmsh order, metres) are known. A
    farfield that is off is never refused: its numbers are kept, not built.
    """
    if not spec.enabled:
        return []
    problems = spec.problems()
    if problems or bounds is None:
        return problems
    try:
        spec.resolve(bounds)
    except primitives().FarfieldError as error:
        return [str(error)]
    return []


def fitted(spec: FarfieldSpec, bounds) -> FarfieldSpec:
    """*spec* with the dimensions of its shape sized to hold *bounds*.

    ``farfield_primitives.suggest``: what a page offers when a sphere or a
    cylinder is chosen, so it does not start out refused. The centre, the
    axis direction and every other leaf are kept; a box is returned as it is,
    since its padding follows the model already.
    """
    from dataclasses import replace

    base = primitives()
    if spec.shape not in (base.SPHERE, base.CYLINDER):
        return spec
    axis = spec.axis if any(spec.axis) else base.DEFAULT_AXIS
    sized = base.suggest(spec.shape, bounds, spec.padding, axis)
    return replace(spec, radius=float(sized['radius']),
                   length=float(sized.get('length', spec.length)))


def title(spec: FarfieldSpec) -> str:
    """How a list names the farfield: the role and its shape."""
    return f'Farfield ({spec.shape})'
