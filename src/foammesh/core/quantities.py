#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One unit ladder, and one precision, for numbers printed side by side.

DP-165. Three lengths that exist to be compared with each other were each
rendered on their own with ``%.4g``, and ``g`` drops the trailing zeros of
whichever component happens to have them. A base cell of 14.12, 14.109 and
14.0 mm reached the screen as ``14.12 x 14.11 x 14``, and a block of 100,
299.9 and 600 mm as ``100 x 299.9 x 600``: one measurement printed to three
precisions, so the component that is a round number reads as a different
kind of number from the two beside it. Equal-looking quantities have to look
equal, so a group is rendered to one decimal count, taken from whichever
component needs the most of them.

The ladder that picks the unit was written twice as well, once for the
viewport readout and once for the sentence beside the picture, the two
copies agreeing by luck rather than by construction. It is written here
once, and both read off it.
"""

from __future__ import annotations

#: Descending, because the first rung a value clears is the one it gets.
LADDER = ((1.0, 1.0, 'm'), (1e-3, 1e3, 'mm'), (0.0, 1e6, chr(181) + 'm'))

#: A ceiling on what alignment may add, for a group whose components sit
#: decades apart and whose smallest would otherwise set the width of all.
_MAX_PLACES = 6


def unit_for(value: float) -> tuple[float, str]:
    """The scale factor and suffix a length of this size reads best in."""
    magnitude = abs(float(value))
    for threshold, scale, suffix in LADDER:
        if magnitude >= threshold:
            return scale, suffix
    return 1.0, 'm'


def _places(text: str) -> int:
    """How many digits this rendering put after the point."""
    _, _, tail = text.partition('.')
    return len(tail)


def aligned(values) -> list[str]:
    """Each number to four significant figures, all to one decimal count.

    ``%.4g`` on its own is the whole fault: it is a per-number format, and
    these numbers are not read one at a time. A group that spans decades
    falls back on the widest component, capped, because there the disparity
    is the thing worth seeing.
    """
    numbers = [float(value) for value in values]
    if not numbers:
        return []
    rendered = [f'{number:.4g}' for number in numbers]
    if any('e' in text or 'E' in text for text in rendered):
        return rendered
    places = min(max(_places(text) for text in rendered), _MAX_PLACES)
    return [f'{number:.{places}f}' for number in numbers]


def format_group(values, separator: str = ' × ') -> str:
    """The aligned numbers joined, in the unit the largest of them wants."""
    numbers = [float(value) for value in values]
    if not numbers:
        return ''
    scale, suffix = unit_for(max(abs(number) for number in numbers))
    body = separator.join(aligned(number * scale for number in numbers))
    return f'{body} {suffix}'


def agreeing(count, singular: str, plural: str = '') -> str:
    """The form that agrees with a count: "shell"/"shells", "is"/"are".

    DP-199. Seventy-eight screen strings spelled the plural 'cell(s)',
    'body(ies)', 'cavity(ies)' -- in a sentence that had already counted the
    things. A reader who is told there are three of something does not need
    to be asked to pick the ending; the writer knows the number, and
    parenthesising the ending is the writer declining to use it.

    Most of those sentences put the number immediately before the noun, and
    those take :func:`count_text`. This is for the rest: the ones that name
    the things instead of counting them, where the number is the length of a
    list about to be read out and a second count would say the same thing
    twice -- and the verbs and pronouns further along the same sentence,
    which have to agree with that number as well. A default plural of
    ``singular + 's'`` serves the nouns; a verb passes both forms.

    The comparison is against the count as given, not against ``int(count)``,
    because a fractional quantity is not singular: a box half a diagonal
    larger takes 'diagonals', and truncating 0.5 to 0 or 1.5 to 1 would get
    one of those two wrong.
    """
    return singular if count == 1 else (plural or singular + 's')


def count_text(count, singular: str, plural: str = '') -> str:
    """"1 cell", "1,284 cells" -- the one spelling for "how many".

    DP-175. Three private copies of this function lived in three modules --
    ``_plural`` in the mesh presentation rules, ``_summary`` in the
    display-control view modes and ``_count`` beside the cell counter -- and
    two of them were the same six lines with the first two arguments in
    opposite orders. The places that used none of them wrote the number with
    ``str()`` and got ``Highlighted 1284 failed cell(s)`` in the same status
    bar that says ``skewFaces: 1,284 cells.`` about the same set.

    DP-199 moved it here from the presentation rules, which import from this
    module already, so that the core -- where the other sixty-odd counts are
    written -- can reach it without reaching through the mesh package.
    """
    count = int(count)
    return f'{count:,} {agreeing(count, singular, plural)}'
