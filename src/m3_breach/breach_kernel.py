"""
FloodSight — Shared trapezoidal breach-discharge kernel
========================================================
One formula, used by both ``ensemble.route_breach`` (the non-cascade,
own-time-loop breach router) and ``cascade.simulate_reservoir_cascade`` (the
cascade path's single continuous per-timestep loop). Extracted so the two
paths cannot silently diverge again — see memory.md's "Overtopping-erosion
breach kernel" P2 gate decision and FLOODSIGHT_DEEP_REVIEW_2026-09-11.md's
mechanism-status table (progressive embankment breach was "Partial,
disagreeing": two independently-coded kernels, not reconciled).

Growth is LINEAR for both width and invert erosion — this replaces
cascade.py's previous uncited ``(elapsed/t_f)**1.2`` / ``**0.8`` exponents.
"""

from __future__ import annotations


def trapezoidal_breach_discharge(
    h_water: float,        # current water surface elevation [m]
    invert_elev: float,    # current breach invert elevation [m] (erodes over time)
    width: float,          # current breach top width [m] (grows over time)
    side_slope_hv: float,  # Z, trapezoidal side slope
    cd_rect: float = 1.7,   # matches ensemble.py's _C_RECT
    cd_side: float = 1.35,  # matches ensemble.py's _C_SIDE
) -> float:
    """One instant's breach discharge through a trapezoidal opening:
    rectangular weir term + triangular side-slope term.

    ``h_water`` and ``invert_elev`` are absolute elevations (or any shared
    consistent datum) — only their difference (head) matters.
    """
    head = max(0.0, h_water - invert_elev)
    return max(0.0, cd_rect * width * head ** 1.5 + cd_side * side_slope_hv * head ** 2.5)


def breach_width_at(elapsed_s: float, formation_s: float, final_width_m: float) -> float:
    """Linear breach-width growth from 0 to final_width_m over formation_s."""
    return final_width_m * min(1.0, elapsed_s / max(1e-9, formation_s))


def breach_invert_at(
    elapsed_s: float,
    formation_s: float,
    invert_start_m: float,
    invert_final_m: float,
) -> float:
    """Linear breach-invert erosion from invert_start_m to invert_final_m over
    formation_s."""
    frac = min(1.0, elapsed_s / max(1e-9, formation_s))
    return invert_start_m - (invert_start_m - invert_final_m) * frac
