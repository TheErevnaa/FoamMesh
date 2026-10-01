"""Reviewed map from the AF0 action/service inventory to facade operations.

This is the machine-checkable evidence for the AF3 exit criterion: *every
operation inventory entry has a headless facade handler*. Each shell ``ActionId``
maps to one of:

- a facade operation id (a registered command handler, lifecycle method, or
  presentation operation),
- ``APPLICATION_SETTING`` — handled by ``application.settings.patch``,
- ``CLIENT_SHELL`` — a reviewed client-only control (external app launch, URL,
  about/license dialog, process exit) with no domain side effect, per §7.

Every public service is mapped to the operations that drive it, so the audit
proves no service was left unwired.
"""
from __future__ import annotations

APPLICATION_SETTING = 'application.settings.patch'
CLIENT_SHELL = '<client_shell>'


# ActionId value -> facade operation id / classification.
ACTION_TO_OPERATION: dict[str, str] = {
    # Lifecycle / persistence
    'new_case': 'case.create',
    # Same operation, with the directory chosen by the shell in the temporary
    # location instead of by the user. Nothing about the case differs.
    'new_scratch_case': 'case.create',
    'open_project': 'case.open',
    'close_project': 'case.close',
    'save': 'case.save',
    'save_as': 'case.save',            # save-to-path variant of the same op
    'save_project_as': 'case.save',
    'archive_case': 'case.archive',
    'clean_case': 'case.clean',
    'undo': 'history.undo',
    'redo': 'history.redo',
    # Geometry / mesh input
    'load_geometry': 'geometry.import',
    'load_mesh': 'mesh.import.native',
    # Mesh inspection / QA / transforms / repair / recovery
    'mesh_info': 'mesh.info',
    'mesh_quality': 'quality.report',
    'mesh_check': 'mesh.check',
    'mesh_repair': 'mesh.repair',
    'mesh_restore': 'mesh.restore',
    'mesh_rotate': 'mesh.transform.rotate',
    'mesh_translate': 'mesh.transform.translate',
    'mesh_scale': 'mesh.transform.scale',
    'extrude': 'mesh.extrude',
    # Export
    'export': 'case.export.native',
    # Presentation (view controls)
    'view_fit': 'presentation.view.fit',
    # DP-755. A camera fit bounded by the selection rather than the scene.
    'view_zoom_selection': 'presentation.view.fit',
    'view_axis': 'presentation.view.axis',
    'view_cube_axis': 'presentation.view.cube_axis',
    'view_ruler': 'presentation.view.ruler',
    # DP-757. The operation keeps its public name; the action is named for
    # what it does.
    'view_parallel_projection': 'presentation.view.perspective',
    'view_align_axis': 'presentation.view.align_axis',
    'view_roll': 'presentation.view.roll',
    'view_rotation_center': 'presentation.view.rotation_center',
    # Application settings (own revision domain)
    'parallel_environment': APPLICATION_SETTING,
    'ui_scale': APPLICATION_SETTING,
    'language': APPLICATION_SETTING,
    'preferences': APPLICATION_SETTING,  # DP-759: the Preferences dialog
    # Reviewed client-only shell controls (no domain handler by design)
    'exit': CLIENT_SHELL,               # quits the client process
    'terminal_here': CLIENT_SHELL,      # opens an OS terminal
    'tutorials': CLIENT_SHELL,          # opens documentation in a browser
    'license': CLIENT_SHELL,            # shows an about/license dialog
    'about': CLIENT_SHELL,              # shows the about dialog
    'run_details': CLIENT_SHELL,        # shows the last run sentence and its log
}


# public_service class name -> the facade operations that drive it.
SERVICE_TO_OPERATIONS: dict[str, tuple[str, ...]] = {
    'SelectionService': ('presentation.select', 'presentation.visibility'),
    'EngineSelectionService': ('mesh.engine.list', 'mesh.engine.probe',
                               'mesh.engine.workflow', 'mesh.engine.select',
                               'mesh.target_solver.get',
                               'mesh.target_solver.set'),
    'AuthoredExportService': ('case.export.authored',),
    'CaseService': ('case.classify',),
    'WorkflowTransitionService': ('workflow.status', 'workflow.start_authored',
                                  'workflow.return_to_external'),
    'WorkflowController': ('workflow.run_stage',),
    'CaseArchiveService': ('case.archive',),
    'CaseCleanService': ('case.clean', 'case.clean.preview'),
    'ConverterImportService': ('mesh.import.converter',),
    'FluentMeshExportService': ('case.export.fluent',),
    'FoamFormatConvertService': ('case.export.format_convert',),
    'NativeMeshImportService': ('mesh.import.native',),
    'ImportExportService': ('case.export.entries', 'case.export.native',
                            'case.export.vtk', 'case.export.cgns',
                            'case.export.gmsh', 'case.export.su2',
                            'case.export.med', 'case.export.unv'),
    'JobManager': ('job.test.start', 'job.cancel'),
    'MeshInfoService': ('mesh.info',),
    'MeshRecoveryService': ('mesh.restore', 'mesh.recovery.list'),
    'MeshRepairService': ('mesh.repair', 'mesh.repair.preview',
                          'mesh.repair.recommendations'),
    'SurfaceRepairService': ('geometry.repair', 'geometry.repair.preview',
                             'geometry.repair.apply', 'geometry.prepare.cancel'),
    'GeometryArtifactStore': ('geometry.import', 'geometry.diagnostics', 'geometry.classify',
                              'geometry.split', 'geometry.combine', 'geometry.transform',
                              'geometry.readiness', 'geometry.repair.rollback',
                              'geometry.wrap.estimate',
                              'geometry.patches.split_by_angle',
                              'geometry.split_interfaces'),
    'MeshTransformService': ('mesh.transform.rotate', 'mesh.transform.translate',
                             'mesh.transform.scale'),
    'MeshCheckService': ('mesh.check', 'quality.su2_readiness',
                         'quality.report', 'quality.failed_sets',
                         'quality.failed_set.select', 'quality.compare',
                         'quality.report.export'),
}


# Operation-inventory entries that are not shell ``ActionId`` values: the
# programmatic geometry-page actions and the recent-files / generic menu action.
# Keyed by the entry ``name`` recorded in the AF0 manifest.
SUPPLEMENTAL_TO_OPERATION: dict[str, str] = {
    '_removeAction': 'geometry.items.remove',   # geometry page: remove entity
    'editAction': 'geometry.items.patch',       # geometry page: edit entity
    'capture': CLIENT_SHELL,                    # viewport-only image capture
    'cheatSheet': CLIENT_SHELL,                 # viewport shortcut overlay
    'zoomSelection': CLIENT_SHELL,              # viewport-only camera fit
    'fitSelectionOrAll': CLIENT_SHELL,          # DP-697: the viewport's F key
    'action': CLIENT_SHELL,                     # recent-files / menu container control
    # DP-292 (W-B): the boundary actions on the geometry page are the four
    # patch routes; the host supplies the selection and the angle.
    '_renameAction': 'geometry.patches.rename',
    '_mergeAction': 'geometry.patches.merge',
    '_splitAction': 'geometry.patches.split',
    '_angleAction': 'geometry.patches.split_by_angle',
    # DP-290 (W-E): the Help menu step entries open the help and details
    # panes of the step on screen; the separator between them is a menu line.
    '_stepHelpAction': CLIENT_SHELL,
    '_stepDetailsAction': CLIENT_SHELL,
    'separator': CLIENT_SHELL,
    # Plan 35 CR0: Help > "Open logs folder" and "Create crash report..."
    # show files the diagnostics already wrote; neither touches the case.
    '_openLogsAction': CLIENT_SHELL,
    '_crashReportAction': CLIENT_SHELL,
    # Plan 37: the Farfield row's menu. Edit opens the dialog, which saves
    # through configuration.patch; Remove switches the one record off.
    'farfieldEdit': CLIENT_SHELL,
    'farfieldRemove': 'configuration.patch',
}


def resolve_inventory_entry(name: str, object_name_to_action: dict) -> str | None:
    """Resolve a manifest operation entry name to an operation id / sentinel.

    ``object_name_to_action`` inverts ``ACTION_OBJECT_NAMES`` (Designer object
    name -> ActionId value). Returns ``None`` for a public-service class name,
    which is audited via ``SERVICE_TO_OPERATIONS`` instead.
    """
    if name in ACTION_TO_OPERATION:                       # action_id value
        return ACTION_TO_OPERATION[name]
    if name in object_name_to_action:                     # designer actionX
        return ACTION_TO_OPERATION[object_name_to_action[name]]
    if name in SUPPLEMENTAL_TO_OPERATION:                 # programmatic / generic
        return SUPPLEMENTAL_TO_OPERATION[name]
    if name in SERVICE_TO_OPERATIONS:                     # public_service class
        return None
    return '<unresolved>'


def mapped_operations() -> set[str]:
    """Facade operation ids referenced by the action map (excludes sentinels)."""
    return {value for value in ACTION_TO_OPERATION.values()
            if value not in (APPLICATION_SETTING, CLIENT_SHELL)} | {APPLICATION_SETTING}
