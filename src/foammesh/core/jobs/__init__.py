#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Meshing job control: launch OpenFOAM utilities in a killable process group so
Cancel terminates the *running* process tree (fixing the reported bug where
Cancel spawned a new snappyHexMesh instead of killing the active one).
"""

from .process_control import new_process_group_kwargs, kill_process_tree, is_running
from .job import Job, JobStatus
from .manager import JobErrorCategory, JobManager, JobRequest, JobResult
from .operation_executor import (
    ExpectedArtifact, OperationContext, OperationExecution, OperationExecutor, OperationSpec,
)

__all__ = ['new_process_group_kwargs', 'kill_process_tree', 'is_running',
           'Job', 'JobStatus', 'JobErrorCategory', 'JobManager', 'JobRequest', 'JobResult',
           'ExpectedArtifact', 'OperationContext', 'OperationExecution', 'OperationExecutor',
           'OperationSpec']
