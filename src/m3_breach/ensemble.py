"""
M3 — Breach Ensemble
====================
Combines three breach parameter methods into a pessimistic / central /
optimistic ensemble and generates N outflow hydrographs Q(t).

Ensemble logic
--------------
- **Central**     : Froehlich (2008)  — CWC-recommended for Indian dams
- **Pessimistic** : arm with highest Q_p among the three methods
- **Optimistic**  : arm with lowest  Q_p among the three methods

Hydrograph shape
----------------
A simplified triangular hydrograph (USBR SCS-style) is used:
    - rises linearly from 0 to Q_p over 0.5 * t_f
    - falls linearly from Q_p to 0.1*Q_p over 1.5 * t_f
This is adequate for far-field routing; the near-field SWE-SPH run uses
a higher-fidelity parameterisation built from the breach geometry.
"""

from __future__ import annotations

import logging
import numpy as np
from dataclasses import dataclass
from typing import Tuple

from . import DamGeometry, BreachParams, BreachEnsemble
from . import froehlich, von_thun, macdonald

logger = logging.getLogger(__name__)


@dataclass
class Hydrograph:
    """Time series Q(t) for one ensemble arm."""
    arm: str          # "pessimistic" | "central" | "optimistic"
    t_s: np.ndarray   # time [seconds]
    Q_m3s: np.ndarray # discharge [m³/s]
    params: BreachParams


def build_ensemble(dam: DamGeometry) -> BreachEnsemble:
    """Run all three methods and return the labelled ensemble, calibrated by engineering specs if present."""
    f  = froehlich.compute(dam)
    vt = von_thun.compute(dam)
    ml = macdonald.compute(dam)

    arms = [f, vt, ml]

    # Engineering dossier calibration
    if dam.crest_length_m is not None and dam.crest_length_m > 0:
        for arm in arms:
            if arm.breach_width_m > dam.crest_length_m:
                ratio = dam.crest_length_m / arm.breach_width_m
                arm.breach_width_m = float(dam.crest_length_m)
                arm.peak_discharge_m3s *= ratio

    if dam.dam_type in ("concrete", "masonry"):
        # Concrete/masonry structures exhibit slower erosion and partial breaches (CWC 2018)
        for arm in arms:
            arm.formation_time_h *= 2.0
            arm.peak_discharge_m3s *= 0.7

    if dam.spillway_capacity_m3s is not None and dam.spillway_capacity_m3s > 0:
        for arm in arms:
            arm.peak_discharge_m3s += float(dam.spillway_capacity_m3s)

    arms_sorted = sorted(arms, key=lambda p: p.peak_discharge_m3s)

    return BreachEnsemble(
        pessimistic=arms_sorted[-1],    # highest Q_p
        central=f,                      # Froehlich always central (CWC)
        optimistic=arms_sorted[0],      # lowest  Q_p
        dam=dam,
    )


def make_hydrograph(params: BreachParams, arm_label: str,
                    dt_s: float = 60.0) -> Hydrograph:
    """
    Build a triangular outflow hydrograph.

    Parameters
    ----------
    params    : BreachParams from one ensemble arm
    arm_label : "pessimistic" | "central" | "optimistic"
    dt_s      : time step in seconds (default 60 s)
    """
    t_f_s = params.formation_time_h * 3600.0
    Q_p   = params.peak_discharge_m3s

    t_rise  = 0.5 * t_f_s
    t_fall  = 1.5 * t_f_s
    t_total = t_rise + t_fall + 0.5 * t_f_s   # a little tail

    t = np.arange(0, t_total + dt_s, dt_s)
    Q = np.zeros_like(t)

    # Rising limb
    mask_rise = t <= t_rise
    Q[mask_rise] = Q_p * (t[mask_rise] / t_rise)

    # Falling limb
    mask_fall = (t > t_rise) & (t <= t_rise + t_fall)
    frac = (t[mask_fall] - t_rise) / t_fall
    Q[mask_fall] = Q_p * (1.0 - 0.9 * frac)   # falls to 0.1*Q_p

    # Tail (baseflow-like residual)
    Q[t > t_rise + t_fall] = 0.1 * Q_p

    return Hydrograph(arm=arm_label, t_s=t, Q_m3s=Q, params=params)


# ──────────────────────────────────────────────────────────────────────────────
# Physically routed breach outflow
# ──────────────────────────────────────────────────────────────────────────────

# Broad-crested weir coefficients for a trapezoidal breach (NWS DAMBRK, SI).
_C_RECT = 1.7      # rectangular part:      Q = C * B * head^1.5
_C_SIDE = 1.35     # triangular side slopes: Q = C * Z * head^2.5


def route_breach(
    dam: DamGeometry,
    params: BreachParams,
    arm_label: str,
    dt_s: float = 30.0,
    stage_h: np.ndarray | None = None,
    stage_V: np.ndarray | None = None,
    inflow_m3s: float = 0.0,
    storage_exponent: float = 3.0,
    max_time_factor: float = 12.0,
) -> Hydrograph:
    """
    Outflow hydrograph from weir flow through a growing breach, coupled to
    reservoir depletion.

    Why this replaced the triangular hydrograph
    -------------------------------------------
    The triangle was assembled from the regression values ``Q_p`` and ``t_f``,
    so the area under it bore no relation to the water actually impounded, and
    its tail sat at ``0.1 * Q_p`` indefinitely — which integrates to infinite
    volume. Here ``Q(t)`` is emergent: the breach cuts down and widens, weir
    flow follows the head over the invert, and the reservoir drains through the
    stage-storage curve. Total outflow equals the impounded volume by
    construction, so the mass balance downstream means something.

    Parameters
    ----------
    stage_h, stage_V : optional stage-storage curve from M2. Without one, a
        power law ``V = V0 (h/H)^b`` stands in, with ``b = 3`` for a steep
        valley — narrow at the bottom, wider higher up.
    inflow_m3s : steady inflow to the impoundment during the breach.
    storage_exponent : ``b`` above. Only used when no curve is supplied.
    """
    H  = float(dam.height_m)          # head above the breach invert at failure
    V0 = float(dam.volume_m3)
    Z  = float(params.side_slope_hv)
    B_final = float(params.breach_width_m)
    t_f = max(float(params.formation_time_h) * 3600.0, dt_s)

    if stage_h is not None and stage_V is not None and len(stage_h) > 1:
        # Invert the measured curve: depth as a function of remaining volume.
        order = np.argsort(stage_V)
        sV, sH = np.asarray(stage_V)[order], np.asarray(stage_h)[order]

        def depth_of_volume(V: float) -> float:
            return float(np.interp(V, sV, sH))
    else:
        def depth_of_volume(V: float) -> float:
            if V <= 0.0 or V0 <= 0.0:
                return 0.0
            return H * (V / V0) ** (1.0 / storage_exponent)

    t = 0.0
    V = V0
    times: list[float] = []
    flows: list[float] = []
    t_max = max_time_factor * t_f

    while t <= t_max:
        h_water = depth_of_volume(V)
        # The invert erodes from the crest down to the base over t_f.
        invert = H * max(0.0, 1.0 - t / t_f)
        head = max(0.0, h_water - invert)
        width = B_final * min(1.0, t / t_f)

        Q = _C_RECT * width * head ** 1.5 + _C_SIDE * Z * head ** 2.5
        Q = max(0.0, Q)

        times.append(t)
        flows.append(Q)

        # Never drain more than is left in the step.
        dV = (Q - inflow_m3s) * dt_s
        V = max(0.0, V - dV)
        t += dt_s

        if V <= 1e-4 * V0 and t > t_f:
            times.append(t)
            flows.append(0.0)
            break

    t_arr = np.asarray(times, dtype=float)
    Q_arr = np.asarray(flows, dtype=float)

    released = float(np.trapezoid(Q_arr, t_arr))
    logger.info(
        "%s breach routed: Q_peak = %.0f m^3/s at T+%.0f min, "
        "released %.3e m^3 of %.3e (%.1f%%); regression Q_p = %.0f m^3/s",
        arm_label, Q_arr.max(), t_arr[int(Q_arr.argmax())] / 60.0,
        released, V0, 100.0 * released / V0 if V0 else 0.0,
        params.peak_discharge_m3s,
    )
    return Hydrograph(arm=arm_label, t_s=t_arr, Q_m3s=Q_arr, params=params)


def get_hydrographs(dam: DamGeometry,
                    dt_s: float = 60.0,
                    routed: bool = True,
                    stage_h: np.ndarray | None = None,
                    stage_V: np.ndarray | None = None,
                    ) -> Tuple[Hydrograph, Hydrograph, Hydrograph]:
    """
    Returns (pessimistic, central, optimistic) hydrographs for the dam.

    ``routed=True`` uses the physically routed breach, whose released volume
    matches the impoundment. ``routed=False`` falls back to the triangular
    regression shape, kept only for comparison.
    """
    ensemble = build_ensemble(dam)
    arms = (("pessimistic", ensemble.pessimistic),
            ("central",     ensemble.central),
            ("optimistic",  ensemble.optimistic))

    if not routed:
        return tuple(make_hydrograph(p, label, dt_s) for label, p in arms)  # type: ignore

    # Route every arm, then RE-LABEL by the routed peak. The ensemble names come
    # from the regression Q_p, but routing through the reservoir reorders them —
    # a wide, slow breach can out-peak a narrow, fast one. Keeping the original
    # labels would put "pessimistic" on an arm that is not the worst case.
    routed_arms = [
        route_breach(dam, p, label, dt_s=dt_s, stage_h=stage_h, stage_V=stage_V)
        for label, p in arms
    ]
    routed_arms.sort(key=lambda hg: float(hg.Q_m3s.max()))
    optimistic, central, pessimistic = routed_arms

    relabelled = []
    for hg, label in ((pessimistic, "pessimistic"), (central, "central"),
                      (optimistic, "optimistic")):
        if hg.arm != label:
            logger.info("  arm from %s re-labelled %s by routed peak (%.0f m^3/s)",
                        hg.params.method, label, float(hg.Q_m3s.max()))
        relabelled.append(Hydrograph(arm=label, t_s=hg.t_s, Q_m3s=hg.Q_m3s,
                                     params=hg.params))
    return tuple(relabelled)  # type: ignore[return-value]
