"""Snappy workflow page: ``snappy.snap``.

Plan 30 WP-09 / F-17. Every key the legacy Designer page wrote --
``nSmoothPatch``, ``nSolveIter``, ``nRelaxIter``, ``nFeatureSnapIter``,
``implicitFeatureSnap``, ``explicitFeatureSnap``, ``multiRegionFeatureSnap``
and ``tolerance`` -- is declared on this task, so the form is the descriptor's.
The two feature-snap switches are the pair WP-11 (F-41) split out of the old
``featureSnapType`` enum: OpenFOAM 13 reads them independently and accepts both
on, which the enum could never express.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from .base import SnappyTaskPage


class SnappySnapPage(SnappyTaskPage):
    """Pull the castellated mesh onto the surface it was cut from."""

    task_id_default = 'snappy.snap'
    run_stage = 'snap'

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'Snapping moves the castellated boundary onto the geometry. '
            'Implicit and explicit feature snapping are independent switches: '
            'implicit infers creases from the surface, explicit uses the '
            'extracted feature edges, and multi-region feature snapping is '
            'read by the explicit snapper only.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)
