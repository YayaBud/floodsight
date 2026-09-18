"""
FloodSight — M3 Hydrologic Cascade & Breach Hydrograph
======================================================
Provides both the Annamayya-specific cascade (Pincha -> Annamayya,
evidence-anchored) and a generic scenario-driven cascade that can model
any upstream-dam-failure -> downstream-reservoir routing, driven by a
configuration dict rather than hard-coded constants.

Generic cascade config schema (``cascade`` key in SCENARIOS dict):
  upstream:
    peak_q_m3s           : upstream dam peak breach discharge  [m3/s]
    base_q_m3s           : pre-failure background flow         [m3/s, default 50]
    duration_s           : breach pulse duration               [s, default 5400]
    lead_time_s          : time before t=0 that upstream fails [s]
    classification       : epistemic label (OBSERVED, OFFICIAL ESTIMATE, etc.)
  routing:
    k_s                  : Muskingum K travel time             [s]
    x                    : Muskingum X weighting factor        [0-0.5]
    classification       : epistemic label
  reservoir:
    z_bed_m              : thalweg / breach invert elevation   [m MSL]
    z_frl_m              : full reservoir level                [m MSL]
    z_crest_m            : dam crest (overtopping threshold)   [m MSL]
    v_frl_mcm            : gross storage at FRL                [MCM]
    alpha_exp            : exponent for V(h) = alpha * h^exp   [default 2.5]
    classification       : epistemic label
  spillway:
    cd                   : weir discharge coefficient          [default 2.15]
    length_m             : effective spillway crest length      [m]
    z_crest_m            : spillway crest elevation            [m MSL]
    max_q_m3s            : maximum rated spillway discharge    [m3/s]
  catchment_runoff:
    base_m3s             : background tributary inflow         [m3/s, default 0]
    peak_m3s             : peak catchment runoff               [m3/s, default 0]
    peak_offset_s        : time offset of peak from t=0        [s, default -3600]
    sigma_s              : Gaussian width of runoff pulse       [s, default 5400]
  breach_ensemble:
    optimistic:
      width_m            : breach width                        [m]
      formation_s        : time for full breach development    [s]
      peak_q_m3s         : empirical peak envelope             [m3/s]
      source             : citation string
    central:   (same keys)
    pessimistic: (same keys)

Authoritative Evidence Alignment (ANNAMAYYA_DATA_AUDIT.md):
-----------------------------------------------------------
- EVD-04: Pincha washout at ~03:30 AM IST (T-180 min against the 06:30 T=0).
- EVD-05: Pincha surge ~3,964 m3/s (1.40 lakh cusecs).
- EVD-06 / EVD-07: 34 km reach, Muskingum travel time ~105-135 min (K=7200s, X=0.20).
- EVD-08 to EVD-11: Annamayya FRL +203.6m (63.4 MCM), Crest +206.0m, Bed +180.0m.
- EVD-14: Spillway 4 gates operating, ~4,136 m3/s.
- EVD-18: 336 m eroded earth-dam section is a structural upper bound, NOT the
          hydraulic breach width.
- EVD-19 / EVD-20: Empirical breach ensemble (B_avg = 110-150m Central, Froehlich 2008).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional
import numpy as np

from . import FailureMechanism, require_implemented_mechanism
from .breach_kernel import trapezoidal_breach_discharge, breach_width_at, breach_invert_at

logger = logging.getLogger(__name__)


def route_muskingum_1d(
    t_s: np.ndarray,
    q_in_m3s: np.ndarray,
    k_s: float = 7200.0,
    x: float = 0.20,
) -> np.ndarray:
    """
    1D Muskingum flood routing along the Pincha -> Annamayya Cheyyeru reach (34 km).
    
    Parameters
    ----------
    t_s : np.ndarray
        Simulation time array in seconds.
    q_in_m3s : np.ndarray
        Upstream inflow hydrograph (m³/s).
    k_s : float
        Travel time parameter K in seconds (7200s = 2.0 hours).
    x : float
        Dimensionless weighting factor (0.0 to 0.5, typical natural river = 0.2).
        
    Returns
    -------
    q_out_m3s : np.ndarray
        Routed downstream hydrograph at Annamayya reservoir inlet.
    """
    dt = float(t_s[1] - t_s[0]) if len(t_s) > 1 else 60.0
    denom = 2.0 * k_s * (1.0 - x) + dt
    c0 = (dt - 2.0 * k_s * x) / denom
    c1 = (dt + 2.0 * k_s * x) / denom
    c2 = (2.0 * k_s * (1.0 - x) - dt) / denom
    
    q_out = np.zeros_like(q_in_m3s)
    q_out[0] = q_in_m3s[0]
    
    for i in range(1, len(t_s)):
        q_out[i] = c0 * q_in_m3s[i] + c1 * q_in_m3s[i - 1] + c2 * q_out[i - 1]
        if q_out[i] < 0.0:
            q_out[i] = 0.0
            
    return q_out


def generate_pincha_outflow(
    t_s: np.ndarray,
    t_failure_s: float = 0.0,
    peak_q_m3s: float = 3964.0,   # 1.40 lakh cusecs
    base_q_m3s: float = 300.0,
    duration_s: float = 5400.0,   # ~1.5 hour pulse
) -> np.ndarray:
    """Generate the Pincha ring-bund failure breach surge hydrograph."""
    q = np.full_like(t_s, base_q_m3s, dtype=float)
    dt_rel = t_s - t_failure_s
    active = (dt_rel >= 0.0) & (dt_rel <= duration_s * 2.0)
    
    # Asymmetric triangular/gamma pulse
    t_peak = duration_s * 0.35
    for i in np.where(active)[0]:
        t = dt_rel[i]
        if t <= t_peak:
            pulse = (t / t_peak) * (peak_q_m3s - base_q_m3s)
        else:
            pulse = ((duration_s * 2.0 - t) / (duration_s * 2.0 - t_peak)) * (peak_q_m3s - base_q_m3s)
        q[i] = base_q_m3s + max(0.0, pulse)
        
    return q


@dataclass
class ReservoirCascadeResult:
    """One continuous reservoir trajectory: pre-trigger rise through post-trigger breach."""
    t_s: np.ndarray                    # full timeline, negative through positive
    q_upstream_m3s: np.ndarray
    q_inflow_reservoir_m3s: np.ndarray
    q_spillway_m3s: np.ndarray
    q_breach_m3s: np.ndarray
    q_total_outflow_m3s: np.ndarray
    reservoir_storage_m3: np.ndarray
    reservoir_elevation_m: np.ndarray
    t_trigger_s: float | None          # None if failure never initiates
    breach_width_m: np.ndarray         # 0 until triggered, grows after
    breach_invert_m: np.ndarray        # z_crest until triggered, erodes after

    v_initial_m3: float
    v_inflow_total_m3: float
    v_outflow_total_m3: float
    v_breach_total_m3: float
    v_spill_total_m3: float
    v_final_m3: float
    mass_error_m3: float
    mass_error_pct: float

    ensemble_metadata: dict


def _check_trigger(z: float, cfg: dict, mechanism: FailureMechanism) -> bool:
    """Failure-initiation test, dispatched by mechanism (Stage D).

    Both mechanisms this codebase implements trigger identically (crest
    overtopping) — see module docstring / Stage D+E report for the finding
    that no distinguishing initiation criterion for progressive breach exists
    in this codebase or is sourced anywhere in it. Any other mechanism value
    raises via require_implemented_mechanism rather than falling through to
    one of these two physics paths under a different label.
    """
    if mechanism == FailureMechanism.OVERTOPPING_EROSION:
        return z >= float(cfg["reservoir"]["z_crest_m"])
    if mechanism == FailureMechanism.PROGRESSIVE_BREACH:
        # Progressive embankment breach is triggered the same way as
        # overtopping erosion (level reaching crest) -- the DISTINCTION
        # between these two mechanisms in this codebase is in how the
        # breach then evolves, not in what starts it. Neither this
        # codebase nor FLOODSIGHT_DEEP_REVIEW_2026-09-11.md's table
        # describes a different, non-overtopping initiation criterion for
        # progressive breach specifically -- if a real distinguishing
        # trigger condition exists in the dam-safety literature, it is not
        # sourced anywhere in this repository and must not be invented
        # here. Flagged as an open finding, see Stage D+E report.
        return z >= float(cfg["reservoir"]["z_crest_m"])
    require_implemented_mechanism(mechanism)  # raises for every other value
    return False  # unreachable, satisfies type checkers


def reconcile_stage_storage(
    res_cfg: dict,
    dem_z_min_m: float,
    dem_stage_h: np.ndarray,
    dem_stage_V: np.ndarray,
    dem_wse_m: float,
) -> dict:
    """Stage C — compare the cascade's analytical power-law stage-storage
    curve against a DEM-derived one, at a single defensible comparison point.

    Comparison point: **volume at FRL** (`z_frl_m`). The analytical law is
    fitted so V(z_frl) == v_frl_mcm by construction — the real question is
    "does the DEM curve, sampled independently from terrain, agree with the
    *configured* v_frl at that same absolute elevation." This is meaningful
    because both curves are asked about the same physical thing (storage at
    full reservoir level) even though they are parameterized completely
    differently (the analytical law is a global 2-parameter fit; the DEM
    curve is a locally sampled integral from `z_min` to `wse_m`). Any other
    single-z comparison point would either be arbitrary (some z with no
    physical meaning) or degenerate (z_bed, where both curves trivially read
    ~0) — FRL is the one elevation both curves have an opinion about that
    also matters physically (it is the reservoir's declared operating level).

    ``dem_stage_h``/``dem_stage_V`` are `ImpoundmentGeometry.stage_curve_h`/
    `stage_curve_V` — stage ABOVE THALWEG (i.e. `absolute_z - dem_z_min_m`),
    not absolute elevation. This function reconstructs absolute elevation as
    `dem_z_min_m + dem_stage_h` before comparing against the cascade config's
    absolute z_frl_m, since `res_cfg`'s elevations are absolute MSL.

    Parameters
    ----------
    res_cfg      : cascade config's `reservoir` block (has z_bed_m, z_frl_m,
                   v_frl_mcm, alpha_exp).
    dem_z_min_m  : `ImpoundmentGeometry.wse_m - ImpoundmentGeometry.dam_height_m`
                   (the connected pool's floor elevation, absolute).
    dem_stage_h  : `ImpoundmentGeometry.stage_curve_h`.
    dem_stage_V  : `ImpoundmentGeometry.stage_curve_V`.
    dem_wse_m    : the absolute WSE the DEM curve was built to (the upper end
                   of its sampled domain).

    Note on `reconciliation_status`: this function does not know the
    agree/disagree threshold (that is a pipeline-level policy decision, see
    `run_pipeline.py` / `findings_results.md`), so when a comparison was
    actually possible it reports the *un-thresholded* status `"COMPARED"`
    (gap fields are populated; the caller classifies AGREE vs
    DISAGREE_SURFACED by applying its own threshold to `reconciliation_gap_pct`).
    The one status this function does decide on its own is
    `"FRL_BELOW_DEM_POOL_FLOOR"`, since that is a structural fact about the
    two domains, not a threshold judgment.

    Returns
    -------
    dict with keys:
      reconciliation_status : "COMPARED" | "FRL_BELOW_DEM_POOL_FLOOR"
      reconciliation_gap_m3 : |V_dem(z_frl) - v_frl_m3|, or None if status is
                               FRL_BELOW_DEM_POOL_FLOOR (no DEM volume exists
                               at z_frl to compare against).
      reconciliation_gap_pct: reconciliation_gap_m3 / v_frl_m3 * 100, or None.
      v_frl_m3               : configured volume at FRL (m3).
      v_dem_at_frl_m3         : DEM-interpolated volume at z_frl (m3), or None.
      z_frl_m / dem_z_min_m / dem_wse_m : elevations used, for audit.
    """
    z_frl = float(res_cfg["z_frl_m"])
    v_frl_m3 = float(res_cfg["v_frl_mcm"]) * 1e6

    dem_z_abs = dem_z_min_m + np.asarray(dem_stage_h, dtype=float)
    dem_V = np.asarray(dem_stage_V, dtype=float)

    if z_frl < dem_z_min_m:
        return {
            "reconciliation_status": "FRL_BELOW_DEM_POOL_FLOOR",
            "reconciliation_gap_m3": None,
            "reconciliation_gap_pct": None,
            "v_frl_m3": v_frl_m3,
            "v_dem_at_frl_m3": None,
            "z_frl_m": z_frl,
            "dem_z_min_m": dem_z_min_m,
            "dem_wse_m": dem_wse_m,
        }

    # z_frl within [dem_z_min_m, dem_wse_m]: np.interp clamps above dem_wse_m
    # too, so a z_frl slightly above the DEM curve's sampled top (e.g. rounding
    # at the domain edge) still returns a defined (clamped) number rather than
    # raising — the clamp is reported via the returned z_frl/dem_wse_m fields
    # so callers can see when that happened.
    v_dem_at_frl = float(np.interp(z_frl, dem_z_abs, dem_V))
    gap_m3 = abs(v_dem_at_frl - v_frl_m3)
    gap_pct = gap_m3 / v_frl_m3 * 100.0 if v_frl_m3 > 0 else float("inf")

    return {
        "reconciliation_status": "COMPARED",
        "reconciliation_gap_m3": gap_m3,
        "reconciliation_gap_pct": gap_pct,
        "v_frl_m3": v_frl_m3,
        "v_dem_at_frl_m3": v_dem_at_frl,
        "z_frl_m": z_frl,
        "dem_z_min_m": dem_z_min_m,
        "dem_wse_m": dem_wse_m,
    }


def simulate_reservoir_cascade(
    cfg: dict,
    total_duration_s: float = 10800.0,
    pre_breach_s: float | None = None,
    dt_s: float = 30.0,
    breach_tier: Literal["optimistic", "central", "pessimistic"] = "central",
    stage_storage_curve: tuple[np.ndarray, np.ndarray] | None = None,
    failure_mechanism: FailureMechanism = FailureMechanism.OVERTOPPING_EROSION,
) -> ReservoirCascadeResult:
    """One continuous reservoir integration: FRL -> inflow-driven rise ->
    failure trigger (evaluated every step, may never fire) -> breach growth
    from time-of-trigger -> outflow -> mass-consistent drawdown.

    Replaces the old simulate_prebreach_rise + simulate_generic_cascade pair,
    which reinitialized reservoir state at t=0 rather than evolving one state
    continuously. See module docstring / cascade config schema for cfg shape.

    Parameters
    ----------
    failure_mechanism : FailureMechanism
        Dispatches `_check_trigger`'s initiation criterion (Stage D) and is
        checked via `require_implemented_mechanism` for any value this
        codebase has no physics for. Defaults to OVERTOPPING_EROSION so
        existing callers keep today's exact behavior. Both mechanisms
        currently implemented (overtopping erosion, progressive breach) use
        the identical trigger condition and breach-growth kernel — see Stage
        D+E's report for the finding that no distinguishing physics exists
        yet between them in this codebase.
    stage_storage_curve : (elevation_m, volume_m3) or None
        Stage C. When provided, both arrays must be strictly monotone
        increasing (raises ValueError otherwise) and the reservoir's
        elevation<->storage relationship is looked up from this DEM-derived
        curve via ``np.interp`` instead of the analytical power law fitted to
        the single (z_frl, v_frl) config point. `np.interp` clamps to the
        curve's endpoints outside its domain rather than extrapolating —
        deliberate: extrapolating a DEM-sampled curve past its sampled range
        has no physical basis. When None (default), behavior is unchanged
        from before this parameter existed.
    """
    up_cfg   = cfg["upstream"]
    rout_cfg = cfg["routing"]
    res_cfg  = cfg["reservoir"]
    sp_cfg   = cfg["spillway"]
    ens_cfg  = cfg["breach_ensemble"][breach_tier]
    cr_cfg   = cfg.get("catchment_runoff", {})

    # Fail fast on a NOT_IMPLEMENTED mechanism rather than only at the first
    # trigger check — a run that never triggers must still refuse to silently
    # simulate a mechanism this codebase has no physics for.
    require_implemented_mechanism(failure_mechanism)

    lead_s = float(up_cfg["lead_time_s"])
    if pre_breach_s is None:
        pre_breach_s = lead_s + 1800.0  # start well before upstream failure

    # ── 1. Upstream pulse + routing + catchment runoff over the FULL timeline ──
    t_s = np.arange(-pre_breach_s, total_duration_s + dt_s, dt_s)
    n   = len(t_s)

    q_up = generate_pincha_outflow(
        t_s,
        t_failure_s=-lead_s,
        peak_q_m3s=float(up_cfg["peak_q_m3s"]),
        base_q_m3s=float(up_cfg.get("base_q_m3s", 50.0)),
        duration_s=float(up_cfg.get("duration_s", 5400.0)),
    )
    q_routed = route_muskingum_1d(
        t_s, q_up,
        k_s=float(rout_cfg["k_s"]),
        x=float(rout_cfg["x"]),
    )

    peak_cr = float(cr_cfg.get("peak_m3s", 0.0))
    base_cr = float(cr_cfg.get("base_m3s", 0.0))
    off_cr  = float(cr_cfg.get("peak_offset_s", -3600.0))
    sig_cr  = float(cr_cfg.get("sigma_s", 5400.0))
    catchment = base_cr + peak_cr * np.exp(-((t_s - off_cr) / sig_cr) ** 2)

    q_inflow = q_routed + catchment

    # ── 2. Reservoir geometry ─────────────────────────────────────────────────
    z_bed   = float(res_cfg["z_bed_m"])
    z_frl   = float(res_cfg["z_frl_m"])
    z_crest = float(res_cfg["z_crest_m"])
    v_frl   = float(res_cfg["v_frl_mcm"]) * 1e6
    exp     = float(res_cfg.get("alpha_exp", 2.5))

    if stage_storage_curve is not None:
        z_arr, V_arr = stage_storage_curve
        z_arr = np.asarray(z_arr, dtype=float)
        V_arr = np.asarray(V_arr, dtype=float)
        if z_arr.ndim != 1 or V_arr.ndim != 1 or len(z_arr) != len(V_arr) or len(z_arr) < 2:
            raise ValueError("stage_storage_curve arrays must be 1D, equal length, and length >= 2")
        if np.any(np.diff(z_arr) <= 0.0):
            raise ValueError("stage_storage_curve elevation array must be strictly monotone increasing")
        if np.any(np.diff(V_arr) <= 0.0):
            raise ValueError("stage_storage_curve volume array must be strictly monotone increasing")

        def elev_from_v(v: float) -> float:
            return float(np.interp(v, V_arr, z_arr))

        def v_from_elev(z: float) -> float:
            return float(np.interp(z, z_arr, V_arr))

        stage_storage_source = "DEM"
    else:
        alpha = v_frl / ((z_frl - z_bed) ** exp)

        def elev_from_v(v: float) -> float:
            return z_bed + (max(1e-3, v) / alpha) ** (1.0 / exp)

        def v_from_elev(z: float) -> float:
            return alpha * (max(0.0, z - z_bed) ** exp)

        stage_storage_source = "ANALYTICAL"

    # ── 3. Breach ensemble parameters ─────────────────────────────────────────
    b_final  = float(ens_cfg["width_m"])
    t_f      = float(ens_cfg["formation_s"])
    # `ens_cfg["peak_q_m3s"]` is deliberately NOT read. It was bound to
    # `q_p_env` and used only by the `min(..., q_p_env * 1.15)` envelope clamp
    # that was removed (see below), after which the binding sat dead -- a config
    # value with no causal path to any solver or output, which is a defect by
    # this project's own standard (audit Part VI N-4). The config key is left in
    # place because the scenario files are data, not code; nothing reads it.

    # ── 4. Spillway ───────────────────────────────────────────────────────────
    cd_spill   = float(sp_cfg.get("cd", 2.15))
    l_spill    = float(sp_cfg["length_m"])
    z_sp_crest = float(sp_cfg["z_crest_m"])
    q_sp_max   = float(sp_cfg.get("max_q_m3s", 1e9))
    # Side slope Z for the shared trapezoidal kernel. No cascade scenario
    # config carries a side_slope_hv field (checked: SCENARIOS[...]["cascade"]
    # has no such key anywhere) — default to 1.0 H:1V, matching Froehlich's
    # own overtopping-branch Z (froehlich.py), which is what both mechanisms
    # implemented here (overtopping erosion, progressive breach) already use
    # per Part 1's reasoning. Optional override via cfg["breach_side_slope_hv"]
    # for a future scenario that wants to specify one without a code change.
    z_side = float(cfg.get("breach_side_slope_hv", 1.0))

    # ── 5. Single continuous forward integration, FRL to end of run ───────────
    q_spill  = np.zeros(n)
    q_breach = np.zeros(n)
    v_res    = np.zeros(n)
    z_res    = np.zeros(n)
    b_t_arr  = np.zeros(n)
    z_inv_arr = np.full(n, z_crest)

    v_res[0] = v_from_elev(z_frl)
    z_res[0] = z_frl

    t_trigger: float | None = None

    for i in range(n - 1):
        z = z_res[i]

        # 1. Failure-initiation check — real per-step evaluation, may never fire.
        if t_trigger is None and _check_trigger(z, cfg, failure_mechanism):
            t_trigger = float(t_s[i])

        # 2. Spillway — gated at FRL, then rating curve above it.
        if z <= z_frl:
            q_spill_i = min(max(0.0, q_inflow[i]), q_sp_max)
        else:
            h_sp = max(0.0, z - z_sp_crest)
            q_spill_i = min(cd_spill * l_spill * (h_sp ** 1.5), q_sp_max)

        # 3. Breach — zero until triggered, then grows with elapsed time since
        # trigger, via the SAME shared trapezoidal kernel route_breach uses
        # (breach_kernel.py) — linear width/invert growth, not the old,
        # uncited (elapsed/t_f)**1.2 / **0.8 exponents.
        if t_trigger is None:
            q_breach_i = 0.0
            b_t = 0.0
            z_inv = z_crest
        else:
            elapsed = t_s[i] - t_trigger
            b_t = breach_width_at(elapsed, t_f, b_final)
            z_inv = breach_invert_at(elapsed, t_f, invert_start_m=z_crest, invert_final_m=z_bed)
            if b_t > 1.0:
                # No envelope clamp. The previous `min(..., q_p_env * 1.15)`
                # capped the physically-derived weir discharge at 115 % of a
                # typed config peak, so the hydraulics could never disagree
                # with the number it was supposed to be tested against. If the
                # weir and the envelope disagree, that disagreement is the
                # finding -- it is reported, not clipped away.
                q_breach_i = trapezoidal_breach_discharge(z, z_inv, b_t, z_side)
            else:
                q_breach_i = 0.0

        q_spill[i]   = q_spill_i
        q_breach[i]  = q_breach_i
        b_t_arr[i]   = b_t
        z_inv_arr[i] = z_inv

        # 4. Cap outflow so storage never goes negative and never manufactures mass.
        available = v_res[i] + q_inflow[i] * dt_s
        q_out_uncapped = q_spill_i + q_breach_i
        if q_out_uncapped * dt_s > available:
            if q_out_uncapped > 0.0:
                scale = available / (q_out_uncapped * dt_s)
            else:
                scale = 1.0
            q_spill[i]  = q_spill_i * scale
            q_breach[i] = q_breach_i * scale

        q_out = q_spill[i] + q_breach[i]

        # 5/6. Continuity + elevation update.
        v_res[i + 1] = v_res[i] + (q_inflow[i] - q_out) * dt_s
        z_res[i + 1] = elev_from_v(v_res[i + 1])

    # Last step: evaluate trigger/geometry bookkeeping only (no further integration).
    if t_trigger is None and _check_trigger(z_res[-1], cfg, failure_mechanism):
        t_trigger = float(t_s[-1])
    if t_trigger is None:
        b_t_arr[-1] = 0.0
        z_inv_arr[-1] = z_crest
    else:
        elapsed = t_s[-1] - t_trigger
        b_t_arr[-1] = breach_width_at(elapsed, t_f, b_final)
        z_inv_arr[-1] = breach_invert_at(elapsed, t_f, invert_start_m=z_crest, invert_final_m=z_bed)

    q_total = q_spill + q_breach

    # ── 6. Mass balance ───────────────────────────────────────────────────────
    # Sum only over steps that actually advanced storage (0..n-2) — the final
    # index's inflow/outflow are never applied to v_res, so including them
    # here would count mass that never actually moved (this was the source
    # of the old ~0.03%-scale "mass error" before this fix).
    v_i    = float(v_res[0])
    v_f    = float(v_res[-1])
    v_in   = float(np.sum(q_inflow[:-1]) * dt_s)
    v_sp   = float(np.sum(q_spill[:-1])  * dt_s)
    v_br   = float(np.sum(q_breach[:-1]) * dt_s)
    v_out  = v_sp + v_br
    merr   = (v_i + v_in) - v_out - v_f
    merr_p = abs(merr) / (v_i + v_in) * 100.0

    meta = {
        "scenario":                    cfg.get("scenario_key", "generic"),
        "tier":                        breach_tier,
        "classification":              ens_cfg.get("classification", "MODEL RECONSTRUCTION"),
        "source":                      ens_cfg.get("source", "Froehlich 2008"),
        "breach_width_m":              b_final,
        "formation_time_min":          t_f / 60.0,
        "peak_breach_q_m3s":           float(np.max(q_breach)),
        "peak_total_inflow_m3s":       float(np.max(q_inflow)),
        "upstream_classification":     up_cfg.get("classification", "ASSUMPTION"),
        "routing_classification":      rout_cfg.get("classification", "ASSUMPTION"),
        "reservoir_classification":    res_cfg.get("classification", "ASSUMPTION"),
        "t_trigger_s":                 t_trigger,
        "triggered":                   t_trigger is not None,
        "stage_storage_source":        stage_storage_source,
    }
    logger.info(
        "Reservoir cascade '%s' tier='%s': t_trigger=%s, Q_p_breach=%.0f m3/s, mass_err=%.6f%%",
        meta["scenario"], breach_tier, t_trigger, meta["peak_breach_q_m3s"], merr_p,
    )

    return ReservoirCascadeResult(
        t_s=t_s,
        q_upstream_m3s=q_up,
        q_inflow_reservoir_m3s=q_inflow,
        q_spillway_m3s=q_spill,
        q_breach_m3s=q_breach,
        q_total_outflow_m3s=q_total,
        reservoir_storage_m3=v_res,
        reservoir_elevation_m=z_res,
        t_trigger_s=t_trigger,
        breach_width_m=b_t_arr,
        breach_invert_m=z_inv_arr,
        v_initial_m3=v_i,
        v_inflow_total_m3=v_in,
        v_outflow_total_m3=v_out,
        v_breach_total_m3=v_br,
        v_spill_total_m3=v_sp,
        v_final_m3=v_f,
        mass_error_m3=merr,
        mass_error_pct=merr_p,
        ensemble_metadata=meta,
    )
