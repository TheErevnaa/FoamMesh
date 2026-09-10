#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Meshing-domain helpers (headless): boundary-layer math, y+ calculator."""

from .layers import (
    ShrinkAlgorithm, first_cell_height, yplus_from_height,
    total_thickness, final_layer_thickness, expansion_for_first_and_overall,
)

__all__ = [
    'ShrinkAlgorithm', 'first_cell_height', 'yplus_from_height',
    'total_thickness', 'final_layer_thickness', 'expansion_for_first_and_overall',
]
