"""Whether a published field applies to the case as it is configured.

Plan 31 CP-09 item 4. ``FieldDescriptor.applies_when`` has been carried
end-to-end since the AF2 registry was written -- built in
:mod:`foammesh.core.facade.fields`, declared in
:mod:`foammesh.core.facade.field_metadata`, serialised into every engine
contract -- and no editor has ever read it. The result is a desktop that
offers ``gmsh/output/elementOrder`` while snappy is selected, writes it when
Apply is pressed, and stales the mesh for a setting the run cannot reach.

The grammar is deliberately tiny, because the metadata only ever uses one
shape: ``"<field id> == <literal>"``, with ``!=`` accepted for symmetry, and
a clause list that is an AND. Anything richer would be a rule engine hiding
in the schema, and nothing in the schema asks for one.

This module is pure: it takes clauses and a mapping of field id to value and
returns a verdict plus the sentence to show the user. The view disables the
editor and quotes the sentence; the caller decides what "does not apply"
should mean for a patch.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Comparisons the metadata uses. Longest first so ``!=`` is not read as
#: ``=`` with a stray ``!``.
_OPERATORS = ('==', '!=')


@dataclass(frozen=True)
class Clause:
    """One ``field == literal`` condition out of an ``applies_when`` tuple."""

    field_id: str
    operator: str
    literal: str

    def holds(self, values) -> bool | None:
        """True, False, or None when ``values`` cannot answer.

        Unknown is not False. A page that renders one group of fields cannot
        see the whole configuration, and greying a control because the caller
        did not look up its condition would be worse than leaving it live.
        """
        if self.field_id not in values:
            return None
        actual = normalise(values[self.field_id])
        wanted = normalise(self.literal)
        return actual == wanted if self.operator == '==' else actual != wanted


@dataclass(frozen=True)
class Applicability:
    """The verdict for one field, and the sentence that explains it."""

    applies: bool
    reason: str = ''
    #: The clause that decided a negative verdict, for callers that want to
    #: point at the control the user should change instead.
    blocking: Clause | None = None


def normalise(value) -> str:
    """Compare schema literals and stored values on the same footing.

    ``disabled`` is stored as a JSON boolean and written in the metadata as
    the word ``false``; the engine ids are strings either way.
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    return str(value).strip().lower()


def parse(applies_when) -> tuple[Clause, ...]:
    """Read the clause list. Anything unparseable is dropped, not guessed."""
    clauses = []
    for text in applies_when or ():
        clause = _parse_one(str(text))
        if clause is not None:
            clauses.append(clause)
    return tuple(clauses)


def _parse_one(text: str) -> Clause | None:
    for operator in _OPERATORS:
        head, sep, tail = text.partition(operator)
        if not sep:
            continue
        field_id = head.strip()
        literal = tail.strip()
        if field_id and literal:
            return Clause(field_id, operator, literal)
        return None
    return None


def referenced_fields(applies_when) -> tuple[str, ...]:
    """Which field ids a caller must look up to judge these clauses."""
    seen: list[str] = []
    for clause in parse(applies_when):
        if clause.field_id not in seen:
            seen.append(clause.field_id)
    return tuple(seen)


def evaluate(applies_when, values, *, titles=None) -> Applicability:
    """Judge one field's clauses against ``values``.

    ``titles`` maps a referenced field id to the name the user sees for it,
    so the sentence reads "Applies only when Meshing method is Gmsh" rather
    than quoting a dotted id at somebody who never chose one.
    """
    titles = titles or {}
    for clause in parse(applies_when):
        holds = clause.holds(values)
        if holds is None or holds:
            continue
        return Applicability(False, _reason(clause, values, titles), clause)
    return Applicability(True, '')


def _reason(clause: Clause, values, titles) -> str:
    name = titles.get(clause.field_id) or humanise_field(clause.field_id)
    wanted = humanise_value(clause.literal)
    actual = humanise_value(values.get(clause.field_id))
    if clause.operator == '!=':
        return (f'This setting is inactive while {name} is {actual}. '
                f'It applies to every other {name.lower()}.')
    return (f'This setting applies only when {name} is {wanted}. '
            f'It is {actual}, so the run will not read it.')


def humanise_field(field_id: str) -> str:
    leaf = str(field_id).rsplit('.', 1)[-1]
    return leaf.replace('_', ' ').strip().capitalize()


def humanise_value(value) -> str:
    text = normalise(value)
    if not text:
        return 'not set'
    known = {'gmsh': 'Gmsh', 'snappy': 'snappyHexMesh', 'su2': 'SU2',
             'openfoam': 'OpenFOAM', 'true': 'on', 'false': 'off'}
    return known.get(text, str(value).replace('_', ' '))
