"""One deterministic source of truth for shell action availability."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from foammesh.core.case import WorkflowMode


class ContentState(str, Enum):
    EMPTY = 'empty'
    GEOMETRY = 'geometry'
    MESH = 'mesh'
    GEOMETRY_AND_MESH = 'geometry_and_mesh'


class JobState(str, Enum):
    IDLE = 'idle'
    RUNNING_READ = 'running_read'
    RUNNING_MUTATION = 'running_mutation'
    CANCELLING = 'cancelling'


class ActionId(str, Enum):
    NEW_CASE = 'new_case'
    NEW_SCRATCH_CASE = 'new_scratch_case'
    OPEN_PROJECT = 'open_project'
    SAVE = 'save'
    SAVE_AS = 'save_as'
    SAVE_PROJECT_AS = 'save_project_as'
    LOAD_GEOMETRY = 'load_geometry'
    LOAD_MESH = 'load_mesh'
    CLOSE_PROJECT = 'close_project'
    EXIT = 'exit'
    UNDO = 'undo'
    REDO = 'redo'
    MESH_INFO = 'mesh_info'
    MESH_SCALE = 'mesh_scale'
    MESH_TRANSLATE = 'mesh_translate'
    MESH_ROTATE = 'mesh_rotate'
    MESH_QUALITY = 'mesh_quality'
    MESH_CHECK = 'mesh_check'
    MESH_REPAIR = 'mesh_repair'
    MESH_RESTORE = 'mesh_restore'
    VIEW_FIT = 'view_fit'
    VIEW_ZOOM_SELECTION = 'view_zoom_selection'
    VIEW_AXIS = 'view_axis'
    VIEW_CUBE_AXIS = 'view_cube_axis'
    VIEW_RULER = 'view_ruler'
    VIEW_PARALLEL_PROJECTION = 'view_parallel_projection'
    VIEW_ALIGN_AXIS = 'view_align_axis'
    VIEW_ROLL = 'view_roll'
    VIEW_ROTATION_CENTER = 'view_rotation_center'
    PREFERENCES = 'preferences'
    TERMINAL_HERE = 'terminal_here'
    TUTORIALS = 'tutorials'
    LICENSE = 'license'
    ABOUT = 'about'
    RUN_DETAILS = 'run_details'


# Stable IDs are deliberately separate from translated labels.  Keeping the
# Qt object-name bridge here also lets source-level tests prove that the policy
# and the Designer menu cannot silently drift apart.
ACTION_OBJECT_NAMES: Mapping[ActionId, str] = {
    ActionId.NEW_CASE: 'actionNew',
    ActionId.NEW_SCRATCH_CASE: 'actionNewUntitled',
    ActionId.OPEN_PROJECT: 'actionOpen',
    ActionId.SAVE: 'actionSave',
    ActionId.SAVE_AS: 'actionSaveAs',
    ActionId.SAVE_PROJECT_AS: 'actionSaveProjectAs',
    ActionId.LOAD_GEOMETRY: 'actionLoadGeometry',
    ActionId.LOAD_MESH: 'actionLoadMesh',
    ActionId.CLOSE_PROJECT: 'actionClose',
    ActionId.EXIT: 'actionExit',
    ActionId.UNDO: 'actionUndo',
    ActionId.REDO: 'actionRedo',
    ActionId.MESH_INFO: 'actionMeshInfo',
    ActionId.MESH_SCALE: 'actionMeshScale',
    ActionId.MESH_TRANSLATE: 'actionMeshTranslate',
    ActionId.MESH_ROTATE: 'actionMeshRotate',
    ActionId.MESH_QUALITY: 'actionParameters',
    ActionId.MESH_CHECK: 'actionMeshCheck',
    ActionId.MESH_REPAIR: 'actionMeshRepair',
    ActionId.MESH_RESTORE: 'actionMeshRestore',
    ActionId.VIEW_FIT: 'actionViewFit',
    ActionId.VIEW_ZOOM_SELECTION: 'actionViewZoomSelection',
    ActionId.VIEW_AXIS: 'actionViewAxis',
    ActionId.VIEW_CUBE_AXIS: 'actionViewCubeAxis',
    ActionId.VIEW_RULER: 'actionViewRuler',
    ActionId.VIEW_PARALLEL_PROJECTION: 'actionViewParallelProjection',
    ActionId.VIEW_ALIGN_AXIS: 'actionViewAlignAxis',
    ActionId.VIEW_ROLL: 'actionViewRoll',
    ActionId.VIEW_ROTATION_CENTER: 'actionViewRotationCenter',
    ActionId.PREFERENCES: 'actionPreferences',
    ActionId.TERMINAL_HERE: 'actionTerminalHere',
    ActionId.TUTORIALS: 'actionTutorials',
    ActionId.LICENSE: 'actionLicense',
    ActionId.ABOUT: 'actionAbout',
    ActionId.RUN_DETAILS: 'actionRunDetails',
}


@dataclass(frozen=True)
class AppSnapshot:
    project_ready: bool = False
    content: ContentState = ContentState.EMPTY
    workflow: WorkflowMode = WorkflowMode.NONE
    dirty: bool = False
    job: JobState = JobState.IDLE
    undo_available: bool = False
    redo_available: bool = False
    undo_label: str = ''
    redo_label: str = ''
    rendering_available: bool = False
    capabilities: frozenset[str] = field(default_factory=frozenset)
    capability_reasons: Mapping[str, str] = field(default_factory=dict)

    @property
    def has_mesh(self) -> bool:
        return self.content in (ContentState.MESH, ContentState.GEOMETRY_AND_MESH)

    @property
    def has_geometry(self) -> bool:
        return self.content in (ContentState.GEOMETRY, ContentState.GEOMETRY_AND_MESH)

    @property
    def job_idle(self) -> bool:
        return self.job is JobState.IDLE


@dataclass(frozen=True)
class ActionPresentation:
    enabled: bool
    visible: bool = True
    reason: str = ''


class ActionPolicy:
    """Evaluate the complete menu contract without importing Qt widgets."""

    def evaluate(self, snapshot: AppSnapshot) -> dict[ActionId, ActionPresentation]:
        result = {action: ActionPresentation(False, reason='Create or open a case first')
                  for action in ActionId}
        result[ActionId.NEW_CASE] = ActionPresentation(snapshot.job_idle, reason=self._job_reason(snapshot))
        result[ActionId.NEW_SCRATCH_CASE] = ActionPresentation(
            snapshot.job_idle, reason=self._job_reason(snapshot))
        result[ActionId.OPEN_PROJECT] = ActionPresentation(snapshot.job_idle, reason=self._job_reason(snapshot))
        # Importing a model is how most people start, so it must not be behind
        # "create a case first". With nothing open it creates a scratch case
        # and imports into that; the directory is chosen later, when there is
        # something worth keeping. Once a case IS open the capability rules
        # below take over and can still refuse it.
        result[ActionId.LOAD_GEOMETRY] = ActionPresentation(
            snapshot.job_idle, reason=self._job_reason(snapshot))
        # Close/Exit stay reachable so their lifecycle handler can offer
        # Cancel operation / Keep running / Return instead of trapping users.
        result[ActionId.EXIT] = ActionPresentation(True)
        for action in (ActionId.PREFERENCES, ActionId.TUTORIALS,
                       ActionId.LICENSE, ActionId.ABOUT,
                       ActionId.RUN_DETAILS):
            result[action] = ActionPresentation(True)

        if not snapshot.project_ready:
            return result

        project_idle = snapshot.job_idle
        project_reason = self._job_reason(snapshot)
        for action in (
                ActionId.SAVE_AS, ActionId.SAVE_PROJECT_AS, ActionId.LOAD_GEOMETRY,
                ActionId.LOAD_MESH, ActionId.CLOSE_PROJECT,
                ActionId.MESH_QUALITY):
            result[action] = self._capability_action(snapshot, action, project_idle, project_reason)
        result[ActionId.CLOSE_PROJECT] = self._capability_action(
            snapshot, ActionId.CLOSE_PROJECT, True)

        result[ActionId.SAVE] = ActionPresentation(
            snapshot.dirty and project_idle,
            reason=self._save_reason(snapshot, project_reason))
        result[ActionId.UNDO] = ActionPresentation(
            snapshot.undo_available and project_idle,
            reason=('Nothing to undo' if not snapshot.undo_available else project_reason))
        result[ActionId.REDO] = ActionPresentation(
            snapshot.redo_available and project_idle,
            reason=('Nothing to redo' if not snapshot.redo_available else project_reason))

        result[ActionId.TERMINAL_HERE] = self._capability_action(
            snapshot, ActionId.TERMINAL_HERE, project_idle, project_reason)

        rendering_reason = ('Open a case to use viewport controls'
                            if not snapshot.rendering_available else project_reason)
        for action in (
                ActionId.VIEW_FIT, ActionId.VIEW_ZOOM_SELECTION,
                ActionId.VIEW_AXIS, ActionId.VIEW_CUBE_AXIS,
                ActionId.VIEW_RULER, ActionId.VIEW_PARALLEL_PROJECTION,
                ActionId.VIEW_ALIGN_AXIS, ActionId.VIEW_ROLL,
                ActionId.VIEW_ROTATION_CENTER):
            result[action] = ActionPresentation(
                snapshot.rendering_available and project_idle, reason=rendering_reason)

        mesh_reason = 'No complete constant/polyMesh was found' if not snapshot.has_mesh else project_reason
        for action in (ActionId.MESH_INFO, ActionId.MESH_SCALE, ActionId.MESH_TRANSLATE,
                       ActionId.MESH_ROTATE, ActionId.MESH_CHECK, ActionId.MESH_RESTORE):
            result[action] = self._capability_action(
                snapshot, action, snapshot.has_mesh and project_idle, mesh_reason)
        repair_ready = (snapshot.has_mesh or snapshot.has_geometry) and project_idle
        repair_reason = ('No mesh or imported surface geometry was found'
                         if not (snapshot.has_mesh or snapshot.has_geometry) else project_reason)
        result[ActionId.MESH_REPAIR] = self._capability_action(
            snapshot, ActionId.MESH_REPAIR, repair_ready, repair_reason)

        return result

    @staticmethod
    def _capability_action(snapshot: AppSnapshot, action: ActionId, enabled: bool,
                           reason: str = '') -> ActionPresentation:
        if action.value not in snapshot.capabilities:
            return ActionPresentation(False, reason=snapshot.capability_reasons.get(
                action.value, 'This feature is not available in this build'))
        return ActionPresentation(enabled, reason=reason)

    @staticmethod
    def _save_reason(snapshot: AppSnapshot, project_reason: str) -> str:
        """Why Save is greyed out.

        DP-1264. While a mesh ran, Save said only "Wait for the active mesh
        operation to finish or cancel it", and a user an hour into a run read
        that as "everything so far is at risk until it ends". It is not: each
        finished stage is kept as it lands (DP-1234) and a stopped run resumes
        from the last of them (DP-1236). The tooltip now says so.
        """
        if snapshot.job is JobState.RUNNING_MUTATION:
            return ('Save is unavailable while the active mesh operation '
                    'runs. Each finished stage is kept automatically, and a '
                    'stopped run can be resumed from the last one.')
        if not snapshot.dirty:
            return 'No unsaved changes'
        return project_reason

    @staticmethod
    def _job_reason(snapshot: AppSnapshot) -> str:
        if snapshot.job is JobState.IDLE:
            return ''
        if snapshot.job is JobState.RUNNING_MUTATION:
            return 'Wait for the active mesh operation to finish or cancel it'
        if snapshot.job is JobState.CANCELLING:
            return 'The active operation is being cancelled'
        return 'Wait for the active operation to finish'


def mesh_quality_capability(engine_id: str) -> tuple[bool, str]:
    """Whether Mesh > Quality thresholds has anything to edit on this case.

    DP-752. The dialog edits the meshQualityDict limits snappyHexMesh applies
    while it snaps and adds layers. Nothing else reads them, so on a Gmsh case
    the entry was a form whose every value was ignored.
    """
    if str(engine_id) == 'snappy':
        return True, ''
    if str(engine_id) == 'gmsh':
        return False, ('These thresholds drive snappyHexMesh only. A Gmsh mesh '
                       'is judged by the quality gate on Generate mesh')
    return False, ('These thresholds drive snappyHexMesh. Choose it on Mesh '
                   'setup to set them')


def job_state_from_manager(manager) -> JobState:
    """Project the live job manager into the immutable shell snapshot model."""
    if manager.has_cancelling_job:
        return JobState.CANCELLING
    if manager.has_mutating_job:
        return JobState.RUNNING_MUTATION
    if manager.active_job_ids:
        return JobState.RUNNING_READ
    return JobState.IDLE
