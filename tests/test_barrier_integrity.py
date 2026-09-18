"""F-2 — a barrier holds at ONE cell thickness.

Before the Audusse reconstruction landed, the interface bed was the mean of the
two adjoining cells, so a one-cell wall had no face anywhere in the domain at
its own crest and simply did not exist to the flux operator. Measured on
2026-09-13 with the pool 5 m BELOW the crest and friction off (audit Part VI
§55.3):

    thickness   on the apron after 300 s      retained in domain
        1            5.3462e+03 m^3                 44.601 %
        2            7.9596e+01 m^3                 98.322 %
        3            1.2106e+02 m^3                 98.734 %
        5            1.3448e+02 m^3                 98.746 %

i.e. a one-cell wall passed 55.4 % of its pool in five minutes, and even a
two-cell wall leaked 1.4e-5 of it. Both numbers were fixture-specific, which is
why this file runs TWO independent fixtures: a flat-floored reservoir and a
sloping one. A barrier's integrity must not depend on what is behind it.

The pool sits 5 m below the crest throughout. Nothing should cross. Not "almost
nothing" -- the water is below the wall, and a well-balanced scheme reproduces
that exactly.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.m4_solvers.swe_2d import run_2d_swe_simulation

DX = 25.0
CREST = 160.0
POOL = 155.0          # 5 m of freeboard
APRON = 60.0
RIM = 300.0
DURATION = 900.0


def _flat_floor(nres, nx):
    return np.full((24, nx), 100.0)


def _sloping_floor(nres, nx):
    """A reservoir floor that falls toward the wall, so `lap(z)` is non-zero
    everywhere behind it rather than only at the wall face."""
    z = np.empty((24, nx))
    z[:] = 100.0
    ramp = np.linspace(130.0, 95.0, nres)
    z[:, :nres] = ramp[None, :]
    return z


def _build(thickness, floor_fn):
    nres, napron = 20, 20
    nx = nres + thickness + napron
    z = floor_fn(nres, nx)
    z[:, nres:nres + thickness] = CREST
    z[:, nres + thickness:] = APRON
    # Seal the pool on its other three sides with a 3-cell rim, so the ONLY way
    # out of the reservoir is across the wall. Three cells, not one: this
    # fixture must not be able to leak through its own container.
    z[:3, :nres + thickness] = RIM
    z[-3:, :nres + thickness] = RIM
    z[:, :3] = RIM
    h0 = np.zeros_like(z)
    inner = (slice(3, -3), slice(3, nres))
    h0[inner] = np.maximum(POOL - z[inner], 0.0)
    return z, h0, nres, thickness


def _volume_past_the_wall(thickness, floor_fn):
    z, h0, nres, th = _build(thickness, floor_fn)
    v0 = float(h0.sum()) * DX * DX
    res = run_2d_swe_simulation(
        elevation_grid=z, dx_m=DX, dy_m=DX,
        inflow_x_idx=5, inflow_y_idx=12,
        hydrograph_t_s=np.array([0.0, DURATION]),
        hydrograph_Q_m3s=np.array([0.0, 0.0]),
        total_duration_s=DURATION, save_interval_s=DURATION,
        manning_n=0.0,                 # friction must not be what holds it back
        scenario_name=f"barrier{th}", initial_depth=h0,
    )
    h1 = res.depth_grids[-1]
    downstream = float(h1[:, nres + th:].sum()) * DX * DX
    retained = 100.0 * float(h1.sum()) * DX * DX / v0
    return downstream, retained, v0


@pytest.mark.parametrize("floor_name,floor_fn", [
    ("flat floor", _flat_floor),
    ("sloping floor", _sloping_floor),
])
@pytest.mark.parametrize("thickness", [1, 2, 3, 5])
def test_f2_a_barrier_holds_at_any_thickness(floor_name, floor_fn, thickness):
    downstream, retained, v0 = _volume_past_the_wall(thickness, floor_fn)
    assert downstream == 0.0, (
        f"{floor_name}, {thickness}-cell wall: {downstream:.4e} m^3 crossed a "
        f"barrier whose crest stands 5 m above the pool, in {DURATION:.0f} s "
        f"with friction off. Pool was {v0:.4e} m^3."
    )
    assert retained > 99.999, (
        f"{floor_name}, {thickness}-cell wall: only {retained:.4f} % of the "
        f"pool is still in the domain — it is leaving somewhere"
    )


def test_f2_one_cell_is_not_a_special_case():
    """The whole point: thickness must not change the answer.

    A scheme whose barriers only hold above some thickness cannot represent a
    30 m dam in a 112 m cell, which is every scenario this project has.
    """
    results = {t: _volume_past_the_wall(t, _flat_floor)[0] for t in (1, 2, 3, 5)}
    assert set(results.values()) == {0.0}, (
        f"barrier integrity depends on thickness: {results}"
    )
