#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Dependency-aware meshing workflow.

Replaces rigid "locked steps where going back deletes downstream
work" with a graph where editing an upstream stage marks downstream stages
*stale* (to be re-run) rather than destroying them.
"""

from .graph import Stage, StageStatus, WorkflowGraph
from .checkpoints import CheckpointStore, Checkpoint
from .controller import WorkflowController
from .dynamic import EngineWorkflowGraph, TaskTransition
from .snap_checkpoint import (
    SnapCheckpoint, SnapCheckpointError, SnapCheckpointStore,
    poly_mesh_fingerprint,
)

__all__ = ['Stage', 'StageStatus', 'WorkflowGraph',
           'CheckpointStore', 'Checkpoint', 'WorkflowController',
           'EngineWorkflowGraph', 'TaskTransition',
           'SnapCheckpoint', 'SnapCheckpointError', 'SnapCheckpointStore',
           'poly_mesh_fingerprint']
