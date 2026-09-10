"""Regular-expression patch selectors for ``addLayersControls/layers``.

C31-11. ``layerParameters.C:265-282`` walks the ``layers`` sub-dictionary and
turns *every* key into a ``wordRe`` before asking the boundary mesh which
patches it names::

    const keyType& key = iter().keyword();
    const labelHashSet patchIDs
    (
        boundaryMesh.patchSet(List<wordRe>(1, wordRe(key)))
    );

A ``keyType`` remembers whether it was quoted in the dictionary, and a quoted
one becomes a pattern rather than a literal word -- which is why the writer
quotes the pattern and leaves literal patch names bare. ``patchSet`` matches
patch *groups* as well as patch names, so ``background`` selects all six faces
of the base block.

This module sits in ``core`` rather than beside the writer because the layer
editor's match preview calls it, and a view is not allowed to import from
``foammesh.openfoam`` -- the boundary the release gate enforces.  The rule it
implements is a fact about OpenFOAM's dictionary parser, not about writing a
case, so it belongs on the shared side of that line.

The matching here has to agree with OpenFOAM's, not merely resemble it:
``Foam::regExp::match`` requires the expression to consume the whole name, so
``wall`` does not select ``wall_inlet`` and ``wall.*`` does. That is what
:func:`matching_patches` implements, and it is the same function the editor's
match preview calls -- the preview is only trustworthy if it is the writer's
own rule rather than a second opinion about it.
"""
from __future__ import annotations

import re


class PatternError(ValueError):
    """A patch pattern OpenFOAM could not compile, said in plain words."""


def compile_pattern(pattern: str) -> re.Pattern:
    """The compiled expression, or :class:`PatternError` naming the fault."""
    text = str(pattern or '').strip()
    if not text:
        raise PatternError(
            'a layer group set to match by pattern needs a pattern; an empty '
            'one would select every patch or none, depending on the release')
    if '"' in text:
        raise PatternError(
            f'{text!r} contains a quote character, which ends the dictionary '
            f'key before OpenFOAM ever sees the rest of the expression')
    try:
        return re.compile(text)
    except re.error as error:
        raise PatternError(
            f'{text!r} is not a valid regular expression ({error}); '
            f'OpenFOAM compiles the same expression and stops the run') from error


def matching_patches(pattern: str, names) -> tuple[str, ...]:
    """The names *pattern* selects, in the order they were given.

    Whole-name matching, because that is what ``Foam::regExp::match`` does.
    Raises :class:`PatternError` rather than returning nothing for a pattern
    that cannot compile: "matches no patch" and "is not a pattern" are
    different answers and the editor has to be able to say which.
    """
    expression = compile_pattern(pattern)
    return tuple(str(name) for name in names
                 if expression.fullmatch(str(name)))


def quoted_key(pattern: str) -> str:
    """The dictionary key OpenFOAM reads as a pattern rather than a word."""
    return f'"{compile_pattern(pattern).pattern}"'


#: The ``inGroups`` owner every background-block face is written into, so the
#: faces blockMesh creates are addressable as one named patch group rather
#: than reaching the delivered mesh owned by nothing (R106).
BACKGROUND_PATCH_GROUP = 'background'

#: The names blockMesh gives the background block's six faces, in the order
#: the writer emits them.  They live beside the matcher rather than in the
#: writer because the match preview has to offer exactly the names the writer
#: will create: a preview built from its own second list of names would go on
#: looking right for however long the two lists happened to agree.
BLOCK_FACE_NAMES = ('xMin', 'xMax', 'yMin', 'yMax', 'zMin', 'zMax')
