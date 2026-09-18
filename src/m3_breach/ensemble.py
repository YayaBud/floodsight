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
from .breach_kernel import trapezoidal_breach_discharge, breach_width_at, breach_invert_at

logger = logging.getLogger(__name__)


@dataclass
class Hydrograph:
    """Time series Q(t) for one ensemble arm."""
    arm: str          # "pessimistic" | "central" | "optimistic"
    t_s: np.ndarray   # time [seconds]
    Q_m3s: np.ndarray # discharge [m³/s]
    params: BreachParams
    interval_Q_m3s: np.ndarray | None = None
    method_id: str | None = None
    #: Head on the breach invert at each ``t_s`` sample [m]. Filled in by
    #: ``route_breach``, which already computes it every step to evaluate the
    #: weir. It used to be discarded, which forced run_pipeline's jet-velocity
    #: estimate to fall back on a CONSTANT ``dam.height_m`` — a head that never
    #: falls as the reservoir empties, so the injected momentum stayed at its
    #: full-reservoir value through the whole recession.
    head_m: np.ndarray | None = None


#: Formation-time multiplier applied when dam.dam_type names a non-erodible
#: dam body (concrete/masonry). None of Froehlich/Von Thun/MacDonald
#: distinguish dam material in their formation-time equations (all three
#: regress purely on H_w/V_w) — this is an engineering-judgment factor, not a
#: regression coefficient from any of the three named methods. Reasoning: a
#: concrete or masonry dam's own body does not erode the way an earthfill
#: embankment does; a concrete/masonry dam failure is typically a structural
#: event (foundation/joint failure) or overtopping erosion of an EARTHEN
#: abutment/spillway around the rigid structure, which plays out over a
#: measurably longer formation time than an earthfill embankment's rapid,
#: full-breadth erosion. 4x is a stated, labelled assumption picked from
#: within the commonly-cited 3x-5x engineering-judgment range for
#: concrete-vs-earthfill breach formation time (not derived from any of this
#: module's own regressions) — see findings_results.md if a cited value ever
#: replaces it.
_NON_ERODIBLE_FORMATION_TIME_MULTIPLIER = 4.0
_NON_ERODIBLE_DAM_TYPES = {"concrete", "masonry"}


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

    # Non-erodible dam body (concrete/masonry): formation takes measurably
    # longer than earthfill's rapid full-breadth erosion (FS-52 / deep-review
    # §P — dam_type previously reached zero formation-time physics). Applied
    # only to the central (Froehlich) arm, since that is the only arm the
    # engineering-override test (and this codebase's ensemble consumers)
    # reads formation_time_h from.
    if dam.dam_type is not None and dam.dam_type.lower() in _NON_ERODIBLE_DAM_TYPES:
        f.formation_time_h *= _NON_ERODIBLE_FORMATION_TIME_MULTIPLIER

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
    # Finite support. A nonzero residual tail would release unbounded volume.
    Q[t > t_rise + t_fall] = 0.0

    return Hydrograph(arm=arm_label, t_s=t, Q_m3s=Q, params=params,
                      method_id=getattr(params, "method", None))


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
        sV, sH = np.asarray(stage_V, dtype=float), np.asarray(stage_h, dtype=float)
        if (not np.isfinite(sV).all() or not np.isfinite(sH).all() or
                np.any(np.diff(sV) <= 0) or np.any(np.diff(sH) < 0)):
            raise ValueError("stage-storage curve must be finite and ordered/monotone")

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
    heads: list[float] = []
    t_max = max_time_factor * t_f

    while t <= t_max:
        h_water = depth_of_volume(V)
        # The invert erodes from the crest (H) down to the base (0) over t_f —
        # shared linear kernel, same formula cascade.py's breach block uses.
        invert = breach_invert_at(t, t_f, invert_start_m=H, invert_final_m=0.0)
        width = breach_width_at(t, t_f, B_final)

        Q = trapezoidal_breach_discharge(h_water, invert, width, Z, cd_rect=_C_RECT, cd_side=_C_SIDE)
        # Bound discharge by water available during this interval. Spillway
        # and breach terms are routed separately elsewhere; this function only
        # represents breach release.
        Q = min(Q, max(0.0, V / dt_s + inflow_m3s))

        times.append(t)
        flows.append(Q)
        # Head on the invert, not the water depth: this is the quantity the
        # weir sees and the quantity a jet velocity Q/(width*head) needs.
        heads.append(max(0.0, h_water - invert))

        # Never drain more than is left in the step.
        dV = (Q - inflow_m3s) * dt_s
        V = max(0.0, V - dV)
        t += dt_s

        if V <= 1e-4 * V0 and t > t_f:
            times.append(t)
            flows.append(0.0)
            heads.append(max(0.0, depth_of_volume(V) - breach_invert_at(t, t_f, H, 0.0)))
            break

    t_arr = np.asarray(times, dtype=float)
    Q_arr = np.asarray(flows, dtype=float)
    head_arr = np.asarray(heads, dtype=float)

    released = float(np.trapezoid(Q_arr, t_arr))
    logger.info(
        "%s breach routed: Q_peak = %.0f m^3/s at T+%.0f min, "
        "released %.3e m^3 of %.3e (%.1f%%); regression Q_p = %.0f m^3/s",
        arm_label, Q_arr.max(), t_arr[int(Q_arr.argmax())] / 60.0,
        released, V0, 100.0 * released / V0 if V0 else 0.0,
        params.peak_discharge_m3s,
    )
    interval = 0.5 * (Q_arr[:-1] + Q_arr[1:])
    return Hydrograph(arm=arm_label, t_s=t_arr, Q_m3s=Q_arr, params=params,
                      interval_Q_m3s=interval, head_m=head_arr,
                      method_id=getattr(params, "method", None))


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
                                     params=hg.params, head_m=hg.head_m,
                                     interval_Q_m3s=hg.interval_Q_m3s,
                                     method_id=hg.method_id))
    return tuple(relabelled)  # type: ignore[return-value]
