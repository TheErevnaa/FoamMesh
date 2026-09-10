"""Feature identity for geometry-fidelity tolerances (Plan 23 §7.1)."""
from .detect import authored, detect, edge_signature, from_cad_edges, point_signature
from .manifest import (
    DEFAULT_FEATURE_ANGLE_DEG, Feature, FeatureManifest, FeatureManifestError,
    FeatureManifestStore, FeaturePolicy, carry_forward, default_detection, mint,
)

__all__ = [
    'DEFAULT_FEATURE_ANGLE_DEG', 'Feature', 'FeatureManifest',
    'FeatureManifestError', 'FeatureManifestStore', 'FeaturePolicy',
    'authored', 'carry_forward', 'default_detection', 'detect', 'edge_signature',
    'from_cad_edges', 'mint',
    'point_signature',
]
