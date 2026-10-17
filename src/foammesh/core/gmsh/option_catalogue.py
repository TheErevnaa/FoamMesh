"""Every option of the installed Gmsh, as data (Plan 41 GN2, section 4.5).

The Gmsh route used to offer a hand-picked handful of options and clamp each to
a "qualified" set, so an option nobody had thought of could not be reached and
one somebody had thought of could not be taken past the set.  This module is
the other way round: the catalogue holds every ``General``, ``Geometry``,
``Mesh``, ``Solver``, ``PostProcessing`` and ``Print`` option of Gmsh 4.15.2
(name, storage, factory default, kind, enum choices, unit, owning page, help),
and a value the user sets is passed to Gmsh as set.

What the catalogue refuses, it refuses *before* the run and with a reason:

* a name the build does not have (``Mesh.CompoundLcFactor`` is in older manuals);
* a read-only option (``Mesh.NbNodes``);
* an enum value Gmsh does not document -- MEASURED, Gmsh does not reject these
  but silently rewrites them: ``Mesh.Algorithm3D`` 2 reads back as 1 and
  ``Mesh.RecombinationAlgorithm`` 9 as 0, and 4 reads back as 4 and meshes
  nothing (``plans/evidence/plan41/measured-refusals.md``);
* a value that is not of the option's kind.

The data is generated (``scripts/gmsh_option_catalogue.py``) and checked
against the installed build by ``tests/unit/test_gn2_option_catalogue.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re

from .option_catalogue_data import BUILD_VERSION, ROWS

#: The pages that mount an Advanced tier rendered from the catalogue.  The
#: geometry-description page is deliberately absent: it is shared with the
#: Preparation step that comes before the engine choice, and nothing before
#: the choice changes (Plan 41 section 1).  Its options are in the Options
#: panel on the Compute page.
MOUNTED_PAGES = (
    # Plan 41 GN18 (section 4.1): every define row has its Advanced tier.
    # The geometry row's tier is the ``Geometry.*`` family the catalogue files
    # under the (hosted) describe-geometry task; Structure and Periodic take
    # the ``Mesh.*`` options that decide a structured or a periodic mesh.
    'gmsh.geometry', 'gmsh.global_sizing', 'gmsh.volume_controls',
    'gmsh.boundary_layers', 'gmsh.periodic', 'gmsh.compute',
    'gmsh.qa', 'gmsh.publish',
)
#: The page that carries the complete, searchable Options panel.
OPTIONS_PANEL_PAGE = 'gmsh.compute'

#: The tabs of Gmsh's Options window, in its order.
GROUPS = ('General', 'Geometry', 'Mesh', 'Solver', 'PostProcessing', 'Print')

#: Where the stored overrides live in the project.
OVERRIDES_FIELD = 'gmsh.options.overrides'

#: Options that were in older manuals (or in this plan's first reading of
#: them) and that the 4.15.2 build does not have.  PROBED, see
#: ``plans/evidence/plan41/measured-refusals-probe.json``.
ABSENT_ON_THIS_BUILD = {
    'Mesh.QuadqsTopoOptimizationMethods':
        'there is no such option in Gmsh 4.15.2; the quasi-structured quad '
        'algorithm reads Mesh.QuadqsTopologyOptimizationMethods',
    'Mesh.QuadqsScalingOnNearCritical': None,
    'Mesh.CompoundLcFactor': 'the option is Mesh.CompoundMeshSizeFactor here',
    'Mesh.SecondOrderExperimental': None,
    'Geometry.OCCSafeUnbind': None,
    'Geometry.OCCMeshExactBoundary': None,
}


#: UXB3c. Options that hold a length although the generated unit column does
#: not say so (``Geometry.Offset*`` are in model coordinates, ``Mesh.Radius*``
#: bound an edge length, ``Geometry.MatchMeshTolerance`` is a distance).
LENGTH_OPTIONS = frozenset((
    'Geometry.OffsetX', 'Geometry.OffsetY', 'Geometry.OffsetZ',
    'Geometry.MatchMeshTolerance', 'Mesh.RadiusInf', 'Mesh.RadiusSup'))


class OptionRefusal(ValueError):
    """An option value Gmsh cannot be given as asked, with the measured why."""


@dataclass(frozen=True)
class Option:
    name: str
    storage: str          #: number | string | color
    default: object
    kind: str             #: bool | int | float | enum | string | color
    choices: tuple
    unit: str
    page: str
    read_only: bool
    help: str

    @property
    def group(self) -> str:
        return self.name.split('.', 1)[0]

    @property
    def label(self) -> str:
        return self.name.split('.', 1)[1]

    @property
    def is_length(self) -> bool:
        """UXB3c. Holds a length in model units, which a panel shows in the
        project display unit. The generated unit column names five; the
        others are the options whose help says they are in model coordinates
        or are an edge length."""
        return self.unit == 'length' or self.name in LENGTH_OPTIONS

    @property
    def is_display(self) -> bool:
        """An option that draws Gmsh's own window (reaches Gmsh, shows
        nothing in FoamMesh's viewport until GN17)."""
        return self.page in ('display', 'general')


def _build() -> dict:
    table = {}
    for row in ROWS:
        option = Option(*row)
        if option.name in table:
            raise ValueError(f'duplicate option {option.name}')
        table[option.name] = option
    return table


CATALOGUE: dict[str, Option] = _build()


def get(name: str):
    return CATALOGUE.get(str(name))


def names() -> tuple[str, ...]:
    return tuple(CATALOGUE)


#: Plan 41 GN18. The catalogue files an option under the task that first
#: owned it. The rows of section 4.1 regroup the work, so an option is shown
#: on the row whose question it answers: the geometry family on Geometry, the
#: structure and recombination options on Structure, the periodicity options
#: on Periodic. ``Option.page`` itself is the generated record and is not
#: rewritten.
ROW_OF_PAGE = {'gmsh.describe_geometry': 'gmsh.geometry'}
_STRUCTURE_PREFIXES = (
    'Mesh.Recombin', 'Mesh.Quadqs', 'Mesh.QuasiTransfinite',
    'Mesh.FlexibleTransfinite', 'Mesh.TransfiniteTri',
    'Mesh.SubdivisionAlgorithm', 'Mesh.Compound')
_PERIODIC_NAMES = ('Mesh.IgnorePeriodicity', 'Mesh.HighOrderPeriodic',
                   'Mesh.HighOrderSavePeriodic')
_LAYER_NAMES = ('Mesh.AnisoMax', 'Mesh.BoundaryLayerFanElements')


def row_of(option: Option) -> str:
    """The row (page id) whose Advanced tier shows *option*."""
    if option.page in ('display', 'general', 'gmsh.publish', 'gmsh.qa'):
        return option.page
    if option.name in _PERIODIC_NAMES:
        return 'gmsh.periodic'
    if option.name in _LAYER_NAMES:
        return 'gmsh.boundary_layers'
    if option.name.startswith(_STRUCTURE_PREFIXES):
        return 'gmsh.volume_controls'
    return ROW_OF_PAGE.get(option.page, option.page)


def options_for_page(page: str) -> tuple[Option, ...]:
    return tuple(option for option in CATALOGUE.values()
                 if row_of(option) == page)


def options_for_group(group: str) -> tuple[Option, ...]:
    return tuple(option for option in CATALOGUE.values()
                 if option.group == group)


def search(text: str, group: str = '') -> tuple[Option, ...]:
    """Options whose name or help holds every word of *text*."""
    words = [word.lower() for word in str(text or '').split() if word]
    found = []
    for option in CATALOGUE.values():
        if group and option.group != group:
            continue
        haystack = (option.name + ' ' + option.help).lower()
        if all(word in haystack for word in words):
            found.append(option)
    return tuple(found)


def control_for_option() -> dict:
    """Option name -> the page control that already sets it.

    Read from the control register so the two cannot disagree.  An override of
    one of these wins over the control (it is applied after it) and the ledger
    and the derivation's warning both say so.
    """
    from .fields import CONTROLS, OPTION

    mapping = {}
    for control in CONTROLS.values():
        if control.native_kind == OPTION and control.native_name:
            mapping.setdefault(control.native_name, control.path)
    return mapping


def precedence_note(name: str) -> str:
    """What wins when *name* has a page control and an Options row.

    Plan 42 SG20. Empty for an option with no page control.  The rule is the
    one :func:`resolve_overrides` applies: the Options value is applied after
    the page controls, so it wins.
    """
    control = control_for_option().get(name)
    if not control:
        return ''
    return (f'Also set by the page control {control}. A value set here is '
            'applied after the control and wins.')


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #

_TRUE = {'1', 'true', 'yes', 'on'}
_FALSE = {'0', 'false', 'no', 'off', ''}


def refusal_for_unknown(name: str) -> str:
    if name in ABSENT_ON_THIS_BUILD:
        why = ABSENT_ON_THIS_BUILD[name]
        return (f'Gmsh {BUILD_VERSION} has no option {name}'
                + (f': {why}' if why else ''))
    return (f'{name} is not an option of Gmsh {BUILD_VERSION}; the catalogue '
            'holds every option the installed build documents')


def _number(option: Option, raw) -> float:
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, str):
        text = raw.strip().lower()
        if option.kind == 'bool' and text in _TRUE | _FALSE:
            return 1.0 if text in _TRUE else 0.0
        try:
            value = float(text)
        except ValueError:
            raise OptionRefusal(
                f'{option.name} takes a number, not {raw!r}') from None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise OptionRefusal(
                f'{option.name} takes a number, not {raw!r}') from None
    if not math.isfinite(value):
        raise OptionRefusal(f'{option.name} takes a finite number')
    return value


def parse_value(name: str, raw):
    """The value Gmsh is to be given for *name*, or :class:`OptionRefusal`."""
    option = get(name)
    if option is None:
        raise OptionRefusal(refusal_for_unknown(str(name)))
    if option.read_only:
        raise OptionRefusal(
            f'{name} is read-only in Gmsh {BUILD_VERSION}: it reports a value '
            'and cannot be set')
    if option.storage == 'string':
        return '' if raw is None else str(raw)
    if option.storage == 'color':
        if isinstance(raw, str):
            raw = [part for part in re.split(r'[\s,;{}\[\]]+', raw) if part]
        try:
            parts = [int(float(part)) for part in raw]
        except (TypeError, ValueError):
            raise OptionRefusal(
                f'{name} takes a colour as red, green, blue and optionally '
                f'alpha (0 to 255), not {raw!r}') from None
        if len(parts) == 3:
            parts.append(255)
        if len(parts) != 4 or any(not 0 <= part <= 255 for part in parts):
            raise OptionRefusal(
                f'{name} takes a colour as red, green, blue and optionally '
                'alpha, each 0 to 255')
        return tuple(parts)
    value = _number(option, raw)
    if option.kind == 'enum':
        allowed = {key for key, _label in option.choices}
        if value != int(value) or int(value) not in allowed:
            listing = ', '.join(f'{key}: {label}'
                                for key, label in option.choices)
            raise OptionRefusal(
                f'{name} = {raw!r} is not a value Gmsh {BUILD_VERSION} '
                f'documents ({listing}). Gmsh does not reject the others: it '
                'rewrites or ignores them (measured, '
                'plans/evidence/plan41/measured-refusals.md), so the request '
                'is refused here')
        return float(int(value))
    if option.kind in ('bool', 'int') and not option.is_length:
        # A length is never a whole number by nature: the generated kind of
        # Mesh.MeshSizeMin / ToleranceEdgeLength is inferred from a default
        # of 0 (UXB3c), and 0.005 m is a valid minimum size.
        if value != int(value):
            raise OptionRefusal(f'{name} takes a whole number, not {raw!r}')
        if option.kind == 'bool' and int(value) not in (0, 1):
            raise OptionRefusal(f'{name} is on (1) or off (0), not {raw!r}')
    return value


def same_value(option: Option, left, right) -> bool:
    if option.storage == 'number':
        return math.isclose(float(left), float(right), rel_tol=1e-9,
                            abs_tol=1e-12)
    return tuple(left) == tuple(right) if option.storage == 'color' \
        else str(left) == str(right)


# --------------------------------------------------------------------------- #
# The stored form: one JSON object, name -> value
# --------------------------------------------------------------------------- #

def overrides_from_text(text) -> dict:
    """The stored overrides, as ``{name: raw value}``.  Empty for no text."""
    if text is None or not str(text).strip():
        return {}
    try:
        document = json.loads(str(text))
    except ValueError as error:
        raise OptionRefusal(
            f'the stored Gmsh options are not readable: {error}') from None
    if not isinstance(document, dict):
        raise OptionRefusal('the stored Gmsh options must be an object of '
                            'name: value pairs')
    return document


def overrides_to_text(mapping) -> str:
    if not mapping:
        return ''
    return json.dumps({key: (list(value) if isinstance(value, tuple)
                             else value)
                       for key, value in sorted(dict(mapping).items())},
                      separators=(',', ':'))


def resolve_overrides(mapping) -> tuple[tuple, tuple[str, ...]]:
    """``(rows, warnings)``: validated ``(name, storage, value)`` rows.

    Raises :class:`OptionRefusal` listing every refused option at once.
    """
    rows = []
    refused = []
    warnings = []
    controls = None
    for name, raw in sorted(dict(mapping or {}).items()):
        try:
            value = parse_value(name, raw)
        except OptionRefusal as error:
            refused.append(str(error))
            continue
        option = CATALOGUE[name]
        if controls is None:
            controls = control_for_option()
        control = controls.get(name)
        if control:
            warnings.append(
                f'Gmsh option {name} is set in the Options list and by the '
                f'control {control}; the Options value is applied last and '
                'wins.')
        rows.append((name, option.storage,
                     list(value) if isinstance(value, tuple) else value))
    if refused:
        raise OptionRefusal('; '.join(refused))
    return tuple(rows), tuple(warnings)


# --------------------------------------------------------------------------- #
# Gmsh's own options-file syntax (``Mesh.Smoothing = 3;``)
# --------------------------------------------------------------------------- #

def to_opt_text(mapping) -> str:
    lines = ['// FoamMesh Gmsh options (Gmsh ' + BUILD_VERSION + ')']
    for name, value in sorted(dict(mapping or {}).items()):
        option = get(name)
        if option is None:
            continue
        if option.storage == 'string':
            lines.append(f'{name} = "{value}";')
        elif option.storage == 'color':
            lines.append(f'{name} = {{{",".join(str(int(v)) for v in value)}}};')
        else:
            number = float(value)
            lines.append(f'{name} = '
                         + (str(int(number)) if number == int(number)
                            and abs(number) < 1e15 else repr(number)) + ';')
    return '\n'.join(lines) + '\n'


_OPT_LINE = re.compile(r'^\s*([A-Za-z]+(?:\.[A-Za-z0-9_]+)+)\s*=\s*(.*?)\s*;'
                       r'\s*(?://.*)?$')


def from_opt_text(text: str) -> dict:
    """Parse an options file; refuses a line it cannot read, silently skips
    none (a skipped line is a setting that did not arrive)."""
    mapping = {}
    for number, line in enumerate(str(text).splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith('//'):
            continue
        match = _OPT_LINE.match(line)
        if not match:
            raise OptionRefusal(f'line {number}: cannot read {stripped!r} as '
                                'Name = value;')
        name, value = match.groups()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            mapping[name] = value[1:-1]
        elif value.startswith('{'):
            mapping[name] = [int(float(part)) for part in
                             re.split(r'[\s,{}]+', value) if part]
        else:
            try:
                mapping[name] = float(value)
            except ValueError:
                raise OptionRefusal(
                    f'line {number}: {value!r} is not a number') from None
    return mapping


# --------------------------------------------------------------------------- #
# The user's default options (Gmsh: "Save options as default")
# --------------------------------------------------------------------------- #

def defaults_path() -> Path:
    override = os.environ.get('FOAMMESH_GMSH_OPTION_DEFAULTS')
    if override:
        return Path(override)
    return Path.home() / '.FoamMesh' / 'gmsh-default-options.json'


def save_defaults(mapping) -> Path:
    path = defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(overrides_to_text(mapping) or '{}', encoding='utf-8')
    return path


def load_defaults() -> dict:
    path = defaults_path()
    if not path.is_file():
        return {}
    return overrides_from_text(path.read_text(encoding='utf-8'))


def read_opt_file(path) -> dict:
    """The options of a Gmsh ``.opt`` file; a bad line refuses the whole file."""
    return from_opt_text(Path(path).read_text(encoding='utf-8'))


def write_opt_file(path, mapping) -> None:
    """Write *mapping* as a Gmsh ``.opt`` file."""
    Path(path).write_text(to_opt_text(mapping), encoding='utf-8')
