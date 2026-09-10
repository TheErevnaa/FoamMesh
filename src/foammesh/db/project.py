#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from PySide6.QtCore import QObject

from foammesh.settings.app_settings import appSettings
from foammesh.settings.local_settings import LocalSettings, LocalSettingKey
from foammesh.db.configurations import Configurations
from foammesh.db.configurations_schema import schema
from foammesh.core.project import Event, ProjectState
from foammesh.core.case.conflict import CaseConflictError, CaseExternalSnapshot
from foammesh.core.case.model import CaseMetadata, save_case_metadata
from foammesh.core.case.artifact_history import ArtifactHistoryStore

logger = logging.getLogger(__name__)

# Transaction history is persisted alongside the configuration so the audit
# trail survives reopening the project. It is non-critical: a missing or corrupt
# file degrades to "no prior history", never blocks opening the project.
HISTORY_FILE = 'foammesh_history.json'
PROPOSALS_FILE = 'foammesh_proposals.json'


class Project(QObject):
    def __init__(self, path, *, storage_path=None, event_bus=None):
        super().__init__()

        self._path = Path(path).resolve()
        self._storagePath = Path(storage_path).resolve() if storage_path else self._path
        self._storagePath.mkdir(parents=True, exist_ok=True)
        self._settings = LocalSettings(self._storagePath)
        self._lock = None
        self._db = Configurations(schema)
        self._state = None
        self._eventBus = event_bus
        self._artifactDirty = False
        self._externalBaseline = None

    @property
    def path(self):
        return self._path

    @property
    def storagePath(self):
        """Directory owned by FoamMesh for configuration and audit data.

        Authored project directories use the project root. In-place cases use their
        ``foammesh/`` sidecar so native OpenFOAM files stay separate.
        """
        return self._storagePath

    @property
    def isInPlace(self):
        """Whether this project stores FoamMesh data in a case sidecar."""
        return self._storagePath != self._path

    def name(self):
        return self.path.name

    def db(self):
        return self._db

    def state(self):
        """The project-state engine wrapping this project's configuration.
        All edits (GUI/API/CLI/agent) should commit through this object so they
        become recorded, undoable transactions.
        """
        if self._state is None:
            self._state = ProjectState(self._db, bus=self._eventBus)
        return self._state

    @property
    def isDirty(self):
        return self._db.isModified() or self._artifactDirty

    def assertUnchanged(self):
        if self._externalBaseline is None:
            return
        current = CaseExternalSnapshot.capture(self._path, self._storagePath)
        changed = self._externalBaseline.differences(current)
        if changed:
            raise CaseConflictError(changed)

    def acceptExternalChanges(self):
        """Reset the external baseline after a verified app-owned mutation."""
        self._externalBaseline = CaseExternalSnapshot.capture(self._path, self._storagePath)

    def markArtifactChanged(self):
        self._artifactDirty = True
        self.acceptExternalChanges()
        self.state().bus.publish(
            Event.ARTIFACT_MESH_CHANGED, case_id=str(self._path), path=str(self._path))
        self.state().bus.publish(
            Event.ARTIFACT_STALE, case_id=str(self._path),
            artifact_ids=('quality', 'exports'), reason='mesh fingerprint changed')

    def markGeometryChanged(self):
        """Publish geometry invalidation after an already-recorded state edit."""
        self.state().bus.publish(
            Event.ARTIFACT_GEOMETRY_CHANGED, case_id=str(self._path), path=str(self._path))

    def unifiedHistory(self):
        """Merge undoable state edits with non-undoable artifact operations."""
        state_entries = []
        for item in self.state().dump_history():
            value = dict(item)
            value['kind'] = 'state'
            state_entries.append(value)
        artifact_entries = [item.to_dict() for item in ArtifactHistoryStore(self._path).entries()]
        return sorted(
            (*state_entries, *artifact_entries),
            key=lambda item: (item.get('timestamp', ''), item.get('tx_id', item.get('entry_id', ''))))

    def getLocalSetting(self, key):
        return self._settings.get(key)

    def setLocalSetting(self, key, value):
        self._settings.set(key, value)

    def parallelEnvironment(self):
        return self._settings.parallelEnvironment()

    def setParallelEnvironment(self, environment):
        self._settings.setParallelEnvironment(environment)
        appSettings.updateParallelEnvironment(environment)

    def parallelCores(self):
        return self._settings.get(LocalSettingKey.PARALLEL_NP, 1)

    def save(self):
        self.assertUnchanged()
        self._db.save()
        self._saveHistory(self._storagePath)
        self._artifactDirty = False
        self.acceptExternalChanges()
        self.state().bus.publish(
            Event.PROJECT_SAVED, case_id=str(self._path), path=str(self._path))

    def saveAs(self, path, *, storage_path=None):
        path = Path(path)
        path.mkdir()
        destination = Path(storage_path) if storage_path else path
        destination.mkdir(parents=True, exist_ok=True)
        self._db.saveAs(destination)
        self._settings.saveAs(destination)
        self._saveHistory(destination)

    def saveStateCopy(self, case_path):
        """Write the current in-memory FoamMesh state into an existing case copy."""
        case = Path(case_path)
        destination = case / 'foammesh' if self.isInPlace else case
        destination.mkdir(parents=True, exist_ok=True)
        original_path = self._db._path
        original_modified = self._db._modified
        try:
            self._db.saveAs(destination)
        finally:
            self._db._path = original_path
            self._db._modified = original_modified
        self._saveLocalSettingsCopy(destination)
        self._saveHistory(destination)

    def _saveLocalSettingsCopy(self, destination):
        original = dict(self._settings._settings)
        try:
            self._settings.saveAs(destination)
        finally:
            self._settings._settings = original

    def saveStateAsCase(self, case_path):
        """Atomically create a state-only case; native artifacts are not copied."""
        destination = Path(case_path).resolve()
        if destination.exists():
            raise FileExistsError(f'destination already exists: {destination}')
        staging = destination.with_name(
            f'.{destination.name}.foammesh-state-{uuid.uuid4().hex}')
        original_db_path = self._db._path
        original_modified = self._db._modified
        try:
            sidecar = staging / 'foammesh'
            sidecar.mkdir(parents=True)
            self._db.saveAs(sidecar)
            self._saveLocalSettingsCopy(sidecar)
            self._saveHistory(sidecar)
            save_case_metadata(staging, CaseMetadata(provenance={
                'state_saved_from': str(self._path),
                'copy_policy': 'foammesh-state-only-no-native-artifacts',
            }))
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            self._db._path = original_db_path
            self._db._modified = original_modified
        return destination

    def new(self):
        self._settings.acquireLock(0.01)
        self._db.create(self._storagePath)
        self.acceptExternalChanges()

    def open(self, create=False):
        self._settings.acquireLock(0.01)
        self._db.load(self._storagePath)
        self._loadHistory(self._storagePath)
        self.acceptExternalChanges()

    def _saveHistory(self, path):
        try:
            self._atomicWriteJson(path / HISTORY_FILE, self.state().dump_history())
            self._atomicWriteJson(path / PROPOSALS_FILE, self.state().dump_proposals())
        except OSError:
            logger.warning('could not write transaction history', exc_info=True)

    @staticmethod
    def _atomicWriteJson(path, value):
        """Write a FoamMesh-owned JSON document without truncating its prior value."""
        temporary = path.with_suffix(f'{path.suffix}.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as output:
                json.dump(value, output)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _loadHistory(self, path):
        f = path / HISTORY_FILE
        if f.is_file():
            try:
                self.state().load_history(json.loads(f.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                logger.warning('could not read transaction history', exc_info=True)
        pf = path / PROPOSALS_FILE
        if pf.is_file():
            try:
                self.state().load_proposals(json.loads(pf.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                logger.warning('could not read proposals', exc_info=True)

    def close(self):
        self._settings.releaseLock()
        self._externalBaseline = None
