"""
Stage F — reservoir-to-solver coupling tests.

Two real, confirmed defects fixed here: (1) `initial_depth` existed but was
never passed by run_pipeline.py, so every flood started on a dry bed; (2)
injected inflow carried zero momentum despite a comment claiming otherwise.
These tests prove causality — the fix actually changes the physics, not just
that new arguments exist and don't crash — plus the backward-compatibility /
additivity guarantees the multi-inflow interface (Part 3) requires.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.m4_solvers.swe_2d import (          # noqa: E402
    run_2d_swe_simulation, InflowBoundary,
)


def _basin(ny=70, nx=90):
    """A simple tilted valley: flat pool region upstream, slope downstream."""
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 40.0 - 0.30 * xx
    return z.astype(np.float64)


def _hydrograph(peak=400.0, dur=300.0, n=30):
    t = np.linspace(0.0, dur, n)
    q = peak * np.exp(-((t - 100.0) / 70.0) ** 2)
    return t, q


# ── Test 1: initial_depth changes the solution (Part 1 causal proof) ────────

def test_initial_depth_changes_the_solution():
    z = _basin()
    t_s, Q = _hydrograph()

    pool = np.zeros_like(z)
    pool[:, :15] = np.maximum(0.0, 38.0 - z[:, :15])  # a real pre-existing pool

    wet_kwargs = dict(
        elevation_grid=z, dx_m=20.0, dy_m=20.0,
        inflow_x_idx=20, inflow_y_idx=35,
        hydrograph_t_s=t_s, hydrograph_Q_m3s=Q,
        total_duration_s=180.0, save_interval_s=60.0,
        manning_n=0.04, scenario_name="ic-wet",
    )
    res_dry = run_2d_swe_simulation(**wet_kwargs, initial_depth=None)
    res_wet = run_2d_swe_simulation(**wet_kwargs, initial_depth=pool)

    dry_sum = float(res_dry.depth_grids[0].sum())
    wet_sum = float(res_wet.depth_grids[0].sum())
    print(f"[test1] max_depth_grid.sum(): dry={dry_sum:.6f} wet={wet_sum:.6f}")
    assert wet_sum > dry_sum, (dry_sum, wet_sum)

    # The initial pool sits at t=0 in the actually-impounded area (x<15), not
    # manufactured elsewhere -- disconnected terrain does not flood from this.
    assert res_wet.depth_grids[0][:, :15].sum() > 0.0
    assert res_dry.depth_grids[0][:, :15].sum() == 0.0


# ── Test 2: injected momentum changes near-field velocity (Part 2 causal proof) ──

def test_momentum_changes_near_field_velocity():
    z = _basin()
    t_s, Q = _hydrograph(peak=600.0, dur=300.0)
    ix, iy = 30, 35

    common = dict(
        elevation_grid=z, dx_m=20.0, dy_m=20.0,
        inflow_x_idx=ix, inflow_y_idx=iy,
        hydrograph_t_s=t_s, hydrograph_Q_m3s=Q,
        total_duration_s=60.0, save_interval_s=20.0,
        manning_n=0.04, scenario_name="momentum-test",
    )
    v_ms = np.full_like(t_s, 8.0)  # a real, sizeable jet velocity

    # `_basin()` is `z = 40 - 0.30 * x`, so the bed FALLS with increasing x and
    # downstream is +x. This test used to inject toward (-1, 0) under a comment
    # calling that "downstream", which is backwards -- it was injecting uphill,
    # against the gravity-driven flow. It passed anyway on the old bed-slope
    # source; with the Audusse face-pair source it stopped passing, and the
    # measurement showed why: injecting uphill correctly OPPOSES the flow and
    # reduces peak |u| (6.887 -> 4.611 m/s), while injecting downhill correctly
    # reinforces it (6.887 -> 9.238 m/s). Measured 2026-09-13.
    #
    # The fixture's direction was the defect, so it is corrected here rather
    # than the assertion weakened -- and the assertion is strengthened: it now
    # pins BOTH directions and the sign of the total injected momentum, which
    # is the property that actually proves the coupling is causal.
    res_no_mom = run_2d_swe_simulation(**common)
    res_down = run_2d_swe_simulation(**common, hydrograph_v_ms=v_ms,
                                     inflow_direction=(1.0, 0.0))   # downstream
    res_up = run_2d_swe_simulation(**common, hydrograph_v_ms=v_ms,
                                   inflow_direction=(-1.0, 0.0))    # upstream

    window = (slice(iy - 4, iy + 5), slice(ix - 6, ix + 1))
    peak = lambda r: float(np.max(np.abs(r.u_grids[0][window])))
    peak_none, peak_down, peak_up = peak(res_no_mom), peak(res_down), peak(res_up)
    print(f"[test2] near-field peak |u| at t={res_down.times_s[0]:.0f}s: "
          f"none={peak_none:.6f}  downstream={peak_down:.6f}  upstream={peak_up:.6f} m/s")

    assert peak_down > peak_none, (
        f"injecting momentum downstream must speed the near field up: "
        f"{peak_none:.4f} -> {peak_down:.4f} m/s")
    assert peak_up < peak_none, (
        f"injecting momentum upstream must slow the near field down: "
        f"{peak_none:.4f} -> {peak_up:.4f} m/s")

    # The causal proof: total x-momentum must carry the sign of the injection.
    mx = lambda r: float((r.depth_grids[0] * r.u_grids[0]).sum())
    mx_none, mx_down, mx_up = mx(res_no_mom), mx(res_down), mx(res_up)
    print(f"[test2] total x-momentum: none={mx_none:+.4f} "
          f"downstream={mx_down:+.4f} upstream={mx_up:+.4f}")
    assert mx_down > mx_none > mx_up, (mx_none, mx_down, mx_up)
    assert mx_up < 0.0, (
        f"injecting 8 m/s toward -x left the domain with positive total "
        f"x-momentum ({mx_up:+.4f}) — the direction is not reaching the solver")


# ── Test 3: None/None regression guard — byte-identical hu/hv ───────────────

def test_no_momentum_when_direction_and_v_ms_are_none():
    z = _basin()
    t_s, Q = _hydrograph()
    kwargs = dict(
        elevation_grid=z, dx_m=20.0, dy_m=20.0,
        inflow_x_idx=20, inflow_y_idx=35,
        hydrograph_t_s=t_s, hydrograph_Q_m3s=Q,
        total_duration_s=120.0, save_interval_s=40.0,
        manning_n=0.04, scenario_name="no-momentum-default",
    )
    res_a = run_2d_swe_simulation(**kwargs)
    res_b = run_2d_swe_simulation(**kwargs, hydrograph_v_ms=None, inflow_direction=None)
    for ga, gb in zip(res_a.u_grids, res_b.u_grids):
        assert np.array_equal(ga, gb)
    for ga, gb in zip(res_a.depth_grids, res_b.depth_grids):
        assert np.array_equal(ga, gb)


# ── Test 4: inflows=None is byte-identical to the pre-Part-3 scalar path ────

def test_inflows_none_is_byte_identical_to_scalar_path():
    z = _basin()
    t_s, Q = _hydrograph()
    kwargs = dict(
        elevation_grid=z, dx_m=20.0, dy_m=20.0,
        inflow_x_idx=20, inflow_y_idx=35,
        hydrograph_t_s=t_s, hydrograph_Q_m3s=Q,
        total_duration_s=120.0, save_interval_s=40.0,
        manning_n=0.04, scenario_name="inflows-none",
    )
    res_a = run_2d_swe_simulation(**kwargs)
    res_b = run_2d_swe_simulation(**kwargs, inflows=None)
    assert len(res_a.depth_grids) == len(res_b.depth_grids)
    for ga, gb in zip(res_a.depth_grids, res_b.depth_grids):
        assert np.array_equal(ga, gb)
    for ga, gb in zip(res_a.u_grids, res_b.u_grids):
        assert np.array_equal(ga, gb)
    for ga, gb in zip(res_a.v_grids, res_b.v_grids):
        assert np.array_equal(ga, gb)
    assert res_a.volume_injected_m3 == res_b.volume_injected_m3


# ── Test 5: multi-inflow sums both boundaries correctly ─────────────────────

def test_multi_inflow_sums_mass_from_both_boundaries():
    z = _basin(ny=60, nx=100)
    t1 = np.linspace(0.0, 200.0, 20)
    Q1 = 150.0 * np.exp(-((t1 - 60.0) / 50.0) ** 2)
    t2 = np.linspace(0.0, 200.0, 20)
    Q2 = 90.0 * np.exp(-((t2 - 90.0) / 50.0) ** 2)

    b1 = InflowBoundary(x_idx=15, y_idx=15, hydrograph_t_s=t1, hydrograph_Q_m3s=Q1)
    b2 = InflowBoundary(x_idx=80, y_idx=45, hydrograph_t_s=t2, hydrograph_Q_m3s=Q2)

    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=20.0, dy_m=20.0,
        inflow_x_idx=15, inflow_y_idx=15,   # ignored when inflows is given
        hydrograph_t_s=t1, hydrograph_Q_m3s=Q1,
        total_duration_s=200.0, save_interval_s=50.0,
        manning_n=0.04, scenario_name="multi-inflow",
        inflows=[b1, b2],
    )

    expected = float(np.trapezoid(Q1, t1) + np.trapezoid(Q2, t2))
    print(f"[test5] volume_injected_m3={res.volume_injected_m3:.6e}  "
          f"expected(sum of both hydrographs)={expected:.6e}")
    assert res.volume_injected_m3 == pytest.approx(expected, rel=1e-2)

    final = res.depth_grids[-1]
    wet_near_1 = final[15 - 5:15 + 6, 15 - 5:15 + 6].sum()
    wet_near_2 = final[45 - 5:45 + 6, 80 - 5:80 + 6].sum()
    print(f"[test5] wet depth sum near boundary1={wet_near_1:.4f} "
          f"near boundary2={wet_near_2:.4f}")
    assert wet_near_1 > 0.0, "boundary 1 injected nothing"
    assert wet_near_2 > 0.0, "boundary 2 injected nothing"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
