"""The size field a run produced, and a vectorised lookup over its octree.

Plan 41 section 12.2 S2. ``foammesh-sizefield`` 1.0.0 writes the octree as
integer leaf coordinates on a 2^19 grid (``leaves_ijk``), a level per leaf and
the size assigned to it. ``lookup`` answers "what size does the field give at
these points" without a tree walk: one ``searchsorted`` per level present.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

ROOT_LEVEL = 19
_BITS = 20


class SizeFieldError(RuntimeError):
    pass


def _encode(ijk: np.ndarray) -> np.ndarray:
    ijk = ijk.astype(np.uint64)
    return (ijk[:, 0] << np.uint64(2 * _BITS)) | (
        ijk[:, 1] << np.uint64(_BITS)) | ijk[:, 2]


@dataclass
class SizeField:
    origin: np.ndarray
    length: float
    leaves_ijk: np.ndarray
    leaves_level: np.ndarray
    leaves_size: np.ndarray
    meta: dict
    directory: Path | None = None

    def __post_init__(self):
        self.origin = np.asarray(self.origin, dtype=float)
        self._levels = {}
        for level in np.unique(self.leaves_level):
            where = np.nonzero(self.leaves_level == level)[0]
            keys = _encode(self.leaves_ijk[where])
            order = np.argsort(keys)
            self._levels[int(level)] = (keys[order], where[order])

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(cls, directory) -> 'SizeField':
        directory = Path(directory)
        try:
            meta = json.loads((directory / 'field.json').read_text('utf-8'))
            arrays = {name: np.load(directory / f'{name}.npy')
                      for name in ('leaves_ijk', 'leaves_level', 'leaves_size')}
        except (OSError, ValueError) as error:
            raise SizeFieldError(
                f'the size field in {directory} cannot be read: {error}'
            ) from error
        root = meta['root']
        return cls(np.array(root['origin'], dtype=float), float(root['length']),
                   arrays['leaves_ijk'], arrays['leaves_level'],
                   arrays['leaves_size'], meta, directory)

    # -- facts ------------------------------------------------------------- #

    @property
    def leaf_count(self) -> int:
        return int(len(self.leaves_level))

    @property
    def settings(self) -> dict:
        return dict(self.meta.get('settings') or {})

    @property
    def hbulk(self) -> float:
        return float(self.settings.get('hbulk', float(self.leaves_size.max())))

    @property
    def hmin(self) -> float:
        return float(self.settings.get('hmin', float(self.leaves_size.min())))

    def leaf_edges(self) -> np.ndarray:
        return self.length / (2.0 ** self.leaves_level.astype(float))

    def leaf_centres(self) -> np.ndarray:
        scale = self.length / float(2 ** ROOT_LEVEL)
        half = self.leaf_edges() / 2.0
        return (self.origin + self.leaves_ijk.astype(float) * scale
                + half[:, None])

    def per_surface(self) -> dict:
        return {row['name']: dict(row)
                for row in self.meta.get('per_surface') or ()}

    def estimated_hex_cells(self) -> int:
        """O7. The tool's estimate for the octree box."""
        return int(self.meta.get('estimated_hex_cells_in_octree_box')
                   or self.meta.get('estimated_hex_cells') or 0)

    def estimated_tets(self) -> int:
        return int(self.meta.get('estimated_tets_in_octree_box')
                   or self.meta.get('estimated_tets') or 0)

    # -- lookup ------------------------------------------------------------ #

    def lookup(self, points, outside=np.nan) -> np.ndarray:
        """Field size at each of *points* (N x 3); *outside* beyond the cube."""
        points = np.atleast_2d(np.asarray(points, dtype=float))
        grid = float(2 ** ROOT_LEVEL)
        scaled = (points - self.origin) / self.length * grid
        inside = np.all((scaled >= 0) & (scaled < grid), axis=1)
        result = np.full(len(points), outside, dtype=float)
        cell = np.floor(np.where(inside[:, None], scaled, 0)).astype(np.int64)
        todo = inside.copy()
        for level, (keys, index) in self._levels.items():
            if not todo.any():
                break
            shift = ROOT_LEVEL - level
            corner = (cell[todo] >> shift) << shift
            wanted = _encode(corner)
            at = np.minimum(np.searchsorted(keys, wanted), len(keys) - 1)
            hit = keys[at] == wanted
            rows = np.nonzero(todo)[0][hit]
            result[rows] = self.leaves_size[index[at[hit]]]
            todo[rows] = False
        return result

    def sample_along(self, origins, directions, distances) -> np.ndarray:
        """Sizes on rays: shape (rays, len(distances))."""
        origins = np.asarray(origins, dtype=float)
        directions = np.asarray(directions, dtype=float)
        distances = np.asarray(distances, dtype=float)
        points = (origins[:, None, :]
                  + directions[:, None, :] * distances[None, :, None])
        flat = self.lookup(points.reshape(-1, 3))
        return flat.reshape(len(origins), len(distances))
