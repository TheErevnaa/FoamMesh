#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One unit ladder for every length the viewport readout says out loud.

DP-106. The extent half of the scale readout has always carried a unit --
``300 mm across`` -- and the base-cell half beside it carried none, so
``base cell 0.4111 x 0.4059 x 0.5161`` could have been metres or
millimetres and nothing on screen said which. Two numbers that are meant
to be compared have to be in the same sentence *and* the same unit, so
both halves are formatted here.

DP-165. The ladder moved to ``foammesh.core.quantities`` so that the
sentence beside the picture reads off the same table as the readout above
it, and so that a triple meant to be compared is printed to one precision
rather than to three.
"""

from foammesh.core import display_unit
from foammesh.core.quantities import aligned, format_group, unit_for

__all__ = ['aligned', 'format_extent', 'format_lengths', 'unit_for']


def format_extent(extent: float) -> str:
    """A model size a user can sanity-check at a glance.

    Unit mistakes are the most common silent error in a meshing workflow and
    cost a whole run to find out about.
    """
    if extent <= 0:
        return ''
    if display_unit.is_chosen():
        # Plan 40 UXB3 (#58): the project display unit, not a guess per number.
        scale, suffix = 1.0 / display_unit.metres_per_unit(), display_unit.current()
    else:
        scale, suffix = unit_for(extent)
    return f'{extent * scale:.4g} {suffix} across'


def format_lengths(values) -> str:
    """Several lengths in one unit, chosen from the largest of them.

    One unit for the whole triple rather than one per component: a base cell
    written ``0.4 m x 410 mm x 52 cm`` is three numbers nobody can compare.
    """
    lengths = [float(value) for value in values]
    if not lengths or min(lengths) <= 0:
        return ''
    if display_unit.is_chosen():
        body = ' × '.join(aligned(
            length / display_unit.metres_per_unit() for length in lengths))
        return f'{body} {display_unit.current()}'
    return format_group(lengths)
