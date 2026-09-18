"""P1 — the three mass gates, and the escape-head search behind G3.

The gate these replace could not fail. `SimulationResult.mass_closure()` forms
``total_in - stored - outflow + clipped`` with ``total_in = initial + injected``
and ``clipped`` added back as accounted mass, so the identity holds for any run
in which water merely stays put — the 2x-water production run, the ``Q = 0``
run and the dry-bed run all scored 0.00000 %.

The archived run ``7d96e858d7c74eefacf2b79edcff9610`` is the fixture: it is the
run that was certified `valid: True` while carrying exactly twice the
impoundment's water. If G1 ever stops failing on it, G1 has become a tautology
again.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.m2_geometry.dem_utils import escape_head_4connected

REPO = Path(__file__).resolve().parents[1]
CERTIFIED_RUN = REPO / "data" / "scenarios" / "7d96e858d7c74eefacf2b79edcff9610"

# THE gates, not copies of them. This file used to carry local re-implementations
# annotated "Must match run_pipeline.execute_full_simulation" -- and they did not
# have to: changing the real gate's tolerance, numerator or sign left every test
# below green, so the suite could not notice a gate that stopped being able to
# fail (audit Part VI N-6). Import, do not restate.
from run_pipeline import (                       # noqa: E402
    gate_g1 as _gate_g1,
    gate_g2 as _gate_g2,
    G1_TOLERANCE,
    G2_TOLERANCE,
)


def test_the_gates_under_test_are_the_pipeline_s_own():
    """Guards the import above. If someone re-inlines a copy, this fails."""
    import run_pipeline
    assert _gate_g1 is run_pipeline.gate_g1
    assert _gate_g2 is run_pipeline.gate_g2
    assert G1_TOLERANCE == run_pipeline.G1_TOLERANCE == 0.05
    assert G2_TOLERANCE == run_pipeline.G2_TOLERANCE == 0.001


# ── G1 ────────────────────────────────────────────────────────────────────────

def test_g1_fails_on_the_certified_double_counted_run():
    """The P1 acceptance test, stated in the audit SS36.

    This run was shipped with `validity.valid == True` and a mass-closure
    relative error of 2.1e-15 while the domain held 2.00x the water the
    impoundment ever contained.
    """
    if not (CERTIFIED_RUN / "manifest.json").is_file():
        pytest.skip("certified archive run not present in this working tree")
    pr = json.loads((CERTIFIED_RUN / "manifest.json").read_text(encoding="utf-8"))["pipeline_result"]
    mb = pr["validation"]["mass_balance"]
    impounded = pr["geometry_diagnostic"]["volume_m3"]

    # The gate it passed under.
    assert pr["validity"]["mass_balance_relative_error"] < 0.01
    assert pr["validity"]["valid"] is True

    ok, ratio = _gate_g1(mb["initial_m3"], mb["injected_m3"], impounded)
    assert not ok, "G1 still passes the certified 2x run — the gate is a tautology again"
    assert ratio == pytest.approx(2.0, abs=0.01), (
        f"expected the measured 2.00x double count, got {ratio:.4f}"
    )


def test_g1_passes_when_the_pool_is_its_own_and_only_supply():
    """What P3 is supposed to produce: the impoundment drains through an
    opening, so `injected` is zero and `initial` is the whole supply."""
    ok, ratio = _gate_g1(initial_m3=1.3038e7, injected_m3=0.0, impounded_m3=1.3038e7)
    assert ok and ratio == pytest.approx(1.0)


def test_g1_fails_a_dry_bed_run_supplied_only_by_injection():
    """The pre-Part-II shape: no initial pool, hydrograph injected. Closure is
    perfect, provenance is not — the reservoir never appears in the domain."""
    ok, _ = _gate_g1(initial_m3=0.0, injected_m3=1.3e7, impounded_m3=1.3038e7)
    assert ok, "an injection-only run of the right volume is provenance-clean"
    ok, ratio = _gate_g1(initial_m3=0.0, injected_m3=3.0e7, impounded_m3=1.3038e7)
    assert not ok and ratio > 2.0


# ── G2 ────────────────────────────────────────────────────────────────────────

def test_g2_treats_clipping_as_failure_not_as_an_accounted_term():
    ok, frac = _gate_g2(clipped_m3=1.0e6, initial_m3=1.3e7, injected_m3=0.0)
    assert not ok and frac > G2_TOLERANCE


def test_g2_passes_the_certified_run_so_g1_is_the_gate_that_bites():
    """Documents which gate actually catches the archived defect: the certified
    run's clipping was 0.055 % — real, but under tolerance. G1 is what fails."""
    if not (CERTIFIED_RUN / "manifest.json").is_file():
        pytest.skip("certified archive run not present in this working tree")
    mb = json.loads((CERTIFIED_RUN / "manifest.json").read_text(encoding="utf-8"))[
        "pipeline_result"]["validation"]["mass_balance"]
    ok, frac = _gate_g2(mb["clipped_m3"], mb["initial_m3"], mb["injected_m3"])
    assert ok and frac == pytest.approx(5.47e-4, rel=0.05)


# ── G3 / escape_head_4connected ───────────────────────────────────────────────

def test_escape_head_is_zero_when_the_release_cell_is_on_the_edge():
    z = np.full((7, 7), 100.0)
    assert escape_head_4connected(z, 0, 3) == pytest.approx(100.0)


def test_escape_head_measures_the_bottleneck_of_a_sealed_bowl():
    z = np.full((9, 9), 100.0)
    z[4, 4] = 90.0
    assert escape_head_4connected(z, 4, 4) - z[4, 4] == pytest.approx(10.0)


def test_escape_head_finds_a_face_connected_slot():
    z = np.full((9, 9), 100.0)
    z[4, 4] = 90.0
    z[4, 5:] = 90.0                       # a face-connected channel to the edge
    assert escape_head_4connected(z, 4, 4) - z[4, 4] == pytest.approx(0.0)


def test_escape_head_refuses_a_corner_only_path():
    """The whole reason this is 4-connected: a staircase joined only at cell
    CORNERS carries no flux in a finite-volume scheme, so it is not an outlet.
    An 8-connected search would report 0 m of head here and certify a sealed
    domain as drained."""
    z = np.full((9, 9), 100.0)
    z[4, 4] = 90.0
    for k in range(5, 9):
        z[k, k] = 90.0                    # diagonal staircase to the corner
    assert escape_head_4connected(z, 4, 4) - z[4, 4] == pytest.approx(10.0)


def test_escape_head_returns_inf_when_terrain_is_unreachable():
    z = np.full((9, 9), np.nan)
    z[4, 4] = 90.0
    assert escape_head_4connected(z, 4, 4) == float("inf")


def test_escape_head_rejects_a_release_cell_off_the_grid():
    with pytest.raises(ValueError):
        escape_head_4connected(np.zeros((5, 5)), 9, 0)


def test_g3_threshold_is_the_impoundment_head_not_a_magic_number():
    """Part II's measured before/after, checked against the rule the gate uses:
    a domain is sealed for an event when the water must pond deeper than the
    whole impoundment before any of it can leave."""
    dam_height_m = 58.0                                   # phutkal
    assert not (1572.5 < dam_height_m), "pre-Part-II sealed domain must fail G3"
    assert 36.5 < dam_height_m, "post-Part-II phutkal must pass G3"
    assert 6.6 < 70.0, "post-Part-II rishiganga must pass G3"
