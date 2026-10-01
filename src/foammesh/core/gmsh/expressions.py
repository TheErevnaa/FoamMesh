"""Validation and costing for user-supplied size expressions.

A ``MathEval`` size field is the most expressive refinement Gmsh offers: one
expression in ``x``, ``y`` and ``z`` sets the element size everywhere. It is
also the easiest control in the pipeline to misuse. Measured on a pipe, a
plausible radial expression produced 302,379 cells in 98 seconds; a mistyped
exponent would ask for far more and Gmsh would try.

So an expression is accepted only if two things hold:

* every name in it is one this module allows, and
* the size it produces across the domain does not ask for an absurd mesh.

The second is the point. Rejecting unknown *names* stops typos and would-be
injection; rejecting an absurd *cost* stops the far commoner mistake of an
expression that parses perfectly and means something enormous.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

from foammesh.core.quantities import agreeing

#: Variables Gmsh binds when evaluating a size expression.
VARIABLES = frozenset({'x', 'y', 'z'})

#: Functions Gmsh's expression parser provides. Anything outside this is
#: rejected by name rather than discovered at mesh time.
FUNCTIONS = frozenset({
    'abs', 'acos', 'asin', 'atan', 'atan2', 'ceil', 'cos', 'cosh', 'exp',
    'fabs', 'floor', 'fmod', 'hypot', 'log', 'log10', 'max', 'min', 'pow',
    'sin', 'sinh', 'sqrt', 'step', 'tan', 'tanh',
})

#: Named constants the parser understands.
#:
#: Plan 31 CP-08 item 1. ``F`` used to be in here. It is not a constant: in
#: Gmsh's expression parser ``F3`` means "the value of field 3", so listing
#: ``F`` admitted a reference to a field this plan does not know about, under
#: a numbering the user never sees. A reference like that is refused by name
#: below instead of being smuggled in as a constant.
CONSTANTS = frozenset({'pi', 'e', 'Pi'})

#: A reference to another Gmsh field by its runtime tag.
_FIELD_REFERENCE = re.compile(r'^F[0-9]*$')

_NAME = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
_ALLOWED_CHARACTERS = re.compile(r'^[A-Za-z_0-9 .,+\-*/^()%<>=!&|?:]*$')

#: Samples per axis when costing an expression over the bounding box. 9^3 is
#: 729 evaluations: enough to catch a runaway, cheap enough to run on a
#: keystroke.
SAMPLES = 9

#: No fixed element cap (2026-10-01: a mesh may go beyond 150 M cells when
#: the RAM holds it): an expression is refused only when the mesh it asks
#: for needs more RAM than is free (``resource_budget.mesher_memory_refusal``,
#: the same rule as the global sizing). This only keeps a runaway estimate a
#: finite integer.
ESTIMATE_CEILING = 10 ** 15


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class ExpressionCost:
    """What an expression asks for across the domain."""

    minimum_size: float
    maximum_size: float
    estimated_elements: int
    sampled: int
    non_positive: int

    def to_dict(self) -> dict:
        return {
            'minimumSize': self.minimum_size,
            'maximumSize': self.maximum_size,
            'estimatedElements': self.estimated_elements,
            'sampled': self.sampled,
            'nonPositive': self.non_positive,
        }


def validate_syntax(expression: str) -> str:
    """Check an expression uses only names and characters Gmsh understands."""
    text = str(expression or '').strip()
    if not text:
        raise ExpressionError('a math_eval size field needs an expression')
    if len(text) > 512:
        raise ExpressionError(
            f'the expression is {len(text)} characters; 512 is the limit')
    if not _ALLOWED_CHARACTERS.match(text):
        bad = sorted({item for item in text if not _ALLOWED_CHARACTERS.match(item)})
        raise ExpressionError(
            f'the expression contains characters Gmsh will not parse: '
            f'{" ".join(bad)}')
    names = set(_NAME.findall(text))
    # Field references first, so the message says what the name meant rather
    # than listing it among the typos.
    references = sorted(name for name in names if _FIELD_REFERENCE.match(name))
    if references:
        raise ExpressionError(
            f'the expression reads another size field ({", ".join(references)}); '
            'Gmsh numbers fields at mesh time and this plan cannot say which '
            'field that is, so write the size in x, y and z instead')
    unknown = sorted({
        name for name in names
        if name not in VARIABLES and name not in FUNCTIONS
        and name not in CONSTANTS})
    if unknown:
        raise ExpressionError(
            f'unknown {agreeing(len(unknown), "name")} in the expression: '
            f'{", ".join(unknown)}. '
            f'Available: {", ".join(sorted(VARIABLES))} and '
            f'{", ".join(sorted(FUNCTIONS))}')
    if not (names & VARIABLES):
        raise ExpressionError(
            'the expression does not use x, y or z, so it is a constant size; '
            'use a box, ball or cylinder field instead')
    return text


def _to_python(expression: str) -> str:
    """Rewrite Gmsh's expression syntax into Python for costing.

    Only used to *estimate* the mesh, never to produce it -- Gmsh evaluates
    the original string. The rewrite is limited to the caret operator, which
    is the one real difference.
    """
    return expression.replace('^', '**')


def cost(expression: str, bbox) -> ExpressionCost:
    """Sample the expression across the bounding box and size the result.

    ``bbox`` is any object exposing ``xmin``/``xmax`` and so on.
    """
    text = validate_syntax(expression)
    if bbox is None:
        raise ExpressionError(
            'the expression cannot be costed without geometry bounds; prepare '
            'the geometry first')
    try:
        bounds = (float(bbox.xmin), float(bbox.xmax), float(bbox.ymin),
                  float(bbox.ymax), float(bbox.zmin), float(bbox.zmax))
    except (AttributeError, TypeError, ValueError) as error:
        raise ExpressionError(
            'the geometry has no usable bounding box') from error

    environment = {name: getattr(math, name) for name in FUNCTIONS
                   if hasattr(math, name)}
    environment.update({
        'pi': math.pi, 'Pi': math.pi, 'e': math.e,
        'max': max, 'min': min, 'abs': abs, 'pow': pow,
        'step': lambda value: 1.0 if value > 0 else 0.0,
    })
    compiled = _to_python(text)

    smallest, largest, non_positive = math.inf, 0.0, 0
    # Element count is integrated over the domain rather than taken from the
    # smallest size: a size *field* varies, so assuming the finest size fills
    # the whole box overstates the mesh by orders of magnitude. Measured on a
    # pipe, that assumption predicted 57 million elements for an expression
    # that produces 302,379.
    density_sum = 0.0
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    for i in range(SAMPLES):
        for j in range(SAMPLES):
            for k in range(SAMPLES):
                point = {
                    'x': xmin + (xmax - xmin) * i / (SAMPLES - 1),
                    'y': ymin + (ymax - ymin) * j / (SAMPLES - 1),
                    'z': zmin + (zmax - zmin) * k / (SAMPLES - 1),
                }
                try:
                    value = float(eval(  # noqa: S307 - allowlisted names only
                        compiled, {'__builtins__': {}}, {**environment, **point}))
                except ZeroDivisionError:
                    non_positive += 1
                    continue
                except Exception as error:
                    raise ExpressionError(
                        f'the expression could not be evaluated at '
                        f'({point["x"]:.4g}, {point["y"]:.4g}, '
                        f'{point["z"]:.4g}): {error}') from error
                if not math.isfinite(value) or value <= 0:
                    non_positive += 1
                    continue
                smallest = min(smallest, value)
                largest = max(largest, value)
                density_sum += 1.0 / (value ** 3)

    total = SAMPLES ** 3
    if non_positive == total:
        raise ExpressionError(
            'the expression produced no positive size anywhere in the domain; '
            'an element size must be greater than zero')
    if non_positive:
        # A size that goes non-positive somewhere is a mesh Gmsh cannot make.
        raise ExpressionError(
            f'the expression is zero, negative or undefined at {non_positive} '
            f'of {total} sampled points; an element size must be positive '
            'everywhere in the domain')

    box_volume = abs((xmax - xmin) * (ymax - ymin) * (zmax - zmin))
    evaluated = total - non_positive
    # Roughly six tetrahedra fill a cube of edge h, so each sample contributes
    # its share of the box divided by h cubed.
    estimate = int(min(6.0 * box_volume * density_sum / max(evaluated, 1),
                       float(ESTIMATE_CEILING)))
    from foammesh.support.resource_budget import mesher_memory_refusal

    refusal = mesher_memory_refusal('gmsh', estimate)
    if refusal is not None:
        raise ExpressionError(
            f'the expression asks for roughly {estimate:,} elements '
            f'(sizes from {smallest:.6g} to {largest:.6g} m across a '
            f'{box_volume:.6g} m3 domain): {refusal}. Raise the smallest '
            'size it produces, or restrict it to a smaller region.')
    return ExpressionCost(
        minimum_size=smallest, maximum_size=largest,
        estimated_elements=estimate, sampled=total, non_positive=non_positive)
