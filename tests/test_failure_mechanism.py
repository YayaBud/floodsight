"""
Stage D+E — failure_mechanism taxonomy + shared breach kernel.

Covers the required-verification list from the Stage D+E spec:
1. Overtopping erosion and progressive breach share ONE kernel (numerically
   identical outflow curves), not two independently-coded ones.
2. Every NOT_IMPLEMENTED mechanism raises NotImplementedError.
3. The cascade path's breach growth is now linear, not (t/t_f)**1.2.
4. test_cwc_dam_engineering_override passes (confirmed here, not duplicated).
5. Mechanism threading end-to-end: OVERTOPPING_EROSION vs. PROGRESSIVE_BREACH
   trigger identically and produce identical breach curves on the same config
   — documents, machine-checked, that these two mechanisms are currently
   physically identical except in name.
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

from src.data_fetcher import SCENARIOS
from src.m3_breach import FailureMechanism, require_implemented_mechanism
from src.m3_breach.breach_kernel import breach_width_at, breach_invert_at, trapezoidal_breach_discharge
from src.m3_breach.cascade import simulate_reservoir_cascade

SOUTH_LHONAK_CFG = SCENARIOS["south_lhonak"]["cascade"]

_NOT_IMPLEMENTED_MECHANISMS = [
    m for m in FailureMechanism
    if m not in (FailureMechanism.OVERTOPPING_EROSION, FailureMechanism.PROGRESSIVE_BREACH)
]


# ── 1. Overtopping erosion and progressive breach share one kernel ─────────

def test_overtopping_and_progressive_breach_share_identical_kernel():
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    res_ot = simulate_reservoir_cascade(
        cfg, total_duration_s=20000.0, dt_s=30.0, breach_tier="central",
        failure_mechanism=FailureMechanism.OVERTOPPING_EROSION)
    res_pb = simulate_reservoir_cascade(
        cfg, total_duration_s=20000.0, dt_s=30.0, breach_tier="central",
        failure_mechanism=FailureMechanism.PROGRESSIVE_BREACH)

    assert res_ot.t_trigger_s is not None
    assert res_ot.t_trigger_s == res_pb.t_trigger_s
    np.testing.assert_array_equal(res_ot.q_breach_m3s, res_pb.q_breach_m3s)
    np.testing.assert_array_equal(res_ot.breach_width_m, res_pb.breach_width_m)
    np.testing.assert_array_equal(res_ot.breach_invert_m, res_pb.breach_invert_m)
    print(f"KERNEL IDENTITY: t_trigger equal={res_ot.t_trigger_s == res_pb.t_trigger_s}, "
          f"max|dq|={float(np.max(np.abs(res_ot.q_breach_m3s - res_pb.q_breach_m3s)))!r}, "
          f"peak_q_breach={float(res_ot.q_breach_m3s.max())!r}")


def test_shared_kernel_function_is_pure_and_matches_manual_formula():
    """Direct proof the extracted function computes the documented formula
    (rectangular weir term + triangular side-slope term)."""
    h_water, invert, width, Z = 105.0, 100.0, 20.0, 1.0
    head = h_water - invert
    expected = 1.7 * width * head ** 1.5 + 1.35 * Z * head ** 2.5
    got = trapezoidal_breach_discharge(h_water, invert, width, Z)
    assert got == pytest.approx(expected, rel=1e-12)


# ── 2. Every NOT_IMPLEMENTED mechanism raises ───────────────────────────────

@pytest.mark.parametrize("mechanism", _NOT_IMPLEMENTED_MECHANISMS)
def test_not_implemented_mechanism_raises(mechanism):
    with pytest.raises(NotImplementedError):
        require_implemented_mechanism(mechanism)


@pytest.mark.parametrize("mechanism", _NOT_IMPLEMENTED_MECHANISMS)
def test_not_implemented_mechanism_raises_from_simulate_reservoir_cascade(mechanism):
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    with pytest.raises(NotImplementedError):
        simulate_reservoir_cascade(
            cfg, total_duration_s=20000.0, dt_s=30.0, breach_tier="central",
            failure_mechanism=mechanism)


def test_implemented_mechanisms_do_not_raise():
    for mechanism in (FailureMechanism.OVERTOPPING_EROSION, FailureMechanism.PROGRESSIVE_BREACH):
        require_implemented_mechanism(mechanism)  # must not raise


# ── 3. Cascade breach growth is linear, not (t/t_f)**1.2 ───────────────────

def test_cascade_breach_growth_is_linear_not_old_exponent():
    formation_s = 3600.0
    final_width_m = 100.0
    elapsed = 0.5 * formation_s  # elapsed/t_f = 0.5

    new_width = breach_width_at(elapsed, formation_s, final_width_m)
    old_exponent_width = final_width_m * min(1.0, (elapsed / formation_s) ** 1.2)

    assert new_width == pytest.approx(50.0, rel=1e-9)          # linear: exactly 50%
    assert old_exponent_width == pytest.approx(43.527528, rel=1e-6)  # old: 0.5**1.2 ~= 43.5%
    assert new_width != pytest.approx(old_exponent_width, rel=1e-3)
    print(f"BEFORE/AFTER at elapsed/t_f=0.5: old(0.5**1.2)={old_exponent_width!r} "
          f"new(linear)={new_width!r} final_width_m={final_width_m}")

    # Invert erosion: old was frac**0.8, new is linear (frac).
    invert_start, invert_final = 200.0, 150.0
    new_invert = breach_invert_at(elapsed, formation_s, invert_start, invert_final)
    old_exponent_frac = (elapsed / formation_s) ** 0.8
    old_invert = invert_start - (invert_start - invert_final) * old_exponent_frac
    assert new_invert == pytest.approx(175.0, rel=1e-9)  # linear: exactly halfway
    assert new_invert != pytest.approx(old_invert, rel=1e-3)
    print(f"BEFORE/AFTER invert at elapsed/t_f=0.5: old(frac**0.8)={old_invert!r} "
          f"new(linear)={new_invert!r}")


def test_cascade_path_breach_width_is_linear_end_to_end():
    """Same proof, exercised through simulate_reservoir_cascade's actual
    per-step loop rather than calling breach_width_at directly."""
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    res = simulate_reservoir_cascade(cfg, total_duration_s=20000.0, dt_s=30.0,
                                      breach_tier="central")
    assert res.t_trigger_s is not None
    t_f = float(cfg["breach_ensemble"]["central"]["formation_s"])
    b_final = float(cfg["breach_ensemble"]["central"]["width_m"])

    # Find the timestep closest to elapsed/t_f == 0.5 while still growing
    # (before full formation), and compare against the exact linear value.
    target_t = res.t_trigger_s + 0.5 * t_f
    idx = int(np.argmin(np.abs(res.t_s - target_t)))
    elapsed = res.t_s[idx] - res.t_trigger_s
    expected_linear = b_final * min(1.0, elapsed / t_f)
    old_exponent_value = b_final * min(1.0, (elapsed / t_f) ** 1.2)

    assert res.breach_width_m[idx] == pytest.approx(expected_linear, rel=1e-6)
    assert res.breach_width_m[idx] != pytest.approx(old_exponent_value, rel=1e-3)
    print(f"CASCADE E2E at idx={idx} elapsed/t_f={elapsed/t_f!r}: "
          f"actual_width={res.breach_width_m[idx]!r} expected_linear={expected_linear!r} "
          f"old_exponent_would_be={old_exponent_value!r}")


# ── 4. test_cwc_dam_engineering_override passes (confirm, don't duplicate) ─

def test_cwc_dam_engineering_override_is_fixed():
    """Confirms Part 4's fix by importing and running the actual existing
    test function — not a re-implementation of its assertions."""
    from tests.test_ground_connectors import test_cwc_dam_engineering_override
    test_cwc_dam_engineering_override()  # must not raise


# ── 5. Mechanism threading end-to-end ───────────────────────────────────────

def test_mechanism_threading_end_to_end_identical_behavior():
    cfg = copy.deepcopy(SOUTH_LHONAK_CFG)
    res_default = simulate_reservoir_cascade(
        cfg, total_duration_s=20000.0, dt_s=30.0, breach_tier="central")
    res_explicit_progressive = simulate_reservoir_cascade(
        cfg, total_duration_s=20000.0, dt_s=30.0, breach_tier="central",
        failure_mechanism=FailureMechanism.PROGRESSIVE_BREACH)

    assert res_default.t_trigger_s == res_explicit_progressive.t_trigger_s
    np.testing.assert_array_equal(res_default.q_breach_m3s, res_explicit_progressive.q_breach_m3s)
    np.testing.assert_array_equal(res_default.reservoir_elevation_m,
                                   res_explicit_progressive.reservoir_elevation_m)
    print("MECHANISM THREADING: default (OVERTOPPING_EROSION) and explicit "
          "PROGRESSIVE_BREACH produce byte-identical trajectories — no "
          "distinguishing physics exists yet, only a distinguishing label "
          "(see Stage D+E report).")
