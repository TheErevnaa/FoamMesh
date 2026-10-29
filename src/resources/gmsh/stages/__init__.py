"""Shared stage functions for the batch runner and the resident session.

Plan 48 RR (DP-4009). The farfield, the explicit healing pass, the scopes,
the curve and volume controls, the structuring options, the boundary layers
and the periodic pairs used to be bodies inside ``runner_v1.py``. They live
here now, unchanged, as mixins of one :class:`StageRun`. ``GmshRun`` (batch)
and the resident session both build a ``StageRun`` and call the same methods
in the same order with the same job.
"""
from __future__ import annotations

from .build_follow import BuildFollowStage
from .common import (
    InvalidSurfaceMesh, Ledger, LayerCollision, LayerFold, LayerLanding,
    MeshFailure, SurfaceCrossing, TESTED_RECOMBINERS, _cavities,
    _successors_of, invalid_surface_counts)
from .controls import ControlsStage
from .farfield import FarfieldStage
from .geometry import GeometryStage
from .healing import HealingStage
from .layers import LayersStage
from .periodic import PeriodicStage
from .recombination import RecombinationStage
from .roles import RolesStage
from .scopes import ScopeStage
from .state import StageBase
from .structuring import StructuringStage
from .tagging import TaggingStage


class NullReporter:
    """A reporter that says nothing: the session has no progress stream."""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


class StageRun(StageBase, GeometryStage, HealingStage, ScopeStage,
               FarfieldStage, StructuringStage, ControlsStage, LayersStage,
               PeriodicStage, TaggingStage, RecombinationStage,
               BuildFollowStage, RolesStage):
    """Every stage body, on the state they share."""
