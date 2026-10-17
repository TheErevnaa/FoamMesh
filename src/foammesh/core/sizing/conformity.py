"""Size conformity: ``cell size / field size`` per cell (Plan 41 4.8.3, 4.8.4).

The first measure of whether a mesh did what the sizing asked. Above 2 a cell
is under-resolved (too coarse for the field), below 0.5 over-resolved. Both
routes use this one function; snappy cells and Gmsh elements differ only in
how the caller turns a cell into a size.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

UNDER_RESOLVED = 2.0
OVER_RESOLVED = 0.5


def cell_size(volumes) -> np.ndarray:
    """Edge length of the cube with each cell's volume."""
    return np.cbrt(np.maximum(np.asarray(volumes, dtype=float), 0.0))


def tet_size(volumes) -> np.ndarray:
    """Edge length of the regular tetrahedron with each element's volume."""
    return np.cbrt(np.maximum(np.asarray(volumes, dtype=float), 0.0)
                   * 6.0 * np.sqrt(2.0))


@dataclass(frozen=True)
class Conformity:
    ratio: np.ndarray
    under: np.ndarray
    over: np.ndarray

    @property
    def count(self) -> int:
        return int(len(self.ratio))

    @property
    def under_count(self) -> int:
        return int(self.under.sum())

    @property
    def over_count(self) -> int:
        return int(self.over.sum())

    @property
    def under_fraction(self) -> float:
        return self.under_count / self.count if self.count else 0.0

    @property
    def over_fraction(self) -> float:
        return self.over_count / self.count if self.count else 0.0

    def to_dict(self) -> dict:
        return {'cells': self.count, 'under': self.under_count,
                'over': self.over_count,
                'under_fraction': self.under_fraction,
                'over_fraction': self.over_fraction}


def conformity(field, centres, sizes, *, under=UNDER_RESOLVED,
               over=OVER_RESOLVED) -> Conformity:
    """Ratio of each cell's size to the field at its centre.

    Cells outside the octree (NaN field) get NaN and are in neither count.
    """
    sizes = np.asarray(sizes, dtype=float)
    wanted = field.lookup(centres)
    with np.errstate(invalid='ignore', divide='ignore'):
        ratio = sizes / wanted
    return Conformity(ratio, ratio > under, ratio < over)
