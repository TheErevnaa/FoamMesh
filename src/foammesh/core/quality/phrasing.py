"""The sentences the product uses about a case that has no mesh.

DP-107. Three surfaces answered the same question in three vocabularies,
because each was written by the work package that needed it. MEASURED on
one frame of snappy ``ball_valve``: the verdict strip said ``No mesh
yet.`` while the Mesh quality tab a centimetre below it said ``No mesh has
been produced in this case yet.`` A reader seeing two different sentences
that close together has to work out whether they are two facts or one, and
the work is wasted every time, because they are always one.

The sentence lives in core rather than in a widget for the same reason
:mod:`foammesh.core.quality.verdict` does: which words the product uses
for a state is a decision about the product, not about a label.

It is deliberately *not* the answer to every mesh-shaped absence. "No mesh
is loaded" (the viewport has nothing drawn), "No mesh has been measured
yet" (a mesh exists and has not been checked) and this one (the case has
produced no mesh at all) are three different facts, and collapsing them
would trade a cosmetic duplication for a wrong sentence.
"""
from __future__ import annotations

#: The one sentence for "this case has produced no mesh".
NO_MESH_YET = 'No mesh has been produced in this case yet.'


def no_mesh_yet(consequence: str = '') -> str:
    """*NO_MESH_YET*, optionally with what follows from it.

    A refusal says what cannot happen and what to do instead, so the view
    modes append their own clause rather than writing their own opening.
    """
    if not consequence:
        return NO_MESH_YET
    return f'{NO_MESH_YET[:-1]}, so {consequence}'
