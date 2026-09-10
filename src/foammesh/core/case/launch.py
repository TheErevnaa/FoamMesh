"""Startup-argument handling that does not depend on Qt initialization."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence


def startup_case_path(arguments: Sequence[str]) -> Path | None:
    """Return the optional positional case directory passed to FoamMesh.

    Qt and deployment wrappers may add their own switches.  FoamMesh accepts a
    case only as the first positional argument, so a leading option never gets
    mistaken for a path and an invalid request remains recoverable in the main
    window after it is visible.
    """
    if not arguments or arguments[0].startswith('-'):
        return None
    return Path(arguments[0]).expanduser()
