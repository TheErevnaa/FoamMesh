"""AF4 bridge: derive the shell ``AppSnapshot`` from the facade snapshot.

``ActionPolicy`` is the single source of truth for menu/action availability. In
the facade-client GUI it must be driven from facade state rather than ad-hoc
widget inspection, so menus reflect exactly what the facade will accept. This
module builds an ``AppSnapshot`` from a facade case snapshot plus the shared
job/capability state. It imports no Qt.
"""
from __future__ import annotations

from foammesh.core.shell.action_policy import (
    AppSnapshot, ContentState, JobState, job_state_from_manager)


def _content_state(configuration: dict, *, has_mesh: bool) -> ContentState:
    has_geometry = bool(configuration.get('geometry'))
    if has_geometry and has_mesh:
        return ContentState.GEOMETRY_AND_MESH
    if has_mesh:
        return ContentState.MESH
    if has_geometry:
        return ContentState.GEOMETRY
    return ContentState.EMPTY


def app_snapshot_from_facade(facade_snapshot: dict | None, *, workflow, dirty: bool = False,
                             has_mesh: bool = False, job_manager=None,
                             capabilities: frozenset[str] = frozenset(),
                             capability_reasons=None, rendering_available: bool = False,
                             undo_available: bool = False, redo_available: bool = False,
                             undo_label: str = '', redo_label: str = '') -> AppSnapshot:
    """Compose the deterministic action snapshot from facade + shared state.

    ``facade_snapshot`` is ``None`` when no case is open. Everything the policy
    needs about *content* comes from the facade configuration; job, capability,
    and rendering state are supplied by the shared managers.
    """
    if facade_snapshot is None:
        return AppSnapshot(project_ready=False, job=job_state_from_manager(job_manager)
                           if job_manager is not None else JobState.IDLE)
    configuration = facade_snapshot.get('configuration', {})
    return AppSnapshot(
        project_ready=True,
        content=_content_state(configuration, has_mesh=has_mesh),
        workflow=workflow,
        dirty=dirty,
        job=job_state_from_manager(job_manager) if job_manager is not None else JobState.IDLE,
        undo_available=undo_available, redo_available=redo_available,
        undo_label=undo_label, redo_label=redo_label,
        rendering_available=rendering_available,
        capabilities=frozenset(capabilities),
        capability_reasons=dict(capability_reasons or {}),
    )
