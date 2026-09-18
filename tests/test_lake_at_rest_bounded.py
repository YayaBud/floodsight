"""The lake-at-rest benchmark does not test a lake that stays in the domain.

`tests/test_swe_validation.py::test_lake_at_rest_is_still` passes, and its
docstring claims "the measured value is 0.0 m/s". Measured here: on that
benchmark's own terrain, **0.002 % of the lake is still in the domain after
300 s** — the water runs straight off the transmissive boundary, so there is
almost nothing left to be at rest and the velocity sample is taken over cells
that have gone dry.

Bound the same lake with dry terrain — which is what an impoundment behind a
dam actually is — and the scheme generates large currents in standing water:

    case                                        eta spread   |v|max   retained
    A  open-edge lake (the shipped benchmark)          n/a      n/a     0.002 %
    B  same lake, walled                            28.0 m   21.1 m/s  98.945 %
    D  flat bed, walled 15 m proud, 55 m deep       35.4 m    1.8 m/s  98.164 %

(300 s, manning_n = 0 so friction cannot mask the imbalance, 25-30 m cells,
eta and |v| sampled only over cells wet at BOTH ends so a drained shoreline
cannot be mistaken for drift.)

Why this matters more than it looks
-----------------------------------
The forensic audit calls the well-balanced property "what makes this solver
worth keeping" (SS16.1) and makes "the pool holds with Q = 0" the acceptance
test for emplacing the barrier (SS37, P2). Neither survives this measurement:
the pool cannot hold, for reasons upstream of the barrier, the breach and the
initial condition. It is also a sufficient explanation for two defects the
audit attributed elsewhere — the Q = 0 pool spreading from 35 to 195 wet cells
(E5), and the +115 m superelevation (A2).

This file does not fix the scheme. It measures the defect so the next session
starts from a number instead of rediscovering it, and so that a genuine fix
announces itself by making these assertions fail.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.m4_solvers.swe_2d import run_2d_swe_simulation


def _at_rest(z, h0, *, dx=25.0, duration=300.0):
    """Run standing water and report (eta spread, |v|max, % retained)."""
    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=dx, dy_m=dx,
        inflow_x_idx=z.shape[1] // 2, inflow_y_idx=z.shape[0] // 2,
        hydrograph_t_s=np.array([0.0, duration]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=duration, save_interval_s=duration,
        manning_n=0.0,                 # friction would damp the imbalance away
        scenario_name="at_rest", initial_depth=h0,
    )
    h1 = res.depth_grids[-1]
    retained = 100.0 * h1[h0 > 0].sum() / h0.sum()
    deep = (h0 > 1.0) & (h1 > 1.0)     # a real free-surface sample, not a shoreline
    if not deep.any():
        return float("nan"), float("nan"), retained
    eta = (z + h1)[deep]
    vmax = float(np.hypot(res.u_grids[-1][deep], res.v_grids[-1][deep]).max())
    return float(eta.max() - eta.min()), vmax, retained


def _benchmark_terrain():
    """The exact terrain test_lake_at_rest_is_still uses."""
    xx, yy = np.meshgrid(np.linspace(0, 1, 60), np.linspace(0, 1, 60))
    return 100.0 + 40.0 * xx + 15.0 * np.sin(3.0 * yy)


def test_the_shipped_benchmark_lake_now_stays_in_the_domain():
    """Inverted 2026-09-13 — and the reason is the most useful thing this file
    found.

    This used to assert `retained < 1.0`, because the shipped benchmark's lake
    left the domain almost entirely (0.002 % retained in 300 s). That was the
    whole premise of the file: `test_lake_at_rest_is_still` sampled velocity
    over cells that had gone dry, so "nothing moved" was not evidence that
    nothing moves.

    The drain-away turned out to be a SYMPTOM, not the setup. The lake was not
    flowing out through the transmissive boundary under its own weight -- it
    was being pushed off the grid by the spurious currents the unbalanced
    scheme generated (~40 m/s in standing water, measured). With the Audusse
    reconstruction in place the same open-boundary terrain retains 100.000 %:
    edge-replicated ghosts see the same free surface as the interior, so there
    is no gradient and no flux.

    Consequence: `tests/test_swe_validation.py::test_lake_at_rest_is_still` is
    no longer a test that cannot fail. It now has a lake to test, so it was
    kept rather than deleted.
    """
    z = _benchmark_terrain()
    h0 = np.maximum(130.0 - z, 0.0)
    _, _, retained = _at_rest(z, h0, dx=30.0)
    assert retained > 99.999, (
        f"{retained:.3f} % retained — the open-boundary lake is leaving the "
        f"domain again, which is what the unbalanced scheme used to do"
    )


def test_a_bounded_lake_stays_at_rest():
    """FIXED 2026-09-13. This was a strict xfail: standing water developed
    21.1 m/s currents and 28.0 m of free-surface drift in 300 s with friction
    off, because the scheme was well-balanced only where the bed's discrete
    Laplacian vanished. The Audusse hydrostatic reconstruction in
    `swe_2d._rhs` -- interface bed as the MAX, free surface against the cell's
    own bed, face-pair bed-slope source -- removed it exactly. The xfail is
    gone rather than relaxed; the tolerances below are the originals."""
    z = _benchmark_terrain()
    z[:2] = z[-2:] = 200.0
    z[:, :2] = z[:, -2:] = 200.0
    h0 = np.maximum(130.0 - z, 0.0)
    spread, vmax, retained = _at_rest(z, h0, dx=30.0)
    assert retained > 95.0, "sanity: a walled lake must not drain away"
    assert vmax < 1e-3, f"|v| = {vmax:.3f} m/s in standing water"
    assert spread < 1e-3, f"free surface drifted {spread:.2f} m"


def test_the_bounded_lake_defect_is_gone():
    """Inverted 2026-09-13. This used to PIN the defect's magnitude so a partial
    fix would be visible:

        retained ~= 98.16 %,  1.0 < |v|max < 5.0 m/s,  25 < spread < 45 m
        (measured 98.164 %, 1.798 m/s, 35.41 m on 2026-09-12)

    The fix was not partial. The same fixture now holds its lake to machine
    precision, so the assertions are inverted rather than deleted: this fixture
    was the one that caught the defect, and it is the one that should catch a
    regression.
    """
    z = np.full((30, 40), 100.0)
    z[:3] = z[-3:] = 170.0
    z[:, :3] = z[:, -3:] = 170.0
    h0 = np.zeros_like(z)
    h0[3:-3, 3:-3] = 55.0

    spread, vmax, retained = _at_rest(z, h0)
    assert retained > 99.999, (
        f"{retained:.4f} % retained; was 98.164 % when the defect was live"
    )
    assert vmax < 1e-6, (
        f"|v|max = {vmax:.3e} m/s in standing water; was 1.798 m/s on "
        f"2026-09-12. Do NOT loosen this back toward the old range."
    )
    assert spread < 1e-6, (
        f"free-surface drift {spread:.3e} m; was 35.41 m on 2026-09-12"
    )


def test_friction_is_no_longer_what_keeps_the_surface_flat():
    """This used to assert the OPPOSITE: that with realistic friction the free
    surface still drifted more than 1 m, because friction hid the velocity
    without restoring the balance. That was the point -- production runs use
    manning_n ~ 0.03-0.045, which is why the defect went unnoticed for so long.

    Now the balance is exact, so friction has nothing to hide. Inverted
    2026-09-13."""
    z = np.full((30, 40), 100.0)
    z[:3] = z[-3:] = 170.0
    z[:, :3] = z[:, -3:] = 170.0
    h0 = np.zeros_like(z)
    h0[3:-3, 3:-3] = 55.0

    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=25.0, dy_m=25.0, inflow_x_idx=20, inflow_y_idx=15,
        hydrograph_t_s=np.array([0.0, 300.0]), hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=300.0, save_interval_s=300.0, manning_n=0.045,
        scenario_name="at_rest_friction", initial_depth=h0,
    )
    h1 = res.depth_grids[-1]
    deep = (h0 > 1.0) & (h1 > 1.0)
    eta = (z + h1)[deep]
    assert float(eta.max() - eta.min()) < 1e-6, (
        f"free surface drifted {float(eta.max() - eta.min()):.3e} m with "
        f"realistic friction; drifted > 1 m when the defect was live"
    )
