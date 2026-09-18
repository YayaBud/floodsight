"""The run ends when the flood stops, not when an arbitrary clock runs out.

A fixed `total_duration_s` either truncates a flood that is still moving or
burns wall time integrating still water. With `stop_when_quiescent=True` the
duration becomes a safety cap and the solver decides.

These tests pin the two directions that matter: it must stop when the water
really has settled, and it must NOT stop while water is still being delivered
or is still moving.
"""
from __future__ import annotations

import numpy as np

from src.m4_solvers.swe_2d import run_2d_swe_simulation


def _flat_basin(ny: int = 24, nx: int = 24, wall: float = 40.0):
    """A closed box: water put in it settles and stays."""
    z = np.zeros((ny, nx), dtype=float)
    z[0, :] = z[-1, :] = z[:, 0] = z[:, -1] = wall
    return z


def _still_pool(z, depth=1.0):
    h = np.zeros_like(z)
    h[2:-2, 2:-2] = depth
    return h


def test_settled_water_stops_long_before_the_cap():
    z = _flat_basin()
    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=10.0, dy_m=10.0,
        inflow_x_idx=0, inflow_y_idx=0,
        hydrograph_t_s=np.array([0.0, 10_000.0]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),      # nothing is supplied
        initial_depth=_still_pool(z),
        total_duration_s=10_000.0,                  # the cap
        save_interval_s=100.0, manning_n=0.03,
        stop_when_quiescent=True, quiescent_hold_s=200.0,
        scenario_name="quiescence-settles",
    )
    assert res.stopped_early, "a lake at rest never triggered the quiescence stop"
    assert res.end_time_s < 10_000.0
    # It must not stop before stillness has actually been held.
    assert res.end_time_s >= 200.0
    assert res.times_s[-1] == res.end_time_s


def test_it_does_not_stop_while_water_is_still_arriving():
    """Supply is checked first: a still domain with a hydrograph yet to deliver
    must keep running, or a delayed surge would be cut off before it arrives."""
    z = _flat_basin()
    t_s = np.array([0.0, 400.0, 500.0, 600.0, 3_000.0])
    Q = np.array([0.0, 0.0, 250.0, 0.0, 0.0])       # a pulse, late
    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=10.0, dy_m=10.0,
        inflow_x_idx=12, inflow_y_idx=12,
        hydrograph_t_s=t_s, hydrograph_Q_m3s=Q,
        total_duration_s=3_000.0,
        save_interval_s=100.0, manning_n=0.03,
        stop_when_quiescent=True, quiescent_hold_s=200.0,
        scenario_name="quiescence-late-pulse",
    )
    # The pulse is delivered at t=500 s; stopping before it would lose the flood.
    assert res.end_time_s > 500.0, (
        f"stopped at {res.end_time_s:.0f} s, before the t=500 s inflow pulse")
    assert res.volume_injected_m3 > 0.0


def test_without_the_flag_the_cap_is_still_honoured():
    """Default behaviour is unchanged: run the full window, stopped_early False."""
    z = _flat_basin()
    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=10.0, dy_m=10.0,
        inflow_x_idx=0, inflow_y_idx=0,
        hydrograph_t_s=np.array([0.0, 600.0]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        initial_depth=_still_pool(z),
        total_duration_s=600.0,
        save_interval_s=100.0, manning_n=0.03,
        scenario_name="quiescence-off",
    )
    assert not res.stopped_early
    assert res.end_time_s >= 600.0
