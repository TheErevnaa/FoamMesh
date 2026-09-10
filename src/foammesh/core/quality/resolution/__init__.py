"""Resolution adequacy: enough cells where it matters (Plan 23 §6.5, §6.6)."""
from .assessment import (
    MEASURED, NOT_APPLICABLE, UNRELIABLE, ChannelResult, ResolutionError,
    ResolutionReport, SizeComparison, combine, compare_size, edge_lengths,
    elements_along, pair_surfaces, traverse,
)

__all__ = [
    'ChannelResult', 'MEASURED', 'NOT_APPLICABLE', 'ResolutionError',
    'ResolutionReport', 'SizeComparison', 'UNRELIABLE', 'combine',
    'compare_size', 'edge_lengths', 'elements_along', 'pair_surfaces',
    'traverse',
]
