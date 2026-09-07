"""
The active window must be an optimisation, never an approximation.

The solver restricts each step to a box around the wet cells because a dry cell
contributes exactly zero: with hL = hR = 0 the Rusanov flux vanishes and the
bed-slope source carries h_bar = 0. That makes the windowed update *identical*
to the full-domain one, not merely close to it -- so these tests compare the
two paths with `==`, not `approx`. A tolerance here would hide precisely the
kind of drift the window could introduce if its halo were ever too thin.

FLOODSIGHT_FULL_DOMAIN=1 selects the unwindowed path. It exists for this
comparison; if these tests pass, it is dead weight and can go.
"""
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from src.m4_solvers.swe_2d import (
    run_2d_swe_simulation, _active_window, _window_breached, _DRY, _HALO,
)


def sloped_basin(ny=90, nx=110):
    """A tilted channel with a ridge, so the flood forms a diagonal ribbon --
    the geometry where a bounding box is loosest and the halo works hardest."""
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 60.0 - 0.35 * xx - 0.12 * yy
    z += 14.0 * np.exp(-((yy - 0.55 * xx - 8.0) ** 2) / 90.0)
    return z.astype(np.float64)


def hydrograph(peak=900.0, dur=420.0):
    t = np.linspace(0.0, dur, 40)
    return t, peak * np.exp(-((t - 140.0) / 90.0) ** 2)


def run(dur=300.0, **kw):
    z = sloped_basin()
    t_s, q = hydrograph()
    return run_2d_swe_simulation(
        elevation_grid=z, dx_m=25.0, dy_m=25.0,
        inflow_x_idx=12, inflow_y_idx=20,
        hydrograph_t_s=t_s, hydrograph_Q_m3s=q,
        total_duration_s=dur, save_interval_s=dur,
        manning_n=0.04, scenario_name="window-test", **kw)


# --- the window never contains less than it must ----------------------------

def test_window_contains_every_wet_cell_with_halo():
    h = np.zeros((60, 80))
    h[30:34, 40:44] = 1.0
    sy, sx = _active_window(h, seed_iy=30, seed_ix=40)
    assert sy.start <= 30 - _HALO and sy.stop >= 34 + _HALO
    assert sx.start <= 40 - _HALO and sx.stop >= 44 + _HALO


def test_window_always_contains_the_inflow_point():
    """Otherwise the injection kernel is clipped and mass is silently lost."""
    h = np.zeros((60, 80))
    h[5:7, 5:7] = 1.0                       # water far from the inflow
    sy, sx = _active_window(h, seed_iy=50, seed_ix=70)
    assert sy.start <= 50 < sy.stop
    assert sx.start <= 70 < sx.stop


def test_window_on_a_dry_grid_is_still_valid():
    h = np.zeros((60, 80))
    sy, sx = _active_window(h, seed_iy=30, seed_ix=40)
    assert sy.stop > sy.start and sx.stop > sx.start


def test_breach_detector_fires_on_water_at_the_perimeter():
    h = np.zeros((60, 80))
    win = (slice(10, 40), slice(10, 40))
    assert not _window_breached(h, win)
    h[11, 20] = 1.0                          # inside the 2-cell border ring
    assert _window_breached(h, win)


def test_breach_detector_ignores_the_true_domain_edge():
    """At the real boundary the transmissive condition is genuine, not a proxy."""
    h = np.zeros((60, 80))
    h[0, 5] = 1.0
    assert not _window_breached(h, (slice(0, 40), slice(0, 40)))


# --- and the result is bit-identical to the full-domain solve ----------------

@pytest.mark.parametrize("dur", [120.0, 300.0])
def test_windowed_result_is_bit_identical(dur):
    """Run both paths in subprocesses: the flag is read at import time."""
    src = textwrap.dedent(f"""
        import sys, numpy as np
        sys.path.insert(0, r"{os.getcwd()}")
        from tests.test_swe_active_window import run
        r = run(dur={dur})
        md, at = r.max_depth_grid, r.arrival_time_s_grid
        print(repr({{
            "state": (float(md.sum()), float(md.max()), int((md > 0.01).sum()),
                      float(np.nansum(at)), float(r.volume_injected_m3)),
            "ledger": (float(r.volume_outflow_m3), float(r.volume_clipped_m3)),
        }}))
    """)
    out = {}
    for label, flag in (("window", "0"), ("full", "1")):
        env = dict(os.environ, FLOODSIGHT_FULL_DOMAIN=flag,
                   PYTHONPATH=os.getcwd())
        p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                           text=True, env=env, timeout=600)
        assert p.returncode == 0, p.stderr[-2000:]
        out[label] = eval(p.stdout.strip().splitlines()[-1])
    # The simulated state must match exactly -- that is the whole claim.
    assert out["window"]["state"] == out["full"]["state"], (
        "active window changed the answer: windowed %r vs full %r"
        % (out["window"]["state"], out["full"]["state"]))

    # The mass ledger may differ in the last bits, and only there. vol_outflow
    # and vol_clipped are running sums over the boundary faces of whatever grid
    # was solved, so the windowed path accumulates a different (smaller) set of
    # faces in a different order.
    #
    # The tolerance is against INJECTED VOLUME, not against the ledger values
    # themselves. On a flood that never reaches the domain edge both outflow
    # figures are numerical zero -- 6.03e-17 vs 6.25e-17 m3 on this fixture --
    # and a self-relative test would call that a 3% divergence when it is two
    # spellings of nothing. Measured against the 1.4e5 m3 actually injected,
    # every ledger difference here is below 1e-20 relative.
    injected = out["full"]["state"][4]
    for name, w, f in zip(("outflow", "clipped"),
                          out["window"]["ledger"], out["full"]["ledger"]):
        assert abs(w - f) <= 1e-9 * max(injected, 1.0), (
            "%s ledger diverged: %r vs %r (injected %r)" % (name, w, f, injected))


def test_window_actually_shrinks_the_work():
    """Guards against the window silently degrading to the whole domain."""
    r = run(dur=200.0)
    wet = r.max_depth_grid > _DRY
    ys, xs = np.where(wet)
    assert ys.size, "test flood never wetted anything"
    box = (ys.max() - ys.min() + 1) * (xs.max() - xs.min() + 1)
    assert box < 0.6 * r.max_depth_grid.size


def test_mass_is_still_conserved():
    mc = run(dur=300.0).mass_closure()
    assert abs(mc["relative_error"]) < 1e-9
