#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Non-destructive transform stack for imported geometry.

Scale / rotate / translate are stored as an ordered list of operations rather
than baked into the geometry, so they survive save/reload and can be reverted —
fixing the class of bug where a transform "reverts after OK" because it was never
persisted. The GUI applies the composed matrix for preview; committing the stack
through ``ProjectState`` is what makes it durable.

Rotations are in degrees about the X, Y, Z axes. The composed matrix is a 4x4
homogeneous transform (numpy).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


VALID_KINDS = ('scale', 'rotate', 'translate')


@dataclass
class TransformOp:
    kind: str               # 'scale' | 'rotate' | 'translate'
    values: tuple[float, float, float]

    def __post_init__(self):
        if self.kind not in VALID_KINDS:
            raise ValueError(f'invalid transform kind: {self.kind!r}')
        self.values = tuple(float(v) for v in self.values)

    def matrix(self) -> np.ndarray:
        x, y, z = self.values
        if self.kind == 'scale':
            m = np.diag([x, y, z, 1.0])
            return m
        if self.kind == 'translate':
            m = np.identity(4)
            m[0, 3], m[1, 3], m[2, 3] = x, y, z
            return m
        # rotate: apply Rz @ Ry @ Rx (degrees)
        rx, ry, rz = math.radians(x), math.radians(y), math.radians(z)
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        Rx = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]])
        Ry = np.array([[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]])
        Rz = np.array([[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        return Rz @ Ry @ Rx

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'values': list(self.values)}

    @classmethod
    def from_dict(cls, d: dict) -> 'TransformOp':
        return cls(kind=d['kind'], values=tuple(d['values']))


class TransformStack:
    """Ordered, non-destructive transform operations."""

    def __init__(self, ops: list[TransformOp] | None = None):
        self._ops: list[TransformOp] = list(ops or [])

    # building -------------------------------------------------------------
    def scale(self, x: float, y: float = None, z: float = None) -> 'TransformStack':
        if y is None:
            y = z = x
        self._ops.append(TransformOp('scale', (x, y, z)))
        return self

    def rotate(self, rx: float, ry: float, rz: float) -> 'TransformStack':
        self._ops.append(TransformOp('rotate', (rx, ry, rz)))
        return self

    def translate(self, tx: float, ty: float, tz: float) -> 'TransformStack':
        self._ops.append(TransformOp('translate', (tx, ty, tz)))
        return self

    def push(self, op: TransformOp) -> 'TransformStack':
        self._ops.append(op)
        return self

    def pop(self) -> TransformOp | None:
        return self._ops.pop() if self._ops else None

    def clear(self) -> None:
        self._ops.clear()

    @property
    def ops(self) -> list[TransformOp]:
        return list(self._ops)

    def is_identity(self) -> bool:
        return not self._ops

    # evaluation -----------------------------------------------------------
    def matrix(self) -> np.ndarray:
        """Composed 4x4 matrix: ops applied in insertion order."""
        m = np.identity(4)
        for op in self._ops:
            m = op.matrix() @ m
        return m

    def apply(self, points: np.ndarray) -> np.ndarray:
        """Apply to an (N, 3) array of points, returning (N, 3)."""
        pts = np.asarray(points, dtype=float)
        homog = np.hstack([pts, np.ones((pts.shape[0], 1))])
        out = (self.matrix() @ homog.T).T
        return out[:, :3]

    # persistence ----------------------------------------------------------
    def to_list(self) -> list[dict]:
        return [op.to_dict() for op in self._ops]

    @classmethod
    def from_list(cls, items: list[dict]) -> 'TransformStack':
        return cls([TransformOp.from_dict(i) for i in (items or [])])
