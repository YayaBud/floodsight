"""F-1 and F-3 — the instruments for the E-1/E-2 flux fix.

These are written to FAIL against the scheme as it stands. A green run here
before the fix lands means the test is wrong, not that the defect is gone.

What is being pinned
--------------------
A well-balanced scheme preserves the lake-at-rest state ``eta = h + z = const,
u = v = 0`` EXACTLY, over arbitrary bed topography. `swe_2d._rhs` does not: it
re-derives each cell's bed as the mean of its two interface values, which is
``z + lap(z)/4`` exactly, and it takes the interface bed as the MEAN of the two
adjoining cells rather than the max. So the state is preserved only where the
bed's discrete Laplacian vanishes -- a flat or exactly linear bed -- and a
barrier's crest is averaged away at its own upstream face (audit RC-1, RC-2).

F-3 asserts the property on the REAL operator
---------------------------------------------
`test_f3_*` calls `_rhs` itself and asserts its output is identically zero for a
lake at rest. It deliberately does NOT re-derive `z_bar` and compare: a test that
reproduces the implementation's internals is a copy of the thing it is meant to
check, and stops being able to notice when that thing changes -- the defect
audit Part VI N-6 found in `tests/test_mass_gates.py`. Asserting on `_rhs`'s
output also survives the fix: the correct scheme returns zero for the same
reason, so this file does not need editing when E-1/E-2 lands.

F-1 asserts it end to end, on a BOUNDED lake
--------------------------------------------
The shipped `test_lake_at_rest_is_still` cannot fail: its lake leaves the domain
through the transmissive boundary (0.002 % retained, measured in
`test_lake_at_rest_bounded.py`), so the velocity sample is taken over cells that
have gone dry. Every case here is walled.

Measured before the fix, 2026-09-13 (audit Part VI §55.1, §55.4):
  one-cell wall     bed error 40.00 m,  |dh/dt| 5.38e+01
  one-cell bump     bed error  2.50 m
  phutkal coarsen 1 mean per-axis bed error  2.09 m
  phutkal coarsen 4 mean per-axis bed error 10.40 m
"""

from __future__ import annotations

import numpy as np
import pytest

from src.m4_solvers.swe_2d import run_2d_swe_simulation, _rhs

DX = 25.0
LEVEL = 155.0


def _topographies():
    """Bed shapes a well-balanced scheme must hold a flat lake over.

    The one-cell wall is the shape of a dam; the one-cell notch is the shape of
    a breach opening. Both are exactly what a 28-113 m grid renders a real
    structure as, which is why one-cell features are the cases that matter.
    """
    flat = np.full((12, 16), 100.0)
    lin = np.tile(np.linspace(130.0, 100.0, 16), (12, 1))
    bump = flat.copy();  bump[:, 8] = 105.0
    wall = flat.copy();  wall[:, 8] = 160.0
    notch = flat.copy(); notch[:, 8] = 60.0
    diag = flat.copy()
    for i in range(12):
        diag[i, (i + 3) % 16] = 140.0          # a wall that is not axis-aligned
    return [
        ("flat", flat),
        ("linear", lin),
        ("one-cell bump", bump),
        ("one-cell wall", wall),
        ("one-cell notch", notch),
        ("diagonal wall", diag),
    ]


# ── F-3: the spatial operator must return zero for a lake at rest ────────────

@pytest.mark.parametrize("name,z", _topographies())
def test_f3_rhs_is_identically_zero_for_a_lake_at_rest(name, z):
    """The defining property of a well-balanced scheme, asserted on `_rhs`."""
    h = np.maximum(LEVEL - z, 0.0)
    hu = np.zeros_like(h)
    hv = np.zeros_like(h)

    dh, dhu, dhv, _a, _out, _gross = _rhs(h, hu, hv, z, DX, DX)

    assert np.abs(dh).max() < 1e-9, (
        f"{name}: the scheme moves {np.abs(dh).max():.3e} m/s of water out of a "
        f"lake that is at rest. A flat free surface over any bed must produce "
        f"exactly zero mass flux."
    )
    assert np.abs(dhu).max() < 1e-9, (
        f"{name}: spurious x-momentum {np.abs(dhu).max():.3e} m^2/s^2 in "
        f"standing water — the pressure flux and the bed-slope source are not "
        f"cancelling."
    )
    assert np.abs(dhv).max() < 1e-9, (
        f"{name}: spurious y-momentum {np.abs(dhv).max():.3e} m^2/s^2 in "
        f"standing water."
    )


def test_f3_on_real_conditioned_terrain():
    """The same property on the array the solver actually integrates.

    Run at every coarsening the geometry gate accepts for phutkal (1, 2, 4), so
    a fix that only works on smooth synthetic beds cannot pass.
    """
    rasterio = pytest.importorskip("rasterio")
    from src.data_fetcher import get_dem
    from src.m2_geometry.dem_utils import condition_dem

    dem_path, _ = get_dem("phutkal", allow_synthetic=False)
    with rasterio.open(dem_path) as src:
        raw, nodata = src.read(1), src.nodata
    cond, _ = condition_dem(raw, nodata=nodata)

    failures = []
    for coarsen in (1, 2, 4):
        z = cond[200:280, 120:200][::coarsen, ::coarsen].astype(float)
        level = float(np.percentile(z, 75))
        h = np.maximum(level - z, 0.0)
        if not (h > 0).any():
            continue
        dh, dhu, dhv, _a, _o, _g = _rhs(h, np.zeros_like(h), np.zeros_like(h),
                                        z, 27.9 * coarsen, 27.9 * coarsen)
        worst = max(np.abs(dh).max(), np.abs(dhu).max(), np.abs(dhv).max())
        if worst >= 1e-9:
            failures.append(f"coarsen {coarsen}: max |rhs| = {worst:.3e}")

    assert not failures, (
        "the scheme is not well-balanced on real conditioned terrain:\n  "
        + "\n  ".join(failures)
    )


# ── F-1: a bounded lake stays at rest, end to end ────────────────────────────

def _bounded(inner_z, rim=400.0, wall=3):
    """Wrap `inner_z` in a `wall`-cell rim so the lake cannot leave the domain.

    Three cells, not one: a one-cell rim does not exist to this scheme (RC-3),
    and this fixture has to isolate the free-surface defect from that one.
    """
    ny, nx = inner_z.shape
    z = np.full((ny + 2 * wall, nx + 2 * wall), float(rim))
    z[wall:-wall, wall:-wall] = inner_z
    return z


def _at_rest(z, level, *, duration=300.0, dx=DX):
    """Run standing water and report (eta spread, |v|max, % retained)."""
    h0 = np.maximum(level - z, 0.0)
    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=dx, dy_m=dx,
        inflow_x_idx=z.shape[1] // 2, inflow_y_idx=z.shape[0] // 2,
        hydrograph_t_s=np.array([0.0, duration]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=duration, save_interval_s=duration,
        manning_n=0.0,                 # friction would damp the imbalance away
        scenario_name="f1", initial_depth=h0,
    )
    h1 = res.depth_grids[-1]
    retained = 100.0 * h1.sum() / h0.sum()
    deep = (h0 > 1.0) & (h1 > 1.0)     # a free-surface sample, not a shoreline
    assert deep.any(), "the fixture drained — fix the rim, not the tolerance"
    eta = (z + h1)[deep]
    vmax = float(np.hypot(res.u_grids[-1][deep], res.v_grids[-1][deep]).max())
    return float(eta.max() - eta.min()), vmax, retained


@pytest.mark.parametrize("name,inner", _topographies())
def test_f1_a_bounded_lake_stays_at_rest(name, inner):
    spread, vmax, retained = _at_rest(_bounded(inner), LEVEL)
    assert retained > 99.9, f"{name}: only {retained:.3f} % retained"
    assert spread < 1e-6, f"{name}: free surface drifted {spread:.6f} m"
    assert vmax < 1e-6, f"{name}: |v| = {vmax:.6f} m/s in standing water"


def test_f1_on_real_conditioned_terrain():
    """The end-to-end property on a real DEM patch, walled so it cannot drain."""
    rasterio = pytest.importorskip("rasterio")
    from src.data_fetcher import get_dem
    from src.m2_geometry.dem_utils import condition_dem

    dem_path, _ = get_dem("phutkal", allow_synthetic=False)
    with rasterio.open(dem_path) as src:
        raw, nodata = src.read(1), src.nodata
    cond, _ = condition_dem(raw, nodata=nodata)

    patch = cond[200:250, 120:170].astype(float)
    level = float(np.percentile(patch, 70))
    z = _bounded(patch, rim=float(patch.max()) + 500.0)
    spread, vmax, retained = _at_rest(z, level, dx=27.9)

    assert retained > 99.9, f"only {retained:.3f} % retained"
    assert spread < 1e-6, f"free surface drifted {spread:.6f} m"
    assert vmax < 1e-6, f"|v| = {vmax:.6f} m/s in standing water"
