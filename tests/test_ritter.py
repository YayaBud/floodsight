"""
Property tests for the Ritter ANALYTICAL solution and the breach ensemble.
The solver-vs-analytical comparison lives in test_swe_validation.py.
Run: pytest tests/test_ritter.py -v
"""

import numpy as np
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.m4_solvers.ritter import solve, rmse


# ── Ritter solution properties ─────────────────────────────────────────────

def test_ritter_wave_front_speed():
    """Wave front must travel at x = 2*c0*t (Ritter 1892)."""
    h1 = 5.0  # m
    t  = 30.0  # s
    c0 = np.sqrt(9.81 * h1)
    r  = solve(h1=h1, t=t, x_min=-500, x_max=500, n_points=1000)

    # At x = 2*c0*t depth must be ~0
    x_front = 2 * c0 * t
    i_front = np.argmin(np.abs(r.x - x_front))
    assert r.h[i_front] < 0.05, f"Depth at wave front = {r.h[i_front]:.4f} m (expected ~0)"


def test_ritter_upstream_undisturbed():
    """Upstream of the rarefaction the depth must equal h1."""
    h1 = 8.0
    t  = 20.0
    c0 = np.sqrt(9.81 * h1)
    r  = solve(h1=h1, t=t, x_min=-500, x_max=500, n_points=2000)

    mask = r.x < -c0 * t - 5  # safely upstream
    if mask.any():
        assert np.allclose(r.h[mask], h1, atol=1e-6), "Upstream depth should equal h1"


def test_ritter_volume_conservation():
    """
    Total volume under the depth profile must equal the original volume
    (within numerical integration error for the rarefaction zone).
    """
    h1 = 10.0
    t  = 50.0
    c0 = np.sqrt(9.81 * h1)
    # Use a domain large enough to capture the whole rarefaction
    x_min = -c0 * t * 1.2
    x_max =  2 * c0 * t * 1.1
    r = solve(h1=h1, t=t, x_min=x_min, x_max=x_max, n_points=5000)
    dx    = r.x[1] - r.x[0]

    # Numerical volume under profile (rarefaction zone only)
    mask_rare  = (r.x >= -c0 * t) & (r.x <= 2 * c0 * t)
    # np.trapezoid is the NumPy 2.x name (trapz was removed in NumPy 2.0)
    _trapz = getattr(np, "trapezoid", getattr(np, "trapz", None))
    if _trapz is None:
        pytest.skip("No trapezoid/trapz in this NumPy version")
    V_profile  = float(_trapz(r.h[mask_rare], r.x[mask_rare]))

    # Analytical volume in the rarefaction zone:
    # integral from -c0*t to 2*c0*t of (1/9g)(2c0 - x/t)^2 dx
    x_r = r.x[mask_rare]
    h_r = (1 / (9 * 9.81)) * (2 * c0 - x_r / t) ** 2
    V_analytic = float(_trapz(h_r, x_r))

    assert abs(V_profile - V_analytic) / V_analytic < 0.01, (
        f"Volume mismatch: numerical={V_profile:.2f}, analytical={V_analytic:.2f}"
    )


def test_ritter_raises_on_t_zero():
    with pytest.raises(ValueError, match="t must be > 0"):
        solve(h1=5.0, t=0.0)


# ── Breach ensemble round-trip ─────────────────────────────────────────────

def test_breach_ensemble_ordering():
    """Pessimistic Q_p must be >= Central >= Optimistic."""
    from src.m3_breach import DamGeometry
    from src.m3_breach.ensemble import build_ensemble, get_hydrographs

    dam = DamGeometry(height_m=30.0, volume_m3=50e6, dam_height_m=35.0, failure_mode="overtopping")
    ens = build_ensemble(dam)

    assert ens.pessimistic.peak_discharge_m3s >= ens.central.peak_discharge_m3s, (
        "Pessimistic Q_p must be >= central"
    )
    assert ens.central.peak_discharge_m3s >= ens.optimistic.peak_discharge_m3s, (
        "Central Q_p must be >= optimistic"
    )


def test_hydrograph_starts_at_zero():
    """All hydrographs must start with Q=0 at t=0."""
    from src.m3_breach import DamGeometry
    from src.m3_breach.ensemble import get_hydrographs

    dam = DamGeometry(height_m=20.0, volume_m3=100e6, dam_height_m=22.0, failure_mode="piping")
    for hyd in get_hydrographs(dam):
        assert hyd.Q_m3s[0] == pytest.approx(0.0, abs=1.0), (
            f"{hyd.arm} hydrograph Q[0] should be ~0, got {hyd.Q_m3s[0]}"
        )
