#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""OpenFOAM Foundation v13 target capabilities.

Foundation v13 is the sole production target. The explicit record keeps
writers and launchers aligned to one verified utility/dictionary contract.

Pure data + helpers — no Qt / no app coupling, so it is unit-testable headless.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from foammesh.core.quality.policy import (
    GENERATOR_LIMITS, GENERATOR_RELAXED_LIMITS,
)


class FoamFlavor(str, Enum):
    FOUNDATION = 'foundation'   # openfoam.org


# OpenFOAM v13 (Foundation) snappyHexMesh meshQualityControls — conservative,
# tutorial-aligned defaults. Values are strings to match dictionary output.
#
# Plan 30 WP-04 (F-25). These are the *generator* half of one policy: the
# limits snappyHexMesh judges candidate cells against. The *acceptance* half —
# what QA calls an acceptable finished mesh — used to live in two other
# modules with no stated relation to these numbers. Both halves now come from
# :mod:`foammesh.core.quality.policy`, which also asserts the relation
# (strict <= acceptance <= relaxed). Re-exported here under their old names
# because the writers and the golden dictionaries address them by that name.
V13_MESH_QUALITY_DEFAULTS: dict[str, str] = dict(GENERATOR_LIMITS)

# Relaxed controls applied after nRelaxedIter.
V13_MESH_QUALITY_RELAXED_DEFAULTS: dict[str, str] = dict(
    GENERATOR_RELAXED_LIMITS)


@dataclass(frozen=True)
class FoamTarget:
    flavor: FoamFlavor
    version: str
    # surface feature extraction utility + whether it reads surfaceFeaturesDict
    surface_features_utility: str = 'surfaceFeatures'
    uses_surface_features_dict: bool = True
    # layer-shrink algorithms snappy supports for this target. Foundation 13
    # registers exactly one externalDisplacementMeshMover; the motion-solver
    # variant belongs to the motionSolver table and cannot be selected here.
    shrink_algorithms: tuple[str, ...] = ('displacementMedialAxis',)
    mesh_quality_defaults: dict = field(default_factory=lambda: dict(V13_MESH_QUALITY_DEFAULTS))
    mesh_quality_relaxed_defaults: dict = field(default_factory=lambda: dict(V13_MESH_QUALITY_RELAXED_DEFAULTS))

    @property
    def is_foundation(self) -> bool:
        return self.flavor is FoamFlavor.FOUNDATION


# The primary, default target for FoamMesh.
FOUNDATION_V13 = FoamTarget(
    flavor=FoamFlavor.FOUNDATION,
    version='13',
)

DEFAULT_TARGET = FOUNDATION_V13

_TARGETS = {
    'foundation-13': FOUNDATION_V13,
}


def get_target(name: str | None = None) -> FoamTarget:
    """Resolve the sole production target and reject any other runtime."""
    if not name:
        return DEFAULT_TARGET
    try:
        return _TARGETS[name.lower()]
    except KeyError as error:
        raise ValueError(
            f'unsupported OpenFOAM target {name!r}; expected foundation-13') \
            from error


def mesh_quality_controls(target: FoamTarget = DEFAULT_TARGET,
                          overrides: dict | None = None) -> dict:
    """v13 meshQualityControls as a dict, with optional user overrides merged in.
    Unknown override keys are kept (snappy tolerates extra quality keys) but the
    known v13 defaults guarantee a valid, complete control set.
    """
    controls = dict(target.mesh_quality_defaults)
    if overrides:
        controls.update({k: str(v) for k, v in overrides.items() if v is not None})
    controls['relaxed'] = dict(target.mesh_quality_relaxed_defaults)
    return controls
