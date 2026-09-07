"""
Ritter (1892) analytical dam-break solution
============================================
Provides the EXACT solution for a 1D dam break on a frictionless, flat,
horizontal bed — the standard validation benchmark for shallow-water solvers.

Both ANUGA and PySPH SWE-SPH must reproduce this before being trusted on
real terrain. A match proves numerical correctness; a mismatch points to a
bug before the demo, not during it.

Equations
---------
For t > 0, x in [-L, L] relative to the dam face:

    Dry-bed downstream (h_0 = 0):
        c_0 = sqrt(g * h_1)   [initial wave speed on wet side]

    Wave front speed: x_f = 2 * c_0
    Rarefaction wave: spans  -c_0 <= x/t <= 2*c_0

    h(x, t) = (1/9g) * (2*c_0 - x/t)^2    for -c_0*t <= x <= 2*c_0*t
    u(x, t) = (2/3)  * (c_0 + x/t)         for -c_0*t <= x <= 2*c_0*t
    h = h_1, u = 0                          for x < -c_0*t
    h = 0,   u = 0                          for x > 2*c_0*t

Reference: Ritter, A. (1892). Die Fortpflanzung der Wasserwellen.
    Zeitschrift des VDI, 36, 947–954.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass

_G = 9.81


@dataclass
class RitterResult:
    x: np.ndarray    # spatial positions [m]
    t: float         # time since breach [s]
    h: np.ndarray    # water depth [m]
    u: np.ndarray    # depth-averaged velocity [m/s]
    h1: float        # initial upstream depth [m]
    c0: float        # initial wave celerity [m/s]


def solve(h1: float, t: float, x: np.ndarray | None = None,
          x_min: float = -5000.0, x_max: float = 5000.0,
          n_points: int = 1000) -> RitterResult:
    """
    Compute the Ritter solution at time t after dam break.

    Parameters
    ----------
    h1       : upstream depth at the dam face [m]
    t        : time since instantaneous breach [s]
    x        : spatial array [m] relative to dam face (optional)
    x_min/x_max : domain limits if x is auto-generated
    n_points : resolution if x is auto-generated
    """
    if t <= 0:
        raise ValueError("t must be > 0 (the solution is instantaneous at t=0)")

    c0 = np.sqrt(_G * h1)
    if x is None:
        x = np.linspace(x_min, x_max, n_points)

    h = np.zeros_like(x, dtype=float)
    u = np.zeros_like(x, dtype=float)

    # Region: undisturbed upstream (x < -c0*t)
    mask_up = x < -c0 * t
    h[mask_up] = h1
    u[mask_up] = 0.0

    # Region: rarefaction wave (-c0*t <= x <= 2*c0*t)
    mask_rare = (x >= -c0 * t) & (x <= 2.0 * c0 * t)
    h[mask_rare] = (1.0 / (9.0 * _G)) * (2.0 * c0 - x[mask_rare] / t) ** 2
    u[mask_rare] = (2.0 / 3.0) * (c0 + x[mask_rare] / t)

    # Region: dry downstream (x > 2*c0*t) — zeros already set

    return RitterResult(x=x, t=t, h=h, u=u, h1=h1, c0=c0)


def rmse(h_sim: np.ndarray, h_ritter: np.ndarray) -> float:
    """Root-mean-square error between a simulation and the Ritter solution."""
    return float(np.sqrt(np.mean((h_sim - h_ritter) ** 2)))
