#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Axis-aligned bounding box utilities."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BBox:
    xmin: float
    xmax: float
    ymin: float
    ymax: float
    zmin: float
    zmax: float

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.xmax - self.xmin, self.ymax - self.ymin, self.zmax - self.zmin)

    @property
    def diagonal(self) -> float:
        dx, dy, dz = self.size
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    @property
    def center(self) -> tuple[float, float, float]:
        return ((self.xmin + self.xmax) / 2,
                (self.ymin + self.ymax) / 2,
                (self.zmin + self.zmax) / 2)

    def to_tuple(self) -> tuple[float, float, float, float, float, float]:
        return (self.xmin, self.xmax, self.ymin, self.ymax, self.zmin, self.zmax)

    @classmethod
    def from_bounds(cls, bounds) -> 'BBox':
        """From a VTK-style (xmin, xmax, ymin, ymax, zmin, zmax) tuple."""
        return cls(*bounds)

    @classmethod
    def from_polydata(cls, polydata) -> 'BBox':
        return cls(*polydata.GetBounds())

    @classmethod
    def union(cls, boxes) -> 'BBox | None':
        boxes = list(boxes)
        if not boxes:
            return None
        return cls(
            min(b.xmin for b in boxes), max(b.xmax for b in boxes),
            min(b.ymin for b in boxes), max(b.ymax for b in boxes),
            min(b.zmin for b in boxes), max(b.zmax for b in boxes),
        )
