"""One ``decomposeParDict`` body, with the coefficients each method requires.

Plan 26 WP9.2. The method was a source constant in **two** writers -- both
hardcoded ``scotch`` -- and the enum that declared nine methods had no writer
accepting a method parameter at all. Given P8, the decomposition is a meshing
input: snappy's refinement depends on where the partition cuts fall relative to
the refined region, so a `duct` case moves +12.4% in cell count between serial
and 16 ranks while a 609k-cell `annulus` moves -0.04% on the same ranks.

**Shipping the combo without the coefficients would be worse than not shipping
it.** ``hierarchical`` and ``simple`` both refuse to run without an ``n``
vector, so a dictionary naming them and omitting it is one ``decomposePar``
rejects -- an inert control that fails at run time rather than in the GUI.
This module exists so the method and its coefficients cannot be written apart.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path


@dataclass(frozen=True)
class MethodCapability:
    """One ``decomposeParDict`` method, and what it takes to actually run it.

    Plan 31 CP-07 item 4. The methods were declared in three places that did
    not agree -- an enum of nine in the dictionary class, a tuple of three in
    this module, an enum of three in the schema -- and none of them consulted
    the runtime. ``metis`` is the case that makes this concrete: this machine's
    OpenFOAM 13 ships ``libmetisDecomp.so``, but only inside ``lib/dummy``,
    which is OpenFOAM's stub directory. A probe that merely found the filename
    would offer the user a method that aborts at run time.
    """

    name: str
    #: Whether this product can write a *complete* dictionary for it. A method
    #: needing inputs the product does not author is not a control, it is a
    #: dictionary decomposePar rejects.
    authorable: bool
    #: The runtime library that must be loadable, if the method needs one
    #: beyond ``libdecompositionMethods.so``.
    library: str | None
    needs_coefficients: bool
    #: Why it is not authorable, in the user's terms. Empty when it is.
    note: str = ''


#: Every method ``decomposeParDict`` accepts, in one place.
REGISTRY: tuple[MethodCapability, ...] = (
    MethodCapability('scotch', True, 'libscotchDecomp.so', False),
    MethodCapability('hierarchical', True, None, True),
    MethodCapability('simple', True, None, True),
    MethodCapability(
        'none', False, None, False,
        'writes every cell to one processor, which is what running serially '
        'already does'),
    MethodCapability(
        'manual', False, None, False,
        'needs a file naming the processor of every single cell, which this '
        'product does not author'),
    MethodCapability(
        'metis', True, 'libmetisDecomp.so', False),
    MethodCapability(
        'kahip', True, 'libkahipDecomp.so', False),
    MethodCapability(
        'structured', False, None, False,
        'decomposes a mesh in layers off a named patch, which needs a patch '
        'list this product does not author'),
    MethodCapability(
        'multiLevel', False, None, False,
        'nests one method inside another and needs a per-level dictionary '
        'this product does not author'),
)

#: OpenFOAM builds stub versions of the optional decomposition libraries into
#: this directory so a case naming one fails with a message rather than a
#: missing symbol. Finding a library *here* means the runtime does not have it.
STUB_LIBRARY_DIRECTORY = 'dummy'

#: Methods this product writes. Derived from the registry rather than typed
#: again: a fourth list of method names is how the first three came to
#: disagree.
METHODS = tuple(item.name for item in REGISTRY if item.authorable)
#: Methods that need an ``n`` vector whose product equals the rank count.
NEEDS_COEFFICIENTS = tuple(
    item.name for item in REGISTRY if item.needs_coefficients)
#: Valid ``order`` values for ``hierarchicalCoeffs``.
ORDERS = ('xyz', 'xzy', 'yxz', 'yzx', 'zxy', 'zyx')


class DecompositionError(ValueError):
    pass


def factor_ranks(ranks: int) -> tuple[int, int, int]:
    """Split *ranks* into three factors whose product is exactly *ranks*.

    Exactness is the whole requirement: ``decomposePar`` refuses when
    ``n.x * n.y * n.z != numberOfSubdomains``, so an approximation here becomes
    a run-time failure the user cannot connect to anything they typed. The
    split is deliberately balanced -- a 16-rank job becomes 4x2x2 rather than
    16x1x1 -- because a single-axis cut on a long domain puts every partition
    boundary through the same features.
    """
    ranks = int(ranks)
    if ranks < 1:
        raise DecompositionError('rank count must be positive')
    factors = [1, 1, 1]
    remaining = ranks
    divisor = 2
    while remaining > 1:
        while remaining % divisor:
            divisor += 1
            if divisor * divisor > remaining:
                divisor = remaining
        remaining //= divisor
        # Always grow the currently smallest axis, so the result is as close to
        # cubic as the factorisation allows.
        factors[factors.index(min(factors))] *= divisor
    factors.sort(reverse=True)
    return tuple(factors)                                # type: ignore[return-value]


def parse_cells(text, ranks: int) -> tuple[int, int, int]:
    """A user's ``n`` vector, or a derived one when they did not state it."""
    values = [item for item in str(text or '').replace(',', ' ').split() if item]
    if not values:
        return factor_ranks(ranks)
    if len(values) != 3:
        raise DecompositionError(
            'the decomposition cell counts need three values, one per axis')
    try:
        parsed = tuple(int(item) for item in values)
    except ValueError as error:
        raise DecompositionError(
            'the decomposition cell counts must be whole numbers') from error
    if any(item < 1 for item in parsed):
        raise DecompositionError('each decomposition axis needs at least one part')
    product = math.prod(parsed)
    if product != int(ranks):
        raise DecompositionError(
            f'the decomposition {parsed[0]}x{parsed[1]}x{parsed[2]} makes '
            f'{product} subdomains but the run uses {int(ranks)}; '
            'decomposePar requires them to be equal')
    return parsed                                        # type: ignore[return-value]


def parse_names(text) -> tuple[str, ...]:
    """Split a user-typed name list on commas or whitespace, order kept.

    OpenFOAM name lists are ``(a b c)``; a text field is the honest editor for
    them because the names are patches and zones the user knows by name. Empty
    means "no constraint", which is what an untouched case has always written.
    """
    if not text:
        return ()
    raw = str(getattr(text, 'value', text))
    seen: list[str] = []
    for token in raw.replace(',', ' ').replace('(', ' ').replace(')', ' ').split():
        if token not in seen:
            seen.append(token)
    return tuple(seen)


@dataclass(frozen=True)
class DecompositionConstraints:
    """The ``constraints {}`` block of ``decomposeParDict``, as v13 reads it.

    Plan 31. Nothing wrote this block, so a faceZone or a baffle the
    castellation step created could be cut straight down the middle by the
    partitioner and no surface said so until a solver refused the case.

    ``decompositionMethod.C:106-130`` reads ``constraints`` as a dictionary of
    dictionaries and looks up ``type`` in each entry; the entry *names* are
    free, so they are chosen here to say what each one preserves. Every
    constraint below was run through ``decomposePar`` on OpenFOAM 13
    (``13-58ed5c2046ef``) on a zoned single-block case:

    * ``preserveFaceZones`` reads ``zones`` (a wordReList) -- measured
      "preserveFaceZones : adding constraints to keep owner and neighbour of
      faces in zones 1(midZone) on same processor".
    * ``preserveBaffles`` reads nothing but its ``type``.
    * ``preservePatches`` reads ``patches``.
    * ``refinementHistory`` reads nothing but its ``type`` and is a no-op on a
      mesh that carries none.
    * ``singleProcessorFaceSets`` reads a key **of its own name** --
      ``singleProcessorFaceSetsConstraint.C:59`` does
      ``constraintsDict.lookup("singleProcessorFaceSets")``. This module used
      to write ``sets`` with an ``enabled`` switch beside it, which is the ESI
      spelling; measured on v13 that dictionary is a hard failure ("keyword
      singleProcessorFaceSets is undefined in dictionary
      .../decomposeParDict/processors") and no ranks start at all.

    Everything defaults to "no constraint", so a case that never opens the
    Execution page gets the dictionary it always got.
    """

    #: faceZone names to keep whole. Owner and neighbour of every face in the
    #: zone land on one processor.
    face_zones: tuple[str, ...] = ()
    preserve_baffles: bool = False
    #: Patch names whose faces stay unsplit across processors.
    patches: tuple[str, ...] = ()
    refinement_history: bool = False
    #: faceSet names, each pushed entirely onto one processor (``-1`` lets
    #: the decomposition choose which).
    single_processor_face_sets: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.face_zones or self.preserve_baffles or self.patches
                    or self.refinement_history
                    or self.single_processor_face_sets)

    def render(self) -> dict:
        """The ``constraints`` sub-dictionary, or ``{}`` when nothing is set."""
        block: dict = {}
        if self.face_zones:
            block['faceZones'] = {
                'type': 'preserveFaceZones',
                'zones': list(self.face_zones),
            }
        if self.preserve_baffles:
            block['baffles'] = {'type': 'preserveBaffles'}
        if self.patches:
            block['patches'] = {
                'type': 'preservePatches',
                'patches': list(self.patches),
            }
        if self.refinement_history:
            block['refinementHistory'] = {'type': 'refinementHistory'}
        if self.single_processor_face_sets:
            block['processors'] = {
                'type': 'singleProcessorFaceSets',
                # The key repeats the type name. That is not a typo; it is
                # what v13 looks up, and the alternative spelling is fatal.
                'singleProcessorFaceSets': [
                    [name, '-1'] for name in self.single_processor_face_sets],
            }
        return block


def build(ranks: int, *, method: str = 'scotch', order: str = 'xyz',
          cells=None, single_processor_face_sets=None,
          constraints: 'DecompositionConstraints | None' = None,
          weight_field: str = '') -> dict:
    """The ``decomposeParDict`` body for *ranks*, complete for *method*.

    ``weight_field`` names a ``volScalarField`` the decomposition weights
    cells by. It is read with ``IOobject::MUST_READ``
    (``domainDecompositionDecompose.C:144-160``), so naming a field the case
    does not carry is fatal -- measured on v13: "cannot find file
    <case>/0/cellWeight". Empty, the default, writes no key.
    """
    ranks = int(ranks)
    if ranks < 1:
        raise DecompositionError('decomposition rank count must be positive')
    name = str(getattr(method, 'value', method) or 'scotch').split('.')[-1]
    if name not in METHODS:
        raise DecompositionError(
            f'unknown decomposition method {name!r}; expected one of '
            f'{", ".join(METHODS)}')

    document: dict = {'numberOfSubdomains': ranks, 'method': name}
    if name in NEEDS_COEFFICIENTS:
        counts = parse_cells(cells, ranks)
        chosen = str(getattr(order, 'value', order) or 'xyz').split('.')[-1]
        if chosen not in ORDERS:
            raise DecompositionError(
                f'unknown decomposition order {chosen!r}; expected one of '
                f'{", ".join(ORDERS)}')
        coefficients = {'n': list(counts)}
        if name == 'hierarchical':
            # `simple` has no order; writing one would be an entry OpenFOAM
            # ignores, which reads as a control that did something.
            coefficients['order'] = chosen
        document[f'{name}Coeffs'] = coefficients

    combined = constraints or DecompositionConstraints()
    if single_processor_face_sets:
        combined = replace(
            combined,
            single_processor_face_sets=tuple(
                combined.single_processor_face_sets)
            + tuple(str(item) for item in single_processor_face_sets))
    rendered = combined.render()
    if rendered:
        document['constraints'] = rendered
    if str(weight_field or '').strip():
        document['weightField'] = str(weight_field).strip()
    return document


@dataclass(frozen=True)
class MethodChoice:
    """One method as the user meets it: offered, or refused with a reason."""

    name: str
    available: bool
    reason: str = ''

    def as_choice(self) -> tuple:
        """The 4-tuple ``FieldEditor`` takes for a runtime-narrowed combo."""
        return (self.name, self.name,
                self.reason or 'available in the selected runtime',
                self.available)


def capability(name) -> MethodCapability | None:
    wanted = str(getattr(name, 'value', name) or '').split('.')[-1]
    return next((item for item in REGISTRY if item.name == wanted), None)


def selectable(libraries=None) -> tuple[MethodChoice, ...]:
    """Every declared method, said to be available only when it really is.

    *libraries* is what the runtime probe found: a mapping of library file
    name to the directory it was found in, or ``None`` when the runtime has
    not been probed. Unavailable methods are returned rather than dropped, so
    a page can show the whole vocabulary and say why a row is closed -- a
    silently shortened list is indistinguishable from a product that never
    had the feature.
    """
    choices = []
    for item in REGISTRY:
        if not item.authorable:
            choices.append(MethodChoice(item.name, False, item.note))
            continue
        if item.library is None or libraries is None:
            choices.append(MethodChoice(item.name, True))
            continue
        found = libraries.get(item.library)
        if found is None:
            choices.append(MethodChoice(
                item.name, False,
                f'{item.library} is not in the selected OpenFOAM runtime'))
        elif str(found).rstrip('/').endswith(STUB_LIBRARY_DIRECTORY):
            choices.append(MethodChoice(
                item.name, False,
                f'the selected OpenFOAM runtime ships only the stub '
                f'{item.library}, which aborts when a case asks for it'))
        else:
            choices.append(MethodChoice(item.name, True))
    return tuple(choices)


def available_names(libraries=None) -> tuple[str, ...]:
    return tuple(item.name for item in selectable(libraries) if item.available)


@dataclass(frozen=True)
class DecompositionSettings:
    """The project's decomposition choice, read once and passed whole.

    Plan 31 CP-07 item 4. Two of the three writers took a ``method`` argument
    and neither of their callers passed one, so a user who chose
    ``hierarchical`` on the Execution page got ``scotch`` written the moment
    the Parallel Environment dialog applied. Reading the three fields together
    is what stops one of them travelling without the others.

    Plan 31 (parallel.decompose_extras) adds the constraint switches for the
    same reason: a user who asks for whole faceZones and then opens the
    Parallel Environment dialog must not have the request written away by a
    second writer that never heard of it.
    """

    method: str = 'scotch'
    order: str = 'xyz'
    cells: str = ''
    #: Keep each faceZone's two sides on one processor.
    preserve_face_zones: bool = False
    preserve_baffles: bool = False
    #: Patch names, whitespace- or comma-separated as the user typed them.
    preserve_patches: str = ''
    preserve_refinement_history: bool = False
    #: A ``volScalarField`` name to weight the decomposition by. MUST_READ on
    #: v13, so an empty value writes no key.
    weight_field: str = ''

    @classmethod
    def read(cls, db) -> 'DecompositionSettings':
        def value(path, fallback):
            try:
                stored = db.getValue(path)
            except Exception:
                return fallback
            if stored is None:
                return fallback
            return str(getattr(stored, 'value', stored)).split('.')[-1]

        def flag(path) -> bool:
            raw = value(path, 'false')
            return str(raw).strip().lower() in ('true', '1', 'yes', 'on')

        return cls(
            value('mesh/execution/decompositionMethod', 'scotch'),
            value('mesh/execution/decompositionOrder', 'xyz'),
            value('mesh/execution/decompositionCells', '') or '',
            flag('mesh/execution/preserveFaceZones'),
            flag('mesh/execution/preserveBaffles'),
            value('mesh/execution/preservePatches', '') or '',
            flag('mesh/execution/preserveRefinementHistory'),
            value('mesh/execution/decompositionWeightField', '') or '')

    def constraints(self, face_zones=()) -> DecompositionConstraints:
        """What this project asks the partitioner to keep whole.

        *face_zones* are the zones the case actually has -- the writer knows
        them, the user does not, so the switch is "keep my zones whole" and
        the names come from the case rather than from a text box nobody can
        fill in correctly.
        """
        return DecompositionConstraints(
            face_zones=tuple(face_zones) if self.preserve_face_zones else (),
            preserve_baffles=bool(self.preserve_baffles),
            patches=parse_names(self.preserve_patches),
            refinement_history=bool(self.preserve_refinement_history))


def write(case_root, ranks: int, settings: DecompositionSettings | None = None,
          *, single_processor_face_sets=None, face_zones=()) -> Path:
    """Write ``system/decomposeParDict``, as the run is about to read it.

    Plan 31 CP-07 item 4. Three call sites used to put this file on disk --
    the case builder at generation, the engine seam before a parallel run, and
    the facade when the Parallel Environment dialog applied -- and they
    disagreed about the rank count, the method and the coefficients, because
    each formatted its own document. That is what item 4 closed: `build` is
    now the only thing that composes this dictionary, and the facade dialog
    reaches disk through this function rather than past it.

    It is not, however, the only thing that *writes* the file, and saying so
    here was wrong. `case_builder.write_case` still emits it at generation
    time, deliberately: nothing at generation knows the effective rank count
    (see `decompose_par_dict`, CP-07 item 6), so it writes the honest serial
    default and omits `singleProcessorFaceSets`, which is a launch-time
    constraint. This function then overwrites both from the allocation that
    actually ran. The order is fixed -- generation, then launch -- so the
    dictionary `decomposePar` opens is always the one written here. What a
    reader can still see is a generated case whose `numberOfSubdomains` is 1
    until a parallel run starts.
    """
    from foammesh.openfoam.dict_format import format_dictionary_file

    settings = settings or DecompositionSettings()
    target = Path(case_root) / 'system' / 'decomposeParDict'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        format_dictionary_file('decomposeParDict', build(
            int(ranks), method=settings.method, order=settings.order,
            cells=settings.cells,
            single_processor_face_sets=single_processor_face_sets,
            constraints=settings.constraints(face_zones),
            weight_field=settings.weight_field)),
        encoding='utf-8', newline='\n')
    return target


def written_ranks(case_root) -> int | None:
    """``numberOfSubdomains`` as it stands on disk, or ``None`` if unwritten.

    Plan 31 CP-07 item 6 turns on this being readable: a stored core count
    proves nothing, so the check that the dictionary and the launched ranks
    agree has to read the dictionary rather than the setting it came from.
    """
    target = Path(case_root) / 'system' / 'decomposeParDict'
    try:
        text = target.read_text(encoding='utf-8')
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('numberOfSubdomains'):
            digits = stripped[len('numberOfSubdomains'):].strip().rstrip(';')
            try:
                return int(digits)
            except ValueError:
                return None
    return None
