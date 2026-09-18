"""
Stage A — reservoir/state continuity tests for src/m3_breach/cascade.py.

Covers the 8 numbered requirements from the Stage A spec: one continuous
integrator (FRL -> trigger -> breach), no reinitialization at t=0, a real
per-step trigger check (placeholder: crest overtopping only — Stage D
replaces the condition), mass-consistent capping instead of floor-clamping,
and exact full-run mass closure.
"""
import copy

import numpy as np
import pytest

from src.data_fetcher import SCENARIOS
from src.m3_breach.cascade import simulate_reservoir_cascade


ANNAMAYYA_CFG = SCENARIOS["annamayya"]["cascade"]
SOUTH_LHONAK_CFG = SCENARIOS["south_lhonak"]["cascade"]


def test_no_reinitialization_continuous_across_trigger():
    """Requirement 1: one array holds storage/elevation across the whole run —
    elevation must be continuous (no jump) across the trigger boundary."""
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    cfg["reservoir"]["z_crest_m"] = 1102.0  # delay trigger a bit past t=0
    res = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                      breach_tier="central")
    assert res.t_trigger_s is not None
    idx = int(np.searchsorted(res.t_s, res.t_trigger_s))
    assert 0 < idx < len(res.t_s) - 1

    dt_s = 30.0
    v = res.reservoir_storage_m3
    z = res.reservoir_elevation_m
    q_in = res.q_inflow_reservoir_m3s
    q_out = res.q_total_outflow_m3s

    # The storage value immediately after the trigger boundary must be
    # exactly what the ordinary continuity equation produces FROM the
    # pre-trigger state v[idx] — i.e. the same array/variable carried
    # forward, not reinitialized to a fixed constant like the old
    # `v_from_elev(z_crest + 0.05)` IC. If storage had been reinitialized,
    # this reconstruction (using the recorded pre-boundary state) would not
    # match the recorded post-boundary state.
    v_reconstructed = v[idx] + (q_in[idx] - q_out[idx]) * dt_s
    rel_diff_v = abs(v[idx + 1] - v_reconstructed) / abs(v[idx + 1])
    assert rel_diff_v < 1e-6, (
        f"storage at idx+1 ({v[idx+1]!r}) does not match continuity-equation "
        f"reconstruction from idx ({v_reconstructed!r}) — indicates a "
        f"reinitialization jump, rel_diff={rel_diff_v!r}"
    )

    z_before = z[idx]
    z_after = z[idx + 1]
    print(f"REQ1: t_trigger={res.t_trigger_s} idx={idx} "
          f"v[idx]={v[idx]!r} v[idx+1]_actual={v[idx+1]!r} "
          f"v[idx+1]_reconstructed={v_reconstructed!r} rel_diff_v={rel_diff_v!r} "
          f"z_before={z_before!r} z_after={z_after!r}")


def test_insufficient_forcing_no_failure():
    """Requirement 2: scaled-down inflow never lifts z to z_crest_m ->
    breach discharge zero for the whole run, t_trigger stays None, outflow
    is spillway-only."""
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    cfg["upstream"]["peak_q_m3s"] *= 0.01
    cfg["catchment_runoff"]["peak_m3s"] *= 0.01
    res = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                      breach_tier="central")

    assert res.t_trigger_s is None
    assert np.all(res.q_breach_m3s == 0.0)
    assert res.reservoir_elevation_m.max() < cfg["reservoir"]["z_crest_m"]
    print(f"REQ2: t_trigger={res.t_trigger_s} max_q_breach={res.q_breach_m3s.max()!r} "
          f"max_z={res.reservoir_elevation_m.max()!r} z_crest={cfg['reservoir']['z_crest_m']}")


def test_inflow_change_increases_peak_level():
    """Requirement 3: doubling upstream.peak_q_m3s (holding everything else
    fixed) must strictly increase the peak reservoir level reached."""
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    res_base = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                           breach_tier="central")
    cfg_2x = copy.deepcopy(cfg)
    cfg_2x["upstream"]["peak_q_m3s"] *= 2.0
    res_2x = simulate_reservoir_cascade(cfg_2x, total_duration_s=20000.0, dt_s=30.0,
                                         breach_tier="central")

    peak_base = res_base.reservoir_elevation_m.max()
    peak_2x = res_2x.reservoir_elevation_m.max()
    assert peak_2x > peak_base
    print(f"REQ3: peak_z(1x)={peak_base!r} peak_z(2x)={peak_2x!r}")


def test_higher_inflow_triggers_earlier():
    """Requirement 4: increasing inflow enough to cross z_crest_m earlier
    must make t_trigger strictly smaller than a run with lower inflow that
    still crosses eventually."""
    cfg_low = copy.deepcopy(SOUTH_LHONAK_CFG)
    res_low = simulate_reservoir_cascade(cfg_low, total_duration_s=20000.0, dt_s=30.0,
                                          breach_tier="central")
    cfg_hi = copy.deepcopy(SOUTH_LHONAK_CFG)
    cfg_hi["upstream"]["peak_q_m3s"] *= 1.5
    res_hi = simulate_reservoir_cascade(cfg_hi, total_duration_s=20000.0, dt_s=30.0,
                                         breach_tier="central")

    assert res_low.t_trigger_s is not None
    assert res_hi.t_trigger_s is not None
    assert res_hi.t_trigger_s < res_low.t_trigger_s
    print(f"REQ4: t_trigger(low inflow)={res_low.t_trigger_s!r} "
          f"t_trigger(high inflow)={res_hi.t_trigger_s!r}")


def test_trigger_time_shifts_breach_geometry():
    """Requirement 5: q_breach must be zero for all t_s[i] < t_trigger, and
    the breach only starts growing from t_trigger onward (offset in time
    consistent with t_trigger) for both an early- and late-triggering run."""
    cfg_early = copy.deepcopy(SOUTH_LHONAK_CFG)
    res_early = simulate_reservoir_cascade(cfg_early, total_duration_s=20000.0, dt_s=30.0,
                                            breach_tier="central")
    cfg_late = copy.deepcopy(SOUTH_LHONAK_CFG)
    cfg_late["reservoir"]["z_crest_m"] = 1102.0  # takes longer to reach -> later trigger
    res_late = simulate_reservoir_cascade(cfg_late, total_duration_s=20000.0, dt_s=30.0,
                                           breach_tier="central")

    for label, res in (("early", res_early), ("late", res_late)):
        tt = res.t_trigger_s
        assert tt is not None
        before_mask = res.t_s < tt
        assert np.all(res.q_breach_m3s[before_mask] == 0.0), (
            f"{label}: nonzero q_breach before t_trigger"
        )
        nz_idx = np.nonzero(res.q_breach_m3s)[0]
        if len(nz_idx) > 0:
            assert res.t_s[nz_idx[0]] >= tt

    assert res_late.t_trigger_s > res_early.t_trigger_s
    print(f"REQ5: t_trigger(early)={res_early.t_trigger_s!r} "
          f"t_trigger(late)={res_late.t_trigger_s!r} "
          f"all q_breach==0 before trigger in both runs")


def test_storage_never_exceeds_inflow_bound():
    """Requirement 6: at every timestep, v_res[i+1] <= v_res[i] + q_inflow[i]*dt_s."""
    cfg = copy.deepcopy(ANNAMAYYA_CFG)
    cfg["upstream"]["peak_q_m3s"] *= 2.0  # ensure trigger fires, exercising the full path
    res = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                      breach_tier="central")
    dt_s = 30.0
    v = res.reservoir_storage_m3
    q_in = res.q_inflow_reservoir_m3s
    bound = v[:-1] + q_in[:-1] * dt_s
    violations = np.where(v[1:] > bound + 1e-6)[0]
    assert len(violations) == 0, f"{len(violations)} timesteps exceeded the inflow bound"
    print(f"REQ6: checked {len(v) - 1} timesteps, violations={len(violations)}, "
          f"max_slack={float(np.max(bound - v[1:]))!r}")


def test_no_manufactured_mass_when_outflow_capacity_exceeds_storage():
    """Requirement 7: when spillway+breach capacity far exceeds remaining
    storage, outflow must be capped (not floored afterward) so storage never
    goes negative and outflow volume never exceeds v_res[i] + inflow_i*dt_s."""
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    cfg["upstream"]["peak_q_m3s"] *= 3.0
    cfg["breach_ensemble"]["central"]["peak_q_m3s"] *= 50.0
    cfg["breach_ensemble"]["central"]["formation_s"] = 60.0
    cfg["reservoir"]["v_frl_mcm"] = 5.0  # tiny reservoir vs. breach discharge capacity
    res = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                      breach_tier="central")

    dt_s = 30.0
    v = res.reservoir_storage_m3
    q_in = res.q_inflow_reservoir_m3s
    q_out = res.q_total_outflow_m3s

    assert res.t_trigger_s is not None  # sanity: the breach path actually ran

    # (a) storage never goes negative (to floating-point tolerance).
    assert v.min() >= -1e-6, f"storage went negative: {v.min()!r}"

    # (b) outflow volume for each step never exceeds v_res[i] + inflow_i*dt_s.
    available = v[:-1] + q_in[:-1] * dt_s
    used = q_out[:-1] * dt_s
    overshoot = used - available
    assert np.max(overshoot) < 1e-6, f"manufactured mass detected: max overshoot {np.max(overshoot)!r} m3"
    print(f"REQ7: v.min()={v.min()!r} max_overshoot_m3={float(np.max(overshoot))!r} "
          f"t_trigger={res.t_trigger_s}")


def test_full_run_mass_closure():
    """Requirement 8: full-run mass ledger closes to within 1e-6 relative
    (machine precision, since requirement 7 removed the floor-clamping that
    used to manufacture mass)."""
    cfg = copy.deepcopy(ANNAMAYYA_CFG)
    res = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                      breach_tier="central")

    denom = res.v_initial_m3 + res.v_inflow_total_m3
    rel_err = abs(denom - res.v_outflow_total_m3 - res.v_final_m3) / denom
    assert rel_err < 1e-6
    print(f"REQ8: v_initial={res.v_initial_m3!r} v_inflow_total={res.v_inflow_total_m3!r} "
          f"v_outflow_total={res.v_outflow_total_m3!r} v_final={res.v_final_m3!r} "
          f"mass_error_m3={res.mass_error_m3!r} rel_err={rel_err!r}")
