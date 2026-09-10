"""Snappy workflow task pages for the engine branch.

Plan 26 WP5. ``SNAPPY_WORKFLOW`` declares thirteen tasks and the tree mapped
**eight**, over six distinct widgets -- two pairs doubled up, and five tasks
never reached the tree at all. The five were all qualification tasks, four of
them ``run_gated`` and three ``accepts_override``, and every one of them gates
``common.export`` through ``depends_on``. So a snappy user met an export
refusal produced by nodes that were never rendered, could not be run, and
could not be waived.

Plan 30 WP-09 (F-17) finishes the job. The six tasks this module deliberately
left to the legacy step machine -- domain regions, base grid, castellation,
snap, layers and export -- are registry pages here now. Two page systems meant
two refresh paths, two Run idioms and a routing table in ``StepManager`` that
had to be kept in step with the workflow descriptor by hand; the branch view
skipped every task it named, so the one code path that knows what the engine
declares could not see half of the engine. There is one system now.
"""
from __future__ import annotations

from foammesh.view.export_task_page import SnappyExportPage

from .base import SnappyTaskPage
from .base_grid_page import SnappyBaseGridPage
from .castellation_page import SnappyCastellationPage
from .domain_regions_page import SnappyDomainRegionsPage
from .layers_page import SnappyLayersPage
from .qa_page import SnappyQaPage
from .qualification_pages import SNAPPY_QUALIFICATION_PAGES
from .snap_page import SnappySnapPage
from .surface_features_page import SnappySurfaceFeaturesPage

#: Task id -> page class, mounted by the engine branch. Every task
#: ``SNAPPY_WORKFLOW`` declares has an entry, so ``EngineBranchView._rebuild``
#: skips nothing.
SNAPPY_TASK_PAGES = {
    **SNAPPY_QUALIFICATION_PAGES,
    'snappy.domain_regions': SnappyDomainRegionsPage,
    'snappy.base_grid': SnappyBaseGridPage,
    'snappy.surface_features': SnappySurfaceFeaturesPage,
    'snappy.castellation': SnappyCastellationPage,
    'snappy.snap': SnappySnapPage,
    'snappy.layers': SnappyLayersPage,
    'snappy.qa': SnappyQaPage,
    'common.export': SnappyExportPage,
}

__all__ = [
    'SNAPPY_QUALIFICATION_PAGES',
    'SNAPPY_TASK_PAGES',
    'SnappyBaseGridPage',
    'SnappyCastellationPage',
    'SnappyDomainRegionsPage',
    'SnappyExportPage',
    'SnappyLayersPage',
    'SnappyQaPage',
    'SnappySnapPage',
    'SnappySurfaceFeaturesPage',
    'SnappyTaskPage',
]
