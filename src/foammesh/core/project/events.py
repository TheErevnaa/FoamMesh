#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""In-process event bus for the project-state engine.

Plain synchronous pub/sub so the core has no Qt dependency (the GUI, API
WebSocket bridge, and agent all subscribe to the same bus). Phase 09 bridges
these events to WebSocket; the GUI can additionally connect them to Qt signals.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

logger = logging.getLogger(__name__)


class Event:
    """Canonical event names published by the project-state engine."""
    # state edits
    TRANSACTION_APPLIED = 'transaction.applied'
    TRANSACTION_REVERTED = 'transaction.reverted'
    UNDONE = 'state.undone'
    REDONE = 'state.redone'
    # proposals (agent / preview)
    PROPOSAL_CREATED = 'proposal.created'
    PROPOSAL_ACCEPTED = 'proposal.accepted'
    PROPOSAL_REJECTED = 'proposal.rejected'
    PROJECT_OPENING = 'project.opening'
    PROJECT_OPENED = 'project.opened'
    PROJECT_SAVED = 'project.saved'
    PROJECT_CLOSING = 'project.closing'
    PROJECT_CLOSED = 'project.closed'
    PROJECT_ERROR = 'project.error'
    ARTIFACT_GEOMETRY_CHANGED = 'artifact.geometry_changed'
    GEOMETRY_PREPARATION_DECIDED = 'geometry.preparation.decided'
    GEOMETRY_PREPARE_PROGRESS = 'geometry.prepare.progress'
    GEOMETRY_REVISION_CREATED = 'geometry.revision_created'
    ARTIFACT_DICTIONARIES_CHANGED = 'artifact.dictionaries_changed'
    ARTIFACT_MESH_CHANGED = 'artifact.mesh_changed'
    ARTIFACT_QUALITY_CHANGED = 'artifact.quality_changed'
    WORKFLOW_MODE_CHANGED = 'workflow.mode_changed'
    WORKFLOW_STEP_STALE = 'workflow.step_stale'
    ARTIFACT_STALE = 'artifact.stale'
    #: Plan 26 WP3.1. The configuration changed after a mesh was produced, so
    #: any verdict on screen now describes a mesh that is no longer the one the
    #: settings would make. Deliberately distinct from ARTIFACT_STALE, which
    #: means "the mesh fingerprint changed" -- the opposite direction -- and
    #: from WORKFLOW_STEP_STALE, which is about task prerequisites. A
    #: permanently visible "PASS" for a superseded mesh is the same defect as a
    #: gate certifying layers that are not there.
    MESH_VERDICT_STALE = 'mesh.verdict_stale'
    ARTIFACT_RESTORED = 'artifact.restored'
    JOB_STARTED = 'job.started'
    JOB_OUTPUT = 'job.output'
    JOB_PROGRESS = 'job.progress'
    JOB_CANCEL_REQUESTED = 'job.cancel_requested'
    JOB_FINISHED = 'job.finished'
    JOB_FAILED = 'job.failed'
    JOB_CANCELLED = 'job.cancelled'
    OPERATION_STARTED = 'operation.started'
    OPERATION_SUCCEEDED = 'operation.succeeded'
    OPERATION_FAILED = 'operation.failed'
    OPERATION_RECOVERED = 'operation.recovered'
    CAPABILITIES_CHANGED = 'capabilities.changed'


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event: str, callback: Callable) -> Callable:
        """Register *callback* for *event*. Returns an unsubscribe function."""
        self._subscribers[event].append(callback)

        def _unsubscribe():
            try:
                self._subscribers[event].remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def publish(self, event: str, **payload) -> None:
        for callback in list(self._subscribers.get(event, ())):
            try:
                callback(event=event, **payload)
            except Exception:  # a bad subscriber must not break the engine
                logger.exception('event subscriber failed for %s', event)
