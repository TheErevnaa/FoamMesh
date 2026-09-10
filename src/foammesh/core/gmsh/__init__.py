"""Gmsh meshing backend: runtime seam, derivation, execution and publication.

The engine adapter in :mod:`foammesh.core.engine.gmsh` is the only thing the
facade knows about; everything here is behind it.
"""

from .launch_profiles import (
    GmshLaunchCommand,
    GmshLaunchProfile,
    GmshProfileError,
    configured_profiles,
)
from .fields import CONTROLS, GmshControl, control, native_name, paths_for
from .layers import BoundaryLayers, LayerError, derive_boundary_layers
from .periodic import PeriodicError, PeriodicPlan, derive_periodic_pairs
from .plan_derivation import (
    JobIntent, PlanDerivationError, derive_from_native, derive_job_intent,
)
from .quality import QualityError, QualityThresholds, QualityVerdict, assess
from .size_fields import SizeFieldError, SizeFieldPlan, derive_size_fields
from .sizing import GlobalSizing, SizingError, derive_global_sizing
from .topology import Healing, TopologyError, derive_healing
from .runtime import (
    GmshFailureCategory,
    GmshRuntimeProbe,
    GmshRuntimeReport,
    REQUIRED_CAPABILITIES,
    decode_process_output,
    probe_configured,
    select_runtime,
    unavailable_reason,
)

__all__ = [
    'BoundaryLayers',
    'CONTROLS',
    'GlobalSizing',
    'GmshControl',
    'GmshFailureCategory',
    'GmshLaunchCommand',
    'GmshLaunchProfile',
    'GmshProfileError',
    'GmshRuntimeProbe',
    'GmshRuntimeReport',
    'REQUIRED_CAPABILITIES',
    'configured_profiles',
    'decode_process_output',
    'probe_configured',
    'select_runtime',
    'unavailable_reason',
    'Healing',
    'JobIntent',
    'LayerError',
    'PeriodicError',
    'PeriodicPlan',
    'PlanDerivationError',
    'QualityError',
    'QualityThresholds',
    'QualityVerdict',
    'SizeFieldError',
    'SizeFieldPlan',
    'SizingError',
    'TopologyError',
    'assess',
    'control',
    'derive_boundary_layers',
    'derive_from_native',
    'derive_global_sizing',
    'derive_healing',
    'derive_job_intent',
    'derive_periodic_pairs',
    'derive_size_fields',
    'native_name',
    'paths_for',
]
