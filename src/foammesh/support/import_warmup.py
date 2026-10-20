"""DP-2817. The modules the window imports on first use, imported early off
the GUI thread.

MEASURED (CASEX walks, plans/evidence/plan46/agents/case): the two largest GUI-thread
holds of the round-2 two-engine walk were imports, 11.15 s and 3.7 s, every
sample inside importlib's ``get_data`` -- the .pyc read itself on a cold or
busy disk (the 3.7 s one was ``foammesh.rendering.solid_volumes`` from
``volume_controls_page.solidVolumes`` on first use). The round-3 walk with an
import recorder (walk-20261020-r3a-stalls_imports.json) found 80 imports the
GUI thread made after start-up, 0.43 s in all on a warm disk, 0.148 s the
largest (matplotlib for a colour map). This list is those modules, in the order
the walk first needed them; each import pulls in the rest of its chain.

After the window is up, `start()` imports them on one daemon thread, one at a
time, so when a page asks for one the GUI thread finds it in ``sys.modules``.
Every module here was checked to make no Qt object when imported (it only
defines classes and functions), so importing it off the GUI thread is safe.
A module that fails to import is skipped and logged at debug level: the GUI
thread's own import later says why, as it always did.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

#: Off for a run that asks (tests, profiling): ``FOAMMESH_NO_IMPORT_WARMUP=1``.
DISABLE_ENV = 'FOAMMESH_NO_IMPORT_WARMUP'

#: In the order the round-3 walk first imported them on the GUI thread.
WARM_MODULES: tuple[str, ...] = (
    'matplotlib',
    'foammesh.core.jobs.case_lease',
    'foammesh.core.jobs.run_records',
    'foammesh.core.jobs.unlock_transaction',
    'foammesh.core.workflow',
    'foammesh.core.workflow.checkpoints',
    'foammesh.core.workflow.controller',
    'foammesh.core.workflow.dynamic',
    'foammesh.core.workflow.graph',
    'foammesh.core.workflow.snap_checkpoint',
    'foammesh.core.workflow.task_state_store',
    'foammesh.core.jobs.redistribute_transaction',
    'foammesh.rendering.seed_gizmo',
    'foammesh.core.mesh.farfield_spec',
    'foammesh.core.geometry.patches.manifest',
    'foammesh.view.widgets.link_button',
    'foammesh.view.geometry.cad_page',
    'foammesh.core.geometry.interface_contact',
    'foammesh.view.geometry.interface_pair_panel',
    'foammesh.view.meshing_method',
    'foammesh.view.meshing_method.method_page',
    'foammesh.view.meshing_method.size_page',
    'foammesh.view.workflow_controls.field_group_page',
    'foammesh.view.workflow_controls.warning_text',
    'foammesh.core.geometry.boundary_roles',
    'foammesh.core.gmsh.execution',
    'foammesh.core.gmsh.manifest',
    'foammesh.core.import_export.authored',
    'foammesh.core.import_export.region_zones',
    'foammesh.core.mesh.background_estimate',
    'foammesh.core.mesh.grading',
    'foammesh.core.quality.readout',
    'foammesh.openfoam.constant',
    'foammesh.openfoam.constant.region_properties',
    'foammesh.openfoam.system',
    'foammesh.openfoam.system.collapse_dict',
    'foammesh.core.sizing',
    'foammesh.core.sizing.derive',
    'foammesh.core.presets',
    'foammesh.core.presets.physics',
    'foammesh.core.execution',
    'foammesh.core.execution.dag',
    'foammesh.core.execution.resources',
    'foammesh.core.mesh.stage_strip',
    'foammesh.core.case.workflow',
    'foammesh.core.facade.mesh_presence',
    'foammesh.core.facade.confirmation',
    'foammesh.core.mesh.exclude_points',
    'foammesh.core.mesh.snappy_farfield',
    'vtkmodules.vtkIOXML',
    'vtkmodules.vtkIOXMLParser',
    'foammesh.core.engine.selection',
    'foammesh.view.sizing',
    'foammesh.view.sizing.badges',
    'foammesh.core.openfoam_runtime.detect',
    'foammesh.core.sizing.features',
    'foammesh.core.sizing.field_model',
    'foammesh.core.sizing.field_runner',
    'foammesh.core.sizing.gmsh_mapping',
    'foammesh.core.sizing.inputs',
    'foammesh.core.sizing.proposals',
    'foammesh.core.sizing.service',
    'foammesh.core.sizing.snappy_mapping',
    'foammesh.core.sizing.staging',
    'foammesh.core.sizing.tool_setup',
    'foammesh.view.workflow_controls.lock_refusal',
    'foammesh.core.quality.geometry_fidelity.tolerance_source',
    'foammesh.view.sizing.automatic_sizing_card',
    'foammesh.view.snappy_workflow.sheet_folds',
    'foammesh.core.mesh.emesh_reader',
    'foammesh.core.quality.summary',
    'foammesh.view.gmsh_workflow.layer_stacks_panel',
    'foammesh.core.sizing.preview',
    'foammesh.core.mesh.fluid_regions',
    'foammesh.rendering.region_volume_actor',
    'foammesh.core.mesh.fluid_spaces',
    'foammesh.core.quality.waiver',
    'foammesh.core.facade.quality_routes',
    'foammesh.core.quality.check_artifacts',
    'foammesh.core.mesh.seed_classifier',
    'vtkmodules.vtkImagingGeneral',
    'foammesh.core.geometry.occupancy',
    'foammesh.view.workflow_controls.configured',
    'foammesh.core.jobs.run_listing',
    'foammesh.core.jobs.local_worker',
    'foammesh.core.mesh.transform_record',
    'foammesh.core.mesh.poly_mesh_binary',
    'foammesh.core.mesh.poly_mesh_lease',
    'foammesh.core.mesh.poly_mesh_topology',
    'foammesh.core.quality.retained_regions',
    'foammesh.view.mesh_check_action',
    'foammesh.core.import_export.overwrite',
    'foammesh.core.quality.export_authorization',
    'foammesh.core.gmsh.export_writers',
    'foammesh.core.engine.interface_intent',
    'foammesh.view.gmsh_workflow.options_panel',
    'foammesh.view.gmsh_workflow.generate_panel',
    'foammesh.core.gmsh.plugin_catalogue',
    'foammesh.view.gmsh_workflow.plugins_panel',
    'foammesh.core.gmsh.session_entities',
    'foammesh.view.gmsh_workflow.display_panel',
    'foammesh.core.gmsh.session_generate',
    'foammesh.core.selection.pick',
    'foammesh.view.gmsh_workflow.entity_panel',
    'foammesh.view.gmsh_workflow.shared_pick',
    'foammesh.core.gmsh.geometry_build',
    'foammesh.view.gmsh_workflow.geometry_build_panel',
    'foammesh.core.gmsh.import_formats',
    'foammesh.view.gmsh_workflow.import_panel',
    'foammesh.view.workflow_controls.scope_pick',
    'foammesh.view.gmsh_workflow.point_sizes_panel',
    'foammesh.view.gmsh_workflow.native_fields_panel',
    'foammesh.view.gmsh_workflow.entity_controls_panel',
    'foammesh.core.gmsh.structured_grid',
    'foammesh.view.gmsh_workflow.structure_panel',
    'foammesh.core.quality.su2_readiness',
    'foammesh.view.gmsh_workflow.quality_limits_panel',
    'foammesh.core.gmsh.export_service',
    'foammesh.core.gmsh.export_verify',
    'foammesh.view.export.gmsh_writer_panel',
    'foammesh.core.gmsh.script_export',
    'foammesh.view.gmsh_workflow.script_panel',
    'foammesh.core.sizing.structure',
    'foammesh.rendering.solid_volumes',
    'foammesh.core.mesh.cad_solids',
    'vtkmodules.vtkFiltersHybrid',
    'foammesh.core.sizing.background',
    'foammesh.core.gmsh.publish',
    'foammesh.core.gmsh.run_mesh',
    'foammesh.core.jobs.session_actions',
    # The engine branch's own chain: the round-3 recorder kept only each
    # import's first 12 new modules, so the re-walk with this list found it
    # left (0.019 s, 20 modules) when the first Gmsh page mounted.
    'foammesh.view.main_window.engine_branch',
)

_started: threading.Thread | None = None
_report: dict = {}


def report() -> dict:
    """What the last warm-up did: ``imported``, ``failed``, ``seconds``,
    ``done``."""
    return dict(_report)


def _warm(modules) -> None:
    started = time.perf_counter()
    imported, failed = 0, []
    for name in modules:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
            imported += 1
        except Exception:                                   # noqa: BLE001
            failed.append(name)
            logger.debug('warm-up import of %s failed', name, exc_info=True)
        # Hand the interpreter back between modules: the GUI thread waits
        # for at most one module's execution, never the list.
        time.sleep(0)
    _report.update(imported=imported, failed=failed, done=True,
                   seconds=round(time.perf_counter() - started, 3))
    logger.info('Import warm-up: %d modules in %.2f s off the GUI thread%s',
                imported, _report['seconds'],
                f' ({len(failed)} failed)' if failed else '')


def start(modules=WARM_MODULES) -> threading.Thread | None:
    """Import ``modules`` on a daemon thread; once per process."""
    global _started
    if _started is not None or os.environ.get(DISABLE_ENV) == '1':
        return _started
    _report.clear()
    _report.update(imported=0, failed=[], done=False, seconds=0.0)
    _started = threading.Thread(target=_warm, args=(tuple(modules),),
                                name='import-warmup', daemon=True)
    _started.start()
    return _started
