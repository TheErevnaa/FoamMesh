"""How a stored value is spelled on screen.

DP-143 wrote the rule here, for the combos this package builds. DP-163 moved
it to `foammesh.core.naming`, because the title a field is shown under is
generated in `core.facade.fields` and was being spelled by a different rule --
`str.title()` -- so a form could read `Max Cpu Cores` above `Keep faceZones
whole`. The one rule now answers both. This module stays as the name the view
already imports it by.
"""
from __future__ import annotations

from foammesh.core.naming import (
    ACRONYMS, AXIS_LETTERS, EXACT_NAMES, PROPER_WORDS, humanise_option,
    split_words)

__all__ = ['ACRONYMS', 'AXIS_LETTERS', 'EXACT_NAMES', 'PROPER_WORDS',
           'humanise_option', 'split_words']
