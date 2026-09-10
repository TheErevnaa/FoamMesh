"""Names for geometry that a person can match to a part.

E12. The castellation surface picker listed a part as
``surface_312c656294824f14bab0582dfece8303``. That string is how the prepared
surface is keyed on disk; it is not anything the user chose, and with three
such rows there is no way to tell which one is the pipe wall. The file the
surface came from and the volume it belongs to are, so build the label out of
those and keep the raw key where it is still useful -- in the tooltip.
"""
from __future__ import annotations

import re
from pathlib import Path

#: ``surface_<hex>``, ``solid-1a2b3c`` and friends: a key, not a name.
_OPAQUE = re.compile(
    r'^(?:surface|solid|patch|face|part)[_-]?([0-9a-f]{8,})$', re.IGNORECASE)


def is_opaque_name(name) -> bool:
    """Whether this name identifies a file rather than a part."""
    return bool(_OPAQUE.match(str(name or '').strip()))


def readable_geometry_name(name, *, path=None, parent=None) -> str:
    """A label for ``name``, falling back to the file and volume it came from.

    A name the user can read is returned unchanged -- this only rewrites the
    opaque ones.
    """
    name = str(name or '').strip()
    match = _OPAQUE.match(name)
    if match is None:
        return name
    digest = match.group(1)[:6]
    stem = Path(str(path)).stem if path else ''
    label = ' / '.join(part for part in (str(parent or ''), stem) if part)
    return f'{label} ({digest})' if label else f'surface {digest}'
