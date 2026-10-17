"""Gmsh workflow page: gmsh.geometry, the first row after Preparation.

Plan 41 GN18 (section 4.1, row 1). The prepared revision is the start and
Preparation (shared with the other engine, unchanged) is where it is imported,
named and repaired. This row is what only the Gmsh route does on top of it:
build entities and primitives, transform, extrude and combine them, merge a
file Gmsh alone reads, pick entities and name physical groups. Every one of
those is an operation on the resident session and is picked in the viewport.

The row asks nothing it stores: the operations are the session log, so the
task carries no field. It is optional, and a case that builds nothing here
records a skip with one press.
"""
from __future__ import annotations

from .base import GmshTaskPage
from .session_tiers import SessionTiers


class GmshGeometryPage(SessionTiers, GmshTaskPage):
    task_id_default = 'gmsh.geometry'

    def build_sections(self, layout) -> None:
        self.mountEntityTier(layout)
        self.mountHealingTier(layout)
