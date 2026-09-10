"""Snappy's binding of the engine-neutral task page.

Plan 30 WP-09 / F-17. Six snappy tasks were served by Designer widgets routed
from a hand-written table in ``StepManager``, so the branch view skipped them
and the app carried two page systems, two refresh paths and two Run idioms for
one workflow. These pages are the same ``EngineTaskPage`` every Gmsh task
already uses, bound to snappy: the guided and advanced forms are built from the
engine's own workflow descriptor through the AF2 registry, so a field the
schema does not publish cannot appear and a field the descriptor does publish
cannot be quietly dropped.
"""
from __future__ import annotations

from foammesh.view.workflow_controls.task_page import EngineTaskPage


class SnappyTaskPage(EngineTaskPage):
    """One snappy workflow task, rendered from its descriptor."""

    engine_id = 'snappy'
    #: Snappy has no single whole-pipeline task page; "Run to end" lives on
    #: the branch row (§7.1), so no page claims the pipeline button.
    run_all_task_id = None
    task_id_default = ''

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)
