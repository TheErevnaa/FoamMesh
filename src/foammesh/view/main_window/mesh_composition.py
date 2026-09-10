"""What the viewport is counting, in the mesh's own vocabulary.

Plan 31 CP-09 item 6: "show visible/total cells, named regions and boundary
patches instead of ambiguous render-actor counts; label slice-only versus
whole-mesh statistics."

CP-02 got the first half: the parts chip counts patches and zones rather than
props, and the cell readout shows visible over total. Two things were still
missing, and both are here.

*Names.* "9 of 11 mesh parts shown" says how many, never which, and never that
they are two different kinds of thing. A user asking "did my inlet survive"
is asking about a boundary patch by name, and a multi-region case has no way
at all to see its regions -- ``MeshManager.regions()`` has existed since the
scene was built and nothing reads it.

*Which mesh a number describes.* "12,004 / 39,921 shown" is honest about the
fraction and silent about the consequence: every quality statistic taken
while a section is cut describes the section. The readout says so.

The formatting is here, apart from Qt, because it is the part worth measuring
and because two surfaces (the overlay chip and the status-bar cell counter)
have to say the same thing about the same mesh.
"""
from __future__ import annotations

#: How many names to spell out before the summary gives up and counts.
#: Eleven patches listed in a tooltip is a wall; four and "and 7 more" is a
#: sentence.
_NAMES_SHOWN = 4


def _count(number: int, singular: str, plural: str = '') -> str:
    plural = plural or singular + 's'
    return f'{number:,} {singular if number == 1 else plural}'


def _listed(names) -> str:
    names = [str(name) for name in names if str(name).strip()]
    if not names:
        return ''
    if len(names) <= _NAMES_SHOWN:
        return ', '.join(names)
    rest = len(names) - _NAMES_SHOWN
    return ', '.join(names[:_NAMES_SHOWN]) + f' and {rest:,} more'


def composition_text(regions=(), patches=(), zones=()) -> str:
    """One sentence naming what this mesh is made of.

    Empty when there is nothing to say, so a caller can hide the line rather
    than show "0 boundary patches" over a geometry that has none because it
    is not a mesh.
    """
    patches = list(patches)
    zones = list(zones)
    regions = list(regions)
    parts = []
    if patches:
        parts.append(_count(len(patches), 'boundary patch', 'boundary patches')
                     + f' ({_listed(patches)})')
    if zones:
        parts.append(_count(len(zones), 'cell zone') + f' ({_listed(zones)})')
    if not parts:
        return ''
    text = '; '.join(parts)
    if regions:
        text += (f', in {_count(len(regions), "region")} '
                 f'({_listed(regions)})')
    return text + '.'


def cell_count_text(visible: int, total: int = 0) -> tuple[str, str]:
    """``(label, tooltip)`` for the cell counter.

    ``total`` is the whole mesh; ``visible`` is what survives the section.
    When they differ the label says which number is which and the tooltip
    says what that means for anything measured on screen -- CP-09 item 6's
    "label slice-only versus whole-mesh statistics", which is the difference
    between "the worst cell in my mesh" and "the worst cell in this slice".
    """
    visible = max(int(visible), 0)
    total = max(int(total), visible)
    if not total:
        # `_clear` calls this with one argument on teardown. "0 cells" over an
        # empty viewport is a claim about a mesh that is not there; the bare
        # zero the readout has always shown is not.
        return '0', ''
    if visible < total:
        return (
            f'Section: {visible:,} of {total:,} cells',
            f'A section is hiding {total - visible:,} of the {total:,} cells '
            f'in this mesh.\nAnything measured on screen now -- the quality '
            f'range, the worst-cell colouring -- describes the '
            f'{visible:,}-cell section, not the whole mesh.')
    return (f'Whole mesh: {total:,} cells',
            'No section is cut: this is every cell in the mesh, and '
            'statistics taken here describe all of it.')
