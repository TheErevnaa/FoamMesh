"""Gmsh workflow page: gmsh.curve_controls."""
from __future__ import annotations

from .base import GmshTaskPage


class GmshCurveControlsPage(GmshTaskPage):
    """The curve task, whose controls are edited on the sizing step.

    Plan 33 CURVE-06. The task is still a task: the runner reads
    `gmsh/curveControls`, the engine declares it, and the one press on
    `Size fields` settles it after the size-field task. What it no longer
    has is a row and a page of its own, because the question it answers --
    where is the mesh finer than the global size -- is the question the
    sizing step asks, and the table was empty on almost every case.

    The class stays because every engine task has a page (`GMSH_TASK_PAGES`
    is keyed by task id and gate 1 asserts the two sets are equal), and
    because the outline still walks the task as a substep. The page draws
    nothing, the way `gmsh.describe_geometry` does: what it would have
    drawn is drawn where the reader is.
    """

    task_id_default = 'gmsh.curve_controls'

    #: What the sizing step shows of a row. Left here rather than moved so
    #: that the columns are named beside the task that owns the collection.
    COLUMNS = ('name', 'enabled', 'scope_token', 'mode', 'segments', 'law',
               'coefficient', 'priority')

    def build_sections(self, layout) -> None:
        return None
