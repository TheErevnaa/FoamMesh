#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
from enum import Enum
from pathlib import Path

import yaml
from filelock import FileLock

from foammesh.support.mpi import ParallelEnvironment, ParallelType

from foammesh.settings.app_settings import appSettings
from foammesh.core.case.locking import (
    LOCK_INFO_FILE, CaseLockInfo, write_case_lock_info,
)
from app_properties import APP_VERSION


FORMAT_VERSION = 2
FILE_NAME = 'local.cfg'


class LocalSettingKey(Enum):
    FORMAT_VERSION = 'format_version'
    PATH = 'case_full_path'
    PARALLEL_NP = 'parallel_np'
    PARALLEL_TYPE = 'parallel_type'
    PARALLEL_HOSTS = 'parallel_hosts'
    EXECUTION_PROFILE_ID = 'execution_profile_id'
    CASE_RESOURCE_OVERRIDE = 'case_resource_override'


class LocalSettings:
    def __init__(self, path):
        self._settingsFile = path / FILE_NAME

        self._settings = None
        self._stamp = None
        self._lock = None

        self._load()
        if self._settings is None:
            self._create()

        self.set(LocalSettingKey.PATH, str(path.resolve()))

    @property
    def path(self):
        if path := self.get(LocalSettingKey.PATH):
            return Path(path)

        return None

    def parallelEnvironment(self):
        ptypeStr = self.get(LocalSettingKey.PARALLEL_TYPE, ParallelType.LOCAL_MACHINE)
        try:
            parallelType = ParallelType(int(ptypeStr))
        except ValueError:
            parallelType = ParallelType[ptypeStr]

        return ParallelEnvironment(
            self.get(LocalSettingKey.PARALLEL_NP, 1),
            parallelType,
            self.get(LocalSettingKey.PARALLEL_HOSTS, '')
        )

    def setParallelEnvironment(self, environment):
        self.set(LocalSettingKey.PARALLEL_NP, environment.np()),
        self.set(LocalSettingKey.PARALLEL_TYPE, environment.type().name),
        self.set(LocalSettingKey.PARALLEL_HOSTS, environment.hosts())

    def acquireLock(self, timeout):
        self._lock = FileLock(self.path / 'case.lock')
        self._lock.acquire(timeout=timeout)
        try:
            write_case_lock_info(self.path, CaseLockInfo.current(APP_VERSION))
        except Exception:
            self._lock.release()
            self._lock = None
            raise

    def releaseLock(self):
        if self._lock is None:
            return
        info_path = self.path / LOCK_INFO_FILE
        try:
            if info_path.is_file():
                try:
                    raw = yaml.safe_load(info_path.read_text(encoding='utf-8')) or {}
                except OSError:
                    raw = {}
                if raw.get('pid') == os.getpid():
                    info_path.unlink(missing_ok=True)
        finally:
            self._lock.release()
            self._lock = None

    def get(self, key, default=None):
        self._reloadIfChanged()
        return self._settings.get(key.value, default)

    def set(self, key, value):
        if self.get(key) != value:
            self._settings[key.value] = value
            self._save()

    def saveAs(self, path):
        self._settings[LocalSettingKey.FORMAT_VERSION.value] = FORMAT_VERSION
        self._settings[LocalSettingKey.PATH.value] = str(path.resolve())
        self._writeSettings(path / FILE_NAME)

    def _reloadIfChanged(self):
        """Pick up a write made through another instance of these settings.

        The facade edits parallel settings through its own ``LocalSettings``
        for the case, so an open project went on reporting the core count it
        was opened with -- the dialog reported success and every reader still
        saw the old value.
        """
        try:
            stamp = self._settingsFile.stat().st_mtime_ns
        except OSError:
            return
        if stamp != self._stamp:
            self._load()

    def _load(self):
        if self._settingsFile.is_file():
            with open(self._settingsFile) as file:
                self._settings = yaml.load(file, Loader=yaml.FullLoader)
            try:
                self._stamp = self._settingsFile.stat().st_mtime_ns
            except OSError:
                self._stamp = None

    def _create(self):
        environment = appSettings.getParallenEnvironment()

        self._settings = {
            LocalSettingKey.PARALLEL_NP.value:      environment.np(),
            LocalSettingKey.PARALLEL_TYPE.value:    environment.type().name,
            LocalSettingKey.PARALLEL_HOSTS.value:   environment.hosts(),
            LocalSettingKey.EXECUTION_PROFILE_ID.value: 'local',
            LocalSettingKey.CASE_RESOURCE_OVERRIDE.value: None,
        }

    def _save(self):
        self._settings[LocalSettingKey.FORMAT_VERSION.value] = FORMAT_VERSION
        self._writeSettings(self._settingsFile)
        try:
            # Our own write must not read back as somebody else's change.
            self._stamp = self._settingsFile.stat().st_mtime_ns
        except OSError:
            self._stamp = None

    def _writeSettings(self, path):
        temporary = path.with_suffix(f'{path.suffix}.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as output:
                yaml.dump(self._settings, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
