"""Stable selection entities and the shared selection service."""

from .service import (
    SelectionEntity,
    SelectionError,
    SelectionKind,
    SelectionService,
    SelectionSignal,
    SelectionSnapshot,
    SelectionStatus,
)

__all__ = [
    'SelectionEntity',
    'SelectionError',
    'SelectionKind',
    'SelectionService',
    'SelectionSignal',
    'SelectionSnapshot',
    'SelectionStatus',
]
