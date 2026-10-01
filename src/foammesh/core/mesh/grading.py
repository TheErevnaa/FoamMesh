"""The grading vocabulary a page may use: sides, presets and what they write.

Plan 37 UF12. A page names where a direction's small cells go and shows what
that choice writes, and it must not reach into the dictionary writer to do
so (views do not import ``foammesh.openfoam``, AF8). The arithmetic lives
with the writer, which is what makes the words and the file agree; this
module is the one door the view side goes through to it.
"""
from __future__ import annotations

from foammesh.openfoam.background_mesh import (
    FINE_BOTH_EDGES,
    FINE_CENTRE,
    FINE_CUSTOM_PROFILE,
    FINE_END,
    FINE_PRESETS,
    FINE_START,
    BackgroundMeshError,
    grading_summary,
    preset_grading,
)
from foammesh.openfoam.background_mesh import _strip_group


def preset_text(fine: str, ratio) -> str:
    """One direction's grading as a user types it, for a side and a ratio.

    Without the outer brackets the dictionary puts round a direction's
    segments: ``(0.5 0.5 0.25) (0.5 0.5 4)`` for Centre at ratio 4.
    """
    return _strip_group(preset_grading(fine, ratio).render())


__all__ = [
    'FINE_BOTH_EDGES', 'FINE_CENTRE', 'FINE_CUSTOM_PROFILE', 'FINE_END',
    'FINE_PRESETS', 'FINE_START', 'BackgroundMeshError', 'grading_summary',
    'preset_grading', 'preset_text',
]
