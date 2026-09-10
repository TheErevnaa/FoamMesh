#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Boundary-layer math: y+ / first-cell-height calculator and layer thicknesses.

snappyHexMesh supports two Foundation-v13 shrink algorithms; both are exposed
axis. FoamMesh exposes both (the dict writer emits the chosen one).
"""
from __future__ import annotations

import math
from enum import Enum


class ShrinkAlgorithm(str, Enum):
    # Only the medial-axis mover is registered as an
    # externalDisplacementMeshMover in OpenFOAM Foundation 13.
    MEDIAL_AXIS = 'displacementMedialAxis'


def first_cell_height(velocity: float, length: float, nu: float,
                      target_yplus: float, rho: float = 1.0) -> float:
    """Estimate the wall-adjacent cell height for a target y+.

    Flat-plate turbulent correlation:  Cf = 0.026 / Re^(1/7),
    tau_w = Cf * 1/2 * rho * U^2,  u_tau = sqrt(tau_w/rho),  y = y+ * nu / u_tau.
    nu is kinematic viscosity (m^2/s). Returns wall distance to the cell centre;
    first-layer height ~ 2*y.
    """
    if min(velocity, length, nu, target_yplus, rho) <= 0:
        raise ValueError('all inputs must be positive')
    Re = velocity * length / nu
    Cf = 0.026 / Re ** (1.0 / 7.0)
    tau_w = Cf * 0.5 * rho * velocity ** 2
    u_tau = math.sqrt(tau_w / rho)
    return target_yplus * nu / u_tau


def yplus_from_height(y: float, velocity: float, length: float, nu: float,
                      rho: float = 1.0) -> float:
    """Inverse of :func:`first_cell_height` — y+ for a given wall distance."""
    if min(y, velocity, length, nu, rho) <= 0:
        raise ValueError('all inputs must be positive')
    Re = velocity * length / nu
    Cf = 0.026 / Re ** (1.0 / 7.0)
    tau_w = Cf * 0.5 * rho * velocity ** 2
    u_tau = math.sqrt(tau_w / rho)
    return y * u_tau / nu


def total_thickness(first: float, ratio: float, n: int) -> float:
    """Total stack thickness of *n* geometric layers (first layer + ratio)."""
    if n <= 0:
        return 0.0
    if abs(ratio - 1.0) < 1e-12:
        return first * n
    return first * (ratio ** n - 1.0) / (ratio - 1.0)


def final_layer_thickness(first: float, ratio: float, n: int) -> float:
    return first * ratio ** (n - 1) if n > 0 else 0.0


def expansion_for_first_and_overall(first: float, overall: float, n: int,
                                    tol: float = 1e-9, max_iter: int = 200) -> float:
    """Solve the expansion ratio so *n* layers starting at *first* sum to *overall*.

    Bisection on total_thickness(first, ratio, n) == overall.
    """
    if n <= 0 or first <= 0 or overall <= 0:
        raise ValueError('first, overall must be > 0 and n > 0')
    if abs(overall - first * n) < tol:
        return 1.0
    lo, hi = (1.0, 10.0) if overall > first * n else (0.1, 1.0)
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        t = total_thickness(first, mid, n)
        if abs(t - overall) < tol:
            return mid
        if t < overall:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
