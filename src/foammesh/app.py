#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QTranslator, QCoreApplication, QLocale, Signal
from PySide6.QtWidgets import QApplication

from foammesh.db.project import Project
from foammesh.settings.app_settings import appSettings
from foammesh.settings.project_manager import ProjectManager
from foammesh.openfoam.file_system import FileSystem
from foammesh.core.case import (
    ArtifactState,
    CaseKind,
    MeshOrigin,
    WorkflowMode,
    WorkflowResolution,
    classify_case,
    fingerprint_poly_mesh,
    load_case_metadata,
    resolve_workflow,
)
from foammesh.core.jobs import JobManager
from foammesh.core.project import Event, EventBus
from foammesh.core.facade import CaseSession, FoamMeshFacade
from foammesh.view.facade_client import DesktopFacadeClient
from foammesh.core.shell import CapabilityRegistry
from foammesh.core.selection import SelectionService
from resources import resource

if TYPE_CHECKING:
    from foammesh.settings.app_settings import AppSettings


class App(QObject):
    renderingToggled = Signal(bool)

    def __init__(self):
        super().__init__()

        self._settings = None
        self._project: Optional[Project] = None
        self._fileSystem = None
        self._workflowResolution = WorkflowResolution(
            WorkflowMode.NONE, MeshOrigin.NONE, ArtifactState.ABSENT, 'no case is open')

        self._window = None
        self._translator = None
        self._events = EventBus()
        self._projectManager = ProjectManager(self._events)

        self._qApplication: Optional[QApplication] = None
        self._themeManager = None
        self._jobManager = JobManager(self._events)
        self._selectionService = SelectionService()
        self._capabilities = CapabilityRegistry()
        self._facade = FoamMeshFacade(capabilities=self._capabilities)
        self._caseSession = None
        self._facadeClient = DesktopFacadeClient(self._facade, lambda: self._caseSession)

    @property
    def settings(self) -> 'AppSettings':
        assert self._settings is not None
        return self._settings

    @property
    def window(self):
        return self._window

    @property
    def project(self):
        return self._project

    @property
    def fileSystem(self):
        return self._fileSystem

    @property
    def workflowResolution(self):
        return self._workflowResolution

    @property
    def db(self):
        """Detached facade-backed compatibility view of case configuration.

        UI code may read this snapshot, but mutations have no effect until a
        working copy is explicitly committed through ``facadeClient``.
        """
        return self._facadeClient.checkout() if self._caseSession else None

    @property
    def state(self):
        """Project-state engine: the single write path for all edits.
        Views/API/CLI/agent commit through ``app.state.commit(...)`` so every
        change is a recorded, undoable transaction.
        """
        return self._project.state() if self._project else None

    @property
    def consoleView(self):
        return self._window.consoleView

    @window.setter
    def window(self, window):
        self._window = window

    @property
    def qApplication(self):
        return self._qApplication

    @qApplication.setter
    def qApplication(self, application):
        self._qApplication = application
        from foammesh.view.theming import ThemeManager
        self._themeManager = ThemeManager(
            application, configured_mode=self._settings.getTheme(), persist=self._settings.setTheme)
        self._themeManager.apply()

    @property
    def themeManager(self):
        return self._themeManager

    @property
    def jobManager(self):
        return self._jobManager

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def selectionService(self):
        return self._selectionService

    @property
    def facade(self):
        """Shared case facade attached to the live desktop case session."""
        return self._facade

    @property
    def facadeClient(self):
        """The GUI's single write path (AF4): submits human GUI commands to the
        shared facade instead of touching ``app.state``/``app.db`` directly."""
        return self._facadeClient

    @property
    def caseSession(self):
        return self._caseSession

    @property
    def events(self):
        return self._events

    def setupApplication(self, properties):
        appSettings.load(properties.name)
        self._settings = appSettings
        from foammesh.core.openfoam_runtime import OpenFoamLaunchProfile
        try:
            runtime = self._settings.getOpenFoamRuntime()
        except (AssertionError, TypeError):
            # Lightweight embedders may supply a load stub; retain the exact
            # production defaults without falling back to host OpenFOAM.
            runtime = {
                'profile_id': 'wsl-ubuntu22-openfoam-13',
                'wsl_distro': 'OpenFOAM13Runtime',
                'wsl_user': 'foamuser',
                'bashrc': '/opt/openfoam13/etc/bashrc',
            }
        self._capabilities.configure_profiles((
            OpenFoamLaunchProfile(
                runtime['profile_id'], 'wsl',
                distribution=runtime['wsl_distro'],
                user=runtime['wsl_user'],
                bashrc=runtime['bashrc'],
                expected_project='OpenFOAM',
                expected_version='13',
            ),
        ))

    def applyLanguage(self):
        """Keep the desktop UI on its source-language English strings."""
        QCoreApplication.removeTranslator(self._translator)
        self._translator = QTranslator()
        QLocale.setDefault(QLocale('en'))

    def createProject(self, path):
        assert(self._project is None)
        resolved = Path(path).resolve()
        self._events.publish(Event.PROJECT_OPENING, case_id=str(resolved), path=str(resolved))
        try:
            self._project = self._projectManager.createProject(path)
            self._settings.updateRecents(self._project.path, True)
            self._fileSystem = FileSystem(self._project.path)
            self._fileSystem.createCase(resource.file('openfoam/case'))
            self._attachFacadeSession()
            self._events.publish(Event.PROJECT_OPENED, case_id=str(resolved), path=str(resolved))
            return self._project
        except Exception as error:
            self._events.publish(Event.PROJECT_ERROR, case_id=str(resolved), error=str(error))
            self.closeProject()
            raise

    def createInPlaceCase(self, path):
        """Create a new native OpenFOAM case with FoamMesh state in its sidecar."""
        assert self._project is None

        self._events.publish(Event.PROJECT_OPENING, case_id=str(Path(path).resolve()), path=str(Path(path).resolve()))
        try:
            self._project = self._projectManager.createInPlaceCase(path)
            # A scratch case is deleted when it goes stale, so recording it
            # would leave the user a Recents entry that opens nothing.
            from foammesh.core.case import is_scratch_case
            if not is_scratch_case(self._project.path):
                self._settings.updateRecents(self._project.path, True)
            self._fileSystem = FileSystem(self._project.path, case_root=self._project.path)
            self._fileSystem.createNativeCase(resource.file('openfoam/case'))
            self._fileSystem.ensureFoamFile()
            self._refreshWorkflowResolution()
            self._project.acceptExternalChanges()
            self._attachFacadeSession()
            self._events.publish(Event.PROJECT_OPENED, case_id=str(self._project.path), path=str(self._project.path))
            return self._project
        except Exception as error:
            self._events.publish(Event.PROJECT_ERROR, case_id=str(Path(path).resolve()), error=str(error))
            self.closeProject()
            raise

    def createScratchCase(self, name='untitled'):
        """A real case in a temporary directory, for work that has no home yet.

        The single backend for every door that opens without one: File > New
        Untitled, importing a model with nothing open, and the empty-case page.
        It is an ordinary in-place case -- sidecar, skeleton, lock, journal --
        that happens to sit under the scratch root, so the whole workflow runs
        against it and only :meth:`isScratchCase` distinguishes it.

        Deferring the directory is the point. Requiring one up front meant a
        user holding a CAD file could not get in at all: New refused their
        folder for not being empty, Open refused it for not being a case.
        """
        from foammesh.core.case import create_scratch_dir

        return self.createInPlaceCase(create_scratch_dir(name))

    def isScratchCase(self) -> bool:
        """Whether the open case still lives in the temporary directory."""
        from foammesh.core.case import is_scratch_case

        return (self._project is not None
                and is_scratch_case(self._project.path))

    def openProject(self, path):
        assert(self._project is None)
        resolved = Path(path).resolve()
        self._events.publish(Event.PROJECT_OPENING, case_id=str(resolved), path=str(resolved))
        try:
            self._project = self._projectManager.openProject(path)
            self._settings.updateRecents(self._project.path)
            self._fileSystem = FileSystem(self._project.path)
            self._attachFacadeSession()
            self._events.publish(Event.PROJECT_OPENED, case_id=str(resolved), path=str(resolved))
            return self._project
        except Exception as error:
            self._events.publish(Event.PROJECT_ERROR, case_id=str(resolved), error=str(error))
            self.closeProject()
            raise

    def openCase(self, path):
        """Open a native in-place FoamMesh or OpenFOAM case."""
        assert self._project is None
        path = Path(path).resolve()
        self._events.publish(Event.PROJECT_OPENING, case_id=str(path), path=str(path))
        classification = self._projectManager.classifyCase(path)
        if classification.kind in {
                CaseKind.EMPTY, CaseKind.OPENFOAM_CASE,
                CaseKind.RAW_POLY_MESH_CASE, CaseKind.FOAMMESH_CASE}:
            try:
                self._project = self._projectManager.openInPlaceCase(path)
                self._settings.updateRecents(self._project.path)
                self._fileSystem = FileSystem(self._project.path, case_root=self._project.path)
                self._fileSystem.ensureFoamFile()
                self._refreshWorkflowResolution()
                self._project.acceptExternalChanges()
                self._attachFacadeSession()
                self._events.publish(Event.PROJECT_OPENED, case_id=str(path), path=str(path))
                return self._project
            except Exception as error:
                self._events.publish(Event.PROJECT_ERROR, case_id=str(path), error=str(error))
                self.closeProject()
                raise

        details = '; '.join(classification.reasons) or classification.kind.value
        self._events.publish(Event.PROJECT_ERROR, case_id=str(path), error=details)
        raise ValueError(f'cannot open case: {details}')

    def _attachFacadeSession(self):
        """Attach the facade to the exact ProjectState already open in desktop.

        The existing ``Project`` remains responsible for its established case
        lock during AF1. The facade receives the same state and JobManager, so
        a later loopback API cannot manufacture a shadow in-memory project.
        """
        assert self._project is not None
        if self._caseSession is not None:
            self._facade.detach(self._caseSession.case_id, save=False)
        self._caseSession = CaseSession.from_state(
            self._project.path, self._project.state(), storage_path=self._project.storagePath,
            jobs=self._jobManager)
        self._facade.attach(self._caseSession)

    def closeProject(self):
        closing = self._project.path if self._project else None
        if closing is not None:
            self._events.publish(Event.PROJECT_CLOSING, case_id=str(closing), path=str(closing))
        if self._caseSession is not None:
            self._facade.detach(self._caseSession.case_id, save=False)
            self._caseSession = None
        if self._project:
            self._project.close()
        self._project = None
        self._fileSystem = None
        self._workflowResolution = WorkflowResolution(
            WorkflowMode.NONE, MeshOrigin.NONE, ArtifactState.ABSENT, 'no case is open')
        if closing is not None:
            self._events.publish(Event.PROJECT_CLOSED, case_id=str(closing), path=str(closing))

    def _refreshWorkflowResolution(self):
        if self._project is None or self._fileSystem is None:
            return
        previous_mode = self._workflowResolution.workflow
        classification = classify_case(self._project.path)
        fingerprint = (
            fingerprint_poly_mesh(classification.poly_mesh_path)
            if classification.poly_mesh_path is not None else None)
        metadata = load_case_metadata(self._project.path) if self._project.isInPlace else None
        self._workflowResolution = resolve_workflow(metadata, fingerprint)
        if self._workflowResolution.workflow is not previous_mode:
            self._events.publish(
                Event.WORKFLOW_MODE_CHANGED, case_id=str(self._project.path),
                previous=previous_mode.value, current=self._workflowResolution.workflow.value,
                reason=self._workflowResolution.reason)

    def refreshWorkflowResolution(self):
        self._refreshWorkflowResolution()


app = App()
