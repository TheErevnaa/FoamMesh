"""Gmsh workflow task pages.

Each page is the engine-neutral :class:`EngineTaskPage` bound to one task; the
guided and advanced fields are rendered from the facade's field registry, so a
page cannot show a control the schema does not publish. Only two pages add
anything of their own: the collection editors, and the boundary-layer page's
statement of scope.
"""

from .boundary_layers_page import GmshBoundaryLayersPage
from .compute_page import GmshComputePage
from .curve_controls_page import GmshCurveControlsPage
from .describe_page import GmshDescribePage
from .global_sizing_page import GmshGlobalSizingPage
from .periodic_page import GmshPeriodicPage
from .publish_page import GmshPublishPage
from .qa_page import GmshQaPage
from .size_fields_page import GmshSizeFieldsPage
from .volume_controls_page import GmshVolumeControlsPage

from foammesh.view.export_task_page import GmshExportPage

from .qualification_pages import GMSH_QUALIFICATION_PAGES

#: Task id -> page class, mounted by the engine branch.
# Plan 23 WP7B. The `common.*` qualification pages are shared with snappy --
# same task, same evidence, same words -- so both registries name the same
# classes rather than each carrying a copy that could drift.
GMSH_TASK_PAGES = {
    **GMSH_QUALIFICATION_PAGES,
    'gmsh.describe_geometry': GmshDescribePage,
    'gmsh.global_sizing': GmshGlobalSizingPage,
    'gmsh.size_fields': GmshSizeFieldsPage,
    'gmsh.curve_controls': GmshCurveControlsPage,
    'gmsh.volume_controls': GmshVolumeControlsPage,
    'gmsh.boundary_layers': GmshBoundaryLayersPage,
    'gmsh.periodic': GmshPeriodicPage,
    'gmsh.compute': GmshComputePage,
    'gmsh.publish': GmshPublishPage,
    'gmsh.qa': GmshQaPage,
    # Plan 30 WP-09 (F-17). Declared by both engines and registered by
    # neither: the last row of the workflow was routed from the legacy step
    # table, so the branch skipped it on Gmsh too.
    'common.export': GmshExportPage,
}

__all__ = [
    'GMSH_QUALIFICATION_PAGES',
    'GMSH_TASK_PAGES',
    'GmshBoundaryLayersPage',
    'GmshComputePage',
    'GmshExportPage',
    'GmshCurveControlsPage',
    'GmshDescribePage',
    'GmshGlobalSizingPage',
    'GmshPeriodicPage',
    'GmshPublishPage',
    'GmshQaPage',
    'GmshSizeFieldsPage',
    'GmshVolumeControlsPage',
]
