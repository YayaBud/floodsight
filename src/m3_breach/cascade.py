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
- EVD-04: Pincha washout at ~03:30 AM IST (T-150 min).
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

logger = logging.getLogger(__name__)


@dataclass
class CascadeResult:
    """Hydrograph and mass-balance audit results (generic + Annamayya)."""
    t_s: np.ndarray
    # upstream surge (field kept generic; for Annamayya this is the Pincha pulse)
    q_upstream_m3s: np.ndarray
    q_inflow_reservoir_m3s: np.ndarray
    q_spillway_m3s: np.ndarray
    q_breach_m3s: np.ndarray
    q_total_outflow_m3s: np.ndarray
    reservoir_storage_m3: np.ndarray
    reservoir_elevation_m: np.ndarray

    # Authoritative Mass Balance
    v_initial_m3: float
    v_inflow_total_m3: float
    v_outflow_total_m3: float
    v_breach_total_m3: float
    v_spill_total_m3: float
    v_final_m3: float
    mass_error_m3: float
    mass_error_pct: float

    # Plausibility Verification (TEST 11)
    volume_plausibility_passed: bool
    ensemble_metadata: dict

    # ---- backward-compat aliases (Annamayya callers use these field names) ----
    @property
    def q_pincha_m3s(self) -> np.ndarray:
        return self.q_upstream_m3s

    @property
    def q_inflow_annamayya_m3s(self) -> np.ndarray:
        return self.q_inflow_reservoir_m3s


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


def simulate_generic_cascade(
    cfg: dict,
    total_duration_s: float = 10800.0,
    dt_s: float = 30.0,
    breach_tier: Literal["optimistic", "central", "pessimistic"] = "central",
) -> CascadeResult:
    """
    Scenario-agnostic cascade: upstream pulse -> 1D Muskingum routing ->
    reservoir storage routing -> spillway -> dynamic breach hydrograph.

    Parameters
    ----------
    cfg : dict
        Configuration dict following the schema described in the module docstring.
        Must contain: upstream, routing, reservoir, spillway, breach_ensemble.
        catchment_runoff is optional (defaults to zero if absent).
    total_duration_s : float
        Length of the simulation window in seconds (from t=0 = breach initiation).
    dt_s : float
        Time step in seconds.
    breach_tier : str
        One of "optimistic" / "central" / "pessimistic".

    Returns
    -------
    CascadeResult
    """
    up_cfg   = cfg["upstream"]
    rout_cfg = cfg["routing"]
    res_cfg  = cfg["reservoir"]
    sp_cfg   = cfg["spillway"]
    ens_cfg  = cfg["breach_ensemble"][breach_tier]
    cr_cfg   = cfg.get("catchment_runoff", {})

    # ── 1. Upstream pulse on extended time window ─────────────────────────────
    lead_s = float(up_cfg["lead_time_s"])
    t_eval = np.arange(-lead_s - 1800.0, total_duration_s + dt_s, dt_s)

    q_up_full = generate_pincha_outflow(
        t_eval,
        t_failure_s=-lead_s,
        peak_q_m3s=float(up_cfg["peak_q_m3s"]),
        base_q_m3s=float(up_cfg.get("base_q_m3s", 50.0)),
        duration_s=float(up_cfg.get("duration_s", 5400.0)),
    )
    q_routed_full = route_muskingum_1d(
        t_eval, q_up_full,
        k_s=float(rout_cfg["k_s"]),
        x=float(rout_cfg["x"]),
    )

    # ── 2. Catchment runoff (Gaussian pulse, optional) ────────────────────────
    peak_cr = float(cr_cfg.get("peak_m3s", 0.0))
    base_cr = float(cr_cfg.get("base_m3s", 0.0))
    off_cr  = float(cr_cfg.get("peak_offset_s", -3600.0))
    sig_cr  = float(cr_cfg.get("sigma_s", 5400.0))
    catchment = base_cr + peak_cr * np.exp(-((t_eval - off_cr) / sig_cr) ** 2)

    q_inflow_full = q_routed_full + catchment

    # ── 3. Extract simulation window [0, total_duration_s] ───────────────────
    t_s = np.arange(0.0, total_duration_s + dt_s, dt_s)
    n   = len(t_s)
    idx = int(np.searchsorted(t_eval, 0.0))
    q_up     = q_up_full[idx: idx + n]
    q_inflow = q_inflow_full[idx: idx + n]

    # ── 4. Reservoir geometry ─────────────────────────────────────────────────
    z_bed   = float(res_cfg["z_bed_m"])
    z_frl   = float(res_cfg["z_frl_m"])
    z_crest = float(res_cfg["z_crest_m"])
    v_frl   = float(res_cfg["v_frl_mcm"]) * 1e6
    exp     = float(res_cfg.get("alpha_exp", 2.5))

    alpha = v_frl / ((z_frl - z_bed) ** exp)

    def elev_from_v(v: float) -> float:
        return z_bed + (max(1e-3, v) / alpha) ** (1.0 / exp)

    def v_from_elev(z: float) -> float:
        return alpha * (max(0.0, z - z_bed) ** exp)

    # ── 5. Breach ensemble parameters ─────────────────────────────────────────
    b_final  = float(ens_cfg["width_m"])
    t_f      = float(ens_cfg["formation_s"])
    q_p_env  = float(ens_cfg["peak_q_m3s"])

    # ── 6. Spillway ───────────────────────────────────────────────────────────
    cd_spill   = float(sp_cfg.get("cd", 2.15))
    l_spill    = float(sp_cfg["length_m"])
    z_sp_crest = float(sp_cfg["z_crest_m"])
    q_sp_max   = float(sp_cfg.get("max_q_m3s", 1e9))
    cd_breach  = float(cfg.get("breach_cd", 0.85))

    # ── 7. Time-stepping (continuity + dynamic breach) ────────────────────────
    q_spill  = np.zeros(n)
    q_breach = np.zeros(n)
    v_res    = np.zeros(n)
    z_res    = np.zeros(n)

    v_res[0] = v_from_elev(z_crest + 0.05)  # starts at crest-overtopping
    z_res[0] = elev_from_v(v_res[0])

    for i in range(n - 1):
        t = t_s[i]
        z = z_res[i]

        # Spillway
        if z > z_sp_crest:
            h_sp = z - z_sp_crest
            q_spill[i] = min(cd_spill * l_spill * (h_sp ** 1.5), q_sp_max)

        # Dynamic breach (trapezoidal widening + invert erosion)
        frac = min(1.0, (t / t_f) ** 1.2)
        b_t  = b_final * frac
        z_inv = z_crest - (z_crest - z_bed) * (frac ** 0.8)
        if z > z_inv and b_t > 1.0:
            h_b = z - z_inv
            q_breach[i] = min(cd_breach * b_t * (h_b ** 1.5), q_p_env * 1.15)

        # Continuity
        q_out = q_spill[i] + q_breach[i]
        dv = (q_inflow[i] - q_out) * dt_s
        v_res[i + 1] = max(1e-3, v_res[i] + dv)
        z_res[i + 1] = elev_from_v(v_res[i + 1])

    # Last step
    z_last = z_res[-1]
    if z_last > z_sp_crest:
        q_spill[-1] = min(cd_spill * l_spill * ((z_last - z_sp_crest) ** 1.5), q_sp_max)
    if z_last > z_bed:
        q_breach[-1] = cd_breach * b_final * ((z_last - z_bed) ** 1.5)

    q_total = q_spill + q_breach

    # ── 8. Mass balance ───────────────────────────────────────────────────────
    v_i    = float(v_res[0])
    v_f    = float(v_res[-1])
    v_in   = float(np.sum(q_inflow) * dt_s)
    v_sp   = float(np.sum(q_spill)  * dt_s)
    v_br   = float(np.sum(q_breach) * dt_s)
    v_out  = v_sp + v_br
    merr   = (v_i + v_in) - v_out - v_f
    merr_p = abs(merr) / (v_i + v_in) * 100.0
    ok11   = v_br <= (v_i + v_in)

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
    }
    logger.info(
        "Generic cascade '%s' tier='%s': Q_p_breach=%.0f m3/s, mass_err=%.4f%%",
        meta["scenario"], breach_tier, meta["peak_breach_q_m3s"], merr_p,
    )

    return CascadeResult(
        t_s=t_s,
        q_upstream_m3s=q_up,
        q_inflow_reservoir_m3s=q_inflow,
        q_spillway_m3s=q_spill,
        q_breach_m3s=q_breach,
        q_total_outflow_m3s=q_total,
        reservoir_storage_m3=v_res,
        reservoir_elevation_m=z_res,
        v_initial_m3=v_i,
        v_inflow_total_m3=v_in,
        v_outflow_total_m3=v_out,
        v_breach_total_m3=v_br,
        v_spill_total_m3=v_sp,
        v_final_m3=v_f,
        mass_error_m3=merr,
        mass_error_pct=merr_p,
        volume_plausibility_passed=ok11,
        ensemble_metadata=meta,
    )




def simulate_annamayya_cascade(
    total_duration_s: float = 14400.0,  # 4 hours
    dt_s: float = 30.0,
    breach_tier: Literal["optimistic", "central", "pessimistic"] = "central",
) -> CascadeResult:
    """
    Run complete coupled cascade:
      1. Pincha breach pulse -> 1D Muskingum routing -> Annamayya inflow
      2. Catchment runoff combination (intermediate 150-240mm storm)
      3. Annamayya reservoir storage routing (FRL +203.6m, Crest +206.0m, Thalweg +180.0m)
      4. Spillway discharge (4 radial gates open, ~4,136 m3/s max)
      5. Overtopping breach initiation + dynamic trapezoidal breach growth
      6. Authoritative mass conservation & TEST 11 volume plausibility check
    """
    # This function is now a thin wrapper around simulate_generic_cascade.
    # All evidence-anchored values are in the annamayya_cfg dict inside that
    # wrapper above.  See ANNAMAYYA_DATA_AUDIT.md for epistemic classification.
    return simulate_generic_cascade(
        {
            "scenario_key": "annamayya",
            "upstream": {
                "peak_q_m3s": 3964.0, "base_q_m3s": 300.0, "duration_s": 5400.0,
                "lead_time_s": 9000.0, "classification": "OFFICIAL ESTIMATE",
            },
            "routing": {"k_s": 7200.0, "x": 0.20, "classification": "RECONSTRUCTION"},
            "reservoir": {
                "z_bed_m": 180.0, "z_frl_m": 203.6, "z_crest_m": 206.0,
                "v_frl_mcm": 63.43, "alpha_exp": 2.5, "classification": "OBSERVED",
            },
            "spillway": {"cd": 2.15, "length_m": 55.0, "z_crest_m": 189.6, "max_q_m3s": 4136.0},
            "catchment_runoff": {
                "base_m3s": 800.0, "peak_m3s": 3800.0,
                "peak_offset_s": -1800.0, "sigma_s": 5400.0,
            },
            "breach_ensemble": {
                "optimistic":  {"width_m":  85.0, "formation_s": 4200.0, "peak_q_m3s":  8800.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) lower confidence interval"},
                "central":     {"width_m": 130.0, "formation_s": 3000.0, "peak_q_m3s": 12200.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) best estimate"},
                "pessimistic": {"width_m": 240.0, "formation_s": 2100.0, "peak_q_m3s": 15500.0,
                                "classification": "MODEL RECONSTRUCTION",
                                "source": "Froehlich (2008) upper envelope bounded by 336m earthen section"},
            },
        },
        total_duration_s=total_duration_s, dt_s=dt_s, breach_tier=breach_tier,
    )
