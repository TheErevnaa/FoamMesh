"""Gmsh workflow task pages.

Each page is the engine-neutral :class:`EngineTaskPage` bound to one task; the
guided and advanced fields are rendered from the facade's field registry, so a
page cannot show a control the schema does not publish. Only two pages add
anything of their own: the collection editors, and the boundary-layer page's
statement of scope.
"""

# Plan 45 start-up. The pages are imported the first time one of the names
# below is asked for, not with the package: the Mesh quality tab and the
# command palette import modules of this package while the window is built,
# and importing every page with them held the GUI thread ~0.04 s (MEASURED
# live 18 October 2026). The imports stay written out so the package build
# still finds them.


def _load_pages():
    from .boundary_layers_page import GmshBoundaryLayersPage
    from .compute_page import GmshComputePage
    from .curve_controls_page import GmshCurveControlsPage
    from .describe_page import GmshDescribePage
    from .geometry_page import GmshGeometryPage
    from .global_sizing_page import GmshGlobalSizingPage
    from .periodic_page import GmshPeriodicPage
    from .publish_page import GmshPublishPage
    from .qa_page import GmshQaPage
    from .quality_gate_page import GmshQualityGatePage
    from .size_fields_page import GmshSizeFieldsPage
    from .sizing_page import GmshSizeFieldsHostedPage, GmshSizingPage
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
        # Plan 41 GN18 (section 4.1): the Geometry row is a task of its own, and
        # the Sizing row's page draws the size-field task's controls as well.
        'gmsh.geometry': GmshGeometryPage,
        'gmsh.global_sizing': GmshSizingPage,
        'gmsh.size_fields': GmshSizeFieldsHostedPage,
        'gmsh.curve_controls': GmshCurveControlsPage,
        'gmsh.volume_controls': GmshVolumeControlsPage,
        'gmsh.boundary_layers': GmshBoundaryLayersPage,
        'gmsh.periodic': GmshPeriodicPage,
        'gmsh.compute': GmshComputePage,
        'gmsh.publish': GmshPublishPage,
        'gmsh.qa': GmshQualityGatePage,
        # Plan 30 WP-09 (F-17). Declared by both engines and registered by
        # neither: the last row of the workflow was routed from the legacy step
        # table, so the branch skipped it on Gmsh too.
        'common.export': GmshExportPage,
    }
    globals().update({name: value for name, value in locals().items()
                      if name in __all__})


def __getattr__(name):
    if name in __all__:
        _load_pages()
        return globals()[name]
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


__all__ = [
    'GMSH_QUALIFICATION_PAGES',
    'GMSH_TASK_PAGES',
    'GmshBoundaryLayersPage',
    'GmshComputePage',
    'GmshExportPage',
    'GmshGeometryPage',
    'GmshCurveControlsPage',
    'GmshDescribePage',
    'GmshGlobalSizingPage',
    'GmshPeriodicPage',
    'GmshPublishPage',
    'GmshQaPage',
    'GmshQualityGatePage',
    'GmshSizeFieldsHostedPage',
    'GmshSizeFieldsPage',
    'GmshSizingPage',
    'GmshVolumeControlsPage',
]
