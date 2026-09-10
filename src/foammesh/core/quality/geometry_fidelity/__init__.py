"""Engine-neutral geometry-fidelity measurement (Plan 23).

The calculation lives below ``core/quality`` rather than in either engine,
because both are judged against the same prepared geometry and the same
tolerance policy after publication.
"""
from .boundary import (
    BoundaryAdapterError, BoundaryModel, BoundarySection, NativeSection,
    extraction_fingerprint, native_sections, outward_fraction, sections_from,
)
from .features import (
    FeatureMatchError, FeatureResult, TopologyResult, compare_topology,
    measure_feature, measure_features, section_verdict, topology_of,
)
from .distance import (
    DistanceError, Distribution, NormalError, SurfaceLocator, measure,
)
from .sampling import (
    SampleSet, SamplingError, floor_for, required_radius, sample_triangles,
    upper_bound, verdict,
)

__all__ = [
    'FeatureMatchError', 'FeatureResult', 'TopologyResult', 'compare_topology',
    'measure_feature', 'measure_features', 'section_verdict', 'topology_of',
    'BoundaryAdapterError', 'BoundaryModel', 'BoundarySection', 'DistanceError',
    'Distribution', 'NativeSection', 'NormalError', 'SampleSet',
    'SamplingError', 'SurfaceLocator', 'extraction_fingerprint', 'floor_for',
    'measure', 'native_sections', 'outward_fraction', 'required_radius',
    'sample_triangles', 'sections_from', 'upper_bound', 'verdict',
]
