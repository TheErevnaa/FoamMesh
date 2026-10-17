"""Names OpenFOAM reads as words (shared by the writer and the pages)."""
from __future__ import annotations


def cell_zone_word(name) -> str:
    """The cell zone name a volume called *name* is written as.

    CHT1. OpenFOAM's tokeniser reads a leading digit as a number:
    `cellZone 02_pipe_solid_r10to12mm_L500mm;` died at castellation on
    `wrong token type - expected word, found ... integer 2`, MEASURED live on
    the issue's files, whose volumes are named after them. The name is kept
    and made a word, not refused: a file name is not something a user chose
    as a zone name. CHT2 item 7: the Domain & regions page shows this
    name beside the volume's own, so it is one function for both.
    """
    name = str(name or '')
    return f'zone_{name}' if name and not name[0].isalpha() else name
