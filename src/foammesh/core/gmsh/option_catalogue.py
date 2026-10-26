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

from .option_catalogue_data import ALIASES, BUILD_VERSION, DETAILS, ROWS

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
    kind: str             #: bool | int | float | enum | bitmask | path | string | color
    choices: tuple
    unit: str
    page: str
    read_only: bool
    help: str
    # Plan 48 GM2: read from Gmsh's source (Context.h, Options.cpp,
    # optionWindow.cpp), all optional so ``Option(*row)`` of nine stays valid.
    ctype: str = ''       #: Gmsh's own storage: int, double, bool, ...
    minimum: object = None    #: native range (widget, else setter clamp)
    maximum: object = None
    hard: object = None   #: the setter-enforced ``(low, high, low_exclusive)``
    bits: tuple = ()      #: for a bitmask: ((bit, label), ...)
    evidence: str = ''    #: where the kind came from

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


#: Plan 48 G15 (DP-5707). Options of FoamMesh's own Gmsh build only: the six
#: patch fields of the native High-order tools window
#: (``highOrderToolsWindow.cpp``), added by
#: ``scripts/gmsh_build/patches/gmsh-4.15.2-highorder-options.patch`` and read
#: by ``optimize("HighOrder")``. The generated rows come from the stock
#: 4.15.2, which has none of them, so they are written here by hand; on the
#: stock wheel the session refuses them with the reason.
FOAMMESH_BUILD_EVIDENCE = 'FoamMesh Gmsh build (G15 high-order options patch)'
FOAMMESH_BUILD_ROWS = (
    ('Mesh.HighOrderStrategy', 'number', 0.0, 'enum',
     ((0, 'Disjoint strong'), (1, 'Adaptive one-by-one'),
      (2, 'Disjoint weak')), '', 'gmsh.compute', False,
     'High-order optimization patch strategy'),
    ('Mesh.HighOrderWeight', 'number', 1.0, 'float', (), '', 'gmsh.compute',
     False, 'High-order optimization weight on node displacement'),
    ('Mesh.HighOrderDistanceFactor', 'number', 12.0, 'float', (), '',
     'gmsh.compute', False,
     'High-order optimization distance factor (patch size)'),
    ('Mesh.HighOrderMaxAdaptBlob', 'number', 2.0, 'int', (), '',
     'gmsh.compute', False,
     'Maximum number of patch adaptation iterations (adaptive strategy)'),
    ('Mesh.HighOrderAdaptBlobLayerFactor', 'number', 2.0, 'int', (), '',
     'gmsh.compute', False,
     'Number of layer adaptation factor (adaptive strategy)'),
    ('Mesh.HighOrderAdaptBlobDistFactor', 'number', 2.0, 'float', (), '',
     'gmsh.compute', False, 'Distance adaptation factor (adaptive strategy)'),
)
FOAMMESH_BUILD_RANGES = {
    'Mesh.HighOrderStrategy': (0, 2),
    'Mesh.HighOrderDistanceFactor': (1, 20000),
    'Mesh.HighOrderMaxAdaptBlob': (1, 100),
    'Mesh.HighOrderAdaptBlobLayerFactor': (1, 100),
    'Mesh.HighOrderAdaptBlobDistFactor': (1, 100),
}


#: DP-1437 (user, 2026-10-23). The Medit (.mesh) export is retired: Gmsh
#: 4.15.2's writer segfaults the session on tet+prism meshes and crashes on
#: some triangle/quadrangle surface meshes. Reading .mesh files stays.
MEDIT_RETIRED = ("Medit (.mesh) export was retired in 1.1.4: Gmsh 4.15.2's "
                 'writer crashes; export .msh, .su2, VTK or polyMesh instead.')
#: Enum values of generated rows that FoamMesh no longer offers, each with
#: the sentence a request for it is refused with. ``Mesh.Format`` 30 selects
#: the Medit writer whatever the file is called.
RETIRED_CHOICES = {'Mesh.Format': {30: MEDIT_RETIRED}}


def _retire_choices(row: tuple) -> tuple:
    retired = RETIRED_CHOICES.get(row[0])
    if not retired:
        return row
    choices = tuple(item for item in row[4] if item[0] not in retired)
    help_text = row[8]
    for key, label in row[4]:
        if key in retired:
            help_text = help_text.replace(f', {key}: {label}', '')
    return row[:4] + (choices,) + row[5:8] + (help_text,) + row[9:]


def _build() -> dict:
    table = {}
    for row in ROWS:
        row = _retire_choices(row)
        extra = DETAILS.get(row[0], {})
        option = Option(*row, **{key: extra[key] for key in (
            'ctype', 'minimum', 'maximum', 'hard', 'bits', 'evidence')
            if key in extra})
        if option.name in table:
            raise ValueError(f'duplicate option {option.name}')
        table[option.name] = option
    for row in FOAMMESH_BUILD_ROWS:
        if row[0] in table:
            raise ValueError(f'duplicate option {row[0]}')
        low, high = FOAMMESH_BUILD_RANGES.get(row[0], (None, None))
        table[row[0]] = Option(*row, minimum=low, maximum=high,
                               evidence=FOAMMESH_BUILD_EVIDENCE)
    return table


CATALOGUE: dict[str, Option] = _build()


def canonical_name(name: str) -> str:
    """The option *name* stands for: itself, or the live option a deprecated
    alias of Gmsh's ``DefaultOptions.h`` forwards to."""
    return ALIASES.get(str(name), str(name))


def is_alias(name: str) -> bool:
    return str(name) in ALIASES


def get(name: str):
    return CATALOGUE.get(canonical_name(name))


def is_view_option(name: str) -> bool:
    """Plan 48 FIX3 (DP-3002). A ``View.*`` option, or a deprecated name that
    forwards to one (``View.Format`` -> ``View.NumberFormat``), belongs to a
    post-processing view. FoamMesh draws no Gmsh view, so a legacy ``.opt``
    that names one is read and told it is not applied, not refused whole."""
    return canonical_name(name).startswith('View.')


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
            # GM2. A control may set several options ('A+B+C') or write a
            # deprecated name; the Options list is keyed by the live names.
            for part in control.native_name.split('+'):
                mapping.setdefault(canonical_name(part.strip()), control.path)
    return mapping


def precedence_note(name: str) -> str:
    """What wins when *name* has a page control and an Options row.

    Plan 42 SG20. Empty for an option with no page control.  The rule is the
    one :func:`resolve_overrides` applies: the Options value is applied after
    the page controls, so it wins.
    """
    control = control_for_option().get(canonical_name(name))
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
    name = option.name
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
        retired = RETIRED_CHOICES.get(name, {})
        if value == int(value) and int(value) in retired:
            raise OptionRefusal(retired[int(value)])
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
    _check_hard(option, value, raw)
    if option.kind in ('bool', 'int', 'bitmask')             and not option.is_length:
        # A length is never a whole number by nature: the generated kind of
        # Mesh.MeshSizeMin / ToleranceEdgeLength is inferred from a default
        # of 0 (UXB3c), and 0.005 m is a valid minimum size.
        if value != int(value):
            raise OptionRefusal(f'{name} takes a whole number, not {raw!r}')
        if option.kind == 'bool' and int(value) not in (0, 1):
            raise OptionRefusal(f'{name} is on (1) or off (0), not {raw!r}')
    return value


def _check_hard(option: Option, value: float, raw) -> None:
    """Refuse what Gmsh's own setter would refuse or silently rewrite."""
    if not option.hard:
        return
    low, high, exclusive = option.hard
    if low is not None and (value <= low if exclusive else value < low):
        raise OptionRefusal(
            f'{option.name} = {raw!r} is below what Gmsh accepts (it must be '
            + ('above ' if exclusive else 'at least ') + f'{low:g})')
    if high is not None and value > high:
        raise OptionRefusal(
            f'{option.name} = {raw!r} is above what Gmsh accepts (at most '
            f'{high:g})')


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
    given = dict(mapping or {})
    for name, raw in sorted(given.items()):
        canonical = canonical_name(name)
        if is_view_option(name):
            warnings.append(
                f'Gmsh option {name} belongs to a post-processing view; '
                'FoamMesh shows no Gmsh view, so it is not applied.')
            continue
        if canonical != name and canonical in given:
            warnings.append(
                f'Gmsh option {name} is a deprecated name of {canonical}, '
                f'which is also set; {canonical} wins.')
            continue
        try:
            value = parse_value(name, raw)
        except OptionRefusal as error:
            refused.append(str(error))
            continue
        option = CATALOGUE[canonical]
        name = canonical
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
            parsed = value[1:-1]
        elif value.startswith('{'):
            parsed = [int(float(part)) for part in
                      re.split(r'[\s,{}]+', value) if part]
        else:
            try:
                parsed = float(value)
            except ValueError:
                raise OptionRefusal(
                    f'line {number}: {value!r} is not a number') from None
        canonical = canonical_name(name)
        if canonical != name:
            # A deprecated name of an old file: the live option takes it, but
            # a line that names the live option itself wins either way.
            mapping.setdefault(canonical, parsed)
        else:
            mapping[name] = parsed
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


def startup_opt_path() -> Path:
    """The ``.opt`` file read at start-up (Gmsh reads ``gmsh-options`` from the
    user's folder the same way)."""
    override = os.environ.get('FOAMMESH_GMSH_STARTUP_OPT')
    if override:
        return Path(override)
    return Path.home() / '.FoamMesh' / 'gmsh-startup.opt'


def startup_defaults() -> dict:
    """Plan 48 GM2. The options every project starts with: the startup ``.opt``
    file, then the saved defaults on top of it.  A file that cannot be read is
    reported (:class:`OptionRefusal`), never skipped."""
    mapping = {}
    path = startup_opt_path()
    if path.is_file():
        mapping.update(read_opt_file(path))
    mapping.update(load_defaults())
    return mapping


def with_startup_defaults(stored) -> dict:
    """*stored* (the project's overrides) over the start-up defaults.

    What the project holds wins; a default fills only an option the project
    does not mention.  Deprecated names in either are mapped to the live name
    first, so ``{'Mesh.MinimumElementsPerTwoPi': 8}`` cannot sit beside a
    default for ``Mesh.MeshSizeFromCurvature``.
    """
    merged = {canonical_name(name): value
              for name, value in startup_defaults().items()}
    for name, value in dict(stored or {}).items():
        merged[canonical_name(name)] = value
    return merged


def read_opt_file(path) -> dict:
    """The options of a Gmsh ``.opt`` file; a bad line refuses the whole file."""
    return from_opt_text(Path(path).read_text(encoding='utf-8'))


def write_opt_file(path, mapping) -> None:
    """Write *mapping* as a Gmsh ``.opt`` file."""
    Path(path).write_text(to_opt_text(mapping), encoding='utf-8')
