"""
Solver validation — the benchmarks that actually run the solver.

``test_ritter.py`` checks properties of the *analytical* solution. This file
checks the *numerical* one against it, which is the part that was missing:
the pipeline used to write the analytical curve to ``ritter_validation.json``
and call that validation, while nothing ever compared the two.

Run: pytest tests/test_swe_validation.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.m4_solvers.validation import (          # noqa: E402
    run_ritter_benchmark,
    run_lake_at_rest_benchmark,
    mass_balance,
)
from src.m4_solvers.swe_2d import run_2d_swe_simulation   # noqa: E402


# ── Ritter: does the solver reproduce the analytical dam break? ──────────────

@pytest.fixture(scope="module")
def ritter():
    return run_ritter_benchmark()


def test_ritter_rmse_within_tolerance(ritter):
    """Depth profile must match Ritter within the stated RMSE tolerance."""
    assert ritter.passed, ritter.summary()
    assert ritter.value <= ritter.tolerance


def test_ritter_conserves_reservoir_shape(ritter):
    """Peak depth must not exceed the initial reservoir depth."""
    h_sim = np.array(ritter.detail["h_simulated_m"])
    h1 = ritter.detail["h1_m"]
    assert h_sim.max() <= h1 * 1.02, (
        f"max simulated depth {h_sim.max():.3f} m exceeds initial {h1:.1f} m — "
        "the scheme is creating water"
    )


def test_ritter_front_position_regression(ritter):
    """
    Track wave-front error, which RMSE alone does not catch.

    A diffusive scheme sits comfortably inside the RMSE tolerance while placing
    the front badly — and front position IS arrival time, the headline output
    of this project. Both fronts are measured at the same 5 cm depth so the
    comparison is like-for-like.

    First-order Rusanov measured 26%; MUSCL + SSP-RK2 brings it to ~10%.
    """
    err = ritter.detail["front_relative_error"]
    assert err <= 0.15, (
        f"wave front error {err:.1%} exceeds the 15% regression guard — "
        "the scheme became more diffusive"
    )


# ── Lake at rest: is the scheme well-balanced? ───────────────────────────────

def test_lake_at_rest_is_still():
    """
    Standing water over sloping terrain must stay standing.

    Before the well-balanced rewrite this produced 20 m/s spurious currents —
    saturating the hard velocity clamp that existed to hide it — and a 39 m
    drift in the free surface. The continuous-bed reconstruction now cancels
    exactly, so the measured value is 0.0 m/s.
    """
    ny, nx = 60, 60
    xx, yy = np.meshgrid(np.linspace(0, 1, nx), np.linspace(0, 1, ny))
    z = 100.0 + 40.0 * xx + 15.0 * np.sin(3.0 * yy)     # a tilted, rippled bed

    res = run_lake_at_rest_benchmark(z, dx_m=30.0, dy_m=30.0,
                                     water_level_m=130.0, duration_s=300.0)
    assert res.passed, res.summary()


def test_lake_at_rest_reports_a_number():
    """Whatever the verdict, the benchmark must produce a measurable value."""
    ny, nx = 40, 40
    xx, _ = np.meshgrid(np.linspace(0, 1, nx), np.linspace(0, 1, ny))
    z = 100.0 + 20.0 * xx

    res = run_lake_at_rest_benchmark(z, dx_m=30.0, dy_m=30.0,
                                     water_level_m=112.0, duration_s=120.0)
    assert np.isfinite(res.value)
    assert res.detail["wet_cells"] > 0


# ── Mass balance ─────────────────────────────────────────────────────────────

def test_mass_balance_closes_on_a_closed_domain():
    """
    Injected water must be accounted for.

    The domain is a bowl so nothing escapes through the open boundaries within
    the run, which makes the closure error a direct measure of what the
    positivity clip creates or destroys.
    """
    ny, nx = 50, 50
    cx, cy = nx / 2, ny / 2
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 0.5 * ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5      # a shallow bowl

    t = np.array([0.0, 300.0, 600.0])
    Q = np.array([50.0, 50.0, 0.0])

    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=20.0, dy_m=20.0,
        inflow_x_idx=nx // 2, inflow_y_idx=ny // 2,
        hydrograph_t_s=t, hydrograph_Q_m3s=Q,
        total_duration_s=600.0, save_interval_s=600.0,
        manning_n=0.03, scenario_name="mass_balance_test",
    )

    mb = mass_balance(res, t, Q)
    assert mb["injected_volume_m3"] > 0
    assert mb["relative_error"] < 0.01, (
        f"mass closure error {mb['relative_error']:.2%} — the positivity clip "
        "is creating or destroying water"
    )


def test_initial_depth_is_honoured():
    """An initial condition must actually reach the solver."""
    z = np.zeros((5, 40))
    h0 = np.zeros_like(z)
    h0[:, :20] = 3.0

    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=10.0, dy_m=10.0,
        inflow_x_idx=20, inflow_y_idx=2,
        hydrograph_t_s=np.array([0.0, 100.0]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=1.0, save_interval_s=1.0,
        manning_n=0.0, scenario_name="ic_test",
        initial_depth=h0,
    )
    # Water started on the left, so after 1 s the left half is still wet
    assert res.depth_grids[-1][2, :20].max() > 1.0


def test_initial_depth_shape_is_validated():
    """A mismatched initial condition must fail loudly, not broadcast silently."""
    z = np.zeros((5, 40))
    with pytest.raises(ValueError, match="does not match DEM"):
        run_2d_swe_simulation(
            elevation_grid=z, dx_m=10.0, dy_m=10.0,
            inflow_x_idx=20, inflow_y_idx=2,
            hydrograph_t_s=np.array([0.0, 1.0]),
            hydrograph_Q_m3s=np.array([0.0, 0.0]),
            total_duration_s=1.0, save_interval_s=1.0,
            initial_depth=np.zeros((3, 3)),
        )
