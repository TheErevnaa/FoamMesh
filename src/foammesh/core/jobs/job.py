#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A cancellable external job (e.g. blockMesh / snappyHexMesh / checkMesh).

Tracks the launched process so cancel() kills the whole tree via
process_control. The actual OpenFOAM command strings are supplied by the
openfoam runners; this class is generic and unit-testable with any command.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum

from .process_control import new_process_group_kwargs, kill_process_tree


class JobStatus(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    TIMED_OUT = 'timed_out'


@dataclass
class Job:
    name: str
    command: list[str]
    cwd: str | None = None
    status: JobStatus = JobStatus.PENDING
    returncode: int | None = None
    _proc: subprocess.Popen | None = field(default=None, repr=False)

    def start(self) -> 'Job':
        self._proc = subprocess.Popen(
            self.command, cwd=self.cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **new_process_group_kwargs())
        self.status = JobStatus.RUNNING
        return self

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def wait(self) -> int:
        assert self._proc is not None
        self.returncode = self._proc.wait()
        self.status = JobStatus.DONE if self.returncode == 0 else JobStatus.FAILED
        return self.returncode

    def cancel(self) -> int:
        """Kill the running process tree. Does NOT start anything new."""
        if self._proc is None:
            return 0
        killed = kill_process_tree(self._proc.pid)
        self.status = JobStatus.CANCELLED
        return killed
