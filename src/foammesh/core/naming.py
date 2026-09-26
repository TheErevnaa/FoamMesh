"""How a stored name is spelled on screen.

DP-143 wrote this rule for stored *values* -- the options in a combo --
because `value.replace('_', ' ').capitalize()` flattened every acronym and
product name the product does not own. It lived in the view, because that is
where combos live.

DP-163 moved it here, because the other half of the same question is decided
in `core.facade.fields`: the title a field is shown under, generated from its
leaf when nobody authored one. That generator used `str.title()`, which
capitalises every word, while the hundred and thirty authored titles beside it
are written as sentences. So one form could read `Group Name`, `Included
Angle`, `Cell zone side` in three consecutive rows, and the same page could
offer `Max Cpu Cores` above `Keep faceZones whole`. Both halves ask the same
question -- how is this name written for a reader -- so both halves now ask it
here, and a rule added for one is a rule the other keeps.

Two things are spelled against the words, in this order:

* a run of letters that is an acronym or an axis, which keeps its capitals
  (`occ_parallel` is `OCC parallel`, `num_cells_x` is `Num cells X`);
* a name that belongs to someone else -- OpenFOAM, SU2, the Gmsh algorithms,
  the decomposition libraries, Delaunay.

Everything else is sentence case, which is how the field documentation
already writes these ("As imported"), so the label and the help text agree.
"""
from __future__ import annotations

import re


#: Whole values whose spelling belongs to someone else. Keyed by the stored
#: value, lower-cased. A value that is merely a word -- `wall`, `inlet`,
#: `discard` -- is not in here, because sentence case already spells it.
EXACT_NAMES: dict[str, str] = {
    # The two solvers this product writes meshes for.
    'openfoam': 'OpenFOAM',
    'su2': 'SU2',
    # DP-226. The two meshers this product drives, each spelled the
    # way its own project spells it. `snappyHexMesh` is the OpenFOAM
    # utility, which is what the capability probe asks for by name
    # and what every tooltip mentioning the mesher already says.
    'gmsh': 'Gmsh',
    'snappy': 'snappyHexMesh',
    # Gmsh's own names for its algorithms and quality measures.
    'meshadapt': 'MeshAdapt',
    'hxt': 'HXT',
    'mmg3d': 'MMG3D',
    'sicn': 'SICN',
    'sige': 'SIGE',
    # The decomposition libraries OpenFOAM can be pointed at.
    'metis': 'METIS',
    'kahip': 'KaHIP',
    # Dimensionality, which nobody writes out in words.
    'two_d': '2D',
    'three_d': '3D',
}

#: Single words that keep their capital wherever in a label they land, so
#: that `frontal_delaunay` reads `Frontal Delaunay` and not `Frontal delaunay`.
PROPER_WORDS: dict[str, str] = {
    'delaunay': 'Delaunay',
    'blossom': 'Blossom',
    'scotch': 'Scotch',
}

#: DP-163. Words that are initials rather than words, so sentence case would
#: lower-case something that is not a word. Only the ones the schema actually
#: uses are here: `Max Cpu Cores` and `Occ Parallel` were the two the title
#: generator produced.
ACRONYMS: frozenset = frozenset({
    'cpu', 'occ', 'obj', 'vtk', 'stl', 'cad', 'msh', 'qa',
})

#: DP-163. The three axis letters, which are names rather than words and are
#: written as capitals everywhere else in the product -- the background block
#: is divided `Num cells X`, not `num cells x`. A single letter that is *not*
#: an axis is left alone, because the one the schema has is the `m` of
#: `qualification_tolerance_m`, which is a unit and not a name.
AXIS_LETTERS: frozenset = frozenset({'x', 'y', 'z'})

#: `asImported` -> `as Imported`, and `HXTMesh` -> `HXT Mesh`. Both halves
#: need a lower-case neighbour, which is what keeps a run of capitals together
#: rather than spacing out every letter of an acronym -- and what leaves an
#: ALL_CAPS value such as `workflow.step`'s `BASE_GRID` alone, since it has
#: no lower-case character anywhere to seam against.
_CASE_SEAM = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')


def split_words(value) -> list[str]:
    """The value as a list of words, each still spelled as it was stored."""
    text = _CASE_SEAM.sub(' ', str(value))
    return [word for word in text.replace('_', ' ').split() if word]


def _spell(word: str, first: bool) -> str:
    low = word.lower()
    if low in AXIS_LETTERS or low in ACRONYMS:
        return low.upper()
    proper = PROPER_WORDS.get(low)
    if proper:
        return proper
    if first:
        return low[0].upper() + low[1:]
    return low


def humanise_option(value) -> str:
    """The label one stored name is shown under."""
    text = str(value)
    exact = EXACT_NAMES.get(text.lower())
    if exact:
        return exact
    words = split_words(text)
    if not words:
        return ''
    return ' '.join(_spell(word, index == 0)
                    for index, word in enumerate(words))
