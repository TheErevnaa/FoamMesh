#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Pre-run cell-count and memory estimates.

Rough, deliberately conservative estimates so the user gets a warning before
launching a job that could exhaust the workstation. Each refinement level
roughly multiplies the refined band's cells (octree: x8 per level), applied to
an assumed surface fraction of the base mesh.
"""
from __future__ import annotations

# very rough: snappyHexMesh hex cells ~ 0.5-1.0 GB per million for meshing peak
_GB_PER_MILLION_CELLS = 1.0


def estimate_cell_count(base_cells: int,
                        surface_refinement_levels: list[int] | None = None,
                        surface_fraction: float = 0.1) -> int:
    """Estimate final cell count from the base grid + surface refinement levels.

    *surface_fraction* approximates the share of base cells touching refined
    surfaces; each level multiplies those by 8.
    """
    if base_cells <= 0:
        return 0
    total = float(base_cells)
    for level in (surface_refinement_levels or []):
        if level <= 0:
            continue
        refined_band = base_cells * surface_fraction
        total += refined_band * (8 ** level - 1)
    return int(total)


def estimate_memory_gb(cell_count: int) -> float:
    """Estimate peak meshing memory (GB) for a given cell count."""
    return max(0.0, cell_count / 1_000_000.0 * _GB_PER_MILLION_CELLS)
